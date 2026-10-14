# PS1 asset formats (BattleTanx: Global Assault, SLUS-01044)

This document records what is proven about the PS1 release's file formats,
the evidence for each fact, and what remains unknown. The decoders live in
`tools/ps1_assets.py`; `tools/inventory_ps1_assets.py` runs them over an
extracted disc.

**Disc identity:** serial `SLUS_010.44`.
- Raw BIN SHA-256: `f27b4ca1741b25ba76fe1a86ecba5c9c714fc964c3070557a6a24f0b9f3dd709`.
- Executable SHA-256: `7bc99c17872dc244886ab85033be1596c206b298da450bea8c774851b4ab8f2f`.

**Executable layout:** a PS-X EXE that loads `0x800` bytes in, at `0x80010000`.
- Every address below is a RAM address in that image.
- File offset = address − `0x80010000` + `0x800`.

**Evidence levels:**
- **Proven:** read from the retail loader or renderer code.
- **Corroborated:** proven framing, with every corpus file agreeing on the meaning.
- **Hypothesis:** consistent with the data but not traced in code.
- **Rejected:** a hypothesis that was tested and failed.

All multi-byte values are little-endian. Reads are aligned to their width.

## Corpus summary

The disc holds 737 files, 598,581,392 bytes in total. Every file is classified, and none is rejected.

| Family | Files | Status |
|---|---:|---|
| `.TEX` texture bank | 256 | decoded (1,328 textures) |
| `.IMG` pixel bank + `.PLT` palette bank | 71 + 71 | decoded (4,269 textures) |
| `.MMM` model bank | 76 | decoded (4,761 models, 115,599 primitives) |
| `.TTT` model texture lists | 76 | decoded (9,524 references) |
| `.FLB` floor grid | 75 | structural |
| level `.BIN` | 75 | structural (30,758 placements) |
| `*LN.BIN` line tables | 4 | inventory only |
| `.STR` movies, `.PCK` audio, `.FUL` bitmaps, `.SFX` sound bank | 16, 4, 7, 1 | inventory only |
| executable, `SYSTEM.CNF`, license data, text | 1, 1, 1, 2 | inventory only |

### Level sets

- **Level-name table:** 70 pointers at `0x800A48E0`, holding SF1–SF6, Texas1–6, DC1–5, london1–6, paris1–6, berlin1–6, RUSSIA1–5 and 30 "M" variants. The loaders at `0x8003BDEC`, `0x8003BF04`, `0x8003D6B0` and `0x8003DE80` index it by level number. They build `assets\<name>` + `.TEX`, `.plt`, `.img`, `.MMM`, `.TTT`, `.flb` or `.bin`.
- **Radar textures** come from `assets\radar\<name>.TEX`.
- **Orphans:** `LOND1M`, `LOND2M`, `LOND3M`, `LOND6M` and `SF5M` have MMM/TTT/TEX/FLB/BIN files, but no IMG/PLT. They are not in the level-name table, and no string in the executable names them. These are unreferenced, incomplete level sets.
- **`ENUMS`** (MMM/TTT/IMG/PLT) is the shared model bank, loaded by the explicit path `assets\enums` (`0x8003D5B8`, `0x8003D98C`).

## Pixel modes and colours

The texture, IMG and PLT loaders all switch on the same mode word:

| Mode | Texels | Pixel bytes (loader formula) | Palette entries |
|---:|---|---|---:|
| 0 | 4-bit CLUT | `(width / 2) * height`, signed halving | 16 |
| 1 | 8-bit CLUT | `width * height` | 256 |
| 2 | 15-bit direct | `width * 2 * height` | none |

- **Mode 0 nibble order:** the low nibble is the left texel. That is the PS1 GPU's sampling order; no code was traced for it.
- **Cursor advance (proven):** the loaders advance the file cursor by the pixel size rounded down to a whole word (`srl 2; sll 2` at `0x8003CC00`, `0x8003CC58`, `0x8003CCAC` and `0x8003D838`). The full size is kept separately for the VRAM upload.
- **Colours** are PS1 15-bit:
  - Red is bits 0–4, green 5–9, blue 10–14; bit 15 is STP (semi-transparency).
  - The GPU does not draw texels whose value is `0x0000`, so previews make only that value transparent.
  - STP stays in the raw palette data.
- **Texture descriptor flags (proven, `func_8003D4B4`):**
  - Flag `0x8000` is set when palette entry 0 is `0x0000`.
  - Flag `0x4000` is set when the record has no palette.
  - 786 of 5,596 CLUT textures have a transparent entry 0.

## `.TEX`: self-contained texture bank

Proven from `func_8003CCE8` and `func_8003CEDC` (file walk) and `func_8003CB80` (per-record parse).

```
0x00  char[4]  magic        "3TEX" (or "3PAL", see below)
0x04  u32      count
0x08  record[count]:
      +0  u32  width
      +4  u32  height
      +8  u32  mode (0, 1, 2)
      +12 u8   pixels[size]           cursor advances by size & ~3
      [if magic == "3PAL": u32 palette_count]   else palette_count = 1
      u16 palettes[palette_count][16 or 256]    absent for mode 2
```

- **Magic check:** `func_8003CB80` compares the file's first word with `0x4C415033` (`"3PAL"`). The palette count is read only for that magic. The disc has no `3PAL` file, but the decoder supports and tests it.
- **Corpus:** all 256 files parse.
  - 254 end exactly at the file length.
  - Modes: 1,258 4bpp, 69 8bpp and one 16bpp record.
- **Loader quirk (proven):** `GANG/PIECE13.TEX` (18×87, 8bpp) and `GANG/PIECE_7.TEX` (154×25, 8bpp) have pixel sizes that are not a whole number of words.
  - Each file stores its palette immediately after the full pixel data, leaving 2 trailing bytes.
  - The loader reads the palette 2 bytes early. So in-game, entry 0 is the last two pixel bytes and every other entry is shifted down by one.
  - The manifest reports both offsets. Previews show the loader-effective palette, with an extra `.authored.png` showing the stored one.

## `.IMG` + `.PLT`: level and enum texture banks

Proven from `func_8003D5B8` (both files loaded and paired), `func_8003D0CC` (PLT walk) and the IMG walk at `0x8003D794`/`0x8003D858`.

```
.IMG                                     .PLT
0x00 "3IMG"                              0x00 "3PLT"
0x04 u32 count                           0x04 u32 count
0x08 record[count]:                      0x08 record[count]:
     u32 width, u32 height, u32 mode          u32 mode
     u8  pixels[size] (advance size & ~3)     [mode 0/1: u32 palette_count,
                                                u16 palettes[palette_count][16|256]]
```

- **Pairing:** IMG record *i* uses PLT record *i*.
  - The IMG mode sizes the pixels and the PLT mode sizes the palettes, so the decoder rejects mismatched counts or modes.
  - The loaders do not check the magics; the decoder does.
- **Multiple palettes:** when a PLT record has more than one palette, the palettes are uploaded as stacked CLUT rows (`func_8008D3B8` with height `palette_count`).
  - 148 records carry 9 palettes and one carries 8.
  - Which row a face uses is chosen by the model stream; it is not decoded.
- **Corpus:** all 71 pairs parse exactly to their file ends. All 4,269 records are 4bpp. Every CLUT index is within its palette.

## `.TTT`: per-model texture lists

Proven from `0x8003D9F8` (ENUMS) and `0x8003DB70` (level):

```
0x00 "TT01"
0x04 s32 count                      one list per MMM model slot
0x08 list[count]:
     char[4] "LBTX"                 pointer kept; tag not checked by the loader
     s32     n                      loader skips the list when n <= 0
     u32     img_index[n]           replaced in place by pointers to the paired
                                    IMG/PLT texture descriptors (stride 0x14)
```

- **Corpus:** in all 76 files the list count equals the matching MMM model count.
- **Index range:** every index is below the paired IMG count, except in the five orphan sets, which have no IMG.

## `.MMM`: model banks

The container is proven from `func_8003BF88`:

```
0x00 "MM01"
0x04 u32 count
0x08 slot[count]: s32 offset, u32, u32   offset -1 = empty slot; otherwise relative
                                          to 8 + 12*count. The loader overwrites all
                                          three words with {body, TTT list, model} pointers.
```

Each model starts at its offset. The record extends to the next model's start, or to the end of the file.

### Model record

```
+0x00 "CM01"
+0x04 s16 x_a   +0x08 s16 z_a   +0x0C s16 x_b   +0x10 s16 z_b
                footprint: read at 0x80025170 and rotated by the placement yaw at 0x800249A0
+0x22 s16 radius     culled at 0x80069DE8 as radius << 12 against 20.12 positions
other header bytes   unknown (kept raw)
+0x24 "3MDL" body; the slot's first word points here
  +0x04 u32 part_count     number of 32-byte matrices the type-3 enqueuer (0x80069C2C)
                           allocates; the per-part transform at 0x80092510 counts it down
  +0x08 u32 vertex_count   transformed by 0x8007E570 into vertex_count * 8 bytes of scratch
  +0x0C u32, +0x10 u32     not read by any traced path (+0x10 is 0x47F in every model)
  +0x14 u32 body_size      == record size - 0x24 in every model
  +0x18 vertex[vertex_count]:
        s16 x, s16 y      loaded as GTE VXY (0x8007E5E8)
        s16 z             loaded as GTE VZ
        u16 pad           bit 15 ends a part for 0x80092510; no other bit is ever set
  then the command stream: u32 words up to the end of the record
```

- **Footprint units:** the footprint is in quarter model units. Vertex extent ÷ footprint has a median of 4.0 on both axes, and the footprint is shifted left 14 bits into the same 20.12 space as placements.
- **Radius units:** the radius uses the same units as the footprint.
- **Multipart models:** these are rigid articulated parts. Each vertex group (ending at pad bit 15) is transformed by its own runtime-supplied matrix; the caller at `0x80073FF4` fills them.
  - 4,695 models carry exactly `part_count` end markers.
  - 66 carry one fewer. They are only safe on the single-matrix (type-4) path, which ignores parts.
- **No animation or skinning data** appears in the files.

### Command stream interpreter

The interpreter is `func_8007C0D4`, called from `0x80069314`. A second copy at `0x8007D580`, called from `0x80072224`, has identical handlers: only 6 words differ, and each is a branch back to that copy's own loop.

**Main loop (`0x8007C2D8`):**
1. Read a word.
2. `jalr 0x8007C960 + (word & 0xFC) * 2`, with operand = `word >> 8`.
3. Run `nclip`.
4. Look at `word & 3`:
   - `0`: continue to the next word.
   - `1`: emit the primitive if `MAC0 >= 0` (visible when clockwise on the PS1's y-down screen).
   - `2`: emit if `MAC0 < 0`.
   - `3`: emit regardless (double-sided).

**Model start:** the normals are reset to `(0, 0, 4096)` (`0x8007C298`–`0x8007C2C8`). The other corner registers persist across primitives: they are sticky state. Every corpus primitive is fully specified by its own model, with no state inherited from an earlier model.

**Proven opcode table.** Corner `k` is `(opcode >> 2) & 3`. Corner scratch addresses are relative to `0x1F800300`.

| Opcode | Handler | Meaning |
|---|---|---|
| `00 04 08 0C` | `0x8007D098`+0xC·k | corner colour: `RGB = operand` (R in bits 8–15) |
| `10 14 18 1C`, `90 94 98 9C` | `0x8007D0C8`+0x34·k | corner normal: `VX = word >>arith 19`, `VY = s8(bits 16–23)·32`, `VZ = s8(bits 8–15)·32`, with 4096 = 1.0 |
| `20 24 28 2C` | `0x8007D1A0`+0x10·k | corner UV: `u = bits 8–15`, `v = bits 16–23` (texels within the texture) |
| `80 84 88 8C` | `0x8007D198`+0x10·k | the same UV, also storing `word >> 22` to scratch `+0x40` (no reader found; kept raw) |
| `30 34 38 3C` | `0x8007D1D8`+0x24·k | corner vertex: index = 24-bit operand |
| `40 44 48 4C 50 54` | `0x8007D268`+0x48·i | two vertex indices (12 bits each): corners (1,0) (2,1) (3,2) (2,0) (3,0) (3,1) take (bits 8–19, bits 20–31) |
| `58` | `0x8007D4BC` | texture: slot = `word >> 24` into the model's TTT list. Loads the descriptor's UV offset, texture page (with the frame's blend bits) and CLUT, plus the per-instance CLUT row ×64 |
| `5C` | `0x8007D488` | render mode = `word >> 28`: bit 0 quad, bit 2 Gouraud, bit 3 textured. Bit 1 picks the same emitter, and the caller may force it. Bits 8–27 unused |
| `60 74 78 7C` | `0x8007D4B4`, `0x8007D578` | no operation |
| `64` | `0x8007D418` | visibility block `[op, mask]`: runs the body when `mask & M0`, otherwise jumps to `op + operand` |
| `6C` | `0x8007D438` | visibility block `[op, mask, 4 words]` (the 4 words are unread) |
| `70` | `0x8007D458` | visibility block `[op, mask0, mask1, 4 words]`: runs when `(mask0 & M0) \| (mask1 & M1)` |
| `68` | `0x8007D510` | end of model |
| `A0` | `0x8007D560` | depth-sort mode: `(word >> 16) & 3` |
| `A4`–`FC` | — | dispatch would land in the emitter table or code. The decoder rejects these |

Block and end handlers jump straight back to the loop or out of it, so their flag bits never emit. The masks `M0` and `M1` are runtime arguments (caller stack `0x50`/`0x54`).

**Emitters.** There are 16 pointers at `0x8007CAA8`, indexed by mode.

| Mode bits | GPU command | Packet vertex order |
|---|---|---|
| tri, flat / Gouraud | `0x20` / `0x30` | c0, c1, c2 |
| tri, textured flat / Gouraud | `0x24` / `0x34` | c0, c1, c2 |
| quad, flat / Gouraud | `0x28` / `0x38` | c0, c1, c3, c2 |
| quad, textured flat / Gouraud | `0x2C` / `0x3C` | c0, c1, c3, c2 |

- **Quad split:** the GPU rasterises quads as `(v0,v1,v2)` and `(v1,v2,v3)`, so the shared diagonal is c1–c3.
- **No semi-transparency:** no emitter sets GPU code bit 1. A separate overlay pass (`0x8007CE08`/`0x8007CF3C`) adds depth-cue primitives at runtime; these are not model data.
- **Lighting:** each corner's colour and normal feed GTE `NCCS` (`0x8007C40C`–`0x8007C548`). The vertex colour is a pre-lighting material colour, and the light matrix comes from the runtime.

**Corpus:**
- All 4,761 models decode.
- Every stream byte is accounted for.
- No primitive needs inherited state.
- Every textured primitive's slot is inside its TTT list.
- Every resolved UV lies inside the referenced texture.

| Measure | Values |
|---|---|
| Opcodes used | 0x00–0x0C, 0x10–0x1C, 0x20–0x2C, 0x30–0x3C, 0x40–0x50, 0x58, 0x5C, 0x68, 0x70, 0x90–0x9C |
| Primitives by GPU code | `0x34` 58,653; `0x3C` 56,770; `0x38` 97; `0x30` 79 (all Gouraud) |
| Primitives by cull flag | flag 1: 54,992; flag 2: 18,223; double-sided: 42,384 |
| Visibility blocks | 194 blocks (`0x70`) gating 307 primitives |

### Coordinates

| Property | Status | Evidence |
|---|---|---|
| Vertex format | proven | s16 x, y, z |
| Placement positions | proven | `s16 << 14` into 20.12, which is ×4 model units |
| Model unit = 20.12 integer unit | corroborated | the footprint relation above |
| +Y up | corroborated | the median model's lowest vertex is at y = 0 |
| Front faces counter-clockwise in a right-handed frame | corroborated | authored normals agree with the right-handed cross product of corners 0, 1, 2 for 98.8% of cull-flag-1 primitives, and disagree for 98.0% of cull-flag-2 primitives |
| No mirror | unproven | the camera matrix was not traced |

## `.FLB`: floor grid

Proven from `func_8003AF64`:

```
0x00 u16 dimension     the loader handles 0x40, 0x80 and 0x100 only
0x02 u16               not read by this loader (0x80 in 74 files; 0x100 in LONDON5)
0x04 u32 cell[dimension][dimension]
```

- **Values are lattice points, 512 model units apart (proven):**
  - `0x80039F80..0x8003A05C` store the viewer position rounded `>> 12` (model units) at `0x800D5B38` as the floor camera.
  - `0x8003AF64` emits floor vertices at `4 × coordinate − camera` (`0x8003B094..0x8003B0D8`) with a quad edge of `0x200`, and samples a value for each quad corner, `(coordinate >> 7) + dimension / 2`. A value therefore belongs to a point, not an area.
- **Object lookup (proven, `0x8003BADC`):** callers such as `0x8002CB08` pass an object's 20.12 position `>> 12`. The nearest point is `col = ((256 − x) >> 9) + dim/2`, `row = ((z + 256) >> 9) + dim/2`; **x is mirrored**. Point (col, row) sits at `x = −512 (col − dim/2)`, `z = 512 (row − dim/2)`; values are stored row by row.
- **Corroboration:** bits 15–21 are set 3–4× more often at points under placed models than across the grid, and more often under the mirrored mapping than an unmirrored one, for every such bit.

| Bits | Status | Meaning |
|---|---|---|
| 0–6 | proven | near floor vertex colour, `((c & 0x7C) << 19) \| ((c & 0x3E) << 11) \| ((c & 0x1F) << 3)` (`0x8003B100`) |
| 0–14 | proven | cleared (`c &= 0xFFFF8000`) by `0x8003BADC` under an object that is destroyed |
| 24–31 | proven | far floor colour: byte 3 of neighbouring points packed into one colour (`0x8003B828`, depth-cued by `func_8008BDF0`) |
| 15–21 | unresolved | correlated with placed models (above); no reader traced |
| 7–14, 22–23 | unresolved | raw |

- **Not established:** no traced path reads a height, walkability, blocking, material or navigation value from the lattice.
- **LONDON5 exception:** `LONDON5.FLB` declares 64 but stores 128 × 128 cells, so its tail is unreachable.

## Collision

Proven from the placement dispatcher `func_80025094` (table `0x800BD6A4`):

- **Which kinds:** kinds 0, 1, 11, 12, 14, 22 and 43 read the CM01 footprint `{x_a, z_a, x_b, z_b}` and call `0x800249A0`.
- **Rotation:** `0x800249A0` rotates the four corners, with y = 0, by the placement yaw and returns `[min_x, min_z, max_x, max_z]`.
- **Storage:** the dispatcher stores those values `<< 14` plus the 20.12 position at object `+0x2C..+0x38`.
- **Shape:** a world-aligned rectangle with **no vertical field**. Scenes export it as `shape: "aabb"` with `vertical.status = "none_stored"`, replacing the earlier 64-unit placeholder.
- **Corpus:** 20,703 collision rectangles.

## Level `.BIN`

Proven from `func_8002429C`, the placement loop at `0x8002457C` and the placement dispatcher `func_80025094`:

```
0x00 u32 offset[5]          byte offsets from file start, ascending
0x14 u32 x3                 zero in all 75 files
section 1 (offset[1]):  u16 count, u16 first   placements [first, first+count) are placed
                         s16 at +0x14, +0x18, +0x1A, +0x1E read by the loader (x64/72)
section 2 (offset[2]..offset[3]):  12-byte placement records
       +0 s16 x, +2 s16 y, +4 s16 z    shifted << 14 into 20.12 (x4 model units)
       +6 u16 yaw                      65536 per turn: 0x800249A0 builds
                                       [[c,0,s],[0,1,0],[-s,0,c]] from a quarter-sine table at 0x800B0EEC
       +8 u32                          byte offset into section 3
section 3: per-placement records; byte 0 = object kind (< 0x2C), dispatched through 0x800BD6A4
section 4 (offset[4]..end):        opaque
```

- **Model kinds:** kinds 0, 1, 11, 14, 22 and 43 read the u16 at section-3 +2 as a level MMM model index (`0xFFFF` = none). Kind 12 reads it at +4 (`0x80025768`) and skips `0xFFFF`. Kind 42 passes +2 to `func_80024D80`.
- **No-op kinds:** 2, 9, 18, 19, 23, 25, 33, 34, 36, 39, 40 and 41 jump straight to the loop tail `0x80026168`.
- **Typed kinds:**
  - **13, spawner:** `0x80025928 → func_80034CD4`, object class 14, 0x34 bytes. It stores b1, b2, the owner slot through table `0x800D3018`, the u16 +4 budget, the u16 +6 mask, the u32 +8 interval (doubled), and b +0xE maximum (0 → 255). This is the N64 layout; u16 +0xC (the N64 spawner model) is not read.
  - **27, hazard:** `func_80027F38(position, 0, 0x7F)`. No record byte is read, so the size index is always 0.
  - **30, zone:** inline at `0x80025BD0`. It reads s16 +2/+6/+8/+0xC, scales them by `trunc(v × 64 / 72)` (multiply by `0x38E38E39`, `>> 4`), and switches on byte 1 (0..4, table `0x800BD754`).
    - Mode 0 writes lane bounds (placement in model units / 4, plus the scaled extents) to the object at `0x800D2FE4 +0x14..+0x1A`.
    - Those bounds do not enclose the level (about 10% of placements fall inside), and their consumer is untraced.
- **Other kinds:** these go to dedicated handlers (`func_80024720`, `func_8002D6A0`, `func_8002A914`, `func_80029BF0` and others), and their records are kept raw with the handler address.
- **Kind numbering:** where both are traced, PS1 kind numbers match N64 Global Assault (kind 12's model at +4, kind 13's spawner layout, kind 30's extent offsets).
- **Yaw scope:** the yaw matrix is proven for the footprint and is stored as the object's matrix at object +0. It is used as the draw rotation, which is corroborated by the rendered scenes.
- **Corpus:**
  - 30,758 placements. Of the model-kind placements, 20,703 resolve, 225 have no model (25 at +2 and all 200 kind-12 records at +4), and 2 index past their bank.
  - Section-1 selection covers every record except in `LOND2M.BIN` (174 of 175).
  - The loader's section-1 reads land in section 2 in four files.

## Hypotheses (not yet proven)

- **Mode bit 1:** it may select a variant the interpreter does not distinguish, since the emitter table is identical for it.
- **Visibility masks** `M0`/`M1` probably carry per-instance damage or part states. The callers were not traced.
- **`.FUL`:** a u32 count followed by 16-bit images (`AMAA.FUL` = 4 + 320×256×2).
- **`*LN.BIN`:** glyph line tables paired with `BT14PT.TEX` and the other `*PT` textures.

## Rejected hypotheses

- **"Multi-record TEX files carry one shared palette":** rejected. Each record has its own palette, and the arithmetic matches every file exactly.
- **"The MMM stream's opcode is the high byte":** rejected. Read that way, terminators never line up; with the low byte, every model ends in `0x68`.
- **"Level-BIN section 1 contains its own bounds":** rejected. Four files have 16-byte section 1s that the loader reads past.
- **"FLB file size always equals `4 + dim² × 4`":** rejected by `LONDON5.FLB`.
- **"FLB cells are 128 world units":** rejected, because the placement extents exceed the grid. The proven spacing is 512 model units.
- **"PS1 collision has a recoverable height":** rejected. The object stores only the planar rectangle.
- **"`0x63`, `0x70`, `0x80` are part-delimiting opcodes":** rejected. `0x63` and `0x80` were mask words inside `0x70` block headers, which the interpreter skips as data.

## Unresolved

- **Model header:** `CM01` bytes other than the footprint and radius; `3MDL` `+0x0C` and `+0x10`.
- **Stream fields:** the extra words in `0x6C`/`0x70` headers, the `0x80`-family side value, the runtime sources of the visibility masks, and mode bit 1.
- **Coordinates:** the camera-space handedness (mirror question). The FLB lookup mirrors x relative to placements, which constrains but does not settle it.
- **FLB:** cell bits 7–14 and 15–23 (beyond the destroy-clear rule), and header word 2.
- **Level BIN:** section-3 records for non-model kinds other than 13, 27 and 30, zone modes 1–4, section 4, and the section-1 s16 reads.
- **Inventory-only families:** STR, PCK, FUL and SFX are not decoded.
