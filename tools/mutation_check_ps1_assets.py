#!/usr/bin/env python3
"""Prove the asset tests catch deliberately broken PS1/N64 decoder and exporter logic.

Each mutation rewrites one snippet of one module in a temporary copy of the
tools and tests, runs tests.test_ps1_assets and tests.test_ps1_geometry
there, and expects a failure.  A surviving mutation means the tests do not
guard that logic.  The working tree is never modified.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORMATS = "tools/btga_assets/ps1_formats.py"
MODEL = "tools/btga_assets/ps1_model.py"
NEUTRAL = "tools/btga_assets/neutral.py"
GLTF = "tools/btga_assets/gltf.py"
SCENE = "tools/btga_assets/scene.py"
COMPARE = "tools/btga_assets/compare.py"
BINARY = "tools/btga_assets/n64_binary.py"
GBI = "tools/btga_assets/n64_gbi.py"
SOURCES = "tools/btga_assets/n64_sources.py"
BT1 = "tools/btga_assets/n64_bt1.py"
BTGA = "tools/btga_assets/n64_btga.py"
MESH = "tools/btga_assets/mesh.py"
EXPORT = "tools/btga_assets/export.py"
CROSS = "tools/btga_assets/cross_platform.py"
STUDIO = "tools/btga_assets/studio.py"
SEM = "tools/btga_assets/semantics.py"
GASEM = "tools/btga_assets/n64_btga_semantics.py"
BT1SEM = "tools/btga_assets/n64_bt1_semantics.py"
PS1SEM = "tools/btga_assets/ps1_semantics.py"
OVERLAY = "tools/btga_assets/overlay.py"
COPIED = (
    "tools/btga_assets",
    "tools/ps1_assets.py",
    "tools/inventory_ps1_assets.py",
    "tools/export_ps1_geometry.py",
    "tools/compare_btga_assets.py",
    "tools/btga_world.py",
    "tools/lzari.py",
    "tools/n64_texture.py",
    "tests/__init__.py",
    "tests/test_n64_assets.py",
    "tests/test_ps1_assets.py",
    "tests/test_ps1_geometry.py",
    "tests/test_world_semantics.py",
    "src/code/unknown/800B06E0.c",
)
TESTS = ("tests.test_ps1_assets", "tests.test_ps1_geometry", "tests.test_n64_assets", "tests.test_world_semantics")

# (file, name, original text, mutated text); every original must occur exactly once.
MUTATIONS = (
    (FORMATS, "bounds: allow reads past the end", "if offset < 0 or size < 0 or offset + size > len(self.data):",
     "if offset < 0 or size < 0 or offset + size > len(self.data) + 4:"),
    (FORMATS, "bounds: skip alignment", "        if offset % align:\n", "        if False:\n"),
    (FORMATS, "endian: big-endian u16", 'struct.unpack_from("<H", self.data, offset)', 'struct.unpack_from(">H", self.data, offset)'),
    (FORMATS, "endian: big-endian u32", 'struct.unpack_from("<I", self.data, offset)', 'struct.unpack_from(">I", self.data, offset)'),
    (FORMATS, "endian: big-endian s32", 'struct.unpack_from("<i", self.data, offset)', 'struct.unpack_from(">i", self.data, offset)'),
    (FORMATS, "loader: no word truncation", "texture = Texture(index, width, height, mode, header_offset, cursor, size, size & ~3)",
     "texture = Texture(index, width, height, mode, header_offset, cursor, size, size)"),
    (FORMATS, "loader: IMG no word truncation", "        cursor += size & ~3\n", "        cursor += size\n"),
    (FORMATS, "pixels: high nibble first", "values.append(byte & 0x0F if x % 2 == 0 else byte >> 4)",
     "values.append(byte >> 4 if x % 2 == 0 else byte & 0x0F)"),
    (FORMATS, "palette: 32-entry 4bpp CLUT", "PALETTE_ENTRIES = {MODE_4BPP: 16, MODE_8BPP: 256}",
     "PALETTE_ENTRIES = {MODE_4BPP: 32, MODE_8BPP: 256}"),
    (FORMATS, "palette: STP as transparency", "    if color == 0:\n        return bytes((0, 0, 0, 0))",
     "    if color & 0x8000 or color == 0:\n        return bytes((0, 0, 0, 0))"),
    (FORMATS, "palette: swap red and blue", "            _five_to_eight(color & 0x1F),\n            _five_to_eight((color >> 5) & 0x1F),\n            _five_to_eight((color >> 10) & 0x1F),",
     "            _five_to_eight((color >> 10) & 0x1F),\n            _five_to_eight((color >> 5) & 0x1F),\n            _five_to_eight(color & 0x1F),"),
    (FORMATS, "palette: ignore 3PAL count", "            if magic == PAL_MAGIC:", "            if magic != PAL_MAGIC:"),
    (FORMATS, "palette: skip pairing mode check", '        if texture.mode != record["mode"]:', "        if False:"),
    (FORMATS, "palette: skip pairing count check", '    if img["count"] != plt["count"]:', "    if False:"),
    (FORMATS, "models: wrong table stride", "    base = 8 + count * 12\n", "    base = 8 + count * 8\n"),
    (FORMATS, "models: skip terminator", "    if not words or words[-1] != MODEL_STREAM_END:", "    if not words:"),
    (FORMATS, "models: skip size check", "    if body_size != size - MODEL_HEADER_SIZE:", "    if False:"),
    (FORMATS, "floor: skip size check", "    if len(data) < expected:\n", "    if False:\n"),
    (FORMATS, "bin: skip record-span check", "    if span % 12:\n", "    if False:\n"),
    (FORMATS, "bin: skip placement target check", "        if target >= section3_size:", "        if False:"),
    (FORMATS, "bin: placement u32 read as s16", "        target = reader.u32(record + 8)", "        target = reader.u16(record + 8)"),
    (FORMATS, "vram: skip dimension limit", "    if halfwords > VRAM_HALFWORDS_WIDE or height > VRAM_LINES:", "    if False:"),
    (MODEL, "stream: accept opcodes past the table", "            if opcode > MAX_OPCODE:", "            if opcode > 0xFC:"),
    (MODEL, "stream: vertex bound off by one", "        if index >= self.vertex_count:", "        if index > self.vertex_count:"),
    (MODEL, "stream: swap pair corners", "    0x40: (1, 0),", "    0x40: (0, 1),"),
    (MODEL, "stream: normal Y/Z swapped", "sign_extend(word >> 16, 8) * 32, sign_extend(word >> 8, 8) * 32",
     "sign_extend(word >> 8, 8) * 32, sign_extend(word >> 16, 8) * 32"),
    (MODEL, "stream: flags ignored", "            if flags:\n                self.emit(command)", "            if False:\n                self.emit(command)"),
    (MODEL, "stream: UV bytes swapped", "uv = (operand & 0xFF, (operand >> 8) & 0xFF)", "uv = ((operand >> 8) & 0xFF, operand & 0xFF)"),
    (MODEL, "stream: mode from wrong nibble", "            self.mode = command.word >> 28", "            self.mode = (command.word >> 24) & 0xF"),
    (MODEL, "stream: texture slot from wrong byte", "            self.texture_slot = command.word >> 24", "            self.texture_slot = (command.word >> 16) & 0xFF"),
    (MODEL, "stream: block length from header end", "                block_end = command.offset + operand", "                block_end = cursor + operand"),
    (MODEL, "stream: short 0x6C header", "BLOCK_HEADERS = {0x64: (1, 1), 0x6C: (5, 1), 0x70: (6, 2)}", "BLOCK_HEADERS = {0x64: (1, 1), 0x6C: (4, 1), 0x70: (6, 2)}"),
    (MODEL, "stream: normals not reset", "            corner.normal = (0, 0, 0x1000)", "            corner.normal = (0, 0, 0)"),
    (MODEL, "stream: corner state not copied", "            corners=[corner.copy() for corner in self.corners],", "            corners=self.corners,"),
    (NEUTRAL, "winding: wrong quad diagonal", "        triangles = [(0, 1, 3), (1, 2, 3)]", "        triangles = [(0, 1, 2), (0, 2, 3)]"),
    (NEUTRAL, "winding: reverse the wrong cull flag", '    if primitive["cull"] == "front_screen_ccw":', '    if primitive["cull"] == "front_screen_cw":'),
    (NEUTRAL, "paths: allow traversal", '        if part in ("", ".", "..") or', '        if part in ("", ".") or'),
    (NEUTRAL, "ids: ignore record index", '    key = "|".join([platform, source_sha256, safe_source_path(path), kind, *map(str, indices)])',
     '    key = "|".join([platform, source_sha256, safe_source_path(path), kind])'),
    (GLTF, "gltf: half-texel UV offset", '[u / texture["width"], v / texture["height"]]', '[(u + 0.5) / texture["width"], (v + 0.5) / texture["height"]]'),
    (GLTF, "gltf: colour unity 255", "COLOUR_UNITY = 128.0", "COLOUR_UNITY = 255.0"),
    (GLTF, "gltf: never double-sided", '        double = primitive["cull"] == "double_sided"', "        double = False"),
    (GLTF, "gltf: 4096-unit yaw", "    theta = (angle_units & 0xFFFF) * 2.0 * math.pi / 65536.0", "    theta = (angle_units & 0xFFFF) * 2.0 * math.pi / 4096.0"),
    (SCENE, "scene: unscaled translation", '            "translation": [value * 4 for value in placement["position"]],', '            "translation": list(placement["position"]),'),
    (SCENE, "scene: kind 0 not a model kind", "MODEL_KINDS = (0, 1, 11, 12, 14, 22, 43)", "MODEL_KINDS = (1, 11, 12, 14, 22, 43)"),
    (SCENE, "scene: 0xFFFF treated as an index", "            if model_index == NO_MODEL:", "            if False:"),
    (COMPARE, "compare: confirm without proof", "    if not proof or not proof.strip():", "    if False:"),
    (COMPARE, "compare: accept unproven confirmations", '        if link["status"] == "confirmed" and not link.get("proof"):', "        if False:"),
    (BINARY, "n64: little-endian u16", 'struct.unpack_from(">H", self.data, offset)', 'struct.unpack_from("<H", self.data, offset)'),
    (BINARY, "n64: little-endian u32", 'struct.unpack_from(">I", self.data, offset)', 'struct.unpack_from("<I", self.data, offset)'),
    (BINARY, "n64: bounds off by one", "if offset < 0 or offset + size > len(self.data):", "if offset < 0 or offset + size > len(self.data) + 1:"),
    (BINARY, "n64: skip alignment", "        if offset % align:\n            raise FormatError(f\"{self.name}: {size}-byte read", "        if False:\n            raise FormatError(f\"{self.name}: {size}-byte read"),
    (GBI, "gbi: F3DEX2 vertex count field", "                count = (w0 >> 12) & 0xFF", "                count = (w0 >> 10) & 0x3F"),
    (GBI, "gbi: F3DEX2 first slot", "                first = ((w0 >> 1) & 0x7F) - count", "                first = (w0 >> 1) & 0x7F"),
    (GBI, "gbi: F3DEX first slot not halved", "                first = ((w0 >> 16) & 0xFF) // 2", "                first = (w0 >> 16) & 0xFF"),
    (GBI, "gbi: F3DEX triangle word", "            word = w0 if dialect is F3DEX2 else w1", "            word = w0"),
    (GBI, "gbi: skip unloaded slot check", "        if any(slot not in cache for slot in slots):", "        if False:"),
    (GBI, "gbi: F3DEX2 geometry mode ignores clear mask", "            state.geometry_mode = (state.geometry_mode & (w0 & 0xFFFFFF | 0xFF000000)) | w1", "            state.geometry_mode = state.geometry_mode | w1"),
    (GBI, "gbi: F3DEX2 othermode shift", "            shift = 32 - ((w0 >> 8) & 0xFF) - length", "            shift = (w0 >> 8) & 0xFF"),
    (GBI, "gbi: TLUT TMEM base", "            first = load[\"tmem\"] - 0x100", "            first = load[\"tmem\"] - 0x80"),
    (GBI, "gbi: CI4 ignores palette number", "        first, count = (tile.palette * 16, 16) if bits == 4 else (0, 256)", "        first, count = (0, 16) if bits == 4 else (0, 256)"),
    (GBI, "gbi: RGBA32 stride not doubled", "stride = tile.line * 8 * (2 if tile.siz == 3 else 1) if tile.line", "stride = tile.line * 8 if tile.line"),
    (GBI, "gbi: RGBA5551 alpha inverted", "255 if pixel & 1 else 0", "0 if pixel & 1 else 255"),
    (GBI, "gbi: 4bpp low nibble first", "value = (row[x // 2] >> (4 if x % 2 == 0 else 0)) & 0xF", "value = (row[x // 2] >> (0 if x % 2 == 0 else 4)) & 0xF"),
    (GBI, "gbi: tile line field", "        tile.line, tile.tmem = (w0 >> 9) & 0x1FF, w0 & 0x1FF", "        tile.line, tile.tmem = (w0 >> 10) & 0xFF, w0 & 0x1FF"),
    (SOURCES, "sources: DMA range not inclusive", "ranges.append((starts[slot], starts[slot] + ((ends[slot] - starts[slot] + 1) & ~1)))", "ranges.append((starts[slot], ends[slot]))"),
    (SOURCES, "sources: no case fallthrough", "        position += 1\n    return []", "        break\n    return []"),
    (SOURCES, "sources: flag condition inverted", '"if (a1) {": flag, "if (!a1) {": not flag', '"if (a1) {": not flag, "if (!a1) {": flag'),
    (BT1, "bt1: marker group start", "    first_active = 0 if group_index == -1 else 1", "    first_active = 0"),
    (BT1, "bt1: placement layout", 'PLACEMENT = struct.Struct(">BBBBHHIffHhhH")', 'PLACEMENT = struct.Struct(">BBBBHHIffhHhH")'),
    (BT1, "bt1: nav count halves", "    points, links = words[2] >> 16, words[2] & 0xFFFF", "    points, links = words[2] & 0xFFFF, words[2] >> 16"),
    (BT1, "bt1: skip model range check", "        if row[11] != -1 and not 0 <= row[11] < len(models):", "        if False:"),
    (BTGA, "ga: translation axes swapped", '        translation = [placement["x"], placement["y"], placement["z"]]', '        translation = [placement["x"], placement["z"], placement["y"]]'),
    (BTGA, "ga: kind 30 box modes swapped", "values, 0x01000000 if mode == 1 else 0x80000, source)", "values, 0x80000 if mode == 1 else 0x01000000, source)"),
    (BTGA, "ga: lanes ignore placement", '"x": [placement["x"] + values[1], placement["x"] + values[4]]', '"x": [values[1], values[4]]'),
    (BTGA, "ga: uv shift ignored", "    if 1 <= shift <= 10:\n        texel /= 1 << shift", "    if False:\n        texel /= 1 << shift"),
    (GASEM, "ga: model collision bounds", '[bounds_["min_x"], 0, bounds_["min_z"]]', '[bounds_["min_z"], 0, bounds_["min_x"]]'),
    (BTGA, "ga: content id ignores ROM", "{hashlib.sha256(f'{rom_sha256}|{kind}|{text}'.encode()).hexdigest()[:24]}", "{hashlib.sha256(f'{kind}|{text}'.encode()).hexdigest()[:24]}"),
    (MESH, "mesh: accept degenerate triangles", '        if len({corner["p"] for corner in clean}) != 3:', "        if False:"),
    (EXPORT, "export: allow unsafe IDs", "    if not _ID.match(record_id):", "    if False:"),
    (EXPORT, "export: inventory gaps tolerated", "    if total != len(rom.data):", "    if False:"),
    (CROSS, "cross: reflection sign", "        determinant = sx * sz * (-1 if swap else 1)", "        determinant = sx * sz"),
    (CROSS, "cross: shape threshold ignored", "    if d2 > 0.25 or ratio > 0.12 or count_ratio < 0.35:", "    if False:"),
    (STUDIO, "studio: yaw units", "return (int(units) & 0xFFFF) / 65536.0", "return (int(units) & 0xFFFF) / 4096.0"),
    (STUDIO, "studio: minimap edge inclusive", 'if 0 <= px < image["width"] and 0 <= py < image["height"]:', 'if 0 <= px <= image["width"] and 0 <= py <= image["height"]:'),
    # world semantics: minimap
    (GASEM, "minimap: scale uses the larger ratio", "scale = other if other < scale else scale", "scale = other if other > scale else scale"),
    (GASEM, "minimap: no degenerate fallback", "    if x1 == x0:\n        x0, z0, x1, z1 = -9000", "    if False:\n        x0, z0, x1, z1 = -9000"),
    (GASEM, "minimap: variant predicate inverted", "predicate = VARIANT_PREDICATE[condition]", "predicate = not VARIANT_PREDICATE[condition]"),
    (GASEM, "minimap: union ignores later bundles", "    for bundle in bundles[1:]:", "    for bundle in []:"),
    (GASEM, "minimap: unchanged reported as none", '        if level.level not in table:\n            entry.update({"status": "unchanged"', '        if level.level not in table:\n            entry.update({"status": "none"'),
    # world semantics: Global Assault markers and collision
    (GASEM, "ga marker: spawner mask treated as model", '], [12], {}, "proven"),', '], [6, 12], {}, "proven"),'),
    (GASEM, "ga marker: effect prop offset 6 as model", '], [2, 4, 8, 10, 12, 14], {"modes_only"', '], [2, 4, 6, 8, 10, 12, 14], {"modes_only"'),
    (GASEM, "ga marker: spawner budget offset", '("spawn_budget", 4, "u16"', '("spawn_budget", 6, "u16"'),
    (GASEM, "ga marker: embedded definition kind unchecked", '"status": "resolved" if found == expected_kind else', '"status": "resolved" if True else'),
    (GASEM, "ga collision: snap window widened", "if min((yaw - right) & 0xFFFF, (right - yaw) & 0xFFFF) <= 0x1000:", "if min((yaw - right) & 0xFFFF, (right - yaw) & 0xFFFF) <= 0x2000:"),
    (GASEM, "ga collision: snap mask ignored", "    if not flags & SNAP_MASK:", "    if False:"),
    (GASEM, "ga collision: pickup height relative", "0 - translation[1], 100 - translation[1]", "0, 100"),
    (GASEM, "ga collision: hazard sizes swapped", "hx = hz = (5, 150)[size]", "hx = hz = (150, 5)[size]"),
    (GASEM, "ga collision: model bottom forced to 0", '"min": bounds_["min_y"], "max": vertical_top', '"min": 0, "max": vertical_top'),
    # world semantics: BattleTanx
    (BT1SEM, "bt1: no wall margin", "WALL_MARGIN = 1.0", "WALL_MARGIN = 0.0"),
    (BT1SEM, "bt1: grid margin dropped", "GRID_MARGIN = 500", "GRID_MARGIN = 0"),
    (BT1SEM, "bt1: fence mask", "19: (0, 0x1A, WALL_MARGIN)", "19: (0, 0xF85F, WALL_MARGIN)"),
    (BT1SEM, "bt1: team nibble", '"team": field(p["subtype"] >> 4', '"team": field(p["subtype"] & 0xF'),
    (BT1SEM, "bt1: bounds edge exclusive", 'inside = bounds_["x"][0] <= x <= bounds_["x"][1]', 'inside = bounds_["x"][0] < x < bounds_["x"][1]'),
    (BT1SEM, "bt1: nav targets from odd fields", '"targets": [link[0], link[2], link[4]]', '"targets": [link[1], link[3], link[5]]'),
    (BT1SEM, "bt1: invented height", 'NO_VERTICAL = {"status": "none_stored", "min": None, "max": None, "reference": None,',
     'NO_VERTICAL = {"status": "proven", "min": 0, "max": 64, "reference": None,'),
    (BT1SEM, "bt1: generator gating dropped", 'restrictions={"only_when": "func_8007AAA0() == 1"}, **common)\n    if kind == 23:', 'restrictions={}, **common)\n    if kind == 23:'),
    # world semantics: PS1
    (PS1SEM, "ps1: lattice x not mirrored", "return ((256 - int(x)) >> 9) + dimension // 2", "return ((int(x) + 256) >> 9) + dimension // 2"),
    (PS1SEM, "ps1: lattice spacing 128", "LATTICE_SPACING = 512", "LATTICE_SPACING = 128"),
    (PS1SEM, "ps1: zone extent scale inverted", "return int(value * 64 / 72)", "return int(value * 72 / 64)"),
    (PS1SEM, "ps1: footprint not rotated", "rotated = [(x * c + z * s, -x * s + z * c) for x, z in corners]", "rotated = corners"),
    (PS1SEM, "ps1: kind 12 model at +2", "FOOTPRINT_KINDS = {0: 2, 1: 2, 11: 2, 12: 4,", "FOOTPRINT_KINDS = {0: 2, 1: 2, 11: 2, 12: 2,"),
    (PS1SEM, "ps1: spawner big-endian", 'u16 = lambda o: struct.unpack_from("<H", raw, o)[0]', 'u16 = lambda o: struct.unpack_from(">H", raw, o)[0]'),
    # world semantics: shared contract
    (SEM, "contract: bitmask bits ignored", '    if "bitmask_bits" in spec and value >> spec["bitmask_bits"]:', "    if False:"),
    (SEM, "contract: closed enum ignored", '    if spec.get("enum_closed") and str(value) not in spec["enum"]:', "    if False:"),
    (SEM, "contract: sentinels rejected", '    if str(value) in spec.get("sentinel", {}):\n        return []', '    if False:\n        return []'),
    (SEM, "contract: unproven heights allowed", 'if vertical["status"] != "proven" and (vertical.get("min")', 'if False and (vertical.get("min")'),
    # overlays
    (OVERLAY, "overlay: apply mutates source", "    out = copy.deepcopy(scene)", "    out = scene"),
    (OVERLAY, "overlay: source records deletable", 'if op == "delete" and record.get("source", {}).get("user_created"):', 'if op == "delete":'),
    (OVERLAY, "overlay: bounds unchecked", 'errors.append(_error(code, record["id"], problem, field=name))\n        if bounds:', 'errors.append(_error(code, record["id"], problem, field=name))\n        if False:'),
    (OVERLAY, "overlay: team errors unclassified", 'code = "INVALID_TEAM" if name in TEAM_FIELDS else "ILLEGAL_FIELD"', 'code = "ILLEGAL_FIELD"'),
    (OVERLAY, "overlay: overlap ignored", "if box[0] < ob[2] and ob[0] < box[2] and box[1] < ob[3] and ob[1] < box[3]:", "if False:"),
    (OVERLAY, "overlay: nav targets unchecked", "if not isinstance(target, int) or not 0 <= target < count:", "if False:"),
    (OVERLAY, "overlay: unsorted save", "text = json.dumps(document, sort_keys=True, indent=1", "text = json.dumps(document, sort_keys=False, indent=1"),
    (OVERLAY, "overlay: user IDs repeat", "        self.next_user_index += 1\n", "        pass\n"),
    (OVERLAY, "overlay: host paths saved", "        _refuse_host_paths(text)\n", "        pass\n"),
    (OVERLAY, "overlay: team limit raised", "        if team_counts[team] > 5:", "        if team_counts[team] > 6:"),
    (OVERLAY, "overlay: heights on planar shapes allowed", '(lo[1] is not None and shape.get("vertical", {}).get("status") != "proven")', "False"),
    (COMPARE, "compare: no aspect check", '            if abs(ofp["width"] / ofp["height"] - aspect) > 0.05 * aspect:', "            if False:"),
)


def run_tests(workdir: Path, corpus: str | None) -> bool:
    env = dict(os.environ)
    env.pop("BTGA_PS1_EXTRACTED", None)
    if corpus:
        env["BTGA_PS1_EXTRACTED"] = corpus
    result = subprocess.run(
        [sys.executable, "-m", "unittest", *TESTS],
        cwd=workdir,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", help="also run the gated real-corpus tests against this directory")
    args = parser.parse_args(argv)

    originals = {path: (ROOT / path).read_text(encoding="utf-8") for path in {item[0] for item in MUTATIONS}}
    with tempfile.TemporaryDirectory() as temp:
        work = Path(temp)
        for relative in COPIED:
            source = ROOT / relative
            target = work / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy2(source, target)
        # The output-guard tests ask git whether private-work/ is ignored.
        (work / ".gitignore").write_text("private-work/\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(work)], check=True)

        if not run_tests(work, args.corpus):
            print("baseline tests fail; refusing to judge mutations")
            return 2
        survivors = []
        for path, name, before, after in MUTATIONS:
            original = originals[path]
            if original.count(before) != 1:
                print(f"STALE {name}: original text occurs {original.count(before)} times")
                survivors.append(name)
                continue
            (work / path).write_text(original.replace(before, after), encoding="utf-8")
            passed = run_tests(work, args.corpus)
            (work / path).write_text(original, encoding="utf-8")
            print(f"{'SURVIVED' if passed else 'killed  '} {name}")
            if passed:
                survivors.append(name)
    print(f"{len(MUTATIONS) - len(survivors)}/{len(MUTATIONS)} mutations killed")
    return 1 if survivors else 0


if __name__ == "__main__":
    raise SystemExit(main())
