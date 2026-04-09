#!/usr/bin/env python3
"""Tests for fs_detect.py — FS detection and capability probing."""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fs_detect


# ════════════════════════════════════════════════════════════════════════════
# FS type detection
# ════════════════════════════════════════════════════════════════════════════

class TestFSTypeDetection(unittest.TestCase):
    """FS type detection on the current platform."""

    def test_detect_current_dir(self):
        fs_type, method = fs_detect.detect_fs_type(".")
        self.assertIsInstance(fs_type, str)
        self.assertIsInstance(method, str)
        # On the test machine, we should get something other than "unknown"
        self.assertNotEqual(fs_type, "")

    def test_detect_root(self):
        fs_type, method = fs_detect.detect_fs_type("/")
        self.assertIsInstance(fs_type, str)
        # The root FS should always be detectable on Linux/macOS
        if sys.platform in ("linux", "darwin"):
            self.assertNotEqual(fs_type, "unknown")

    def test_detect_tmpdir(self):
        with tempfile.TemporaryDirectory() as td:
            fs_type, method = fs_detect.detect_fs_type(td)
            self.assertIsInstance(fs_type, str)

    def test_detect_nonexistent_walks_up(self):
        # Should walk up to find an existing parent
        fs_type, method = fs_detect.detect_fs_type("/this/does/not/exist/yet")
        self.assertIsInstance(fs_type, str)
        # Walking up from /this/does/... eventually hits /
        self.assertNotEqual(fs_type, "")

    def test_method_name_matches_platform(self):
        _, method = fs_detect.detect_fs_type(".")
        if sys.platform == "linux":
            self.assertEqual(method, "linux_mountinfo")
        elif sys.platform == "darwin":
            self.assertEqual(method, "macos_statfs")
        elif sys.platform == "win32":
            self.assertEqual(method, "windows_GetVolumeInformation")


# ════════════════════════════════════════════════════════════════════════════
# Individual capability probes
# ════════════════════════════════════════════════════════════════════════════

class TestCapabilityProbes(unittest.TestCase):
    """Individual probe functions."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="fs_probe_test_")
        self.probe_dir = fs_detect._make_probe_dir(self.tmpdir)

    def tearDown(self):
        fs_detect._cleanup_probe_dir(self.probe_dir)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_probe_hardlink_returns_bool(self):
        result = fs_detect.probe_hardlink(self.probe_dir)
        self.assertIsInstance(result, bool)
        # /tmp on Linux is usually tmpfs which supports hardlinks
        if sys.platform == "linux":
            self.assertTrue(result, "Linux tmpfs should support hardlinks")

    def test_probe_symlink_returns_bool(self):
        result = fs_detect.probe_symlink(self.probe_dir)
        self.assertIsInstance(result, bool)
        if sys.platform in ("linux", "darwin"):
            self.assertTrue(result, "Unix tmpfs should support symlinks")

    def test_probe_reflink_returns_bool(self):
        result = fs_detect.probe_reflink(self.probe_dir)
        self.assertIsInstance(result, bool)
        # tmpfs does NOT support FICLONE, so this should be False on Linux

    def test_probe_case_sensitivity_returns_bool(self):
        result = fs_detect.probe_case_sensitivity(self.probe_dir)
        self.assertIsInstance(result, bool)
        # Linux is case sensitive
        if sys.platform == "linux":
            self.assertTrue(result)


class TestProbeCleanup(unittest.TestCase):
    """Probes must clean up after themselves."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="fs_cleanup_test_")
        self.probe_dir = fs_detect._make_probe_dir(self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_hardlink_probe_cleans_up(self):
        fs_detect.probe_hardlink(self.probe_dir)
        self.assertEqual(os.listdir(self.probe_dir), [])

    def test_symlink_probe_cleans_up(self):
        fs_detect.probe_symlink(self.probe_dir)
        self.assertEqual(os.listdir(self.probe_dir), [])

    def test_reflink_probe_cleans_up(self):
        fs_detect.probe_reflink(self.probe_dir)
        self.assertEqual(os.listdir(self.probe_dir), [])

    def test_case_probe_cleans_up(self):
        fs_detect.probe_case_sensitivity(self.probe_dir)
        self.assertEqual(os.listdir(self.probe_dir), [])

    def test_all_probes_cleanup(self):
        fs_detect.probe_hardlink(self.probe_dir)
        fs_detect.probe_symlink(self.probe_dir)
        fs_detect.probe_reflink(self.probe_dir)
        fs_detect.probe_case_sensitivity(self.probe_dir)
        leftovers = os.listdir(self.probe_dir)
        self.assertEqual(leftovers, [],
                         "Probes left files behind: {}".format(leftovers))


# ════════════════════════════════════════════════════════════════════════════
# detect_capabilities (the high-level entry point)
# ════════════════════════════════════════════════════════════════════════════

class TestDetectCapabilities(unittest.TestCase):
    """High-level detection."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="fs_caps_test_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_returns_fsinfo(self):
        info = fs_detect.detect_capabilities(self.tmpdir)
        self.assertIsInstance(info, fs_detect.FSInfo)

    def test_strategy_in_valid_set(self):
        info = fs_detect.detect_capabilities(self.tmpdir)
        self.assertIn(info.strategy,
                      ("reflink", "hardlink", "symlink", "none"))

    def test_metrics_populated(self):
        info = fs_detect.detect_capabilities(self.tmpdir)
        self.assertGreaterEqual(info.detection_ms, 0)
        self.assertGreaterEqual(info.probe_ms, 0)
        self.assertIsInstance(info.probes_run, list)
        self.assertIsInstance(info.probe_timings, dict)
        self.assertIsInstance(info.method, str)

    def test_force_probe_runs_all_probes(self):
        info = fs_detect.detect_capabilities(self.tmpdir, force_probe=True)
        for name in ("hardlink", "symlink", "reflink", "case_sensitivity"):
            self.assertIn(name, info.probes_run,
                          "Force probe should run {}".format(name))
        self.assertFalse(info.from_table)

    def test_no_probe_dir_left_behind(self):
        fs_detect.detect_capabilities(self.tmpdir)
        leftovers = [e for e in os.listdir(self.tmpdir)
                     if e.startswith(".fast_copy_probe")]
        self.assertEqual(leftovers, [],
                         "Probe directory not cleaned up: {}".format(leftovers))

    def test_force_probe_no_dir_left(self):
        fs_detect.detect_capabilities(self.tmpdir, force_probe=True)
        leftovers = [e for e in os.listdir(self.tmpdir)
                     if e.startswith(".fast_copy_probe")]
        self.assertEqual(leftovers, [])

    def test_nonexistent_destination(self):
        # Walks up to find existing parent
        nonexistent = os.path.join(self.tmpdir, "doesnt", "exist", "yet")
        info = fs_detect.detect_capabilities(nonexistent)
        self.assertIsInstance(info, fs_detect.FSInfo)

    def test_capabilities_are_namedtuple(self):
        info = fs_detect.detect_capabilities(self.tmpdir)
        caps = info.capabilities
        self.assertIsInstance(caps, fs_detect.FSCapabilities)
        self.assertIsInstance(caps.hardlink, bool)
        self.assertIsInstance(caps.symlink, bool)
        self.assertIsInstance(caps.reflink, bool)
        self.assertIsInstance(caps.case_sensitive, bool)

    def test_known_fs_skips_probes(self):
        """For known FS types in the table, only case_sensitivity is probed."""
        info = fs_detect.detect_capabilities(self.tmpdir)
        if info.fs_type.lower() in ("ext4", "ext3", "ext2", "tmpfs", "btrfs",
                                     "apfs", "vfat", "exfat", "f2fs"):
            # Should be from table; only case_sensitivity probe
            self.assertTrue(info.from_table,
                            "FS {} should use the table".format(info.fs_type))
            self.assertEqual(info.probes_run, ["case_sensitivity"])


# ════════════════════════════════════════════════════════════════════════════
# Strategy selection logic
# ════════════════════════════════════════════════════════════════════════════

class TestStrategySelection(unittest.TestCase):
    """select_dedup_strategy with mocked capabilities."""

    def _caps(self, hl=False, sl=False, rl=False, cs=True):
        return fs_detect.FSCapabilities(
            hardlink=hl, symlink=sl, reflink=rl, case_sensitive=cs)

    def test_reflink_wins_when_available(self):
        caps = self._caps(hl=True, sl=True, rl=True)
        self.assertEqual(fs_detect.select_dedup_strategy(caps), "reflink")

    def test_hardlink_when_no_reflink(self):
        caps = self._caps(hl=True, sl=True, rl=False)
        self.assertEqual(fs_detect.select_dedup_strategy(caps), "hardlink")

    def test_symlink_when_no_hardlink(self):
        caps = self._caps(hl=False, sl=True, rl=False)
        self.assertEqual(fs_detect.select_dedup_strategy(caps), "symlink")

    def test_none_when_no_links(self):
        caps = self._caps(hl=False, sl=False, rl=False)
        self.assertEqual(fs_detect.select_dedup_strategy(caps), "none")

    def test_reflink_only_no_hardlink(self):
        # Edge case: filesystem that supports reflinks but not hardlinks
        caps = self._caps(hl=False, sl=False, rl=True)
        self.assertEqual(fs_detect.select_dedup_strategy(caps), "reflink")


# ════════════════════════════════════════════════════════════════════════════
# FS capability table sanity checks
# ════════════════════════════════════════════════════════════════════════════

class TestFSCapabilityTable(unittest.TestCase):
    """Sanity check the lookup table."""

    def test_fat_no_links(self):
        for fs in ("vfat", "fat32", "exfat", "msdos"):
            entry = fs_detect._FS_CAPABILITY_TABLE[fs]
            hl, sl, rl, _ = entry
            self.assertFalse(hl, "{}: hardlink should be False".format(fs))
            self.assertFalse(sl, "{}: symlink should be False".format(fs))
            self.assertFalse(rl, "{}: reflink should be False".format(fs))

    def test_btrfs_has_reflink(self):
        hl, sl, rl, np = fs_detect._FS_CAPABILITY_TABLE["btrfs"]
        self.assertTrue(hl)
        self.assertTrue(rl)
        self.assertFalse(np)  # known capabilities, no probe needed

    def test_apfs_has_reflink(self):
        hl, sl, rl, np = fs_detect._FS_CAPABILITY_TABLE["apfs"]
        self.assertTrue(rl)

    def test_refs_has_reflink(self):
        hl, sl, rl, np = fs_detect._FS_CAPABILITY_TABLE["refs"]
        self.assertTrue(rl)

    def test_xfs_needs_probe(self):
        _, _, _, np = fs_detect._FS_CAPABILITY_TABLE["xfs"]
        self.assertTrue(np, "XFS reflink is conditional, must probe")

    def test_ntfs_needs_probe(self):
        _, _, _, np = fs_detect._FS_CAPABILITY_TABLE["ntfs"]
        self.assertTrue(np, "NTFS reflink (Dev Drive) requires probing")

    def test_network_fs_need_probe(self):
        for fs in ("nfs", "nfs4", "cifs", "smbfs", "fuseblk", "sshfs"):
            _, _, _, np = fs_detect._FS_CAPABILITY_TABLE[fs]
            self.assertTrue(np, "{} should be probed".format(fs))

    def test_ext4_no_probe(self):
        _, _, _, np = fs_detect._FS_CAPABILITY_TABLE["ext4"]
        self.assertFalse(np, "ext4 capabilities are stable, no probe needed")


# ════════════════════════════════════════════════════════════════════════════
# Format helper
# ════════════════════════════════════════════════════════════════════════════

class TestFormatFSInfo(unittest.TestCase):
    """format_fs_info output."""

    def test_returns_string_with_key_fields(self):
        with tempfile.TemporaryDirectory() as td:
            info = fs_detect.detect_capabilities(td)
            text = fs_detect.format_fs_info(info)
            self.assertIsInstance(text, str)
            for required in ("Path:", "FS type:", "Detection:",
                             "Capabilities:", "Strategy:"):
                self.assertIn(required, text)


# ════════════════════════════════════════════════════════════════════════════
# Edge cases
# ════════════════════════════════════════════════════════════════════════════

class TestSecurityHardening(unittest.TestCase):
    """Verify the hardening fixes from the security audit."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="fs_sec_test_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_walk_up_rejects_null_bytes(self):
        """Null bytes in input must not propagate to syscalls."""
        self.assertIsNone(fs_detect._walk_up_to_existing("/tmp\x00/evil"))

    def test_walk_up_rejects_none(self):
        self.assertIsNone(fs_detect._walk_up_to_existing(None))

    def test_walk_up_rejects_non_string(self):
        self.assertIsNone(fs_detect._walk_up_to_existing(12345))

    def test_walk_up_handles_symlink_loop(self):
        """Symlink loops in the parent chain must not crash."""
        a = os.path.join(self.tmpdir, "loop_a")
        b = os.path.join(self.tmpdir, "loop_b")
        try:
            os.symlink(b, a)
            os.symlink(a, b)
        except OSError:
            self.skipTest("symlinks not supported here")
        # realpath on a symlink loop may raise — _walk_up_to_existing must
        # catch and return None instead of propagating
        result = fs_detect._walk_up_to_existing(a)
        # Either None or a valid existing dir, but never a crash
        self.assertTrue(result is None or os.path.isdir(result))

    def test_make_probe_dir_uses_high_entropy(self):
        """Probe dir name must include 16 bytes (32 hex chars) of randomness."""
        d = fs_detect._make_probe_dir(self.tmpdir)
        try:
            name = os.path.basename(d)
            # Format: .fast_copy_probe_<pid>_<32 hex chars>
            parts = name.split("_")
            self.assertEqual(parts[-1].__len__(), 32,
                             "expected 16-byte hex suffix, got: " + parts[-1])
        finally:
            fs_detect._cleanup_probe_dir(d)

    def test_make_probe_dir_refuses_existing_path(self):
        """If a file already exists at the probe path, mkdir should fail."""
        # This is hard to test directly because the random name is unguessable.
        # But we can monkeypatch os.urandom to a known value and pre-create.
        import unittest.mock as mock
        # Pre-create a file with a name we'll force to be picked
        fixed_random = b"\x00" * 16
        target_name = ".fast_copy_probe_{}_{}".format(
            os.getpid(), fixed_random.hex())
        target_path = os.path.join(self.tmpdir, target_name)
        with open(target_path, "w") as f:
            f.write("blocker")
        with mock.patch("os.urandom", return_value=fixed_random):
            with self.assertRaises(OSError):
                fs_detect._make_probe_dir(self.tmpdir)
        os.unlink(target_path)

    def test_make_probe_dir_mode_is_owner_only(self):
        """Probe dir should be created with mode 0o700."""
        d = fs_detect._make_probe_dir(self.tmpdir)
        try:
            import stat as _stat
            mode = _stat.S_IMODE(os.stat(d).st_mode)
            # Group/other bits should be zero
            self.assertEqual(mode & 0o077, 0,
                             "probe dir is group/world accessible: oct {}".format(
                                 oct(mode)))
        finally:
            fs_detect._cleanup_probe_dir(d)

    def test_cleanup_handles_nested_subdirs(self):
        """Cleanup must handle malicious nested content."""
        d = fs_detect._make_probe_dir(self.tmpdir)
        # Inject nested content (simulating an attacker writing during probe)
        nested = os.path.join(d, "evil_subdir")
        os.makedirs(nested)
        with open(os.path.join(nested, "file"), "w") as f:
            f.write("bad")
        # Cleanup should still succeed
        fs_detect._cleanup_probe_dir(d)
        self.assertFalse(os.path.exists(d),
                         "cleanup failed to remove probe dir with nested content")

    def test_cleanup_does_not_follow_symlinks(self):
        """A symlink inside probe_dir must not cause deletion of the target."""
        d = fs_detect._make_probe_dir(self.tmpdir)
        # Create a "victim" file outside the probe dir
        victim = os.path.join(self.tmpdir, "victim.txt")
        with open(victim, "w") as f:
            f.write("important")
        # Create a symlink inside probe_dir pointing to victim
        link = os.path.join(d, "link_to_victim")
        try:
            os.symlink(victim, link)
        except OSError:
            self.skipTest("symlinks not supported")
        fs_detect._cleanup_probe_dir(d)
        # The victim file outside must STILL exist
        self.assertTrue(os.path.exists(victim),
                        "cleanup followed a symlink and deleted external file!")
        with open(victim) as f:
            self.assertEqual(f.read(), "important")

    def test_unescape_mountinfo(self):
        """Mountinfo escape decoder handles all four sequences."""
        self.assertEqual(fs_detect._unescape_mountinfo("/path"), "/path")
        self.assertEqual(fs_detect._unescape_mountinfo("/path\\040with\\040spaces"),
                         "/path with spaces")
        self.assertEqual(fs_detect._unescape_mountinfo("tab\\011here"),
                         "tab\there")
        self.assertEqual(fs_detect._unescape_mountinfo("back\\134slash"),
                         "back\\slash")

    def test_detect_capabilities_with_null_bytes(self):
        """Top-level detect must reject null-byte paths gracefully."""
        info = fs_detect.detect_capabilities("/tmp\x00/evil")
        # Should not crash; returns some FSInfo (probably from fallback)
        self.assertIsInstance(info, fs_detect.FSInfo)


class TestDefaultCaseSensitivity(unittest.TestCase):
    """The fallback case-sensitivity helper used when probing isn't possible."""

    def test_case_insensitive_filesystems(self):
        for fs in ("vfat", "fat32", "exfat", "msdos", "ntfs", "ntfs3",
                   "hfs", "hfsplus", "apfs"):
            self.assertFalse(
                fs_detect._default_case_sensitive(fs),
                "{} should default to case-insensitive".format(fs)
            )

    def test_case_sensitive_filesystems(self):
        for fs in ("ext4", "btrfs", "xfs", "tmpfs", "f2fs", "zfs"):
            self.assertTrue(
                fs_detect._default_case_sensitive(fs),
                "{} should default to case-sensitive".format(fs)
            )

    def test_unknown_fs_defaults_sensitive(self):
        # Unknown FS → assume sensitive (Unix-like default)
        self.assertTrue(fs_detect._default_case_sensitive("unknown"))


class TestEdgeCases(unittest.TestCase):

    def test_empty_path_fs_type(self):
        fs_type, _ = fs_detect.detect_fs_type("")
        self.assertEqual(fs_type, "unknown")

    def test_root_detection(self):
        info = fs_detect.detect_capabilities("/")
        self.assertIsInstance(info, fs_detect.FSInfo)
        # Even read-only or restricted, should not crash

    def test_relative_path(self):
        info = fs_detect.detect_capabilities(".")
        self.assertIsInstance(info, fs_detect.FSInfo)

    def test_walk_up_function(self):
        existing = fs_detect._walk_up_to_existing("/this/never/exists")
        # Should walk up to "/"
        self.assertEqual(existing, "/")

    def test_walk_up_current_dir(self):
        existing = fs_detect._walk_up_to_existing(".")
        self.assertTrue(os.path.isdir(existing))


# ════════════════════════════════════════════════════════════════════════════
# Performance metrics
# ════════════════════════════════════════════════════════════════════════════

class TestMetrics(unittest.TestCase):
    """Verify detection is fast enough for production use."""

    def test_detection_under_100ms(self):
        """Detection should complete in < 100 ms on a normal FS."""
        with tempfile.TemporaryDirectory() as td:
            info = fs_detect.detect_capabilities(td)
            total = info.detection_ms + info.probe_ms
            self.assertLess(total, 100,
                            "Detection took {:.2f} ms".format(total))

    def test_known_fs_faster_than_force_probe(self):
        """Table-based detection should be faster than force-probing."""
        with tempfile.TemporaryDirectory() as td:
            # Run several times to reduce noise
            table_times = []
            probe_times = []
            for _ in range(5):
                t = fs_detect.detect_capabilities(td)
                table_times.append(t.detection_ms + t.probe_ms)
                p = fs_detect.detect_capabilities(td, force_probe=True)
                probe_times.append(p.detection_ms + p.probe_ms)
            avg_table = sum(table_times) / len(table_times)
            avg_probe = sum(probe_times) / len(probe_times)
            # If the FS is in the table, table version should be faster
            # (it skips hardlink/symlink/reflink probes)
            t_info = fs_detect.detect_capabilities(td)
            if t_info.from_table:
                self.assertLessEqual(avg_table, avg_probe + 1.0,
                    "Table version ({:.2f} ms) should be ≤ probe version "
                    "({:.2f} ms)".format(avg_table, avg_probe))


if __name__ == "__main__":
    unittest.main(verbosity=2)
