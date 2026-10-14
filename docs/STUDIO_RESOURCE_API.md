# BTGA Studio resource API

BTGA Studio loads PS1 and N64 content through one interface, `tools/btga_assets/studio.py`. It reads a catalog exported by `tools/export_neutral_assets.py` and never branches on the source platform. `platform` is kept only as provenance.

## Producing a catalog

```bash
python3 tools/export_neutral_assets.py local-data/catalog --ps1 "$PS1_EXTRACTED"
```

- ROMs come from `BTGA_N64_ROM` and `BTGA_BT1_ROM`, or from `--ga-rom` and `--bt1-rom`. They are recorded by hash only.
- The output directory must be git-ignored.

**Catalog layout** (every file name derives from a stable ID):

| Path | Contents |
|---|---|
| `catalog.json` | `btga-catalog` v2: sources, levels (with loading `variants`), models (with `library`), textures, inventories |
| `scenes/<id>.json` / `.glb` | neutral scene manifest, plus an inspection GLB with `visible`, `collision` and `markers` layers |
| `models/<id>.json` / `.glb` | model record with its shared mesh, plus a per-model GLB |
| `textures/<id>.png` | decoded texture |
| `inventory/<platform>.json` | every ROM byte classified as decoded, preserved raw or unsupported |

**Stable IDs:**
- N64 IDs come from the ROM hash plus the record's own content (its pool-reference list), so identical geometry has one ID across levels.
- PS1 IDs come from the file hash and record index.
- IDs never depend on traversal order.

## API

```python
from tools.btga_assets.studio import StudioLibrary

library = StudioLibrary.open("local-data/catalog")   # reads catalog.json only
library.sources()            # [Source(platform, title, identity)]
library.campaigns()          # {campaign: [level ids]}             (catalog only)
library.levels(campaign)     # [LevelSummary(id, name, campaign, variant, platform, counts…, variants)]
game_map = library.load_map(level_id)                 # one scene file
game_map.instances           # [Instance(id, model, position, yaw_turns, category)]
game_map.collision           # [CollisionShape(id, kind, position, yaw_turns, local_min, local_max, flags,
                             #                 vertical, orientation, confidence, disabled, edited)]
game_map.markers             # [Marker(id, category, position, yaw_turns, models, source_kind, fields,
                             #         dependencies, restrictions, confidence, disabled, edited, source)]
game_map.bounds, game_map.environment, game_map.minimap, game_map.navigation, game_map.floor
library.model(model_id)      # Model(id, mesh, bounds, provenance)
library.texture(texture_id)  # Texture(id, width, height, wrap, alpha, rgba)
library.thumbnail(model_id, size=96)                  # RGBA, rendered on demand, LRU of 256
library.minimap(level_id, variant)                    # {status, selection, transform, origin, scale, texture}
StudioLibrary.world_to_minimap(minimap, x, z)         # (px, py) or None, as func_800C7C10 draws
library.placeable_objects()  # [{category, models}]   (reads every scene: not for the browser)
library.gangs_and_tanks()    # shared vehicle/object libraries (names not mapped)
library.environment_presets(), library.minimaps()
```

### Typed fields

`Marker.fields` maps a field name to `{value, type, offset, range, evidence, meaning}`, plus optional `enum`, `enum_closed`, `bitmask_bits`, `reference` and `sentinel`.

- **Edit widgets:** an editor builds them from `type` and `range`. Values listed in `sentinel` (for example `0xFFFF = unlimited`) are always legal.
- **Model fields:** `reference: "model"` appears only where the field's consumer is a proven model lookup. Model dependencies are listed in `Marker.dependencies` with their catalog IDs.
- **Evidence:** `evidence` and `Marker.confidence` use proven / corroborated / inferred / unresolved. Fields nobody reads stay in the scene's `raw_hex` and `unknown_bytes`.

### Collision height

`CollisionShape.vertical["status"]` is one of:
- `"proven"`: Global Assault. `local_min[1]`/`local_max[1]` are placement-relative heights.
- `"none_stored"`: BattleTanx and PS1. The game's collision is planar, and the heights are `None`; a viewer may extrude for display only.

BattleTanx shapes whose position the game rejects carry `extras.registered = False` in the scene file.

### Editing with overlays

```python
overlay = library.new_overlay(level_id)
template = library.marker_templates(level_id)["ga_kind_13"]     # any source marker of that kind
new_id = overlay.add_marker(template, (x, y, z), yaw_units, {"spawn_budget": 20})
overlay.move(marker_id, (x, y, z), yaw_units)
overlay.set_field(marker_id, "unit_type_mask", 0b101)
overlay.disable(source_marker_id)           # non-destructive; delete() is for user-created records
overlay.add({"op": "set_collision", "target": shape_id, "min": [...], "max": [...]})
overlay.add({"op": "set_minimap", "minimap": {"mode": "source", "variant": "flag"}})
overlay.add({"op": "set_nav_link", "link": 3, "targets": [4, 9, 12]})
errors = library.validate_overlay(level_id, overlay, target_platform=None)
edited = library.load_map(level_id, overlay)    # touched records have edited=True and keep their source
library.save_overlay(overlay, "project/london.overlay.json")
overlay = library.load_overlay("project/london.overlay.json")
overlay.revert(1)
```

- **Immutable sources:** source scenes are never modified.
- **Deterministic saves:** an overlay is sorted, indented JSON (`btga-overlay` v1). It contains no host path (saving refuses one) and repeats byte for byte. User IDs are `user:<scene hash>:<n>`.
- **Errors:** validation errors are `{code, target, message, details}`. The codes are:
  - Placement: `OUT_OF_BOUNDS`, `INVALID_TEAM`, `TEAM_LIMIT`, `ILLEGAL_FIELD`.
  - Dependencies and IDs: `MISSING_DEPENDENCY`, `DUPLICATE_ID`.
  - Collision: `COLLISION_OVERLAP`, `INVALID_COLLISION`, which includes a height on a planar platform.
  - Platform: `UNSUPPORTED_MARKER`, checked against the target platform's loader kinds.
  - Navigation and minimap: `INVALID_NAV_LINK`, `INVALID_MINIMAP` (generated images are limited to the HUD's 72 × 75).
  - Edit structure: `PROTECTED_SOURCE`, `UNKNOWN_TARGET`, `INVALID_EDIT`.
- **Not yet supported:** writing an overlay back into a ROM or disc image.

### Conventions every map follows

| Convention | Rule |
|---|---|
| Axes | +Y up, right-handed, counter-clockwise front faces; units are the source's model units |
| Rotation | `yaw_turns` is a rotation about +Y in turns. All three games build the same matrix, `[[c,0,s],[0,1,0],[-s,0,c]]`. |
| Mesh | `positions`; `materials` (`texture`, `double_sided`, `alpha_mode`); `triangles` with per-corner `p`, `uv` (texture space), `colour` (RGBA 0..1) and `normal`; `lods`; `rejected_triangles` |
| Missing data | absent data stays `None`. Unresolved records are listed in the scene's `unresolved`, never dropped. |

## Loading a map without knowing its source

`tests/test_n64_assets.py::StudioApiTests` exports two synthetic maps, one tagged `ps1` and one tagged `n64_btga`, and loads both through the same function:

```python
def load_any_map(library, level_id):
    game_map = library.load_map(level_id)
    return [(i.position, i.yaw_turns, len(library.model(i.model).mesh["triangles"]))
            for i in game_map.instances]
```

Both maps return identical results. Nothing in the editor-facing objects reveals or depends on the platform.

## Performance

`tools/benchmark_studio_api.py <catalog>` measures the API against a full export.

**Test setup:** WSL2 on Linux, Python 3.14, one core. The catalog holds 14,742 files: 155 levels, 4,078 models and 6,273 textures.

| Step | Time | Files read | RSS after |
|---|---|---|---|
| Open the catalog | 0.34 s | catalog.json only | 62 MiB |
| List campaigns and maps | 0.002 s | none | 62 MiB |
| Open the largest map (3,530 instances, 668 markers, 529 shapes) | 0.34 s | 1 scene | 91 MiB |
| 24 thumbnails, first request | 32.9 s | 24 models, 37 textures | 92 MiB |
| 24 thumbnails, cached | 0.0001 s | none | 92 MiB |
| Open and close 100 maps | 16.0 s | 100 scenes | 99 MiB |
| The same 100 maps again | 15.3 s | 100 scenes | 99 MiB (+28 KiB) |
| 100 overlay edits, then apply, validate and revert | 1.9 s | 6 scenes | 148 MiB |

- **Startup:** the browser reads only the catalog, with a Python heap peak of 23 MiB.
- **Memory:** memory stays flat across repeated map cycles.
- **Thumbnails:** they use the pure-Python offline rasteriser (about 1.4 s each). That is acceptable as a lazy background job; a GPU viewer would replace it. The renderer has not been optimised.

## Not yet provided

- **Named gangs and tanks:** no traced table maps names onto the shared models.
- **BattleTanx minimap:** no source traced. **PS1 radar images:** paired by file name, not by a traced selection.
- **Music.**
- **Writing edits to a ROM or disc image:** overlays are project data only.
- **Still raw:** Global Assault kinds 6, 8, 23, 26 and 41; BattleTanx actor footprints for types 3, 5, 20 and 31; most PS1 non-model kinds. Their records are raw bytes with handler addresses.
