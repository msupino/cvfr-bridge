# cvfr-bridge / Python flavor

External Python script that polls X-Plane via UDP and serves the same JSON as the C plugin on `http://localhost:2020/`. Use this when you can't (or don't want to) install the [C plugin](../c/), or when X-Plane is running on a different machine from the bridge.

## When to pick this over the C plugin

- **Remote X-Plane**: bridge runs on machine A, X-Plane on machine B (or in a VM, container, etc.)
- **No plugin install allowed**: shared sim rigs where you can't drop files into `Resources/plugins/`
- **Quick experiments**: edit the .py and re-run, no build cycle
- **Other sims**: can be adapted to anything that speaks the X-Plane UDP protocol via a shim

For the typical "Mac + local X-Plane" setup, the [C plugin](../c/) is the better default (~5× faster updates, auto-start/stop with the sim, no separate process).

## Run

```bash
$ python3 cvfrmap-bridge.py
cvfr-bridge / Python flavor
  iPad/browser IP : 192.168.1.42
  Listening on    : http://192.168.1.42:2020
  X-Plane         : 127.0.0.1:49000 (waiting for RPOS/RREF)

  X-Plane connected
  map req lat=32.0055 lon=34.8854 hdg=4.6 alt=135ft ias=0.0kt [192.168.1.50]
```

## Prerequisites

- **Python 3.10+** (uses `dict | None` annotations and `from __future__ import annotations`)
- **X-Plane 12 with UDP networking enabled**:
  - `Settings → Network → Receive External Datarefs`: ON
  - Port `49000` (default; change `XP_PORT` in the script if you customised this)

The script uses only the standard library (`socket`, `struct`, `http.server`, `threading`) — **no `pip install` needed**.

## Configuring for a remote X-Plane

Edit the constants at the top of `cvfrmap-bridge.py`:

```python
XP_HOST = "192.168.1.100"   # X-Plane's IP (default: 127.0.0.1)
XP_PORT = 49000             # X-Plane's UDP receive port
BRIDGE_PORT = 2020          # HTTP port we serve on
RPOS_HZ = 5                 # X-Plane RPOS broadcast rate
RREF_HZ = 5                 # X-Plane RREF broadcast rate per dataref
```

If X-Plane is on another machine, ensure that machine's firewall lets UDP `49000` in.

## JSON schema

Identical to the C plugin — see the [top-level README](../README.md#json-schema-both-backends) for the field table.

## How the data gets in

The script juggles two X-Plane UDP protocols simultaneously:

### `RPOS` — position, attitude, velocities (one packet per ~200 ms)

Bridge subscribes once with `b"RPOS\0" + chr(rate_hz)` to `localhost:49000`. X-Plane then streams binary RPOS packets containing:

| offset | type | field | unit |
|---|---|---|---|
| 5  | double | longitude | deg |
| 13 | double | latitude  | deg |
| 21 | double | elevation | m MSL |
| 29 | float  | height_agl | m |
| 33 | float  | pitch | deg, nose-up positive |
| 37 | float  | heading | deg **TRUE** |
| 41 | float  | roll | deg, right-wing-down positive |
| 45 | 3×float | vx, vy, vz | m/s, local frame |
| 57 | 3×float | P, Q, R | deg/s, body angular rates |

The bridge converts heading from true → magnetic by subtracting the magnetic variation it gets from `RREF` (see below).

### `RREF` — arbitrary datarefs the bridge needs (one stream per dataref)

For things RPOS doesn't include (IAS, VSI, wind, QNH, magnetic variation), the bridge sends one `RREF` subscription per dataref. Each subscription packet is `b"RREF\0" + freq(int) + idx(int) + path(400 bytes null-padded)`. X-Plane echoes the per-update value as `b"RREF\0" + tuples_of_(idx, value_float)`.

Subscribed datarefs:

| idx | dataref | unit |
|---|---|---|
| 1 | `sim/flightmodel/position/magnetic_variation` | deg |
| 2 | `sim/flightmodel/position/indicated_airspeed` | KIAS |
| 3 | `sim/flightmodel/position/vh_ind_fpm` | fpm |
| 4 | `sim/cockpit2/gauges/indicators/wind_speed_kts` | kt |
| 5 | `sim/cockpit2/gauges/indicators/wind_heading_deg_mag` | deg mag |
| 6 | `sim/cockpit2/gauges/actuators/barometer_setting_in_hg_pilot` | inHg |

## Resilience

- **2-second socket timeout**: if X-Plane stops responding (sim quit, paused without RPOS, network glitch), the reader catches the exception, marks `sim_connected = False`, falls back to LLBG, and retries.
- **Re-subscribe every 10 s**: cheap insurance against X-Plane having forgotten the subscriptions across a sim restart.
- **Single-instance check**: catches `EADDRINUSE` on port 2020 and tells you the C plugin is probably already serving (you only need one backend running).

## Bugs fixed vs the original `cvfrmap-bridge.py`

The pre-rewrite version had two real bugs:

1. **Offset 33 was being read as `spd_ms` (ground speed in m/s)**, then converted to KIAS via `× 1.94384`. Offset 33 is actually **pitch in degrees** (per the X-Plane RPOS spec). The "IAS" field on the iPad would show `pitch × 1.94`, which is meaningless: at 0° pitch you'd see 0 kt regardless of actual airspeed; at 5° pitch you'd see "9.7 kt". This rewrite reads IAS from `RREF` (`sim/flightmodel/position/indicated_airspeed`) so the value is correct.

2. **The `heading` field was true heading**, not magnetic. The iPad map probably expects magnetic (matching what a pilot sees on the compass). With Israel's ~+4.7° E variation, the displayed track was off by ~5°. The rewrite computes `heading = (true_heading - variation) % 360.0` after both RPOS and the magnetic variation RREF have been received.

These bugs were masked by the iPad app's loose tolerance for jittery position fixes — heading was off by ~5° but you might never have noticed in normal flight.

## Run as a launch-on-boot daemon

If you want the Python bridge to start automatically (instead of running it manually each session), wrap it in a `launchd` plist or just have it auto-start in your shell profile. Example launchd:

```xml
<!-- ~/Library/LaunchAgents/cvfr-bridge.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key>            <string>cvfr-bridge</string>
  <key>ProgramArguments</key> <array>
    <string>/usr/bin/python3</string>
    <string>/Users/you/cvfr-bridge/python/cvfrmap-bridge.py</string>
  </array>
  <key>RunAtLoad</key>        <true/>
  <key>KeepAlive</key>        <true/>
  <key>StandardOutPath</key>  <string>/tmp/cvfr-bridge.log</string>
  <key>StandardErrorPath</key><string>/tmp/cvfr-bridge.err</string>
</dict></plist>
```

Then: `launchctl load ~/Library/LaunchAgents/cvfr-bridge.plist`.

For most users it's simpler to just run it manually when needed, or via XLauncher's "scripts" feature if you use [XLauncher](https://github.com/...) to manage X-Plane sessions.
