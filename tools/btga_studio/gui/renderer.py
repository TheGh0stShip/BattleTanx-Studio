"""OpenGL 3.3 core viewport renderer for neutral scenes (no game renderer involved).

Source instances are baked once into world-space vertex batches, one per
texture, so a large map draws in a few hundred calls.  Each baked instance
remembers its vertex ranges; when the overlay edits, selects or removes it,
its ranges are zeroed (degenerate triangles) and it is drawn dynamically
from a per-model buffer instead.  Overlays (grid, axes, collision, markers,
navigation, bounds, selection, placement preview) are a line batch rebuilt
only when the scene version, selection or layer set changes.

The renderer draws into its own framebuffer, so the same code runs in the
GUI and in headless EGL tests.
"""

from __future__ import annotations

import ctypes
import math

import numpy as np
from OpenGL import GL as gl
from OpenGL.raw.GL.VERSION.GL_1_1 import glBindTexture as raw_bind_texture
from OpenGL.raw.GL.VERSION.GL_1_1 import glDrawArrays as raw_draw_arrays
from OpenGL.raw.GL.VERSION.GL_2_0 import glUniform1i as raw_uniform_1i
from OpenGL.raw.GL.VERSION.GL_2_0 import glVertexAttribPointer as raw_vertex_attrib_pointer
from OpenGL.raw.GL.VERSION.GL_3_0 import glBindVertexArray as raw_bind_vertex_array

from tools.btga_studio.core import geometry as g
from tools.btga_studio.core import sky as sky_module
from tools.btga_studio.core.scene import MARKER_HALF, EditorScene, record_box

VERTEX_SHADER = """
#version 330 core
layout(location = 0) in vec3 position;
layout(location = 1) in vec2 uv;
layout(location = 2) in vec4 colour;
uniform mat4 mvp;
uniform mat4 model;
out vec2 v_uv;
out vec4 v_colour;
out float v_depth;
void main() {
    vec4 world = model * vec4(position, 1.0);
    gl_Position = mvp * world;
    v_uv = uv;
    v_colour = colour;
    v_depth = gl_Position.w;
}
"""
FRAGMENT_SHADER = """
#version 330 core
in vec2 v_uv;
in vec4 v_colour;
in float v_depth;
uniform sampler2D tex;
uniform int textured;
uniform vec4 tint;
uniform vec3 fog_colour;
uniform float fog_far;
out vec4 frag;
void main() {
    vec4 base = textured == 1 ? texture(tex, v_uv) : vec4(1.0);
    if (textured == 1 && base.a < 0.5) discard;
    vec4 colour = base * v_colour * tint;
    float fog = clamp(v_depth / fog_far, 0.0, 0.85);
    frag = vec4(mix(colour.rgb, fog_colour, fog * fog), colour.a);
}
"""
FLOATS = 9  # position 3, uv 2, colour 4
LAYER_COLOURS = {
    "collision_proven": (0.95, 0.25, 0.25, 1.0),
    "collision_planar": (1.0, 0.6, 0.15, 1.0),
    "collision_inactive": (0.45, 0.45, 0.45, 1.0),
    "marker": (0.3, 0.6, 1.0, 1.0),
    "marker_unresolved": (0.75, 0.3, 0.75, 1.0),
    "navigation": (0.2, 0.85, 0.85, 1.0),
    "bounds": (1.0, 1.0, 1.0, 1.0),
    "grid": (0.32, 0.32, 0.38, 1.0),
    "selection": (1.0, 0.95, 0.2, 1.0),
    "preview": (0.3, 1.0, 0.4, 1.0),
    "disabled": (0.5, 0.5, 0.5, 1.0),
    "trigger_volume": (1.0, 0.85, 0.2, 1.0),
    "capture_zone": (0.35, 0.65, 1.0, 1.0),
    "hazard": (1.0, 0.3, 0.25, 1.0),
    "teleporter": (0.8, 0.4, 1.0, 1.0),
    "spawn": (0.35, 1.0, 0.45, 1.0),
    "bot": (1.0, 0.45, 0.85, 1.0),
    "reachable": (0.3, 1.0, 0.4, 1.0),
    "unreachable": (1.0, 0.3, 0.3, 1.0),
}
VOLUME_CATEGORIES = ("trigger_volume", "capture_zone", "hazard", "teleporter")
SPAWN_ARROW_CATEGORIES = ("player_spawn", "team_start", "team_spawn_slot", "bot_slot")


def _compile(source: str, kind) -> int:
    shader = gl.glCreateShader(kind)
    gl.glShaderSource(shader, source)
    gl.glCompileShader(shader)
    if not gl.glGetShaderiv(shader, gl.GL_COMPILE_STATUS):
        raise RuntimeError(gl.glGetShaderInfoLog(shader).decode())
    return shader


def mesh_vertices(mesh, translation=None, yaw=0, scale=None) -> dict[str | None, np.ndarray]:
    """Interleaved float32 vertices per texture for one mesh, optionally in world space."""

    corners = mesh.corners
    if translation is not None:
        if scale is not None:
            corners = corners * np.asarray(scale, float)
        corners = corners @ g.yaw_matrix3(yaw).T + np.asarray(translation, float)
    out = {}
    for material_index in np.unique(mesh.triangle_materials) if mesh.triangle_count else []:
        selected = mesh.triangle_materials == material_index
        texture = mesh.materials[int(material_index)].get("texture")
        block = np.empty((int(selected.sum()) * 3, FLOATS), np.float32)
        block[:, 0:3] = corners[selected].reshape(-1, 3)
        block[:, 3:5] = mesh.uvs[selected].reshape(-1, 2)
        block[:, 5:9] = mesh.colours[selected].reshape(-1, 4)
        out.setdefault(texture, []).append(block)
    return {key: np.concatenate(blocks) for key, blocks in out.items()}


class Batch:
    def __init__(self, texture: str | None, vertices: np.ndarray):
        self.texture = texture
        self.count = len(vertices)
        self.vao = gl.glGenVertexArrays(1)
        self.vbo = gl.glGenBuffers(1)
        gl.glBindVertexArray(self.vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, vertices.nbytes, vertices if len(vertices) else None, gl.GL_DYNAMIC_DRAW)
        stride = FLOATS * 4
        for location, size, offset in ((0, 3, 0), (1, 2, 12), (2, 4, 20)):
            gl.glEnableVertexAttribArray(location)
            # The raw entry point: PyOpenGL's wrapper also files the pointer under the current context, which fails
            # where it cannot identify that context (WSLg); these are offsets into our own buffer, so nothing needs keeping.
            raw_vertex_attrib_pointer(location, size, gl.GL_FLOAT, gl.GL_FALSE, stride, ctypes.c_void_p(offset))
        gl.glBindVertexArray(0)

    def write(self, start: int, vertices: np.ndarray) -> None:
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.vbo)
        gl.glBufferSubData(gl.GL_ARRAY_BUFFER, start * FLOATS * 4, vertices.nbytes, vertices)

    def draw(self, mode=gl.GL_TRIANGLES) -> None:
        # Raw entry points: these run thousands of times a frame, and the wrappers' per-call glGetError dominates.
        if self.count:
            raw_bind_vertex_array(self.vao)
            raw_draw_arrays(mode, 0, self.count)

    def delete(self) -> None:
        gl.glDeleteBuffers(1, [self.vbo])
        gl.glDeleteVertexArrays(1, [self.vao])


def authoring_segments(scene, hidden=frozenset()) -> list:
    """Authoring overlay lines: trigger volumes, zones, damage areas and teleporter pads as their real extent, spawn and
    bot facing arrows, each bot's line to its target and its patrol route.  Pure function of the scene (tested without OpenGL)."""

    out: list = []
    records = {record["id"]: record for _, record in scene.records("markers") if not record.get("disabled") and record["id"] not in hidden}

    def ring(x, y, z, radius, colour, steps=40):
        points = [(x + radius * math.cos(2 * math.pi * i / steps), y + 4.0, z + radius * math.sin(2 * math.pi * i / steps)) for i in range(steps + 1)]
        for a, b in zip(points[:-1], points[1:]):
            out.extend([(a, colour), (b, colour)])

    for record_id in sorted(records):
        record = records[record_id]
        category = record.get("category")
        fields = record.get("fields", {})
        x, y, z = (float(v) for v in record["translation"])
        if category in VOLUME_CATEGORIES:
            colour = LAYER_COLOURS[category]
            if category == "trigger_volume" and (fields.get("shape") or {}).get("value") == 1:
                hx = float((fields.get("half_x") or {}).get("value") or 100)
                hz = float((fields.get("half_z") or {}).get("value") or 100)
                height = float((fields.get("height") or {}).get("value") or 100)
                theta = g.yaw_radians(record.get("yaw", 0))
                c, s_ = math.cos(theta), math.sin(theta)
                corners = [(x + a * c + b * s_, z - a * s_ + b * c) for a, b in ((-hx, -hz), (hx, -hz), (hx, hz), (-hx, hz))]
                for level in (y + 4.0, y + height):
                    for (ax, az), (bx, bz) in zip(corners, corners[1:] + corners[:1]):
                        out.extend([((ax, level, az), colour), ((bx, level, bz), colour)])
                for ax, az in corners:
                    out.extend([((ax, y + 4.0, az), colour), ((ax, y + height, az), colour)])
            else:
                ring(x, y, z, float((fields.get("radius") or {}).get("value") or 90), colour)
        if category in SPAWN_ARROW_CATEGORIES:
            colour = LAYER_COLOURS["bot" if category == "bot_slot" else "spawn"]
            theta = g.yaw_radians(record.get("yaw", 0))
            forward = (-math.sin(theta), -math.cos(theta))
            side = (-forward[1], forward[0])
            tip = (x + forward[0] * 140, y + 12.0, z + forward[1] * 140)
            out.extend([((x, y + 12.0, z), colour), (tip, colour)])
            for sign in (1, -1):
                out.extend([(tip, colour), ((tip[0] - forward[0] * 40 + sign * side[0] * 25, tip[1], tip[2] - forward[1] * 40 + sign * side[1] * 25), colour)])
        if category == "bot_slot":
            colour = LAYER_COLOURS["bot"]
            for name in ("patrol_start", "guard_target", "capture_target", "escort_target"):
                target = records.get((fields.get(name) or {}).get("value"))
                if target is not None:
                    tx, ty, tz = target["translation"]
                    out.extend([((x, y + 30.0, z), colour), ((tx, ty + 30.0, tz), colour)])
            node = records.get((fields.get("patrol_start") or {}).get("value"))
            seen = set()
            while node is not None and node["id"] not in seen and len(seen) < 64:
                seen.add(node["id"])
                following = records.get((node.get("fields", {}).get("link_0") or {}).get("value"))
                if following is None:
                    break
                a, b = node["translation"], following["translation"]
                out.extend([((a[0], a[1] + 20.0, a[2]), colour), ((b[0], b[1] + 20.0, b[2]), colour)])
                node = following
    return out


def navigation_segments(scene, hidden=frozenset(), reach: set | None = None) -> list:
    """Navigation overlay lines: source navigation points, plus authored ``navigation_node`` markers and their links.

    Blank Studio maps have no source navigation table; their graph lives in the
    markers' ``link_*`` fields (the same rule the package export uses), so it is
    drawn from there.  Pure function of the scene, so it is tested without OpenGL.
    """

    colour = LAYER_COLOURS["navigation"]
    out: list = []

    def cross(x, y, z, node_colour=None):
        node_colour = node_colour or colour
        p = np.array([x, y, z], float)
        for d in ((16, 0, 0), (0, 0, 16), (0, 32, 0)):
            out.extend([(tuple(p - d), node_colour), (tuple(p + d), node_colour)])

    for point in (scene.navigation or {}).get("points", []):
        cross(point["x"], 0.0, point["z"])
    nodes = {record["id"]: record for _, record in scene.records("markers")
             if record.get("category") == "navigation_node" and not record.get("disabled") and record["id"] not in hidden}
    for record_id in sorted(nodes):
        record = nodes[record_id]
        x, y, z = record["translation"]
        cross(x, y, z, None if reach is None else LAYER_COLOURS["reachable" if record_id in reach else "unreachable"])
        for name in sorted(record.get("fields", {})):
            target = nodes.get(record["fields"][name].get("value")) if name.startswith("link_") else None
            if target is not None:
                tx, ty, tz = target["translation"]
                out.extend([((x, y + 8.0, z), colour), ((tx, ty + 8.0, tz), colour)])
    return out


class ViewportRenderer:
    def __init__(self, library):
        self.library = library
        self.program = gl.glCreateProgram()
        gl.glAttachShader(self.program, _compile(VERTEX_SHADER, gl.GL_VERTEX_SHADER))
        gl.glAttachShader(self.program, _compile(FRAGMENT_SHADER, gl.GL_FRAGMENT_SHADER))
        gl.glLinkProgram(self.program)
        if not gl.glGetProgramiv(self.program, gl.GL_LINK_STATUS):
            raise RuntimeError(gl.glGetProgramInfoLog(self.program).decode())
        self.uniforms = {name: gl.glGetUniformLocation(self.program, name)
                         for name in ("mvp", "model", "tex", "textured", "tint", "fog_colour", "fog_far")}
        self.fbo = self.colour = self.depth = None
        self.size = (0, 0)
        self.textures: dict[str, int] = {}
        self.white = self._upload_texture(1, 1, b"\xff\xff\xff\xff", "repeat", "repeat")
        self.scene: EditorScene | None = None
        self.batches: list[Batch] = []
        self.ranges: dict[str, list[tuple[Batch, int, np.ndarray]]] = {}
        self.hidden: set[str] = set()
        self.user_hidden: set[str] = set()  # records the user hid (never drawn)
        self.model_batches: dict[str, list[Batch]] = {}
        self.lines: Batch | None = None
        self.lines_key = None
        self.background = (0.12, 0.13, 0.16)
        self.wireframe = False
        self.layers = {"geometry": True, "collision": True, "markers": True, "navigation": True, "bounds": True, "grid": True, "unresolved": True}
        self.draw_calls = 0
        self.authoring_overlays = True  # trigger volumes, zones, spawn and bot arrows, bot targets (authoring_segments)
        self.reach: set | None = None  # navigation nodes reachable from the Drive Preview tank (others are drawn red)
        self.marker_model = None  # callable(record) -> model ID drawn at a marker (bots, targets, item pickups), or None
        self.extras: list[tuple] = []  # (model id, translation, yaw units, scale) drawn on top of the scene, such as the Drive Preview tank

    # -- resources --------------------------------------------------------------
    def _upload_texture(self, width, height, rgba: bytes, wrap_s: str, wrap_t: str) -> int:
        texture = gl.glGenTextures(1)
        gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, width, height, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, rgba)
        modes = {"repeat": gl.GL_REPEAT, "mirror": gl.GL_MIRRORED_REPEAT, "clamp": gl.GL_CLAMP_TO_EDGE}
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, modes.get(wrap_s, gl.GL_REPEAT))
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, modes.get(wrap_t, gl.GL_REPEAT))
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
        return texture

    def texture(self, texture_id: str | None) -> int:
        if texture_id is None:
            return self.white
        if texture_id not in self.textures:
            try:
                tex = self.library.texture(texture_id)
                self.textures[texture_id] = self._upload_texture(tex.width, tex.height, tex.rgba, tex.wrap.get("s"), tex.wrap.get("t"))
            except Exception:
                self.textures[texture_id] = self.white
        return self.textures[texture_id]

    def ensure_target(self, width: int, height: int) -> None:
        width, height = max(1, int(width)), max(1, int(height))
        if (width, height) == self.size and self.fbo:
            return
        if self.fbo:
            gl.glDeleteFramebuffers(1, [self.fbo])
            gl.glDeleteTextures(1, [self.colour])
            gl.glDeleteRenderbuffers(1, [self.depth])
        self.fbo = gl.glGenFramebuffers(1)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.fbo)
        self.colour = gl.glGenTextures(1)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.colour)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, width, height, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, None)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
        gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0, gl.GL_TEXTURE_2D, self.colour, 0)
        self.depth = gl.glGenRenderbuffers(1)
        gl.glBindRenderbuffer(gl.GL_RENDERBUFFER, self.depth)
        gl.glRenderbufferStorage(gl.GL_RENDERBUFFER, gl.GL_DEPTH_COMPONENT24, width, height)
        gl.glFramebufferRenderbuffer(gl.GL_FRAMEBUFFER, gl.GL_DEPTH_ATTACHMENT, gl.GL_RENDERBUFFER, self.depth)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
        self.size = (width, height)

    # -- scene ------------------------------------------------------------------
    def load_scene(self, scene: EditorScene) -> None:
        self.unload()
        self.scene = scene
        environment = next((e for e in scene.base.get("environment", []) if "clear_and_fog_colour" in e), None)
        if environment:
            self.background = tuple(v / 255 for v in environment["clear_and_fog_colour"])
        per_texture: dict[str | None, list[tuple[str, np.ndarray]]] = {}
        # Bake every model present now (source records and ones added in Studio) into per-texture batches;
        # only records edited, selected or removed later are drawn one by one.
        self.baked: dict[str, dict] = {}
        for record_id, (family, record) in scene.view.items():
            if family != "instances":
                continue
            self.baked[record_id] = record
            mesh = scene.meshes.get(record["model"])
            for texture, vertices in mesh_vertices(mesh, record["translation"], record["yaw"], record.get("scale")).items():
                per_texture.setdefault(texture, []).append((record_id, vertices))
        for texture, parts in sorted(per_texture.items(), key=lambda item: str(item[0])):
            batch = Batch(texture, np.concatenate([v for _, v in parts]))
            self.texture(texture)
            start = 0
            for record_id, vertices in parts:
                self.ranges.setdefault(record_id, []).append((batch, start, vertices))
                start += len(vertices)
            self.batches.append(batch)
        self.lines_key = None

    def unload(self) -> None:
        for batch in self.batches:
            batch.delete()
        for batches in self.model_batches.values():
            for batch in batches:
                batch.delete()
        if self.lines:
            self.lines.delete()
        self.batches, self.ranges, self.hidden, self.model_batches, self.lines = [], {}, set(), {}, None
        self.baked = {}
        self.scene = None

    def _sync_dynamic(self, live: set[str]) -> None:
        """Hide baked copies of instances drawn dynamically; restore the rest."""

        for record_id in live - self.hidden:
            for batch, start, vertices in self.ranges.get(record_id, []):
                batch.write(start, np.zeros_like(vertices))
        for record_id in self.hidden - live:
            for batch, start, vertices in self.ranges.get(record_id, []):
                batch.write(start, vertices)
        self.hidden = live & set(self.ranges)

    def dynamic_instances(self, selection: set[str]) -> set[str]:
        key = (self.scene.version, tuple(sorted(selection)))
        if getattr(self, "_dynamic_key", None) == key:
            return self._dynamic
        self._dynamic_key, self._dynamic = key, self._dynamic_instances(selection)
        return self._dynamic

    def _dynamic_instances(self, selection: set[str]) -> set[str]:
        scene = self.scene
        live = set()
        for record_id, (family, record) in scene.view.items():
            if family != "instances":
                continue
            baked = self.baked.get(record_id)
            if baked is None or record is not baked or record_id in selection:
                live.add(record_id)
        live |= {r for r in self.ranges if scene.record(r) is None}
        return live

    def _model_batches(self, model_id: str) -> list[Batch]:
        if model_id not in self.model_batches:
            mesh = self.scene.meshes.get(model_id)
            self.model_batches[model_id] = [Batch(texture, vertices) for texture, vertices in sorted(mesh_vertices(mesh).items(), key=lambda i: str(i[0]))]
            for batch in self.model_batches[model_id]:
                self.texture(batch.texture)
        return self.model_batches[model_id]

    # -- overlay lines -------------------------------------------------------------
    def _box_lines(self, out: list, lo, hi, translation, yaw, colour) -> None:
        """Queue one box; boxes are expanded together in _expand_boxes."""

        out.append(("box", lo, hi, translation, yaw, colour))

    @staticmethod
    def _expand_boxes(boxes: list) -> tuple[np.ndarray, np.ndarray]:
        if not boxes:
            return np.zeros((0, 3)), np.zeros((0, 4))
        lo = np.array([b[1] for b in boxes], float)
        hi = np.array([b[2] for b in boxes], float)
        translation = np.array([b[3] for b in boxes], float)
        theta = np.array([(int(b[4]) & 0xFFFF) for b in boxes], float) * (2 * math.pi / 65536)
        c, s_ = np.cos(theta), np.sin(theta)
        selector = np.array([[x, y, z] for x in (0, 1) for y in (0, 1) for z in (0, 1)], float)  # 8 corners
        corners = lo[:, None, :] + selector[None, :, :] * (hi - lo)[:, None, :]
        x, y, z = corners[..., 0], corners[..., 1], corners[..., 2]
        world = np.stack([x * c[:, None] + z * s_[:, None], y, -x * s_[:, None] + z * c[:, None]], axis=-1) + translation[:, None, :]
        edges = np.array([(0, 1), (2, 3), (4, 5), (6, 7), (0, 2), (1, 3), (4, 6), (5, 7), (0, 4), (1, 5), (2, 6), (3, 7)]).reshape(-1)
        points = world[:, edges, :].reshape(-1, 3)
        colours = np.repeat(np.array([b[5] for b in boxes], float), len(edges), axis=0)
        return points, colours

    def build_lines(self, selection: set[str], preview: dict | None = None) -> np.ndarray:
        scene = self.scene
        segments: list = []
        layers = self.layers
        lo, hi = scene.world_bounds()
        if layers.get("grid"):
            step = 10 ** math.floor(math.log10(max(hi[0] - lo[0], hi[2] - lo[2], 10) / 10))
            for x in np.arange(math.floor(lo[0] / step) * step, hi[0] + step, step):
                segments += [((x, 0, lo[2]), LAYER_COLOURS["grid"]), ((x, 0, hi[2]), LAYER_COLOURS["grid"])]
            for z in np.arange(math.floor(lo[2] / step) * step, hi[2] + step, step):
                segments += [((lo[0], 0, z), LAYER_COLOURS["grid"]), ((hi[0], 0, z), LAYER_COLOURS["grid"])]
            for axis, colour in ((0, (1, 0.2, 0.2, 1)), (1, (0.2, 1, 0.2, 1)), (2, (0.3, 0.4, 1, 1))):
                end = [0.0, 0.0, 0.0]
                end[axis] = step * 2
                segments += [((0, 0, 0), colour), (tuple(end), colour)]
        bounds = scene.bounds
        if layers.get("bounds") and bounds:
            x0, x1, z0, z1 = bounds["x"][0], bounds["x"][1], bounds["z"][0], bounds["z"][1]
            for a, b in (((x0, z0), (x1, z0)), ((x1, z0), (x1, z1)), ((x1, z1), (x0, z1)), ((x0, z1), (x0, z0))):
                segments += [((a[0], 0, a[1]), LAYER_COLOURS["bounds"]), ((b[0], 0, b[1]), LAYER_COLOURS["bounds"])]
        for family, record in scene.records():
            if record["id"] in self.user_hidden:
                continue
            selected = record["id"] in selection
            if family == "collision" and (layers.get("collision") or selected):
                if record.get("disabled") or record.get("extras", {}).get("registered") is False:
                    colour = LAYER_COLOURS["collision_inactive"]
                elif record.get("vertical", {}).get("status") == "proven":
                    colour = LAYER_COLOURS["collision_proven"]
                else:
                    colour = LAYER_COLOURS["collision_planar"]
                box_lo, box_hi = record_box(record, family, None)
                self._box_lines(segments, box_lo, box_hi, record["translation"], record["yaw"], LAYER_COLOURS["selection"] if selected else colour)
            elif family == "markers":
                unresolved = record.get("confidence") == "unresolved"
                if not (layers.get("markers") or selected) or (unresolved and not layers.get("unresolved")):
                    continue
                colour = LAYER_COLOURS["disabled"] if record.get("disabled") else (LAYER_COLOURS["marker_unresolved"] if unresolved else LAYER_COLOURS["marker"])
                self._box_lines(segments, (-MARKER_HALF, 0, -MARKER_HALF), (MARKER_HALF, 4 * MARKER_HALF, MARKER_HALF), record["translation"], record["yaw"],
                                LAYER_COLOURS["selection"] if selected else colour)
                theta = g.yaw_radians(record["yaw"])
                base = record["translation"]
                tip = (base[0] - 3 * MARKER_HALF * math.sin(theta), base[1], base[2] - 3 * MARKER_HALF * math.cos(theta))
                segments += [(tuple(base), colour), (tip, colour)]
            elif family == "instances" and selected:
                mesh = scene.meshes.get(record["model"])
                scale = np.asarray(record.get("scale") or (1.0, 1.0, 1.0), float)
                self._box_lines(segments, mesh.lo * scale, mesh.hi * scale, record["translation"], record["yaw"], LAYER_COLOURS["selection"])
        if layers.get("navigation"):
            segments += navigation_segments(scene, self.user_hidden, self.reach)
        if layers.get("markers", True) and self.authoring_overlays:
            segments += authoring_segments(scene, self.user_hidden)
        if preview:
            self._box_lines(segments, preview["lo"], preview["hi"], preview["translation"], preview.get("yaw", 0), LAYER_COLOURS["preview"])
        boxes = [s for s in segments if len(s) == 6 and s[0] == "box"]
        plain = [s for s in segments if not (len(s) == 6 and s[0] == "box")]
        box_points, box_colours = self._expand_boxes(boxes)
        count = len(plain) + len(box_points)
        out = np.zeros((count, FLOATS), np.float32)
        if plain:
            out[: len(plain), 0:3] = np.array([s[0] for s in plain], np.float32)
            out[: len(plain), 5:9] = np.array([s[1] for s in plain], np.float32)
        if len(box_points):
            out[len(plain):, 0:3] = box_points
            out[len(plain):, 5:9] = box_colours
        return out

    MODEL_MARKERS = ("bot_slot", "destructible_target", "item_pickup",
                     # original game records that create an object from a model of their own (see AuthoringUI.marker_models)
                     "destructible", "building", "structure", "turret", "turret_emplacement", "turret_random", "static_prop", "foliage",
                     "effect_prop", "multiplayer_structure", "powerup_pad", "crate", "objective", "mission_trigger")

    def _model_markers(self) -> list[dict]:
        """Enabled markers that are drawn with a chosen model (cached per scene version)."""

        key = (id(self.scene), self.scene.version)
        if getattr(self, "_model_markers_key", None) != key:
            self._model_markers_cache = [r for _, r in self.scene.records("markers") if r.get("category") in self.MODEL_MARKERS
                                         and not r.get("disabled")]
            self._model_markers_key = key
        return self._model_markers_cache

    # -- frame ---------------------------------------------------------------------
    def render(self, camera: g.Camera, width: int, height: int, selection: set[str] = frozenset(), preview: dict | None = None) -> int:
        self.ensure_target(width, height)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.fbo)
        gl.glViewport(0, 0, *self.size)
        gl.glClearColor(*self.background, 1.0)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDisable(gl.GL_CULL_FACE)
        self.draw_calls = 0
        if self.scene is None:
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
            return self.colour
        gl.glUseProgram(self.program)
        view_projection = camera.projection(self.size[0] / self.size[1]) @ camera.view()
        gl.glUniformMatrix4fv(self.uniforms["mvp"], 1, True, view_projection.astype(np.float32))
        gl.glUniform1i(self.uniforms["tex"], 0)
        gl.glUniform3f(self.uniforms["fog_colour"], *self.background)
        gl.glUniform1f(self.uniforms["fog_far"], float(camera.far))
        gl.glUniform4f(self.uniforms["tint"], 1, 1, 1, 1)
        identity = np.identity(4, dtype=np.float32)
        gl.glUniformMatrix4fv(self.uniforms["model"], 1, True, identity)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        live = self.dynamic_instances(set(selection)) | (self.user_hidden & set(self.ranges))
        self._sync_dynamic(live)
        gl.glDepthFunc(gl.GL_LEQUAL)
        gl.glEnable(gl.GL_POLYGON_OFFSET_FILL)
        gl.glPolygonOffset(1.0, 2.0)  # filled surfaces sit behind coincident overlay lines
        if self.wireframe:
            gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_LINE)
        sky = self.sky_record()
        if sky and isinstance(camera, g.Camera) and self.layers.get("geometry", True):
            self._draw_sky(sky, camera)
        if self.layers.get("geometry", True):
            for batch in self.batches:
                raw_bind_texture(gl.GL_TEXTURE_2D, self.texture(batch.texture))
                raw_uniform_1i(self.uniforms["textured"], 1 if batch.texture else 0)
                batch.draw()
                self.draw_calls += 1
            for record_id in sorted(live):
                entry = self.scene.record(record_id)
                if entry is None or record_id in self.user_hidden:
                    continue
                record = entry[1]
                gl.glUniformMatrix4fv(self.uniforms["model"], 1, True,
                                      g.model_matrix(record["translation"], record["yaw"], record.get("scale")).astype(np.float32))
                tint = (0.5, 0.5, 0.5, 1.0) if record.get("disabled") else ((1.0, 1.0, 0.6, 1.0) if record_id in selection else (1, 1, 1, 1))
                gl.glUniform4f(self.uniforms["tint"], *tint)
                for batch in self._model_batches(record["model"]):
                    raw_bind_texture(gl.GL_TEXTURE_2D, self.texture(batch.texture))
                    raw_uniform_1i(self.uniforms["textured"], 1 if batch.texture else 0)
                    batch.draw()
                    self.draw_calls += 1
            if self.marker_model is not None and self.layers.get("markers", True):
                for record in self._model_markers():
                    if record["id"] in self.user_hidden:
                        continue
                    tint = (1.0, 1.0, 0.6, 1.0) if record["id"] in selection else (1, 1, 1, 1)
                    for model_id in self.marker_model(record) or []:
                        if self.library is None or model_id not in self.library._models:
                            continue
                        gl.glUniformMatrix4fv(self.uniforms["model"], 1, True, g.model_matrix(record["translation"], record["yaw"], None).astype(np.float32))
                        gl.glUniform4f(self.uniforms["tint"], *tint)
                        for batch in self._model_batches(model_id):
                            raw_bind_texture(gl.GL_TEXTURE_2D, self.texture(batch.texture))
                            raw_uniform_1i(self.uniforms["textured"], 1 if batch.texture else 0)
                            batch.draw()
                            self.draw_calls += 1
            for model_id, translation, yaw, scale in self.extras:
                gl.glUniformMatrix4fv(self.uniforms["model"], 1, True, g.model_matrix(translation, yaw, scale).astype(np.float32))
                gl.glUniform4f(self.uniforms["tint"], 1, 1, 1, 1)
                for batch in self._model_batches(model_id):
                    gl.glBindTexture(gl.GL_TEXTURE_2D, self.texture(batch.texture))
                    gl.glUniform1i(self.uniforms["textured"], 1 if batch.texture else 0)
                    batch.draw()
                    self.draw_calls += 1
            gl.glUniformMatrix4fv(self.uniforms["model"], 1, True, identity)
            gl.glUniform4f(self.uniforms["tint"], 1, 1, 1, 1)
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
        gl.glDisable(gl.GL_POLYGON_OFFSET_FILL)
        key = (self.scene.version, tuple(sorted(selection)), tuple(sorted(self.layers.items())), repr(preview), tuple(sorted(self.user_hidden)),
               self.authoring_overlays, None if self.reach is None else tuple(sorted(self.reach)))
        if key != self.lines_key:
            if self.lines:
                self.lines.delete()
            self.lines = Batch(None, self.build_lines(set(selection), preview))
            self.lines_key = key
        gl.glUniform1i(self.uniforms["textured"], 0)
        self.lines.draw(gl.GL_LINES)
        self.draw_calls += 1
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
        return self.colour

    def sky_record(self) -> dict | None:
        environment = (self.scene.base.get("environment") or [{}]) if self.scene else [{}]
        sky = environment[0].get("sky") if environment and isinstance(environment[0], dict) else None
        return sky if sky and sky.get("texture") in getattr(self.library, "_textures", {}) else None

    def _draw_sky(self, sky: dict, camera) -> None:
        """The panorama band around the camera, behind everything and without fog."""

        radius = float(camera.far) * 0.45
        key = (tuple(sorted(sky.items())), radius)
        if getattr(self, "_sky_key", None) != key:
            if getattr(self, "_sky_batch", None) is not None:
                self._sky_batch.delete()
            self._sky_batch = Batch(sky["texture"], sky_module.vertices(sky, radius))
            self._sky_key = key
            gl.glBindTexture(gl.GL_TEXTURE_2D, self.texture(sky["texture"]))
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_REPEAT)
        gl.glUniformMatrix4fv(self.uniforms["model"], 1, True, g.model_matrix(tuple(camera.position), 0).astype(np.float32))
        gl.glUniform1f(self.uniforms["fog_far"], 1e12)
        gl.glUniform1i(self.uniforms["textured"], 1)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.texture(sky["texture"]))
        gl.glDepthMask(gl.GL_FALSE)
        gl.glDisable(gl.GL_DEPTH_TEST)
        self._sky_batch.draw()
        self.draw_calls += 1
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDepthMask(gl.GL_TRUE)
        gl.glUniform1f(self.uniforms["fog_far"], float(camera.far))
        gl.glUniformMatrix4fv(self.uniforms["model"], 1, True, np.identity(4, dtype=np.float32))

    def use_target(self, name: str) -> None:
        """Switch between named render targets (the viewport is "main")."""

        targets = self.__dict__.setdefault("_targets", {})
        current = self.__dict__.get("_target_name", "main")
        if name == current:
            return
        targets[current] = (self.fbo, self.colour, self.depth, self.size)
        self.fbo, self.colour, self.depth, self.size = targets.get(name, (None, None, None, (0, 0)))
        self._target_name = name

    def render_top_down(self, size: int, selection: set[str] = frozenset()) -> tuple[int, "g.TopDownCamera"] | None:
        """Overhead orthographic picture of the map limits into the "minimap" target."""

        bounds = self.scene.bounds if self.scene else None
        if not bounds:
            return None
        camera = g.TopDownCamera(bounds["x"], bounds["z"])
        self.use_target("minimap")
        try:
            texture = self.render(camera, size, size, selection)  # the overlay line set is camera-independent and shared
        finally:
            self.use_target("main")
        return texture, camera

    def read_pixels(self) -> bytes:
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.fbo)
        data = gl.glReadPixels(0, 0, self.size[0], self.size[1], gl.GL_RGBA, gl.GL_UNSIGNED_BYTE)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
        rows = [data[i * self.size[0] * 4 : (i + 1) * self.size[0] * 4] for i in range(self.size[1])]
        return b"".join(reversed(rows))
