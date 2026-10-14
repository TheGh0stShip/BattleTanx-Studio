"""Typed world-semantics records shared by every platform adapter.

Scene schema v3 adds three editable record families on top of the v2
geometry contract:

* typed markers: every field carries an explicit type, legal range,
  source offset and evidence class; bytes no handler reads stay in
  ``raw_hex`` and are listed in ``unknown_bytes``;
* collision shapes: footprint geometry plus an explicit ``vertical`` block.
  A height is only ever present when executable code stores one; shapes
  whose collision test is planar carry ``vertical.status = "none_stored"``
  and ``None`` heights instead of a placeholder;
* minimaps: the image the game actually selects, with the proven
  world-to-pixel transform, or an explicit ``none`` / ``unchanged`` state.

Evidence classes (documents and records use the same words):
    proven        read from executable code
    corroborated  proven framing, and corpus data agrees with the meaning
    inferred      a name or role chosen from how code uses a field, not stated by it
    unresolved    retained raw; meaning not established
"""

from __future__ import annotations

EVIDENCE = ("proven", "corroborated", "inferred", "unresolved")
# Besides these storage types: "f32", "ref" (ID of another record, or None)
# and "text" (a printable string; used only by Studio-authored records).
FIELD_TYPES = {
    "u8": (0, 0xFF),
    "u16": (0, 0xFFFF),
    "s16": (-0x8000, 0x7FFF),
    "u32": (0, 0xFFFFFFFF),
    "s32": (-0x80000000, 0x7FFFFFFF),
}
SCENE_SCHEMA_VERSION = 3


class SemanticsError(ValueError):
    pass


def field(value, kind: str, offset: int | None, evidence: str, meaning: str, *, legal: tuple | None = None,
          enum: dict | None = None, bits: int | None = None, reference: str | None = None, sentinel: dict | None = None, closed: bool = False) -> dict:
    """One editable field.

    ``kind`` is a storage type from FIELD_TYPES, or "f32".  ``legal`` narrows
    the storage range to what the consumer accepts; ``enum`` lists named
    values; ``bits`` marks a bitmask of that many low bits; ``reference``
    names the record family an index points into (only when a consumer
    proves it); ``sentinel`` maps special values to their meaning; a
    ``closed`` enum rejects values outside it.
    """

    if evidence not in EVIDENCE:
        raise SemanticsError(f"unknown evidence class {evidence!r}")
    if kind not in ("f32", "ref", "text") and kind not in FIELD_TYPES:
        raise SemanticsError(f"unknown field type {kind!r}")
    record = {"value": value, "type": kind, "offset": offset, "evidence": evidence, "meaning": meaning}
    if kind in ("ref", "text"):
        if reference:
            record["reference"] = reference
        if legal:
            record["max_length"] = legal[1]
        return record
    if kind != "f32":
        lo, hi = legal or FIELD_TYPES[kind]
        record["range"] = [lo, hi]
    elif legal:
        record["range"] = list(legal)
    if enum:
        record["enum"] = {str(key): value_ for key, value_ in sorted(enum.items())}
        record["enum_closed"] = closed
    if bits:
        record["bitmask_bits"] = bits
    if reference:
        record["reference"] = reference
    if sentinel:
        record["sentinel"] = {str(key): value_ for key, value_ in sorted(sentinel.items())}
    return record


def field_errors(name: str, spec: dict, value) -> list[str]:
    """Return why ``value`` is not legal for ``spec`` (empty when it is)."""

    if spec["type"] == "ref":
        if value is not None and (not isinstance(value, str) or not value):
            return [f"{name}: expected a record ID or none"]
        return []
    if spec["type"] == "text":
        if not isinstance(value, str):
            return [f"{name}: expected text"]
        if len(value) > spec.get("max_length", 256) or any(ord(c) < 32 for c in value):
            return [f"{name}: text is too long or contains control characters"]
        return []
    if spec["type"] == "f32":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return [f"{name}: expected a number"]
        if "range" in spec and not spec["range"][0] <= value <= spec["range"][1]:
            return [f"{name}: {value} outside {spec['range']}"]
        return []
    if not isinstance(value, int) or isinstance(value, bool):
        return [f"{name}: expected an integer"]
    if str(value) in spec.get("sentinel", {}):
        return []
    lo, hi = spec["range"]
    if not lo <= value <= hi:
        return [f"{name}: {value} outside [{lo}, {hi}]"]
    if "bitmask_bits" in spec and value >> spec["bitmask_bits"]:
        return [f"{name}: bits above bit {spec['bitmask_bits'] - 1} are not read by the game"]
    if spec.get("enum_closed") and str(value) not in spec["enum"]:
        return [f"{name}: {value} is not one of {sorted(spec['enum'], key=int)}"]
    return []


def unknown_bytes(size: int, known: list[tuple[int, int]]) -> list[int]:
    """Byte offsets of a record that no traced consumer reads."""

    covered = set()
    for offset, width in known:
        covered.update(range(offset, offset + width))
    return [offset for offset in range(size) if offset not in covered]


WIDTH = {"u8": 1, "u16": 2, "s16": 2, "u32": 4, "s32": 4, "f32": 4}


def marker(marker_id: str, platform: str, source_kind: str, category: str, translation, yaw: int, fields: dict,
           *, source: dict, evidence: list[str], confidence: str, dependencies: list[dict] | None = None,
           raw: bytes | None = None, record_size: int | None = None, conditions: list | None = None,
           restrictions: dict | None = None, models: list[str] | None = None) -> dict:
    if confidence not in EVIDENCE:
        raise SemanticsError(f"unknown confidence {confidence!r}")
    record = {
        "id": marker_id,
        "platform": platform,
        "source_kind": source_kind,
        "category": category,
        "translation": [float(v) for v in translation],
        "yaw": int(yaw) & 0xFFFF,
        "fields": fields,
        "dependencies": dependencies or [],
        "models": models or [],
        "source": source,
        "confidence": confidence,
        "evidence": evidence,
        "conditions": conditions or [],
        "restrictions": restrictions or {},
    }
    if raw is not None:
        record["raw_hex"] = raw.hex()
        known = [(spec["offset"], WIDTH[spec["type"]]) for spec in fields.values() if spec.get("offset") is not None]
        record["unknown_bytes"] = unknown_bytes(record_size or len(raw), known)
    return record


def collision(shape_id: str, shape: str, translation, yaw: int, local_min, local_max, *, vertical: dict, orientation: str,
              space: str, source: dict, evidence: list[str], confidence: str, flags=None, extras: dict | None = None) -> dict:
    """A collision volume.

    ``shape``: "box" (3D), "footprint" (planar oriented rectangle) or
    "aabb" (planar world-aligned rectangle).  ``space`` is "local"
    (min/max relative to translation and yaw) or "world" (absolute XZ).
    ``vertical`` = {status, min, max, reference}; status "proven" carries
    numbers, "none_stored" means the runtime test is planar.
    """

    if vertical["status"] not in ("proven", "none_stored", "unresolved"):
        raise SemanticsError("bad vertical status")
    if vertical["status"] != "proven" and (vertical.get("min") is not None or vertical.get("max") is not None):
        raise SemanticsError("only a proven vertical extent may carry heights")
    lo = [float(local_min[0]), None if vertical.get("min") is None else float(vertical["min"]), float(local_min[2])]
    hi = [float(local_max[0]), None if vertical.get("max") is None else float(vertical["max"]), float(local_max[2])]
    if lo[0] > hi[0] or lo[2] > hi[2] or (lo[1] is not None and hi[1] is not None and lo[1] > hi[1]):
        raise SemanticsError(f"{shape_id}: inverted collision extent")
    record = {
        "id": shape_id,
        "shape": shape,
        "space": space,
        "translation": [float(v) for v in translation],
        "yaw": int(yaw) & 0xFFFF,
        "min": lo,
        "max": hi,
        "vertical": vertical,
        "orientation": orientation,
        "flags": flags,
        "source": source,
        "confidence": confidence,
        "evidence": evidence,
    }
    if extras:
        record["extras"] = extras
    return record
