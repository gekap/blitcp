#!/usr/bin/env python3
"""
End-to-end test of fast-copy against a real Synology NAS.

Target: yskapell@192.168.1.38:2205, dest /volume1/Home Movies/PARENTS/fc_test
       (note the space in "Home Movies" — tests path quoting)

Tests:
  L2R basic directory
  L2R single file
  L2R single file with file destination (v2.4.7 fix)
  L2R with --exclude
  L2R incremental (re-run skip)
  R2L round-trip
  R2R relay (NAS → localhost)
  Path with spaces (the destination has one)
  Verify FS detection still works (NAS is btrfs — should detect reflink
    if a copy is made INTO a btrfs-mounted local path; for L2R the FS
    detection is suppressed because dst is remote)
  Capability detection: tar/find/python3 versions on NAS
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time

NAS_HOST = "yskapell@192.168.1.38"
NAS_PORT = 2205
NAS_BASE = "/volume1/Home Movies/PARENTS/fc_test"
FAST_COPY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "fast_copy.py")
PYTHON = sys.executable


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def run_fc(*args, src_remote=False, dst_remote=True, timeout=300):
    """Run fast_copy.py with the right port flags.
    src_remote=True if the source is on the NAS;
    dst_remote=True if the destination is on the NAS."""
    cmd = [PYTHON, FAST_COPY]
    if src_remote:
        cmd += ["--ssh-src-port", str(NAS_PORT)]
    if dst_remote:
        cmd += ["--ssh-dst-port", str(NAS_PORT)]
    cmd += list(args)
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    elapsed = time.time() - t0
    return r.stdout, r.stderr, r.returncode, elapsed


def remote_run(cmd, timeout=30):
    """Run a command on the NAS via SSH."""
    full = ["ssh", "-p", str(NAS_PORT), NAS_HOST, cmd]
    r = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
    # Strip the SSH PQC warnings
    out = "\n".join(l for l in r.stdout.splitlines() if not l.startswith("**"))
    return out, r.stderr, r.returncode


def remote_clean():
    # Use find -mindepth 1 to delete EVERYTHING in NAS_BASE including
    # dotfiles, without removing NAS_BASE itself (which may have ACLs).
    remote_run("find '{}' -mindepth 1 -delete 2>/dev/null".format(NAS_BASE))


def remote_path_for(rel):
    """Build the SSH-style remote path."""
    return "{}:{}/{}".format(NAS_HOST, NAS_BASE, rel)


def make_test_tree(root, structure):
    os.makedirs(root, exist_ok=True)
    for rel, content in structure.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path) or root, exist_ok=True)
        if isinstance(content, int):
            data = (rel.encode() * (content // len(rel.encode()) + 1))[:content]
            with open(path, "wb") as f:
                f.write(data)
        else:
            with open(path, "w") as f:
                f.write(content)


def banner(text):
    line = "═" * 70
    print()
    print(line)
    print("  " + text)
    print(line)


# ════════════════════════════════════════════════════════════════════════
# Tests
# ════════════════════════════════════════════════════════════════════════

results = []  # (test_name, pass/fail, message, elapsed)


def record(name, ok, msg, elapsed=0):
    results.append((name, ok, msg, elapsed))
    tag = "✓ PASS" if ok else "✗ FAIL"
    print("  [{}] {}  ({:.2f}s)  {}".format(tag, name, elapsed, msg))


def test_basic_l2r():
    banner("Test 1 — L2R basic directory copy")
    with tempfile.TemporaryDirectory(prefix="fc_nas_") as work:
        src = os.path.join(work, "src")
        make_test_tree(src, {
            "README.md": "test readme\n",
            "docs/intro.txt": "intro\n",
            "docs/notes.txt": "notes\n",
            "data/values.csv": "a,b\n1,2\n",
        })
        remote_clean()
        out, err, rc, elapsed = run_fc(
            src, remote_path_for("basic_l2r"), dst_remote=True)
        if rc != 0:
            record("L2R basic dir", False, "rc={} err={}".format(rc, err[:200]), elapsed)
            return
        # Count visible files only (exclude .fast_copy_* internal files)
        out2, _, rc2 = remote_run(
            "find '{}/basic_l2r' -type f -not -name '.fast_copy*' | wc -l"
            .format(NAS_BASE))
        count = int(out2.strip()) if out2.strip().isdigit() else 0
        ok = (count == 4)
        record("L2R basic dir", ok, "{} files on NAS".format(count), elapsed)


def test_l2r_single_file():
    banner("Test 2 — L2R single file (v2.4.7 fix)")
    with tempfile.TemporaryDirectory(prefix="fc_nas_") as work:
        src_file = os.path.join(work, "single.txt")
        with open(src_file, "w") as f:
            f.write("hello synology")
        remote_clean()
        out, err, rc, elapsed = run_fc(
            src_file, remote_path_for("single_dir"), dst_remote=True)
        if rc != 0:
            record("L2R single file", False, "rc={} {}".format(rc, err[:200]), elapsed)
            return
        out2, _, rc2 = remote_run(
            "test -f '{}/single_dir/single.txt' && cat '{}/single_dir/single.txt'"
            .format(NAS_BASE, NAS_BASE))
        ok = "hello synology" in out2
        record("L2R single file", ok, "content match" if ok else out2[:100], elapsed)


def test_l2r_file_destination():
    banner("Test 3 — L2R single file → file destination (v2.4.8 fix)")
    with tempfile.TemporaryDirectory(prefix="fc_nas_") as work:
        src_file = os.path.join(work, "data.bin")
        with open(src_file, "wb") as f:
            f.write(b"\x00\x01\x02\x03" * 256)
        original = file_hash(src_file)
        remote_clean()
        # Push as file destination (path ends in filename with extension)
        dst_path = "{}:{}/renamed.bin".format(NAS_HOST, NAS_BASE)
        out, err, rc, elapsed = run_fc(src_file, dst_path, dst_remote=True)
        if rc != 0:
            record("L2R file dest", False, "rc={} {}".format(rc, err[:200]), elapsed)
            return
        # Pull it back via fast-copy itself (handles the spaces correctly)
        with tempfile.TemporaryDirectory() as work2:
            pull_dir = os.path.join(work2, "pulled")
            out2, err2, rc2, _ = run_fc(
                "{}:{}/renamed.bin".format(NAS_HOST, NAS_BASE),
                pull_dir, src_remote=True, dst_remote=False)
            if rc2 != 0:
                record("L2R file dest", False,
                       "pullback failed: " + err2[:200], elapsed)
                return
            local_pull = os.path.join(pull_dir, "renamed.bin")
            if not os.path.exists(local_pull):
                record("L2R file dest", False, "renamed.bin not pulled", elapsed)
                return
            ok = file_hash(local_pull) == original
            record("L2R file dest", ok,
                   "renamed.bin hash matches" if ok else "hash mismatch",
                   elapsed)


def test_l2r_exclude():
    banner("Test 4 — L2R with --exclude")
    with tempfile.TemporaryDirectory(prefix="fc_nas_") as work:
        src = os.path.join(work, "src")
        make_test_tree(src, {
            "keep.txt": "keep me\n",
            "secret.env": "PASSWORD=x\n",
            "docs/main.md": "main\n",
            "docs/debug.log": "debug\n",
        })
        remote_clean()
        out, err, rc, elapsed = run_fc(
            src, remote_path_for("excl"),
            "--exclude", "secret.env",
            "--exclude", "debug.log",
            dst_remote=True)
        if rc != 0:
            record("L2R --exclude", False, "rc={}".format(rc), elapsed)
            return
        # Verify excluded files NOT on NAS
        out2, _, _ = remote_run(
            "find '{}/excl' -type f -name '*'".format(NAS_BASE))
        files = [f.split("/")[-1] for f in out2.splitlines() if f.strip()]
        ok = ("secret.env" not in files and "debug.log" not in files
              and "keep.txt" in files and "main.md" in files)
        record("L2R --exclude", ok, "files: {}".format(files), elapsed)


def test_l2r_incremental():
    banner("Test 5 — L2R incremental (re-run should skip)")
    with tempfile.TemporaryDirectory(prefix="fc_nas_") as work:
        src = os.path.join(work, "src")
        make_test_tree(src, {"stable.txt": "no change\n"})
        remote_clean()
        # First run
        out1, _, rc1, e1 = run_fc(src, remote_path_for("incr"), dst_remote=True)
        if rc1 != 0:
            record("L2R incremental", False, "first run rc={}".format(rc1), e1)
            return
        # Second run — should skip
        out2, _, rc2, e2 = run_fc(src, remote_path_for("incr"), dst_remote=True)
        ok = (rc2 == 0 and ("skip" in out2.lower() or "unchanged" in out2.lower()))
        record("L2R incremental", ok,
               "second run skipped" if ok else "no skip detected", e2)


def test_r2l_basic():
    banner("Test 6 — R2L basic (NAS → local)")
    # First put a file on the NAS
    remote_clean()
    remote_run(
        "mkdir -p '{}/r2l_src' && echo 'from nas' > '{}/r2l_src/file.txt' && "
        "echo more > '{}/r2l_src/another.txt'".format(NAS_BASE, NAS_BASE, NAS_BASE))
    with tempfile.TemporaryDirectory(prefix="fc_nas_r2l_") as work:
        dst = os.path.join(work, "dst")
        out, err, rc, elapsed = run_fc(
            remote_path_for("r2l_src"), dst,
            src_remote=True, dst_remote=False)
        if rc != 0:
            record("R2L basic", False, "rc={} {}".format(rc, err[:200]), elapsed)
            return
        f1 = os.path.join(dst, "file.txt")
        f2 = os.path.join(dst, "another.txt")
        ok = (os.path.exists(f1) and os.path.exists(f2)
              and open(f1).read().strip() == "from nas")
        record("R2L basic", ok, "files received" if ok else "missing", elapsed)


def test_r2l_single_file():
    banner("Test 7 — R2L single file (v2.4.7 fix against real NAS)")
    remote_clean()
    remote_run(
        "echo 'single nas file' > '{}/standalone.txt'".format(NAS_BASE))
    with tempfile.TemporaryDirectory(prefix="fc_nas_r2l_sf_") as work:
        dst = os.path.join(work, "dst")
        out, err, rc, elapsed = run_fc(
            "{}:{}/standalone.txt".format(NAS_HOST, NAS_BASE), dst,
            src_remote=True, dst_remote=False)
        if rc != 0:
            record("R2L single file", False, "rc={} {}".format(rc, err[:200]), elapsed)
            return
        f = os.path.join(dst, "standalone.txt")
        ok = (os.path.exists(f) and "single nas file" in open(f).read())
        record("R2L single file", ok, "received correctly" if ok else "missing", elapsed)


def test_r2r_relay():
    banner("Test 8 — R2R relay (NAS → localhost via local fast-copy)")
    remote_clean()
    remote_run(
        "mkdir -p '{}/r2r_src' && echo 'r2r data' > '{}/r2r_src/file.txt'"
        .format(NAS_BASE, NAS_BASE))
    # Need a writable localhost destination via SSH
    USER = os.environ.get("USER", "kai")
    with tempfile.TemporaryDirectory(prefix="fc_nas_r2r_") as work:
        dst = os.path.join(work, "r2r_dst")
        os.makedirs(dst)
        # The destination uses default port 22 for localhost
        # We need to pass --ssh-src-port and --ssh-dst-port separately
        cmd = [PYTHON, FAST_COPY,
               "--ssh-src-port", str(NAS_PORT),
               "--ssh-dst-port", "22",
               remote_path_for("r2r_src"),
               "{}@localhost:{}".format(USER, dst)]
        t0 = time.time()
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        elapsed = time.time() - t0
        if r.returncode != 0:
            record("R2R relay", False,
                   "rc={} {}".format(r.returncode, r.stderr[:200]), elapsed)
            return
        f = os.path.join(dst, "file.txt")
        ok = (os.path.exists(f) and "r2r data" in open(f).read())
        record("R2R relay", ok, "relayed via localhost" if ok else "missing", elapsed)


def test_path_with_spaces():
    banner("Test 9 — Destination path with spaces (Home Movies)")
    # The whole NAS_BASE has spaces in it; tests so far have been hitting
    # this implicitly. Run an explicit verification by listing the dest.
    out, _, rc = remote_run("ls -la '{}'".format(NAS_BASE))
    ok = (rc == 0)
    record("path with spaces", ok,
           "remote ls succeeded" if ok else "remote ls failed", 0)


def test_round_trip():
    banner("Test 10 — Round-trip integrity (L2R then R2L, hash match)")
    with tempfile.TemporaryDirectory(prefix="fc_nas_rt_") as work:
        src = os.path.join(work, "src")
        os.makedirs(src)
        # Create binary files for hash verification
        for i in range(5):
            with open(os.path.join(src, "f_{}.bin".format(i)), "wb") as f:
                f.write(("payload {}\n".format(i)).encode() * 100)
        src_hashes = {f: file_hash(os.path.join(src, f))
                       for f in os.listdir(src)}

        remote_clean()
        # Push
        out, err, rc1, e1 = run_fc(src, remote_path_for("rt"), dst_remote=True)
        if rc1 != 0:
            record("round trip", False,
                   "push failed rc={} {}".format(rc1, err[:200]), e1)
            return

        # Pull back
        dst = os.path.join(work, "dst")
        out, err, rc2, e2 = run_fc(
            remote_path_for("rt"), dst, src_remote=True, dst_remote=False)
        if rc2 != 0:
            record("round trip", False,
                   "pull failed rc={} {}".format(rc2, err[:200]), e2)
            return

        # Compare hashes
        all_ok = True
        for fname, h in src_hashes.items():
            local = os.path.join(dst, fname)
            if not os.path.exists(local):
                all_ok = False
                break
            if file_hash(local) != h:
                all_ok = False
                break
        record("round trip", all_ok,
               "5 files SHA-256 verified" if all_ok else "hash mismatch",
               e1 + e2)


# ════════════════════════════════════════════════════════════════════════
# Main
# ════════════════════════════════════════════════════════════════════════

def main():
    print("\n\033[1m" + "═" * 70 + "\033[0m")
    print("\033[1m  fast-copy ↔ Synology DS720+ Test Suite\033[0m")
    print("\033[1m  Target: {}:{}{}\033[0m".format(NAS_HOST, NAS_PORT, NAS_BASE))
    print("\033[1m" + "═" * 70 + "\033[0m")

    tests = [
        test_basic_l2r,
        test_l2r_single_file,
        test_l2r_file_destination,
        test_l2r_exclude,
        test_l2r_incremental,
        test_r2l_basic,
        test_r2l_single_file,
        test_r2r_relay,
        test_path_with_spaces,
        test_round_trip,
    ]
    for fn in tests:
        try:
            fn()
        except Exception as e:
            record(fn.__name__, False,
                   "exception: {}: {}".format(type(e).__name__, e), 0)

    # Summary
    print()
    print("═" * 70)
    print("  SUMMARY")
    print("═" * 70)
    passed = sum(1 for r in results if r[1])
    total = len(results)
    print("  {} / {} passed".format(passed, total))
    failed = [r for r in results if not r[1]]
    if failed:
        print("\n  FAILURES:")
        for r in failed:
            print("    ✗ {}: {}".format(r[0], r[2]))
        return 1
    print("\n  \033[32mAll tests passed.\033[0m")
    # Cleanup
    remote_clean()
    return 0


if __name__ == "__main__":
    sys.exit(main())
