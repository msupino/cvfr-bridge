#!/usr/bin/env python3
"""
cvfr-bridge / Python flavor.

Polls X-Plane via UDP (RPOS for position + heading/pitch/roll, RREF for
ground-truth extras like indicated airspeed, magnetic variation, wind,
QNH) and serves the merged snapshot as JSON on http://localhost:2020/.

Drop-in compatible with the C plugin (cvfr-bridge.xpl) - same JSON
schema, same port, same CORS behaviour. The C plugin is preferred when
you have X-Plane and can install plugins; this Python script is the
fallback when you can't, want zero build steps, or are talking to a
remote X-Plane via the network.

Requires X-Plane's UDP networking enabled in:
    Settings > Network > Receive External Datarefs (and IP/Port that
    matches XP_HOST/XP_PORT below; defaults are localhost:49000).
"""

from __future__ import annotations

import json
import signal
import socket
import struct
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

XP_HOST = "127.0.0.1"
XP_PORT = 49000
RPOS_HZ = 5     # X-Plane RPOS broadcast rate
RREF_HZ = 5     # X-Plane RREF broadcast rate per dataref

# ---------------------------------------------------------------------
# Schema-driven setup. Both cvfr-bridge backends (this script and the
# C plugin under c/) consume ../schema.json as the single source of
# truth for the JSON wire format. Everything below derives from it:
# the LLBG fallback dict, the RREF subscriptions, and (via field-order
# preservation) the JSON serialization.
# ---------------------------------------------------------------------
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.json"
with SCHEMA_PATH.open() as _f:
    SCHEMA = json.load(_f)

BRIDGE_PORT = SCHEMA["port"]
SCHEMA_VERSION = SCHEMA.get("version", "0.0.0")

# Build the per-field config tables from schema.json. Each field has a
# source kind ("rpos" | "rref" | "computed") plus the bits each kind
# needs (offset+ctype for RPOS, dataref path for RREF, expression for
# computed). We only act on rref + the standard rpos/computed shape;
# the rpos parser in parse_rpos() and the computed expressions in
# xplane_reader() are still hand-coded for clarity.
LLBG: dict = {f["name"]: f["fallback"] for f in SCHEMA["fields"]}
LLBG["sim_ready"] = False  # always false at startup regardless of schema fallback

# RREF subscriptions: dataref path -> snapshot field name. We assign
# RREF indices in declaration order. Keep this stable across runs;
# X-Plane caches the subscription table per source IP:port.
RREF_FIELDS: list[tuple[int, str, str]] = [   # (rref_index, dataref_path, field_name)
    (i + 1, f["source"]["dataref"], f["name"])
    for i, f in enumerate(SCHEMA["fields"])
    if f["source"]["kind"] == "rref"
]
RREF_KEY: dict[int, str] = {idx: name for idx, _, name in RREF_FIELDS}

aircraft: dict = dict(LLBG)
aircraft_lock = threading.Lock()
sim_connected = False


def local_ip() -> str:
    """Best-guess outbound IP (the address an iPad on the LAN should hit)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    finally:
        s.close()


def m_to_ft(m: float) -> int:
    return int(round(m * 3.28084))


def subscribe_rref(sock: socket.socket) -> None:
    """Register all RREF subscriptions. Must be re-sent if X-Plane forgets
    them (e.g. after a sim restart) - we just re-send unconditionally
    every iteration of the reader loop, idempotent."""
    for idx, dref, _name in RREF_FIELDS:
        # RREF packet: 'RREF\0' + freq(int) + idx(int) + dref(400 bytes,
        # null-padded). 5 + 4 + 4 + 400 = 413 bytes total.
        pkt = (
            b"RREF\x00"
            + struct.pack("<ii", RREF_HZ, idx)
            + dref.encode("ascii").ljust(400, b"\x00")
        )
        sock.sendto(pkt, (XP_HOST, XP_PORT))


def subscribe_rpos(sock: socket.socket) -> None:
    """RPOS streams position+attitude+velocities at the requested rate
    until we send rate=0 to stop it."""
    sock.sendto(b"RPOS\x00" + struct.pack("B", RPOS_HZ), (XP_HOST, XP_PORT))


def parse_rpos(data: bytes) -> dict | None:
    """Parse one RPOS packet body. Returns the position-side fields of
    the snapshot dict, or None if the packet is malformed.

    X-Plane RPOS layout (after 5-byte 'RPOS\\0' prefix):
        offset 5:  longitude    (double, deg)
        offset 13: latitude     (double, deg)
        offset 21: elevation    (double, m MSL)
        offset 29: height_agl   (float,  m)
        offset 33: pitch        (float,  deg, nose-up positive)
        offset 37: heading      (float,  deg TRUE)
        offset 41: roll         (float,  deg, right-wing-down positive)
        offset 45: vx, vy, vz   (3 floats, m/s in local frame)
        offset 57: P, Q, R      (3 floats, deg/s body angular rates)
    """
    if len(data) < 45 or data[:4] != b"RPOS":
        return None
    try:
        lon, lat, alt_m = struct.unpack_from("<ddd", data, 5)
        # Note: the original (broken) script read offset 33 as "speed"
        # and offset 37 as "heading". Offset 33 is actually pitch and
        # the heading at offset 37 is TRUE not magnetic; both bugs are
        # fixed here. IAS now comes from RREF, magnetic heading is
        # computed from true-heading minus variation.
        height_agl_m, pitch, hdg_true, roll = struct.unpack_from(
            "<ffff", data, 29
        )
    except struct.error:
        return None
    return {
        "_lat": lat,
        "_lon": lon,
        "_alt_m": alt_m,
        "_pitch": pitch,
        "_hdg_true": hdg_true,
        "_roll": roll,
    }


def parse_rref(data: bytes) -> dict[int, float]:
    """Parse one RREF response packet into {index: value} pairs. X-Plane
    packs as many (idx:int, value:float) tuples as fit in the datagram
    after the 5-byte 'RREF,\\0' prefix. Each tuple is 8 bytes."""
    if len(data) < 13 or data[:4] != b"RREF":
        return {}
    out: dict[int, float] = {}
    pos = 5
    while pos + 8 <= len(data):
        idx, val = struct.unpack_from("<if", data, pos)
        out[idx] = val
        pos += 8
    return out


def xplane_reader() -> None:
    """Single thread that owns the UDP socket: subscribes, receives,
    updates the shared aircraft dict. Reconnects on timeout/exception."""
    global sim_connected
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(2.0)
    last_subscribe = 0.0
    last_rpos = 0.0
    while True:
        try:
            now = time.monotonic()
            # Re-send subscriptions every ~10 s. Cheap insurance against
            # X-Plane having forgotten them across a sim restart.
            if now - last_subscribe > 10.0:
                subscribe_rpos(sock)
                subscribe_rref(sock)
                last_subscribe = now

            data, _src = sock.recvfrom(4096)
            head = data[:4]
            if head == b"RPOS":
                p = parse_rpos(data)
                if p is None:
                    continue
                last_rpos = now
                update = {
                    "latitude": round(p["_lat"], 6),
                    "longitude": round(p["_lon"], 6),
                    "altitude": m_to_ft(p["_alt_m"]),
                    "pitch": round(p["_pitch"], 2),
                    "roll": round(p["_roll"], 2),
                    "_hdg_true": p["_hdg_true"],   # used to compute heading mag
                }
                with aircraft_lock:
                    if p["_lat"] == 0.0 and p["_lon"] == 0.0:
                        aircraft.update(LLBG)
                        aircraft["sim_ready"] = False
                    else:
                        aircraft.update(update)
                        # Compute magnetic heading: true minus E-positive variation
                        var = aircraft.get("variation", 0.0)
                        aircraft["heading"] = round(
                            (p["_hdg_true"] - var) % 360.0, 1
                        )
                        aircraft["sim_ready"] = True
                if not sim_connected:
                    sim_connected = True
                    print("  X-Plane connected")
            elif head == b"RREF":
                vals = parse_rref(data)
                if not vals:
                    continue
                with aircraft_lock:
                    for idx, val in vals.items():
                        key = RREF_KEY.get(idx)
                        if key is None:
                            continue
                        if key == "ias":
                            aircraft["ias"] = round(abs(val), 1)
                        elif key == "vsi":
                            aircraft["vsi"] = int(round(val))
                        elif key in ("wind_speed", "wind_dir"):
                            aircraft[key] = round(val, 1) if key == "wind_speed" else round(val, 0)
                        elif key == "qnh":
                            aircraft["qnh"] = round(val, 2)
                        elif key == "variation":
                            aircraft["variation"] = round(val, 1)
        except socket.timeout:
            if sim_connected:
                sim_connected = False
                print("  X-Plane disconnected - falling back to LLBG")
            with aircraft_lock:
                aircraft.update(LLBG)
        except Exception as e:
            print(f"  reader error: {e}", file=sys.stderr)
            time.sleep(1)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        with aircraft_lock:
            # Strip the leading-underscore "private" fields (the raw
            # true-heading we use to compute the magnetic one).
            body = json.dumps(
                {k: v for k, v in aircraft.items() if not k.startswith("_")}
            ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        with aircraft_lock:
            a = aircraft.copy()
        print(
            f"  map req lat={a['latitude']:.4f} lon={a['longitude']:.4f} "
            f"hdg={a['heading']} alt={a['altitude']}ft ias={a['ias']}kt "
            f"[{self.client_address[0]}]"
        )


def on_exit(sig, frame) -> None:
    print("\nStopped.")
    sys.exit(0)


def main() -> None:
    signal.signal(signal.SIGINT, on_exit)
    ip = local_ip()
    threading.Thread(target=xplane_reader, daemon=True).start()
    print(f"cvfr-bridge / Python flavor (schema v{SCHEMA_VERSION})")
    print(f"  iPad/browser IP : {ip}")
    print(f"  Listening on    : http://{ip}:{BRIDGE_PORT}")
    print(f"  X-Plane         : {XP_HOST}:{XP_PORT} (waiting for RPOS/RREF)")
    print(f"  RREF subscribed : {len(RREF_FIELDS)} datarefs")
    print()
    try:
        HTTPServer(("0.0.0.0", BRIDGE_PORT), Handler).serve_forever()
    except OSError as e:
        if e.errno == 48:
            print(
                f"  Port {BRIDGE_PORT} already in use - is the C plugin "
                f"(cvfr-bridge.xpl) loaded by X-Plane?\n"
                f"  Stop one of them; you only need ONE backend serving the JSON.",
                file=sys.stderr,
            )
            sys.exit(1)
        raise


if __name__ == "__main__":
    main()
