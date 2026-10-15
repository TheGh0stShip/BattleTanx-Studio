"""Turn OpenStreetMap data (and optional elevation tiles) into a Studio city map.

Input: an ``.osm`` XML export (openstreetmap.org > Export) or Overpass API
JSON written with ``out geom``.  Optional: a folder of Terrarium elevation
tiles (``z/x/y.png``).  Output: a ``CityPlan`` holding an ``ImportResult``
(models and textures for the user's library) plus the instances, collision
boxes and map limits that ``apply_plan`` places on a blank map in one
undoable step.

What is built, at true scale (1 metre = 16 Studio units, map north = -Z):

* ground: a terrain mesh per 256 m chunk following the elevation tiles, with
  a 1 m-per-pixel picture painted from land use, parks, water, parking,
  railways, roads (by class, with centre lines) and paths;
* buildings: every ``building`` and ``building:part`` outline extruded from
  the lowest ground under it (``min_height`` for parts) to its height, with
  flat, pyramidal/hipped/gabled (as a hip) or dome roofs; facades chosen
  from material, colour, type and height; outlines that have parts are drawn
  by their parts only (the Simple 3D Buildings convention); named tall
  buildings, stadiums and domes become their own models so they can be
  selected by name;
* heights: ``height``/``min_height``/``roof:height`` (metres, feet ``'`` or
  ``ft``), else levels x 3.4 m, else an estimate from building type and size
  (counted in ``CityPlan.estimated``);
* bridges for roads, paths and railways (decks between the ground at their
  ends, with piers), and covered/indoor footbridges between buildings as
  glass skyways at second-floor height;
* trees from ``natural=tree`` points plus woods and scrub;
* one collision box (minimum-area rectangle) per building outline, marked
  ``allow_overlap`` because neighbouring rectangles of irregular outlines
  may overlap; boxes that would cross the map edge are left out.

OpenStreetMap data is (c) OpenStreetMap contributors under the ODbL; the
credit text travels with every model and package made from it.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from tools.btga_assets import mesh as meshmod

from . import geo, imaging
from .imports import IMPORTER_VERSION, PLATFORM, ImportOptions, ImportProblem, ImportResult, content_id, make_texture

SCALE = 16.0  # Studio units per metre
OSM_CREDIT = "Map data (c) OpenStreetMap contributors, ODbL 1.0 (openstreetmap.org/copyright)"
ELEVATION_CREDIT = "Elevation: Terrain Tiles (USGS 3DEP and other public sources) via the AWS Open Data Registry"
LEVEL_HEIGHT = 3.4
MAX_ELEMENTS = 400_000


# ------------------------------------------------------------------- reading --

@dataclass
class OsmData:
    ways: list[dict] = field(default_factory=list)  # {"id", "tags", "coords": [(lat, lon)]}
    relations: list[dict] = field(default_factory=list)  # {"id", "tags", "members": [{"role", "coords"}]}
    nodes: list[dict] = field(default_factory=list)  # tagged nodes {"id", "tags", "lat", "lon"}
    bounds: tuple[float, float, float, float] | None = None  # south, west, north, east
    sha256: str = ""
    file: str = ""


def read_osm(path: Path) -> OsmData:
    path = Path(path)
    data = path.read_bytes()
    if len(data) > 512 * 1024 * 1024:
        raise ImportProblem(f"{path.name} is larger than 512 MB; export a smaller area")
    out = OsmData(sha256=hashlib.sha256(data).hexdigest(), file=path.name)
    head = data[:200].lstrip()
    if head.startswith(b"{"):
        _read_json(json.loads(data.decode("utf-8")), out)
    elif head.startswith(b"<"):
        _read_xml(data, out)
    else:
        raise ImportProblem(f"{path.name} is not OpenStreetMap XML (.osm) or Overpass JSON")
    if not out.ways and not out.nodes:
        raise ImportProblem(f"{path.name} holds no map features")
    return out


def _read_json(document: dict, out: OsmData) -> None:
    elements = document.get("elements")
    if not isinstance(elements, list):
        raise ImportProblem("the JSON file is not an Overpass API result")
    if len(elements) > MAX_ELEMENTS:
        raise ImportProblem(f"more than {MAX_ELEMENTS:,} map features; export a smaller area")
    nodes = {}
    for element in elements:
        if element.get("type") == "node" and "lat" in element:
            nodes[element["id"]] = (element["lat"], element["lon"])
    for element in elements:
        kind, tags = element.get("type"), element.get("tags") or {}
        if kind == "node" and tags:
            out.nodes.append({"id": element["id"], "tags": tags, "lat": element["lat"], "lon": element["lon"]})
        elif kind == "way":
            if "geometry" in element:
                coords = [(p["lat"], p["lon"]) for p in element["geometry"] if p]
            else:
                coords = [nodes[n] for n in element.get("nodes", []) if n in nodes]
            if len(coords) >= 2:
                out.ways.append({"id": element["id"], "tags": tags, "coords": coords})
        elif kind == "relation":
            members = []
            for member in element.get("members", []):
                if member.get("type") == "way" and member.get("geometry"):
                    members.append({"role": member.get("role") or "outer", "coords": [(p["lat"], p["lon"]) for p in member["geometry"] if p]})
            out.relations.append({"id": element["id"], "tags": tags, "members": members})


def _read_xml(data: bytes, out: OsmData) -> None:
    if b"<!DOCTYPE" in data[:4096] or b"<!ENTITY" in data[:4096]:
        raise ImportProblem("the .osm file declares entities, which are not allowed")
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as error:
        raise ImportProblem(f"the .osm file is damaged ({error})") from error
    if root.tag != "osm":
        raise ImportProblem("the XML file is not an OpenStreetMap export")
    bounds = root.find("bounds")
    if bounds is not None:
        out.bounds = tuple(float(bounds.get(k)) for k in ("minlat", "minlon", "maxlat", "maxlon"))
    nodes, ways = {}, {}
    count = 0
    for element in root:
        count += 1
        if count > MAX_ELEMENTS * 4:
            raise ImportProblem("the .osm file is too large; export a smaller area")
        tags = {t.get("k"): t.get("v") for t in element.findall("tag")}
        if element.tag == "node":
            lat, lon = float(element.get("lat")), float(element.get("lon"))
            nodes[element.get("id")] = (lat, lon)
            if tags:
                out.nodes.append({"id": int(element.get("id")), "tags": tags, "lat": lat, "lon": lon})
        elif element.tag == "way":
            coords = [nodes[n.get("ref")] for n in element.findall("nd") if n.get("ref") in nodes]
            ways[element.get("id")] = coords
            if len(coords) >= 2:
                out.ways.append({"id": int(element.get("id")), "tags": tags, "coords": coords})
        elif element.tag == "relation":
            members = [{"role": m.get("role") or "outer", "coords": ways[m.get("ref")]} for m in element.findall("member")
                       if m.get("type") == "way" and m.get("ref") in ways]
            out.relations.append({"id": int(element.get("id")), "tags": tags, "members": members})


def merge(*datasets: OsmData) -> OsmData:
    """Several downloads of the same area as one (features present in more than one are kept once)."""

    out = OsmData(sha256=hashlib.sha256("|".join(d.sha256 for d in datasets).encode()).hexdigest(), file=" + ".join(d.file for d in datasets))
    seen = set()
    for data in datasets:
        for family in ("ways", "relations", "nodes"):
            for element in getattr(data, family):
                if (family, element["id"]) not in seen:
                    seen.add((family, element["id"]))
                    getattr(out, family).append(element)
        out.bounds = out.bounds or data.bounds
    return out


# ------------------------------------------------------------------ tag values --

_FEET = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(?:'|ft|feet)\s*(?:(\d+(?:\.\d+)?)\s*(?:\"|in))?\s*$")


def length(value) -> float | None:
    """Metres from an OSM length: ``12``, ``12.5 m``, ``40'``, ``40 ft``, ``12'6"``."""

    if value is None:
        return None
    text = str(value).strip().replace(",", ".")
    match = _FEET.match(text)
    if match:
        return round(float(match.group(1)) * 0.3048 + float(match.group(2) or 0) * 0.0254, 3)
    match = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*(m|metres|meters)?\s*$", text)
    if match:
        number = float(match.group(1))
        return number if math.isfinite(number) else None
    return None


def levels(value) -> float | None:
    try:
        number = float(str(value).split(";")[0].strip())
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= 200 else None


NAMED_COLOURS = {"white": (240, 240, 235), "black": (40, 40, 40), "grey": (150, 150, 150), "gray": (150, 150, 150), "silver": (192, 192, 192),
                 "red": (170, 60, 50), "maroon": (120, 40, 35), "brown": (130, 90, 60), "tan": (210, 180, 140), "beige": (225, 210, 180),
                 "yellow": (220, 200, 90), "orange": (220, 130, 50), "green": (80, 130, 80), "blue": (70, 100, 160), "navy": (40, 50, 100),
                 "darkgrey": (90, 90, 90), "darkgray": (90, 90, 90), "lightgrey": (200, 200, 200), "lightgray": (200, 200, 200),
                 "cream": (240, 230, 200), "copper": (110, 160, 140), "sandstone": (205, 180, 140), "brick": (160, 80, 60)}


def colour(value) -> tuple[float, float, float, float] | None:
    if not value:
        return None
    text = str(value).strip().lower()
    match = re.fullmatch(r"#?([0-9a-f]{6}|[0-9a-f]{3})", text)
    if match:
        digits = match.group(1)
        if len(digits) == 3:
            digits = "".join(c * 2 for c in digits)
        rgb = tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4))
    elif text.replace(" ", "") in NAMED_COLOURS:
        rgb = NAMED_COLOURS[text.replace(" ", "")]
    else:
        return None
    return tuple(round(min(1.0, c / 255 * 1.15), 3) for c in rgb) + (1.0,)


# ------------------------------------------------------------------ textures --

def _noise(shape, seed: int, amplitude: float) -> np.ndarray:
    return np.random.default_rng(seed).normal(0, amplitude, shape)


def _texture_pixels(name: str, size: int = 128) -> np.ndarray:
    """Procedural facade, roof, deck, foliage and street pictures (rows top to bottom, RGBA).

    Patterns are laid out on a 128-unit design grid; a larger ``size`` samples
    them more finely and adds the small parts (window frames, mullions, sills)
    that high detail shows.
    """

    fine = size > 128
    y, x = (np.mgrid[0:size, 0:size] + 0.5) * (128.0 / size)
    image = np.zeros((size, size, 4), dtype=np.float64)
    image[..., 3] = 255
    floor_y = y % 64  # 4 m floors (64 px per floor, 8 m per picture)
    bay_x = x % 64
    if name == "glass":
        sky = 150 + 60 * (1 - y / size)
        image[..., 0], image[..., 1], image[..., 2] = sky * 0.62, sky * 0.78, sky * 0.95
        image[..., :3] += _noise((size, size, 1), 7, 4)
        mullion = (x % 32 < 2) | (floor_y < 4)
        image[mullion] = (205, 210, 215, 255)
    elif name == "glass_dark":
        image[..., :3] = (60, 72, 88)
        image[..., :3] += (y[..., None] % 64) * 0.4 + _noise((size, size, 1), 8, 3)
        mullion = (x % 21 < 2) | (floor_y < 3)
        image[mullion] = (110, 118, 128, 255)
    elif name in ("stone", "brick", "concrete", "house"):
        base = {"stone": (228, 222, 210), "brick": (200, 120, 100), "concrete": (205, 205, 200), "house": (232, 228, 220)}[name]
        image[..., :3] = base
        image[..., :3] += _noise((size, size, 1), 9, 6)
        if name == "brick":
            course = (y % 8 == 0) | (((x + (y // 8 % 2) * 8) % 16) == 0)
            image[course] = (215, 205, 195, 255)
        windows = (floor_y >= 16) & (floor_y < 52) & (bay_x >= 12) & (bay_x < 52) if name != "house" else \
                  (floor_y >= 22) & (floor_y < 46) & (bay_x >= 20) & (bay_x < 44)
        image[windows] = (58, 68, 82, 255)
        image[windows & (floor_y < 22)] = (95, 110, 125, 255)
        if fine:  # frames, a mullion and a sill under each window
            frame = windows & ~((floor_y >= 17) & (floor_y < 51) & (bay_x >= 13) & (bay_x < 51)) if name != "house" else \
                windows & ~((floor_y >= 23) & (floor_y < 45) & (bay_x >= 21) & (bay_x < 43))
            image[frame] = (232, 230, 224, 255)
            image[windows & (np.abs(bay_x - 32) < 0.6)] = (210, 210, 205, 255)
            sill = (floor_y >= (52 if name != "house" else 46)) & (floor_y < (54 if name != "house" else 48)) & \
                (bay_x >= (10 if name != "house" else 18)) & (bay_x < (54 if name != "house" else 46))
            image[sill] = (170, 168, 160, 255)
        if name == "house":
            siding = (y % 6 == 0) & ~windows
            image[siding] = (200, 196, 188, 255)
    elif name == "office_bands":  # ribbon windows between solid spandrels
        image[..., :3] = (198, 196, 190)
        image[..., :3] += _noise((size, size, 1), 24, 4)
        band = (floor_y >= 20) & (floor_y < 50)
        image[band] = (70, 88, 105, 255)
        image[band & (x % 16 < (0.8 if fine else 1.0))] = (180, 182, 184, 255)
    elif name == "apartments":  # punched windows with balconies
        image[..., :3] = (214, 200, 180)
        image[..., :3] += _noise((size, size, 1), 25, 5)
        window = (floor_y >= 14) & (floor_y < 50) & (bay_x >= 8) & (bay_x < 30)
        door = (floor_y >= 10) & (floor_y < 56) & (bay_x >= 36) & (bay_x < 56)
        image[window | door] = (60, 70, 84, 255)
        balcony = (floor_y >= 48) & (floor_y < 58) & (bay_x >= 33) & (bay_x < 59)
        image[balcony] = (150, 150, 152, 255)
        image[balcony & (x % 4 < 1)] = (110, 110, 112, 255)
    elif name == "hedge":
        image[..., :3] = (52, 92, 46)
        image[..., :3] += _noise((size, size, 1), 26, 14)
    elif name == "fence":
        image[..., 3] = 0
        mesh = (np.abs((x + y) % 12 - 6) < 0.8) | (np.abs((x - y) % 12 - 6) < 0.8) | (y < 4) | (x % 64 < 3)
        image[mesh] = (150, 152, 150, 255)
    elif name == "metal":
        image[..., :3] = (88, 92, 96)
        image[..., :3] += _noise((size, size, 1), 27, 3)
    elif name == "lamp":
        image[..., :3] = (250, 238, 200)
    elif name == "signal":
        image[..., :3] = (35, 35, 35)
        for row, colour in ((24, (220, 40, 30)), (64, (230, 180, 30)), (104, (40, 200, 70))):
            image[(np.abs(y - row) < 14) & (np.abs(x - 64) < 14)] = (*colour, 255)
    elif name == "parking":
        image[..., :3] = (190, 190, 186)
        opening = (floor_y >= 14) & (floor_y < 50)
        image[opening] = (55, 58, 62, 255)
        image[opening & (x % 32 < 3)] = (190, 190, 186, 255)
    elif name == "stadium":
        image[..., :3] = (70, 80, 95)
        image[..., :3] += _noise((size, size, 1), 10, 5)
        image[(x % 16 < 1) | (y % 32 < 1)] = (110, 118, 130, 255)
    elif name == "roof":
        image[..., :3] = 150 + _noise((size, size, 1), 11, 10)
        image[(x % 64 < 2) | (y % 64 < 2)] = (120, 120, 118, 255)
    elif name == "roof_tiles":
        image[..., :3] = (110, 70, 60)
        image[..., :3] += _noise((size, size, 1), 12, 8)
        image[y % 8 < 1] = (80, 50, 45, 255)
    elif name == "dome":
        image[..., :3] = (120, 170, 150)
        image[..., :3] += _noise((size, size, 1), 13, 8)
        image[x % 16 < 1] = (95, 140, 120, 255)
    elif name == "deck":
        image[..., :3] = 72 + _noise((size, size, 1), 14, 5)
        image[(x < 4) | (x >= size - 4)] = (190, 190, 185, 255)
    elif name == "bridge_stone":
        image[..., :3] = (196, 178, 150)
        image[..., :3] += _noise((size, size, 1), 15, 10)
        image[(y % 16 == 0) | (((x + (y // 16 % 2) * 16) % 32) == 0)] = (150, 135, 112, 255)
    elif name == "skyway":
        image[..., :3] = (120, 160, 180)
        image[..., :3] += _noise((size, size, 1), 16, 4)
        image[(x % 32 < 3) | (y < 10) | (y >= size - 10)] = (200, 205, 210, 255)
    elif name in ("tree", "conifer"):
        image[..., 3] = 0
        cx = size / 2
        if name == "tree":
            crown = ((x - cx) / 58) ** 2 + ((y - 50) / 50) ** 2 <= 1
            leaf = (70 + _noise((size, size), 17, 18), 115 + _noise((size, size), 18, 22), 55 + _noise((size, size), 19, 12))
        else:
            crown = (y >= 4) & (y < 112) & (np.abs(x - cx) <= (y - 4) * 0.5)
            leaf = (45 + _noise((size, size), 20, 10), 85 + _noise((size, size), 21, 14), 55 + _noise((size, size), 22, 10))
        trunk = (np.abs(x - cx) <= 5) & (y >= 80)
        image[trunk] = (95, 70, 50, 255)
        for channel in range(3):
            image[..., channel][crown] = leaf[channel][crown]
        image[..., 3][crown] = 255
        holes = crown & (_noise((size, size), 23, 1) > 1.6)
        image[..., 3][holes] = 0
    else:
        raise ValueError(name)
    return np.clip(np.rint(image), 0, 255).astype(np.uint8)


TEXTURE_NAMES = ("glass", "glass_dark", "stone", "brick", "concrete", "house", "parking", "stadium", "roof", "roof_tiles", "dome", "deck",
                 "bridge_stone", "skyway", "tree", "conifer")
HIGH_TEXTURE_NAMES = ("office_bands", "apartments", "hedge", "fence", "metal", "lamp", "signal")
FACADES = ("glass", "glass_dark", "stone", "brick", "concrete", "house", "office_bands", "apartments")
DETAILS = ("standard", "high")

GROUND = {"paved": (172, 170, 164), "lawn": (110, 140, 85), "grass": (98, 140, 70), "wood": (70, 105, 60), "water": (70, 96, 110),
          "rail": (128, 118, 105), "parking": (92, 94, 98), "road": (64, 66, 70), "path": (196, 192, 182), "pitch": (90, 150, 80),
          "sand": (210, 195, 150), "building": (120, 118, 112), "line_yellow": (220, 190, 70), "line_white": (225, 225, 220),
          "rail_steel": (70, 65, 60), "plaza": (190, 175, 160), "curb": (200, 198, 192), "zebra": (232, 232, 226)}
LANDUSE = {"residential": "lawn", "grass": "grass", "meadow": "grass", "recreation_ground": "grass", "village_green": "grass",
           "cemetery": "grass", "forest": "wood", "railway": "rail", "construction": "sand", "brownfield": "sand",
           "commercial": "paved", "retail": "paved", "industrial": "paved"}
LEISURE = {"park": "grass", "garden": "grass", "playground": "sand", "pitch": "pitch", "stadium": "paved", "track": "sand"}
NATURAL = {"water": "water", "wood": "wood", "scrub": "wood", "grassland": "grass", "sand": "sand", "beach": "sand"}
ROAD_WIDTH = {"motorway": 11.0, "trunk": 11.0, "primary": 13.0, "secondary": 12.0, "tertiary": 10.0, "residential": 8.0, "unclassified": 8.0,
              "living_street": 6.0, "service": 4.5, "motorway_link": 6.0, "trunk_link": 6.0, "primary_link": 7.0, "secondary_link": 7.0,
              "tertiary_link": 6.0, "pedestrian": 8.0, "busway": 7.0, "track": 3.0}
PATH_WIDTH = {"footway": 2.5, "cycleway": 3.0, "path": 2.5, "bridleway": 2.5}
CENTRE_LINE = {"primary", "secondary", "tertiary", "trunk"}


# ------------------------------------------------------------------ options --

@dataclass
class CityOptions:
    centre: tuple[float, float]  # latitude, longitude of the map centre
    half_extent_m: float = 2048.0
    chunk_m: float = 256.0
    terrain_spacing_m: float = 16.0
    ground_metres_per_pixel: float = 1.0
    landmark_height_m: float = 60.0
    collision: bool = True
    trees: bool = True
    group: str = "City"
    credit: str = ""
    title: str = "City"
    detail: str = "standard"  # "high": floor-accurate windows, parapets, gable and hip roofs, street lamps and signals, walls, fences,
    # hedges, crosswalks and curbs, street and park trees, sharper facades
    fine_ground_radius_m: float | None = None  # with a finer ground_metres_per_pixel: only chunks this close to fine_ground_centre
    fine_ground_centre: tuple[float, float] = (0.0, 0.0)  # (east, north) metres from the map centre
    overrides: dict = field(default_factory=dict)  # "way/123", "wikidata:Q1" or exact name -> tags that replace the map data
    heights: dict = field(default_factory=dict)  # same keys -> {"height"| "building:levels"} used only where the map data has neither


@dataclass
class CityPlan:
    result: ImportResult
    instances: list[dict]  # {"model", "translation", "yaw", "label"}
    collision: list[dict]  # collision shapes ready for add_collision
    half_extent: float  # Studio units
    reference_elevation: float  # metres above sea level at y = 0
    landmarks: dict[str, dict]  # name -> {"model", "centre": (x, y, z) units, "top": units, "height_m"}
    counts: dict[str, int]
    estimated: int  # buildings whose height was estimated
    credit: str


# ----------------------------------------------------------------- buildings --

@dataclass
class Building:
    key: str
    tags: dict
    outer: list  # (east, north) metres, open ring
    holes: list
    part: bool
    height: float = 0.0
    min_height: float = 0.0
    roof_shape: str = "flat"
    roof_height: float = 0.0
    estimated: bool = False
    base: float = 0.0  # ground elevation metres
    parent: "Building | None" = None
    has_parts: bool = False
    bowl: list | None = None  # open-air stadium: the playing field ring inside the stands
    roof_kind: str = "flat"  # the map's roof:shape (gabled, hipped, ...) before simplification
    level_count: float | None = None
    area: float = 0.0
    centroid: tuple = (0.0, 0.0)


def _estimate_height(tags: dict, area: float) -> float:
    kind = tags.get("building") or tags.get("building:part") or "yes"
    table = {"house": 8.0, "detached": 8.0, "semidetached_house": 8.0, "terrace": 9.0, "garage": 3.5, "garages": 3.5, "shed": 3.0,
             "roof": 4.0, "carport": 3.0, "kiosk": 3.0, "apartments": 15.0, "residential": 12.0, "dormitory": 15.0, "hotel": 30.0,
             "office": 20.0, "commercial": 10.0, "retail": 7.0, "industrial": 10.0, "warehouse": 10.0, "church": 16.0, "school": 12.0,
             "university": 15.0, "hospital": 20.0, "parking": 12.0, "train_station": 10.0, "transportation": 8.0, "stadium": 30.0,
             "civic": 15.0, "public": 15.0, "government": 20.0, "service": 4.0, "construction": 10.0}
    if kind in table:
        return table[kind]
    return 7.0 if area < 150 else 10.0 if area < 1000 else 14.0 if area < 5000 else 18.0


def _lookup(table: dict, building: Building) -> dict:
    tags = building.tags
    for key in (building.key, f"wikidata:{tags.get('wikidata')}" if tags.get("wikidata") else None, tags.get("name")):
        if key and key in table:
            return table[key]
    return {}


def _heights(building: Building, overrides: dict, fill: dict | None = None) -> None:
    tags = dict(building.tags)
    tags.update({k: str(v) for k, v in _lookup(overrides, building).items()})
    if fill and tags.get("height") is None and tags.get("building:levels") is None:
        extra = _lookup(fill, building)
        tags.update({k: str(v) for k, v in extra.items() if k in ("height", "building:levels")})
        if extra:
            building.tags = {**building.tags, "studio:height_source": str(extra.get("source", "heights file"))}
    height = length(tags.get("height"))
    roof_height = length(tags.get("roof:height"))
    roof_levels = levels(tags.get("roof:levels"))
    if roof_height is None and roof_levels is not None:
        roof_height = roof_levels * LEVEL_HEIGHT * 0.8
    if height is None:
        count = levels(tags.get("building:levels"))
        if count is not None and count > 0:
            height = count * LEVEL_HEIGHT + (roof_height or 0.0)
        elif building.part and building.parent is not None and building.parent.height:
            height = building.parent.height
        else:
            height = _estimate_height(tags, building.area)
            building.estimated = True
    minimum = length(tags.get("min_height"))
    if minimum is None:
        min_level = levels(tags.get("building:min_level"))
        minimum = min_level * LEVEL_HEIGHT if min_level else 0.0
    building.height = max(0.5, min(height, 1000.0))
    building.min_height = max(0.0, min(minimum, building.height - 0.5))
    shape = (tags.get("roof:shape") or "flat").lower()
    building.roof_kind = shape
    building.level_count = levels(tags.get("building:levels")) or None
    if shape in ("pyramidal", "hipped", "gabled", "half-hipped", "mansard", "gambrel", "saltbox", "side_hipped"):
        shape = "pyramidal"
    elif shape in ("dome", "onion"):
        shape = "dome"
    else:
        shape = "flat"
    building.roof_shape = shape
    width = math.sqrt(max(building.area, 1.0))
    if roof_height is None:
        roof_height = {"pyramidal": min(width * 0.35, 12.0), "dome": width * 0.5, "flat": 0.0}[shape]
    building.roof_height = max(0.0, min(roof_height, building.height - building.min_height - 0.5)) if shape != "flat" else 0.0
    if tags.get("roof:colour"):
        building.tags["roof:colour"] = tags["roof:colour"]


def _facade(building: Building, high: bool = False) -> str:
    tags = building.tags
    kind = tags.get("building") or tags.get("building:part") or "yes"
    material = (tags.get("building:material") or "").lower()
    if kind == "stadium" or tags.get("leisure") == "stadium":
        return "stadium"
    if kind == "parking":
        return "parking"
    if material in ("glass", "metal"):
        return "glass" if material == "glass" else "glass_dark"
    if material in ("brick",):
        return "brick"
    if material in ("stone", "sandstone", "limestone", "granite", "marble"):
        return "stone"
    if material in ("concrete", "plaster", "stucco"):
        return "concrete"
    if kind in ("house", "detached", "semidetached_house", "garage", "garages", "shed", "terrace"):
        return "house"
    seed = int(hashlib.sha256(building.key.encode()).hexdigest()[:8], 16)
    if building.height >= 70:
        return "glass" if seed % 3 else "glass_dark"
    if kind in ("warehouse", "industrial"):
        return "brick"
    if high and kind in ("apartments", "residential", "dormitory", "hotel"):
        return "apartments" if seed % 3 else "brick"
    if high and kind in ("office", "commercial", "government", "civic", "public", "university"):
        return "office_bands" if seed % 2 else "stone"
    return ("stone", "concrete", "brick")[seed % 3]


# --------------------------------------------------------------------- mesh --

class _Mesh:
    """Collects triangles in local metres; converted to Studio units on build."""

    def __init__(self, textures: dict[str, str]):
        self.textures = textures
        self.builder = meshmod.MeshBuilder()
        self.index: dict[tuple, int] = {}
        self.materials: dict[str, int] = {}
        self.count = 0

    def material(self, name: str, double_sided: bool = False) -> int:
        texture = self.textures[name]
        key = f"{name}|{double_sided}"
        if key not in self.materials:
            alpha = "MASK" if name in ("tree", "conifer") else "OPAQUE"
            self.materials[key] = self.builder.material({"id": f"{texture}|{key}|city", "double_sided": double_sided, "alpha_mode": alpha,
                                                         "texture": texture})
        return self.materials[key]

    def point(self, east: float, y: float, north: float) -> int:
        key = (round(east * SCALE, 2), round(y * SCALE, 2), round(-north * SCALE, 2))
        if key not in self.index:
            self.index[key] = self.builder.position(key)
        return self.index[key]

    def triangle(self, material: int, corners, tint=None) -> None:
        """corners: three ((east, y, north), (u, v)) tuples, counter-clockwise seen from the visible side."""

        indices = [self.point(*position) for position, _ in corners]
        if len(set(indices)) != 3:
            return
        colour = list(tint) if tint is not None and tuple(tint) != (1.0, 1.0, 1.0, 1.0) else None
        self.builder.triangle(material, [{"p": i, "uv": [round(uv[0], 4), round(uv[1], 4)], "colour": colour, "normal": None}
                                         for i, (_, uv) in zip(indices, corners)])
        self.count += 1

    def quad(self, material: int, corners, tint=None) -> None:
        self.triangle(material, (corners[0], corners[1], corners[2]), tint)
        self.triangle(material, (corners[0], corners[2], corners[3]), tint)

    def empty(self) -> bool:
        return self.count == 0


def _orient(ring, outer: bool) -> list:
    """Outer rings counter-clockwise in (east, north), holes clockwise: walls then face outwards."""

    area = geo.signed_area(ring)
    if (area > 0) != outer:
        return list(reversed(ring))
    return list(ring)


def _walls(mesh: _Mesh, material: int, ring, y0: float, y1: float, tint, tile: float = 8.0, tile_v: float | None = None,
           v_start: float = 0.0) -> None:
    tile_v = tile_v or tile
    travelled = 0.0
    for (ax, an), (bx, bn) in zip(ring, ring[1:] + ring[:1]):
        span = math.hypot(bx - ax, bn - an)
        if span < 1e-3:
            continue
        u0, u1 = travelled / tile, (travelled + span) / tile
        v0, v1 = -v_start / tile_v, -(v_start + y1 - y0) / tile_v
        # Seen from outside an anticlockwise (east, north) ring, a is on the left and b on the right.
        mesh.quad(material, [((ax, y0, an), (u0, v0)), ((bx, y0, bn), (u1, v0)), ((bx, y1, bn), (u1, v1)), ((ax, y1, an), (u0, v1))], tint)
        travelled += span


def _cap(mesh: _Mesh, material: int, outer, holes, y: float, tint, tile: float = 16.0, down: bool = False) -> None:
    points = list(outer) + [p for hole in holes for p in hole]
    for a, b, c in geo.triangulate(outer, holes):
        pa, pb, pc = points[a], points[b], points[c]
        if (geo.signed_area([pa, pb, pc]) < 0) != down:
            pb, pc = pc, pb
        # Anticlockwise in (east, north) seen from above is anticlockwise in Studio's (x, -z) view from +Y.
        mesh.triangle(material, [((p[0], y, p[1]), (p[0] / tile, -p[1] / tile)) for p in (pa, pb, pc)], tint)


def _ray(origin, angle: float, ring):
    """Farthest point where a ray from ``origin`` at ``angle`` crosses ``ring`` (None if it never does)."""

    dx, dy = math.cos(angle), math.sin(angle)
    best = None
    for (ax, ay), (bx, by) in zip(ring, ring[1:] + ring[:1]):
        ex, ey = bx - ax, by - ay
        denominator = dx * ey - dy * ex
        if abs(denominator) < 1e-12:
            continue
        t = ((ax - origin[0]) * ey - (ay - origin[1]) * ex) / denominator
        u = ((ax - origin[0]) * dy - (ay - origin[1]) * dx) / denominator
        if t > 0 and 0 <= u <= 1 and (best is None or t > best):
            best = t
    return None if best is None else (origin[0] + dx * best, origin[1] + dy * best)


def _field_ring(outer, share: float = 0.3) -> list:
    """The outline scaled about its centroid to ``share`` of its area (a playing field inside open stands)."""

    cx = sum(p[0] for p in outer) / len(outer)
    cn = sum(p[1] for p in outer) / len(outer)
    k = math.sqrt(share)
    return [(cx + (x - cx) * k, cn + (n - cn) * k) for x, n in outer]


def _along(line, spacing: float) -> list[tuple[float, float]]:
    out, carried = [], 0.0
    for a, b in zip(line[:-1], line[1:]):
        span = math.dist(a, b)
        distance = spacing - carried
        while distance <= span:
            t = distance / span
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
            distance += spacing
        carried = (carried + span) % spacing
    return out


def _pyramid(mesh: _Mesh, material: int, outer, y0: float, rise: float, tint) -> None:
    cx = sum(p[0] for p in outer) / len(outer)
    cn = sum(p[1] for p in outer) / len(outer)
    apex = (cx, y0 + rise, cn)
    for (ax, an), (bx, bn) in zip(outer, outer[1:] + outer[:1]):
        mesh.triangle(material, [((bx, y0, bn), (1.0, 1.0)), (apex, (0.5, 0.0)), ((ax, y0, an), (0.0, 1.0))], tint)


def _gable(mesh: _Mesh, material: int, gable_material: int, outer, y0: float, rise: float, hipped: bool, tint, wall_tint) -> bool:
    """A gabled or hipped roof over the outline's minimum rectangle (ridge along its long side); False if the outline is not near-rectangular."""

    (cx, cn), angle, half_length, half_width = geo.min_area_rectangle(outer)
    if half_length < half_width:
        half_length, half_width, angle = half_width, half_length, angle + math.pi / 2
    if half_width < 1.0 or abs(geo.signed_area(outer)) < 0.85 * 4 * half_length * half_width:
        return False
    c, s = math.cos(angle), math.sin(angle)

    def at(a, b, y):
        return (cx + a * c - b * s, y, cn + a * s + b * c)

    inset = half_width if hipped else 0.0
    ridge_a, ridge_b = at(-half_length + inset, 0, y0 + rise), at(half_length - inset, 0, y0 + rise)
    corners = [at(-half_length, -half_width, y0), at(half_length, -half_width, y0), at(half_length, half_width, y0), at(-half_length, half_width, y0)]
    # Long slopes (anticlockwise seen from above and outside).
    mesh.quad(material, [(corners[0], (0, 1)), (corners[1], (half_length / 2, 1)), (ridge_b, (half_length / 2, 0)), (ridge_a, (0, 0))], tint)
    mesh.quad(material, [(corners[2], (0, 1)), (corners[3], (half_length / 2, 1)), (ridge_a, (half_length / 2, 0)), (ridge_b, (0, 0))], tint)
    ends = ((corners[1], corners[2], ridge_b), (corners[3], corners[0], ridge_a))
    for a, b, top in ends:  # hipped: sloped ends; gabled: upright wall triangles
        mesh.triangle(material if hipped else gable_material, [(a, (0, 1)), (b, (1, 1)), (top, (0.5, 0))], tint if hipped else wall_tint)
    return True


def _dome(mesh: _Mesh, material: int, outer, y0: float, rise: float, tint, rings: int = 6) -> None:
    cx = sum(p[0] for p in outer) / len(outer)
    cn = sum(p[1] for p in outer) / len(outer)
    previous = list(outer)
    for step in range(1, rings + 1):
        angle = step / rings * math.pi / 2
        shrink = math.cos(angle)
        height = y0 + rise * math.sin(angle)
        current = [(cx + (x - cx) * shrink, cn + (n - cn) * shrink) for x, n in outer]
        last_height = y0 + rise * math.sin((step - 1) / rings * math.pi / 2)
        for i in range(len(outer)):
            j = (i + 1) % len(outer)
            a0, b0, a1, b1 = previous[i], previous[j], current[i], current[j]
            mesh.quad(material, [((a0[0], last_height, a0[1]), (0, step / rings)), ((b0[0], last_height, b0[1]), (1, step / rings)),
                                 ((b1[0], height, b1[1]), (1, (step - 1) / rings)), ((a1[0], height, a1[1]), (0, (step - 1) / rings))], tint)
        previous = current


# ------------------------------------------------------------------- builder --

class CityBuilder:
    def __init__(self, osm: OsmData, options: CityOptions, terrain: geo.Terrain | None = None):
        if not 256 <= options.half_extent_m <= 2048:
            raise ImportProblem("the map half-size must be between 256 and 2048 metres (Studio maps are at most 4 km across)")
        if options.detail not in DETAILS:
            raise ImportProblem(f"detail must be one of: {', '.join(DETAILS)}")
        if options.ground_metres_per_pixel not in (0.5, 1.0, 2.0):
            raise ImportProblem("the ground picture must use 0.5, 1 or 2 metres per pixel")
        self.osm, self.options, self.terrain = osm, options, terrain
        self.projection = geo.LocalProjection(*options.centre)
        self.half = float(options.half_extent_m)
        self.warnings: list[str] = []
        credit = OSM_CREDIT + ("; " + ELEVATION_CREDIT if terrain is not None else "")
        self.credit = credit + (f"; {options.credit}" if options.credit else "")
        self.import_options = ImportOptions(group=options.group, credit=self.credit, texture_limit=256)
        self.origin = {"file": osm.file, "sha256": osm.sha256, "format": "osm"}
        self.result = ImportResult(osm.file, osm.sha256, "osm")
        self.high = options.detail == "high"
        self.texture_ids: dict[str, str] = {}
        for name in TEXTURE_NAMES + (HIGH_TEXTURE_NAMES if self.high else ()):
            pixels = _texture_pixels(name, 256 if self.high and name in FACADES else 128)
            record = make_texture(f"city {name}", pixels.shape[1], pixels.shape[0], pixels.tobytes(), self.import_options, origin=self.origin)
            self.result.textures[record["id"]] = record
            self.texture_ids[name] = record["id"]
        self.synthetic_fields: list = []
        self.reference = 0.0
        if terrain is not None:
            centre = terrain.elevation([options.centre[0]], [options.centre[1]])[0]
            if math.isnan(centre):
                raise ImportProblem("the elevation tiles do not cover the map centre")
            self.reference = float(round(centre, 1))

    # -- helpers ---------------------------------------------------------------------
    def project(self, coords) -> list:
        array = np.asarray(coords, float)
        east, north = self.projection.forward(array[:, 0], array[:, 1])
        return list(zip(east.tolist(), north.tolist()))

    def ground(self, points) -> np.ndarray:
        """Ground elevation (metres) at (east, north) points."""

        points = np.asarray(points, float).reshape(-1, 2)
        if self.terrain is None:
            return np.full(len(points), self.reference)
        lat, lon = self.projection.inverse(points[:, 0], points[:, 1])
        values = self.terrain.elevation(lat, lon)
        return np.where(np.isnan(values), self.reference, values)

    def inside(self, point, margin: float = 0.0) -> bool:
        return abs(point[0]) <= self.half - margin and abs(point[1]) <= self.half - margin

    def model(self, label: str, mesh: _Mesh, group: str | None = None) -> str:
        built = mesh.builder.build()
        record = {"schema": "btga-neutral", "schema_version": 1, "kind": "model",
                  "source": {"platform": PLATFORM, "label": label[:48], "library": f"user:{group or self.options.group}",
                             "import": dict(self.origin, credit=self.credit)},
                  "mesh": built, "bounds": meshmod.bounds(built)}
        record["id"] = content_id("model", {"label": label[:48], "mesh": built, "importer": IMPORTER_VERSION})
        self.result.models[record["id"]] = record
        return record["id"]

    # -- buildings --------------------------------------------------------------------
    def buildings(self) -> list[Building]:
        out = []

        def add(key, tags, outer, holes, part):
            ring = geo.open_ring(self.project(outer))
            if len(ring) < 3:
                return
            area = abs(geo.signed_area(ring))
            if area < 4.0:
                return
            centroid = (sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring))
            if not self.inside(centroid, 2.0):
                return
            ring_holes = [geo.open_ring(self.project(h)) for h in holes]
            out.append(Building(key, tags, _orient(ring, True), [_orient(h, False) for h in ring_holes if len(h) >= 3], part,
                                area=area, centroid=centroid))

        for way in self.osm.ways:
            tags = way["tags"]
            if (tags.get("building") or tags.get("building:part")) and way["coords"][0] == way["coords"][-1] \
                    and tags.get("building") not in ("no", "construction_site") and tags.get("building:part") != "no":
                add(f"way/{way['id']}", tags, way["coords"], [], bool(tags.get("building:part")) and not tags.get("building"))
        for relation in self.osm.relations:
            tags = relation["tags"]
            if not (tags.get("building") or tags.get("building:part")):
                continue
            outers = geo.assemble_rings([m["coords"] for m in relation["members"] if m["role"] == "outer"])
            inners = geo.assemble_rings([m["coords"] for m in relation["members"] if m["role"] == "inner"])
            for number, outer in enumerate(outers):
                add(f"relation/{relation['id']}" + (f"#{number}" if number else ""), tags, outer,
                    [h for h in inners if geo.point_in_ring(h[0][0], h[0][1], outer)], bool(tags.get("building:part")) and not tags.get("building"))
        outlines = [b for b in out if not b.part]
        cells: dict[tuple[int, int], list[Building]] = {}
        for building in outlines:
            xs, ns = [p[0] for p in building.outer], [p[1] for p in building.outer]
            for cx in range(int(min(xs) // 64), int(max(xs) // 64) + 1):
                for cn in range(int(min(ns) // 64), int(max(ns) // 64) + 1):
                    cells.setdefault((cx, cn), []).append(building)
        for part in (b for b in out if b.part):
            for candidate in cells.get((int(part.centroid[0] // 64), int(part.centroid[1] // 64)), []):
                if geo.point_in_ring(part.centroid[0], part.centroid[1], candidate.outer):
                    part.parent = candidate
                    candidate.has_parts = True
                    break
        for building in outlines:
            _heights(building, self.options.overrides, self.options.heights)
        for building in out:
            if building.part:
                _heights(building, self.options.overrides, self.options.heights)
        pitches = []
        for way in self.osm.ways:
            if way["tags"].get("leisure") == "pitch" and way["coords"][0] == way["coords"][-1]:
                ring = geo.open_ring(self.project(way["coords"]))
                if len(ring) >= 3:
                    pitches.append(ring)
        for building in outlines:
            if (building.tags.get("building") == "stadium" or building.tags.get("leisure") == "stadium") \
                    and not building.tags.get("roof:shape") and not building.has_parts:
                inside = [r for r in pitches if geo.point_in_ring(*np.mean(r, axis=0), building.outer)]
                if inside:
                    building.bowl = max(inside, key=lambda r: abs(geo.signed_area(r)))
                elif building.tags.get("building") == "stadium":  # no field mapped: the outline drawn in, about a real field's share
                    # (an arena tagged only leisure=stadium, such as an indoor one, stays solid)
                    building.bowl = _field_ring(building.outer)
                    self.synthetic_fields.append(building.bowl)
        parts_of: dict[int, list[Building]] = {}
        for part in out:
            if part.part and part.parent is not None:
                parts_of.setdefault(id(part.parent), []).append(part)
        for building in outlines:
            parts = parts_of.get(id(building), [])
            if parts:  # the outline is as tall as its tallest part, and only estimated if every part is
                measured = [p.height for p in parts if not p.estimated]
                if building.estimated and measured:
                    building.height, building.estimated = max(measured), False
                else:
                    building.height = max([building.height] + [p.height for p in parts])
        bases = self.ground([p for b in out for p in b.outer])
        offset = 0
        for building in out:
            count = len(building.outer)
            building.base = float(np.min(bases[offset:offset + count]))
            offset += count
        for building in out:
            if building.part and building.parent is not None:
                building.base = building.parent.base
        return out

    def add_building(self, mesh: _Mesh, building: Building, origin) -> None:
        ox, on = origin
        outer = [(x - ox, n - on) for x, n in building.outer]
        holes = [[(x - ox, n - on) for x, n in hole] for hole in building.holes]
        y0 = building.base - self.reference + building.min_height
        y1 = building.base - self.reference + building.height - building.roof_height
        if y1 - y0 < 0.3:
            y1 = y0 + 0.3
        tint = colour(building.tags.get("building:colour"))
        facade = mesh.material(_facade(building, self.high))
        if building.bowl is not None:
            self._bowl(mesh, building, origin, y0, y1, facade, tint)
            return
        tile_v, v_start = None, 0.0
        if self.high:  # windows line up with the real floors: the picture holds two floors
            floors = building.level_count or max(1.0, round((building.height - building.roof_height) / LEVEL_HEIGHT))
            floor = min(6.0, max(2.6, (building.height - building.roof_height) / floors))
            tile_v, v_start = 2 * floor, building.min_height
        _walls(mesh, facade, outer, y0, y1, tint, tile_v=tile_v, v_start=v_start)
        for hole in holes:
            _walls(mesh, facade, hole, y0, y1, tint, tile_v=tile_v, v_start=v_start)
        roof_tint = colour(building.tags.get("roof:colour"))
        if self.high and building.roof_kind in ("gabled", "hipped", "side_hipped", "half-hipped") and building.roof_height > 0 and not holes \
                and _gable(mesh, mesh.material("roof_tiles"), facade, outer, y1, building.roof_height, building.roof_kind != "gabled", roof_tint, tint):
            pass
        elif building.roof_shape == "dome" and building.roof_height > 0:
            _cap(mesh, mesh.material("roof"), outer, holes, y1, roof_tint)
            _dome(mesh, mesh.material("dome"), outer, y1, building.roof_height, roof_tint)
        elif building.roof_shape == "pyramidal" and building.roof_height > 0 and not holes:
            _pyramid(mesh, mesh.material("roof_tiles"), outer, y1, building.roof_height, roof_tint)
        else:
            _cap(mesh, mesh.material("roof"), outer, holes, y1, roof_tint)
            if self.high and building.height - building.min_height >= 6.0 and building.roof_shape == "flat":  # a parapet round the roof
                _walls(mesh, facade, outer, y1, y1 + 0.9, tint, tile=8.0)
                _walls(mesh, mesh.material("roof"), list(reversed(outer)), y1, y1 + 0.9, roof_tint, tile=8.0)
        if building.min_height > 0.5:  # floating parts (bridging floors, overhangs) get an underside
            _cap(mesh, facade, outer, holes, y0, tint, down=True)

    def _box(self, building: Building, points, height: float, shrink: float = 1.0) -> dict | None:
        """Collision box: the minimum-area rectangle around ``points``, pulled in 0.25 m so neighbours sharing a wall do not overlap."""

        (cx, cn), angle, half_length, half_width = geo.min_area_rectangle(points)
        half_length, half_width = half_length * shrink - 0.25, half_width * shrink - 0.25
        if half_length < 0.5 or half_width < 0.5:
            return None
        c, s = math.cos(angle), math.sin(angle)
        corners = [(cx + a * c - b * s, cn + a * s + b * c) for a in (-half_length, half_length) for b in (-half_width, half_width)]
        if not all(self.inside(corner) for corner in corners):
            return None  # a building cut by the map edge stays visible but is not solid beyond the limits
        # Studio yaw turns local +X towards -Z, so local x runs along the rectangle's angle in (east, north).
        yaw = int(round(angle / (2 * math.pi) * 65536)) & 0xFFFF
        return {"shape": "box", "translation": [round(cx * SCALE, 2), round((building.base - self.reference) * SCALE, 2), round(-cn * SCALE, 2)],
                "yaw": yaw, "min": [round(-half_length * SCALE, 2), 0.0, round(-half_width * SCALE, 2)],
                "max": [round(half_length * SCALE, 2), round(height * SCALE, 2), round(half_width * SCALE, 2)],
                "vertical": {"status": "proven", "evidence": "Studio box from OpenStreetMap building outline"},
                "confidence": "proven", "category": "collision", "allow_overlap": True, "source": {"user_created": True, "osm": building.key}}

    def _bowl(self, mesh: _Mesh, building: Building, origin, y0: float, y1: float, facade: int, tint) -> None:
        """Open-air stadium: outer walls, stands sloping down to the field, a low wall around the field."""

        ox, on = origin
        outer = [(x - ox, n - on) for x, n in building.outer]
        field = [(x - ox, n - on) for x, n in building.bowl]
        centre = (sum(p[0] for p in field) / len(field), sum(p[1] for p in field) / len(field))
        _walls(mesh, facade, outer, y0, y1, tint)
        samples = 72
        outer_ring = [_ray(centre, k / samples * 2 * math.pi, outer) for k in range(samples)]
        inner_ring = [_ray(centre, k / samples * 2 * math.pi, field) for k in range(samples)]
        seats = mesh.material("stadium")
        low = y0 + 3.0
        for k in range(samples):
            j = (k + 1) % samples
            a_out, b_out, a_in, b_in = outer_ring[k], outer_ring[j], inner_ring[k], inner_ring[j]
            if None in (a_out, b_out, a_in, b_in):
                continue
            # Anticlockwise seen from above: inner a, outer a, outer b, inner b.
            mesh.quad(seats, [((a_in[0], low, a_in[1]), (0, 1)), ((a_out[0], y1, a_out[1]), (0, 0)),
                              ((b_out[0], y1, b_out[1]), (1, 0)), ((b_in[0], low, b_in[1]), (1, 1))], tint)
            mesh.quad(facade, [((b_in[0], y0, b_in[1]), (1, 0.4)), ((a_in[0], y0, a_in[1]), (0, 0.4)),
                               ((a_in[0], low, a_in[1]), (0, 0)), ((b_in[0], low, b_in[1]), (1, 0))], tint)

    # -- ground -----------------------------------------------------------------------
    def paint(self) -> geo.Canvas:
        size = int(round(2 * self.half / self.options.ground_metres_per_pixel))
        canvas = geo.Canvas(-self.half, self.half, size, size, self.options.ground_metres_per_pixel, GROUND["paved"])
        noise = np.random.default_rng(5).normal(0, 5, (97, 101)).astype(np.int16)

        def areas(kind_of):
            items = []
            for way in self.osm.ways:
                kind = kind_of(way["tags"])
                if kind and way["coords"][0] == way["coords"][-1]:
                    items.append((kind, [self.project(way["coords"])], way["tags"]))
            for relation in self.osm.relations:
                kind = kind_of(relation["tags"])
                if kind:
                    rings = geo.assemble_rings([m["coords"] for m in relation["members"]])
                    if rings:
                        items.append((kind, [self.project(r) for r in rings], relation["tags"]))
            return items

        layers = [
            lambda t: LANDUSE.get(t.get("landuse")),
            lambda t: "parking" if t.get("amenity") == "parking" and t.get("parking") not in ("multi-storey", "underground") else None,
            lambda t: LEISURE.get(t.get("leisure")),
            lambda t: NATURAL.get(t.get("natural")) if t.get("natural") != "water" else None,
            lambda t: "water" if t.get("natural") == "water" or t.get("waterway") == "riverbank" or t.get("landuse") in ("reservoir", "basin") else None,
        ]
        for layer in layers:
            for kind, rings, _ in sorted(areas(layer), key=lambda item: -abs(geo.signed_area(item[1][0]))):
                canvas.fill(rings, GROUND[kind], noise)
        for way in self.osm.ways:
            tags = way["tags"]
            if tags.get("waterway") in ("river", "stream", "canal") and tags.get("tunnel") is None and way["coords"][0] != way["coords"][-1]:
                canvas.line(self.project(way["coords"]), length(tags.get("width")) or (30.0 if tags["waterway"] == "river" else 4.0), GROUND["water"])
        for way in self.osm.ways:
            tags = way["tags"]
            if tags.get("railway") in ("rail", "light_rail", "tram") and not tags.get("bridge") and not tags.get("tunnel"):
                points = self.project(way["coords"])
                canvas.line(points, 4.0, GROUND["rail"])
                canvas.line(points, 1.0, GROUND["rail_steel"])
        ordered = sorted(self.osm.ways, key=lambda w: (w["tags"].get("highway") in ROAD_WIDTH, ROAD_WIDTH.get(w["tags"].get("highway"), 0)))
        for way in ordered:
            tags = way["tags"]
            highway = tags.get("highway")
            if not highway or tags.get("bridge") or tags.get("tunnel") or tags.get("indoor") == "yes" or tags.get("area") == "yes":
                continue
            if tags.get("layer", "0").lstrip("-").isdigit() and int(tags.get("layer", "0")) < 0:
                continue
            points = self.project(way["coords"])
            if highway in ROAD_WIDTH:
                width = length(tags.get("width")) or ROAD_WIDTH[highway]
                if self.high and highway not in ("pedestrian", "track", "service"):
                    canvas.line(points, width + 0.8, GROUND["curb"])  # a curb either side
                canvas.line(points, width, GROUND["plaza"] if highway == "pedestrian" else GROUND["road"])
                if highway in CENTRE_LINE and tags.get("oneway") not in ("yes", "1"):
                    canvas.line(points, 0.4, GROUND["line_yellow"], dash=(3.0, 6.0))
                elif highway in ("motorway", "trunk") or tags.get("oneway") in ("yes", "1") and highway in CENTRE_LINE:
                    canvas.line(points, 0.4, GROUND["line_white"], dash=(3.0, 9.0))
            elif highway in PATH_WIDTH:
                if self.high and tags.get("footway") == "crossing":
                    canvas.line(points, 3.0, GROUND["zebra"], dash=(0.6, 0.6))  # zebra stripes run along the road
                    continue
                canvas.line(points, length(tags.get("width")) or PATH_WIDTH[highway], GROUND["path"])
        if self.high:
            self._crossings(canvas)
        open_air = lambda t: t.get("building") == "stadium" and not t.get("roof:shape")  # noqa: E731
        for kind, rings, _ in areas(lambda t: "building" if t.get("building") and t.get("building") != "roof" and not open_air(t) else None):
            canvas.fill(rings, GROUND["building"])
        for ring in getattr(self, "synthetic_fields", []):
            canvas.fill([ring], GROUND["pitch"], noise)
        return canvas

    def _crossings(self, canvas: geo.Canvas) -> None:
        """Zebra crossings painted across the road at marked crossing points."""

        roads = {}
        for way in self.osm.ways:
            highway = way["tags"].get("highway")
            if highway in ROAD_WIDTH and highway not in ("pedestrian", "track") and not way["tags"].get("bridge"):
                width = length(way["tags"].get("width")) or ROAD_WIDTH[highway]
                coords = way["coords"]
                for k, point in enumerate(coords):
                    a, b = coords[max(0, k - 1)], coords[min(len(coords) - 1, k + 1)]
                    if a != b:
                        roads.setdefault(tuple(point), (a, b, width))
        for node in self.osm.nodes:
            tags = node["tags"]
            if tags.get("highway") != "crossing" or tags.get("crossing") in ("unmarked", "no", "informal"):
                continue
            road = roads.get((node["lat"], node["lon"]))
            if road is None:
                continue
            (ea, na), (eb, nb), (ec, nc) = self.project([road[0], road[1], (node["lat"], node["lon"])])
            span = math.hypot(eb - ea, nb - na) or 1.0
            across = (-(nb - na) / span, (eb - ea) / span)
            half = road[2] / 2
            canvas.line([(ec - across[0] * half, nc - across[1] * half), (ec + across[0] * half, nc + across[1] * half)], 3.0, GROUND["zebra"],
                        dash=(0.6, 0.6))

    def ground_chunks(self, canvas: geo.Canvas) -> list[tuple[str, tuple]]:
        out = []
        chunk, spacing = self.options.chunk_m, self.options.terrain_spacing_m
        count = int(round(2 * self.half / chunk))
        steps = int(round(chunk / spacing))
        for row in range(count):
            for column in range(count):
                west, north = -self.half + column * chunk, self.half - row * chunk
                origin = (west + chunk / 2, north - chunk / 2)
                pixels = canvas.crop(west, north, chunk)
                radius = self.options.fine_ground_radius_m
                if radius is not None and canvas.mpp < 1.0:
                    centre = self.options.fine_ground_centre
                    if math.hypot(origin[0] - centre[0], origin[1] - centre[1]) > radius:  # outside the fine core: 1 m per pixel
                        factor = int(round(1.0 / canvas.mpp))
                        h, w = pixels.shape[0] // factor, pixels.shape[1] // factor
                        pixels = pixels[:h * factor, :w * factor].reshape(h, factor, w, factor, 3).mean(axis=(1, 3)).round().astype(np.uint8)
                rgba = np.dstack([pixels, np.full(pixels.shape[:2], 255, dtype=np.uint8)])
                texture = make_texture(f"ground {row} {column}", rgba.shape[1], rgba.shape[0], rgba.tobytes(),
                                       ImportOptions(group=self.options.group, credit=self.credit, texture_limit=512 if chunk <= 512 else 1024),
                                       wrap="clamp", origin=self.origin)
                self.result.textures[texture["id"]] = texture
                easts = west + np.arange(steps + 1) * spacing
                norths = north - np.arange(steps + 1) * spacing
                grid_e, grid_n = np.meshgrid(easts, norths)
                heights = (self.ground(np.stack([grid_e.ravel(), grid_n.ravel()], axis=1)) - self.reference).reshape(grid_e.shape)
                mesh = _Mesh({**self.texture_ids, "ground": texture["id"]})
                material = mesh.material("ground")
                for i in range(steps):
                    for j in range(steps):
                        corners = []
                        for di, dj in ((1, 0), (1, 1), (0, 1), (0, 0)):  # south-west, south-east, north-east, north-west
                            e, n = grid_e[i + di, j + dj], grid_n[i + di, j + dj]
                            corners.append(((e - origin[0], heights[i + di, j + dj], n - origin[1]), ((j + dj) / steps, (i + di) / steps)))
                        mesh.quad(material, corners)
                model = self.model(f"ground {row:02d}-{column:02d}", mesh, f"{self.options.group} ground")
                out.append((model, origin))
        return out

    # -- bridges, skyways, trees ------------------------------------------------------------
    def _deck(self, mesh: _Mesh, points, width: float, y_start: float, y_end: float, origin, material: str, rail: float = 1.0,
              piers: bool = True) -> None:
        ox, on = origin
        lengths = [0.0] + list(np.cumsum([math.dist(a, b) for a, b in zip(points[:-1], points[1:])]))
        total = lengths[-1] or 1.0
        deck = mesh.material(material)
        side = mesh.material("bridge_stone" if material == "bridge_stone" else "concrete")
        ground = self.ground(points) - self.reference
        for (a, b), la, lb in zip(zip(points[:-1], points[1:]), lengths[:-1], lengths[1:]):
            span = math.dist(a, b)
            if span < 0.05:
                continue
            dx, dn = (b[0] - a[0]) / span, (b[1] - a[1]) / span
            px, pn = -dn * width / 2, dx * width / 2
            ya = y_start + (y_end - y_start) * la / total
            yb = y_start + (y_end - y_start) * lb / total
            l0, r0 = (a[0] + px - ox, a[1] + pn - on), (a[0] - px - ox, a[1] - pn - on)
            l1, r1 = (b[0] + px - ox, b[1] + pn - on), (b[0] - px - ox, b[1] - pn - on)
            mesh.quad(deck, [((r0[0], ya, r0[1]), (1, la / 8)), ((r1[0], yb, r1[1]), (1, lb / 8)), ((l1[0], yb, l1[1]), (0, lb / 8)),
                             ((l0[0], ya, l0[1]), (0, la / 8))])
            for edge0, edge1 in ((l0, l1), (r1, r0)):
                y_a, y_b = (ya, yb) if edge0 is l0 else (yb, ya)
                mesh.quad(side, [((edge1[0], y_b - 1.2, edge1[1]), (span / 8, 0.2)), ((edge0[0], y_a - 1.2, edge0[1]), (0, 0.2)),
                                 ((edge0[0], y_a + rail, edge0[1]), (0, -rail / 8)), ((edge1[0], y_b + rail, edge1[1]), (span / 8, -rail / 8))])
        if not piers:
            return
        spacing = 30.0 if material == "bridge_stone" else 40.0
        distance = spacing / 2
        while distance < total - spacing / 4:
            k = max(0, int(np.searchsorted(lengths, distance)) - 1)
            a, b = points[k], points[min(k + 1, len(points) - 1)]
            t = (distance - lengths[k]) / max(1e-6, lengths[k + 1] - lengths[k]) if k + 1 < len(lengths) else 0
            centre = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
            top = y_start + (y_end - y_start) * distance / total - 1.2
            bottom = float(self.ground([centre])[0] - self.reference) - 1.0
            if top - bottom > 3.0:
                half_w = width / 2 * 0.8
                dx, dn = (b[0] - a[0]), (b[1] - a[1])
                norm = math.hypot(dx, dn) or 1.0
                dx, dn = dx / norm, dn / norm
                corners = [(centre[0] + sx * dn * half_w + sl * dx * 2.0 - ox, centre[1] - sx * dx * half_w + sl * dn * 2.0 - on)
                           for sx, sl in ((1, -1), (1, 1), (-1, 1), (-1, -1))]
                _walls(mesh, side, _orient(corners, True), bottom, top, None)
            distance += spacing

    def bridges(self) -> tuple[list[tuple[str, tuple]], int, int]:
        """Road, rail and path bridges plus skyways, merged per chunk."""

        chunks: dict[tuple[int, int], _Mesh] = {}
        bridges = skyways = 0
        outlines = [b for b in self._buildings if not b.part]
        cells: dict[tuple[int, int], list[Building]] = {}
        for building in outlines:
            xs, ns = [p[0] for p in building.outer], [p[1] for p in building.outer]
            for cx in range(int(min(xs) // 64), int(max(xs) // 64) + 1):
                for cn in range(int(min(ns) // 64), int(max(ns) // 64) + 1):
                    cells.setdefault((cx, cn), []).append(building)

        def in_building(point):
            return any(geo.point_in_ring(point[0], point[1], b.outer) for b in cells.get((int(point[0] // 64), int(point[1] // 64)), []))

        for way in self.osm.ways:
            tags = way["tags"]
            highway, railway = tags.get("highway"), tags.get("railway")
            if not (highway or railway in ("rail", "light_rail", "tram")):
                continue
            points = geo.open_ring(self.project(way["coords"])) if way["coords"][0] == way["coords"][-1] else self.project(way["coords"])
            if len(points) < 2 or not all(self.inside(p) for p in (points[0], points[-1])):
                continue
            footway = highway in ("footway", "corridor", "steps", "path", "cycleway")
            covered = tags.get("covered") == "yes" or tags.get("bridge") == "covered" or tags.get("indoor") == "yes" or \
                "skyway" in (tags.get("name") or "").lower()
            is_bridge = tags.get("bridge") not in (None, "no")
            if footway and covered and (is_bridge or (in_building(points[0]) and in_building(points[-1]))):
                middles = [((a[0] + b[0]) / 2, (a[1] + b[1]) / 2) for a, b in zip(points[:-1], points[1:])]
                if all(in_building(p) for p in middles):
                    continue  # an indoor corridor: it never leaves the buildings
                skyways += 1
                self._skyway(chunks, points)
                continue
            if not is_bridge:
                continue
            ends = self.ground([points[0], points[-1]]) - self.reference
            width = length(tags.get("width")) or (ROAD_WIDTH.get(highway) or (5.0 if railway else max(6.0, PATH_WIDTH.get(highway, 3.0))))
            material = "bridge_stone" if tags.get("bridge:structure") == "arch" or "stone arch" in (tags.get("name") or "").lower() else "deck"
            key = self._chunk_key(points[len(points) // 2])
            mesh = chunks.setdefault(key, _Mesh(self.texture_ids))
            self._deck(mesh, points, width, float(ends[0]) + 0.15, float(ends[1]) + 0.15, self._chunk_origin(key), material)
            bridges += 1
        out = []
        for key, mesh in sorted(chunks.items()):
            if not mesh.empty():
                out.append((self.model(f"bridges {key[0]:02d}-{key[1]:02d}", mesh), self._chunk_origin(key)))
        return out, bridges, skyways

    def _skyway(self, chunks, points) -> None:
        key = self._chunk_key(points[len(points) // 2])
        mesh = chunks.setdefault(key, _Mesh(self.texture_ids))
        ox, on = self._chunk_origin(key)
        floor = float(np.max(self.ground(points)) - self.reference) + 5.5
        glass, roof = mesh.material("skyway", True), mesh.material("roof")
        for a, b in zip(points[:-1], points[1:]):
            span = math.dist(a, b)
            if span < 0.2:
                continue
            dx, dn = (b[0] - a[0]) / span, (b[1] - a[1]) / span
            px, pn = -dn * 2.2, dx * 2.2
            ring = [(a[0] - px - ox, a[1] - pn - on), (b[0] - px - ox, b[1] - pn - on), (b[0] + px - ox, b[1] + pn - on), (a[0] + px - ox, a[1] + pn - on)]
            ring = _orient(ring, True)
            _walls(mesh, glass, ring, floor, floor + 3.6, None, tile=4.0)
            _cap(mesh, roof, ring, [], floor + 3.6, None)
            _cap(mesh, roof, ring, [], floor, None, down=True)

    def trees(self) -> list[tuple[str, tuple]]:
        points = []
        for node in self.osm.nodes:
            if node["tags"].get("natural") == "tree":
                e, n = self.projection.forward(node["lat"], node["lon"])
                points.append((float(e), float(n), node["tags"].get("leaf_type") == "needleleaved", node["id"]))
        rng = np.random.default_rng(11)
        for way in self.osm.ways:
            if way["tags"].get("natural") in ("wood", "scrub") or way["tags"].get("landuse") == "forest":
                ring = geo.open_ring(self.project(way["coords"]))
                if len(ring) < 3:
                    continue
                area = abs(geo.signed_area(ring))
                xs, ns = [p[0] for p in ring], [p[1] for p in ring]
                for k in range(min(400, int(area / 180))):
                    e, n = rng.uniform(min(xs), max(xs)), rng.uniform(min(ns), max(ns))
                    if geo.point_in_ring(e, n, ring):
                        points.append((e, n, False, way["id"] * 1000 + k))
        if self.high:
            for way in self.osm.ways:
                tags = way["tags"]
                if tags.get("natural") == "tree_row":  # a tree every 9 m along the row
                    for k, (e, n) in enumerate(_along(self.project(way["coords"]), 9.0)):
                        points.append((e, n, False, way["id"] * 1000 + k))
            mapped = [(p[0], p[1]) for p in points]
            cells = {(int(e // 40), int(n // 40)) for e, n in mapped}
            for way in self.osm.ways:
                if way["tags"].get("leisure") in ("park", "garden") and way["coords"][0] == way["coords"][-1]:
                    ring = geo.open_ring(self.project(way["coords"]))
                    if len(ring) < 3:
                        continue
                    xs, ns = [p[0] for p in ring], [p[1] for p in ring]
                    area = abs(geo.signed_area(ring))
                    for k in range(min(300, int(area / 300))):  # parks without mapped trees get some (about one per 300 m2)
                        e, n = rng.uniform(min(xs), max(xs)), rng.uniform(min(ns), max(ns))
                        if (int(e // 40), int(n // 40)) not in cells and geo.point_in_ring(e, n, ring):
                            points.append((e, n, k % 7 == 0, way["id"] * 1000 + 500 + k))
        chunks: dict[tuple[int, int], _Mesh] = {}
        points = [p for p in points if self.inside(p[:2], 4.0)]
        grounds = self.ground([p[:2] for p in points]) - self.reference if points else []
        for (e, n, conifer, identifier), ground in zip(points, grounds):
            key = self._chunk_key((e, n))
            mesh = chunks.setdefault(key, _Mesh(self.texture_ids))
            ox, on = self._chunk_origin(key)
            seed = int(hashlib.sha256(str(identifier).encode()).hexdigest()[:8], 16)
            height = 9.0 + (seed % 700) / 100.0
            half = height * (0.32 if conifer else 0.42)
            material = mesh.material("conifer" if conifer else "tree", True)
            angle = (seed % 360) * math.pi / 180
            for turn in (0.0, math.pi / 2):
                c, s = math.cos(angle + turn) * half, math.sin(angle + turn) * half
                mesh.quad(material, [((e - c - ox, ground, n - s - on), (0, 1)), ((e + c - ox, ground, n + s - on), (1, 1)),
                                     ((e + c - ox, ground + height, n + s - on), (1, 0)), ((e - c - ox, ground + height, n - s - on), (0, 0))])
        out = []
        for key, mesh in sorted(chunks.items()):
            if not mesh.empty():
                out.append((self.model(f"trees {key[0]:02d}-{key[1]:02d}", mesh, f"{self.options.group} trees"), self._chunk_origin(key)))
        self.tree_count = len(points)
        return out

    def furniture(self) -> tuple[list[tuple[str, tuple]], dict]:
        """Street lamps, traffic signals, walls, fences and hedges from the map data (high detail)."""

        chunks: dict[tuple[int, int], _Mesh] = {}
        counts = {"lamps": 0, "signals": 0, "barriers": 0}

        def mesh_at(point):
            key = self._chunk_key(point)
            return chunks.setdefault(key, _Mesh(self.texture_ids)), self._chunk_origin(key)

        def post(mesh, origin, e, n, ground, height, radius, material):
            ox, on = origin
            ring = [(e - radius - ox, n - radius - on), (e + radius - ox, n - radius - on), (e + radius - ox, n + radius - on), (e - radius - ox, n + radius - on)]
            _walls(mesh, material, ring, ground, ground + height, None, tile=4.0)
            return ring

        points = []
        for node in self.osm.nodes:
            kind = node["tags"].get("highway") or node["tags"].get("man_made")
            if kind in ("street_lamp", "traffic_signals"):
                e, n = self.projection.forward(node["lat"], node["lon"])
                if self.inside((float(e), float(n)), 2.0):
                    points.append((kind, float(e), float(n)))
        grounds = self.ground([p[1:] for p in points]) - self.reference if points else []
        for (kind, e, n), ground in zip(points, grounds):
            mesh, origin = mesh_at((e, n))
            metal = mesh.material("metal")
            if kind == "street_lamp":
                post(mesh, origin, e, n, ground, 8.0, 0.15, metal)
                head = post(mesh, origin, e, n, ground + 7.6, 0.5, 0.45, mesh.material("lamp"))
                _cap(mesh, mesh.material("lamp"), head, [], ground + 7.6, None, down=True)
                counts["lamps"] += 1
            else:
                post(mesh, origin, e, n, ground, 5.0, 0.15, metal)
                box = post(mesh, origin, e, n, ground + 4.0, 1.2, 0.35, mesh.material("signal"))
                _cap(mesh, metal, box, [], ground + 5.2, None)
                counts["signals"] += 1
        styles = {"wall": (2.0, "concrete"), "city_wall": (3.0, "stone"), "retaining_wall": (1.5, "concrete"), "fence": (1.6, "fence"),
                  "hedge": (1.4, "hedge")}
        for way in self.osm.ways:
            barrier = (way["tags"].get("barrier") or "").split(";")[0]
            if barrier not in styles:
                continue
            points = [p for p in self.project(way["coords"]) if self.inside(p, 1.0)]
            if len(points) < 2:
                continue
            height, texture = styles[barrier]
            height = length(way["tags"].get("height")) or height
            ground = self.ground(points) - self.reference
            for (a, b), ga, gb in zip(zip(points[:-1], points[1:]), ground[:-1], ground[1:]):
                if math.dist(a, b) < 0.1:
                    continue
                mesh, (ox, on) = mesh_at(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2))
                material = mesh.material(texture, True)
                span = math.dist(a, b)
                mesh.quad(material, [((a[0] - ox, ga, a[1] - on), (0, 1)), ((b[0] - ox, gb, b[1] - on), (span / 4, 1)),
                                     ((b[0] - ox, gb + height, b[1] - on), (span / 4, 1 - height / 4)),
                                     ((a[0] - ox, ga + height, a[1] - on), (0, 1 - height / 4))])
            counts["barriers"] += 1
        out = []
        for key, mesh in sorted(chunks.items()):
            if not mesh.empty():
                out.append((self.model(f"street {key[0]:02d}-{key[1]:02d}", mesh, f"{self.options.group} street"), self._chunk_origin(key)))
        return out, counts

    def _chunk_key(self, point) -> tuple[int, int]:
        chunk = self.options.chunk_m
        count = int(round(2 * self.half / chunk))
        column = min(count - 1, max(0, int((point[0] + self.half) // chunk)))
        row = min(count - 1, max(0, int((self.half - point[1]) // chunk)))
        return row, column

    def _chunk_origin(self, key) -> tuple[float, float]:
        chunk = self.options.chunk_m
        return -self.half + (key[1] + 0.5) * chunk, self.half - (key[0] + 0.5) * chunk

    # -- everything ---------------------------------------------------------------------
    def build(self, progress=None) -> CityPlan:
        def step(text):
            if progress:
                progress(text)

        instances, collision, landmarks = [], [], {}
        step("buildings")
        self._buildings = buildings = self.buildings()
        drawn = [b for b in buildings if not b.has_parts]
        domed = {id(p.parent) for p in buildings if p.parent is not None and p.roof_shape == "dome"}
        groups: dict[str, list[Building]] = {}
        for building in drawn:
            owner = building.parent or building
            name = owner.tags.get("name")
            tall = max(owner.height, building.height + building.min_height)
            special = owner.tags.get("leisure") == "stadium" or owner.tags.get("building") in ("stadium", "cathedral", "basilica") \
                or owner.roof_shape == "dome" or id(owner) in domed
            if name and (tall >= self.options.landmark_height_m or special):
                groups.setdefault(f"{name}|{owner.key}", []).append(building)
            else:
                groups.setdefault(f"chunk|{self._chunk_key(building.centroid)}", []).append(building)
        for key, members in sorted(groups.items()):
            if key.startswith("chunk|"):
                origin = self._chunk_origin(self._chunk_key(members[0].centroid))
                label = f"buildings {key[6:].strip('()').replace(', ', '-')}"
            else:
                xs = [p[0] for b in members for p in b.outer]
                ns = [p[1] for b in members for p in b.outer]
                origin = ((min(xs) + max(xs)) / 2, (min(ns) + max(ns)) / 2)
                label = key.split("|", 1)[0]
            mesh = _Mesh(self.texture_ids)
            for building in members:
                self.add_building(mesh, building, origin)
            if mesh.empty():
                continue
            model = self.model(label, mesh, None if key.startswith("chunk|") else f"{self.options.group} landmarks")
            instances.append({"model": model, "translation": [origin[0] * SCALE, 0.0, -origin[1] * SCALE], "yaw": 0, "label": label})
            if not key.startswith("chunk|"):
                owner = members[0].parent or members[0]
                top = max(b.base - self.reference + b.height for b in members)
                landmarks[label] = {"model": model, "centre": (owner.centroid[0] * SCALE, (owner.base - self.reference) * SCALE, -owner.centroid[1] * SCALE),
                                    "top": top * SCALE, "height_m": round(max(b.height for b in members), 1), "key": owner.key}
        if self.options.collision:
            step("collision")
            for building in buildings:
                if building.part or building.height < 2.5 or building.area < 12.0:
                    continue
                if building.bowl is not None:
                    field_centre = tuple(np.mean(building.bowl, axis=0))
                    sectors = 24
                    for k in range(sectors):
                        angles = (k / sectors * 2 * math.pi, (k + 1) / sectors * 2 * math.pi)
                        corners = [_ray(field_centre, t, ring) for t in angles for ring in (building.outer, building.bowl)]
                        if None not in corners:
                            collision.append(self._box(building, corners, building.height, shrink=0.9))
                    continue
                collision.append(self._box(building, building.outer, building.height))
            collision = [c for c in collision if c is not None]
        step("ground")
        canvas = self.paint()
        for model, origin in self.ground_chunks(canvas):
            instances.append({"model": model, "translation": [origin[0] * SCALE, 0.0, -origin[1] * SCALE], "yaw": 0, "label": "ground"})
        step("bridges and skyways")
        bridge_models, bridges, skyways = self.bridges()
        for model, origin in bridge_models:
            instances.append({"model": model, "translation": [origin[0] * SCALE, 0.0, -origin[1] * SCALE], "yaw": 0, "label": "bridges"})
        street_counts = {}
        if self.high:
            step("street furniture")
            street_models, street_counts = self.furniture()
            for model, origin in street_models:
                instances.append({"model": model, "translation": [origin[0] * SCALE, 0.0, -origin[1] * SCALE], "yaw": 0, "label": "street"})
        self.tree_count = 0
        if self.options.trees:
            step("trees")
            for model, origin in self.trees():
                instances.append({"model": model, "translation": [origin[0] * SCALE, 0.0, -origin[1] * SCALE], "yaw": 0, "label": "trees"})
        estimated = sum(1 for b in buildings if b.estimated and not b.part)
        counts = {"buildings": sum(1 for b in buildings if not b.part), "parts": sum(1 for b in buildings if b.part), "bridges": bridges,
                  "skyways": skyways, "trees": self.tree_count, "models": len(self.result.models), "textures": len(self.result.textures),
                  "triangles": self.result.triangles, "collision": len(collision), **street_counts, "detail": self.options.detail}
        if estimated:
            self.warnings.append(f"{estimated} of {counts['buildings']} buildings have no height or level count in the map data; "
                                 "their heights are estimated from building type and size")
        self.result.warnings = self.warnings
        return CityPlan(self.result, instances, collision, self.half * SCALE, self.reference, landmarks, counts, estimated, self.credit)


WIKIDATA_UNITS = {"Q11573": 1.0, "Q3710": 0.3048, "Q218593": 0.0254, "Q828224": 1000.0}


def heights_from_wikidata(document: dict) -> dict:
    """Fill-in heights from a Wikidata query result (SPARQL JSON with ?item ?itemLabel ?height ?heightUnit ?floors).

    Keys are ``wikidata:Q..`` and the English label; floors win when a height
    disagrees with them (more than 8 m per floor), which catches unit mistakes.
    """

    out: dict[str, dict] = {}
    for row in (document.get("results") or {}).get("bindings", []):
        qid = row.get("item", {}).get("value", "").rsplit("/", 1)[-1]
        if not re.fullmatch(r"Q\d+", qid):
            continue
        entry = {"source": f"Wikidata {qid}"}
        floors = levels(row.get("floors", {}).get("value"))
        if floors:
            entry["building:levels"] = floors
        if "height" in row:
            unit = WIKIDATA_UNITS.get(row.get("heightUnit", {}).get("value", "").rsplit("/", 1)[-1])
            try:
                metres = float(row["height"]["value"]) * unit if unit else None
            except ValueError:
                metres = None
            if metres and 0 < metres <= 1000 and not (floors and metres > floors * 8):
                entry["height"] = round(metres, 2)
        if len(entry) > 1:
            out[f"wikidata:{qid}"] = entry
            label = row.get("itemLabel", {}).get("value")
            if label and not re.fullmatch(r"Q\d+", label):
                out.setdefault(label, entry)
    return out


def build_city(osm: OsmData, options: CityOptions, terrain: geo.Terrain | None = None, progress=None) -> CityPlan:
    return CityBuilder(osm, options, terrain).build(progress)


def apply_plan(document, plan: CityPlan) -> list[str]:
    """Place a city on an open map in one undoable step; returns the new instance IDs in plan order."""

    edits = [{"op": "add_instance", "instance": {"model": item["model"], "translation": [float(v) for v in item["translation"]],
                                                  "yaw": int(item["yaw"]) & 0xFFFF, "category": "model", "source": {"user_created": True}}}
             for item in plan.instances]
    edits += [{"op": "add_collision", "collision": shape} for shape in plan.collision]
    transaction = document._commit("Import city", edits, set())
    return [edit["instance"]["id"] for edit in transaction.edits if edit["op"] == "add_instance"]
