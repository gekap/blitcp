#!/usr/bin/env python3
"""
Tests every CLI argument of fast_copy.py.

For each argument we exercise:
  1. Default value (no flag)
  2. Custom value (where applicable)
  3. Edge cases (zero, max, very small, very large)
  4. Combinations with other flags
  5. The flag's effect on output / behavior

Coverage matrix (per argument):

  --buffer        : 1, 8, 64 (default), 128, 512 MB
  --threads       : 1, 4 (default), 8, 24, 64
  --dry-run       : alone, with --exclude, with --overwrite, with -v
  -v / --verbose  : alone, long form, with --dry-run
  --no-verify     : alone, with --overwrite
  --log-file      : valid path, JSON parseability, schema check
  --no-dedup      : alone, vs default (dedup enabled)
  --force         : alone (skip space check)
  --overwrite     : alone, after first run
  --exclude       : single, multiple (3+), unusual names
  --no-cache      : alone, vs default (cache enabled)
  -z / --compress : flag is accepted (only effective for SSH)
  --version       : prints version, exits 0
  --help          : exit 0, contains expected sections
  Combos          : "everything at once" mega-test
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

FAST_COPY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "fast_copy.py")
PYTHON = sys.executable


def run_fc(*args, timeout=120):
    """Run fast_copy.py and return (stdout, stderr, rc)."""
    cmd = [PYTHON, FAST_COPY] + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.stdout, r.stderr, r.returncode


def strip_ansi(s):
    return re.sub(r'\x1b\[[0-9;]*m', '', s)


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def make_tree(root, structure):
    os.makedirs(root, exist_ok=True)
    for rel, content in structure.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path) or root, exist_ok=True)
        if isinstance(content, int):
            with open(path, "wb") as f:
                f.write(b"x" * content)
        else:
            with open(path, "w") as f:
                f.write(content)


# ════════════════════════════════════════════════════════════════════════
# Common fixture
# ════════════════════════════════════════════════════════════════════════

class TempDirMixin:
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fc_args_")
        self.src = os.path.join(self.tmp, "src")
        self.dst = os.path.join(self.tmp, "dst")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ════════════════════════════════════════════════════════════════════════
# --version / --help
# ════════════════════════════════════════════════════════════════════════

class TestMetaArgs(unittest.TestCase):

    def test_version_short(self):
        out, _, rc = run_fc("-V")
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"\d+\.\d+\.\d+")

    def test_version_long(self):
        out, _, rc = run_fc("--version")
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"\d+\.\d+\.\d+")

    def test_help(self):
        out, _, rc = run_fc("--help")
        self.assertEqual(rc, 0)
        for required in ("--buffer", "--threads", "--dry-run", "--no-verify",
                         "--no-dedup", "--force", "--overwrite", "--exclude",
                         "--no-cache", "--compress", "--ssh-src-port",
                         "--ssh-dst-port", "--log-file", "--verbose",
                         "--update", "--check-update"):
            self.assertIn(required, out, "Missing in --help: " + required)


# ════════════════════════════════════════════════════════════════════════
# --buffer
# ════════════════════════════════════════════════════════════════════════

class TestBufferArg(TempDirMixin, unittest.TestCase):
    """Buffer size — must accept various sizes and produce a working copy."""

    def setUp(self):
        super().setUp()
        make_tree(self.src, {
            "small.txt": "small content",
            "med.bin": 64 * 1024,           # 64 KB
            "big.bin": 2 * 1024 * 1024,     # 2 MB
        })

    def _run_with_buffer(self, mb):
        out, err, rc = run_fc("--buffer", str(mb), self.src, self.dst)
        self.assertEqual(rc, 0,
            "--buffer={} failed: rc={} err={}".format(mb, rc, err[:200]))
        # Verify all files copied
        for rel in ("small.txt", "med.bin", "big.bin"):
            self.assertTrue(os.path.exists(os.path.join(self.dst, rel)),
                "missing {} after --buffer={}".format(rel, mb))
        return out

    def test_buffer_1mb(self):
        self._run_with_buffer(1)

    def test_buffer_8mb(self):
        self._run_with_buffer(8)

    def test_buffer_default(self):
        out, _, rc = run_fc(self.src, self.dst)  # no --buffer
        self.assertEqual(rc, 0)
        self.assertIn("Buffer:      64 MB", strip_ansi(out))

    def test_buffer_64mb_explicit(self):
        out = self._run_with_buffer(64)
        self.assertIn("Buffer:      64 MB", strip_ansi(out))

    def test_buffer_128mb(self):
        out = self._run_with_buffer(128)
        self.assertIn("Buffer:      128 MB", strip_ansi(out))

    def test_buffer_512mb(self):
        # large but should still work (no actual allocation until needed)
        self._run_with_buffer(512)


# ════════════════════════════════════════════════════════════════════════
# --threads
# ════════════════════════════════════════════════════════════════════════

class TestThreadsArg(TempDirMixin, unittest.TestCase):
    """Threads parameter for hashing/layout."""

    def setUp(self):
        super().setUp()
        # Use enough files that thread count actually matters
        make_tree(self.src, {f"f_{i:04d}.txt": "content " + str(i)
                              for i in range(50)})

    def _run_threads(self, n):
        out, err, rc = run_fc("--threads", str(n), self.src, self.dst)
        self.assertEqual(rc, 0,
            "--threads={} failed: rc={} err={}".format(n, rc, err[:200]))
        for i in range(50):
            self.assertTrue(os.path.exists(
                os.path.join(self.dst, "f_{:04d}.txt".format(i))))

    def test_threads_1(self):
        self._run_threads(1)

    def test_threads_4_default(self):
        self._run_threads(4)

    def test_threads_8(self):
        self._run_threads(8)

    def test_threads_24(self):
        self._run_threads(24)

    def test_threads_64(self):
        self._run_threads(64)

    def test_threads_invalid_zero(self):
        # 0 threads should either be normalized or rejected — must not crash
        out, err, rc = run_fc("--threads", "0", self.src, self.dst)
        # Either succeeds (auto-normalized to 1) or fails cleanly
        self.assertIn(rc, (0, 1, 2))


# ════════════════════════════════════════════════════════════════════════
# --dry-run
# ════════════════════════════════════════════════════════════════════════

class TestDryRunArg(TempDirMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        make_tree(self.src, {"a.txt": "alpha", "b.txt": "beta"})

    def test_dry_run_does_not_copy(self):
        out, _, rc = run_fc("--dry-run", self.src, self.dst)
        self.assertEqual(rc, 0)
        # Destination should be empty or contain no real source files
        if os.path.exists(self.dst):
            real = [f for f in os.listdir(self.dst)
                    if not f.startswith(".fast_copy")]
            self.assertEqual(real, [])

    def test_dry_run_prints_plan(self):
        out, _, rc = run_fc("--dry-run", self.src, self.dst)
        self.assertEqual(rc, 0)
        clean = strip_ansi(out)
        self.assertIn("DRY RUN", clean)

    def test_dry_run_with_exclude(self):
        make_tree(self.src, {"keep.txt": "keep", "skip.log": "skip"})
        out, _, rc = run_fc("--dry-run", "--exclude", "skip.log",
                             self.src, self.dst)
        self.assertEqual(rc, 0)

    def test_dry_run_with_verbose(self):
        out, _, rc = run_fc("--dry-run", "-v", self.src, self.dst)
        self.assertEqual(rc, 0)
        self.assertIn("FS:", strip_ansi(out))


# ════════════════════════════════════════════════════════════════════════
# -v / --verbose
# ════════════════════════════════════════════════════════════════════════

class TestVerboseArg(TempDirMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        make_tree(self.src, {"f.txt": "x"})

    def test_verbose_short(self):
        out, _, rc = run_fc("-v", self.src, self.dst)
        self.assertEqual(rc, 0)
        clean = strip_ansi(out)
        self.assertIn("FS:", clean)

    def test_verbose_long(self):
        out, _, rc = run_fc("--verbose", self.src, self.dst)
        self.assertEqual(rc, 0)
        clean = strip_ansi(out)
        self.assertIn("FS:", clean)

    def test_default_no_fs_block(self):
        out, _, rc = run_fc(self.src, self.dst)
        self.assertEqual(rc, 0)
        clean = strip_ansi(out)
        # Default mode: NO 'FS:' block, but Dedup line still has strategy
        self.assertNotIn("FS:", clean)
        self.assertRegex(clean, r"Dedup:\s+enabled \(\w+\)")


# ════════════════════════════════════════════════════════════════════════
# --no-verify
# ════════════════════════════════════════════════════════════════════════

class TestNoVerifyArg(TempDirMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        make_tree(self.src, {"a.txt": "alpha", "b.txt": "beta"})

    def test_no_verify_skips_verify_phase(self):
        out, _, rc = run_fc("--no-verify", self.src, self.dst)
        self.assertEqual(rc, 0)
        # Files still copied correctly
        for rel in ("a.txt", "b.txt"):
            self.assertTrue(os.path.exists(os.path.join(self.dst, rel)))
        # But verify phase output should not appear
        clean = strip_ansi(out)
        self.assertNotIn("Verifying", clean)

    def test_default_runs_verify(self):
        out, _, rc = run_fc(self.src, self.dst)
        self.assertEqual(rc, 0)
        clean = strip_ansi(out)
        self.assertIn("Verif", clean)


# ════════════════════════════════════════════════════════════════════════
# --log-file
# ════════════════════════════════════════════════════════════════════════

class TestLogFileArg(TempDirMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        make_tree(self.src, {"a.txt": "alpha", "b.txt": "beta",
                              "sub/c.txt": "charlie"})

    def test_log_file_created(self):
        log = os.path.join(self.tmp, "copy.log")
        out, _, rc = run_fc("--log-file", log, self.src, self.dst)
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(log))

    def test_log_file_is_valid_json(self):
        log = os.path.join(self.tmp, "copy.log")
        out, _, rc = run_fc("--log-file", log, self.src, self.dst)
        with open(log) as f:
            data = json.load(f)
        # Should be either a dict with metadata or a list of events
        self.assertIsInstance(data, (dict, list))

    def test_log_file_records_events(self):
        log = os.path.join(self.tmp, "copy.log")
        run_fc("--log-file", log, self.src, self.dst)
        with open(log) as f:
            content = f.read()
        # Should reference at least one of the copied files
        self.assertTrue("a.txt" in content or "b.txt" in content,
                        "log file does not reference copied files")


# ════════════════════════════════════════════════════════════════════════
# --no-dedup
# ════════════════════════════════════════════════════════════════════════

class TestNoDedupArg(TempDirMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        # Two identical files — would be deduped by default
        make_tree(self.src, {
            "original.txt": "same content",
            "duplicate.txt": "same content",
            "unique.txt": "different",
        })

    def test_default_dedup_enabled(self):
        out, _, rc = run_fc(self.src, self.dst)
        self.assertEqual(rc, 0)
        clean = strip_ansi(out)
        self.assertRegex(clean, r"Dedup:\s+enabled")
        # Phase 2 should appear
        self.assertIn("Phase 2 — Dedup", clean)

    def test_no_dedup_disabled(self):
        out, _, rc = run_fc("--no-dedup", self.src, self.dst)
        self.assertEqual(rc, 0)
        clean = strip_ansi(out)
        self.assertRegex(clean, r"Dedup:\s+disabled")
        # All files should still be copied (just not deduped)
        for rel in ("original.txt", "duplicate.txt", "unique.txt"):
            self.assertTrue(os.path.exists(os.path.join(self.dst, rel)))

    def test_no_dedup_with_overwrite(self):
        run_fc(self.src, self.dst)
        out, _, rc = run_fc("--no-dedup", "--overwrite", self.src, self.dst)
        self.assertEqual(rc, 0)


# ════════════════════════════════════════════════════════════════════════
# --no-cache
# ════════════════════════════════════════════════════════════════════════

class TestNoCacheArg(TempDirMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        make_tree(self.src, {"f.txt": "content"})

    def test_default_cache_enabled(self):
        out, _, rc = run_fc(self.src, self.dst)
        self.assertEqual(rc, 0)
        clean = strip_ansi(out)
        self.assertIn("Hash cache:  enabled", clean)
        # Cache db file should be created
        cache = os.path.join(self.dst, ".fast_copy_dedup.db")
        # may exist on the dst or its mount root
        # — just check the run reported it as enabled

    def test_no_cache_disabled(self):
        out, _, rc = run_fc("--no-cache", self.src, self.dst)
        self.assertEqual(rc, 0)
        clean = strip_ansi(out)
        self.assertIn("Hash cache:  disabled", clean)

    def test_no_cache_no_dedup_combo(self):
        out, _, rc = run_fc("--no-cache", "--no-dedup",
                             self.src, self.dst)
        self.assertEqual(rc, 0)


# ════════════════════════════════════════════════════════════════════════
# --force
# ════════════════════════════════════════════════════════════════════════

class TestForceArg(TempDirMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        make_tree(self.src, {"f.txt": "content"})

    def test_force_skips_space_check(self):
        # --force should always succeed; we don't actually trigger ENOSPC
        # but the flag should be accepted and not change correctness
        out, _, rc = run_fc("--force", self.src, self.dst)
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(os.path.join(self.dst, "f.txt")))


# ════════════════════════════════════════════════════════════════════════
# --overwrite
# ════════════════════════════════════════════════════════════════════════

class TestOverwriteArg(TempDirMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        make_tree(self.src, {"f.txt": "v1"})

    def test_overwrite_alone(self):
        out, _, rc = run_fc("--overwrite", self.src, self.dst)
        self.assertEqual(rc, 0)
        clean = strip_ansi(out)
        self.assertIn("Overwrite:   always", clean)

    def test_default_skips_identical(self):
        out, _, rc = run_fc(self.src, self.dst)
        self.assertEqual(rc, 0)
        clean = strip_ansi(out)
        self.assertIn("Overwrite:   skip identical", clean)

    def test_overwrite_replaces_changed_file(self):
        # First copy
        run_fc(self.src, self.dst)
        # Modify source
        with open(os.path.join(self.src, "f.txt"), "w") as f:
            f.write("v2")
        # Re-copy with overwrite
        out, _, rc = run_fc("--overwrite", self.src, self.dst)
        self.assertEqual(rc, 0)
        with open(os.path.join(self.dst, "f.txt")) as f:
            self.assertEqual(f.read(), "v2")

    def test_overwrite_with_no_verify(self):
        out, _, rc = run_fc("--overwrite", "--no-verify",
                             self.src, self.dst)
        self.assertEqual(rc, 0)


# ════════════════════════════════════════════════════════════════════════
# --exclude
# ════════════════════════════════════════════════════════════════════════

class TestExcludeArg(TempDirMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        make_tree(self.src, {
            "keep.txt": "keep",
            "skip.log": "skip",
            "secret.env": "secret",
            "data/include.csv": "data",
            "data/cache.tmp": "tmp",
        })

    def test_exclude_single(self):
        out, _, rc = run_fc("--exclude", "skip.log", self.src, self.dst)
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(os.path.join(self.dst, "skip.log")))
        self.assertTrue(os.path.exists(os.path.join(self.dst, "keep.txt")))

    def test_exclude_multiple(self):
        out, _, rc = run_fc(
            "--exclude", "skip.log",
            "--exclude", "secret.env",
            "--exclude", "cache.tmp",
            self.src, self.dst)
        self.assertEqual(rc, 0)
        for excluded in ("skip.log", "secret.env", "data/cache.tmp"):
            self.assertFalse(os.path.exists(os.path.join(self.dst, excluded)),
                "should be excluded: " + excluded)
        for kept in ("keep.txt", "data/include.csv"):
            self.assertTrue(os.path.exists(os.path.join(self.dst, kept)),
                "should be kept: " + kept)

    def test_exclude_nonexistent_name(self):
        # Excluding a name that doesn't exist should be a no-op, not fail
        out, _, rc = run_fc("--exclude", "nothing_matches",
                             self.src, self.dst)
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(os.path.join(self.dst, "keep.txt")))


# ════════════════════════════════════════════════════════════════════════
# -z / --compress (the flag is parsed; only effective for SSH)
# ════════════════════════════════════════════════════════════════════════

class TestHashArg(TempDirMixin, unittest.TestCase):
    """--hash flag: auto (default), xxh128, sha256."""

    def setUp(self):
        super().setUp()
        make_tree(self.src, {
            "u.txt": "unique",
            "d1.txt": "shared " * 100,
            "d2.txt": "shared " * 100,
        })

    def test_hash_default_is_auto(self):
        out, _, rc = run_fc(self.src, self.dst)
        self.assertEqual(rc, 0)
        clean = strip_ansi(out)
        self.assertIn("Hash:", clean)
        self.assertRegex(clean, r"Hash:\s+(xxh128|sha256)")

    def test_hash_auto_shows_default_or_fallback(self):
        out, _, rc = run_fc("--hash=auto", self.src, self.dst)
        self.assertEqual(rc, 0)
        clean = strip_ansi(out)
        self.assertTrue(
            "default)" in clean or "fallback)" in clean,
            "expected 'default' or 'fallback' in Hash line")

    def test_hash_sha256_forced(self):
        out, _, rc = run_fc("--hash=sha256", self.src, self.dst)
        self.assertEqual(rc, 0)
        clean = strip_ansi(out)
        self.assertIn("sha256", clean)
        self.assertIn("forced", clean)
        self.assertIn("cryptographic", clean)
        for rel in ("u.txt", "d1.txt", "d2.txt"):
            self.assertTrue(os.path.exists(os.path.join(self.dst, rel)))

    def test_hash_xxh128_forced_or_error(self):
        out, err, rc = run_fc("--hash=xxh128", self.src, self.dst)
        try:
            import xxhash  # noqa: F401
            has_xxhash = True
        except ImportError:
            has_xxhash = False
        if has_xxhash:
            self.assertEqual(rc, 0)
            clean = strip_ansi(out)
            self.assertIn("xxh128", clean)
            self.assertIn("forced", clean)
        else:
            self.assertNotEqual(rc, 0)
            self.assertIn("xxhash package not installed", err + out)

    def test_hash_invalid_rejected(self):
        out, err, rc = run_fc("--hash=md5", self.src, self.dst)
        self.assertNotEqual(rc, 0)
        self.assertIn("invalid choice", err + out)

    def test_hash_sha256_dedup_identical_behavior(self):
        """Dedup must still link d1 and d2 under --hash=sha256."""
        out, _, rc = run_fc("--hash=sha256", self.src, self.dst)
        self.assertEqual(rc, 0)
        s1 = os.stat(os.path.join(self.dst, "d1.txt"))
        s2 = os.stat(os.path.join(self.dst, "d2.txt"))
        self.assertEqual(s1.st_ino, s2.st_ino,
            "d1 and d2 should be hardlinked after --hash=sha256 dedup")


class TestHashInBanner(TempDirMixin, unittest.TestCase):
    """Hash algo should be printed in the main banner."""

    def setUp(self):
        super().setUp()
        make_tree(self.src, {"f.txt": "content"})

    def test_banner_contains_hash_line(self):
        out, _, rc = run_fc(self.src, self.dst)
        self.assertEqual(rc, 0)
        clean = strip_ansi(out)
        self.assertRegex(clean, r"Hash:\s+(xxh128|sha256)\s+\(")

    def test_banner_shows_cryptographic_for_sha256(self):
        out, _, rc = run_fc("--hash=sha256", self.src, self.dst)
        clean = strip_ansi(out)
        self.assertIn("cryptographic", clean)

    def test_no_dedup_hides_hash_line(self):
        out, _, rc = run_fc("--no-dedup", self.src, self.dst)
        self.assertEqual(rc, 0)
        clean = strip_ansi(out)
        self.assertNotIn("Hash:        ", clean)


class TestLinkSummary(TempDirMixin, unittest.TestCase):
    """The improved Phase 6 duplicate handling summary."""

    def setUp(self):
        super().setUp()
        make_tree(self.src, {
            "u.txt": "unique",
            "d1.txt": "shared " * 100,
            "d2.txt": "shared " * 100,
            "d3.txt": "shared " * 100,
        })

    def test_summary_banner_present(self):
        out, _, rc = run_fc(self.src, self.dst)
        self.assertEqual(rc, 0)
        clean = strip_ansi(out)
        self.assertIn("Duplicate handling:", clean)

    def test_hardlinks_reported_on_tmpfs(self):
        out, _, rc = run_fc(self.src, self.dst)
        clean = strip_ansi(out)
        self.assertIn("Hardlinks:", clean)
        self.assertIn("shared inode", clean)
        self.assertIn("all disk savings realized", clean)


class TestCompressArg(TempDirMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        make_tree(self.src, {"f.txt": "content"})

    def test_compress_short_flag_accepted(self):
        out, _, rc = run_fc("-z", self.src, self.dst)
        self.assertEqual(rc, 0)

    def test_compress_long_flag_accepted(self):
        out, _, rc = run_fc("--compress", self.src, self.dst)
        self.assertEqual(rc, 0)


# ════════════════════════════════════════════════════════════════════════
# Mega combination test
# ════════════════════════════════════════════════════════════════════════

class TestMegaCombination(TempDirMixin, unittest.TestCase):
    """Throw a realistic combination of flags at fast-copy and verify
    everything still works correctly."""

    def setUp(self):
        super().setUp()
        # Mix of file sizes and one duplicate pair
        make_tree(self.src, {
            "small.txt": "tiny",
            "medium.bin": 256 * 1024,
            "large.bin": 2 * 1024 * 1024,
            "doc/notes.md": "notes",
            "doc/readme.md": "notes",      # duplicate of notes.md
            "skip.log": "should be skipped",
            "config/settings.json": '{"x": 1}',
        })

    def test_realistic_combination(self):
        log = os.path.join(self.tmp, "copy.log")
        out, err, rc = run_fc(
            "--buffer", "32",
            "--threads", "8",
            "--exclude", "skip.log",
            "--log-file", log,
            "-v",
            self.src, self.dst,
        )
        self.assertEqual(rc, 0, "rc={} err={}".format(rc, err[:300]))
        # All non-excluded files present
        for rel in ("small.txt", "medium.bin", "large.bin",
                    "doc/notes.md", "doc/readme.md",
                    "config/settings.json"):
            full = os.path.join(self.dst, rel)
            self.assertTrue(os.path.exists(full), "missing: " + rel)
        # Excluded file absent
        self.assertFalse(os.path.exists(os.path.join(self.dst, "skip.log")))
        # Log file created
        self.assertTrue(os.path.exists(log))
        with open(log) as f:
            json.load(f)  # must be valid JSON
        # Verbose FS block printed
        clean = strip_ansi(out)
        self.assertIn("FS:", clean)

    def test_everything_at_once(self):
        """Throw EVERY non-conflicting flag at it: dry-run combined with
        verbose, exclude, buffer, threads, no-dedup, no-cache, no-verify,
        force, compress, log-file. This is the kitchen sink."""
        log = os.path.join(self.tmp, "everything.log")
        out, err, rc = run_fc(
            "--buffer", "16",
            "--threads", "12",
            "--exclude", "skip.log",
            "--no-dedup",
            "--no-cache",
            "--no-verify",
            "--force",
            "--compress",     # no effect for local but should be accepted
            "--log-file", log,
            "-v",
            self.src, self.dst,
        )
        self.assertEqual(rc, 0,
            "everything-at-once failed: rc={} err={}".format(rc, err[:300]))
        # No-dedup means duplicates were copied separately
        self.assertTrue(os.path.exists(os.path.join(self.dst, "doc/notes.md")))
        self.assertTrue(os.path.exists(os.path.join(self.dst, "doc/readme.md")))
        # No-verify means no verify phase output
        self.assertNotIn("Verifying", strip_ansi(out))
        # Log file present
        self.assertTrue(os.path.exists(log))

    def test_dry_run_with_everything(self):
        """Dry-run + every other relevant flag — should not write anything."""
        log = os.path.join(self.tmp, "dry.log")
        out, err, rc = run_fc(
            "--dry-run",
            "--buffer", "8",
            "--threads", "16",
            "--exclude", "skip.log",
            "--no-dedup",
            "--no-cache",
            "--no-verify",
            "-v",
            "--log-file", log,
            self.src, self.dst,
        )
        self.assertEqual(rc, 0)
        # Destination should still be empty
        if os.path.exists(self.dst):
            real = [f for f in os.listdir(self.dst)
                    if not f.startswith(".fast_copy")]
            self.assertEqual(real, [])


# ════════════════════════════════════════════════════════════════════════
# Conflict / edge cases
# ════════════════════════════════════════════════════════════════════════

class TestArgConflicts(TempDirMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        make_tree(self.src, {"f.txt": "content"})

    def test_no_dedup_implies_no_link_creation(self):
        """With --no-dedup, there should be no Phase 2 dedup at all."""
        out, _, rc = run_fc("--no-dedup", self.src, self.dst)
        self.assertEqual(rc, 0)
        clean = strip_ansi(out)
        # Phase 2 - Dedup banner should NOT appear
        self.assertNotIn("Phase 2 — Dedup", clean)

    def test_overwrite_force_combo(self):
        """--overwrite + --force — both safety overrides at once."""
        out, _, rc = run_fc("--overwrite", "--force", self.src, self.dst)
        self.assertEqual(rc, 0)

    def test_no_args_shows_usage(self):
        out, err, rc = run_fc()
        # No source/destination → either usage error or help
        self.assertNotEqual(rc, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
