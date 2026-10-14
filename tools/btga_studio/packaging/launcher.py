"""Frozen entry point for BTGAStudio.exe."""

import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()  # the thumbnail worker pool re-enters this module on Windows
    from tools.btga_studio.gui.app import main

    sys.exit(main())
