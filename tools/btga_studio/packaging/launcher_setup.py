"""Frozen entry point for BattleTanxStudioSetup.exe."""

import sys

if __name__ == "__main__":
    from tools.btga_studio.setup.main import main

    sys.exit(main())
