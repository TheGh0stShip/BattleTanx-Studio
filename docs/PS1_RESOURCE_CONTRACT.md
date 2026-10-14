# Neutral resources for BTGA Studio (schema `btga-neutral` v1)

This is the interchange layer that BTGA Studio and the cross-version comparator consume. It is implemented in `tools/btga_assets/`; the formats it is built from are documented in `docs/PS1_ASSET_FORMATS.md`.

It defines records and files only. The editor, the package format and the storage layout belong to the Studio lane.

## Envelope (every record)

| Field | Content |
|---|---|
| `schema`, `schema_version` | `"btga-neutral"`, `1` |
| `kind` | `model`, `scene`, or `texture` (embedded in materials) |
| `id` | `"<platform>:<kind>:<24 hex>"`, the SHA-256 of `platform \| source SHA-256 \| normalised path \| kind \| indices` |
| `source.platform` | `ps1`, `n64_btga`, `n64_bt1`, `restored_pack` or `user` |
| `source.file` | path relative to the source root, e.g. `ASSETS/BERLIN1.MMM`, or `rom` |
| `source.file_sha256`, `source.range` | source hash and `{offset, size, sha256}` byte ranges |

**IDs** depend only on source identity and record indices. They are identical on every host and never contain host paths.

**Path safety:** `safe_source_path` refuses paths that are:
- absolute or drive-qualified;
- contain backslashes, control characters or NUL;
- have empty components, `.` or `..`, or characters outside a printable allow-list.

Spaces are allowed.

**Evidence levels:** fields that are not proven say so in an `evidence` map (`proven`, `corroborated`, `unknown`). Unknown bytes are kept as raw ranges.

## Model record

- **`header`:** footprint (`x_a`, `z_a`, `x_b`, `z_b`), `radius`, and the raw 0x24-byte header range.
- **`body`:** `part_count`, `vertex_count`, and the raw unknown words `word_0c` and `word_10`.
- **`vertices`:** `[x, y, z]` in model units; `vertex_pad` holds the raw u16s; `parts` holds `[start, end)` vertex ranges.
- **`texture_list`:** TTT indices into the level's IMG/PLT records.
- **`commands`:** every command, losslessly: offset, raw word, opcode, flags, handler address, decoded operands (including unused bit fields), extra header words, and the enclosing visibility-block path.
- **`primitives`:** for each one:
  - `gpu_code`, `quad`, `gouraud`, `textured`, `cull` (`front_screen_cw`, `front_screen_ccw` or `double_sided`), `texture_slot` and `block_path`;
  - per-corner `vertex`, `rgb`, `normal` and `uv`;
  - `missing`: the list of state the stream never set. It is empty for every retail model.
- **`stream`:** offset, size, covered bytes, and any unreached raw range.

## glTF 2.0 export (`tools/btga_assets/gltf.py`)

| glTF item | Rule |
|---|---|
| Coordinates | PS1 model coordinates unchanged: +Y up, right-handed, CCW front; one unit = one PS1 model unit. Scene placements use ×4 positions and a quaternion about +Y for the u16 yaw. |
| Vertices | not welded: one vertex per triangle corner, so per-corner data is exact |
| Triangulation | quads split on the c1–c3 diagonal the GPU uses: `(c0,c1,c3)`, `(c1,c2,c3)` |
| Winding | cull flag 2 primitives are reversed; double-sided ones get a `doubleSided` material |
| `COLOR_0` | authored corner RGB ÷ 128, clamped to 1.0. The PS1 GPU modulates by colour/128, and this is the colour before runtime lighting. |
| `NORMAL` | authored corner normal, normalised (the reset default is `(0, 0, 1)`) |
| `TEXCOORD_0` | `u / width`, `v / height`. With `NEAREST` sampling this selects the same texel the PS1 does. |
| Textures | decoded IMG/PLT with CLUT row 0, embedded PNG. `MASK` alpha when any texel is `0x0000`. The row is a per-instance runtime value, and `palette_rows_available` is recorded. |
| Semi-transparency | the material's `extras.semi_transparent` is `false` (proven: model emitters never set GPU bit 1) |
| Provenance | mesh and asset `extras.source` (file, hash, bank index, byte range); `extras.unresolved_primitives` lists anything not exported |

Output is deterministic: sorted JSON keys, fixed float packing, no timestamps. It writes `models/<id>.glb` plus `<id>.json` (the full neutral record).

## Scene manifest (`tools/btga_assets/scene.py`)

- **`files`:** each level file with its hash.
- **`section1`:** the loader's `first`/`count` selection.
- **`placements`:** for each one:
  - `id`, `index`, `placed_by_loader`, `position_raw`, `translation` (×4), `word_6` (yaw), `yaw_turns`, `kind`;
  - the raw section-3 range;
  - `model_index`/`model_id` when the kind's handler proves a model reference;
  - `status`: `resolved`, `no model`, an out-of-range index, or "kind handled by dedicated code".
- **`models`:** the model IDs used.
- **`floor`:** FLB dimension, cell index rule, `cell_size: null` (unresolved), shade histogram, and `collision: null`, `navigation: null`.
- **`dependency_problems`:** every missing file, out-of-range index or unresolved texture, stated explicitly.

## Comparison document (`btga-compare` v1, `tools/btga_assets/compare.py`)

- **`assets`:** `{id, platform, kind (texture|model|level|name), name, normalised_name, source, fingerprints}`.
  - Textures carry `width`, `height`, `rgba_sha256` and a 64-bit `dhash`.
  - Models carry `geometry_sha256`, `vertex_count`, `triangle_count`, `bounds_size` and `part_count`.
- **`links`:** `{a, b, confidence, status, evidence[{type, detail, weight}]}`.
  - Confidence combines evidence weights as `1 − Π(1 − w)`, capped at 0.99.
  - Matchers only create `candidate` links. `confirmed` requires `confirm(link, proof)` with a non-empty proof statement, and `document()` rejects any unproven confirmation.
- **Evidence types:**
  - identical decoded pixels (0.97);
  - dHash distance ≤ 6 bits at the same aspect ratio (0.75 − 0.08·d), plus equal dimensions (0.2);
  - identical geometry (0.95);
  - equal counts with matching bounds proportions (0.35 + 0.3);
  - normalised-name equality (0.4);
  - executable or ROM string references, which go in the catalog evidence.
- **`sources`:** image hashes only, never paths.

## Comparing a PS1 resource with an N64 resource

1. **Identity:** platform + `id`; never a path.
2. **Textures:** compare `rgba_sha256` first, then dHash with an aspect-ratio gate.
   - The palette is row 0; alternate rows need their own fingerprints.
   - N64 sources so far are the LZARI images and world textures from the repository's extractors.
3. **Models:** compare counts and bounds proportions, then geometry hashes. N64 model fingerprints need an N64 model decoder that emits this neutral form; that is the next dependency.
4. **Placements:** compare placement sets after fitting a scale. PS1 placements are in model units ×4.
5. **Names:** normalised tokens. String references prove only that a name is mentioned, not that the content is present.

## Version 2 changes (shared with the N64 adapters)

The N64 adapters required these contract changes. PS1 output now follows them too: `tools/btga_assets/ps1_adapter.py` feeds the shared exporter.

- **Shared mesh** (`tools/btga_assets/mesh.py`): every model carries a `mesh` (see `docs/STUDIO_RESOURCE_API.md`).
  - For PS1, `mesh.ps1_mesh` applies the same proven rules as the PS1 glTF path: quads split on c1–c3, cull flag 2 reversed, colour ÷ 128, UV ÷ texture size.
  - Triangles that cannot be represented go into `rejected_triangles` with a reason.
  - The PS1-specific record (commands, primitives, raw words) stays alongside the mesh.
- **Scene v2:** `instances` (model ID, translation, u16 yaw), `collision` (local boxes), `markers` (category plus raw fields), `environment`, `bounds`, `minimap`, `music`, `unresolved`.
  - PS1 footprint collision boxes are the CM01 footprint × 4 with a nominal height of 64. The PS1 files hold no collision height.
- **Content-derived N64 IDs:** an N64 model or texture ID hashes the ROM SHA-256 with the record's pool-reference content. Geometry shared between levels therefore keeps one ID. PS1 IDs keep the v1 rule.
- **Catalog** (`btga-catalog` v1): one directory per export, holding scenes, models, textures and inventories (see `docs/STUDIO_RESOURCE_API.md`).
- **Comparison v2** (`tools/compare_neutral_catalog.py`, `tools/btga_assets/cross_platform.py`):
  - Model candidates use scale-, rotation- and reflection-invariant shape signatures, with a separate mirror indicator.
  - Level candidates combine layout matching (across eight Y-up rotations and reflections, after a scale fit) with shared-model and name evidence.
  - A level link with only one kind of evidence is labelled `weak: needs visual inspection`.
  - Texture-only links are never promoted.

## Version 3 changes: world semantics (`semantics_version: 3`)

Scenes from every adapter gain typed, editable world records (`tools/btga_assets/semantics.py`). Each record uses the evidence words **proven**, **corroborated**, **inferred** and **unresolved**.

- **Collision shapes** carry the following fields:

  | Field | Contents |
  |---|---|
  | `shape` | `box`, `footprint` (planar oriented) or `aabb` (planar world-aligned) |
  | `space` | always `local`: `min`/`max` are relative to `translation` |
  | `orientation` | `oriented`, `axis_aligned_snapped`, `axis_aligned` or `axis_aligned_world` |
  | `vertical` | `{status, min, max, reference, evidence}` |
  | `flags`, `source`, `confidence`, `evidence` | provenance and runtime flags |

  - A height is present only when `vertical.status` is `proven`. Planar collision has `status: none_stored` and `None` heights.
  - This **replaces the v2 nominal height of 64** for PS1 and BattleTanx footprints. Both games' collision structures store no vertical extent.
  - The GLB collision layer draws planar shapes as an 8-unit display slab, and marks them through `vertical`.
- **Markers** are typed records with these fields:
  - `source_kind` (for example `ga_kind_13`, `bt1_marker_25`, `ps1_kind_30`)
  - `category`
  - `fields`: name → `{value, type, offset, range, evidence, meaning, enum?, enum_closed?, bitmask_bits?, reference?, sentinel?}`
  - `dependencies`, `restrictions` (mode, player-count and team rules)
  - `source`, `confidence`, `evidence`
  - `raw_hex` and `unknown_bytes` (record bytes no traced consumer reads)
  - A field is only `reference: "model"` when its consumer is a proven model lookup.
- **Minimap** records one of four statuses:
  - `proven`: N64 Global Assault, with `origin`, `scale`, `transform` and one `selections[]` entry per loading variant. Each entry has status `selected` (with an image-table descriptor and texture ID), `none` or `unchanged`.
  - `paired_by_name`: PS1 radar textures.
  - `unsupported`: BattleTanx.
- **Bounds:** `bounds.map` holds the proven map limits where code defines them:
  - Global Assault: the group-0 grid box.
  - BattleTanx: the collision half-extents.
  - PS1: the floor-lattice extent, labelled as such.
- **Floor (PS1):** `floor` is the FLB lattice, with spacing 512, the point and lookup transforms, every value, and a bit table that separates proven, corroborated and unresolved bits.
- **Navigation (BattleTanx):** `points[]` and `links[]`. Each link has corroborated `targets` and unresolved `values`.
- **Overlays:** edits never modify a scene. They are stored separately as `btga-overlay` v1 (`tools/btga_assets/overlay.py`; see `docs/STUDIO_RESOURCE_API.md`).
- **Catalog v2:**
  - Model entries carry `library`.
  - N64 Global Assault level entries list every `variants` label that loads the same stream set.
