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
        io = imgui.get_io()
        io.display_size = imgui.ImVec2(width, height)
        io.set_ini_filename("")
        io.config_flags |= imgui.ConfigFlags_.docking_enable
        self.backend = ProgrammablePipelineRenderer()
        self.size = (width, height)
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
            self.app.show_menus()
            imgui.end_main_menu_bar()
        width, height = self.size
        for label, method, (x, y, w, h) in LAYOUT:
            imgui.set_next_window_pos(imgui.ImVec2(x * width, y * height))
            imgui.set_next_window_size(imgui.ImVec2(w * width, h * height))
            if imgui.begin(label, None, imgui.WindowFlags_.no_collapse | imgui.WindowFlags_.no_move | imgui.WindowFlags_.no_resize)[0]:
                getattr(self.app, method)()
            imgui.end()
        self.app.show_gui()
        imgui.render()
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.fbo)
        gl.glViewport(0, 0, width, height)
        gl.glClearColor(0.08, 0.08, 0.1, 1)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)
        self.backend.render(imgui.get_draw_data())
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)

    def screenshot(self, path: Path) -> None:
        from tools.btga_assets.ps1_formats import encode_png_rgba

        gl = self.gl
        width, height = self.size
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.fbo)
        data = gl.glReadPixels(0, 0, width, height, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
        rows = [data[i * width * 4 : (i + 1) * width * 4] for i in range(height)]
        Path(path).write_bytes(encode_png_rgba(width, height, b"".join(reversed(rows))))

    def close(self) -> None:
        self.app.before_exit()
        self.imgui.destroy_context(self.context)
