#!/usr/bin/env python3
# Copyright 2026 George Kapellakis
# Licensed under the Apache License, Version 2.0
"""User-Acceptance Test scenarios for the WHOLE of blitcp.

Black-box: each scenario builds a real workspace, runs the actual blitcp.py
as a child process, and inspects the resulting filesystem + output. Scenarios
are grouped; infra-dependent groups self-skip when the infra is absent.

  Groups:  local  — local-to-local copy surface (always auto)
           index  — --index-existing / --dedup-existing
           ssh    — pull / push / R2R / --ssh-no-sftp  (auto iff localhost sshd)
           cloud  — s3:// az:// gs://     (manual-only; SKIP in auto)
           smb    — smb:// / UNC          (manual-only; SKIP in auto)
           info   — --version / --check-update

  Modes:   AUTO   (default)   — run, assert, exit 1 on any FAIL (CI gate).
           MANUAL (--manual)  — guided walkthrough: prints the workspace + the
                                exact command, runs it live, states what to
                                verify, then asks you to accept/reject each one.

Usage:
  python uat_blitcp.py                      # auto, every applicable scenario
  python uat_blitcp.py --manual             # guided, interactive
  python uat_blitcp.py --group local index  # only these groups
  python uat_blitcp.py --only UAT-LOCAL-1
  python uat_blitcp.py --list
  python uat_blitcp.py --target ./blitcp.py --keep
"""
import os
import sys
import json
import stat
import shutil
import hashlib
import argparse
import subprocess
import tempfile

# i18n guard (I18N_DESIGN.md, M0): this suite asserts on English output.
# Pin the C locale for this process and every child it spawns so future
# translations can never break (or falsely pass) these checks.
os.environ["LC_ALL"] = "C"
os.environ["LANG"] = "C"
os.environ.pop("LANGUAGE", None)
os.environ.pop("BLITCP_LANG", None)

HERE = os.path.dirname(os.path.abspath(__file__))


class C:
    if sys.stdout.isatty() and os.environ.get("NO_COLOR") is None:
        B = "\033[1m"; R = "\033[31m"; G = "\033[32m"; Y = "\033[33m"
        CY = "\033[36m"; GREY = "\033[90m"; X = "\033[0m"
    else:
        B = R = G = Y = CY = GREY = X = ""


# ── tiny helpers ─────────────────────────────────────────────────────────────

def _h(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write(path, data: bytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


def _rand(n, seed=b"uat"):
    out = bytearray()
    block = hashlib.sha256(seed).digest()
    while len(out) < n:
        block = hashlib.sha256(block).digest()
        out.extend(block)
    return bytes(out[:n])


def _tree(root, spec):
    """spec: {relpath: bytes}. Returns root."""
    for rel, data in spec.items():
        _write(os.path.join(root, rel), data)
    return root


def _verify(dst, spec):
    """Every rel in spec must exist in dst with matching content."""
    for rel, data in spec.items():
        p = os.path.join(dst, rel)
        if not os.path.exists(p):
            return False, f"missing in destination: {rel}"
        with open(p, "rb") as f:
            if f.read() != data:
                return False, f"content mismatch: {rel}"
    return True, ""


# A throwaway credentials file for the whole suite. Without it every SSH
# scenario resolves the REAL credentials.json beside blitcp.py, and if that one
# is encrypted the child asks for its passphrase — on /dev/tty, which no amount
# of pipe capturing intercepts. Run from a terminal that meant nothing to it,
# every SSH scenario then blocked until its 240s timeout. Tests must never read
# the operator's actual secrets, so this points them somewhere empty.
_NULL_CREDS = os.path.join(tempfile.gettempdir(), ".blitcp_uat_no_creds.json")


def run_fc(target, args, timeout=240):
    env = dict(os.environ)
    env["NO_COLOR"] = "1"
    env["BLITCP_CREDENTIALS"] = _NULL_CREDS
    cmd = [sys.executable, target] + [str(a) for a in args]
    try:
        # stdin=DEVNULL: a prompt that reaches a real terminal hangs the whole
        # suite, and a test that waits for a human is not a test.
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, env=env,
                           stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired as e:
        # TimeoutExpired carries BYTES even when text=True was requested, so
        # concatenating them raised TypeError and destroyed the timeout report
        # it was written to produce.
        def _txt(v):
            if v is None:
                return ""
            return v.decode("utf-8", "replace") if isinstance(v, bytes) else v
        return 124, _txt(e.stdout) + _txt(e.stderr) + "\n[timeout]"


def _have_ssh():
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
             "-o", "ConnectTimeout=5", "localhost", "true"],
            capture_output=True, timeout=12)
        return r.returncode == 0
    except Exception:
        return False


def _have_paramiko():
    """blitcp refuses every SSH transfer without paramiko, exiting 1 with an
    install hint. Gating the ssh group on sshd alone therefore turned one
    missing package into ten failures -- and UAT-SSH-7, which distinguishes
    exit 3 from exit 1, read that exit 1 as "wrongly reported CORRUPT". Probe
    the interpreter that will actually run the target, not this one."""
    try:
        r = subprocess.run([sys.executable, "-c", "import paramiko"],
                           capture_output=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


HAVE_SSH = _have_ssh()
HAVE_PARAMIKO = _have_paramiko()

# ── LOCAL scenarios ──────────────────────────────────────────────────────────

_SPEC = {"docs/a.txt": b"hello world\n", "docs/b.bin": _rand(40_000, b"b"),
         "data/c.csv": b"x,y\n1,2\n", "empty.dat": b""}


def b_basic(ws):
    _tree(os.path.join(ws, "src"), _SPEC)
    return [os.path.join(ws, "src") + "/", os.path.join(ws, "dst") + "/"], {"spec": _SPEC}


def c_basic(ws, rc, out, info):
    if rc != 0:
        return False, f"exit {rc}"
    return _verify(os.path.join(ws, "dst"), info["spec"])


def b_incremental(ws):
    _tree(os.path.join(ws, "src"), _SPEC)
    a = [os.path.join(ws, "src") + "/", os.path.join(ws, "dst") + "/"]
    run_fc(info_target[0], a)                       # first copy (populate)
    return a, {"spec": _SPEC}


def c_incremental(ws, rc, out, info):
    if rc != 0:
        return False, f"exit {rc}"
    ok, det = _verify(os.path.join(ws, "dst"), info["spec"])
    if not ok:
        return False, det
    if not any(m in out for m in ("skip identical", "already on drive",
                                  "link instead of copy", "Space saved")):
        return False, "second run did not report skipping/linking identical files"
    return True, "re-run skipped/linked already-present files"


def b_dedup(ws):
    p = _rand(60_000, b"dup")
    _tree(os.path.join(ws, "src"), {"one.bin": p, "sub/two.bin": p})
    return [os.path.join(ws, "src") + "/", os.path.join(ws, "dst") + "/"], {}


def _shares_extents(a, b):
    """True if two files share physical storage (reflink/CoW dedup) — via
    filefrag. Best-effort: False if filefrag is missing."""
    try:
        import subprocess as _sp
        o = _sp.run(["filefrag", "-v", a, b], capture_output=True,
                    text=True, timeout=15).stdout
        return "shared" in o
    except Exception:
        return False


def c_dedup(ws, rc, out, info):
    if rc != 0:
        return False, f"exit {rc}"
    a = os.path.join(ws, "dst/one.bin")
    b = os.path.join(ws, "dst/sub/two.bin")
    if os.stat(a).st_ino == os.stat(b).st_ino:
        return True, "identical files share one inode (hardlink dedup)"
    if _shares_extents(a, b):           # reflink FS: distinct inodes, shared extents
        return True, "identical files share extents (reflink dedup)"
    return False, "identical files were not deduplicated (distinct inodes, no shared extents)"


def b_nodedup(ws):
    p = _rand(60_000, b"dup")
    _tree(os.path.join(ws, "src"), {"one.bin": p, "two.bin": p})
    return [os.path.join(ws, "src") + "/", os.path.join(ws, "dst") + "/",
            "--no-dedup"], {}


def c_nodedup(ws, rc, out, info):
    if rc != 0:
        return False, f"exit {rc}"
    i1 = os.stat(os.path.join(ws, "dst/one.bin")).st_ino
    i2 = os.stat(os.path.join(ws, "dst/two.bin")).st_ino
    if i1 == i2:
        return False, "--no-dedup still shared an inode"
    return True, "--no-dedup kept independent copies"


def b_dryrun(ws):
    _tree(os.path.join(ws, "src"), _SPEC)
    return [os.path.join(ws, "src") + "/", os.path.join(ws, "dst") + "/",
            "--dry-run"], {}


def c_dryrun(ws, rc, out, info):
    if rc != 0:
        return False, f"exit {rc}"
    dst = os.path.join(ws, "dst")
    wrote = os.path.exists(dst) and any(
        files for _r, _d, files in os.walk(dst))
    if wrote:
        return False, "--dry-run wrote files to the destination"
    if "DRY RUN" not in out:
        return False, "no DRY RUN plan printed"
    return True, "plan printed, nothing written"


def b_exclude(ws):
    _tree(os.path.join(ws, "src"),
          {"keep.txt": b"k", "skip.log": b"s", "sub/also.log": b"s2"})
    return [os.path.join(ws, "src") + "/", os.path.join(ws, "dst") + "/",
            "--exclude", "*.log"], {}


def c_exclude(ws, rc, out, info):
    if rc != 0:
        return False, f"exit {rc}"
    dst = os.path.join(ws, "dst")
    if not os.path.exists(os.path.join(dst, "keep.txt")):
        return False, "non-excluded file missing"
    if os.path.exists(os.path.join(dst, "skip.log")) or \
       os.path.exists(os.path.join(dst, "sub/also.log")):
        return False, "*.log files were not excluded"
    return True, "*.log excluded, others copied"


def b_overwrite(ws):
    _tree(os.path.join(ws, "src"), {"f.txt": b"NEW-CONTENT"})
    _write(os.path.join(ws, "dst/f.txt"), b"OLD-DIFFERENT")
    return [os.path.join(ws, "src") + "/", os.path.join(ws, "dst") + "/",
            "--overwrite"], {}


def c_overwrite(ws, rc, out, info):
    if rc != 0:
        return False, f"exit {rc}"
    with open(os.path.join(ws, "dst/f.txt"), "rb") as f:
        if f.read() != b"NEW-CONTENT":
            return False, "--overwrite did not replace the stale file"
    return True, "stale destination file overwritten"


def b_sha256(ws):
    _tree(os.path.join(ws, "src"), _SPEC)
    return [os.path.join(ws, "src") + "/", os.path.join(ws, "dst") + "/",
            "--hash", "sha256"], {"spec": _SPEC}


def c_sha256(ws, rc, out, info):
    if rc != 0:
        return False, f"exit {rc}"
    return _verify(os.path.join(ws, "dst"), info["spec"])


def b_preserve_mode(ws):
    f = _write(os.path.join(ws, "src/secret.sh"), b"#!/bin/sh\n")
    os.chmod(f, 0o700)
    return [os.path.join(ws, "src") + "/", os.path.join(ws, "dst") + "/",
            "--preserve", "mode"], {}


def c_preserve_mode(ws, rc, out, info):
    if rc != 0:
        return False, f"exit {rc}"
    m = stat.S_IMODE(os.stat(os.path.join(ws, "dst/secret.sh")).st_mode)
    if m != 0o700:
        return False, f"mode not preserved (got {oct(m)})"
    return True, "file mode 0700 preserved"


def b_logfile(ws):
    _tree(os.path.join(ws, "src"), _SPEC)
    log = os.path.join(ws, "run.jsonl")
    return [os.path.join(ws, "src") + "/", os.path.join(ws, "dst") + "/",
            "--log-file", log], {"log": log}


def c_logfile(ws, rc, out, info):
    if rc != 0:
        return False, f"exit {rc}"
    if not os.path.exists(info["log"]) or os.path.getsize(info["log"]) == 0:
        return False, "log file not written / empty"
    try:
        with open(info["log"]) as f:
            doc = json.load(f)          # one structured JSON document, not JSONL
    except Exception as e:
        return False, f"log not valid JSON: {e}"
    if not doc:
        return False, "log JSON is empty"
    return True, "structured JSON log written"


def b_glob(ws):
    _tree(os.path.join(ws, "src"),
          {"r1.csv": b"a", "r2.csv": b"b", "notes.txt": b"x"})
    return [os.path.join(ws, "src", "*.csv"), os.path.join(ws, "dst") + "/"], {}


def c_glob(ws, rc, out, info):
    if rc != 0:
        return False, f"exit {rc}"
    dst = os.path.join(ws, "dst")
    got = sorted(f for _r, _d, fs in os.walk(dst) for f in fs)
    if got != ["r1.csv", "r2.csv"]:
        return False, f"glob copied {got}, expected the two .csv only"
    return True, "glob selected only *.csv"


def b_symlink(ws):
    src = os.path.join(ws, "src")
    _write(os.path.join(src, "real.txt"), b"link-target\n")
    os.symlink("real.txt", os.path.join(src, "alias.txt"))
    return [src + "/", os.path.join(ws, "dst") + "/"], {}


def c_symlink(ws, rc, out, info):
    if rc != 0:
        return False, f"exit {rc}"
    alias = os.path.join(ws, "dst/alias.txt")
    if not os.path.lexists(alias):
        return False, "symlink entry missing at destination"
    try:
        with open(alias, "rb") as f:
            if f.read() != b"link-target\n":
                return False, "symlink does not resolve to correct content"
    except OSError as e:
        return False, f"symlink unreadable: {e}"
    return True, "symlink handled without error, resolves correctly"


def b_sparse(ws):
    p = os.path.join(ws, "src/sparse.img")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.truncate(1 << 20)          # 1 MiB hole
        f.seek(1 << 20)
        f.write(b"END")
    return [os.path.join(ws, "src") + "/", os.path.join(ws, "dst") + "/"], {}


def c_sparse(ws, rc, out, info):
    if rc != 0:
        return False, f"exit {rc}"
    a = _h(os.path.join(ws, "src/sparse.img"))
    b = _h(os.path.join(ws, "dst/sparse.img"))
    if a != b:
        return False, "sparse file content differs after copy"
    return True, "sparse file content preserved"


# ── INDEX scenarios (index-existing / dedup-existing) ────────────────────────

def _payload(ws, size=200_000):
    p = _rand(size, b"dup")
    existing = _write(os.path.join(ws, "dst/existing/old_data.bin"), p)
    _write(os.path.join(ws, "src/new_copy.bin"), p)
    _write(os.path.join(ws, "src/really_new.bin"), _rand(50_000, b"new"))
    return existing


def b_dedup_cached_twin(ws):
    """Regression (v4.1.0 link audit): a dry-run warms the source-hash cache
    for a duplicate pair; a NEW identical file added afterwards is uncached.
    The selective pre-hash must still group it with its cached twins — a
    cache-hit that skips prefix grouping silently copies instead of linking."""
    data = os.urandom(1_234_567)
    _write(os.path.join(ws, "src/A.bin"), data)
    _write(os.path.join(ws, "src/Adup.bin"), data)
    a = [os.path.join(ws, "src") + "/", os.path.join(ws, "dst") + "/"]
    run_fc(info_target[0], a + ["--dry-run"])       # warms cache, copies nothing
    _write(os.path.join(ws, "src/D_late.bin"), data)  # uncached twin
    return a, {}


def c_dedup_cached_twin(ws, rc, out, info):
    if rc != 0:
        return False, f"exit {rc}"
    paths = [os.path.join(ws, "dst", n) for n in ("A.bin", "Adup.bin", "D_late.bin")]
    if len({_h(p) for p in paths}) != 1:
        return False, "content mismatch across the duplicate group"
    inodes = {os.stat(p).st_ino for p in paths}
    if len(inodes) != 1:
        return False, (f"{len(inodes)} inodes for 3 identical files — the "
                       f"uncached twin was copied instead of linked")
    return True, "cached + uncached duplicates all share one inode"


def b_idx_link(ws):
    existing = _payload(ws)
    return [os.path.join(ws, "src") + "/", os.path.join(ws, "dst") + "/",
            "--index-existing", os.path.join(ws, "dst")], {"existing": existing}


def c_idx_link(ws, rc, out, info):
    if rc != 0:
        return False, f"exit {rc}"
    linked = os.path.join(ws, "dst/new_copy.bin")
    if _h(linked) != _h(info["existing"]):
        return False, "linked file content mismatch"
    if "link instead of copy" not in out:
        return False, "no cross-run/existing link reported"
    if not os.path.exists(os.path.join(ws, "dst/really_new.bin")):
        return False, "genuinely-new file not copied"
    same = os.stat(linked).st_ino == os.stat(info["existing"]).st_ino
    return True, "hardlinked to pre-existing" if same else "reflinked (verified)"


def b_idx_collision(ws):
    n = 128_000
    _write(os.path.join(ws, "dst/existing/a.bin"), b"A" * n)
    _write(os.path.join(ws, "src/b.bin"), b"B" * n)
    return [os.path.join(ws, "src") + "/", os.path.join(ws, "dst") + "/",
            "--index-existing", os.path.join(ws, "dst")], {"n": n}


def c_idx_collision(ws, rc, out, info):
    if rc != 0:
        return False, f"exit {rc}"
    with open(os.path.join(ws, "dst/b.bin"), "rb") as f:
        if f.read() != b"B" * info["n"]:
            return False, "FALSE size-match corrupted b.bin"
    return True, "same-size/different-content not falsely deduped"


def b_idx_idem(ws):
    _payload(ws)
    a = [os.path.join(ws, "src") + "/", os.path.join(ws, "dst") + "/",
         "--index-existing", os.path.join(ws, "dst")]
    run_fc(info_target[0], a)
    return a, {}


def c_idx_idem(ws, rc, out, info):
    if rc != 0:
        return False, f"exit {rc}"
    if _h(os.path.join(ws, "dst/new_copy.bin")) != \
       _h(os.path.join(ws, "dst/existing/old_data.bin")):
        return False, "content diverged after a second indexed run"
    return True, "re-indexing the destination is stable"


def b_idx_offmount(ws):
    _payload(ws)
    # A genuinely off-mount index path needs a second filesystem — provided via
    # FC_UAT_OTHER_MOUNT (a writable dir on a different mount). uat.py auto-sets
    # it when it finds a second writable mount.
    other = os.environ.get("FC_UAT_OTHER_MOUNT")
    if other and os.path.isdir(other) and os.access(other, os.W_OK):
        outside = tempfile.mkdtemp(prefix="fc_uat_offmount_", dir=other)
    else:
        outside = os.path.join(ws, "elsewhere")
    _write(os.path.join(outside, "x.bin"), _rand(9_000, b"x"))
    return [os.path.join(ws, "src") + "/", os.path.join(ws, "dst") + "/",
            "--index-existing", outside], {"outside": outside, "ws": ws}


def c_idx_offmount(ws, rc, out, info):
    try:
        if rc != 0:
            return False, f"copy aborted (exit {rc})"
        # The engine skips an index path only when it is on a DIFFERENT mount.
        dev_dst = os.stat(os.path.join(ws, "dst")).st_dev
        dev_out = os.stat(info["outside"]).st_dev
        if dev_dst == dev_out:
            if not os.path.exists(os.path.join(ws, "dst/really_new.bin")):
                return False, "copy did not complete"
            return None, "same filesystem — set FC_UAT_OTHER_MOUNT to a 2nd mount"
        if "not on the destination mount" not in out:
            return False, "off-mount index path was not warned/skipped"
        return True, "real cross-mount index path warned & skipped; copy proceeded"
    finally:
        if not os.path.realpath(info["outside"]).startswith(os.path.realpath(ws)):
            import shutil
            shutil.rmtree(info["outside"], ignore_errors=True)


def b_idx_dedup_alone(ws):
    _payload(ws)
    return [os.path.join(ws, "src") + "/", os.path.join(ws, "dst") + "/",
            "--dedup-existing"], {}


def c_idx_dedup_alone(ws, rc, out, info):
    if rc != 0:
        return False, f"exit {rc}"
    if not os.path.exists(os.path.join(ws, "dst/new_copy.bin")):
        return False, "copy did not complete"
    if "Indexing existing files" in out:
        return False, "index phase ran without --index-existing"
    return True, "--dedup-existing alone is a safe no-op"


def b_idx_inplace(ws):
    n = 300_000
    p = _rand(n, b"inplace")
    _write(os.path.join(ws, "dst/existing/copy1.bin"), p)
    _write(os.path.join(ws, "dst/existing/copy2.bin"), p)        # identical pair
    # Source: SAME size, DIFFERENT content — so both pre-existing copies get
    # lazily hashed (no early break on a source-content match), and the in-place
    # dedup between copy1 & copy2 fires. (btrfs/XFS-with-reflink only.)
    _write(os.path.join(ws, "src/trigger.bin"), _rand(n, b"different-content"))
    return [os.path.join(ws, "src") + "/", os.path.join(ws, "dst") + "/",
            "--index-existing", os.path.join(ws, "dst"), "--dedup-existing"], {}


def c_idx_inplace(ws, rc, out, info):
    if rc != 0:
        return False, f"exit {rc}"
    if "Inplace dedup:" in out:
        return True, "FIDEDUPERANGE merged pre-existing duplicates"
    return None, "no in-place dedup (filesystem not btrfs/XFS)"


# ── SSH scenarios (auto iff localhost sshd) ──────────────────────────────────

def b_ssh_pull(ws):
    _tree(os.path.join(ws, "src"), _SPEC)
    return [f"localhost:{os.path.join(ws, 'src')}/",
            os.path.join(ws, "dst") + "/"], {"spec": _SPEC}


def c_ssh_pull(ws, rc, out, info):
    if rc != 0:
        return False, f"exit {rc}: {out.strip()[-200:]}"
    return _verify(os.path.join(ws, "dst"), info["spec"])


def b_ssh_push(ws):
    _tree(os.path.join(ws, "src"), _SPEC)
    return [os.path.join(ws, "src") + "/",
            f"localhost:{os.path.join(ws, 'dst')}/"], {"spec": _SPEC}


def c_ssh_push(ws, rc, out, info):
    if rc != 0:
        return False, f"exit {rc}: {out.strip()[-200:]}"
    return _verify(os.path.join(ws, "dst"), info["spec"])


def b_ssh_r2r(ws):
    _tree(os.path.join(ws, "src"), _SPEC)
    return [f"localhost:{os.path.join(ws, 'src')}/",
            f"localhost:{os.path.join(ws, 'dst')}/"], {"spec": _SPEC}


def c_ssh_r2r(ws, rc, out, info):
    if rc != 0:
        return False, f"exit {rc}: {out.strip()[-200:]}"
    return _verify(os.path.join(ws, "dst"), info["spec"])


def b_ssh_nosftp(ws):
    _tree(os.path.join(ws, "src"), _SPEC)
    return [f"localhost:{os.path.join(ws, 'src')}/",
            os.path.join(ws, "dst") + "/", "--ssh-no-sftp"], {"spec": _SPEC}


def b_ssh_pull_dirmeta(ws):
    # #2 regression: the pull (remote->local) flow must restore directory
    # mode/times like the local flow. A 0700 source dir used to land at the
    # makedirs default (0755) because _apply_dir_metadata ran only locally.
    src = os.path.join(ws, "src")
    os.makedirs(os.path.join(src, "priv", "inner"))
    _write(os.path.join(src, "priv", "inner", "f.txt"), b"secret")
    os.chmod(os.path.join(src, "priv"), 0o700)
    os.chmod(os.path.join(src, "priv", "inner"), 0o711)
    return [f"localhost:{src}/", os.path.join(ws, "dst") + "/",
            "--preserve", "mode"], {}


def c_ssh_pull_dirmeta(ws, rc, out, info):
    if rc != 0:
        return False, f"exit {rc}: {out.strip()[-200:]}"
    for d, want in (("priv", 0o700), (os.path.join("priv", "inner"), 0o711)):
        p = os.path.join(ws, "dst", d)
        if not os.path.isdir(p):
            return False, f"pull dst dir {d} missing"
        got = stat.S_IMODE(os.stat(p).st_mode)
        if got != want:
            return False, f"pull dir {d}: {oct(want)} landed as {oct(got)}"
    return True, "pull restored directory modes (0700 / 0711)"


def b_ssh_pull_strips_setuid(ws):
    # SECURITY regression: a pull from an (untrusted) remote source must NOT
    # preserve setuid/setgid — under sudo the file lands root-owned, so honoring
    # a remote header's setuid bit would be a root-owned attacker-content setuid
    # binary (local privesc). Covers small (<1MB, tar-extract path) AND large
    # (>=1MB, streaming path) files.
    src = os.path.join(ws, "src")
    os.makedirs(src, exist_ok=True)
    _write(os.path.join(src, "small_suid"), b"x" * 4096)
    _write(os.path.join(src, "large_suid"), _rand(1_500_000, b"suid"))
    os.chmod(os.path.join(src, "small_suid"), 0o4755)   # setuid
    os.chmod(os.path.join(src, "large_suid"), 0o6755)   # setuid+setgid
    return [f"localhost:{src}/", os.path.join(ws, "dst") + "/",
            "--preserve", "mode"], {}


def c_ssh_pull_strips_setuid(ws, rc, out, info):
    if rc != 0:
        return False, f"exit {rc}: {out.strip()[-200:]}"
    for f in ("small_suid", "large_suid"):
        p = os.path.join(ws, "dst", f)
        if not os.path.isfile(p):
            return False, f"pulled file {f} missing"
        m = os.stat(p).st_mode
        if m & (stat.S_ISUID | stat.S_ISGID):
            return False, (f"{f} kept setuid/setgid from remote "
                           f"(mode {oct(stat.S_IMODE(m))}) — privesc risk")
    return True, "pull strips setuid/setgid from untrusted remote files (small + large)"


def b_ssh_push_source_skip(ws):
    # #6 regression: a benign unreadable SOURCE file on PUSH must exit 3
    # (source_skipped) like the local flow, not exit 1 (corrupt). Testable as
    # non-root: an owner can't read its own 0o000 file.
    src = os.path.join(ws, "src")
    os.makedirs(src, exist_ok=True)
    _write(os.path.join(src, "readable.txt"), b"ok")
    lost = _write(os.path.join(src, "locked.txt"), b"secret")
    os.chmod(lost, 0o000)
    return [src + "/", f"localhost:{os.path.join(ws, 'dst')}/"], {}


def c_ssh_push_source_skip(ws, rc, out, info):
    try:
        os.chmod(os.path.join(ws, "src", "locked.txt"), 0o644)  # cleanup
    except OSError:
        pass
    if os.path.exists(os.path.join(ws, "dst", "locked.txt")):
        return None, "skip: source was readable (privileged run)"
    if rc == 1:
        return False, "push benign source-skip wrongly reported CORRUPT (exit 1)"
    if rc != 3:
        return False, f"push unreadable-source should exit 3, got {rc}"
    return True, "push benign source-skip → exit 3 (matches local flow)"


def c_ssh_nosftp(ws, rc, out, info):
    if rc != 0:
        return False, f"exit {rc}: {out.strip()[-200:]}"
    ok, det = _verify(os.path.join(ws, "dst"), info["spec"])
    if ok:
        return True, "tar stream honored trailing-slash like SFTP"
    # Content may have arrived but at a different layout than SFTP — flag it.
    if os.path.isdir(os.path.join(ws, "dst", "src")):
        return False, ("transport inconsistency: --ssh-no-sftp nests under the "
                       "source basename (dst/src/...) while SFTP copies contents "
                       "(dst/...) for the same 'src/' spec")
    return False, det


# ── INFO scenarios ───────────────────────────────────────────────────────────

def b_version(ws):
    return ["--version"], {}


def c_version(ws, rc, out, info):
    if rc != 0:
        return False, f"exit {rc}"
    if not any(ch.isdigit() for ch in out):
        return False, "no version string printed"
    return True, f"version reported: {out.strip().splitlines()[0][:60]}"


# ── cloud (manual stub) / SMB (env-driven, real round-trip) ──────────────────

def b_manual(ws):
    return [], {}


def b_cloud(ws):
    # Runs when FC_UAT_CLOUD_URL is set to a writable cloud target, e.g. a named
    # connection: s3://aws_fastcopies@fastcopies  (az://NAME@container, gs://NAME@bucket).
    # Needs BLITCP_CREDS_PASSPHRASE in the env to unlock the saved connection.
    # Uses a FIXED 'uat_cloud/' prefix so re-runs overwrite (no accumulation).
    base = os.environ["FC_UAT_CLOUD_URL"].rstrip("/")
    _tree(os.path.join(ws, "src"), _SPEC)
    return [os.path.join(ws, "src") + "/", base + "/uat_cloud/"], {"base": base}


def c_cloud(ws, rc, out, info):
    if rc != 0:
        return False, f"cloud upload exit {rc}: {out.strip()[-180:]}"
    rc2, out2 = run_fc(info_target[0], [info["base"] + "/uat_cloud/", os.path.join(ws, "back") + "/"])
    if rc2 != 0:
        return False, f"cloud download exit {rc2}: {out2.strip()[-180:]}"
    ok, det = _verify(os.path.join(ws, "back"), _SPEC)
    return ok, "cloud upload + download round-trip verified" if ok else det


def b_smb(ws):
    # Runs when FC_UAT_SMB_URL (smb://host/share[/prefix]) is set, with optional
    # FC_UAT_SMB_USER + FC_UAT_SMB_PASS (env). uat.py auto-sets these when a
    # local Samba / reachable SMB share is detected.
    base = os.environ["FC_UAT_SMB_URL"].rstrip("/")
    user = os.environ.get("FC_UAT_SMB_USER", "")
    _tree(os.path.join(ws, "src"), _SPEC)
    args = [os.path.join(ws, "src") + "/", base + "/uat_smb/"]
    if user:
        args += ["--smb-user", user, "--smb-password-env", "FC_UAT_SMB_PASS"]
    return args, {"base": base, "user": user}


def c_smb(ws, rc, out, info):
    if rc != 0:
        return False, f"SMB upload exit {rc}: {out.strip()[-160:]}"
    dargs = [info["base"] + "/uat_smb/", os.path.join(ws, "back") + "/"]
    if info["user"]:
        dargs += ["--smb-user", info["user"], "--smb-password-env", "FC_UAT_SMB_PASS"]
    rc2, out2 = run_fc(info_target[0], dargs)
    if rc2 != 0:
        return False, f"SMB download exit {rc2}: {out2.strip()[-160:]}"
    ok, det = _verify(os.path.join(ws, "back"), _SPEC)
    return ok, "SMB upload + download round-trip verified" if ok else det


# ── registry ─────────────────────────────────────────────────────────────────

def S(id, group, title, build, check, needs=None, manual_only=False):
    return {"id": id, "group": group, "title": title, "build": build,
            "check": check, "needs": needs, "manual_only": manual_only,
            "expect": title}


def b_verify_catches_missing(ws):
    # One source file is made unreadable so the copy cannot include it — the
    # destination ends up incomplete, which verification MUST catch with a
    # non-zero exit (regression: the engine used to ignore verify_copy()'s
    # result and print DONE / exit 0 on an incomplete copy).
    src = os.path.join(ws, "src")
    os.makedirs(src, exist_ok=True)
    with open(os.path.join(src, "keep.txt"), "w") as f:
        f.write("copied fine")
    lost = os.path.join(src, "lost.txt")
    with open(lost, "w") as f:
        f.write("cannot read me")
    os.chmod(lost, 0o000)
    return [src + "/", os.path.join(ws, "dst") + "/", "--no-dedup"], {}


def c_verify_catches_missing(ws, rc, out, info):
    try:
        os.chmod(os.path.join(ws, "src", "lost.txt"), 0o644)   # so cleanup can rm
    except OSError:
        pass
    if os.path.exists(os.path.join(ws, "dst", "lost.txt")):
        return True, "skip: unreadable source still copied (privileged run)"
    if rc == 0:
        return False, "destination missing a file but verify exited 0"
    # An UNREADABLE SOURCE file (not corruption) must exit 3 (source_skipped),
    # distinct from corruption/incomplete which exits 1 — so it isn't flagged as a
    # corrupt/failed transfer.
    if rc != 3:
        return False, f"unreadable-source skip should exit 3, got {rc}"
    if "could NOT be read" not in out:
        return False, "exit 3 but no 'could NOT be read from source' verdict printed"
    return True, "unreadable source → exit 3 (distinct from corruption exit 1)"


def b_stream_file_modes(ws):
    # Regression: Python 3.12 tarfile's 'data' filter clamps group/other-write,
    # so 664/775 files copied through the small-file tar stream landed as
    # 644/755. The engine must re-apply the real source mode after extract.
    for name, mode in (("f664.txt", 0o664), ("f775.sh", 0o775),
                       ("f640.txt", 0o640)):
        # Distinct content per file — identical bodies would be dedup-hardlinked
        # into one inode, which by design shares a single mode.
        f = _write(os.path.join(ws, "src", name), b"mode test " + name.encode())
        os.chmod(f, mode)
    return [os.path.join(ws, "src") + "/", os.path.join(ws, "dst") + "/"], {}


def c_stream_file_modes(ws, rc, out, info):
    if rc != 0:
        return False, f"exit {rc}"
    for name, want in (("f664.txt", 0o664), ("f775.sh", 0o775),
                       ("f640.txt", 0o640)):
        got = stat.S_IMODE(os.stat(os.path.join(ws, "dst", name)).st_mode)
        if got != want:
            return False, f"{name}: {oct(want)} landed as {oct(got)} (data-filter clamp)"
    return True, "664/775/640 file modes survive the tar stream"


def b_dir_metadata(ws):
    # Regression: directories were created at default 755 with fresh mtimes —
    # a private 0700 source dir landed world-readable and setgid was lost.
    # The engine must mirror dir mode (incl. setgid) and mtime after Phase 5.
    old = 1588647905  # 2020-05-05 05:05:05 UTC — clearly not "now"
    for d, mode in (("d700", 0o700), ("d2755", 0o2755), ("d775", 0o775)):
        f = _write(os.path.join(ws, "src", d, "payload.txt"), d.encode())
        os.chmod(os.path.dirname(f), mode)
        os.utime(os.path.dirname(f), (old, old))
    return [os.path.join(ws, "src") + "/", os.path.join(ws, "dst") + "/"], \
           {"old": old}


def c_dir_metadata(ws, rc, out, info):
    if rc != 0:
        return False, f"exit {rc}"
    for d, want in (("d700", 0o700), ("d2755", 0o2755), ("d775", 0o775)):
        st = os.stat(os.path.join(ws, "dst", d))
        got = stat.S_IMODE(st.st_mode)
        if got != want:
            return False, f"dir {d}: {oct(want)} landed as {oct(got)}"
        if abs(st.st_mtime - info["old"]) > 2:
            return False, f"dir {d}: mtime not preserved (got {int(st.st_mtime)})"
    return True, "dir modes (700/setgid/775) + mtimes preserved"


def b_preserve_acl(ws):
    # Regression pair for the ACL work:
    #   1. a file carrying a real POSIX ACL must still carry it at the dest
    #      (the getxattr fast-path must not skip real ACLs), and
    #   2. a file WITHOUT an ACL must keep its exact mode — an early fast-path
    #      draft skipped the setfacl round-trip that was masking the tar-stream
    #      mode clamp, silently turning 664 into 644.
    import subprocess
    fa = _write(os.path.join(ws, "src", "with_acl.txt"), b"acl\n")
    fn = _write(os.path.join(ws, "src", "no_acl.txt"), b"plain\n")
    os.chmod(fa, 0o644)
    os.chmod(fn, 0o664)
    r = subprocess.run(["setfacl", "-m", "u:12345:rwx", fa],
                       capture_output=True)
    ok = r.returncode == 0
    return [os.path.join(ws, "src") + "/", os.path.join(ws, "dst") + "/",
            "--preserve", "mode,times,acl"], {"acl_ok": ok}


def c_preserve_acl(ws, rc, out, info):
    if not info.get("acl_ok"):
        return None, "setfacl unavailable / filesystem without POSIX ACLs"
    if rc != 0:
        return False, f"exit {rc}"
    try:
        src_acl = os.getxattr(os.path.join(ws, "src/with_acl.txt"),
                              "system.posix_acl_access")
        dst_acl = os.getxattr(os.path.join(ws, "dst/with_acl.txt"),
                              "system.posix_acl_access")
    except OSError as e:
        return False, f"ACL missing on destination ({e})"
    if src_acl != dst_acl:
        return False, "ACL bytes differ between source and destination"
    got = stat.S_IMODE(os.stat(os.path.join(ws, "dst/no_acl.txt")).st_mode)
    if got != 0o664:
        return False, f"no-ACL file mode 0o664 landed as {oct(got)}"
    return True, "real ACL carried over; ACL-less file keeps exact mode"


def b_local_keeps_setuid(ws):
    # Guard the setuid fix from over-stripping: a LOCAL copy (trusted source =
    # the user's own tree) must still preserve setuid/setgid like cp -a. Only
    # UNTRUSTED remote pulls strip them (see UAT-SSH-6).
    src = os.path.join(ws, "src")
    os.makedirs(src, exist_ok=True)
    _write(os.path.join(src, "small_suid"), b"x" * 4096)
    _write(os.path.join(src, "large_suid"), _rand(1_500_000, b"lsuid"))
    os.chmod(os.path.join(src, "small_suid"), 0o4755)
    os.chmod(os.path.join(src, "large_suid"), 0o6755)
    return [src + "/", os.path.join(ws, "dst") + "/", "--preserve", "mode"], {}


def c_local_keeps_setuid(ws, rc, out, info):
    if rc != 0:
        return False, f"exit {rc}"
    for f, want in (("small_suid", 0o4755), ("large_suid", 0o6755)):
        got = stat.S_IMODE(os.stat(os.path.join(ws, "dst", f)).st_mode)
        if got != want:
            return False, f"local {f}: setuid/setgid lost — {oct(want)} landed {oct(got)}"
    return True, "local copy preserves setuid/setgid (cp -a; not over-stripped)"


def b_dest_write_fail_is_corrupt(ws):
    # #1 regression: a DESTINATION-write permission failure must be classified
    # as a real, incomplete copy (exit 1 corrupt), NOT downgraded to the benign
    # source-skipped verdict (exit 3) just because its EACCES text reads the same
    # as a source-read failure. >1MB so it takes the copy_individual path where
    # the write-open EACCES is deterministic.
    src = os.path.join(ws, "src")
    os.makedirs(os.path.join(src, "sub"), exist_ok=True)
    _write(os.path.join(src, "sub", "big.bin"), _rand(2_000_000, b"destfail"))
    # Pre-create the destination subdir read-only so the file cannot be written.
    dsub = os.path.join(ws, "dst", "sub")
    os.makedirs(dsub, exist_ok=True)
    os.chmod(dsub, 0o500)
    return [src + "/", os.path.join(ws, "dst") + "/", "--no-dedup"], {}


def c_dest_write_fail_is_corrupt(ws, rc, out, info):
    dsub = os.path.join(ws, "dst", "sub")
    try:
        os.chmod(dsub, 0o755)   # restore so cleanup can rm
    except OSError:
        pass
    if os.path.exists(os.path.join(dsub, "big.bin")):
        return None, "skip: destination was writable (privileged run) — file landed"
    if rc == 3:
        return False, "dest-write failure WRONGLY downgraded to source-skipped (exit 3)"
    if rc != 1:
        return False, f"dest-write failure should be corrupt (exit 1), got {rc}"
    return True, "destination-write EACCES → exit 1 (corrupt), not exit 3"


def b_dedup_dir_metadata(ws):
    # F4 regression: a directory whose files are ALL deduplicated (linked, not
    # copied) still must get its source mode mirrored. Two identical files in two
    # distinctively-moded dirs — dedup links one, so that dir appears only in
    # link_map, the path _apply_dir_metadata originally missed.
    p = _rand(60_000, b"f4dup")
    _tree(os.path.join(ws, "src"), {"canon/a.bin": p, "deduped/b.bin": p})
    os.chmod(os.path.join(ws, "src", "canon"), 0o702)
    os.chmod(os.path.join(ws, "src", "deduped"), 0o701)
    return [os.path.join(ws, "src") + "/", os.path.join(ws, "dst") + "/"], {}


def c_dedup_dir_metadata(ws, rc, out, info):
    if rc != 0:
        return False, f"exit {rc}"
    a = os.path.join(ws, "dst/canon/a.bin")
    b = os.path.join(ws, "dst/deduped/b.bin")
    # One of the two moded dirs holds only a linked (deduped) file, so it lives
    # only in link_map — exactly the case F4 recovers. If the FS didn't dedup,
    # the path isn't exercised, so skip rather than false-pass.
    if not (os.stat(a).st_ino == os.stat(b).st_ino or _shares_extents(a, b)):
        return None, "files were not deduplicated on this FS — F4 path not exercised"
    for d, want in (("canon", 0o702), ("deduped", 0o701)):
        got = stat.S_IMODE(os.stat(os.path.join(ws, "dst", d)).st_mode)
        if got != want:
            return False, f"deduped-tree dir {d}: {oct(want)} landed as {oct(got)}"
    return True, "all-deduplicated directories keep their source mode (F4)"


def b_progress_no_early_100(ws):
    # Regression (2026-07-28): byte-weighted bar + large-files-first ordering
    # showed 100% / ETA 0s while thousands of small files were still streaming.
    # A few large files carry >99% of the bytes; a big tail of distinct small
    # files makes files_done lag far behind bytes_done during the copy —
    # without the cap, pct sits above 99 for the whole small-file stream.
    spec = {"large0.bin": b"A" * 50_000_000, "large1.bin": b"B" * 50_000_000}
    for i in range(3000):
        spec[f"small/f{i:04d}.dat"] = _rand(100, b"s%d" % i)
    _tree(os.path.join(ws, "src"), spec)
    return [os.path.join(ws, "src") + "/", os.path.join(ws, "dst") + "/",
            "--progress-json"], {"nfiles": len(spec)}


def c_progress_no_early_100(ws, rc, out, info):
    if rc != 0:
        return False, f"exit {rc}"
    recs = []
    for ln in out.splitlines():
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("t") in ("progress", "done"):
            recs.append(r)
    partial = [r for r in recs if r["t"] == "progress"
               and r.get("files_done", 0) < r.get("files_total", 0)]
    if not partial:
        return None, "copy finished too fast to sample mid-copy progress"
    early_full = [r for r in partial if r["pct"] > 99.0]
    if early_full:
        r = early_full[0]
        return False, (f"pct {r['pct']}% with only {r['files_done']}/"
                       f"{r['files_total']} files copied (early-100% regression)")
    over = [r for r in recs if r["pct"] > 100.0]
    if over:
        return False, f"pct overshot 100%: {over[0]['pct']}"
    return True, (f"{len(partial)} mid-copy samples all capped ≤99% "
                  f"while files remained")


# ── Saved-connection scenarios ───────────────────────────────────────────────
# A named connection carries its own `path`. Nothing here used to exercise that
# resolution at all: a profile with path=/data plus a `name:sub` argument
# silently addressed the SSH login directory instead, and a bare filename left
# the tar producer with an empty root, which broke remote-to-remote entirely.

def _write_conn(ws, name="uatsrv", base=None, port=22):
    """A plaintext credentials file inside the workspace. No secret is stored:
    localhost SSH here is key-based, exactly like the other ssh scenarios."""
    import getpass
    conn = {name: {"type": "ssh", "host": "localhost",
                   "user": getpass.getuser(), "port": port,
                   "path": base if base is not None else os.path.join(ws, "base")}}
    path = os.path.join(ws, "creds.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(conn, f, indent=2)
    os.chmod(path, 0o600)
    return path


def b_conn_path_join(ws):
    base = os.path.join(ws, "base")
    _tree(os.path.join(base, "sub"), _SPEC)
    creds = _write_conn(ws, base=base)
    # 'uatsrv:sub/' must mean <base>/sub, NOT 'sub' under the login directory.
    return ["--credentials-file", creds, "uatsrv:sub/",
            os.path.join(ws, "dst") + "/"], {"spec": _SPEC, "base": base}


def c_conn_path_join(ws, rc, out, info):
    if rc != 0:
        return False, f"exit {rc}: {out.strip()[-200:]}"
    wanted = os.path.join(info["base"], "sub")
    if wanted not in out:
        return False, (f"source resolved somewhere other than {wanted} — the "
                       f"profile's path was dropped")
    return _verify(os.path.join(ws, "dst"), info["spec"])


def b_conn_r2r_bare_name(ws):
    """Remote → remote where the suffix is a BARE FILENAME.

    This is the shape that failed in the field: with the profile path dropped,
    the source root came out empty and `cd '' && tar ...` is an error under
    bash (dash accepts it), so the producer never ran, the consumer reported
    'does not look like a tar archive', and the run still drew a 100% bar."""
    base = os.path.join(ws, "base")
    os.makedirs(base, exist_ok=True)
    _write(os.path.join(base, "payload.bin"), _rand(64 * 1024))
    creds = _write_conn(ws, base=base)
    dst = os.path.join(ws, "dst")
    os.makedirs(dst, exist_ok=True)
    return ["--credentials-file", creds, "uatsrv:payload.bin",
            f"localhost:{dst}/"], {"dst": dst,
                                   "src": os.path.join(base, "payload.bin")}


def c_conn_r2r_bare_name(ws, rc, out, info):
    if rc != 0:
        return False, f"exit {rc}: {out.strip()[-200:]}"
    if "Traceback" in out:
        return False, "a Python traceback reached the output"
    landed = os.path.join(info["dst"], "payload.bin")
    if not os.path.isfile(landed):
        return False, "the file never arrived"
    if open(landed, "rb").read() != open(info["src"], "rb").read():
        return False, "content differs"
    if "0.0 B piped" in out:
        return False, "relay reported success while piping nothing"
    return True, "bare filename resolves against the profile path"


def b_remote_scan_targeted(ws):
    """The incremental check must ask about ITS files, not list the tree.

    Listing everything under the destination is what timed out against a
    1.26M-file home directory. 2000 files is far too few to time out, but it is
    plenty to tell the two questions apart in the output."""
    src = os.path.join(ws, "src")
    os.makedirs(src, exist_ok=True)
    _write(os.path.join(src, "one.bin"), _rand(4096))
    dst = os.path.join(ws, "dst")
    os.makedirs(dst, exist_ok=True)
    for i in range(2000):
        _write(os.path.join(dst, "noise%04d.bin" % i), b"x" * 64)
    return [os.path.join(src, "one.bin"), f"localhost:{dst}/"], {"dst": dst}


def c_remote_scan_targeted(ws, rc, out, info):
    if rc != 0:
        return False, f"exit {rc}: {out.strip()[-200:]}"
    if "Scanned remote" in out:
        return False, ("enumerated the whole destination to check one file "
                       "— the timeout this guards against comes back at scale")
    if "remote path" not in out:
        return None, "no incremental phase in this run"
    if not os.path.isfile(os.path.join(info["dst"], "one.bin")):
        return False, "the file never arrived"
    return True, "asked per path instead of listing 2000 files"


SCENARIOS = [
    S("UAT-LOCAL-1", "local", "basic tree copy preserves all content", b_basic, c_basic),
    S("UAT-LOCAL-2", "local", "incremental re-run skips/links identical files", b_incremental, c_incremental),
    S("UAT-LOCAL-3", "local", "within-run dedup shares one inode", b_dedup, c_dedup),
    S("UAT-LOCAL-4", "local", "--no-dedup keeps independent copies", b_nodedup, c_nodedup),
    S("UAT-LOCAL-5", "local", "--dry-run writes nothing", b_dryrun, c_dryrun),
    S("UAT-LOCAL-6", "local", "--exclude drops matching files", b_exclude, c_exclude),
    S("UAT-LOCAL-7", "local", "--overwrite replaces a stale destination file", b_overwrite, c_overwrite),
    S("UAT-LOCAL-8", "local", "--hash sha256 copies with integrity", b_sha256, c_sha256),
    S("UAT-LOCAL-9", "local", "--preserve mode keeps file permissions", b_preserve_mode, c_preserve_mode),
    S("UAT-LOCAL-10", "local", "--log-file writes a structured JSON log", b_logfile, c_logfile),
    S("UAT-LOCAL-11", "local", "glob source selects only matches", b_glob, c_glob),
    S("UAT-LOCAL-12", "local", "symlink handled without error", b_symlink, c_symlink),
    S("UAT-LOCAL-13", "local", "sparse file content preserved", b_sparse, c_sparse),
    S("UAT-LOCAL-14", "local", "unreadable source → verify exits 3 (distinct from corruption)", b_verify_catches_missing, c_verify_catches_missing),
    S("UAT-LOCAL-15", "local", "664/775 file modes survive the small-file tar stream", b_stream_file_modes, c_stream_file_modes),
    S("UAT-LOCAL-16", "local", "directory mode (700/setgid) + mtime preserved", b_dir_metadata, c_dir_metadata),
    S("UAT-LOCAL-17", "local", "--preserve acl keeps real ACLs; ACL-less files keep exact mode", b_preserve_acl, c_preserve_acl),
    S("UAT-LOCAL-18", "local", "all-deduplicated directories keep their source mode (F4)", b_dedup_dir_metadata, c_dedup_dir_metadata),
    S("UAT-LOCAL-19", "local", "destination-write failure → exit 1 (corrupt), not exit 3", b_dest_write_fail_is_corrupt, c_dest_write_fail_is_corrupt),
    S("UAT-LOCAL-20", "local", "local copy preserves setuid/setgid (not over-stripped)", b_local_keeps_setuid, c_local_keeps_setuid),
    S("UAT-LOCAL-21", "local", "progress never reports 100% while files remain", b_progress_no_early_100, c_progress_no_early_100),
    S("UAT-LOCAL-22", "local", "uncached twin of a cache-warmed duplicate still links", b_dedup_cached_twin, c_dedup_cached_twin),

    S("UAT-INDEX-1", "index", "index-existing links an identical pre-existing file", b_idx_link, c_idx_link),
    S("UAT-INDEX-2", "index", "same size / different content not falsely matched", b_idx_collision, c_idx_collision),
    S("UAT-INDEX-3", "index", "re-indexing the destination is idempotent", b_idx_idem, c_idx_idem),
    S("UAT-INDEX-4", "index", "off-mount --index-existing path warned & skipped", b_idx_offmount, c_idx_offmount),
    S("UAT-INDEX-5", "index", "--dedup-existing alone is a safe no-op", b_idx_dedup_alone, c_idx_dedup_alone),
    S("UAT-INDEX-6", "index", "--dedup-existing merges duplicates in place", b_idx_inplace, c_idx_inplace),

    S("UAT-SSH-1", "ssh", "pull over SSH (remote source)", b_ssh_pull, c_ssh_pull, needs="ssh"),
    S("UAT-SSH-2", "ssh", "push over SSH (remote destination)", b_ssh_push, c_ssh_push, needs="ssh"),
    S("UAT-SSH-3", "ssh", "remote-to-remote over SSH", b_ssh_r2r, c_ssh_r2r, needs="ssh"),
    S("UAT-SSH-4", "ssh", "--ssh-no-sftp tar streaming", b_ssh_nosftp, c_ssh_nosftp, needs="ssh"),
    S("UAT-SSH-5", "ssh", "pull restores directory metadata (mode) — #2", b_ssh_pull_dirmeta, c_ssh_pull_dirmeta, needs="ssh"),
    S("UAT-SSH-6", "ssh", "pull strips setuid/setgid from untrusted remote (privesc)", b_ssh_pull_strips_setuid, c_ssh_pull_strips_setuid, needs="ssh"),
    S("UAT-SSH-7", "ssh", "push benign source-skip → exit 3 (not corrupt) — #6", b_ssh_push_source_skip, c_ssh_push_source_skip, needs="ssh"),
    S("UAT-SSH-8", "ssh", "saved connection: a suffix refines its path, does not replace it", b_conn_path_join, c_conn_path_join, needs="ssh"),
    S("UAT-SSH-9", "ssh", "remote-to-remote via saved connection with a bare filename", b_conn_r2r_bare_name, c_conn_r2r_bare_name, needs="ssh"),
    S("UAT-SSH-10", "ssh", "incremental check asks per path, never lists the whole destination", b_remote_scan_targeted, c_remote_scan_targeted, needs="ssh"),

    S("UAT-CLOUD-1", "cloud", "object-storage round trip (s3/az/gs)", b_cloud, c_cloud, needs="cloud"),
    S("UAT-SMB-1", "smb", "SMB upload/download round trip", b_smb, c_smb, needs="smb"),

    S("UAT-INFO-1", "info", "--version prints a version", b_version, c_version),
]

# build_* helpers that pre-populate need the target; expose it module-wide.
info_target = [os.path.join(HERE, "blitcp.py")]


def _verdict(ok):
    return f"{C.G}PASS{C.X}" if ok is True else (
        f"{C.Y}SKIP{C.X}" if ok is None else f"{C.R}FAIL{C.X}")


def _short(a, ws):
    return str(a).replace(ws + "/", "").replace(ws, ".")


def _indent(text, p="      | "):
    return "\n".join(p + ln for ln in text.splitlines()[-25:])


def _run_one(sc, target, manual, keep):
    needs = sc["needs"]
    if not manual:
        if sc["manual_only"]:
            return None, "manual-only (needs live endpoint/credentials)"
        if needs == "ssh" and not HAVE_SSH:
            return None, "no localhost sshd"
        if needs == "ssh" and not HAVE_PARAMIKO:
            return None, (f"paramiko not importable by {sys.executable} "
                          f"- SSH transfers cannot run")
        if needs == "smb" and not os.environ.get("FC_UAT_SMB_URL"):
            return None, "set FC_UAT_SMB_URL (+USER/PASS) to test SMB"
        if needs == "cloud" and not os.environ.get("FC_UAT_CLOUD_URL"):
            return None, "set FC_UAT_CLOUD_URL (+BLITCP_CREDS_PASSPHRASE) to test cloud"
    ws = tempfile.mkdtemp(prefix=f"{sc['id']}_")
    try:
        args, info = sc["build"](ws)
        if manual:
            print(f"\n{C.B}{sc['id']} — {sc['title']}{C.X}  {C.GREY}[{sc['group']}]{C.X}")
            print(f"  {C.GREY}workspace:{C.X} {ws}")
            cmd = os.path.basename(target) + " " + " ".join(_short(a, ws) for a in args)
            print(f"  {C.GREY}command:{C.X}   {cmd if args else '(manual steps below)'}")
            print(f"  {C.CY}expect:{C.X}    {sc['expect']}")
            if sc["manual_only"]:
                _ok, hint = sc["check"](ws, 0, "", info)
                print(f"  {C.Y}manual:{C.X}    {hint}")
                ans = input(f"  Accept {sc['id']}? [y/n/s] ").strip().lower()
                return {"y": True, "n": False}.get(ans, None), "manual verdict"
            input(f"  {C.GREY}[enter to run]{C.X} ")
        rc, out = run_fc(target, args)
        if manual:
            print(_indent(out.strip()))
        ok, detail = sc["check"](ws, rc, out, info)
        if manual:
            ans = input(f"  Accept {sc['id']}? [y/n] (auto: {_verdict(ok)} — {detail}) ").strip().lower()
            if ans in ("y", "n"):
                ok = ans == "y"
        return ok, detail
    finally:
        if keep:
            print(f"  {C.GREY}kept: {ws}{C.X}")
        elif not manual:
            shutil.rmtree(ws, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", default=os.path.join(HERE, "blitcp.py"))
    ap.add_argument("--manual", action="store_true")
    ap.add_argument("--group", nargs="+",
                    choices=["local", "index", "ssh", "cloud", "smb", "info"])
    ap.add_argument("--only", nargs="+", metavar="ID")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args(argv)

    if args.list:
        for sc in SCENARIOS:
            tag = "" if not sc["manual_only"] else "  (manual-only)"
            print(f"  {sc['id']:<14} [{sc['group']:<5}] {sc['title']}{tag}")
        return 0

    target = os.path.abspath(args.target)
    if not os.path.isfile(target):
        print(f"{C.R}error:{C.X} target not found: {target}")
        return 2
    info_target[0] = target

    todo = SCENARIOS
    if args.group:
        todo = [s for s in todo if s["group"] in set(args.group)]
    if args.only:
        want = {s.upper() for s in args.only}
        todo = [s for s in todo if s["id"] in want]
    if not todo:
        print(f"{C.R}error:{C.X} no scenarios selected")
        return 2

    print(f"{C.B}UAT — blitcp{C.X}")
    print(f"  target: {target}")
    print(f"  mode:   {'MANUAL (interactive)' if args.manual else 'AUTO'}"
          f"   ssh-localhost: {'yes' if HAVE_SSH else 'no'}")

    npass = nfail = nskip = 0
    for sc in todo:
        ok, detail = _run_one(sc, target, args.manual, args.keep)
        if ok is True:
            npass += 1
        elif ok is None:
            nskip += 1
        else:
            nfail += 1
        if not args.manual:
            print(f"  {_verdict(ok)}  {sc['id']:<14} {sc['title']}")
            if detail and ok is not True:
                print(f"        {C.GREY}{detail}{C.X}")

    print(f"\n{C.B}{'='*64}{C.X}")
    verdict = (f"{C.R}UAT FAILED{C.X}" if nfail else f"{C.G}UAT PASSED{C.X}")
    print(f" {verdict} — {npass} pass, {nfail} fail, {nskip} skip")
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
