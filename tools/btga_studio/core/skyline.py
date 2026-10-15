"""Compute a sky panorama of the real horizon around a map.

From the map centre, every column of the picture is a compass bearing (left
edge north, east a quarter of the way in).  For each bearing the ground is
sampled outward from the map edge to ``reach`` metres on Terrarium
elevation tiles, with a tree/rooftop canopy, earth curvature and standard
refraction; then the tall buildings, domes and masts beyond the map edge are
drawn at their true bearing, angular width and angular height.  Colours fade
into the horizon haze with distance (aerial perspective).  Everything inside
the map is left out because the map models it in 3D.

The picture follows ``core.sky``: ``vertical_degrees`` of elevation over its
height with the horizon at row ``horizon``.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

import numpy as np

from . import geo, osm

EARTH_RADIUS = 6371008.8
REFRACTION = 0.13  # standard atmospheric refraction coefficient


@dataclass
class SkylineOptions:
    centre: tuple[float, float]
    viewer_height_m: float = 40.0  # above the ground at the map centre
    half_extent_m: float = 2048.0  # features inside the square map are modelled in 3D
    reach_m: float = 45_000.0
    canopy_m: float = 11.0
    width: int = 4096
    height: int = 512
    vertical_degrees: float = 45.0
    horizon: float = 0.75
    haze_m: float = 22_000.0
    zoom: int | None = None  # elevation tile level for the horizon (None: the finest available)


def _drop(distance: np.ndarray) -> np.ndarray:
    """Apparent drop of a distant point below the viewer's level from curvature and refraction."""

    return distance * distance / (2 * EARTH_RADIUS) * (1 - REFRACTION)


def _haze(colour: np.ndarray, distance, haze_colour: np.ndarray, haze_m: float) -> np.ndarray:
    amount = 1 - np.exp(-np.asarray(distance, float) / haze_m)
    return colour * (1 - amount[..., None]) + haze_colour * amount[..., None]


def landmarks_from_wikidata(document: dict) -> list[dict]:
    """Points with heights from a Wikidata query result (?item ?itemLabel ?coord ?height ?heightUnit ?floors)."""

    out = []
    for row in (document.get("results") or {}).get("bindings", []):
        match = re.match(r"Point\(([-\d.]+) ([-\d.]+)\)", row.get("coord", {}).get("value", ""))
        if not match:
            continue
        heights = osm.heights_from_wikidata({"results": {"bindings": [row]}})
        entry = next(iter(heights.values()), None)
        if not entry:
            continue
        tall = entry.get("height") or (entry["building:levels"] * osm.LEVEL_HEIGHT if entry.get("building:levels") else None)
        if not tall:
            continue
        name = row.get("itemLabel", {}).get("value", "")
        out.append({"lat": float(match.group(2)), "lon": float(match.group(1)), "height": float(tall), "name": name,
                    "dome": bool(re.search(r"cathedral|basilica|capitol", name, re.I)), "source": entry["source"]})
    return out


def render(options: SkylineOptions, terrain: geo.Terrain, features: osm.OsmData | None = None,
           landmarks: list[dict] = ()) -> tuple[np.ndarray, dict]:
    """(RGBA uint8 picture [height, width, 4], summary) of the horizon around ``options.centre``.

    ``landmarks`` (for example from ``landmarks_from_wikidata``) supply heights
    for map features that have none, matched within 150 m, and are drawn on
    their own (35 m wide) where no map feature matches.
    """

    lat0, lon0 = options.centre
    projection = geo.LocalProjection(lat0, lon0)
    ground = terrain.elevation([lat0], [lon0], options.zoom)[0]
    if math.isnan(ground):
        raise geo.GeoError("the elevation tiles do not cover the map centre")
    eye = ground + options.viewer_height_m
    width, height = options.width, options.height
    per_degree = height / options.vertical_degrees
    horizon_row = options.horizon * height
    rows = np.arange(height) + 0.5
    row_angle = np.radians((horizon_row - rows) / per_degree)  # elevation angle of each row (radians)
    bearings = (np.arange(width) + 0.5) / width * 360.0

    zenith, horizon_sky = np.array([62.0, 118.0, 196.0]), np.array([196.0, 214.0, 230.0])
    haze_colour = np.array([182.0, 196.0, 210.0])
    t = np.clip(np.degrees(row_angle) / 34.0, 0, 1) ** 0.55
    sky = horizon_sky * (1 - t[:, None]) + zenith * t[:, None]
    image = np.repeat(sky[:, None, :], width, axis=1)

    # Ground: sample every bearing from the map edge outwards, paint far to near.
    radians = np.radians(bearings)
    sine, cosine = np.sin(radians), np.cos(radians)
    edge = options.half_extent_m / np.maximum(np.abs(sine), np.abs(cosine))
    steps = np.concatenate([np.arange(0, 3000, 60.0), np.arange(3000, 12000, 150.0), np.arange(12000, options.reach_m, 400.0)])
    distances = edge[None, :] + steps[:, None]  # [samples, columns]
    east, north = distances * sine[None, :], distances * cosine[None, :]
    lat, lon = projection.inverse(east.ravel(), north.ravel())
    elevation = terrain.elevation(lat, lon, options.zoom).reshape(distances.shape)
    elevation = np.where(np.isnan(elevation), ground, elevation)
    surface = elevation + options.canopy_m * np.clip((distances - edge[None, :]) / 500.0, 0.3, 1.0)
    angle = np.arctan2(surface - eye - _drop(distances), distances)
    canopy, rooftops = np.array([60.0, 82.0, 58.0]), np.array([118.0, 114.0, 108.0])
    # Suburbs: patches of tree canopy and rooftops a few hundred metres across (deterministic).
    blocks = np.random.default_rng(7).random((len(steps), width // 8 + 1))
    urban = np.repeat(blocks, 8, axis=1)[:, :width] < 0.38
    for sample in range(len(steps) - 1, -1, -1):
        base = np.where(urban[sample][:, None], rooftops[None, :], canopy[None, :])
        shade = 0.9 + 0.1 * np.cos(np.radians(bearings) * 7 + sample)  # gentle variation between ridges
        colour = _haze(base * shade[:, None], distances[sample], haze_colour, options.haze_m)  # [columns, 3]
        mask = row_angle[:, None] <= angle[sample][None, :]
        if sample == 0:  # the closest ground fills every lower row: give it a pattern in both directions
            patches = np.repeat(np.repeat(np.random.default_rng(8).random((height // 4 + 1, width // 8 + 1)), 4, axis=0), 8, axis=1)
            nearest = np.where((patches[:height, :width] < 0.38)[..., None], rooftops, canopy) * shade[None, :, None]
            colour = _haze(nearest, distances[0][None, :], haze_colour, options.haze_m)
            image = np.where(mask[..., None], colour, image)
            continue
        image = np.where(mask[..., None], colour[None, :, :], image)

    drawn = []
    if features is not None:
        items = []
        for way in features.ways:
            tags = way["tags"]
            if not (tags.get("building") or tags.get("building:part") or tags.get("man_made")):
                continue
            items.append((tags, way["coords"], way["id"]))
        for relation in features.relations:
            coords = [c for m in relation["members"] if m["role"] == "outer" for c in m["coords"]]
            if coords:
                items.append((relation["tags"], coords, relation["id"]))
        for node in features.nodes:
            if node["tags"].get("man_made") in ("mast", "tower", "communications_tower", "water_tower"):
                items.append((node["tags"], [(node["lat"], node["lon"])], node["id"]))
        landmarks = [dict(item, used=False) for item in landmarks]
        for item in landmarks:
            item["east"], item["north"] = (float(v) for v in projection.forward(item["lat"], item["lon"]))
        shapes = []
        for tags, coords, identifier in items:
            points = np.asarray(coords, float)
            e, n = projection.forward(points[:, 0], points[:, 1])
            centre_e, centre_n = float(e.mean()), float(n.mean())
            if abs(centre_e) <= options.half_extent_m and abs(centre_n) <= options.half_extent_m:
                continue  # inside the map: modelled in 3D
            tall = osm.length(tags.get("height"))
            if tall is None:
                count = osm.levels(tags.get("building:levels"))
                tall = count * osm.LEVEL_HEIGHT if count else None
            if tall is None:
                near = [m for m in landmarks if math.hypot(m["east"] - centre_e, m["north"] - centre_n) < 150.0]
                if near:
                    best = min(near, key=lambda m: math.hypot(m["east"] - centre_e, m["north"] - centre_n))
                    best["used"] = True
                    tall = best["height"]
                    if best["dome"]:
                        tags = {**tags, "roof:shape": "dome"}
            if tall is None:
                tall = 45.0 if tags.get("man_made") == "water_tower" else None
            if tall is None or tall < 20:
                continue
            distance = math.hypot(centre_e, centre_n)
            if distance > options.reach_m:
                continue
            bearings_of = (np.degrees(np.arctan2(e, n)) + 360.0) % 360.0
            base = terrain.elevation([float(points[:, 0].mean())], [float(points[:, 1].mean())], options.zoom)[0]
            base = ground if math.isnan(base) else base
            low = osm.length(tags.get("min_height")) or 0.0
            shapes.append((distance, bearings_of, base, low, tall, tags, identifier))
        named = {s[5].get("name") for s in shapes if s[5].get("name")}
        for number, item in enumerate(landmarks):
            distance = math.hypot(item["east"], item["north"])
            if item["used"] or item["name"] in named or distance > options.reach_m or (abs(item["east"]) <= options.half_extent_m and abs(item["north"]) <= options.half_extent_m):
                continue
            half_angle = math.degrees(17.5 / distance)
            bearing = (math.degrees(math.atan2(item["east"], item["north"])) + 360.0) % 360.0
            base = terrain.elevation([item["lat"]], [item["lon"]], options.zoom)[0]
            tags = {"name": item["name"], **({"roof:shape": "dome"} if item["dome"] else {})}
            shapes.append((distance, np.array([bearing - half_angle, bearing + half_angle]), ground if math.isnan(base) else base, 0.0,
                           item["height"], tags, 10_000_000_000 + number))
        shapes.sort(key=lambda s: -s[0])
        for distance, bearings_of, base, low, tall, tags, identifier in shapes:
            spread = (bearings_of - bearings_of[0] + 180.0) % 360.0 - 180.0 + bearings_of[0]
            left, right = float(spread.min()), float(spread.max())
            mast = tags.get("man_made") in ("mast", "communications_tower", "tower") and len(bearings_of) == 1
            pixel_width = width / 360.0
            if mast:
                left, right = left - 0.5 / pixel_width, left + 0.5 / pixel_width
            elif (right - left) * pixel_width < 1.0:
                middle = (left + right) / 2
                left, right = middle - 0.5 / pixel_width, middle + 0.5 / pixel_width
            top = math.atan2(base + tall - eye - _drop(np.array(distance)), distance)
            bottom = math.atan2(base + low - eye - _drop(np.array(distance)), distance)
            columns = np.arange(int(math.floor(left * pixel_width)), int(math.ceil(right * pixel_width)) + 1) % width
            tone = 0.82 + 0.18 * ((identifier * 2654435761) % 1000) / 1000.0
            if mast:
                body = np.array([150.0, 150.0, 155.0])
            elif osm.colour(tags.get("building:colour")):
                body = np.array(osm.colour(tags["building:colour"])[:3]) * 200
            else:
                body = np.array([128.0, 132.0, 140.0]) * tone
            colour = _haze(body, distance, haze_colour, options.haze_m)
            dome = (tags.get("roof:shape") == "dome")
            floor_rows = max(1.0, osm.LEVEL_HEIGHT / distance * 180 / math.pi * per_degree)
            for column in columns:
                column_bearing = (column + 0.5) / pixel_width
                top_here = top
                if dome and right > left:
                    x = ((column_bearing - left) % 360.0) / (right - left) * 2 - 1
                    rise = top - bottom
                    top_here = bottom + rise * 0.6 + rise * 0.4 * math.sqrt(max(0.0, 1 - x * x))
                nearer = distances[:, column] < distance
                hidden_below = float(angle[nearer, column].max()) if nearer.any() else -math.pi / 2
                mask = (row_angle <= top_here) & (row_angle >= hidden_below)
                image[mask, column] = colour
                if floor_rows >= 3 and not mast:  # windows: darker bands one floor apart
                    window = mask & (((horizon_row - rows) % floor_rows) < floor_rows * 0.4)
                    image[window, column] = colour * 0.82
            if mast:  # aircraft warning light
                row = int(horizon_row - math.degrees(top) * per_degree)
                if 0 <= row < height:
                    image[row, columns[0]] = (230, 60, 50)
            drawn.append({"id": identifier, "name": tags.get("name"), "distance_m": round(distance), "bearing": round(float(np.mean(spread)) % 360, 2),
                          "height_m": round(tall, 1), "top_degrees": round(math.degrees(top), 3)})
    # The bottom rows continue to the nadir in the viewport: make them one even colour so no streaks fan out below the map.
    image[-4:] = image[-12:-4].reshape(-1, 3).mean(axis=0)
    pixels = np.dstack([np.clip(np.rint(image), 0, 255).astype(np.uint8), np.full((height, width), 255, dtype=np.uint8)])
    summary = {"viewer_elevation_m": round(float(eye), 1), "features": sorted(drawn, key=lambda d: d["distance_m"])}
    return pixels, summary
