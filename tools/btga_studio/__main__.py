"""``python -m tools.btga_studio`` starts the editor."""

import os
import sys

# On Linux desktops (and WSLg) the window's OpenGL context comes from X11/GLX; PyOpenGL would otherwise pick EGL
# whenever a Wayland session is present and then fail to find the context. Off-screen tools set their own platform.
if sys.platform.startswith("linux") and "PYOPENGL_PLATFORM" not in os.environ and os.environ.get("DISPLAY"):
    os.environ["PYOPENGL_PLATFORM"] = "glx"

from tools.btga_studio.gui.app import main

sys.exit(main())
