"""Build the "Minneapolis" multiplayer map with BattleTanx Studio and capture it off-screen.

Real data, true scale (1 m = 16 units), map north = -Z:

* OpenStreetMap (ODbL) for buildings, building parts, roads, rail, water,
  parks, bridges, skyways and trees; Wikidata (CC0) floor counts and heights
  where OpenStreetMap has none;
* Terrain Tiles (USGS 3DEP) for the ground and the river valley;
* a sky panorama computed from the real horizon: terrain to 45 km and the
  tall buildings, domes and masts beyond the map at their true bearings
  (Saint Paul lies about 104 degrees east-south-east).

Everything goes through Studio's own importers (``core.imports`` for the
teleport ramp OBJ and the sky picture, ``core.osm`` for the city), exactly as
the Import window does.  Gameplay: four team starts and player spawns at real
landmarks, pickups, navigation along real streets, and a teleporter on a ramp
by the IDS Center on Nicollet Mall that lifts a tank to the roof, where a
weapon pickup and a return teleporter wait.  Every multiplayer mode is enabled.

    python tools/build_minneapolis_map.py SOURCES OUTPUT [--variant full-standard|full-high|shrunk-high|shrunk-standard] [--download] [--no-capture]

SOURCES holds ``city.json`` and ``skyline.json`` (Overpass ``out geom``
results), ``wikidata_area.json`` / ``wikidata_metro.json`` (Wikidata query
results) and ``terrarium/z/x/y.png``; ``--download`` fetches them (one
Overpass query each, Terrarium tiles at zoom 15 and 10, two Wikidata queries
one minute apart).  OUTPUT receives the project, the content package, the
kit files, screenshots with JSON records and a build report.  Nothing here is
game data; the downloaded data stays in the folders you name.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CENTRE = (44.97793, -93.26941)  # map centre: between the IDS Center and the Mississippi
BOX = (44.9595, -93.2954, 44.9964, -93.2434)  # south, west, north, east of the 4 km map
METRO = (44.70, -93.75, 45.25, -92.80)
HALF_M = 2048.0
# The four builds: the full 4 km map and a game-sized 2.3 km map (2,304 m: nine 256 m ground chunks) (IDS Center, Target Field, US Bank Stadium, Stone Arch Bridge,
# Nicollet Island and the Hennepin Avenue Bridge all inside), each at standard or high detail.
VARIANTS = {
    "full-standard": {"title": "Minneapolis", "half": 2048.0, "shift": (0.0, 0.0), "detail": "standard", "mpp": 1.0, "spacing": 16.0,
                      "fine_radius": None},
    "full-high": {"title": "Minneapolis (high detail)", "half": 2048.0, "shift": (0.0, 0.0), "detail": "high", "mpp": 0.5, "spacing": 16.0,
                  "fine_radius": 1100.0},  # finer ground only within 1.1 km of downtown so the package stays under 256 MB
    "shrunk-high": {"title": "Minneapolis downtown (high detail)", "half": 1152.0, "shift": (330.0, 390.0), "detail": "high", "mpp": 0.5,
                    "spacing": 8.0, "fine_radius": None},
    "shrunk-standard": {"title": "Minneapolis downtown", "half": 1152.0, "shift": (330.0, 390.0), "detail": "standard", "mpp": 1.0,
                        "spacing": 16.0, "fine_radius": None},
}
IDS_FROM_CENTRE = (-200.0, -200.0)  # metres east, north of CENTRE
USER_AGENT = "BattleTanxStudio/0.2 (map data import)"
SKY_CREDIT = ("Horizon computed by BattleTanx Studio from OpenStreetMap (ODbL), Wikidata (CC0) and Terrain Tiles (USGS 3DEP); "
              "ramp made for this map")


# ----------------------------------------------------------------- download --

def download(sources: Path) -> None:
    """Fetch the map data once (polite: identified client, single queries, rate limits respected)."""

    sources.mkdir(parents=True, exist_ok=True)
    s, w, n, e = BOX
    area = f"({s},{w},{n},{e})"
    city = "[out:json][timeout:180][maxsize:536870912];(" + "".join(
        f"{kind}[{selector}]{area};" for kind, selector in (
            ("way", '"building"'), ("relation", '"building"'), ("way", '"building:part"'), ("relation", '"building:part"'),
            ("way", '"highway"'), ("way", '"railway"'), ("way", '"natural"~"water|wood|scrub|grassland"'), ("relation", '"natural"="water"'),
            ("way", '"waterway"'), ("relation", '"waterway"="riverbank"'), ("way", '"leisure"~"park|pitch|garden|playground|stadium|track"'),
            ("relation", '"leisure"~"park|stadium"'), ("way", '"landuse"'), ("relation", '"landuse"'), ("way", '"man_made"~"bridge|pier"'),
            ("way", '"amenity"="parking"'), ("node", '"natural"="tree"'))) + ");out geom;"
    ms, mw, mn, me = METRO
    metro = f"({ms},{mw},{mn},{me})"
    skyline = ("[out:json][timeout:180];("
               f'way["building"]["height"](if: number(t["height"]) >= 35){metro};'
               f'way["building:part"]["height"](if: number(t["height"]) >= 35){metro};'
               f'relation["building"]["height"](if: number(t["height"]) >= 35){metro};'
               f'way["building"]["building:levels"](if: number(t["building:levels"]) >= 11){metro};'
               f'way["building"]["roof:shape"="dome"]{metro};way["building:part"]["roof:shape"="dome"]{metro};'
               f'way["building"~"cathedral|church"]["name"~"Cathedral of Saint Paul|Basilica"]{metro};'
               f'way["building"]["name"~"State Capitol"]{metro};'
               f'node["man_made"~"mast|tower|communications_tower|water_tower"]["height"]{metro};'
               f'way["man_made"~"water_tower|tower"]{metro};);out geom tags;')
    for name, query in (("city.json", city), ("skyline.json", skyline)):
        if not (sources / name).is_file():
            subprocess.run(["curl", "-s", "-f", "-m", "400", "-A", USER_AGENT, "--data-urlencode", f"data={query}", "-o", str(sources / name),
                            "https://overpass-api.de/api/interpreter"], check=True)
    from tools.btga_studio.core import geo

    for zoom, (south, west, north, east) in ((15, (s - 0.002, w - 0.0026, n + 0.002, e + 0.0029)), (10, (44.58, -93.85, 45.38, -92.70))):
        x0, y0 = (int(v) for v in geo.tile_xy(north, west, zoom))
        x1, y1 = (int(v) for v in geo.tile_xy(south, east, zoom))
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                path = sources / "terrarium" / str(zoom) / str(x) / f"{y}.png"
                if not path.is_file():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    subprocess.run(["curl", "-s", "-f", "-m", "60", "-A", USER_AGENT, "-o", str(path),
                                    f"https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{zoom}/{x}/{y}.png"], check=True)
    queries = {
        "wikidata_area.json": f"""SELECT ?item ?itemLabel ?height ?heightUnit ?floors WHERE {{
  SERVICE wikibase:box {{ ?item wdt:P625 ?coord . bd:serviceParam wikibase:cornerWest "Point({w} {n})"^^geo:wktLiteral .
                         bd:serviceParam wikibase:cornerEast "Point({e} {s})"^^geo:wktLiteral . }}
  OPTIONAL {{ ?item p:P2048/psv:P2048 [wikibase:quantityAmount ?height; wikibase:quantityUnit ?heightUnit] }}
  OPTIONAL {{ ?item wdt:P1101 ?floors }} FILTER(BOUND(?height) || BOUND(?floors))
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }} }}""",
        "wikidata_metro.json": f"""SELECT ?item ?itemLabel ?coord ?height ?heightUnit ?floors WHERE {{
  SERVICE wikibase:around {{ ?item wdt:P625 ?coord . bd:serviceParam wikibase:center "Point({CENTRE[1]} {CENTRE[0]})"^^geo:wktLiteral .
                            bd:serviceParam wikibase:radius "40" . }}
  OPTIONAL {{ ?item p:P2048/psv:P2048 [wikibase:quantityAmount ?height; wikibase:quantityUnit ?heightUnit] }}
  OPTIONAL {{ ?item wdt:P1101 ?floors }}
  FILTER((BOUND(?height) && ?height >= 30) || (BOUND(?floors) && ?floors >= 10))
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }} }}"""}
    first = True
    for name, query in queries.items():
        if (sources / name).is_file():
            continue
        if not first:
            time.sleep(70)  # the query service may allow one request a minute
        first = False
        subprocess.run(["curl", "-s", "-f", "-m", "120", "-A", USER_AGENT, "-H", "Accept: application/sparql-results+json",
                        "--data-urlencode", f"query={query}", "-o", str(sources / name), "https://query.wikidata.org/sparql"], check=True)


# ---------------------------------------------------------------------- kit --

def write_kit(kit: Path, sources: Path, centre=CENTRE, half: float = HALF_M) -> dict:
    """The teleport ramp (OBJ + MTL + PNG, made here) and the computed horizon panorama (PNG)."""

    import numpy as np

    from tools.btga_assets.ps1_formats import encode_png_rgba
    from tools.btga_studio.core import geo, osm, skyline

    kit.mkdir(parents=True, exist_ok=True)
    size = 128
    y, x = np.mgrid[0:size, 0:size]
    stripes = np.where(((x + y) // 16) % 2 == 0, 1, 0)
    chevrons = np.zeros((size, size, 4), np.uint8)
    chevrons[...] = (35, 35, 38, 255)
    chevrons[stripes == 1] = (236, 196, 40, 255)
    chevrons[(x < 6) | (x >= size - 6)] = (210, 210, 205, 255)
    (kit / "ramp_chevrons.png").write_bytes(encode_png_rgba(size, size, chevrons.tobytes()))
    pad = np.zeros((size, size, 4), np.uint8)
    pad[...] = (120, 122, 128, 255)
    radius = np.hypot(x - size / 2 + 0.5, y - size / 2 + 0.5)
    pad[(radius > 40) & (radius < 52)] = (150, 70, 190, 255)
    pad[radius <= 12] = (235, 235, 235, 255)
    (kit / "ramp_pad.png").write_bytes(encode_png_rgba(size, size, pad.tobytes()))
    (kit / "teleport_ramp.mtl").write_text("newmtl slope\nKd 1 1 1\nmap_Kd ramp_chevrons.png\n"
                                           "newmtl deck\nKd 1 1 1\nmap_Kd ramp_pad.png\n"
                                           "newmtl side\nKd 0.55 0.56 0.6\n", encoding="utf-8")
    # Metres, Y up: a 24 m slope rising to 3.5 m, then a 12 m square landing; 12 m wide; the slope starts at +Z.
    w, h = 6.0, 3.5
    vertices = [(-w, 0, 18), (w, 0, 18), (w, h, -6), (-w, h, -6), (w, h, -18), (-w, h, -18), (w, 0, -6), (-w, 0, -6), (w, 0, -18), (-w, 0, -18)]
    lines = ["# Teleport ramp for the Minneapolis map (made with BattleTanx Studio's map script)", "mtllib teleport_ramp.mtl"]
    lines += [f"v {a:g} {b:g} {c:g}" for a, b, c in vertices]
    lines += ["vt 0 0", "vt 1 0", "vt 1 3", "vt 0 3", "vt 1 1", "vt 0 1"]
    lines += ["usemtl slope", "f 1/1 2/2 3/3 4/4", "usemtl deck", "f 4/1 3/2 5/5 6/6",
              "usemtl side", "f 2 7 3", "f 7 9 5 3", "f 8 1 4", "f 10 8 4 6", "f 9 10 6 5"]
    (kit / "teleport_ramp.obj").write_text("\n".join(lines) + "\n", encoding="utf-8")
    terrain = geo.Terrain(sources / "terrarium")
    landmarks = skyline.landmarks_from_wikidata(json.loads((sources / "wikidata_metro.json").read_text(encoding="utf-8")))
    pixels, summary = skyline.render(skyline.SkylineOptions(centre=centre, half_extent_m=half), terrain,
                                     osm.read_osm(sources / "skyline.json"), landmarks)
    (kit / "minneapolis_horizon.png").write_bytes(encode_png_rgba(pixels.shape[1], pixels.shape[0], pixels.tobytes()))
    (kit / "minneapolis_horizon.json").write_text(json.dumps(summary, indent=1) + "\n", encoding="utf-8")
    return summary


# ------------------------------------------------------------------- places --

def named_points(data, projection, names: dict[str, tuple]) -> dict[str, tuple[float, float]]:
    """(east, north) metres of the centroid of the first feature matching each (key, value-or-name)."""

    import numpy as np

    out = {}
    features = [(way["tags"], way["coords"]) for way in data.ways]
    features += [(relation["tags"], [c for m in relation["members"] if m["role"] == "outer" for c in m["coords"]]) for relation in data.relations]
    for label, (key, value, name) in names.items():
        for tags, coords in features:
            if coords and (key is None or tags.get(key) == value) and (name is None or tags.get("name") == name):
                points = np.asarray(coords, float)
                e, n = projection.forward(points[:, 0], points[:, 1])
                out[label] = (float(e.mean()), float(n.mean()))
                break
    return out


def street_line(data, projection, name: str, bridges: bool = False) -> list[tuple[float, float]]:
    """The longest chain of ways carrying a street name (bridges only with ``bridges``), as (east, north) metres."""

    segments = [way["coords"] for way in data.ways if way["tags"].get("name") == name and way["tags"].get("highway")
                and bool(way["tags"].get("bridge")) == bridges and not way["tags"].get("tunnel")]
    chains = []
    pending = [list(s) for s in segments]
    while pending:
        chain = pending.pop(0)
        grown = True
        while grown:
            grown = False
            for index, other in enumerate(pending):
                if other[0] == chain[-1]:
                    chain += other[1:]
                elif other[-1] == chain[0]:
                    chain = other[:-1] + chain
                elif other[-1] == chain[-1]:
                    chain += other[-2::-1]
                elif other[0] == chain[0]:
                    chain = other[:0:-1] + chain
                else:
                    continue
                pending.pop(index)
                grown = True
                break
        chains.append(chain)
    best = max(chains, key=len) if chains else []
    points = [projection.forward(lat, lon) for lat, lon in best]
    return [(float(e), float(n)) for e, n in points] if len(points) > 1 else []


def along(line, spacing: float) -> list[tuple[float, float]]:
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


def roof_point(library, model_id: str, translation) -> tuple[float, float, float]:
    """Centre and height (Studio units) of the highest flat area of a placed model."""

    import numpy as np

    mesh = library.model(model_id).mesh
    points = np.asarray(mesh["positions"], float)
    top = points[:, 1].max()
    roof = points[points[:, 1] >= top - 2.0]
    return (float(roof[:, 0].mean() + translation[0]), float(top + translation[1]), float(roof[:, 2].mean() + translation[2]))


def look(camera, eye, target) -> None:
    import numpy as np

    camera.position = np.asarray(eye, float)
    dx, dy, dz = (target[i] - eye[i] for i in range(3))
    camera.yaw = math.atan2(-dx, -dz)
    camera.pitch = math.atan2(dy, math.hypot(dx, dz))


# -------------------------------------------------------------------- build --

def build(sources: Path, output: Path, capture: bool = True, variant: str = "full-standard") -> dict:
    os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
    import numpy as np

    from tools import studio_evidence as ev
    from tools.btga_studio.core import geo
    from tools.btga_studio.core import geo, hub, osm, package
    from tools.btga_studio.core.imports import ImportOptions
    from tools.btga_studio.core.settings import Settings
    from tools.btga_studio.fixtures import ScriptedDialogs, recording_launcher
    from tools.btga_studio.gui.app import StudioApp
    from tools.btga_studio.gui.offscreen import OffscreenUI

    started = time.time()
    setup = VARIANTS[variant]
    half_m = setup["half"]
    base_projection = geo.LocalProjection(*CENTRE)
    lat, lon = base_projection.inverse(setup["shift"][0], setup["shift"][1])
    centre = (round(float(lat), 6), round(float(lon), 6))
    title = setup["title"]
    output.mkdir(parents=True, exist_ok=True)
    shots = output / "screenshots"
    shots.mkdir(exist_ok=True)
    kit = output / "kit"
    report = {"steps": [], "screenshots": [], "variant": {"name": variant, **setup, "centre": centre}}

    def note(text):
        report["steps"].append({"t": round(time.time() - started, 1), "step": text})
        print(f"[{time.time() - started:6.1f}s] {text}", flush=True)

    note("computing the horizon and writing the ramp kit")
    horizon = write_kit(kit, sources, centre, half_m)
    identity = ev.source_identity(ROOT)
    os.environ["BTGA_STUDIO_HOME"] = str(output / "studio-home")
    with recording_launcher():
        app = StudioApp(Settings.open())
        app.settings.preferences.welcome_seen = True
        app.settings.preferences.show_hub_at_start = False
        app.settings.preferences.tutorial_done = True
        app.settings.preferences.tutorial_active = False
        ui = OffscreenUI(app, 1600, 960) if capture else None
        c = app.controller
        app.show_hub = False
        number = [0]

        def shoot(name, scenario, expected, settle=4):
            if ui is None:
                return
            for _ in range(settle):
                ui.frame()
            number[0] += 1
            width, height, rgba = ui.pixels()
            stats = ev.image_stats(width, height, rgba)
            problems = ev.inspect_image(stats)
            png = shots / f"{number[0]:02d}_{name}.png"
            ui.screenshot(png)
            ev.write_record(png, identity, scenario, expected, f"automatic: {stats['unique_colours']} colours", not problems,
                            "automatic checks only; pending visual review" if not problems else "; ".join(problems), stats=stats)
            report["screenshots"].append({"file": png.name, "scenario": scenario, "ok": not problems, "problems": problems})
            note(f"screenshot {png.name}{'' if not problems else ' PROBLEMS ' + '; '.join(problems)}")

        def attach() -> None:
            if ui is not None:  # the viewport renderer needs the graphics context only a capture run has
                app.attach_library()

        def scenic(on: bool) -> None:
            for layer in ("grid", "collision", "bounds", "navigation"):
                app.settings.preferences.layers[layer] = not on

        # 1. a new project with an empty map
        c.new_studio_project(title)
        c.project.project["description"] = (f"Downtown Minneapolis at true scale ({2 * half_m / 1000:.1f} km across, {setup['detail']} detail): the IDS Center, Nicollet Mall and the skyways, "
                                            "the Mississippi at St. Anthony Falls, the Stone Arch Bridge, Target Field and US Bank Stadium.")
        if ui:
            ui.frame()

        # 2. import the ramp and the sky panorama through the Import window
        app.open_import()
        app.import_state.update({"group": "Minneapolis kit", "credit": SKY_CREDIT, "shape": 4, "units": 0, "ground": True})
        app.dialogs = ScriptedDialogs(kit / "teleport_ramp.obj", kit / "minneapolis_horizon.png")
        for _ in range(2):
            app.import_state["files"].append(app.dialogs.open_file("Choose a model or picture to import", []))
        app.check_imports()  # the window does this on its next frame; done here too so builds without screenshots work
        shoot("import_window_checked", "Import window with the teleport ramp OBJ and the computed horizon picture checked",
              "both files listed in green with what Studio found; sky panorama option chosen; credit filled in", settle=6)
        results = [app.import_state["results"][p] for p in app.import_state["files"] if p in app.import_state["results"]]
        if len(results) != 2:
            raise SystemExit(f"import check failed: {app.import_state['errors']}")
        for result in results:
            c.add_import(result)
        attach()
        app.import_state.update({"files": [], "results": {}, "errors": {}, "key": None,
                                 "done": "Added the ramp and the horizon to your library under \"Imported: Minneapolis kit\"."})
        shoot("import_window_done", "Import window after importing", "green confirmation; library section lists the ramp and the sky")
        app.show_import = False
        ramp_model = next(m for m, e in c.library._models.items() if e.get("label") == "teleport_ramp")
        sky_texture = next(t["id"] for t in c.library.catalog["textures"] if t.get("sky"))

        # 3. the city from OpenStreetMap, elevation tiles and Wikidata heights
        note("building the city from OpenStreetMap")
        data = osm.read_osm(sources / "city.json")
        if setup["detail"] == "high" and (sources / "details.json").is_file():  # street lamps, signals, crossings, walls, fences, hedges
            data = osm.merge(data, osm.read_osm(sources / "details.json"))
        terrain = geo.Terrain(sources / "terrarium")
        heights = osm.heights_from_wikidata(json.loads((sources / "wikidata_area.json").read_text(encoding="utf-8")))
        options = osm.CityOptions(centre=centre, half_extent_m=half_m, group="Minneapolis", title=title, heights=heights, detail=setup["detail"],
                                  ground_metres_per_pixel=setup["mpp"], terrain_spacing_m=setup["spacing"], fine_ground_radius_m=setup["fine_radius"],
                                  fine_ground_centre=(IDS_FROM_CENTRE[0] - setup["shift"][0], IDS_FROM_CENTRE[1] - setup["shift"][1]))
        plan = osm.build_city(data, options, terrain, lambda stage: note(f"  city: {stage}"))
        note(f"city built: {plan.counts}")
        c.add_import(plan.result)
        key = c.place_city(plan, title, "minneapolis")
        c.project.set_map_sky(key, {"texture": sky_texture, "north": 0.0, "horizon": 0.75, "vertical_degrees": 45.0})
        attach()
        projection = geo.LocalProjection(*centre)
        S = osm.SCALE

        def inside(en, margin=40.0):
            return abs(en[0]) < half_m - margin and abs(en[1]) < half_m - margin

        def ground_y(e, n):
            return float((terrain.elevation(*projection.inverse(np.array([e]), np.array([n])))[0] - plan.reference_elevation) * S)

        def world(e, n, lift=0.0):
            return (e * S, ground_y(e, n) + lift * S, -n * S)

        ids = plan.landmarks["IDS Center"]
        ids_instance = next(i for i in plan.instances if i["model"] == ids["model"])
        roof = roof_point(c.library, ids["model"], ids_instance["translation"])
        scenic(True)
        k = half_m / 2048.0  # the overview stands back in proportion to the map
        look(c.camera, (roof[0] - 9000 * k, 14000 * k, roof[2] + 16000 * k), (roof[0] + 2000 * k, 600, roof[2] - 4000 * k))
        shoot("city_overview_southwest", "Downtown Minneapolis from the south-west after the OpenStreetMap import",
              "real skyline: IDS Center, Wells Fargo Center, Capella Tower; the Mississippi beyond; sky panorama on the horizon", settle=8)

        # 4. gameplay: teleporter ramp on Nicollet Mall by the IDS Center, roof exit, pickups, starts
        doc = c.document
        templates = {t["source_kind"].removeprefix("studio_"): t for t in c.library.starter()["templates"]}
        mall = street_line(data, projection, "Nicollet Mall")
        ids_en = (roof[0] / S, -roof[2] / S)
        nearest = min(range(len(mall) - 1), key=lambda k: math.dist(((mall[k][0] + mall[k + 1][0]) / 2, (mall[k][1] + mall[k + 1][1]) / 2), ids_en))
        a, b = mall[nearest], mall[nearest + 1]
        direction = ((b[0] - a[0]) / math.dist(a, b), (b[1] - a[1]) / math.dist(a, b))
        mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        # Ramp model: slope rises towards local -Z.  Yaw so that local -Z points along the mall direction.
        heading = math.atan2(direction[0], direction[1])  # compass bearing of the mall direction (radians)
        yaw = int(round(-heading / (2 * math.pi) * 65536)) & 0xFFFF
        ramp_centre = world(mid[0], mid[1])
        ramp = doc.place_model(ramp_model, ramp_centre, yaw)
        landing = (mid[0] + direction[0] * 12.0, mid[1] + direction[1] * 12.0)
        street_exit = (mid[0] - direction[0] * 30.0, mid[1] - direction[1] * 30.0)
        roof_exit = doc.place_marker(templates["teleport_exit"], (roof[0] - 120.0, roof[1], roof[2] + 120.0), yaw,
                                     values={"label": "IDS Center roof (241 m)"})
        pad = doc.place_marker(templates["teleporter"], (landing[0] * S, ramp_centre[1] + 3.5 * S, -landing[1] * S), yaw,
                               values={"exit": roof_exit, "radius": 90})
        street = doc.place_marker(templates["teleport_exit"], world(*street_exit), (yaw + 0x8000) & 0xFFFF,
                                  values={"label": "Nicollet Mall at the IDS Center"})
        back = doc.place_marker(templates["teleporter"], (roof[0] + 140.0, roof[1], roof[2] - 140.0), 0, values={"exit": street, "radius": 70})
        weapon = doc.place_marker(templates["pickup"], (roof[0] + 160.0, roof[1], roof[2] + 160.0), 0,
                                  values={"pickup_type": 2, "respawn_seconds": 45})
        places = named_points(data, projection, {
            "target_field_plaza": (None, None, "Target Field Station Plaza"),
            "commons": ("leisure", "park", "The Commons"), "loring": ("leisure", "park", "Loring Park"),
            "gold_medal": ("leisure", "park", "Gold Medal Park"), "mill_ruins": ("leisure", "park", "Mill Ruins Park"),
            "government_plaza": ("leisure", "park", "Government Center Park"), "peavey": ("leisure", "park", "Peavey Plaza"),
            "nicollet_island": ("leisure", "park", "Nicollet Island Park"), "boom_island": ("leisure", "park", "Boom Island Park"),
            "father_hennepin": ("leisure", "park", "Father Hennepin Bluff Park")})
        places = {k: v for k, v in places.items() if inside(v)}
        note(f"named places inside the map: {sorted(places)}")
        starts = [p for p in ("target_field_plaza", "commons", "loring", "mill_ruins") if p in places]
        for fallback in ("gold_medal", "boom_island", "father_hennepin", "nicollet_island", "peavey"):
            if len(starts) < 4 and fallback in places:
                starts.append(fallback)
        start_ids = []
        for team, name in enumerate(starts[:4]):
            e, n = places[name]
            toward = math.atan2(ids_en[0] - e, ids_en[1] - n)
            facing = int(round(-toward / (2 * math.pi) * 65536)) & 0xFFFF
            start_ids.append(doc.place_marker(templates["team_start"], world(e, n, 0.2), facing, values={"team": team}))
            offset = (e + 25.0 * math.sin(toward + 1.2), n + 25.0 * math.cos(toward + 1.2))
            doc.place_marker(templates["player_spawn"], world(*offset, 0.2), facing, values={"team": team, "player": team})
        pickups = [("peavey", 0), ("government_plaza", 1), ("gold_medal", 3), ("nicollet_island", 0), ("father_hennepin", 1), ("boom_island", 2)]
        for name, kind in pickups:
            if name in places:
                doc.place_marker(templates["pickup"], world(*places[name], 0.2), 0, values={"pickup_type": kind, "respawn_seconds": 30})
        bridge = street_line(data, projection, "Stone Arch Bridge", bridges=True)
        nodes = []
        for street_name in ("Hennepin Avenue", "Nicollet Mall", "Washington Avenue South", "South 5th Street", "Chicago Avenue",
                            "North 5th Street"):
            line = street_line(data, projection, street_name)
            points = [p for p in along(line, 260.0) if inside(p)]
            chain = [doc.place_marker(templates["nav_node"], world(*p, 0.2), 0) for p in points]
            for first, second in zip(chain[:-1], chain[1:]):
                doc.set_field(first, "link_0", second)
            nodes += [(node, p) for node, p in zip(chain, points)]
        for marker in start_ids:  # a node at each start, linked to the nearest street node
            record = doc.record(marker)
            position = (record["translation"][0] / S, -record["translation"][2] / S)
            nearest_node = min(nodes, key=lambda item: math.dist(item[1], position))[0]
            start_node = doc.place_marker(templates["nav_node"], world(position[0] + 12.0, position[1], 0.2), 0)
            doc.set_field(start_node, "link_0", nearest_node)
            nodes.append((start_node, position))
        bridge = [p for p in bridge if inside(p)]
        if bridge:
            doc.place_marker(templates["pickup"], world(*bridge[len(bridge) // 2], 6.0), 0, values={"pickup_type": 3, "respawn_seconds": 30})
        doc.selection = []
        c.project.define("multiplayer", "minneapolis", {"title": f"{title}: Downtown Showdown", "map": key, "modes": list(hub.MODES),
                                                        "max_players": 4})
        note(f"gameplay placed: ramp {ramp}, teleporter {pad} -> {roof_exit}, return {back} -> {street}, weapon {weapon}, "
             f"{len(start_ids)} team starts, {len(nodes)} navigation nodes")

        eye_en = (mid[0] - direction[0] * 70.0, mid[1] - direction[1] * 70.0)  # back along the mall, above the street
        look(c.camera, (eye_en[0] * S, ramp_centre[1] + 26 * S, -eye_en[1] * S), (ramp_centre[0], ramp_centre[1] + 8 * S, ramp_centre[2]))
        shoot("ids_ramp_nicollet_mall", "The teleport ramp on Nicollet Mall in front of the IDS Center",
              "striped ramp with the purple teleporter pad on its landing; IDS tower and skyways around", settle=6)
        ring = np.asarray(c.library.model(ids["model"]).mesh["positions"], float)
        roof_east = float(ring[:, 0].max() + ids_instance["translation"][0])
        east = (roof_east - 2 * S, roof[1] + 25 * S, roof[2])
        # Bearing 104 degrees (Saint Paul): east is +X, north is -Z; look 6 degrees down to show downtown and the horizon.
        direction_x, direction_z = math.sin(math.radians(104)), -math.cos(math.radians(104))
        look(c.camera, east, (east[0] + direction_x * 10000.0, east[1] - 10000.0 * math.tan(math.radians(6)), east[2] + direction_z * 10000.0))
        shoot("ids_roof_facing_east", "On the IDS Center roof facing east-south-east",
              "the roof exit, return teleporter and weapon pickup in front; Saint Paul low on the horizon in the panorama", settle=6)
        look(c.camera, (roof[0] + 700, roof[1] + 500, roof[2] + 900), (roof[0], roof[1], roof[2]))
        shoot("ids_roof_markers", "IDS Center roof with the teleport exit, return teleporter and weapon pickup",
              "three markers on the flat roof of the 241 m tower", settle=6)

        def sight(name, label, en, distance, height, bearing):
            e, n = en
            target = world(e, n)
            eye_e, eye_n = e + distance * math.sin(math.radians(bearing)), n + distance * math.cos(math.radians(bearing))
            look(c.camera, (eye_e * S, target[1] + height * S, -eye_n * S), (target[0], target[1] + 20 * S, target[2]))
            shoot(name, label, f"{label} as modelled from OpenStreetMap", settle=6)

        landmark = plan.landmarks
        if "Target Field" in landmark:
            centre = landmark["Target Field"]["centre"]
            sight("target_field", "Target Field, the open-air ballpark", (centre[0] / S, -centre[2] / S), 420, 160, 120)
        stadium = next((k for k in landmark if "Bank Stadium" in k), None)
        if stadium:
            centre = landmark[stadium]["centre"]
            sight("us_bank_stadium", "US Bank Stadium", (centre[0] / S, -centre[2] / S), 520, 140, 250)
        if bridge:
            sight("stone_arch_bridge", "The Stone Arch Bridge and St. Anthony Falls", bridge[len(bridge) // 2], 520, 90, 160)
        if "nicollet_island" in places:
            sight("nicollet_island", "Nicollet Island and the Hennepin Avenue Bridge", places["nicollet_island"], 600, 140, 200)
        if "loring" in places:
            sight("loring_park", "Loring Park towards downtown", places["loring"], 400, 70, 225)
        if mall:
            point = mall[max(0, nearest - 3)]
            side = (direction[1] * 5.0, -direction[0] * 5.0)  # 5 m off the centre line, clear of lamps and signals mapped on it
            eye = world(point[0] + side[0], point[1] + side[1], 4.0)
            ahead = mall[min(len(mall) - 1, nearest + 2)]
            look(c.camera, eye, world(ahead[0] + side[0], ahead[1] + side[1], 8))
            shoot("nicollet_mall_street_level", "Nicollet Mall at street level towards the IDS Center",
                  "tank's-eye view: skyways crossing the mall, towers either side", settle=6)
        scenic(False)
        top_y = 64000.0 * half_m / 2048.0
        look(c.camera, (0.0, top_y, 1.0), (0.0, 0.0, 0.0))
        shoot("map_from_above_with_gameplay", "Whole map from above with gameplay markers and collision",
              f"{2 * half_m / 1000:.1f} km square: river, downtown grid, starts, pickups, navigation lines", settle=8)

        # 5. the gameplay editor, validation and export
        app.open_gameplay("Multiplayer")
        for _ in range(2):
            ui and ui.frame()
        shoot("gameplay_multiplayer", "Gameplay editor: Multiplayer tab", "Minneapolis: Downtown Showdown with deathmatch and team deathmatch ticked, 4 players")
        app.open_gameplay("Map")
        shoot("gameplay_map_sky", "Gameplay editor: Map tab with the sky panorama", "Sky panorama set to the computed Minneapolis horizon")
        app.show_gameplay = False
        c.request_diagnostics(immediate=True)
        for _ in range(200):
            c.tick()
            if c._diag_future is None or c._diag_future.done():
                break
            time.sleep(0.05)
        c.tick()
        errors = [d for d in c.diagnostics if d.severity == "error"]
        warnings = [d for d in c.diagnostics if d.severity == "warning"]
        note(f"validation: {len(errors)} errors, {len(warnings)} warnings")
        report["validation"] = {"errors": [d.as_dict() for d in errors[:50]], "warnings": len(warnings),
                                "warning_codes": sorted({d.code for d in warnings})}
        project_path = output / "Minneapolis.btgaproj"
        c.save(project_path)
        package_path = output / "Minneapolis.btgapkg"
        note("exporting the content package")
        data_bytes = package.build_package(c.project, c.library, "0.2.0")
        package_path.write_bytes(data_bytes)
        checked = package.read_package(data_bytes)
        app.export_result = {"name": package_path.name, "path": str(package_path), "size": len(data_bytes), "maps": len(checked.maps)}
        shoot("export_done", "Content package exported", "green confirmation with size and map count")
        app.export_result = None
        report.update({"project": project_path.name, "package": package_path.name, "package_bytes": len(data_bytes),
                       "package_files": len(checked.manifest["files"]), "credits": len(checked.manifest.get("credits", [])),
                       "counts": plan.counts, "estimated_heights": plan.estimated, "reference_elevation_m": plan.reference_elevation,
                       "landmarks": {k: v["height_m"] for k, v in sorted(plan.landmarks.items())},
                       "horizon": {"viewer_elevation_m": horizon["viewer_elevation_m"],
                                   "saint_paul": [f for f in horizon["features"] if 95 <= f["bearing"] <= 112 and f["distance_m"] > 10000]},
                       "multiplayer": c.project.multiplayer, "seconds": round(time.time() - started, 1)})
        c.shutdown()
        if ui is not None:
            ui.close()
    (output / "build-report.json").write_text(json.dumps(report, indent=1, default=str) + "\n", encoding="utf-8")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("sources", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--download", action="store_true", help="fetch missing map data first")
    parser.add_argument("--no-capture", action="store_true", help="build without screenshots")
    parser.add_argument("--variant", choices=sorted(VARIANTS), default="full-standard", help="map size and detail (default: the original 4 km map)")
    args = parser.parse_args(argv)
    if args.download:
        download(args.sources)
    report = build(args.sources, args.output, capture=not args.no_capture, variant=args.variant)
    print(json.dumps({k: report[k] for k in ("counts", "package_bytes", "validation", "seconds") if k in report}, indent=1, default=str))
    return 0 if not report.get("validation", {}).get("errors") else 1


if __name__ == "__main__":
    raise SystemExit(main())
