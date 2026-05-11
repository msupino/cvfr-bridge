# cvfr-bridge

Aircraft pose → JSON over HTTP. Two interchangeable backends for **X-Plane 12** that expose the same `GET http://<host>:2020/` endpoint, designed to feed the [cvfr-map](https://msupino.github.io/cvfr-map/) iPad/web moving-map app (or anything else that wants live aircraft state on a LAN).

```bash
$ curl http://localhost:2020/
{"latitude":32.0055,"longitude":34.8854,"altitude":135,"heading":4.6,
 "variation":4.7,"pitch":-1.20,"roll":0.45,"ias":102.5,"vsi":0,
 "wind_dir":270,"wind_speed":8.5,"qnh":29.92,"sim_ready":true}
```

## Two backends, same wire format

| | [`c/`](c) — X-Plane plugin | [`python/`](python) — UDP bridge |
|---|---|---|
| What it is | `cvfr-bridge.xpl` loaded inside X-Plane | `cvfrmap-bridge.py` external Python script |
| Update rate | 10 Hz (flight-loop) | 5 Hz (UDP RPOS+RREF poll) |
| Latency to client | ~50 ms | ~250 ms |
| Process model | Lives inside X-Plane, no separate program | Separate `python3` process |
| Setup | Build once, drop into `Resources/plugins/` | Just `python3 cvfrmap-bridge.py` |
| Auto start/stop | Yes, with X-Plane | Manual or via launcher script |
| Portability | macOS arm64+x86_64 / Linux / Windows (build per-platform) | Anywhere with Python 3.10+ |
| Code size | ~310 LOC C | ~250 LOC Python |
| External deps | X-Plane SDK (build only) | None (stdlib only) |
| Network access | Direct dataref reads, no network needed | UDP to `localhost:49000` (or remote X-Plane) |
| Original use case | Local sim on the same Mac | Remote sim, plugin-restricted environments |

**The wire format is identical.** Switching backends doesn't require any client-side change. Both backends derive their field list, types, and LLBG fallback values from a single source of truth — see [Schema-driven architecture](#schema-driven-architecture) below.

## JSON schema (both backends)

| field | unit | meaning |
|---|---|---|
| `latitude` | deg WGS84 | aircraft position |
| `longitude` | deg WGS84 | |
| `altitude` | feet MSL | |
| `heading` | deg magnetic | what the compass shows |
| `variation` | deg | magnetic variation, E positive |
| `pitch` | deg | nose-up positive |
| `roll` | deg | right-wing-down positive |
| `ias` | knots | indicated airspeed |
| `vsi` | fpm | vertical speed |
| `wind_dir` | deg magnetic | surface wind direction (FROM) |
| `wind_speed` | kt | surface wind speed |
| `qnh` | inHg | altimeter setting |
| `sim_ready` | bool | `false` when sim hasn't placed the aircraft yet (`lat==lon==0`) |

When `sim_ready: false`, position falls back to LLBG (Ben Gurion) so the iPad map shows something sensible at startup.

## Which backend should you use?

Decision tree:

```
Are you running X-Plane locally on the same machine as the bridge?
├── YES  → use c/ (the plugin) - lower latency, auto-start/stop, no separate process
│
└── NO   → use python/ (the script) - works over the network, no install on the X-Plane host
                                       Set XP_HOST in cvfrmap-bridge.py to the sim's IP.
```

For the typical "Mac + X-Plane on the same machine" setup, the C plugin is the better default. The Python bridge stays useful for:
- Talking to a remote X-Plane on another machine (set `XP_HOST` in the script)
- Environments where you can't install plugins (some shared-rig setups)
- Quick experiments without rebuilding
- Other sims that speak the X-Plane UDP protocol via a shim

## Quick start: C plugin (preferred)

```bash
cd c/
./build.sh
# → installs to ~/XPlane-Plugins-Available/cvfr-bridge/ if you use XLauncher,
#   otherwise into X-Plane 12/Resources/plugins/cvfr-bridge/
```

Launch X-Plane. The plugin auto-starts on port 2020. See [`c/README.md`](c/README.md) for build details, datarefs, and architecture.

## Quick start: Python script (fallback)

```bash
cd python/
python3 cvfrmap-bridge.py
# → CVFR Map Bridge
# →   iPad/browser IP : 192.168.1.42
# →   Listening on    : http://192.168.1.42:2020
# →   X-Plane         : 127.0.0.1:49000 (waiting for RPOS/RREF)
```

In X-Plane: `Settings → Network → Receive External Datarefs` must be enabled (default port `49000`). See [`python/README.md`](python/README.md) for remote-sim setup, schema details, and the `RPOS` vs `RREF` protocol breakdown.

## Develop the web UI without X-Plane

[`tools/cvfrmap-fake.py`](tools/cvfrmap-fake.py) is a dev-only "third backend" that serves the same JSON shape on the same port (`2020`) as the real backends, but with no X-Plane and no UDP — just a synthetic aircraft flying a figure-8 over LLBG (two rate-1 loops, 240 s/cycle, level at 2500 ft, 90 KIAS). Use it to develop the [cvfr-map](https://msupino.github.io/cvfr-map/) web UI, tweak gauges, or demo the page when you don't have X-Plane handy. It reads `schema.json` for the port, field order, and fallbacks, so new schema fields appear in its output automatically.

```bash
python3 tools/cvfrmap-fake.py
# → cvfr-bridge / fake (dev simulator)
# →   serving : http://0.0.0.0:2020/
```

Then point the cvfr-map page at `http://localhost:2020/` as usual. The fake is a development tool only — it's intentionally not listed in the [Two backends, same wire format](#two-backends-same-wire-format) table above.

## Schema-driven architecture

Both backends consume **[`schema.json`](schema.json)** at the repo root as the single source of truth for the JSON wire format. Add, remove, or rename a field there and both backends pick it up:

```
schema.json (canonical, hand-edited)
├──► python/cvfrmap-bridge.py    (X-Plane UDP backend)
│      derives:
│        - the LLBG fallback dict
│        - the RREF dataref subscriptions
│        - the JSON serialization order
│
├──► tools/gen_c_schema.py  ─►  c/schema.h  ─►  c/plugin.c  (X-Plane plugin backend)
│      generates (CMake custom_command, before compile):
│        ├── CVFR_PORT, CVFR_SCHEMA_VERSION
│        ├── CVFR_FIELD_<NAME>          string-literal field names
│        ├── CVFR_FORMAT_<NAME>         per-field printf format
│        ├── CVFR_FALLBACK_<NAME>       LLBG fallback constants
│        └── CVFR_JSON_TEMPLATE         single printf template for JSON body
│
└──► tools/cvfrmap-fake.py       (dev simulator, no X-Plane required)
       reads schema.json at startup; every field initialised from its
       fallback, then a synthetic orbit overlays the moving fields.
```

`c/schema.h` is `.gitignore`d — every clean build regenerates it from `schema.json` so what's in the binary always matches the canonical source. To verify the generator output without compiling:

```bash
python3 tools/gen_c_schema.py --schema schema.json --output /tmp/schema.h
cat /tmp/schema.h
```

When you change `schema.json`:

1. Run `c/build.sh` — picks up changes via CMake's dependency tracking.
2. Restart the Python script — re-reads `schema.json` at startup.
3. Update the README's [JSON schema](#json-schema-both-backends) field table to match (this is the only doc that's hand-maintained).

The one bit of manual coupling that survives codegen is the **argument list to `snprintf` in `c/plugin.c`'s `format_json()`**: if you reorder fields in `schema.json`, you must also reorder the snprintf args to match the new template's positional argument order. The template itself regenerates correctly; only the argument values have no codegen.

## Relation to the original cvfr-map server

[cvfr-map](https://arielbider.github.io/cvfr-map/) by Ariel Bider ships with its **own** backend server — a Windows-only, PyInstaller-bundled `.exe` that reads aircraft state from **Microsoft Flight Simulator** (and FSX / Prepar3D) via FSUIPC and serves the same JSON shape on the same port. **That server is the right choice for MSFS users on Windows; this repo doesn't replace it.**

This repo is a **sibling implementation for X-Plane**: same wire format and same iPad/web client, but reading from X-Plane's UDP API (Python flavor) or its plugin SDK (C flavor) instead of FSUIPC. The two backends pair this way:

| If you fly | Use |
|---|---|
| MSFS / FSX / P3D on Windows | [Bider's original cvfr-map server](https://arielbider.github.io/cvfr-map/cvfr-map/server/CVFRMAP-SERVER.zip) |
| X-Plane 12 (any platform) | this repo (`c/` for local sim, `python/` for remote/no-plugin) |

Both speak the same JSON to the same iPad/web cvfr-map app, so users can switch sims without re-configuring the client.

## License

MIT — see [LICENSE](LICENSE).
