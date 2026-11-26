# Copyright 2026 George Kapellakis
# Licensed under the Apache License, Version 2.0
"""Where blitcp's tests and UAT scripts put their data: never under /tmp.

/tmp is tmpfs (RAM) on many Linux machines. Test trees of tens of MB per
case, kept by pytest across runs, once filled it to 4.7 GB and took the
host out of memory. Everything goes under ~/.cache/blitcp-tests instead
(BLITCP_TEST_ROOT overrides it), on a real disk.

Call use_test_tmp() before anything asks tempfile for a directory: it sets
tempfile.tempdir for this process and TMPDIR / TEMP / TMP for every child,
so a blitcp run started by a test spills there too.
"""
import os
import tempfile


def test_root():
    return os.path.expanduser(os.environ.get("BLITCP_TEST_ROOT")
                              or os.path.join("~", ".cache", "blitcp-tests"))


def use_test_tmp():
    d = os.path.join(test_root(), "tmp")
    os.makedirs(d, exist_ok=True)
    for var in ("TMPDIR", "TEMP", "TMP"):
        os.environ[var] = d
    tempfile.tempdir = d
    return d
