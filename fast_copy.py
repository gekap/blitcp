#!/usr/bin/env python3
"""Compatibility shim — fast-copy is now blitcp (https://blitcp.dev).

`import fast_copy` returns the blitcp module itself, so existing scripts and
the GUI keep working unchanged. Running this file delegates to the blitcp CLI
after a one-line notice. This shim will be removed one or two releases after
v4.0.0 — switch imports and commands to `blitcp`.
"""
import sys

import blitcp

sys.modules[__name__] = blitcp

if __name__ == "__main__":
    sys.stderr.write("note: fast-copy is now blitcp — this launcher will be "
                     "removed in a future release; use 'blitcp' instead.\n")
    blitcp.cli_entry()
