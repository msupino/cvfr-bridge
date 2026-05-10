#!/usr/bin/env python3
"""
cvfr-bridge / fake (dev simulator).

A schema-driven dev backend that serves the cvfr-bridge JSON shape on
the same port as the real backends, using a synthetic in-flight
aircraft. Use it to develop, tweak, and demo the cvfr-map web UI when
X-Plane (and the real Python/C bridges) aren't available.

Like python/cvfrmap-bridge.py, this script reads ../schema.json at
startup as the single source of truth: port, endpoint, field order,
and per-field fallback values all come from there. Adding a field to
schema.json automatically adds it to the fake's output (with its
schema fallback) without a code change here.

A small handful of fields are then overridden on every request with
synthesized "in flight" values so the six-pack gauges actually move:

  - latitude/longitude orbit the schema fallback at standard rate
    (3 deg/s right turn = rate-1, ~0.02 deg radius, ~120 s/orbit)
  - heading leads the orbit angle by 90 deg so the nose tracks the
    direction of travel (HSI spins with the aircraft)
  - altitude is steady at 2500 ft (level turn, no climb/descent)
  - vsi is 0 (level flight)
  - pitch is 0 (level flight; a real coordinated turn would carry a
    couple of degrees nose-up but we keep it simple)
  - roll is a steady 15 deg right bank (the bank that produces a
    rate-1 turn at ~100 kt - matches the orbit rate above)
  - ias is a steady 100 kt
  - sim_ready is always true (this is a fake sim - it's always live)

Stdlib only, no X-Plane required. Not a real backend - intentionally
NOT listed in the README's "two backends, same wire format" table.
"""

from __future__ import annotations

import argparse
import json
import math
import signal
import socketserver
import sys
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

DEFAULT_SCHEMA = Path(__file__).resolve().parent.parent / "schema.json"

# Animated demo flight constants. Kept at module scope so they show up
# in the startup banner and are easy to tweak without hunting through
# the request handler. The vsi peak is the analytical derivative of
# the altitude sinusoid, so non-zero ALT_AMPLITUDE_FT will automatically
# drive a non-zero VSI swing - keep them coherent.
ORBIT_RADIUS_DEG = 0.02       # ~1.2 NM at LLBG (Tel Aviv) latitude
ORBIT_RATE_DEG_S = 3.0        # rate-1 (standard rate), 120 s/orbit
BANK_DEG = 15.0               # the bank that yields rate-1 at ~100 kt
ALT_CENTER_FT = 2500.0
ALT_AMPLITUDE_FT = 0.0        # 0 -> level turn; bump for an oscillation
ALT_PERIOD_S = 50.0
PITCH_AMPLITUDE_DEG = 0.0     # 0 -> nose level; couples to ALT_AMPLITUDE
IAS_KT = 90.0


def load_schema(path: Path) -> dict:
    with path.open() as f:
        return json.load(f)


def base_snapshot(fields: list[dict]) -> "OrderedDict[str, Any]":
    """Build the initial snapshot: every schema field at its fallback,
    in schema declaration order. Re-built per request so a hot edit of
    schema.json + restart is the only thing needed to add a field."""
    snap: OrderedDict[str, Any] = OrderedDict()
    for f in fields:
        snap[f["name"]] = f["fallback"]
    return snap


def animate(snap: "OrderedDict[str, Any]", t: float, lat0: float, lon0: float) -> None:
    """Overlay synthesized flight values onto the schema-default
    snapshot. Only touches fields that participate in the demo orbit;
    everything else stays at its schema fallback so new schema fields
    keep working without a change here."""
    theta_deg = (ORBIT_RATE_DEG_S * t) % 360.0
    theta = math.radians(theta_deg)

    # Heading derived from the orbit position so the HSI tracks the
    # aircraft. Right turn means heading leads the position angle by
    # 90 deg: at theta=0 (north of centre) heading=090 (eastbound).
    heading = (theta_deg + 90.0) % 360.0

    if ALT_AMPLITUDE_FT != 0.0 and ALT_PERIOD_S > 0.0:
        omega = 2.0 * math.pi / ALT_PERIOD_S
        alt = ALT_CENTER_FT + ALT_AMPLITUDE_FT * math.sin(omega * t)
        vsi_fpm = ALT_AMPLITUDE_FT * omega * math.cos(omega * t) * 60.0
        pitch_norm = math.cos(omega * t)
    else:
        alt = ALT_CENTER_FT
        vsi_fpm = 0.0
        pitch_norm = 0.0

    if "latitude" in snap:
        snap["latitude"] = round(lat0 + ORBIT_RADIUS_DEG * math.cos(theta), 6)
    if "longitude" in snap:
        snap["longitude"] = round(lon0 + ORBIT_RADIUS_DEG * math.sin(theta), 6)
    if "altitude" in snap:
        snap["altitude"] = int(round(alt))
    if "heading" in snap:
        snap["heading"] = round(heading, 1)
    if "pitch" in snap:
        snap["pitch"] = round(PITCH_AMPLITUDE_DEG * pitch_norm, 2)
    if "roll" in snap:
        snap["roll"] = round(BANK_DEG, 2)
    if "vsi" in snap:
        snap["vsi"] = int(round(vsi_fpm))
    if "ias" in snap:
        snap["ias"] = round(IAS_KT, 1)
    if "sim_ready" in snap:
        snap["sim_ready"] = True


def make_handler(schema: dict, t0: float) -> type:
    """Build a request-handler class closed over the loaded schema and
    a single shared start time. Every GET re-derives the snapshot from
    schema.json + the elapsed time since t0 - no per-request state."""
    endpoint = schema["endpoint"]
    fields = schema["fields"]
    by_name = {f["name"]: f for f in fields}
    lat0 = float(by_name["latitude"]["fallback"]) if "latitude" in by_name else 0.0
    lon0 = float(by_name["longitude"]["fallback"]) if "longitude" in by_name else 0.0

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != endpoint:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            snap = base_snapshot(fields)
            animate(snap, time.monotonic() - t0, lat0, lon0)
            body = json.dumps(snap).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: Any, **_kwargs: Any) -> None:
            # Per-request access logs would otherwise drown the terminal
            # at the cvfr-map polling rate. Match the real Python bridge.
            pass

    return Handler


def banner(schema: dict, host: str, port: int) -> None:
    version = schema.get("version", "0.0.0")
    by_name = {f["name"]: f for f in schema["fields"]}
    lat0 = float(by_name.get("latitude", {}).get("fallback", 0.0))
    lon0 = float(by_name.get("longitude", {}).get("fallback", 0.0))
    if ALT_AMPLITUDE_FT != 0.0:
        alt_desc = f"~{int(ALT_CENTER_FT)} ft \u00b1 {int(ALT_AMPLITUDE_FT)} ft"
    else:
        alt_desc = f"level @ {int(ALT_CENTER_FT)} ft"
    print("cvfr-bridge / fake (dev simulator)")
    print(f"  schema  : schema.json v{version}")
    print(f"  serving : http://{host}:{port}/")
    print(
        f"  orbit   : {ORBIT_RADIUS_DEG}\u00b0 around "
        f"{lat0:.4f},{lon0:.4f} @ {ORBIT_RATE_DEG_S}\u00b0/s "
        f"(rate-1, {BANK_DEG:.0f}\u00b0 bank), {alt_desc}"
    )
    print("Ctrl-C to stop.")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Schema-driven fake cvfr-bridge for cvfr-map web-UI dev.",
    )
    p.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA,
                   help=f"Path to schema.json (default: {DEFAULT_SCHEMA})")
    p.add_argument("--port", type=int, default=None,
                   help="Override the schema's port")
    p.add_argument("--bind", default="0.0.0.0",
                   help="Bind address (default: 0.0.0.0)")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress the startup banner")
    args = p.parse_args(argv)

    schema = load_schema(args.schema)
    port = args.port if args.port is not None else int(schema["port"])

    socketserver.TCPServer.allow_reuse_address = True
    handler_cls = make_handler(schema, t0=time.monotonic())
    server = socketserver.TCPServer((args.bind, port), handler_cls)

    def _shutdown(_sig: int, _frame: Any) -> None:
        server.server_close()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    if not args.quiet:
        banner(schema, args.bind, port)

    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
