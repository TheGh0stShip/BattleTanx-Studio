#!/usr/bin/env python3
"""Compatibility entry point for the PS1 format decoders in tools/btga_assets."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.btga_assets.ps1_formats import *  # noqa: E402,F401,F403
from tools.btga_assets.ps1_formats import _five_to_eight  # noqa: E402,F401
