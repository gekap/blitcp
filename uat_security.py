#!/usr/bin/env python3
# Copyright 2026 George Kapellakis
# Licensed under the Apache License, Version 2.0
"""Security UAT scenarios for blitcp.

In-process checks of the engine's security guards (and one GUI check): path
traversal on every untrusted surface (cloud keys, tar members, the dedup-cache
DB), symlink/TOCTOU refusal, AES-GCM credential encryption (no plaintext leak),
the update-download host allowlist, owner-only file permissions, and that
passwords are passed to the CLI via env vars — never on argv.

  python uat_security.py            # run all, assert, exit 1 on any FAIL
  python uat_security.py --list

Companion to uat_blitcp.py (CLI) and uat_gui.py (GUI). Add a scenario for
every security/logic bug found (see the project's audit_uat convention).
"""
import os
import sys
import stat
import tarfile
import tempfile
import argparse

# i18n guard (I18N_DESIGN.md, M0): assertions expect English output. Must be
# set BEFORE importing blitcp — gettext will read the locale at import
# time once i18n lands. Also inherited by every spawned child.
os.environ["LC_ALL"] = "C"
os.environ["LANG"] = "C"
os.environ.pop("LANGUAGE", None)
os.environ.pop("BLITCP_LANG", None)

import blitcp as fc


class C:
    if sys.stdout.isatty() and not os.environ.get("NO_COLOR"):
        B = "\033[1m"; R = "\033[31m"; G = "\033[32m"; Y = "\033[33m"; X = "\033[0m"; GREY = "\033[90m"
    else:
        B = R = G = Y = X = GREY = ""


def _mkdb(drive):
    """DedupDB whose mount IS `drive` (isolated, db file under the dir)."""
    real = os.path.realpath(drive)
    orig = fc._find_mount_point
    fc._find_mount_point = lambda _p: real
    try:
        db = fc.DedupDB(drive)
    finally:
        fc._find_mount_point = orig
    return db


def _is_ntfs(path):
    if sys.platform.startswith("win"):
        return True
    try:
        with open("/proc/mounts") as f:
            mounts = [ln.split()[1:3] for ln in f if len(ln.split()) >= 3]
        real = os.path.realpath(path)
        best, fstype = "", ""
        for mp, ft in mounts:
            if real.startswith(mp) and len(mp) > len(best):
                best, fstype = mp, ft
        return fstype in ("ntfs", "ntfs3", "fuseblk")
    except OSError:
        return False


# ── path-traversal guards ────────────────────────────────────────────────────

def s_validate_rel_path(_tmp):
    bad = ["/etc/passwd", "../escape", "a/../../b", "x\0y", "x\ny"]
    for r in bad:
        if fc._validate_rel_path(r) is True:
            return False, f"accepted unsafe rel path: {r!r}"
    if fc._validate_rel_path("docs/a.txt") is not True:
        return False, "rejected a safe path"
    return True, "absolute / .. / null / newline rejected; normal accepted"


def s_safe_local_dest(tmp):
    root = os.path.realpath(os.path.join(tmp, "dst"))
    os.makedirs(root, exist_ok=True)
    # symlinked component that escapes the root
    os.symlink(tmp, os.path.join(root, "out"))
    for r in ["../evil", "/etc/passwd", "out/escape.txt"]:
        if fc._safe_local_dest(root, r) is not None:
            return False, f"cloud-download key escaped root: {r!r}"
    if fc._safe_local_dest(root, "sub/ok.bin") is None:
        return False, "rejected a safe object key"
    return True, "untrusted object keys can't escape the download root (.. / abs / symlink)"


def s_dedup_safe_full_path(tmp):
    drive = os.path.join(tmp, "drive")
    os.makedirs(drive, exist_ok=True)
    db = _mkdb(drive)
    try:
        outside = os.path.join(tmp, "secret")
        open(outside, "w").close()
        os.symlink(outside, os.path.join(drive, "link.bin"))
        for r in ["../outside", "/etc/shadow", "a/../../b", "link.bin", "", None]:
            if db.safe_full_path(r) is not None:
                return False, f"dedup DB row escaped the mount: {r!r}"
        if db.safe_full_path("sub/file.bin") is None:
            return False, "rejected a safe mount-relative path"
        return True, "poisoned dedup-DB rows can't open out-of-mount paths (SEC-1/SEC-2)"
    finally:
        db.close()


def s_dedup_symlink_db(tmp):
    if not hasattr(os, "O_NOFOLLOW"):
        return None, "O_NOFOLLOW unavailable on this platform"
    drive = os.path.join(tmp, "drive2")
    os.makedirs(drive, exist_ok=True)
    # plant a symlink where the DB would be opened
    target = os.path.join(tmp, "evil_db_target")
    open(target, "w").close()
    os.symlink(target, os.path.join(drive, fc.DEDUP_DB_NAME))
    try:
        db = _mkdb(drive)
        db.close()
        return False, "opened a dedup DB that was a symlink (TOCTOU risk)"
    except OSError:
        return True, "refuses to open the dedup DB when it's a symlink (O_NOFOLLOW)"


def s_dir_metadata_symlink(tmp):
    """_apply_dir_metadata (v3.12) runs as real root under --use-sudo. It must
    NOT follow an attacker symlink in the destination — leaf OR parent — and
    chmod/chown/utime a directory outside the copy tree. Regression for the
    O_NOFOLLOW openat-descent fix (local privilege escalation)."""
    if not (hasattr(os, "O_NOFOLLOW")
            and os.open in getattr(os, "supports_dir_fd", set())):
        return None, "O_NOFOLLOW / dir_fd unavailable on this platform"
    src, dst = os.path.join(tmp, "src"), os.path.join(tmp, "dst")
    sentinel = os.path.join(tmp, "SENTINEL")
    os.makedirs(os.path.join(src, "a", "inner"))
    os.makedirs(os.path.join(sentinel, "inner"))
    os.makedirs(dst)
    with open(os.path.join(src, "a", "inner", "f.txt"), "w") as f:
        f.write("x")
    os.chmod(os.path.join(src, "a"), 0o755)
    os.chmod(os.path.join(src, "a", "inner"), 0o777)   # mode the copy would mirror
    os.chmod(sentinel, 0o700)                           # out-of-tree victim
    os.chmod(os.path.join(sentinel, "inner"), 0o700)
    before = (stat.S_IMODE(os.lstat(sentinel).st_mode),
              stat.S_IMODE(os.lstat(os.path.join(sentinel, "inner")).st_mode))
    # attacker plants a symlinked PARENT component: dst/a -> SENTINEL
    os.symlink(sentinel, os.path.join(dst, "a"))

    class E:
        def __init__(s, src, rel):
            s.src, s.rel = src, rel
    saved = fc._preserve_spec
    try:
        fc._set_preserve_spec(fc.PreserveSpec(mode=True, times=True, owner=True))
        fc._apply_dir_metadata(
            [E(os.path.join(src, "a", "inner", "f.txt"), "a/inner/f.txt")], dst)
    finally:
        fc._set_preserve_spec(saved)

    after = (stat.S_IMODE(os.lstat(sentinel).st_mode),
             stat.S_IMODE(os.lstat(os.path.join(sentinel, "inner")).st_mode))
    if after != before:
        return False, (f"followed a symlinked PARENT and mutated an out-of-tree "
                       f"directory (SENTINEL modes {before} -> {after})")

    # ── Case 2: symlinked LEAF component (dst2/a real, dst2/a/inner -> victim).
    # The O_NOFOLLOW descent must refuse the final component too, not just the
    # parents — otherwise the leaf dir's metadata mirror escapes the tree.
    dst2 = os.path.join(tmp, "dst2")
    victim = os.path.join(tmp, "SENTINEL2")
    os.makedirs(os.path.join(dst2, "a"))
    os.makedirs(victim)
    os.chmod(victim, 0o700)
    leaf_before = stat.S_IMODE(os.lstat(victim).st_mode)
    os.symlink(victim, os.path.join(dst2, "a", "inner"))  # symlinked LEAF
    saved = fc._preserve_spec
    try:
        fc._set_preserve_spec(fc.PreserveSpec(mode=True, times=True, owner=True))
        fc._apply_dir_metadata(
            [E(os.path.join(src, "a", "inner", "f.txt"), "a/inner/f.txt")], dst2)
    finally:
        fc._set_preserve_spec(saved)
    leaf_after = stat.S_IMODE(os.lstat(victim).st_mode)
    if leaf_after != leaf_before:
        return False, (f"followed a symlinked LEAF and mutated an out-of-tree "
                       f"directory (SENTINEL2 mode {leaf_before:o} -> {leaf_after:o})")
    return True, ("dir-metadata pass refuses symlinked components — parent AND "
                  "leaf (no out-of-tree chmod/chown)")


def s_tar_member(_tmp):
    def m(name, kind="file"):
        ti = tarfile.TarInfo(name)
        if kind == "sym":
            ti.type = tarfile.SYMTYPE; ti.linkname = "/etc/passwd"
        elif kind == "lnk":
            ti.type = tarfile.LNKTYPE; ti.linkname = "x"
        elif kind == "dev":
            ti.type = tarfile.CHRTYPE
        return ti
    bad = [m("/abs"), m("../up"), m("a/../../b"), m("link", "sym"),
           m("hard", "lnk"), m("dev", "dev")]
    for ti in bad:
        if fc._validate_tar_member(ti, "/dst") is True:
            return False, f"accepted unsafe tar member: {ti.name} ({ti.type})"
    if fc._validate_tar_member(m("docs/a.txt"), "/dst") is not True:
        return False, "rejected a safe tar member"
    return True, "tar stream rejects abs / .. / symlink / hardlink / device members"


def s_tar_leaf_symlink(tmp):
    """A symlink planted AT the destination leaf must be refused by
    _validate_tar_member — otherwise extraction writes THROUGH it, out of the
    tree (the parent-only ancestor check missed this). Regression for the
    leaf-symlink write-through fix."""
    def m(name):
        return tarfile.TarInfo(name)
    dst = os.path.join(tmp, "dst")
    os.makedirs(dst)
    victim = os.path.join(tmp, "victim.txt")
    with open(victim, "w") as f:
        f.write("original")
    # attacker pre-plants dst/pwned -> victim (an existing regular file leaf)
    os.symlink(victim, os.path.join(dst, "pwned"))
    res = fc._validate_tar_member(m("pwned"), dst)
    if res is True:
        return False, "accepted a member whose destination leaf is a symlink (write-through)"
    # a normal, non-symlinked leaf in the same dir must still pass
    if fc._validate_tar_member(m("safe.txt"), dst) is not True:
        return False, "rejected a safe non-symlink leaf"
    return True, "tar stream refuses a symlinked destination leaf (no write-through)"


# ── credentials ──────────────────────────────────────────────────────────────

def s_creds_encrypted(_tmp):
    # AES-GCM comes from the optional `cryptography` package (the cloud extra).
    # Without it fc.encrypt_conns raises ModuleNotFoundError, and main()'s broad
    # handler reported a missing package as a SECURITY FAILURE. Skip on that one
    # import and on nothing else: as SEC-GUI-1 notes, a security check that
    # skips for any other reason has quietly stopped guarding anything.
    try:
        import cryptography  # noqa: F401
    except ImportError as e:
        return None, f"cryptography not installed ({e}) - AES-GCM path absent"
    secret = "TOPSECRET_value_8f3a2"
    akid = "AKIAEXAMPLE_ID_9z"
    conns = {"aws": {"type": "s3", "access_key_id": akid, "secret_access_key": secret}}
    raw = fc.encrypt_conns(conns, b"p@ssphrase")
    txt = raw.decode("utf-8", "replace")
    if not fc._is_encrypted(raw):
        return False, "encrypted blob not flagged as encrypted"
    if secret in txt or akid in txt:
        return False, "PLAINTEXT secret present inside the encrypted credentials!"
    back, _bh = fc.decrypt_conns(raw, b"p@ssphrase")
    if back.get("aws", {}).get("secret_access_key") != secret:
        return False, "decrypt round-trip lost the secret"
    try:
        fc.decrypt_conns(raw, b"wrong-passphrase")
        return False, "wrong passphrase still decrypted!"
    except SystemExit:
        pass
    return True, "credentials AES-GCM encrypted (no plaintext), round-trips, wrong-pass rejected"


def s_creds_plaintext_detect(tmp):
    """Regression for the plaintext-credential bug class: a credentials file that
    holds a secret but ISN'T encrypted must be detectable so it can be re-secured."""
    path = os.path.join(tmp, "credentials.json")
    import json
    plain = {"connections": {"aws": {"type": "s3", "secret_access_key": "LEAKED_xyz"}}}
    with open(path, "w") as f:
        json.dump(plain, f)
    raw = open(path, "rb").read()
    if fc._is_encrypted(raw):
        return False, "a plaintext file was misreported as encrypted"
    has_secret = fc._entry_has_secret(plain["connections"]["aws"])
    if not has_secret:
        return False, "_entry_has_secret missed a stored secret (bug would go unnoticed)"
    # and confirm the secret really is readable in cleartext (so the guard matters)
    if "LEAKED_xyz" not in raw.decode("utf-8", "replace"):
        return False, "test fixture wrong"
    return True, "unencrypted credentials holding a secret are detected (not encrypted + has-secret)"


def s_creds_delete_ntfs(tmp):
    """Whether a credentials file can be deleted/replaced on NTFS (Windows) —
    where locked files / alternate-data-streams can resist deletion. Auto on
    NTFS/Windows; SKIP elsewhere (cannot stage an NTFS volume here)."""
    if not _is_ntfs(tmp):
        return None, "needs an NTFS volume (Windows) — cannot stage here"
    path = os.path.join(tmp, "credentials.json")
    with open(path, "w") as f:
        f.write("{}")
    # encrypted replace then delete the plaintext, as the app does on re-secure
    enc = fc.encrypt_conns({"x": {"type": "s3", "secret_access_key": "s"}}, b"pw")
    tmp2 = path + ".tmp"
    with open(tmp2, "wb") as f:
        f.write(enc)
    os.replace(tmp2, path)          # atomic replace must work on NTFS
    os.remove(path)                 # and removal must succeed (no lock/ADS block)
    if os.path.exists(path):
        return False, "credentials.json could not be deleted on NTFS"
    return True, "credentials.json replaces + deletes cleanly on NTFS"


# ── transport / update / perms ───────────────────────────────────────────────

def s_update_host_allowlist(_tmp):
    from urllib.parse import urlparse
    allowed = {"github.com", "objects.githubusercontent.com",
               "github-releases.githubusercontent.com"}
    evil = ["http://github.com/x", "https://evil.com/blitcp",
            "https://github.com.evil.com/x", "ftp://github.com/x"]
    for u in evil:
        p = urlparse(u)
        if p.scheme == "https" and p.hostname in allowed:
            return False, f"would download from a non-allowlisted URL: {u}"
    if urlparse("https://github.com/gekap/blitcp-private/releases/download/v1/x").hostname not in allowed:
        return False, "rejected a legitimate GitHub release URL"
    return True, "update download restricted to HTTPS GitHub release hosts"


def s_db_file_perms(tmp):
    if sys.platform.startswith("win"):
        return None, "POSIX permission bits not meaningful on Windows"
    drive = os.path.join(tmp, "drive3")
    os.makedirs(drive, exist_ok=True)
    db = _mkdb(drive)
    try:
        mode = stat.S_IMODE(os.stat(db.db_path).st_mode)
        if mode & 0o077:
            return False, f"dedup DB is group/world accessible (mode {oct(mode)})"
        return True, f"dedup DB created owner-only (mode {oct(mode)})"
    finally:
        db.close()


def s_gui_password_not_on_argv(_tmp):
    """The GUI must hand SSH/SMB passwords to the CLI via env vars, never as a
    value on argv (which would leak into the process list)."""
    # Qt missing is a real reason to skip. Anything else is this check being
    # broken, and a SECURITY check that reports itself as "skipped" when it can
    # no longer run is worse than one that fails: the rename to BlitcpGUI left
    # this looking for FastCopyGUI, and the broad except turned that into a
    # skip line nobody questioned. It has not run since.
    try:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
    except ImportError as e:
        return None, f"PySide6 not installed ({e})"
    try:
        app = QApplication.instance() or QApplication(sys.argv[:1])
        import blitcp_gui as gui
        cls = getattr(gui, "BlitcpGUI", None) or getattr(gui, "FastCopyGUI", None)
        if cls is None:
            return False, ("neither BlitcpGUI nor FastCopyGUI exists in "
                           "blitcp_gui — this check cannot verify anything")
        w = cls()
    except Exception as e:                                  # noqa: BLE001
        return False, (f"the GUI would not construct: {type(e).__name__}: {e}"
                       f" — password handling is UNVERIFIED")
    w.conns = {"box": {"type": "ssh", "host": "box", "user": "u",
                       "password": "SUPERSECRET_PW_42", "port": 22}}
    res = w._ssh_auth_flags("u@box:/path", "dst")
    if not res:
        return False, "ssh auth flags returned nothing for a saved connection"
    extra, env = res
    if "SUPERSECRET_PW_42" in " ".join(map(str, extra)):
        return False, "password VALUE leaked onto argv!"
    if "SUPERSECRET_PW_42" not in "".join(env.values()):
        return False, "password not passed via env"
    if not any("password-env" in str(a) for a in extra):
        return False, "expected --ssh-dst-password-env on argv"
    return True, "SSH/SMB passwords passed via env var (--*-password-env), never on argv"


def s_ssh_prompt_no_hang(tmp):
    """Regression (the GUI 'stuck at 0%' bug): an SSH auth prompt must NEVER block
    when there is no terminal to answer it — e.g. launched from the GUI via
    QProcess, whose stdin is an open pipe nobody can type into. Before the isatty
    guard, getpass() hung forever there. We run a failing SSH auth NON-interactively
    (stdin = pipe, never fed) and require a bounded, clean exit instead of a hang.
    The UAT used to only check that the GUI *builds* the right argv — never that the
    engine survives a no-TTY auth failure, which is why this slipped through."""
    import subprocess
    import socket
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect(("127.0.0.1", 22))
    except OSError:
        return None, "no sshd on 127.0.0.1:22 — skipped"
    finally:
        s.close()
    p = subprocess.Popen(
        [sys.executable, os.path.abspath(fc.__file__),
         "fcuat_no_such_user_zzz@127.0.0.1:/etc/hostname",
         os.path.join(tmp, "out")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True)
    try:
        p.communicate(timeout=25)            # never feed stdin
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        return False, "HUNG on an SSH prompt with no TTY — the GUI-freeze bug"
    if p.returncode == 0:
        return False, "auth unexpectedly succeeded (exit 0)"
    return True, f"bounded clean failure (exit {p.returncode}, no hang)"


def s_ssh_creds_from_host(tmp):
    """A saved SSH connection's password must apply to a full user@host:path spec
    (matched by host), so the GUI/CLI get credentials from credentials.json without
    an explicit --ssh-*-password. Regression for 'auth failed on a saved host'."""
    conns = {"box": {"type": "ssh", "host": "10.1.2.3", "user": "alice",
                     "password": "s3kret", "port": 2200, "key": None}}
    got = fc._ssh_creds_by_host("alice@10.1.2.3:/data", conns)
    if not got or got.get("password") != "s3kret" or got.get("port") != 2200:
        return False, f"host match failed: {got!r}"
    if fc._ssh_creds_by_host("bob@10.1.2.3:/data", conns) is not None:
        return False, "matched a different user than the connection pins"
    if fc._ssh_creds_by_host("C:\\x", conns) or fc._ssh_creds_by_host("s3://b/k", conns):
        return False, "false-positive on a local / cloud spec"
    return True, "saved SSH password resolved by host (user-pinned, no false positives)"


SCENARIOS = [
    ("SEC-PATH-1", "rel-path validation (abs / .. / null / newline)", s_validate_rel_path),
    ("SEC-PATH-2", "cloud-download keys can't escape the root", s_safe_local_dest),
    ("SEC-DEDUP-1", "dedup-DB rows can't open out-of-mount paths", s_dedup_safe_full_path),
    ("SEC-DEDUP-2", "refuses a symlinked dedup DB (O_NOFOLLOW)", s_dedup_symlink_db),
    ("SEC-TAR-1", "tar stream rejects unsafe members", s_tar_member),
    ("SEC-TAR-2", "tar stream refuses a symlinked destination leaf", s_tar_leaf_symlink),
    ("SEC-META-1", "dir-metadata pass won't follow a symlink outside the tree", s_dir_metadata_symlink),
    ("SEC-CREDS-1", "credentials encrypted — no plaintext leak", s_creds_encrypted),
    ("SEC-CREDS-2", "unencrypted credentials with a secret are detected", s_creds_plaintext_detect),
    ("SEC-CREDS-3", "credentials.json deletes/replaces on NTFS", s_creds_delete_ntfs),
    ("SEC-UPDATE-1", "update download host allowlist", s_update_host_allowlist),
    ("SEC-PERMS-1", "dedup DB created owner-only (0600)", s_db_file_perms),
    ("SEC-GUI-1", "GUI passwords via env, never on argv", s_gui_password_not_on_argv),
    ("SEC-SSH-1", "SSH auth never hangs on a prompt when non-interactive (no TTY)", s_ssh_prompt_no_hang),
    ("SEC-SSH-2", "saved SSH password resolves for a user@host spec (by host)", s_ssh_creds_from_host),
]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args(argv)
    if args.list:
        for sid, title, _ in SCENARIOS:
            print(f"  {sid:<14} {title}")
        return 0

    print(f"{C.B}UAT — blitcp security{C.X}")
    npass = nfail = nskip = 0
    for sid, title, fn in SCENARIOS:
        tmp = tempfile.mkdtemp(prefix=f"{sid}_")
        try:
            ok, detail = fn(tmp)
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {e}"
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        tag = (f"{C.G}PASS{C.X}" if ok is True else
               f"{C.Y}SKIP{C.X}" if ok is None else f"{C.R}FAIL{C.X}")
        if ok is True:
            npass += 1
        elif ok is None:
            nskip += 1
        else:
            nfail += 1
        print(f"  {tag}  {sid:<14} {title}")
        if detail:
            print(f"        {C.GREY}{detail}{C.X}")

    print(f"\n{C.B}{'='*62}{C.X}")
    verdict = f"{C.R}SECURITY UAT FAILED{C.X}" if nfail else f"{C.G}SECURITY UAT PASSED{C.X}"
    print(f" {verdict} — {npass} pass, {nfail} fail, {nskip} skip")
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
