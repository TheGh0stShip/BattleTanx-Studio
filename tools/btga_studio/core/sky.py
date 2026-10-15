"""Sky panoramas: a 360-degree picture around the horizon of a map.

A map's first environment record may carry::

    "sky": {"texture": "<texture id>", "north": 0.0, "horizon": 0.75, "vertical_degrees": 45.0}

The picture wraps once around the viewer.  Its left edge faces compass
north and bearings increase to the right (east a quarter of the way in).
``north`` turns the picture: the bearing of map north measured from Studio's
-Z axis (Studio's camera looks down -Z at yaw 0, +X is east).  ``horizon`` is
the picture row (0 = top, 1 = bottom) on the horizon and ``vertical_degrees``
how many degrees of elevation the picture height covers.  The panorama sits
at infinity: it moves with the camera, so distant scenery such as a
neighbouring city's skyline stays on its true bearing from anywhere on the map.
"""

from __future__ import annotations

import math

import numpy as np

SEGMENTS = 96
RINGS = 12
MAX_ELEVATION = 85.0


class SkyError(ValueError):
    pass


def panorama_info(width: int, height: int, horizon: float | None = None) -> dict:
    """Defaults for a picture: square pixels cover 360 degrees across; a 2:1 picture is a full sphere."""

    vertical = min(180.0, 360.0 * height / width)
    return {"horizon": 0.5 if horizon is None else float(horizon), "vertical_degrees": round(vertical, 4)}


def check(sky, textures: set[str] | None = None) -> dict:
    if not isinstance(sky, dict) or not isinstance(sky.get("texture"), str):
        raise SkyError("a sky needs a texture")
    if textures is not None and sky["texture"] not in textures:
        raise SkyError(f"sky texture {sky['texture']} is not in any loaded library")
    north, horizon, vertical = sky.get("north", 0.0), sky.get("horizon", 0.5), sky.get("vertical_degrees", 90.0)
    for value, low, high, what in ((north, -360.0, 360.0, "north"), (horizon, 0.0, 1.0, "horizon"), (vertical, 1.0, 180.0, "vertical_degrees")):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise SkyError(f"sky {what} must be between {low:g} and {high:g}")
    return {"texture": sky["texture"], "north": float(north), "horizon": float(horizon), "vertical_degrees": float(vertical)}


def direction(bearing_degrees: float, north: float = 0.0) -> tuple[float, float]:
    """World (x, z) unit direction of a compass bearing."""

    angle = math.radians(bearing_degrees + north)
    return math.sin(angle), -math.cos(angle)


def vertices(sky: dict, radius: float) -> np.ndarray:
    """Triangles (position 3, uv 2, colour 4 per vertex) of the panorama sphere around the origin.

    Rows of the picture are equal steps of elevation, so the picture wraps a
    sphere (not a cylinder).  Above and below the picture the first and last
    rows continue to the poles, so no viewing angle shows a gap.
    """

    sky = check(sky)
    vertical, horizon = sky["vertical_degrees"], sky["horizon"]
    top = min(MAX_ELEVATION, horizon * vertical)
    bottom = max(-MAX_ELEVATION, -(1.0 - horizon) * vertical)
    elevations = np.concatenate([[-89.0], np.linspace(bottom, top, RINGS + 1), [89.0]])
    out = []
    for column in range(SEGMENTS):
        u0, u1 = column / SEGMENTS, (column + 1) / SEGMENTS
        (x0, z0), (x1, z1) = direction(u0 * 360.0, sky["north"]), direction(u1 * 360.0, sky["north"])
        for ring in range(len(elevations) - 1):
            e0, e1 = math.radians(elevations[ring]), math.radians(elevations[ring + 1])
            v0 = min(1.0, max(0.0, horizon - elevations[ring] / vertical))
            v1 = min(1.0, max(0.0, horizon - elevations[ring + 1] / vertical))
            c0, c1 = math.cos(e0) * radius, math.cos(e1) * radius
            y0, y1 = math.sin(e0) * radius, math.sin(e1) * radius
            a = (x0 * c0, y0, z0 * c0, u0, v0)
            b = (x1 * c0, y0, z1 * c0, u1, v0)
            c = (x1 * c1, y1, z1 * c1, u1, v1)
            d = (x0 * c1, y1, z0 * c1, u0, v1)
            for corner in (a, b, c, a, c, d):
                out.append(corner + (1.0, 1.0, 1.0, 1.0))
    return np.asarray(out, dtype=np.float32)
