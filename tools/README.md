# tools/

Helper scripts that hang off `schema.json`. Neither is a backend in
its own right — `gen_c_schema.py` is build-time codegen for the C
plugin, and `cvfrmap-fake.py` is a dev simulator that lets you work on
the [cvfr-map](https://msupino.github.io/cvfr-map/) web UI without
launching X-Plane.

## `gen_c_schema.py`

Reads `../schema.json` and emits `c/schema.h`: per-field name / format /
fallback `#define`s plus a single `CVFR_JSON_TEMPLATE` printf string in
canonical field order. CMake invokes this as a `custom_command` before
compiling `c/plugin.c`, so the generated header is always in sync with
the canonical schema. The header is `.gitignore`d on purpose — every
clean build regenerates it from `schema.json`.

```bash
python3 tools/gen_c_schema.py --schema schema.json --output /tmp/schema.h
```

## `cvfrmap-fake.py`

Schema-driven dev simulator. Serves the cvfr-bridge JSON shape on the
schema's port (`2020` today) using a synthetic in-flight aircraft, so
the cvfr-map web UI, gauge tweaks, and demos work without X-Plane.
Like `python/cvfrmap-bridge.py`, it loads `../schema.json` at startup
and initializes every field from its `fallback`; only a handful of
fields (lat/lon/heading/roll/ias/sim_ready, plus altitude/vsi/pitch
when oscillation is enabled) are overridden each request to fly a
**figure-8** over LLBG: a 120 s right loop joined to a 120 s left
loop, both rate-1 turns, level at 2500 ft, 90 KIAS — so the heading
indicator, attitude indicator, and turn coordinator all exercise
both turn directions every cycle. Stdlib only.

```bash
python3 tools/cvfrmap-fake.py
```

```bash
$ curl -s http://localhost:2020/ | python3 -m json.tool
{
    "latitude": 32.025500,
    "longitude": 34.910100,
    "altitude": 2500,
    "heading": 92.9,
    "variation": 4.7,
    "pitch": 0.0,
    "roll": 15.0,
    "ias": 90.0,
    "vsi": 0,
    "wind_dir": 0.0,
    "wind_speed": 0.0,
    "qnh": 29.92,
    "sim_ready": true
}
```

`--port`, `--bind`, `--schema`, and `--quiet` are available; see
`python3 tools/cvfrmap-fake.py --help`. Not a real backend —
intentionally not listed in the top-level README's "two backends, same
wire format" table.
