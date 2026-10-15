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
| PyInstaller | Freezes a windowed `BattleTanxStudio.exe` (no console) with every runtime library |
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

On Windows, start `BattleTanxStudio.exe`. A first launch shows the welcome screen (Create Original Project, Import From My Games, Open Existing Project, Open Example Project); File > Project Hub shows it again with recent projects.

**Settings:**
- `%APPDATA%\BattleTanx Studio` holds `preferences.json`, `layout.ini` and `sources.json`; `%LOCALAPPDATA%\BattleTanx Studio` holds `catalogs/`, `thumbnails/`, `starter/`, `recovery/`, `logs/` and `prefabs/` (Linux: `$XDG_CONFIG_HOME/battletanx-studio` and `$XDG_DATA_HOME/battletanx-studio`). Nothing is written next to the program.
- Data from earlier builds in `%APPDATA%\BTGAStudio` is migrated once (preferences and source registry; catalogs are rebuilt on demand).
- **Portable mode:** `--portable`, or a `portable.txt` next to the executable, keeps everything in `BattleTanxStudioData\`.
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
- `tests/test_studio_acceptance.py` drives the complete ROM-free workflow through the real application off-screen: Dear ImGui mouse clicks place and select objects in the viewport, keyboard shortcuts duplicate, copy, hide, isolate, lock, delete, undo and redo, and the project is saved to a path with spaces and non-ASCII characters, reopened and exported twice to byte-identical packages. It also holds regression tests for defects found in review (malformed projects and packages, Shift-drag cancel, duplicate scale, thumbnail browser, navigation overlay, packaged self-check).
- `tests/test_studio_evidence.py` covers the acceptance tooling below with synthetic inputs.
- `tools/mutation_check_studio.py` breaks picking, axes, yaw direction, unknown-byte preservation, serialisation determinism, undo grouping, thumbnail keys, ID stability and validation, plus every fix covered by the acceptance tests, and checks that the tests catch each.

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

## Product modules

| Module | Role | Consumers |
|---|---|---|
| `core/atomic.py` | Atomic file replacement (temporary file in the same folder, fsync, `os.replace`; cleanup on failure) | project save, autosave, source registry, preferences, package export, prefabs, release tooling |
| `core/jobs.py` | Background jobs with progress, cancellation, bounded queue (16), retry; callbacks run on the UI thread | source identify/register/verify/search/relink, catalog builds, package export, per-user install |
| `core/applog.py` | JSON-lines logs (rotated, 1 MiB x 5) with absolute paths removed; problem reports | controller, error dialogs, Help > Report a problem |
| `core/hub.py` | Templates, project creation, recent-project status with missing dependencies, relink, duplicate, rename | Project Hub, wizard, tests |
| `core/prefabs.py` | Named reusable object groups stored per user | gameplay editor, context menus |
| `core/imaging.py` | PNG (all colour types, Adam7), baseline JPEG, BMP and TGA decoding; power-of-two fitting | importers, elevation tiles |
| `core/imports.py` | OBJ/MTL, glTF 2.0/GLB and picture import (texture, ground tile, upright panel, box, sky panorama); the user library catalog (`user:` IDs) with credits; restore from packages | Import window, city builder, packages |
| `core/sky.py` | Sky panorama record checks and the sphere the viewport draws | renderer, Map tab, packages |
| `core/geo.py` | WGS84 local projection, Terrarium elevation tiles, polygon triangulation with holes, minimum rectangles, raster painting | city builder, skyline |
| `core/osm.py` | OpenStreetMap (`.osm` or Overpass JSON) to a true-scale city: terrain, painted ground, buildings and parts with real heights (Wikidata fill-in), open stadium bowls, bridges, skyways, trees, collision | Import window (Build a city), map scripts |
| `core/skyline.py` | A sky panorama of the real horizon: terrain to 45 km plus distant buildings, domes and masts at true bearings | map scripts |
| `core/installer.py` | Transactional per-user install, upgrade, repair, downgrade policy, rollback journal, uninstall that keeps user data; platform effects behind `WindowsSystem` / `SandboxSystem` adapters | `BattleTanxStudioSetup.exe`, Settings, `--uninstall`, installer audit |
| `core/versioning.py` | Version parsing and ordering (pre-releases sort first) | installer, release tooling |
| `core/environment.py` | Installed / portable / source detection, build revision, repair guidance | About, Settings |
| `core/history.py` | Undo/redo for gameplay definitions, map and minimap settings and prefab files; coalescing; shared sequence with map edits | Gameplay editor, toolbar, Edit menu |
| `core/tutorial.py` | Guided-tour steps, completion checks against editor facts, per-user progress | welcome screen, Help |
| `core/fontset.py`, `gui/fonts.py` | Bundled font manifest, SHA-256 verification, staging for builds, Dear ImGui loading with merged fallbacks | editor, setup, release tooling |
| `setup/` | Setup pages and uninstall confirmation (window-independent), `BattleTanxStudioSetup.exe` entry point | release |
| `core/desktop.py` | Open a folder or an allow-listed project URL without a console | settings, help, export, error dialogs |
| `gui/windows.py` | Welcome/Hub, wizard, Source Manager, gameplay editor, settings, about, help, dialogs, toolbar, status bar, context menus | `StudioApp` |
| `fixtures.py` | Synthetic ROMs, temporary supported revisions, simulated catalog builder, scripted dialogs | tests, screenshot capture |
| `packaging/release.py` | Version resource, build information, third-party notices, deterministic ZIP, checksums, build manifest | `build_windows.ps1` |

Every panel and window runs inside `StudioApp.guard`: an unexpected exception is logged with its traceback, Dear ImGui's error recovery repairs the frame, and the user sees one plain-language dialog instead of the editor closing. File pickers go through `StudioApp.dialogs`, so the off-screen tests drive every window.

## Importing models, pictures and real places

File > Import models and textures (or Import... above the asset browser) reads OBJ (with MTL and pictures), glTF/GLB and PNG/JPEG/BMP/TGA files from anywhere. Each file is checked with the chosen options before anything is stored (units, up axis, standing on the ground, largest texture, what a picture becomes); problems are listed per file in plain language (missing textures are looked up by name next to the model, files outside the model's folder are never read, progressive JPEGs and FBX files come with advice). Imports go into the user's library (`%LOCALAPPDATA%\BattleTanx Studio\user-library`), appear in the asset browser under "Imported: <group>", keep their credit and licence text, and travel inside content packages. A missing import shows as a magenta stand-in box and a MISSING_IMPORT validation error, never a crash. Right-click > Make solid adds a collision box matching a placed model.

The same window builds a city from OpenStreetMap data (and optional Terrarium elevation tiles and a Wikidata heights result) into a new map in the background; the whole city is one undoable step. `tools/build_minneapolis_map.py` shows the full workflow, including a computed horizon sky, a teleporter ramp and multiplayer setup.

## Undo for gameplay definitions

Map-object edits keep their per-map stacks; gameplay definitions (project name and description, campaigns, missions, multiplayer, minigames, tanks, gangs), map name, size, sky colour and sky panorama, minimap settings and prefab files go through `core/history.py`. One user action is one transaction; typing in one text box or dragging one slider coalesces until the control loses focus (or 2 s pass); an edit that raises is rolled back and never recorded; an edit that changes nothing is not recorded. Every transaction, in either history, takes a number from one shared sequence, so Ctrl+Z reverses the most recent action wherever it happened and restores the Gameplay editor tab and map it came from. Saved projects store both histories with their sequence numbers replaced by ranks, so serialisation stays deterministic. The unsaved-changes state compares project content only, so undoing back to the saved state is clean.

## Fonts

`tools/btga_studio/fonts/fonts.json` lists the bundled fonts (DejaVu Sans for Latin, Greek and Cyrillic; Droid Sans Fallback for Chinese and Japanese; Nanum Gothic for Korean) with version, licence, provenance, pinned Ubuntu package URL and package/font SHA-256; the licence texts sit beside it. The font files are not in the repository. On Linux/WSL, `python tools/fetch_studio_fonts.py private-work/toolchains/fonts` downloads the pinned packages over HTTPS, verifies them before extraction with `dpkg-deb`, verifies the extracted fonts, and installs nothing system-wide. Point `BTGA_STUDIO_FONT_DIR` (or `build_windows.ps1 -FontDir`) at that folder. A file whose hash differs is never used; without the primary font Studio falls back to Dear ImGui's built-in font and logs it.

## Acceptance tooling

| Tool | Inputs | Outputs | Notes |
|---|---|---|---|
| `gui/offscreen.py` (`OffscreenUI`) | a `StudioApp` | frames, `click`/`drag`/`press` through Dear ImGui IO, `world_to_screen`, `pixels`, `screenshot` | EGL surfaceless; never creates a window; screenshots are the application framebuffer only |
| `tools/studio_evidence.py` | a checkout, PNG files, directories | source identity (commit plus SHA-256 of uncommitted changes), `stale`, conflicting processes (`/proc`), image statistics and blank/flat rejection, adjacent JSON evidence records, file manifests and their comparison | git is used read-only; records contain no absolute paths; hashing is streamed |
| `tools/capture_studio_acceptance.py`, `tools/capture_studio_product.py`, `tools/capture_studio_release.py` (each `OUT [--review VERDICTS]`) | starter content only | 18 application screenshots with JSON records; `--review` merges reviewer verdicts and moves failures to `rejected/` | automatic checks reject blank or flat images, human review is still required |
| `tools/audit_studio_package.py DIST [--manifest M] [--forbid TEXT]`, `--compare A B` | a packaged folder | PE subsystem, required files, forbidden file types, forbidden text (ASCII and UTF-16LE), manifest; differing files between two builds | constant memory (4 MiB scan windows) |
| `tools/benchmark_studio_cycles.py [--cycles N]` | starter content | open/edit/undo/validate latency, per-cycle RSS and traced heap, second-half spread | samples are preallocated so the probe does not measure itself |

## Windows build

`tools/btga_studio/packaging/build_windows.ps1` creates an isolated venv with Python 3.13 (`-PythonVersion`; `py -3` would pick the newest installed interpreter), installs the pinned requirements, writes the Windows version resource and `data/build.json` (`release.py prepare`), freezes `BattleTanxStudio.exe` with PyInstaller (windowed; the Global Assault level table is bundled as data so N64 catalogs build in the packaged program), adds the synthetic sample, the ROM-free starter example, `README.txt`, `COPYING.txt` and `THIRD-PARTY-NOTICES.txt`, and writes `release/` with a deterministic ZIP, `SHA256SUMS.txt` and `build-manifest.json` (`release.py finalize`).
- **Self-check:** `BattleTanxStudio.exe --self-check REPORT.json` exercises the starter workflow without a window and, when frozen, verifies the bundled data files and that the shipped starter example equals a fresh build of it.
- **Setup:** the build also freezes `BattleTanxStudioSetup.exe` (one file, Windows GUI subsystem, version resource with publisher) that embeds the program folder and its `payload-manifest.json`. It installs per user (default `%LOCALAPPDATA%\Programs\BattleTanx Studio`, selectable), adds a Start menu entry and an optional desktop shortcut, registers an HKCU uninstall entry with a stable upgrade code, upgrades and repairs in place, refuses downgrades unless confirmed, and rolls every step back on failure. `--silent`, `--self-check` and (tests only) `--sandbox` are available. Windows Settings > Apps runs `BattleTanxStudio.exe --uninstall`, which keeps projects, settings and catalogs unless the user ticks the removal option. Tools > Settings > Install for this user does the same from a portable copy. Builds are not signed.
- **Release folder:** exactly `BattleTanxStudioSetup.exe`, `BattleTanxStudio-portable-x64.zip`, `SHA256SUMS.txt`, `build-manifest.json`, `RELEASE-NOTES.txt`, `THIRD-PARTY-NOTICES.txt` and `README.txt`; `release.py finalize` refuses anything else.
- **Checks:** `tools/audit_studio_installer.py` (metadata, payload hashes, upgrade-code stability, registration, shortcuts, quoting, rollback at every step, repair, uninstall preservation, all in a sandbox) and `tools/windows_release_check.py` (the same on the real Windows filesystem through the setup's silent sandbox mode, plus read-only folders and running-program detection).
- **Run it** with `powershell -WindowStyle Hidden -NonInteractive`. It never launches the editor.
- **Outputs** go outside the repository.
