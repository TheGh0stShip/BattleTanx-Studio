"""Off-screen OpenGL context (EGL, surfaceless) for tests and screenshots: never opens a window."""

from __future__ import annotations

import ctypes
import os


def create_context() -> object:
    """Create and make current a surfaceless OpenGL 3.3 core context; Linux/Mesa only."""

    os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
    os.environ.setdefault("EGL_PLATFORM", "surfaceless")
    from OpenGL import EGL

    display = EGL.eglGetDisplay(EGL.EGL_DEFAULT_DISPLAY)
    major, minor = EGL.EGLint(), EGL.EGLint()
    if not EGL.eglInitialize(display, ctypes.pointer(major), ctypes.pointer(minor)):
        raise RuntimeError("EGL initialisation failed")
    config, count = EGL.EGLConfig(), EGL.EGLint()
    attributes = (EGL.EGLint * 5)(EGL.EGL_RENDERABLE_TYPE, EGL.EGL_OPENGL_BIT, EGL.EGL_SURFACE_TYPE, EGL.EGL_PBUFFER_BIT, EGL.EGL_NONE)
    EGL.eglChooseConfig(display, attributes, ctypes.pointer(config), 1, ctypes.pointer(count))
    EGL.eglBindAPI(EGL.EGL_OPENGL_API)
    context_attributes = (EGL.EGLint * 7)(0x3098, 3, 0x30FB, 3, 0x30FD, 0x1, EGL.EGL_NONE)  # 3.3 core
    context = EGL.eglCreateContext(display, config, EGL.EGL_NO_CONTEXT, context_attributes)
    if not context or not EGL.eglMakeCurrent(display, EGL.EGL_NO_SURFACE, EGL.EGL_NO_SURFACE, context):
        raise RuntimeError("could not make an EGL context current")
    return display, context
