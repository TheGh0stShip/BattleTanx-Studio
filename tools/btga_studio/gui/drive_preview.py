"""Drive Preview in the viewport: an editor approximation for checking layout and scale with a tank-sized body.

``DrivePreviewUI`` is a mixin for ``StudioApp``.  Starting a preview saves
the editing camera; the viewport then routes W/S/A/D (or the arrow keys, or
a gamepad) to ``core.drive.DrivePreview``; Space brakes, R returns to the
start, T is an editor jump to the exit of the teleporter pad the tank is on,
Esc (or the toolbar button) ends the preview and restores the camera.
Nothing about the map changes.

The overlay and the settings window always say what it is: an editor
approximation that runs none of the game's physics, AI, combat, destruction,
pickups or scripting.  Final behaviour needs the PC runtime ("Test in Game").
"""

from __future__ import annotations

import time

import numpy as np
from imgui_bundle import imgui

from tools.btga_studio.core import drive as drive_module
from tools.btga_studio.core import game_models
from tools.btga_studio.core import geometry as g
from tools.btga_studio.core.navgraph import NavGraph

START_CATEGORIES = ("player_spawn", "team_start", "team_spawn_slot", "multiplayer_spawn")
# The game's own tank when the user's Global Assault cartridge is registered (see core.game_models), else the starter stand-in.
GAME_TANK_PARTS = (game_models.TANK_HULL, game_models.TANK_TURRET)
GAME_TANK_LENGTH = 145.0  # hull length in game units
DISCLAIMER = ("DRIVE PREVIEW - editor approximation",
              "No game physics, AI, combat, destruction, pickups or scripting run here. Use Test in Game for real behaviour.")


class DrivePreviewUI:
    def init_drive_preview(self) -> None:
        self.drive: drive_module.DrivePreview | None = None
        self.drive_settings = drive_module.DriveSettings()
        self.drive_saved_camera = None
        self.drive_saved_layers = None
        self.drive_messages: list[tuple[float, str]] = []
        self.drive_input = None  # tests set (throttle, steer, brake) to drive without a keyboard
        self.drive_frames: list[float] = []  # recent frame times (seconds) for the frame-rate readout
        self.drive_overlays = {"collision": True, "navigation": True, "volumes": True}
        self.drive_graph: NavGraph | None = None
        self.drive_reach_node: str | None = None

    # -- start and stop -----------------------------------------------------------------
    def drive_start_point(self):
        """Selected spawn or start marker, else the ground under the middle of the view, else the map centre."""

        c = self.controller
        doc = c.document
        for record_id in doc.selection:
            found = doc.scene.record(record_id)
            if found and found[1].get("category") in START_CATEGORIES:
                return np.asarray(found[1]["translation"], float), g.yaw_radians(found[1].get("yaw", 0))
        width, height = c.viewport
        ray = g.screen_ray(c.camera, width / 2, height / 2, width, height)
        surface = doc.scene.surface_point(ray)
        if surface is not None:
            return np.asarray(surface[0], float), c.camera.yaw
        for _, record in doc.scene.records("markers"):
            if record.get("category") in START_CATEGORIES:
                return np.asarray(record["translation"], float), g.yaw_radians(record.get("yaw", 0))
        return None, 0.0

    def start_drive_preview(self) -> bool:
        c = self.controller
        if c.document is None:
            return False
        position, yaw = self.drive_start_point()
        self.drive = drive_module.DrivePreview(c.document.scene, self.drive_settings, position, yaw)
        camera = c.camera
        self.drive_saved_camera = (camera.position.copy(), camera.yaw, camera.pitch)
        self.drive_saved_layers = dict(c.preferences.layers)
        self.apply_drive_overlays()
        self.drive_graph = NavGraph.from_scene(c.document.scene)
        self.drive_reach_node = None
        c.placement = None
        self.drive.chase_camera(camera, 0.0)
        self.set_idling(False)  # keep frames coming while the tank moves without mouse input
        self.drive_message("Drive Preview: W/S drive, A/D turn, Space brake, R restart, T editor jump at a teleporter pad, Esc stop")
        c.log("info", "Drive Preview started (editor approximation; no game physics, AI, combat, destruction, pickups or scripting)")
        return True

    def apply_drive_overlays(self) -> None:
        layers = self.controller.preferences.layers
        base = self.drive_saved_layers or layers
        layers["collision"] = base.get("collision", False) or self.drive_overlays["collision"]
        layers["navigation"] = base.get("navigation", False) or self.drive_overlays["navigation"]
        layers["markers"] = base.get("markers", True) or self.drive_overlays["volumes"]
        if self.renderer:
            self.renderer.authoring_overlays = self.drive_overlays["volumes"]

    def stop_drive_preview(self) -> None:
        if self.drive is None:
            return
        distance = self.drive.state.distance / drive_module.UNITS_PER_METRE
        fps = len(self.drive_frames) / sum(self.drive_frames) if self.drive_frames and sum(self.drive_frames) > 0 else 0.0
        self.drive = None
        if self.drive_saved_camera is not None:
            camera = self.controller.camera
            camera.position, camera.yaw, camera.pitch = self.drive_saved_camera
        if self.drive_saved_layers is not None:
            self.controller.preferences.layers.update(self.drive_saved_layers)
            self.drive_saved_layers = None
        if self.renderer:
            self.renderer.extras = []
            self.renderer.reach = None
            self.renderer.authoring_overlays = True
        self.set_idling(True)
        self.controller.log("info", f"Drive Preview ended after {distance:,.0f} m; {fps:.0f} frames per second on average")
        self.drive_frames = []

    @staticmethod
    def set_idling(enabled: bool) -> None:
        try:
            from imgui_bundle import hello_imgui

            hello_imgui.get_runner_params().fps_idling.enable_idling = enabled
        except Exception:  # off-screen runs have no runner
            pass

    def drive_message(self, text: str) -> None:
        self.drive_messages.append((time.monotonic(), text))
        del self.drive_messages[:-4]

    # -- per frame ------------------------------------------------------------------------
    def drive_frame(self, hovered: bool) -> None:
        io = imgui.get_io()
        self.drive_frames.append(io.delta_time)
        del self.drive_frames[:-600]
        if self.drive_input is not None:
            throttle, steer, brake = self.drive_input
        else:
            down = imgui.is_key_down
            throttle = (1.0 if down(imgui.Key.w) or down(imgui.Key.up_arrow) else 0.0) - \
                       (1.0 if down(imgui.Key.s) or down(imgui.Key.down_arrow) else 0.0)
            steer = (1.0 if down(imgui.Key.a) or down(imgui.Key.left_arrow) else 0.0) - \
                    (1.0 if down(imgui.Key.d) or down(imgui.Key.right_arrow) else 0.0)
            brake = down(imgui.Key.space)
            # Gamepad (read as buttons: this binding exposes no analogue values): left stick steers, R2/L2 drive and reverse.
            if down(imgui.Key.gamepad_l_stick_left) or down(imgui.Key.gamepad_dpad_left):
                steer = 1.0
            elif down(imgui.Key.gamepad_l_stick_right) or down(imgui.Key.gamepad_dpad_right):
                steer = -1.0
            if down(imgui.Key.gamepad_r2) or down(imgui.Key.gamepad_face_down):
                throttle = 1.0
            elif down(imgui.Key.gamepad_l2):
                throttle = -1.0
            brake = brake or down(imgui.Key.gamepad_face_left)
            if imgui.is_key_pressed(imgui.Key.r, False) and not io.want_text_input:
                self.drive.reset()
                self.drive_message("Back at the start")
            if imgui.is_key_pressed(imgui.Key.t, False) and not io.want_text_input:
                if not self.drive.jump_to_exit():
                    self.drive_message("T works on a teleporter pad that has an exit")
                for event in self.drive.events[-1:]:
                    self.drive_message(event.text)
            if imgui.is_key_pressed(imgui.Key.escape, False):
                self.stop_drive_preview()
                return
        for event in self.drive.step(io.delta_time, throttle, steer, brake):
            if event.kind != "blocked" or not any(text == event.text for _, text in self.drive_messages[-1:]):
                self.drive_message(event.text)
        self.drive.chase_camera(self.controller.camera, io.delta_time)
        self.update_drive_tank()
        self.update_drive_reach()

    def update_drive_reach(self) -> None:
        """Colour navigation points by whether they connect to the point nearest the tank."""

        if self.renderer is None or not self.drive_graph or not self.drive_overlays["navigation"]:
            if self.renderer is not None:
                self.renderer.reach = None
            return
        position = self.drive.state.position
        node = self.drive_graph.nearest(position[0], position[2])
        if node != self.drive_reach_node:
            self.drive_reach_node = node
            self.renderer.reach = self.drive_graph.reachable(node) if node else set()

    def drive_tank_parts(self) -> tuple[list[str], float, int]:
        """(model IDs, hull length in units, yaw offset): the game's tank if its catalog is loaded, else the starter stand-in."""

        library = self.controller.library
        if library is None:
            return [], 1.0, 0
        if all(part in library._models for part in GAME_TANK_PARTS):
            return list(GAME_TANK_PARTS), GAME_TANK_LENGTH, 0x8000
        model = (library.starter() or {}).get("models", {}).get("tank_placeholder")
        return ([model] if model else []), 110.0, 0  # the placeholder hull is 110 units long (its barrel adds 35)

    def update_drive_tank(self) -> None:
        parts, length, turn = self.drive_tank_parts()
        if not parts or self.renderer is None:
            return
        translation, yaw, scale = self.drive.tank_transform(length)
        self.renderer.extras = [(part, translation, (yaw + turn) & 0xFFFF, scale) for part in parts]

    def drive_overlay(self, origin) -> None:
        draw = imgui.get_window_draw_list()
        state = self.drive.state
        settings = self.drive_settings
        x, y = origin
        recent = self.drive_frames[-60:]
        fps = len(recent) / sum(recent) if recent and sum(recent) > 0 else 0.0
        banner_width = imgui.calc_text_size(DISCLAIMER[1]).x + 20
        draw.add_rect_filled(imgui.ImVec2(x + 4, y + 20), imgui.ImVec2(x + 4 + banner_width, y + 58),
                             imgui.get_color_u32(imgui.ImVec4(0.32, 0.18, 0.02, 0.88)), 4.0)
        lines = [(DISCLAIMER[0], (1.0, 0.8, 0.25, 1.0)), (DISCLAIMER[1], (1.0, 0.92, 0.75, 1.0)),
                 (f"{self.drive.speed_kmh():5.1f} km/h   tank {settings.tank_length_m:.1f} m long   {fps:4.0f} fps   Esc to stop", (1, 1, 1, 0.95)),
                 (f"position {state.position[0] / 16:,.0f}, {state.position[2] / 16:,.0f} m   height {state.position[1] / 16:,.1f} m"
                  f"   driven {state.distance / 16:,.0f} m", (1, 1, 1, 0.95))]
        now = time.monotonic()
        lines += [(text, (0.75, 0.95, 1.0, 1.0)) for stamp, text in self.drive_messages if now - stamp < 5.0]
        for number, (text, colour) in enumerate(lines):
            top = y + 24 + 17 * number + (6 if number >= 2 else 0)
            draw.add_text(imgui.ImVec2(x + 13, top + 1), imgui.get_color_u32(imgui.ImVec4(0, 0, 0, 0.8)), text)
            draw.add_text(imgui.ImVec2(x + 12, top), imgui.get_color_u32(imgui.ImVec4(*colour)), text)

    def window_drive_settings(self) -> None:
        if self.drive is None:
            return
        imgui.set_next_window_size(imgui.ImVec2(340, 0), imgui.Cond_.appearing)
        imgui.set_next_window_pos(imgui.ImVec2(imgui.get_io().display_size.x - 360, 90), imgui.Cond_.appearing)
        opened, keep = imgui.begin("Drive Preview###drive", True, imgui.WindowFlags_.no_collapse | imgui.WindowFlags_.always_auto_resize)
        if opened:
            s = self.drive_settings
            imgui.text_colored(imgui.ImVec4(1.0, 0.8, 0.25, 1.0), "Editor approximation, not the game")
            imgui.text_wrapped("It checks scale, blocked movement, water, spawn facing, navigation reach, trigger areas, pickups and bots. "
                               "It runs no game physics, AI, combat, destruction, pickups or scripting; launch the PC runtime "
                               "(Test in Game) for final behaviour. 1 m = 16 units.")
            imgui.separator()
            for label, attribute, low, high in (("Tank length (m)", "tank_length_m", 3.0, 15.0), ("Tank width (m)", "tank_width_m", 1.5, 6.0),
                                                ("Top speed (m/s)", "top_speed_m_s", 2.0, 40.0), ("Turn rate (deg/s)", "turn_rate_deg_s", 20.0, 240.0)):
                _, value = imgui.slider_float(label, getattr(s, attribute), low, high, "%.1f")
                setattr(s, attribute, value)
            imgui.separator()
            imgui.text("Overlays")
            changed = False
            for key, label in (("collision", "Collision outlines"), ("navigation", "Navigation reach (green: connected to the tank)"),
                               ("volumes", "Trigger areas, zones, spawn and bot arrows")):
                hit, self.drive_overlays[key] = imgui.checkbox(label, self.drive_overlays[key])
                changed |= hit
            if changed:
                self.apply_drive_overlays()
                self.drive_reach_node = None
            if imgui.button("Restart at the start"):
                self.drive.reset()
            imgui.same_line()
            if imgui.button("Stop Drive Preview"):
                self.stop_drive_preview()
        imgui.end()
        if not keep:
            self.stop_drive_preview()
