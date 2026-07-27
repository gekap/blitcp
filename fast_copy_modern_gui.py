#!/usr/bin/env python3
# Copyright 2026 George Kapellakis
# Licensed under the Apache License, Version 2.0
# See LICENSE file for details.
"""fast-copy — PySide6 modern GUI.

A faithful native reproduction of the fast-copy modern look-and-feel mockup:
sidebar navigation, transfer composer with live progress simulation, connections
manager, remote/local file browser, history and settings — light & dark themes.

Icons use the bundled Tabler Icons webfont (assets/tabler-icons.ttf).
"""
import os
import base64
import re
import subprocess
import sys
import time
from collections import deque


def _ensure_std_streams():
    """Give the process real std streams. Frozen, windowed builds (double-clicked
    on Windows) have NO console, so sys.stdout/stderr are None — and fast_copy.py
    (a CLI module) reads sys.stdout.isatty() at import and writes to these streams,
    so without this the engine import fails and the GUI silently loses it
    (FC_OK=False). It also makes `--fc-core` a fully working CLI:

      * launched from cmd WITH redirection (> file / | pipe) → wrap the inherited
        OS handle so the output actually lands in the file/pipe;
      * launched from cmd WITHOUT redirection → attach to the parent console and
        print there;
      * double-clicked (no console) → route to os.devnull so nothing crashes.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return

    if sys.platform.startswith("win"):
        try:
            import ctypes
            import msvcrt
            k = ctypes.windll.kernel32
            k.GetStdHandle.restype = ctypes.c_void_p
            k.GetStdHandle.argtypes = [ctypes.c_uint]
            STD = {"stdout": 0xFFFFFFF5, "stderr": 0xFFFFFFF4, "stdin": 0xFFFFFFF6}
            INVALID = ctypes.c_void_p(-1).value

            def _wrap(which, mode):
                """Wrap an already-inherited std handle (redirection) as a file."""
                h = k.GetStdHandle(STD[which])
                if not h or h == INVALID:
                    return None
                try:
                    fd = msvcrt.open_osfhandle(h, 0)
                    return os.fdopen(fd, mode, buffering=1)
                except OSError:
                    return None

            out = _wrap("stdout", "w") if sys.stdout is None else sys.stdout
            err = _wrap("stderr", "w") if sys.stderr is None else sys.stderr
            # Nothing inherited (no redirection) → attach to the parent console.
            if out is None or err is None:
                if k.AttachConsole(-1):           # ATTACH_PARENT_PROCESS
                    if out is None:
                        out = open("CONOUT$", "w", buffering=1)
                    if err is None:
                        err = open("CONOUT$", "w", buffering=1)
                    if sys.stdin is None:
                        try:
                            sys.stdin = open("CONIN$", "r")
                        except OSError:
                            pass
            if sys.stdout is None and out is not None:
                sys.stdout = out
            if sys.stderr is None and err is not None:
                sys.stderr = err
        except Exception:
            pass

    _devnull = open(os.devnull, "w")
    if sys.stdout is None:
        sys.stdout = _devnull
    if sys.stderr is None:
        sys.stderr = _devnull


_ensure_std_streams()

# Reuse the real fast_copy engine for credentials + remote listing when it sits
# next to this file. Optional: the GUI still runs (with demo data) without it.
# Released in lockstep with fast_copy.py — used to fetch the MATCHING core engine
# if someone runs the GUI without it next to them.
GUI_VERSION = "3.12.6"
GUI_REPO = "gekap/fast-copy"

try:
    import fast_copy as fc
    # fast_copy installs a CLI-style excepthook on import; restore the default so
    # the GUI behaves normally.
    sys.excepthook = sys.__excepthook__
    FC_OK = True
except Exception:
    fc = None
    FC_OK = False

from PySide6.QtCore import (
    Qt, QTimer, Signal, QThread, QPropertyAnimation, QEasingCurve, QProcess,
    QProcessEnvironment,
)
from PySide6.QtGui import QFontDatabase, QFont, QColor, QPixmap, QPainter, QIcon
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QLineEdit, QComboBox,
    QVBoxLayout, QHBoxLayout, QGridLayout, QStackedWidget, QScrollArea,
    QDialog, QGraphicsOpacityEffect, QSizePolicy, QFrame, QFormLayout,
    QFileDialog, QMessageBox, QCheckBox, QTextBrowser,
)

class _BoundedLog:
    """Memory-bounded transcript for the on-failure diagnostic log.

    The root cause of a failure is carried by the ENGINE'S STDERR lines (the GUI
    feeds those in prefixed with "[stderr] "), which are low-volume — so we retain
    ALL of them (bounded) regardless of WHERE in a million-line run they occur,
    instead of a fixed head window that only ever captures startup banners. Plus
    a tail of recent output for context. Memory stays bounded without dropping the
    line that explains the failure."""

    def __init__(self, errors=4000, tail=6000):
        self._errors = deque(maxlen=errors)  # stderr lines — the root-cause carriers
        self._tail = deque(maxlen=tail)       # recent output of any kind

    def append(self, line):
        self._tail.append(line)
        if line.startswith("[stderr]"):
            self._errors.append(line)

    def extend(self, lines):
        for ln in lines:
            self.append(ln)

    def lines(self):
        tail = list(self._tail)
        tail_set = set(tail)
        # stderr/error lines that scrolled out of the tail window — the earliest
        # (often root-cause) errors that a plain tail buffer would have lost.
        earlier_errors = [e for e in self._errors if e not in tail_set]
        out = []
        if earlier_errors:
            out.append("=== earlier error output (stderr) ===")
            out.extend(earlier_errors)
            out.append("=== recent output (tail) ===")
        out.extend(tail)
        return out


# ─────────────────────────────────────────────────────────── icon font ──
HERE = os.path.dirname(os.path.abspath(__file__))
FONT_PATH = os.path.join(HERE, "assets", "tabler-icons.ttf")

# name → PUA codepoint, extracted from tabler-icons 3.35.0 css
TI = {
    "adjustments": "ea03", "alert-triangle": "ea06", "arrow-down": "ea16",
    "arrow-down-circle": "ea11", "arrow-up-circle": "ea20", "arrows-exchange": "f1f4",
    "bolt": "ea38", "book": "ea39", "brand-aws": "fa4c", "brand-azure": "fa4d",
    "brand-google": "ec1f", "brand-python": "ed01", "bucket": "ea47", "check": "ea5e",
    "chevron-right": "ea61", "cloud": "ea76", "corner-left-up": "ea7f",
    "device-desktop": "ea89", "device-floppy": "eb62", "eye": "ea9a",
    "file-text": "eaa2", "file-zip": "ed4e", "flask": "ebd2", "folder": "eaad",
    "folders": "eaae", "history": "ebea", "key": "eac7", "lock-open": "eae1",
    "moon": "eaf8", "pencil": "eb04", "player-play": "ed46", "plus": "eb0b",
    "refresh": "eb13", "server-2": "f07c", "settings": "eb20", "sun": "eb30",
    "trash": "eb41", "x": "eb55",
}

ICON_FAMILY = "tabler-icons"

# The Tabler icon font (subset to just the glyphs this GUI uses) is embedded as
# base64 so the app is a single self-contained .py — no assets/ folder needed on
# Windows/macOS/Linux. Falls back to assets/tabler-icons.ttf if present.
_ICON_FONT_B64 = (
"""AAEAAAALAIAAAwAwR1NVQrjmuNIAADVkAAAAKk9TLzJAKYCTAAAzOAAAAGBjbWFwpNudFQAAM5gAAAFEZ2x5ZkljIIoAAAC8AAAx
WmhlYWQxz4alAAAyiAAAADZoaGVhDFoIdQAAMxQAAAAkaG10eAPyAAAAADLAAAAAUmxvY2HPq9xLAAAyOAAAAFBtYXhwAD0BTgAA
MhgAAAAgbmFtZQQ0GH8AADTcAAAAaHBvc3QADQAAAAA1RAAAACAABgAAAAADeQMHAC8AXwCKAJgApgC0AAATJicmNTQ3Nj8BNTQ3
Njc2MzIWFxYdARcyFxYXFhcWBwYHBgcVFAcGBwYiJyYnJjUXJicmNTQ3Nj8BNTQ3Njc2MzIWFxYdARcyFxYXFhcWBwYHBgcVFAcG
BwYHIyInJjUTNDc2MzIWFRQXMhcWFxYXFgYHBgcVFAcGBwYiJyYnJj0BJicmNTQ3Nj8BFw4BFhcWFzMyNzY3NiYFDgEWHwEeATMy
Njc2JhcOARYfAR4BMzI2NzYm2hoVIwsSKwoBAgYNFg0YAgECBw4RDREICQYEDRcpAQEGDCwMBgEB+hoVIwsSKwoBAgYNFg0YAgEC
Bw4RDREICQYEDRcpAwURBAMKHAkF+ggMGA8ZAgcOEQ0RCAkMFBYhAQEGDCwMBgEBGxQjCxIrCiEREggTBAMKFwsGAQIf/fcREggT
AQQGBhEXAQIf5RESCBMBBAYGERcBAh8BbgkUIjcbGSoTBERFAwkJEBUNA0ZDAQkLEBQYHR8WFSUOl5kDCAkQEAkIA5ljCRQiNxsZ
KhMEwcEECQkQFQ0EwsABCQsQFBgdHxYVJQ4aHAoOCQIBFgweAoEODBIZEwUBCQsQFBgcPhkcC9bWBAgJEBAJCATW1gkUIzcaGCsT
BE0DHyMJAgERCQgWHYEDHyMJAQIBFwwWHf4DHyMJAQIBFwwWHQAEAAAAAAPMA0EAGwAuAEkAVAAAAT4BFxYXHgEXFgAXFgYHBgcG
IyEmJyYnJjc2AAE2NzYnJgAnJiMiBwYABwYXFhcBPgEWFxYdARQGDwEOAisBIi4BLwEuAT0BNBcOAR4BNz4BNTQmAZoQOiEkHgcQ
BAYBUgMPChgaKQQS/Us1GxcBAQ0CAVMBxRUEBAYD/q4DCRATCgP+rgQFAwUVATMEJCUFAQIDAQILCgcMBwkMAgEDAiARDwUbFg0R
IAMKGB4CARcGEAYJ/coGHkkcHgkBDCMdJyIZBQI1/WwIEw4MBgI0AwsMA/3NCAsOEggBpxYQERUEWEEUDAUBBAsFBQsEAQUMFEFY
/wMdIhQFBBcNFBkAAgAA/+4CpwMzAD4ASgAAARYHBgcGDwERNz4BNzYeAQcVBgcGBwYPAQ4BByMGJyMuAS8BJicmJyYnNSY+ARce
AR8BEScmJyY1NDc+ARcWBw4BFhcWNjc2LgEiAnkCAQENEycKHRgOCQ0YDQICAwQPDSUyEAsHAQcHAQcLEDIlDQ8EAwICDRgNCgwY
HgoXEh8jH10lKX0NEAELDyoJBwgWGALKCxAfGSMSBP5BHBgLAQINGA0BBwUHDw4lMg8HAQEBAQcPMiUODwcFBwENGA0CAQoXHgG/
BAoUIjA4IiAHGRwiBBkeCw8HEw4cEgABAAAAAAMkAuIALwAAAQ4BBwYdAScmJyYOARcVFhcWHwEWFxYXFhczFjY/ATY3Njc1Ni4B
BwYPATU0Jy4BAfAKDwIBXFoGEB4RAgEFCBxfRhgdCgYHAg0VfmgWBgQBAhEeEAZaXAEDIwLbBBELA/TyW1oECAkbDwIGBwocYEYY
GwgFAgIMfmgXCAYGAg8bCQgEWlvy8wQSFQAAAgAA/+sCqAMyADoASAAAATY3NhcWHwEeARcVFgYnLgEvAREXMhcWFxYXFgcGBw4B
JyYnLgE1NDc2PwERBw4BBwYuATc1PgE/ATYTDgEWFxYXMzI3Njc2JgHpCAkQDQIdVw4HAgMeFQkOGB0CBw4RDREICQYIHx1NIiQT
BwYLEisKHhgMCg0YDQICBw8yQRIREggTBAMKFwsGAQIfAysEAQIJAR5XDwsHARYdAwELGBz+QQEJCxAUGBwfKxsaChASJQ8aEhoY
KxMEAb8eFwoBAg0YDQEHCxAyQf1uAx8jCQIBEQkIFh0AAAIAAP/tA0oDNAAdADMAAAE2Fh8BFTMWFxYXFhUUBgAOASYvATUjJicm
NTQ2AAMGDwEUOwEeARcVNzY/ATQrAS4BJzUCDBErCAHJEAQKBwsE/rcRHhwGAcgRBBwEAVJyIDAZWVoPCAdoIDAZWVoPBwgDJg4K
GIKDAQEDBwsSDAn+ORQHDxKCgwEBCh0MCgHR/tQsQSMBCQgPrI8sQSMBCQgPrAAAAwAAAAADnwLqADYARgBYAAABNhcWHwE3Njc2
FxYXHgEXFhURBgcGLwEmJyYHBgcGBwYmJyYnJgcGBwYHBicmJwMQPgE3Njc2FyYnJg8CFTc2NzYXFhcRNwYPARE3Njc2FxYXNS8B
JicmASoYJUlADhBFSTw7LCcbFwUBCAgQFQ8YIDg5TEUSBgkPDRkhNzhLRRIJDgsNCwEFDA4XIkPRNjlQTAwBDRAeNjRHN8I1LQwN
EB42NEc3AQodIjkC5QMEBh4GBx8FBA0JEgsSDAIP/dsPCA0GCA4KEgEBJQkCAwMIDgsRAQIkCQIEBgcVARYBEhUOBwwNGG8XBQYh
BeLiBQUGCwECGAHDIAYUBf48BQUGCwECGOLhBQ0HDQADAAD/6gN0AzgAJgA/AFsAABM2NzY3NhcWFxYXHgEVFAYHBgcGBwYHBicm
JyYnJicmJyYnJj0BNgU0JyYnJicmKwEGBw4BFRQWFxYzMjc2NzYFMBcWFxYXFhcWMzI3Njc2NzY1NAcGBwYnJicmixFLQ2ZhZGdI
TxkFAwgVNBQYBxxbQloyHSogLw8EOToBBgECmBITIyYyOkIpSj44QUpARVRaSD4kIf3XBxgQLgEKHCtVNiM9DwctKwpJSo2KHi0J
An9DLSkQDwwNJik/DhMTICxOwklXED4XEAUCBggTHS8M0dUHHSYXBBMRExYSFQsNBBQSNhoeOxITFhMfHYIaUzypAhQLEQYLHA+k
nwUBBR8MFyYIFAQAAQAAAAADeAKKACUAAAEGBwYHAScmJyYnIyYGBwYdAQYXFhcWHwEWFxY2NzYANzYuASMiAz4ECQwr/rGgFQcG
BgIOGQYCAQUFGBFAXxEFCRUJBAGkBQoEFw8JAogBCAsq/rGgFAUEAQINDQQHCggGCRkRQF4RAgQBBQIBogcNIBYAAAEAAAAAAqYC
tQAtAAABDgEXFRYXFh8BBwYHBgcVBh4BNzM2NzY/AjY3Njc1Nic1JicmLwEmJyYnIyYBeBATAwIEBxjCwhgHBAICDRgNAgYFBhI7
mxYFBAIBAQIEBRbGHAoGBgQKArMEHBACBwYJGcLCGQkGBwINGA0CAQQEEjqbFggGBgIHBwIGBggWxhwHBQICAAACAAAAAAPQAuMA
KQBUAAABPgIXFhcWFxYdARceAgcOAQcGByMiJyYvASYnLgE3Njc2NzY3NjMyNwYHBgcGBwYHBgcOAhYXFh8BITc2NzY1NCcmJyYn
JiMiJyYnNDc2Jy4BAQIXWXQ9SDk1HR0NNlMmCgxTOggn574wJREFQDAsJQ4OMRoiGRoQBgHzOC00EAcOChcdEBw1EhQcHy4KAhYK
GBMfDw4WFBYGHBkLEgMBChoZZgI6N04kCAkmJDUzNQkBBUBkNzpPCgEBAQEDAQwsKnY7Py8ZEQwGBFYEICQ7FggFAgMGCjVKShsd
CgMDCBEcLiAZFQ0LAgEFCBkJBzUwLjUAAAEAAAAAAyYC3gBKAAABBg8BBg8BBgcGHQEUFx4BNzM+AT8BFRQXHgEXFhcWIDc+ASYn
JisBIiciLwEuAScmPQEXHgEXMxY3Njc2PQE0JyYvAiYnJisBIgFzBgMEMChFEAQDAgYZDgEHCgtZAQMnICIqCwECBRYPEBUEhGIe
CgcIARMaBAJZCwoHAQUJDggLAwQQOVcRBgUIAwsC2wIDBC4oRhIHBQcKBwQNDQIBBgtYoaQGJUEVFgUBAQUkJAUBAgMBCCAUDpyi
WAsGAQECAggLEwQHBQcSOVYRBAMAAAMAAAAAA54DBwA9AEEARgAAEzAxNjc2NzYhBRYXFhcRBgcGByMGIwYrARUzMhcWFxYGDwEG
BwYrASInLgE0Njc+ATsBNSMiJyInIyYnJicTESERARUzNSNeBQoSIQwBVAFaIxEKBgUMER0BBg0SO1kZEwgPCAsIEAEEBQkcttIK
DREPDQQLDxlZOxINBgEVDxQHUwKa/mCmpgLAFQ4bBwIBCRgOF/5GFhAWCAIBUgIECw8lCgECAQECBBYbFQUCAVIBAgYNEx4Brf5g
AaD94ylSAAQAAAAAA6ICswAkAEUAVwBhAAABMzEWFxYXFhcWFzEWFxYHBgcGBwYHBicmJy4BJyY3Njc2Nz4BFyIHBgcGBwYHBhQX
FhcWFxYXFjc2NzY3PgE0JicmJy4BAyYnJjY3NhcWFxYXFgcGBwYmNw4BFhcWNjc2JgHoHS0hPDdIPyQZEAMEBAMQLzdSYVhWc2Qg
VRQKChctMis0bz8wMSkpIh8YEg8OEhggJC0wOTxKQkg9DB8mDmWBCDVIHwQDKyQpLSMWFAcHBQonJV4zFRIQFxEcAgMdArMBBwwf
KUgrKBkICwwIGEk1ThwZDRFSG2cmFRQrNjwfJydPFREhGiIaGRYGFRsaIxshERUCAiElTA4sBTQQdRIBAf7ZICsoRxARDQsbFx4b
FzEdGwaiAyYmAwIUERUdAAAGAAD/7wNMAzIAKABMAFAAXgBvAH8AAAEyMzY7ATIWHwEWHwEeARcVFhUPAQ4BBwYrASYnJicmJyYn
ETY3Njc2FwYPAQYHBh0BEBcWFxYXITY3Njc2PQEjIicuAScmNSY9ASMGBRUzJwU2MhceAQYHBiInLgE2FzY7ARcWFxYUBgcGIicu
ATYXDgEWHwEhNzY3NjQmJyYiAR4BB1NFgxULBAEDNZcOBwEBAQIKOygH3bwiBRsXKAkBAQEBBxIfNxAIAQIBAQIDCgUMAbQMBgsC
AUJFBBckBwMB7hQBVkEg/tsFMgUVEBAVBTEFFQ8QEgOHhgwDAwwVDwf2BxUTDRoTEwkSBgEQDAMDDBUPB/cDMAECAgEBNZkPCgUC
Be7sCik3BAEBAQYQHTIHLQIMLQcdGCtPBg4CBA0RP9T+3QoNCAUFBQUJDgTT0QEEIBYIEA0zPQE6QSFMAgIEJSUEAQEFJCSiAQcC
AwwhGAIBAQIkJqADICMJBAcCAwwhGAIBAAACAAAAAAOfAwcAJwBGAAATPgE3NjMXHgEfATMyFx4BFxYVEQcGBwYHBgchJicmJyYn
JjURNDc2FwYHBgcRFhcWFxYgNzY3NjcRLgEnIyciJi8BJi8BI5QRHBwTPGAIEi48jogQJzgIAQIHEhwwBy399i0HGRgqCwEBDGEN
BgQDBQUIDQoCRgoNCAUFBwcQ1kcVDAQBAjw6nwLxCwkBAQEDEC47AgY3JwQL/psKHhckCQEBAQEFEBwzBA4B1w0ENCUGCQUJ/iIM
BQoDAgIDCgUMAWIOBwkBAgIBATw6AAADAAAAAAOfAzEAQQBsAIkAAAE2MhceAR8BMzIXMhczFhcWFxYVERQHDgEHDgEHIxUUBwYH
BgcGICcuAS8BLgE1ETQ3Njc2Nz4BOwE1NDc2NzY3NhcGBwYHERYXFhcWOwEyNzY/ATY3NjcRJicmLwEmIyYrASYnJicmLwEjIg8B
BgcGBxEeARchNjc2NzUjIiciJzEuAScmPQEjBgF2BogDCQ4eJmhDFg4IAR0YJQkBAQYjGRAcHBsFCBgeJgr+TAshNgsBBAEBBxAX
JwsVFhoDBA4IDx4pCwcFCAsEBgwE1ZotDQoEAgcFAwUEAwUIAQUGCBmmGQgGBAISNzkZIK8MBgQEBgoOAbYNBwUFmWYfFQgoNAcC
LwUDMAEBAgweJgECBxEcLwQK/skKBBwuDQgGARofER8YHgYBAQUuIQQKJmYBDQwEHRYgDAQCGhcQGhUNDRhQAwgFDv5zEAQGAwEB
AQMBBAcECgE4CQUIBAEDAgEBAgQBEzcBqAUJBQn+dA8JBgQIBQs1AQIJNicQfIIBAAADAAD/8AOfAz0ARACNAJoAAAE2NzYXMxYX
Fh8BFhcWFRQHDgEHBgcGLwEHBgcGBwYjIiYnLgEnJjQ3Njc2Nz4BPwEzNTQ2NzY7ATU+AT8BJyY3Njc+AQEOAQcGHQEzMjY3Nj8B
Njc2FzMyFxYXFhcWNjc+ATc2NCcuAScmBwYHDgEHDgEXFhcWFxYVFAYHBg8BFQcOAQcGKwEVFAcOAQcBDgEWFxY3Njc2NC4BAgAY
IT47AQsGCRWODgUcJQV8BSk2NSgJfnsMGRENKR0PChQcBQIBBA0IEwsHCRsaDQ4KGBcGChggBRsCASEFe/7ICQYBARsXDQYCj3oU
CAUHBA0LBgoPDBMoEAd5BAwMBKAHFBwYDwZ5BQsFCAUPCwQGBAoHGykBCA4LBxQWAQINEAFkFBAQFggKDQcKERcDDBQKEx8GBQcU
jg8HKjI4LgZ7BSEBAhsFfnsIEAQDAQQHHxUHPgseFg0TCwUEARccGgQDNQsLGCEJKDU2KQV8/Y8JCggHFB0BBAGPeRMFBAEGBAsP
BQgECwV7BhQsEwahBQ4BAQoEeAcQKRMMDwoGCw0JCwsHGyomJg8NAgEWFQgKDQkBoAUlJAQBAgQICyEUCAADAAD/7wNMA2EATABp
AHwAAAE2Nz4BFxYXFhcWFxYHDgEmJyYnJicmJyYGBwYHMQYVBh0BITIXFh8BHgEXFhcVBgcGBwYPASEiJyYnJicmJzU0NzQ/ATY3
Nj8BPgE3ASYnIyIHIw4BBwYdARQXFB8BFhcWFyE2NzY3NjUlDgIXFhcWFzMyNzY1NCYnIyYBNBIyMH03JxsXDAoDAgMFHyEJAwEC
BQ8kIVEeIgsCAQEsJg0ICQMiLQcBAQEBBRAcMQr+SQ0EGhcpCgEBAgMBESsYIgEBAQUBxAoW2GptBAwSAgECAwEECAUJAbQLBgsD
Af74HiQFDhAfBwMOIRgaLSIGCQK9QyooDhwUIRsjGxsVCBAQCREGEBYPKhYVBBgaLggNFDxbAQEDAQs0IwYX6xYHGhgoCwIBBhAd
MwcW2x8LCAkCMhYMBFxLHxL+tBYKAQITDAOCXxwJBgYBCAUDBAUFCA4EhU8FKzgXGQoCARQXKCEwAQEAAAIAAP/tA4UDMgA0AFUA
AAE2NzY3NhYXFhUUBw4BBwYHBhcWFx4BNz4BFx4BBwYHBgcGBwYHBicmJyYnMSYnJicmNz4BNwYHDgEXFhcWFxYzMjc2PwEnIicm
JyYnJicmJyY/ATQHARg3Qi8vHBcKDAEDEBQiDxAJCyg4qFcODQcQEQUDDxgiLzxLXDk4TkBQNhYMFwUHFhRYzGJAOyIdH0sjIkBN
ODdVOwQfJRw1Lz4yFxMfBQYsCQgC7CURDQIBBAoMEQgFBhQUKTg6O0E2RzIeBQECBB4RCxwtJjYhKgkECg8nMVUlHzc6TU5HeQ8b
SUS1VVk7GxAdEx5HBAEFCRkhOx0oQkRcVxIBAgADAAAAAANbAvEAGgAmAC4AADcmJzU3NjcBNjc2PwE2FhcWFxYHBgcGBwYPAQE2
LgEHBg8BFhc3NgUHFTM2NyYnphUJAwMgAWgyEAsLAyxkJScKAwEEGQXi5AgHAZ8IESoYHBgKNTYKDv6QsWyxsTY2GgcXvAcFIAFp
MQ4KBQIXDiEjNhAYLiMI4uMFAwIZGTEcAQMWCjY1Cg6ZsmuxsjU2AAABAAAAAANLAt4ATQAAAQ4BBxUGBwYdASMiBwYPAQYHBhQX
Fh8BFhcWOwEVFBcWHwEWFxYyNzY/ATY3Nj0BMzI3Nj8BNjc2NCcmLwEmJyYrATU0JyYvASYnJiMiAfAHDAQDAQHcHAkGBQEGBgkJ
BgYBBQYJHNwBAQIBAwYLIgsGAwECAQHcHAkGBQEGBgkJBgYBBQYJHNwBAQIBAgcMDgwC2wIMBgEFBgkc3AEBAgEDBgsiCwYDAQIB
AdwcCQYFAQYGCQkGBgEFBgkc3AEBAgEDBgsiCwYDAQIBAdwcCQYFAQUGCwACAAAAAAN5AxQAPgB8AAATMhcWFxUXNzY3NhYXFhcW
BwYmJyYnJicmJyYnLgEHBgcGDwEXMhceARcWFxUUBwYHBiInJicmLwE1Njc2NzYTBgcGFxYXHgEXFjc2PwEXFRYXFjI3Njc2NzUn
JicmJyYiBwYHBhQXFhcWMxcGBwYHBgcGIyInLgEnJicuAbARCQYKAQpKamXMSEwVBQ0NJwwEAgIDCA4hQDyXRUszAgICJh0JDw8G
AgEQCQgFrAUNCQUGAQEBAwcLCQ4JDAUOMjGSVFlVUjcKAQoGCSMLBwMBAQEGBQkNA7ADCAkQEAkIAykmAw0TGSMoMjdKQD1RDAIC
BhwC3AcFEh8fC1IcGzlMUXQdEA4CDwUIBQ4oHkgtKxQcHUECAwMBAQEKDQQDChYMBgEBAQIKBgxcUA0FCgcL/rMEChAbVUdEVAcH
IiM8Cx8fEgUHCwcKBQ1QXAwGCgIBAQEGDCwMBgEBAQUPFxMaDxMhIHBFDQcNDgAEAAD/7AOgAzQAeQDvAQMBEAAANzQ3Njc2NTQn
JicmJyY3Njc2NzY3NjU0JyYnJjc+ATc2FxYXFjMyNz4BNzY3Njc2MzYXFhcWFxYXFjMyNzY3NhcWFxYXFgcGFhcWFxYUBwYHBhUU
FxYXFgYHBiMiJyYnJiIHBgcGBwYHBicmJy4BJyYjIgcGBwYnLgEBNCcmIyIHBgcGJyYnJicuAQYHBgcGIyInLgEiBwYVFBcWFxYH
BgcxDgIXFhcWFxYXFh0BFAcGBw4BFRQWNz4BMzIXFhceARcWMjc2NzY3Njc+ARcWFxYzMjYnLgE1NDc2NzY3NiYnJicmJyYnLgE1
NDc2NzYBJicmNjc2MxYXHgEHBgcGBwYHBjcOAhceAT4CJicmtgQDBgccExQWCw0GBQ8VJgsHCgcLAgMPDSoaFRUMEhEGDwcECwUI
DQ8VEg0QFhoUGQkCBgkPBxEUDhcbFhUcBwkZBxARJhYVFBcoGggJAgQREiAyEg0IDA4TCAsDBh0fJismDAgEDAUHDwcRFA4XGyQs
AjwFBxIJDRQXChAdFh4MBRsaAwgSIDMTDggZEggMBwoDAxYYKA0JBgMEFhESIwkCBAIGBAMcDBYZESofHAkDBgcFEwQIBAIDBggO
PB8SGg8FEREGCwcLFTIYAwEICAQMEwsUEAYGBAMGB/7CNhgWGCkuQzAnJSkCAhsUISIoLA8ZIgoKCzA4KAgYFxq6Eg0IDA4JGQcE
EBIaHiIbFB4LAgYJDwYRGREdHhkgBQQEAwgHCAUgCA8MDggGAwUHERUhCwcKCAkCBAgHFBkgKS4OGwMLHh1KHSAMBRoHERQOFzUU
IgQDBgcFBxAeGBkCAiALDgggBQkICQIECAw9AdAKCAwHCgMBAwQQFScUCw8RGRQkBAMNBQcSCQ0UFyYjJAoEBxMJDwYFDRgmBw0K
Fw4ICQUIBxERBgsHGRciDAgEAgIEBwULEwwUIAQCCwccDBYZERoZLA4HEwsQBQIDBggNHw4WDhEOCAwO/p4bNjJuIycBGhhOLC8o
HhMVAgP6BSQxFxocBig2MAwNAAAKAAD/6wOjAzUAEgAiADIARQBXAGoAfQCOAKAArwAAAT4BFhcWFAcxBgcGIicmJzEmNAU0NjMy
HgIVFAYjIi4CBSImNTQ+AjIXFhUUDgIHFAcOAScmJy4BNzY3Njc2FhcWBzYuAScmBwYHBhYXFhcWNjc2JTYyFzEWFxYUBwYHMQYi
Jy4BNgU0NzY3NjIXHgEGBwYiJzEmJyYBIiY1NDY3PgEyFxYVFA4CJTQ3NjMyFx4CFRQGIyIuAgcGBwYUFx4BNjc2NCcuAQHVBCQl
BQEBAQYMLAwGAQH+9hYVCxAgChgSDBAhCQIiEhgJJA0cDA4JIRAqJiR5QTYoJh8MDCkwQTtyIyVVBxs4IiQhJhIRCxocKxw/Fhn+
CwQvBwkIEREICQUvBRUQDwK5EAkIBTEEFg8QFQUvBQkIEf3SEhgJEBIPHAwOCiEPAcIJDBYMCQYhCRgSDA8hCtIVBgEBBSQkBQEB
AyMDDxYQERUFLwUJCBERCAkFL3ASGAogEAwSGAohDzoYEgwOJAgKDRMMDyEKxEQ1My0PDigmaDQ4KjILCy8yNVciQCsEBRITJSJO
HB4IBhUYGmMBAQEGDCwMBgEBAQUkJCQWDAYBAQEFJCQFAQEBBgz+4hgSDA8QEgkLDBMMECEJRg8LEAYEIQ8MEhgJIRAsBxcFMAYW
DxAVBS8FEhUABQAA/+8DeQMxAC8AMwBPAGQAewAAATYzMhceAR8BHgEdATMWFx4BBiMHAgcOAQcGICcmJyYnJgMnIiY2NzY3MzU0
Nz4BFxUzNQUUHwEWFxYfARYXFhchNjc2PwE2NzY/ATY1NCAXPgEWFxYdAQYHBg8BDgEmJyYnNTY3DgEHBh0BFhcWHwEeATY3Njc1
JicuAQGcBVxTEBUhCAEEAqMMBBYLGRgCJAEFOCsH/pkIMR0XAwEkAhgZCxYEDKMCBCUppv63Ew4FAgIEAQkLBBgBLhgECwkBBAIC
BQ4T/hR7ByUiAwEBAQEDAgofHAUBAQHJDBUCAQEBAQMCCh8cBQEBAQEFGQMwAQIEGhMCChQkMAEBBygjE/5BBCo9CAEBCiQeIwQB
vxMiKQcBASwoDholTlNTqA3dqDIOCgQCCwMBAQEBAwsCBAoOMqjdDQFwFA0TFgSCXhwJBgUCDQcPEAQS5hMhAhUOBIJeHAkGBQIN
Bw8QBBLlFAQPEAAAAQAAAAADJQK2AD8AABMOARcVFhcWHwEHBgcGBxUGHgE3MzY3Nj8BFxYXFhczFjYnNSYnJi8BNzY3Njc1Ni4B
ByMGBwYPAScmJyYnIyb6EBIDAgQHGMLCGAcEAgINGA0CBgYHE8vCGQkGBwIVHgMCBAcYwsIYBwQCAg0YDQIHBgkZwsMZCAYHAwsC
swQcEAIHBgkZwsIZCQYHAg0YDQICAwUTysIYBwQCAx0WAgcGCRnCwhkJBgcCDRgNAgIEBxjCwhgHBAICAAAFAAAAAAN1AwcAGwBB
AEUAWQBoAAATMDM2MxceAx8BFAcOAQ8BISYnJicDEDc+ARciBwYHERYXITY3Nj8BNSYnIxUUBgcxBg8BDgErAScmJyYnJj0BMxUz
NQc2FxYXFhUUBgcGJyYnJjY3Njc2FwYHBhUUFjY3Njc1NCcm8gdyadwMGoAYBgEBBTMmDP3mKRkcCgEBBj1JGAoOCAYWAgwJBQkF
AUdGGQEFBwgBBAoSPbwRBQYCAVSmNRkfIhYZLyUoKSwWFQYaHC0ECAsHDB8mCQEBEQ4DBgEBBhiAGgza3QYmOQoCCRgaLQEIAQsG
KjxOBAUU/fQWBgMEBw7OzUdHQTUTCAsFAQICAQkFCBMNMT5SUvoEDA0bIC0oQQ0ODg8lIlMfIggBVAMHDBMWFgoSBAIKGQsJAAAD
AAD/7wMoAzIAJgAqADQAAAE2IBceAQYPARUXFhcWBwYPAQYjBiMhJicuASc1Jjc2NxM1JicmNhcVMzUHBgchJzQvAiMBeAUBAAYV
EBAWB1NRBAUHChsDBgcLIv50IAYXHQIBAwUSkR0JCRNvVGFLSwGaAUtLNjYDMAICBSUkBAHI5dwOFBQbDQIDAgEBBiIXAQgLDzIB
kMgEExEmTqam/M/PAgHPzQEAAAIAAP/zA6oDMwBdAHkAAAEzMhceARcWBwYHBgcGBwYmJy4BJyYnMRUUBw4BBwYHIyInJicmJzU0
Nz4BNzYyFxYXFhUUDwEOAQ8BFx4BNzY3Njc2Nz4BJyYnLgEHBgcOAQcGBw4BLgE3Njc2NzYXBgcxDgEVFBceARcWFxY+AScmLwE1
LgEnMS4BAd4mZFdRcBQVFBVBNkNNXQ8wDUmCMh4CAQEKDQQDCQwKDQYBAQECEwwF1QYICRASAQYOI1gIL6dcYlApHSsLAgICCDc0
oldcSzdECQIBBRweEwIJJS5LW44QBwQBAQIOIi4DECYSCwEnJQECAwYdAzMtKpNZXFphTz8lKwcBAQEJQDQhAywhCg4OBgIBBQcP
BBBebwQNEwIBAQEGDBYZCgEEAQEBDElSBwc/ICs/TA0xDl9KRkgICDkpdEUOAw8OBhoVS0hXOELJBg4IGUVYBAoRIi4CDAwjFAMn
JoYSCwYLDgACAAD/6QOlAzoAQQCCAAATNjc+ARcWFzEWFxYHDgEHBicmJyYnJiMiBw4BFxYXFhcWMzI/AScuASc1NjcxNjchHgEX
FhcWBgcGBwYnJicuAiUiBwYHBgcGBwYXFhcWFxY3Njc2NzY9ASMVFzMWFxYVFAcGBwYHBgcGJicmJyY3Njc2Nz4BHwE3NjQnJicm
Jy4BiCpKRa5XWkwXCRAPAnQGDwsIDxQQGyQ/LywnCgspFRUnM081CG0MBwwBAQUXAWIODAMBAgZfV1tvP0dSQ09cAgGKJy4zLDIf
JAoJEhMsMERNXV5JQiYj+gGMEAYJERIfIyoxNkB4KCsKBAIEFh8/Oos/CicJEBUYICEINAJIVTo3Kw8PNQ8NFhcEaQQIAgEJDAUJ
IiBoNTgoFQsVNQcCBQcShQsEEwkGDAwIGGrBPUALBxEUMDmwx+0REiImNDtJO0JFOD0gIwUINjBKRkUJFRUJBgoPFB4gHSITFwID
OjQ4RRkfNy9AKiYNHAUjCAIJCggLBQEBAAUAAP/vA6ADMQBWAIsAlgDQAN8AAAEyMzYzMhceARcWFxYdATMyFxYfAR4BFxUWFRYV
FAcOAQcGKwEVFAYHDgEHBisBIiYnJicmJy4BPQEjIicuASc1JjUmNTQ3PgE/ATY3NjsBNTQ2NzY3NhcOAQcGHQEzMhceAQYHBisB
BgcOAQcGBxUXFhcWOwE1NzY3PgE3NjcyNzY3NjQnJicmJyMGFzQ2HgEOASYnJicFFAcUBw4BBwYHBiMiBw4BBwYUFx4BFxYXMzY3
Njc2PQEjIicuATY3NjsBNjc+ATc2NzUnJicmJyYiAw4BHgE3Njc2NTQnJicmAZwBBzQqPhUeIxMZBwUzJgwJDAIfLAcCAQEEPiwF
MSwCBAo0IgUmZjIUDBMTHwwEAjMuDCUzCQIBAQQuIwINCQwlMgIECRIeMQsPAgFAQQQVEA8WBJeDFgQKDgMBAQEJEwMRTAEBAQoz
JQleYwMUBgICBhYEDZ4NCh8mEQ0eHwkCAQEjAQECDxAZKQxsUwMKDgIBAQIPCwQNng0EFgYBQEEEFRAPFgSXhBUECQ8DAQEBBgYJ
DgZRsREQAxcTCgkSCggNCgMwAQECDxMZHBEhGwEBBAELMSEBBw0UPGAHLD8FAUU5GQ0gLQcBAQQGDRglCxUWGwIJMyYBBw0UPGAH
JToLAQQBAUU5GQ0aFCBOBBEKBEFAAQUkJAUBAQEDDwoEEE1cFwYBOSkIBiUxCAEBAgkVB9UHFggBAQFSFxYKIx4PCRIEA0kYGCMO
FSESGwkDAgQRCgXXBQoRBAEBAQEIFgVBQAEFJCQFAQEBAw8JBQ1QXA0FCgIB/ogDGiEWAQEFDBgTCgkDAgAAAgAAAAADdQMMABYA
HwAAJSYnJicRNjc2FzIAFx4BFAYHBgArAQYTED8BNjQvASYBKw8JBgkNCA4TAQIqAwYHBwYD/dYBAggnAtLQ0NICGQEJBg8CsBQG
CQb+rQMFEhASBQP+rQIBd/78AoGAAoCBAgAACAAA/8YDTAMyAG0AfwCXAKoAvQDLAOYA9wAAATIzNjsBMhcyHwEWFxYXFhcWFREU
BxQPAQ4BByMGIyInJicmJzU0NzYzNzY3Nj8BNjc2NREmJyMiBhceAQYHBgcjIicmJyY2NzYmKwEHBgcGBwYRFRQXFh8BHgEXFg4B
JyYnJicmJxE2Nz4BNzYXMjM2MzIXFhUUBgcGIicuATYHNDc2NzYyFxYXFhQHBgcOASMiJyYnJicXNjIXHgEVFAYHBgcjJicmJyY2
BzY3NjsBFhceAQYHDgEiJicuARc2MhceAQYHBiInLgE2Bz4BFxYXFhUUBgcOAQcOASImJy4BLwE1NDc2FwYHBg8BFTM1NCc0JzEm
JyYBHgEHU0VwIQoHBQEHaW0EAwEBAgQBCzAgAg0bIgkNCQIBFQwdHgkFCAQBAwEBXFtVCgMDEQ4NEwQFGBwKEQYFDxADBA0sPwwF
CgMCAQECAQQZBAYHGBEUFh0HAQEBAQQdFhjzAQMQCxwMFhQQBicHFhIOXQ0ICQYzBgwICwoFBQYOFh0JDAkCAXYGKQYQFA4PBAY2
EAUIAgMVWQgKCBcZBQQQDwYOCQ0tDggQA3QGMQYVDxAUBTAGFRAPYiJcJSYJAQIEBhgPDBNUEgoTHAYCAQhpDQgEBQFTAQIECQ8D
MAECAwEFaGwJBQoPNP6oMw8LCQQfLQcDAwQSBAMKHAkFAQQDBQgCBAoOMAF/XFwBAQYhIgYBAQQFFBAfBQEBAQUFCA0K/t3UPxEN
BAIJEQkMHxMDAxUdJgctAgstBxYtDxGjAQUJHBAXAgEBAiQndxQLBwICAgMKCx8MBgMEAgMFEQQDIAEBAhcQDRYFAQEJBQgNEB5c
CAIBAQEFHR8JBQMDBQsoOwECBiMiBQIBBSQkcSIDGx0xCDYsEwwPGAYEAgEEBxwUClwXBSUVBAkGCy4vLh8KBwQKBw4AAAcAAAAA
A58DBwAtAE0AWQBqAIgAlgCnAAATNiAXHgEXFhQHBg8BFxYXFhQHBgcGBwYHBiAnLgEnJjQ3Nj8BJyYnJjQ3Njc2FwYHBgcGBxUU
Fx4BFxYgNz4BNzY1NCYvASYnJiclIgcXMhcWFRQGJicmPgEXNDc2OwEXHgEHBg8BIiYnJgcOAQcGFRQWHwEWFxYXITY3Nj8BPgE1
NCcuAScmIBc0NhYXFhUUBiYvAS4BNwYVFBcWFyE3PgEnJi8BIwbwCAIMBztSBgEBBSoICCoFAQECCQ4cJTkJ/fgJO1IGAQEFKggI
KgUBAQYoKUcVEBkLAQECBSMYDgH6DhgjBQICBQELFQwT/v95hSoKChMbJAsJAhaRHAOIhwUODwYGF4WBFAcMuhgiBQICBQEKEQoc
AggdCg8LAQUCAgUjGA7+AwYgJgoCHyYJAQIBxBwHBRIBEAUODwYHF4VyFQMGAQEIUzsKXgo6LAgILDoKXgoWFyYZIQkBAQhTOwte
CjgtCAgsOgpeCjkqLEwCCw8hBAouLw4YJAUCAgUkGA4vLBILARcMBwUBAVMGChoVFgQPDB8XKiAJAQMHHg8RCgEFBgu/BSQYDi8s
EgsBFQsHCQoGChYBCxIsLw4YJAUCfRQZBxUFDBcWCRMBBAYuCh0RCQYKAwceDxEKAQEAAgAAAAADoQK4ACEARQAAASY+ARcWHwEW
FxYXFRYGBwYgJy4BNj8BNjc2MyEvAS4BJwEOARcVFhcWHwEeARcWNz4BJzUuAS8CITI3Nj8BPgEmJyYgAqUDFCAOBldAEgYDAgIU
EQf9uwcTEwcQAQMHCSABwz8eCQQB/dsRFAMCBAUTexUUBwQJERUDAQQJHj8BpTQPCgQCEAYUEgb9uAKEEhoHCQRXQBQHBQcCEB4C
AQEDHiMKAQIBAT8fCQkG/uMDHRACBgYHFHoVEAIBAgMcEAEGCQkfPwEBAwEKIx0DAQAGAAD/7AOhAt4AKABhAJYAoQDLAO4AABM2
MzIXFh8CFAcGBwYiJyYnJj0BIxUUBwYPAQ4BIyInJicmNT8BPgEXNjc2MzIeAx8BNzY3NjIXFh8BNzY3Njc2NzY7ATIXFhcHBgcG
IyImJyYvAQcGBw4BIyInJicmJTY7ARYXHgEGBwYuAScjFTMyFxYVFgcGBwYHIyInLgI2FxYXFhczNSMiJy4BJyY3NDc+AQUmBgcG
BxUzNTQmATY3NhcWFxYXHgEHBgcGBwYHBiYnJjc2NzY3NiYnJicmJyYnJicmPQE0BQYHBhcWFxYXFhcWNzY3Njc2JicmBgcOASci
JyYnJicuASKxBRIlHBkJAgEBAgoMIgwKAgEpAwURAQQGBhEMCgIBAQIIK8UDCAsUCQsNBQcHDAsOBwolCgcNCwwIAwQGAwwEBwYQ
CA4EGhoDCxoODwcGDhMTDgYHDw4aCwIaHAG6CiEgCAUZHwETDBwVAisaPBcQAQcOKwQIIiMNFB8BGxUPCgUFKhwcDA8hBgQBAwYi
/doJEQYBASkCAdAJEAkVWzYWCQoGAQQRDhQSDRAkBQcPEw0KBQMDDxMVHBsTBw0JAv3TCgkMAgMZLjtrXUpQQDYvCA0OFQkODjyE
QBUjMDA9Ng4LFALcARgWIQpmaAQMCgwMCgwDGBYWGQkOCQECAQwKDARoZgoeKRwPCQwFCwocJT8XHggJCggdFz8pCxEGBQgCBQkb
g4MGFgkNCh8pKR8KDQkWBICFKwIBAQcoLQ0IAxEMKiEWKyMWJg0BAQMGJSwcBAILBgwqBAQhFBEgGQoXIVEHBQkCBj8iHQv+oxEF
AwEGGwsHBxQTODcvJCAGBw0RExkgKB4dGAMFBgQFAQEDBg8EBwwHOQQLDQ8UEiIeNgUCDw0VEgwRKQcDAwYbGwEICxUbKAsFAAQA
AAAAA8sDXgAZAEAATABhAAABNhceAQcGAgcOASMGIyInLgE1NDc2PwI2FzY3NjMyFxYXARYHBgcGByEmJyYnJjU0NzY3Nj8BNicm
JyY3Njc2DwIGHQEzMj8BNiMXBg8BFBcWFx4BFRQPAQ4BOwEDBwYB5xQQDw8BAf4EBw8WEDZXCg0RAgVRVZB0cQQGDBANCAUJASgE
BwcSBCf9+isECgcMAwYOAmK1A1A3AQYCAj889jVFRSUlAVZVAcQKEQw0ZAMDARM9PAOPj+AMDgNUCgYEGw0H/YYFCQYBAgMWDQcI
DsDJhWqLBwUJBQML/YUODxIFAQEBAQQHCxEJCA4HAiREAWNFAwoOCZSOXTKiogMCAdbVohknHAFAfgcFBgcVDRgYAQHgGiAAAAAA
AQAAACcBTQAVAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAABBgGPAgUCUgLGAxgDpgQyBHMEvQU/Ba4GFwayB3IH4QiqCZEKTArS
CyMLlQxSDeEO4g+fEAMQnxD0EaoSbhOtE+YVTRZKFroYFxitAAEAAAABAABjGSblXw889QALA+gAAAAA5O4SgwAAAADk7hKDAAD/
mQjUISEAAAAIAAIAAAAAAAAAAAAAA/IAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQAAA4T/nAAACNYAAAAACNQAAQAAAAAAAAAAAAAAAAAAAAIABAPyAZAABQAABZsC
vAAAAIwFmwK8AAAB4AAxAQIAAAIABQMAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUGZFZADA6gP6TQOE/5wAWiEhAGcAAAABAAAAAAAA
AAAAAAAAAAIAAAACAAAAAwAAABQAAwABAAAAFAAEATAAAABIAEAABQAI6gPqBuoR6hbqIOo56kfqXuph6nbqf+qJ6prqouqu6sfq
4er46wTrC+sT6yDrMOtB61XrYuvS6+rsH+0B7UbtTvB88fT6Tf//AADqA+oG6hHqFuog6jjqR+pe6mHqdup/6onqmuqi6q3qx+rh
6vjrBOsL6xPrIOsw60HrVeti69Lr6uwf7QHtRu1O8Hzx9PpM//8V/hX8FfIV7hXlFc4VwRWrFakVlRWNFYQVdBVtFWMVSxUyFRwV
ERULFQQU+BTpFNkUxhS6FEsUNBQAEx8S2xLUD6cOMAXZAAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFAEIAAwABBAkAAQAYAAAAAwABBAkAAgAOABgAAwABBAkAAwAYAAAA
AwABBAkABAAYAAAAAwABBAkABgAYAAAAdABhAGIAbABlAHIALQBpAGMAbwBuAHMAUgBlAGcAdQBsAGEAcgADAAAAAAAAAAoAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAEAAAAOAAoADAAAAAAAAkRGTFQADmxhdG4AEgAIAAAAAAAAAAD//wAAAAA="""
)


def load_icon_font():
    """Register the icon font and set ICON_FAMILY. Returns True on success."""
    global ICON_FAMILY
    from PySide6.QtCore import QByteArray
    fid = -1
    try:
        raw = base64.b64decode(_ICON_FONT_B64)
        fid = QFontDatabase.addApplicationFontFromData(QByteArray(raw))
    except Exception:
        fid = -1
    if fid == -1 and os.path.exists(FONT_PATH):
        fid = QFontDatabase.addApplicationFont(FONT_PATH)
    fams = QFontDatabase.applicationFontFamilies(fid) if fid != -1 else []
    if fams:
        ICON_FAMILY = fams[0]
        return True
    return False


def ic(name):
    """Return the unicode glyph for a tabler icon name."""
    cp = TI.get(name)
    return chr(int(cp, 16)) if cp else "?"


def make_app_icon():
    """Build the window/taskbar icon: green bolt on a dark rounded badge.
    Multi-size so it stays crisp in titlebars, taskbars and alt-tab."""
    icon = QIcon()
    for s in (16, 24, 32, 48, 64, 128, 256):
        pm = QPixmap(s, s)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#16171d"))
        r = s * 0.16
        p.drawRoundedRect(0, 0, s, s, r, r)
        f = QFont(ICON_FAMILY)
        f.setPixelSize(int(s * 0.64))
        p.setFont(f)
        p.setPen(QColor("#2fe06a"))
        p.drawText(pm.rect(), Qt.AlignCenter, ic("bolt"))
        p.end()
        icon.addPixmap(pm)
    return icon


# ───────────────────────────────────────────────────────────── themes ──
LIGHT = {
    "bg": "#f6f7f9", "panel": "#ffffff", "p2": "#f1f3f6", "line": "#cfd3da",
    "line2": "#bcc1ca", "txt": "#1a1d23", "muted": "#6b7280", "faint": "#9ca3af",
    "accent": "#6366f1", "accenth": "#4f46e5", "asoft": "#eef0fe", "atx": "#4338ca",
    "ok": "#15803d", "okbg": "#e9f7ee", "warn": "#b45309", "warnbg": "#fdf3e3",
    "danger": "#dc2626", "side": "#16171d", "sidetx": "#b9bcc7", "sideact": "#23252e",
    "sideico": "#7c7f8c",
}
DARK = {
    "bg": "#0f1014", "panel": "#1a1c22", "p2": "#23262e", "line": "#333845",
    "line2": "#414755", "txt": "#e9eaee", "muted": "#9aa0ab", "faint": "#676d78",
    "accent": "#818cf8", "accenth": "#a5aefc", "asoft": "#262a48", "atx": "#c7caff",
    "ok": "#4ade80", "okbg": "#16271c", "warn": "#fbbf24", "warnbg": "#2a2113",
    "danger": "#f87171", "side": "#0b0c10", "sidetx": "#b9bcc7", "sideact": "#1c1f27",
    "sideico": "#6a6e7b",
}

MONO = "'SF Mono','DejaVu Sans Mono',Menlo,Consolas,monospace"


def stylesheet(v):
    """Build the global QSS from a theme variable map."""
    return f"""
    * {{ font-family: 'Inter','Segoe UI',-apple-system,sans-serif,'{ICON_FAMILY}';
        font-size: 13px; color: {v['txt']}; outline: none; }}
    QWidget#root {{ background: {v['bg']}; }}

    /* sidebar */
    QWidget#side {{ background: {v['side']}; }}
    QLabel#brandName {{ color: #fff; font-size: 13px; font-weight: 700; letter-spacing: 0.5px; }}
    QLabel#brandLogo {{ color: #2fe06a; font-family: '{ICON_FAMILY}'; font-size: 18px;
        background: #1d1f27; border: 1px solid #2a2c35;
        border-radius: 9px; min-width: 30px; max-width: 30px; min-height: 30px; max-height: 30px;
        qproperty-alignment: AlignCenter; }}
    QLabel#brandVer {{ color: {v['sideico']}; font-size: 10px; border: 1px solid #2a2c35;
        border-radius: 8px; padding: 2px 7px; }}

    QPushButton#navitem {{ color: {v['sidetx']}; background: transparent; border: none;
        border-radius: 10px; padding: 10px 12px; text-align: left; font-size: 13px; }}
    QPushButton#navitem:hover {{ background: {v['sideact']}; }}
    QPushButton#navitem[active="true"] {{ background: {v['sideact']}; color: #fff; font-weight: 500; }}

    QPushButton#themebtn {{ color: {v['sidetx']}; background: {v['sideact']}; border: none;
        border-radius: 10px; padding: 9px 12px; text-align: left; font-size: 12px; }}
    QLabel#sfoot {{ color: {v['sideico']}; font-size: 10px; }}

    /* content */
    QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
    QLabel#h2 {{ font-size: 19px; font-weight: 600; }}
    QLabel#sub {{ font-size: 12px; color: {v['muted']}; }}
    QLabel#lbl {{ font-size: 11px; color: {v['muted']}; font-weight: 500; }}
    QLabel#sectl {{ font-size: 11px; color: {v['faint']}; font-weight: 600; }}
    QLabel#pill {{ font-size: 10px; background: {v['asoft']}; color: {v['atx']};
        border-radius: 10px; padding: 3px 10px; }}

    /* rows */
    QFrame#row {{ background: {v['panel']}; border: 1px solid {v['line']}; border-radius: 8px; }}
    QFrame#row[warn="true"] {{ border: 1px solid {v['warn']}; }}
    QLabel#rowicon {{ color: {v['faint']}; font-family: '{ICON_FAMILY}'; font-size: 17px; }}
    QLabel#path {{ font-family: {MONO}; font-size: 12px; color: {v['txt']}; }}
    QLineEdit#path {{ font-family: {MONO}; font-size: 12px; color: {v['txt']};
        border: none; background: transparent; }}

    /* tags */
    QPushButton#tag {{ font-size: 10px; font-weight: 500; border-radius: 11px; padding: 4px 10px;
        border: none; }}
    QPushButton#tag[state="on"] {{ background: {v['accent']}; color: #fff; }}
    QPushButton#tag[state="off"] {{ background: transparent; color: {v['faint']}; }}
    QPushButton#tag[state="off"]:hover {{ color: {v['muted']}; }}
    QPushButton#tag[state="dis"] {{ background: transparent; color: {v['line2']}; }}

    QPushButton#rm {{ color: {v['faint']}; font-family: '{ICON_FAMILY}'; font-size: 17px;
        border: none; background: transparent; border-radius: 7px; padding: 3px; }}
    QPushButton#rm:hover {{ color: {v['danger']}; background: {v['warnbg']}; }}

    /* add source button */
    QPushButton#add {{ background: transparent; border: 1.5px dashed {v['line2']};
        border-radius: 8px; color: {v['muted']}; font-size: 12px; min-height: 40px; }}
    QPushButton#add:hover {{ border-color: {v['accent']}; color: {v['accent']};
        background: {v['asoft']}; }}

    /* warn bar */
    QFrame#warnbar {{ background: {v['warnbg']}; border-radius: 8px; }}
    QLabel#warntext {{ color: {v['warn']}; font-size: 12px; }}
    QLabel#warnicon {{ color: {v['warn']}; font-family: '{ICON_FAMILY}'; font-size: 16px; }}
    QLabel#arrow {{ color: {v['faint']}; font-family: '{ICON_FAMILY}'; font-size: 18px; }}

    /* option pills */
    QPushButton#opt {{ font-size: 11px; font-weight: 500; padding: 7px 13px; border-radius: 11px;
        background: {v['p2']}; color: {v['muted']}; border: 1px solid transparent; }}
    QPushButton#opt:hover {{ border-color: {v['line2']}; }}
    QPushButton#opt[active="true"] {{ background: {v['asoft']}; color: {v['atx']};
        border-color: transparent; }}
    QLabel#optlbl {{ font-size: 12px; color: {v['muted']}; }}

    QComboBox#mini {{ min-height: 32px; border: 1px solid {v['line']}; border-radius: 9px;
        background: {v['panel']}; font-size: 12px; padding: 0 8px; color: {v['txt']}; }}
    QComboBox#mini::drop-down {{ border: none; width: 20px; }}
    QComboBox#mini::down-arrow {{ image: none; border-left: 4px solid transparent;
        border-right: 4px solid transparent; border-top: 5px solid {v['muted']};
        margin-right: 6px; }}
    QComboBox QAbstractItemView {{ background: {v['panel']}; color: {v['txt']};
        border: 1px solid {v['line']}; selection-background-color: {v['asoft']};
        selection-color: {v['atx']}; }}

    /* buttons */
    QPushButton#btn {{ min-height: 42px; border-radius: 8px; border: 1px solid {v['line2']};
        background: {v['panel']}; color: {v['txt']}; font-size: 13px; font-weight: 500;
        padding: 0 17px; }}
    QPushButton#btn:hover {{ border-color: {v['accent']}; color: {v['accent']}; }}
    QPushButton#btn:disabled {{ color: {v['faint']}; border-color: {v['line']}; }}
    QPushButton#btnprimary {{ min-height: 42px; border-radius: 8px; border: 1px solid {v['accent']};
        background: {v['accent']}; color: #fff; font-size: 13px; font-weight: 500; padding: 0 17px; }}
    QPushButton#btnprimary:hover {{ background: {v['accenth']}; border-color: {v['accenth']}; }}
    QPushButton#btnprimary:disabled {{ background: {v['line2']}; border-color: {v['line2']};
        color: {v['panel']}; }}
    QPushButton#btnsm {{ min-height: 34px; border-radius: 8px; border: 1px solid {v['line2']};
        background: {v['panel']}; color: {v['txt']}; font-size: 12px; font-weight: 500; padding: 0 13px; }}
    QPushButton#btnsm:hover {{ border-color: {v['accent']}; color: {v['accent']}; }}
    QPushButton#btnsmprimary {{ min-height: 34px; border-radius: 8px; border: 1px solid {v['accent']};
        background: {v['accent']}; color: #fff; font-size: 12px; font-weight: 500; padding: 0 13px; }}
    QPushButton#btnsmprimary:hover {{ background: {v['accenth']}; border-color: {v['accenth']}; }}

    /* advanced panel */
    QFrame#adv {{ border: 1px solid {v['line']}; border-radius: 8px; background: {v['panel']}; }}
    QLabel#gl {{ font-size: 10px; color: {v['faint']}; font-weight: 600; }}
    QLabel#advlbl {{ font-size: 11px; color: {v['muted']}; }}
    QLineEdit#advinput {{ min-height: 34px; border: 1px solid {v['line']}; border-radius: 9px;
        background: {v['bg']}; font-size: 12px; padding: 0 10px; color: {v['txt']}; }}
    QLineEdit#advinput:focus {{ border-color: {v['accent']}; }}
    QFrame#chips {{ border: 1px solid {v['line']}; border-radius: 9px; background: {v['bg']}; }}
    QFrame#chip {{ background: {v['p2']}; border-radius: 7px; }}
    QLabel#chiptext {{ font-family: {MONO}; font-size: 11px; color: {v['txt']}; }}
    QPushButton#chipx {{ color: {v['faint']}; font-family: '{ICON_FAMILY}'; font-size: 13px;
        border: none; background: transparent; }}
    QPushButton#chipx:hover {{ color: {v['danger']}; }}
    QPushButton#chipadd {{ color: {v['muted']}; border: none; background: transparent; font-size: 12px; }}
    QPushButton#chipadd:hover {{ color: {v['accent']}; }}

    /* progress */
    QFrame#prog {{ background: {v['panel']}; border: 1px solid {v['line']}; border-radius: 10px; }}
    QLabel#pstate {{ font-size: 13px; font-weight: 600; }}
    QLabel#pfiles {{ font-size: 12px; color: {v['muted']}; font-family: {MONO}; }}
    QFrame#bar {{ background: {v['p2']}; border-radius: 5px; }}
    QFrame#barf {{ border-radius: 5px; }}
    QLabel#statk {{ font-size: 10px; color: {v['faint']}; font-weight: 500; }}
    QLabel#statv {{ font-size: 17px; font-weight: 600; }}
    QLabel#statvok {{ font-size: 17px; font-weight: 600; color: {v['ok']}; }}
    QFrame#logbox {{ background: {v['bg']}; border: 1px solid {v['line']}; border-radius: 9px; }}
    QLabel#logline {{ font-family: {MONO}; font-size: 12.5px; color: {v['muted']}; }}

    /* connection cards */
    QFrame#conn {{ background: {v['panel']}; border: 1px solid {v['line']}; border-radius: 8px; }}
    QLabel#connicon {{ color: {v['accent']}; font-family: '{ICON_FAMILY}'; font-size: 19px; }}
    QLabel#connname {{ font-size: 13px; font-weight: 600; }}
    QLabel#connmeta {{ font-size: 11px; color: {v['faint']}; font-family: {MONO}; }}
    QLabel#badgeok {{ font-size: 10px; background: {v['okbg']}; color: {v['ok']};
        border-radius: 10px; padding: 4px 10px; }}
    QLabel#badgeidle {{ font-size: 10px; background: {v['p2']}; color: {v['muted']};
        border-radius: 10px; padding: 4px 10px; }}
    QLabel#badgetest {{ font-size: 10px; background: {v['warnbg']}; color: {v['warn']};
        border-radius: 10px; padding: 4px 10px; }}
    QPushButton#ib, QPushButton#ibdel {{ color: {v['faint']}; font-family: '{ICON_FAMILY}';
        font-size: 17px; border: none; background: transparent; border-radius: 8px; padding: 5px; }}
    QPushButton#ib:hover {{ color: {v['accent']}; background: {v['asoft']}; }}
    QPushButton#ibdel:hover {{ color: {v['danger']}; background: {v['warnbg']}; }}

    /* browser */
    QFrame#crumb {{ background: {v['panel']}; border: 1px solid {v['line']}; border-radius: 8px; }}
    QLabel#crumbico {{ color: {v['muted']}; font-family: '{ICON_FAMILY}'; font-size: 15px; }}
    QPushButton#crumbc {{ color: {v['accent']}; font-family: {MONO}; font-size: 12px;
        border: none; background: transparent; }}
    QLabel#crumbsep {{ color: {v['muted']}; font-family: '{ICON_FAMILY}'; font-size: 15px; }}
    QLabel#crumbpart {{ color: {v['muted']}; font-family: {MONO}; font-size: 12px; }}
    QFrame#fitem {{ background: {v['panel']}; border: 1px solid {v['line']}; border-radius: 8px; }}
    QFrame#fitem[sel="true"] {{ border-color: {v['accent']}; background: {v['asoft']}; }}
    QLabel#fi {{ color: {v['muted']}; font-family: '{ICON_FAMILY}'; font-size: 18px; }}
    QLabel#fidir {{ color: {v['accent']}; font-family: '{ICON_FAMILY}'; font-size: 18px; }}
    QLabel#fn {{ font-family: {MONO}; font-size: 13px; color: {v['txt']}; }}
    QLabel#fs {{ color: {v['faint']}; font-size: 11px; font-family: {MONO}; }}

    /* history */
    QFrame#hrun {{ background: {v['panel']}; border: 1px solid {v['line']}; border-radius: 8px; }}
    QLabel#hp {{ font-family: {MONO}; font-size: 12px; font-weight: 500; }}
    QLabel#hs {{ font-size: 11px; color: {v['muted']}; }}

    /* dialogs */
    QDialog {{ background: {v['panel']}; }}
    QLabel#sheettitle {{ font-size: 16px; font-weight: 600; }}
    QLabel#fieldlbl {{ font-size: 11px; color: {v['muted']}; font-weight: 500; }}
    QLineEdit#fieldinput, QComboBox#fieldinput {{ min-height: 38px; border: 1px solid {v['line']};
        border-radius: 9px; background: {v['bg']}; font-size: 13px; padding: 0 11px; color: {v['txt']}; }}
    QLineEdit#fieldinput:focus, QComboBox#fieldinput:focus {{ border-color: {v['accent']}; }}
    QComboBox#fieldinput::drop-down {{ border: none; width: 22px; }}
    QComboBox#fieldinput::down-arrow {{ image: none; border-left: 4px solid transparent;
        border-right: 4px solid transparent; border-top: 5px solid {v['muted']}; margin-right: 7px; }}

    QScrollBar:vertical {{ background: transparent; width: 9px; margin: 0; }}
    QScrollBar::handle:vertical {{ background: {v['line2']}; border-radius: 4px; min-height: 24px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    QScrollBar:horizontal {{ background: transparent; height: 9px; }}
    QScrollBar::handle:horizontal {{ background: {v['line2']}; border-radius: 4px; min-width: 24px; }}
    """


# ─────────────────────────────────────────────────────── helper widgets ──
def repolish(w):
    w.style().unpolish(w)
    w.style().polish(w)
    w.update()


class ClickLabel(QLabel):
    clicked = Signal()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(e)


class ClickFrame(QFrame):
    clicked = Signal()
    doubleClicked = Signal()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.doubleClicked.emit()
        super().mouseDoubleClickEvent(e)


def iconbtn(name, oid="ib", size=None):
    b = QPushButton(ic(name))
    b.setObjectName(oid)
    b.setCursor(Qt.PointingHandCursor)
    b.setFont(QFont(ICON_FAMILY))
    if size:
        b.setFixedSize(*size)
    return b


def textbtn(text, icon=None, oid="btn"):
    label = (ic(icon) + "  " + text) if icon else text
    b = QPushButton(label)
    b.setObjectName(oid)
    b.setCursor(Qt.PointingHandCursor)
    b.setFont(_mixed_font())
    return b


def _mixed_font():
    # Inter for text but the glyphs come from the icon font; Qt falls back per-glyph,
    # so we set a normal UI font and rely on font-substitution for the PUA glyphs.
    f = QFont("Inter")
    return f


def fmt_size(n):
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024.0
        i += 1
    return (f"{n:.1f}" if i else f"{int(n)}") + " " + units[i]


def parse_size(text):
    """'8.3 GB' / '512KB' / '1.2GiB' → bytes (int). None on failure."""
    m = re.match(r"\s*([\d.]+)\s*([KMGTP]?)i?B\s*$", text, re.I)
    if not m:
        return None
    mult = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3,
            "T": 1024**4, "P": 1024**5}[m.group(2).upper()]
    try:
        return int(float(m.group(1)) * mult)
    except ValueError:
        return None


def fmt_time(s):
    s = round(s)
    return f"{s}s" if s < 60 else f"{s // 60}m {s % 60}s"


def mask(v):
    if not v:
        return ""
    v = str(v)
    return v[:2] + "…" + v[-2:] if len(v) > 6 else "••••"


def base(p):
    p = re.sub(r"[\\/]+$", "", p)
    return re.split(r"[\\/]", p)[-1]


def is_remote(p):
    return bool(re.search(r"@.+:", p) or re.search(r"://", p))


# ──────────────────────────────────────────────────────────── toast ──
class Toast(QLabel):
    def __init__(self, parent):
        super().__init__(parent)
        self.setStyleSheet(
            "background:#16171d; color:#fff; font-size:12px; font-weight:500;"
            "padding:10px 16px; border-radius:11px;")
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._eff = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._eff)
        self._eff.setOpacity(0.0)
        self._anim = QPropertyAnimation(self._eff, b"opacity")
        self._anim.setDuration(200)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fade_out)
        self.hide()

    def show_msg(self, msg):
        self.setText(ic("check") + "  " + msg)
        self.adjustSize()
        self._reposition()
        self.show()
        self.raise_()
        self._anim.stop()
        self._anim.setStartValue(self._eff.opacity())
        self._anim.setEndValue(1.0)
        self._anim.start()
        self._timer.start(1700)

    def _fade_out(self):
        self._anim.stop()
        self._anim.setStartValue(self._eff.opacity())
        self._anim.setEndValue(0.0)
        self._anim.start()

    def _reposition(self):
        p = self.parent()
        x = (p.width() - self.width()) // 2
        y = p.height() - self.height() - 18
        self.move(x, y)


# ───────────────────────────────────────────────────── modal dialogs ──
class PasswordDialog(QDialog):
    """Generic password / passphrase prompt (matches #pwModal)."""

    def __init__(self, parent, title, sub, label, allow_empty=False, confirm=False):
        super().__init__(parent)
        self.allow_empty = allow_empty
        self.confirm = confirm
        self.setWindowTitle(title)
        self.setModal(True)
        self.setFixedWidth(400)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 22)
        lay.setSpacing(0)

        t = QLabel(title)
        t.setObjectName("sheettitle")
        lay.addWidget(t)
        if sub:
            lay.addSpacing(8)
            s = QLabel(sub)
            s.setObjectName("sub")
            s.setWordWrap(True)
            s.setTextFormat(Qt.RichText)
            lay.addWidget(s)
        lay.addSpacing(14)

        ll = QLabel(label)
        ll.setObjectName("fieldlbl")
        lay.addWidget(ll)
        lay.addSpacing(5)
        self.input = QLineEdit()
        self.input.setObjectName("fieldinput")
        self.input.setEchoMode(QLineEdit.Password)
        self.input.setPlaceholderText("••••••••")
        self.input.returnPressed.connect(self._confirm)
        lay.addWidget(self.input)

        self.input2 = None
        if confirm:
            lay.addSpacing(12)
            ll2 = QLabel("Confirm passphrase")
            ll2.setObjectName("fieldlbl")
            lay.addWidget(ll2)
            lay.addSpacing(5)
            self.input2 = QLineEdit()
            self.input2.setObjectName("fieldinput")
            self.input2.setEchoMode(QLineEdit.Password)
            self.input2.setPlaceholderText("••••••••")
            self.input2.returnPressed.connect(self._confirm)
            lay.addWidget(self.input2)

        self.err = QLabel("")
        self.err.setObjectName("sub")
        self.err.setWordWrap(True)
        self.err.setStyleSheet("color:#e5484d;")
        self.err.hide()
        lay.addSpacing(8)
        lay.addWidget(self.err)
        lay.addSpacing(18)

        row = QHBoxLayout()
        row.addStretch(1)
        cancel = textbtn("Cancel", oid="btn")
        cancel.clicked.connect(self.reject)
        ok = textbtn("Confirm", "lock-open", oid="btnprimary")
        ok.clicked.connect(self._confirm)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)
        QTimer.singleShot(40, self.input.setFocus)

    def _confirm(self):
        txt = self.input.text()
        if self.confirm:
            if not txt and not self.allow_empty:
                return self._fail("Enter a passphrase.")
            if txt != self.input2.text():
                return self._fail("Passphrases do not match.")
            return self.accept()
        if txt or self.allow_empty:
            self.accept()

    def _fail(self, msg):
        self.err.setText(msg)
        self.err.show()


FIELDSETS = {
    "s3": [
        ("endpoint_url", "Endpoint URL (blank = AWS)", "MinIO / R2 / Wasabi", False),
        ("access_key_id", "Access key ID", "", False),
        ("secret_access_key", "Secret access key", "", True),
        ("region", "Region", "us-east-1", False),
        ("container", "Default bucket", "", False),
        ("prefix", "Default prefix", "", False),
    ],
    "az": [
        ("connection_string", "Connection string (blank = account+key)", "", True),
        ("account", "Account name", "", False),
        ("key", "Account key", "", True),
        ("container", "Default container", "", False),
        ("prefix", "Default prefix", "", False),
    ],
    "gs": [
        ("project", "GCP project id", "", False),
        ("credentials", "Service-account JSON (blank = ADC)", "/path/sa.json", False),
        ("container", "Default bucket", "", False),
        ("prefix", "Default prefix", "", False),
    ],
    "ssh": [
        ("host", "Host", "", False),
        ("user", "User", "", False),
        ("port", "Port", "", False),
        ("key", "Private key path (blank = password/agent)", "~/.ssh/id_ed25519", False),
        ("password", "Password — or passphrase if the key is encrypted", "", True),
        ("path", "Default remote path", "", False),
    ],
    "smb": [
        ("host", "Host (name or IP)", "", False),
        ("user", "User", "", False),
        ("password", "Password (blank = anonymous/guest)", "", True),
        ("domain", "Domain (blank = none)", "", False),
        ("port", "Port", "445", False),
        ("share", "Default share", "", False),
    ],
}
TYPE_LABELS = [
    ("s3", "Amazon S3 / compatible"), ("az", "Azure Blob"),
    ("gs", "Google Cloud Storage"), ("ssh", "SSH / SFTP"),
    ("smb", "SMB / CIFS"),
]


class ConnectionDialog(QDialog):
    """Add / edit connection sheet (matches #modal)."""

    def __init__(self, parent, name, conn):
        super().__init__(parent)
        self.setModal(True)
        self.setFixedWidth(440)
        self.editing = name
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        self.body = body = QWidget()
        scroll.setWidget(body)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(24, 22, 24, 22)
        lay.setSpacing(0)

        title = QLabel(("Edit " + name) if name else "Add connection")
        title.setObjectName("sheettitle")
        lay.addWidget(title)
        lay.addSpacing(16)

        lay.addWidget(self._lbl("Name"))
        lay.addSpacing(5)
        self.f_name = self._inp(name or "")
        lay.addWidget(self.f_name)
        lay.addSpacing(13)

        lay.addWidget(self._lbl("Type"))
        lay.addSpacing(5)
        self.f_type = QComboBox()
        self.f_type.setObjectName("fieldinput")
        for val, label in TYPE_LABELS:
            self.f_type.addItem(label, val)
        lay.addWidget(self.f_type)
        lay.addSpacing(13)

        c = conn or {"type": "s3"}
        ctype = c.get("type", "s3")
        # field widgets per type
        self.fieldsets = {}
        self.fields = {}
        for t, specs in FIELDSETS.items():
            box = QWidget()
            bl = QVBoxLayout(box)
            bl.setContentsMargins(0, 0, 0, 0)
            bl.setSpacing(0)
            for key, label, ph, pw in specs:
                bl.addWidget(self._lbl(label))
                bl.addSpacing(5)
                inp = self._inp("", ph)
                if pw:
                    inp.setEchoMode(QLineEdit.Password)
                bl.addWidget(inp)
                bl.addSpacing(13)
                self.fields[(t, key)] = inp
            self.fieldsets[t] = box
            lay.addWidget(box)

        self._load_values(ctype, c)
        idx = [v for v, _ in TYPE_LABELS].index(ctype)
        self.f_type.setCurrentIndex(idx)
        self.f_type.currentIndexChanged.connect(self._sync_type)
        self._sync_type()

        lay.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = textbtn("Cancel", oid="btn")
        cancel.clicked.connect(self.reject)
        save = textbtn("Save", "device-floppy", oid="btnprimary")
        save.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(save)
        lay.addLayout(row)
        self._fit_height()

    def _fit_height(self):
        # Grow the dialog to show every field of the selected type PLUS the
        # Cancel/Save buttons (no scrolling to reach them), capped at the
        # available screen height — only a type taller than the screen scrolls.
        self.body.layout().activate()
        needed = self.body.sizeHint().height() + 6   # + scroll-area frame
        avail = 1000
        scr = self.screen()
        if scr is not None:
            avail = scr.availableGeometry().height() - 60
        self.setMaximumHeight(avail)
        self.resize(self.width(), min(needed, avail))

    def _lbl(self, text):
        l = QLabel(text)
        l.setObjectName("fieldlbl")
        return l

    def _inp(self, val, ph=""):
        e = QLineEdit(val)
        e.setObjectName("fieldinput")
        if ph:
            e.setPlaceholderText(ph)
        return e

    def _load_values(self, ctype, c):
        for (t, key), inp in self.fields.items():
            if t != ctype:
                if t == "ssh" and key == "port":
                    inp.setText("22")
                continue
            if key == "port":
                dp = 445 if ctype == "smb" else 22
                inp.setText(str(c.get("port", dp) or dp))
            else:
                inp.setText(str(c.get(key, "") or ""))

    def _sync_type(self):
        t = self.f_type.currentData()
        for key, box in self.fieldsets.items():
            box.setVisible(key == t)
        self._fit_height()

    def result_data(self):
        """Return (name, conn_dict) or (None, error_message)."""
        name = self.f_name.text().strip()
        if not name:
            return None, "Name is required"
        t = self.f_type.currentData()
        e = {"type": t}

        def put(k, src_key):
            v = self.fields[(t, src_key)].text().strip()
            if v:
                e[k] = v

        if t == "s3":
            if not self.fields[(t, "access_key_id")].text().strip() or \
               not self.fields[(t, "secret_access_key")].text():
                return None, "S3 needs key + secret"
            put("endpoint_url", "endpoint_url")
            put("access_key_id", "access_key_id")
            put("secret_access_key", "secret_access_key")
            put("region", "region")
            put("container", "container")
            if e.get("container"):
                put("prefix", "prefix")
        elif t == "az":
            if self.fields[(t, "connection_string")].text().strip():
                put("connection_string", "connection_string")
            elif self.fields[(t, "account")].text().strip() and self.fields[(t, "key")].text():
                put("account", "account")
                put("key", "key")
            else:
                return None, "Azure needs conn string or account+key"
            put("container", "container")
            if e.get("container"):
                put("prefix", "prefix")
        elif t == "gs":
            if not self.fields[(t, "project")].text().strip():
                return None, "GCS needs a project"
            put("project", "project")
            put("credentials", "credentials")
            put("container", "container")
            if e.get("container"):
                put("prefix", "prefix")
        elif t == "ssh":
            if not self.fields[(t, "host")].text().strip():
                return None, "SSH needs a host"
            put("host", "host")
            e["user"] = self.fields[(t, "user")].text().strip() or "root"
            _pt = self.fields[(t, "port")].text().strip()
            try:
                # Default ONLY when the field is empty — a typed value (even 0)
                # must pass through, not be silently rewritten by `or 22`.
                e["port"] = int(_pt) if _pt else 22
            except ValueError:
                e["port"] = 22
            if self.fields[(t, "key")].text().strip():
                put("key", "key")
            elif self.fields[(t, "password")].text():
                e["password"] = self.fields[(t, "password")].text()
            put("path", "path")
        elif t == "smb":
            if not self.fields[(t, "host")].text().strip():
                return None, "SMB needs a host"
            put("host", "host")
            put("user", "user")
            if self.fields[(t, "password")].text():
                e["password"] = self.fields[(t, "password")].text()
            put("domain", "domain")
            put("share", "share")
            _pt = self.fields[(t, "port")].text().strip()
            try:
                # Default ONLY when empty — a typed value (even 0) passes through.
                e["port"] = int(_pt) if _pt else 445
            except ValueError:
                e["port"] = 445
        return name, e


class TextPromptDialog(QDialog):
    """Small single-line prompt (exclude pattern)."""

    def __init__(self, parent, title):
        super().__init__(parent)
        self.setModal(True)
        self.setFixedWidth(360)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 22)
        t = QLabel(title)
        t.setObjectName("sheettitle")
        lay.addWidget(t)
        lay.addSpacing(12)
        self.input = QLineEdit()
        self.input.setObjectName("fieldinput")
        self.input.returnPressed.connect(self.accept)
        lay.addWidget(self.input)
        lay.addSpacing(16)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = textbtn("Cancel", oid="btn")
        cancel.clicked.connect(self.reject)
        ok = textbtn("Add", oid="btnprimary")
        ok.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)
        QTimer.singleShot(40, self.input.setFocus)


class RemoteBrowseDialog(QDialog):
    """Popup to browse an SSH server or cloud bucket and pick a path."""

    def __init__(self, gui, kind, start_conn=None):
        super().__init__(gui)
        self.gui = gui
        self.kind = kind                       # 'ssh' or 'cloud'
        self.result_path = None
        self.conn = None
        self.cwd = "/"
        self.selected = None
        self.setModal(True)
        self.setWindowTitle("Browse " + ("SSH" if kind == "ssh" else "cloud"))
        self.resize(580, 580)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(0)
        title = QLabel("Browse " + ("SSH server" if kind == "ssh" else "cloud storage"))
        title.setObjectName("sheettitle")
        lay.addWidget(title)
        lay.addSpacing(12)

        self.sel = QComboBox()
        self.sel.setObjectName("fieldinput")
        names = gui._conns_of_kind(kind)
        for n in names:
            self.sel.addItem(f"{n} · {gui.conns[n].get('type')}", n)
        lay.addWidget(self.sel)
        lay.addSpacing(10)

        self.crumb = QFrame()
        self.crumb.setObjectName("crumb")
        self.crumb_lay = QHBoxLayout(self.crumb)
        self.crumb_lay.setContentsMargins(12, 9, 12, 9)
        self.crumb_lay.setSpacing(7)
        lay.addWidget(self.crumb)
        lay.addSpacing(10)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        self.files_lay = QVBoxLayout(inner)
        self.files_lay.setContentsMargins(0, 0, 0, 0)
        self.files_lay.setSpacing(6)
        self.files_lay.addStretch(1)
        self.scroll.setWidget(inner)
        lay.addWidget(self.scroll, 1)
        lay.addSpacing(10)

        self.pathlbl = QLabel("")
        self.pathlbl.setObjectName("connmeta")
        self.pathlbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        lay.addWidget(self.pathlbl)
        lay.addSpacing(10)

        row = QHBoxLayout()
        row.addStretch(1)
        cancel = textbtn("Cancel", oid="btn")
        cancel.clicked.connect(self.reject)
        self.okbtn = textbtn("Select this path", "check", oid="btnprimary")
        self.okbtn.clicked.connect(self._accept)
        row.addWidget(cancel)
        row.addWidget(self.okbtn)
        lay.addLayout(row)

        if start_conn and start_conn in names:
            self.sel.setCurrentIndex(names.index(start_conn))
        self.sel.currentIndexChanged.connect(self._on_sel)
        self._set_conn()
        self._render()

    def _set_conn(self):
        name = self.sel.currentData()
        self.conn = {"name": name, **self.gui.conns[name]}
        self.cwd = (self.conn.get("path") or "/") if self.conn["type"] == "ssh" else "/"
        self.selected = None

    def _on_sel(self, *_):
        self._set_conn()
        self._render()

    def _render(self):
        is_ssh = self.conn["type"] == "ssh"
        entries, err = (self.gui._ssh_entries(self.conn, self.cwd) if is_ssh
                        else self.gui._cloud_entries(self.conn, self.cwd))
        # breadcrumb
        while self.crumb_lay.count():
            it = self.crumb_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        cico = QLabel(ic("server-2" if is_ssh else "bucket"))
        cico.setObjectName("crumbico")
        self.crumb_lay.addWidget(cico)
        root = self.conn.get("host", self.conn["name"]) if is_ssh \
            else self.conn.get("container", "bucket")
        crumbs = [(root, "/")]
        acc = ""
        for p in ([] if self.cwd == "/" else [x for x in self.cwd.split("/") if x]):
            acc += "/" + p
            crumbs.append((p, acc))
        for i, (label, target) in enumerate(crumbs):
            if i:
                sep = QLabel(ic("chevron-right"))
                sep.setObjectName("crumbsep")
                self.crumb_lay.addWidget(sep)
            pb = QPushButton(label)
            pb.setObjectName("crumbc")
            pb.setCursor(Qt.PointingHandCursor)
            pb.clicked.connect(lambda _=False, t=target: self._goto(t))
            self.crumb_lay.addWidget(pb)
        self.crumb_lay.addStretch(1)
        # files
        while self.files_lay.count() > 1:
            it = self.files_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        if self.cwd != "/":
            self._add_item(self._mk_item("..", "up", up=True))
        if err:
            self._add_msg(ic("alert-triangle") + "  " + err)
        elif not entries:
            self._add_msg("— empty —")
        else:
            for name, meta in entries:
                self._add_item(self._mk_item(name, meta))
        self.pathlbl.setText(self.gui._compose_path(self.conn, self.cwd, self.selected))

    def _add_item(self, w):
        self.files_lay.insertWidget(self.files_lay.count() - 1, w)

    def _add_msg(self, text):
        l = QLabel(text)
        l.setObjectName("sub")
        l.setWordWrap(True)
        self._add_item(l)

    def _mk_item(self, name, meta, up=False):
        is_dir = up or meta in ("dir", "drive")
        item = ClickFrame()
        item.setObjectName("fitem")
        item.setProperty("sel", (not is_dir and name == self.selected))
        il = QHBoxLayout(item)
        il.setContentsMargins(10, 11, 10, 11)
        il.setSpacing(11)
        iconname = "corner-left-up" if up else "folder" if is_dir else \
            ("file-text" if name.endswith(".json") else "file-zip")
        fi = QLabel(ic(iconname))
        fi.setObjectName("fidir" if is_dir else "fi")
        fn = QLabel(name + ("/" if is_dir and not up else ""))
        fn.setObjectName("fn")
        fs = QLabel("" if up else ("folder" if is_dir else meta))
        fs.setObjectName("fs")
        il.addWidget(fi)
        il.addWidget(fn, 1)
        il.addWidget(fs)
        if up:
            item.clicked.connect(self._up)
        elif is_dir:
            item.clicked.connect(lambda n=name: self._enter(n))
        else:
            item.clicked.connect(lambda n=name: self._pick(n))
            item.doubleClicked.connect(lambda n=name: self._accept_file(n))
        return item

    def _goto(self, path):
        self.cwd = path
        self.selected = None
        self._render()

    def _enter(self, name):
        self.cwd = ("" if self.cwd == "/" else self.cwd) + "/" + name
        self.selected = None
        self._render()

    def _up(self):
        self.cwd = "/".join(self.cwd.split("/")[:-1]) or "/"
        self.selected = None
        self._render()

    def _pick(self, name):
        self.selected = name
        self._render()

    def _accept_file(self, name):
        self.selected = name
        self._accept()

    def _accept(self):
        self.result_path = self.gui._compose_path(self.conn, self.cwd, self.selected)
        self.accept()


class _UpdateCheckWorker(QThread):
    """Queries GitHub releases off the UI thread via the engine's fetcher."""
    done = Signal(object)

    def run(self):
        try:
            releases = fc._fetch_releases()
            if releases is None:
                self.done.emit({"err": "network"})
                return
            cur = fc._parse_version(fc.__version__)
            newer = [r for r in releases
                     if fc._parse_version(r.get("tag_name", "")) and
                     fc._parse_version(r["tag_name"]) > cur]
            newer.sort(key=lambda r: fc._parse_version(r["tag_name"]), reverse=True)
            if newer:
                self.done.emit({"latest": newer[0]["tag_name"],
                                "current": fc.__version__, "releases": newer})
            else:
                self.done.emit({"uptodate": fc.__version__})
        except Exception as e:
            msg = str(e).strip().splitlines()[0] if str(e).strip() else e.__class__.__name__
            self.done.emit({"err": msg})


class _DownloadWorker(QThread):
    """Downloads a release asset off the UI thread. Emits done(ok, path_or_err).

    When expected_size is given, a size mismatch fails the download — the same
    integrity guard the CLI self-update applies before replacing a binary."""
    done = Signal(bool, str)

    def __init__(self, url, dest, expected_size=None):
        super().__init__()
        self.url, self.dest = url, dest
        self.expected_size = expected_size

    def run(self):
        import urllib.request
        try:
            ctx = fc._get_ssl_context() if (FC_OK and hasattr(fc, "_get_ssl_context")) else None
            # Accept: octet-stream => the API asset endpoint returns the binary
            # (redirecting to the CDN) instead of the asset JSON metadata.
            headers = {"User-Agent": "fast-copy-gui",
                       "Accept": "application/octet-stream"}
            tok = fc._update_token() if (FC_OK and hasattr(fc, "_update_token")) else ""
            if tok:                       # private-repo release assets need auth
                headers["Authorization"] = f"Bearer {tok}"
            req = urllib.request.Request(self.url, headers=headers)
            written = 0
            with urllib.request.urlopen(req, timeout=60, context=ctx) as r, \
                    open(self.dest, "wb") as f:
                while True:
                    chunk = r.read(262144)
                    if not chunk:
                        break
                    f.write(chunk)
                    written += len(chunk)
            if self.expected_size and written != self.expected_size:
                raise OSError(f"size mismatch — expected {self.expected_size}, "
                              f"got {written} bytes")
            self.done.emit(True, self.dest)
        except Exception as e:
            try:
                if os.path.exists(self.dest):
                    os.remove(self.dest)
            except OSError:
                pass
            self.done.emit(False, str(e).splitlines()[0] if str(e).strip() else e.__class__.__name__)


class UpdateDialog(QDialog):
    """Startup 'new version available' popup. Shows the categorized release notes
    (the same sections the CLI's --check-update prints, via the engine's
    _classify_release_sections), a 'don't show again for this version' checkbox,
    and Close / Download only / Download & install."""
    CLOSE, DOWNLOAD_ONLY, DOWNLOAD_INSTALL = 0, 1, 2

    def __init__(self, parent, latest, current, releases, can_install, theme=None):
        super().__init__(parent)
        self._v = theme or DARK
        self.setModal(True)
        self.setWindowTitle("Update available")
        self.setMinimumWidth(520)
        self.action = self.CLOSE
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 20)
        lay.setSpacing(0)

        title = QLabel(f"fast-copy {latest} is available")
        title.setObjectName("sheettitle")
        lay.addWidget(title)
        lay.addSpacing(4)
        sub = QLabel(f"You're on v{current}. Here's what's new:")
        sub.setObjectName("fieldlbl")
        lay.addWidget(sub)
        lay.addSpacing(12)

        notes = QTextBrowser()
        notes.setOpenExternalLinks(True)
        notes.setMinimumHeight(230)
        notes.setStyleSheet(
            f"QTextBrowser{{background:{self._v['panel']};color:{self._v['txt']};"
            f"border:1px solid {self._v['line']};border-radius:8px;padding:6px;}}")
        notes.setHtml(self._build_html(releases, self._v))
        lay.addWidget(notes, 1)
        lay.addSpacing(12)

        self.skip_cb = QCheckBox(f"Don't show this again for {latest}")
        lay.addWidget(self.skip_cb)
        lay.addSpacing(14)

        row = QHBoxLayout()
        close = textbtn("Close", oid="btn")
        close.clicked.connect(self.reject)
        row.addWidget(close)
        row.addStretch(1)
        dlonly = textbtn("Download only", "arrow-down-circle", oid="btn")
        dlonly.clicked.connect(lambda: self._choose(self.DOWNLOAD_ONLY))
        row.addWidget(dlonly)
        install = textbtn("Download & install", "arrow-down-circle", oid="btnprimary")
        install.clicked.connect(lambda: self._choose(self.DOWNLOAD_INSTALL))
        if not can_install:
            install.setToolTip("In-place install needs a frozen Linux/Windows build "
                               "— use Download only here.")
        row.addWidget(install)
        lay.addLayout(row)

    def _choose(self, action):
        self.action = action
        self.accept()

    @staticmethod
    def _build_html(releases, v=None):
        import html, re
        v = v or DARK
        def md(s):                       # escape + light markdown (bold / `code`)
            s = html.escape(s)
            s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
            s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
            return s
        # Semantic theme colors so the sections read well in BOTH themes.
        labels = [
            ("security", "Security Fixes", v["danger"]),
            ("new_features", "New Features", v["ok"]),
            ("bug_fixes", "Bug Fixes", v["warn"]),
            ("performance", "Performance", v["accent"]),
            ("improvements", "Improvements", v["accenth"]),
        ]
        parts = []
        for rel in releases:
            tag = html.escape(rel.get("tag_name", ""))
            body = rel.get("body", "") or ""
            secs = fc._classify_release_sections(body) if FC_OK else {}
            parts.append(f"<h3 style='margin:8px 0 2px'>{tag}</h3>")
            shown = False
            for key, label, color in labels:
                if secs.get(key):
                    shown = True
                    parts.append(f"<p style='margin:7px 0 1px;color:{color};"
                                 f"font-weight:600'>{label}</p><ul style='margin:0 0 4px 0'>")
                    for bullet in secs[key]:
                        parts.append("<li>" + md(bullet.lstrip("-").strip()) + "</li>")
                    parts.append("</ul>")
            if not shown:
                raw = md(body.strip()[:800]) or "No release notes."
                parts.append(f"<p style='white-space:pre-wrap'>{raw}</p>")
        return "<div style='font-size:13px;line-height:1.4'>" + "".join(parts) + "</div>"


class _ConfirmHostKeyPolicy:
    """paramiko host-key policy: instead of silently auto-adding an unknown host
    key (a MITM risk), prompt the user to verify the fingerprint — OpenSSH-style
    TOFU. Known hosts are checked by paramiko before this runs, so a CHANGED key
    raises BadHostKeyException (rejected). SSH browsing runs on the main thread,
    so a modal dialog here is safe. Declining raises SSHException (connection
    rejected)."""

    def __init__(self, parent, known_hosts_path):
        self.parent = parent
        self.known_hosts_path = known_hosts_path

    def missing_host_key(self, client, hostname, key):
        import paramiko
        import hashlib
        fp = "SHA256:" + base64.b64encode(
            hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
        had_cursor = QApplication.overrideCursor() is not None
        if had_cursor:
            QApplication.restoreOverrideCursor()
        try:
            m = QMessageBox(self.parent)
            m.setWindowTitle("Unknown SSH host")
            m.setIcon(QMessageBox.Warning)
            m.setText(f"The authenticity of host '{hostname}' can't be established.")
            m.setInformativeText(
                f"{key.get_name()} key fingerprint:\n{fp}\n\n"
                "Trust this host and save it to ~/.ssh/known_hosts?")
            trust = m.addButton("Trust", QMessageBox.AcceptRole)
            m.addButton("Cancel", QMessageBox.RejectRole)
            m.exec()
            if m.clickedButton() is not trust:
                raise paramiko.SSHException(
                    f"Host key for {hostname} was not trusted by the user")
        finally:
            if had_cursor:
                QApplication.setOverrideCursor(Qt.WaitCursor)
        # Trusted: accept for this session and append a single entry to known_hosts.
        client.get_host_keys().add(hostname, key.get_name(), key)
        try:
            os.makedirs(os.path.dirname(self.known_hosts_path), exist_ok=True)
            with open(self.known_hosts_path, "a", encoding="utf-8") as f:
                f.write(f"{hostname} {key.get_name()} {key.get_base64()}\n")
        except OSError:
            pass


# ───────────────────────────────────────────────────────── main window ──
class FastCopyGUI(QWidget):
    def __init__(self):
        super().__init__()
        self.dark = True       # dark is the default theme
        self.setWindowTitle("fast-copy — " + GUI_VERSION)
        self.resize(1000, 780)

        # ── data model (mirrors the JS state) ──
        self.sources = [
            {"p": r"C:\projects\backups\data", "t": "Local"},
        ]
        self.dest_type = "Local"
        # Demo fallback — replaced by the real credentials.json when fast_copy.py
        # is available (see _init_credentials()).
        self._demo_conns = {
            "aws-dev": {"type": "s3", "access_key_id": "AKIAEXAMPLE7",
                        "secret_access_key": "wJalrXUtnFEMI", "region": "eu-central-1",
                        "container": "backups"},
            "r2-archive": {"type": "s3", "endpoint_url": "https://acct.r2.cloudflarestorage.com",
                           "access_key_id": "R2KEY123", "secret_access_key": "r2secretval"},
            "k3s-01": {"type": "ssh", "host": "k3s-01", "user": "deploy", "port": 22,
                       "key": "~/.ssh/id_ed25519", "path": "/data"},
        }
        self.conns = {}
        self.conn_state = {}
        self.exclude_patterns = [".venv", ".git*", "*.bat"]
        self.index_existing_paths = []
        self.running = False

        # ── credentials.json state ──
        self.creds_path = fc.default_credentials_path() if FC_OK else None
        self.creds_encrypted = False     # file on disk is AES-256-GCM
        self.creds_loaded = False        # real entries are in self.conns
        self.creds_pw = None             # cached passphrase (bytes) once unlocked
        self.creds_tamper = False        # file bound to a different fast_copy.py

        # browse state
        self.browse_conn = None
        self.cwd = "/"
        self.selected_file = None
        self.local_home = os.path.expanduser("~")
        self._ssh_clients = {}           # name -> [SSHClient, SFTPClient|None]
        self._ssh_no_sftp = set()        # conns whose server has no SFTP subsystem
        self._sudo_pw = None             # cached sudo password for this session
        self._build_trees()

        self._build_ui()
        self.toast = Toast(self)
        self.apply_theme()

        self._init_credentials()
        self.render_sources()
        self.render_dest()
        self.render_conns()
        self.fill_conn_sel()
        self.render_browse()
        self.render_history()

    # ───────────────────────────────────────────── credentials.json ──
    def _init_credentials(self):
        """At startup: load an unencrypted file immediately; leave an encrypted
        one locked (unlocked on first visit to Connections). No fast_copy.py or
        no file → fall back to the demo connections."""
        if not FC_OK or not self.creds_path or not os.path.isfile(self.creds_path):
            self.conns = dict(self._demo_conns)
            return
        try:
            raw = open(self.creds_path, "rb").read()
        except OSError:
            self.conns = dict(self._demo_conns)
            return
        if fc._is_encrypted(raw):
            self.creds_encrypted = True
            self.conns = {}              # locked until the user enters the passphrase
            # tamper check: the binary hash bound into the file (plaintext AAD)
            # vs this fast_copy.py — visible even while locked.
            try:
                stored, cur = fc._stored_binhash(raw), fc._self_hash()
                self.creds_tamper = bool(stored and cur and stored != cur)
            except Exception:
                self.creds_tamper = False
        else:
            self.conns = self._parse_plain_creds(raw)
            self.creds_loaded = True

    def _parse_plain_creds(self, raw):
        import json
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        conns = data.get("connections", data) if isinstance(data, dict) else {}
        return conns if isinstance(conns, dict) else {}

    def unlock_credentials(self):
        """Prompt for the passphrase and load the real entries. No-op if already
        loaded or the file isn't encrypted."""
        if not FC_OK or self.creds_loaded or not self.creds_encrypted:
            return
        dlg = PasswordDialog(
            self, "Unlock credentials.json", "", "Enter passphrase")
        if not dlg.exec():
            return
        pw = dlg.input.text().encode("utf-8")
        try:
            raw = open(self.creds_path, "rb").read()
            conns, _ = fc.decrypt_conns(raw, pw)
        except SystemExit as e:
            self.show_toast(str(e).replace("Error: ", "").split(",")[0])
            return
        except Exception as e:
            self.show_toast("Could not read credentials: " + str(e).splitlines()[0])
            return
        self.conns = conns if isinstance(conns, dict) else {}
        self.creds_pw = pw
        self.creds_loaded = True
        self.render_conns()
        self.fill_conn_sel()
        self.show_toast(f"Unlocked — {len(self.conns)} connection(s)")

    def reload_credentials(self):
        """Forget the cached passphrase and re-read the file from disk."""
        self.creds_loaded = False
        self.creds_pw = None
        self.conns = {}
        if FC_OK:
            fc._creds_cache.clear()
        self._init_credentials()
        if self.creds_encrypted and not self.creds_loaded:
            self.unlock_credentials()
        self.render_conns()
        self.fill_conn_sel()

    # ── trees for the browser ──
    def _build_trees(self):
        self.cloud_tree = {
            "/": [["database-dumps", "dir"], ["2026", "dir"], ["manifest.json", "12 KB"]],
            "/2026": [["q1", "dir"], ["q2", "dir"]],
            "/2026/q1": [["alpha-vbr-01.tar.gz", "4.2 GB"], ["artesca.img", "61 GB"]],
            "/database-dumps": [["piraeus-01.dump", "9.1 GB"], ["alpha-01.dump", "7.4 GB"]],
        }
        self.ssh_tree = {
            "/": [["data", "dir"], ["var", "dir"], ["home", "dir"]],
            "/data": [["backups", "dir"], ["dump-2026-01.tar.gz", "12 GB"]],
            "/data/backups": [["longhorn-replica.img", "61 GB"], ["pg-base.tar", "8 GB"]],
            "/var": [["log", "dir"], ["lib", "dir"]],
            "/var/log": [["syslog", "88 MB"], ["nginx.access", "210 MB"]],
            "/home": [["deploy", "dir"]],
            "/home/deploy": [[".ssh", "dir"], ["scripts", "dir"]],
        }
        self.local_tree = {
            "/": [["C:", "drive"], ["D:", "drive"], ["E:", "drive"]],
            "/C:": [["Users", "dir"], ["projects", "dir"]],
            "/C:/projects": [["backups", "dir"], ["fast-copy", "dir"]],
            "/C:/projects/backups": [["2026", "dir"], ["db-2026-01.dump", "4.2 GB"], ["notes.txt", "3 KB"]],
            "/D:": [["archive", "dir"], ["Projects", "dir"]],
            "/D:/Projects": [["src.tar.gz", "820 MB"], ["node_modules", "dir"], ["README.md", "8 KB"]],
            "/E:": [["vmdumps", "dir"]],
            "/E:/vmdumps": [["images", "dir"], ["vol-001.img", "61 GB"]],
        }

    # ─────────────────────────────────────────────────────── UI build ──
    def _build_ui(self):
        self.setObjectName("root")
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_sidebar())

        # content area in a scroll
        self.stack = QStackedWidget()
        self.screens = {}
        for key, builder in [
            ("transfer", self._screen_transfer), ("connections", self._screen_connections),
            ("cloud", self._screen_cloud), ("history", self._screen_history),
            ("settings", self._screen_settings),
        ]:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            inner = QWidget()
            wrap = QVBoxLayout(inner)
            wrap.setContentsMargins(24, 22, 24, 22)
            builder(wrap)
            wrap.addStretch(1)
            scroll.setWidget(inner)
            self.stack.addWidget(scroll)
            self.screens[key] = self.stack.count() - 1
        root.addWidget(self.stack, 1)

    def _build_sidebar(self):
        side = QWidget()
        side.setObjectName("side")
        side.setFixedWidth(196)
        lay = QVBoxLayout(side)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # brand
        brand = QHBoxLayout()
        brand.setContentsMargins(14, 16, 10, 16)
        brand.setSpacing(8)
        logo_png = os.path.join(HERE, "assets", "fast-copy-logo.png")
        if os.path.exists(logo_png):
            # exact wordmark supplied by the user — show it as a single lockup
            from PySide6.QtGui import QPixmap
            logo = QLabel()
            logo.setObjectName("brandWordmark")
            pm = QPixmap(logo_png)
            logo.setPixmap(pm.scaledToHeight(26, Qt.SmoothTransformation))
            ver = QLabel(GUI_VERSION)
            ver.setObjectName("brandVer")
            brand.addWidget(logo)
            brand.addStretch(1)
            brand.addWidget(ver)
        else:
            logo = QLabel(ic("bolt"))
            logo.setObjectName("brandLogo")
            logo.setFont(QFont(ICON_FAMILY, 18))
            nm = QLabel("FAST-COPY")
            nm.setObjectName("brandName")
            brand.addWidget(logo)
            brand.addWidget(nm)
            brand.addStretch(1)
        lay.addLayout(brand)

        # nav
        nav = QVBoxLayout()
        nav.setContentsMargins(10, 6, 10, 6)
        nav.setSpacing(2)
        self.nav_items = {}
        items = [
            ("transfer", "arrows-exchange", "Transfer"),
            ("connections", "key", "Connections"),
            ("cloud", "folders", "Browse files"),
            ("history", "history", "History"),
            ("settings", "settings", "Settings"),
        ]
        for key, icon, label in items:
            b = QPushButton("   " + ic(icon) + "    " + label)
            b.setObjectName("navitem")
            b.setCursor(Qt.PointingHandCursor)
            b.setProperty("active", key == "transfer")
            b.clicked.connect(lambda _=False, k=key: self.navigate(k))
            self.nav_items[key] = b
            nav.addWidget(b)
        lay.addLayout(nav)
        lay.addStretch(1)

        self.doc_btn = QPushButton("  " + ic("book") + "   Documentation")
        self.doc_btn.setObjectName("themebtn")
        self.doc_btn.setCursor(Qt.PointingHandCursor)
        self.doc_btn.clicked.connect(self.open_docs)
        wrap1 = QHBoxLayout()
        wrap1.setContentsMargins(10, 0, 10, 10)
        wrap1.addWidget(self.doc_btn)
        lay.addLayout(wrap1)

        self.theme_btn = QPushButton(
            "  " + (ic("sun") + "   Light mode" if self.dark
                    else ic("moon") + "   Dark mode"))
        self.theme_btn.setObjectName("themebtn")
        self.theme_btn.setCursor(Qt.PointingHandCursor)
        self.theme_btn.clicked.connect(self.toggle_theme)
        wrap2 = QHBoxLayout()
        wrap2.setContentsMargins(10, 0, 10, 10)
        wrap2.addWidget(self.theme_btn)
        lay.addLayout(wrap2)

        foot = QLabel("block-order copy · dedup\nssh · s3 / az / gs")
        foot.setObjectName("sfoot")
        fwrap = QHBoxLayout()
        fwrap.setContentsMargins(16, 12, 16, 14)
        fwrap.addWidget(foot)
        lay.addLayout(fwrap)
        return side

    # ── small header helper ──
    def _header(self, lay, title, sub):
        h = QLabel(title)
        h.setObjectName("h2")
        lay.addWidget(h)
        s = QLabel(sub)
        s.setObjectName("sub")
        s.setWordWrap(True)
        lay.addWidget(s)
        lay.addSpacing(14)

    # ───────────────────────────────────────────── TRANSFER screen ──
    def _screen_transfer(self, lay):
        self._header(lay, "New transfer",
                     "Πολλά sources → ένα destination. Με πάνω από ένα source, μόνο local.")

        # sources label + multi pill
        lblrow = QHBoxLayout()
        srclbl = QLabel("SOURCES")
        srclbl.setObjectName("lbl")
        self.multi_pill = QLabel(ic("folder") + " local only when multiple")
        self.multi_pill.setObjectName("pill")
        self.multi_pill.hide()
        lblrow.addWidget(srclbl)
        lblrow.addSpacing(8)
        srchint = QLabel("example — edit before running")
        srchint.setObjectName("sub")
        lblrow.addWidget(srchint)
        lblrow.addSpacing(8)
        lblrow.addWidget(self.multi_pill)
        lblrow.addStretch(1)
        lay.addLayout(lblrow)
        lay.addSpacing(8)

        self.sources_box = QVBoxLayout()
        self.sources_box.setSpacing(8)
        lay.addLayout(self.sources_box)

        add = textbtn(ic("plus") + " Add source", oid="add")
        add.setCursor(Qt.PointingHandCursor)
        add.clicked.connect(self.add_source)
        lay.addWidget(add)
        lay.addSpacing(4)

        self.warn_wrap = QVBoxLayout()
        lay.addLayout(self.warn_wrap)

        arrow = QLabel(ic("arrow-down"))
        arrow.setObjectName("arrow")
        arrow.setAlignment(Qt.AlignCenter)
        lay.addWidget(arrow)
        lay.addSpacing(6)

        # destination
        drow = QHBoxLayout()
        dl = QLabel("DESTINATION")
        dl.setObjectName("lbl")
        dh = QLabel("single · local / SSH / cloud")
        dh.setObjectName("sub")
        drow.addWidget(dl)
        drow.addSpacing(8)
        drow.addWidget(dh)
        drow.addSpacing(8)
        dhint = QLabel("example — edit before running")
        dhint.setObjectName("sub")
        drow.addWidget(dhint)
        drow.addStretch(1)
        lay.addLayout(drow)
        lay.addSpacing(8)

        self.dst_row = QFrame()
        self.dst_row.setObjectName("row")
        self.dst_row.setFixedHeight(42)
        dlay = QHBoxLayout(self.dst_row)
        dlay.setContentsMargins(12, 0, 9, 0)
        dlay.setSpacing(9)
        self.dst_icon = ClickLabel(ic("folder"))
        self.dst_icon.setObjectName("rowicon")
        self.dst_icon.setCursor(Qt.PointingHandCursor)
        self.dst_icon.setToolTip("Browse…")
        self.dst_icon.clicked.connect(self.browse_dest_path)
        self.dst_input = QLineEdit("F:\\consolidated\\")
        self.dst_input.setObjectName("path")
        self.dst_input.editingFinished.connect(self._normalize_dest)
        dlay.addWidget(self.dst_icon)
        dlay.addWidget(self.dst_input, 1)
        self.dst_tags = {}
        for k in ("Local", "SSH", "Cloud"):
            t = QPushButton(k)
            t.setObjectName("tag")
            t.setCursor(Qt.PointingHandCursor)
            t.setProperty("state", "on" if k == "Local" else "off")
            t.clicked.connect(lambda _=False, key=k: self.set_dest_type(key))
            self.dst_tags[k] = t
            dlay.addWidget(t)
        lay.addWidget(self.dst_row)
        lay.addSpacing(8)

        # options row
        opts = QHBoxLayout()
        opts.setSpacing(8)
        self.transfer_opts = {}
        for name, active in [("dedup", True), ("verify", True)]:
            o = self._opt(name, active)
            self.transfer_opts[name] = o
            opts.addWidget(o)
        hlbl = QLabel("hash")
        hlbl.setObjectName("optlbl")
        opts.addWidget(hlbl)
        self.hash_sel = QComboBox()
        self.hash_sel.setObjectName("mini")
        self.hash_sel.addItems(["auto", "xxh128", "sha256"])
        self.hash_sel.setCurrentText("xxh128")
        opts.addWidget(self.hash_sel)
        tlbl = QLabel("threads")
        tlbl.setObjectName("optlbl")
        opts.addWidget(tlbl)
        self.thread_sel = QComboBox()
        self.thread_sel.setObjectName("mini")
        self.thread_sel.addItems(["2", "4", "8", "16"])
        self.thread_sel.setCurrentText("4")
        opts.addWidget(self.thread_sel)
        opts.addStretch(1)
        self.adv_toggle = self._opt(ic("adjustments") + " Advanced", False)
        self.adv_toggle.clicked.disconnect()
        self.adv_toggle.clicked.connect(self.toggle_advanced)
        opts.addWidget(self.adv_toggle)
        lay.addSpacing(7)
        lay.addLayout(opts)
        lay.addSpacing(11)

        # advanced panel
        self.adv = self._build_advanced()
        self.adv.hide()
        lay.addWidget(self.adv)
        lay.addSpacing(7)

        # actions
        actions = QHBoxLayout()
        actions.setSpacing(9)
        self.start_btn = textbtn("Start transfer", "player-play", oid="btnprimary")
        self.start_btn.clicked.connect(lambda: self.run_transfer(False))
        self.preview_btn = textbtn("Preview plan", "eye", oid="btn")
        self.preview_btn.clicked.connect(lambda: self.run_transfer(True))
        actions.addWidget(self.start_btn, 1)
        actions.addWidget(self.preview_btn)
        lay.addLayout(actions)
        lay.addSpacing(11)

        # progress panel
        lay.addWidget(self._build_progress())

    def _opt(self, text, active):
        o = QPushButton(text)
        o.setObjectName("opt")
        o.setCursor(Qt.PointingHandCursor)
        o.setProperty("active", active)
        o.clicked.connect(lambda: self._toggle_opt(o))
        return o

    def _toggle_opt(self, o):
        o.setProperty("active", not o.property("active"))
        repolish(o)

    def _build_advanced(self):
        adv = QFrame()
        adv.setObjectName("adv")
        lay = QVBoxLayout(adv)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(0)

        lay.addWidget(self._gl("COPY"))
        lay.addSpacing(9)
        grid = QGridLayout()
        grid.setHorizontalSpacing(15)
        grid.setVerticalSpacing(5)
        grid.addWidget(self._advlbl("Buffer (MB)"), 0, 0)
        grid.addWidget(self._advlbl("Cloud concurrency"), 0, 1)
        grid.addWidget(self._advlbl("SSH chunk (MB)"), 0, 2)
        self.buf_input = QLineEdit("64")
        self.buf_input.setObjectName("advinput")
        self.conc_input = QLineEdit("16")
        self.conc_input.setObjectName("advinput")
        self.chunk_input = QLineEdit("100")
        self.chunk_input.setObjectName("advinput")
        self.chunk_input.setToolTip("Tar batch size for SSH (--ssh-no-sftp) transfers. "
                                    "Not the same as Buffer.")
        grid.addWidget(self.buf_input, 1, 0)
        grid.addWidget(self.conc_input, 1, 1)
        grid.addWidget(self.chunk_input, 1, 2)
        lay.addLayout(grid)
        lay.addSpacing(16)

        lay.addWidget(self._gl("PRESERVE METADATA"))
        lay.addSpacing(9)
        meta = QHBoxLayout()
        meta.setSpacing(8)
        self.meta_opts = {}
        for name, active in [("mode", True), ("times", True), ("owner", False),
                             ("xattr", False), ("acl", False)]:
            o = self._opt(name, active)
            self.meta_opts[name] = o
            meta.addWidget(o)
        meta.addStretch(1)
        lay.addLayout(meta)
        lay.addSpacing(16)

        lay.addWidget(self._gl("FLAGS"))
        lay.addSpacing(9)
        flags = QHBoxLayout()
        flags.setSpacing(8)
        self.flag_opts = {}
        for name in ["overwrite all", "force", "no hash cache", "--use-sudo",
                     "dedup existing", "include node_modules"]:
            o = self._opt(name, False)
            self.flag_opts[name] = o
            flags.addWidget(o)
        flags.addStretch(1)
        lay.addLayout(flags)
        lay.addSpacing(15)

        lay.addWidget(self._gl("EXCLUDE PATTERNS"))
        lay.addSpacing(9)
        self.chips_frame = QFrame()
        self.chips_frame.setObjectName("chips")
        self.chips_lay = QHBoxLayout(self.chips_frame)
        self.chips_lay.setContentsMargins(11, 9, 11, 9)
        self.chips_lay.setSpacing(7)
        lay.addWidget(self.chips_frame)
        self.render_chips()
        lay.addSpacing(15)

        lay.addWidget(self._gl("INDEX EXISTING (reflink dedup vs pre-existing files)"))
        lay.addSpacing(9)
        self.idx_chips_frame = QFrame()
        self.idx_chips_frame.setObjectName("chips")
        self.idx_chips_lay = QHBoxLayout(self.idx_chips_frame)
        self.idx_chips_lay.setContentsMargins(11, 9, 11, 9)
        self.idx_chips_lay.setSpacing(7)
        lay.addWidget(self.idx_chips_frame)
        self.render_idx_chips()
        return adv

    def _gl(self, text):
        l = QLabel(text)
        l.setObjectName("gl")
        return l

    def _advlbl(self, text):
        l = QLabel(text)
        l.setObjectName("advlbl")
        return l

    def render_chips(self):
        while self.chips_lay.count():
            item = self.chips_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for pat in self.exclude_patterns:
            chip = QFrame()
            chip.setObjectName("chip")
            cl = QHBoxLayout(chip)
            cl.setContentsMargins(9, 4, 7, 4)
            cl.setSpacing(5)
            txt = QLabel(pat)
            txt.setObjectName("chiptext")
            x = QPushButton(ic("x"))
            x.setObjectName("chipx")
            x.setCursor(Qt.PointingHandCursor)
            x.clicked.connect(lambda _=False, p=pat: self.remove_chip(p))
            cl.addWidget(txt)
            cl.addWidget(x)
            self.chips_lay.addWidget(chip)
        add = QPushButton("+ add…")
        add.setObjectName("chipadd")
        add.setCursor(Qt.PointingHandCursor)
        add.clicked.connect(self.add_chip)
        self.chips_lay.addWidget(add)
        self.chips_lay.addStretch(1)

    def remove_chip(self, pat):
        if pat in self.exclude_patterns:
            self.exclude_patterns.remove(pat)
            self.render_chips()

    def add_chip(self):
        dlg = TextPromptDialog(self, "Exclude pattern")
        if dlg.exec() and dlg.input.text().strip():
            self.exclude_patterns.append(dlg.input.text().strip())
            self.render_chips()

    def render_idx_chips(self):
        while self.idx_chips_lay.count():
            item = self.idx_chips_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for path in self.index_existing_paths:
            chip = QFrame()
            chip.setObjectName("chip")
            cl = QHBoxLayout(chip)
            cl.setContentsMargins(9, 4, 7, 4)
            cl.setSpacing(5)
            txt = QLabel(path)
            txt.setObjectName("chiptext")
            txt.setToolTip(path)
            x = QPushButton(ic("x"))
            x.setObjectName("chipx")
            x.setCursor(Qt.PointingHandCursor)
            x.clicked.connect(lambda _=False, p=path: self.remove_idx_path(p))
            cl.addWidget(txt)
            cl.addWidget(x)
            self.idx_chips_lay.addWidget(chip)
        add = QPushButton("+ add folder…")
        add.setObjectName("chipadd")
        add.setCursor(Qt.PointingHandCursor)
        add.clicked.connect(self.add_idx_path)
        self.idx_chips_lay.addWidget(add)
        self.idx_chips_lay.addStretch(1)

    def remove_idx_path(self, path):
        if path in self.index_existing_paths:
            self.index_existing_paths.remove(path)
            self.render_idx_chips()

    def add_idx_path(self):
        # Must be a real directory on the destination filesystem; the engine
        # warns and skips any path not under the destination mount.
        d = QFileDialog.getExistingDirectory(self, "Index existing folder")
        if d and d not in self.index_existing_paths:
            self.index_existing_paths.append(d)
            self.render_idx_chips()

    def _build_progress(self):
        prog = QFrame()
        prog.setObjectName("prog")
        lay = QVBoxLayout(prog)
        lay.setContentsMargins(17, 17, 17, 17)
        lay.setSpacing(0)

        top = QHBoxLayout()
        self.pstate = QLabel("Ready")
        self.pstate.setObjectName("pstate")
        self.pfiles = QLabel("0 / 0 files")
        self.pfiles.setObjectName("pfiles")
        top.addWidget(self.pstate)
        top.addStretch(1)
        top.addWidget(self.pfiles)
        lay.addLayout(top)
        lay.addSpacing(11)

        self.bar = QFrame()
        self.bar.setObjectName("bar")
        self.bar.setFixedHeight(9)
        barlay = QHBoxLayout(self.bar)
        barlay.setContentsMargins(0, 0, 0, 0)
        self.barf = QFrame()
        self.barf.setObjectName("barf")
        self.barf.setFixedWidth(0)
        barlay.addWidget(self.barf)
        barlay.addStretch(1)
        # Indeterminate "breathing" pulse for phases with no known total (the
        # existing-files indexing walk doesn't know its file count up front, so
        # a percentage bar would sit stuck at 0% — pulse instead of pretending).
        self._pulse = QTimer(self)
        self._pulse.setInterval(33)
        self._pulse.timeout.connect(self._pulse_tick)
        self._pulse_x = 0.0
        lay.addWidget(self.bar)
        lay.addSpacing(15)

        stats = QGridLayout()
        stats.setHorizontalSpacing(11)
        self.stat_vals = {}
        cols = [("Progress", "0%", False), ("Speed", "—", True),
                ("ETA", "—", False), ("Dedup saved", "—", False)]
        for i, (k, v, okc) in enumerate(cols):
            kl = QLabel(k.upper())
            kl.setObjectName("statk")
            vl = QLabel(v)
            vl.setObjectName("statvok" if okc else "statv")
            stats.addWidget(kl, 0, i)
            stats.addWidget(vl, 1, i)
            self.stat_vals[k] = vl
        lay.addLayout(stats)
        lay.addSpacing(13)

        self.log_box = QFrame()
        self.log_box.setObjectName("logbox")
        # Taller log so more copy lines + phase banners are visible; grows with
        # the window.
        self.log_box.setMinimumHeight(500)
        self.log_box.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        logwrap = QVBoxLayout(self.log_box)
        logwrap.setContentsMargins(0, 0, 0, 0)
        self.log_scroll = QScrollArea()
        self.log_scroll.setWidgetResizable(True)
        self.log_scroll.setFrameShape(QFrame.NoFrame)
        log_inner = QWidget()
        self.log_lay = QVBoxLayout(log_inner)
        self.log_lay.setContentsMargins(11, 9, 11, 9)
        self.log_lay.setSpacing(2)
        self.log_lay.addStretch(1)
        self.log_scroll.setWidget(log_inner)
        logwrap.addWidget(self.log_scroll)
        lay.addWidget(self.log_box)
        return prog

    # ───────────────────────────────────────── CONNECTIONS screen ──
    def _screen_connections(self, lay):
        self._header(lay, "Connections",
                     "Read from / saved to credentials.json. Double-click to edit.")
        addrow = QHBoxLayout()
        addrow.addStretch(1)
        self.reload_btn = textbtn("Reload", "refresh", oid="btnsm")
        self.reload_btn.clicked.connect(self.reload_credentials)
        self.rekey_btn = textbtn("Rekey", "key", oid="btnsm")
        self.rekey_btn.clicked.connect(self.rekey_credentials)
        addc = textbtn("Add connection", "plus", oid="btnsmprimary")
        addc.clicked.connect(lambda: self.open_conn_modal(None))
        addrow.addWidget(self.reload_btn)
        addrow.addWidget(self.rekey_btn)
        addrow.addWidget(addc)
        lay.addLayout(addrow)
        lay.addSpacing(8)

        # banner: locked / loaded / demo status
        self.creds_banner = QVBoxLayout()
        lay.addLayout(self.creds_banner)

        cl = QLabel("CLOUD")
        cl.setObjectName("sectl")
        lay.addWidget(cl)
        lay.addSpacing(8)
        self.cloud_conns = QVBoxLayout()
        self.cloud_conns.setSpacing(9)
        lay.addLayout(self.cloud_conns)
        lay.addSpacing(8)

        sl = QLabel("SSH")
        sl.setObjectName("sectl")
        lay.addWidget(sl)
        lay.addSpacing(8)
        self.ssh_conns = QVBoxLayout()
        self.ssh_conns.setSpacing(9)
        lay.addLayout(self.ssh_conns)

    # ───────────────────────────────────────────── CLOUD/browse screen ──
    def _screen_cloud(self, lay):
        self._header(lay, "Browse files",
                     "Φάκελος = πλοήγηση · αρχείο = κλικ για επιλογή · double-click = set as source.")
        self.conn_sel = QComboBox()
        self.conn_sel.setObjectName("mini")
        self.conn_sel.setMinimumHeight(34)
        self.conn_sel.currentIndexChanged.connect(self.on_conn_sel)
        lay.addWidget(self.conn_sel)
        lay.addSpacing(12)

        self.crumb = QFrame()
        self.crumb.setObjectName("crumb")
        self.crumb_lay = QHBoxLayout(self.crumb)
        self.crumb_lay.setContentsMargins(12, 9, 12, 9)
        self.crumb_lay.setSpacing(7)
        lay.addWidget(self.crumb)
        lay.addSpacing(12)

        self.files_box = QVBoxLayout()
        self.files_box.setSpacing(6)
        lay.addLayout(self.files_box)
        lay.addSpacing(14)

        bts = QHBoxLayout()
        bts.setSpacing(9)
        setsrc = textbtn("Set as source", "arrow-down-circle", oid="btn")
        setsrc.clicked.connect(self.browse_set_source)
        setdst = textbtn("Set as destination", "arrow-up-circle", oid="btn")
        setdst.clicked.connect(self.browse_set_dest)
        bts.addWidget(setsrc, 1)
        bts.addWidget(setdst, 1)
        lay.addLayout(bts)

    # ───────────────────────────────────────────── HISTORY screen ──
    def _screen_history(self, lay):
        self._header(lay, "History", "Προηγούμενες μεταφορές από τα saved JSON logs.")
        self.history_box = QVBoxLayout()
        self.history_box.setSpacing(9)
        lay.addLayout(self.history_box)

    # ───────────────────────────────────────────── SETTINGS screen ──
    def _screen_settings(self, lay):
        self._header(lay, "Settings", "Dependencies, έκδοση, ενημερώσεις.")
        dl = QLabel("DEPENDENCIES")
        dl.setObjectName("sectl")
        lay.addWidget(dl)
        lay.addSpacing(8)
        # (icon, label, meta, [import names], [pip specs]) — checked live
        deps = [
            ("brand-python", "paramiko", "SSH transfers",
             ["paramiko"], ["paramiko"]),
            ("bolt", "xxhash", "~10× faster hashing",
             ["xxhash"], ["xxhash"]),
            ("cloud", "boto3 / azure / gcs", "object storage backends",
             ["boto3", "azure.storage.blob", "google.cloud.storage"],
             ["boto3", "azure-storage-blob", "google-cloud-storage"]),
        ]
        self._dep_box = QVBoxLayout()
        self._dep_box.setSpacing(9)
        lay.addLayout(self._dep_box)
        self._dep_specs = deps
        self._render_deps()

        vl = QLabel("VERSION")
        vl.setObjectName("sectl")
        lay.addWidget(vl)
        lay.addSpacing(8)
        card = QFrame()
        card.setObjectName("conn")
        cl = QHBoxLayout(card)
        cl.setContentsMargins(14, 12, 14, 12)
        cl.setSpacing(13)
        cic = QLabel(ic("arrows-exchange"))
        cic.setObjectName("connicon")
        col = QVBoxLayout()
        col.setSpacing(2)
        nm = QLabel("fast-copy " + GUI_VERSION)
        nm.setObjectName("connname")
        mt = QLabel(GUI_REPO)
        mt.setObjectName("connmeta")
        col.addWidget(nm)
        col.addWidget(mt)
        cl.addWidget(cic)
        cl.addLayout(col, 1)
        self.upd_btn = textbtn("Check for updates", oid="btnsm")
        self.upd_btn.clicked.connect(self._update_btn_clicked)
        cl.addWidget(self.upd_btn)
        lay.addWidget(card)

    @staticmethod
    def _have_module(name):
        import importlib.util
        try:
            return importlib.util.find_spec(name) is not None
        except Exception:
            return False

    def _render_deps(self):
        # clear and rebuild the dependency cards (called on open + after install)
        while self._dep_box.count():
            it = self._dep_box.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        for icon, name, meta, imports, pips in self._dep_specs:
            have = [m for m in imports if self._have_module(m)]
            missing = [pips[i] for i, m in enumerate(imports) if not self._have_module(m)]
            self._dep_box.addWidget(self._dep_card(icon, name, meta, len(have),
                                                   len(imports), missing))

    def _dep_card(self, icon, name, meta, n_have, n_total, missing):
        card = QFrame()
        card.setObjectName("conn")
        outer = QVBoxLayout(card)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)
        row = QHBoxLayout()
        row.setSpacing(13)
        cic = QLabel(ic(icon))
        cic.setObjectName("connicon")
        col = QVBoxLayout()
        col.setSpacing(2)
        nm = QLabel(name)
        nm.setObjectName("connname")
        mt = QLabel(meta)
        mt.setObjectName("connmeta")
        col.addWidget(nm)
        col.addWidget(mt)
        if not missing:
            b = QLabel(ic("check") + " installed")
            b.setObjectName("badgeok")
        elif n_have == 0:
            b = QLabel("not installed")
            b.setObjectName("badgeidle")
        else:
            b = QLabel(f"partial · {n_have}/{n_total}")
            b.setObjectName("badgeidle")
        row.addWidget(cic)
        row.addLayout(col, 1)
        row.addWidget(b)
        outer.addLayout(row)
        if missing:
            cmd = "python -m pip install " + " ".join(missing)
            crow = QHBoxLayout()
            crow.setSpacing(8)
            cmd_lbl = QLineEdit(cmd)
            cmd_lbl.setReadOnly(True)
            cmd_lbl.setObjectName("path")
            cmd_lbl.setCursor(Qt.IBeamCursor)
            crow.addWidget(cmd_lbl, 1)
            btn = textbtn("Install", oid="btnsm")
            btn.clicked.connect(lambda _=False, m=list(missing): self._pip_install_deps(m))
            crow.addWidget(btn)
            outer.addLayout(crow)
        return card

    # ───────────────────────────────── core engine (fast_copy.py) ──
    def _ensure_core(self):
        """If the GUI was launched WITHOUT fast_copy.py next to it (and isn't a
        bundled build), offer to download the version that matches this GUI."""
        if FC_OK or getattr(sys, "frozen", False):
            return
        m = QMessageBox(self)
        m.setWindowTitle("Core engine missing")
        m.setIcon(QMessageBox.Warning)
        m.setText("fast_copy.py (the copy engine) was not found next to the GUI.")
        m.setInformativeText(
            f"The GUI needs it to run transfers. Download the matching "
            f"version (v{GUI_VERSION}) from {GUI_REPO}?")
        dl = m.addButton("Download v" + GUI_VERSION, QMessageBox.AcceptRole)
        m.addButton("Not now", QMessageBox.RejectRole)
        m.exec()
        if m.clickedButton() is dl:
            self._download_core()

    def _download_core(self):
        import urllib.request
        self.show_toast("Downloading fast_copy.py v" + GUI_VERSION + " …")
        QApplication.processEvents()
        base = "https://raw.githubusercontent.com/" + GUI_REPO
        urls = [base + "/v" + GUI_VERSION + "/fast_copy.py",
                base + "/" + GUI_VERSION + "/fast_copy.py",
                base + "/main/fast_copy.py"]
        dest = os.path.join(HERE, "fast_copy.py")
        err = "no source reachable"
        for u in urls:
            try:
                with urllib.request.urlopen(u, timeout=25) as r:
                    data = r.read()
                if b"__version__" in data and b"def main" in data:
                    with open(dest, "wb") as f:
                        f.write(data)
                    self._core_downloaded(dest)
                    return
                err = "unexpected file contents"
            except Exception as e:
                err = str(e)
        QMessageBox.critical(self, "Download failed",
                             "Could not download fast_copy.py:\n" + err +
                             "\n\nGet it manually from:\n"
                             "https://github.com/" + GUI_REPO + "/releases")

    def _core_downloaded(self, dest):
        m = QMessageBox(self)
        m.setWindowTitle("Core engine installed")
        m.setIcon(QMessageBox.Information)
        m.setText("Downloaded fast_copy.py to:\n" + dest)
        m.setInformativeText("Restart the GUI to load it now?")
        r = m.addButton("Restart now", QMessageBox.AcceptRole)
        m.addButton("Later", QMessageBox.RejectRole)
        m.exec()
        if m.clickedButton() is r:
            QProcess.startDetached(sys.executable, [os.path.abspath(__file__)])
            self.close()

    def _pip_install_deps(self, specs):
        """Install the missing pip packages into the running interpreter, then
        refresh the dependency panel."""
        if getattr(sys, "frozen", False):
            # In the packaged (PyInstaller) build, sys.executable is THIS app,
            # not a Python interpreter — running it with "-m pip install" just
            # relaunches the GUI in a second window, and a frozen process can't
            # import packages added at runtime anyway. Explain instead of
            # spawning another window.
            cmd = "pip install " + " ".join(specs)
            try:
                QApplication.clipboard().setText(cmd)
                copied = "\n\n(The command was copied to your clipboard.)"
            except Exception:
                copied = ""
            m = QMessageBox(self)
            m.setWindowTitle("Can't install into the packaged app")
            m.setIcon(QMessageBox.Information)
            m.setText("This standalone fast-copy build can't add Python "
                      "packages to itself at runtime.")
            m.setInformativeText(
                "To use these optional integrations, run fast-copy from the "
                "Python source and install them into that environment:\n\n  "
                + cmd + copied)
            m.exec()
            return
        if getattr(self, "_pip_proc", None) is not None:
            self.show_toast("Install already running…")
            return
        self.show_toast("Installing " + ", ".join(specs) + " …")
        proc = QProcess(self)
        self._pip_proc = proc

        def _done(*_):
            ok = (proc.exitCode() == 0)
            self.show_toast("Installed ✓" if ok else "Install failed — see terminal")
            self._pip_proc = None
            self._render_deps()
            proc.deleteLater()
        proc.finished.connect(_done)
        proc.setProgram(sys.executable)
        proc.setArguments(["-m", "pip", "install", *specs])
        proc.start()

    # ─────────────────────────────────────────────────────── nav/theme ──
    def navigate(self, key):
        for k, b in self.nav_items.items():
            b.setProperty("active", k == key)
            repolish(b)
        self.stack.setCurrentIndex(self.screens[key])
        # First visit to Connections with an encrypted file → ask for the passphrase.
        if key == "connections" and self.creds_encrypted and not self.creds_loaded:
            QTimer.singleShot(60, self.unlock_credentials)
        # Refresh history each time it's opened so new runs show up.
        if key == "history":
            self.render_history()

    def toggle_theme(self):
        self.dark = not self.dark
        self.theme_btn.setText("  " + (ic("sun") + "   Light mode" if self.dark
                                       else ic("moon") + "   Dark mode"))
        self.apply_theme()

    def apply_theme(self):
        v = DARK if self.dark else LIGHT
        self.setStyleSheet(stylesheet(v))
        # progress bar fill gradient + barf color (needs explicit set)
        self.barf.setStyleSheet(
            f"background: qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            f"stop:0 {v['accent']}, stop:1 {v['accenth']}); border-radius:5px;")

    def open_docs(self):
        import webbrowser
        webbrowser.open("https://fast-copy.dev/#docs")

    # ─────────────────────────────────────────────────────── sources ──
    def add_source(self):
        self.sources.append({"p": r"G:\new\folder" + str(len(self.sources)), "t": "Local"})
        self.render_sources()

    def render_sources(self):
        while self.sources_box.count():
            item = self.sources_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        multi = len(self.sources) > 1
        self.multi_pill.setVisible(multi)
        counts = {}
        for s in self.sources:
            b = base(s["p"])
            counts[b] = counts.get(b, 0) + 1

        for i, s in enumerate(self.sources):
            b = base(s["p"])
            dup = counts[b] > 1
            row = QFrame()
            row.setObjectName("row")
            row.setProperty("warn", dup)
            row.setFixedHeight(42)
            rl = QHBoxLayout(row)
            rl.setContentsMargins(12, 0, 9, 0)
            rl.setSpacing(9)
            icon = ClickLabel(ic(self._src_icon(s["t"])))
            icon.setObjectName("rowicon")
            icon.setCursor(Qt.PointingHandCursor)
            icon.setToolTip("Browse…")
            icon.clicked.connect(lambda idx=i: self.browse_source_path(idx))
            rl.addWidget(icon)
            path = QLineEdit(s["p"])
            path.setObjectName("path")
            path.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            path.editingFinished.connect(lambda le=path, idx=i: self.edit_source_path(idx, le.text()))
            rl.addWidget(path, 1)
            for k in ("Local", "SSH", "Cloud"):
                rl.addWidget(self._src_tag(s, multi, k, i))
            rm = QPushButton(ic("x"))
            rm.setObjectName("rm")
            rm.setCursor(Qt.PointingHandCursor)
            rm.clicked.connect(lambda _=False, idx=i: self.remove_source(idx))
            rl.addWidget(rm)
            self.sources_box.addWidget(row)

        self._render_source_warnings(counts, multi)

    def _src_tag(self, s, multi, k, idx):
        t = QPushButton(k)
        t.setObjectName("tag")
        t.setCursor(Qt.PointingHandCursor)
        if multi and k != "Local":
            t.setProperty("state", "dis")
            t.setEnabled(False)
        else:
            t.setProperty("state", "on" if s["t"] == k else "off")
            t.clicked.connect(lambda _=False, i=idx, key=k: self.set_source_type(i, key))
        return t

    def _src_icon(self, t):
        return "folder" if t == "Local" else "server-2" if t == "SSH" else "cloud"

    def set_source_type(self, idx, k):
        self.sources[idx]["t"] = k
        self.render_sources()

    def edit_source_path(self, idx, text):
        if idx < len(self.sources):
            if self.sources[idx]["t"] == "Local":
                text = self._normalize_local(text)
            if self.sources[idx]["p"] != text:
                self.sources[idx]["p"] = text
                self.render_sources()

    @staticmethod
    def _normalize_local(path):
        """Local paths → native separator (e.g. E:/test3 → E:\\test3 on Windows).
        Leaves SSH (user@host:) and cloud (scheme://) paths untouched."""
        p = (path or "").strip()
        if not p or "://" in p or re.search(r"@[^/\\]+:", p):
            return path
        return p.replace("/", os.sep) if os.sep != "/" else p

    def _normalize_dest(self):
        if self.dest_type == "Local":
            norm = self._normalize_local(self.dst_input.text())
            if norm != self.dst_input.text():
                self.dst_input.setText(norm)

    def _conns_of_kind(self, kind):
        """Names of loaded connections matching a browse kind ('ssh'/'cloud')."""
        wanted = ("ssh",) if kind == "ssh" else ("s3", "az", "gs")
        return [n for n, c in self.conns.items()
                if isinstance(c, dict) and c.get("type") in wanted]

    def _match_conn(self, kind, path):
        """Best-effort: pre-select the connection a typed remote path refers to."""
        if kind == "ssh":
            m = re.match(r"(?:[^@/]+@)?([^:/]+):", path or "")
            host = m.group(1) if m else None
            for n, c in self.conns.items():
                if isinstance(c, dict) and c.get("type") == "ssh" and c.get("host") == host:
                    return n
        else:
            m = re.match(r"\w+://([^@/]+)@", path or "")
            if m and m.group(1) in self.conns:
                return m.group(1)
        return None

    def _open_remote_browser(self, kind, path, on_pick):
        # Locked credentials → ask for the passphrase right here instead of
        # bouncing the user to Connections.
        if self.creds_encrypted and not self.creds_loaded:
            self.unlock_credentials()
            if not self.creds_loaded:
                return                       # cancelled / wrong passphrase
        if not self._conns_of_kind(kind):
            label = "SSH" if kind == "ssh" else "cloud"
            self.show_toast(f"No {label} connections — add one in Connections")
            return
        dlg = RemoteBrowseDialog(self, kind, start_conn=self._match_conn(kind, path))
        if dlg.exec() and dlg.result_path:
            on_pick(dlg.result_path)

    def browse_source_path(self, idx):
        if idx >= len(self.sources):
            return
        s = self.sources[idx]
        if s["t"] == "Local":
            start = s["p"] if os.path.isdir(s["p"]) else self.local_home
            d = QFileDialog.getExistingDirectory(self, "Choose source folder", start)
            if d:
                # Qt returns forward-slash paths even on Windows; show native form.
                self.sources[idx]["p"] = self._normalize_local(d)
                self.render_sources()
        else:
            kind = "ssh" if s["t"] == "SSH" else "cloud"

            def pick(path, i=idx):
                if i < len(self.sources):
                    self.sources[i]["p"] = path
                    self.render_sources()
            self._open_remote_browser(kind, s["p"], pick)

    def browse_dest_path(self):
        if self.dest_type == "Local":
            cur = self.dst_input.text()
            start = cur if os.path.isdir(cur) else self.local_home
            d = QFileDialog.getExistingDirectory(self, "Choose destination folder", start)
            if d:
                # Qt returns forward-slash paths even on Windows; show native form.
                self.dst_input.setText(self._normalize_local(d))
        else:
            kind = "ssh" if self.dest_type == "SSH" else "cloud"
            self._open_remote_browser(kind, self.dst_input.text(),
                                      lambda path: self.dst_input.setText(path))

    def remove_source(self, idx):
        del self.sources[idx]
        self.render_sources()

    def _render_source_warnings(self, counts, multi):
        while self.warn_wrap.count():
            item = self.warn_wrap.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        dups = [k for k, n in counts.items() if n > 1]
        remote = multi and any(is_remote(s["p"]) for s in self.sources)
        msgs = []
        if remote:
            msgs.append("Multi-source μόνο για local — τα SSH/cloud sources θα απορριφθούν.")
        if dups:
            msgs.append("Ίδιο basename <b>" + ", ".join(dups) +
                        "</b> σε >1 source → merge στο destination.")
        if msgs:
            bar = QFrame()
            bar.setObjectName("warnbar")
            bl = QHBoxLayout(bar)
            bl.setContentsMargins(12, 10, 12, 10)
            bl.setSpacing(9)
            wi = QLabel(ic("alert-triangle"))
            wi.setObjectName("warnicon")
            wi.setAlignment(Qt.AlignTop)
            wt = QLabel("<br>".join(msgs))
            wt.setObjectName("warntext")
            wt.setWordWrap(True)
            wt.setTextFormat(Qt.RichText)
            bl.addWidget(wi)
            bl.addWidget(wt, 1)
            self.warn_wrap.addWidget(bar)
            spacer = QWidget()
            spacer.setFixedHeight(4)
            self.warn_wrap.addWidget(spacer)

    # ─────────────────────────────────────────────── destination ──
    def set_dest_type(self, k):
        self.dest_type = k
        self.render_dest()
        self._normalize_dest()           # switching to Local → fix E:/ → E:\

    def render_dest(self):
        self.dst_icon.setText(ic(self._src_icon(self.dest_type)))
        for k, t in self.dst_tags.items():
            t.setProperty("state", "on" if k == self.dest_type else "off")
            repolish(t)

    def toggle_advanced(self):
        self.adv.setVisible(not self.adv.isVisible())
        self.adv_toggle.setProperty("active", self.adv.isVisible())
        repolish(self.adv_toggle)

    # ─────────────────────────────────────────── real transfer ──
    def _sudo_active(self):
        return self.flag_opts["--use-sudo"].property("active")

    def _fc_script_path(self):
        cand = os.path.join(HERE, "fast_copy.py")
        if os.path.isfile(cand):
            return cand
        return getattr(fc, "__file__", None) if FC_OK else None

    def _build_argv(self, dry, sources, dest):
        argv = list(sources) + [dest, "--progress-json"]
        if not self.transfer_opts["dedup"].property("active"):
            argv.append("--no-dedup")
        if not self.transfer_opts["verify"].property("active"):
            argv.append("--no-verify")
        if dry:
            argv.append("--dry-run")
        h = self.hash_sel.currentText().strip()
        if h:
            argv += ["--hash", h]
        argv += ["--threads", self.thread_sel.currentText().strip() or "4"]
        if self.buf_input.text().strip():
            argv += ["--buffer", self.buf_input.text().strip()]
        if self.conc_input.text().strip():
            argv += ["--cloud-concurrency", self.conc_input.text().strip()]
        if self.chunk_input.text().strip():
            argv += ["--chunk-size", self.chunk_input.text().strip()]
        for p in self.exclude_patterns:
            argv += ["--exclude", p]
        # Always send an explicit --preserve so deselecting every chip means
        # "preserve nothing" (--preserve none). Omitting the flag would let the
        # engine fall back to its mode,times default — the opposite of intent.
        preserve = [k for k, o in self.meta_opts.items() if o.property("active")]
        argv += ["--preserve", ",".join(preserve) if preserve else "none"]
        if self.flag_opts["overwrite all"].property("active"):
            argv.append("--overwrite")
        if self.flag_opts["force"].property("active"):
            argv.append("--force")
        if self.flag_opts["no hash cache"].property("active"):
            argv.append("--no-cache")
        if self.flag_opts["--use-sudo"].property("active"):
            argv.append("--use-sudo")
        for p in self.index_existing_paths:
            argv += ["--index-existing", p]
        if self.flag_opts["dedup existing"].property("active"):
            argv.append("--dedup-existing")
        if self.flag_opts["include node_modules"].property("active"):
            argv.append("--include-node-modules")
        return argv

    def _ssh_auth_flags(self, path, which):
        """SSH auth for one endpoint. which='dst'|'src'.
        Returns (extra_argv, env_dict), or None if the user cancelled.
        A matched saved connection supplies key/password/port directly; an
        ad-hoc host gets a password prompt (leave blank to use key/agent)."""
        prefix = "--ssh-" + which
        var = "FC_SSH_" + which.upper() + "_PW"
        extra, env = [], {}
        matched = self._match_conn("ssh", path)
        if matched:
            c = self.conns[matched]
            if c.get("key"):
                extra += [prefix + "-key", os.path.expanduser(c["key"])]
            if c.get("password"):
                env[var] = str(c["password"])
                extra += [prefix + "-password-env", var]
            try:
                # Default ONLY when the saved value is missing/empty — a stored
                # port (even 0) must pass through, not be rewritten by `or 22`.
                _pt = c.get("port", 22)
                port = int(_pt) if _pt not in (None, "") else 22
            except (ValueError, TypeError):
                port = 22
            if port != 22:
                extra += [prefix + "-port", str(port)]
            return extra, env
        # ad-hoc host (not saved) — offer a password, blank = use key/agent
        m = re.match(r"(?:([^@]+)@)?([^:/]+):", path or "")
        who = ((m.group(1) + "@") if m and m.group(1) else "") + \
              (m.group(2) if m else path)
        dlg = PasswordDialog(
            self, "SSH password",
            f"Password or key-passphrase for <span style='font-family:monospace'>{who}</span>. "
            "Leave blank for an unencrypted key / ssh-agent.",
            "Password / key passphrase", allow_empty=True)
        if not dlg.exec():
            return None
        pw = dlg.input.text()
        if pw:
            env[var] = pw
            extra += [prefix + "-password-env", var]
        return extra, env

    def run_transfer(self, dry):
        if self.running:
            return
        if not FC_OK or not self._fc_script_path():
            self.show_toast("fast_copy.py not found next to the GUI")
            return
        self._normalize_dest()                # E:/test3 → E:\test3 before we run
        sources = [s["p"].strip() for s in self.sources if s["p"].strip()]
        dest = self.dst_input.text().strip()
        if self.dest_type == "Local":
            dest = self._normalize_local(dest)
        if not sources:
            self.show_toast("Add at least one source")
            return
        if not dest:
            self.show_toast("Set a destination")
            return

        # Resolve SSH auth (key / password) for remote endpoints.
        ssh_extra, ssh_env = [], {}
        involves_ssh = False
        if self.dest_type == "SSH":
            r = self._ssh_auth_flags(dest, "dst")
            if r is None:
                return                       # user cancelled
            ssh_extra += r[0]
            ssh_env.update(r[1])
            involves_ssh = True
        if len(sources) == 1 and self.sources and self.sources[0]["t"] == "SSH":
            r = self._ssh_auth_flags(sources[0], "src")
            if r is None:
                return
            ssh_extra += r[0]
            ssh_env.update(r[1])
            involves_ssh = True
        if involves_ssh:
            # SSH transfers always use plain SSH (tar over the exec channel) —
            # no SFTP is attempted at all.
            ssh_extra.append("--ssh-no-sftp")

        proc = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        # Pass the unlocked credentials passphrase so the child can decrypt
        # credentials.json for named cloud/SSH connections without prompting.
        if self.creds_pw:
            env.insert("FAST_COPY_CREDS_PASSPHRASE",
                       bytes(self.creds_pw).decode("utf-8", "replace"))
        for k, v in ssh_env.items():
            env.insert(k, v)
        env.insert("PYTHONUNBUFFERED", "1")
        env.insert("NO_COLOR", "1")
        proc.setProcessEnvironment(env)
        core_argv = self._build_argv(dry, sources, dest) + ssh_extra
        if getattr(sys, "frozen", False):
            # bundled single-file build: re-invoke ourselves as the engine
            proc.setProgram(sys.executable)
            proc.setArguments(["--fc-core"] + core_argv)
        else:
            proc.setProgram(sys.executable)
            proc.setArguments([self._fc_script_path()] + core_argv)

        self._run_meta = {
            "dry": dry, "cfg": self._current_config(), "last": None,
            "dedup_bytes": None, "files_total": None, "bytes_written": None,
            "speed_bps": None, "outbuf": "", "errtail": "", "in_tb": False,
            # Memory-bounded transcript keeping head + tail (see _BoundedLog):
            # bounds memory on huge runs without dropping the earliest lines,
            # which is where the root-cause error usually is.
            "t0": time.monotonic(), "verified": None,
            "fulllog": _BoundedLog(),
            "skipped_unreadable": 0,
        }
        self.running = True
        self.start_btn.setEnabled(False)
        self.preview_btn.setEnabled(False)
        self.pstate.setText("Dry run…" if dry else "Copying…")
        self._clear_log()
        self._stop_indeterminate()
        self.barf.setFixedWidth(0)
        self.stat_vals["Progress"].setText("0%")
        self.stat_vals["Speed"].setText("—")
        self.stat_vals["ETA"].setText("—")
        self.stat_vals["Dedup saved"].setText("—")
        self.pfiles.setText("0 / 0 files")

        proc.readyReadStandardOutput.connect(self._on_proc_stdout)
        proc.readyReadStandardError.connect(self._on_proc_stderr)
        proc.finished.connect(self._on_proc_finished)
        proc.errorOccurred.connect(self._on_proc_error)
        self._proc = proc
        proc.start()

    @staticmethod
    def _strip_ansi(s):
        return re.sub(r"\x1b\[[0-9;]*m", "", s)

    def _on_proc_stdout(self):
        data = bytes(self._proc.readAllStandardOutput()).decode("utf-8", "replace")
        buf = self._run_meta["outbuf"] + data
        parts = buf.split("\n")
        self._run_meta["outbuf"] = parts.pop()
        self._run_meta["fulllog"].extend(parts)   # raw, for the diagnostic log file
        for line in parts:
            self._handle_out_line(line)

    def _on_proc_stderr(self):
        data = bytes(self._proc.readAllStandardError()).decode("utf-8", "replace")
        for line in data.splitlines():
            self._run_meta["fulllog"].append("[stderr] " + line)  # raw, for the log file
            line = self._strip_ansi(line).rstrip()
            if line.strip():
                self._add_log(line)
                self._run_meta["errtail"] = line

    _PHASE_LABELS = (
        ("scanning", "Scanning…"), ("indexing", "Indexing…"),
        ("dedup", "Hashing…"), ("incremental", "Checking…"),
        ("mapping", "Mapping…"), ("block copy", "Copying…"),
        ("verif", "Verifying…"),
    )

    def _phase_label(self, line):
        """Map a 'Phase N — <name>' banner to a friendly active-state header label
        (so it tracks the real work, e.g. 'Hashing…' during dedup). Returns None for
        non-banner lines and for the end-of-run timing block (those carry a time)."""
        m = re.search(r"Phase\s+[\w.]+\s*[—-]\s*(.+)", line)
        if not m:
            return None
        name = m.group(1).strip().lower()
        if any(c.isdigit() for c in name):   # skip the timing block ("… 6.6s")
            return None
        for key, label in self._PHASE_LABELS:
            if key in name:
                return label
        return None

    def _start_indeterminate(self):
        if not self._pulse.isActive():
            self._pulse_x = 0.0
            self._pulse.start()

    def _stop_indeterminate(self):
        if self._pulse.isActive():
            self._pulse.stop()

    def _pulse_tick(self):
        # Triangle wave 0→1→0 so the fill breathes between 12% and 100% width —
        # a clear "working" signal without a fake percentage.
        self._pulse_x = (self._pulse_x + 0.02) % 1.0
        tri = 1.0 - abs(2.0 * self._pulse_x - 1.0)
        self.barf.setFixedWidth(int(self.bar.width() * (0.12 + 0.88 * tri)))

    def _handle_out_line(self, line):
        # A line can carry several \r-overwritten progress updates ("Scanning…
        # 1000", "…2000", "…3000"); a terminal shows only the last. rstrip FIRST
        # so a trailing \r (Windows \r\n line endings, after splitting on \n)
        # is removed BEFORE we take the last \r-segment — otherwise every line
        # collapses to "" and the log goes blank on Windows.
        line = self._strip_ansi(line).rstrip()
        if "\r" in line:
            line = line.split("\r")[-1]
        if not line.strip():
            return
        if line.lstrip().startswith("{"):
            import json
            try:
                ev = json.loads(line)
            except ValueError:
                self._add_log(line)
                return
            if ev.get("t") == "phase":
                # pre-copy phase (hashing / mapping / indexing): label the header
                # with the phase and show its live N/total so it never reads a
                # stuck "Copying… 0/0".
                self.pstate.setText((ev.get("phase") or "Working") + "…")
                fd = int(ev.get("files_done", 0))
                ft = int(ev.get("files_total", 0))
                if ft:
                    self._stop_indeterminate()
                    pct = ev.get("pct", 0) or 0
                    self.barf.setFixedWidth(int(self.bar.width() * min(pct, 100) / 100))
                    self.stat_vals["Progress"].setText(f"{round(pct)}%")
                    self.pfiles.setText(f"{fd:,} / {ft:,} files")
                else:
                    # Unknown total (discovery walk, e.g. indexing existing) —
                    # pulse the bar and show the running count, not a stuck 0%.
                    self._start_indeterminate()
                    self.stat_vals["Progress"].setText("—")
                    self.pfiles.setText(f"{fd:,} files")
                # Prefer MB/s when the engine sends a byte rate (hashing large
                # files) — "10 files/s" reads as slow while the disk is actually
                # maxed out; the real throughput tells the true story.
                brate = ev.get("bytes_rate", 0) or 0
                rate = ev.get("rate", 0) or 0
                if brate:
                    self.stat_vals["Speed"].setText(fmt_size(brate) + "/s")
                elif rate:
                    self.stat_vals["Speed"].setText(f"{rate:,.0f} files/s")
                else:
                    self.stat_vals["Speed"].setText("—")
                self.stat_vals["ETA"].setText("—")
                return
            if ev.get("t") == "verify":
                # verify phase: bar now shows how many files are verified
                self._stop_indeterminate()
                self.pstate.setText("Verifying…")
                pct = ev.get("pct", 0) or 0
                self.barf.setFixedWidth(int(self.bar.width() * min(pct, 100) / 100))
                self.stat_vals["Progress"].setText(f"{round(pct)}%")
                self.pfiles.setText(
                    f"{int(ev.get('files_done', 0)):,} / {int(ev.get('files_total', 0)):,} verified")
                self.stat_vals["ETA"].setText("—")
                return
            if ev.get("t") in ("progress", "done"):
                self._stop_indeterminate()
                pct = ev.get("pct", 0) or 0
                self.barf.setFixedWidth(int(self.bar.width() * min(pct, 100) / 100))
                self.stat_vals["Progress"].setText(f"{round(pct)}%")
                self.pfiles.setText(
                    f"{int(ev.get('files_done', 0)):,} / {int(ev.get('files_total', 0)):,} files")
                self.stat_vals["Speed"].setText(fmt_size(ev.get("speed_bps", 0) or 0) + "/s")
                self.stat_vals["ETA"].setText(
                    "0s" if ev.get("t") == "done" else fmt_time(ev.get("eta_s", 0) or 0))
                self._run_meta["last"] = ev
            return
        # plain text → log (collapsing Python tracebacks to one clean line)
        self._emit_log(line)
        ph = self._phase_label(line)
        if ph:
            self.pstate.setText(ph)
        if re.search(r"Verified:?\s+(?:all\s+)?\d+\s+(?:files?|objects?)", line):
            self._run_meta["verified"] = True
        elif re.search(r"Verification failed|verify mismatch|MISSING:|SIZE MISMATCH",
                       line):
            self._run_meta["verified"] = False
        ms = re.search(r"(\d+) file\(s\) could NOT be read", line)
        if ms:
            self._run_meta["skipped_unreadable"] = int(ms.group(1))
        m = re.search(r"saved:?\s+([\d.]+\s*[KMGTP]?i?B)", line)
        if m:
            self._run_meta["dedup_bytes"] = parse_size(m.group(1))
            self.stat_vals["Dedup saved"].setText(m.group(1).strip())
        mf = re.search(r"Files:\s*([\d,]+)\s*total", line)
        if mf:
            n = int(mf.group(1).replace(",", ""))
            self._run_meta["files_total"] = n
            self.pfiles.setText(f"{n:,} / {n:,} files")
        md = re.search(r"Data:\s*([\d.]+\s*[KMGTP]?i?B)\s+written", line)
        if md:
            self._run_meta["bytes_written"] = parse_size(md.group(1))
        ms = re.search(r"Speed:\s*([\d.]+\s*[KMGTP]?i?B)/s", line)
        if ms:
            self._run_meta["speed_bps"] = parse_size(ms.group(1))
            self.stat_vals["Speed"].setText(ms.group(1).strip() + "/s")

    @staticmethod
    def _friendly(msg):
        """Turn a raw engine error into a clean, single-line message."""
        low = msg.lower()
        if any(k in low for k in ("channel closed", "open_sftp", "invoke_subsystem",
                                  "subsystem", "sftpclient")):
            return ("SFTP is not available on the SSH server. Enable SFTP "
                    "(Synology: Control Panel → File Services → FTP → tick "
                    "“Enable SFTP service”).")
        # drop noisy exception-class prefixes like 'paramiko.ssh_exception.X: '
        msg = re.sub(r"^([\w.]+(Error|Exception)):\s*", "", msg)
        return msg.replace("Error: ", "")

    def _emit_log(self, line):
        """Append a log line, but fold multi-line Python tracebacks into a single
        clean message (the user never wants raw tracebacks)."""
        rm = self._run_meta
        stripped = line.strip()
        if stripped.startswith("Traceback (most recent call last)"):
            rm["in_tb"] = True
            return
        if rm.get("in_tb"):
            # suppress frame lines / caret markers; the first non-indented line
            # is the exception message that ends the traceback.
            if (line[:1] in (" ", "\t") or stripped.startswith("File ")
                    or (stripped and set(stripped) <= set("^~ "))):
                return
            rm["in_tb"] = False
            msg = self._friendly(stripped)
            self._add_log(msg)
            rm["errtail"] = msg
            return
        self._add_log(line)
        if re.match(r"\s*(Error|Fatal|Authentication failed)", stripped):
            rm["errtail"] = self._friendly(stripped)

    def _on_proc_error(self, err):
        if not self.running:
            return
        self.running = False
        self.pstate.setText("Failed")
        self.start_btn.setEnabled(True)
        self.preview_btn.setEnabled(True)
        self.show_toast("Could not start fast_copy.py")

    def _on_proc_finished(self, code, status):
        # Drain anything still buffered in the pipe (finished can fire before the
        # last readyRead), then flush the trailing partial line.
        tail = bytes(self._proc.readAllStandardOutput()).decode("utf-8", "replace")
        if tail:
            buf = self._run_meta["outbuf"] + tail
            parts = buf.split("\n")
            self._run_meta["outbuf"] = parts.pop()
            for line in parts:
                self._handle_out_line(line)
        err = bytes(self._proc.readAllStandardError()).decode("utf-8", "replace")
        for line in err.splitlines():
            line = self._strip_ansi(line).rstrip()
            if line.strip():
                self._emit_log(line)
        if self._run_meta["outbuf"].strip():
            self._handle_out_line(self._run_meta["outbuf"])
            self._run_meta["outbuf"] = ""
        self.running = False
        self.start_btn.setEnabled(True)
        self.preview_btn.setEnabled(True)
        rm = self._run_meta
        dry = rm["dry"]
        ok = (code == 0)
        skipped = (code == 3)   # engine EXIT_SOURCE_UNREADABLE: only unreadable
        n_skip = rm.get("skipped_unreadable", 0)   # source files skipped, rest OK
        last = rm["last"] or {}
        elapsed = time.monotonic() - rm.get("t0", time.monotonic())
        vtxt = (" · ✓ verified" if rm["verified"] is True
                else " · ✗ verify failed" if rm["verified"] is False else "")
        self._stop_indeterminate()
        if ok or skipped:
            self.barf.setFixedWidth(self.bar.width())
            self.stat_vals["Progress"].setText("100%")
            self.stat_vals["ETA"].setText("0s")
        if ok:
            self.pstate.setText("Plan ready" if dry
                                else f"Done · {fmt_time(elapsed)}{vtxt}")
        elif skipped:
            self.pstate.setText(f"Completed · {fmt_time(elapsed)} · "
                                f"{n_skip} skipped (unreadable)")
        else:
            self.pstate.setText(f"Failed · {fmt_time(elapsed)}")

        # Record EVERY real (non-dry) run — success or failure — so history is
        # never silently empty. Dry runs / previews are not recorded.
        if not dry:
            files = rm["files_total"] or int(last.get("files_total")
                                             or last.get("files_done", 0))
            nbytes = rm["bytes_written"]
            if not nbytes:
                nbytes = int(last.get("bytes_total") or last.get("bytes_done", 0))
            speed = rm["speed_bps"] or (last.get("speed_bps", 0) or 0)
            self._write_history({
                "config": rm["cfg"],
                "status": "ok" if ok else "partial" if skipped else "failed",
                "error": (None if ok else
                          f"{n_skip} source file(s) unreadable — skipped" if skipped
                          else (rm["errtail"] or f"exit code {code}")),
                "stats": {
                    "files": files, "bytes": nbytes,
                    "dedup_saved_bytes": rm["dedup_bytes"],
                    "speed_bps": speed,
                    "elapsed_s": last.get("elapsed_s") or elapsed,
                    "verified": rm["verified"],
                },
            })

        if dry:
            self.show_toast("Dry run complete" if ok else "Dry run failed — see log")
        elif ok:
            self.show_toast(f"Transfer complete in {fmt_time(elapsed)}"
                            + (" · verified" if rm["verified"] else ""))
        elif skipped:
            self.show_toast(f"Completed — {n_skip} file(s) unreadable, skipped "
                            f"(everything else copied)")
        else:
            logpath = self._write_transfer_log()
            tail = rm["errtail"]
            msg = "Transfer failed" + (": " + tail if tail else f" (exit {code})")
            self.show_toast(msg + ("  ·  full log saved for diagnosis" if logpath else ""))

    def _write_transfer_log(self):
        """Persist the full RAW engine output of the last run to a file, so a
        failure stays diagnosable even though the UI keeps errors to one clean
        line (no tracebacks). Returns the path, or None on error."""
        try:
            os.makedirs(self._config_dir(), exist_ok=True)
            path = os.path.join(self._config_dir(), "last-transfer.log")
            fl = self._run_meta.get("fulllog")
            lines = fl.lines() if fl is not None else []
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            return path
        except OSError:
            return None

    def _clear_log(self):
        while self.log_lay.count() > 1:
            item = self.log_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _add_log(self, text, link=False):
        l = QLabel(text)
        l.setObjectName("logline")
        v = DARK if self.dark else LIGHT
        if link:
            l.setTextFormat(Qt.RichText)
            l.setText(f"<span style='color:{v['ok']}'>linked</span> 312 duplicates (8.3 GB saved)")
        else:
            # Colour like the terminal: dim rules, cyan phase banners, green ✓,
            # red errors — so the GUI log reads like the CLI output, not flat grey.
            s = text.strip()
            color = None
            if s and set(s) <= set("─—-=_ "):
                color = v["faint"]
            elif re.match(r"\s*Phase\b", text):
                color = v["accent"]
            elif "✓" in text:
                color = v["ok"]
            elif re.search(r"✗|\bError\b|\bFailed\b|FAILED|mismatch", text):
                color = v["danger"]
            if color:
                from html import escape
                l.setTextFormat(Qt.RichText)
                l.setText(f"<span style='color:{color}; white-space:pre'>"
                          f"{escape(text)}</span>")
        self.log_lay.insertWidget(self.log_lay.count() - 1, l)
        QTimer.singleShot(0, lambda: self.log_scroll.verticalScrollBar().setValue(
            self.log_scroll.verticalScrollBar().maximum()))

    # ───────────────────────────────────────────────── connections ──
    def _conn_summary(self, c):
        t = c.get("type", "s3")
        if t == "s3":
            r = f"endpoint={c.get('endpoint_url', 'AWS')}  key={mask(c.get('access_key_id'))}"
            if c.get("container"):
                r += f"  default={c['container']}"
            return r
        if t == "az":
            if c.get("connection_string"):
                return f"conn={mask(c['connection_string'])}"
            return f"account={c.get('account')}  key={mask(c.get('key'))}"
        if t == "gs":
            return f"project={c.get('project')}  creds={c.get('credentials', 'ADC')}"
        if t == "ssh":
            a = (f"key={c['key']}" if c.get("key")
                 else "password=***" if c.get("password") else "agent")
            return f"{c.get('user')}@{c.get('host')}:{c.get('port', 22)}  {a}"
        return ""

    def _conn_card(self, name, c):
        card = QFrame()
        card.setObjectName("conn")
        cl = QHBoxLayout(card)
        cl.setContentsMargins(14, 12, 14, 12)
        cl.setSpacing(13)
        ctype = c.get("type", "s3")
        iconname = ("server-2" if ctype == "ssh" else "brand-google" if ctype == "gs"
                    else "brand-azure" if ctype == "az" else "brand-aws")
        cic = QLabel(ic(iconname))
        cic.setObjectName("connicon")
        col = QVBoxLayout()
        col.setSpacing(2)
        nm = QLabel(name)
        nm.setObjectName("connname")
        mt = QLabel(self._conn_summary(c))
        mt.setObjectName("connmeta")
        mt.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        col.addWidget(nm)
        col.addWidget(mt)

        st = self.conn_state.get(name, "idle")
        if st == "ok":
            badge = QLabel(ic("check") + " tested")
            badge.setObjectName("badgeok")
        elif st == "test":
            badge = QLabel("testing…")
            badge.setObjectName("badgetest")
        else:
            badge = QLabel("untested")
            badge.setObjectName("badgeidle")

        test = iconbtn("flask", "ib")
        test.clicked.connect(lambda _=False, n=name: self.test_conn(n))
        edit = iconbtn("pencil", "ib")
        edit.clicked.connect(lambda _=False, n=name: self.open_conn_modal(n))
        delete = iconbtn("trash", "ibdel")
        delete.clicked.connect(lambda _=False, n=name: self.delete_conn(n))

        cl.addWidget(cic)
        cl.addLayout(col, 1)
        cl.addWidget(badge)
        cl.addWidget(test)
        cl.addWidget(edit)
        cl.addWidget(delete)

        card.doubleClicked = None  # frames aren't ClickFrame; double-click via event filter
        card.mouseDoubleClickEvent = lambda e, n=name: self.open_conn_modal(n)
        return card

    def render_conns(self):
        self._render_creds_banner()
        for box in (self.cloud_conns, self.ssh_conns):
            while box.count():
                item = box.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
        # Locked: don't show anything until the user unlocks the file.
        if self.creds_encrypted and not self.creds_loaded:
            self._empty(self.cloud_conns)
            self._empty(self.ssh_conns)
            return
        cloud = [(n, c) for n, c in self.conns.items()
                 if isinstance(c, dict) and c.get("type") != "ssh"]
        ssh = [(n, c) for n, c in self.conns.items()
               if isinstance(c, dict) and c.get("type") == "ssh"]
        if cloud:
            for n, c in cloud:
                self.cloud_conns.addWidget(self._conn_card(n, c))
        else:
            self._empty(self.cloud_conns)
        if ssh:
            for n, c in ssh:
                self.ssh_conns.addWidget(self._conn_card(n, c))
        else:
            self._empty(self.ssh_conns)

    def _render_creds_banner(self):
        if not hasattr(self, "creds_banner"):
            return
        while self.creds_banner.count():
            item = self.creds_banner.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        # tamper / re-bind note — shown until a successful Rekey re-binds the file
        if self.creds_tamper:
            bar = QFrame()
            bar.setObjectName("warnbar")
            bl = QHBoxLayout(bar)
            bl.setContentsMargins(12, 10, 12, 10)
            bl.setSpacing(9)
            wi = QLabel(ic("alert-triangle"))
            wi.setObjectName("warnicon")
            wi.setAlignment(Qt.AlignTop)
            wt = QLabel("This <b>credentials.json</b> was bound to a different "
                        "fast_copy.py (tamper check). If you just updated fast-copy, "
                        "press <b>Rekey</b> to re-bind it.")
            wt.setObjectName("warntext")
            wt.setWordWrap(True)
            wt.setTextFormat(Qt.RichText)
            wt.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            rk = textbtn("Rekey", "key", oid="btnsm")
            rk.clicked.connect(self.rekey_credentials)
            bl.addWidget(wi)
            bl.addWidget(wt, 1)
            bl.addWidget(rk)
            self.creds_banner.addWidget(bar)
            self.creds_banner.addSpacing(8)
        if self.creds_encrypted and not self.creds_loaded:
            card = QFrame()
            card.setObjectName("conn")
            cl = QHBoxLayout(card)
            cl.setContentsMargins(14, 12, 14, 12)
            cl.setSpacing(13)
            icon = QLabel(ic("key"))
            icon.setObjectName("connicon")
            col = QVBoxLayout()
            col.setSpacing(2)
            nm = QLabel("credentials.json is locked")
            nm.setObjectName("connname")
            mt = QLabel(self.creds_path or "")
            mt.setObjectName("connmeta")
            mt.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            col.addWidget(nm)
            col.addWidget(mt)
            unlock = textbtn("Unlock", "lock-open", oid="btnsmprimary")
            unlock.clicked.connect(self.unlock_credentials)
            cl.addWidget(icon)
            cl.addLayout(col, 1)
            cl.addWidget(unlock)
            self.creds_banner.addWidget(card)
            self.creds_banner.addSpacing(8)
        elif not FC_OK:
            l = QLabel(ic("alert-triangle") + "  fast_copy.py not found — showing demo connections")
            l.setObjectName("sub")
            self.creds_banner.addWidget(l)
            self.creds_banner.addSpacing(8)
        elif self.creds_loaded or (self.creds_path and os.path.isfile(self.creds_path)):
            n = len([c for c in self.conns.values() if isinstance(c, dict)])
            l = QLabel(ic("check") + f"  {self.creds_path}  ·  {n} connection(s)")
            l.setObjectName("sub")
            l.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            self.creds_banner.addWidget(l)
            self.creds_banner.addSpacing(8)

    def _empty(self, box):
        l = QLabel("—")
        l.setObjectName("sub")
        box.addWidget(l)

    def test_conn(self, name):
        self.conn_state[name] = "test"
        self.render_conns()
        QTimer.singleShot(1000, lambda: self._test_done(name))

    def _test_done(self, name):
        self.conn_state[name] = "ok"
        self.render_conns()
        self.show_toast(name + ": connection OK")

    def open_conn_modal(self, name):
        dlg = ConnectionDialog(self, name, self.conns.get(name) if name else None)
        while dlg.exec():
            rname, data = dlg.result_data()
            if rname is None:
                self.show_toast(data)
                continue
            if rname != name and rname in self.conns:
                self.show_toast("Name already exists")
                continue
            snapshot = dict(self.conns)           # for rollback if the write fails
            if name and name != rname:
                del self.conns[name]
                self.conn_state.pop(name, None)
            self.conns[rname] = data
            ok, err = self._persist_credentials()
            if not ok:
                self.conns = snapshot             # keep memory == disk
                self.show_toast("Could not save: " + err)
                break
            self.render_conns()
            self.fill_conn_sel()
            self.show_toast(("Updated " if name else "Added ") + rname)
            break

    def delete_conn(self, name):
        snapshot = dict(self.conns)
        self.conns.pop(name, None)
        self.conn_state.pop(name, None)
        ok, err = self._persist_credentials()
        if not ok:
            self.conns = snapshot
            self.show_toast("Could not save: " + err)
            return
        self.render_conns()
        self.fill_conn_sel()
        self.show_toast("Removed " + name)

    def _persist_credentials(self):
        """Write self.conns to credentials.json — re-encrypting with the unlocked
        passphrase, and transparently clearing a read-only / immutable lock
        (handled by the engine's _save_credentials_file). Returns (ok, error)."""
        if not FC_OK or not self.creds_path or not hasattr(fc, "_save_credentials_file"):
            return True, None                     # demo / no engine → in-memory only
        if self.creds_encrypted and not self.creds_pw:
            return False, "Unlock credentials.json first"
        # Encrypt by default: the first time a secret would be written to a
        # not-yet-encrypted file, offer to set a passphrase and switch to
        # AES-256-GCM.
        if not self.creds_encrypted and self._has_secret_entries():
            self._offer_encryption()
            # Encryption is mandatory for secret-bearing credentials. If the
            # user cancelled the passphrase prompt (or left it blank), refuse
            # the write rather than persist passwords/keys in cleartext. The
            # caller rolls back the in-memory change, so nothing — on disk or
            # in memory — is left holding plaintext secrets.
            if not self.creds_encrypted:
                return False, ("Encryption required: enter a passphrase to "
                               "save passwords or keys. Nothing was written.")
        try:
            if self.creds_encrypted:
                fc._creds_passphrase_cache = bytearray(bytes(self.creds_pw))
            # read-only → writable (Windows attrib / Unix chmod) so the rewrite
            # isn't blocked; the engine also clears its own immutable lock.
            try:
                if os.path.exists(self.creds_path):
                    os.chmod(self.creds_path, 0o600)
            except OSError:
                pass
            fc._save_credentials_file(self.creds_path, self.conns,
                                      encrypt=bool(self.creds_encrypted))
            if hasattr(fc, "_creds_cache"):
                fc._creds_cache.clear()
            self.creds_tamper = False    # any save re-binds to this fast_copy.py
            return True, None
        except SystemExit as e:
            return False, str(e).replace("Error: ", "").strip() or "write failed"
        except Exception as e:
            msg = str(e).strip().splitlines()[0] if str(e).strip() else e.__class__.__name__
            return False, msg.replace("Error: ", "")

    def _has_secret_entries(self):
        """True if any connection holds a cleartext secret value worth encrypting
        (mirrors the engine's _entry_has_secret)."""
        has = getattr(fc, "_entry_has_secret", None)
        if not has:
            return False
        return any(isinstance(c, dict) and has(c) for c in self.conns.values())

    def _offer_encryption(self):
        """Mandatory-encryption prompt: ask for a new passphrase and switch the
        file to AES-256-GCM. No-op if the user cancels — and the caller
        (_persist_credentials) then refuses the write, so secrets are never
        persisted in cleartext."""
        dlg = PasswordDialog(
            self, "Encrypt credentials",
            "Protect your saved passwords and keys with a passphrase "
            "(AES-256-GCM). You'll need it to unlock credentials.json later. "
            "Cancelling will not save your passwords or keys.",
            "New passphrase", confirm=True)
        if not dlg.exec():
            return                       # declined → falls through to plaintext write
        pw = dlg.input.text().encode("utf-8")
        if not pw:
            return
        self.creds_pw = pw
        self.creds_encrypted = True
        self.creds_loaded = True

    def rekey_credentials(self):
        """Re-bind credentials.json to this fast_copy.py (engine `creds rekey`).
        Re-encrypts with the SAME passphrase — does not change it."""
        if not FC_OK or not self.creds_path or not hasattr(fc, "_save_credentials_file"):
            self.show_toast("Rekey needs fast_copy.py")
            return
        if not (os.path.isfile(self.creds_path) and self.creds_encrypted):
            self.show_toast("credentials.json is not encrypted — nothing to rekey")
            return
        if not self.creds_loaded or not self.creds_pw:   # must be unlocked to re-encrypt
            self.unlock_credentials()
            if not self.creds_loaded or not self.creds_pw:
                return
        ok, err = self._persist_credentials()            # writes → re-binds to this binary
        if not ok:
            self.show_toast("Rekey failed: " + err)
            return
        self.creds_tamper = False
        self.render_conns()
        self.show_toast("Re-bound credentials.json to this fast_copy.py")

    # ──────────────────────────────────────────────── file browser ──
    def fill_conn_sel(self):
        self.conn_sel.blockSignals(True)
        self.conn_sel.clear()
        self.conn_sel.addItem("This PC · local files", "__local")
        for n, c in self.conns.items():
            if isinstance(c, dict):
                self.conn_sel.addItem(f"{n} · {c.get('type', '?')}", n)
        self.conn_sel.blockSignals(False)
        self._set_browse_conn(self.conn_sel.currentData())

    def _set_browse_conn(self, val):
        if val == "__local" or val not in self.conns:
            self.browse_conn = {"name": "local", "type": "local"}
            # start in the user's home folder, browsing the real filesystem
            self.cwd = self.local_home if os.path.isdir(self.local_home) else os.path.abspath(os.sep)
        else:
            self.browse_conn = {"name": val, **self.conns[val]}
            # SSH starts at its default remote path; cloud at the bucket root
            if self.browse_conn.get("type") == "ssh":
                self.cwd = self.browse_conn.get("path") or "/"
            else:
                self.cwd = "/"
        self.selected_file = None

    def on_conn_sel(self):
        self._set_browse_conn(self.conn_sel.currentData())
        self.render_browse()

    def _local_entries(self, path):
        """Real directory listing. Returns (entries, denied):
        entries = folders first (alpha) then files with sizes;
        denied = True when the folder needs elevated privileges."""
        try:
            scanned = list(os.scandir(path))
        except PermissionError:
            return [], True
        except OSError:
            return [], False
        dirs, files = [], []
        for e in scanned:
            try:
                if e.is_dir(follow_symlinks=False) or (e.is_symlink() and os.path.isdir(e.path)):
                    dirs.append((e.name, "dir"))
                else:
                    try:
                        meta = fmt_size(e.stat(follow_symlinks=False).st_size)
                    except OSError:
                        meta = "—"
                    files.append((e.name, meta))
            except OSError:
                files.append((e.name, "—"))
        dirs.sort(key=lambda x: x[0].lower())
        files.sort(key=lambda x: x[0].lower())
        return dirs + files, False

    # listing helper run under sudo to read root-only directories
    _SUDO_LS = (
        "import os,sys\n"
        "for e in os.scandir(sys.argv[1]):\n"
        " try:\n"
        "  d=e.is_dir(follow_symlinks=False)\n"
        " except OSError:\n"
        "  d=False\n"
        " try:\n"
        "  s=0 if d else e.stat(follow_symlinks=False).st_size\n"
        " except OSError:\n"
        "  s=-1\n"
        " sys.stdout.write('%d\\t%d\\t%s\\n'%(1 if d else 0,s,e.name))\n"
    )

    def _sudo_entries(self, path):
        """List a directory via `sudo`. Returns the entries list, or None if the
        password is wrong / sudo is unavailable."""
        if not self._sudo_pw:
            return None
        try:
            proc = subprocess.run(
                ["sudo", "-S", "-p", "", sys.executable, "-c", self._SUDO_LS, path],
                input=(self._sudo_pw + "\n").encode(),
                capture_output=True, timeout=20)
        except Exception:
            return None
        if proc.returncode != 0:
            return None
        dirs, files = [], []
        for line in proc.stdout.decode("utf-8", "replace").splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            isdir, size, name = parts
            if isdir == "1":
                dirs.append((name, "dir"))
            else:
                try:
                    files.append((name, fmt_size(int(size)) if int(size) >= 0 else "—"))
                except ValueError:
                    files.append((name, "—"))
        dirs.sort(key=lambda x: x[0].lower())
        files.sort(key=lambda x: x[0].lower())
        return dirs + files

    def elevate_local(self):
        """Prompt for the sudo/admin password to browse a protected folder."""
        if sys.platform.startswith("win"):
            self.show_toast("Access denied — run as Administrator to browse this folder")
            return
        dlg = PasswordDialog(
            self, "Administrator access",
            f"Ο φάκελος <span style='font-family:monospace'>{self.cwd}</span> χρειάζεται "
            "δικαιώματα root. Δώσε το <span style='font-family:monospace'>sudo</span> "
            "password για να εμφανιστεί το περιεχόμενο.",
            "Password for sudo")
        if not dlg.exec():
            return
        self._sudo_pw = dlg.input.text()
        if self._sudo_entries(self.cwd) is None:
            self._sudo_pw = None
            self.show_toast("sudo: authentication failed")
            return
        self.show_toast("sudo authenticated")
        self.render_browse()

    @staticmethod
    def _clean_err(e):
        msg = str(e).strip()
        msg = msg.splitlines()[0] if msg else e.__class__.__name__
        msg = re.sub(r"^\[Errno [^\]]*\]\s*", "", msg)
        return msg.replace("Error: ", "")

    def _ssh_close(self, name):
        pair = self._ssh_clients.pop(name, None)
        if pair:
            for obj in pair:
                try:
                    obj.close()
                except Exception:
                    pass

    def _ssh_client(self, conn):
        """Return a live cached SSH client for this connection (no SFTP yet),
        connecting if needed."""
        name = conn["name"]
        pair = self._ssh_clients.get(name)
        if pair:
            tr = pair[0].get_transport()
            if tr and tr.is_active():
                return pair[0]
            self._ssh_close(name)
        import paramiko
        cli = paramiko.SSHClient()
        # Verify host keys (no silent MITM): load known hosts so a CHANGED key on
        # a known host is rejected (paramiko raises BadHostKeyException), and an
        # unknown host triggers an explicit fingerprint-confirmation prompt
        # (TOFU, like OpenSSH) instead of auto-adding.
        known = os.path.expanduser("~/.ssh/known_hosts")
        try:
            cli.load_system_host_keys()
        except Exception:
            pass
        try:
            cli.load_host_keys(known)
        except (OSError, IOError):
            pass
        cli.set_missing_host_key_policy(_ConfirmHostKeyPolicy(self, known))
        # Default ONLY when the saved value is missing/empty — a stored port
        # (even 0) must pass through, not be rewritten by `or 22`.
        _pt = conn.get("port", 22)
        try:
            _port = int(_pt) if _pt not in (None, "") else 22
        except (ValueError, TypeError):
            _port = 22
        kw = dict(hostname=conn.get("host"), port=_port,
                  username=conn.get("user") or None,
                  timeout=8, banner_timeout=8, auth_timeout=8)
        if conn.get("key"):
            kw["key_filename"] = os.path.expanduser(conn["key"])
        if conn.get("password"):
            kw["password"] = conn["password"]
        cli.connect(**kw)
        tr = cli.get_transport()
        if tr:
            tr.set_keepalive(15)
        self._ssh_clients[name] = [cli, None]
        return cli

    def _sftp_hint(self, msg):
        """Friendlier text for 'server has no SFTP subsystem'."""
        low = msg.lower()
        if any(k in low for k in ("channel closed", "administratively prohibited",
                                  "eof", "sftp", "subsystem")):
            return (msg + " — the server has no SFTP subsystem (and the shell "
                    "fallback also failed). Enable SFTP on the server — Synology: "
                    "Control Panel → File Services → FTP → tick “Enable SFTP "
                    "service”.")
        return msg

    def _ssh_entries(self, conn, path):
        """List a remote dir. Tries SFTP; if the server has no SFTP subsystem,
        falls back to listing over the shell (`ls`). Returns (entries, error)."""
        import stat as statmod
        try:
            import paramiko  # noqa: F401
        except ImportError:
            return None, "paramiko is not installed"
        name = conn["name"]
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            try:
                cli = self._ssh_client(conn)
            except Exception as e:
                self._ssh_close(name)
                return None, self._clean_err(e)

            sftp_err = None
            if name not in self._ssh_no_sftp:
                try:
                    pair = self._ssh_clients.get(name)
                    sftp = pair[1] if pair and pair[1] else cli.open_sftp()
                    if pair:
                        pair[1] = sftp
                    attrs = sftp.listdir_attr(path or "/")
                    dirs, files = [], []
                    for a in attrs:
                        if statmod.S_ISDIR(a.st_mode):
                            dirs.append((a.filename, "dir"))
                        else:
                            files.append((a.filename, fmt_size(a.st_size or 0)))
                    dirs.sort(key=lambda x: x[0].lower())
                    files.sort(key=lambda x: x[0].lower())
                    return dirs + files, None
                except Exception as e:
                    sftp_err = e
                    if self._ssh_clients.get(name):
                        self._ssh_clients[name][1] = None   # drop dead SFTP
                    # remember SFTP is unavailable → skip straight to shell next time
                    self._ssh_no_sftp.add(name)

            # shell fallback
            try:
                return self._ssh_ls(cli, path), None
            except Exception as e:
                base = sftp_err if sftp_err is not None else e
                return None, self._sftp_hint(self._clean_err(base))
        finally:
            QApplication.restoreOverrideCursor()

    def _ssh_ls(self, cli, path):
        """List a remote directory via `ls` (works without an SFTP subsystem).
        Dirs end with '/' thanks to -p; sizes are omitted in this mode."""
        import shlex
        cmd = "LC_ALL=C ls -1ApL -- " + shlex.quote(path or "/")
        _in, out, errs = cli.exec_command(cmd, timeout=15)
        data = out.read().decode("utf-8", "replace")
        rc = out.channel.recv_exit_status()
        if rc != 0:
            emsg = errs.read().decode("utf-8", "replace").strip()
            raise OSError(emsg or f"ls exited {rc}")
        dirs, files = [], []
        for line in data.splitlines():
            nm = line.rstrip("\r")
            if not nm or nm in ("./", "../", ".", ".."):
                continue
            if nm.endswith("/"):
                dirs.append((nm[:-1], "dir"))
            else:
                files.append((nm, ""))
        dirs.sort(key=lambda x: x[0].lower())
        files.sort(key=lambda x: x[0].lower())
        return dirs + files

    def _cloud_entries(self, conn, cwd):
        """List objects under the current prefix. Returns (entries, error)."""
        import types
        ctype = conn.get("type")
        if not FC_OK:
            return None, "fast_copy.py is required for cloud browsing"
        is_smb = ctype == "smb"
        bucket = (conn.get("share") if is_smb
                  else conn.get("container") or conn.get("bucket"))
        if not bucket:
            kind = "share" if is_smb else "bucket/container"
            return None, f"no default {kind} set on this connection"
        backend_cls = {"s3": fc.S3Backend, "az": fc.AzureBackend,
                       "gs": fc.GCSBackend, "smb": fc.SMBBackend}.get(ctype)
        if not backend_cls:
            return None, f"unsupported connection type {ctype!r}"
        prefix = "" if cwd in ("/", "") else cwd.strip("/") + "/"
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            creds = {k: v for k, v in conn.items() if k not in ("type", "name")}
            if is_smb:
                spec = fc.SMBSpec(scheme="smb", container=bucket, prefix="",
                                  connection=conn["name"], host=conn.get("host"),
                                  port=int(conn.get("port", 445)),
                                  user=conn.get("user"))
            else:
                spec = fc.CloudSpec(scheme=ctype, container=bucket, prefix="",
                                    connection=conn["name"])
            backend = backend_cls(spec, types.SimpleNamespace(), creds=creds)
            objs = backend.list_objects(prefix)
        except SystemExit as e:
            return None, self._clean_err(e)
        except Exception as e:
            return None, self._clean_err(e)
        finally:
            QApplication.restoreOverrideCursor()
        dirs, files = set(), []
        for key, meta in (objs or {}).items():
            rest = key[len(prefix):] if key.startswith(prefix) else key
            if not rest:
                continue
            if "/" in rest:
                dirs.add(rest.split("/", 1)[0])
            else:
                files.append((rest, fmt_size((meta or {}).get("size", 0))))
        entries = [(d, "dir") for d in sorted(dirs)] + sorted(files, key=lambda x: x[0].lower())
        return entries, None

    def _src_type(self):
        t = self.browse_conn["type"]
        return "SSH" if t == "ssh" else "Local" if t == "local" else "Cloud"

    def _active_tree(self):
        t = self.browse_conn["type"]
        return self.local_tree if t == "local" else self.ssh_tree if t == "ssh" else self.cloud_tree

    def _compose_path(self, conn, cwd, selected):
        """Build the display/transfer path for a (conn, cwd, selected-file)."""
        if conn["type"] == "local":
            return os.path.join(cwd, selected) if selected else cwd
        if selected:
            sub = ("/" + selected if cwd == "/" else cwd + "/" + selected)
        else:
            sub = cwd
        if conn["type"] == "ssh":
            return f"{conn.get('user', 'user')}@{conn.get('host', conn['name'])}:{sub}"
        if conn["type"] == "smb":
            # Reference the saved connection by name so credentials resolve
            # (the SMB analogue of the cloud name@bucket form); expands to
            # smb://host/share/<rel> with its password via resolve_named_endpoint.
            rel = sub.lstrip("/")
            return f"{conn['name']}:{rel}" if rel else conn["name"]
        bucket = conn.get("container", "backups")
        scheme = "az" if conn["type"] == "az" else "gs" if conn["type"] == "gs" else "s3"
        return f"{scheme}://{conn['name']}@{bucket}{sub}"

    def _cur_path(self):
        return self._compose_path(self.browse_conn, self.cwd, self.selected_file)

    def render_browse(self):
        if not self.browse_conn:
            self.fill_conn_sel()
        bc = self.browse_conn
        is_ssh = bc["type"] == "ssh"
        is_local = bc["type"] == "local"

        # breadcrumb
        while self.crumb_lay.count():
            item = self.crumb_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        cico = QLabel(ic("device-desktop" if is_local else "server-2" if is_ssh else "bucket"))
        cico.setObjectName("crumbico")
        self.crumb_lay.addWidget(cico)

        if is_local:
            crumbs = self._local_crumbs(self.cwd)   # list of (label, abspath)
        else:
            root = (bc.get("host", bc["name"]) if is_ssh else bc.get("container", "backups"))
            crumbs = [(root, "/")]
            acc = ""
            for p in ([] if self.cwd == "/" else [x for x in self.cwd.split("/") if x]):
                acc += "/" + p
                crumbs.append((p, acc))
        for i, (label, target) in enumerate(crumbs):
            if i:
                sep = QLabel(ic("chevron-right"))
                sep.setObjectName("crumbsep")
                self.crumb_lay.addWidget(sep)
            pb = QPushButton(label)
            pb.setObjectName("crumbc")
            pb.setCursor(Qt.PointingHandCursor)
            pb.clicked.connect(lambda _=False, t=target: self._crumb_to(t))
            self.crumb_lay.addWidget(pb)
        self.crumb_lay.addStretch(1)

        # file list
        while self.files_box.count():
            item = self.files_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if is_local:
            if os.path.dirname(self.cwd) != self.cwd:
                self.files_box.addWidget(self._file_item("..", "up", up=True))
            entries, denied = self._local_entries(self.cwd)
            if denied:
                sudo_entries = self._sudo_entries(self.cwd)
                if sudo_entries is not None:
                    for name, meta in sudo_entries:
                        self.files_box.addWidget(self._file_item(name, meta))
                else:
                    self.files_box.addWidget(self._locked_folder_row())
            elif not entries:
                self._files_message("— empty —")
            else:
                for name, meta in entries:
                    self.files_box.addWidget(self._file_item(name, meta))
        else:
            if self.cwd != "/":
                self.files_box.addWidget(self._file_item("..", "up", up=True))
            entries, err = (self._ssh_entries(bc, self.cwd) if is_ssh
                            else self._cloud_entries(bc, self.cwd))
            if err:
                self._files_message(ic("alert-triangle") + "  " + err)
            elif not entries:
                self._files_message("— empty —")
            else:
                for name, meta in entries:
                    self.files_box.addWidget(self._file_item(name, meta))

    def _files_message(self, text):
        l = QLabel(text)
        l.setObjectName("sub")
        l.setWordWrap(True)
        self.files_box.addWidget(l)

    def _locked_folder_row(self):
        """A clickable row prompting for sudo/admin to read a protected folder."""
        item = ClickFrame()
        item.setObjectName("fitem")
        il = QHBoxLayout(item)
        il.setContentsMargins(10, 11, 10, 11)
        il.setSpacing(11)
        fi = QLabel(ic("key"))
        fi.setObjectName("fidir")
        fn = QLabel("Permission denied — click to unlock with sudo")
        fn.setObjectName("fn")
        fs = QLabel("locked")
        fs.setObjectName("fs")
        il.addWidget(fi)
        il.addWidget(fn, 1)
        il.addWidget(fs)
        item.clicked.connect(self.elevate_local)
        return item

    def _local_crumbs(self, path):
        """Return [(label, abspath), …] for a real local path."""
        from pathlib import Path
        p = Path(path)
        parts = list(p.parts)            # ('/', 'home', 'kai') or ('C:\\', 'Users', …)
        if not parts:
            return [(path, path)]
        crumbs = [(parts[0], parts[0])]
        acc = parts[0]
        for part in parts[1:]:
            acc = os.path.join(acc, part)
            crumbs.append((part, acc))
        return crumbs

    def _file_item(self, name, meta, up=False):
        is_dir = up or meta in ("dir", "drive")
        item = ClickFrame()
        item.setObjectName("fitem")
        sel = (not is_dir and name == self.selected_file)
        item.setProperty("sel", sel)
        il = QHBoxLayout(item)
        il.setContentsMargins(10, 11, 10, 11)
        il.setSpacing(11)
        if up:
            iconname = "corner-left-up"
        elif meta == "drive":
            iconname = "device-desktop"
        elif is_dir:
            iconname = "folder"
        elif name.endswith(".json"):
            iconname = "file-text"
        else:
            iconname = "file-zip"
        fi = QLabel(ic(iconname))
        fi.setObjectName("fidir" if is_dir else "fi")
        fn = QLabel(name + ("/" if is_dir and not up else ""))
        fn.setObjectName("fn")
        fs = QLabel("" if up else ("drive" if meta == "drive" else "folder" if is_dir else meta))
        fs.setObjectName("fs")
        il.addWidget(fi)
        il.addWidget(fn, 1)
        il.addWidget(fs)

        if up:
            item.clicked.connect(self._go_up)
        elif is_dir:
            item.clicked.connect(lambda n=name: self._enter_dir(n))
        else:
            item.clicked.connect(lambda n=name: self._select_file(n))
            item.doubleClicked.connect(lambda n=name: self._dblclick_file(n))
        return item

    def _crumb_to(self, path):
        self.cwd = path
        self.selected_file = None
        self.render_browse()

    def _go_up(self):
        if self.browse_conn["type"] == "local":
            self.cwd = os.path.dirname(self.cwd) or self.cwd
        else:
            self.cwd = "/".join(self.cwd.split("/")[:-1]) or "/"
        self.selected_file = None
        self.render_browse()

    def _enter_dir(self, name):
        if self.browse_conn["type"] == "local":
            self.cwd = os.path.join(self.cwd, name)
        else:
            self.cwd = ("" if self.cwd == "/" else self.cwd) + "/" + name
        self.selected_file = None
        self.render_browse()

    def _select_file(self, name):
        self.selected_file = name
        self.render_browse()

    def _dblclick_file(self, name):
        self.selected_file = name
        self.sources.append({"p": self._cur_path(), "t": self._src_type()})
        self.render_sources()
        self.show_toast("Added " + name + " as source")

    def browse_set_source(self):
        self.sources.append({"p": self._cur_path(), "t": self._src_type()})
        self.render_sources()
        self.show_toast(("Added " + self.selected_file + " as source")
                        if self.selected_file else "Added as source")

    def browse_set_dest(self):
        self.dest_type = self._src_type()
        path = self._cur_path()
        if self.dest_type == "Local":
            path = self._normalize_local(path)
        self.dst_input.setText(path)
        self.render_dest()
        self.show_toast("Set as destination")

    # ───────────────────────────────────────────────── history ──
    HISTORY_KEEP = 100   # retention: keep the last N runs

    def _config_dir(self):
        """Per-user config dir, matching where credentials.json is stored."""
        if sys.platform.startswith("win"):
            base = os.environ.get("APPDATA") or os.path.expanduser("~")
            return os.path.join(base, "fast_copy")
        xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.path.expanduser("~"), ".config")
        return os.path.join(xdg, "fast_copy")

    def history_dir(self):
        return os.path.join(self._config_dir(), "history")

    def _current_config(self):
        """Snapshot of the transfer form — enough to display and to re-run."""
        return {
            "sources": [dict(s) for s in self.sources],
            "dest": self.dst_input.text(),
            "dest_type": self.dest_type,
            "dedup": self.transfer_opts["dedup"].property("active"),
            "verify": self.transfer_opts["verify"].property("active"),
            "hash": self.hash_sel.currentText(),
            "threads": self.thread_sel.currentText(),
            "buffer_mb": self.buf_input.text(),
            "cloud_concurrency": self.conc_input.text(),
            "chunk_mb": self.chunk_input.text(),
            "preserve": [k for k, o in self.meta_opts.items() if o.property("active")],
            "flags": [k for k, o in self.flag_opts.items() if o.property("active")],
            "exclude": list(self.exclude_patterns),
            "index_existing": list(self.index_existing_paths),
        }

    def _write_history(self, record):
        import json, datetime
        record["timestamp"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        try:
            d = self.history_dir()
            os.makedirs(d, exist_ok=True)
            # sortable, collision-resistant filename
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            with open(os.path.join(d, f"run-{stamp}.json"), "w", encoding="utf-8") as f:
                json.dump(record, f, indent=2)
            self._prune_history()
        except OSError as e:
            self.show_toast("Could not save history: " + str(e).splitlines()[0])

    def _prune_history(self):
        """Keep only the most recent HISTORY_KEEP run files."""
        try:
            files = sorted(f for f in os.listdir(self.history_dir())
                           if f.startswith("run-") and f.endswith(".json"))
        except OSError:
            return
        for old in files[:-self.HISTORY_KEEP]:
            try:
                os.remove(os.path.join(self.history_dir(), old))
            except OSError:
                pass

    def load_history(self):
        import json
        out = []
        try:
            files = sorted((f for f in os.listdir(self.history_dir())
                            if f.startswith("run-") and f.endswith(".json")), reverse=True)
        except OSError:
            return out
        for name in files:
            try:
                with open(os.path.join(self.history_dir(), name), encoding="utf-8") as f:
                    out.append(json.load(f))
            except (OSError, ValueError):
                continue
        return out

    def render_history(self):
        while self.history_box.count():
            item = self.history_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        records = self.load_history()
        if not records:
            empty = QLabel("Δεν υπάρχουν ακόμη μεταφορές. Ολοκλήρωσε ένα transfer "
                           "για να εμφανιστεί εδώ.")
            empty.setObjectName("sub")
            empty.setWordWrap(True)
            self.history_box.addWidget(empty)
            return
        for rec in records:
            self.history_box.addWidget(self._hist_card(rec))

    def _hist_summary(self, cfg):
        srcs = cfg.get("sources", [])
        dest = cfg.get("dest", "")
        if len(srcs) == 1:
            left = srcs[0].get("p", "")
        elif srcs:
            left = f"{len(srcs)} sources"
        else:
            left = "—"
        return f"{left} → {dest}"

    def _hist_when(self, iso):
        import datetime
        try:
            dt = datetime.datetime.fromisoformat(iso)
            if dt.tzinfo:
                dt = dt.astimezone()
            return dt.strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            return iso or ""

    def _hist_card(self, rec):
        cfg = rec.get("config", {})
        st = rec.get("stats", {})
        files = int(st.get("files", 0) or 0)
        data = fmt_size(st.get("bytes", 0) or 0)
        saved = st.get("dedup_saved_bytes")
        saved = fmt_size(saved) if saved else "—"
        speed = fmt_size(st.get("speed_bps", 0) or 0) + "/s"
        tm = fmt_time(st.get("elapsed_s", 0) or 0)
        when = self._hist_when(rec.get("timestamp", ""))

        card = QFrame()
        card.setObjectName("hrun")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(15, 13, 15, 13)
        lay.setSpacing(7)
        failed = rec.get("status") == "failed"
        top = QHBoxLayout()
        hp = QLabel(self._hist_summary(cfg))
        hp.setObjectName("hp")
        hp.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        if failed:
            badge = QLabel(ic("alert-triangle") + " failed")
            badge.setObjectName("badgetest")
        again = textbtn("Run again", "refresh", oid="btnsm")
        again.clicked.connect(lambda _=False, c=cfg: self._run_again(c))
        top.addWidget(hp, 1)
        if failed:
            top.addWidget(badge)
        top.addWidget(again)
        lay.addLayout(top)
        if failed:
            stats = (f"<span style='color:#9ca3af'>{rec.get('error') or 'transfer failed'}"
                     f"</span>     <span style='color:#9ca3af'>{when}</span>")
        else:
            stats = (f"files <b>{files:,}</b>     data <b>{data}</b>     "
                     f"dedup <b>{saved}</b>     speed <b>{speed}</b>     "
                     f"time <b>{tm}</b>     <span style='color:#9ca3af'>{when}</span>")
        hs = QLabel(stats)
        hs.setObjectName("hs")
        hs.setTextFormat(Qt.RichText)
        hs.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        lay.addWidget(hs)
        return card

    def _run_again(self, cfg):
        """Restore a saved transfer's form and jump to the Transfer screen."""
        if cfg.get("sources"):
            self.sources = [dict(s) for s in cfg["sources"]]
        self.dst_input.setText(cfg.get("dest", ""))
        self.dest_type = cfg.get("dest_type", "Local")
        for key, on in (("dedup", cfg.get("dedup")), ("verify", cfg.get("verify"))):
            if key in self.transfer_opts and on is not None:
                self.transfer_opts[key].setProperty("active", bool(on))
                repolish(self.transfer_opts[key])
        if cfg.get("hash"):
            self.hash_sel.setCurrentText(cfg["hash"])
        if cfg.get("threads"):
            self.thread_sel.setCurrentText(cfg["threads"])
        if "buffer_mb" in cfg:
            self.buf_input.setText(str(cfg["buffer_mb"]))
        if "cloud_concurrency" in cfg:
            self.conc_input.setText(str(cfg["cloud_concurrency"]))
        if "chunk_mb" in cfg:
            self.chunk_input.setText(str(cfg["chunk_mb"]))
        for k, o in self.meta_opts.items():
            o.setProperty("active", k in cfg.get("preserve", []))
            repolish(o)
        for k, o in self.flag_opts.items():
            o.setProperty("active", k in cfg.get("flags", []))
            repolish(o)
        if "exclude" in cfg:
            self.exclude_patterns = list(cfg["exclude"])
            self.render_chips()
        if "index_existing" in cfg:
            self.index_existing_paths = list(cfg["index_existing"])
            self.render_idx_chips()
        self.render_sources()
        self.render_dest()
        self.navigate("transfer")
        self.nav_items["transfer"].setProperty("active", True)
        repolish(self.nav_items["transfer"])
        self.show_toast("Loaded transfer — review and Start")

    def check_update(self):
        if not FC_OK or not hasattr(fc, "_fetch_releases"):
            self.show_toast("Update check needs fast_copy.py")
            return
        if getattr(self, "_upd_running", False):
            return
        self.upd_btn.setText("Checking…")
        self.upd_btn.setEnabled(False)
        self._upd_running = True
        self._upd_thread = _UpdateCheckWorker()
        self._upd_thread.done.connect(self._on_update_result)
        self._upd_thread.finished.connect(self._upd_cleanup)
        self._upd_thread.start()

    def _upd_cleanup(self):
        self._upd_running = False
        t, self._upd_thread = getattr(self, "_upd_thread", None), None
        if t:
            t.deleteLater()

    def _on_update_result(self, res):
        self.upd_btn.setEnabled(True)
        startup = getattr(self, "_startup_check", False)
        self._startup_check = False
        if res.get("err"):
            self.upd_btn.setText("Check for updates")
            if not startup:
                self.show_toast("Could not check for updates"
                                + (": " + res["err"] if res["err"] != "network" else ""))
        elif res.get("uptodate"):
            self.upd_btn.setText("Up to date")
            if not startup:
                self.show_toast(f"fast-copy is up to date (v{res['uptodate']})")
        elif res.get("latest"):
            self._update_tag = res["latest"]
            self._update_releases = res.get("releases", [])
            self.upd_btn.setText("Download " + res["latest"])
            if startup:
                # Don't nag if the user dismissed this exact version before.
                if res["latest"] in self._load_gui_settings().get("skip_versions", []):
                    return
                self._show_update_dialog(res["latest"], res["current"],
                                         self._update_releases)
            else:
                self.show_toast(f"Update available: {res['latest']} "
                                f"(you have v{res['current']}) — click to download")

    # ── startup update popup ─────────────────────────────────────────────
    def _gui_settings_path(self):
        return os.path.join(self._config_dir(), "gui_settings.json")

    def _load_gui_settings(self):
        import json
        try:
            with open(self._gui_settings_path()) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_gui_settings(self, data):
        import json
        try:
            os.makedirs(self._config_dir(), exist_ok=True)
            with open(self._gui_settings_path(), "w") as f:
                json.dump(data, f, indent=2)
        except OSError:
            pass

    def _startup_update_check(self):
        """Quietly check for a newer release at launch; if one exists (and the
        user hasn't dismissed that version), pop the UpdateDialog. Silent on
        errors / when running without the engine."""
        if not FC_OK or not hasattr(fc, "_fetch_releases"):
            return
        if getattr(self, "_upd_running", False):
            return
        self._startup_check = True
        self._upd_running = True
        self._upd_thread = _UpdateCheckWorker()
        self._upd_thread.done.connect(self._on_update_result)
        self._upd_thread.finished.connect(self._upd_cleanup)
        self._upd_thread.start()

    def _show_update_dialog(self, latest, current, releases):
        dlg = UpdateDialog(self, latest, current, releases, self._can_auto_install(),
                           DARK if self.dark else LIGHT)
        dlg.exec()
        if dlg.skip_cb.isChecked():
            s = self._load_gui_settings()
            skip = set(s.get("skip_versions", []))
            skip.add(latest)
            s["skip_versions"] = sorted(skip)
            self._save_gui_settings(s)
        if dlg.action == UpdateDialog.DOWNLOAD_ONLY:
            self.download_update(install=False)
        elif dlg.action == UpdateDialog.DOWNLOAD_INSTALL:
            self.download_update(install=True)

    def _update_btn_clicked(self):
        """One button: check first, then become a real downloader once an update
        is known."""
        if getattr(self, "_update_tag", None):
            self.download_update()
        else:
            self.check_update()

    def _gui_asset_name(self):
        """The release asset for THIS GUI build/platform."""
        import platform as _pf
        if sys.platform.startswith("win"):
            return "fast_copy_gui-windows.exe"
        if sys.platform == "darwin":
            if _pf.machine().lower() in ("x86_64", "i386"):
                return "fast_copy_gui-macos-intel.app.zip"
            return "fast_copy_gui-macos-arm64.app.zip"
        return "fast_copy_gui-linux"

    def _can_auto_install(self):
        """Whether the GUI can replace its own binary in place — like the CLI's
        self-update. Supported only on frozen Linux/Windows builds with write
        access to the binary's directory. A running-from-source (.py) launch has
        no binary to swap, and a running macOS .app bundle can't be safely
        replaced in place, so both fall back to a download + manual handoff."""
        if not getattr(sys, "frozen", False):
            return False
        if sys.platform == "darwin":
            return False
        try:
            return os.access(os.path.dirname(sys.executable), os.W_OK)
        except OSError:
            return False

    def download_update(self, install=True):
        """Fetch the matching GUI asset for the available release. With
        install=True on a frozen Linux/Windows build the new binary replaces the
        running one in place (mirroring the CLI self-update), then the GUI offers
        to relaunch. install=False (Download only), macOS .app bundles, and
        source runs download to ~/Downloads and hand off to the user. Never runs
        while a copy is in progress."""
        tag = getattr(self, "_update_tag", None)
        if not tag or not FC_OK or getattr(self, "_dl_running", False):
            return
        # Do not touch the binary while any copy/transfer is running: an
        # in-place swap mid-job could destabilise the running engine.
        if self.running:
            self.show_toast("Finish or cancel the running copy before updating")
            return
        asset = self._gui_asset_name()
        url, size = None, None
        try:
            for rel in (fc._fetch_releases() or []):
                if rel.get("tag_name") == tag:
                    for a in rel.get("assets", []):
                        if a.get("name") == asset:
                            # API asset url (a["url"]) works for PRIVATE repos with
                            # a Bearer token; browser_download_url 404s there.
                            url = a.get("url")
                            size = a.get("size")
                            break
                    break
        except Exception:
            url = None
        if not url:
            QMessageBox.warning(
                self, "Update",
                f"Couldn't find {asset} in release {tag}.\n\nDownload manually:\n"
                f"https://github.com/{GUI_REPO}/releases/tag/{tag}")
            return
        # Defence in depth: only download from GitHub over HTTPS (the URL comes
        # from the GitHub release API, but verify before replacing a binary).
        from urllib.parse import urlparse
        parsed = urlparse(url)
        allowed = {"api.github.com", "github.com", "objects.githubusercontent.com",
                   "github-releases.githubusercontent.com",
                   "release-assets.githubusercontent.com"}
        if parsed.scheme != "https" or parsed.hostname not in allowed:
            QMessageBox.critical(self, "Update",
                                 "Unexpected download URL (not HTTPS GitHub):\n" + url)
            return

        if install and self._can_auto_install():
            # Download beside the running binary so the swap is an atomic,
            # same-filesystem replace.
            dest = sys.executable + ".update_tmp"
            self._update_inplace_target = sys.executable
        else:
            downloads = os.path.join(os.path.expanduser("~"), "Downloads")
            if not os.path.isdir(downloads):
                downloads = os.path.expanduser("~")
            dest = os.path.join(downloads, asset)
            self._update_inplace_target = None
        self._dl_running = True
        self.upd_btn.setEnabled(False)
        self.upd_btn.setText("Downloading…")
        self.show_toast("Downloading " + asset + " …")
        self._dl_thread = _DownloadWorker(url, dest, expected_size=size)
        self._dl_thread.done.connect(self._on_download_done)
        self._dl_thread.finished.connect(lambda: setattr(self, "_dl_thread", None))
        self._dl_thread.start()

    def _on_download_done(self, ok, info):
        self._dl_running = False
        self.upd_btn.setEnabled(True)
        self.upd_btn.setText("Download " + getattr(self, "_update_tag", "update"))
        target = getattr(self, "_update_inplace_target", None)
        if not ok:
            if target:                       # clean a partial .update_tmp
                try:
                    os.remove(target + ".update_tmp")
                except OSError:
                    pass
            QMessageBox.critical(self, "Download failed",
                                 "Could not download the update:\n" + info)
            return
        dest = info

        # In-place install (frozen Linux/Windows): replace the running binary
        # and offer to relaunch — the GUI equivalent of `fast_copy --update`.
        if target:
            if self.running:                 # a copy started during the download
                self.show_toast("Copy in progress — update not applied")
                try:
                    os.remove(dest)
                except OSError:
                    pass
                return
            ok2, msg = self._install_inplace(dest, target)
            if not ok2:
                try:
                    os.remove(dest)
                except OSError:
                    pass
                QMessageBox.critical(
                    self, "Update failed",
                    "Could not replace the application:\n" + msg)
                return
            self._prompt_relaunch()
            return

        if dest.endswith(".app.zip"):
            hint = ("Quit fast-copy, unzip the file, and drag fast-copy.app into "
                    "Applications (replacing the old one).")
        elif sys.platform.startswith("win"):
            hint = ("Close fast-copy, then replace your current "
                    "fast_copy_gui.exe with the downloaded file.")
        else:
            hint = ("Close fast-copy, then replace your current binary with the "
                    "downloaded file (chmod +x it).")
        m = QMessageBox(self)
        m.setWindowTitle("Update downloaded")
        m.setIcon(QMessageBox.Information)
        m.setText("Downloaded to:\n" + dest)
        m.setInformativeText(hint)
        reveal = m.addButton("Reveal in folder", QMessageBox.AcceptRole)
        m.addButton("Close", QMessageBox.RejectRole)
        m.exec()
        if m.clickedButton() is reveal:
            self._reveal(dest)

    def _install_inplace(self, downloaded_path, target):
        """Replace the running GUI binary with the freshly downloaded asset,
        mirroring the CLI self-update swap: Windows renames the locked .exe out
        of the way (current -> .old) then moves the new one in; Linux does an
        atomic os.replace and restores the executable bit. Returns (ok, msg)."""
        try:
            if sys.platform.startswith("win"):
                old = target + ".old"
                try:
                    os.remove(old)
                except OSError:
                    pass
                os.rename(target, old)
                try:
                    os.rename(downloaded_path, target)
                except OSError:
                    # Swap-in failed (AV/lock on the fresh temp). Restore the
                    # original from .old so the app isn't left with NO binary at
                    # its own path, then report the failure.
                    try:
                        os.rename(old, target)
                    except OSError:
                        pass
                    raise
            else:
                try:
                    mode = os.stat(target).st_mode
                except OSError:
                    mode = 0o755
                os.replace(downloaded_path, target)
                os.chmod(target, mode)
            return True, target
        except OSError as e:
            return False, str(e)

    def _prompt_relaunch(self):
        """After an in-place update, offer to restart into the new binary."""
        tag = getattr(self, "_update_tag", "update")
        m = QMessageBox(self)
        m.setWindowTitle("Update installed")
        m.setIcon(QMessageBox.Information)
        m.setText(f"fast-copy was updated to {tag}.")
        m.setInformativeText("Restart now to use the new version?")
        r = m.addButton("Restart now", QMessageBox.AcceptRole)
        m.addButton("Later", QMessageBox.RejectRole)
        m.exec()
        # update is applied either way; reset the button out of "Download" state
        self._update_tag = None
        self._update_inplace_target = None
        self.upd_btn.setText("Up to date")
        if m.clickedButton() is r:
            args = [] if getattr(sys, "frozen", False) else [os.path.abspath(__file__)]
            QProcess.startDetached(sys.executable, args)
            self.close()

    def _reveal(self, path):
        try:
            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
        except Exception:
            pass

    # ───────────────────────────────────────────────────── toast ──
    def show_toast(self, msg):
        self.toast.show_msg(msg)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "toast"):
            self.toast._reposition()


def main():
    # Single-file bundled build: when invoked as the engine, dispatch to
    # fast_copy.main() instead of launching the GUI. (No-op for plain .py runs,
    # which shell out to fast_copy.py directly.)
    if len(sys.argv) > 1 and sys.argv[1] == "--fc-core":
        if not FC_OK:
            sys.stderr.write("fast_copy engine not bundled\n")
            raise SystemExit(2)
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        # Full CLI entry (version/creds/ls/deps/update + copies); fall back to
        # main() for older engines that predate cli_entry().
        entry = getattr(fc, "cli_entry", fc.main)
        raise SystemExit(entry())

    # Windows: give the app its own taskbar identity so it uses OUR icon
    # instead of grouping under the python/pythonw launcher icon.
    if sys.platform.startswith("win"):
        # Clean up the .old binary left by a previous in-place update — the
        # running .exe can't be deleted during the swap, only renamed, so we
        # remove it on the next launch (mirrors the CLI's cli_entry cleanup).
        if getattr(sys, "frozen", False):
            try:
                old = sys.executable + ".old"
                if os.path.exists(old):
                    os.remove(old)
            except OSError:
                pass
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "gekap.fast-copy.gui")
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setApplicationName("fast-copy")
    app.setApplicationDisplayName("fast-copy")
    app.setDesktopFileName("fast-copy")     # Wayland/GNOME taskbar association
    if not load_icon_font():
        sys.stderr.write("warning: icon font failed to load; icons may show as boxes\n")
    # window/taskbar icon for the main window and every dialog
    app.setWindowIcon(make_app_icon())
    # prefer Inter if available, else fall back to a clean system UI font
    ui = "Inter"
    if "Inter" not in QFontDatabase.families():
        ui = "Segoe UI" if sys.platform.startswith("win") else \
             ("Helvetica Neue" if sys.platform == "darwin" else "")
    base_font = QFont(ui, 10) if ui else QFont()
    if not ui:
        base_font.setPointSize(10)
    app.setFont(base_font)
    w = FastCopyGUI()
    w.show()
    # If fast_copy.py isn't next to the GUI, offer to fetch the matching version.
    QTimer.singleShot(300, w._ensure_core)
    # Quietly check for a new release shortly after launch; pops a popup with the
    # release notes + Download only / Download & install if one is available.
    QTimer.singleShot(1800, w._startup_update_check)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
