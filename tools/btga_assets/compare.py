"""Cross-version comparison records and evidence-weighted candidate matching.

A comparison document links assets from any of the neutral platforms
(PS1 BTGA, N64 BTGA, N64 BattleTanx, restored-content packs, user
replacements).  Matching only ever proposes *candidates*: each carries a
confidence in [0, 1] and the list of evidence that produced it.  The
status "confirmed" is reserved for links a person (or a future proof
step) supplies with explicit proof; nothing here sets it.
"""

from __future__ import annotations

import re
import struct
import zlib

from .neutral import PLATFORMS, NeutralError, safe_source_path

SCHEMA = "btga-compare"
SCHEMA_VERSION = 1
STATUSES = ("candidate", "confirmed", "rejected")
KINDS = ("texture", "model", "level", "name")


class CompareError(NeutralError):
    pass


# ----------------------------------------------------------------- images --


def png_rgba(data: bytes) -> tuple[int, int, bytes]:
    """Decode a non-interlaced 8-bit RGB/RGBA PNG (the forms our tools write)."""

    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise CompareError("not a PNG")
    position, idat, header = 8, bytearray(), None
    while position + 8 <= len(data):
        length, kind = struct.unpack_from(">I4s", data, position)
        body = data[position + 8 : position + 8 + length]
        if len(body) != length:
            raise CompareError("truncated PNG chunk")
        position += 12 + length
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
    if header is None:
        raise CompareError("PNG has no IHDR")
    width, height, depth, colour, _, _, interlace = header
    if depth != 8 or colour not in (2, 6) or interlace:
        raise CompareError("unsupported PNG layout")
    channels = 4 if colour == 6 else 3
    if width * height > 1 << 24:
        raise CompareError("PNG too large")
    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    if len(raw) != height * (stride + 1):
        raise CompareError("PNG data length mismatch")
    out = bytearray()
    previous = bytearray(stride)
    for row in range(height):
        kind = raw[row * (stride + 1)]
        line = bytearray(raw[row * (stride + 1) + 1 : (row + 1) * (stride + 1)])
        for index in range(stride):
            left = line[index - channels] if index >= channels else 0
            up = previous[index]
            corner = previous[index - channels] if index >= channels else 0
            if kind == 1:
                line[index] = (line[index] + left) & 0xFF
            elif kind == 2:
                line[index] = (line[index] + up) & 0xFF
            elif kind == 3:
                line[index] = (line[index] + (left + up) // 2) & 0xFF
            elif kind == 4:
                estimate = left + up - corner
                pa, pb, pc = abs(estimate - left), abs(estimate - up), abs(estimate - corner)
                predictor = left if pa <= pb and pa <= pc else up if pb <= pc else corner
                line[index] = (line[index] + predictor) & 0xFF
            elif kind != 0:
                raise CompareError(f"bad PNG filter {kind}")
        previous = line
        if channels == 4:
            out += line
        else:
            for pixel in range(width):
                out += line[pixel * 3 : pixel * 3 + 3] + b"\xff"
    return width, height, bytes(out)


def dhash(width: int, height: int, rgba: bytes) -> int:
    """64-bit difference hash of the alpha-composited luminance (9x8 box grid)."""

    if width <= 0 or height <= 0 or len(rgba) != width * height * 4:
        raise CompareError("bad image for hashing")
    grid = []
    for gy in range(8):
        y0, y1 = gy * height // 8, max(gy * height // 8 + 1, (gy + 1) * height // 8)
        row = []
        for gx in range(9):
            x0, x1 = gx * width // 9, max(gx * width // 9 + 1, (gx + 1) * width // 9)
            total = count = 0
            for y in range(y0, min(y1, height)):
                for x in range(x0, min(x1, width)):
                    o = (y * width + x) * 4
                    alpha = rgba[o + 3]
                    total += (299 * rgba[o] + 587 * rgba[o + 1] + 114 * rgba[o + 2]) * alpha // 255
                    count += 1
            row.append(total // max(count, 1))
        grid.append(row)
    value = 0
    for row in grid:
        for left, right in zip(row, row[1:]):
            value = (value << 1) | (1 if left > right else 0)
    return value


# ------------------------------------------------------------------ names --


def normalise_name(name: str) -> str:
    """Lowercase alphanumeric tokens with digit runs split out: 'BERLIN1M' -> 'berlin 1 m'."""

    tokens = re.findall(r"[a-z]+|[0-9]+", name.lower())
    return " ".join(tokens)


def image_strings(data: bytes, minimum: int = 4, maximum: int = 80) -> list[tuple[int, str]]:
    return [(match.start(), match.group().decode("ascii")) for match in re.finditer(rb"[ -~]{%d,%d}" % (minimum, maximum), data)]


def references(strings: list[tuple[int, str]], term: str) -> list[dict]:
    pattern = re.compile(r"(?<![a-z])" + r"[\s_\-]*".join(map(re.escape, normalise_name(term).split())) + r"(?![a-z])", re.I)
    out = []
    for offset, text in strings:
        if pattern.search(text):
            out.append({"offset": offset, "text": text})
    return out


# ----------------------------------------------------------------- assets --


def asset(platform: str, kind: str, asset_id: str, name: str, source: dict, fingerprints: dict) -> dict:
    if platform not in PLATFORMS:
        raise CompareError(f"unknown platform {platform!r}")
    if kind not in KINDS:
        raise CompareError(f"unknown asset kind {kind!r}")
    if "file" in source:
        source = dict(source, file=safe_source_path(source["file"]))
    return {
        "id": asset_id,
        "platform": platform,
        "kind": kind,
        "name": name,
        "normalised_name": normalise_name(name),
        "source": source,
        "fingerprints": fingerprints,
    }


def candidate(a: dict, b: dict, evidence: list[dict]) -> dict:
    if a["id"] == b["id"]:
        raise CompareError("an asset cannot match itself")
    weight = 0.0
    for item in evidence:
        if not 0.0 < item["weight"] <= 1.0:
            raise CompareError("evidence weight must be in (0, 1]")
        weight = 1.0 - (1.0 - weight) * (1.0 - item["weight"])
    first, second = sorted((a["id"], b["id"]))
    return {
        "a": first,
        "b": second,
        "confidence": round(min(weight, 0.99), 4),
        "status": "candidate",
        "evidence": sorted(evidence, key=lambda item: (item["type"], item["detail"])),
    }


def confirm(link: dict, proof: str) -> dict:
    """Mark a candidate confirmed; requires a non-empty proof statement."""

    if not proof or not proof.strip():
        raise CompareError("confirmation requires explicit proof")
    return dict(link, status="confirmed", proof=proof.strip())


def match_textures(left: list[dict], right: list[dict], *, max_distance: int = 6) -> list[dict]:
    """Propose texture links between two asset lists.

    Exact decoded-pixel hashes are strong evidence; a perceptual distance
    of at most `max_distance` bits with the same aspect ratio is weaker.
    Solid or near-uniform images (dhash 0) are skipped to avoid noise.
    """

    by_sha: dict[str, list[dict]] = {}
    for item in right:
        by_sha.setdefault(item["fingerprints"]["rgba_sha256"], []).append(item)
    links = []
    for item in left:
        fp = item["fingerprints"]
        for other in by_sha.get(fp["rgba_sha256"], []):
            links.append(candidate(item, other, [{"type": "rgba_sha256", "detail": "identical decoded pixels", "weight": 0.97}]))
        if fp["dhash"] == 0:
            continue
        aspect = fp["width"] / fp["height"]
        for other in right:
            ofp = other["fingerprints"]
            if ofp["dhash"] == 0 or ofp["rgba_sha256"] == fp["rgba_sha256"]:
                continue
            if abs(ofp["width"] / ofp["height"] - aspect) > 0.05 * aspect:
                continue
            distance = (fp["dhash"] ^ ofp["dhash"]).bit_count()
            if distance > max_distance:
                continue
            evidence = [{"type": "dhash", "detail": f"{distance}/64 bits differ", "weight": round(0.75 - 0.08 * distance, 3)}]
            if (ofp["width"], ofp["height"]) == (fp["width"], fp["height"]):
                evidence.append({"type": "dimensions", "detail": f"{fp['width']}x{fp['height']}", "weight": 0.2})
            links.append(candidate(item, other, evidence))
    return sorted(links, key=lambda link: (-link["confidence"], link["a"], link["b"]))


def match_models(left: list[dict], right: list[dict]) -> list[dict]:
    """Propose model links from identical geometry hashes or matching counts and bounds."""

    links = []
    for item in left:
        fp = item["fingerprints"]
        for other in right:
            if other["id"] == item["id"]:
                continue
            ofp = other["fingerprints"]
            evidence = []
            if fp.get("geometry_sha256") and fp["geometry_sha256"] == ofp.get("geometry_sha256"):
                evidence.append({"type": "geometry_sha256", "detail": "identical vertices and primitives", "weight": 0.95})
            else:
                if fp["vertex_count"] == ofp["vertex_count"] and fp["triangle_count"] == ofp["triangle_count"] and fp["vertex_count"] > 8:
                    evidence.append({"type": "counts", "detail": f"{fp['vertex_count']} vertices, {fp['triangle_count']} triangles", "weight": 0.35})
                spans = [ofp["bounds_size"][axis] / fp["bounds_size"][axis] for axis in range(3) if fp["bounds_size"][axis]]
                if evidence and spans and max(spans) - min(spans) < 0.02:
                    evidence.append({"type": "bounds_shape", "detail": "same bounding-box proportions", "weight": 0.3})
            if evidence:
                links.append(candidate(item, other, evidence))
    return sorted(links, key=lambda link: (-link["confidence"], link["a"], link["b"]))


def duplicate_models(models: list[dict]) -> list[dict]:
    """Link models with identical geometry hashes (each to the first of its group)."""

    groups: dict[str, list[dict]] = {}
    for item in sorted(models, key=lambda model: model["id"]):
        groups.setdefault(item["fingerprints"]["geometry_sha256"], []).append(item)
    links = []
    for members in groups.values():
        for other in members[1:]:
            links.append(
                candidate(members[0], other, [{"type": "geometry_sha256", "detail": "identical vertices and primitives", "weight": 0.95}])
            )
    return sorted(links, key=lambda link: (link["a"], link["b"]))


def match_names(left: list[dict], right: list[dict]) -> list[dict]:
    links = []
    index: dict[str, list[dict]] = {}
    for other in right:
        index.setdefault(other["normalised_name"], []).append(other)
    for item in left:
        for other in index.get(item["normalised_name"], []):
            if other["id"] != item["id"]:
                links.append(candidate(item, other, [{"type": "normalised_name", "detail": item["normalised_name"], "weight": 0.4}]))
    return sorted(links, key=lambda link: (-link["confidence"], link["a"], link["b"]))


def document(assets: list[dict], links: list[dict], sources: dict, catalog: list[dict] | None = None) -> dict:
    ids = {item["id"] for item in assets}
    if len(ids) != len(assets):
        raise CompareError("duplicate asset IDs")
    for link in links:
        if link["a"] not in ids or link["b"] not in ids:
            raise CompareError("link references an unknown asset")
        if link["status"] not in STATUSES:
            raise CompareError("bad link status")
        if link["status"] == "confirmed" and not link.get("proof"):
            raise CompareError("confirmed link without proof")
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "sources": sources,
        "assets": sorted(assets, key=lambda item: item["id"]),
        "links": links,
        "catalog": catalog or [],
    }
