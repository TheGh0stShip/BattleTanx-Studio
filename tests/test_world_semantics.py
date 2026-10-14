"""World-semantics tests: minimaps, collision, typed markers, overlays, Studio API.

Synthetic fixtures cover every decoded structure.  Real-ROM/disc tests are
gated on BTGA_N64_ROM, BTGA_BT1_ROM and BTGA_PS1_EXTRACTED.
"""

from __future__ import annotations

import json
import os
import re
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.btga_assets import n64_bt1_semantics as bt1  # noqa: E402
from tools.btga_assets import n64_btga_semantics as ga  # noqa: E402
from tools.btga_assets import overlay as ov  # noqa: E402
from tools.btga_assets import ps1_semantics as ps1  # noqa: E402
from tools.btga_assets.semantics import SemanticsError, collision, field, field_errors, marker  # noqa: E402

SHA = "ab" * 32


def ga_bundle(index, box, definitions=b"", offsets=None):
    groups = [] if box is None else [{"min_x": box[0], "min_z": box[1], "max_x": box[2], "max_z": box[3]}]
    world = {"groups": groups, "offsets": offsets or [0, 0, 0, 0, len(definitions)], "models": [{}] * 40}
    return SimpleNamespace(index=index, world=world, data=definitions, rom_range=(0, len(definitions)))


def ga_level(level, *boxes):
    return SimpleNamespace(level=level, variant="default", bundles=[ga_bundle(i, b) for i, b in enumerate(boxes)])


IMAGES = {i: {"table_index": i, "start": 0x1000 + i, "end": 0x1100 + i, "width": 72, "height": 71, "format": 2, "size": 0} for i in range(161, 196)}


class MinimapTests(unittest.TestCase):
    def test_selection_table_matches_decompilation(self):
        source = (ROOT / "src/code/unknown/800B06E0.c").read_text()
        body = source[source.index("if (func_8009D144())"):source.index("void func_800B0B8C")]
        halves = body.split("} else {", 1)

        def parse(text):
            out, pending = {}, []
            for line in text.splitlines():
                case = re.match(r"\s*case (\d+):", line)
                if case:
                    pending.append(int(case.group(1)))
                    continue
                store = re.match(r"\s*g->tbl = (?:D_([0-9A-F]+)|0);", line)
                if store:
                    for level in pending:
                        out[level] = None if store.group(1) is None else (int(store.group(1), 16) - ga.IMAGE_TABLE_RAM) // 28
                    pending = []
            return out

        self.assertEqual(parse(halves[0]), ga.MINIMAP_SELECTION[True])
        self.assertEqual(parse(halves[1]), ga.MINIMAP_SELECTION[False])

    def test_area_box_union_and_degenerate_fallback(self):
        self.assertEqual(ga.area0_box(ga_level(1, (-100, -50, 300, 200), (-400, 0, 100, 500)).bundles), (-400, -50, 300, 500))
        self.assertEqual(ga.area0_box(ga_level(1, (0, 0, 0, 0)).bundles), (-9000, -9000, 9000, 9000))
        self.assertEqual(ga.area0_box(ga_level(1, (5, 5, 5, 9)).bundles), (-9000, -9000, 9000, 9000))

    def test_scale_origin_and_variant_selection(self):
        record = ga.minimap_record(ga_level(2, (-1000, -2000, 1000, 1500)), IMAGES, SHA, ["default", "flag"])
        self.assertEqual(record["origin"], [-1000, -2000])
        self.assertEqual(record["scale"], struct.unpack(">f", struct.pack(">f", 70.0 / 3500))[0])
        chosen = {s["variant"]: s for s in record["selections"]}
        self.assertEqual(chosen["default"]["image"]["table_index"], 187)
        self.assertEqual(chosen["flag"]["image"]["table_index"], 186)
        self.assertEqual(chosen["flag"]["image"]["descriptor_ram"], "0x80117CB8")
        self.assertEqual(chosen["default"]["fit"]["world_span_pixels"], [40.0, 70.0])

    def test_none_and_unchanged_are_explicit(self):
        none = ga.minimap_record(ga_level(25, (0, 0, 10, 10)), IMAGES, SHA, ["default"])
        self.assertEqual(none["selections"][0]["status"], "none")
        stale = ga.minimap_record(ga_level(19, (0, 0, 10, 10)), IMAGES, SHA, ["flag"])
        self.assertEqual(stale["selections"][0]["status"], "unchanged")
        self.assertIsNone(stale["selections"][0]["image"])

    def test_studio_world_to_minimap(self):
        from tools.btga_assets.studio import StudioLibrary

        mapping = {"origin": [-1000, -2000], "scale": 0.02, "selection": {"image": {"width": 40, "height": 70}}}
        self.assertEqual(StudioLibrary.world_to_minimap(mapping, -1000, -2000), (0, 0))
        self.assertEqual(StudioLibrary.world_to_minimap(mapping, 999, 1499), (39, 69))
        self.assertIsNone(StudioLibrary.world_to_minimap(mapping, 1000, 0))
        self.assertIsNone(StudioLibrary.world_to_minimap(mapping, -1100, 0))


class GlobalAssaultCollisionTests(unittest.TestCase):
    def test_orientation_rule(self):
        self.assertEqual(ga.orientation_for(0x80, 0x1234), ("oriented", 0x1234))
        self.assertEqual(ga.orientation_for(0x80, 0x0800), ("oriented", 0x0800))  # kind 22 boxes never snap
        self.assertEqual(ga.orientation_for(1, 0x0F00), ("axis_aligned_snapped", 0))
        self.assertEqual(ga.orientation_for(1, 0xF100), ("axis_aligned_snapped", 0))
        self.assertEqual(ga.orientation_for(1, 0x5000), ("axis_aligned_snapped", 0x4000))
        self.assertEqual(ga.orientation_for(1, 0x2000), ("oriented", 0x2000))
        self.assertEqual(ga.orientation_for(0x800, 0x4000), ("axis_aligned", 0))

    def test_model_box_heights_follow_model_bounds(self):
        bounds = {"min_x": -8, "min_y": -2, "min_z": -9, "max_x": 8, "max_y": 40, "max_z": 9}
        shape = ga.model_box("p", [0, 10, 0], 0, bounds, 1, 1, None, {})
        self.assertEqual((shape["min"][1], shape["max"][1]), (-2, 40))
        tall = ga.model_box("p", [0, 10, 0], 0, bounds, 1, 43, 5000 - 10, {})
        self.assertEqual(tall["max"][1], 4990)

    def test_handler_boxes(self):
        pickup = ga.handler_box("p", [0, 30, 0], 0, 16, bytes((16, 4, 6, 1)), {})
        self.assertEqual((pickup["min"], pickup["max"]), ([-4, -30, -6], [4, 70, 6]))
        hazard = ga.handler_box("p", [0, 0, 0], 0, 27, bytes((27, 1)), {})
        self.assertEqual((hazard["min"][0], hazard["max"][1]), (-150, 10))
        small = ga.handler_box("p", [0, 0, 0], 0, 27, bytes((27, 0)), {})
        self.assertEqual(small["max"][0], 5)


class GlobalAssaultMarkerTests(unittest.TestCase):
    def build(self, payload, bundle=None):
        bundle = bundle or ga_bundle(0, (0, 0, 10, 10))
        level = SimpleNamespace(level=3, variant="default", bundles=[bundle])
        definition = {"kind": payload[0], "raw_hex": payload.hex(), "offset": 0}
        resolved = []

        def model_id(bundle_index, index, kind):
            resolved.append(index)
            return f"model:{index}" if index < 40 else None

        record = ga.typed_marker(level, bundle, definition, {"x": 1, "y": 2, "z": 3, "yaw": 0x100}, 0, 7, [], model_id)
        return record, resolved

    def test_spawner_fields_are_typed_from_their_consumers(self):
        payload = bytes((13, 2, 9, 1)) + struct.pack(">HHIH", 12, 0b101, 300, 5) + bytes((4, 0))
        record, resolved = self.build(payload)
        values = {k: v["value"] for k, v in record["fields"].items()}
        self.assertEqual(values["spawn_budget"], 12)
        self.assertEqual(values["unit_type_mask"], 5)
        self.assertEqual(record["fields"]["unit_type_mask"]["bitmask_bits"], 15)
        self.assertNotIn("reference", record["fields"]["unit_type_mask"])
        self.assertEqual(values["spawn_interval"], 300)
        self.assertEqual(values["max_spawned"], 4)
        self.assertEqual(resolved, [5])  # only +0xC is a model lookup
        self.assertEqual(record["unknown_bytes"], [0])  # the kind byte; +0xF is beyond the 15 bytes the handler reads
        self.assertEqual(field_errors("unit_type_mask", record["fields"]["unit_type_mask"], 0x8000),
                         ["unit_type_mask: 32768 outside [1, 32767]"])

    def test_effect_prop_offset_six_is_not_a_model(self):
        payload = bytes((32, 3)) + struct.pack(">HH", 1, 2) + bytes((200, 0)) + struct.pack(">4H", 3, 4, 5, 6)
        record, resolved = self.build(payload)
        self.assertEqual(sorted(resolved), [1, 2, 3, 4, 5, 6])
        self.assertEqual(record["fields"]["rate_parameter"]["value"], 200)
        self.assertEqual(record["restrictions"]["modes_only"], [4, 10])

    def test_multiplayer_structure_resolves_embedded_definition(self):
        structure = bytes((3, 0)) + struct.pack(">8H", 7, 8, 0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF) + bytes((1, 0))
        definitions = bytes((31, 2)) + struct.pack(">Hi", 0xFFFF, 8) + structure
        bundle = ga_bundle(0, (0, 0, 10, 10), definitions, [0, 0, 0, 0, len(definitions)])
        record, resolved = self.build(definitions[:8], bundle)
        dependency = next(d for d in record["dependencies"] if d["kind"] == "definition")
        self.assertEqual((dependency["status"], dependency["found_kind"]), ("resolved", 3))
        self.assertEqual(sorted(resolved), [7, 8])
        self.assertEqual(record["restrictions"]["modes_only"], [1, 3, 11])

    def test_wrong_embedded_kind_is_reported(self):
        definitions = bytes((31, 2)) + struct.pack(">Hi", 0xFFFF, 8) + bytes((13,)) + bytes(15)
        bundle = ga_bundle(0, (0, 0, 10, 10), definitions, [0, 0, 0, 0, len(definitions)])
        record, _ = self.build(definitions[:8], bundle)
        self.assertEqual(record["dependencies"][0]["status"], "kind 13 found, 3 expected")


class BattleTanxTests(unittest.TestCase):
    header = {"bounds_b": [-3300.0, -3300.0, 3300.0, 3300.0]}

    def world(self):
        return {"header": self.header, "models": [{"footprint": [-10, -20, 10, 20]}]}

    def placement(self, kind, x=0.0, z=0.0, subtype=0, flags=0):
        return {"type": kind, "x": x, "z": z, "yaw": 0x2000, "model": 0, "subtype": subtype, "flags": flags, "word4": 0}

    def test_map_bounds(self):
        self.assertEqual(bt1.map_bounds(self.header)["x"], [-3800, 3800])

    def test_collision_is_planar_with_margins_and_masks(self):
        wall, _ = bt1.placement_collision(9, 1, self.placement(1), self.world(), {})
        self.assertEqual((wall["min"], wall["max"]), ([-11.0, None, -21.0], [11.0, None, 21.0]))
        self.assertEqual((wall["vertical"]["status"], wall["flags"], wall["orientation"]), ("none_stored", 0xF85F, "oriented"))
        fence, _ = bt1.placement_collision(9, 2, self.placement(19), self.world(), {})
        self.assertEqual(fence["flags"], 0x1A)
        owned, _ = bt1.placement_collision(9, 3, self.placement(30), self.world(), {})
        self.assertEqual((owned["min"][0], owned["extras"]["owner"]), (-10.0, 2))
        self.assertTrue(wall["extras"]["registered"])

    def test_outside_bounds_is_rejected_like_func_80106D18(self):
        far, _ = bt1.placement_collision(9, 1, self.placement(1, x=3801.0), self.world(), {})
        self.assertFalse(far["extras"]["registered"])
        edge, _ = bt1.placement_collision(9, 1, self.placement(1, x=3800.0), self.world(), {})
        self.assertTrue(edge["extras"]["registered"])

    def test_untraced_actor_collision_is_reported_not_invented(self):
        shape, note = bt1.placement_collision(9, 1, self.placement(3), self.world(), {})
        self.assertIsNone(shape)
        self.assertEqual(note["type"], 3)
        quiet, _ = bt1.placement_collision(9, 1, self.placement(6, subtype=0), self.world(), {})
        self.assertIsNone(quiet)
        loud, _ = bt1.placement_collision(9, 1, self.placement(6, subtype=0x10), self.world(), {})
        self.assertIsNotNone(loud)

    def test_restrictions(self):
        rules = bt1.restrictions(self.placement(9, flags=0x16))
        self.assertIn("disabled_when", rules)
        self.assertEqual(rules["team_gate"]["team_table_index"], 1)
        self.assertTrue(rules["flag_conditions"]["bit_0x10"])

    def test_marker_group_types(self):
        raw = bytes(28)
        slot = bt1.marker_group_record(9, 1, {"type": 25, "subtype": 0x23, "word4": 0x3044, "x": 1.0, "z": 2.0, "yaw": 0}, raw, {})
        self.assertEqual({k: v["value"] for k, v in slot["fields"].items()}, {"team": 2, "slot_kind": 3, "parameter": 0x44, "parameter_bits": 3})
        start = bt1.marker_group_record(9, 2, {"type": 22, "subtype": 0, "word4": 0b11011, "x": 0, "z": 0, "yaw": 0}, bytes(5) + b"\x01" + bytes(22), {})
        self.assertEqual({k: v["value"] for k, v in start["fields"].items()},
                         {"requires_flag": 1, "team_index": 1, "variant": 3, "byte5_bit0": 1})
        generator = bt1.marker_group_record(9, 3, {"type": 21, "subtype": 65, "word4": 162, "x": 141.0, "z": -2812.0, "yaw": 0}, raw, {})
        self.assertEqual(generator["restrictions"]["only_when"], "func_8007AAA0() == 1")
        idle = bt1.marker_group_record(9, 4, {"type": 14, "subtype": 0, "word4": 0, "x": 0, "z": 0, "yaw": 0}, raw, {})
        self.assertEqual(idle["category"], "inactive_in_marker_group")

    def test_navigation_split(self):
        nav = bt1.navigation({"points": [[0, 0], [1, 1]], "links": [[1, 50, 0, 60, 1, 70]], "tables": [], "point_evidence": "x"}, (0, 8))
        self.assertEqual(nav["links"][0]["targets"], [1, 0, 1])
        self.assertEqual(nav["links"][0]["values"], [50, 60, 70])


class PS1Tests(unittest.TestCase):
    def test_lattice_transform_round_trip_and_mirror(self):
        for col, row in ((64, 64), (10, 100), (127, 0)):
            x, z = ps1.lattice_point(col, row, 128)
            self.assertEqual(ps1.lattice_index(x, z, 128), (col, row))
        self.assertEqual(ps1.lattice_point(65, 64, 128), (-512, 0))
        self.assertEqual(ps1.lattice_index(255, 0, 128), (64, 64))
        self.assertEqual(ps1.lattice_index(-257, 0, 128), (65, 64))
        self.assertEqual(ps1.lattice_index(0, 256, 128), (64, 65))

    def test_floor_layer_bits(self):
        layer = ps1.floor_layer({"dimension": 64, "cells": [0] * 4096, "header_word_2": 0x80, "unreachable_tail": None}, "ASSETS/X.FLB", SHA)
        self.assertEqual(layer["spacing"], 512)
        self.assertEqual(layer["extent"]["x"], [-31 * 512, 32 * 512])
        self.assertEqual(layer["bits"]["24-31"]["evidence"], "proven")
        self.assertIn("height", layer["not_established"])

    def test_footprint_aabb(self):
        lo, hi = ps1.footprint_aabb({"x_a": -10, "z_a": -5, "x_b": 10, "z_b": 5}, 0x4000)
        self.assertEqual([round(v) for v in lo], [-20, 0, -40])
        self.assertEqual([round(v) for v in hi], [20, 0, 40])

    def test_scaled_extent_truncates_toward_zero(self):
        self.assertEqual([ps1.scaled_extent(v) for v in (72, -72, 9, -9, 1, -1)], [64, -64, 8, -8, 0, 0])

    def test_spawner_and_kind_twelve(self):
        raw = bytes((13, 2, 9, 1)) + struct.pack("<HHIH", 12, 0b101, 300, 5) + bytes((4, 0))
        record = ps1.typed_marker({"kind": 13, "id": "p", "index": 3, "translation": [0, 0, 0], "word_6": 0}, raw)
        values = {k: v["value"] for k, v in record["fields"].items()}
        self.assertEqual((values["spawn_budget"], values["unit_type_mask"], values["spawn_interval"], values["max_spawned"]), (12, 5, 300, 4))
        self.assertEqual(ps1.model_index_for({"kind": 12}, bytes(4) + struct.pack("<H", 9)), 9)
        self.assertIsNone(ps1.model_index_for({"kind": 12}, bytes(4) + b"\xff\xff"))
        ignored = ps1.typed_marker({"kind": 9, "id": "q", "index": 4, "translation": [0, 0, 0], "word_6": 0}, bytes(4))
        self.assertEqual(ignored["category"], "ignored_by_dispatcher")


class SemanticsContractTests(unittest.TestCase):
    def test_heights_require_proof(self):
        with self.assertRaises(SemanticsError):
            collision("c", "footprint", [0, 0, 0], 0, [0, 0, 0], [1, 0, 1],
                      vertical={"status": "none_stored", "min": 0, "max": 64}, orientation="oriented", space="local",
                      source={}, evidence=[], confidence="proven")
        with self.assertRaises(SemanticsError):
            collision("c", "box", [0, 0, 0], 0, [2, 0, 0], [1, 0, 1], vertical={"status": "proven", "min": 0, "max": 1},
                      orientation="oriented", space="local", source={}, evidence=[], confidence="proven")

    def test_field_rules(self):
        spec = field(3, "u8", 1, "proven", "x", enum={1: "a", 3: "b"}, closed=True)
        self.assertEqual(field_errors("x", spec, 2), ["x: 2 is not one of ['1', '3']"])
        self.assertEqual(field_errors("x", spec, 3), [])
        sentinel = field(0xFFFF, "u16", 4, "proven", "y", legal=(0, 10), sentinel={0xFFFF: "none"})
        self.assertEqual(field_errors("y", sentinel, 0xFFFF), [])
        self.assertEqual(field_errors("y", sentinel, 11), ["y: 11 outside [0, 10]"])
        with self.assertRaises(SemanticsError):
            field(1, "u8", 0, "guess", "z")
        mask = field(3, "u8", 0, "proven", "m", bits=4)
        self.assertEqual(field_errors("m", mask, 0x10), ["m: bits above bit 3 are not read by the game"])


def overlay_scene():
    team = field(1, "u8", 2, "proven", "team", legal=(0, 3))
    spawn = marker("src:spawn", "n64_bt1", "bt1_marker_25", "team_spawn_slot", [0, 0, 0], 0, {"team": team, "parameter": field(5, "u8", 5, "proven", "p")},
                   source={"placement_index": 1}, evidence=["e"], confidence="proven", restrictions={"max_per_team": 5},
                   dependencies=[{"kind": "model", "id": "model:a", "status": "resolved"}])
    wall = collision("src:wall", "footprint", [100, 0, 100], 0, [-10, 0, -10], [10, 0, 10], vertical=dict(bt1.NO_VERTICAL),
                     orientation="oriented", space="local", source={}, evidence=[], confidence="proven", flags=0xF85F)
    return {"id": "scene:test", "source": {"platform": "n64_bt1"}, "markers": [spawn], "collision": [wall], "instances": [],
            "bounds": {"map": {"x": [-1000, 1000], "z": [-1000, 1000]}},
            "navigation": {"points": [{"index": 0, "x": 0.0, "z": 0.0}, {"index": 1, "x": 50.0, "z": 50.0}], "point_count": 2, "links": [{"index": 0, "targets": [1, 0, 1], "values": [1, 2, 3]}]},
            "minimap": {"selections": [{"variant": "default"}]}}


class OverlayTests(unittest.TestCase):
    def setUp(self):
        self.scene = overlay_scene()
        self.template = self.scene["markers"][0]

    def codes(self, overlay, **kwargs):
        return sorted(e["code"] for e in ov.validate(self.scene, overlay, **kwargs))

    def test_round_trip_is_deterministic_and_preserves_source(self):
        before = json.dumps(self.scene, sort_keys=True)
        overlay = ov.Overlay.for_scene(self.scene)
        new = overlay.add_marker(self.template, [10, 0, 20], 0x4000, {"team": 2})
        overlay.move("src:spawn", [5, 0, 5], 0x1000)
        overlay.set_field("src:spawn", "parameter", 9)
        overlay.disable("src:wall")
        text = overlay.to_json()
        self.assertEqual(text, json.dumps(json.loads(text), sort_keys=True, indent=1) + "\n")
        again = ov.Overlay.from_json(text)
        self.assertEqual(again.to_json(), text)
        edited = ov.apply(self.scene, again)
        self.assertEqual(json.dumps(self.scene, sort_keys=True), before)
        markers = {m["id"]: m for m in edited["markers"]}
        self.assertEqual(markers[new]["fields"]["team"]["value"], 2)
        self.assertEqual(markers[new]["source"]["template"], "src:spawn")
        self.assertEqual(markers["src:spawn"]["translation"], [5.0, 0.0, 5.0])
        self.assertEqual(markers["src:spawn"]["source"], {"placement_index": 1})
        self.assertEqual([e["op"] for e in markers["src:spawn"]["overlay"]["edits"]], ["move", "set_field"])
        self.assertTrue(edited["collision"][0]["disabled"])
        self.assertEqual(self.codes(again, known_models={"model:a"}), [])

    def test_user_marker_delete_and_revert(self):
        overlay = ov.Overlay.for_scene(self.scene)
        new = overlay.add_marker(self.template, [0, 0, 0])
        overlay.delete(new)
        self.assertEqual(len(ov.apply(self.scene, overlay)["markers"]), 1)
        overlay.revert(1)
        self.assertEqual(len(ov.apply(self.scene, overlay)["markers"]), 2)
        overlay.delete("src:spawn")
        self.assertIn("PROTECTED_SOURCE", self.codes(overlay))
        self.assertIn("src:spawn", {m["id"] for m in ov.apply(self.scene, overlay)["markers"]})

    def test_validation_errors(self):
        overlay = ov.Overlay.for_scene(self.scene)
        overlay.add_marker(self.template, [5000, 0, 0])
        self.assertEqual(self.codes(overlay, known_models={"model:a"}), ["OUT_OF_BOUNDS"])
        overlay = ov.Overlay.for_scene(self.scene)
        overlay.set_field("src:spawn", "team", 7)
        self.assertEqual(self.codes(overlay), ["INVALID_TEAM"])
        overlay = ov.Overlay.for_scene(self.scene)
        overlay.move("src:spawn", [1, 0, 1])
        self.assertEqual(self.codes(overlay, known_models=set()), ["MISSING_DEPENDENCY"])
        overlay = ov.Overlay.for_scene(self.scene)
        overlay.add_marker(self.template, [0, 0, 0])
        self.assertEqual(self.codes(overlay, target_platform="ps1"), ["UNSUPPORTED_MARKER"])
        overlay = ov.Overlay.for_scene(self.scene)
        for _ in range(5):
            overlay.add_marker(self.template, [0, 0, 0])
        self.assertEqual(self.codes(overlay), ["TEAM_LIMIT"])
        overlay = ov.Overlay.for_scene(self.scene)
        overlay.add({"op": "set_nav_link", "link": 0, "targets": [1, 5, 0]})
        self.assertEqual(self.codes(overlay), ["INVALID_NAV_LINK"])
        overlay = ov.Overlay.for_scene(self.scene)
        overlay.move("missing", [0, 0, 0])
        self.assertEqual(self.codes(overlay), ["UNKNOWN_TARGET"])
        overlay = ov.Overlay.for_scene(self.scene)
        overlay.add({"op": "set_minimap", "minimap": {"mode": "generated", "width": 200, "height": 10}})
        self.assertEqual(self.codes(overlay), ["INVALID_MINIMAP"])

    def test_collision_validation(self):
        shape = dict(self.scene["collision"][0])
        overlay = ov.Overlay.for_scene(self.scene)
        overlay.add({"op": "add_collision", "collision": {**shape, "translation": [105.0, 0.0, 100.0]}})
        self.assertEqual(self.codes(overlay), ["COLLISION_OVERLAP"])
        overlay = ov.Overlay.for_scene(self.scene)
        overlay.add({"op": "set_collision", "target": "src:wall", "min": [-10, 0, -10], "max": [10, 64, 10]})
        self.assertEqual(self.codes(overlay), ["INVALID_COLLISION"])
        overlay = ov.Overlay.for_scene(self.scene)
        overlay.add({"op": "set_collision", "target": "src:wall", "min": [10, None, -10], "max": [-10, None, 10]})
        self.assertEqual(self.codes(overlay), ["INVALID_COLLISION"])
        overlay = ov.Overlay.for_scene(self.scene)
        overlay.add({"op": "add_collision", "collision": {**shape, "translation": [400.0, 0.0, 400.0]}})
        self.assertEqual(self.codes(overlay), [])

    def test_duplicate_ids(self):
        self.scene["markers"].append(dict(self.template))
        self.assertIn("DUPLICATE_ID", self.codes(ov.Overlay.for_scene(self.scene)))

    def test_host_paths_are_refused(self):
        overlay = ov.Overlay.for_scene(self.scene)
        overlay.add_marker(self.template, [0, 0, 0])
        overlay.edits[-1]["marker"]["source"]["note"] = "/home/someone/rom.z64"
        with self.assertRaises(ov.OverlayError):
            overlay.to_json()

    def test_user_ids_are_stable(self):
        a, b = ov.Overlay.for_scene(self.scene), ov.Overlay.for_scene(self.scene)
        self.assertEqual(a.add_marker(self.template, [0, 0, 0]), b.add_marker(self.template, [0, 0, 0]))


class PlanPreviewTests(unittest.TestCase):
    def test_plan_and_minimap_alignment_are_deterministic(self):
        from tools.btga_assets import preview

        scene = overlay_scene()
        first = preview.plan_view(scene, 128).png()
        self.assertEqual(first, preview.plan_view(scene, 128).png())
        self.assertTrue(first.startswith(b"\x89PNG"))
        scene["minimap"] = {"origin": [-1000, -1000], "scale": 0.01, "selections": [{"variant": "default", "image": {"width": 20, "height": 20}}]}
        texture = {"width": 20, "height": 20, "rgba": bytes(20 * 20 * 4)}
        image = preview.minimap_alignment(scene, texture, zoom=2)
        self.assertTrue(image.startswith(b"\x89PNG"))
        plan = preview.Plan([0, 100], [0, 100], 116)
        self.assertEqual(plan.to_pixel(0, 0), (8, 8))
        self.assertEqual(plan.to_pixel(100, 100), (108, 108))


class StudioLazinessTests(unittest.TestCase):
    def test_browser_reads_only_the_catalog_and_overlays_round_trip(self):
        from tests.test_n64_assets import write_synthetic_export
        from tools.btga_assets.studio import StudioLibrary

        with tempfile.TemporaryDirectory() as temp:
            write_synthetic_export(Path(temp))
            library = StudioLibrary.open(Path(temp))
            reads = []
            original = library._read
            library._read = lambda relative: reads.append(relative) or original(relative)
            library.campaigns()
            levels = library.levels()
            library.library_models()
            self.assertEqual(reads, [])
            game_map = library.load_map(levels[0].id)
            self.assertEqual(len(reads), 1)
            model = game_map.instances[0].model
            first = library.thumbnail(model, 16)
            count = len(reads)
            self.assertEqual(library.thumbnail(model, 16), first)
            self.assertEqual(len(reads), count)
            overlay = library.new_overlay(levels[0].id)
            template = dict(next(iter(library.marker_templates(levels[0].id).values())))
            template.setdefault("fields", {})
            template.setdefault("source_kind", "user")
            overlay.add_marker(template, [1, 0, 2], 0x4000)
            path = Path(temp) / "project.overlay.json"
            library.save_overlay(overlay, path)
            again = library.load_overlay(path)
            self.assertEqual(again.to_json(), path.read_text())
            edited = library.load_map(levels[0].id, again)
            self.assertEqual(len(edited.markers), len(game_map.markers) + 1)
            self.assertTrue(edited.markers[-1].edited)
            self.assertEqual(edited.markers[-1].yaw_turns, 0.25)


GA_ROM = os.environ.get("BTGA_N64_ROM")
BT1_ROM = os.environ.get("BTGA_BT1_ROM")
PS1_CORPUS = os.environ.get("BTGA_PS1_EXTRACTED")


@unittest.skipUnless(GA_ROM and Path(GA_ROM).is_file(), "set BTGA_N64_ROM to the Global Assault N64 USA Rev 0 ROM")
class RealGlobalAssaultSemanticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tools.btga_assets import n64_btga, n64_sources as src

        cls.src, cls.n64_btga = src, n64_btga
        cls.rom = src.load_rom(Path(GA_ROM))
        cls.slot = (ROOT / "src/code/frontend/800E8380_slot_asset_ranges.c").read_text()
        cls.images = {image["table_index"]: image for image in src.ga_image_ranges(cls.rom.data)}

    def scene(self, level, variant="default"):
        lv = self.n64_btga.load_level(self.rom.data, self.slot, level, variant)
        return self.n64_btga.scene_record(self.rom.data, self.rom.sha256, lv, {}, self.images, [variant])[0]

    def test_minimap_descriptors_are_table_images(self):
        for index in range(161, 196):
            self.assertIn(index, self.images)
            self.assertEqual((self.images[index]["format"], self.images[index]["size"]), (2, 0))
            self.assertLessEqual(self.images[index]["width"], 74)

    def test_tower_bridge_minimap(self):
        record = self.scene(7)["minimap"]
        self.assertEqual(record["area0_box"], {"x0": -2683, "z0": -11909, "x1": 2683, "z1": 11690})
        selection = record["selections"][0]
        self.assertEqual((selection["image"]["table_index"], selection["image"]["width"], selection["image"]["height"]), (193, 18, 71))
        self.assertEqual(selection["fit"]["world_span_pixels"][1], 70.0)

    def test_no_out_of_range_model_lookups_remain(self):
        for level in (1, 4, 6, 12, 15, 20):
            scene = self.scene(level)
            self.assertEqual(scene["unresolved"], [])
            for record in scene["markers"]:
                for name, spec in record["fields"].items():
                    self.assertEqual(field_errors(name, spec, spec["value"]), [], (level, record["id"], name))
                for dependency in record["dependencies"]:
                    self.assertIn(dependency["status"], ("resolved", None) if dependency["kind"] == "model" else ("resolved",))

    def test_spawner_masks_select_unit_types(self):
        # Spawner placements occur only in the func_8009D144() (modes 7..14) stream variants.
        self.assertFalse([m for m in self.scene(4)["markers"] if m["category"] == "unit_spawner"])
        masks = [m["fields"]["unit_type_mask"]["value"] for level in (1, 4, 12) for m in self.scene(level, "flag")["markers"] if m["category"] == "unit_spawner"]
        self.assertTrue(masks)
        self.assertTrue(all(0 < mask < 1 << 15 for mask in masks))


@unittest.skipUnless(BT1_ROM and Path(BT1_ROM).is_file(), "set BTGA_BT1_ROM to the BattleTanx N64 USA ROM")
class RealBattleTanxSemanticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tools.btga_assets import n64_bt1, n64_sources as src

        cls.n64_bt1 = n64_bt1
        cls.rom = src.load_rom(Path(BT1_ROM))

    def scene(self, slot):
        return self.n64_bt1.scene_record(self.rom.data, self.rom.sha256, self.n64_bt1.load_level(self.rom.data, slot), {})[0]

    def test_validation_maps(self):
        # 9 Chicago Bonus, 11 dense (2,044 placements), 4 narrow (aspect 7.7), 5 and 17 bonus arenas (mode-7 scenarios).
        for slot in (9, 11, 4, 5, 17):
            scene = self.scene(slot)
            self.assertTrue(scene["collision"], slot)
            for shape in scene["collision"]:
                self.assertEqual(shape["vertical"]["status"], "none_stored")
                self.assertIsNone(shape["max"][1])
                self.assertNotEqual(shape["max"][1], 64)
            bounds = scene["bounds"]["map"]
            registered = [s for s in scene["collision"] if s["extras"]["registered"]]
            for shape in registered:
                self.assertTrue(bounds["x"][0] <= shape["translation"][0] <= bounds["x"][1])

    def test_chicago_bonus_markers(self):
        scene = self.scene(9)
        self.assertEqual(scene["bounds"]["map"]["x"], [-3800, 3800])
        generators = [m for m in scene["markers"] if m["category"] == "tank_generator"]
        self.assertEqual(sorted(round(m["translation"][0]) for m in generators), [-1385, 141, 1645])
        self.assertTrue(all(m["fields"]["subtype"]["value"] == 65 for m in generators))


@unittest.skipUnless(PS1_CORPUS and Path(PS1_CORPUS).is_dir(), "set BTGA_PS1_EXTRACTED to the extracted PS1 disc")
class RealPS1SemanticsTests(unittest.TestCase):
    def test_level_semantics(self):
        from tools.btga_assets import ps1_adapter

        scene, _, _ = ps1_adapter.level_assets(Path(PS1_CORPUS), "ASSETS/LONDON1")
        self.assertEqual(len(scene["collision"]), len(scene["instances"]))
        self.assertTrue(all(c["vertical"]["status"] == "none_stored" for c in scene["collision"]))
        self.assertEqual(scene["floor"]["spacing"], 512)
        self.assertEqual(len(scene["floor"]["cells"]), scene["floor"]["dimension"] ** 2)

    def test_mirrored_lookup_is_corroborated_by_placement_bits(self):
        from tools.btga_assets import ps1_formats as pf
        from tools.btga_assets.scene import LevelSet, assemble_scene

        hits = {"mirror": [0, 0], "plain": [0, 0]}
        for stem in ("LONDON1", "SF1", "TEXAS2", "BERLIN4", "PARIS2", "DC3"):
            level = LevelSet(Path(PS1_CORPUS), f"ASSETS/{stem}")
            scene, _, _ = assemble_scene(level)
            flb = pf.parse_flb(level.files[".FLB"][1])
            d, cells = flb["dimension"], flb["cells"]
            for p in scene["placements"]:
                if p["model_index"] is None:
                    continue
                x, _, z = p["translation"]
                for mode in hits:
                    col = (((256 - x) if mode == "mirror" else (x + 256)) >> 9) + d // 2
                    row = ((z + 256) >> 9) + d // 2
                    if 0 <= col < d and 0 <= row < d:
                        hits[mode][0] += (cells[row * d + col] >> 16) & 1
                        hits[mode][1] += 1
        self.assertGreater(hits["mirror"][0] / hits["mirror"][1], hits["plain"][0] / hits["plain"][1])


if __name__ == "__main__":
    unittest.main()
