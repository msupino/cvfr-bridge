# tools/

Helper scripts that hang off `schema.json`. Neither is a backend in
its own right — `gen_c_schema.py` is build-time codegen for the C
plugin, and `cvfrmap-fake.py` is a dev simulator that lets you work on
the [cvfr-map](https://arielbider.github.io/cvfr-map/) web UI without
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
fields (lat/lon/heading/altitude/pitch/roll/vsi/ias/sim_ready) are
overridden each request to drive a 3 deg/s right orbit at ~3000 ft
± 200 ft around LLBG. Stdlib only.

```bash
python3 tools/cvfrmap-fake.py
```

```bash
$ curl -s http://localhost:2020/ | python3 -m json.tool
{
    "latitude": 32.0255,
    "longitude": 34.8854,
    "altitude": 3142,
    "heading": 90.0,
    "variation": 4.7,
    "pitch": 1.57,
    "roll": 15.0,
    "ias": 100.0,
    "vsi": 119,
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
