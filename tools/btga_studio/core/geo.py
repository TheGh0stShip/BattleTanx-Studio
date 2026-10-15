"""Geographic helpers for map imports: projection, elevation tiles, polygons and rasterising.

* ``LocalProjection`` turns WGS84 latitude/longitude into metres east and
  north of an origin (local tangent plane; well under 0.1 % error across a
  few kilometres).
* ``Terrain`` reads Terrarium-encoded elevation tiles (``z/x/y.png``,
  elevation = R*256 + G + B/256 - 32768 metres), the format of the open
  Terrain Tiles dataset, and samples them bilinearly.
* Polygon tools: signed area, point-in-polygon, ear-clipping triangulation
  with holes, minimum-area bounding rectangles, multipolygon ring assembly.
* ``Canvas``: an RGB raster painted with filled polygons and thick lines.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from . import imaging

WGS84_A = 6378137.0
WGS84_E2 = 0.00669437999014


class GeoError(ValueError):
    pass


class LocalProjection:
    def __init__(self, lat0: float, lon0: float):
        if not (-85 <= lat0 <= 85 and -180 <= lon0 <= 180):
            raise GeoError("the map centre must be a latitude between -85 and 85 and a longitude between -180 and 180")
        self.lat0, self.lon0 = float(lat0), float(lon0)
        phi = math.radians(lat0)
        w = 1 - WGS84_E2 * math.sin(phi) ** 2
        self.m_per_lat = math.radians(1) * WGS84_A * (1 - WGS84_E2) / w ** 1.5
        self.m_per_lon = math.radians(1) * WGS84_A / math.sqrt(w) * math.cos(phi)

    def forward(self, lat, lon):
        """(east, north) metres; works on scalars and numpy arrays."""

        return (np.asarray(lon) - self.lon0) * self.m_per_lon, (np.asarray(lat) - self.lat0) * self.m_per_lat

    def inverse(self, east, north):
        return self.lat0 + np.asarray(north) / self.m_per_lat, self.lon0 + np.asarray(east) / self.m_per_lon


def bearing_distance(lat0: float, lon0: float, lat1: float, lon1: float) -> tuple[float, float]:
    """Initial great-circle bearing (degrees from north) and distance (metres)."""

    p0, p1 = math.radians(lat0), math.radians(lat1)
    dl = math.radians(lon1 - lon0)
    y = math.sin(dl) * math.cos(p1)
    x = math.cos(p0) * math.sin(p1) - math.sin(p0) * math.cos(p1) * math.cos(dl)
    bearing = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
    a = math.sin((p1 - p0) / 2) ** 2 + math.cos(p0) * math.cos(p1) * math.sin(dl / 2) ** 2
    return bearing, 2 * 6371008.8 * math.asin(min(1.0, math.sqrt(a)))


# ---------------------------------------------------------------- elevation --

def tile_xy(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    n = 2 ** zoom
    return (lon + 180.0) / 360.0 * n, (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n


class Terrain:
    """Bilinear elevation from a folder of Terrarium tiles laid out as ``z/x/y.png``."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.zooms = sorted((int(p.name) for p in self.folder.iterdir() if p.is_dir() and p.name.isdigit()), reverse=True) \
            if self.folder.is_dir() else []
        if not self.zooms:
            raise GeoError(f"{self.folder.name} holds no elevation tiles (expected folders named by zoom level: z/x/y.png)")
        self.tiles: dict[tuple[int, int, int], np.ndarray | None] = {}

    def _tile(self, zoom: int, x: int, y: int) -> np.ndarray | None:
        key = (zoom, x, y)
        if key not in self.tiles:
            path = self.folder / str(zoom) / str(x) / f"{y}.png"
            if path.is_file():
                width, height, rgba = imaging.decode(path.read_bytes(), path.name)
                if (width, height) != (256, 256):
                    raise GeoError(f"elevation tile {zoom}/{x}/{y} is {width}x{height}; Terrarium tiles are 256x256")
                pixels = np.frombuffer(rgba, dtype=np.uint8).reshape(256, 256, 4).astype(np.float64)
                self.tiles[key] = pixels[..., 0] * 256 + pixels[..., 1] + pixels[..., 2] / 256 - 32768
            else:
                self.tiles[key] = None
        return self.tiles[key]

    def elevation(self, lat, lon, zoom: int | None = None) -> np.ndarray:
        """Elevations in metres for arrays of positions (NaN where no tile covers the point)."""

        lat, lon = np.atleast_1d(np.asarray(lat, float)), np.atleast_1d(np.asarray(lon, float))
        out = np.full(lat.shape, np.nan)
        for level in ([zoom] if zoom is not None else self.zooms):
            missing = np.isnan(out)
            if not missing.any():
                break
            n = 2 ** level
            fx = (lon[missing] + 180.0) / 360.0 * n * 256 - 0.5
            fy = (1 - np.arcsinh(np.tan(np.radians(lat[missing]))) / np.pi) / 2 * n * 256 - 0.5
            values = np.full(fx.shape, np.nan)
            x0, y0 = np.floor(fx).astype(np.int64), np.floor(fy).astype(np.int64)
            tx, ty = fx - x0, fy - y0
            corners = []
            for dx, dy in ((0, 0), (1, 0), (0, 1), (1, 1)):
                px, py = x0 + dx, y0 + dy
                sample = np.full(fx.shape, np.nan)
                keys = np.stack([px // 256, py // 256], axis=1)
                for key in {tuple(k) for k in keys.tolist()}:
                    tile = self._tile(level, key[0], key[1])
                    if tile is None:
                        continue
                    mask = (keys[:, 0] == key[0]) & (keys[:, 1] == key[1])
                    sample[mask] = tile[py[mask] % 256, px[mask] % 256]
                corners.append(sample)
            values = (corners[0] * (1 - tx) * (1 - ty) + corners[1] * tx * (1 - ty) + corners[2] * (1 - tx) * ty + corners[3] * tx * ty)
            out[np.where(missing)[0]] = values
        return out


# ------------------------------------------------------------------ polygons --

def signed_area(ring) -> float:
    points = np.asarray(ring, float)
    if len(points) < 3:
        return 0.0
    x, y = points[:, 0], points[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(np.roll(x, -1), y))


def open_ring(ring) -> list:
    ring = [tuple(p) for p in ring]
    if len(ring) > 1 and ring[0] == ring[-1]:
        ring = ring[:-1]
    out = []
    for point in ring:
        if not out or math.dist(point, out[-1]) > 1e-6:
            out.append(point)
    if len(out) > 1 and math.dist(out[0], out[-1]) <= 1e-6:
        out.pop()
    return out


def point_in_ring(x: float, y: float, ring) -> bool:
    inside = False
    n = len(ring)
    for i in range(n):
        x0, y0 = ring[i]
        x1, y1 = ring[(i + 1) % n]
        if (y0 > y) != (y1 > y) and x < (x1 - x0) * (y - y0) / (y1 - y0) + x0:
            inside = not inside
    return inside


def assemble_rings(segments: list[list[tuple]]) -> list[list[tuple]]:
    """Join way segments of a multipolygon role into closed rings (open fragments are dropped)."""

    pending = [list(s) for s in segments if len(s) >= 2]
    rings = []
    while pending:
        ring = pending.pop(0)
        changed = True
        while ring[0] != ring[-1] and changed:
            changed = False
            for index, other in enumerate(pending):
                if other[0] == ring[-1]:
                    ring += other[1:]
                elif other[-1] == ring[-1]:
                    ring += other[-2::-1]
                elif other[-1] == ring[0]:
                    ring = other[:-1] + ring
                elif other[0] == ring[0]:
                    ring = other[:0:-1] + ring
                else:
                    continue
                pending.pop(index)
                changed = True
                break
        if ring[0] == ring[-1] and len(ring) >= 4:
            rings.append(ring)
    return rings


def _cross(o, a, b) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def convex_hull(points) -> list:
    points = sorted(set(map(tuple, points)))
    if len(points) <= 2:
        return points
    lower, upper = [], []
    for p in points:
        while len(lower) >= 2 and _cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(points):
        while len(upper) >= 2 and _cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def min_area_rectangle(points) -> tuple[tuple[float, float], float, float, float]:
    """(centre, angle radians, half-length along angle, half-width): smallest rectangle around the points."""

    hull = convex_hull(points)
    if len(hull) < 3:
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        return ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2), 0.0, (max(xs) - min(xs)) / 2, (max(ys) - min(ys)) / 2
    hull_array = np.asarray(hull)
    best = None
    for i in range(len(hull)):
        a, b = hull_array[i], hull_array[(i + 1) % len(hull)]
        angle = math.atan2(b[1] - a[1], b[0] - a[0])
        c, s = math.cos(angle), math.sin(angle)
        u = hull_array @ np.array([c, s])
        v = hull_array @ np.array([-s, c])
        area = (u.max() - u.min()) * (v.max() - v.min())
        if best is None or area < best[0]:
            cu, cv = (u.max() + u.min()) / 2, (v.max() + v.min()) / 2
            best = (area, (cu * c - cv * s, cu * s + cv * c), angle, (u.max() - u.min()) / 2, (v.max() - v.min()) / 2)
    return best[1], best[2], best[3], best[4]


def triangulate(outer, holes=()) -> list[tuple[int, int, int]]:
    """Ear clipping with holes (holes are bridged into the outer ring).

    Returns triangles as index triples into ``outer + holes[0] + holes[1] ...``,
    wound like ``outer`` (counter-clockwise when outer has positive signed area).
    Degenerate input falls back to a fan so a building always gets a roof.
    """

    points = [tuple(p) for p in outer] + [tuple(p) for hole in holes for p in hole]
    if len(outer) < 3:
        return []
    orientation = 1.0 if signed_area(outer) > 0 else -1.0
    ring = list(range(len(outer)))
    if orientation < 0:
        ring.reverse()
    offset = len(outer)
    hole_rings = []
    for hole in holes:
        indices = list(range(offset, offset + len(hole)))
        offset += len(hole)
        if len(hole) < 3:
            continue
        if signed_area(hole) > 0:
            indices.reverse()  # holes run clockwise against a counter-clockwise outer ring
        hole_rings.append(indices)
    hole_rings.sort(key=lambda h: -max(points[i][0] for i in h))
    for hole in hole_rings:
        ring = _bridge(points, ring, hole)
    triangles = _clip(points, ring)
    if orientation < 0:
        triangles = [(a, c, b) for a, b, c in triangles]
    return triangles


def _bridge(points, ring, hole):
    m = max(hole, key=lambda i: (points[i][0], -points[i][1]))
    mx, my = points[m]
    best, best_x = None, math.inf
    for k in range(len(ring)):
        a, b = points[ring[k]], points[ring[(k + 1) % len(ring)]]
        if (a[1] - my) * (b[1] - my) > 0 or a[1] == b[1]:
            continue
        x = a[0] + (my - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
        if mx <= x < best_x:
            best_x = x
            best = k if a[0] > b[0] else (k + 1) % len(ring)
    if best is None:
        best = min(range(len(ring)), key=lambda k: math.dist(points[ring[k]], (mx, my)))
    start = hole.index(m)
    loop = hole[start:] + hole[:start]
    return ring[:best + 1] + loop + [m, ring[best]] + ring[best + 1:]


def _clip(points, ring) -> list[tuple[int, int, int]]:
    ring = list(ring)
    out = []
    guard = 0
    while len(ring) > 3 and guard < 10 * len(ring) * len(ring):
        guard += 1
        n = len(ring)
        clipped = False
        for i in range(n):
            a, b, c = ring[i - 1], ring[i], ring[(i + 1) % n]
            pa, pb, pc = points[a], points[b], points[c]
            if _cross(pa, pb, pc) <= 1e-12:
                continue
            inside = False
            for other in ring:
                if other in (a, b, c):
                    continue
                p = points[other]
                if p in (pa, pb, pc):
                    continue
                if _cross(pa, pb, p) >= 0 and _cross(pb, pc, p) >= 0 and _cross(pc, pa, p) >= 0:
                    inside = True
                    break
            if inside:
                continue
            out.append((a, b, c))
            ring.pop(i)
            clipped = True
            break
        if not clipped:
            break
    if len(ring) == 3:
        out.append(tuple(ring))
    elif len(ring) > 3:  # degenerate remainder: fan it
        out += [(ring[0], ring[i], ring[i + 1]) for i in range(1, len(ring) - 1)]
    return out


# ------------------------------------------------------------------- raster --

class Canvas:
    """RGB raster covering [x0, x0 + size*scale) x [y0, y0 + size*scale) in map metres; row 0 is the north edge."""

    def __init__(self, west: float, north: float, width: int, height: int, metres_per_pixel: float, colour):
        self.west, self.north, self.mpp = float(west), float(north), float(metres_per_pixel)
        self.pixels = np.empty((height, width, 3), dtype=np.uint8)
        self.pixels[:] = colour

    def to_pixels(self, points) -> np.ndarray:
        points = np.asarray(points, float)
        return np.stack([(points[:, 0] - self.west) / self.mpp, (self.north - points[:, 1]) / self.mpp], axis=1)

    def fill(self, rings, colour, noise: np.ndarray | None = None) -> None:
        """Even-odd fill of one or more rings given in (east, north) metres."""

        edges = []
        for ring in rings:
            pixels = self.to_pixels(ring)
            edges.append(np.concatenate([pixels, np.roll(pixels, -1, axis=0)], axis=1))
        if not edges:
            return
        edges = np.concatenate(edges)
        edges = edges[edges[:, 1] != edges[:, 3]]
        if not len(edges):
            return
        height, width = self.pixels.shape[:2]
        top = max(0, int(math.floor(min(edges[:, 1].min(), edges[:, 3].min()))))
        bottom = min(height, int(math.ceil(max(edges[:, 1].max(), edges[:, 3].max()))) + 1)
        if top >= bottom:
            return
        rows = np.arange(top, bottom) + 0.5
        y0, y1 = edges[:, 1][None, :], edges[:, 3][None, :]
        crosses = (y0 <= rows[:, None]) != (y1 <= rows[:, None])
        with np.errstate(divide="ignore", invalid="ignore"):
            xs = edges[:, 0][None, :] + (rows[:, None] - y0) * (edges[:, 2] - edges[:, 0])[None, :] / (y1 - y0)
        xs = np.where(crosses, xs, np.inf)
        xs.sort(axis=1)
        counts = crosses.sum(axis=1)
        colour = np.asarray(colour, dtype=np.uint8)
        for r in range(len(rows)):
            values = xs[r, :counts[r]]
            for k in range(0, len(values) - 1, 2):
                start, end = max(0, int(math.ceil(values[k] - 0.5))), min(width, int(math.floor(values[k + 1] - 0.5)) + 1)
                if start < end:
                    row = top + r
                    self.pixels[row, start:end] = colour if noise is None else np.clip(
                        colour.astype(np.int16) + noise[row % noise.shape[0], np.arange(start, end) % noise.shape[1]][:, None], 0, 255)

    def line(self, points, width_m: float, colour, dash: tuple[float, float] | None = None) -> None:
        """A thick polyline (round joins) in (east, north) metres; ``dash`` = (on, off) metres."""

        pixels = self.to_pixels(points)
        radius = max(0.5, width_m / self.mpp / 2)
        height, width = self.pixels.shape[:2]
        colour = np.asarray(colour, dtype=np.uint8)
        travelled = 0.0
        for (ax, ay), (bx, by) in zip(pixels[:-1], pixels[1:]):
            length = math.hypot(bx - ax, by - ay)
            x0, x1 = int(max(0, math.floor(min(ax, bx) - radius))), int(min(width, math.ceil(max(ax, bx) + radius) + 1))
            y0, y1 = int(max(0, math.floor(min(ay, by) - radius))), int(min(height, math.ceil(max(ay, by) + radius) + 1))
            if x0 >= x1 or y0 >= y1:
                travelled += length
                continue
            gy, gx = np.mgrid[y0:y1, x0:x1] + 0.5
            if length < 1e-9:
                t = np.zeros_like(gx)
            else:
                t = np.clip(((gx - ax) * (bx - ax) + (gy - ay) * (by - ay)) / (length * length), 0, 1)
            distance = np.hypot(gx - (ax + t * (bx - ax)), gy - (ay + t * (by - ay)))
            mask = distance <= radius
            if dash is not None:
                on, off = dash[0] / self.mpp, dash[1] / self.mpp
                mask &= ((travelled + t * length) % (on + off)) < on
            self.pixels[y0:y1, x0:x1][mask] = colour
            travelled += length

    def crop(self, west: float, north: float, size_m: float) -> np.ndarray:
        column, row = int(round((west - self.west) / self.mpp)), int(round((self.north - north) / self.mpp))
        count = int(round(size_m / self.mpp))
        return self.pixels[row:row + count, column:column + count]
