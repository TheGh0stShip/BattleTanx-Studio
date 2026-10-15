# Test in Game: the Studio to PC runtime contract

Studio's **Drive Preview** is an editor approximation: it runs none of the
game's physics, AI, combat, destruction, pickups or scripting. Real
behaviour needs the PC runtime. **Test in Game** starts it with the user's
exported package. Studio implements only its side of this contract; it does
not implement or modify the runtime.

Reference code: `tools/btga_studio/core/runtime.py`. JSON Schemas:
`schemas/btga-runtime-capabilities-v1.schema.json` and
`schemas/btga-launch-request-v1.schema.json`.

## 1. The runtime declares what it can do

A runtime that supports Studio launches ships `btga-runtime.json` next to its
program:

```json
{"schema": "btga-runtime-capabilities", "schema_version": 1,
 "runtime": {"name": "...", "version": "1.2.3"},
 "launch_request_versions": [1], "package_schema_versions": [1],
 "games": ["n64_btga"], "features": ["map.blank", "scenario", "..."],
 "bot_slots": 8, "teams": 4, "capture_modes": ["none", "screenshot", "video"]}
```

Studio reads this file (at most 1 MiB) and never runs the program to ask.
"Test in Game" stays disabled, with the reason in its tooltip, until a
program with a compatible file is chosen in Settings > Test in Game.

## 2. Studio checks everything before a launch

`preflight` returns every problem, each with an actionable message. The
Launch button is enabled only when there are none.

| Code | When |
|---|---|
| `RUNTIME_ABSENT` | no runtime configured, or the program is gone |
| `RUNTIME_INCOMPATIBLE` | no capabilities file, unreadable, wrong schema, or launch request / package versions not supported |
| `PACKAGE_MISSING` | no package exported in this session, or the file is gone |
| `PACKAGE_STALE` | the package file changed since Studio wrote it, or the project changed since it was exported |
| `ROM_MISSING` | no cartridge configured, or the file is gone |
| `ROM_MISMATCH` | the cartridge's SHA-256 is not the one the project's content comes from |
| `CONTENT_REJECTED` | the runtime does not declare a feature the content uses, a requested AI slot, or the capture mode |
| `INVALID_SELECTION` | the map, scenario or capture mode chosen is not valid |

Studio never falls back to Drive Preview and never reports a launch that did
not happen.

## 3. The launch request

Studio starts `<program> --btga-launch-request -` with the program's folder as
the working directory and writes one JSON document to its standard input:

```json
{"schema": "btga-launch-request", "schema_version": 1,
 "session_id": "<32 lowercase hex digits, new for every launch>",
 "package": {"path": "<absolute>", "sha256": "<64 hex>", "size": 123},
 "map": "<map key>", "scenario": "<scenario key>", "player_spawn": "<record id>",
 "bot_slots": [4, 5, 6, 7],
 "capture": {"mode": "none", "directory": null},
 "rom": {"path": "<absolute>", "sha256": "<64 hex>"},
 "studio": {"version": "<Studio version>"},
 "features": ["..."]}
```

`scenario` and `player_spawn` may be null (free play; runtime's choice).
`capture.directory` is required unless the mode is `none`. The ROM path
comes from the user's local settings; it is passed only to the process,
through standard input, and is never written to disk, into a project or into
a package. `features` repeats what the package content needs, so the
runtime can refuse cleanly if its declaration was wrong.

The runtime must verify `package.sha256` and `rom.sha256` before loading and
must refuse requests whose `schema_version` it does not support.

## Not yet decided

Exit codes and a progress channel from the runtime back to Studio are not
part of version 1. Studio only reports that it started the process.
