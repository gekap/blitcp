#!/usr/bin/env python3
"""
Run fast_copy.py against every available filesystem and report
a comparison table.

For each writable destination filesystem, runs three scenarios:
  1. single small file copy
  2. single large file copy (5 MB)
  3. directory tree (50 files, mixed sizes)

For non-writable destinations, just reports the detected strategy.

Captures detection metrics + copy timing + correctness.
"""

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import namedtuple

FAST_COPY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "fast_copy.py")
PYTHON = sys.executable


# ────────────────────────────────────────────────────────────────────────────
# Destinations to test
# ────────────────────────────────────────────────────────────────────────────

# Each entry: (label, root_path)
# We'll create a unique subdirectory under each for tests.
DESTINATIONS = [
    ("tmpfs",            "/tmp"),
    ("ext4 /home",       os.path.expanduser("~")),
    ("xfs /mnt/folders", "/mnt/folders"),
    ("ext4 /mnt/usb",    "/mnt/usb"),
    ("FAT32 /mnt/usb1",  "/mnt/usb1"),
]


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def make_test_source(parent, scenario):
    """Build a source tree for a given scenario.
    Returns (src_path, list_of_relative_files, total_bytes).
    """
    src = os.path.join(parent, "src_" + scenario)
    os.makedirs(src, exist_ok=True)
    files = []
    total = 0

    if scenario == "single_small":
        path = os.path.join(src, "small.txt")
        data = b"hello world\n" * 100
        with open(path, "wb") as f:
            f.write(data)
        files.append("small.txt")
        total = len(data)

    elif scenario == "single_large":
        path = os.path.join(src, "large.bin")
        size = 5 * 1024 * 1024  # 5 MB
        with open(path, "wb") as f:
            # Deterministic content for reproducibility
            chunk = (b"abcdefghijklmnop" * 4096)
            written = 0
            while written < size:
                w = min(len(chunk), size - written)
                f.write(chunk[:w])
                written += w
        files.append("large.bin")
        total = size

    elif scenario == "directory":
        for i in range(50):
            sub = "subdir{}".format(i // 10)
            os.makedirs(os.path.join(src, sub), exist_ok=True)
            rel = os.path.join(sub, "file_{:03d}.dat".format(i))
            data = ("content of file {}\n".format(i) * (i + 1)).encode()
            with open(os.path.join(src, rel), "wb") as f:
                f.write(data)
            files.append(rel)
            total += len(data)

    return src, files, total


def run_copy(src, dst):
    """Run fast_copy.py with -v (so the FS block is printed) and return
    (stdout, stderr, returncode, elapsed_s)."""
    t0 = time.perf_counter()
    result = subprocess.run(
        [PYTHON, FAST_COPY, "-v", src, dst],
        capture_output=True, text=True, timeout=300,
    )
    elapsed = time.perf_counter() - t0
    return result.stdout, result.stderr, result.returncode, elapsed


def parse_fs_info(stdout):
    """Pull the FS info out of fast_copy.py -v output.

    The new verbose format is:
        FS:          xfs → reflink
                     hardlink=y symlink=y reflink=y case=sens
                     detect=4.3ms probe=1.1ms (4 probes)
    """
    fs_type = strategy = None
    detect_ms = probe_ms = None
    caps = {}
    for line in stdout.splitlines():
        line_clean = re.sub(r'\x1b\[[0-9;]*m', '', line)
        # FS: <fs_type> → <strategy>
        m = re.search(r'FS:\s+(\S+)\s+→\s+(\S+)', line_clean)
        if m:
            fs_type = m.group(1)
            strategy = m.group(2)
            continue
        # capability matrix
        m = re.search(r'hardlink=(\w+)\s+symlink=(\w+)\s+reflink=(\w+)\s+'
                      r'case=(\w+)', line_clean)
        if m:
            caps["hardlink"] = (m.group(1) == "y")
            caps["symlink"] = (m.group(2) == "y")
            caps["reflink"] = (m.group(3) == "y")
            caps["case"] = m.group(4)
        # timings
        m = re.search(r'detect=([\d.]+)ms\s+probe=([\d.]+)ms', line_clean)
        if m:
            detect_ms = float(m.group(1))
            probe_ms = float(m.group(2))
    return fs_type, strategy, caps, detect_ms, probe_ms


def verify_copy(src, dst, files):
    """Confirm every file in `files` exists at dst with matching content."""
    for rel in files:
        sp = os.path.join(src, rel)
        dp = os.path.join(dst, rel)
        if not os.path.exists(dp):
            return False, "missing: {}".format(rel)
        if os.path.getsize(sp) != os.path.getsize(dp):
            return False, "size mismatch: {}".format(rel)
        if file_hash(sp) != file_hash(dp):
            return False, "hash mismatch: {}".format(rel)
    return True, "ok"


# ────────────────────────────────────────────────────────────────────────────
# Test runner
# ────────────────────────────────────────────────────────────────────────────

Result = namedtuple("Result", [
    "destination", "fs_type", "strategy", "scenario",
    "writable", "passed", "elapsed_s", "detection_ms", "probe_ms",
    "caps", "error",
])


def run_one(label, root, scenario):
    """Run one test scenario against one destination root.
    Returns a Result namedtuple.
    """
    if not os.path.exists(root):
        return Result(label, "?", "?", scenario,
                      writable=False, passed=False,
                      elapsed_s=0, detection_ms=0, probe_ms=0,
                      caps={}, error="path does not exist")

    if not os.access(root, os.W_OK):
        # Detection-only: try to detect and report, but no copy
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import fs_detect
            info = fs_detect.detect_capabilities(root)
            return Result(
                label, info.fs_type, info.strategy, scenario,
                writable=False, passed=False,
                elapsed_s=0,
                detection_ms=info.detection_ms,
                probe_ms=info.probe_ms,
                caps={
                    "hardlink": info.capabilities.hardlink,
                    "symlink": info.capabilities.symlink,
                    "reflink": info.capabilities.reflink,
                    "case": "sens" if info.capabilities.case_sensitive else "insens",
                },
                error="not writable (detection only)",
            )
        except Exception as e:
            return Result(label, "?", "?", scenario,
                          writable=False, passed=False,
                          elapsed_s=0, detection_ms=0, probe_ms=0,
                          caps={}, error="detect failed: {}".format(e))

    # Writable: build source/dst and run a real copy
    work = tempfile.mkdtemp(prefix="fc_dist_test_", dir=root)
    try:
        src, files, total_bytes = make_test_source(work, scenario)
        dst = os.path.join(work, "dst_" + scenario)

        out, err, rc, elapsed = run_copy(src, dst)
        fs_type, strategy, caps, detect_ms, probe_ms = parse_fs_info(out)

        if rc != 0:
            return Result(
                label, fs_type or "?", strategy or "?", scenario,
                writable=True, passed=False,
                elapsed_s=elapsed,
                detection_ms=detect_ms or 0, probe_ms=probe_ms or 0,
                caps=caps,
                error="rc={}: {}".format(rc, err.strip()[:80] or out.strip().splitlines()[-1] if out.strip() else ""),
            )

        ok, msg = verify_copy(src, dst, files)
        return Result(
            label, fs_type or "?", strategy or "?", scenario,
            writable=True, passed=ok,
            elapsed_s=elapsed,
            detection_ms=detect_ms or 0, probe_ms=probe_ms or 0,
            caps=caps,
            error="" if ok else msg,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    scenarios = ["single_small", "single_large", "directory"]
    results = []
    print("Running tests on {} destinations × {} scenarios = {} tests..."
          .format(len(DESTINATIONS), len(scenarios),
                  len(DESTINATIONS) * len(scenarios)))
    print()
    for label, root in DESTINATIONS:
        for sc in scenarios:
            r = run_one(label, root, sc)
            results.append(r)
            ok = "✓" if r.passed else ("·" if not r.writable else "✗")
            print("  {} {:<20} {:<14} {}".format(ok, label, sc, r.error or "ok"))

    # ── Build the comparison table ──────────────────────────────────────
    print()
    print("=" * 110)
    print("RESULTS")
    print("=" * 110)
    print()

    # Per-destination summary (one row per FS)
    print("Per-filesystem summary:")
    print()
    print("{:<22} {:<10} {:<10} {:>9} {:>9} {:<8} {:<8} {:<8} {:<8}".format(
        "Destination", "FS", "Strategy", "detect ms", "probe ms",
        "hardlink", "symlink", "reflink", "case"))
    print("-" * 110)
    seen = set()
    for r in results:
        key = r.destination
        if key in seen:
            continue
        seen.add(key)
        if not r.caps:
            print("{:<22} {:<10} {:<10} {:>9} {:>9} {}".format(
                r.destination[:22], r.fs_type[:10], r.strategy[:10],
                "n/a", "n/a", r.error))
        else:
            print("{:<22} {:<10} {:<10} {:>9.2f} {:>9.2f} {:<8} {:<8} {:<8} {:<8}".format(
                r.destination[:22], r.fs_type[:10], r.strategy[:10],
                r.detection_ms, r.probe_ms,
                "y" if r.caps.get("hardlink") else "n",
                "y" if r.caps.get("symlink") else "n",
                "y" if r.caps.get("reflink") else "n",
                r.caps.get("case", "?")))

    # Per-scenario timing table (only writable destinations)
    print()
    print("Per-scenario timings (writable destinations only):")
    print()
    print("{:<22} {:>15} {:>15} {:>15}".format(
        "Destination", "single_small", "single_large", "directory"))
    print("-" * 80)
    by_dest = {}
    for r in results:
        if not r.writable:
            continue
        by_dest.setdefault(r.destination, {})[r.scenario] = r
    for dest, scs in by_dest.items():
        cells = []
        for sc in ("single_small", "single_large", "directory"):
            r = scs.get(sc)
            if r and r.passed:
                cells.append("{:>13.3f} s".format(r.elapsed_s))
            elif r:
                cells.append("{:>15}".format("FAIL"))
            else:
                cells.append("{:>15}".format("n/a"))
        print("{:<22} {} {} {}".format(dest[:22], *cells))

    # Pass/fail tally
    print()
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    skipped = sum(1 for r in results if not r.writable)
    failed = total - passed - skipped
    print("Total: {} | Passed: {} | Failed: {} | Skipped (read-only): {}".format(
        total, passed, failed, skipped))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
