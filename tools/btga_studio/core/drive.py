"""Drive Preview: an editor approximation for checking a map's layout and scale with a tank-sized body.

It is not the game.  It runs none of the game's physics, AI, combat,
destruction, pickups or scripting; final behaviour needs the PC runtime
(``core.runtime``, "Test in Game").  What it does check, for authoring:

* the tank follows the ground by casting down onto the map's models
  (terrain, bridges, ramps and rooftops), climbs slopes up to a limit and
  reports where it is too steep;
* collision boxes and solid upright geometry block it; ground marked as
  water (``surface: "water"``) is not driven into; the map limits hold;
* areas it enters are reported as notices, never acted on: a teleporter pad
  names its exit (``jump_to_exit`` is an explicit editor jump for checking
  exits, not a game teleport), a pickup says it is not collected, trigger
  volumes, zones and damage areas say that only the game runs them.

Tank size, speed and handling are settings; nothing here changes the map.

Coordinates and yaw follow the scene: +Y up, yaw about +Y, forward is
(-sin yaw, 0, -cos yaw) as for the camera and for models facing -Z.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from . import geometry as g

UNITS_PER_METRE = 16.0


@dataclass
class DriveSettings:
    tank_length_m: float = 7.5
    tank_width_m: float = 3.6
    tank_height_m: float = 3.0
    top_speed_m_s: float = 14.0
    reverse_speed_m_s: float = 6.0
    acceleration_m_s2: float = 9.0
    braking_m_s2: float = 22.0
    turn_rate_deg_s: float = 90.0
    step_m: float = 0.5  # highest kerb or ledge it drives over (a roof parapet is about 0.9 m)
    max_slope_deg: float = 40.0
    gravity_m_s2: float = 19.6
    pickup_radius_m: float = 5.0  # how close counts as "at" a pickup for the notice

    def units(self, metres: float) -> float:
        return metres * UNITS_PER_METRE


@dataclass
class DriveEvent:
    kind: str  # notice | jumped | blocked | fell | reset
    text: str
    record: str | None = None


@dataclass
class TankState:
    position: np.ndarray
    yaw: float  # radians
    speed: float = 0.0  # units per second along forward
    vertical_speed: float = 0.0
    on_ground: bool = True
    pitch: float = 0.0
    distance: float = 0.0
    inside: set = field(default_factory=set)  # areas (marker IDs) the tank is in, so each notice is shown once per entry


class WallIndex:
    """Upright solid surfaces as wall segments in plan (with their height range), binned like ``HeightIndex``."""

    def __init__(self, tris: np.ndarray, cell: float):
        self.cell = cell
        flat = tris[:, :, [0, 2]]
        # An upright triangle projects onto a line: keep its two farthest-apart corners in plan.
        pairs = ((0, 1), (1, 2), (2, 0))
        spans = np.stack([np.linalg.norm(flat[:, a] - flat[:, b], axis=1) for a, b in pairs], axis=1)
        best = np.argmax(spans, axis=1) if len(tris) else np.zeros(0, int)
        first = np.array([p[0] for p in pairs])[best]
        second = np.array([p[1] for p in pairs])[best]
        rows = np.arange(len(tris))
        self.a = flat[rows, first]
        self.b = flat[rows, second]
        self.low = tris[:, :, 1].min(axis=1)
        self.high = tris[:, :, 1].max(axis=1)
        keep = np.linalg.norm(self.b - self.a, axis=1) > 1e-3
        self.a, self.b, self.low, self.high = self.a[keep], self.b[keep], self.low[keep], self.high[keep]
        lo = np.floor(np.minimum(self.a, self.b) / cell).astype(np.int64)
        hi = np.floor(np.maximum(self.a, self.b) / cell).astype(np.int64)
        span = hi - lo + 1
        count = span[:, 0] * span[:, 1]
        owners = np.repeat(np.arange(len(self.a)), count)
        start = np.repeat(np.cumsum(count) - count, count)
        local = np.arange(len(owners)) - start
        width = span[owners, 0]
        keys = HeightIndex._key(lo[owners, 0] + local % width, lo[owners, 1] + local // width)
        order = np.argsort(keys, kind="stable")
        self.keys, self.owners = keys[order], owners[order]

    def push(self, x: float, z: float, radius: float, bottom: float, top: float) -> np.ndarray | None:
        """Push-out (x, z) for a circle at (x, z) touching walls that overlap the height range [bottom, top]."""

        cells = []
        for cx in range(math.floor((x - radius) / self.cell), math.floor((x + radius) / self.cell) + 1):
            for cz in range(math.floor((z - radius) / self.cell), math.floor((z + radius) / self.cell) + 1):
                key = int(HeightIndex._key(cx, cz))
                start, end = np.searchsorted(self.keys, [key, key + 1])
                if start < end:
                    cells.append(self.owners[start:end])
        if not cells:
            return None
        index = np.unique(np.concatenate(cells))
        index = index[(self.high[index] > bottom) & (self.low[index] < top)]
        if not len(index):
            return None
        a, b = self.a[index], self.b[index]
        d = b - a
        t = np.clip(((x - a[:, 0]) * d[:, 0] + (z - a[:, 1]) * d[:, 1]) / np.einsum("ij,ij->i", d, d), 0.0, 1.0)
        nearest = a + d * t[:, None]
        offset = np.array([x, z]) - nearest
        distance = np.linalg.norm(offset, axis=1)
        touching = distance < radius
        if not touching.any():
            return None
        push = np.zeros(2)
        for o, dist in zip(offset[touching], distance[touching]):
            push += (o / dist if dist > 1e-6 else np.array([1.0, 0.0])) * (radius - dist) / max(1, touching.sum())
        return push


class HeightIndex:
    """Walkable (non-vertical) triangles of every placed model, binned into a 2D grid for fast vertical probes."""

    def __init__(self, scene, cell: float = 256.0):
        self.cell = float(cell)
        triangles, solid, wet = [], [], []
        for _, record in scene.records("instances"):
            mesh = scene.meshes.get(record["model"])
            if mesh.triangle_count == 0:
                continue
            scale = np.asarray(record.get("scale") or (1.0, 1.0, 1.0), float)
            rotation = g.yaw_matrix3(record["yaw"])
            corners = (mesh.corners.reshape(-1, 3) * scale) @ rotation.T + np.asarray(record["translation"], float)
            triangles.append(corners.reshape(-1, 3, 3))
            # See-through materials (tree cards, fences) are not walls.
            opaque = np.array([m.get("alpha_mode", "OPAQUE") == "OPAQUE" for m in mesh.materials] or [True])
            solid.append(opaque[mesh.triangle_materials] if len(mesh.triangle_materials) else np.ones(mesh.triangle_count, bool))
            water = np.array([m.get("surface") == "water" for m in mesh.materials] or [False])
            wet.append(water[mesh.triangle_materials] if len(mesh.triangle_materials) else np.zeros(mesh.triangle_count, bool))
        if triangles:
            tris = np.concatenate(triangles)
            solid = np.concatenate(solid)
            wet = np.concatenate(wet)
        else:
            tris, solid, wet = np.zeros((0, 3, 3)), np.zeros(0, bool), np.zeros(0, bool)
        normal = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
        length = np.linalg.norm(normal, axis=1)
        walkable = length > 1e-9
        walkable[walkable] &= np.abs(normal[walkable, 1]) / length[walkable] > 0.05
        self.walls = WallIndex(tris[(length > 1e-9) & ~walkable & solid], self.cell)
        self.tris = tris[walkable].astype(np.float64)
        self.water = wet[walkable]  # surfaces marked as water (rivers, lakes): not drivable
        self.last_water = False
        lo = np.floor(self.tris[:, :, [0, 2]].min(axis=1) / self.cell).astype(np.int64)
        hi = np.floor(self.tris[:, :, [0, 2]].max(axis=1) / self.cell).astype(np.int64)
        # Every (cell, triangle) pair over each triangle's cell rectangle, without a Python loop per triangle.
        span = hi - lo + 1
        count = span[:, 0] * span[:, 1]
        owners = np.repeat(np.arange(len(self.tris)), count)
        first = np.repeat(np.cumsum(count) - count, count)
        local = np.arange(len(owners)) - first
        width = span[owners, 0]
        keys = self._key(lo[owners, 0] + local % width, lo[owners, 1] + local // width)
        order = np.argsort(keys, kind="stable")
        self.keys, self.owners = keys[order], owners[order]

    @staticmethod
    def _key(x, z):
        return (np.asarray(x, np.int64) + (1 << 20)) * (1 << 21) + (np.asarray(z, np.int64) + (1 << 20))

    def height(self, x: float, z: float, below: float) -> float | None:
        """Highest surface at (x, z) no higher than ``below``; ``last_water`` then says whether that surface is water."""

        self.last_water = False
        key = int(self._key(math.floor(x / self.cell), math.floor(z / self.cell)))
        start, end = np.searchsorted(self.keys, [key, key + 1])
        if start == end:
            return None
        owners = self.owners[start:end]
        tris = self.tris[owners]
        a, b, c = tris[:, 0], tris[:, 1], tris[:, 2]
        v0x, v0z = b[:, 0] - a[:, 0], b[:, 2] - a[:, 2]
        v1x, v1z = c[:, 0] - a[:, 0], c[:, 2] - a[:, 2]
        px, pz = x - a[:, 0], z - a[:, 2]
        det = v0x * v1z - v1x * v0z
        with np.errstate(divide="ignore", invalid="ignore"):
            u = (px * v1z - v1x * pz) / det
            v = (v0x * pz - px * v0z) / det
        inside = (np.abs(det) > 1e-12) & (u >= -1e-6) & (v >= -1e-6) & (u + v <= 1 + 1e-6)
        if not inside.any():
            return None
        heights = a[inside, 1] + u[inside] * (b[inside, 1] - a[inside, 1]) + v[inside] * (c[inside, 1] - a[inside, 1])
        water = self.water[owners[inside]]
        keep = heights <= below + 1e-6
        if not keep.any():
            return None
        heights, water = heights[keep], water[keep]
        top = int(np.argmax(heights))
        self.last_water = bool(water[top])
        return float(heights[top])


NOTICE_CATEGORIES = {"teleporter", "teleport_exit", "pickup", "item_pickup", "powerup_pad", "crate", "trigger_volume", "capture_zone", "hazard",
                     "bot_slot"}


class DrivePreview:
    def __init__(self, scene, settings: DriveSettings | None = None, start=None, yaw: float = 0.0):
        self.scene = scene
        self.settings = settings or DriveSettings()
        self.start = (np.asarray(start, float) if start is not None else self._default_start(), float(yaw))
        self.state = TankState(self.start[0].copy(), self.start[1])
        self.heights = HeightIndex(scene)
        self._boxes = self._collision_boxes()
        self.events: list[DriveEvent] = []
        self.reset(silent=True)

    # -- setup -----------------------------------------------------------------------
    def _default_start(self) -> np.ndarray:
        lo, hi = self.scene.world_bounds()
        return (lo + hi) / 2

    def _collision_boxes(self):
        boxes = [r for _, r in self.scene.records("collision") if not r.get("disabled")]
        if not boxes:
            return None
        translation = np.array([b["translation"] for b in boxes], float)
        yaw = np.array([g.yaw_radians(b["yaw"]) for b in boxes], float)
        lo = np.array([[v if v is not None else -1e9 for v in b["min"]] for b in boxes], float)
        hi = np.array([[v if v is not None else 1e9 for v in b["max"]] for b in boxes], float)
        return translation, np.cos(yaw), np.sin(yaw), lo, hi

    def reset(self, silent: bool = False) -> None:
        position, yaw = self.start
        self.state = TankState(position.copy(), yaw)
        ground = self.ground_height(position, above=self.settings.units(200.0))
        if ground is not None:
            self.state.position[1] = ground
        if not silent:
            self.events.append(DriveEvent("reset", "Back at the start"))

    # -- queries ---------------------------------------------------------------------
    def forward(self) -> np.ndarray:
        return np.array([-math.sin(self.state.yaw), 0.0, -math.cos(self.state.yaw)])

    def ground_height(self, point, above: float) -> float | None:
        """Height of the highest surface under ``point`` no more than ``above`` units over it."""

        return self.heights.height(float(point[0]), float(point[2]), float(point[1]) + above)

    def _blocked(self, position: np.ndarray) -> np.ndarray | None:
        """Push-out vector (x, z) from collision boxes and solid upright geometry for a tank circle at ``position``; None if clear."""

        radius = self.settings.units(self.settings.tank_width_m) / 2
        bottom = position[1] + self.settings.units(self.settings.step_m)
        top = position[1] + self.settings.units(self.settings.tank_height_m)
        walls = self.heights.walls.push(float(position[0]), float(position[2]), radius, bottom, top)
        boxes = self._box_push(position)
        if walls is None and boxes is None:
            return None
        return (walls if walls is not None else np.zeros(2)) + (boxes if boxes is not None else np.zeros(2))

    def _box_push(self, position: np.ndarray) -> np.ndarray | None:
        if self._boxes is None:
            return None
        translation, cos, sin, lo, hi = self._boxes
        radius = self.settings.units(self.settings.tank_width_m) / 2
        offset = position[None, :] - translation
        # Inverse of the yaw rotation (yaw_matrix3 is [[c,0,s],[0,1,0],[-s,0,c]]): local = R^T @ offset.
        local_x = cos * offset[:, 0] - sin * offset[:, 2]
        local_z = sin * offset[:, 0] + cos * offset[:, 2]
        top = position[1] + self.settings.units(self.settings.tank_height_m)
        vertical = (position[1] + self.settings.units(self.settings.step_m) < hi[:, 1]) & (top > lo[:, 1])
        near_x = np.clip(local_x, lo[:, 0], hi[:, 0])
        near_z = np.clip(local_z, lo[:, 2], hi[:, 2])
        dx, dz = local_x - near_x, local_z - near_z
        distance = np.hypot(dx, dz)
        hit = vertical & (distance < radius)
        if not hit.any():
            return None
        push = np.zeros(2)
        for index in np.where(hit)[0]:
            if distance[index] > 1e-6:
                nx, nz = dx[index] / distance[index], dz[index] / distance[index]
                depth = radius - distance[index]
            else:  # centre inside the box: leave by the nearest face
                gaps = [local_x[index] - lo[index, 0], hi[index, 0] - local_x[index], local_z[index] - lo[index, 2], hi[index, 2] - local_z[index]]
                side = int(np.argmin(gaps))
                nx, nz = ((-1, 0), (1, 0), (0, -1), (0, 1))[side]
                depth = gaps[side] + radius
            # Back to world: world = R @ local.
            push += np.array([cos[index] * nx + sin[index] * nz, -sin[index] * nx + cos[index] * nz]) * depth
        return push

    # -- stepping ---------------------------------------------------------------------
    def step(self, dt: float, throttle: float, steer: float, brake: bool = False) -> list[DriveEvent]:
        """Advance by ``dt`` seconds. throttle in [-1, 1] (back to forward), steer in [-1, 1] (right to left)."""

        settings, state = self.settings, self.state
        self.events = []
        dt = max(0.0, min(dt, 0.1))
        throttle, steer = max(-1.0, min(1.0, throttle)), max(-1.0, min(1.0, steer))
        top, reverse = settings.units(settings.top_speed_m_s), settings.units(settings.reverse_speed_m_s)
        target = throttle * (top if throttle >= 0 else reverse)
        rate = settings.units(settings.braking_m_s2 if brake or (target * state.speed < 0) or abs(target) < abs(state.speed)
                              else settings.acceleration_m_s2)
        if brake:
            target = 0.0
        delta = target - state.speed
        state.speed += max(-rate * dt, min(rate * dt, delta))
        turning = math.radians(settings.turn_rate_deg_s) * steer * dt
        state.yaw = (state.yaw + (turning if state.speed >= -1e-3 else -turning)) % (2 * math.pi)
        move = self.forward() * state.speed * dt
        candidate = state.position + move
        candidate = self._limit(candidate)
        step_up = settings.units(settings.step_m)
        ground = self.ground_height(candidate, above=step_up)
        current = state.position[1]
        if ground is not None and state.on_ground and abs(state.speed) > 1e-6:
            # Judge slopes over half the tank's length, so a steep but smooth slope is caught as well as a ledge.
            half = settings.units(settings.tank_length_m) / 2
            direction = 1.0 if state.speed > 0 else -1.0
            ahead_point = state.position + self.forward() * direction * half
            # Only surfaces a slope a little steeper than the limit could reach: bridges and skyways overhead are not ramps.
            reach = half * math.tan(math.radians(min(settings.max_slope_deg * 1.25, 80.0))) + step_up
            ahead = self.ground_height(ahead_point, above=reach)
            rise = (ahead if ahead is not None else ground) - current
            if rise > step_up and math.degrees(math.atan2(rise, half)) > settings.max_slope_deg:
                candidate = state.position.copy()  # too steep: stop at the foot
                state.speed = 0.0
                self.events.append(DriveEvent("blocked", "Too steep"))
                ground = self.ground_height(candidate, above=step_up)
        if ground is not None and self.heights.last_water and state.on_ground and abs(state.speed) > 1e-6:
            here = self.ground_height(state.position, above=step_up)
            if here is None or not self.heights.last_water:  # driving in from the bank (a tank already in the water may drive out)
                candidate = state.position.copy()
                state.speed = 0.0
                self.events.append(DriveEvent("blocked", "Water: tanks cannot drive into the river"))
                ground = here
        push = self._blocked(candidate)
        if push is not None:
            candidate[0] += push[0]
            candidate[2] += push[1]
            normal = push / (np.linalg.norm(push) or 1.0)
            along = move[[0, 2]] @ normal
            if along < 0:
                state.speed *= 0.2
                self.events.append(DriveEvent("blocked", "Bumped into a solid object"))
            ground = self.ground_height(candidate, above=step_up)
        if ground is not None and ground >= candidate[1] - step_up and (state.on_ground or candidate[1] <= ground):
            candidate[1] = ground
            state.vertical_speed = 0.0
            state.on_ground = True
        else:  # airborne: fall towards whatever is below
            state.vertical_speed -= settings.units(settings.gravity_m_s2) * dt
            previous = max(candidate[1], state.position[1])
            candidate[1] += state.vertical_speed * dt
            # Look for ground from where the tank was this frame, so a fast fall cannot pass through a surface in one step.
            below = self.ground_height(np.array([candidate[0], previous, candidate[2]]), above=0.0)
            state.on_ground = False
            if below is not None and candidate[1] <= below:
                candidate[1] = below
                state.vertical_speed = 0.0
                state.on_ground = True
            elif below is None and candidate[1] < self.scene.world_bounds()[0][1] - settings.units(200.0):
                self.events.append(DriveEvent("fell", "Fell off the map"))
                self.reset()
                return self.events
        state.distance += float(np.linalg.norm((candidate - state.position)[[0, 2]]))
        state.position = candidate
        state.pitch = self._pitch()
        self._gameplay(dt)
        return self.events

    def _limit(self, point: np.ndarray) -> np.ndarray:
        bounds = self.scene.bounds
        if not bounds:
            return point
        margin = self.settings.units(self.settings.tank_length_m) / 2
        point = point.copy()
        point[0] = min(max(point[0], bounds["x"][0] + margin), bounds["x"][1] - margin)
        point[2] = min(max(point[2], bounds["z"][0] + margin), bounds["z"][1] - margin)
        return point

    def _pitch(self) -> float:
        half = self.settings.units(self.settings.tank_length_m) / 2
        step_up = self.settings.units(self.settings.step_m) * 4
        front = self.ground_height(self.state.position + self.forward() * half, above=step_up)
        back = self.ground_height(self.state.position - self.forward() * half, above=step_up)
        if front is None or back is None:
            return 0.0
        return math.atan2(front - back, 2 * half)

    def _area(self, record: dict) -> float:
        fields = record.get("fields", {})
        category = record.get("category")
        if category in ("teleporter", "trigger_volume", "capture_zone", "hazard"):
            if category == "trigger_volume" and (fields.get("shape") or {}).get("value") == 1:
                return float(max((fields.get("half_x") or {}).get("value") or 0, (fields.get("half_z") or {}).get("value") or 0))
            return float((fields.get("radius") or {}).get("value") or 90)
        return self.settings.units(self.settings.pickup_radius_m)

    def _label(self, record: dict) -> str:
        fields = record.get("fields", {})
        label = (fields.get("label") or {}).get("value")
        return label or str(record.get("category", "object")).replace("_", " ")

    def _gameplay(self, dt: float) -> None:
        """Notices for areas entered; nothing is collected, triggered or moved."""

        state = self.state
        now_inside = set()
        for _, record in self.scene.records("markers"):
            category = record.get("category")
            if record.get("disabled") or category not in NOTICE_CATEGORIES or category in ("teleport_exit", "bot_slot"):
                continue
            offset = np.asarray(record["translation"], float) - state.position
            if abs(offset[1]) > self.settings.units(6.0) or float(np.hypot(offset[0], offset[2])) > self._area(record):
                continue
            now_inside.add(record["id"])
            if record["id"] in state.inside:
                continue
            fields = record.get("fields", {})
            if category == "teleporter":
                target = (fields.get("exit") or {}).get("value")
                found = self.scene.record(target) if target else None
                where = self._label(found[1]) if found else "no exit (validation reports it)"
                text = f"Teleporter pad to {where}: the game moves vehicles there; the preview does not (T jumps the preview there to check the exit)"
            elif category in ("pickup", "item_pickup", "powerup_pad", "crate"):
                item = (fields.get("item") or {}).get("value")
                text = f"Pickup{' ' + item if item else ''}: not collected; pickups only work in the game"
            else:
                text = f"Inside {self._label(record)}: its triggers and effects only run in the game"
            self.events.append(DriveEvent("notice", text, record["id"]))
        state.inside = now_inside

    def jump_to_exit(self) -> bool:
        """Editor jump: put the preview tank on the exit of the teleporter pad it stands on (not a game teleport)."""

        for record_id in sorted(self.state.inside):
            found = self.scene.record(record_id)
            if found is None or found[1].get("category") != "teleporter":
                continue
            target = (found[1].get("fields", {}).get("exit") or {}).get("value")
            exit_record = self.scene.record(target)[1] if target and self.scene.record(target) else None
            if exit_record is None:
                continue
            self.state.position = np.asarray(exit_record["translation"], float).copy()
            self.state.yaw = g.yaw_radians(exit_record.get("yaw", 0))
            self.state.speed, self.state.vertical_speed, self.state.on_ground = 0.0, 0.0, True
            self.state.inside = set()
            self.events.append(DriveEvent("jumped", f"Editor jump to {self._label(exit_record)} (not a game teleport)", exit_record["id"]))
            return True
        return False

    # -- camera and drawing -------------------------------------------------------------
    def chase_camera(self, camera: g.Camera, dt: float, distance_m: float = 14.0, height_m: float = 5.5) -> None:
        """Move ``camera`` behind and above the tank, smoothly."""

        target = self.state.position + np.array([0.0, self.settings.units(2.0), 0.0])
        desired = target - self.forward() * self.settings.units(distance_m) + np.array([0.0, self.settings.units(height_m), 0.0])
        blend = 1.0 - math.exp(-dt * 6.0) if dt > 0 else 1.0
        camera.position = camera.position + (desired - camera.position) * blend
        direction = target - camera.position
        camera.yaw = math.atan2(-direction[0], -direction[2])
        camera.pitch = math.atan2(direction[1], math.hypot(direction[0], direction[2]))

    def tank_transform(self, model_length_units: float) -> tuple[list[float], int, list[float]]:
        """(translation, yaw units, scale) for drawing a tank model ``model_length_units`` long at the set size."""

        scale = self.settings.units(self.settings.tank_length_m) / max(model_length_units, 1e-6)
        return [float(v) for v in self.state.position], g.yaw_units(self.state.yaw), [scale, scale, scale]

    def speed_kmh(self) -> float:
        return abs(self.state.speed) / UNITS_PER_METRE * 3.6
