"""Cross-platform candidate matching over the shared neutral catalog.

Every comparison accounts for the conventions the adapters cannot unify by
proof alone:

* scale: shape signatures are scale-free; a matched pair reports the
  extent ratio as a scale estimate rather than assuming one;
* axes / rotation: D2 distance histograms and sorted extents do not
  depend on orientation; level layouts are compared under the eight
  rotations and reflections that keep +Y up;
* mirroring: D2 cannot see reflection, so a signed skew pattern is
  reported separately as a mirror indicator;
* triangle order / triangulation: signatures use positions and area-free
  pair distances, not index order; triangle counts are compared loosely;
* textures/materials: texture evidence is kept separate and never decides
  a match on its own.

Results are candidates with evidence and an uncertainty note.  Nothing is
labelled confirmed, and a texture hash or a name alone only ever yields a
"needs visual inspection" candidate.
"""

from __future__ import annotations

import math

BINS = 16


def shape_signature(positions: list[list[float]], triangle_count: int) -> dict | None:
    if len(positions) < 4:
        return None
    unique = sorted({tuple(round(v, 3) for v in p) for p in positions})
    if len(unique) < 4:
        return None
    step = max(1, len(unique) // 160)
    sample = unique[::step]
    centre = [sum(p[i] for p in unique) / len(unique) for i in range(3)]
    extents = sorted(max(p[i] for p in unique) - min(p[i] for p in unique) for i in range(3))
    if extents[2] <= 0:
        return None
    # Near-flat pieces (ground tiles, decals) match almost anything; they are
    # not compared by shape.
    if extents[0] / extents[2] < 0.02:
        return None
    distances = []
    for i in range(len(sample)):
        for j in range(i + 1, len(sample)):
            distances.append(math.dist(sample[i], sample[j]))
    longest = max(distances) or 1.0
    histogram = [0] * BINS
    for distance in distances:
        histogram[min(BINS - 1, int(distance / longest * BINS))] += 1
    total = sum(histogram)
    skews = []
    for axis in range(3):
        values = [p[axis] - centre[axis] for p in unique]
        spread = math.sqrt(sum(v * v for v in values) / len(values)) or 1.0
        skews.append(sum((v / spread) ** 3 for v in values) / len(values))
    return {
        "d2": [round(count / total, 5) for count in histogram],
        "extent_ratios": [round(extents[0] / extents[2], 4), round(extents[1] / extents[2], 4)],
        "longest": longest,
        "vertices": len(unique),
        "triangles": triangle_count,
        "skew_signs": [1 if s > 0.05 else (-1 if s < -0.05 else 0) for s in skews],
    }


def model_signature(record: dict) -> dict | None:
    mesh = record["mesh"]
    triangles = mesh["triangles"]
    if mesh.get("lods"):
        lod0 = [entry for entry in mesh["lods"] if entry["index"] == 0]
        if lod0:
            triangles = triangles[lod0[0]["first_triangle"] : lod0[0]["first_triangle"] + lod0[0]["triangle_count"]]
    used = sorted({corner["p"] for triangle in triangles for corner in triangle["corners"]})
    return shape_signature([mesh["positions"][i] for i in used], len(triangles))


def compare_shapes(a: dict, b: dict) -> dict | None:
    d2 = sum(abs(x - y) for x, y in zip(a["d2"], b["d2"]))
    ratio = max(abs(x - y) for x, y in zip(a["extent_ratios"], b["extent_ratios"]))
    count_ratio = min(a["triangles"], b["triangles"]) / max(a["triangles"], b["triangles"], 1)
    if d2 > 0.25 or ratio > 0.12 or count_ratio < 0.35:
        return None
    evidence = [
        {"type": "shape_d2", "detail": f"L1 {d2:.3f} (rotation/mirror/scale invariant)", "weight": round(max(0.05, 0.6 - 2.0 * d2), 3)},
        {"type": "extent_ratios", "detail": f"max difference {ratio:.3f}", "weight": round(max(0.05, 0.3 - 2.0 * ratio), 3)},
        {"type": "triangle_count", "detail": f"{a['triangles']} vs {b['triangles']}", "weight": round(0.2 * count_ratio, 3)},
    ]
    mirrored = [x * y for x, y in zip(a["skew_signs"], b["skew_signs"]) if x and y]
    return {
        "evidence": evidence,
        "scale_estimate": round(b["longest"] / a["longest"], 4) if a["longest"] else None,
        "mirror_indicator": "possible reflection" if mirrored.count(-1) % 2 == 1 else "no reflection indicated",
    }


def combine(weights: list[float]) -> float:
    value = 0.0
    for weight in weights:
        value = 1 - (1 - value) * (1 - weight)
    return round(min(value, 0.99), 4)


def match_models(left: list[tuple[str, dict]], right: list[tuple[str, dict]], limit_per_model: int = 3) -> list[dict]:
    """Candidate model links between two signature lists (pruned by buckets)."""

    buckets: dict[tuple, list] = {}
    for key, signature in right:
        bucket = (round(signature["extent_ratios"][0], 1), round(signature["extent_ratios"][1], 1))
        buckets.setdefault(bucket, []).append((key, signature))
    links = []
    for key, signature in left:
        base = (round(signature["extent_ratios"][0], 1), round(signature["extent_ratios"][1], 1))
        candidates = []
        for dx in (-0.1, 0.0, 0.1):
            for dy in (-0.1, 0.0, 0.1):
                for other_key, other in buckets.get((round(base[0] + dx, 1), round(base[1] + dy, 1)), []):
                    result = compare_shapes(signature, other)
                    if result:
                        confidence = combine([e["weight"] for e in result["evidence"]])
                        candidates.append((confidence, other_key, result))
        for confidence, other_key, result in sorted(candidates, key=lambda c: (-c[0], c[1]))[:limit_per_model]:
            links.append({"a": key, "b": other_key, "confidence": confidence, "status": "candidate", **result,
                          "uncertainty": "geometry-only evidence; textures and placements not considered"})
    return sorted(links, key=lambda link: (-link["confidence"], link["a"], link["b"]))


TRANSFORMS = [(sx, sz, swap) for swap in (False, True) for sx in (1, -1) for sz in (1, -1)]


def layout_points(scene: dict) -> list[tuple[float, float]]:
    return [(float(i["translation"][0]), float(i["translation"][2])) for i in scene.get("instances", [])]


def normalise_layout(points: list[tuple[float, float]], samples: int = 64) -> tuple[list, float] | None:
    """Centre, scale to unit RMS radius and evenly subsample a layout (deterministic)."""

    if len(points) < 5:
        return None
    ordered = sorted(points)
    cx = sum(p[0] for p in ordered) / len(ordered)
    cz = sum(p[1] for p in ordered) / len(ordered)
    radius = math.sqrt(sum((p[0] - cx) ** 2 + (p[1] - cz) ** 2 for p in ordered) / len(ordered)) or 1.0
    step = max(1, len(ordered) // samples)
    return [((p[0] - cx) / radius, (p[1] - cz) / radius) for p in ordered[::step]], radius


def compare_layouts(a, b) -> dict | None:
    """Best symmetric chamfer distance over 8 Y-up rotations/reflections, after scale fit.

    Accepts raw point lists or normalise_layout results.
    """

    if a is None or b is None:
        return None
    na = a if isinstance(a, tuple) else normalise_layout(a)
    nb = b if isinstance(b, tuple) else normalise_layout(b)
    if na is None or nb is None:
        return None
    (sa, ra), (sb, rb) = na, nb

    def chamfer(p, q):
        return sum(min((x - u) ** 2 + (z - v) ** 2 for u, v in q) for x, z in p) / len(p)

    best = None
    for sx, sz, swap in TRANSFORMS:
        moved = [((z if swap else x) * sx, (x if swap else z) * sz) for x, z in sb]
        score = math.sqrt(0.5 * (chamfer(sa, moved) + chamfer(moved, sa)))
        determinant = sx * sz * (-1 if swap else 1)
        if best is None or score < best[0]:
            best = (score, sx, sz, swap, determinant)
    score, sx, sz, swap, determinant = best
    return {"rms_distance": round(score, 4), "transform": {"flip_x": sx < 0, "flip_z": sz < 0, "swap_xz": swap},
            "reflection": determinant < 0, "scale_estimate": round(rb / ra, 4) if ra else None}
