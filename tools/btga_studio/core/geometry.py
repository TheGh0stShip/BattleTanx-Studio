"""Camera, projection, rays and intersection primitives for BTGA Studio.

Conventions match the neutral contract: +Y up, right-handed, yaw in u16
units (65536 per turn) applied as the row-vector matrix shared by every
game, x' = x cos + z sin, z' = -x sin + z cos.

Matrices are numpy float64 4x4 in column-vector convention (OpenGL);
they are transposed on upload.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

TURN = 65536


def yaw_radians(units: int | float) -> float:
    return (int(units) & 0xFFFF) * 2 * math.pi / TURN


def yaw_units(radians: float) -> int:
    return int(round(radians * TURN / (2 * math.pi))) & 0xFFFF


def yaw_matrix3(units: int | float) -> np.ndarray:
    """Column-vector 3x3 for the game yaw: (x, y, z) -> (x c + z s, y, -x s + z c)."""

    theta = yaw_radians(units)
    c, s = math.cos(theta), math.sin(theta)
    return np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]])


def model_matrix(translation, yaw_units_: int | float, scale=None) -> np.ndarray:
    m = np.identity(4)
    m[:3, :3] = yaw_matrix3(yaw_units_) * (np.asarray(scale, float) if scale is not None else 1.0)
    m[:3, 3] = translation
    return m


def to_local(point, translation, yaw_units_: int | float) -> np.ndarray:
    """Inverse of model_matrix for a point."""

    return yaw_matrix3(yaw_units_).T @ (np.asarray(point, float) - np.asarray(translation, float))


def perspective(fov_y_degrees: float, aspect: float, near: float, far: float) -> np.ndarray:
    f = 1.0 / math.tan(math.radians(fov_y_degrees) / 2)
    m = np.zeros((4, 4))
    m[0, 0] = f / aspect
    m[1, 1] = f
    m[2, 2] = (far + near) / (near - far)
    m[2, 3] = 2 * far * near / (near - far)
    m[3, 2] = -1.0
    return m


@dataclass
class Camera:
    """Free-fly camera: yaw about +Y (0 looks down -Z), pitch about the camera's right axis."""

    position: np.ndarray = field(default_factory=lambda: np.zeros(3))
    yaw: float = 0.0
    pitch: float = 0.0
    fov_y: float = 60.0
    near: float = 1.0
    far: float = 200000.0
    speed: float = 1500.0
    look_sensitivity: float = 0.0035
    max_pitch: float = math.radians(89.0)

    def forward(self) -> np.ndarray:
        cp = math.cos(self.pitch)
        return np.array([-math.sin(self.yaw) * cp, math.sin(self.pitch), -math.cos(self.yaw) * cp])

    def right(self) -> np.ndarray:
        return np.array([math.cos(self.yaw), 0.0, -math.sin(self.yaw)])

    def up(self) -> np.ndarray:
        return np.cross(self.right(), self.forward())

    def view(self) -> np.ndarray:
        f, r, u = self.forward(), self.right(), self.up()
        m = np.identity(4)
        m[0, :3], m[1, :3], m[2, :3] = r, u, -f
        m[:3, 3] = -m[:3, :3] @ self.position
        return m

    def projection(self, aspect: float) -> np.ndarray:
        return perspective(self.fov_y, aspect, self.near, self.far)

    def look(self, dx_pixels: float, dy_pixels: float) -> None:
        self.yaw -= dx_pixels * self.look_sensitivity
        self.pitch = max(-self.max_pitch, min(self.max_pitch, self.pitch - dy_pixels * self.look_sensitivity))

    def move(self, dt: float, forward: float = 0, strafe: float = 0, vertical: float = 0, fast: bool = False) -> None:
        """WASD moves on the horizontal plane; Q/E move along world Y."""

        step = self.speed * dt * (4.0 if fast else 1.0)
        flat = np.array([-math.sin(self.yaw), 0.0, -math.cos(self.yaw)])
        self.position = self.position + step * (forward * flat + strafe * self.right() + vertical * np.array([0.0, 1.0, 0.0]))

    def adjust_speed(self, wheel_steps: float) -> None:
        self.speed = float(min(1e6, max(1.0, self.speed * (1.25 ** wheel_steps))))

    def frame(self, lo, hi) -> None:
        """Look at a bounding box from above and behind so it fills the view."""

        lo, hi = np.asarray(lo, float), np.asarray(hi, float)
        centre = (lo + hi) / 2
        radius = max(float(np.linalg.norm((hi - lo)[[0, 2]])) / 2, float(hi[1] - lo[1]) / 2, 1.0)
        distance = radius / math.tan(math.radians(self.fov_y) / 2) * 0.9
        self.yaw, self.pitch = 0.0, math.radians(-45.0)
        self.position = centre - self.forward() * distance
        self.far = max(self.far, distance + 4 * radius)
        self.speed = max(50.0, radius / 2)


@dataclass(frozen=True)
class Ray:
    origin: np.ndarray
    direction: np.ndarray  # unit length


def screen_ray(camera: Camera, x: float, y: float, width: int, height: int) -> Ray:
    """Ray through pixel (x, y), origin top-left, from the camera position."""

    ndc_x = 2.0 * (x + 0.5) / width - 1.0
    ndc_y = 1.0 - 2.0 * (y + 0.5) / height
    inverse = np.linalg.inv(camera.projection(width / height) @ camera.view())
    near = inverse @ np.array([ndc_x, ndc_y, -1.0, 1.0])
    far = inverse @ np.array([ndc_x, ndc_y, 1.0, 1.0])
    near, far = near[:3] / near[3], far[:3] / far[3]
    direction = far - near
    return Ray(np.array(camera.position, float), direction / np.linalg.norm(direction))


def project(camera: Camera, point, width: int, height: int) -> tuple[float, float] | None:
    clip = camera.projection(width / height) @ camera.view() @ np.append(np.asarray(point, float), 1.0)
    if clip[3] <= 1e-9:
        return None
    ndc = clip[:3] / clip[3]
    return (ndc[0] + 1) / 2 * width - 0.5, (1 - ndc[1]) / 2 * height - 0.5


def ray_aabb(ray: Ray, lo, hi) -> float | None:
    """Entry distance of the ray into an axis-aligned box (0 when inside), else None."""

    with np.errstate(divide="ignore", invalid="ignore"):
        inv = 1.0 / ray.direction
        t1 = (np.asarray(lo, float) - ray.origin) * inv
        t2 = (np.asarray(hi, float) - ray.origin) * inv
    t1 = np.where(np.isnan(t1), -np.inf, t1)
    t2 = np.where(np.isnan(t2), np.inf, t2)
    near = float(np.max(np.minimum(t1, t2)))
    far = float(np.min(np.maximum(t1, t2)))
    if far < max(near, 0.0):
        return None
    return max(near, 0.0)


def ray_box(ray: Ray, translation, yaw: int | float, lo, hi) -> float | None:
    """Ray against a box given in a yawed local frame."""

    rotation = yaw_matrix3(yaw).T
    local = Ray(rotation @ (ray.origin - np.asarray(translation, float)), rotation @ ray.direction)
    return ray_aabb(local, lo, hi)


def ray_sphere(ray: Ray, centre, radius: float) -> float | None:
    offset = ray.origin - np.asarray(centre, float)
    b = float(offset @ ray.direction)
    c = float(offset @ offset) - radius * radius
    disc = b * b - c
    if disc < 0:
        return None
    root = math.sqrt(disc)
    t = -b - root
    if t < 0:
        t = -b + root
    return t if t >= 0 else None


def ray_triangles(ray: Ray, a: np.ndarray, b: np.ndarray, c: np.ndarray) -> tuple[float, int] | None:
    """Möller–Trumbore over arrays of triangles (N x 3 each); both faces hit.

    Returns (distance, triangle index) of the nearest hit; ties take the lowest index.
    """

    if len(a) == 0:
        return None
    e1, e2 = b - a, c - a
    p = np.cross(ray.direction, e2)
    det = np.einsum("ij,ij->i", e1, p)
    valid = np.abs(det) > 1e-12
    inv = np.zeros_like(det)
    inv[valid] = 1.0 / det[valid]
    s = ray.origin - a
    u = np.einsum("ij,ij->i", s, p) * inv
    q = np.cross(s, e1)
    v = (q @ ray.direction) * inv
    t = np.einsum("ij,ij->i", e2, q) * inv
    hit = valid & (u >= 0) & (v >= 0) & (u + v <= 1) & (t >= 0)
    if not hit.any():
        return None
    candidates = np.where(hit, t, np.inf)
    index = int(np.argmin(candidates))
    return float(candidates[index]), index


def ray_plane_y(ray: Ray, height: float) -> np.ndarray | None:
    if abs(ray.direction[1]) < 1e-12:
        return None
    t = (height - ray.origin[1]) / ray.direction[1]
    if t < 0:
        return None
    return ray.origin + t * ray.direction


def snap(value: float, step: float) -> float:
    return round(value / step) * step if step else value


YAW_SNAP_DEGREES = (0, 15, 45, 90)


def snap_yaw(units: int, degrees: int) -> int:
    if not degrees:
        return int(units) & 0xFFFF
    step = TURN * degrees / 360
    return int(round(round((int(units) & 0xFFFF) / step) * step)) & 0xFFFF


class TopDownCamera:
    """Orthographic camera looking straight down on a map box: +X right, +Z down the image.

    That is the orientation of the games' own minimaps (pixel y grows with world z).
    """

    def __init__(self, x_range, z_range, height: float = 100000.0):
        self.x0, self.x1 = float(x_range[0]), float(x_range[1])
        self.z0, self.z1 = float(z_range[0]), float(z_range[1])
        self.height = height
        self.near, self.far = 1.0, 2.0 * height
        self.position = np.array([(self.x0 + self.x1) / 2, height, (self.z0 + self.z1) / 2])

    def view(self) -> np.ndarray:
        m = np.identity(4)
        m[0, :3] = (1.0, 0.0, 0.0)   # right
        m[1, :3] = (0.0, 0.0, -1.0)  # screen up is world -Z
        m[2, :3] = (0.0, 1.0, 0.0)   # looking down -Y
        m[:3, 3] = -m[:3, :3] @ self.position
        return m

    def projection(self, aspect: float = 1.0) -> np.ndarray:
        hx, hz = (self.x1 - self.x0) / 2, (self.z1 - self.z0) / 2
        m = np.identity(4)
        m[0, 0] = 1.0 / hx
        m[1, 1] = 1.0 / hz
        m[2, 2] = -2.0 / (self.far - self.near)
        m[2, 3] = -(self.far + self.near) / (self.far - self.near)
        return m

    def to_pixel(self, x: float, z: float, width: int, height: int) -> tuple[float, float]:
        return (x - self.x0) / (self.x1 - self.x0) * width, (z - self.z0) / (self.z1 - self.z0) * height
