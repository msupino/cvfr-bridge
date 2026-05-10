# cvfr-bridge / C plugin (preferred backend)

X-Plane plugin (`cvfr-bridge.xpl`) that serves the current aircraft pose as JSON over HTTP on port `2020`. The preferred backend when X-Plane is running on the same machine — lives inside X-Plane, ~10× faster updates than the [Python flavor](../python/), no separate process.

## Why this instead of the [Python bridge](../python/)

| | [Python bridge](../python/) | This plugin |
|---|---|---|
| Process model | Separate `python3` process | Lives inside X-Plane |
| How data gets out of X-Plane | UDP `RPOS` + `RREF` to localhost:49000 | Direct dataref reads via XPLM |
| Update rate to iPad/web app | ~5 Hz | ~10 Hz |
| Start/stop | XLauncher script or manual `python3 ...` | Automatic with X-Plane |
| Failure modes | Bridge can die without the sim noticing | Lives or dies with X-Plane |
| `python3` dependency | Required | None |
| Cross-machine support | Yes (set `XP_HOST` to remote sim's IP) | No (in-process only) |

## JSON shape

```bash
$ curl http://localhost:2020/
{"latitude":32.0055,"longitude":34.8854,"altitude":135,"heading":0.0,
 "variation":4.7,"pitch":0.00,"roll":0.00,"ias":0.0,"vsi":0,
 "wind_dir":270,"wind_speed":8.5,"qnh":29.92,"sim_ready":true}
```

| field | unit | dataref | notes |
|---|---|---|---|
| `latitude`   | deg WGS84  | `sim/flightmodel/position/latitude` | |
| `longitude`  | deg WGS84  | `sim/flightmodel/position/longitude` | |
| `altitude`   | feet MSL   | `sim/flightmodel/position/elevation` | meters → feet |
| `heading`    | mag deg    | `sim/flightmodel/position/mag_psi` | what the compass reads |
| `variation`  | deg        | `sim/flightmodel/position/magnetic_variation` | E positive |
| `pitch`      | deg        | `sim/flightmodel/position/theta` | nose-up positive |
| `roll`       | deg        | `sim/flightmodel/position/phi` | right-wing-down positive |
| `ias`        | KIAS       | `sim/flightmodel/position/indicated_airspeed` | |
| `vsi`        | fpm        | `sim/flightmodel/position/vh_ind_fpm` | |
| `wind_dir`   | mag deg    | `sim/cockpit2/gauges/indicators/wind_heading_deg_mag` | wind FROM |
| `wind_speed` | kt         | `sim/cockpit2/gauges/indicators/wind_speed_kts` | |
| `qnh`        | inHg       | `sim/cockpit2/gauges/actuators/barometer_setting_in_hg_pilot` | pilot's altimeter setting |
| `sim_ready`  | bool       | computed | `false` if the sim hasn't placed the aircraft yet (`lat == lon == 0`) |

When `sim_ready` is false, position fields fall back to LLBG (Ben Gurion) so the iPad map shows something sensible rather than an aircraft icon at the equator/prime meridian.

## Build

Prerequisite: [X-Plane SDK](https://developer.x-plane.com/sdk/plugin-sdk-downloads/), unzipped somewhere. CMake locates it via `SDK_XPLANE` (defaults to `~/supino/XPlaneSDK/SDK`).

```bash
cd ~/x-plane-utils/cvfr-bridge
./build.sh
```

The script auto-detects your X-Plane install (defaults to `~/X-Plane 12`; override with `XPLANE=...`), builds a universal `arm64+x86_64` `.xpl`, and installs it to `<X-Plane>/Resources/plugins/cvfr-bridge/mac_x64/cvfr-bridge.xpl`.

For a clean rebuild: `./build.sh clean`.

Manual build (without the convenience script):

```bash
cmake -B build -DSDK_XPLANE=/path/to/SDK -DCMAKE_BUILD_TYPE=Release
cmake --build build
mkdir -p "$XPLANE/Resources/plugins/cvfr-bridge/mac_x64"
cp build/cvfr-bridge.xpl "$XPLANE/Resources/plugins/cvfr-bridge/mac_x64/"
```

## Verify

After launching X-Plane:

```bash
$ curl http://localhost:2020/
{"latitude":32.0055,...

# or check the log
$ grep cvfr-bridge "$HOME/X-Plane 12/Log.txt"
cvfr-bridge: HTTP listening on http://0.0.0.0:2020/
cvfr-bridge: started
```

## Uninstall

```bash
rm -rf "$HOME/X-Plane 12/Resources/plugins/cvfr-bridge"
```

## Architecture

```
┌────────────────────────────────────────────────┐
│  X-Plane 12 process                            │
│                                                │
│   ┌──────────────────────┐                     │
│   │ flight-loop callback │  ←── 10 Hz ──┐      │
│   │  reads datarefs      │              │      │
│   │  publishes snapshot  │              │      │
│   └──────────┬───────────┘              │      │
│              │ pthread_mutex            │      │
│              ▼                          │      │
│   ┌──────────────────────┐              │      │
│   │  shared snapshot     │              │      │
│   └──────────┬───────────┘              │      │
│              │                          │      │
│   ┌──────────▼───────────┐              │      │
│   │  HTTP server thread  │  ←── pthread_create │
│   │  bind 0.0.0.0:2020   │                     │
│   │  accept + send JSON  │                     │
│   └──────────┬───────────┘                     │
└──────────────┼─────────────────────────────────┘
               │ TCP/HTTP
               ▼
       iPad / browser polling
       GET http://<mac-ip>:2020/
```

The flight-loop runs on X-Plane's main thread; the HTTP thread is independent and never blocks the sim. The shared snapshot is protected by a mutex (cheap — one writer, low-rate readers).

## Migration from the Python bridge

If you've been running the Python flavor via XLauncher's "scripts" feature:

1. Build + install this plugin (`./build.sh`).
2. Stop the Python script (`pkill -f cvfrmap-bridge.py`) — port 2020 must be free for this plugin to bind.
3. In XLauncher: remove the cvfrmap-bridge.py entry from your profile's "scripts" list (it's no longer needed; the plugin auto-starts with X-Plane).
4. Launch X-Plane normally. The iPad/web app will see the same JSON on the same port — no client-side changes needed.

If port 2020 is already taken at startup, this plugin logs the conflict to `Log.txt` and stays loaded but inert (no crash). Free the port and restart X-Plane.

## See also

- The [Python bridge](../python/) sibling — same wire format, useful when you can't install a plugin or X-Plane is on another machine
- [Top-level README](../README.md) — backend comparison and decision tree
- Original Windows + MSFS server (FSUIPC-based): [arielbider.github.io/cvfr-map](https://arielbider.github.io/cvfr-map/)
