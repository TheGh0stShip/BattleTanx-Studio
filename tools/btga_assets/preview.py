"""Deterministic offline preview rendering for shared meshes (no windows, no GPU).

A small z-buffered rasteriser: orthographic or perspective camera, back-face
culling for single-sided materials, nearest texture sampling with wrap
modes, alpha-mask discard.  It exists for contact sheets and visual checks;
it is not a faithful console renderer.
"""

from __future__ import annotations

import math

from .mesh import with_game_lighting
from .ps1_formats import encode_png_rgba

BACKGROUND = (40, 40, 48, 255)


def _sample(texture: dict, u: float, v: float):
    width, height, rgba = texture["width"], texture["height"], texture["rgba"]
    wrap = texture.get("wrap") or {"s": "clamp", "t": "clamp"}

    def coord(value: float, size: int, mode: str) -> int:
        texel = math.floor(value * size)
        if mode == "clamp":
            return min(size - 1, max(0, texel))
        if mode == "mirror":
            period = texel % (2 * size)
            return period if period < size else 2 * size - 1 - period
        return texel % size

    x = coord(u, width, wrap["s"])
    y = coord(v, height, wrap["t"])
    offset = (y * width + x) * 4
    return rgba[offset : offset + 4]


def render(meshes: list[tuple[dict, list[float], int]], textures: dict[str, dict], size: tuple[int, int],
           eye: list[float], target: list[float], fov_degrees: float | None = 40.0, cull: bool = True) -> bytes:
    """Render [(mesh, translation, yaw_units)] and return RGBA8888 pixels."""

    width, height = size
    forward = [t - e for t, e in zip(target, eye)]
    length = math.sqrt(sum(v * v for v in forward)) or 1.0
    forward = [v / length for v in forward]
    up = [0.0, 1.0, 0.0]
    side = [forward[1] * up[2] - forward[2] * up[1], forward[2] * up[0] - forward[0] * up[2], forward[0] * up[1] - forward[1] * up[0]]
    side_length = math.sqrt(sum(v * v for v in side)) or 1.0
    side = [v / side_length for v in side]
    true_up = [side[1] * forward[2] - side[2] * forward[1], side[2] * forward[0] - side[0] * forward[2], side[0] * forward[1] - side[1] * forward[0]]
    focal = (width / 2) / math.tan(math.radians(fov_degrees) / 2) if fov_degrees else None
    ortho_scale = None if focal else (width / 2) / (length * 0.6)
    depth = [math.inf] * (width * height)
    frame = bytearray(BACKGROUND * (width * height))

    for mesh, translation, yaw in meshes:
        mesh = with_game_lighting(mesh)
        theta = (yaw & 0xFFFF) * 2 * math.pi / 65536
        c, s = math.cos(theta), math.sin(theta)
        world = [[x * c + z * s + translation[0], y + translation[1], -x * s + z * c + translation[2]] for x, y, z in mesh["positions"]]
        for triangle in mesh["triangles"]:
            material = mesh["materials"][triangle["material"]]
            texture = textures.get(material.get("texture")) if material.get("texture") else None
            if texture is not None and "rgba" not in texture:
                texture = None
            screen = []
            for corner in triangle["corners"]:
                d = [world[corner["p"]][i] - eye[i] for i in range(3)]
                x, y, z = (sum(d[i] * side[i] for i in range(3)), sum(d[i] * true_up[i] for i in range(3)), sum(d[i] * forward[i] for i in range(3)))
                if focal:
                    if z < 1.0:
                        break
                    screen.append((width / 2 + focal * x / z, height / 2 - focal * y / z, z))
                else:
                    screen.append((width / 2 + ortho_scale * x, height / 2 - ortho_scale * y, z))
            if len(screen) < 3:
                continue
            area = (screen[1][0] - screen[0][0]) * (screen[2][1] - screen[0][1]) - (screen[2][0] - screen[0][0]) * (screen[1][1] - screen[0][1])
            if area == 0 or (cull and area > 0 and not material["double_sided"]):
                continue
            base = material.get("base_colour") or [0.8, 0.8, 0.8, 1.0]
            min_x, max_x = max(0, int(min(p[0] for p in screen))), min(width - 1, int(max(p[0] for p in screen)) + 1)
            min_y, max_y = max(0, int(min(p[1] for p in screen))), min(height - 1, int(max(p[1] for p in screen)) + 1)
            for py in range(min_y, max_y + 1):
                for px in range(min_x, max_x + 1):
                    cx, cy = px + 0.5, py + 0.5
                    w0 = ((screen[1][0] - cx) * (screen[2][1] - cy) - (screen[2][0] - cx) * (screen[1][1] - cy)) / area
                    w1 = ((screen[2][0] - cx) * (screen[0][1] - cy) - (screen[0][0] - cx) * (screen[2][1] - cy)) / area
                    w2 = 1 - w0 - w1
                    if w0 < 0 or w1 < 0 or w2 < 0:
                        continue
                    z = w0 * screen[0][2] + w1 * screen[1][2] + w2 * screen[2][2]
                    index = py * width + px
                    if z >= depth[index]:
                        continue
                    weights = (w0, w1, w2)
                    colours = [corner["colour"] for corner in triangle["corners"]]
                    shade = [sum(weights[k] * colours[k][ch] for k in range(3)) for ch in range(3)] if all(colours) else base[:3]
                    if texture is not None and all(corner["uv"] for corner in triangle["corners"]):
                        u = sum(weights[k] * triangle["corners"][k]["uv"][0] for k in range(3))
                        v = sum(weights[k] * triangle["corners"][k]["uv"][1] for k in range(3))
                        texel = _sample(texture, u, v)
                        if material["alpha_mode"] != "OPAQUE" and texel[3] < 128:
                            continue
                        rgb = [min(255, int(texel[ch] * shade[ch])) for ch in range(3)]
                    else:
                        rgb = [min(255, int(255 * shade[ch])) for ch in range(3)]
                    depth[index] = z
                    frame[index * 4 : index * 4 + 3] = bytes(rgb)
    return bytes(frame)


def model_thumbnail(mesh: dict, textures: dict[str, dict], size: int = 96) -> bytes:
    """Three-quarter view framed on the mesh bounds; returns RGBA pixels."""

    if not mesh["positions"]:
        return bytes(BACKGROUND * (size * size))
    lo = [min(p[i] for p in mesh["positions"]) for i in range(3)]
    hi = [max(p[i] for p in mesh["positions"]) for i in range(3)]
    centre = [(a + b) / 2 for a, b in zip(lo, hi)]
    radius = max(math.dist(lo, hi) / 2, 1.0)
    eye = [centre[0] + radius * 1.6, centre[1] + radius * 1.2, centre[2] - radius * 1.9]
    return render([(mesh, [0.0, 0.0, 0.0], 0)], textures, (size, size), eye, centre, 40.0, cull=False)


def contact_sheet(tiles: list[list[bytes]], tile: int) -> bytes:
    """Grid of RGBA tiles (rows of equal length) as a PNG."""

    rows, columns = len(tiles), max(len(row) for row in tiles)
    width, height = columns * (tile + 4), rows * (tile + 4)
    sheet = bytearray(bytes((20, 20, 24, 255)) * (width * height))
    for r, row in enumerate(tiles):
        for col, pixels in enumerate(row):
            for y in range(tile):
                start = ((r * (tile + 4) + y) * width + col * (tile + 4)) * 4
                sheet[start : start + tile * 4] = pixels[y * tile * 4 : (y + 1) * tile * 4]
    return encode_png_rgba(width, height, bytes(sheet))


# ------------------------------------------------------------ plan views --

PLAN_COLOURS = {
    "collision_proven": (230, 60, 60, 255),
    "collision_planar": (240, 150, 40, 255),
    "collision_unregistered": (110, 110, 110, 255),
    "instance": (150, 150, 160, 255),
    "spawn": (70, 140, 255, 255),
    "objective": (255, 220, 40, 255),
    "pickup": (60, 220, 90, 255),
    "hazard": (230, 60, 230, 255),
    "generator": (40, 230, 230, 255),
    "marker": (235, 235, 235, 255),
    "navigation": (40, 170, 170, 255),
    "bounds": (255, 255, 255, 255),
    "minimap_box": (255, 90, 90, 255),
}
MARKER_GROUPS = {
    "spawn": {"unit_spawner", "team_spawn_slot", "team_start", "team_point_a", "team_point_b", "multiplayer_structure"},
    "objective": {"objective", "mission_trigger"},
    "pickup": {"pickup", "powerup_pad", "crate"},
    "hazard": {"hazard"},
    "generator": {"tank_generator"},
}


class Plan:
    """Top-down canvas: x to the right, z downwards (the minimap's axes)."""

    def __init__(self, x_range, z_range, size: int = 768, background=(24, 24, 30, 255)):
        span = max(x_range[1] - x_range[0], z_range[1] - z_range[0]) or 1.0
        self.scale = (size - 16) / span
        self.x0, self.z0 = x_range[0], z_range[0]
        self.width = int((x_range[1] - x_range[0]) * self.scale) + 16
        self.height = int((z_range[1] - z_range[0]) * self.scale) + 16
        self.pixels = bytearray(bytes(background) * (self.width * self.height))

    def to_pixel(self, x: float, z: float) -> tuple[int, int]:
        return int(round((x - self.x0) * self.scale)) + 8, int(round((z - self.z0) * self.scale)) + 8

    def dot(self, px: int, py: int, colour, radius: int = 0) -> None:
        for y in range(py - radius, py + radius + 1):
            for x in range(px - radius, px + radius + 1):
                if 0 <= x < self.width and 0 <= y < self.height:
                    self.pixels[(y * self.width + x) * 4 : (y * self.width + x) * 4 + 4] = bytes(colour)

    def line(self, a: tuple[int, int], b: tuple[int, int], colour) -> None:
        (x0, y0), (x1, y1) = a, b
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        error = dx + dy
        for _ in range(4 * (self.width + self.height)):
            self.dot(x0, y0, colour)
            if (x0, y0) == (x1, y1):
                break
            doubled = 2 * error
            if doubled >= dy:
                error += dy
                x0 += sx
            if doubled <= dx:
                error += dx
                y0 += sy

    def polygon(self, points: list[tuple[float, float]], colour) -> None:
        pixels = [self.to_pixel(x, z) for x, z in points]
        for a, b in zip(pixels, pixels[1:] + pixels[:1]):
            self.line(a, b, colour)

    def png(self) -> bytes:
        return encode_png_rgba(self.width, self.height, bytes(self.pixels))


def _corners(shape: dict) -> list[tuple[float, float]]:
    theta = (shape["yaw"] & 0xFFFF) * 2 * math.pi / 65536
    c, s = math.cos(theta), math.sin(theta)
    (x0, _, z0), (x1, _, z1) = shape["min"], shape["max"]
    tx, _, tz = shape["translation"]
    return [(tx + x * c + z * s, tz - x * s + z * c) for x, z in ((x0, z0), (x1, z0), (x1, z1), (x0, z1))]


def plan_view(scene: dict, size: int = 768) -> Plan:
    """Collision, instances, typed markers, navigation and map bounds of one scene."""

    bounds = (scene.get("bounds") or {}).get("map")
    points = [i["translation"] for i in scene.get("instances", [])] + [m["translation"] for m in scene.get("markers", [])]
    if bounds:
        x_range, z_range = bounds["x"], bounds["z"]
    else:
        x_range = [min(p[0] for p in points), max(p[0] for p in points)]
        z_range = [min(p[2] for p in points), max(p[2] for p in points)]
    plan = Plan(x_range, z_range, size)
    floor = scene.get("floor")
    if floor and floor.get("cells"):
        d = floor["dimension"]
        for row in range(d):
            for col in range(d):
                value = floor["cells"][row * d + col] & 0x7F
                x, z = -512 * (col - d // 2), 512 * (row - d // 2)
                px, py = plan.to_pixel(x, z)
                shade = 30 + value
                plan.dot(px, py, (shade, shade, shade + 10, 255), max(0, int(256 * plan.scale) - 1))
    if bounds:
        plan.polygon([(x_range[0], z_range[0]), (x_range[1], z_range[0]), (x_range[1], z_range[1]), (x_range[0], z_range[1])], PLAN_COLOURS["bounds"])
    for instance in scene.get("instances", []):
        plan.dot(*plan.to_pixel(instance["translation"][0], instance["translation"][2]), PLAN_COLOURS["instance"])
    for shape in scene.get("collision", []):
        if shape.get("extras", {}).get("registered") is False:
            colour = PLAN_COLOURS["collision_unregistered"]
        elif shape.get("vertical", {}).get("status") == "proven":
            colour = PLAN_COLOURS["collision_proven"]
        else:
            colour = PLAN_COLOURS["collision_planar"]
        plan.polygon(_corners(shape), colour)
    # Only navigation points are drawn: which point a link record starts from is not established.
    for point in (scene.get("navigation") or {}).get("points", []):
        plan.dot(*plan.to_pixel(point["x"], point["z"]), PLAN_COLOURS["navigation"], 1)
    for marker in scene.get("markers", []):
        group = next((name for name, members in MARKER_GROUPS.items() if marker["category"] in members), "marker")
        plan.dot(*plan.to_pixel(marker["translation"][0], marker["translation"][2]), PLAN_COLOURS[group], 2 if group != "marker" else 1)
    minimap = scene.get("minimap") or {}
    if minimap.get("area0_box"):
        box = minimap["area0_box"]
        plan.polygon([(box["x0"], box["z0"]), (box["x1"], box["z0"]), (box["x1"], box["z1"]), (box["x0"], box["z1"])], PLAN_COLOURS["minimap_box"])
    return plan


def minimap_alignment(scene: dict, texture: dict, variant: str | None = None, zoom: int = 6) -> bytes | None:
    """The selected minimap image enlarged, with every marker and instance plotted by func_800C7C10's mapping."""

    minimap = scene.get("minimap") or {}
    selection = next((s for s in minimap.get("selections", []) if s.get("image") and (variant is None or s["variant"] == variant)), None)
    if not selection or not minimap.get("scale"):
        return None
    width, height = texture["width"], texture["height"]
    canvas = bytearray(width * zoom * height * zoom * 4)
    for y in range(height * zoom):
        for x in range(width * zoom):
            source = ((y // zoom) * width + x // zoom) * 4
            canvas[(y * width * zoom + x) * 4 : (y * width * zoom + x) * 4 + 4] = texture["rgba"][source : source + 4]

    def plot(x: float, z: float, colour, radius: int) -> None:
        px = int((x - minimap["origin"][0]) * minimap["scale"])
        py = int((z - minimap["origin"][1]) * minimap["scale"])
        if not (0 <= px < width and 0 <= py < height):
            return
        cx, cy = px * zoom + zoom // 2, py * zoom + zoom // 2
        for yy in range(cy - radius, cy + radius + 1):
            for xx in range(cx - radius, cx + radius + 1):
                if 0 <= xx < width * zoom and 0 <= yy < height * zoom:
                    canvas[(yy * width * zoom + xx) * 4 : (yy * width * zoom + xx) * 4 + 4] = bytes(colour)

    for instance in scene.get("instances", []):
        plot(instance["translation"][0], instance["translation"][2], (255, 255, 255, 255), 0)
    for marker in scene.get("markers", []):
        group = next((name for name, members in MARKER_GROUPS.items() if marker["category"] in members), None)
        if group:
            plot(marker["translation"][0], marker["translation"][2], PLAN_COLOURS[group], 1)
    return encode_png_rgba(width * zoom, height * zoom, bytes(canvas))
