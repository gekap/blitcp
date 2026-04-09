#!/usr/bin/env python3
"""
Resource leak verification for fs_detect.py.

Checks:
  1. No file descriptors left open after detect_capabilities()
  2. No probe directories or files left on disk
  3. No memory growth across many runs (tracemalloc)
  4. Cleanup still works when probes fail or raise exceptions
  5. Cleanup still works under interrupted operations
"""

import gc
import os
import shutil
import sys
import tempfile
import tracemalloc
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fs_detect


def open_fd_count():
    """Return the number of file descriptors currently held by this process."""
    try:
        return len(os.listdir("/proc/self/fd"))
    except (OSError, FileNotFoundError):
        # macOS / Windows fallback
        return -1


def list_probe_files(parent):
    """Return any file or directory in `parent` whose name starts with
    `.fast_copy_probe`."""
    try:
        return [e for e in os.listdir(parent) if e.startswith(".fast_copy_probe")]
    except OSError:
        return []


# ════════════════════════════════════════════════════════════════════════════
# File descriptor leak tests
# ════════════════════════════════════════════════════════════════════════════

class TestNoFDLeaks(unittest.TestCase):
    """Verify that detection does not leak file descriptors."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="fs_fd_leak_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_no_fd_leak_default_mode(self):
        """1000 detection runs should not increase open fd count."""
        if open_fd_count() < 0:
            self.skipTest("/proc/self/fd not available")
        # Warm up to populate any caches
        for _ in range(10):
            fs_detect.detect_capabilities(self.tmpdir)
        gc.collect()
        before = open_fd_count()
        for _ in range(1000):
            fs_detect.detect_capabilities(self.tmpdir)
        gc.collect()
        after = open_fd_count()
        self.assertEqual(before, after,
                         "FD count grew from {} to {} over 1000 runs".format(
                             before, after))

    def test_no_fd_leak_force_probe(self):
        """Force-probe runs all 4 probes — verify no FD leak."""
        if open_fd_count() < 0:
            self.skipTest("/proc/self/fd not available")
        for _ in range(10):
            fs_detect.detect_capabilities(self.tmpdir, force_probe=True)
        gc.collect()
        before = open_fd_count()
        for _ in range(500):
            fs_detect.detect_capabilities(self.tmpdir, force_probe=True)
        gc.collect()
        after = open_fd_count()
        self.assertEqual(before, after,
                         "FD count grew from {} to {} over 500 force-probe runs"
                         .format(before, after))

    def test_no_fd_leak_each_probe(self):
        """Run each probe individually 1000 times and check fd count."""
        if open_fd_count() < 0:
            self.skipTest("/proc/self/fd not available")
        probe_dir = fs_detect._make_probe_dir(self.tmpdir)
        try:
            for fn in (fs_detect.probe_hardlink,
                       fs_detect.probe_symlink,
                       fs_detect.probe_reflink,
                       fs_detect.probe_case_sensitivity):
                # Warm up
                for _ in range(5):
                    fn(probe_dir)
                gc.collect()
                before = open_fd_count()
                for _ in range(250):
                    fn(probe_dir)
                gc.collect()
                after = open_fd_count()
                self.assertEqual(before, after,
                                 "{}: fd count grew from {} to {}".format(
                                     fn.__name__, before, after))
        finally:
            fs_detect._cleanup_probe_dir(probe_dir)


# ════════════════════════════════════════════════════════════════════════════
# Disk file leak tests
# ════════════════════════════════════════════════════════════════════════════

class TestNoDiskLeaks(unittest.TestCase):
    """Verify no probe files/directories are left behind on disk."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="fs_disk_leak_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_no_disk_residue_after_one_run(self):
        fs_detect.detect_capabilities(self.tmpdir)
        leftovers = list_probe_files(self.tmpdir)
        self.assertEqual(leftovers, [], "Probe files left: {}".format(leftovers))

    def test_no_disk_residue_after_many_runs(self):
        for _ in range(100):
            fs_detect.detect_capabilities(self.tmpdir)
        leftovers = list_probe_files(self.tmpdir)
        self.assertEqual(leftovers, [], "Probe files left: {}".format(leftovers))

    def test_no_disk_residue_after_force_probe(self):
        for _ in range(100):
            fs_detect.detect_capabilities(self.tmpdir, force_probe=True)
        leftovers = list_probe_files(self.tmpdir)
        self.assertEqual(leftovers, [], "Probe files left: {}".format(leftovers))

    def test_individual_probe_no_residue(self):
        """Each probe should leave nothing behind in the probe dir."""
        probe_dir = fs_detect._make_probe_dir(self.tmpdir)
        try:
            for fn in (fs_detect.probe_hardlink,
                       fs_detect.probe_symlink,
                       fs_detect.probe_reflink,
                       fs_detect.probe_case_sensitivity):
                fn(probe_dir)
                leftovers = os.listdir(probe_dir)
                self.assertEqual(leftovers, [],
                                 "{} left files: {}".format(fn.__name__, leftovers))
        finally:
            fs_detect._cleanup_probe_dir(probe_dir)


# ════════════════════════════════════════════════════════════════════════════
# Memory leak test (tracemalloc)
# ════════════════════════════════════════════════════════════════════════════

class TestNoMemoryLeak(unittest.TestCase):
    """Use tracemalloc to verify no memory growth across runs."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="fs_mem_leak_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_no_memory_growth(self):
        # Warm up
        for _ in range(20):
            fs_detect.detect_capabilities(self.tmpdir)
        gc.collect()
        tracemalloc.start()
        snap1 = tracemalloc.take_snapshot()

        for _ in range(500):
            fs_detect.detect_capabilities(self.tmpdir)
        gc.collect()
        snap2 = tracemalloc.take_snapshot()
        tracemalloc.stop()

        # Compare memory usage between snapshots, looking only at fs_detect
        diff = snap2.compare_to(snap1, "filename")
        fs_detect_diff = [d for d in diff if "fs_detect" in str(d)]

        # Total growth in bytes for fs_detect
        total_growth = sum(d.size_diff for d in fs_detect_diff)
        # Allow up to 50 KB of growth as noise (tracemalloc itself, GC slack)
        self.assertLess(abs(total_growth), 50 * 1024,
                        "fs_detect memory grew by {} bytes after 500 runs"
                        .format(total_growth))


# ════════════════════════════════════════════════════════════════════════════
# Cleanup under exceptions
# ════════════════════════════════════════════════════════════════════════════

class TestCleanupUnderExceptions(unittest.TestCase):
    """Verify cleanup runs even when probes raise unexpectedly."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="fs_excep_test_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_cleanup_after_probe_raise(self):
        """Inject a probe that raises, verify probe dir is cleaned up."""
        import unittest.mock as mock
        # Force probe_hardlink to raise mid-flight
        original = fs_detect.probe_hardlink

        def raising(probe_dir):
            # Create a stray file, then raise
            with open(os.path.join(probe_dir, "stray.tmp"), "w") as f:
                f.write("oops")
            raise RuntimeError("simulated probe failure")

        with mock.patch.object(fs_detect, "probe_hardlink", side_effect=raising):
            try:
                fs_detect.detect_capabilities(self.tmpdir, force_probe=True)
            except RuntimeError:
                pass
        # Even though the probe raised, the cleanup should have removed
        # the probe directory and all its contents.
        leftovers = list_probe_files(self.tmpdir)
        self.assertEqual(leftovers, [],
                         "Probe dir left after exception: {}".format(leftovers))

    def test_cleanup_after_keyboard_interrupt(self):
        """Simulate KeyboardInterrupt during probe — finally must still run."""
        import unittest.mock as mock

        def interrupt(probe_dir):
            with open(os.path.join(probe_dir, "stray.tmp"), "w") as f:
                f.write("oops")
            raise KeyboardInterrupt()

        with mock.patch.object(fs_detect, "probe_symlink", side_effect=interrupt):
            try:
                fs_detect.detect_capabilities(self.tmpdir, force_probe=True)
            except KeyboardInterrupt:
                pass
        leftovers = list_probe_files(self.tmpdir)
        self.assertEqual(leftovers, [],
                         "Probe dir left after KeyboardInterrupt: {}".format(leftovers))


# ════════════════════════════════════════════════════════════════════════════
# Concurrent runs
# ════════════════════════════════════════════════════════════════════════════

class TestConcurrentRuns(unittest.TestCase):
    """Verify that concurrent detect_capabilities calls don't collide."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="fs_concurrent_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_threaded_detection(self):
        """Run detection from many threads simultaneously — verify all
        complete without errors and no leftover probe dirs."""
        import threading
        errors = []

        def worker():
            try:
                for _ in range(20):
                    fs_detect.detect_capabilities(self.tmpdir)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], "Errors in threads: {}".format(errors))
        leftovers = list_probe_files(self.tmpdir)
        self.assertEqual(leftovers, [],
                         "Probe dirs left after concurrent runs: {}".format(leftovers))


if __name__ == "__main__":
    unittest.main(verbosity=2)
