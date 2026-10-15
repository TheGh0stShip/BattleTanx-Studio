"""Drive the complete editor UI off-screen (EGL framebuffer, synthetic input): no window is ever created.

Used by tests and to produce screenshots from synthetic sample data.
"""

from __future__ import annotations

from pathlib import Path

from tools.btga_studio.gui.headless import create_context

LAYOUT = (("Project & layers", "panel_hierarchy", (0.0, 0.03, 0.18, 0.69)), ("Viewport", "panel_viewport", (0.18, 0.03, 0.58, 0.69)),
          ("Inspector", "panel_inspector", (0.76, 0.03, 0.24, 0.45)), ("Minimap", "panel_minimap", (0.76, 0.48, 0.24, 0.24)),
          ("Assets", "panel_assets", (0.0, 0.72, 0.6, 0.28)), ("Output", "panel_output", (0.6, 0.72, 0.4, 0.28)))


class OffscreenUI:
    def __init__(self, app, width: int = 1600, height: int = 960):
        create_context()
        from imgui_bundle import imgui, imguizmo
        from imgui_bundle.python_backends.opengl_backend_programmable import ProgrammablePipelineRenderer
        from OpenGL import GL as gl

        from tools.btga_studio.gui.renderer import ViewportRenderer

        self.imgui, self.gizmo, self.gl = imgui, imguizmo.im_guizmo, gl
        self.context = imgui.create_context()
        from tools.btga_studio.gui import fonts

        self.font_status = fonts.load()
        io = imgui.get_io()
        io.display_size = imgui.ImVec2(width, height)
        io.set_ini_filename("")
        io.config_flags |= imgui.ConfigFlags_.docking_enable
        self.backend = ProgrammablePipelineRenderer()
        self.size = (width, height)
        self.status_bar = True
        self.app = app
        app._renderer_cls = ViewportRenderer
        app.post_init()
        self.fbo = gl.glGenFramebuffers(1)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.fbo)
        self.colour = gl.glGenRenderbuffers(1)
        gl.glBindRenderbuffer(gl.GL_RENDERBUFFER, self.colour)
        gl.glRenderbufferStorage(gl.GL_RENDERBUFFER, gl.GL_RGBA8, width, height)
        gl.glFramebufferRenderbuffer(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0, gl.GL_RENDERBUFFER, self.colour)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)

    def frame(self, mouse=(-1.0, -1.0), buttons=(False, False), keys: dict | None = None, text: str = "", dt: float = 1 / 60) -> None:
        imgui, gl = self.imgui, self.gl
        io = imgui.get_io()
        io.delta_time = dt
        io.add_mouse_pos_event(*mouse)
        for button, down in enumerate(buttons):
            io.add_mouse_button_event(button, down)
        for key, down in (keys or {}).items():
            io.add_key_event(key, down)
        for character in text:
            io.add_input_character(ord(character))
        imgui.new_frame()
        self.gizmo.begin_frame()
        if imgui.begin_main_menu_bar():
            self.app.guard("menus", self.app.show_menus)
            imgui.end_main_menu_bar()
        width, height = self.size
        for label, method, (x, y, w, h) in LAYOUT:
            imgui.set_next_window_pos(imgui.ImVec2(x * width, y * height))
            imgui.set_next_window_size(imgui.ImVec2(w * width, h * height))
            if imgui.begin(label, None, imgui.WindowFlags_.no_collapse | imgui.WindowFlags_.no_move | imgui.WindowFlags_.no_resize)[0]:
                self.app.guard(f"{label} panel", getattr(self.app, method))
            imgui.end()
        self.app.show_gui()
        if self.status_bar:
            imgui.set_next_window_pos(imgui.ImVec2(0, height - 22))
            imgui.set_next_window_size(imgui.ImVec2(width, 22))
            if imgui.begin("##status", None, imgui.WindowFlags_.no_decoration | imgui.WindowFlags_.no_move | imgui.WindowFlags_.no_saved_settings)[0]:
                self.app.guard("status bar", lambda: imgui.text(self.app.status_text()))
            imgui.end()
        imgui.render()
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.fbo)
        gl.glViewport(0, 0, width, height)
        gl.glClearColor(0.08, 0.08, 0.1, 1)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)
        self.backend.render(imgui.get_draw_data())
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)

    # -- synthetic input through Dear ImGui (the same path as real mouse and keyboard) --------------
    def viewport_to_screen(self, x: float, y: float) -> tuple[float, float]:
        if self.app.viewport_origin is None:
            self.frame()
        ox, oy = self.app.viewport_origin
        return (ox + x, oy + y)

    def world_to_screen(self, point) -> tuple[float, float] | None:
        """Screen position of a world point in the viewport, or None when it is behind the camera."""

        from tools.btga_studio.core import geometry as g

        c = self.app.controller
        local = g.project(c.camera, point, *c.viewport)
        return None if local is None else self.viewport_to_screen(local[0], local[1])

    def _modifiers(self, ctrl: bool, shift: bool) -> dict:
        imgui = self.imgui
        return {imgui.Key.mod_ctrl: ctrl, imgui.Key.left_ctrl: ctrl, imgui.Key.mod_shift: shift, imgui.Key.left_shift: shift}

    def click(self, screen, ctrl: bool = False, shift: bool = False, settle: int = 1) -> None:
        """Press and release the left button at a screen position (no movement in between)."""

        modifiers = self._modifiers(ctrl, shift)
        self.frame(mouse=screen, keys=modifiers)
        self.frame(mouse=screen, buttons=(True, False), keys=modifiers)
        self.frame(mouse=screen, buttons=(False, False), keys=modifiers)
        self.frame(mouse=screen, keys=self._modifiers(False, False))
        for _ in range(settle - 1):
            self.frame(mouse=screen)

    def drag(self, start, end, steps: int = 6, shift: bool = False) -> None:
        """Left-button drag in screen space (box selection in the viewport)."""

        modifiers = self._modifiers(False, shift)
        self.frame(mouse=start, keys=modifiers)
        self.frame(mouse=start, buttons=(True, False), keys=modifiers)
        for step in range(1, steps + 1):
            t = step / steps
            self.frame(mouse=(start[0] + (end[0] - start[0]) * t, start[1] + (end[1] - start[1]) * t), buttons=(True, False), keys=modifiers)
        self.frame(mouse=end, buttons=(False, False), keys=modifiers)
        self.frame(mouse=end, keys=self._modifiers(False, False))

    def press(self, key_name: str, mouse=(-1.0, -1.0), ctrl: bool = False, shift: bool = False) -> None:
        """Press and release one key (``imgui.Key`` attribute name) with optional modifiers."""

        key = getattr(self.imgui.Key, key_name)
        modifiers = self._modifiers(ctrl, shift)
        self.frame(mouse=mouse, keys=modifiers)
        self.frame(mouse=mouse, keys={**modifiers, key: True})
        self.frame(mouse=mouse, keys={**modifiers, key: False})
        self.frame(mouse=mouse, keys=self._modifiers(False, False))

    def pixels(self) -> tuple[int, int, bytes]:
        """Top-down RGBA8 framebuffer contents."""

        gl = self.gl
        width, height = self.size
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.fbo)
        data = gl.glReadPixels(0, 0, width, height, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
        rows = [data[i * width * 4 : (i + 1) * width * 4] for i in range(height)]
        return width, height, b"".join(reversed(rows))

    def screenshot(self, path: Path) -> None:
        """Write the application framebuffer only (never the desktop) as PNG."""

        from tools.btga_assets.ps1_formats import encode_png_rgba

        width, height, rgba = self.pixels()
        Path(path).write_bytes(encode_png_rgba(width, height, rgba))

    def close(self) -> None:
        self.app.before_exit()
        self.imgui.destroy_context(self.context)


class OffscreenSurface:
    """A bare off-screen Dear ImGui surface for small standalone windows (setup, uninstall); same EGL rules as OffscreenUI."""

    def __init__(self, width: int = 760, height: int = 520):
        create_context()
        from imgui_bundle import imgui
        from imgui_bundle.python_backends.opengl_backend_programmable import ProgrammablePipelineRenderer
        from OpenGL import GL as gl

        from tools.btga_studio.gui import fonts

        self.imgui, self.gl = imgui, gl
        self.context = imgui.create_context()
        self.font_status = fonts.load()
        io = imgui.get_io()
        io.display_size = imgui.ImVec2(width, height)
        io.set_ini_filename("")
        self.backend = ProgrammablePipelineRenderer()
        self.size = (width, height)
        self.fbo = gl.glGenFramebuffers(1)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.fbo)
        self.colour = gl.glGenRenderbuffers(1)
        gl.glBindRenderbuffer(gl.GL_RENDERBUFFER, self.colour)
        gl.glRenderbufferStorage(gl.GL_RENDERBUFFER, gl.GL_RGBA8, width, height)
        gl.glFramebufferRenderbuffer(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0, gl.GL_RENDERBUFFER, self.colour)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)

    def frame(self, draw, mouse=(-1.0, -1.0), buttons=(False, False), dt: float = 1 / 60) -> None:
        """Draw one frame: ``draw()`` is called inside a full-size window, as the real app's single window does."""

        imgui, gl = self.imgui, self.gl
        io = imgui.get_io()
        io.delta_time = dt
        io.add_mouse_pos_event(*mouse)
        for button, down in enumerate(buttons):
            io.add_mouse_button_event(button, down)
        imgui.new_frame()
        width, height = self.size
        imgui.set_next_window_pos(imgui.ImVec2(0, 0))
        imgui.set_next_window_size(imgui.ImVec2(width, height))
        flags = imgui.WindowFlags_.no_decoration | imgui.WindowFlags_.no_move | imgui.WindowFlags_.no_saved_settings
        if imgui.begin("##surface", None, flags)[0]:
            draw()
        imgui.end()
        imgui.render()
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.fbo)
        gl.glViewport(0, 0, width, height)
        gl.glClearColor(0.08, 0.08, 0.1, 1)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)
        self.backend.render(imgui.get_draw_data())
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)

    pixels = OffscreenUI.pixels
    screenshot = OffscreenUI.screenshot

    def close(self) -> None:
        self.imgui.destroy_context(self.context)
