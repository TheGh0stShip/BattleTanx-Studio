# N64 asset formats: Global Assault and BattleTanx

This document records the N64 formats behind the neutral adapters in `tools/btga_assets/`: `n64_btga.py`, `n64_bt1.py`, `n64_gbi.py` and `n64_sources.py`.

**ROMs supported** (identified by SHA-1 only; never committed):

| Game | SHA-1 |
|---|---|
| BattleTanx: Global Assault, N64 USA Rev 0 | `805248fb0a0ee694cad8d7dc927b631d860dd8cf` |
| BattleTanx, N64 USA | `535860d941738ac1210c20a9b80114fea0e0ff17` |

**Address conventions:**
- Global Assault addresses refer to the matching decompilation under `src/code`, which matches the retail ROM byte for byte.
- BattleTanx addresses refer to its image, loaded at `0x80071000` from ROM `0x1000`.

**Evidence levels:**
- **Proven:** read from code.
- **Corroborated:** proven framing, with the whole corpus agreeing on the meaning.
- **Inferred:** a name or role chosen from how code uses a field, not stated by it (scene records use this word).
- **Hypothesis:** consistent with the data but not traced in code.
- **Unknown:** kept raw.

All data is big-endian with 32-bit words. Reads are bounds- and alignment-checked (`n64_binary.BEReader`).

## Global Assault

### Level streams

`func_800E8380(level, flag, …)` returns one or two LZARI world bundles per level. The bundle choice depends on `flag` (argument 1) and on whether `D_80117EB4 == 10`.

- **Evaluation:** `n64_sources.ga_level_streams` evaluates the matching C, including its fall-through cases. With the flag set, levels 17–24 continue into later cases and end on SAN FRANCISCO - BREAKOUT's base world plus `0x46B6A8`. That is the retail behaviour, not an adapter guess.
- **DMA size:** `func_800BA6C0` reads `((end - start) + 1) & ~1` bytes.
- **Corpus:** 27 levels yield 74 distinct streams, and every one decodes as an 8-offset world bundle.
- **Level names** come from the campaign map tables (`8011B318_hud_list_layout_data.c`). Levels 22, 23 and 25 have no title there and stay unnamed.

### World bundle

The seven-component layout is already documented in `docs/ASSET_FORMATS.md`. These facts are added here:

| Fact | Evidence |
|---|---|
| Bundles share one model, part and reference index space. Each bundle's `model_base` is the sum of the earlier bundles' counts. | `func_800BA6C0`; `LOOK()` in `func_800DFA5C` |
| Placement `(x, y, z)`: y is vertical. The loader passes `pos = (x, z, y)`, and the object matrix is built as translation `(x, y, z)`. | `func_800BA6C0`, `func_800DF0D0` |
| Yaw is a u16 with 65,536 units per turn. The matrix is `m00 = m22 = cos`, `m20 = sin`, `m02 = −sin` (row vectors), i.e. a rotation about +Y. | `func_8009EFD4`; sine table at `0x80114870` |
| A model's parts are LOD levels. A model with 2 parts switches at 300 units; one with 3 parts switches at 1,000 and 1,700. | `model_draw_select_lod`, `func_800AF978` |
| Each pool reference is three display lists: texture (pool `0x102C70`, `G_SETTIMG` relocated), state (`0x2F8070`) and geometry (`0x3013F0`, `G_VTX` relocated). A texture offset of −1 means no texture. | `func_800BA6C0`, `func_8007B1F0` |

### Definition kinds (`func_800DFA5C`)

| Kinds | Behaviour | Neutral output |
|---|---|---|
| 0, 1, 2, 11, 14, 22, 34, 35, 43, 44 | draw the model at definition +2 | instance |
| 1, 2 (if the model's `max_y` ≥ 2), 11, 14, 22, 35, 42, 43 | `func_800B1898` grid box from the model bounds. Flags: 1, 0x20, 0x40, 0x4000, 0x80; kind 43's top is absolute 5,000. Kind 42 is collision-only (not drawn). | collision box |
| 30, mode 0 | per-group lane bounds, `x0..x1` and `z0..z1` relative to the placement (`world->lanes[group]`) | `bounds.lanes` |
| 30, modes 1/2 | explicit box, flags `0x01000000` / `0x80000`, node y = placement y | collision box |
| 30, modes 3/4 | `func_800E9990` mission trigger box | typed marker `mission_trigger` |
| 37 | bytes 1–3: clear and fog colour (`func_80079C00` → `G_SETFILLCOLOR` and `G_SETFOGCOLOR` in `func_8007A0A0`); 4–15: twelve light bytes (`func_800A9C24`); 16–21: two sky colours (`func_800F7150`) | environment |
| 38 | per-area colour (`func_800A9BF0`) | environment |
| 39 | conditional include; both branches followed, condition recorded | instance or marker with conditions |
| handler kinds | typed from the handler that reads them (next section) | typed marker |
| 9, 25, 33 | no case in the dispatcher | marker `ignored_by_dispatcher` (raw) |

- **Fog range:** fixed by `G_MOVEWORD 0x64009D00` in `func_8007A0A0`.

### Collision volumes

All Global Assault static collision goes through `func_800B1898(data, x, z, y, dx0, dx1, dz0, dz1, y_min, y_max, yaw, flags, group)`.

| Fact | Evidence |
|---|---|
| A node stores y plus a vertical range; overlap tests compare `node.y + y_min .. node.y + y_max` | `800B33FC_grid_overlap_query.c` |
| Model boxes pass node y = 0 and `(model y0 + placement y, model y1 + placement y)`, i.e. the model's own vertical bounds at the placement | `func_800DFA5C` (`B13`, cases 1/2/11/14/22/35/42/43) |
| Kind 43's top is absolute 5,000 | case 43 |
| Kind-30 boxes pass node y = placement y and range `p[2]..p[5]` (placement-relative) | case 30 |
| Pickups (kind 16) pass node y = 0 and range 0..100 (absolute); hazards (kind 27) pass node y = placement height and 0..10 | `func_800E9B50`, `func_800F1B30` |
| Flags in `0x039BEF67` snap the yaw to the nearest right angle when within `0x1000` (22.5°) and rotate the box to axis-aligned; flag `0x800` forces yaw 0; other flags (kind 22's `0x80`) keep an oriented box | `func_800B1898` |
| Grid cells are 1,024 units (`>> 10`), one grid per world group | `func_800B07D4` |

Neutral boxes are placement-relative (`min`/`max` around `translation`), with `vertical.status = "proven"` and `orientation` = `axis_aligned_snapped` / `oriented`.

### Minimap selection

The HUD minimap (`func_800C7C10`) draws `D_80397804` with origin `D_803977F0/F2` and scale `D_80397800`. These are fields of grid 0 (`Grid` at `D_803977F0`: `x0, z0, w, h, cw, ch, cells, scale, tbl`), built by `func_800B07D4` from world group 0 every time a world loads:

- **Box:** world group 0's box is the union of every bundle's group-0 header box; a degenerate box (`max_x == min_x`) becomes ±9,000 (`func_800BA6C0`).
- **Scale:** `min(70.0f / w, 70.0f / h)` with `w`, `h` as u16.
- **Image:** `switch (D_8021949C)` (the level), in two tables chosen by `func_8009D144()` (game mode `D_802194A0` in 7..14). The same predicate is the stream-variant flag `func_800AF43C` passes to `func_800E8380`.
- **Descriptors:** `D_801179FC`..`D_80117DB4` are entries 161–195 of the image table at RAM `0x80116860` (ROM `0xA6860`, 28 bytes each). All are CI4.
- **Missing images:** level 25 stores 0 (no minimap). Levels 17–23 have no case in the mode-7..14 table, so the grid keeps the previous level's pointer (`unchanged`).
- **Transform:** a world point `(x, z)` is plotted at `trunc((x − x0) × scale), trunc((z − z0) × scale)` and drawn only inside the image's width and height. No rotation or flip. Frame drawn when `D_80117EB0 < 3`; primitive alpha `0x96`.

| func_8009D144() | Level → image-table index |
|---|---|
| false | 0/24 185, 1 194, 2 187, 3 175, 4 174, 5 195, 6 181, 7 193, 8 192, 9 168, 10 172, 11 177, 12 166, 13 170, 14 179, 15 184, 16 161, 17 162, 18 163, 19 164, 20/26 188, 21 189, 22/23 190, 25 none |
| true | 0/24 185, 1 194, 2 186, 3 175, 4 173, 5 195, 6 180, 7 193, 8 191, 9 167, 10 171, 11 176, 12 165, 13 169, 14 178, 15 183, 16 161, 26 182, 25 none, 17–23 unchanged |

**Corroboration:** for 56 of the 71 selected (level, variant) images, the scaled box differs from the image size by at most 6 pixels. The 15 others are mostly `D_80117EB4 == 10` variants, whose extra bundle widens the box beyond the image (those markers are clipped in game), plus level 24's degenerate ±9,000 box.

Scenes carry `minimap.selections[]`, one per variant label that loads the stream set, with the image's table index, descriptor address, texture ID, ROM range, size and format.

### Typed markers

Each handler kind's fields are typed from the code that reads them. `func_800DF758` is the model lookup (`ModelOut` table, bundle `model_base` + index); only fields passed to it or to `LOOK()` are models.

| Kind | Handler | Proven fields |
|---|---|---|
| 13 spawner | `func_800DE930`, `func_800DEB1C` | +1 spawn mode (2/3 every check; 3 flags units 0x40; 4 needs player flag 4); +2 unit parameter; +3 owner slot (`D_80219582[]`); u16 +4 spawn budget (0 disables, 0xFFFF unlimited; shared by linked spawners); u16 +6 **unit-type mask** (bits 0–14, a random set bit is spawned); u32 +8 interval (ticks of `D_8021945C`); u16 +0xC model; +0xE maximum spawned (0 → 255) |
| 16 pickup | `func_800E9B50` | +1/+2 box half-extents, +3 pickup type; not created in modes 0, 1, 2, 3, 6, 8, 11, 12, 13 |
| 21 objective | `func_800E9E80` | models +2..+10; +12 draw band; s32 +16 offset of an embedded kind-13 spawner (−1 none); +20 class (1/2 count toward `D_802194B0[0]`; 3 material tag) |
| 27 hazard | `func_800F1B30` | +1 nonzero selects half-extent 150 instead of 5 (`D_80125AA0`) |
| 28 powerup pad | `func_800F2BC0` | models +2, +4, +6 |
| 31 multiplayer structure | `func_800E80F4` | +1 team slot (below player count `D_802194A6`); u16 +2 model (mode 11 only); s32 +4 offset of an embedded kind-3 structure; created only in modes 1, 3, 11 |
| 32 effect prop | `func_800F5298` | +1 owner slot; models +2, +4, +8, +10, +12, +14; **byte +6 is a float parameter, not a model**; created only in modes 4/10 or when `D_80117EB4 == 10` |
| 3, 4, 5, 10, 15, 20, 24, 28, 29, 36, 40 | structure, turret emplacement, foliage, destructible, building, crate, selection, pad, barrel, flicker, static prop | model offsets and the bytes listed in `n64_btga_semantics.SCHEMAS` |
| 7, 12, 17, 45 | dispatcher cases | turret mode / team slot, effect endpoint index/value, effect node enable bit / slot |
| 6, 8, 23, 26, 41 | `func_800B03F4`, bridge, `__dummy`, wave surface, artillery | untraced: raw bytes only |

**Game modes:** `D_802194A0` values are recorded numerically; no names are assigned.

**Corpus:**
- All 58 scenes: 0 unresolved model lookups (previously 254 out-of-range "model index" values), 28,746 resolved model dependencies, and every source field value inside its typed range.
- All 84 kind-31 offsets resolve to kind-3 records.
- Spawner placements occur only in the `func_8009D144()` stream variants.

### Display lists and textures (`n64_gbi.py`)

- **Geometry:**
  - The 32-entry vertex cache is simulated over `G_VTX`, `G_TRI1` and `G_TRI2`.
  - A 16-byte vertex is `s16 x, y, z; u16 flag; s16 s, t; 4 bytes` (RGBA, or a signed normal when `G_LIGHTING` is set).
- **Texture state:**
  - `G_SETTIMG`, `G_SETTILE`, `G_SETTILESIZE`, `G_LOADBLOCK`/`G_LOADTILE` and `G_LOADTLUT` are tracked. TLUT entries are placed by TMEM address from `0x100`.
  - A CI4 tile's palette number selects its 16-entry bank. The palette type comes from othermode-H bits 14–15.
  - An RGBA32 tile's `line` counts 16-bit rows (the RDP splits 32-bit texels), so its DRAM stride is twice `line × 8` bytes.
  - Supported texture formats: RGBA16, RGBA32, CI4, CI8, IA4, IA8, IA16, I4 and I8.
- **UV:** texel = `st / 32 × scale` (where `0xFFFF` = 1.0), then the tile shift (shifts 1–10 divide, 11–15 multiply), minus the tile origin. UV = texel ÷ texture size.
- **Culling:**
  - `G_CULL_BACK` alone means counter-clockwise front.
  - Front-only culling reverses the winding.
  - Neither flag means double-sided.
  - Corroborated: authored normals agree with right-handed CCW winding for 24,217 of 24,279 single-sided lit Global Assault triangles (99.7%).
- **Corpus:** every Global Assault level decodes with 0 rejected triangles and 0 unknown commands. Of 1,138 world textures, 1,136 decode. The two exceptions are already documented in `docs/ASSET_FORMATS.md`: one tile is sampled larger than its stored payload, and one overlapping range lacks tile commands.

### Other ranges

- **Common world** (`0x3F6EE8`–`0x3F9B5C`): 244 shared models, exported as the `common_world` library.
- **Image table** (`0xA6860`): 195 images, decoded with `tools/n64_texture.py`.
- **Minimap:** proven; see [Minimap selection](#minimap-selection).
- **Music:** no level-to-song selection has been traced. Marked unsupported.
- **ROM coverage:** the inventory (`export.ga_inventory`) reuses `tools/inventory_rom_layout.py`, which accounts for all 8 MiB.

## BattleTanx

### Streams and scenarios

- **Stream table:** the initialiser `0x800865E0` fills the 28-slot table at `0x80125A68` with `{world start, world end, nav start, nav end}`. The adapter simulates it from the ROM.
  - It yields 22 worlds and 18 navigation bundles.
  - DMA size is `(end − start + 7) & ~7` (`func_80088CF0`).
- **Scenario table** (`0x80142DE8`, 17 × 0x220 bytes): `u32 mode, u32 world, u32 sequence, u32`. `func_8007B9E4` handles mode 7 as the bonus round, reading the kill target at +0x208 and a difficulty index at +0x217.
  - The mode-7 records are 4, 8, 12 and 15. Record 8 is world 9, Chicago Bonus, with kill target 5.

### World bundle (`func_80088CF0`)

The bundle starts with nine u32 offsets, followed by eight sections:

| Section | Layout | Evidence |
|---|---|---|
| 0 | 0x2E0-byte header: f32 bounds at +0x00 and +0x10, flags at +0x20, group count at +0x27C, marker-group index at +0x280. The rest is kept raw. | words copied to the level state; `0x800878C8` |
| 1 | 20-byte groups: `u16 count, u16 first, f32 × 4` | `func_8008785C` |
| 2 | 28-byte placements: `u8 flags, u8, u8 subtype, u8, u16, u16, u32 type, f32 x, f32 z, u16 yaw, s16 model_set, s16 model, u16` | `0x80089484`–`0x800895A8` |
| 3 | 12-byte models: `u8 parts, u8, u16 first part, s16 x0, z0, x1, z1` (index below 450) | `func_80087634`; footprint at `0x800895E8` |
| 4 | 4-byte model sets: `u8 count, u8, u16 first model` (index below 64) | `func_80087798` |
| 5 | 4-byte parts: `u8 refs, u8, u16 first ref` (index below 600) | `func_800874F4` |
| 6 | 16-byte references: `u32 texture offset, u16, u16 size, u32 geometry offset, u32 size`. Pools are texture `0x5A1110` and geometry `0x3EA970`, both F3DEX. | `func_8008716C`; copies at `0x8008AA00` and `0x8008AB14` |
| 7 | 16-byte records, counted but empty in every retail world | `func_80088CF0` |

- **Groups:** groups are walked from record 0 when header +0x280 is −1, otherwise from record 1.
  - The group that +0x280 names is the marker group. `func_80088670` dispatches its types 10–25 through the table at `0x80071768`.
  - World 9's three type-21 generators are at x = 141, −1,385 and 1,645 (z ≈ −2,800). See [BattleTanx markers](#battletanx-markers).
  - Every placement in all 22 worlds belongs to exactly one active or marker group.
- **Instance matrix:** `func_800A76BC` builds translation `(x, 0, z)` and the same yaw matrix as Global Assault (`func_800F3B40` cos, `func_800F3AE0` sin).
- **Collision:** see [BattleTanx collision](#battletanx-collision).
- **Navigation** (`func_800DDA08`, LZARI):
  - Word 2 packs `u16 points, u16 links`.
  - Points are f32 `(x, z)` pairs, corroborated by their coordinates lying inside the map. World 9 has 115 points and 220 links.
  - Links are 12 bytes. Fields 0, 2 and 4 are point targets: corroborated, as every value in all 18 bundles is below the point count. Fields 1, 3 and 5 are unresolved.
- **Corpus:** all 22 worlds decode with 0 rejected triangles and 0 unknown commands. Every BattleTanx texture decodes, apart from 18 material lists that load no texture.

### BattleTanx collision

Proven from the placement loop `0x80089370..0x8008A758`:

- **Registration:** footprints go to `func_80106D18(owner, x, z, x0, x1, z0, z1, mask, yaw)`.
- **No height:** the 40-byte node (`0x803B8248`) and `func_80109268` store only x, z, the four extents, the yaw and a radius. Collision is a planar oriented rectangle; there is no height to recover.
- **Dead corners:** the corner and radius computation at `0x800895E8..0x80089A74` is dead; its results are never stored.

| Types | Registration |
|---|---|
| 1 | owner 0, mask `0xF85F`, footprint grown by 1.0 (`0x80071894`) |
| 19, 27 | owner 0, mask `0x1A`, grown by 1.0 |
| 26 | owner 1, mask `0xF85F` |
| 30 | owner 2, mask `0xF85F` |
| 8 | actor at y 30.0 via `func_800ECC1C`, mask `0xF85F` |
| 6 (subtype bit 0x10), 9, 15 | actor helpers `func_800F05F4`, `func_800907CC`, `func_80091D6C` with the model footprint |
| 3, 5, 20, 31 | actor helpers whose footprint arguments are untraced (listed in `unresolved`) |

- **Map limits:** `0x80089064` calls `func_80106B60(trunc(−h[+0x10]), trunc(−h[+0x14]))`, which stores value + 500 as half-extents. `func_80106D18` refuses positions outside them, and nodes are bucketed in 2,048-unit cells.
  - 194 collision placements across the corpus lie outside and are exported with `registered: false`.
- **Gating:**
  - When `func_8007AAA0() >= 3`, the table at `0x80071898` rewrites types 3, 6, 9, 15 and 18 to type 7 and disables them.
  - Flag bits 0x10/0x40 consult `func_8007C6D8`.
  - Bit 0x2 compares team table `0x801260A4[(flags & 0xC) >> 2]` with `func_8007AAB0()`.

### BattleTanx markers

| Type (group) | Handler | Typed fields |
|---|---|---|
| 25 (marker) | `0x800887B0` | team = subtype >> 4 (≤ 5 per team), slot kind = subtype & 0xF, byte 5, bits 12–13 of u16 +4 |
| 22 (marker) | `0x800889D0` → `func_800A1408` | u16 +4: bit 0 requires `func_8007C728`, bits 1–2 team index, bits 3–4 variant; byte 5 bit 0 |
| 21 (marker) | `0x80088AA0` → `func_8010D480` | u16 +4 parameter, subtype; only when `func_8007AAA0() == 1` (role "tank generator" inferred) |
| 23, 24 (marker) | `func_8010DAA0`, `func_800A7550` | single-player only; type 24 passes s16 +4 |
| 10 (marker) | `0x8008883C` | subtype 22 adds to a point list (≤ 100) |
| 11–20 (marker) | none | fall through to the loop end |
| 16, 17 (active) | `0x80089C7C`, `0x80089CD4` | subtype indexes the 20-byte records at level state +0xC8 (flag, x, z) |

**Validation maps:**
- World 9 is Chicago Bonus.
- World 11 is the densest (2,044 placements).
- World 4 is the narrowest (aspect 7.7).
- Worlds 5 and 17 are mode-7 bonus arenas.

All five export planar collision and typed markers.

### Hypotheses and unresolved

| Area | Status |
|---|---|
| Pool ends | taken from the next cartridge address the code references (hypothesis) |
| Model parts | role (LOD or multipart) not traced, so all parts are exported, part 0 first |
| Header colour blocks (+0x24..+0xD0) | copied by the loader; meaning untraced |
| Placement bytes 1/3, words at +4/+6/+0x1A | kept raw |
| Navigation link fields 1/3/5 and the two byte tables | kept raw |
| Footprints of actor types 3, 5, 20, 31 | created inside untraced helpers |
| Game-mode and `func_8007AAA0`/`func_8007AAB0` meanings | recorded numerically |
| Minimap, music | unsupported (no minimap source traced) |

## Rejected interpretations

- **"BattleTanx worlds use Global Assault's 7-component bundle":** rejected. BattleTanx has 9 offsets and different record sizes.
- **"Kind 38 is the fog colour":** rejected. Kind 38 is a per-area colour; fog and clear colour come from kind 37, bytes 1–3.
- **"Every `model_index_*` field is a model":** rejected. Kind 13's u16 at +6 is a unit-type mask (`func_800DE930`), kind 13's u16 at +4 a spawn budget, and kind 32's byte at +6 a float parameter (`func_800F5298`).
- **"BattleTanx footprints have a height we can recover":** rejected. The collision node has no vertical field.
- **"The kind-30 box height is a hypothesis":** superseded. `func_800B33FC` adds node y = placement y.
- **"One TLUT load per texture":** rejected. Textures load up to six CI4 banks into successive TMEM slots.
