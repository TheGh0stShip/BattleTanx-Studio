"""Versioned neutral resource records shared by exporters and the comparator.

A neutral record never carries host pointers or absolute paths.  Each one
has a stable ID derived only from its source identity (platform, source
file hash, normalised source path, record kind, record indices), so the
same bytes always produce the same ID on every host.

Evidence levels used in records:
    proven        read from executable code
    corroborated  proven framing; every corpus record agrees with the meaning
    hypothesis    consistent with data, not traced
    unknown       retained raw, meaning not established
"""

from __future__ import annotations

import hashlib
import re

SCHEMA = "btga-neutral"
SCHEMA_VERSION = 1

PLATFORMS = ("ps1", "n64_btga", "n64_bt1", "restored_pack", "user", "studio")

PS1_COORDINATES = {
    "neutral_from_source": "identity",
    "up_axis": "+Y",
    "handedness": "right",
    "front_face": "counter-clockwise",
    "unit": "PS1 model unit (= integer part of the 20.12 world position)",
    "evidence": {
        "vertex_format": "proven (0x8007E5E8 loads s16 x, y, z into GTE VXY/VZ)",
        "placement_scale": "proven (0x800245D0 shifts s16 placement positions left 14 into 20.12)",
        "model_unit": "corroborated (vertex extent / CM01 footprint has median 4.0 on X over 4,255 models "
        "and on Z over 4,671; the footprint is shifted left 14 into the same 20.12 space as placements)",
        "up_axis": "corroborated (vertex minimum Y is 0 for the median model; geometry extends +Y)",
        "front_face": "corroborated (authored normals agree with the right-handed cross product of "
        "corners 0, 1, 2 for 98.8% of cull-flag-1 primitives and disagree for 98.0% of cull-flag-2)",
        "mirror": "unproven (the camera matrix construction was not traced)",
    },
}

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9 _.\-+()#&']+$")


class NeutralError(ValueError):
    pass


def safe_source_path(path: str) -> str:
    """Normalise a source locator and refuse anything that could escape a root.

    Accepts POSIX-style relative paths whose components are printable,
    contain no separators or traversal, and are not absolute or drive
    qualified.  Spaces are allowed.
    """

    if not isinstance(path, str) or not path:
        raise NeutralError("source path must be a non-empty string")
    if "\\" in path or "\x00" in path or any(ord(char) < 0x20 for char in path):
        raise NeutralError(f"source path {path!r} contains forbidden characters")
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise NeutralError(f"source path {path!r} is absolute")
    parts = path.split("/")
    for part in parts:
        if part in ("", ".", "..") or not _SAFE_COMPONENT.match(part):
            raise NeutralError(f"source path {path!r} has an unsafe component {part!r}")
    return "/".join(parts)


def stable_id(platform: str, source_sha256: str, path: str, kind: str, *indices: int) -> str:
    if platform not in PLATFORMS:
        raise NeutralError(f"unknown platform {platform!r}")
    if not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        raise NeutralError("source hash must be a lowercase SHA-256 hex digest")
    if not re.fullmatch(r"[a-z_]+", kind):
        raise NeutralError(f"bad record kind {kind!r}")
    for index in indices:
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise NeutralError("record indices must be non-negative integers")
    key = "|".join([platform, source_sha256, safe_source_path(path), kind, *map(str, indices)])
    return f"{platform}:{kind}:{hashlib.sha256(key.encode('utf-8')).hexdigest()[:24]}"


def envelope(kind: str, record_id: str, source: dict) -> dict:
    return {"schema": SCHEMA, "schema_version": SCHEMA_VERSION, "kind": kind, "id": record_id, "source": source}


def command_record(command) -> dict:
    return {
        "offset": command.offset,
        "word": f"{command.word:08x}",
        "opcode": command.opcode,
        "flags": command.flags,
        "name": command.name,
        "handler": f"0x{command.handler:08X}",
        "operands": command.operands,
        "extra_words": [f"{word:08x}" for word in command.extra_words],
        "block_path": list(command.block_path),
    }


def primitive_record(primitive) -> dict:
    return {
        "index": primitive.index,
        "command": primitive.command,
        "gpu_code": primitive.gpu_code,
        "mode": primitive.mode,
        "quad": primitive.quad,
        "gouraud": primitive.gouraud,
        "textured": primitive.textured,
        "cull": primitive.cull,
        "texture_slot": primitive.texture_slot,
        "block_path": list(primitive.block_path),
        "missing": primitive.missing,
        "corners": [
            {
                "vertex": corner.vertex,
                "rgb": corner.colour,
                "normal": list(corner.normal) if corner.normal else None,
                "uv": list(corner.uv) if corner.uv else None,
            }
            for corner in (primitive.corners[slot] for slot in primitive.used_corners)
        ],
        "corner_slots": primitive.used_corners,
    }


def ps1_model_record(model, *, path: str, file_sha256: str, bank_index: int, texture_list: list[int] | None) -> dict:
    """Neutral form of one decoded PS1 model (lossless for the stream)."""

    record = envelope(
        "model",
        stable_id("ps1", file_sha256, path, "model", bank_index),
        {
            "platform": "ps1",
            "file": safe_source_path(path),
            "file_sha256": file_sha256,
            "bank_index": bank_index,
            "range": {"offset": model.offset, "size": model.size, "sha256": model.sha256},
        },
    )
    xs = [vertex[0] for vertex in model.vertices] or [0]
    ys = [vertex[1] for vertex in model.vertices] or [0]
    zs = [vertex[2] for vertex in model.vertices] or [0]
    record.update(
        {
            "coordinate_system": PS1_COORDINATES,
            "header": {
                "footprint": {"x_a": model.footprint[0], "z_a": model.footprint[1], "x_b": model.footprint[2], "z_b": model.footprint[3]},
                "radius": model.radius,
                "raw": model.header_raw,
                "evidence": {
                    "footprint": "proven read at 0x80025170, rotated at 0x800249A0",
                    "radius": "proven cull at 0x80069DE8 (radius << 12 vs 20.12 position)",
                    "other_bytes": "unknown",
                },
            },
            "body": {
                "part_count": model.part_count,
                "vertex_count": model.vertex_count,
                "word_0c": model.body_word_0c,
                "word_10": model.body_word_10,
                "evidence": {
                    "part_count": "proven (per-part matrices, 0x80069D04 / 0x80092510)",
                    "word_0c": "unknown",
                    "word_10": "unknown",
                },
            },
            "bounds": {"min": [min(xs), min(ys), min(zs)], "max": [max(xs), max(ys), max(zs)]},
            "vertices": [list(vertex[:3]) for vertex in model.vertices],
            "vertex_pad": [vertex[3] for vertex in model.vertices],
            "parts": [list(part) for part in model.parts],
            "part_markers_match_count": model.part_markers_match,
            "texture_list": texture_list,
            "primitives": [primitive_record(primitive) for primitive in model.primitives],
            "commands": [command_record(command) for command in model.commands],
            "stream": {
                "offset": model.stream_offset,
                "size": model.stream_end - model.stream_offset,
                "covered_bytes": model.covered_bytes,
                "unreached": model.unreached,
            },
        }
    )
    return record


def triangle_corner_order(primitive: dict) -> list[tuple[int, int, int]]:
    """Triangles as indices into primitive['corners'] in neutral CCW-front order.

    Quads split on the c1-c3 diagonal the GPU uses; cull flag 2
    (front when screen-counter-clockwise) reverses every triangle.
    """

    if primitive["quad"]:
        triangles = [(0, 1, 3), (1, 2, 3)]
    else:
        triangles = [(0, 1, 2)]
    if primitive["cull"] == "front_screen_ccw":
        triangles = [(a, c, b) for a, b, c in triangles]
    return triangles
