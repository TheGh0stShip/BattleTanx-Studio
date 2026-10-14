# BTGA Studio editor

BTGA Studio is a desktop map editor for the neutral catalog produced by `tools/export_neutral_assets.py`. It opens maps from:
- BattleTanx: Global Assault (N64)
- BattleTanx: Global Assault (PS1)
- the original BattleTanx (N64)

Every edit is stored as a non-destructive project overlay over the source records. The editor never writes a ROM or disc image.

## Technology

| Choice | Reason |
|---|---|
| Python 3 + **imgui-bundle** (Dear ImGui with docking, **ImGuizmo**, hello_imgui on GLFW) | Reuses the existing tested Studio core (`tools/btga_assets`: overlays, typed fields, validation) instead of re-implementing it. ImGuizmo is a mature gizmo, and pinned wheels exist for Windows x64 and Linux. |
| PyOpenGL (OpenGL 3.3 core) + numpy | Draws neutral meshes directly; vectorised picking and baking |
| PyInstaller | Freezes a windowed `BTGAStudio.exe` (no console) with every runtime library |
| Mesa EGL (surfaceless) | Tests and screenshots drive the real UI off-screen; no window is ever created |

**Alternatives rejected:**
- **SDL2 + C++ ImGui:** the Studio core would have to be duplicated in C++, and no mingw cross-compiler is installed.
- **Qt:** not installed, and heavy to package.
- **Browser stack:** excluded by the brief.

The editor code lives in `tools/btga_studio/`, separate from the matching-decompilation sources.

## Running

```bash
python -m pip install -r tools/btga_studio/requirements.txt
python -m tools.btga_studio --catalog local-data/catalog
```

On Windows, start `BTGAStudio.exe`.

**Settings:**
- Stored in `%APPDATA%\BTGAStudio` (Windows) or `~/.config/btga-studio` (Linux): `preferences.json`, `layout.ini`, `thumbnails/`, `recovery/`.
- **Portable mode:** `--portable`, or a `portable.txt` next to the executable, keeps settings in `BTGAStudioData\`.
- `BTGA_STUDIO_HOME` overrides the location; tests always set it.
- Settings and layout are never written into a project.

**Synthetic sample:**

```bash
python -m tools.btga_studio.samples local-data/studio-sample --project local-data/sample.btgaproj
```

It is generated procedurally (checker textures, boxes) with no game data.

## Layout

| Area | Contents |
|---|---|
| Top | File, Edit, View, Create, Tools, Help |
| Left | Maps grouped by game and campaign; layer toggles; outline with filter |
| Centre | 3D viewport with ImGuizmo |
| Right | Inspector, and the minimap with the camera and selection plotted through the game's mapping |
| Bottom | Asset browser (list or thumbnails), plus Output and Validation tabs |

## Controls

| Input | Action |
|---|---|
| Right mouse + move | Look |
| W A S D | Move horizontally |
| Q / E | Down / up |
| Shift | Faster |
| Wheel | Movement speed |
| F | Frame selection |
| Home | Frame map |
| Esc | Cancel placement or drag |
| Left click | Select (nearest hit: markers, then collision, then geometry, then ID) |
| Ctrl + left click | Toggle selection |
| Empty-space click | Clear selection |
| T / R | Translate / rotate-about-Y gizmo |
| Shift + gizmo drag | Duplicate, then drag the copy |
| Ctrl+Z / Ctrl+Y (Ctrl+Shift+Z) | Undo / redo |
| Ctrl+D | Duplicate |
| Del | Delete user objects, or disable source objects |
| Ctrl+S, Ctrl+Shift+S, Ctrl+O | Save, Save As, Open project |

No shortcut fires while a text field has focus (`gui/input.py`, tested).

## Editing

- **Placement:**
  - Click an asset (model or gameplay template), or drag it into the viewport. A green preview follows the surface under the cursor; snapping options are under Tools.
  - Placement outside proven map limits is refused with a message, never silently clamped.
  - On platforms whose collision is planar or unknown, a placed object keeps working but triggers a warning.
- **Transforms:**
  - The gizmo translates on axes and planes, or rotates about Y, in world or local space, with grid and yaw snapping (off, 15°, 45° or 90°).
  - One drag is one undo step, and Esc restores the exact previous state.
  - Multi-selection moves and rotates about the group centre.
  - Numeric position and yaw are edited in the inspector.
- **Inspector:**
  - Controls are generated from typed field schemas: integer, float, angle, enum, bitmask, bool, slot, model reference, position, bounds, colour.
  - Tooltips give each field's range, unit, evidence and meaning.
  - Values are validated before a transaction exists.
  - Unresolved fields and records are shown read-only with the reason, and their raw bytes are preserved.
  - Specialised views exist for spawn/team start, unit generator, pickup, objective, hazard, environment, collision, minimap alignment, navigation and generic props.
- **Read-only:**
  - Environment records (no overlay edit yet).
  - Navigation point positions; links are editable through `set_nav_link`.
  - Model references of placed objects.
  - Every marker with `confidence: unresolved` or category `untraced_handler` / `ignored_by_dispatcher` / `inactive_in_marker_group`.

## Projects

- **Format:** `.btgaproj` (`btga-studio-project` v1) stores the scene ID, a fingerprint of the scene's source identity, and the overlay as sorted JSON. It contains no timestamps, host paths or source bytes; saving twice gives identical bytes.
- **Source check:** opening a project whose fingerprint differs from the catalog raises `ProjectMismatch`. The overlay is not applied.
- **Workflow:** New (open a map), Open, Save, Save As, Recent; a dirty `*` in the title; confirmation before discarding.
- **Autosave:** an optional recovery copy, off by default (Tools menu).
- **Export preview package:** project, edited neutral scene, validation report and a plan image, for inspection only.
- **Validation:** runs in the background after edits. It reports dangling IDs, out-of-limit objects, spawns inside collision, duplicate IDs, unsupported or illegal field edits, missing source assets, overlapping team starts, invalid spawner masks, invalid minimap transforms, and edits to unresolved records. Clicking a diagnostic selects and frames its object.

## Architecture

| Module | Role |
|---|---|
| `core/geometry.py` | Camera, projection, screen rays, ray/box/sphere/triangle tests, snapping |
| `core/scene.py` | Read-only base scene plus incrementally edited view (identical to `overlay.apply`); bounded LRU mesh cache; vectorised picking with deterministic tie-break |
| `core/document.py` | Transactions, undo/redo, selection, transforms, placement, duplicate/delete, field and collision edits |
| `core/controller.py` | Everything the GUI does, GUI-free: catalog/project workflow, picking, placement, gizmo flow, async diagnostics, autosave |
| `core/browser.py` | Asset index from catalog metadata only |
| `core/thumbnails.py` | Checksummed, bounded disk cache and a prioritised, cancellable process-pool renderer |
| `core/inspector.py`, `core/diagnostics.py`, `core/project.py`, `core/settings.py` | Inspector model, validation, project files, preferences |
| `gui/renderer.py` | OpenGL renderer: baked per-texture batches; edited instances hidden in place and redrawn dynamically; overlay line batch |
| `gui/app.py` | ImGui layout |
| `gui/input.py` | Pure input mapping |
| `gui/offscreen.py`, `gui/headless.py` | Off-screen UI driver |

## Tests and measurements

**Test suites:**
- `tests/test_studio_editor.py` (synthetic fixtures; needs `tools/btga_studio/requirements.txt`) covers camera and projection, rays, picking and tie-breaks, snapping, transform transactions, multi-selection, undo/redo, cancel, inspector validation, projects and mismatches, diagnostics, the thumbnail cache, lazy loading, the 1,000-edit × 10-cycle memory test, settings separation, input gating and an off-screen UI run.
- `tools/mutation_check_studio.py` breaks picking, axes, yaw direction, unknown-byte preservation, serialisation determinism, undo grouping, thumbnail keys, ID stability and validation, and checks that the tests catch each.

**Benchmarks:**
- `tools/benchmark_studio_editor.py CATALOG` runs the ten-cycle stress test and the earlier overlay step, and reports RSS, retained heap and cache counts.
- `tools/benchmark_studio_api.py` measures the read API.

**Measured on this machine** (WSL2; Mesa llvmpipe software OpenGL, no hardware GPU), largest map ROUTE 66 - DRIVE IN (3,530 instances):

| Measure | Result |
|---|---|
| Catalog open | 0.22–0.34 s |
| Map open (scene and view) | 0.39–0.63 s |
| GPU bake | 0.73–0.84 s |
| Picking | p50 2.6 ms, p95 6.7 ms |
| Edit latency | p50 1.2 ms, p95 3.9 ms |
| Undo latency | p50 1.1 ms, p95 3.7 ms |
| Editor work per frame | 1.45 ms |
| Overlay rebuild | 36 ms, only when the scene or selection changes |
| Frame time under llvmpipe | ~45 ms, almost all inside the software driver's draw calls |
| Thumbnail lookup when cached | p95 < 20 ms (tested) |

- **60 FPS:** not verifiable without a hardware GPU here; the map needs 26 draw calls.
- **Memory:** ten cycles of 1,000 mixed edits stay flat over the second half (RSS spread 0.5 MiB; retained heap returns to the same value).
- **Earlier 99 → 148 MiB observation:** the allocator's high-water mark from transient full-scene deep copies (a 45 MiB peak in `apply` and again in `validate`), not retained objects; the retained heap returns to 17.5 MiB. The editor now validates against its incremental view (`validate(..., edited=...)`) and makes no full copy.
- **Bounded caches:**
  - Meshes: 2,000,000 triangles (LRU).
  - In-memory thumbnails: 512.
  - GPU thumbnails: 512.
  - Disk thumbnails: 64 MiB (configurable).

## Windows build

`tools/btga_studio/packaging/build_windows.ps1` creates an isolated venv, installs the pinned requirements, freezes `BTGAStudio.exe` with PyInstaller (windowed), adds the synthetic sample catalog and project, and zips the folder.
- **Run it** with `powershell -WindowStyle Hidden -NonInteractive`. It never launches the editor.
- **Outputs** go outside the repository.
EOF
python3 - <<'EOF'
p='config/tooling_inventory.toml'; s=open(p).read()
s=s.replace('''tools = [
  "benchmark_studio_api.py",''','''tools = [
  "benchmark_studio_api.py",
  "benchmark_studio_editor.py",''')
s=s.replace('''  "mutation_check_ps1_assets.py",
  "ps1_assets.py",''','''  "mutation_check_ps1_assets.py",
  "mutation_check_studio.py",
  "ps1_assets.py",''')
s=s.replace('''tests/test_n64_assets.py and tests/test_world_semantics.py, the''','''tests/test_n64_assets.py, tests/test_world_semantics.py and tests/test_studio_editor.py, the''')
s=s.replace('''the Studio resource API and non-destructive edit overlays."''','''the Studio resource API, non-destructive edit overlays and the BTGA Studio desktop editor (tools/btga_studio)."''')
open(p,'w').write(s)
EOF
python3 -m unittest tests.test_studio_editor
