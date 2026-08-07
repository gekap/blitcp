#!/usr/bin/env python3
"""Compatibility shim — the fast-copy GUI is now blitcp_gui (https://blitcp.dev).

`import fast_copy_modern_gui` returns the blitcp_gui module; running this file
launches the GUI. Will be removed one or two releases after v4.0.0."""
import sys

import blitcp_gui

sys.modules[__name__] = blitcp_gui

if __name__ == "__main__":
    blitcp_gui.main()
