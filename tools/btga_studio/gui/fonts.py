"""Load the bundled UI fonts into Dear ImGui (primary + merged CJK fallback).

Glyphs are rasterised on demand by Dear ImGui's dynamic font atlas (the
renderer backends set ``renderer_has_textures``), so a large CJK font adds no
start-up atlas build and never freezes the UI.  Called from hello_imgui's
``load_additional_fonts`` callback and by the off-screen driver.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tools.btga_studio.core import fontset
from tools.btga_studio.core.settings import bundled_data_directory

BASE_SIZE = 15.0


@dataclass
class FontStatus:
    loaded: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def unicode(self) -> bool:
        return bool(self.loaded)


STATUS = FontStatus()


def load(size: float = BASE_SIZE) -> FontStatus:
    from imgui_bundle import imgui

    status = FontStatus()
    found, problems = fontset.resolve(bundled_data_directory() / "fonts")
    status.problems += problems
    io = imgui.get_io()
    primary = [(e, p) for e, p in found if e.role == "primary"]
    if not primary:
        io.fonts.add_font_default()
        status.problems.append("using the built-in font (ASCII and Latin-1 only)")
    for number, (entry, path) in enumerate(primary + [(e, p) for e, p in found if e.role != "primary"]):
        config = imgui.ImFontConfig()
        config.merge_mode = number > 0 and bool(primary)
        if io.fonts.add_font_from_file_ttf(str(path), size, config) is not None:
            status.loaded.append(f"{entry.family} {entry.version}")
    STATUS.loaded, STATUS.problems = status.loaded, status.problems
    return status
