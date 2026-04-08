#!/usr/bin/env python3
"""
Comprehensive test suite for fast-copy v2.4.7
Tests all copy modes: L2L, R2L, L2R, R2R
Tests: directories, single files, exclusions, incremental, overwrite,
       dry-run, verify, dedup, glob, empty dirs, large files, special chars
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

FAST_COPY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fast_copy.py")
PYTHON = sys.executable
USER = os.environ.get("USER", "kai")
REMOTE_PREFIX = f"{USER}@localhost:"


def run_fc(*args, expect_fail=False, timeout=120):
    """Run fast_copy.py with given args, return (stdout, stderr, returncode)."""
    cmd = [PYTHON, FAST_COPY] + list(args)
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout
    )
    if not expect_fail and result.returncode != 0:
        print(f"COMMAND: {' '.join(cmd)}", file=sys.stderr)
        print(f"STDOUT:\n{result.stdout}", file=sys.stderr)
        print(f"STDERR:\n{result.stderr}", file=sys.stderr)
    return result.stdout, result.stderr, result.returncode


def file_hash(path):
    """SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def make_test_tree(root, structure=None):
    """
    Create a test directory tree.
    structure is a dict: {relative_path: content_or_size}
      - str content: written as-is
      - int: generates random-ish bytes of that size
      - None: creates an empty directory
    """
    if structure is None:
        structure = {
            "file1.txt": "Hello World\n",
            "file2.bin": 1024,
            "subdir/nested.txt": "Nested content\n",
            "subdir/deep/deeper.log": "Deep file\n",
            "another/data.csv": "a,b,c\n1,2,3\n",
        }
    os.makedirs(root, exist_ok=True)
    created = {}
    for rel, content in structure.items():
        path = os.path.join(root, rel)
        if content is None:
            os.makedirs(path, exist_ok=True)
            created[rel] = None
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if isinstance(content, int):
            # Deterministic pseudo-random bytes based on path
            data = (rel.encode() * ((content // len(rel.encode())) + 1))[:content]
            with open(path, "wb") as f:
                f.write(data)
        else:
            with open(path, "w") as f:
                f.write(content)
        created[rel] = path
    return created


def verify_tree(test_case, src_root, dst_root, expected_rels=None, excluded_rels=None):
    """Verify destination matches source for the given relative paths."""
    if expected_rels is None:
        # Walk source to get all files
        expected_rels = []
        for dirpath, _, filenames in os.walk(src_root):
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                expected_rels.append(os.path.relpath(full, src_root))

    for rel in expected_rels:
        src_file = os.path.join(src_root, rel)
        dst_file = os.path.join(dst_root, rel)
        test_case.assertTrue(
            os.path.exists(dst_file),
            f"Missing in destination: {rel}"
        )
        test_case.assertEqual(
            os.path.getsize(src_file),
            os.path.getsize(dst_file),
            f"Size mismatch for {rel}"
        )
        test_case.assertEqual(
            file_hash(src_file),
            file_hash(dst_file),
            f"Content mismatch for {rel}"
        )

    if excluded_rels:
        for rel in excluded_rels:
            dst_file = os.path.join(dst_root, rel)
            test_case.assertFalse(
                os.path.exists(dst_file),
                f"Excluded file should not exist: {rel}"
            )


class TempDirMixin:
    """Mixin that creates/destroys a temp directory for each test."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="fc_test_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @property
    def src_dir(self):
        return os.path.join(self.tmpdir, "src")

    @property
    def dst_dir(self):
        return os.path.join(self.tmpdir, "dst")


# ═══════════════════════════════════════════════════════════════════
#  LOCAL-TO-LOCAL TESTS
# ═══════════════════════════════════════════════════════════════════

class TestL2LDirectory(TempDirMixin, unittest.TestCase):
    """L2L: Copy a directory tree."""

    def test_basic_directory_copy(self):
        make_test_tree(self.src_dir)
        out, err, rc = run_fc(self.src_dir, self.dst_dir)
        self.assertEqual(rc, 0)
        verify_tree(self, self.src_dir, self.dst_dir)

    def test_nested_directory_structure(self):
        structure = {
            "a/b/c/d/e/file.txt": "deep nesting\n",
            "a/b/c/other.txt": "mid level\n",
            "a/sibling.txt": "top level child\n",
            "root.txt": "at root\n",
        }
        make_test_tree(self.src_dir, structure)
        out, err, rc = run_fc(self.src_dir, self.dst_dir)
        self.assertEqual(rc, 0)
        verify_tree(self, self.src_dir, self.dst_dir,
                    expected_rels=list(structure.keys()))

    def test_many_small_files(self):
        structure = {f"batch/file_{i:04d}.dat": f"content-{i}\n" for i in range(200)}
        make_test_tree(self.src_dir, structure)
        out, err, rc = run_fc(self.src_dir, self.dst_dir)
        self.assertEqual(rc, 0)
        verify_tree(self, self.src_dir, self.dst_dir)

    def test_large_file(self):
        structure = {"bigfile.bin": 5 * 1024 * 1024}  # 5 MB
        make_test_tree(self.src_dir, structure)
        out, err, rc = run_fc(self.src_dir, self.dst_dir)
        self.assertEqual(rc, 0)
        verify_tree(self, self.src_dir, self.dst_dir,
                    expected_rels=["bigfile.bin"])

    def test_mixed_sizes(self):
        """Mix of small files (<1MB tar-bundled) and large files (individual copy)."""
        structure = {
            "tiny.txt": "x",
            "small.dat": 512,
            "medium.bin": 512 * 1024,
            "large.bin": 2 * 1024 * 1024,
        }
        make_test_tree(self.src_dir, structure)
        out, err, rc = run_fc(self.src_dir, self.dst_dir)
        self.assertEqual(rc, 0)
        verify_tree(self, self.src_dir, self.dst_dir,
                    expected_rels=list(structure.keys()))

    def test_empty_files(self):
        structure = {
            "empty1.txt": "",
            "empty2.dat": "",
            "subdir/empty3.log": "",
            "nonempty.txt": "has content\n",
        }
        make_test_tree(self.src_dir, structure)
        out, err, rc = run_fc(self.src_dir, self.dst_dir)
        self.assertEqual(rc, 0)
        for rel in structure:
            dst = os.path.join(self.dst_dir, rel)
            self.assertTrue(os.path.exists(dst), f"Missing: {rel}")


class TestL2LSingleFile(TempDirMixin, unittest.TestCase):
    """L2L: Copy a single file."""

    def test_single_small_file(self):
        make_test_tree(self.src_dir, {"single.txt": "just one file\n"})
        src_file = os.path.join(self.src_dir, "single.txt")
        out, err, rc = run_fc(src_file, self.dst_dir)
        self.assertEqual(rc, 0)
        dst_file = os.path.join(self.dst_dir, "single.txt")
        self.assertTrue(os.path.exists(dst_file))
        self.assertEqual(file_hash(src_file), file_hash(dst_file))

    def test_single_large_file(self):
        make_test_tree(self.src_dir, {"big.bin": 3 * 1024 * 1024})
        src_file = os.path.join(self.src_dir, "big.bin")
        out, err, rc = run_fc(src_file, self.dst_dir)
        self.assertEqual(rc, 0)
        dst_file = os.path.join(self.dst_dir, "big.bin")
        self.assertTrue(os.path.exists(dst_file))
        self.assertEqual(file_hash(src_file), file_hash(dst_file))

    def test_single_empty_file(self):
        make_test_tree(self.src_dir, {"empty.dat": ""})
        src_file = os.path.join(self.src_dir, "empty.dat")
        out, err, rc = run_fc(src_file, self.dst_dir)
        self.assertEqual(rc, 0)
        dst_file = os.path.join(self.dst_dir, "empty.dat")
        self.assertTrue(os.path.exists(dst_file))
        self.assertEqual(os.path.getsize(dst_file), 0)


class TestL2LExclude(TempDirMixin, unittest.TestCase):
    """L2L: Exclusion tests."""

    def test_exclude_single_file(self):
        structure = {
            "keep.txt": "keep me\n",
            "skip.log": "skip me\n",
            "subdir/also_keep.txt": "keep\n",
        }
        make_test_tree(self.src_dir, structure)
        out, err, rc = run_fc(self.src_dir, self.dst_dir, "--exclude", "skip.log")
        self.assertEqual(rc, 0)
        verify_tree(self, self.src_dir, self.dst_dir,
                    expected_rels=["keep.txt", "subdir/also_keep.txt"],
                    excluded_rels=["skip.log"])

    def test_exclude_multiple_files(self):
        structure = {
            "data.csv": "data\n",
            "debug.log": "debug\n",
            "cache.tmp": "cache\n",
            "subdir/debug.log": "nested debug\n",
            "subdir/important.txt": "important\n",
        }
        make_test_tree(self.src_dir, structure)
        out, err, rc = run_fc(
            self.src_dir, self.dst_dir,
            "--exclude", "debug.log",
            "--exclude", "cache.tmp",
        )
        self.assertEqual(rc, 0)
        verify_tree(self, self.src_dir, self.dst_dir,
                    expected_rels=["data.csv", "subdir/important.txt"],
                    excluded_rels=["debug.log", "cache.tmp", "subdir/debug.log"])

    def test_exclude_by_filename(self):
        """Exclude matches by exact filename across all directories."""
        structure = {
            "src/main.py": "code\n",
            "src/cache.pyc": 256,
            "src/sub/cache.pyc": 128,
            "README.md": "readme\n",
        }
        make_test_tree(self.src_dir, structure)
        out, err, rc = run_fc(
            self.src_dir, self.dst_dir,
            "--exclude", "cache.pyc",
        )
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(os.path.join(self.dst_dir, "src/main.py")))
        self.assertTrue(os.path.exists(os.path.join(self.dst_dir, "README.md")))
        # cache.pyc should be excluded everywhere
        self.assertFalse(
            os.path.exists(os.path.join(self.dst_dir, "src/cache.pyc"))
        )
        self.assertFalse(
            os.path.exists(os.path.join(self.dst_dir, "src/sub/cache.pyc"))
        )


class TestL2LIncremental(TempDirMixin, unittest.TestCase):
    """L2L: Incremental / skip unchanged."""

    def test_incremental_skips_unchanged(self):
        structure = {"file1.txt": "original\n", "file2.txt": "also original\n"}
        make_test_tree(self.src_dir, structure)

        # First copy
        out1, _, rc1 = run_fc(self.src_dir, self.dst_dir)
        self.assertEqual(rc1, 0)

        # Second copy — should skip all files
        out2, _, rc2 = run_fc(self.src_dir, self.dst_dir)
        self.assertEqual(rc2, 0)
        self.assertIn("skip", out2.lower())

    def test_incremental_copies_changed(self):
        structure = {"file1.txt": "v1\n", "file2.txt": "unchanged\n"}
        make_test_tree(self.src_dir, structure)

        # First copy
        run_fc(self.src_dir, self.dst_dir)

        # Modify one file
        with open(os.path.join(self.src_dir, "file1.txt"), "w") as f:
            f.write("v2 - modified\n")

        # Second copy
        out, _, rc = run_fc(self.src_dir, self.dst_dir)
        self.assertEqual(rc, 0)

        # Verify modified file was updated
        dst = os.path.join(self.dst_dir, "file1.txt")
        with open(dst) as f:
            self.assertEqual(f.read(), "v2 - modified\n")

    def test_overwrite_forces_all(self):
        structure = {"file.txt": "original\n"}
        make_test_tree(self.src_dir, structure)
        run_fc(self.src_dir, self.dst_dir)

        # With --overwrite, should not mention skipping
        out, _, rc = run_fc(self.src_dir, self.dst_dir, "--overwrite")
        self.assertEqual(rc, 0)
        # File should still be correct
        verify_tree(self, self.src_dir, self.dst_dir, expected_rels=["file.txt"])


class TestL2LDryRun(TempDirMixin, unittest.TestCase):
    """L2L: Dry run."""

    def test_dry_run_no_copy(self):
        make_test_tree(self.src_dir, {"file.txt": "data\n"})
        out, _, rc = run_fc(self.src_dir, self.dst_dir, "--dry-run")
        self.assertEqual(rc, 0)
        # Destination should be empty or not exist
        if os.path.exists(self.dst_dir):
            contents = os.listdir(self.dst_dir)
            # Filter out internal files
            real = [f for f in contents if not f.startswith(".fast_copy")]
            self.assertEqual(real, [], "Dry run should not copy files")


class TestL2LVerify(TempDirMixin, unittest.TestCase):
    """L2L: Verification phase."""

    def test_verify_succeeds(self):
        make_test_tree(self.src_dir, {"verify_me.txt": "check this\n"})
        out, _, rc = run_fc(self.src_dir, self.dst_dir)
        self.assertEqual(rc, 0)
        self.assertIn("erif", out)  # "Verified" or "verify"

    def test_no_verify_flag(self):
        make_test_tree(self.src_dir, {"file.txt": "data\n"})
        out, _, rc = run_fc(self.src_dir, self.dst_dir, "--no-verify")
        self.assertEqual(rc, 0)
        # File should still be copied correctly
        verify_tree(self, self.src_dir, self.dst_dir, expected_rels=["file.txt"])


class TestL2LDedup(TempDirMixin, unittest.TestCase):
    """L2L: Deduplication."""

    def test_dedup_identical_files(self):
        structure = {
            "original.txt": "duplicate content here\n",
            "copy1.txt": "duplicate content here\n",
            "subdir/copy2.txt": "duplicate content here\n",
            "unique.txt": "i am unique\n",
        }
        make_test_tree(self.src_dir, structure)
        out, _, rc = run_fc(self.src_dir, self.dst_dir)
        self.assertEqual(rc, 0)
        # All files should exist and have correct content
        for rel in structure:
            dst = os.path.join(self.dst_dir, rel)
            self.assertTrue(os.path.exists(dst), f"Missing: {rel}")

    def test_no_dedup_flag(self):
        structure = {
            "a.txt": "same\n",
            "b.txt": "same\n",
        }
        make_test_tree(self.src_dir, structure)
        out, _, rc = run_fc(self.src_dir, self.dst_dir, "--no-dedup")
        self.assertEqual(rc, 0)
        verify_tree(self, self.src_dir, self.dst_dir,
                    expected_rels=list(structure.keys()))


class TestL2LGlob(TempDirMixin, unittest.TestCase):
    """L2L: Glob pattern source."""

    def test_glob_pattern(self):
        structure = {
            "report1.csv": "a,b\n1,2\n",
            "report2.csv": "c,d\n3,4\n",
            "notes.txt": "ignore me\n",
            "data.json": '{"x": 1}\n',
        }
        make_test_tree(self.src_dir, structure)
        glob_pat = os.path.join(self.src_dir, "*.csv")
        out, _, rc = run_fc(glob_pat, self.dst_dir)
        self.assertEqual(rc, 0)
        # Only CSV files should be copied
        self.assertTrue(os.path.exists(os.path.join(self.dst_dir, "report1.csv")))
        self.assertTrue(os.path.exists(os.path.join(self.dst_dir, "report2.csv")))
        self.assertFalse(os.path.exists(os.path.join(self.dst_dir, "notes.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.dst_dir, "data.json")))


class TestL2LSpecialChars(TempDirMixin, unittest.TestCase):
    """L2L: Files with special characters in names."""

    def test_spaces_in_names(self):
        structure = {
            "my file.txt": "spaces\n",
            "my dir/another file.dat": "more spaces\n",
        }
        make_test_tree(self.src_dir, structure)
        out, _, rc = run_fc(self.src_dir, self.dst_dir)
        self.assertEqual(rc, 0)
        verify_tree(self, self.src_dir, self.dst_dir,
                    expected_rels=list(structure.keys()))

    def test_unicode_names(self):
        structure = {
            "données.txt": "french\n",
            "日本語/ファイル.txt": "japanese\n",
            "emoji_🎉.txt": "party\n",
        }
        make_test_tree(self.src_dir, structure)
        out, _, rc = run_fc(self.src_dir, self.dst_dir)
        self.assertEqual(rc, 0)
        verify_tree(self, self.src_dir, self.dst_dir,
                    expected_rels=list(structure.keys()))

    def test_special_shell_chars(self):
        structure = {
            "file with 'quotes'.txt": "quotes\n",
            "hash#tag.txt": "hash\n",
            "dollar$var.txt": "dollar\n",
            "paren(1).txt": "paren\n",
            "amp&ersand.txt": "amp\n",
        }
        make_test_tree(self.src_dir, structure)
        out, _, rc = run_fc(self.src_dir, self.dst_dir)
        self.assertEqual(rc, 0)
        verify_tree(self, self.src_dir, self.dst_dir,
                    expected_rels=list(structure.keys()))


class TestL2LLogFile(TempDirMixin, unittest.TestCase):
    """L2L: JSON log file output."""

    def test_log_file_created(self):
        make_test_tree(self.src_dir, {"file.txt": "log test\n"})
        log_path = os.path.join(self.tmpdir, "copy.log")
        out, _, rc = run_fc(self.src_dir, self.dst_dir, "--log-file", log_path)
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(log_path))
        with open(log_path) as f:
            data = json.load(f)
        self.assertIsInstance(data, (dict, list))


class TestL2LFileDestination(TempDirMixin, unittest.TestCase):
    """L2L: Destination is a file path (rename on copy)."""

    def test_dst_as_file_path(self):
        """Copy single file to a specific filename destination."""
        make_test_tree(self.src_dir, {"original.txt": "file content\n"})
        src_file = os.path.join(self.src_dir, "original.txt")
        dst_file = os.path.join(self.dst_dir, "renamed.txt")
        os.makedirs(self.dst_dir, exist_ok=True)
        out, err, rc = run_fc(src_file, dst_file)
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        self.assertTrue(os.path.isfile(dst_file), "Destination file not created")
        self.assertEqual(file_hash(src_file), file_hash(dst_file))

    def test_dst_as_file_path_parent_created(self):
        """Parent directory of destination file doesn't exist yet."""
        make_test_tree(self.src_dir, {"data.bin": 2048})
        src_file = os.path.join(self.src_dir, "data.bin")
        dst_file = os.path.join(self.tmpdir, "newdir", "output.bin")
        out, err, rc = run_fc(src_file, dst_file)
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        self.assertTrue(os.path.isfile(dst_file), "Destination file not created")
        self.assertEqual(file_hash(src_file), file_hash(dst_file))

    def test_dst_as_file_path_overwrite_existing(self):
        """Destination file already exists — should overwrite."""
        make_test_tree(self.src_dir, {"src.txt": "new content\n"})
        src_file = os.path.join(self.src_dir, "src.txt")
        os.makedirs(self.dst_dir, exist_ok=True)
        dst_file = os.path.join(self.dst_dir, "target.txt")
        with open(dst_file, "w") as f:
            f.write("old content\n")
        out, err, rc = run_fc(src_file, dst_file)
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        with open(dst_file) as f:
            self.assertEqual(f.read(), "new content\n")

    def test_dst_dir_with_trailing_slash(self):
        """Trailing slash means treat as directory, not file."""
        make_test_tree(self.src_dir, {"file.txt": "content\n"})
        src_file = os.path.join(self.src_dir, "file.txt")
        dst_path = os.path.join(self.tmpdir, "target_dir") + os.sep
        out, err, rc = run_fc(src_file, dst_path)
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        # Should be placed inside the directory, keeping original name
        self.assertTrue(
            os.path.isfile(os.path.join(self.tmpdir, "target_dir", "file.txt"))
        )


# ═══════════════════════════════════════════════════════════════════
#  REMOTE-TO-LOCAL TESTS
# ═══════════════════════════════════════════════════════════════════

class TestR2LDirectory(TempDirMixin, unittest.TestCase):
    """R2L: Copy directory from remote (localhost) to local."""

    def test_basic_directory(self):
        make_test_tree(self.src_dir)
        remote_src = REMOTE_PREFIX + self.src_dir
        out, err, rc = run_fc(remote_src, self.dst_dir)
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        verify_tree(self, self.src_dir, self.dst_dir)

    def test_nested_structure(self):
        structure = {
            "a/b/c/deep.txt": "deep\n",
            "a/b/mid.txt": "mid\n",
            "top.txt": "top\n",
        }
        make_test_tree(self.src_dir, structure)
        remote_src = REMOTE_PREFIX + self.src_dir
        out, err, rc = run_fc(remote_src, self.dst_dir)
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        verify_tree(self, self.src_dir, self.dst_dir,
                    expected_rels=list(structure.keys()))

    def test_many_files(self):
        structure = {f"dir/f_{i:03d}.txt": f"content {i}\n" for i in range(100)}
        make_test_tree(self.src_dir, structure)
        remote_src = REMOTE_PREFIX + self.src_dir
        out, err, rc = run_fc(remote_src, self.dst_dir)
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        verify_tree(self, self.src_dir, self.dst_dir)

    def test_large_file_remote(self):
        structure = {"big.bin": 3 * 1024 * 1024}
        make_test_tree(self.src_dir, structure)
        remote_src = REMOTE_PREFIX + self.src_dir
        out, err, rc = run_fc(remote_src, self.dst_dir)
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        verify_tree(self, self.src_dir, self.dst_dir,
                    expected_rels=["big.bin"])

    def test_mixed_sizes_remote(self):
        structure = {
            "tiny.txt": "x",
            "small.dat": 512,
            "large.bin": 2 * 1024 * 1024,
        }
        make_test_tree(self.src_dir, structure)
        remote_src = REMOTE_PREFIX + self.src_dir
        out, err, rc = run_fc(remote_src, self.dst_dir)
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        verify_tree(self, self.src_dir, self.dst_dir,
                    expected_rels=list(structure.keys()))


class TestR2LSingleFile(TempDirMixin, unittest.TestCase):
    """R2L: Copy a single file from remote — the v2.4.7 fix."""

    def test_single_file(self):
        """This is the exact bug scenario fixed in v2.4.7."""
        make_test_tree(self.src_dir, {"deploy.tar.gz": 2048})
        src_file = os.path.join(self.src_dir, "deploy.tar.gz")
        remote_src = REMOTE_PREFIX + src_file
        out, err, rc = run_fc(remote_src, self.dst_dir)
        self.assertEqual(rc, 0, f"v2.4.7 fix failed!\nstdout: {out}\nstderr: {err}")
        dst_file = os.path.join(self.dst_dir, "deploy.tar.gz")
        self.assertTrue(os.path.exists(dst_file))
        self.assertEqual(file_hash(src_file), file_hash(dst_file))

    def test_single_small_file(self):
        make_test_tree(self.src_dir, {"note.txt": "hello from remote\n"})
        src_file = os.path.join(self.src_dir, "note.txt")
        remote_src = REMOTE_PREFIX + src_file
        out, err, rc = run_fc(remote_src, self.dst_dir)
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        dst_file = os.path.join(self.dst_dir, "note.txt")
        self.assertTrue(os.path.exists(dst_file))
        self.assertEqual(file_hash(src_file), file_hash(dst_file))

    def test_single_large_file(self):
        make_test_tree(self.src_dir, {"big_remote.bin": 3 * 1024 * 1024})
        src_file = os.path.join(self.src_dir, "big_remote.bin")
        remote_src = REMOTE_PREFIX + src_file
        out, err, rc = run_fc(remote_src, self.dst_dir)
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        dst_file = os.path.join(self.dst_dir, "big_remote.bin")
        self.assertTrue(os.path.exists(dst_file))
        self.assertEqual(file_hash(src_file), file_hash(dst_file))

    def test_single_empty_file(self):
        make_test_tree(self.src_dir, {"empty_remote.dat": ""})
        src_file = os.path.join(self.src_dir, "empty_remote.dat")
        remote_src = REMOTE_PREFIX + src_file
        out, err, rc = run_fc(remote_src, self.dst_dir)
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        dst_file = os.path.join(self.dst_dir, "empty_remote.dat")
        self.assertTrue(os.path.exists(dst_file))
        self.assertEqual(os.path.getsize(dst_file), 0)

    def test_single_file_with_spaces(self):
        make_test_tree(self.src_dir, {"my archive.tar.gz": 1024})
        src_file = os.path.join(self.src_dir, "my archive.tar.gz")
        remote_src = REMOTE_PREFIX + src_file
        out, err, rc = run_fc(remote_src, self.dst_dir)
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        dst_file = os.path.join(self.dst_dir, "my archive.tar.gz")
        self.assertTrue(os.path.exists(dst_file))
        self.assertEqual(file_hash(src_file), file_hash(dst_file))


class TestR2LFileDestination(TempDirMixin, unittest.TestCase):
    """R2L: Destination is a file path (remote source, local file target)."""

    def test_dst_as_file_path(self):
        make_test_tree(self.src_dir, {"data.tar.gz": 2048})
        src_file = os.path.join(self.src_dir, "data.tar.gz")
        remote_src = REMOTE_PREFIX + src_file
        os.makedirs(self.dst_dir, exist_ok=True)
        dst_file = os.path.join(self.dst_dir, "renamed.tar.gz")
        out, err, rc = run_fc(remote_src, dst_file)
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        self.assertTrue(os.path.isfile(dst_file), "Destination file not created")
        self.assertEqual(file_hash(src_file), file_hash(dst_file))

    def test_dst_as_file_overwrite_existing(self):
        make_test_tree(self.src_dir, {"fresh.bin": 1024})
        src_file = os.path.join(self.src_dir, "fresh.bin")
        remote_src = REMOTE_PREFIX + src_file
        os.makedirs(self.dst_dir, exist_ok=True)
        dst_file = os.path.join(self.dst_dir, "target.bin")
        with open(dst_file, "wb") as f:
            f.write(b"old data")
        out, err, rc = run_fc(remote_src, dst_file)
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        self.assertEqual(file_hash(src_file), file_hash(dst_file))


class TestR2LExclude(TempDirMixin, unittest.TestCase):
    """R2L: Exclusion from remote source."""

    def test_exclude_files(self):
        structure = {
            "app.py": "code\n",
            "app.pyc": 128,
            "debug.log": "logs\n",
            "data/input.csv": "data\n",
            "data/cache.tmp": "tmp\n",
        }
        make_test_tree(self.src_dir, structure)
        remote_src = REMOTE_PREFIX + self.src_dir
        out, err, rc = run_fc(
            remote_src, self.dst_dir,
            "--exclude", "debug.log",
            "--exclude", "cache.tmp",
        )
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        self.assertTrue(os.path.exists(os.path.join(self.dst_dir, "app.py")))
        self.assertTrue(os.path.exists(os.path.join(self.dst_dir, "data/input.csv")))
        self.assertFalse(os.path.exists(os.path.join(self.dst_dir, "debug.log")))
        self.assertFalse(os.path.exists(os.path.join(self.dst_dir, "data/cache.tmp")))


class TestR2LIncremental(TempDirMixin, unittest.TestCase):
    """R2L: Incremental copy from remote."""

    def test_incremental_skip(self):
        structure = {"file.txt": "stable content\n"}
        make_test_tree(self.src_dir, structure)
        remote_src = REMOTE_PREFIX + self.src_dir

        # First copy
        out1, err1, rc1 = run_fc(remote_src, self.dst_dir)
        self.assertEqual(rc1, 0, f"First copy failed: {err1}")

        # Second copy — should skip
        out2, err2, rc2 = run_fc(remote_src, self.dst_dir)
        self.assertEqual(rc2, 0, f"Second copy failed: {err2}")
        self.assertIn("skip", out2.lower())

    def test_incremental_detects_change(self):
        structure = {"file.txt": "v1\n", "stable.txt": "no change\n"}
        make_test_tree(self.src_dir, structure)
        remote_src = REMOTE_PREFIX + self.src_dir

        run_fc(remote_src, self.dst_dir)

        # Modify source
        with open(os.path.join(self.src_dir, "file.txt"), "w") as f:
            f.write("v2 modified\n")

        out, err, rc = run_fc(remote_src, self.dst_dir)
        self.assertEqual(rc, 0)

        dst = os.path.join(self.dst_dir, "file.txt")
        with open(dst) as f:
            self.assertEqual(f.read(), "v2 modified\n")


class TestR2LOverwrite(TempDirMixin, unittest.TestCase):
    """R2L: Overwrite mode from remote."""

    def test_overwrite_all(self):
        structure = {"data.bin": 2048}
        make_test_tree(self.src_dir, structure)
        remote_src = REMOTE_PREFIX + self.src_dir

        run_fc(remote_src, self.dst_dir)
        out, err, rc = run_fc(remote_src, self.dst_dir, "--overwrite")
        self.assertEqual(rc, 0, f"Overwrite failed: {err}")
        verify_tree(self, self.src_dir, self.dst_dir, expected_rels=["data.bin"])


class TestR2LDryRun(TempDirMixin, unittest.TestCase):
    """R2L: Dry run from remote."""

    def test_dry_run_no_copy(self):
        make_test_tree(self.src_dir, {"file.txt": "data\n"})
        remote_src = REMOTE_PREFIX + self.src_dir
        out, err, rc = run_fc(remote_src, self.dst_dir, "--dry-run")
        self.assertEqual(rc, 0)
        if os.path.exists(self.dst_dir):
            real = [f for f in os.listdir(self.dst_dir)
                    if not f.startswith(".fast_copy")]
            self.assertEqual(real, [])


# ═══════════════════════════════════════════════════════════════════
#  LOCAL-TO-REMOTE TESTS
# ═══════════════════════════════════════════════════════════════════

class TestL2RDirectory(TempDirMixin, unittest.TestCase):
    """L2R: Copy directory from local to remote (localhost)."""

    def test_basic_directory(self):
        make_test_tree(self.src_dir)
        remote_dst = REMOTE_PREFIX + self.dst_dir
        out, err, rc = run_fc(self.src_dir, remote_dst)
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        verify_tree(self, self.src_dir, self.dst_dir)

    def test_nested_structure(self):
        structure = {
            "a/b/c.txt": "deep\n",
            "a/d.txt": "mid\n",
            "e.txt": "top\n",
        }
        make_test_tree(self.src_dir, structure)
        remote_dst = REMOTE_PREFIX + self.dst_dir
        out, err, rc = run_fc(self.src_dir, remote_dst)
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        verify_tree(self, self.src_dir, self.dst_dir,
                    expected_rels=list(structure.keys()))

    def test_many_files(self):
        structure = {f"batch/f_{i:03d}.dat": f"data-{i}\n" for i in range(100)}
        make_test_tree(self.src_dir, structure)
        remote_dst = REMOTE_PREFIX + self.dst_dir
        out, err, rc = run_fc(self.src_dir, remote_dst)
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        verify_tree(self, self.src_dir, self.dst_dir)

    def test_large_file(self):
        structure = {"big.bin": 3 * 1024 * 1024}
        make_test_tree(self.src_dir, structure)
        remote_dst = REMOTE_PREFIX + self.dst_dir
        out, err, rc = run_fc(self.src_dir, remote_dst)
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        verify_tree(self, self.src_dir, self.dst_dir, expected_rels=["big.bin"])

    def test_mixed_sizes(self):
        structure = {
            "small.txt": "tiny",
            "medium.bin": 512 * 1024,
            "large.bin": 2 * 1024 * 1024,
        }
        make_test_tree(self.src_dir, structure)
        remote_dst = REMOTE_PREFIX + self.dst_dir
        out, err, rc = run_fc(self.src_dir, remote_dst)
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        verify_tree(self, self.src_dir, self.dst_dir,
                    expected_rels=list(structure.keys()))


class TestL2RSingleFile(TempDirMixin, unittest.TestCase):
    """L2R: Copy a single file to remote."""

    def test_single_file(self):
        make_test_tree(self.src_dir, {"upload.dat": 2048})
        src_file = os.path.join(self.src_dir, "upload.dat")
        remote_dst = REMOTE_PREFIX + self.dst_dir
        out, err, rc = run_fc(src_file, remote_dst)
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        dst_file = os.path.join(self.dst_dir, "upload.dat")
        self.assertTrue(os.path.exists(dst_file))
        self.assertEqual(file_hash(src_file), file_hash(dst_file))

    def test_single_large_file(self):
        make_test_tree(self.src_dir, {"big_upload.bin": 3 * 1024 * 1024})
        src_file = os.path.join(self.src_dir, "big_upload.bin")
        remote_dst = REMOTE_PREFIX + self.dst_dir
        out, err, rc = run_fc(src_file, remote_dst)
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        dst_file = os.path.join(self.dst_dir, "big_upload.bin")
        self.assertTrue(os.path.exists(dst_file))
        self.assertEqual(file_hash(src_file), file_hash(dst_file))


class TestL2RFileDestination(TempDirMixin, unittest.TestCase):
    """L2R: Destination is a remote file path."""

    def test_dst_as_file_path(self):
        make_test_tree(self.src_dir, {"upload.dat": 2048})
        src_file = os.path.join(self.src_dir, "upload.dat")
        os.makedirs(self.dst_dir, exist_ok=True)
        dst_file = os.path.join(self.dst_dir, "renamed.dat")
        remote_dst = REMOTE_PREFIX + dst_file
        out, err, rc = run_fc(src_file, remote_dst)
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        self.assertTrue(os.path.isfile(dst_file), "Destination file not created")
        self.assertEqual(file_hash(src_file), file_hash(dst_file))


class TestL2RExclude(TempDirMixin, unittest.TestCase):
    """L2R: Exclusion to remote."""

    def test_exclude_files(self):
        structure = {
            "app.py": "code\n",
            "secret.env": "PASSWORD=123\n",
            "data/main.csv": "data\n",
            "data/debug.log": "debug\n",
        }
        make_test_tree(self.src_dir, structure)
        remote_dst = REMOTE_PREFIX + self.dst_dir
        out, err, rc = run_fc(
            self.src_dir, remote_dst,
            "--exclude", "secret.env",
            "--exclude", "debug.log",
        )
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        self.assertTrue(os.path.exists(os.path.join(self.dst_dir, "app.py")))
        self.assertTrue(os.path.exists(os.path.join(self.dst_dir, "data/main.csv")))
        self.assertFalse(os.path.exists(os.path.join(self.dst_dir, "secret.env")))
        self.assertFalse(os.path.exists(os.path.join(self.dst_dir, "data/debug.log")))


class TestL2RIncremental(TempDirMixin, unittest.TestCase):
    """L2R: Incremental copy to remote."""

    def test_incremental_skip(self):
        structure = {"stable.txt": "no change\n"}
        make_test_tree(self.src_dir, structure)
        remote_dst = REMOTE_PREFIX + self.dst_dir

        run_fc(self.src_dir, remote_dst)
        out, err, rc = run_fc(self.src_dir, remote_dst)
        self.assertEqual(rc, 0)
        self.assertIn("skip", out.lower())

    def test_incremental_detects_change(self):
        make_test_tree(self.src_dir, {"file.txt": "v1\n"})
        remote_dst = REMOTE_PREFIX + self.dst_dir

        run_fc(self.src_dir, remote_dst)

        with open(os.path.join(self.src_dir, "file.txt"), "w") as f:
            f.write("v2\n")

        out, err, rc = run_fc(self.src_dir, remote_dst)
        self.assertEqual(rc, 0)

        with open(os.path.join(self.dst_dir, "file.txt")) as f:
            self.assertEqual(f.read(), "v2\n")


class TestL2ROverwrite(TempDirMixin, unittest.TestCase):
    """L2R: Overwrite to remote."""

    def test_overwrite_all(self):
        structure = {"data.bin": 2048}
        make_test_tree(self.src_dir, structure)
        remote_dst = REMOTE_PREFIX + self.dst_dir

        run_fc(self.src_dir, remote_dst)
        out, err, rc = run_fc(self.src_dir, remote_dst, "--overwrite")
        self.assertEqual(rc, 0)
        verify_tree(self, self.src_dir, self.dst_dir, expected_rels=["data.bin"])


class TestL2RDryRun(TempDirMixin, unittest.TestCase):
    """L2R: Dry run to remote."""

    def test_dry_run_no_copy(self):
        make_test_tree(self.src_dir, {"file.txt": "data\n"})
        remote_dst = REMOTE_PREFIX + self.dst_dir
        out, err, rc = run_fc(self.src_dir, remote_dst, "--dry-run")
        self.assertEqual(rc, 0)
        if os.path.exists(self.dst_dir):
            real = [f for f in os.listdir(self.dst_dir)
                    if not f.startswith(".fast_copy")]
            self.assertEqual(real, [])


# ═══════════════════════════════════════════════════════════════════
#  REMOTE-TO-REMOTE TESTS
# ═══════════════════════════════════════════════════════════════════

class TestR2RDirectory(TempDirMixin, unittest.TestCase):
    """R2R: Copy directory between remotes (both localhost)."""

    def test_basic_directory(self):
        make_test_tree(self.src_dir)
        remote_src = REMOTE_PREFIX + self.src_dir
        remote_dst = REMOTE_PREFIX + self.dst_dir
        out, err, rc = run_fc(remote_src, remote_dst)
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        verify_tree(self, self.src_dir, self.dst_dir)

    def test_nested_structure(self):
        structure = {
            "level1/level2/level3/file.txt": "deep\n",
            "level1/level2/mid.txt": "mid\n",
            "level1/top.txt": "top-ish\n",
            "root.txt": "root\n",
        }
        make_test_tree(self.src_dir, structure)
        remote_src = REMOTE_PREFIX + self.src_dir
        remote_dst = REMOTE_PREFIX + self.dst_dir
        out, err, rc = run_fc(remote_src, remote_dst)
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        verify_tree(self, self.src_dir, self.dst_dir,
                    expected_rels=list(structure.keys()))

    def test_many_files(self):
        structure = {f"r2r/f_{i:03d}.txt": f"r2r-{i}\n" for i in range(100)}
        make_test_tree(self.src_dir, structure)
        remote_src = REMOTE_PREFIX + self.src_dir
        remote_dst = REMOTE_PREFIX + self.dst_dir
        out, err, rc = run_fc(remote_src, remote_dst)
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        verify_tree(self, self.src_dir, self.dst_dir)

    def test_large_file(self):
        structure = {"r2r_big.bin": 3 * 1024 * 1024}
        make_test_tree(self.src_dir, structure)
        remote_src = REMOTE_PREFIX + self.src_dir
        remote_dst = REMOTE_PREFIX + self.dst_dir
        out, err, rc = run_fc(remote_src, remote_dst)
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        verify_tree(self, self.src_dir, self.dst_dir, expected_rels=["r2r_big.bin"])

    def test_mixed_sizes(self):
        structure = {
            "tiny.txt": "x",
            "medium.bin": 512 * 1024,
            "large.bin": 2 * 1024 * 1024,
        }
        make_test_tree(self.src_dir, structure)
        remote_src = REMOTE_PREFIX + self.src_dir
        remote_dst = REMOTE_PREFIX + self.dst_dir
        out, err, rc = run_fc(remote_src, remote_dst)
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        verify_tree(self, self.src_dir, self.dst_dir,
                    expected_rels=list(structure.keys()))


class TestR2RSingleFile(TempDirMixin, unittest.TestCase):
    """R2R: Copy a single file between remotes."""

    def test_single_file(self):
        make_test_tree(self.src_dir, {"transfer.dat": 2048})
        src_file = os.path.join(self.src_dir, "transfer.dat")
        remote_src = REMOTE_PREFIX + src_file
        remote_dst = REMOTE_PREFIX + self.dst_dir
        out, err, rc = run_fc(remote_src, remote_dst)
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        dst_file = os.path.join(self.dst_dir, "transfer.dat")
        self.assertTrue(os.path.exists(dst_file))
        self.assertEqual(file_hash(src_file), file_hash(dst_file))

    def test_single_large_file(self):
        make_test_tree(self.src_dir, {"r2r_big_single.bin": 3 * 1024 * 1024})
        src_file = os.path.join(self.src_dir, "r2r_big_single.bin")
        remote_src = REMOTE_PREFIX + src_file
        remote_dst = REMOTE_PREFIX + self.dst_dir
        out, err, rc = run_fc(remote_src, remote_dst)
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        dst_file = os.path.join(self.dst_dir, "r2r_big_single.bin")
        self.assertTrue(os.path.exists(dst_file))
        self.assertEqual(file_hash(src_file), file_hash(dst_file))


class TestR2RFileDestination(TempDirMixin, unittest.TestCase):
    """R2R: Destination is a remote file path."""

    def test_dst_as_file_path(self):
        make_test_tree(self.src_dir, {"transfer.dat": 2048})
        src_file = os.path.join(self.src_dir, "transfer.dat")
        remote_src = REMOTE_PREFIX + src_file
        os.makedirs(self.dst_dir, exist_ok=True)
        dst_file = os.path.join(self.dst_dir, "renamed.dat")
        remote_dst = REMOTE_PREFIX + dst_file
        out, err, rc = run_fc(remote_src, remote_dst)
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        self.assertTrue(os.path.isfile(dst_file), "Destination file not created")
        self.assertEqual(file_hash(src_file), file_hash(dst_file))


class TestR2RExclude(TempDirMixin, unittest.TestCase):
    """R2R: Exclusion between remotes."""

    def test_exclude_files(self):
        structure = {
            "app.py": "code\n",
            "build.log": "build output\n",
            "lib/core.py": "lib\n",
            "lib/cache.tmp": "cache\n",
        }
        make_test_tree(self.src_dir, structure)
        remote_src = REMOTE_PREFIX + self.src_dir
        remote_dst = REMOTE_PREFIX + self.dst_dir
        out, err, rc = run_fc(
            remote_src, remote_dst,
            "--exclude", "build.log",
            "--exclude", "cache.tmp",
        )
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        self.assertTrue(os.path.exists(os.path.join(self.dst_dir, "app.py")))
        self.assertTrue(os.path.exists(os.path.join(self.dst_dir, "lib/core.py")))
        self.assertFalse(os.path.exists(os.path.join(self.dst_dir, "build.log")))
        self.assertFalse(os.path.exists(os.path.join(self.dst_dir, "lib/cache.tmp")))


class TestR2RIncremental(TempDirMixin, unittest.TestCase):
    """R2R: Incremental copy between remotes."""

    def test_incremental_skip(self):
        structure = {"stable.txt": "no change\n"}
        make_test_tree(self.src_dir, structure)
        remote_src = REMOTE_PREFIX + self.src_dir
        remote_dst = REMOTE_PREFIX + self.dst_dir

        run_fc(remote_src, remote_dst)
        out, err, rc = run_fc(remote_src, remote_dst)
        self.assertEqual(rc, 0)
        self.assertIn("skip", out.lower())

    def test_incremental_detects_change(self):
        make_test_tree(self.src_dir, {"file.txt": "v1\n"})
        remote_src = REMOTE_PREFIX + self.src_dir
        remote_dst = REMOTE_PREFIX + self.dst_dir

        run_fc(remote_src, remote_dst)

        with open(os.path.join(self.src_dir, "file.txt"), "w") as f:
            f.write("v2\n")

        out, err, rc = run_fc(remote_src, remote_dst)
        self.assertEqual(rc, 0)

        with open(os.path.join(self.dst_dir, "file.txt")) as f:
            self.assertEqual(f.read(), "v2\n")


class TestR2ROverwrite(TempDirMixin, unittest.TestCase):
    """R2R: Overwrite between remotes."""

    def test_overwrite_all(self):
        structure = {"data.bin": 2048}
        make_test_tree(self.src_dir, structure)
        remote_src = REMOTE_PREFIX + self.src_dir
        remote_dst = REMOTE_PREFIX + self.dst_dir

        run_fc(remote_src, remote_dst)
        out, err, rc = run_fc(remote_src, remote_dst, "--overwrite")
        self.assertEqual(rc, 0)
        verify_tree(self, self.src_dir, self.dst_dir, expected_rels=["data.bin"])


class TestR2RDryRun(TempDirMixin, unittest.TestCase):
    """R2R: Dry run between remotes."""

    def test_dry_run_no_copy(self):
        make_test_tree(self.src_dir, {"file.txt": "data\n"})
        remote_src = REMOTE_PREFIX + self.src_dir
        remote_dst = REMOTE_PREFIX + self.dst_dir
        out, err, rc = run_fc(remote_src, remote_dst, "--dry-run")
        self.assertEqual(rc, 0)
        if os.path.exists(self.dst_dir):
            real = [f for f in os.listdir(self.dst_dir)
                    if not f.startswith(".fast_copy")]
            self.assertEqual(real, [])


# ═══════════════════════════════════════════════════════════════════
#  CROSS-MODE SPECIAL CHARS & EDGE CASES
# ═══════════════════════════════════════════════════════════════════

class TestR2LSpecialChars(TempDirMixin, unittest.TestCase):
    """R2L: Special characters in filenames."""

    def test_spaces_remote(self):
        structure = {
            "my file.txt": "spaces\n",
            "my dir/another file.dat": "more spaces\n",
        }
        make_test_tree(self.src_dir, structure)
        remote_src = REMOTE_PREFIX + self.src_dir
        out, err, rc = run_fc(remote_src, self.dst_dir)
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        verify_tree(self, self.src_dir, self.dst_dir,
                    expected_rels=list(structure.keys()))

    def test_unicode_remote(self):
        structure = {
            "données.txt": "french\n",
            "中文/文件.txt": "chinese\n",
        }
        make_test_tree(self.src_dir, structure)
        remote_src = REMOTE_PREFIX + self.src_dir
        out, err, rc = run_fc(remote_src, self.dst_dir)
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        verify_tree(self, self.src_dir, self.dst_dir,
                    expected_rels=list(structure.keys()))


class TestL2RSpecialChars(TempDirMixin, unittest.TestCase):
    """L2R: Special characters in filenames."""

    def test_spaces_to_remote(self):
        structure = {
            "space file.txt": "has spaces\n",
            "dir with spaces/nested file.txt": "deep spaces\n",
        }
        make_test_tree(self.src_dir, structure)
        remote_dst = REMOTE_PREFIX + self.dst_dir
        out, err, rc = run_fc(self.src_dir, remote_dst)
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        verify_tree(self, self.src_dir, self.dst_dir,
                    expected_rels=list(structure.keys()))


class TestR2RSpecialChars(TempDirMixin, unittest.TestCase):
    """R2R: Special characters in filenames."""

    def test_spaces_r2r(self):
        structure = {
            "space file.txt": "spaces r2r\n",
            "dir with spaces/file.dat": "nested spaces r2r\n",
        }
        make_test_tree(self.src_dir, structure)
        remote_src = REMOTE_PREFIX + self.src_dir
        remote_dst = REMOTE_PREFIX + self.dst_dir
        out, err, rc = run_fc(remote_src, remote_dst)
        self.assertEqual(rc, 0, f"stdout: {out}\nstderr: {err}")
        verify_tree(self, self.src_dir, self.dst_dir,
                    expected_rels=list(structure.keys()))


# ═══════════════════════════════════════════════════════════════════
#  VERSION CHECK
# ═══════════════════════════════════════════════════════════════════

class TestVersion(unittest.TestCase):
    """Verify version string."""

    def test_version_output(self):
        out, _, rc = run_fc("--version")
        self.assertEqual(rc, 0)
        self.assertIn("2.4.7", out)


if __name__ == "__main__":
    # Run with verbose output
    unittest.main(verbosity=2)
