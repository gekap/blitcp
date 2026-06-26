#!/usr/bin/env python3
# Copyright 2026 George Kapellakis
# Licensed under the Apache License, Version 2.0
# See LICENSE file for details.
"""
FAST BLOCK-ORDER COPY — Copies files and folders at maximum speed.

Supports local, SSH, and cloud object-storage endpoints in any combination:
  • local  → local   (block-order copy with dedup)
  • local  ↔ remote  (SFTP + tar stream over SSH)
  • remote → remote  (relay through local machine: src SSH → dst SSH)
  • local  ↔ cloud   (s3:// / az:// / gs:// object storage)

Features:
  • Reads files in PHYSICAL disk order (eliminates random seeks)
  • Pre-flight space check (compares source size vs destination free space)
  • Content-aware deduplication (hashes files, copies each unique file once,
    hard-links duplicates — like Dell's backup dedup)
  • Cross-run dedup database (SQLite cache at destination — skips re-hashing
    unchanged files, detects content already on destination from prior runs)
  • Strong hashing (xxh128 / SHA-256 fallback) for collision safety
  • Large I/O buffers (64MB default)
  • Post-copy verification
  • SSH remote support via paramiko (SFTP + tar streaming)
  • Incremental sync — skips files already present and identical
  • Small-file bundling via tar pipe for fast network transfers

Usage, options, subcommands (creds, ls), and examples:
  python fast_copy.py --help

Requires: python -m pip install paramiko
"""

import os
import sys
import warnings

# The cryptography package prints a CryptographyDeprecationWarning to stderr at
# import time when running on an end-of-life Python (e.g. the Python 3.8 that
# the frozen Windows build is compiled against). It is noise the user can do
# nothing about, and it pollutes clean commands like --version. Silence it
# before any import can trigger it; the message text is matched so unrelated
# warnings still surface.
warnings.filterwarnings(
    "ignore",
    message=r"Python 3\.\d+ is no longer supported",
)
warnings.filterwarnings("ignore", module=r"cryptography(\..*)?")

# Install a quiet excepthook before any heavy imports so that pressing Ctrl+C
# during module loading prints a one-line message instead of a 12-frame
# traceback through Python's import machinery.
def _quiet_excepthook(exctype, value, tb):
    if issubclass(exctype, KeyboardInterrupt):
        try:
            sys.stderr.write("\n  Interrupted.\n")
        except Exception:
            pass
        sys.exit(130)
    sys.__excepthook__(exctype, value, tb)
sys.excepthook = _quiet_excepthook

# Windows consoles often default to a legacy codepage (e.g. cp1253 on Greek
# systems) that cannot encode the box-drawing / progress glyphs we print. That
# would crash mid-run with a UnicodeEncodeError instead of copying. Switch the
# console to UTF-8 and force UTF-8 on our streams with replacement, so output is
# always safe: modern terminals render it correctly, legacy ones degrade to a
# replacement char instead of aborting.
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

import stat
import time
import glob as globmod
import struct
import ctypes
import shutil
import hashlib
import tarfile
import io
import json
import base64
import sqlite3
import tempfile
import re
import textwrap
import atexit
import getpass
import posixpath
import shlex
import fnmatch
import argparse
import platform
import threading
from pathlib import Path
import errno
from collections import namedtuple, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# ════════════════════════════════════════════════════════════════════════════
# VERSION
# ════════════════════════════════════════════════════════════════════════════
__version__ = "3.7.5"
GITHUB_REPO = "gekap/fast-copy"

# ════════════════════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════════════════════
DEFAULT_BUFFER_MB = 64
DEFAULT_THREADS = 4
# Object storage has no tar-pipe equivalent: every file is a separate PUT/GET,
# dominated by request latency. Overlapping many in-flight requests is the only
# small-file win available, so cloud concurrency defaults higher than --threads.
DEFAULT_CLOUD_CONCURRENCY = 16
HASH_CHUNK = 1048571            # ~1MB chunks for hashing (prime for alignment)
HASH_ALGO = "xxh128"            # try xxhash first, fallback to sha256

FileEntry = namedtuple(
    "FileEntry",
    ["src", "rel", "size", "physical_offset", "content_hash", "alloc_size"],
    defaults=[None],  # alloc_size=None means "dense" (same as size)
)


def _detect_sparse_alloc(st):
    """Return on-disk allocated bytes if file is sparse, else None.

    A file is "sparse" when its allocated blocks occupy noticeably less space
    than its declared size — common for VM disk images and Longhorn replica
    `volume-head-*.img` files. We allow a 4KB slack for filesystem overhead
    so we don't flag normal files with one-block inline data."""
    blocks = getattr(st, "st_blocks", None)
    if blocks is None:
        return None
    allocated = blocks * 512
    if allocated + 4096 < st.st_size:
        return allocated
    return None


def _effective_alloc(entry):
    """Return the on-disk bytes this entry will need on a sparse-capable FS."""
    return entry.alloc_size if entry.alloc_size is not None else entry.size

# ════════════════════════════════════════════════════════════════════════════
# STRUCTURED LOG — collects per-file actions for --log-file output
# ════════════════════════════════════════════════════════════════════════════
_log_entries = []
_log_enabled = False
_log_lock = threading.Lock()


def _log(action, rel_path, size, **extra):
    """Append a log entry if logging is enabled. Thread-safe."""
    if not _log_enabled:
        return
    entry = {"action": action, "path": rel_path, "size": size}
    entry.update(extra)
    with _log_lock:
        _log_entries.append(entry)


def write_log_file(path, summary):
    """Write JSON log with per-file entries and summary."""
    import datetime
    log = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "summary": summary,
        "files": list(_log_entries),
    }
    with open(path, "w") as f:
        json.dump(log, f, indent=2)
    _log_entries.clear()
    print(f"  Log:     {C.BOLD}{path}{C.RESET}")


SUDO_AUDIT_FILE = ".fast_copy_audit.jsonl"


def _is_elevated():
    """True when running with elevated privileges (sudo or direct root)."""
    if os.environ.get("SUDO_USER"):
        return True
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _safe_open_read_fd(path):
    """Open a file for reading.

    When elevated (sudo / euid==0), use O_NOFOLLOW so that a TOCTOU race
    between scan and copy cannot redirect the read through a symlink — a
    non-root attacker who plants `<src>/leak -> /etc/shadow` must not be
    able to exfiltrate root-readable files.

    When not elevated, open without O_NOFOLLOW so in-tree symlinks (which
    the scan filter explicitly allows for non-elevated runs) still resolve
    to their target content."""
    flags = os.O_RDONLY
    if _is_elevated() and hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return os.open(path, flags)


def _safe_open_write_fd(path, truncate=True):
    """Open a destination file for writing, refusing to follow a symlink.

    Prevents an attacker who can write the destination directory from
    pre-planting `<dst>/file -> /root/.bashrc` and tricking root into
    overwriting an arbitrary file. Returns an open fd; caller wraps with
    os.fdopen() and is responsible for closing."""
    flags = os.O_WRONLY | os.O_CREAT
    flags |= (os.O_TRUNC if truncate else 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return os.open(path, flags, 0o600)


class PreserveSpec:
    """Which metadata kinds to copy alongside file bytes.

    The CLI builds one of these from --preserve TOKENS where TOKENS is a
    comma-separated subset of: mode, times, owner, xattr, acl, all, none.
    Defaults to mode+times (matches v3.1.x behavior); --use-sudo promotes
    it to 'all' unless --preserve was passed explicitly."""

    KINDS = ("mode", "times", "owner", "xattr", "acl")

    def __init__(self, mode=True, times=True, owner=False, xattr=False, acl=False):
        self.mode = mode
        self.times = times
        self.owner = owner
        self.xattr = xattr
        self.acl = acl

    @classmethod
    def from_tokens(cls, raw):
        if raw is None:
            return cls()
        tokens = [t.strip().lower() for t in raw.split(",") if t.strip()]
        spec = cls(mode=False, times=False)
        for t in tokens:
            if t == "none":
                return cls(mode=False, times=False)
            if t == "all":
                return cls(mode=True, times=True, owner=True, xattr=True, acl=True)
            if t in cls.KINDS:
                setattr(spec, t, True)
            else:
                raise ValueError(f"unknown --preserve token: {t!r} "
                                 f"(known: {', '.join(cls.KINDS + ('all', 'none'))})")
        return spec

    def any_extended(self):
        """True if any beyond-mode-times kind is requested."""
        return self.owner or self.xattr or self.acl

    def __repr__(self):
        on = [k for k in self.KINDS if getattr(self, k)]
        return f"PreserveSpec({','.join(on) or 'none'})"


_preserve_spec = PreserveSpec()
_preserve_stats = {
    "xattr_ok": 0, "xattr_skip_unsupported": 0, "xattr_err": 0,
    "acl_ok": 0,   "acl_skip_unsupported": 0,   "acl_err": 0,
    "owner_ok": 0, "owner_skip_unprivileged": 0, "owner_err": 0,
    # Captured most-recent error string from a Windows SD operation, surfaced
    # in the end-of-run summary so users see WHY ACL set failed (winerror=…).
    "_last_acl_err": None,
}
_preserve_dst_caps = {"xattr": None, "acl": None}  # None=unknown, True/False after probe


def _set_preserve_spec(spec):
    """Module-wide singleton; set once from main() after argparse."""
    global _preserve_spec
    _preserve_spec = spec


def _is_elevated_for_preserve():
    """Stricter than _is_elevated(): chown only works as real root."""
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _probe_dst_xattr_support(dst_root):
    """Cache + return whether dst can store extended attributes.

    Linux/macOS: tries os.setxattr on a probe file. FAT32/exFAT fail; ext4,
    btrfs, XFS, APFS succeed.

    Windows: tries to write an Alternate Data Stream on a probe file. NTFS
    supports ADS; ReFS supports it with limits; FAT32/exFAT don't. ADS is
    the NTFS analog of POSIX xattrs from --preserve's point of view."""
    if _preserve_dst_caps["xattr"] is not None:
        return _preserve_dst_caps["xattr"]

    if _system == "Windows":
        try:
            os.makedirs(dst_root, exist_ok=True)
            fd, probe = tempfile.mkstemp(prefix=".fc_xattr_probe_", dir=dst_root)
            os.close(fd)
            try:
                with open(probe + ":fc.probe", "wb") as f:
                    f.write(b"1")
                _preserve_dst_caps["xattr"] = True
            finally:
                try:
                    os.remove(probe)
                except OSError:
                    pass
        except (OSError, AttributeError):
            _preserve_dst_caps["xattr"] = False
        return _preserve_dst_caps["xattr"]

    if not hasattr(os, "setxattr"):
        _preserve_dst_caps["xattr"] = False
        return False
    try:
        os.makedirs(dst_root, exist_ok=True)
        fd, probe = tempfile.mkstemp(prefix=".fc_xattr_probe_", dir=dst_root)
        try:
            os.close(fd)
            os.setxattr(probe, "user.fast_copy.probe", b"1")
            os.removexattr(probe, "user.fast_copy.probe")
            _preserve_dst_caps["xattr"] = True
        finally:
            try:
                os.remove(probe)
            except OSError:
                pass
    except (OSError, AttributeError):
        _preserve_dst_caps["xattr"] = False
    return _preserve_dst_caps["xattr"]


def _probe_dst_acl_support(dst_root):
    """Cache + return whether the destination filesystem can carry ACLs.

      • Linux: setfacl shell-out probe (POSIX 1e ACLs).
      • macOS: chmod +a probe (NFSv4-style ACLs).
      • Windows: win32security GetNamedSecurityInfo probe (NTFS / ReFS DACL).
      • Other: not supported, returns False."""
    if _preserve_dst_caps["acl"] is not None:
        return _preserve_dst_caps["acl"]

    if _system == "Windows":
        # NTFS / ReFS both expose a Security Descriptor on every file.
        # FAT32 / exFAT do not. Probe by trying to GET the SD on a tempfile
        # — that's a read-only check, no privileges needed.
        try:
            import win32security
        except ImportError:
            _preserve_dst_caps["acl"] = False
            return False
        try:
            os.makedirs(dst_root, exist_ok=True)
            fd, probe = tempfile.mkstemp(prefix=".fc_acl_probe_", dir=dst_root)
            os.close(fd)
            try:
                SE_FILE_OBJECT = 1
                # Just attempt to read the DACL — if the FS doesn't support
                # security descriptors at all, this errors out cleanly.
                win32security.GetNamedSecurityInfo(
                    probe, SE_FILE_OBJECT,
                    win32security.DACL_SECURITY_INFORMATION,
                )
                _preserve_dst_caps["acl"] = True
            finally:
                try:
                    os.remove(probe)
                except OSError:
                    pass
        except Exception:
            _preserve_dst_caps["acl"] = False
        return _preserve_dst_caps["acl"]

    if _system not in ("Linux", "Darwin"):
        _preserve_dst_caps["acl"] = False
        return False
    try:
        import subprocess
        os.makedirs(dst_root, exist_ok=True)
        fd, probe = tempfile.mkstemp(prefix=".fc_acl_probe_", dir=dst_root)
        os.close(fd)
        try:
            if _system == "Linux":
                r = subprocess.run(["setfacl", "-m", "u::rw", probe],
                                   capture_output=True, timeout=5)
            else:  # Darwin
                # On macOS we just check that `chmod +a` is callable and the
                # underlying FS accepts an ACE. Use the current user so the
                # operation has a chance of succeeding without elevated perms.
                me = os.environ.get("USER") or os.environ.get("LOGNAME") or "nobody"
                r = subprocess.run(["chmod", "+a",
                                    f"{me} allow read", probe],
                                   capture_output=True, timeout=5)
            _preserve_dst_caps["acl"] = (r.returncode == 0)
        finally:
            try:
                os.remove(probe)
            except OSError:
                pass
    except (OSError, subprocess.SubprocessError, FileNotFoundError):
        _preserve_dst_caps["acl"] = False
    return _preserve_dst_caps["acl"]


def _copy_xattrs(src_path, dst_path):
    """Copy extended attributes from src_path to dst_path.

    Per-attr OSErrors are counted but do not stop the copy. Returns True if
    any xattr was successfully written (counts toward 'preserved'), False
    if the file had no xattrs to copy."""
    if not hasattr(os, "listxattr"):
        return None
    try:
        names = os.listxattr(src_path, follow_symlinks=False)
    except OSError:
        _preserve_stats["xattr_err"] += 1
        return False
    if not names:
        return None
    wrote_any = False
    for name in names:
        try:
            value = os.getxattr(src_path, name, follow_symlinks=False)
            os.setxattr(dst_path, name, value, follow_symlinks=False)
            wrote_any = True
        except OSError as e:
            # ENOTSUP: destination FS doesn't support xattrs at all.
            # EPERM: trusted/system xattr requires root.
            if e.errno in (errno.ENOTSUP, errno.EOPNOTSUPP):
                _preserve_stats["xattr_skip_unsupported"] += 1
                return False
            _preserve_stats["xattr_err"] += 1
    if wrote_any:
        _preserve_stats["xattr_ok"] += 1
    return wrote_any


def _copy_posix_acls(src_path, dst_path):
    """Copy POSIX-style ACLs from src to dst.

    Dispatches by platform:
      • Linux: getfacl/setfacl shell-out (mature, tested).
      • macOS: chmod +a / ls -le shell-out (experimental — see
        _copy_acls_macos for the caveats).
      • Other (Windows/BSD): not supported; returns None silently."""
    if _preserve_dst_caps["acl"] is False:
        _preserve_stats["acl_skip_unsupported"] += 1
        return False
    if _system == "Linux":
        return _copy_acls_linux(src_path, dst_path)
    if _system == "Darwin":
        return _copy_acls_macos(src_path, dst_path)
    return None


def _copy_acls_linux(src_path, dst_path):
    """Linux POSIX 1e ACLs via getfacl/setfacl. Same code as v3.2.0 dev built."""
    try:
        import subprocess
        # -p: don't strip leading / from paths. -E: numeric uid/gid (portable).
        get = subprocess.run(["getfacl", "-p", "-E", "--", src_path],
                             capture_output=True, timeout=10)
        if get.returncode != 0 or not get.stdout.strip():
            return None
        # Rewrite the "# file:" header so setfacl applies to dst_path.
        text = get.stdout.decode("utf-8", errors="replace")
        out_lines = []
        for line in text.splitlines():
            if line.startswith("# file:"):
                out_lines.append(f"# file: {dst_path}")
            else:
                out_lines.append(line)
        rewritten = "\n".join(out_lines) + "\n"
        put = subprocess.run(["setfacl", "--restore=-"],
                             input=rewritten.encode("utf-8"),
                             capture_output=True, timeout=10)
        if put.returncode == 0:
            _preserve_stats["acl_ok"] += 1
            return True
        _preserve_stats["acl_err"] += 1
        return False
    except (OSError, subprocess.SubprocessError, FileNotFoundError):
        _preserve_stats["acl_err"] += 1
        return False


def _list_ntfs_streams_windows(path):
    """Enumerate Alternate Data Streams on an NTFS file.

    Returns a list of stream names (without the file name; e.g.
    [":Zone.Identifier:$DATA", ":com.dropbox.attributes:$DATA"]).
    The default $DATA stream (the file's main content) is excluded —
    that's already handled by the normal copy.

    Uses kernel32.FindFirstStreamW directly via ctypes so this code path
    doesn't require pywin32 (ADS is a real Windows file-system feature
    that Python's stdlib doesn't expose; ctypes is the minimum cost)."""
    if _system != "Windows":
        return []
    import ctypes
    from ctypes import wintypes

    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
    ERROR_HANDLE_EOF = 38

    class WIN32_FIND_STREAM_DATA(ctypes.Structure):
        _fields_ = [
            ("StreamSize", ctypes.c_longlong),  # LARGE_INTEGER
            ("cStreamName", ctypes.c_wchar * (260 + 36)),  # MAX_PATH + 36
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.FindFirstStreamW.argtypes = [
        wintypes.LPCWSTR, ctypes.c_int,
        ctypes.POINTER(WIN32_FIND_STREAM_DATA), wintypes.DWORD,
    ]
    kernel32.FindFirstStreamW.restype = wintypes.HANDLE
    kernel32.FindNextStreamW.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(WIN32_FIND_STREAM_DATA),
    ]
    kernel32.FindNextStreamW.restype = wintypes.BOOL
    kernel32.FindClose.argtypes = [wintypes.HANDLE]
    kernel32.FindClose.restype = wintypes.BOOL

    streams = []
    data = WIN32_FIND_STREAM_DATA()
    # STREAM_INFO_LEVELS.FindStreamInfoStandard = 0
    handle = kernel32.FindFirstStreamW(path, 0, ctypes.byref(data), 0)
    if handle == INVALID_HANDLE_VALUE:
        return streams
    try:
        while True:
            name = data.cStreamName
            # The default $DATA stream is just "::$DATA" — that's the file's
            # main content, skip it.
            if name and name != "::$DATA":
                streams.append(name)
            ok = kernel32.FindNextStreamW(handle, ctypes.byref(data))
            if not ok:
                err = ctypes.get_last_error()
                if err == ERROR_HANDLE_EOF:
                    break
                break  # any other error: stop enumeration silently
    finally:
        kernel32.FindClose(handle)
    return streams


def _copy_ads_windows(src_path, dst_path):
    """Copy NTFS Alternate Data Streams from src to dst.

    Each non-default stream (e.g. `:Zone.Identifier` from browser MOTW)
    is opened via the standard Python `open()` against `path:streamname`,
    which Windows resolves natively. Errors per stream are counted; the
    main file copy is unaffected.

    Validated on Windows 10/11 + NTFS with pywin32 — round-trips
    Zone.Identifier (MOTW) and multi-byte stream names. Returns None on
    non-Windows, True if any stream was copied, False on read/write failure."""
    if _system != "Windows":
        return None
    if _preserve_dst_caps["xattr"] is False:
        _preserve_stats["xattr_skip_unsupported"] += 1
        return False

    try:
        streams = _list_ntfs_streams_windows(src_path)
    except OSError:
        _preserve_stats["xattr_err"] += 1
        return False

    if not streams:
        return None

    wrote_any = False
    for stream in streams:
        # Stream names come back like ":Zone.Identifier:$DATA". Strip the
        # ":$DATA" type suffix; we'll re-address as path + ":Name" which
        # Windows interprets as the same stream.
        name = stream
        if name.endswith(":$DATA"):
            name = name[: -len(":$DATA")]
        if not name.startswith(":"):
            name = ":" + name
        src_full = src_path + name
        dst_full = dst_path + name
        try:
            with open(src_full, "rb") as fin, open(dst_full, "wb") as fout:
                while True:
                    chunk = fin.read(1 << 20)
                    if not chunk:
                        break
                    fout.write(chunk)
            wrote_any = True
        except OSError:
            _preserve_stats["xattr_err"] += 1

    if wrote_any:
        _preserve_stats["xattr_ok"] += 1
    return wrote_any


def _copy_acls_windows(src_path, dst_path, set_owner=False, set_dacl=True):
    """Copy NTFS Security Descriptor parts (owner + group + DACL) from src to dst.

    Uses pywin32's win32security GetNamedSecurityInfo / SetNamedSecurityInfo.

    Important design point: **owner-set and DACL-set are issued as two
    separate calls**, NOT one combined call. Owner-set requires
    SeRestorePrivilege / SeTakeOwnershipPrivilege; if those aren't held it
    fails with ERROR_INVALID_OWNER (1307) or ERROR_PRIVILEGE_NOT_HELD (1314).
    A combined call would take the DACL portion down with the owner failure
    — splitting means a non-admin's DACL still gets applied even when
    ownership preservation can't happen.

    SACL (audit ACL) is intentionally skipped — requires SE_SECURITY_NAME,
    rarely useful for backups.

    Validated on Windows 10/11 + NTFS with pywin32: round-trips explicit
    DACL entries (e.g. Everyone:Read ACE) and owner SID when the running
    process has SeRestorePrivilege enabled. Without that privilege the
    set call silently no-ops the OWNER portion — we re-read after setting
    to detect that and count it as owner_skip_unprivileged."""
    try:
        import win32security
        import pywintypes
    except ImportError:
        # pywin32 not installed — we can't touch NTFS security descriptors.
        return None

    if not (set_owner or set_dacl):
        return None
    if _preserve_dst_caps["acl"] is False:
        if set_dacl:
            _preserve_stats["acl_skip_unsupported"] += 1
        return False

    SE_FILE_OBJECT = 1  # win32security.SE_FILE_OBJECT
    OWNER = win32security.OWNER_SECURITY_INFORMATION
    GROUP = win32security.GROUP_SECURITY_INFORMATION
    DACL = win32security.DACL_SECURITY_INFORMATION

    # Read OWNER+GROUP+DACL from source in one call — reading is cheap and
    # doesn't require privileges.
    read_flags = 0
    if set_owner:
        read_flags |= OWNER | GROUP
    if set_dacl:
        read_flags |= DACL
    try:
        sd = win32security.GetNamedSecurityInfo(
            src_path, SE_FILE_OBJECT, read_flags,
        )
    except pywintypes.error as e:
        # Source SD read failed (file gone, access denied) — true error.
        winerr = getattr(e, "winerror", 0)
        msg = getattr(e, "strerror", str(e))
        _preserve_stats["_last_acl_err"] = (
            f"GetNamedSecurityInfo({src_path}): winerror={winerr} {msg}"
        )
        if set_dacl:
            _preserve_stats["acl_err"] += 1
        if set_owner:
            _preserve_stats["owner_err"] += 1
        return False

    # ── Owner (+ group) — separate call, separate failure handling ─────
    if set_owner:
        owner_sid = sd.GetSecurityDescriptorOwner()
        group_sid = sd.GetSecurityDescriptorGroup()
        owner_set_threw = None
        try:
            win32security.SetNamedSecurityInfo(
                dst_path, SE_FILE_OBJECT, OWNER | GROUP,
                owner_sid, group_sid, None, None,
            )
        except pywintypes.error as e:
            owner_set_threw = e
        # SetNamedSecurityInfo can silently no-op the OWNER portion when
        # SeRestorePrivilege is held by the process but not *enabled* —
        # the call returns success without actually changing the owner.
        # Verify by re-reading the destination owner and comparing SIDs.
        # That catches the no-op case and reports honestly.
        if owner_set_threw is None:
            try:
                verify_sd = win32security.GetNamedSecurityInfo(
                    dst_path, SE_FILE_OBJECT, OWNER,
                )
                actual = verify_sd.GetSecurityDescriptorOwner()
                if str(actual) == str(owner_sid):
                    _preserve_stats["owner_ok"] += 1
                else:
                    # SetNamedSecurityInfo returned success but the owner
                    # didn't actually change — the silent-no-op case.
                    _preserve_stats["owner_skip_unprivileged"] += 1
                    _preserve_stats["_last_acl_err"] = (
                        f"owner unchanged on {dst_path}: "
                        f"SeRestorePrivilege likely held but not enabled "
                        f"(elevate the shell)"
                    )
            except pywintypes.error:
                # Couldn't verify — be conservative and count as skip.
                _preserve_stats["owner_skip_unprivileged"] += 1
        else:
            e = owner_set_threw
            winerr = getattr(e, "winerror", 0)
            msg = getattr(e, "strerror", str(e))
            # All three of these are "non-admin trying to set owner" outcomes:
            #   5    ERROR_ACCESS_DENIED        — token lacks Owner write right
            #   1307 ERROR_INVALID_OWNER        — SID rejected without
            #                                     SeRestorePrivilege
            #   1314 ERROR_PRIVILEGE_NOT_HELD   — privilege explicitly required
            # Treat the lot as "skipped, need privilege" rather than hard failures.
            if winerr in (5, 1307, 1314):
                _preserve_stats["owner_skip_unprivileged"] += 1
            else:
                _preserve_stats["owner_err"] += 1
                _preserve_stats["_last_acl_err"] = (
                    f"SetNamedSecurityInfo(owner, {dst_path}): "
                    f"winerror={winerr} {msg}"
                )

    # ── DACL — separate call. Owner failure above doesn't block this. ──
    if set_dacl:
        dacl_obj = sd.GetSecurityDescriptorDacl()
        try:
            win32security.SetNamedSecurityInfo(
                dst_path, SE_FILE_OBJECT, DACL,
                None, None, dacl_obj, None,
            )
            _preserve_stats["acl_ok"] += 1
        except pywintypes.error as e:
            winerr = getattr(e, "winerror", 0)
            msg = getattr(e, "strerror", str(e))
            _preserve_stats["acl_err"] += 1
            _preserve_stats["_last_acl_err"] = (
                f"SetNamedSecurityInfo(DACL, {dst_path}): "
                f"winerror={winerr} {msg}"
            )

    return True


def _copy_acls_macos(src_path, dst_path):
    """macOS extended (NFSv4-style) ACLs via ls -le + chmod +a.

    EXPERIMENTAL — not yet validated against a real Darwin system from
    this codebase's test harness. The mechanism:
      1. `ls -lde <src>` shows the file's ACEs prefixed by index numbers
         in the lines after the mode/owner header line.
      2. Each ACE has the form `index: subject permset` — we strip the
         index and feed the rest as `chmod +a "<spec>" <dst>`.

    Known limitations:
      • `chmod +a` accepts a subset of the `ls -le` syntax. Round-trip
        is generally safe but some edge cases (fileinherit/directoryinherit
        flags) may need tweaking.
      • If you find a case that round-trips wrong, please open an issue
        with `ls -lde` output from both source and destination so we can
        fix the parser.
      • Does NOT preserve POSIX 1e ACLs on Linux-mounted-on-macOS paths
        — those would need a separate code path."""
    try:
        import subprocess
        # Read source ACL.
        ls = subprocess.run(["ls", "-lde", "--", src_path],
                            capture_output=True, timeout=5)
        if ls.returncode != 0 or not ls.stdout.strip():
            return None
        lines = ls.stdout.decode("utf-8", errors="replace").splitlines()
        # Drop the first line (mode/owner/etc header). ACEs start with " N:".
        ace_lines = []
        for ln in lines[1:]:
            ln = ln.strip()
            # Expect "0: user:fred allow read,write" style.
            if not ln or ":" not in ln:
                continue
            idx, _, spec = ln.partition(":")
            if not idx.strip().isdigit():
                continue
            ace_lines.append(spec.strip())
        if not ace_lines:
            return None
        # Clear any existing ACEs on the destination before applying source
        # ACEs, so overwrite/incremental copies don't accumulate duplicates.
        # `chmod -N` removes all ACEs without touching POSIX mode bits.
        subprocess.run(["chmod", "-N", dst_path],
                       capture_output=True, timeout=5)
        applied_any = False
        for ace in ace_lines:
            r = subprocess.run(["chmod", "+a", ace, dst_path],
                               capture_output=True, timeout=5)
            if r.returncode == 0:
                applied_any = True
            else:
                _preserve_stats["acl_err"] += 1
        if applied_any:
            _preserve_stats["acl_ok"] += 1
            return True
        return False
    except (OSError, subprocess.SubprocessError, FileNotFoundError):
        _preserve_stats["acl_err"] += 1
        return False


def _print_preserve_summary():
    """One-line-per-kind summary of extended metadata preserve results.

    Quiet when nothing extended was requested. Otherwise shows
    'preserved on N files / dropped on M (reason)' so the user can see
    when the destination FS silently couldn't carry some attribute."""
    spec = _preserve_spec
    if not spec.any_extended():
        return
    s = _preserve_stats
    lines = []
    if spec.owner:
        ok = s["owner_ok"]; un = s["owner_skip_unprivileged"]; err = s["owner_err"]
        if ok or un or err:
            parts = [f"preserved on {C.BOLD}{ok}{C.RESET}"]
            if un:
                if _system == "Windows":
                    need = "need Administrator + SeRestorePrivilege"
                else:
                    need = "need root"
                parts.append(f"{C.YELLOW}skipped on {un}{C.RESET} ({need})")
            if err:
                parts.append(f"{C.RED}failed on {err}{C.RESET}")
            lines.append(f"  Owner:   {', '.join(parts)}")
    if spec.xattr:
        ok = s["xattr_ok"]; un = s["xattr_skip_unsupported"]; err = s["xattr_err"]
        if _preserve_dst_caps["xattr"] is False:
            lines.append(f"  xattrs:  {C.YELLOW}dst FS does not support xattrs — all dropped{C.RESET}")
        elif ok or un or err:
            parts = [f"preserved on {C.BOLD}{ok}{C.RESET}"]
            if un:
                parts.append(f"{C.YELLOW}dropped on {un}{C.RESET} (FS not supported)")
            if err:
                parts.append(f"{C.RED}failed on {err}{C.RESET}")
            lines.append(f"  xattrs:  {', '.join(parts)}")
    if spec.acl:
        ok = s["acl_ok"]; un = s["acl_skip_unsupported"]; err = s["acl_err"]
        if _preserve_dst_caps["acl"] is False:
            lines.append(f"  ACLs:    {C.YELLOW}dst FS does not support ACLs — all dropped{C.RESET}")
        elif ok or un or err:
            parts = [f"preserved on {C.BOLD}{ok}{C.RESET}"]
            if un:
                parts.append(f"{C.YELLOW}dropped on {un}{C.RESET}")
            if err:
                parts.append(f"{C.RED}failed on {err}{C.RESET}")
            lines.append(f"  ACLs:    {', '.join(parts)}")
    if lines:
        for line in lines:
            print(line)
    # If any Windows SD operation captured a low-level error, surface it
    # — gives users actionable diagnostics (winerror=NNN) instead of just
    # an opaque "failed on 1" count.
    last_err = _preserve_stats.get("_last_acl_err")
    if last_err and (_preserve_stats["acl_err"] or _preserve_stats["owner_err"]):
        print(f"  {C.DIM}└─ last error: {last_err}{C.RESET}")


def _apply_owner_via_fd(fd, src_st):
    """Apply src_st's uid/gid to the opened destination fd.

    Requires real-root (euid==0). When not root, count as 'skipped' rather
    than 'error' since this is the expected case for non-elevated copies
    with --preserve owner."""
    if not _is_elevated_for_preserve():
        _preserve_stats["owner_skip_unprivileged"] += 1
        return False
    try:
        if not hasattr(os, "fchown"):       # POSIX-only; not present on Windows
            _preserve_stats["owner_skip_unprivileged"] += 1
            return False
        os.fchown(fd, src_st.st_uid, src_st.st_gid)
        _preserve_stats["owner_ok"] += 1
        return True
    except OSError:
        _preserve_stats["owner_err"] += 1
        return False


def _apply_extended_meta(fd, src_path, dst_path, src_st):
    """Apply spec.owner/xattr/acl with the right platform dispatch.

    Mode and times are NOT touched here — both callers (large-file path
    via _safe_apply_meta, small-file path via copy_block_stream's post-extract
    loop) handle those separately or rely on tar headers.

    Centralizing the Windows-vs-POSIX dispatch here means new copy paths
    automatically pick up the right helpers — fixes a v3.3.0-introductory
    bug where copy_block_stream's small-file post-extract loop kept calling
    the POSIX helpers directly and bypassed _copy_acls_windows / _copy_ads_windows."""
    spec = _preserve_spec
    if _system == "Windows":
        # On NTFS, owner + group + DACL all live in the Security Descriptor;
        # one win32security call sets the requested parts. Avoids the
        # POSIX-style two-step (fchown + setfacl) which doesn't map.
        if (spec.owner or spec.acl) and src_path:
            if _preserve_dst_caps["acl"] is not False or spec.owner:
                _copy_acls_windows(
                    src_path, dst_path,
                    set_owner=spec.owner, set_dacl=spec.acl,
                )
        # ADS (alternate data streams) is the NTFS analog of POSIX xattrs.
        if spec.xattr and src_path and _preserve_dst_caps["xattr"] is not False:
            _copy_ads_windows(src_path, dst_path)
    else:
        if spec.owner:
            _apply_owner_via_fd(fd, src_st)
        if spec.xattr and src_path and _preserve_dst_caps["xattr"] is not False:
            _copy_xattrs(src_path, dst_path)
        if spec.acl and src_path and _preserve_dst_caps["acl"] is not False:
            _copy_posix_acls(src_path, dst_path)


def _safe_apply_meta(fd, dst_path, src_st, src_path=None):
    """Apply requested metadata from src_st to dst (opened as fd).

    Honors the module-wide _preserve_spec: mode/times are the v3.1.x
    behavior; owner/xattr/acl run when explicitly requested via --preserve.
    src_path is needed when xattr/acl are requested — the underlying syscalls
    take paths, not fds, in their broadly-portable forms. Symlink-safe:
    fchmod via fd, lstat before path-based calls."""
    spec = _preserve_spec
    if spec.mode:
        # os.fchmod is POSIX-only (absent on Windows → AttributeError, which the
        # OSError handler would NOT catch). Fall back to a path-based chmod so
        # Windows still applies the mode (the read-only bit) instead of crashing.
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, stat.S_IMODE(src_st.st_mode))
            elif dst_path:
                os.chmod(_long_path(dst_path), stat.S_IMODE(src_st.st_mode))
        except OSError:
            pass
    if spec.times:
        try:
            lst = os.lstat(dst_path)
            if not stat.S_ISLNK(lst.st_mode):
                os.utime(dst_path, (src_st.st_atime, src_st.st_mtime))
        except OSError:
            pass
    _apply_extended_meta(fd, src_path, dst_path, src_st)


def _is_under_sudo():
    """True when the current process was launched via sudo.

    sudo sets SUDO_USER to the original (pre-elevation) username. This is the
    most reliable signal — checking geteuid()==0 alone catches anyone running
    as root, including direct logins."""
    return bool(os.environ.get("SUDO_USER"))


def _sudo_user_home():
    """Resolve $SUDO_USER's home directory, or None if unavailable.

    Why: the audit file lives in the invoking user's home so a non-root
    attacker can't pre-plant a symlink at the destination path and trick
    root into chmod / chattr / appending on an arbitrary file."""
    sudo_user = os.environ.get("SUDO_USER")
    if not sudo_user:
        return None
    try:
        import pwd
        return pwd.getpwnam(sudo_user).pw_dir
    except (KeyError, ImportError, OSError):
        return None


def _set_immutable(path, immutable):
    """Set or clear the Linux immutable attribute (chattr +i / -i).

    Returns True on success, False if chattr is unavailable, the filesystem
    doesn't support immutability (tmpfs, FAT32, NFS, …), or we lack
    privileges. Callers must treat False as "no protection" rather than a
    fatal error — the audit record itself is still useful even unprotected.

    Caller MUST verify the path is not a symlink before calling — chattr
    follows symlinks and could otherwise be redirected to a sensitive file."""
    if _system != "Linux":
        return False
    try:
        import subprocess
        subprocess.run(
            ["chattr", "+i" if immutable else "-i", path],
            check=True, capture_output=True, timeout=10,
        )
        return True
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False


# Flags whose VALUES are secrets (or point at one) and must never be written
# verbatim to the audit log. Covers cloud keys and the hidden password-env flags.
_REDACT_FLAGS = {"--az-key", "--az-connection-string",
                 "--ssh-dst-password-env", "--ssh-src-password-env"}


def _redact_argv(argv):
    """Mask the values of known secret-bearing flags before logging the command
    line. Handles both '--flag value' and '--flag=value' forms; everything else
    passes through unchanged."""
    out, redact_next = [], False
    for tok in argv:
        if redact_next:
            out.append("***")
            redact_next = False
            continue
        flag = tok.split("=", 1)[0]
        if flag in _REDACT_FLAGS:
            out.append(f"{flag}=***" if "=" in tok else tok)
            redact_next = "=" not in tok
        else:
            out.append(tok)
    return out


def write_sudo_audit(src_display, dst_display, summary):
    """Append a hidden, immutable audit record to ~$SUDO_USER/.fast_copy_audit.jsonl.

    Only fires when the process is running under sudo — captures who invoked
    sudo, the command, what was copied, and the full per-file list from
    _log_entries. The file is one JSON object per line so it can accumulate
    across runs.

    Location: $SUDO_USER's home directory (not the copy destination) so a
    non-root attacker can't influence the audit path.

    Symlink/hardlink hardening:
      • lstat the path; refuse if it's a symlink or non-regular file
      • open with O_NOFOLLOW | O_APPEND | O_CREAT, fchmod via fd
      • refuse if st_nlink > 1 (hardlink-pinned to a sensitive target)

    Tamper-resistance: after each write the file is chattr +i (immutable),
    so even root cannot modify or delete it without first running
    `chattr -i`. The next sudo invocation does that automatically before
    appending its own record, then re-immutables the file."""
    if not _is_under_sudo():
        return
    audit_dir = _sudo_user_home()
    if not audit_dir or not os.path.isdir(audit_dir):
        return
    import datetime
    audit_path = os.path.join(audit_dir, SUDO_AUDIT_FILE)

    pre_existing_immutable = False
    try:
        lst = os.lstat(audit_path)
    except FileNotFoundError:
        lst = None
    except OSError as e:
        print(f"  {C.YELLOW}Audit: cannot stat {audit_path}: {e}{C.RESET}")
        return
    if lst is not None:
        if stat.S_ISLNK(lst.st_mode):
            print(f"  {C.RED}Audit: refusing — {audit_path} is a symlink{C.RESET}")
            return
        if not stat.S_ISREG(lst.st_mode):
            print(f"  {C.RED}Audit: refusing — {audit_path} is not a regular file{C.RESET}")
            return
        if lst.st_nlink > 1:
            print(f"  {C.RED}Audit: refusing — {audit_path} has {lst.st_nlink} hardlinks{C.RESET}")
            return
        pre_existing_immutable = _set_immutable(audit_path, False)

    record = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "sudo_user": os.environ.get("SUDO_USER"),
        "sudo_uid": os.environ.get("SUDO_UID"),
        "sudo_gid": os.environ.get("SUDO_GID"),
        "effective_uid": os.geteuid() if hasattr(os, "geteuid") else None,
        "command": " ".join(_redact_argv(sys.argv)),
        "cwd": os.environ.get("PWD") or os.getcwd(),
        "source": src_display,
        "destination": dst_display,
        "summary": summary,
        "files": list(_log_entries),
    }
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(audit_path, flags, 0o600)
    except OSError as e:
        if e.errno == errno.ELOOP:
            print(f"  {C.RED}Audit: refusing — symlink at {audit_path}{C.RESET}")
        else:
            print(f"  {C.YELLOW}Could not open audit file: {e}{C.RESET}")
        return
    try:
        try:
            fst = os.fstat(fd)
            if not stat.S_ISREG(fst.st_mode):
                print(f"  {C.RED}Audit: refusing — opened non-regular file{C.RESET}")
                return
            if fst.st_nlink > 1:
                print(f"  {C.RED}Audit: refusing — {audit_path} has {fst.st_nlink} hardlinks{C.RESET}")
                return
        except OSError as e:
            print(f"  {C.YELLOW}Audit: fstat failed: {e}{C.RESET}")
            return
        try:
            os.write(fd, (json.dumps(record) + "\n").encode("utf-8"))
        except OSError as e:
            print(f"  {C.YELLOW}Could not write audit file: {e}{C.RESET}")
            return
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
        except OSError:
            pass
    finally:
        os.close(fd)

    made_immutable = _set_immutable(audit_path, True)
    if made_immutable:
        tamper_note = "immutable"
    elif pre_existing_immutable:
        tamper_note = "WARNING: was immutable but couldn't restore"
    else:
        tamper_note = "not immutable (chattr unavailable or unsupported FS)"
    print(f"  Audit:   {C.BOLD}{audit_path}{C.RESET} "
          f"{C.DIM}(sudo run by {os.environ.get('SUDO_USER')}, "
          f"{tamper_note}){C.RESET}")


# ════════════════════════════════════════════════════════════════════════════
# TERMINAL OUTPUT
# ════════════════════════════════════════════════════════════════════════════
_is_tty = sys.stdout is not None and sys.stdout.isatty()

class C:
    GREEN  = "\033[92m" if _is_tty else ""
    YELLOW = "\033[93m" if _is_tty else ""
    RED    = "\033[91m" if _is_tty else ""
    CYAN   = "\033[96m" if _is_tty else ""
    BOLD   = "\033[1m"  if _is_tty else ""
    DIM    = "\033[2m"  if _is_tty else ""
    RESET  = "\033[0m"  if _is_tty else ""

def fmt_size(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"

def fmt_speed(bps):
    return f"{fmt_size(bps)}/s"

def fmt_time(s):
    if s < 60:
        return f"{s:.1f}s"
    m, s = divmod(int(s), 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s"

def fmt_pct(a, b):
    if b == 0:
        return "0%"
    return f"{a / b * 100:.1f}%"

def banner(msg):
    print(f"\n{C.BOLD}{C.CYAN}{'─'*60}")
    print(f"  {msg}")
    print(f"{'─'*60}{C.RESET}\n")


# ════════════════════════════════════════════════════════════════════════════
# HASHING — use xxhash if available (10x faster), fallback to sha256
#
# Algorithm selection is dynamic: defaults to "auto" at import time, but can
# be overridden via --hash before Phase 2. See _set_hash_algo() below.
# ════════════════════════════════════════════════════════════════════════════
try:
    import xxhash
    _HAS_XXHASH = True
except ImportError:
    _HAS_XXHASH = False


def _make_sha256_hasher():
    return hashlib.sha256()


def _make_xxh128_hasher():
    return xxhash.xxh128()


# Initial auto-selection: xxh128 if installed, else sha256.
# new_hasher and _hash_name may be reassigned later by _set_hash_algo().
if _HAS_XXHASH:
    new_hasher = _make_xxh128_hasher
    _hash_name = "xxh128"
else:
    new_hasher = _make_sha256_hasher
    _hash_name = "sha256"


_EMPTY_HASH = new_hasher().hexdigest()  # hash of zero bytes for active algorithm
_hash_source = "auto"   # "auto" | "forced" — set by --hash flag


def _set_hash_algo(choice):
    """Configure the active hash algorithm based on the --hash flag.

    choice must be one of: "auto", "xxh128", "sha256".
    Raises SystemExit if "xxh128" is requested but the xxhash package
    isn't installed.

    Updates module-level globals: new_hasher, _hash_name, _EMPTY_HASH,
    _hash_source. Must be called before any hashing happens (i.e. before
    Phase 2), otherwise cached hashes would use the previous algorithm.
    """
    global new_hasher, _hash_name, _EMPTY_HASH, _hash_source

    if choice == "auto":
        # Keep the initial auto-selection (already set at import time).
        _hash_source = "auto"
        return

    if choice == "xxh128":
        if not _HAS_XXHASH:
            print(
                f"{C.RED}Error: --hash=xxh128 requested but xxhash package "
                f"not installed.{C.RESET}\n"
                f"  Install with: {C.BOLD}python -m pip install xxhash{C.RESET}\n"
                f"  Or use --hash=sha256 or --hash=auto instead.",
                file=sys.stderr,
            )
            sys.exit(2)
        new_hasher = _make_xxh128_hasher
        _hash_name = "xxh128"
    elif choice == "sha256":
        new_hasher = _make_sha256_hasher
        _hash_name = "sha256"
    else:
        raise ValueError("invalid hash choice: {}".format(choice))

    _EMPTY_HASH = new_hasher().hexdigest()
    _hash_source = "forced"


def hash_file(filepath, buf_size=HASH_CHUNK):
    """Hash file contents. Returns hex digest string."""
    h = new_hasher()
    try:
        with open(filepath, "rb") as f:
            chunk = f.read(buf_size)
            if not chunk:
                return _EMPTY_HASH
            while chunk:
                h.update(chunk)
                chunk = f.read(buf_size)
        return h.hexdigest()
    except OSError:
        return None


# ════════════════════════════════════════════════════════════════════════════
# PATH SAFETY — prevents path traversal attacks
# ════════════════════════════════════════════════════════════════════════════
def _validate_rel_path(rel):
    """Check that a relative path is safe for tar inclusion. Returns True or error string."""
    if not rel or rel.startswith('/') or os.path.isabs(rel):
        return "absolute path"
    for part in rel.replace('\\', '/').split('/'):
        if part == '..':
            return "path traversal (..)"
    if '\0' in rel or '\n' in rel:
        return "null or newline in path"
    return True


def _safe_local_dest(real_root, rel):
    """Resolve `rel` under an already-realpath'd destination root and confirm it
    cannot escape — via '..'/absolute strings OR a symlinked directory component.
    Returns the joinable absolute path, or None if it would escape. Used for
    untrusted object keys / fc_relpath metadata on cloud downloads."""
    if _validate_rel_path(rel) is not True:
        return None
    full = os.path.join(real_root, rel.replace("/", os.sep))
    real_full = os.path.realpath(full)
    if real_full != real_root and not real_full.startswith(real_root + os.sep):
        return None
    return full
def _validate_tar_member(member, dst_root):
    """Validate a tar member for safety. Returns True or error string."""
    # Reject absolute paths
    if member.name.startswith('/') or os.path.isabs(member.name):
        return "blocked: absolute path"
    # Check every component for '..'
    for part in member.name.replace('\\', '/').split('/'):
        if part == '..':
            return "blocked: path traversal (..)"
    # Explicitly reject dangerous member types (symlinks, hard links, devices, FIFOs)
    if member.issym():
        return "blocked: symlink"
    if member.islnk():
        return "blocked: tar hard link"
    if member.isdev() or member.isfifo() or member.ischr() or member.isblk():
        return "blocked: device/fifo"
    # Only allow regular files and directories
    if not (member.isfile() or member.isdir()):
        return "blocked: unsupported member type"
    # Reject null bytes in name
    if '\0' in member.name:
        return "blocked: null byte in name"
    # Resolve final path and verify it stays within dst_root.
    # Use os.path.normcase for the comparison so case-insensitive
    # filesystems (Windows NTFS, macOS HFS+ default, APFS default)
    # don't reject legitimate paths just because Python's string
    # comparison is case-sensitive while the filesystem is not.
    resolved = os.path.realpath(os.path.join(dst_root, member.name))
    real_dst = os.path.realpath(dst_root)
    nc_resolved = os.path.normcase(resolved)
    nc_real_dst = os.path.normcase(real_dst)
    if not (nc_resolved == nc_real_dst or
            nc_resolved.startswith(nc_real_dst + os.sep)):
        return "blocked: resolves outside destination"
    return True


def _safe_tar_extract(tar, member, dst_root):
    """Extract a single tar member safely. Returns True on success, error string on failure.

    Filter selection:
      • By default, use Python 3.12's 'data' filter and zero out uid/gid —
        the safest extraction with no ownership preservation.
      • When --preserve owner is requested, switch to 'tar' filter and keep
        the member's uid/gid intact. _validate_tar_member has already
        rejected absolute paths, symlinks, devices, hardlinks, and any
        member whose realpath escapes dst_root, so the only thing 'tar'
        permits beyond 'data' that we actually want is ownership.
      • Non-root extraction silently fails to chown (tarfile swallows the
        EPERM) — we count those as owner_skip_unprivileged at the start
        of the R2L copy phase rather than per-member."""
    check = _validate_tar_member(member, dst_root)
    if check is not True:
        return check
    extract_path = _long_path(dst_root) if _system == "Windows" else dst_root
    if _system == "Windows":
        # A destination file left read-only/hidden by a prior run (most often a
        # locked credentials.json — Windows immutability is just the read-only
        # attribute) can't be overwritten: open() fails with EACCES/Errno 13.
        # Strip those attributes first so the extract can replace it, mirroring
        # how _save_credentials_file overwrites our own locked creds file.
        target = os.path.join(extract_path, member.name.replace("/", os.sep))
        _windows_clear_attrs(target)
    preserve_owner = _preserve_spec.owner
    if not preserve_owner:
        # Default safe behavior: drop ownership info from the member.
        member.uid = member.gid = 0
        member.uname = member.gname = ""
    try:
        tar.extract(member, path=extract_path,
                    filter='tar' if preserve_owner else 'data')
    except TypeError:
        # Python <3.12: filter kwarg not supported. Without it, tarfile
        # honors uid/gid by default — so if owner preservation was NOT
        # requested, we already sanitized the member above.
        tar.extract(member, path=extract_path)
    if preserve_owner and _is_elevated_for_preserve():
        _preserve_stats["owner_ok"] += 1
    return True


# ════════════════════════════════════════════════════════════════════════════
# DEDUP DATABASE — persistent hash cache across runs
# ════════════════════════════════════════════════════════════════════════════
DEDUP_DB_NAME = ".fast_copy_dedup.db"


def _find_mount_point(path):
    """Walk up from path to find the filesystem mount point."""
    path = os.path.realpath(path)
    while not os.path.ismount(path):
        path = os.path.dirname(path)
    return path


class DedupDB:
    """
    SQLite-backed hash cache stored at the mount/drive root.
    Shared across all destinations on the same drive.

    Three tables:
      source_cache    — keyed on (source rel_path, size, mtime_ns)
                        Speeds up hashing: same source file → same hash
                        regardless of which destination subfolder you copy to.
      dest_files      — keyed on mount-relative path
                        Tracks what's actually on the drive for cross-run dedup.
      existing_index  — keyed on mount-relative path, size only (no hash).
                        Populated by --index-existing; entries are lazily hashed
                        on first size-match and promoted to dest_files.
    """

    def __init__(self, dst_root):
        self.dst_root = os.path.realpath(dst_root)
        self.mount = _find_mount_point(dst_root)
        # Avoid writing DB to filesystem root — fall back to destination dir
        if self.mount == "/" or not os.access(self.mount, os.W_OK):
            db_path = os.path.join(self.dst_root, DEDUP_DB_NAME)
        else:
            db_path = os.path.join(self.mount, DEDUP_DB_NAME)
        self.db_path = db_path
        # Prefix to convert dest-relative → mount-relative
        self._prefix = os.path.relpath(self.dst_root, self.mount)
        # Reject if db_path is a symlink — use O_NOFOLLOW to avoid TOCTOU race
        if hasattr(os, 'O_NOFOLLOW'):
            try:
                fd = os.open(db_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                os.close(fd)
            except OSError as e:
                import errno
                if e.errno in (errno.ELOOP, errno.EMLINK):
                    raise OSError(f"Refusing to open dedup DB: {db_path} is a symlink")
                raise
        elif os.path.islink(db_path):
            raise OSError(f"Refusing to open dedup DB: {db_path} is a symlink")
        # SQLite opens the WAL/SHM (and rollback-journal) side-files through its
        # own C library, NOT through our O_NOFOLLOW-guarded fd — so a symlink
        # planted at one of those paths would otherwise be followed and written
        # through (e.g. -> ~/.ssh/authorized_keys). Refuse if any pre-exists as a
        # symlink before SQLite touches them.
        for _suffix in ("-wal", "-shm", "-journal"):
            _side = db_path + _suffix
            if os.path.islink(_side):
                raise OSError(f"Refusing to open dedup DB: {_side} is a symlink")
        # Create DB with restrictive permissions (owner-only)
        old_umask = os.umask(0o077)
        try:
            self.conn = sqlite3.connect(db_path, check_same_thread=False)
        finally:
            os.umask(old_umask)
        try:
            # secure_delete zeroes freed pages so deleted rows (file paths,
            # sizes, hashes) aren't recoverable from the DB's free list.
            self.conn.execute("PRAGMA secure_delete=ON")
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")  # WAL default, crash-safe
            self.conn.execute("PRAGMA user_version=4718")  # schema v2
            self.lock = threading.Lock()
            self._init_schema()
        except Exception:
            self.conn.close()
            raise

    def _mount_rel(self, rel_path):
        """Convert destination-relative path to mount-relative path.
        Normalizes to forward slashes for cross-platform DB portability."""
        return os.path.join(self._prefix, rel_path).replace(os.sep, '/')

    def _init_schema(self):
        c = self.conn.cursor()
        # Source hash cache — shared across all destination folders
        c.execute("""
            CREATE TABLE IF NOT EXISTS source_cache (
                rel_path    TEXT NOT NULL,
                size        INTEGER NOT NULL,
                mtime_ns    INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                hash_algo   TEXT NOT NULL,
                PRIMARY KEY (rel_path, hash_algo)
            )
        """)
        # Destination file index — tracks files on the drive
        c.execute("""
            CREATE TABLE IF NOT EXISTS dest_files (
                mount_rel   TEXT PRIMARY KEY,
                size        INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                hash_algo   TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_dest_hash
            ON dest_files (content_hash)
        """)
        # Existing-file index — size only, no hash until lazily computed
        c.execute("""
            CREATE TABLE IF NOT EXISTS existing_index (
                mount_rel   TEXT PRIMARY KEY,
                size        INTEGER NOT NULL
            )
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_existing_size
            ON existing_index (size)
        """)
        # Migrate old single-table schema if present
        try:
            c.execute("SELECT 1 FROM file_hashes LIMIT 1")
            c.execute("DROP TABLE file_hashes")
        except sqlite3.OperationalError:
            pass
        self.conn.commit()

    # ── Source cache (hash speedup) ───────────────────────────────

    def lookup(self, rel_path, size, mtime_ns):
        """Return cached hash if source file size+mtime match, else None."""
        with self.lock:
            c = self.conn.cursor()
            c.execute(
                "SELECT content_hash FROM source_cache "
                "WHERE rel_path = ? AND size = ? AND mtime_ns = ? AND hash_algo = ?",
                (rel_path, size, mtime_ns, _hash_name),
            )
            row = c.fetchone()
            return row[0] if row else None

    def store_source_batch(self, rows):
        """Cache source hashes. rows = list of (rel_path, size, mtime_ns, hash)."""
        with self.lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO source_cache "
                "(rel_path, size, mtime_ns, content_hash, hash_algo) "
                "VALUES (?, ?, ?, ?, ?)",
                [(r[0], r[1], r[2], r[3], _hash_name) for r in rows],
            )
            self.conn.commit()

    # ── Destination index (cross-run dedup) ───────────────────────

    def store_dest_batch(self, rows):
        """Record files on the drive. rows = list of (rel_path, size, hash).
        rel_path is destination-relative; stored as mount-relative."""
        with self.lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO dest_files "
                "(mount_rel, size, content_hash, hash_algo) "
                "VALUES (?, ?, ?, ?)",
                [(self._mount_rel(r[0]), r[1], r[2], _hash_name) for r in rows],
            )
            self.conn.commit()

    def lookup_by_hash(self, content_hash):
        """Find files on this drive with this hash.
        Returns list of (mount_rel_path, size)."""
        with self.lock:
            c = self.conn.cursor()
            c.execute(
                "SELECT mount_rel, size FROM dest_files "
                "WHERE content_hash = ? AND hash_algo = ?",
                (content_hash, _hash_name),
            )
            return c.fetchall()

    def dest_sizes(self):
        """Set of distinct file sizes recorded across the whole drive — used to
        decide which source files are worth hashing for dedup-against-existing."""
        with self.lock:
            return set(r[0] for r in
                       self.conn.execute("SELECT DISTINCT size FROM dest_files"))

    def prune_dest(self, present_rels):
        """Drop dest_files entries for files no longer present, SCOPED to this
        destination's subtree (so other folders on the same drive are untouched).
        present_rels = destination-relative paths currently on disk. Returns the
        number of stale rows removed."""
        with self.lock:
            present = set(self._mount_rel(r) for r in present_rels)
            prefix = self._mount_rel("")            # this dst's subtree in the DB
            cached = [row[0] for row in
                      self.conn.execute("SELECT mount_rel FROM dest_files")]
            gone = [m for m in cached
                    if m.startswith(prefix) and m not in present]
            if gone:
                self.conn.executemany(
                    "DELETE FROM dest_files WHERE mount_rel = ?",
                    [(m,) for m in gone])
                self.conn.commit()
            return len(gone)

    # ── Existing-file index (lazy-hash dedup against pre-existing content) ──

    def index_existing(self, root_path):
        """Walk root_path and populate existing_index with (mount_rel, size).
        Skips files already known in dest_files (hash already recorded).
        Safe to call repeatedly — INSERT OR IGNORE skips already-indexed entries."""
        root_path = os.path.realpath(root_path)
        real_mount = os.path.realpath(self.mount)
        if not root_path.startswith(real_mount):
            print(f"  {C.YELLOW}Warning: --index-existing path {root_path!r} is not "
                  f"on the destination mount {real_mount!r} — skipping{C.RESET}")
            return

        batch = []
        scanned = 0
        skipped_known = 0

        with self.lock:
            known_rels = set(
                row[0] for row in self.conn.execute(
                    "SELECT mount_rel FROM dest_files WHERE hash_algo = ?",
                    (_hash_name,),
                )
            )

        print(f"  {C.DIM}Indexing existing files in {root_path} ...{C.RESET}",
              end="", flush=True)

        for dirpath, _dirs, filenames in os.walk(root_path):
            for fname in filenames:
                abs_path = os.path.join(dirpath, fname)
                try:
                    st = os.lstat(abs_path)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                mount_rel = os.path.relpath(abs_path, self.mount).replace(os.sep, '/')
                try:
                    mount_rel.encode('utf-8')
                except UnicodeEncodeError:
                    continue  # skip filenames with non-UTF-8 bytes
                if mount_rel in known_rels:
                    skipped_known += 1
                    continue
                batch.append((mount_rel, st.st_size))
                scanned += 1
                if len(batch) >= 2000:
                    with self.lock:
                        self.conn.executemany(
                            "INSERT OR IGNORE INTO existing_index (mount_rel, size) "
                            "VALUES (?, ?)",
                            batch,
                        )
                        self.conn.commit()
                    batch.clear()
                if scanned % 5000 == 0:
                    print(f"\r  {C.DIM}Indexing... {scanned} files{C.RESET}",
                          end="", flush=True)

        if batch:
            with self.lock:
                self.conn.executemany(
                    "INSERT OR IGNORE INTO existing_index (mount_rel, size) "
                    "VALUES (?, ?)",
                    batch,
                )
                self.conn.commit()

        print(f"\r  {C.GREEN}Indexed {scanned} files into existing_index"
              f" ({skipped_known} already in dest_files){C.RESET}          ")

    def lookup_existing_by_size(self, size):
        """Return list of mount_rel for existing_index entries matching size."""
        with self.lock:
            c = self.conn.cursor()
            c.execute(
                "SELECT mount_rel FROM existing_index WHERE size = ?",
                (size,),
            )
            return [row[0] for row in c.fetchall()]

    def promote_from_existing(self, mount_rel, size, content_hash):
        """Move an entry from existing_index to dest_files (hash now known).
        Always called after lazy hashing, regardless of whether hash matched.
        Does NOT commit — call commit_pending() periodically."""
        with self.lock:
            self.conn.execute(
                "DELETE FROM existing_index WHERE mount_rel = ?",
                (mount_rel,),
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO dest_files "
                "(mount_rel, size, content_hash, hash_algo) VALUES (?, ?, ?, ?)",
                (mount_rel, size, content_hash, _hash_name),
            )

    def remove_existing(self, mount_rel):
        """Remove a stale entry from existing_index (file gone or size changed).
        Does NOT commit — call commit_pending() periodically."""
        with self.lock:
            self.conn.execute(
                "DELETE FROM existing_index WHERE mount_rel = ?",
                (mount_rel,),
            )

    def commit_pending(self):
        """Flush accumulated promote/remove writes to disk."""
        with self.lock:
            self.conn.commit()

    def close(self):
        with self.lock:
            self.conn.commit()
            self.conn.close()


# ════════════════════════════════════════════════════════════════════════════
# SSH REMOTE — connection, parsing, remote operations
# ════════════════════════════════════════════════════════════════════════════
try:
    import paramiko
    _has_paramiko = True
except ImportError:
    _has_paramiko = False

RemoteSpec = namedtuple("RemoteSpec", ["user", "host", "port", "path"])
REMOTE_MANIFEST_NAME = ".fast_copy_manifest.json"


def parse_remote_path(path_str):
    """Parse user@host:/path or host:/path. Returns RemoteSpec or None.
    Supports IPv6 in brackets: user@[::1]:/path"""
    # Try IPv6 in brackets first: user@[host]:/path or [host]:/path
    m = re.match(r'^(?:([^@]+)@)?\[([^\]]+)\]:(.+)$', path_str)
    if not m:
        # Standard: user@host:/path or host:/path
        # Host must not contain whitespace
        m = re.match(r'^(?:([^@]+)@)?([^:\s]+):(.+)$', path_str)
    if not m:
        return None
    # Single-letter host is a Windows drive letter (e.g. C:\), not a remote
    if len(m.group(2)) == 1 and m.group(2).isalpha():
        return None
    user = m.group(1) or getpass.getuser()
    host = m.group(2)
    path = m.group(3)
    return RemoteSpec(user=user, host=host, port=22, path=path)


_ParamikoHostKeyBase = paramiko.MissingHostKeyPolicy if _has_paramiko else object

# When True, unknown SSH host keys are rejected outright (no interactive TOFU
# prompt). Set from --ssh-strict-host-key-checking for automated/CI use.
_strict_host_keys = False


def _user_known_hosts_path():
    """Path to known_hosts. Under sudo we route to $SUDO_USER's home so
    accepted keys persist for the human user, not for root's profile."""
    home = _sudo_user_home()
    if home:
        return os.path.join(home, ".ssh", "known_hosts")
    return os.path.expanduser("~/.ssh/known_hosts")


def _chown_to_sudo_user(path):
    """Chown a file (and its parent .ssh dir if root-created) back to
    $SUDO_USER after writing under sudo. No-op if not under sudo."""
    sudo_user = os.environ.get("SUDO_USER")
    if not sudo_user or not (hasattr(os, "geteuid") and os.geteuid() == 0):
        return
    try:
        import pwd
        pw = pwd.getpwnam(sudo_user)
    except (KeyError, ImportError, OSError):
        return
    try:
        os.chown(path, pw.pw_uid, pw.pw_gid)
    except OSError:
        pass
    ssh_dir = os.path.dirname(path)
    try:
        st = os.lstat(ssh_dir)
        if st.st_uid == 0:
            os.chown(ssh_dir, pw.pw_uid, pw.pw_gid)
    except OSError:
        pass


class _InteractiveHostKeyPolicy(_ParamikoHostKeyBase):
    """Prompts the user to accept unknown host keys, like OpenSSH does."""

    def missing_host_key(self, client, hostname, key):
        key_type = key.get_name()
        fingerprint_md5 = ":".join(f"{b:02x}" for b in key.get_fingerprint())
        import base64
        fingerprint_sha256 = base64.b64encode(
            hashlib.sha256(key.asbytes()).digest()
        ).decode().rstrip("=")
        print(f"\n  {C.RED}WARNING: Unknown host key for {hostname}.{C.RESET}")
        print(f"  {C.YELLOW}Verify this fingerprint with the server administrator{C.RESET}")
        print(f"  {C.YELLOW}before accepting to prevent man-in-the-middle attacks.{C.RESET}")
        print(f"  Type:        {key_type}")
        print(f"  MD5:         {fingerprint_md5}")
        print(f"  SHA256:      {fingerprint_sha256}")
        try:
            answer = input(f"  Accept and save to known_hosts? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in ("y", "yes"):
            raise paramiko.SSHException(
                f"Host key for {hostname} rejected by user"
            )
        known_hosts = _user_known_hosts_path()
        os.makedirs(os.path.dirname(known_hosts), exist_ok=True)
        try:
            host_keys = paramiko.HostKeys(known_hosts)
        except (IOError, OSError):
            host_keys = paramiko.HostKeys()
        host_keys.add(hostname, key_type, key)
        try:
            host_keys.save(known_hosts)
            _chown_to_sudo_user(known_hosts)
            print(f"  {C.GREEN}Host key saved to {known_hosts}{C.RESET}")
        except (IOError, OSError) as e:
            print(f"  {C.YELLOW}Could not save host key: {e}{C.RESET}")


class SSHConnection:
    """Paramiko SSH wrapper with exec, SFTP, and capability detection."""

    def __init__(self, spec, port=22, key_path=None, password=None, compress=False):
        self.spec = spec._replace(port=port)
        self.key_path = key_path
        self.password = password
        self.compress = compress
        self.client = None
        self.sftp = None
        self.caps = {}

    def __repr__(self):
        # Explicit repr so the default one never serializes self.password into a
        # traceback, log line, or debugger output.
        return f"SSHConnection({self.spec.user}@{self.spec.host}:{self.spec.port})"

    __str__ = __repr__

    def connect(self):
        self.client = paramiko.SSHClient()
        # Load system known_hosts for host key verification
        try:
            self.client.load_system_host_keys()
        except IOError:
            pass
        known_hosts = _user_known_hosts_path()
        if os.path.isfile(known_hosts):
            try:
                self.client.load_host_keys(known_hosts)
            except IOError:
                pass
        # Strict mode (for CI/automation): reject any host key not already in
        # known_hosts instead of the interactive trust-on-first-use prompt, so
        # a first-connection MITM can't be accepted by an unattended run.
        if _strict_host_keys:
            self.client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            self.client.set_missing_host_key_policy(_InteractiveHostKeyPolicy())

        connect_kwargs = {
            "hostname": self.spec.host,
            "port": self.spec.port,
            "username": self.spec.user,
            "compress": self.compress,
        }

        # Auth: try key file → agent/default keys → password
        if self.key_path:
            connect_kwargs["key_filename"] = self.key_path
        if self.password:
            connect_kwargs["password"] = self.password

        max_attempts = 3
        try:
            for attempt in range(1, max_attempts + 1):
                try:
                    self.client.connect(**connect_kwargs)
                    break  # success
                except (paramiko.AuthenticationException, paramiko.SSHException) as e:
                    # Only retry on auth-related errors, not connection errors
                    if "auth" not in str(e).lower() and "No authentication" not in str(e):
                        raise
                    if attempt == max_attempts:
                        print(f"\n  {C.RED}Authentication failed after {max_attempts} attempts.{C.RESET}")
                        self.client.close()
                        sys.exit(1)
                    print(f"  {C.YELLOW}Authentication failed. Attempt {attempt}/{max_attempts}.{C.RESET}")
                    pw = getpass.getpass(f"  Password for {self.spec.user}@{self.spec.host}: ")
                    connect_kwargs["password"] = pw
        except (KeyboardInterrupt, EOFError):
            print(f"\n  {C.YELLOW}Authentication cancelled.{C.RESET}")
            self.client.close()
            sys.exit(0)

        transport = self.client.get_transport()
        transport.set_keepalive(30)
        # Push rekey thresholds high so the periodic SSH key re-exchange
        # doesn't fire mid-transfer — slow/busy servers can fail to respond
        # in time and the rekey timeout kills the whole session.
        transport.packetizer.REKEY_BYTES = pow(2, 40)          # 1 TiB
        transport.packetizer.REKEY_PACKETS = pow(2, 40)
        # Increase default window/packet size for much faster SFTP throughput
        transport.default_window_size = 16 * 1024 * 1024      # 16 MB
        transport.default_max_packet_size = 512 * 1024         # 512 KB

        self._detect_capabilities()
        return self

    MAX_CMD_OUTPUT = 100 * 1024 * 1024  # 100 MB cap on command output

    def exec_cmd(self, cmd, input_data=None, timeout=300):
        """Execute remote command. Returns (stdout_str, stderr_str, exit_code)."""
        import threading
        stdin, stdout, stderr = self.client.exec_command(cmd, timeout=timeout)
        if input_data:
            # Write stdin in a background thread to avoid deadlock: the remote
            # command may produce stdout while we're still writing stdin, and if
            # either buffer fills both sides stall.
            data = input_data.encode("utf-8") if isinstance(input_data, str) else input_data
            def _write_stdin():
                try:
                    chunk_size = 65536
                    for i in range(0, len(data), chunk_size):
                        stdin.write(data[i:i + chunk_size])
                finally:
                    stdin.channel.shutdown_write()
            writer = threading.Thread(target=_write_stdin, daemon=True)
            writer.start()
        out_bytes = stdout.read(self.MAX_CMD_OUTPUT)
        err_bytes = stderr.read(self.MAX_CMD_OUTPUT)
        # Warn if output was likely truncated
        if len(out_bytes) >= self.MAX_CMD_OUTPUT:
            print(f"  {C.YELLOW}Warning: remote command output truncated at "
                  f"{self.MAX_CMD_OUTPUT // (1024*1024)}MB{C.RESET}")
        out = out_bytes.decode("utf-8", errors="replace")
        err = err_bytes.decode("utf-8", errors="replace")
        rc = stdout.channel.recv_exit_status()
        if input_data:
            writer.join(timeout=30)
        return out, err, rc

    def open_sftp(self):
        if self.sftp is None:
            transport = self.client.get_transport()
            try:
                self.sftp = paramiko.SFTPClient.from_transport(
                    transport,
                    window_size=16 * 1024 * 1024,     # 16 MB (default ~2 MB)
                    max_packet_size=512 * 1024,         # 512 KB (default 32 KB)
                )
            except Exception:
                # Some SSH servers reject large window/packet sizes — fall back
                self.sftp = paramiko.SFTPClient.from_transport(transport)
        return self.sftp

    def open_channel(self):
        """Open a raw exec channel for streaming."""
        return self.client.get_transport().open_session()

    def _detect_capabilities(self):
        """Check what tools are available on remote."""
        for tool, cmd in [
            ("gnu_find", "find --version 2>/dev/null"),
            ("tar", "tar --version 2>/dev/null"),
            ("python3", "python3 --version 2>/dev/null"),
            ("sha256sum", "sha256sum --version 2>/dev/null"),
        ]:
            _, _, rc = self.exec_cmd(cmd, timeout=10)
            self.caps[tool] = (rc == 0)

    def close(self):
        if self.sftp:
            self.sftp.close()
        if self.client:
            self.client.close()


def check_remote_space(ssh, remote_path, required_bytes, force=False):
    """Check free space on remote via df. Walks up to parent if path doesn't exist."""
    # Try the path itself, then walk up to find an existing parent
    check_path = remote_path
    for _ in range(10):
        out, _, rc = ssh.exec_cmd(f"df -B1 {shlex.quote(check_path)} 2>/dev/null")
        if rc == 0:
            break
        parent = posixpath.dirname(check_path.rstrip("/"))
        if parent == check_path or not parent:
            break
        check_path = parent
    if rc != 0:
        print(f"  {C.YELLOW}Could not check remote space — continuing anyway{C.RESET}")
        return True  # don't block copy just because df failed

    lines = out.strip().split("\n")
    if len(lines) < 2:
        if force:
            return True
        print(f"  {C.RED}Could not parse remote df output{C.RESET}")
        return False

    parts = lines[1].split()
    try:
        total = int(parts[1])
        free = int(parts[3])
    except (IndexError, ValueError):
        if force:
            return True
        return False

    pct_free = free / total * 100 if total else 0
    print(f"  Destination disk (remote):")
    print(f"    Total:     {C.BOLD}{fmt_size(total)}{C.RESET}")
    print(f"    Free:      {C.BOLD}{fmt_size(free)}{C.RESET} ({pct_free:.1f}% free)")
    print(f"    Required:  {C.BOLD}{fmt_size(required_bytes)}{C.RESET}")

    if required_bytes > free:
        shortfall = required_bytes - free
        print(f"\n  {C.RED}✗ NOT ENOUGH SPACE — need {fmt_size(shortfall)} more{C.RESET}")
        if force:
            print(f"  {C.YELLOW}Proceeding anyway (--force){C.RESET}")
            return True
        return False

    print(f"    Headroom:  {fmt_size(free - required_bytes)}")
    print(f"\n  {C.GREEN}✓ Enough space{C.RESET}")
    return True


def ensure_remote_dirs(ssh, remote_root, entries):
    """Create all needed directories on remote in one SSH call."""
    dirs = sorted(set(
        posixpath.join(remote_root, posixpath.dirname(e.rel))
        for e in entries if posixpath.dirname(e.rel)
    ))
    if not dirs:
        return
    # Batch mkdir -p
    dir_args = " ".join(shlex.quote(d) for d in dirs)
    ssh.exec_cmd(f"mkdir -p {dir_args}")


import hmac as _hmac_mod

# Key derived from username + hostname + persistent random salt.
# NOTE: This is an integrity check (detects corruption/accidental edits),
# not cryptographic authentication against a fully compromised remote.
# The random salt prevents key prediction from public info alone.
_MANIFEST_SALT_FILE = os.path.join(os.path.expanduser("~"), ".fast_copy_salt")


def _manifest_key():
    # Load or create a persistent random salt
    salt = b""
    try:
        # All manifest HMACs hinge on this one salt. If it has been replaced by
        # an attacker (different inode, loosened perms, hardlinked), forged
        # manifests would verify — so check it's still a private regular file and
        # warn (once) if not, rather than silently trusting it.
        if _system != "Windows" and os.path.exists(_MANIFEST_SALT_FILE) \
                and not getattr(_manifest_key, "_warned", False):
            st = os.lstat(_MANIFEST_SALT_FILE)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) \
                    or st.st_nlink > 1 or (st.st_mode & 0o077):
                print(f"  {C.YELLOW}Warning: {_MANIFEST_SALT_FILE} is not a "
                      f"private regular file (mode={oct(st.st_mode & 0o777)}, "
                      f"links={st.st_nlink}); manifest integrity may be "
                      f"compromised. Fix: verify and chmod 600.{C.RESET}",
                      file=sys.stderr)
                _manifest_key._warned = True
        with open(_MANIFEST_SALT_FILE, "rb") as f:
            salt = f.read(32)
    except (IOError, OSError):
        pass
    if len(salt) < 32:
        salt = os.urandom(32)
        try:
            old_umask = os.umask(0o077)
            try:
                with open(_MANIFEST_SALT_FILE, "wb") as f:
                    f.write(salt)
            finally:
                os.umask(old_umask)
        except (IOError, OSError):
            pass  # proceed with ephemeral salt — manifests won't persist across runs
    return hashlib.sha256(
        f"fast_copy:{getpass.getuser()}:{platform.node()}:".encode() + salt
    ).digest()


def _read_remote_file(ssh, path):
    """Read a file from remote, trying SFTP first then exec."""
    try:
        sftp = ssh.open_sftp()
        with sftp.open(path, "r") as f:
            return f.read().decode("utf-8")
    except Exception:
        pass
    # Fallback: exec
    try:
        out, _, rc = ssh.exec_cmd(f"cat {shlex.quote(path)}", timeout=30)
        if rc == 0 and out.strip():
            return out
    except Exception:
        pass
    return None


def _write_remote_file(ssh, path, content):
    """Write a file to remote, trying SFTP first then exec."""
    try:
        sftp = ssh.open_sftp()
        with sftp.open(path, "w") as f:
            f.write(content.encode("utf-8") if isinstance(content, str) else content)
        return
    except Exception:
        pass
    # Fallback: exec
    try:
        ssh.exec_cmd(
            f"cat > {shlex.quote(path)}", input_data=content, timeout=30
        )
    except Exception:
        pass


def load_remote_manifest(ssh, remote_root):
    """Load previous-run manifest from remote. Verifies HMAC. Returns dict or empty."""
    manifest_path = posixpath.join(remote_root, REMOTE_MANIFEST_NAME)
    try:
        raw = _read_remote_file(ssh, manifest_path)
        if not raw:
            return {}
        data = json.loads(raw)
        stored_mac = data.pop("__hmac__", None)
        if stored_mac is None:
            return {}  # unsigned manifest — treat as absent
        payload = json.dumps(data, sort_keys=True).encode()
        expected = _hmac_mod.new(_manifest_key(), payload, hashlib.sha256).hexdigest()
        if not _hmac_mod.compare_digest(stored_mac, expected):
            return {}  # tampered — ignore
        return data
    except (IOError, OSError, json.JSONDecodeError, KeyError):
        return {}


def save_remote_manifest(ssh, remote_root, entries, link_map):
    """Save HMAC-signed manifest after successful copy."""
    manifest = {}
    for e in entries:
        if e.content_hash:
            manifest[e.rel] = {"size": e.size, "hash": e.content_hash}
    for dup_rel, target in link_map.items():
        if isinstance(target, tuple):
            continue
        for e in entries:
            if e.rel == target and e.content_hash:
                manifest[dup_rel] = {"size": e.size, "hash": e.content_hash}
                break

    # Sign with HMAC
    payload = json.dumps(manifest, sort_keys=True).encode()
    mac = _hmac_mod.new(_manifest_key(), payload, hashlib.sha256).hexdigest()
    manifest["__hmac__"] = mac

    manifest_path = posixpath.join(remote_root, REMOTE_MANIFEST_NAME)
    _write_remote_file(ssh, manifest_path, json.dumps(manifest))


def scan_remote_destination(ssh, remote_root):
    """Get file listing from remote in one SSH call. Returns {rel_path: size}."""
    # Check if remote directory exists first
    _, _, rc = ssh.exec_cmd(f'test -d {shlex.quote(remote_root)}', timeout=10)
    if rc != 0:
        return {}  # directory doesn't exist yet — nothing to compare

    if ssh.caps.get("gnu_find"):
        cmd = f'find {shlex.quote(remote_root)} -type f -printf "%s\\t%p\\n" 2>/dev/null'
        out, _, rc = ssh.exec_cmd(cmd)
        result = {}
        for line in out.strip().split("\n"):
            if not line:
                continue
            parts = line.split("\t", 1)
            if len(parts) == 2:
                try:
                    size = int(parts[0])
                    path = parts[1]
                    rel = posixpath.relpath(path, remote_root)
                    result[rel] = size
                except (ValueError, TypeError):
                    continue
        return result
    else:
        # Portable fallback: find + stat (Linux stat -c, not BSD stat -f)
        cmd = (f'find {shlex.quote(remote_root)} -type f '
               f'-exec stat -c "%s %n" {{}} + 2>/dev/null || '
               f'find {shlex.quote(remote_root)} -type f '
               f'-exec stat -f "%z %N" {{}} + 2>/dev/null')
        out, _, rc = ssh.exec_cmd(cmd)
        result = {}
        for line in out.strip().split("\n"):
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) == 2:
                try:
                    size = int(parts[0])
                    path = parts[1]
                    rel = posixpath.relpath(path, remote_root)
                    result[rel] = size
                except (ValueError, TypeError):
                    continue
        return result


def remote_hash_files(ssh, remote_root, rel_paths):
    """Hash files on remote in batches. Returns {rel_path: hash_hex}."""
    if not rel_paths:
        return {}

    BATCH_SIZE = 5000  # files per SSH command to avoid channel timeouts
    result = {}

    for batch_start in range(0, len(rel_paths), BATCH_SIZE):
        batch = rel_paths[batch_start:batch_start + BATCH_SIZE]
        full_paths = [posixpath.join(remote_root, rp) for rp in batch]
        path_input = "\n".join(full_paths) + "\n"

        if ssh.caps.get("python3"):
            script = (
                'import sys,hashlib\n'
                'for line in sys.stdin:\n'
                '  p=line.strip()\n'
                '  h=hashlib.sha256()\n'
                '  try:\n'
                '    with open(p,"rb") as f:\n'
                '      while True:\n'
                '        c=f.read(1048576)\n'
                '        if not c:break\n'
                '        h.update(c)\n'
                '    print(h.hexdigest(),p)\n'
                '  except Exception:print("ERROR",p)\n'
            )
            out, _, _ = ssh.exec_cmd(
                f"python3 -c {shlex.quote(script)}", input_data=path_input,
                timeout=600
            )
        elif ssh.caps.get("sha256sum"):
            out, _, _ = ssh.exec_cmd(
                "xargs -d '\\n' sha256sum",
                input_data=path_input, timeout=600
            )
        else:
            return {}  # can't hash remotely

        for line in out.strip().split("\n"):
            if not line or line.startswith("ERROR"):
                continue
            parts = line.split(None, 1)
            if len(parts) == 2:
                h, path = parts
                rel = posixpath.relpath(path.strip(), remote_root)
                result[rel] = h

        if len(rel_paths) > BATCH_SIZE:
            done = min(batch_start + BATCH_SIZE, len(rel_paths))
            print(f"\r  {C.DIM}Hashed {done}/{len(rel_paths)} files on remote...{C.RESET}", end="", flush=True)

    if len(rel_paths) > BATCH_SIZE:
        print()  # newline after progress
    return result


# ════════════════════════════════════════════════════════════════════════════
# REMOTE METADATA ENUMERATION (for R2L --preserve xattr / acl)
# ════════════════════════════════════════════════════════════════════════════
# When copying remote→local with --preserve xattr or --preserve acl, we
# can't reach the source files through the local syscalls our usual
# preserve helpers use. We run a small script on the remote that reads
# rel paths from stdin and emits a parseable per-file record.
#
# Record format (one per file, tab-delimited):
#   F <base64-of-rel-path>           start of record
#   X <name> <base64-of-value>       repeated per xattr (user.* namespace only)
#   A <single acl line>              repeated per ACL entry
#   .                                end of record
#
# python3 on the remote is preferred (handles binary xattr values cleanly
# via base64); we degrade silently if it's missing — xattr/ACL just won't
# be collected, and the existing warning informs the user.
def _remote_collect_metadata(ssh, remote_root, rel_paths,
                             want_xattr, want_acl, want_owner=False):
    """Collect xattrs, ACLs, and/or owner uid/gid for the given remote rel_paths.

    Returns {rel_path: {"xattrs": {name: bytes}, "acl_lines": [str, ...],
                        "owner": (uid, gid) or None}}.
    Files with no requested metadata present are omitted. On any
    infrastructure error (no python3 on remote, channel timeout) returns
    an empty dict — the end-of-run summary will then report '0 preserved'.

    want_owner is used by R2R: we collect uid/gid from the source so the
    destination apply step can chown to the same numeric ids, regardless
    of whether the tar relay's filter preserved them."""
    if not rel_paths or (not want_xattr and not want_acl and not want_owner):
        return {}
    if not ssh.caps.get("python3"):
        return {}

    BATCH_SIZE = 5000
    result = {}
    want_xattr_int = 1 if want_xattr else 0
    want_acl_int = 1 if want_acl else 0
    want_owner_int = 1 if want_owner else 0

    for batch_start in range(0, len(rel_paths), BATCH_SIZE):
        batch = rel_paths[batch_start:batch_start + BATCH_SIZE]
        full_paths = [posixpath.join(remote_root, rp) for rp in batch]
        path_input = "\n".join(full_paths) + "\n"

        # The remote script: read paths from stdin, emit one record per
        # file. Uses os.listxattr/getxattr (Linux/macOS) for xattrs and a
        # getfacl subprocess for ACLs. Errors per file are silenced — the
        # local side just won't see a record for that file.
        script = (
            "import sys,os,base64,subprocess\n"
            f"WX={want_xattr_int}\nWA={want_acl_int}\nWO={want_owner_int}\n"
            "for line in sys.stdin:\n"
            "  p=line.rstrip('\\n')\n"
            "  if not p: continue\n"
            "  try: st=os.stat(p)\n"
            "  except OSError: continue\n"
            "  print('F\\t'+base64.b64encode(p.encode()).decode())\n"
            "  if WO:\n"
            "    print('O\\t'+str(st.st_uid)+'\\t'+str(st.st_gid))\n"
            "  if WX and hasattr(os,'listxattr'):\n"
            "    try:\n"
            "      for n in os.listxattr(p, follow_symlinks=False):\n"
            "        if not n.startswith('user.'): continue\n"
            "        v=os.getxattr(p,n,follow_symlinks=False)\n"
            "        print('X\\t'+n+'\\t'+base64.b64encode(v).decode())\n"
            "    except OSError: pass\n"
            "  if WA:\n"
            "    try:\n"
            "      r=subprocess.run(['getfacl','-p','-E','--',p],\n"
            "                       capture_output=True,timeout=5)\n"
            "      if r.returncode==0:\n"
            "        for ln in r.stdout.decode('utf-8','replace').splitlines():\n"
            "          ln=ln.strip()\n"
            "          if ln and not ln.startswith('#'):\n"
            "            print('A\\t'+ln)\n"
            "    except (OSError,FileNotFoundError): pass\n"
            "  print('.')\n"
        )

        try:
            out, _, _ = ssh.exec_cmd(
                f"python3 -c {shlex.quote(script)}", input_data=path_input,
                timeout=600,
            )
        except Exception:
            return {}

        # Parse records.
        cur_full = None
        cur = None
        for line in out.split("\n"):
            if not line:
                continue
            if line == ".":
                if cur_full is not None and cur is not None and (
                        cur["xattrs"] or cur["acl_lines"] or cur.get("owner")):
                    rel = posixpath.relpath(cur_full, remote_root)
                    result[rel] = cur
                cur_full = None
                cur = None
                continue
            tag, _, rest = line.partition("\t")
            if tag == "F":
                try:
                    cur_full = base64.b64decode(rest).decode("utf-8", "replace")
                except Exception:
                    cur_full = None
                cur = {"xattrs": {}, "acl_lines": [], "owner": None}
            elif tag == "O" and cur is not None:
                uid_s, _, gid_s = rest.partition("\t")
                try:
                    cur["owner"] = (int(uid_s), int(gid_s))
                except ValueError:
                    pass
            elif tag == "X" and cur is not None:
                name, _, val_b64 = rest.partition("\t")
                if name:
                    try:
                        cur["xattrs"][name] = base64.b64decode(val_b64)
                    except Exception:
                        pass
            elif tag == "A" and cur is not None:
                cur["acl_lines"].append(rest)

        if len(rel_paths) > BATCH_SIZE:
            done = min(batch_start + BATCH_SIZE, len(rel_paths))
            print(f"\r  {C.DIM}Collected remote metadata for "
                  f"{done}/{len(rel_paths)} files...{C.RESET}",
                  end="", flush=True)

    if len(rel_paths) > BATCH_SIZE:
        print()
    return result


def _push_metadata_to_remote(ssh, remote_root, entries, src_root,
                             want_owner, want_xattr, want_acl):
    """Symmetric to _remote_collect_metadata: reads metadata from the LOCAL
    source tree, ships a serialized payload to the remote, and runs a
    python3 script there that applies via os.chown/os.setxattr/setfacl.

    Used for L2R copies under --preserve owner/xattr/acl. Requires python3
    on the remote; gracefully no-ops if it's missing. Owner application
    requires the SSH user on the remote to have CAP_CHOWN (typically root);
    failed chowns are counted but don't abort.

    Returns silently — counters update via _preserve_stats so the
    end-of-run summary reflects what actually happened."""
    if not entries or (not want_owner and not want_xattr and not want_acl):
        return
    if not ssh.caps.get("python3"):
        if want_owner:
            _preserve_stats["owner_skip_unprivileged"] += len(entries)
        if want_xattr:
            _preserve_stats["xattr_skip_unsupported"] += len(entries)
        if want_acl:
            _preserve_stats["acl_skip_unsupported"] += len(entries)
        return

    BATCH_SIZE = 5000
    import subprocess as _sp

    # Serialize a batch of entries into the wire format the remote script
    # consumes. One record per file, '.' terminator, tab-delimited fields.
    def _build_payload(batch):
        parts = []
        for entry in batch:
            src_path = entry.src
            rel = entry.rel
            try:
                lst = os.lstat(src_path)
            except OSError:
                continue
            if stat.S_ISLNK(lst.st_mode):
                continue
            parts.append("F\t" + base64.b64encode(rel.encode()).decode())
            if want_owner:
                parts.append(f"O\t{lst.st_uid}\t{lst.st_gid}")
            if want_xattr and hasattr(os, "listxattr"):
                try:
                    for n in os.listxattr(src_path, follow_symlinks=False):
                        if not n.startswith("user."):
                            continue
                        v = os.getxattr(src_path, n, follow_symlinks=False)
                        parts.append("X\t" + n + "\t" +
                                     base64.b64encode(v).decode())
                except OSError:
                    pass
            if want_acl and _system == "Linux":
                try:
                    r = _sp.run(["getfacl", "-p", "-E", "--", src_path],
                                capture_output=True, timeout=5)
                    if r.returncode == 0:
                        for ln in r.stdout.decode("utf-8", "replace").splitlines():
                            ln = ln.strip()
                            if ln and not ln.startswith("#"):
                                parts.append("A\t" + ln)
                except (OSError, _sp.SubprocessError, FileNotFoundError):
                    pass
            parts.append(".")
        return "\n".join(parts) + "\n" if parts else ""

    cmd = (f"python3 -c {shlex.quote(_REMOTE_APPLY_SCRIPT)} "
           f"{shlex.quote(remote_root)} {int(want_xattr)} "
           f"{int(want_acl)} {int(want_owner)}")

    for batch_start in range(0, len(entries), BATCH_SIZE):
        batch = entries[batch_start:batch_start + BATCH_SIZE]
        payload = _build_payload(batch)
        if not payload:
            continue
        try:
            out, _, _ = ssh.exec_cmd(cmd, input_data=payload, timeout=600)
        except Exception:
            return
        _parse_remote_apply_stats(out)
        if len(entries) > BATCH_SIZE:
            done = min(batch_start + BATCH_SIZE, len(entries))
            print(f"\r  {C.DIM}Applied remote metadata for "
                  f"{done}/{len(entries)} files...{C.RESET}",
                  end="", flush=True)
    if len(entries) > BATCH_SIZE:
        print()


def _apply_collected_metadata_to_remote(dst_ssh, dst_root, meta,
                                        want_owner, want_xattr, want_acl):
    """Apply already-collected metadata records to a remote destination.

    Used by R2R: we collect from the source remote via
    _remote_collect_metadata, then call this to push the records onto the
    destination remote. Mechanically symmetric to _push_metadata_to_remote
    but the records are pre-collected rather than read from local files."""
    if not meta or (not want_owner and not want_xattr and not want_acl):
        return
    if not dst_ssh.caps.get("python3"):
        n = len(meta)
        if want_owner:
            _preserve_stats["owner_skip_unprivileged"] += n
        if want_xattr:
            _preserve_stats["xattr_skip_unsupported"] += n
        if want_acl:
            _preserve_stats["acl_skip_unsupported"] += n
        return

    BATCH_SIZE = 5000
    items = list(meta.items())

    def _build_payload(batch):
        parts = []
        for rel, rec in batch:
            parts.append("F\t" + base64.b64encode(rel.encode()).decode())
            if want_owner and rec.get("owner"):
                uid, gid = rec["owner"]
                parts.append(f"O\t{uid}\t{gid}")
            if want_xattr:
                for n, v in rec.get("xattrs", {}).items():
                    parts.append("X\t" + n + "\t" +
                                 base64.b64encode(v).decode())
            if want_acl:
                for ln in rec.get("acl_lines", []):
                    parts.append("A\t" + ln)
            parts.append(".")
        return "\n".join(parts) + "\n" if parts else ""

    cmd = (f"python3 -c {shlex.quote(_REMOTE_APPLY_SCRIPT)} "
           f"{shlex.quote(dst_root)} {int(want_xattr)} "
           f"{int(want_acl)} {int(want_owner)}")

    for batch_start in range(0, len(items), BATCH_SIZE):
        batch = items[batch_start:batch_start + BATCH_SIZE]
        payload = _build_payload(batch)
        if not payload:
            continue
        try:
            out, _, _ = dst_ssh.exec_cmd(cmd, input_data=payload, timeout=600)
        except Exception:
            return
        _parse_remote_apply_stats(out)


# Shared apply script + STATS parser, extracted so both _push_metadata_to_remote
# (L2R) and _apply_collected_metadata_to_remote (R2R) use the same mechanism.
_REMOTE_APPLY_SCRIPT = (
    "import sys,os,base64,subprocess\n"
    "ROOT=sys.argv[1]\n"
    "WX,WA,WO = int(sys.argv[2]),int(sys.argv[3]),int(sys.argv[4])\n"
    "ok_o=err_o=ok_x=err_x=ok_a=err_a=skip_x=skip_a=0\n"
    "cur=None\n"
    "for raw in sys.stdin:\n"
    "  line=raw.rstrip('\\n')\n"
    "  if not line: continue\n"
    "  if line=='.':\n"
    "    if cur is not None and cur.get('rel'):\n"
    "      full=os.path.join(ROOT, cur['rel'])\n"
    "      if WO and 'owner' in cur:\n"
    "        try:\n"
    "          os.chown(full, cur['owner'][0], cur['owner'][1])\n"
    "          ok_o+=1\n"
    "        except OSError: err_o+=1\n"
    "      if WX and cur['xattrs']:\n"
    "        wrote=False\n"
    "        for n,v in cur['xattrs'].items():\n"
    "          try:\n"
    "            os.setxattr(full, n, v, follow_symlinks=False); wrote=True\n"
    "          except OSError as e:\n"
    "            if e.errno in (95,):\n"
    "              skip_x+=1; wrote=False; break\n"
    "            err_x+=1\n"
    "        if wrote: ok_x+=1\n"
    "      if WA and cur['acl_lines']:\n"
    "        payload='# file: '+full+'\\n'+'\\n'.join(cur['acl_lines'])+'\\n'\n"
    "        try:\n"
    "          r=subprocess.run(['setfacl','--restore=-'],\n"
    "                           input=payload.encode(),capture_output=True,timeout=10)\n"
    "          if r.returncode==0: ok_a+=1\n"
    "          else: err_a+=1\n"
    "        except (OSError,FileNotFoundError): skip_a+=1\n"
    "    cur=None\n"
    "    continue\n"
    "  tag,_,rest=line.partition('\\t')\n"
    "  if tag=='F':\n"
    "    try: rel=base64.b64decode(rest).decode('utf-8','replace')\n"
    "    except Exception: rel=None\n"
    "    cur={'rel':rel,'xattrs':{},'acl_lines':[]}\n"
    "  elif tag=='O' and cur is not None:\n"
    "    a,_,b=rest.partition('\\t')\n"
    "    try: cur['owner']=(int(a),int(b))\n"
    "    except ValueError: pass\n"
    "  elif tag=='X' and cur is not None:\n"
    "    n,_,vb=rest.partition('\\t')\n"
    "    try: cur['xattrs'][n]=base64.b64decode(vb)\n"
    "    except Exception: pass\n"
    "  elif tag=='A' and cur is not None:\n"
    "    cur['acl_lines'].append(rest)\n"
    "print(f'STATS\\towner_ok\\t{ok_o}\\towner_err\\t{err_o}')\n"
    "print(f'STATS\\txattr_ok\\t{ok_x}\\txattr_err\\t{err_x}\\txattr_skip\\t{skip_x}')\n"
    "print(f'STATS\\tacl_ok\\t{ok_a}\\tacl_err\\t{err_a}\\tacl_skip\\t{skip_a}')\n"
)


def _parse_remote_apply_stats(out):
    """Parse STATS lines from the remote apply script and update counters."""
    for line in out.split("\n"):
        if not line.startswith("STATS\t"):
            continue
        fields = line.split("\t")
        i = 1
        while i + 1 < len(fields):
            key = fields[i]
            try:
                val = int(fields[i + 1])
            except ValueError:
                val = 0
            if key == "owner_ok":
                _preserve_stats["owner_ok"] += val
            elif key == "owner_err":
                _preserve_stats["owner_err"] += val
            elif key == "xattr_ok":
                _preserve_stats["xattr_ok"] += val
            elif key == "xattr_err":
                _preserve_stats["xattr_err"] += val
            elif key == "xattr_skip":
                _preserve_stats["xattr_skip_unsupported"] += val
            elif key == "acl_ok":
                _preserve_stats["acl_ok"] += val
            elif key == "acl_err":
                _preserve_stats["acl_err"] += val
            elif key == "acl_skip":
                _preserve_stats["acl_skip_unsupported"] += val
            i += 2


def _apply_remote_metadata_local(meta, dst_root, want_xattr, want_acl):
    """Apply collected remote metadata to the local destination tree.

    Counters: this increments xattr_ok / acl_ok the same way the local
    preserve path does, so the end-of-run summary stays consistent."""
    import subprocess
    for rel, rec in meta.items():
        dst_path = os.path.join(dst_root, rel)
        if not os.path.isfile(dst_path):
            continue
        # xattrs
        if want_xattr and rec["xattrs"]:
            if _preserve_dst_caps["xattr"] is False:
                _preserve_stats["xattr_skip_unsupported"] += 1
                continue
            wrote_any = False
            for name, value in rec["xattrs"].items():
                try:
                    os.setxattr(dst_path, name, value, follow_symlinks=False)
                    wrote_any = True
                except OSError as e:
                    if e.errno in (errno.ENOTSUP, errno.EOPNOTSUPP):
                        _preserve_stats["xattr_skip_unsupported"] += 1
                        break
                    _preserve_stats["xattr_err"] += 1
            if wrote_any:
                _preserve_stats["xattr_ok"] += 1
        # ACLs
        if want_acl and rec["acl_lines"]:
            if _preserve_dst_caps["acl"] is False:
                _preserve_stats["acl_skip_unsupported"] += 1
                continue
            payload = f"# file: {dst_path}\n" + "\n".join(rec["acl_lines"]) + "\n"
            try:
                r = subprocess.run(["setfacl", "--restore=-"],
                                   input=payload.encode("utf-8"),
                                   capture_output=True, timeout=10)
                if r.returncode == 0:
                    _preserve_stats["acl_ok"] += 1
                else:
                    _preserve_stats["acl_err"] += 1
            except (OSError, subprocess.SubprocessError, FileNotFoundError):
                _preserve_stats["acl_err"] += 1


def filter_unchanged_remote(entries, link_map, ssh, remote_root,
                            src_ssh=None, src_root=None):
    """Incremental check against remote. Always scans remote for actual
    file existence; the manifest is used only as a hash cache to avoid
    re-hashing unchanged files. This prevents stale-manifest data loss
    when files are deleted out of band.

    For R2R mode, pass src_ssh/src_root to hash source files on the remote."""
    print(f"  {C.DIM}Checking remote for existing files...{C.RESET}", end="", flush=True)

    # ALWAYS scan the actual remote — never trust the manifest for existence.
    # The manifest is a cache from a previous run; the user may have deleted
    # or moved files since then. Truth is the filesystem.
    remote_files = scan_remote_destination(ssh, remote_root)

    # Load the manifest (if present) ONLY as a hash cache, and only trust
    # entries whose size matches what's currently on the remote (otherwise
    # the cached hash is invalid).
    manifest = load_remote_manifest(ssh, remote_root)
    remote_hashes = {}
    if manifest:
        valid_cached = 0
        for k, v in manifest.items():
            if k in remote_files and remote_files[k] == v.get("size"):
                h = v.get("hash")
                if h:
                    remote_hashes[k] = h
                    valid_cached += 1
        print(f"\r  {C.DIM}Scanned remote ({len(remote_files)} files), "
              f"manifest cache hits: {valid_cached}{C.RESET}          ")
    else:
        print(f"\r  {C.DIM}Scanned remote ({len(remote_files)} files), "
              f"no manifest{C.RESET}          ")

    need_copy = []
    need_hash = []
    skipped = []
    skipped_bytes = 0

    # Quick pass: size check
    for entry in entries:
        if entry.rel not in remote_files:
            need_copy.append(entry)
        elif remote_files[entry.rel] != entry.size:
            need_copy.append(entry)
        else:
            # Same size — check hash if available in manifest
            if entry.rel in remote_hashes and remote_hashes[entry.rel]:
                if entry.content_hash and entry.content_hash == remote_hashes[entry.rel]:
                    _log("skipped", entry.rel, entry.size, reason="unchanged")
                    skipped.append(entry)
                    skipped_bytes += entry.size
                else:
                    need_hash.append(entry)
            else:
                need_hash.append(entry)

    # Hash pass for same-size files without manifest match
    if need_hash:
        print(f"  {C.DIM}Hashing {len(need_hash)} files on remote...{C.RESET}", end="", flush=True)
        remote_h = remote_hash_files(ssh, remote_root, [e.rel for e in need_hash])
        # Hash source files: on remote source (R2R) or locally (L2R)
        if src_ssh and src_root:
            src_h = remote_hash_files(src_ssh, src_root, [e.rel for e in need_hash])
        else:
            src_h = None
        for entry in need_hash:
            rh = remote_h.get(entry.rel)
            if rh and entry.content_hash:
                # Get source sha256: from remote source (R2R) or local file (L2R)
                if src_h is not None:
                    local_sha = src_h.get(entry.rel)
                else:
                    local_sha = hash_file_sha256(entry.src)
                if local_sha == rh:
                    _log("skipped", entry.rel, entry.size, reason="unchanged")
                    skipped.append(entry)
                    skipped_bytes += entry.size
                    continue
            need_copy.append(entry)

    # Filter link_map
    new_link_map = {}
    skipped_links = 0
    for dup_rel, canonical_rel in link_map.items():
        if dup_rel in remote_files:
            _log("skipped", dup_rel, 0, reason="link_exists")
            skipped_links += 1
        else:
            new_link_map[dup_rel] = canonical_rel

    print(f"\r  {C.GREEN}Remote incremental check complete:{C.RESET}                              ")
    print(f"    To copy:   {C.BOLD}{len(need_copy)}{C.RESET} files "
          f"({fmt_size(sum(e.size for e in need_copy))})")
    print(f"    Skipped:   {C.BOLD}{len(skipped)}{C.RESET} files unchanged "
          f"({C.GREEN}{fmt_size(skipped_bytes)}{C.RESET})")
    if skipped_links:
        print(f"    Links:     {C.BOLD}{skipped_links}{C.RESET} already exist, "
              f"{C.BOLD}{len(new_link_map)}{C.RESET} to create")

    return need_copy, new_link_map, len(skipped) + skipped_links, skipped_bytes


def hash_file_sha256(filepath):
    """Hash file with SHA-256 (for comparing with remote sha256sum)."""
    h = hashlib.sha256()
    try:
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(HASH_CHUNK)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


# ════════════════════════════════════════════════════════════════════════════
# SPACE CHECK
# ════════════════════════════════════════════════════════════════════════════
def _makedirs_or_die(path, what="destination"):
    """Create a directory tree, turning an OSError (e.g. a read-only or
    permission-denied destination) into a clean single-line error instead of a
    raw Python traceback."""
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        raise SystemExit(f"Error: cannot create {what} {path!r}: "
                         f"{e.strerror or e}")


def check_destination_space(dst, required_bytes, force=False):
    """
    Check if destination has enough free space.
    Creates the destination directory if needed to query its filesystem.
    Returns True if OK to proceed, False to abort.
    """
    # Create dst if it doesn't exist so we can stat its filesystem
    _makedirs_or_die(dst)

    try:
        usage = shutil.disk_usage(dst)
        free = usage.free
        total = usage.total
    except OSError as e:
        print(f"  {C.YELLOW}Warning: Could not check free space: {e}{C.RESET}")
        if force:
            print(f"  {C.YELLOW}--force: proceeding anyway{C.RESET}")
            return True
        print(f"  Use --force to skip this check.")
        return False

    pct_used = (total - free) / total * 100 if total > 0 else 0

    print(f"  Destination disk:")
    print(f"    Total:     {C.BOLD}{fmt_size(total)}{C.RESET}")
    print(f"    Free:      {C.BOLD}{fmt_size(free)}{C.RESET} ({100 - pct_used:.1f}% free)")
    print(f"    Required:  {C.BOLD}{fmt_size(required_bytes)}{C.RESET}")

    if required_bytes > free:
        shortfall = required_bytes - free
        print(f"\n  {C.RED}✗ NOT ENOUGH SPACE — need {fmt_size(shortfall)} more{C.RESET}")
        print(f"  {C.RED}  Source: {fmt_size(required_bytes)} > Free: {fmt_size(free)}{C.RESET}")
        if force:
            print(f"\n  {C.YELLOW}--force: proceeding anyway (copy may fail mid-way){C.RESET}")
            return True
        print(f"\n  Use --force to attempt anyway, or free up space on the destination.")
        return False

    headroom = free - required_bytes
    print(f"    Headroom:  {C.GREEN}{fmt_size(headroom)}{C.RESET}")
    print(f"\n  {C.GREEN}✓ Enough space{C.RESET}")
    return True


# ════════════════════════════════════════════════════════════════════════════
# PHYSICAL OFFSET DETECTION — WINDOWS
# ════════════════════════════════════════════════════════════════════════════
def get_physical_offset_windows(filepath):
    """Use FSCTL_GET_RETRIEVAL_POINTERS for starting LCN."""
    try:
        import ctypes.wintypes as wt

        GENERIC_READ = 0x80000000
        FILE_SHARE_READ = 1
        FILE_SHARE_WRITE = 2
        OPEN_EXISTING = 3
        FSCTL_GET_RETRIEVAL_POINTERS = 0x00090073

        kernel32 = ctypes.windll.kernel32
        CreateFileW = kernel32.CreateFileW
        CreateFileW.restype = wt.HANDLE
        DeviceIoControl = kernel32.DeviceIoControl
        CloseHandle = kernel32.CloseHandle

        handle = CreateFileW(
            str(filepath), GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None, OPEN_EXISTING, 0, None,
        )
        INVALID = wt.HANDLE(-1).value
        if handle == INVALID:
            return 0

        try:
            in_buf = struct.pack("<Q", 0)
            out_size = 16 + 16 * 64
            out_buf = ctypes.create_string_buffer(out_size)
            bytes_returned = wt.DWORD(0)

            ok = DeviceIoControl(
                handle, FSCTL_GET_RETRIEVAL_POINTERS,
                in_buf, len(in_buf),
                out_buf, out_size,
                ctypes.byref(bytes_returned), None,
            )
            if not ok:
                return 0

            raw = out_buf.raw[:bytes_returned.value]
            if len(raw) < 32:
                return 0

            extent_count = struct.unpack_from("<I", raw, 0)[0]
            if extent_count == 0:
                return 0

            first_lcn = struct.unpack_from("<q", raw, 24)[0]
            return first_lcn if first_lcn >= 0 else 0
        finally:
            CloseHandle(handle)
    except (OSError, ValueError, struct.error):
        return 0


# ════════════════════════════════════════════════════════════════════════════
# PHYSICAL OFFSET DETECTION — LINUX
# ════════════════════════════════════════════════════════════════════════════
def get_physical_offset_linux(filepath):
    """Use FIEMAP ioctl for physical block offset."""
    try:
        import fcntl

        FS_IOC_FIEMAP = 0xC020660B
        FIEMAP_SIZE = 32
        EXTENT_SIZE = 56
        FIEMAP_FLAG_SYNC = 0x00000001

        fd = os.open(filepath, os.O_RDONLY)
        try:
            fiemap = bytearray(FIEMAP_SIZE + EXTENT_SIZE)
            struct.pack_into("<Q", fiemap, 0, 0)
            struct.pack_into("<Q", fiemap, 8, 0xFFFFFFFFFFFFFFFF)
            struct.pack_into("<I", fiemap, 16, FIEMAP_FLAG_SYNC)
            struct.pack_into("<I", fiemap, 24, 1)

            fcntl.ioctl(fd, FS_IOC_FIEMAP, fiemap)

            mapped = struct.unpack_from("<I", fiemap, 20)[0]
            if mapped == 0:
                return 0

            physical = struct.unpack_from("<Q", fiemap, FIEMAP_SIZE + 8)[0]
            return physical
        finally:
            os.close(fd)
    except (OSError, ValueError, struct.error):
        try:
            import fcntl
            FIBMAP = 1
            fd = os.open(filepath, os.O_RDONLY)
            try:
                block = struct.pack("<I", 0)
                result = fcntl.ioctl(fd, FIBMAP, block)
                return struct.unpack("<I", result)[0]
            finally:
                os.close(fd)
        except (OSError, ValueError, struct.error):
            return 0


# ════════════════════════════════════════════════════════════════════════════
# PHYSICAL OFFSET DETECTION — MACOS (heuristic)
# ════════════════════════════════════════════════════════════════════════════
def get_physical_offset_macos(filepath):
    """inode number as proxy for physical position."""
    try:
        return os.stat(filepath).st_ino
    except OSError:
        return 0


# ════════════════════════════════════════════════════════════════════════════
# UNIFIED OFFSET GETTER
# ════════════════════════════════════════════════════════════════════════════
_system = platform.system()


# ════════════════════════════════════════════════════════════════════════════
# FILESYSTEM DETECTION (originally fs_detect.py — inlined to preserve
# single-file distribution)
#
# Detects the destination filesystem type and probes its actual capabilities
# (hardlink, symlink, reflink CoW clones, case sensitivity) so dedup can
# pick a safe and efficient strategy:
#
#   - reflink (CoW clones): safest, modifications never affect peer files
#   - hardlink: efficient but requires explicit link-management on update
#   - symlink:  rare fallback for filesystems without hardlinks
#   - none:     no on-disk dedup; transfer-only optimization
#
# Approach: cheap FS type lookup (per-OS API) → skip probes for known-stable
# filesystems → run targeted probes for ambiguous ones (XFS reflink, NTFS
# Dev Drive, network mounts, FUSE). ~5 ms total on warm cache.
# ════════════════════════════════════════════════════════════════════════════

# Public types -------------------------------------------------------------

FSCapabilities = namedtuple("FSCapabilities", [
    "hardlink",        # os.link() works
    "symlink",         # os.symlink() works
    "reflink",         # CoW clone primitive works (FICLONE / clonefile)
    "case_sensitive",  # 'A.txt' and 'a.txt' are distinct files
])

FSInfo = namedtuple("FSInfo", [
    "path",             # destination path that was probed
    "fs_type",          # filesystem name (e.g. "ext4", "btrfs", "NTFS")
    "capabilities",     # FSCapabilities namedtuple
    "strategy",         # recommended dedup strategy
    "detection_ms",     # time spent on FS type detection
    "probe_ms",         # time spent on capability probing (0 if skipped)
    "probes_run",       # list of probe names that were executed
    "probe_timings",    # dict: probe_name -> elapsed ms
    "method",           # which detection method was used
    "from_table",       # True if capabilities came from the lookup table
])


# FS type detection --------------------------------------------------------

def _walk_up_to_existing(path):
    """Walk up the directory tree until we find an existing directory.
    Returns the existing parent, or None on bad input or symlink loops."""
    if not path or not isinstance(path, str):
        return None
    if "\x00" in path:
        return None  # reject null bytes — would corrupt C-API calls below
    try:
        if os.path.exists(path):
            cur = os.path.realpath(path)
        else:
            cur = os.path.abspath(path)
    except (OSError, RuntimeError, ValueError):
        return None
    for _ in range(64):
        if not cur:
            return None
        try:
            if os.path.isdir(cur):
                return cur
        except OSError:
            return None
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent
    return None


def _unescape_mountinfo(field):
    """Decode octal escape sequences in /proc/self/mountinfo (space, tab,
    newline, backslash)."""
    if "\\" not in field:
        return field
    return (field.replace("\\040", " ")
                  .replace("\\011", "\t")
                  .replace("\\012", "\n")
                  .replace("\\134", "\\"))


def _fs_type_linux(path):
    """Parse /proc/self/mountinfo to find the FS type for `path`, using
    longest-prefix mount point match. Handles escaped whitespace."""
    try:
        check = _walk_up_to_existing(path)
        if not check:
            return ("unknown", "linux_mountinfo")

        best_mount = ""
        best_type = "unknown"

        with open("/proc/self/mountinfo") as f:
            for line in f:
                parts = line.split()
                try:
                    sep_idx = parts.index("-")
                    mount_point = _unescape_mountinfo(parts[4])
                    fs_type = _unescape_mountinfo(parts[sep_idx + 1])
                except (IndexError, ValueError):
                    continue

                if check == mount_point or check.startswith(mount_point.rstrip("/") + "/"):
                    if len(mount_point) > len(best_mount):
                        best_mount = mount_point
                        best_type = fs_type

        return (best_type, "linux_mountinfo")
    except (OSError, IOError):
        return ("unknown", "linux_mountinfo")


def _fs_type_macos(path):
    """Use statfs(2) via ctypes on macOS to read f_fstypename."""
    try:
        if not path or not isinstance(path, str) or "\x00" in path:
            return ("unknown", "macos_statfs")
        check = _walk_up_to_existing(path)
        if not check:
            return ("unknown", "macos_statfs")

        class StatFS(ctypes.Structure):
            _fields_ = [
                ("f_bsize",       ctypes.c_uint32),
                ("f_iosize",      ctypes.c_int32),
                ("f_blocks",      ctypes.c_uint64),
                ("f_bfree",       ctypes.c_uint64),
                ("f_bavail",      ctypes.c_uint64),
                ("f_files",       ctypes.c_uint64),
                ("f_ffree",       ctypes.c_uint64),
                ("f_fsid",        ctypes.c_int32 * 2),
                ("f_owner",       ctypes.c_uint32),
                ("f_type",        ctypes.c_uint32),
                ("f_flags",       ctypes.c_uint32),
                ("f_fssubtype",   ctypes.c_uint32),
                ("f_fstypename",  ctypes.c_char * 16),
                ("f_mntonname",   ctypes.c_char * 1024),
                ("f_mntfromname", ctypes.c_char * 1024),
                ("f_reserved",    ctypes.c_uint32 * 8),
            ]

        libc = ctypes.CDLL("libc.dylib")
        libc.statfs.argtypes = [ctypes.c_char_p, ctypes.POINTER(StatFS)]
        libc.statfs.restype = ctypes.c_int

        st = StatFS()
        rc = libc.statfs(check.encode("utf-8"), ctypes.byref(st))
        if rc != 0:
            return ("unknown", "macos_statfs")

        return (st.f_fstypename.decode("utf-8", errors="replace"),
                "macos_statfs")
    except (OSError, AttributeError):
        return ("unknown", "macos_statfs")


def _fs_type_windows(path):
    """Use GetVolumeInformationW to read the FS name. Handles drive-letter
    and UNC paths."""
    try:
        if not path or not isinstance(path, str):
            return ("unknown", "windows_GetVolumeInformation")
        if "\x00" in path:
            return ("unknown", "windows_GetVolumeInformation")

        abs_path = os.path.abspath(path)
        drive, _rest = os.path.splitdrive(abs_path)
        if not drive:
            return ("unknown", "windows_GetVolumeInformation")
        if not drive.endswith("\\"):
            drive = drive + "\\"

        fs_name_buf = ctypes.create_unicode_buffer(256)
        rc = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(drive),
            None, 0, None, None, None,
            fs_name_buf, 256,
        )
        if rc == 0:
            return ("unknown", "windows_GetVolumeInformation")

        return (fs_name_buf.value, "windows_GetVolumeInformation")
    except (OSError, AttributeError, ValueError):
        return ("unknown", "windows_GetVolumeInformation")


def detect_fs_type(path):
    """Detect the filesystem type at `path`. Returns (fs_type, method)."""
    if _system == "Linux":
        return _fs_type_linux(path)
    elif _system == "Darwin":
        return _fs_type_macos(path)
    elif _system == "Windows":
        return _fs_type_windows(path)
    else:
        return ("unknown", "unsupported_os")


# Capability probes --------------------------------------------------------

def _make_probe_dir(parent):
    """Create a unique probe subdirectory inside `parent`.

    Hardened against symlink races: 128 bits of entropy, single-level
    mkdir (not makedirs), mode 0o700, post-create lstat verification.
    """
    name = ".fast_copy_probe_{}_{}".format(os.getpid(), os.urandom(16).hex())
    probe_dir = os.path.join(parent, name)
    os.mkdir(probe_dir, 0o700)
    try:
        st = os.lstat(probe_dir)
    except OSError:
        raise
    if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
        try:
            os.unlink(probe_dir)
        except OSError:
            pass
        raise OSError("probe dir was replaced with non-directory: {}".format(probe_dir))
    return probe_dir


def _cleanup_probe_dir(probe_dir):
    """Remove the probe directory and any leftover files. Symlink-safe
    (uses lstat throughout, never follows links)."""
    if not probe_dir:
        return
    try:
        st = os.lstat(probe_dir)
    except OSError:
        return
    if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
        try:
            os.unlink(probe_dir)
        except OSError:
            pass
        return

    try:
        for root, dirs, files in os.walk(probe_dir, topdown=False,
                                         followlinks=False):
            for fname in files:
                full = os.path.join(root, fname)
                try:
                    entry_st = os.lstat(full)
                    if stat.S_ISLNK(entry_st.st_mode) or \
                       stat.S_ISREG(entry_st.st_mode):
                        os.unlink(full)
                except OSError:
                    pass
            for dname in dirs:
                full = os.path.join(root, dname)
                try:
                    entry_st = os.lstat(full)
                    if stat.S_ISLNK(entry_st.st_mode):
                        os.unlink(full)
                    elif stat.S_ISDIR(entry_st.st_mode):
                        os.rmdir(full)
                except OSError:
                    pass
        os.rmdir(probe_dir)
    except OSError:
        pass


def _safe_probe_unlink(path):
    try:
        if os.path.islink(path) or os.path.exists(path):
            os.unlink(path)
    except OSError:
        pass


def probe_hardlink(probe_dir):
    """Test whether os.link() works in the destination directory."""
    a = os.path.join(probe_dir, "hl_src")
    b = os.path.join(probe_dir, "hl_dst")
    try:
        with open(a, "wb") as f:
            f.write(b"x")
        try:
            os.link(a, b)
            return True
        except (OSError, NotImplementedError):
            return False
    finally:
        _safe_probe_unlink(a)
        _safe_probe_unlink(b)


def probe_symlink(probe_dir):
    """Test whether os.symlink() works (and the result is readable)."""
    target = os.path.join(probe_dir, "sl_target")
    link = os.path.join(probe_dir, "sl_link")
    try:
        with open(target, "wb") as f:
            f.write(b"x")
        try:
            os.symlink(target, link)
            return os.path.islink(link)
        except (OSError, NotImplementedError):
            return False
    finally:
        _safe_probe_unlink(link)
        _safe_probe_unlink(target)


# Linux ioctl(FICLONE) constant. _IOW(0x94, 9, int) = 0x40049409 on the
# generic ioctl ABI used by x86/ARM/RISC-V. PowerPC/MIPS/SPARC/Alpha use
# different bit layouts — we detect arch and skip on unrecognized ones.
_LINUX_FICLONE = 0x40049409
_RECOGNIZED_LINUX_ARCHS = frozenset({
    "x86_64", "i386", "i486", "i586", "i686",
    "armv6l", "armv7l", "armv7hl", "aarch64", "aarch64_be",
    "riscv32", "riscv64",
})


def _probe_reflink_linux(probe_dir):
    """Try ioctl(FICLONE) — works on btrfs, XFS (reflink=1), bcachefs."""
    try:
        import fcntl
    except ImportError:
        return False

    machine = platform.machine().lower()
    if machine not in _RECOGNIZED_LINUX_ARCHS:
        return False

    src_path = os.path.join(probe_dir, "rl_src")
    dst_path = os.path.join(probe_dir, "rl_dst")
    try:
        with open(src_path, "wb") as f:
            f.write(b"x" * 4096)
        try:
            with open(src_path, "rb") as src:
                with open(dst_path, "wb") as dst:
                    fcntl.ioctl(dst.fileno(), _LINUX_FICLONE, src.fileno())
            return True
        except (OSError, IOError):
            return False
    finally:
        _safe_probe_unlink(src_path)
        _safe_probe_unlink(dst_path)


def _probe_reflink_macos(probe_dir):
    """Try clonefile(2) — works on APFS."""
    src_path = os.path.join(probe_dir, "rl_src")
    dst_path = os.path.join(probe_dir, "rl_dst")
    try:
        with open(src_path, "wb") as f:
            f.write(b"x" * 4096)
        try:
            libc = ctypes.CDLL("libc.dylib")
            libc.clonefile.argtypes = [
                ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32
            ]
            libc.clonefile.restype = ctypes.c_int
            rc = libc.clonefile(
                src_path.encode("utf-8"),
                dst_path.encode("utf-8"),
                0,
            )
            return rc == 0
        except (OSError, AttributeError):
            return False
    finally:
        _safe_probe_unlink(src_path)
        _safe_probe_unlink(dst_path)


def _probe_reflink_windows(probe_dir):
    """Windows reflink probe. NTFS Dev Drive detection via FSCTL is
    deferred; regular NTFS is recognized by name in the capability table."""
    return False


def probe_reflink(probe_dir):
    """Test whether reflinks (CoW clone) work in the destination directory."""
    if _system == "Linux":
        return _probe_reflink_linux(probe_dir)
    elif _system == "Darwin":
        return _probe_reflink_macos(probe_dir)
    elif _system == "Windows":
        return _probe_reflink_windows(probe_dir)
    else:
        return False


# Real reflink copy primitives ---------------------------------------------
# Distinct from the probe_reflink_* functions above, which only test
# capability with throwaway files. These are called from copy_individual
# and create_links to actually create reflink copies of user data.

def _try_reflink_linux(src_path, dst_path):
    """Create dst_path as a reflink (CoW clone) of src_path via FICLONE.
    Returns True on success, False on any failure. On failure, removes
    any partially-created destination file so the caller can fall back.
    """
    try:
        import fcntl
    except ImportError:
        return False

    machine = platform.machine().lower()
    if machine not in _RECOGNIZED_LINUX_ARCHS:
        return False

    try:
        with open(src_path, "rb") as src_f:
            with open(dst_path, "wb") as dst_f:
                fcntl.ioctl(dst_f.fileno(), _LINUX_FICLONE, src_f.fileno())
        return True
    except (OSError, IOError):
        # Remove any partial dst so the caller can cleanly fall back
        try:
            if os.path.lexists(dst_path):
                os.unlink(dst_path)
        except OSError:
            pass
        return False


def _try_reflink_macos(src_path, dst_path):
    """Create dst_path as a clone of src_path via clonefile(2) on APFS.
    clonefile requires the destination to NOT exist, so we remove any
    existing entry first."""
    try:
        if os.path.lexists(dst_path):
            try:
                os.unlink(dst_path)
            except OSError:
                return False
        libc = ctypes.CDLL("libc.dylib")
        libc.clonefile.argtypes = [
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32
        ]
        libc.clonefile.restype = ctypes.c_int
        rc = libc.clonefile(
            src_path.encode("utf-8"),
            dst_path.encode("utf-8"),
            0,
        )
        if rc == 0:
            return True
        # Cleanup any partial result
        try:
            if os.path.lexists(dst_path):
                os.unlink(dst_path)
        except OSError:
            pass
        return False
    except (OSError, AttributeError):
        return False


def _try_reflink_windows(src_path, dst_path):
    """Windows reflink copy via FSCTL_DUPLICATE_EXTENTS_TO_FILE.
    Currently not implemented — requires DeviceIoControl + cluster-aligned
    pre-allocation. Deferred to a future release. ReFS users currently
    fall through to the byte-stream copy path."""
    return False


# In-place deduplication via FIDEDUPERANGE ioctl ----------------------------
# Unlike FICLONE (which creates a new file), FIDEDUPERANGE shares extents
# between two *existing* files. The kernel verifies content matches block by
# block before sharing — so already-shared extents are a safe no-op.
#
# _IOWR(0x94, 54, struct file_dedupe_range): header is 24 bytes.
# (3 << 30) | (24 << 16) | (0x94 << 8) | 54 = 0xC0189436
_LINUX_FIDEDUPERANGE = 0xC0189436
_FILE_DEDUPE_RANGE_SAME    = 0  # extents are (now) shared
_FILE_DEDUPE_RANGE_DIFFERS = 1  # content differs — not deduped


class _FileDeduperangeInfo(ctypes.Structure):
    _fields_ = [
        ('dest_fd',       ctypes.c_int64),
        ('dest_offset',   ctypes.c_uint64),
        ('bytes_deduped', ctypes.c_uint64),
        ('status',        ctypes.c_int32),
        ('reserved',      ctypes.c_uint32),
    ]


class _FileDeduperange(ctypes.Structure):
    _fields_ = [
        ('src_offset',  ctypes.c_uint64),
        ('src_length',  ctypes.c_uint64),
        ('dest_count',  ctypes.c_uint16),
        ('reserved1',   ctypes.c_uint16),
        ('reserved2',   ctypes.c_uint32),
        ('info',        _FileDeduperangeInfo * 1),
    ]


def _try_inplace_dedup_linux(src_path, dst_path, size):
    """Share extents between src_path and dst_path via FIDEDUPERANGE.
    The kernel verifies content before sharing; already-shared extents are
    a no-op. Returns True if extents are now shared, False otherwise."""
    if size == 0:
        return True  # empty files share nothing, trivially "same"
    if platform.machine().lower() not in _RECOGNIZED_LINUX_ARCHS:
        return False
    try:
        import fcntl
    except ImportError:
        return False
    try:
        with open(src_path, 'rb') as src_f, open(dst_path, 'r+b') as dst_f:
            req = _FileDeduperange()
            req.src_offset = 0
            req.src_length = size
            req.dest_count = 1
            req.reserved1 = 0
            req.reserved2 = 0
            req.info[0].dest_fd = dst_f.fileno()
            req.info[0].dest_offset = 0
            req.info[0].bytes_deduped = 0
            req.info[0].status = 0
            req.info[0].reserved = 0
            fcntl.ioctl(src_f.fileno(), _LINUX_FIDEDUPERANGE, req)
            return req.info[0].status == _FILE_DEDUPE_RANGE_SAME
    except (OSError, IOError):
        return False


def _try_reflink(src_path, dst_path):
    """Try to create dst_path as a reflink/CoW clone of src_path.

    Returns True on success, False if reflink isn't possible (different
    filesystem, unsupported FS, error). Reflinks only work within a
    single filesystem — checks st_dev before attempting the syscall to
    avoid wasted work.
    """
    try:
        src_st = os.stat(src_path)
        dst_parent = os.path.dirname(dst_path) or "."
        dst_st = os.stat(dst_parent)
    except OSError:
        return False

    if src_st.st_dev != dst_st.st_dev:
        return False  # cross-filesystem — reflink impossible

    if _system == "Linux":
        return _try_reflink_linux(src_path, dst_path)
    elif _system == "Darwin":
        return _try_reflink_macos(src_path, dst_path)
    elif _system == "Windows":
        return _try_reflink_windows(src_path, dst_path)
    return False


def probe_case_sensitivity(probe_dir):
    """Test whether the FS distinguishes 'A' from 'a' as different files."""
    upper = os.path.join(probe_dir, "Case_Probe.tmp")
    lower = os.path.join(probe_dir, "case_probe.tmp")
    try:
        with open(upper, "wb") as f:
            f.write(b"u")
        return not os.path.exists(lower)
    finally:
        _safe_probe_unlink(upper)
        _safe_probe_unlink(lower)


# FS type → known capabilities lookup table -------------------------------

_CASE_INSENSITIVE_FS = frozenset({
    "vfat", "fat", "fat32", "msdos", "exfat",
    "ntfs", "ntfs3", "ntfs-3g",
    "hfs", "hfsplus",
    "apfs",
})


def _default_case_sensitive(fs_type):
    """Best-guess case sensitivity when probing isn't possible."""
    return (fs_type or "").lower() not in _CASE_INSENSITIVE_FS


# Maps lowercase FS type → (hardlink, symlink, reflink, needs_probe)
_FS_CAPABILITY_TABLE = {
    # No links
    "vfat":      (False, False, False, False),
    "fat":       (False, False, False, False),
    "fat32":     (False, False, False, False),
    "msdos":     (False, False, False, False),
    "exfat":     (False, False, False, False),
    # Always reflink-capable
    "btrfs":     (True,  True,  True,  False),
    "bcachefs":  (True,  True,  True,  False),
    "apfs":      (True,  True,  True,  False),
    "refs":      (True,  True,  True,  False),
    # Hardlinks always; reflinks conditional → probe
    "xfs":       (True,  True,  False, True),
    "zfs":       (True,  True,  False, True),
    # Hardlinks always; no reflinks
    "ext2":      (True,  True,  False, False),
    "ext3":      (True,  True,  False, False),
    "ext4":      (True,  True,  False, False),
    "hfs":       (True,  True,  False, False),
    "hfsplus":   (True,  True,  False, False),
    "f2fs":      (True,  True,  False, False),
    "tmpfs":     (True,  True,  False, False),
    "overlay":   (True,  True,  False, False),
    # NTFS — hardlinks yes; reflinks only on Dev Drive → probe
    "ntfs":      (True,  True,  False, True),
    "ntfs3":     (True,  True,  False, True),
    # Network filesystems — depends on server
    "nfs":       (True,  True,  False, True),
    "nfs4":      (True,  True,  False, True),
    "cifs":      (True,  True,  False, True),
    "smbfs":     (True,  True,  False, True),
    "smb3":      (True,  True,  False, True),
    # FUSE — could be anything underneath
    "fuseblk":   (False, False, False, True),
    "fuse":      (False, False, False, True),
    "sshfs":     (False, False, False, True),
}


# Main detection function --------------------------------------------------

def detect_capabilities(dst_dir, force_probe=False):
    """Detect the filesystem and its capabilities at `dst_dir`.

    Returns an FSInfo namedtuple. Handles non-existent destinations (walks
    up to first existing parent), read-only destinations (falls back to
    table), and unknown FS types (runs all probes).
    """
    t0 = time.perf_counter()
    fs_type, method = detect_fs_type(dst_dir)
    detection_ms = (time.perf_counter() - t0) * 1000

    probe_parent = _walk_up_to_existing(dst_dir)
    if probe_parent is None:
        return _info_from_table_only(
            dst_dir, fs_type, method, detection_ms,
            reason="no_existing_parent")

    if not os.access(probe_parent, os.W_OK) and not force_probe:
        return _info_from_table_only(
            dst_dir, fs_type, method, detection_ms,
            reason="no_writable_parent")

    fs_lc = fs_type.lower() if fs_type else "unknown"
    table_entry = _FS_CAPABILITY_TABLE.get(fs_lc)

    probes_run = []
    probe_timings = {}
    probe_ms = 0.0
    from_table = False

    if table_entry and not table_entry[3] and not force_probe:
        # Known FS — use table, only probe case sensitivity
        hl, sl, rl, _ = table_entry
        from_table = True
        probe_dir = None
        cs = _default_case_sensitive(fs_type)
        try:
            probe_dir = _make_probe_dir(probe_parent)
            tcs = time.perf_counter()
            cs = probe_case_sensitivity(probe_dir)
            cs_ms = (time.perf_counter() - tcs) * 1000
            probe_timings["case_sensitivity"] = cs_ms
            probes_run.append("case_sensitivity")
            probe_ms = cs_ms
        except OSError as e:
            probe_timings["_skipped_reason"] = "probe_create_failed: {}".format(e)
        finally:
            if probe_dir:
                _cleanup_probe_dir(probe_dir)
        caps = FSCapabilities(hardlink=hl, symlink=sl, reflink=rl,
                              case_sensitive=cs)
    else:
        # Ambiguous or unknown FS — probe everything
        probe_dir = None
        hl = sl = rl = cs = False
        try:
            try:
                probe_dir = _make_probe_dir(probe_parent)
            except OSError as e:
                probe_timings["_skipped_reason"] = "probe_create_failed: {}".format(e)
                if table_entry:
                    hl, sl, rl, _ = table_entry
                    cs = _default_case_sensitive(fs_type)
                    from_table = True
                caps = FSCapabilities(hardlink=hl, symlink=sl, reflink=rl,
                                      case_sensitive=cs)
                return FSInfo(
                    path=dst_dir, fs_type=fs_type, capabilities=caps,
                    strategy=select_dedup_strategy(caps),
                    detection_ms=detection_ms, probe_ms=0.0,
                    probes_run=[], probe_timings=probe_timings,
                    method=method, from_table=from_table,
                )

            for name, fn in (("hardlink",         probe_hardlink),
                             ("symlink",          probe_symlink),
                             ("reflink",          probe_reflink),
                             ("case_sensitivity", probe_case_sensitivity)):
                t1 = time.perf_counter()
                result = fn(probe_dir)
                elapsed = (time.perf_counter() - t1) * 1000
                probe_timings[name] = elapsed
                probes_run.append(name)
                probe_ms += elapsed
                if name == "hardlink":
                    hl = result
                elif name == "symlink":
                    sl = result
                elif name == "reflink":
                    rl = result
                elif name == "case_sensitivity":
                    cs = result
        finally:
            if probe_dir:
                _cleanup_probe_dir(probe_dir)

        caps = FSCapabilities(hardlink=hl, symlink=sl, reflink=rl,
                              case_sensitive=cs)

    return FSInfo(
        path=dst_dir, fs_type=fs_type, capabilities=caps,
        strategy=select_dedup_strategy(caps),
        detection_ms=detection_ms, probe_ms=probe_ms,
        probes_run=probes_run, probe_timings=probe_timings,
        method=method, from_table=from_table,
    )


def _info_from_table_only(dst_dir, fs_type, method, detection_ms, reason):
    """Return FSInfo using only the FS-type table (no probing)."""
    fs_lc = fs_type.lower() if fs_type else "unknown"
    entry = _FS_CAPABILITY_TABLE.get(fs_lc)
    if entry is None:
        caps = FSCapabilities(False, False, False,
                              _default_case_sensitive(fs_type or "unknown"))
    else:
        hl, sl, rl, _ = entry
        cs = _default_case_sensitive(fs_type)
        caps = FSCapabilities(hardlink=hl, symlink=sl, reflink=rl,
                              case_sensitive=cs)
    return FSInfo(
        path=dst_dir, fs_type=fs_type, capabilities=caps,
        strategy=select_dedup_strategy(caps),
        detection_ms=detection_ms, probe_ms=0.0,
        probes_run=[], probe_timings={"_skipped_reason": reason},
        method=method, from_table=True,
    )


def select_dedup_strategy(caps):
    """Pick dedup strategy: reflink > hardlink > symlink > none."""
    if caps.reflink:
        return "reflink"
    if caps.hardlink:
        return "hardlink"
    if caps.symlink:
        return "symlink"
    return "none"


def format_fs_info(info):
    """Human-readable summary of FSInfo for verbose output."""
    caps = info.capabilities
    lines = [
        "Path:         {}".format(info.path),
        "FS type:      {} (via {})".format(info.fs_type, info.method),
        "Source:       {}".format("table" if info.from_table else "probes"),
        "Detection:    {:.3f} ms".format(info.detection_ms),
        "Probing:      {:.3f} ms ({} probe{})".format(
            info.probe_ms, len(info.probes_run),
            "" if len(info.probes_run) == 1 else "s"),
    ]
    skipped = info.probe_timings.get("_skipped_reason")
    if skipped:
        lines.append("  ⚠ probes skipped: {}".format(skipped))
        lines.append("  ⚠ capabilities below are TABLE DEFAULTS, not verified")
    if info.probes_run:
        lines.append("  probes run: {}".format(", ".join(info.probes_run)))
        for name, ms in info.probe_timings.items():
            if name.startswith("_"):
                continue
            lines.append("    {:18s} {:>7.3f} ms".format(name, ms))
    lines.append("Capabilities:")
    lines.append("  hardlink:        {}".format("yes" if caps.hardlink else "no"))
    lines.append("  symlink:         {}".format("yes" if caps.symlink else "no"))
    lines.append("  reflink (CoW):   {}".format("yes" if caps.reflink else "no"))
    lines.append("  case sensitive:  {}".format("yes" if caps.case_sensitive else "no"))
    lines.append("Strategy:     {}".format(info.strategy))
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════════════
# END OF INLINED fs_detect
# ════════════════════════════════════════════════════════════════════════════


def _long_path(p):
    """On Windows, prefix paths with \\\\?\\ to bypass the 260-char MAX_PATH limit.
    Use ONLY for actual file I/O (open, makedirs, walk), NOT for path comparison.

    UNC paths (\\\\server\\share\\...) require the \\\\?\\UNC\\ form — prepending a
    plain \\\\?\\ to a UNC path produces \\\\?\\\\\\server\\share, which Windows
    rejects with WinError 123."""
    if _system != "Windows" or p.startswith("\\\\?\\"):
        return p
    abs_p = os.path.abspath(p)
    if abs_p.startswith("\\\\"):
        # UNC: \\server\share\... → \\?\UNC\server\share\...
        return "\\\\?\\UNC\\" + abs_p[2:]
    return "\\\\?\\" + abs_p


def _strip_long_path(p):
    """Strip the \\\\?\\ or \\\\?\\UNC\\ prefix if present (for path comparison/relpath)."""
    if p.startswith("\\\\?\\UNC\\"):
        return "\\\\" + p[8:]
    if p.startswith("\\\\?\\"):
        return p[4:]
    return p


def get_physical_offset(filepath):
    if _system == "Linux":
        return get_physical_offset_linux(filepath)
    elif _system == "Windows":
        return get_physical_offset_windows(filepath)
    elif _system == "Darwin":
        return get_physical_offset_macos(filepath)
    return 0


# ════════════════════════════════════════════════════════════════════════════
# FOLDER SCANNER
# ════════════════════════════════════════════════════════════════════════════
def scan_source(src_root, dst_root=None, excludes=None):
    """Walk source tree, catalog all files.
    Follows symlinks and junctions (common on Windows with OneDrive/shell folders).
    Skips the destination directory if it's inside the source (prevents infinite loop).
    """
    print(f"  {C.DIM}Scanning files...{C.RESET}", end="", flush=True)

    entries = []
    errors = []
    dir_errors = []
    scan_count = 0
    skipped_dst = False
    visited_real = set()  # avoid infinite loops from circular symlinks

    # Resolve destination path to detect overlap
    dst_real = os.path.realpath(dst_root) if dst_root else None
    src_real = os.path.realpath(src_root)

    # Check if destination is inside source
    if dst_real and dst_real.startswith(src_real + os.sep):
        print(f"\n  {C.YELLOW}Warning: Destination is inside source — it will be excluded{C.RESET}")

    # Compile exclude patterns. Patterns are glob-style (fnmatch) and applied
    # to both file basenames and directory basenames; matching directories are
    # pruned so we don't descend into them.
    exclude_patterns = [TAR_BUNDLE_NAME, DEDUP_DB_NAME, REMOTE_MANIFEST_NAME,
                        SUDO_AUDIT_FILE]
    if excludes:
        exclude_patterns.extend(excludes)

    def _excluded(name):
        return any(fnmatch.fnmatch(name, p) for p in exclude_patterns)

    def on_walk_error(err):
        """Called by os.walk when it can't list a directory."""
        dir_errors.append((err.filename, str(err)))

    # followlinks=True on Windows so junctions are traversed (junctions are
    # treated as directories by NTFS and are the normal way symbolic links
    # show up there). On POSIX, followlinks=False so a non-root attacker who
    # plants a symlink in the source tree cannot redirect a root-privileged
    # read (e.g. dropping `<src>/leak -> /etc/shadow`).
    # Use _long_path on Windows to see files beyond 260-char MAX_PATH.
    walk_src = _long_path(src_root)
    follow_links_setting = (_system == "Windows")
    elevated = _is_under_sudo() or (hasattr(os, "geteuid") and os.geteuid() == 0)
    symlink_warnings = []
    rejected_symlinks = []
    for root, dirs, files in os.walk(walk_src, followlinks=follow_links_setting, onerror=on_walk_error):
        # Circular symlink protection
        try:
            real = os.path.realpath(_strip_long_path(root))
            if real in visited_real:
                dirs.clear()  # don't descend further
                continue
            visited_real.add(real)
        except OSError:
            dirs.clear()  # can't resolve — skip to avoid infinite loop
            continue

        # Warn if a symlinked directory points outside the source tree
        if os.path.islink(_strip_long_path(root)):
            if not real.startswith(src_real + os.sep) and real != src_real:
                symlink_warnings.append((_strip_long_path(root), real))

        # Skip destination directory if inside source
        if dst_real:
            dirs_to_remove = []
            for d in dirs:
                dir_real = os.path.realpath(_strip_long_path(os.path.join(root, d)))
                if dir_real == dst_real or dst_real.startswith(dir_real + os.sep):
                    dirs_to_remove.append(d)
                    skipped_dst = True
            for d in dirs_to_remove:
                dirs.remove(d)

        # Prune excluded directories so os.walk doesn't descend into them
        dirs[:] = [d for d in dirs if not _excluded(d)]

        for fname in files:
            # Skip excluded files
            if _excluded(fname):
                continue

            src_path = os.path.join(root, fname)
            rel_path = os.path.relpath(_strip_long_path(src_path), src_root).replace(os.sep, "/")
            try:
                lst = os.lstat(src_path)
            except OSError as e:
                errors.append((_strip_long_path(src_path), str(e)))
                continue
            # Symlink in source: refuse outright when running elevated.
            # Otherwise, allow only if its realpath stays inside the source.
            if stat.S_ISLNK(lst.st_mode):
                if elevated:
                    rejected_symlinks.append(_strip_long_path(src_path))
                    continue
                try:
                    real_target = os.path.realpath(src_path)
                except OSError:
                    rejected_symlinks.append(_strip_long_path(src_path))
                    continue
                if not (real_target == src_real or
                        real_target.startswith(src_real + os.sep)):
                    rejected_symlinks.append(_strip_long_path(src_path))
                    continue
            try:
                st = os.stat(src_path)
                alloc = _detect_sparse_alloc(st)
                entries.append(FileEntry(
                    src=src_path, rel=rel_path, size=st.st_size,
                    physical_offset=0, content_hash=None,
                    alloc_size=alloc,
                ))
                scan_count += 1
                if scan_count % 1000 == 0:
                    print(f"\r  {C.DIM}Scanning... {scan_count} files{C.RESET}", end="", flush=True)
            except OSError as e:
                errors.append((_strip_long_path(src_path), str(e)))
    if rejected_symlinks:
        reason = ("running as root — symlinks not followed" if elevated
                  else "target escapes source tree")
        print(f"  {C.YELLOW}Skipped {len(rejected_symlinks)} symlinks ({reason}):{C.RESET}")
        for p in rejected_symlinks[:5]:
            print(f"    {C.YELLOW}→ {p}{C.RESET}")
        if len(rejected_symlinks) > 5:
            print(f"    ... and {len(rejected_symlinks) - 5} more")

    print(f"\r  {C.GREEN}Found {len(entries)} files{C.RESET}                    ")

    if skipped_dst:
        print(f"  {C.YELLOW}Excluded destination directory from scan{C.RESET}")

    # Warn about symlinks pointing outside source tree
    if symlink_warnings:
        print(f"  {C.YELLOW}Warning: {len(symlink_warnings)} symlinks point outside source tree:{C.RESET}")
        for link_path, target in symlink_warnings[:5]:
            print(f"    {C.YELLOW}→ {link_path} → {target}{C.RESET}")
        if len(symlink_warnings) > 5:
            print(f"    ... and {len(symlink_warnings) - 5} more")

    # Show directory access errors (common cause of "0 files found")
    if dir_errors:
        print(f"  {C.YELLOW}Could not access {len(dir_errors)} directories:{C.RESET}")
        for path, err in dir_errors[:10]:
            print(f"    {C.YELLOW}→ {path}{C.RESET}")
            print(f"      {C.DIM}{err}{C.RESET}")
        if len(dir_errors) > 10:
            print(f"    ... and {len(dir_errors) - 10} more")

    # Show file read errors
    if errors:
        print(f"  {C.YELLOW}Skipped {len(errors)} unreadable files:{C.RESET}")
        for path, err in errors[:10]:
            print(f"    {C.YELLOW}→ {path}{C.RESET}")
            print(f"      {C.DIM}{err}{C.RESET}")
        if len(errors) > 10:
            print(f"    ... and {len(errors) - 10} more")

    # Hint if nothing found
    if not entries and not errors and not dir_errors:
        print(f"  {C.YELLOW}The directory appears to be empty.{C.RESET}")
        if _system == "Windows":
            print(f"  {C.YELLOW}Tip: Your Documents folder may be redirected to OneDrive.{C.RESET}")
            print(f"  {C.YELLOW}     Check: OneDrive\\Documents or run in PowerShell:{C.RESET}")
            print(f"  {C.DIM}     (New-Object -ComObject Shell.Application)"
                  f".NameSpace('shell:Personal').Self.Path{C.RESET}")

    return entries, errors


def resolve_physical_offsets(entries, threads=DEFAULT_THREADS):
    """Query physical offsets in parallel, return entries sorted by disk position."""
    print(f"  {C.DIM}Reading disk layout ({_system})...{C.RESET}", end="", flush=True)

    offsets = [0] * len(entries)

    def get_offset(idx):
        offsets[idx] = get_physical_offset(entries[idx].src)

    with ThreadPoolExecutor(max_workers=threads) as pool:
        futures = [pool.submit(get_offset, i) for i in range(len(entries))]
        done = 0
        for f in as_completed(futures):
            f.result()
            done += 1
            if done % 500 == 0:
                print(f"\r  {C.DIM}Reading disk layout... {done}/{len(entries)}{C.RESET}",
                      end="", flush=True)

    new_entries = [
        e._replace(physical_offset=offsets[i])
        for i, e in enumerate(entries)
    ]
    new_entries.sort(key=lambda e: e.physical_offset)

    has_offset = sum(1 for e in new_entries if e.physical_offset > 0)
    print(f"\r  {C.GREEN}Disk layout resolved: {has_offset}/{len(entries)} files mapped{C.RESET}          ")

    if has_offset == 0:
        print(f"  {C.YELLOW}Could not map physical layout — falling back to size-sorted order.{C.RESET}")
        new_entries.sort(key=lambda e: e.size, reverse=True)
    elif has_offset < len(entries) * 0.5:
        print(f"  {C.YELLOW}Partial mapping — unmapped files appended at end.{C.RESET}")

    return new_entries


# ════════════════════════════════════════════════════════════════════════════
# DEDUPLICATION
# ════════════════════════════════════════════════════════════════════════════

def deduplicate(entries, threads=DEFAULT_THREADS, dedup_db=None,
                fs_strategy=None, dedup_inplace=False):
    """
    Content-aware deduplication:
      1. Hash ALL files (using cache when available)
      2. Group by (size, hash) — same-size pre-filter for within-run dedup
      3. Cross-run dedup: check if drive already has matching content
      4. Return (unique_entries, link_map)
    """
    # (Hash algo is shown in the main banner; don't duplicate it here.)
    if dedup_db:
        print(f"  {C.DIM}Hash cache: enabled (cross-run dedup){C.RESET}")

    # ── Step 1: Hash ALL files (cache-aware) ──────────────────────────
    total = len(entries)
    print(f"  Hashing {total} files...")

    hashes = [None] * total
    cache_hits = [0]
    new_hashes = []  # (rel, size, mtime_ns, hash) for source cache
    done_count = [0]
    lock = threading.Lock()

    def do_hash(idx):
        entry = entries[idx]
        cache_key = entry.src
        # Stat before hash to get mtime for cache lookup/store
        try:
            pre_stat = os.stat(entry.src)
            mtime_ns_before = pre_stat.st_mtime_ns
        except OSError:
            mtime_ns_before = 0
        # Try cache first
        if dedup_db and mtime_ns_before:
            cached = dedup_db.lookup(cache_key, entry.size, mtime_ns_before)
            if cached:
                hashes[idx] = cached
                with lock:
                    cache_hits[0] += 1
                return
        # Cache miss — hash the file
        h = hash_file(entry.src)
        hashes[idx] = h
        if dedup_db and h is not None:
            # Stat after hash — only cache if mtime unchanged (no TOCTOU)
            try:
                mtime_ns_after = os.stat(entry.src).st_mtime_ns
            except OSError:
                mtime_ns_after = -1
            if mtime_ns_before == mtime_ns_after and mtime_ns_before != 0:
                with lock:
                    new_hashes.append((cache_key, entry.size, mtime_ns_before, h))

    with ThreadPoolExecutor(max_workers=threads) as pool:
        futures = [pool.submit(do_hash, i) for i in range(total)]
        for f in as_completed(futures):
            f.result()
            with lock:
                done_count[0] += 1
                if done_count[0] % 200 == 0:
                    print(f"\r  {C.DIM}Hashing... {done_count[0]}/{total}{C.RESET}",
                          end="", flush=True)

    # Store newly computed hashes in source cache
    if dedup_db and new_hashes:
        dedup_db.store_source_batch(new_hashes)

    if cache_hits[0] > 0:
        print(f"\r  {C.GREEN}Cache: {cache_hits[0]}/{total} hashes from DB "
              f"({total - cache_hits[0]} computed){C.RESET}          ")

    # Update all entries with hashes
    hashed_entries = [
        e._replace(content_hash=hashes[i])
        for i, e in enumerate(entries)
    ]

    # ── Step 2: Group by (size, hash) to find duplicates ──────────────
    hash_groups = defaultdict(list)
    unique_entries = []

    for e in hashed_entries:
        if e.content_hash is not None:
            key = (e.size, e.content_hash)
            hash_groups[key].append(e)
        else:
            # Couldn't hash → treat as unique
            unique_entries.append(e)

    link_map = {}       # duplicate_rel → canonical_rel or ("__abs__", path)
    saved_bytes = 0
    crossrun_count = 0
    crossrun_bytes = 0
    crossrun_sources = defaultdict(int)  # folder → file count
    inplace_count = 0
    inplace_bytes = 0
    existing_hashed = 0   # candidates hashed from existing_index this run

    total_groups = len(hash_groups)
    print(f"  {C.DIM}Cross-referencing {total_groups} unique sizes/hashes against drive...{C.RESET}",
          end="", flush=True)

    for key, group in hash_groups.items():
        canonical = group[0]

        # ── Cross-run dedup: check if drive already has this content ──
        if dedup_db:
            dst_matches = dedup_db.lookup_by_hash(key[1])  # key[1] = content_hash
            for mount_rel, dst_size in dst_matches:
                # Validate mount_rel doesn't escape mount point via ../
                if '..' in mount_rel.split('/') or '..' in mount_rel.split(os.sep):
                    continue
                # mount_rel is relative to mount point, build full path
                full_path = os.path.join(dedup_db.mount, mount_rel)
                # Verify resolved path stays within mount point
                real_full = os.path.realpath(full_path)
                real_mount = os.path.realpath(dedup_db.mount)
                if not (real_full.startswith(real_mount + os.sep) or real_full == real_mount):
                    continue
                if dst_size == key[0] and os.path.isfile(full_path):
                    # Drive already has a file with this content —
                    # use it as link target (avoids copying entirely)
                    canonical = None
                    # Track which folder the match came from
                    match_folder = mount_rel.split(os.sep)[0] if os.sep in mount_rel else mount_rel.split("/")[0]
                    for e in group:
                        link_map[e.rel] = ("__abs__", full_path)
                        saved_bytes += e.size
                        crossrun_count += 1
                        crossrun_bytes += e.size
                    crossrun_sources[match_folder] += len(group)
                    break

        # ── Existing-index dedup: lazy-hash size-matched pre-existing files ──
        if canonical is not None and dedup_db:
            candidates = dedup_db.lookup_existing_by_size(key[0])  # key[0] = size
            real_mount = os.path.realpath(dedup_db.mount)
            for mount_rel in candidates:
                if '..' in mount_rel.split('/') or '..' in mount_rel.split(os.sep):
                    continue
                full_path = os.path.join(dedup_db.mount, mount_rel)
                real_full = os.path.realpath(full_path)
                if not (real_full.startswith(real_mount + os.sep) or real_full == real_mount):
                    continue
                try:
                    st = os.stat(full_path)
                except OSError:
                    dedup_db.remove_existing(mount_rel)
                    continue
                if st.st_size != key[0]:
                    dedup_db.remove_existing(mount_rel)
                    continue
                # Lazy hash — always promote to dest_files regardless of match
                existing_hashed += 1
                short = full_path if len(full_path) <= 60 else "..." + full_path[-57:]
                print(f"\r  {C.DIM}Hashing existing [{existing_hashed}]: {short}{C.RESET}          ",
                      end="", flush=True)
                h = hash_file(full_path)
                if h is None:
                    continue
                dedup_db.promote_from_existing(mount_rel, key[0], h)
                if existing_hashed % 50 == 0:
                    dedup_db.commit_pending()
                # Inplace dedup: if other files on the target share this hash,
                # deduplicate the newly-hashed file against the first known copy.
                if dedup_inplace and key[0] > 0:
                    same_on_target = dedup_db.lookup_by_hash(h)
                    if len(same_on_target) > 1:
                        canonical_rel, _ = same_on_target[0]
                        if canonical_rel != mount_rel:
                            canonical_full = os.path.join(dedup_db.mount, canonical_rel)
                            if os.path.isfile(canonical_full):
                                if _try_inplace_dedup_linux(canonical_full, full_path, key[0]):
                                    inplace_count += 1
                                    inplace_bytes += key[0]
                if h == key[1]:  # key[1] = content_hash
                    canonical = None
                    match_folder = mount_rel.split('/')[0]
                    for e in group:
                        link_map[e.rel] = ("__abs__", full_path)
                        saved_bytes += e.size
                        crossrun_count += 1
                        crossrun_bytes += e.size
                    crossrun_sources[match_folder] += len(group)
                    break
            if candidates and dedup_db:
                dedup_db.commit_pending()

        if canonical is not None:
            # Normal dedup: first file is canonical, rest are linked
            unique_entries.append(canonical)
            for dup in group[1:]:
                link_map[dup.rel] = canonical.rel
                saved_bytes += dup.size

    dup_count = len(link_map)
    within_run = dup_count - crossrun_count
    total_files = len(entries)

    print(f"\r  {C.GREEN}Dedup complete:{C.RESET}                                                  ")
    print(f"    Unique files:    {C.BOLD}{len(unique_entries)}{C.RESET}")
    if within_run > 0:
        print(f"    Within-run dups: {C.BOLD}{within_run}{C.RESET} files "
              f"(identical files in source)")
    if crossrun_count > 0:
        print(f"    Cross-run dups:  {C.BOLD}{crossrun_count}{C.RESET} files "
              f"({C.GREEN}{fmt_size(crossrun_bytes)}{C.RESET}) — "
              f"already on drive, will link instead of copy")
        for folder, count in sorted(crossrun_sources.items(), key=lambda x: -x[1]):
            print(f"      → {C.CYAN}{folder}/{C.RESET}: {count} files matched")
    if inplace_count > 0:
        print(f"    Inplace dedup:   {C.BOLD}{inplace_count}{C.RESET} files "
              f"({C.GREEN}{fmt_size(inplace_bytes)}{C.RESET}) — "
              f"target duplicates merged into reflinks")
    print(f"    Total duplicates:{C.BOLD} {dup_count}{C.RESET} "
          f"({fmt_pct(dup_count, total_files)} of files)")
    total_input_size = sum(e.size for e in entries)
    # On link-incapable filesystems, dedup only saves bandwidth — duplicates
    # become real copies on disk. Be honest about which saving applies.
    if fs_strategy == "none" and saved_bytes > 0:
        print(f"    Bandwidth saved: {C.GREEN}{C.BOLD}{fmt_size(saved_bytes)}{C.RESET} "
              f"(transfer only)")
        print(f"    Disk usage:      {C.BOLD}{fmt_size(total_input_size)}{C.RESET} "
              f"{C.YELLOW}(full copies — FS does not support links){C.RESET}")
    else:
        print(f"    Space saved:     {C.GREEN}{C.BOLD}{fmt_size(saved_bytes)}{C.RESET} "
              f"({fmt_pct(saved_bytes, total_input_size)} reduction)")

    return unique_entries, link_map, saved_bytes


def create_links(link_map, dst_root, fs_strategy=None):
    """
    Create dedup links for duplicated files.

    Fallback chain (best → worst):
      1. Reflink (CoW clone): metadata-only, CoW-safe so peer files
         remain independent on write. Used when fs_strategy='reflink'.
      2. Hardlink: shared inode. Eliminates extra disk usage but all
         names share data — modifying one modifies all peers.
      3. Symlink: pointer to canonical. Asymmetric — deleting the
         canonical breaks the symlink. Rare fallback for unusual FSes.
      4. Full copy: independent file. Used on FAT32 / exFAT and
         filesystems without any link support.
    """
    if not link_map:
        return

    print(f"  {C.DIM}Creating {len(link_map)} links for duplicates...{C.RESET}", end="", flush=True)
    reflink_ok = 0
    hardlink_ok = 0
    symlink_ok = 0
    copy_fallback = 0
    errors = 0

    for dup_rel, target in link_map.items():
        dst_dup = _long_path(os.path.join(dst_root, dup_rel))
        # Target is either a rel path or ("__abs__", full_path) for cross-run dedup
        if isinstance(target, tuple) and target[0] == "__abs__":
            dst_canonical = _long_path(target[1])
        else:
            dst_canonical = _long_path(os.path.join(dst_root, target))

        os.makedirs(os.path.dirname(dst_dup), exist_ok=True)

        # Get file size for logging
        try:
            _link_size = os.path.getsize(dst_canonical)
        except OSError:
            _link_size = 0
        _link_target = target if isinstance(target, str) else target[1]

        # 1. Try reflink first when the FS supports it. Reflinks are the
        #    BEST dedup mechanism because they share storage initially but
        #    are CoW on write — modifying one peer doesn't affect others.
        #    This eliminates the link-update problem that hardlinks have.
        if fs_strategy == "reflink":
            if _try_reflink(_strip_long_path(dst_canonical),
                            _strip_long_path(dst_dup)):
                _log("linked", dup_rel, _link_size, method="reflink",
                     link_target=_link_target)
                reflink_ok += 1
                continue

        # 2. Try hard link (fast, no extra space, but inode-shared)
        try:
            os.link(dst_canonical, dst_dup)
            _log("linked", dup_rel, _link_size, method="hardlink", link_target=_link_target)
            hardlink_ok += 1
            continue
        except OSError:
            pass

        # 3. Try symlink (works on most filesystems)
        try:
            # Compute relative path from dup to canonical (strip \\?\ for relpath)
            rel_target = os.path.relpath(
                _strip_long_path(dst_canonical),
                os.path.dirname(_strip_long_path(dst_dup)))
            os.symlink(rel_target, dst_dup)
            # Verify the symlink actually resolves (NTFS via Linux creates
            # symlinks that don't work)
            if os.path.isfile(dst_dup):
                _log("linked", dup_rel, _link_size, method="symlink", link_target=_link_target)
                symlink_ok += 1
                continue
            else:
                # Broken symlink — remove and fall through to copy
                os.unlink(dst_dup)
        except OSError:
            pass

        # 4. Last resort: actual copy
        try:
            shutil.copy2(dst_canonical, dst_dup)
            _log("linked", dup_rel, _link_size, method="copy_fallback", link_target=_link_target)
            copy_fallback += 1
        except OSError as e:
            _log("error", dup_rel, _link_size, error=str(e))
            errors += 1

    total = reflink_ok + hardlink_ok + symlink_ok + copy_fallback
    print(f"\r  {C.GREEN}Duplicate handling:{C.RESET}                              ")
    if reflink_ok:
        print(f"    {C.GREEN}✓{C.RESET} Reflinks:         {C.BOLD}{reflink_ok:>6}{C.RESET} "
              f"{C.DIM}(CoW shared blocks; modifying one does not affect peers){C.RESET}")
    if hardlink_ok:
        print(f"    {C.GREEN}✓{C.RESET} Hardlinks:        {C.BOLD}{hardlink_ok:>6}{C.RESET} "
              f"{C.DIM}(shared inode; zero extra disk){C.RESET}")
    if symlink_ok:
        print(f"    {C.YELLOW}~{C.RESET} Symlinks:         {C.BOLD}{symlink_ok:>6}{C.RESET} "
              f"{C.DIM}(pointer to canonical; canonical must not be deleted){C.RESET}")
    if copy_fallback:
        # Full copies mean the FS can't dedup — highlight this so users
        # understand the disk usage implication.
        print(f"    {C.YELLOW}✗{C.RESET} Full copies:      {C.BOLD}{copy_fallback:>6}{C.RESET} "
              f"{C.YELLOW}(FS does not support links — no disk savings){C.RESET}")
    if errors:
        print(f"    {C.RED}✗ Errors:           {errors:>6}{C.RESET}")
    if total > 0:
        # Show the breakdown as a percentage so users can see at a glance
        # how much of the dedup benefit they actually got on disk.
        if reflink_ok == total:
            _disk_msg = f"{C.GREEN}all reflinked (CoW; safe to modify peers){C.RESET}"
        elif hardlink_ok == total:
            _disk_msg = f"{C.GREEN}all disk savings realized{C.RESET}"
        elif copy_fallback == total:
            _disk_msg = f"{C.YELLOW}no disk savings (bandwidth only){C.RESET}"
        else:
            _disk_msg = (f"{reflink_ok + hardlink_ok + symlink_ok}/{total} linked, "
                         f"{copy_fallback} copied")
        print(f"    {C.DIM}→ {_disk_msg}{C.RESET}")


# ════════════════════════════════════════════════════════════════════════════
# CASE-INSENSITIVE FILESYSTEM CONFLICT RESOLUTION
# ════════════════════════════════════════════════════════════════════════════
def _fs_case_insensitive(path):
    """Test whether the filesystem at *path* is case-insensitive."""
    import tempfile
    try:
        os.makedirs(path, exist_ok=True)
        fd, probe = tempfile.mkstemp(dir=path, prefix=".fc_case_")
        os.close(fd)
        try:
            # If the upper-cased version of the probe exists, FS is case-insensitive
            return os.path.exists(probe.upper()) or os.path.exists(probe.swapcase())
        finally:
            os.unlink(probe)
    except OSError:
        # Can't test (e.g. read-only) — assume case-sensitive (safe default)
        return False


def resolve_case_conflicts(entries, link_map, dst):
    """Detect and resolve paths that collide on a case-insensitive filesystem.

    Renames conflicting files (e.g. Default.html -> Default_2.html) so both
    are preserved on disk.  Returns (new_entries, new_link_map, renames_dict).
    renames_dict maps original_rel -> new_rel for use during tar extraction.
    """
    if not _fs_case_insensitive(dst):
        return entries, link_map, {}

    # Collect all rels (entries first, then link_map keys)
    seen = {}          # lower_rel -> first original rel
    conflicts = {}     # lower_rel -> [rel, rel, ...] including first

    all_rels = [e.rel for e in entries] + list(link_map.keys())
    for rel in all_rels:
        low = rel.lower()
        if low in seen:
            conflicts.setdefault(low, [seen[low]]).append(rel)
        else:
            seen[low] = rel

    if not conflicts:
        return entries, link_map, {}

    # Build renames: first occurrence keeps name, rest get _2, _3, ...
    renames = {}  # original_rel -> new_rel
    for low, rels in conflicts.items():
        for i, rel in enumerate(rels[1:], 2):
            base, ext = posixpath.splitext(rel)
            new_rel = f"{base}_{i}{ext}"
            while new_rel.lower() in seen:
                i += 1
                new_rel = f"{base}_{i}{ext}"
            seen[new_rel.lower()] = new_rel
            renames[rel] = new_rel

    # Apply renames to entries (update rel, keep src unchanged for fetching)
    new_entries = []
    for e in entries:
        if e.rel in renames:
            new_entries.append(e._replace(rel=renames[e.rel]))
        else:
            new_entries.append(e)

    # Apply renames to link_map (both keys and values may need renaming)
    new_link_map = {}
    for dup_rel, target in link_map.items():
        new_key = renames.get(dup_rel, dup_rel)
        if isinstance(target, str):
            new_val = renames.get(target, target)
        else:
            new_val = target  # ("__abs__", path) — no rename needed
        new_link_map[new_key] = new_val

    # Report
    n_groups = len(conflicts)
    print(f"\n  {C.YELLOW}Case-insensitive filesystem: {len(renames)} file{'s' if len(renames) != 1 else ''} "
          f"renamed to avoid conflicts:{C.RESET}")
    for old, new in renames.items():
        print(f"    {C.DIM}{old}{C.RESET}")
        print(f"      -> {C.BOLD}{new}{C.RESET}")
    print()

    return new_entries, new_link_map, renames


# ════════════════════════════════════════════════════════════════════════════
# PROGRESS TRACKER
# ════════════════════════════════════════════════════════════════════════════
# When True (set by --progress-json), Progress emits one machine-readable JSON
# line per update instead of the human progress bar. Strictly opt-in: the
# default human output is unchanged when this stays False. Consumed by the GUI.
PROGRESS_JSON = False


class Progress:
    def __init__(self, total_bytes, total_files):
        self.total_bytes = total_bytes
        self.total_files = total_files
        self.bytes_done = 0
        self.files_done = 0
        self.lock = threading.Lock()
        self.start = time.time()
        self._last_print = 0

    def update(self, nbytes, nfiles=0):
        with self.lock:
            self.bytes_done += nbytes
            self.files_done += nfiles

    def display(self):
        now = time.time()
        if now - self._last_print < 0.08:
            return
        self._last_print = now
        elapsed = now - self.start
        if elapsed < 0.01:
            return

        with self.lock:
            bytes_done = self.bytes_done
            files_done = self.files_done

        pct = (bytes_done / self.total_bytes * 100) if self.total_bytes else 100
        speed = bytes_done / elapsed
        eta = (self.total_bytes - bytes_done) / speed if speed > 0 else 0

        if PROGRESS_JSON:
            sys.stdout.write(json.dumps({
                "t": "progress",
                "pct": round(pct, 2),
                "bytes_done": bytes_done,
                "bytes_total": self.total_bytes,
                "speed_bps": speed,
                "files_done": files_done,
                "files_total": self.total_files,
                "eta_s": eta,
            }) + "\n")
            sys.stdout.flush()
            return

        bar_w = 30
        filled = int(bar_w * min(pct, 100) / 100)
        bar = "█" * filled + "░" * (bar_w - filled)

        sys.stdout.write(
            f"\r  {C.CYAN}{bar}{C.RESET} {pct:5.1f}%  "
            f"{fmt_size(bytes_done)}/{fmt_size(self.total_bytes)}  "
            f"{C.GREEN}{fmt_speed(speed)}{C.RESET}  "
            f"{files_done}/{self.total_files} files  "
            f"ETA {fmt_time(eta)}   "
        )
        sys.stdout.flush()

    def finish(self):
        elapsed = time.time() - self.start
        speed = self.bytes_done / elapsed if elapsed > 0 else 0
        if PROGRESS_JSON:
            sys.stdout.write(json.dumps({
                "t": "done",
                "pct": 100.0,
                "bytes_done": self.bytes_done,
                "bytes_total": self.total_bytes,
                "speed_bps": speed,
                "files_done": self.files_done,
                "files_total": self.total_files,
                "elapsed_s": elapsed,
            }) + "\n")
            sys.stdout.flush()
            return
        print(f"\r  {C.GREEN}{'█' * 30}{C.RESET} 100%  "
              f"{fmt_size(self.bytes_done)} in {fmt_time(elapsed)}  "
              f"avg {C.GREEN}{fmt_speed(speed)}{C.RESET}  "
              f"{self.files_done} files                ")


# ════════════════════════════════════════════════════════════════════════════
# COPY ENGINE — TRUE BLOCK-LEVEL WRITES
#
# The problem: USB drives have terrible per-file write latency. Copying
# 3000 small files = 3000 separate open/write/close/flush operations,
# each one hitting the USB controller individually.
#
# The solution: Bundle small files into one big tar archive, write it as
# a single sequential block to USB (one fast write), then extract locally
# on the USB. Large files still copy individually with big buffers.
#
# This is what enterprize backup tools (Dell/EMC, Veeam) actually do —
# they never write thousands of tiny files individually.
# ════════════════════════════════════════════════════════════════════════════

SMALL_FILE_THRESHOLD = 1 * 1024 * 1024  # 1 MB — files below this get bundled

TAR_BUNDLE_NAME = ".fast_copy_bundle.tar"


def split_by_size(entries):
    """Split entries into small files (bundle) and large files (individual copy)."""
    small = [e for e in entries if e.size < SMALL_FILE_THRESHOLD]
    large = [e for e in entries if e.size >= SMALL_FILE_THRESHOLD]
    return small, large


def copy_block_stream(small_entries, dst_root, progress, cancel_check=None):
    """
    STREAMING BLOCK COPY for small files — no temp file on disk:
      1. Producer thread reads source files in physical order, writes tar to pipe
      2. Consumer thread reads tar from pipe, extracts files to destination

    No temporary tar file is created on the destination drive, so this works
    even when the destination has barely enough free space for the final files.
    The pipe buffer (~64KB OS default) is the only memory overhead.
    """
    if not small_entries:
        return

    small_size = sum(e.size for e in small_entries)

    print(f"  {C.CYAN}Streaming {len(small_entries)} small files ({fmt_size(small_size)}) "
          f"via pipe...{C.RESET}")

    os.makedirs(dst_root, exist_ok=True)

    # Create an OS-level pipe for streaming between producer and consumer
    read_fd, write_fd = os.pipe()

    producer_error = [None]  # mutable container for thread error reporting
    consumer_done = threading.Event()  # signals producer to stop on consumer failure

    def _tar_producer():
        """Read source files and stream tar entries into the pipe."""
        write_file = None
        try:
            write_file = os.fdopen(write_fd, "wb")
            with tarfile.open(fileobj=write_file, mode="w|") as tar:
                for entry in small_entries:
                    if (cancel_check and cancel_check()) or consumer_done.is_set():
                        break
                    try:
                        try:
                            src_fd = _safe_open_read_fd(entry.src)
                        except OSError as e:
                            if e.errno == errno.ELOOP:
                                _log("error", entry.rel, entry.size,
                                     error="symlink in source (elevated)")
                                progress.update(entry.size, 1)
                                continue
                            raise
                        with os.fdopen(src_fd, "rb") as f:
                            data = f.read(SMALL_FILE_THRESHOLD + 1)
                            try:
                                st = os.fstat(f.fileno())
                            except OSError:
                                st = None

                        info = tarfile.TarInfo(name=entry.rel)
                        info.size = len(data)
                        if st is not None:
                            info.mtime = st.st_mtime
                            info.mode = stat.S_IMODE(st.st_mode)
                        else:
                            info.mtime = time.time()

                        tar.addfile(info, io.BytesIO(data))
                        _log("copied", entry.rel, entry.size, method="block_stream")
                        progress.update(len(data), 1)
                        progress.display()

                    except (BrokenPipeError, OSError, IOError) as e:
                        if consumer_done.is_set():
                            break  # consumer closed pipe, stop gracefully
                        print(f"\n  {C.RED}Error bundling: {entry.rel}: {e}{C.RESET}")
                        _log("error", entry.rel, entry.size, error=str(e))
                        progress.update(entry.size, 1)
        except BrokenPipeError:
            pass  # consumer closed the read end — normal on error/cancel
        except Exception as e:
            producer_error[0] = e
        finally:
            if write_file:
                try:
                    write_file.close()
                except OSError:
                    pass
            else:
                try:
                    os.close(write_fd)
                except OSError:
                    pass

    # Start producer in background thread
    producer = threading.Thread(target=_tar_producer, daemon=True)
    producer.start()

    # Consumer: streaming extraction from pipe — files written to dst as they arrive
    extracted = 0
    extract_errors = []
    read_file = None

    try:
        read_file = os.fdopen(read_fd, "rb")
        with tarfile.open(fileobj=read_file, mode="r|") as tar:
            for member in tar:
                if member.isdir():
                    check = _validate_tar_member(member, dst_root)
                    if check is True:
                        _safe_tar_extract(tar, member, dst_root)
                    continue
                try:
                    result = _safe_tar_extract(tar, member, dst_root)
                    if result is True:
                        extracted += 1
                    else:
                        extract_errors.append((member.name, result))
                except (OSError, tarfile.TarError) as e:
                    extract_errors.append((member.name, str(e)))
    except (OSError, tarfile.TarError) as e:
        print(f"\n  {C.RED}Streaming extraction failed: {e}{C.RESET}")
    finally:
        consumer_done.set()  # signal producer to stop if still running
        if read_file:
            try:
                read_file.close()
            except OSError:
                pass
        else:
            try:
                os.close(read_fd)
            except OSError:
                pass

    producer.join(timeout=30)

    if producer_error[0]:
        print(f"\n  {C.RED}Producer error: {producer_error[0]}{C.RESET}")

    if extract_errors:
        print(f"\r  {C.YELLOW}Extracted {extracted} files, "
              f"{len(extract_errors)} errors{C.RESET}                    ")
        for name, err in extract_errors[:5]:
            print(f"    {C.YELLOW}→ {name}: {err}{C.RESET}")
        if len(extract_errors) > 5:
            print(f"    ... and {len(extract_errors) - 5} more")
    else:
        print(f"\r  {C.GREEN}Streamed {extracted} files to destination{C.RESET}                    ")

    # Apply extended metadata (owner/xattr/acl) post-extraction. Tar carries
    # mode and mtime in its headers, but xattrs/ACLs/ownership are not part
    # of the small-file tar stream — so we walk small_entries again here and
    # re-open the destination file with O_NOFOLLOW to apply them safely.
    # Dispatch via _apply_extended_meta so Windows (NTFS) goes through
    # _copy_acls_windows / _copy_ads_windows and POSIX through the original
    # fchown / setxattr / setfacl helpers.
    if _preserve_spec.any_extended():
        for entry in small_entries:
            dst_path = os.path.join(dst_root, entry.rel)
            try:
                fd = _safe_open_write_fd(dst_path, truncate=False)
            except OSError:
                continue
            try:
                try:
                    src_st = os.stat(entry.src)
                except OSError:
                    continue
                _apply_extended_meta(fd, entry.src, dst_path, src_st)
            finally:
                os.close(fd)


_HAS_SEEK_HOLE = hasattr(os, "SEEK_HOLE") and hasattr(os, "SEEK_DATA")


def _copy_sparse(src_path, dst_path, buf, progress, cancel_check=None):
    """Copy preserving sparseness via SEEK_DATA / SEEK_HOLE.

    Walks the source one data-extent at a time and writes only the data
    bytes to the destination, seek-skipping over holes. The destination
    file is truncated to the source's logical size at the end so any
    trailing hole is preserved.

    Progress is advanced by *logical* bytes (including holes) so the
    overall progress bar still reaches 100% — actual disk writes are
    much smaller for heavily-sparse files."""
    mv = memoryview(buf)
    buf_size = len(buf)
    try:
        src_fd_raw = _safe_open_read_fd(src_path)
    except OSError as e:
        if e.errno == errno.ELOOP:
            print(f"\n  {C.RED}Refusing to follow symlink: {src_path}{C.RESET}")
            return False
        raise
    try:
        dst_fd_raw = _safe_open_write_fd(dst_path, truncate=True)
    except OSError as e:
        os.close(src_fd_raw)
        if e.errno == errno.ELOOP:
            print(f"\n  {C.RED}Refusing to follow symlink at destination: {dst_path}{C.RESET}")
            return False
        raise
    with os.fdopen(src_fd_raw, "rb") as fin, os.fdopen(dst_fd_raw, "wb") as fout:
        src_fd = fin.fileno()
        src_size = os.fstat(src_fd).st_size
        offset = 0
        while offset < src_size:
            if cancel_check and cancel_check():
                try:
                    os.remove(dst_path)
                except OSError:
                    pass
                return False
            try:
                data_start = os.lseek(src_fd, offset, os.SEEK_DATA)
            except OSError as e:
                # ENXIO: no more data — rest of file is hole. Done.
                if e.errno == errno.ENXIO:
                    break
                raise
            try:
                hole_start = os.lseek(src_fd, data_start, os.SEEK_HOLE)
            except OSError:
                # No more holes — data runs to EOF.
                hole_start = src_size

            # Account for the hole we just skipped over.
            if data_start > offset:
                progress.update(data_start - offset)

            fin.seek(data_start)
            fout.seek(data_start)
            remaining = hole_start - data_start
            while remaining > 0:
                if cancel_check and cancel_check():
                    try:
                        os.remove(dst_path)
                    except OSError:
                        pass
                    return False
                chunk = min(buf_size, remaining)
                n = fin.readinto(mv[:chunk])
                if n == 0:
                    break
                fout.write(mv[:n])
                progress.update(n)
                progress.display()
                remaining -= n
            offset = hole_start

        # Materialize the file at full logical size so any trailing hole
        # is preserved by the filesystem (rather than the file ending
        # short at the last data extent).
        fout.truncate(src_size)
        if src_size > offset:
            progress.update(src_size - offset)
        try:
            src_st = os.fstat(src_fd)
            _safe_apply_meta(fout.fileno(), dst_path, src_st, src_path=src_path)
        except OSError:
            pass
    return True


def copy_individual(entries, dst_root, progress, buf, cancel_check=None,
                    fs_strategy=None):
    """Copy large files individually with big buffers in physical disk order.

    When fs_strategy='reflink' and source+destination are on the same
    filesystem, files are cloned via FICLONE/clonefile (instant, metadata-
    only). On any failure (cross-filesystem, unsupported FS, error), falls
    back to the byte-stream copy path."""
    mv = memoryview(buf)

    for entry in entries:
        # Check for cancellation between files
        if cancel_check and cancel_check():
            return

        dst_path = os.path.join(dst_root, entry.rel)
        dst_dir = os.path.dirname(dst_path)

        try:
            os.makedirs(dst_dir, exist_ok=True)

            if entry.size == 0:
                try:
                    fd = _safe_open_write_fd(dst_path, truncate=True)
                except OSError as e:
                    if e.errno == errno.ELOOP:
                        print(f"\n  {C.RED}Refusing to follow symlink at destination: {dst_path}{C.RESET}")
                        _log("error", entry.rel, entry.size, error="symlink at destination")
                        continue
                    raise
                try:
                    try:
                        st = os.lstat(entry.src)
                    except OSError:
                        st = None
                    if st is not None and not stat.S_ISLNK(st.st_mode):
                        _safe_apply_meta(fd, dst_path, st, src_path=entry.src)
                finally:
                    os.close(fd)
                _log("copied", entry.rel, entry.size, method="individual")
                progress.update(0, 1)
                progress.display()
                continue

            # Try reflink first when the destination FS supports it.
            # Reflinks are O(1) metadata operations — instant for any size.
            if fs_strategy == "reflink" and _try_reflink(entry.src, dst_path):
                # Preserve timestamps and permissions even on reflink.
                # _try_reflink creates dst_path itself; verify it's not a symlink before chmod.
                try:
                    dl = os.lstat(dst_path)
                    if not stat.S_ISLNK(dl.st_mode):
                        st = os.lstat(entry.src)
                        if not stat.S_ISLNK(st.st_mode):
                            os.utime(dst_path, (st.st_atime, st.st_mtime))
                            os.chmod(dst_path, stat.S_IMODE(st.st_mode))
                except OSError:
                    pass
                _log("copied", entry.rel, entry.size, method="reflink")
                progress.update(entry.size, 1)
                progress.display()
                continue

            # Sparse-aware path: when the source file has unallocated holes,
            # use SEEK_DATA/SEEK_HOLE to skip them. Critical for VM disk
            # images / Longhorn replica `.img` files where logical size is
            # huge but actual allocated bytes are small.
            if (_HAS_SEEK_HOLE and entry.alloc_size is not None
                    and entry.alloc_size < entry.size):
                ok = _copy_sparse(entry.src, dst_path, buf, progress,
                                  cancel_check)
                if not ok:
                    return
            else:
                try:
                    src_fd_raw = _safe_open_read_fd(entry.src)
                except OSError as e:
                    if e.errno == errno.ELOOP:
                        print(f"\n  {C.RED}Refusing to follow symlink: {entry.src}{C.RESET}")
                        _log("error", entry.rel, entry.size, error="symlink in source")
                        continue
                    raise
                try:
                    dst_fd_raw = _safe_open_write_fd(dst_path, truncate=True)
                except OSError as e:
                    os.close(src_fd_raw)
                    if e.errno == errno.ELOOP:
                        print(f"\n  {C.RED}Refusing to follow symlink at destination: {dst_path}{C.RESET}")
                        _log("error", entry.rel, entry.size, error="symlink at destination")
                        continue
                    raise
                with os.fdopen(src_fd_raw, "rb") as fin, os.fdopen(dst_fd_raw, "wb") as fout:
                    dst_fd_keep = fout.fileno()
                    while True:
                        # Check for cancellation during large file copy
                        if cancel_check and cancel_check():
                            # Clean up partial file
                            try:
                                os.remove(dst_path)
                            except OSError:
                                pass
                            return
                        n = fin.readinto(buf)
                        if not n:
                            break
                        fout.write(mv[:n])
                        progress.update(n)
                        progress.display()
                    try:
                        st = os.fstat(fin.fileno())
                        _safe_apply_meta(dst_fd_keep, dst_path, st, src_path=entry.src)
                    except OSError:
                        pass

            _log("copied", entry.rel, entry.size, method="individual")
            progress.update(0, 1)

        except (OSError, IOError) as e:
            print(f"\n  {C.RED}Error: {entry.rel}: {e}{C.RESET}")
            _log("error", entry.rel, entry.size, error=str(e))
            # Clean up partial file
            try:
                if os.path.exists(dst_path):
                    os.remove(dst_path)
            except OSError:
                pass
            progress.update(entry.size, 1)


def copy_hybrid(entries, dst_root, progress, buf_size, cancel_check=None,
                fs_strategy=None):
    """
    Hybrid block copy engine:
      - Reflink-capable FS (btrfs, XFS reflink, APFS, ReFS): all files via
        copy_individual using reflinks (instant, metadata-only)
      - Otherwise: small files (<1MB) bundled into tar block stream, large
        files (>=1MB) via individual copy with large buffers
    """
    # Reflink (FICLONE / clonefile) only works *within* the same filesystem.
    # The destination FS detection alone doesn't catch the cross-mount case,
    # so probe the actual source(s) here. If any source is on a different
    # st_dev than the destination, every reflink call will EXDEV and fall
    # back to byte copy — so don't promise "metadata-only" in the banner.
    if fs_strategy == "reflink" and entries:
        try:
            dst_dev = os.stat(dst_root).st_dev
            src_devs = {os.stat(e.src).st_dev for e in entries[:64]}
            if len(src_devs) != 1 or dst_dev not in src_devs:
                fs_strategy = None
        except OSError:
            pass

    # On reflink-capable destinations, route ALL files through copy_individual
    # so each one gets cloned via FICLONE/clonefile. The tar-bundle path for
    # small files is meant to amortize syscall overhead from byte copies —
    # but reflinks are metadata-only, so the per-file syscall is essentially
    # free and there's no benefit to bundling.
    if fs_strategy == "reflink":
        total_size = sum(e.size for e in entries)
        print(f"  Strategy: {C.CYAN}reflink (CoW){C.RESET} for "
              f"{C.BOLD}{len(entries)}{C.RESET} files, "
              f"{C.BOLD}{fmt_size(total_size)}{C.RESET}")
        print(f"    {C.DIM}Metadata-only clone — no data is read or "
              f"written.{C.RESET}")
        print()
        buf = bytearray(buf_size)
        copy_individual(entries, dst_root, progress, buf, cancel_check,
                        fs_strategy=fs_strategy)
        return

    small, large = split_by_size(entries)
    small_size = sum(e.size for e in small)
    large_size = sum(e.size for e in large)

    print(f"  Strategy:")
    print(f"    Small files (<1MB): {C.BOLD}{len(small)}{C.RESET} files, "
          f"{C.BOLD}{fmt_size(small_size)}{C.RESET} → block stream")
    print(f"    Large files (≥1MB): {C.BOLD}{len(large)}{C.RESET} files, "
          f"{C.BOLD}{fmt_size(large_size)}{C.RESET} → individual copy")
    print()

    # Copy large files first (they benefit most from physical ordering)
    if large:
        print(f"  {C.BOLD}── Large files ──{C.RESET}")
        buf = bytearray(buf_size)
        copy_individual(large, dst_root, progress, buf, cancel_check,
                        fs_strategy=fs_strategy)
        if cancel_check and cancel_check():
            return
        print()

    # Block-stream small files
    if small:
        print(f"  {C.BOLD}── Small files (block stream) ──{C.RESET}")
        copy_block_stream(small, dst_root, progress, cancel_check)


# ════════════════════════════════════════════════════════════════════════════
# SSH REMOTE COPY — large files (SFTP) + small files (tar stream)
# ════════════════════════════════════════════════════════════════════════════
def copy_individual_remote(entries, ssh, remote_root, progress, buf_size):
    """Copy large files to remote via SFTP with pipelined writes."""
    sftp = ssh.open_sftp()
    buf = bytearray(buf_size)
    mv = memoryview(buf)

    for entry in entries:
        remote_path = posixpath.join(remote_root, entry.rel)

        try:
            if entry.size == 0:
                with sftp.open(remote_path, "w"):
                    pass
                _log("copied", entry.rel, entry.size, method="sftp")
                progress.update(0, 1)
                progress.display()
                continue

            try:
                src_fd = _safe_open_read_fd(entry.src)
            except OSError as e:
                if e.errno == errno.ELOOP:
                    print(f"\n  {C.RED}Refusing to follow symlink: {entry.src}{C.RESET}")
                    _log("error", entry.rel, entry.size, error="symlink in source (elevated)")
                    progress.update(entry.size, 1)
                    continue
                raise
            with os.fdopen(src_fd, "rb") as fin:
                with sftp.open(remote_path, "wb") as fout:
                    fout.set_pipelined(True)
                    while True:
                        n = fin.readinto(buf)
                        if not n:
                            break
                        fout.write(mv[:n])
                        progress.update(n)
                        progress.display()
                try:
                    st = os.fstat(fin.fileno())
                except OSError:
                    st = None

            # Preserve timestamps
            try:
                if st is None:
                    st = os.stat(entry.src)
                sftp.utime(remote_path, (st.st_atime, st.st_mtime))
            except OSError:
                pass

            _log("copied", entry.rel, entry.size, method="sftp")
            progress.update(0, 1)

        except (OSError, IOError) as e:
            print(f"\n  {C.RED}Error: {entry.rel}: {e}{C.RESET}")
            _log("error", entry.rel, entry.size, error=str(e))
            progress.update(entry.size, 1)


TAR_CHUNK_SIZE = 100 * 1024 * 1024  # 100 MB per tar batch


def _batch_by_size(entries, max_bytes=TAR_CHUNK_SIZE, max_files=10000):
    """Split entries into batches of approximately max_bytes or max_files each.

    Flushes the current batch *before* appending an entry that would overshoot,
    so a large file doesn't get bundled with earlier small ones into one batch.
    A single entry larger than max_bytes is still kept on its own."""
    batches = []
    current = []
    current_size = 0
    for e in entries:
        if current and (current_size + e.size > max_bytes or
                        len(current) >= max_files):
            batches.append(current)
            current = []
            current_size = 0
        current.append(e)
        current_size += e.size
    if current:
        batches.append(current)
    return batches


class _ChannelWriter:
    """File-like wrapper for paramiko channel — used by tarfile streaming."""
    def __init__(self, channel):
        self.channel = channel
        self.written = 0
    def write(self, data):
        self.channel.sendall(data)
        self.written += len(data)
        return len(data)
    def close(self):
        pass
    def tell(self):
        return self.written


def _stream_tar_batch_to_remote(batch, ssh, remote_root, progress):
    """Upload one batch of files via tar stream to remote."""
    channel = ssh.open_channel()
    channel.exec_command(
        f"tar xf - --no-same-owner --no-same-permissions -C {shlex.quote(remote_root)}"
    )

    writer = _ChannelWriter(channel)
    errors = 0

    try:
        with tarfile.open(fileobj=writer, mode="w|") as tar:
            for entry in batch:
                try:
                    check = _validate_rel_path(entry.rel)
                    if check is not True:
                        _log("error", entry.rel, entry.size, error=f"unsafe path: {check}")
                        errors += 1
                        continue
                    try:
                        src_fd = _safe_open_read_fd(entry.src)
                    except OSError as e:
                        if e.errno == errno.ELOOP:
                            _log("error", entry.rel, entry.size,
                                 error="symlink in source (elevated)")
                            errors += 1
                            continue
                        raise
                    with os.fdopen(src_fd, "rb") as f:
                        st = os.fstat(f.fileno())
                        actual_size = st.st_size
                        info = tarfile.TarInfo(name=entry.rel)
                        info.size = actual_size
                        info.mtime = st.st_mtime
                        info.mode = st.st_mode & 0o7777
                        tar.addfile(info, f)

                    _log("copied", entry.rel, entry.size, method="tar_stream")
                    progress.update(entry.size, 1)
                    progress.display()
                except (OSError, IOError) as e:
                    _log("error", entry.rel, entry.size, error=str(e))
                    errors += 1
    except Exception as e:
        print(f"\n  {C.RED}Tar stream error: {e}{C.RESET}")

    channel.shutdown_write()
    rc = channel.recv_exit_status()

    if rc != 0:
        stderr = channel.recv_stderr(4096).decode("utf-8", errors="replace")
        print(f"\n  {C.YELLOW}Remote tar exited {rc}: {stderr[:200]}{C.RESET}")

    if errors:
        print(f"  {C.YELLOW}{errors} files failed to stream{C.RESET}")

    channel.close()
    return writer.written


def copy_block_stream_remote(entries, ssh, remote_root, progress):
    """Stream files as chunked tar batches over SSH → remote tar extracts."""
    if not entries:
        return

    if not ssh.caps.get("tar"):
        print(f"  {C.YELLOW}Remote has no tar — falling back to SFTP{C.RESET}")
        copy_individual_remote(entries, ssh, remote_root, progress, 1 * 1024 * 1024)
        return

    total_size = sum(e.size for e in entries)
    batches = _batch_by_size(entries)
    print(f"  Streaming {len(entries)} files ({fmt_size(total_size)}) in "
          f"{len(batches)} batch{'es' if len(batches) != 1 else ''} to remote...")

    total_sent = 0
    for i, batch in enumerate(batches):
        batch_size = sum(e.size for e in batch)
        if len(batches) > 1:
            print(f"\n  {C.DIM}Batch {i+1}/{len(batches)}: {len(batch)} files "
                  f"({fmt_size(batch_size)}){C.RESET}")
        total_sent += _stream_tar_batch_to_remote(batch, ssh, remote_root, progress)

    print(f"\n  {C.GREEN}Tar stream: {fmt_size(total_sent)} sent{C.RESET}")


def copy_hybrid_remote(entries, ssh, remote_root, progress, buf_size):
    """Local-to-remote: tar stream for all files (much faster than SFTP)."""
    total_size = sum(e.size for e in entries)

    # Create all directories first in one shot
    ensure_remote_dirs(ssh, remote_root, entries)

    if ssh.caps.get("tar"):
        print(f"  Strategy: tar stream for all {C.BOLD}{len(entries)}{C.RESET} files "
              f"({C.BOLD}{fmt_size(total_size)}{C.RESET})")
        print()
        copy_block_stream_remote(entries, ssh, remote_root, progress)
    else:
        # Fallback: SFTP for everything if tar not available
        print(f"  Strategy (no remote tar — using SFTP):")
        print(f"    {C.BOLD}{len(entries)}{C.RESET} files, "
              f"{C.BOLD}{fmt_size(total_size)}{C.RESET}")
        print()
        copy_individual_remote(entries, ssh, remote_root, progress, buf_size)


def create_links_remote(ssh, link_map, remote_root):
    """Create hard links on remote via a single Python script over SSH."""
    if not link_map:
        return

    print(f"  {C.DIM}Creating {len(link_map)} links on remote...{C.RESET}", end="", flush=True)

    # Build link pairs: source\tdest per line
    lines = []
    for dup_rel, target in link_map.items():
        dst_dup = posixpath.join(remote_root, dup_rel)
        if isinstance(target, tuple) and target[0] == "__abs__":
            dst_canonical = target[1]
            if '\0' in dst_canonical or not posixpath.isabs(dst_canonical):
                continue
        else:
            dst_canonical = posixpath.join(remote_root, target)
            if '..' in target.split('/') or '\0' in target:
                continue
        lines.append(f"{dst_canonical}\t{dst_dup}")

    link_input = "\n".join(lines) + "\n"

    # Remote Python script: reads pairs from stdin, creates links efficiently
    script = (
        'import sys,os\n'
        'ok=fail=0\n'
        'for line in sys.stdin:\n'
        '  line=line.strip()\n'
        '  if not line:continue\n'
        '  parts=line.split("\\t",1)\n'
        '  if len(parts)!=2:continue\n'
        '  src,dst=parts\n'
        '  try:\n'
        '    os.makedirs(os.path.dirname(dst),exist_ok=True)\n'
        '    try:os.link(src,dst);ok+=1\n'
        '    except OSError:\n'
        '      try:os.symlink(src,dst);ok+=1\n'
        '      except OSError:\n'
        '        import shutil;shutil.copy2(src,dst);ok+=1\n'
        '  except Exception:fail+=1\n'
        'print(f"{ok} {fail}")\n'
    )

    BATCH = 5000
    total_ok = 0
    total_failed = 0
    for i in range(0, len(lines), BATCH):
        batch_input = "\n".join(lines[i:i + BATCH]) + "\n"
        out, _, rc = ssh.exec_cmd(
            f"python3 -c {shlex.quote(script)}", input_data=batch_input, timeout=600
        )
        try:
            parts = out.strip().split()
            total_ok += int(parts[0])
            total_failed += int(parts[1])
        except (ValueError, IndexError):
            total_failed += min(BATCH, len(lines) - i)

        if len(lines) > BATCH:
            done = min(i + BATCH, len(lines))
            sys.stdout.write(f"\r  {C.DIM}Links: {done}/{len(lines)}...{C.RESET}          ")
            sys.stdout.flush()

    if total_failed:
        print(f"\r  {C.YELLOW}Links: {total_ok} created, {total_failed} failed on remote{C.RESET}                    ")
    else:
        print(f"\r  {C.GREEN}Links created: {total_ok} on remote{C.RESET}                    ")


def verify_copy_remote(ssh, entries, link_map, remote_root):
    """Verify files on remote: check existence + size, and hash-verify a sample.
    Note: remote verification is inherently trust-based — a compromised server
    can fake results. Hash spot-checks raise the bar for undetected tampering."""
    total_to_check = len(entries) + len(link_map)
    print(f"\n  {C.DIM}Verifying {total_to_check} files on remote...{C.RESET}", end="", flush=True)

    remote_files = scan_remote_destination(ssh, remote_root)

    missing = []
    mismatches = []   # destination smaller than expected → real failure
    grew = []         # destination larger than expected → likely active writer
    grew_rels = set()

    for entry in entries:
        if entry.rel not in remote_files:
            missing.append(entry.rel)
        elif remote_files[entry.rel] != entry.size:
            if remote_files[entry.rel] > entry.size:
                grew.append((entry.rel, entry.size, remote_files[entry.rel]))
                grew_rels.add(entry.rel)
            else:
                mismatches.append((entry.rel, entry.size, remote_files[entry.rel]))

    for dup_rel in link_map:
        if dup_rel not in remote_files:
            missing.append(f"{dup_rel} (link)")

    total_checked = total_to_check

    # Hash spot-check: verify a sample of files by hashing on remote.
    # Skip files known to have grown during copy — their hash will differ by
    # design, that's not a corruption signal.
    # remote_hash_files always uses sha256, so re-hash locally with sha256
    hash_failures = []
    if not missing and not mismatches and entries:
        hashed_entries = [e for e in entries
                          if e.content_hash and e.rel not in grew_rels]
        if hashed_entries:
            import random
            sample_size = min(20, len(hashed_entries))
            sample = random.sample(hashed_entries, sample_size)
            sample_rels = [e.rel for e in sample]
            remote_hashes = remote_hash_files(ssh, remote_root, sample_rels)
            for e in sample:
                rh = remote_hashes.get(e.rel)
                if rh:
                    local_sha = hash_file_sha256(e.src)
                    if local_sha and rh != local_sha:
                        hash_failures.append(e.rel)

    if not missing and not mismatches and not hash_failures:
        if grew:
            print(f"\r  {C.GREEN}✓ Verified: all {total_checked} files OK on remote{C.RESET}"
                  f" {C.YELLOW}({len(grew)} grew during copy){C.RESET}")
            for rel, exp, act in grew[:10]:
                print(f"    {C.YELLOW}GREW DURING COPY: {rel} "
                      f"(+{act - exp} bytes — likely an active writer){C.RESET}")
            if len(grew) > 10:
                print(f"    ... and {len(grew) - 10} more")
        else:
            print(f"\r  {C.GREEN}✓ Verified: all {total_checked} files OK on remote{C.RESET}               ")
        return True
    else:
        print(f"\r  {C.RED}✗ Verification failed:{C.RESET}")
        for m in missing[:10]:
            print(f"    {C.RED}MISSING: {m}{C.RESET}")
        for rel, exp, act in mismatches[:10]:
            print(f"    {C.RED}SIZE MISMATCH: {rel} ({exp} → {act}){C.RESET}")
        for rel in hash_failures[:10]:
            print(f"    {C.RED}HASH MISMATCH: {rel}{C.RESET}")
        for rel, exp, act in grew[:10]:
            print(f"    {C.YELLOW}GREW DURING COPY: {rel} "
                  f"(+{act - exp} bytes){C.RESET}")
        shown = (min(len(missing), 10) + min(len(mismatches), 10)
                 + min(len(hash_failures), 10) + min(len(grew), 10))
        remain = len(missing) + len(mismatches) + len(hash_failures) + len(grew) - shown
        if remain > 0:
            print(f"    ... and {remain} more")
        return False


# ════════════════════════════════════════════════════════════════════════════
# VERIFICATION
# ════════════════════════════════════════════════════════════════════════════
def verify_copy(entries, link_map, dst_root):
    """Check existence + file size for all files (unique + linked).
    Uses a single os.walk pass instead of per-file stat calls."""
    total_to_check = len(entries) + len(link_map)
    print(f"\n  {C.DIM}Verifying {total_to_check} files...{C.RESET}", end="", flush=True)

    # Build expected files: rel_path → expected_size (None = just check exists)
    expected = {}
    for entry in entries:
        expected[entry.rel] = entry.size
    for dup_rel in link_map:
        expected[dup_rel] = None  # links: just check existence

    # Single walk of destination — much faster than per-file stat on USB
    # Use _long_path for walk (to see long-path files) but strip it for relpath
    walk_root = _long_path(dst_root)
    rel_base = _strip_long_path(walk_root)
    found = {}
    for root, dirs, files in os.walk(walk_root):
        for fname in files:
            full = os.path.join(root, fname)
            try:
                rel = os.path.relpath(_strip_long_path(full), rel_base).replace(os.sep, "/")
            except ValueError:
                # Windows: path on a different mount (e.g. \\.\nul device)
                print(f"\n  {C.DIM}Skipped verify for cross-mount path: "
                      f"{fname}{C.RESET}", end="", flush=True)
                continue
            if rel in expected:
                try:
                    found[rel] = os.path.getsize(full)
                except OSError:
                    pass

    mismatches = []   # destination smaller than expected → real failure
    grew = []         # destination larger than expected → likely active writer
    missing = []
    for rel, exp_size in expected.items():
        if rel not in found:
            tag = " (link)" if exp_size is None else ""
            missing.append(f"{rel}{tag}")
        elif exp_size is not None and found[rel] != exp_size:
            if found[rel] > exp_size:
                grew.append((rel, exp_size, found[rel]))
            else:
                mismatches.append((rel, exp_size, found[rel]))

    total_checked = len(expected)

    if not missing and not mismatches:
        if grew:
            print(f"\r  {C.GREEN}✓ Verified: all {total_checked} files OK{C.RESET}"
                  f" {C.YELLOW}({len(grew)} grew during copy){C.RESET}")
            for rel, exp, act in grew[:10]:
                print(f"    {C.YELLOW}GREW DURING COPY: {rel} "
                      f"(+{act - exp} bytes — likely an active writer){C.RESET}")
            if len(grew) > 10:
                print(f"    ... and {len(grew) - 10} more")
        else:
            print(f"\r  {C.GREEN}✓ Verified: all {total_checked} files OK{C.RESET}               ")
        return True
    else:
        print(f"\r  {C.RED}✗ Verification failed:{C.RESET}")
        for m in missing[:10]:
            print(f"    {C.RED}MISSING: {m}{C.RESET}")
        for rel, exp, act in mismatches[:10]:
            print(f"    {C.RED}SIZE MISMATCH: {rel} ({exp} → {act}){C.RESET}")
        for rel, exp, act in grew[:10]:
            print(f"    {C.YELLOW}GREW DURING COPY: {rel} "
                  f"(+{act - exp} bytes){C.RESET}")
        shown = min(len(missing), 10) + min(len(mismatches), 10) + min(len(grew), 10)
        remain = len(missing) + len(mismatches) + len(grew) - shown
        if remain > 0:
            print(f"    ... and {remain} more")
        return False


# ════════════════════════════════════════════════════════════════════════════
# SKIP IDENTICAL FILES (incremental mode)
# ════════════════════════════════════════════════════════════════════════════
def filter_unchanged(entries, link_map, dst_root, threads=DEFAULT_THREADS):
    """
    Compare source files against existing destination files.
    Skip files that already exist at destination with identical content.

    Strategy (fast to slow):
      1. If destination file doesn't exist → must copy
      2. If sizes differ → must copy
      3. If sizes match → hash both and compare → skip if identical

    Returns (to_copy, to_link, skipped_count, skipped_bytes)
    """
    print(f"  {C.DIM}Checking destination for existing files...{C.RESET}", end="", flush=True)

    need_copy = []     # entries that need copying
    need_hash = []     # entries where dest exists with same size → need hash check
    skipped = []       # entries skipped (identical)
    skipped_bytes = 0

    # ── Quick pass: size check ────────────────────────────────────────
    for entry in entries:
        dst_path = os.path.join(dst_root, entry.rel)

        if not os.path.exists(dst_path):
            need_copy.append(entry)
            continue

        try:
            dst_size = os.path.getsize(dst_path)
        except OSError:
            need_copy.append(entry)
            continue

        if dst_size != entry.size:
            # Different size → must overwrite
            need_copy.append(entry)
        else:
            # Same size → need hash comparison
            need_hash.append(entry)

    print(f"\r  {C.DIM}Quick check: {len(need_copy)} new/changed, "
          f"{len(need_hash)} same-size need hash check{C.RESET}          ")

    if not need_hash:
        # Nothing to hash-check, everything is new
        return need_copy, link_map, 0, 0

    # ── Hash pass: compare content of same-size files ─────────────────
    print(f"  {C.DIM}Hashing {len(need_hash)} files to check for changes...{C.RESET}", end="", flush=True)

    src_hashes = [None] * len(need_hash)
    dst_hashes = [None] * len(need_hash)

    def hash_pair(idx):
        entry = need_hash[idx]
        dst_path = os.path.join(dst_root, entry.rel)
        src_hashes[idx] = hash_file(entry.src)
        dst_hashes[idx] = hash_file(dst_path)

    done_count = [0]
    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=threads) as pool:
        futures = [pool.submit(hash_pair, i) for i in range(len(need_hash))]
        for f in as_completed(futures):
            f.result()
            with lock:
                done_count[0] += 1
                if done_count[0] % 200 == 0:
                    print(f"\r  {C.DIM}Hashing... {done_count[0]}/{len(need_hash)}{C.RESET}",
                          end="", flush=True)

    # Compare hashes
    for i, entry in enumerate(need_hash):
        if (src_hashes[i] is not None and dst_hashes[i] is not None
                and src_hashes[i] == dst_hashes[i]):
            _log("skipped", entry.rel, entry.size, reason="unchanged")
            skipped.append(entry)
            skipped_bytes += entry.size
        else:
            need_copy.append(entry)

    # Also filter link_map — skip links where destination already exists
    new_link_map = {}
    skipped_links = 0
    for dup_rel, canonical_rel in link_map.items():
        dst_path = os.path.join(dst_root, dup_rel)
        if os.path.exists(dst_path):
            _log("skipped", dup_rel, 0, reason="link_exists")
            skipped_links += 1
        else:
            new_link_map[dup_rel] = canonical_rel

    print(f"\r  {C.GREEN}Incremental check complete:{C.RESET}                              ")
    print(f"    To copy:   {C.BOLD}{len(need_copy)}{C.RESET} files "
          f"({fmt_size(sum(e.size for e in need_copy))})")
    print(f"    Skipped:   {C.BOLD}{len(skipped)}{C.RESET} files unchanged "
          f"({C.GREEN}{fmt_size(skipped_bytes)}{C.RESET})")
    if skipped_links:
        print(f"    Links:     {C.BOLD}{skipped_links}{C.RESET} already exist, "
              f"{C.BOLD}{len(new_link_map)}{C.RESET} to create")

    return need_copy, new_link_map, len(skipped) + skipped_links, skipped_bytes


# ════════════════════════════════════════════════════════════════════════════
# REMOTE SOURCE — scan, hash, and copy FROM a remote machine
# ════════════════════════════════════════════════════════════════════════════

def scan_remote_source(ssh, src_root, excludes=None):
    """Scan remote source tree via SSH find. Returns (entries, errors)."""
    print(f"  {C.DIM}Scanning remote source...{C.RESET}", end="", flush=True)

    exclude_patterns = [TAR_BUNDLE_NAME, DEDUP_DB_NAME, REMOTE_MANIFEST_NAME,
                        SUDO_AUDIT_FILE]
    if excludes:
        exclude_patterns.extend(excludes)

    # Build a -name OR-group used both to prune matching directories and to
    # reject matching files. shlex.quote keeps glob metacharacters intact for
    # find's -name (which does its own globbing).
    name_or = " -o ".join(f"-name {shlex.quote(p)}" for p in exclude_patterns)
    prune_dirs = rf"-type d \( {name_or} \) -prune"
    file_filter = rf"-type f ! \( {name_or} \)"

    if ssh.caps.get("gnu_find"):
        cmd = (f'find {shlex.quote(src_root)} {prune_dirs} -o '
               f'\\( {file_filter} -printf "%s\\t%p\\n" \\) 2>/dev/null')
    else:
        cmd = (f'find {shlex.quote(src_root)} {prune_dirs} -o '
               f'\\( {file_filter} -exec stat -c "%s %n" {{}} + \\) 2>/dev/null || '
               f'find {shlex.quote(src_root)} {prune_dirs} -o '
               f'\\( {file_filter} -exec stat -f "%z %N" {{}} + \\) 2>/dev/null')

    out, _, rc = ssh.exec_cmd(cmd, timeout=600)

    entries = []
    errors = []
    count = 0

    for line in out.strip().split("\n"):
        if not line:
            continue
        sep = "\t" if ssh.caps.get("gnu_find") else None
        parts = line.split(sep, 1) if sep else line.split(None, 1)
        if len(parts) == 2:
            try:
                size = int(parts[0])
                path = parts[1].strip()
                rel = posixpath.relpath(path, src_root)
                entries.append(FileEntry(
                    src=path, rel=rel, size=size,
                    physical_offset=0, content_hash=None,
                ))
                count += 1
                if count % 5000 == 0:
                    print(f"\r  {C.DIM}Scanning... {count} files{C.RESET}",
                          end="", flush=True)
            except (ValueError, TypeError):
                errors.append((parts[1].strip() if len(parts) > 1 else "?", "parse error"))

    print(f"\r  {C.GREEN}Found {len(entries)} files on remote{C.RESET}                    ")
    if errors:
        print(f"  {C.YELLOW}Skipped {len(errors)} problematic entries{C.RESET}")

    return entries, errors


def deduplicate_remote_source(entries, ssh, src_root, threads=DEFAULT_THREADS,
                               fs_strategy=None):
    """
    Dedup by hashing files on the remote source machine.
    Returns (unique_entries, link_map, saved_bytes).
    """
    total = len(entries)
    print(f"  Hashing {total} files on remote source...")

    rel_paths = [e.rel for e in entries]
    remote_hashes = remote_hash_files(ssh, src_root, rel_paths)

    hashed = 0
    hashed_entries = []
    for e in entries:
        h = remote_hashes.get(e.rel)
        hashed_entries.append(e._replace(content_hash=h))
        if h:
            hashed += 1

    print(f"  {C.GREEN}Hashed {hashed}/{total} files on remote{C.RESET}")

    if not remote_hashes:
        print(f"  {C.YELLOW}Could not hash on remote — skipping dedup{C.RESET}")
        return entries, {}, 0

    hash_groups = defaultdict(list)
    unique_entries = []

    for e in hashed_entries:
        if e.content_hash:
            hash_groups[(e.size, e.content_hash)].append(e)
        else:
            unique_entries.append(e)

    link_map = {}
    saved_bytes = 0

    for key, group in hash_groups.items():
        unique_entries.append(group[0])
        for dup in group[1:]:
            link_map[dup.rel] = group[0].rel
            saved_bytes += dup.size

    dup_count = len(link_map)
    total_input_size = sum(e.size for e in entries)
    print(f"  {C.GREEN}Dedup complete:{C.RESET}")
    print(f"    Unique files:    {C.BOLD}{len(unique_entries)}{C.RESET}")
    print(f"    Duplicates:      {C.BOLD}{dup_count}{C.RESET} "
          f"({fmt_pct(dup_count, len(entries))} of files)")
    # On link-incapable filesystems, dedup only saves bandwidth — duplicates
    # become real copies on disk. Be honest about which saving applies.
    if fs_strategy == "none" and saved_bytes > 0:
        print(f"    Bandwidth saved: {C.GREEN}{C.BOLD}{fmt_size(saved_bytes)}{C.RESET} "
              f"(transfer only)")
        print(f"    Disk usage:      {C.BOLD}{fmt_size(total_input_size)}{C.RESET} "
              f"{C.YELLOW}(full copies — FS does not support links){C.RESET}")
    else:
        print(f"    Space saved:     {C.GREEN}{C.BOLD}{fmt_size(saved_bytes)}{C.RESET}")

    return unique_entries, link_map, saved_bytes


# ════════════════════════════════════════════════════════════════════════════
# REMOTE → LOCAL COPY
# ════════════════════════════════════════════════════════════════════════════

def copy_individual_remote_to_local(entries, ssh, dst_root, progress, buf_size,
                                    case_renames=None):
    """Download large files from remote to local via SFTP."""
    sftp = ssh.open_sftp()

    for entry in entries:
        remote_path = entry.src
        dst_path = _long_path(os.path.join(dst_root, entry.rel))

        try:
            os.makedirs(os.path.dirname(dst_path), exist_ok=True)

            if entry.size == 0:
                try:
                    fd = _safe_open_write_fd(dst_path, truncate=True)
                except OSError as e:
                    if e.errno == errno.ELOOP:
                        print(f"\n  {C.RED}Refusing to follow symlink at destination: {dst_path}{C.RESET}")
                        _log("error", entry.rel, entry.size, error="symlink at destination")
                        continue
                    raise
                os.close(fd)
                try:
                    rstat = sftp.stat(remote_path)
                    dl = os.lstat(dst_path)
                    if not stat.S_ISLNK(dl.st_mode):
                        os.utime(dst_path, (rstat.st_atime, rstat.st_mtime))
                        os.chmod(dst_path, stat.S_IMODE(rstat.st_mode))
                except (OSError, IOError):
                    pass
                _log("copied", entry.rel, entry.size, method="sftp")
                progress.update(0, 1)
                progress.display()
                continue

            try:
                dst_fd_raw = _safe_open_write_fd(dst_path, truncate=True)
            except OSError as e:
                if e.errno == errno.ELOOP:
                    print(f"\n  {C.RED}Refusing to follow symlink at destination: {dst_path}{C.RESET}")
                    _log("error", entry.rel, entry.size, error="symlink at destination")
                    continue
                raise
            with sftp.open(remote_path, "rb") as fin, os.fdopen(dst_fd_raw, "wb") as fout:
                fin.prefetch(min(entry.size, 256 * 1024 * 1024))  # cap at 256MB to limit memory
                while True:
                    data = fin.read(buf_size)
                    if not data:
                        break
                    fout.write(data)
                    progress.update(len(data))
                    progress.display()

            try:
                rstat = sftp.stat(remote_path)
                dl = os.lstat(dst_path)
                if not stat.S_ISLNK(dl.st_mode):
                    os.utime(dst_path, (rstat.st_atime, rstat.st_mtime))
                    os.chmod(dst_path, stat.S_IMODE(rstat.st_mode))
            except (OSError, IOError):
                pass

            _log("copied", entry.rel, entry.size, method="sftp")
            progress.update(0, 1)

        except (OSError, IOError) as e:
            print(f"\n  {C.RED}Error: {entry.rel}: {e}{C.RESET}")
            _log("error", entry.rel, entry.size, error=str(e))
            progress.update(entry.size, 1)


class _ProgressTarExtractor:
    """Extract tar members with byte-level progress for large files."""

    def __init__(self, tar, dst_root, progress, allowed_files=None, rename_map=None):
        self._tar = tar
        self._dst_root = dst_root
        self._progress = progress
        self.extracted = 0
        self.rejected = 0
        # If provided, only extract files in this set (prevents injection)
        self._allowed = set(allowed_files) if allowed_files else None
        # Map of original_name -> new_name for case-conflict renames
        self._rename_map = rename_map or {}

    # Maximum bytes to extract from a single tar member (50 GB safety limit)
    MAX_MEMBER_SIZE = 50 * 1024 * 1024 * 1024

    def extract_member(self, member):
        """Extract one member. Large files get mid-extraction progress updates."""
        # Directories: extract silently, don't count in progress
        if member.isdir():
            # Validate even directories
            check = _validate_tar_member(member, self._dst_root)
            if check is not True:
                return check
            result = _safe_tar_extract(self._tar, member, self._dst_root)
            return True

        # Full validation (rejects symlinks, devices, hard links, etc.)
        check = _validate_tar_member(member, self._dst_root)
        if check is not True:
            return check

        # Reject files not in the expected allowlist (prevents injection)
        if self._allowed is not None and member.name not in self._allowed:
            self.rejected += 1
            return "blocked: unexpected file (not in transfer list)"

        # Apply case-conflict rename if needed
        if member.name in self._rename_map:
            member.name = self._rename_map[member.name]

        # Empty or small file — extract normally, update after
        if member.size < 1 * 1024 * 1024:
            result = _safe_tar_extract(self._tar, member, self._dst_root)
            if result is True:
                self.extracted += 1
                _log("copied", member.name, member.size, method="tar_stream")
                self._progress.update(member.size, 1)
                self._progress.display()
            else:
                _log("error", member.name, member.size, error=str(result))
            return result

        # Large file — extract with progress updates during write
        # Validate with plain paths, use _long_path only for I/O.
        # Use normcase for the comparison to handle case-insensitive
        # filesystems (Windows NTFS, macOS HFS+/APFS).
        resolved = os.path.realpath(os.path.join(self._dst_root, member.name))
        real_dst = os.path.realpath(self._dst_root)
        nc_resolved = os.path.normcase(resolved)
        nc_real_dst = os.path.normcase(real_dst)
        if not (nc_resolved == nc_real_dst or
                nc_resolved.startswith(nc_real_dst + os.sep)):
            return "blocked: resolves outside destination"

        io_path = _long_path(resolved)
        os.makedirs(os.path.dirname(io_path), exist_ok=True)
        fileobj = self._tar.extractfile(member)
        if fileobj is None:
            return "blocked: cannot extract"

        written = 0
        try:
            try:
                io_fd = _safe_open_write_fd(io_path, truncate=True)
            except OSError as e:
                if e.errno == errno.ELOOP:
                    return "blocked: symlink at destination path"
                raise
            with os.fdopen(io_fd, "wb") as fout:
                while True:
                    chunk = fileobj.read(1048576)  # 1 MB
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > self.MAX_MEMBER_SIZE:
                        fout.close()
                        os.remove(io_path)
                        return f"blocked: exceeds {self.MAX_MEMBER_SIZE // (1024**3)} GB safety limit"
                    fout.write(chunk)
                    self._progress.update(len(chunk))
                    self._progress.display()
        finally:
            fileobj.close()

        # Preserve mtime
        try:
            os.utime(io_path, (member.mtime, member.mtime))
        except OSError:
            pass

        self.extracted += 1
        _log("copied", member.name, member.size, method="tar_stream")
        self._progress.update(0, 1)  # file count only, bytes already reported
        self._progress.display()
        return True


def _stream_tar_batch_from_remote(batch, ssh, src_root, dst_root, progress,
                                   case_renames=None):
    """Download one batch of files via tar stream with streaming extraction."""
    import threading

    # Build reverse map: new_rel -> original_rel (for fetching from remote)
    _rev = {v: k for k, v in (case_renames or {}).items()}
    file_list = "\0".join(_rev.get(e.rel, e.rel) for e in batch) + "\0"
    file_list_bytes = file_list.encode("utf-8")

    channel = ssh.open_channel()
    channel.exec_command(
        f"cd {shlex.quote(src_root)} && tar cf - --null -T -"
    )

    def _send_file_list():
        try:
            chunk_size = 65536
            for i in range(0, len(file_list_bytes), chunk_size):
                channel.sendall(file_list_bytes[i:i + chunk_size])
        finally:
            channel.shutdown_write()

    sender = threading.Thread(target=_send_file_list, daemon=True)
    sender.start()

    # Streaming extraction with byte-level progress for large files (no temp file)
    os.makedirs(dst_root, exist_ok=True)
    reader = channel.makefile("rb")
    extracted = 0
    try:
        with tarfile.open(fileobj=reader, mode="r|") as tar:
            # Allowlist uses original names (as they arrive from remote tar)
            allowed = [_rev.get(e.rel, e.rel) for e in batch]
            # Rename map: original_name -> new_name for case-conflict files
            rename_map = {v: k for k, v in _rev.items()}  # original -> new
            extractor = _ProgressTarExtractor(tar, dst_root, progress,
                                              allowed_files=allowed,
                                              rename_map=rename_map)
            for member in tar:
                try:
                    result = extractor.extract_member(member)
                    if result is not True:
                        print(f"\n  {C.YELLOW}Skipped: {member.name}: {result}{C.RESET}")
                except (OSError, tarfile.TarError) as e:
                    print(f"\n  {C.YELLOW}Extract error: {member.name}: {e}{C.RESET}")
            extracted = extractor.extracted
            if extractor.rejected > 0:
                print(f"\n  {C.RED}WARNING: {extractor.rejected} unexpected files "
                      f"rejected from remote tar stream (possible injection){C.RESET}")
    except (OSError, tarfile.TarError) as e:
        print(f"\n  {C.RED}Tar extraction failed: {e}{C.RESET}")
    finally:
        reader.close()

    sender.join(timeout=10)
    rc = channel.recv_exit_status()
    if rc != 0:
        stderr = channel.recv_stderr(4096).decode("utf-8", errors="replace")
        print(f"\n  {C.YELLOW}Remote tar exited {rc}: {stderr[:200]}{C.RESET}")
    channel.close()
    return extracted


def copy_block_stream_remote_to_local(entries, ssh, src_root, dst_root, progress,
                                      case_renames=None):
    """Download files from remote via chunked tar streams with streaming extraction."""
    if not entries:
        return

    if not ssh.caps.get("tar"):
        print(f"  {C.YELLOW}Remote has no tar — falling back to SFTP{C.RESET}")
        copy_individual_remote_to_local(entries, ssh, dst_root, progress, 1 * 1024 * 1024,
                                        case_renames=case_renames)
        return

    safe_entries = [e for e in entries if _validate_rel_path(e.rel) is True]
    if len(safe_entries) < len(entries):
        print(f"  {C.YELLOW}Skipped {len(entries) - len(safe_entries)} entries with unsafe paths{C.RESET}")

    total_size = sum(e.size for e in safe_entries)
    batches = _batch_by_size(safe_entries)
    print(f"  Streaming {len(safe_entries)} files ({fmt_size(total_size)}) in "
          f"{len(batches)} batch{'es' if len(batches) != 1 else ''} from remote...")

    total_extracted = 0
    for i, batch in enumerate(batches):
        batch_size = sum(e.size for e in batch)
        if len(batches) > 1:
            print(f"\n  {C.DIM}Batch {i+1}/{len(batches)}: {len(batch)} files "
                  f"({fmt_size(batch_size)}){C.RESET}")
        total_extracted += _stream_tar_batch_from_remote(
            batch, ssh, src_root, dst_root, progress, case_renames=case_renames)

    print(f"\n  {C.GREEN}Extracted {total_extracted} files{C.RESET}")


def copy_hybrid_remote_to_local(entries, ssh, src_root, dst_root, progress, buf_size,
                                case_renames=None):
    """Remote-to-local: tar stream for all files (much faster than SFTP)."""
    total_size = sum(e.size for e in entries)

    if ssh.caps.get("tar"):
        print(f"  Strategy: tar stream for all {C.BOLD}{len(entries)}{C.RESET} files "
              f"({C.BOLD}{fmt_size(total_size)}{C.RESET})")
        print()
        copy_block_stream_remote_to_local(entries, ssh, src_root, dst_root, progress,
                                          case_renames=case_renames)
    else:
        # Fallback: SFTP for everything if tar not available
        small, large = split_by_size(entries)
        small_size = sum(e.size for e in small)
        large_size = sum(e.size for e in large)

        print(f"  Strategy (no remote tar — using SFTP):")
        print(f"    Small files (<1MB): {C.BOLD}{len(small)}{C.RESET} files, "
              f"{C.BOLD}{fmt_size(small_size)}{C.RESET}")
        print(f"    Large files (≥1MB): {C.BOLD}{len(large)}{C.RESET} files, "
              f"{C.BOLD}{fmt_size(large_size)}{C.RESET}")
        print()
        copy_individual_remote_to_local(entries, ssh, dst_root, progress, buf_size,
                                        case_renames=case_renames)


# ════════════════════════════════════════════════════════════════════════════
# REMOTE → REMOTE COPY — relay data through local machine
# ════════════════════════════════════════════════════════════════════════════

def copy_individual_r2r(entries, src_ssh, dst_ssh, dst_root, progress, buf_size):
    """Remote-to-remote SFTP relay for large files (src → local buf → dst)."""
    src_sftp = src_ssh.open_sftp()
    dst_sftp = dst_ssh.open_sftp()

    for entry in entries:
        remote_src_path = entry.src
        remote_dst_path = posixpath.join(dst_root, entry.rel)

        try:
            if entry.size == 0:
                with dst_sftp.open(remote_dst_path, "w"):
                    pass
                _log("copied", entry.rel, entry.size, method="sftp_relay")
                progress.update(0, 1)
                progress.display()
                continue

            with src_sftp.open(remote_src_path, "rb") as fin:
                fin.prefetch(min(entry.size, 256 * 1024 * 1024))  # cap at 256MB
                with dst_sftp.open(remote_dst_path, "wb") as fout:
                    fout.set_pipelined(True)
                    while True:
                        data = fin.read(buf_size)
                        if not data:
                            break
                        fout.write(data)
                        progress.update(len(data))
                        progress.display()

            # Preserve timestamps
            try:
                rstat = src_sftp.stat(remote_src_path)
                dst_sftp.utime(remote_dst_path, (rstat.st_atime, rstat.st_mtime))
            except (OSError, IOError):
                pass

            _log("copied", entry.rel, entry.size, method="sftp_relay")
            progress.update(0, 1)

        except (OSError, IOError) as e:
            print(f"\n  {C.RED}Error: {entry.rel}: {e}{C.RESET}")
            _log("error", entry.rel, entry.size, error=str(e))
            progress.update(entry.size, 1)


def _stream_tar_batch_r2r(batch, src_ssh, dst_ssh, src_root, dst_root, progress):
    """Relay one batch of files via tar pipe: src tar cf → local → dst tar xf."""
    import threading

    safe_entries = [e for e in batch if _validate_rel_path(e.rel) is True]
    if not safe_entries:
        return 0
    file_list = "\0".join(e.rel for e in safe_entries) + "\0"
    file_list_bytes = file_list.encode("utf-8")

    # Source: tar producer
    src_chan = src_ssh.open_channel()
    src_chan.exec_command(
        f"cd {shlex.quote(src_root)} && tar cf - --null -T -"
    )

    def _send_file_list():
        try:
            chunk_size = 65536
            for i in range(0, len(file_list_bytes), chunk_size):
                src_chan.sendall(file_list_bytes[i:i + chunk_size])
        finally:
            src_chan.shutdown_write()

    sender = threading.Thread(target=_send_file_list, daemon=True)
    sender.start()

    # Destination: tar consumer — use safe extraction flags to mitigate
    # compromised source servers injecting symlinks or path traversal.
    # GNU tar already strips leading '/' by default; --no-same-owner and
    # --no-same-permissions limit privilege escalation.
    dst_chan = dst_ssh.open_channel()
    dst_chan.exec_command(
        f"tar xf - --no-same-owner --no-same-permissions -C {shlex.quote(dst_root)}"
    )

    # Relay: src → dst (with size limit to prevent source sending infinite data)
    # Allow 3x the expected batch size for tar overhead
    expected_size = sum(e.size for e in safe_entries)
    max_relay = max(expected_size * 3, 100 * 1024 * 1024)  # at least 100 MB
    relayed = 0
    while True:
        data = src_chan.recv(1048576)
        if not data:
            break
        relayed += len(data)
        if relayed > max_relay:
            print(f"\n  {C.RED}WARNING: Source tar stream exceeded expected size "
                  f"({fmt_size(relayed)} > {fmt_size(max_relay)}) — aborting relay{C.RESET}")
            break
        dst_chan.sendall(data)

    dst_chan.shutdown_write()
    sender.join(timeout=10)

    src_rc = src_chan.recv_exit_status()
    dst_rc = dst_chan.recv_exit_status()

    if src_rc != 0:
        print(f"\n  {C.YELLOW}Source tar exited {src_rc}{C.RESET}")
    if dst_rc != 0:
        stderr = dst_chan.recv_stderr(4096).decode("utf-8", errors="replace")
        print(f"\n  {C.YELLOW}Dest tar exited {dst_rc}: {stderr[:200]}{C.RESET}")

    # Safety check: remove any symlinks the source may have injected
    # (GNU tar strips leading '/' but cannot prevent '..' or symlink members)
    if dst_ssh.caps.get("python3"):
        check_script = (
            'import os,sys,json\n'
            'dst=sys.argv[1]\n'
            'found=[]\n'
            'for r,ds,fs in os.walk(dst):\n'
            '  for f in fs:\n'
            '    p=os.path.join(r,f)\n'
            '    rel=os.path.relpath(p,dst)\n'
            '    if os.path.islink(p):\n'
            '      os.unlink(p)\n'
            '      found.append(rel)\n'
            'if found:print("REMOVED_SYMLINKS:"+json.dumps(found))\n'
        )
        out, _, _ = dst_ssh.exec_cmd(
            f"python3 -c {shlex.quote(check_script)} {shlex.quote(dst_root)}",
            timeout=60
        )
        if "REMOVED_SYMLINKS:" in out:
            removed = out.split("REMOVED_SYMLINKS:", 1)[1].strip()
            print(f"\n  {C.RED}WARNING: Removed symlinks injected by source: {removed}{C.RESET}")
    else:
        # Fallback: use find -type l to remove symlinks (works without python3)
        out, _, rc = dst_ssh.exec_cmd(
            f"find {shlex.quote(dst_root)} -type l -print -delete 2>/dev/null",
            timeout=60
        )
        if out.strip():
            removed = [l.strip() for l in out.strip().split("\n") if l.strip()]
            print(f"\n  {C.RED}WARNING: Removed {len(removed)} symlinks injected by "
                  f"source:{C.RESET}")
            for s in removed[:10]:
                print(f"    {C.RED}{s}{C.RESET}")
            if len(removed) > 10:
                print(f"    ... and {len(removed) - 10} more")

    batch_size = sum(e.size for e in safe_entries)
    for e in safe_entries:
        _log("copied", e.rel, e.size, method="tar_relay")
    progress.update(batch_size, len(safe_entries))
    progress.display()

    src_chan.close()
    dst_chan.close()
    return relayed


def copy_block_stream_r2r(entries, src_ssh, dst_ssh, src_root, dst_root, progress):
    """Remote-to-remote tar pipe relay in chunked batches."""
    if not entries:
        return

    has_src_tar = src_ssh.caps.get("tar")
    has_dst_tar = dst_ssh.caps.get("tar")

    if not has_src_tar or not has_dst_tar:
        print(f"  {C.YELLOW}Tar not available on both ends — falling back to SFTP relay{C.RESET}")
        copy_individual_r2r(entries, src_ssh, dst_ssh, dst_root, progress, 1 * 1024 * 1024)
        return

    total_size = sum(e.size for e in entries)
    batches = _batch_by_size(entries)
    print(f"  Piping {len(entries)} files ({fmt_size(total_size)}) in "
          f"{len(batches)} batch{'es' if len(batches) != 1 else ''} via tar relay...")

    total_relayed = 0
    for i, batch in enumerate(batches):
        batch_size = sum(e.size for e in batch)
        if len(batches) > 1:
            print(f"\n  {C.DIM}Batch {i+1}/{len(batches)}: {len(batch)} files "
                  f"({fmt_size(batch_size)}){C.RESET}")
        total_relayed += _stream_tar_batch_r2r(
            batch, src_ssh, dst_ssh, src_root, dst_root, progress)

    print(f"\n  {C.GREEN}Tar relay: {fmt_size(total_relayed)} piped ({len(entries)} files){C.RESET}")


def copy_hybrid_r2r(entries, src_ssh, dst_ssh, src_root, dst_root, progress, buf_size):
    """Remote-to-remote: tar pipe relay for all files (much faster than SFTP)."""
    total_size = sum(e.size for e in entries)

    # Create all directories on dest first
    ensure_remote_dirs(dst_ssh, dst_root, entries)

    has_tar = src_ssh.caps.get("tar") and dst_ssh.caps.get("tar")
    if has_tar:
        print(f"  Strategy: tar pipe relay for all {C.BOLD}{len(entries)}{C.RESET} files "
              f"({C.BOLD}{fmt_size(total_size)}{C.RESET})")
        print()
        copy_block_stream_r2r(entries, src_ssh, dst_ssh, src_root, dst_root, progress)
    else:
        # Fallback: SFTP relay for everything
        print(f"  Strategy (tar not available — using SFTP relay):")
        print(f"    {C.BOLD}{len(entries)}{C.RESET} files, "
              f"{C.BOLD}{fmt_size(total_size)}{C.RESET}")
        print()
        copy_individual_r2r(entries, src_ssh, dst_ssh, dst_root, progress, buf_size)


def filter_unchanged_remote_to_local(entries, link_map, src_ssh, src_root, dst_root, threads=DEFAULT_THREADS):
    """
    Incremental check for remote→local: compare remote source files
    against existing local destination files.
    Returns (to_copy, to_link, skipped_count, skipped_bytes).
    """
    print(f"  {C.DIM}Checking destination for existing files...{C.RESET}", end="", flush=True)

    need_copy = []
    need_hash = []
    skipped = []
    skipped_bytes = 0

    for entry in entries:
        dst_path = os.path.join(dst_root, entry.rel)

        if not os.path.exists(dst_path):
            need_copy.append(entry)
            continue

        try:
            dst_size = os.path.getsize(dst_path)
        except OSError:
            need_copy.append(entry)
            continue

        if dst_size != entry.size:
            need_copy.append(entry)
        else:
            need_hash.append(entry)

    print(f"\r  {C.DIM}Quick check: {len(need_copy)} new/changed, "
          f"{len(need_hash)} same-size need hash check{C.RESET}          ")

    if not need_hash:
        return need_copy, link_map, 0, 0

    # Hash dest files locally and source files on remote
    print(f"  {C.DIM}Hashing {len(need_hash)} files to check for changes...{C.RESET}", end="", flush=True)

    # Hash local dest files
    dst_hashes = [None] * len(need_hash)

    def hash_dst(idx):
        entry = need_hash[idx]
        dst_path = os.path.join(dst_root, entry.rel)
        dst_hashes[idx] = hash_file_sha256(dst_path)

    with ThreadPoolExecutor(max_workers=threads) as pool:
        futures = [pool.submit(hash_dst, i) for i in range(len(need_hash))]
        for f in as_completed(futures):
            f.result()

    # Hash remote source files
    remote_h = remote_hash_files(src_ssh, src_root, [e.rel for e in need_hash])

    for i, entry in enumerate(need_hash):
        rh = remote_h.get(entry.rel)
        dh = dst_hashes[i]
        if rh and dh and rh == dh:
            _log("skipped", entry.rel, entry.size, reason="unchanged")
            skipped.append(entry)
            skipped_bytes += entry.size
        else:
            need_copy.append(entry)

    # Filter link_map
    new_link_map = {}
    skipped_links = 0
    for dup_rel, canonical_rel in link_map.items():
        dst_path = os.path.join(dst_root, dup_rel)
        if os.path.exists(dst_path):
            _log("skipped", dup_rel, 0, reason="link_exists")
            skipped_links += 1
        else:
            new_link_map[dup_rel] = canonical_rel

    print(f"\r  {C.GREEN}Incremental check complete:{C.RESET}                              ")
    print(f"    To copy:   {C.BOLD}{len(need_copy)}{C.RESET} files "
          f"({fmt_size(sum(e.size for e in need_copy))})")
    print(f"    Skipped:   {C.BOLD}{len(skipped)}{C.RESET} files unchanged "
          f"({C.GREEN}{fmt_size(skipped_bytes)}{C.RESET})")
    if skipped_links:
        print(f"    Links:     {C.BOLD}{skipped_links}{C.RESET} already exist, "
              f"{C.BOLD}{len(new_link_map)}{C.RESET} to create")

    return need_copy, new_link_map, len(skipped) + skipped_links, skipped_bytes


# ════════════════════════════════════════════════════════════════════════════
# SELF-UPDATE — check for new releases and replace the running binary/script
# ════════════════════════════════════════════════════════════════════════════
def _is_frozen():
    """True if running as a PyInstaller binary."""
    return getattr(sys, 'frozen', False)


def _get_self_path():
    """Get the path of the currently running script or binary."""
    if _is_frozen():
        return os.path.realpath(sys.executable)
    return os.path.realpath(__file__)


def _get_asset_name():
    """Determine which release asset to download for this platform."""
    if _is_frozen():
        if _system == "Linux":
            return "fast_copy-linux"
        elif _system == "Darwin":
            machine = platform.machine().lower()
            if machine in ("x86_64", "i386"):
                return "fast_copy-macos-intel"
            return "fast_copy-macos-arm64"
        elif _system == "Windows":
            return "fast_copy-windows.exe"
    return "fast_copy.py"


def _parse_version(tag):
    """Parse 'v2.4.0' → (2, 4, 0). Returns None on failure."""
    tag = tag.lstrip("vV")
    try:
        return tuple(int(x) for x in tag.split("."))
    except (ValueError, AttributeError):
        return None


def _get_ssl_context():
    """Return an SSL context that works on macOS bundled binaries.

    PyInstaller bundles on macOS often can't find the system certificate store,
    causing CERTIFICATE_VERIFY_FAILED errors.  We try several approaches:
    1. certifi (if bundled or installed)
    2. System cert file at common macOS/Linux paths
    3. Unverified context as last resort (with warning)
    """
    import ssl
    # Try default context first — works on most systems
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
        return ctx
    except (ImportError, OSError):
        pass
    # Try common system cert paths (macOS Homebrew, Linux)
    for cert_path in [
        "/etc/ssl/certs/ca-certificates.crt",      # Debian/Ubuntu
        "/etc/pki/tls/certs/ca-bundle.crt",         # RHEL/CentOS
        "/etc/ssl/cert.pem",                         # macOS / BSD
        "/usr/local/etc/openssl/cert.pem",           # Homebrew openssl
        "/usr/local/etc/openssl@3/cert.pem",         # Homebrew openssl@3
    ]:
        if os.path.exists(cert_path):
            try:
                ctx.load_verify_locations(cert_path)
                return ctx
            except OSError:
                continue
    # Last resort: try the default context as-is (may work if the system
    # cert store is accessible via the default mechanism)
    return ctx


def _fetch_releases():
    """Fetch all releases from GitHub. Returns list of release dicts or None."""
    import urllib.request
    import urllib.error
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/releases"
    req = urllib.request.Request(api_url, headers={
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": f"fast-copy/{__version__}",
    })
    ssl_ctx = _get_ssl_context()
    try:
        with urllib.request.urlopen(req, timeout=15, context=ssl_ctx) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        print(f"  {C.RED}Failed to check for updates: {e}{C.RESET}")
        return None


def _classify_release_sections(body):
    """Parse a release body into categorized sections.

    Returns dict with keys like 'security', 'bug_fixes', 'new_features',
    'performance', 'improvements', etc.  Each value is a list of bullet lines.
    """
    sections = {}
    current_key = None
    _SECTION_MAP = {
        "security fixes": "security",
        "security":       "security",
        "bug fixes":      "bug_fixes",
        "new features":   "new_features",
        "performance":    "performance",
        "improvements":   "improvements",
        "windows":        "improvements",
        "reliability":    "improvements",
    }
    for line in (body or "").splitlines():
        stripped = line.strip()
        # Detect markdown headers like ### Security Fixes
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip().lower()
            current_key = _SECTION_MAP.get(heading)
            if current_key and current_key not in sections:
                sections[current_key] = []
        elif current_key and stripped.startswith("-"):
            sections[current_key].append(stripped)
    return sections


def _print_release_notes(releases, current_ver):
    """Print categorized release notes for all versions newer than current_ver."""
    has_security = False
    has_features = False
    for rel in releases:
        tag = rel.get("tag_name", "")
        ver = _parse_version(tag)
        if not ver or ver <= current_ver:
            continue
        body = rel.get("body", "")
        sections = _classify_release_sections(body)
        published = rel.get("published_at", "")[:10]

        print(f"\n  {C.BOLD}{tag}{C.RESET}" +
              (f" {C.DIM}({published}){C.RESET}" if published else ""))

        _LABELS = {
            "security":     (C.RED,    "Security Fixes"),
            "bug_fixes":    (C.YELLOW, "Bug Fixes"),
            "new_features": (C.GREEN,  "New Features"),
            "performance":  (C.CYAN,   "Performance"),
            "improvements": (C.DIM,    "Improvements"),
        }
        for key, (color, label) in _LABELS.items():
            if key in sections:
                if key == "security":
                    has_security = True
                if key == "new_features":
                    has_features = True
                print(f"    {color}{label}:{C.RESET}")
                for bullet in sections[key]:
                    print(f"      {bullet}")

        if not sections:
            # No recognized sections — print raw body (truncated)
            for line in (body or "No release notes.").splitlines()[:15]:
                print(f"    {C.DIM}{line}{C.RESET}")

    return has_security, has_features


def check_for_update():
    """Check GitHub for a newer release.

    Returns (latest_tag, asset_url, asset_size, releases_between) or None.
    releases_between is a list of release dicts newer than the current version.
    """
    releases = _fetch_releases()
    if releases is None:
        return None

    current_ver = _parse_version(__version__)
    if not current_ver:
        print(f"  {C.YELLOW}Could not parse current version: {__version__}{C.RESET}")
        return None

    # Find all releases newer than current, sorted newest first
    newer = []
    for rel in releases:
        tag = rel.get("tag_name", "")
        ver = _parse_version(tag)
        if ver and ver > current_ver:
            newer.append(rel)
    newer.sort(key=lambda r: _parse_version(r["tag_name"]), reverse=True)

    if not newer:
        print(f"  {C.GREEN}Already up to date (v{__version__}){C.RESET}")
        return None

    latest = newer[0]
    latest_tag = latest["tag_name"]

    # Find the right asset for this platform
    asset_name = _get_asset_name()
    for asset in latest.get("assets", []):
        if asset["name"] == asset_name:
            return latest_tag, asset["browser_download_url"], asset["size"], newer

    print(f"  {C.RED}No asset '{asset_name}' found in release {latest_tag}{C.RESET}")
    return None


def _find_release_asset(releases, target_tag):
    """Find download asset for a specific release tag.

    Returns (tag, asset_url, asset_size) or None.
    """
    asset_name = _get_asset_name()
    target_tag_norm = target_tag.lstrip("vV")
    for rel in releases:
        tag = rel.get("tag_name", "")
        if tag.lstrip("vV") == target_tag_norm:
            for asset in rel.get("assets", []):
                if asset["name"] == asset_name:
                    return tag, asset["browser_download_url"], asset["size"]
            print(f"  {C.RED}No asset '{asset_name}' found in release {tag}{C.RESET}")
            return None
    print(f"  {C.RED}Release '{target_tag}' not found on GitHub{C.RESET}")
    available = [r["tag_name"] for r in releases[:10]]
    print(f"  {C.DIM}Available: {', '.join(available)}{C.RESET}")
    return None


def check_update_info():
    """--check-update: show what's new without installing."""
    print(f"\n  {C.BOLD}fast-copy update check{C.RESET}")
    print(f"  Current version: {C.BOLD}v{__version__}{C.RESET}")
    print(f"  Checking GitHub for updates...\n")

    result = check_for_update()
    if result is None:
        return

    latest_tag, download_url, expected_size, newer = result
    current_ver = _parse_version(__version__)

    print(f"  {C.GREEN}New version available: {C.BOLD}{latest_tag}{C.RESET}")
    print(f"  {C.DIM}(you have v{__version__} — "
          f"{len(newer)} release{'s' if len(newer) != 1 else ''} behind){C.RESET}")

    _print_release_notes(newer, current_ver)

    # List available versions
    tags = [r["tag_name"] for r in newer]
    print(f"\n  {C.BOLD}To update:{C.RESET}")
    print(f"    --update             Install latest ({latest_tag})")
    if len(newer) > 1:
        print(f"    --update VERSION     Install a specific version")
        print(f"    {C.DIM}Available: {', '.join(tags)}{C.RESET}")
    print()


# ── dependency check ─────────────────────────────────────────────────────────
# (distribution, import name, pip install spec, what it enables). The core
# local↔local copy is stdlib-only; everything here is feature-gated and imported
# lazily, so a plain copy never needs any of it.
_DEPENDENCIES = [
    ("paramiko", "paramiko", "paramiko",
     "SSH / SFTP transfers — user@host:path, remote↔remote"),
    ("cryptography", "cryptography", "cryptography>=41.0",
     "Encrypted credentials file (creds encrypt)"),
    ("boto3", "boto3", "boto3>=1.28",
     "Amazon S3 and S3-compatible: MinIO / R2 / Wasabi / B2 (s3://)"),
    ("azure-storage-blob", "azure.storage.blob", "azure-storage-blob>=12.0",
     "Azure Blob Storage (az://)"),
    ("google-cloud-storage", "google.cloud.storage", "google-cloud-storage>=2.0",
     "Google Cloud Storage (gs://)"),
    ("xxhash", "xxhash", "xxhash",
     "Faster hashing (xxh128; falls back to SHA-256 if absent)"),
]


def _dep_status(dist, import_name):
    """Return (installed, version_or_None) for one dependency. Prefers dist
    metadata (gives a version); falls back to an import-spec check so a frozen
    build — which bundles modules without dist-info — still reads as present."""
    try:
        import importlib.metadata as _md
        return True, _md.version(dist)
    except Exception:
        pass
    try:
        import importlib.util
        if importlib.util.find_spec(import_name) is not None:
            return True, None
    except Exception:
        pass
    return False, None


def _pip_install_specs(specs):
    """Install pip specs into the running interpreter's environment. Returns an
    exit code (0 on success)."""
    import subprocess
    cmd = [sys.executable, "-m", "pip", "install", *specs]
    print(f"  {C.DIM}Running: {' '.join(cmd)}{C.RESET}\n")
    try:
        rc = subprocess.run(cmd).returncode
    except KeyboardInterrupt:
        print(f"\n  {C.YELLOW}Interrupted.{C.RESET}")
        return 130
    except Exception as e:
        print(f"  {C.RED}Error: could not run pip: {e}{C.RESET}")
        return 1
    print(f"\n  {C.GREEN}✓ Dependencies installed.{C.RESET}" if rc == 0
          else f"\n  {C.RED}pip exited with code {rc}.{C.RESET}")
    return rc


def check_dependencies(install=False):
    """`fast_copy.py deps [--install]` — report which optional packages are
    installed and what each enables; with --install, pip-install the missing
    ones. Returns an exit code."""
    banner("DEPENDENCIES")
    missing = []
    for dist, import_name, spec, feature in _DEPENDENCIES:
        ok, ver = _dep_status(dist, import_name)
        if ok:
            v = f" {ver}" if ver else ""
            print(f"  {C.GREEN}✓{C.RESET} {C.BOLD}{dist}{C.RESET}"
                  f"{C.DIM}{v}{C.RESET}  — {feature}")
        else:
            missing.append((dist, spec, feature))
            print(f"  {C.YELLOW}✗ {dist}{C.RESET} {C.DIM}(missing){C.RESET}"
                  f"  — {feature}")
    print()
    if not missing:
        print(f"  {C.GREEN}All optional dependencies are installed.{C.RESET}")
        return 0
    print(f"  {C.YELLOW}{len(missing)} missing{C.RESET} "
          f"{C.DIM}(each is only needed for the feature shown above).{C.RESET}")
    if _is_frozen():
        print(f"  {C.DIM}This is a frozen build — dependencies are bundled into "
              f"the executable, so pip install does not apply.{C.RESET}")
        return 0
    specs = [s for _, s, _ in missing]
    if not install:
        print(f"\n  Install the missing ones with:")
        print(f"    {C.BOLD}{os.path.basename(sys.executable)} -m pip install "
              f"{' '.join(specs)}{C.RESET}")
        print(f"  {C.DIM}or: fast_copy.py deps --install{C.RESET}")
        return 0
    print()
    return _pip_install_specs(specs)


def _post_update_dep_check():
    """After a successful self-update, surface any missing optional deps and, on
    an interactive terminal, offer to install them. Silent when nothing is
    missing or on a frozen build (deps are bundled)."""
    if _is_frozen():
        return
    missing = [(d, s, f) for d, imp, s, f in _DEPENDENCIES
               if not _dep_status(d, imp)[0]]
    if not missing:
        return
    print(f"\n  {C.YELLOW}{len(missing)} optional dependency(ies) not "
          f"installed:{C.RESET}")
    for d, s, f in missing:
        print(f"    {C.DIM}• {d} — {f}{C.RESET}")
    if sys.stdin.isatty():
        try:
            ans = input("  Install them now with pip? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans in ("y", "yes"):
            _pip_install_specs([s for _, s, _ in missing])
            return
    print(f"  {C.DIM}Install later with: fast_copy.py deps --install{C.RESET}")


def self_update(target_version=None, expected_sha256=None):
    """Download and install a release. If target_version is None, install latest.

    expected_sha256: optional hex SHA-256 string. If provided, the downloaded
    binary's SHA-256 must match exactly or the update is refused. Use this to
    verify the release against a hash obtained out-of-band (release page, etc.)
    — defends against a compromised release publisher pushing a trojan that
    would later run as root via --use-sudo."""
    import urllib.request
    import urllib.error

    print(f"\n  {C.BOLD}fast-copy self-update{C.RESET}")
    print(f"  Current version: {C.BOLD}v{__version__}{C.RESET}")
    print(f"  Checking GitHub for updates...\n")

    result = check_for_update()
    if result is None:
        return

    latest_tag, download_url, expected_size, newer = result
    current_ver = _parse_version(__version__)

    # If a specific version was requested, find that release instead
    if target_version:
        target_ver = _parse_version(target_version)
        if target_ver and target_ver <= current_ver:
            print(f"  {C.YELLOW}{target_version} is not newer than current "
                  f"v{__version__}{C.RESET}")
            return
        specific = _find_release_asset(newer, target_version)
        if specific is None:
            return
        latest_tag, download_url, expected_size = specific
        # Only show notes up to the target version
        target_ver = _parse_version(latest_tag)
        notes_releases = [r for r in newer
                          if _parse_version(r["tag_name"]) <= target_ver]
    else:
        notes_releases = newer

    # Show what's included
    _print_release_notes(notes_releases, current_ver)

    self_path = _get_self_path()
    asset_name = _get_asset_name()

    print(f"\n  {C.GREEN}Updating to: {C.BOLD}{latest_tag}{C.RESET}")
    print(f"  Asset:   {asset_name} ({fmt_size(expected_size)})")
    print(f"  Target:  {self_path}")

    # Check we can write to the target location
    target_dir = os.path.dirname(self_path)
    if not os.access(target_dir, os.W_OK):
        print(f"\n  {C.RED}Error: No write permission to {target_dir}{C.RESET}")
        print(f"  {C.YELLOW}Try running with sudo or as administrator{C.RESET}")
        sys.exit(1)

    # Download to a temporary file in the same directory (ensures same filesystem)
    tmp_path = self_path + ".update_tmp"
    try:
        # Validate download URL comes from expected GitHub domains
        from urllib.parse import urlparse
        parsed = urlparse(download_url)
        _ALLOWED_HOSTS = {"github.com", "objects.githubusercontent.com",
                          "github-releases.githubusercontent.com"}
        if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_HOSTS:
            print(f"\n  {C.RED}Error: Unexpected download URL: {download_url}{C.RESET}")
            print(f"  {C.RED}Expected HTTPS from GitHub domains{C.RESET}")
            sys.exit(1)

        print(f"\n  Downloading...", end="", flush=True)
        req = urllib.request.Request(download_url, headers={
            "User-Agent": f"fast-copy/{__version__}",
        })
        ssl_ctx = _get_ssl_context()
        # Ensure SSL certificate verification is active
        import ssl as _ssl_mod
        if ssl_ctx.verify_mode != _ssl_mod.CERT_REQUIRED:
            print(f"\n  {C.RED}Error: SSL certificate verification is disabled — "
                  f"refusing to download update{C.RESET}")
            sys.exit(1)
        with urllib.request.urlopen(req, timeout=120, context=ssl_ctx) as resp:
            data = resp.read()

        # Verify download size matches expected
        if len(data) != expected_size:
            print(f"\n  {C.RED}Error: Size mismatch — expected {expected_size}, "
                  f"got {len(data)}{C.RESET}")
            sys.exit(1)

        # Verify it's not empty or suspiciously small
        if len(data) < 1024:
            print(f"\n  {C.RED}Error: Downloaded file is suspiciously small "
                  f"({len(data)} bytes){C.RESET}")
            sys.exit(1)

        # Write to temp file
        with open(tmp_path, "wb") as f:
            f.write(data)

        print(f" {C.GREEN}{fmt_size(len(data))} downloaded{C.RESET}")

        # Compute SHA-256 of download. If the caller supplied a pinned hash
        # (out-of-band — typically from the GitHub release page), verify it.
        # Otherwise the hash is printed only for an audit trail.
        dl_hash = hashlib.sha256(data).hexdigest()
        print(f"  SHA-256: {C.DIM}{dl_hash}{C.RESET}")
        if expected_sha256:
            want = expected_sha256.strip().lower()
            if dl_hash.lower() != want:
                print(f"\n  {C.RED}Error: SHA-256 mismatch — refusing update.{C.RESET}")
                print(f"  {C.RED}expected: {want}{C.RESET}")
                print(f"  {C.RED}got:      {dl_hash}{C.RESET}")
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
                sys.exit(1)
            print(f"  {C.GREEN}SHA-256 matches pinned value.{C.RESET}")

    except (urllib.error.URLError, OSError) as e:
        print(f"\n  {C.RED}Download failed: {e}{C.RESET}")
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        sys.exit(1)

    # ── Platform-specific replacement ────────────────────────────────
    try:
        if _system == "Windows":
            # Windows: running .exe is locked — rename-swap strategy
            old_path = self_path + ".old"
            # Clean up leftover from previous update
            try:
                os.remove(old_path)
            except OSError:
                pass
            # Rename current → .old (allowed while running on Windows)
            os.rename(self_path, old_path)
            # Rename new → current
            os.rename(tmp_path, self_path)
            print(f"\n  {C.GREEN}Updated to {latest_tag}{C.RESET}")
            print(f"  {C.DIM}Old version saved as {old_path} (will be cleaned up next run){C.RESET}")
        else:
            # Linux/macOS: atomic replace via os.replace
            # Preserve original file permissions
            try:
                old_mode = os.stat(self_path).st_mode
            except OSError:
                old_mode = None

            os.replace(tmp_path, self_path)

            # Restore permissions (make binary executable)
            if old_mode:
                os.chmod(self_path, old_mode)
            elif _is_frozen():
                os.chmod(self_path, 0o755)

            print(f"\n  {C.GREEN}Updated to {latest_tag}{C.RESET}")

    except OSError as e:
        print(f"\n  {C.RED}Failed to replace binary: {e}{C.RESET}")
        # Try to clean up
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        sys.exit(1)

    print(f"  Run 'fast_copy --version' to verify.\n")
    sys.exit(0)


# ════════════════════════════════════════════════════════════════════════════
# OBJECT STORAGE BACKENDS (v4.0.0) — S3, Azure Blob, Google Cloud Storage
#
# Cloud endpoints (s3://, az://, gs://) become first-class sources and
# destinations. Each provider implements the same small CloudBackend interface;
# one orchestrator (run_cloud_transfer) drives upload / download / cloud-to-cloud
# and reuses the existing scan, hash, Progress, and verify machinery. SDKs
# (boto3 / azure-storage-blob / google-cloud-storage) are imported lazily so a
# plain local copy never pays for them.
# ════════════════════════════════════════════════════════════════════════════
CLOUD_MANIFEST_NAME = ".fast_copy_manifest.json"
CloudSpec = namedtuple("CloudSpec", ["scheme", "container", "prefix", "connection"],
                       defaults=[None])


def parse_cloud_url(path_str):
    """Parse s3://bucket/key, az://container/blob, gs://bucket/object — with an
    optional named connection: s3://conn@bucket/key (bucket names can't contain
    '@', so this is unambiguous). Returns CloudSpec or None for non-cloud paths."""
    if not path_str:
        return None
    for scheme in ("s3", "az", "gs"):
        marker = scheme + "://"
        if path_str.startswith(marker):
            rest = path_str[len(marker):]
            container, _, prefix = rest.partition("/")
            connection = None
            if "@" in container:
                connection, container = container.split("@", 1)
            if not container:
                raise SystemExit(f"Error: malformed {scheme}:// URL "
                                 f"(missing bucket/container): {path_str!r}")
            return CloudSpec(scheme, container, prefix, connection)
    return None


def is_cloud_path(path_str):
    return parse_cloud_url(path_str) is not None


def _looks_like_profile_ref(token):
    """Cheap test (no credentials access) for whether a token could name a saved
    connection: a bare name like `aws-dev` / `aws-dev/`, or `name:subpath`.
    Cloud URLs and SSH user@host:path are never candidates. A bare name still
    qualifies even if a same-named *directory* exists (an explicit connection
    name wins over a coincidental — or auto-created — folder); an existing
    *file* does not. Used to decide whether to open/unlock the creds file."""
    if not token or parse_cloud_url(token):
        return False
    t = token.rstrip("/").rstrip(os.sep)
    if not t or t in (".", ".."):
        return False
    if ":" in t:
        head = t.split(":", 1)[0]
        return bool(head) and "@" not in head and len(head) > 1 \
            and "/" not in head and "\\" not in head
    if "/" in t or "\\" in t:
        return False
    return not os.path.isfile(token)  # a real file is local; a dir may be a conn


def _conns_for_named_endpoints(args=None):
    """Load saved connections for endpoint-name resolution. Honors an explicit
    --credentials-file, else the default path. Only the caller (after a
    _looks_like_profile_ref check) decides to call this, so prompting to unlock
    an encrypted file in a terminal is justified. Unattended + locked → {}, so a
    scripted plain copy never hangs."""
    path = (getattr(args, "credentials_file", None) if args else None) \
        or default_credentials_path()
    if not os.path.isfile(path):
        return {}
    if _file_is_encrypted(path) and not _have_creds_passphrase() \
            and not sys.stdin.isatty():
        return {}
    # In a terminal this prompts for the passphrase; a wrong one surfaces a clean
    # single-line error rather than silently copying to the wrong place.
    return load_credentials_file(path)


def resolve_named_endpoint(token, conns):
    """Expand an endpoint that names a saved connection. Accepts a bare name
    (`azure-prod`) or `name:subpath` (`ssh1:/tmp`, `aws-dev:bucket/key`).

    Returns (new_token, ssh_overrides):
      • cloud connection → ("s3://name@bucket/key", None)
      • ssh connection   → ("user@host:/path", {port, key, password})
      • not a profile    → (None, None)  (leave the token untouched)
    """
    if not token or parse_cloud_url(token):
        return None, None
    name, sub = token, None
    if ":" in token:
        head, _, tail = token.partition(":")
        # Leave SSH user@host:path, Windows drives (C:\) and ./rel:foo alone.
        if "@" in head or len(head) <= 1 or "/" in head or "\\" in head:
            return None, None
        name, sub = head, tail
    else:
        # An existing *file* shadows a same-named profile (clearly a local
        # target). A *directory* does not — an explicit connection name wins
        # over a coincidental or auto-created folder; use ./name for a local
        # dir. Strip any trailing slash so `aws-dev/` matches the connection key.
        if os.path.isfile(token):
            return None, None
        name = token.rstrip("/").rstrip(os.sep)
    conn = conns.get(name)
    if not conn:
        return None, None
    ctype = conn.get("type")
    if ctype in ("s3", "az", "gs"):
        container = conn.get("container")
        if container:
            # Default bucket is set → `name:subfolder` is a folder/prefix INSIDE
            # that bucket (on top of any default prefix). So with bucket=fastcopy:
            #   gcs            -> gs://gcs@fastcopy
            #   gcs:backup     -> gs://gcs@fastcopy/backup
            #   gcs:prod/2024  -> gs://gcs@fastcopy/prod/2024
            parts = [container]
            if conn.get("prefix"):
                parts.append(conn["prefix"].strip("/"))
            if sub:
                parts.append(sub.strip("/"))
            loc = "/".join(p for p in parts if p)
        elif sub:
            # No default bucket → the subpath names the bucket[/key] itself.
            loc = sub
        else:
            raise SystemExit(
                f"Error: connection {name!r} has no default bucket/container. "
                f"Use {ctype}://{name}@<bucket>/<key>, the {name}:<bucket>/<key> "
                f"shorthand, or set a default bucket with: "
                f"fast_copy.py creds edit {name}")
        return f"{ctype}://{name}@{loc}", None
    if ctype == "ssh":
        path = sub if sub is not None else conn.get("path")
        if not path:
            raise SystemExit(f"Error: SSH connection {name!r} needs a path, "
                             f"e.g. {name}:/remote/dir")
        if not conn.get("host"):
            raise SystemExit(f"Error: SSH connection {name!r} has no host.")
        user = conn.get("user") or getpass.getuser()
        new = f"{user}@{conn['host']}:{path}"
        overrides = {"port": int(conn.get("port", 22)), "key": conn.get("key"),
                     "password": conn.get("password")}
        return new, overrides
    return None, None


def apply_named_endpoints(args):
    """Rewrite args.source/args.destination when they name a saved connection,
    routing any SSH credentials from the profile into the existing arg fields."""
    # Only touch the credentials file when an endpoint actually looks like a
    # profile name — keeps plain local/SSH copies prompt-free.
    if not (_looks_like_profile_ref(args.source)
            or _looks_like_profile_ref(args.destination)):
        return
    conns = _conns_for_named_endpoints(args)
    if not conns:
        return
    new_src, src_over = resolve_named_endpoint(args.source, conns)
    if new_src:
        args.source = new_src
        if src_over:
            if args.src_port == 22 and src_over["port"]:
                args.src_port = src_over["port"]
            if not args.src_key and src_over["key"]:
                args.src_key = src_over["key"]
            if src_over["password"]:
                args._resolved_src_password = src_over["password"]
    new_dst, dst_over = resolve_named_endpoint(args.destination, conns)
    if new_dst:
        args.destination = new_dst
        if dst_over:
            if args.ssh_port == 22 and dst_over["port"]:
                args.ssh_port = dst_over["port"]
            if not args.ssh_key and dst_over["key"]:
                args.ssh_key = dst_over["key"]
            if dst_over["password"]:
                args._resolved_dst_password = dst_over["password"]


# ── Credentials file (named connections) ─────────────────────────────────────
def default_credentials_path():
    """Where the named-connection credentials file lives.

    Resolution order:
      1. $FAST_COPY_CREDENTIALS — explicit override (or --credentials-file).
      2. An EXISTING credentials.json beside fast_copy.py — back-compat: older
         versions always stored it there, so keep using it and never "lose"
         creds on upgrade.
      3. Otherwise, a NEW file goes to a per-user private location.

    Why (3) differs by OS: on POSIX the script may sit in a shared, world-
    readable dir (/usr/local/bin, an NFS mount, a repo checkout), which would
    leave credentials.json world-readable unless manually chmod'd — so new files
    default to ~/.config/fast_copy/ (honoring $XDG_CONFIG_HOME). On Windows we
    stay beside the script on purpose: %APPDATA% is silently virtualized by the
    Microsoft-Store Python sandbox, which hides where the file actually went."""
    override = os.environ.get("FAST_COPY_CREDENTIALS")
    if override:
        return override
    try:
        script_dir = os.path.dirname(_get_self_path())
    except Exception:
        script_dir = os.path.dirname(os.path.abspath(__file__))
    legacy = os.path.join(script_dir, "credentials.json")
    if os.path.isfile(legacy) or _system == "Windows":
        return legacy
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    return os.path.join(xdg, "fast_copy", "credentials.json")


def _perms_note(path):
    """How the file is protected, phrased per-platform (the old '0600' message
    was misleading on Windows, where POSIX modes don't apply)."""
    if _system == "Windows":
        return "hidden — view with: type \"%s\"  (attrib -h to unhide)" % path
    return "0600"


# ── Encryption-at-rest for the credentials file ──────────────────────────────
# Confidentiality comes from a PASSPHRASE (env FAST_COPY_CREDS_PASSPHRASE or a
# hidden prompt) → AES-256-GCM with an scrypt-derived key. The SHA-256 of this
# fast_copy.py is bound in as the AEAD's associated data: a swapped/edited binary
# is *detected* (tamper-evidence), but because the key is the passphrase — not
# the hash — a legitimate update never locks you out; `creds rekey` re-binds.
CREDS_MAGIC = "FC-CREDS-ENC-v1"
# scrypt cost for the at-rest credentials key. N=2^17 follows OWASP guidance for
# protecting *stored* secrets (the old 2^14 is only the interactive-login floor).
# The chosen N/r/p are persisted in each envelope, so files written with any
# prior cost still decrypt with their own parameters — only newly written files
# use the stronger setting.
_SCRYPT = {"n": 1 << 17, "r": 8, "p": 1}


def _scrypt_maxmem(n, r):
    # OpenSSL's default cap is 32 MB; scrypt needs ~128*n*r bytes. Give headroom.
    return 128 * n * r * 2 + (1 << 20)


def _self_hash():
    """SHA-256 of the running fast_copy.py (best-effort; '' if unreadable)."""
    try:
        path = _get_self_path() if "_get_self_path" in globals() else os.path.abspath(__file__)
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return ""


# Remembers the passphrase entered during a single run so one command that both
# decrypts (to load) and re-encrypts (to save) only prompts once. Process-scoped
# and in-memory only — never persisted. Held as a bytearray (not bytes) so it can
# be overwritten on exit; see _zero_passphrase_cache.
_creds_passphrase_cache = None


def _zero_passphrase_cache():
    """Best-effort wipe of the cached passphrase. CPython can't guarantee no
    other copy lingers (str interning, allocator reuse), but overwriting the one
    long-lived buffer we hold removes the obvious recoverable copy from a later
    core dump or heap inspection."""
    global _creds_passphrase_cache
    buf = _creds_passphrase_cache
    if isinstance(buf, bytearray):
        for i in range(len(buf)):
            buf[i] = 0
    _creds_passphrase_cache = None


atexit.register(_zero_passphrase_cache)


def _scrub_passphrase_env():
    """Move FAST_COPY_CREDS_PASSPHRASE out of os.environ into the in-process
    cache, once, early. Every subprocess we spawn (chattr/setfacl/getfacl and
    the inline ACL helpers) inherits os.environ, and none of them need the
    passphrase — so removing it here stops the secret from fanning out into
    their environments, where any same-UID process could read it via
    /proc/<pid>/environ. Idempotent; safe to call more than once.

    Caveat: this stops *inheritance* by children. It does not erase the value
    from THIS process's /proc/self/environ — the kernel snapshots that at exec
    and unsetenv() does not rewrite it. Closing that requires the caller not to
    export the secret in the first place (use the interactive prompt)."""
    global _creds_passphrase_cache
    pw = os.environ.pop("FAST_COPY_CREDS_PASSPHRASE", None)
    if pw and _creds_passphrase_cache is None:
        _creds_passphrase_cache = bytearray(pw.encode("utf-8"))


def _have_creds_passphrase():
    """True when a passphrase is available without prompting — from the env var
    (captured into the cache by _scrub_passphrase_env) or a prior prompt this
    run. Use this instead of reading FAST_COPY_CREDS_PASSPHRASE directly, since
    that env var is scrubbed at startup. Also tolerates the env var still being
    present, in case scrubbing hasn't run yet (e.g. imported as a module)."""
    return _creds_passphrase_cache is not None or \
        bool(os.environ.get("FAST_COPY_CREDS_PASSPHRASE"))


def _creds_passphrase(confirm=False):
    global _creds_passphrase_cache
    if _creds_passphrase_cache is None:
        # Normally captured at startup; handle paths where that hasn't run.
        _scrub_passphrase_env()
    if _creds_passphrase_cache is not None:
        return _creds_passphrase_cache  # from env, or already entered — don't re-ask
    if not sys.stdin.isatty():
        raise SystemExit("Error: encrypted credentials need a passphrase. Set "
                         "FAST_COPY_CREDS_PASSPHRASE or run in a terminal.")
    pw = getpass.getpass("  Credentials passphrase: ")
    if confirm and pw != getpass.getpass("  Confirm passphrase: "):
        raise SystemExit("Error: passphrases did not match.")
    if not pw:
        raise SystemExit("Error: empty passphrase.")
    _creds_passphrase_cache = bytearray(pw.encode("utf-8"))
    return _creds_passphrase_cache


def _derive_key(passphrase, salt, n=None, r=None, p=None):
    n = n or _SCRYPT["n"]
    r = r or _SCRYPT["r"]
    p = p or _SCRYPT["p"]
    return hashlib.scrypt(passphrase, salt=salt, dklen=32, n=n, r=r, p=p,
                          maxmem=_scrypt_maxmem(n, r))


def encrypt_conns(conns, passphrase):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, nonce = os.urandom(16), os.urandom(12)
    key = _derive_key(passphrase, salt)
    binhash = _self_hash()
    pt = json.dumps({"connections": conns}).encode("utf-8")
    ct = AESGCM(key).encrypt(nonce, pt, binhash.encode("utf-8"))
    env = {"magic": CREDS_MAGIC, "kdf": "scrypt", **_SCRYPT,
           "salt": base64.b64encode(salt).decode(),
           "nonce": base64.b64encode(nonce).decode(),
           "binhash": binhash,
           "ct": base64.b64encode(ct).decode()}
    return json.dumps(env, indent=2).encode("utf-8")


def _is_encrypted(raw):
    try:
        return json.loads(raw).get("magic") == CREDS_MAGIC
    except (ValueError, AttributeError):
        return False


def _stored_binhash(raw):
    """The bound-binary hash recorded in the envelope (plaintext AEAD AAD), or
    '' if unreadable. Lets us check the binary binding without the passphrase."""
    try:
        return json.loads(raw).get("binhash", "") or ""
    except (ValueError, AttributeError):
        return ""


def decrypt_conns(raw, passphrase):
    """Returns (conns, stored_binhash). Raises SystemExit on bad passphrase."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.exceptions import InvalidTag
    except ImportError:
        raise SystemExit("Error: encrypted credentials need the 'cryptography' "
                         "package. Install: python -m pip install cryptography")
    env = json.loads(raw)
    salt = base64.b64decode(env["salt"])
    key = _derive_key(passphrase, salt, n=env.get("n"), r=env.get("r"),
                      p=env.get("p"))
    try:
        pt = AESGCM(key).decrypt(base64.b64decode(env["nonce"]),
                                 base64.b64decode(env["ct"]),
                                 env.get("binhash", "").encode("utf-8"))
    except InvalidTag:
        raise SystemExit("Error: wrong passphrase, or the credentials file was "
                         "tampered with.")
    return json.loads(pt)["connections"], env.get("binhash", "")


def _set_hidden(path):
    """Windows: set the HIDDEN attribute. Unix: file already lives in ~/.config
    (a hidden dir); nothing extra needed."""
    if sys.platform == "win32":
        try:
            import ctypes
            FILE_ATTRIBUTE_HIDDEN = 0x2
            attrs = ctypes.windll.kernel32.GetFileAttributesW(path)
            if attrs != -1:
                ctypes.windll.kernel32.SetFileAttributesW(
                    path, attrs | FILE_ATTRIBUTE_HIDDEN)
        except Exception:
            pass


def set_immutable(path, on):
    """Toggle OS-level immutability. Returns (ok, message). NOTE: this is
    tamper-resistance, not confidentiality, and is not absolute against root —
    root can always reverse it. Setting it generally requires root/admin."""
    sysname = platform.system()
    try:
        if sysname == "Linux":
            import subprocess
            r = subprocess.run(["chattr", "+i" if on else "-i", path],
                               capture_output=True, text=True)
            if r.returncode != 0:
                msg = (r.stderr.strip() or "chattr failed") + \
                    " (needs root: try sudo, and an ext4/xfs/btrfs filesystem)"
                return False, msg
            return True, None
        if sysname == "Darwin":
            import subprocess
            r = subprocess.run(["chflags", "uchg" if on else "nouchg", path],
                               capture_output=True, text=True)
            return (r.returncode == 0, r.stderr.strip() or None)
        if sysname == "Windows":
            import ctypes
            RO = 0x1
            a = ctypes.windll.kernel32.GetFileAttributesW(path)
            if a == -1:
                return False, "GetFileAttributes failed"
            a = (a | RO) if on else (a & ~RO)
            ok = ctypes.windll.kernel32.SetFileAttributesW(path, a)
            return (bool(ok), None if ok else "SetFileAttributes failed "
                    "(note: read-only is weak; admins override it)")
        return False, f"immutability not supported on {sysname}"
    except FileNotFoundError:
        return False, "the immutability tool (chattr/chflags) is not installed"
    except Exception as e:
        return False, str(e)


def _is_immutable(path):
    """Best-effort check of the OS immutable attribute. Returns True only if we
    can positively confirm it is set; False on any uncertainty (unsupported FS,
    platform without a flag we can read, or error). Unlike the return value of
    `set_immutable(on=False)`, this reflects the file's actual state — clearing
    an already-clear flag 'succeeds' for any owner, so the unlock result can't
    be used to tell whether the file was really protected."""
    try:
        if _system == "Linux":
            import fcntl
            import array
            FS_IOC_GETFLAGS = 0x80086601
            FS_IMMUTABLE_FL = 0x00000010
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags)
            try:
                buf = array.array("B", b"\x00" * 8)
                fcntl.ioctl(fd, FS_IOC_GETFLAGS, buf, True)
                return bool(int.from_bytes(buf, "little") & FS_IMMUTABLE_FL)
            finally:
                os.close(fd)
        if _system == "Windows":
            # No true immutability on Windows — set_immutable() approximates it
            # with the read-only attribute, so mirror that here.
            import ctypes
            FILE_ATTRIBUTE_READONLY = 0x1
            a = ctypes.windll.kernel32.GetFileAttributesW(str(path))
            return a != -1 and bool(a & FILE_ATTRIBUTE_READONLY)
        # macOS/BSD surface immutability through stat flags.
        st = os.lstat(path)
        UF_IMMUTABLE, SF_IMMUTABLE = 0x00000002, 0x00020000
        return bool(getattr(st, "st_flags", 0) & (UF_IMMUTABLE | SF_IMMUTABLE))
    except Exception:
        return False


_tamper_warned = False  # one tamper-check Note per process (file is read twice)
_creds_cache = {}       # realpath -> decrypted conns, this process only


def load_credentials_file(path=None):
    """Load {name: {type, ...}} connections. Returns {} if no file. Decrypts
    transparently when the file is encrypted. Warns if perms are loose, or if
    the bound binary hash no longer matches (tamper-evidence).

    The result is memoized per resolved path for the life of the process: a
    single run reads/decrypts the file at most once even though it's loaded
    from several call sites (endpoint-name resolution, then the backend). Any
    write through _save_credentials_file invalidates the cache."""
    explicit = path is not None
    path = path or default_credentials_path()
    if not os.path.isfile(path):
        if explicit:
            print(f"  {C.YELLOW}Warning: credentials file not found: {path}{C.RESET}")
        return {}
    ckey = os.path.realpath(path)
    if ckey in _creds_cache:
        return _creds_cache[ckey]
    if hasattr(os, "geteuid"):
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                print(f"  {C.YELLOW}Warning: {path} is group/world-accessible "
                      f"(mode {oct(mode)}). Fix: chmod 600 {path}{C.RESET}")
        except OSError:
            pass
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        raise SystemExit(f"Error: cannot read credentials file {path}: {e}")
    if _is_encrypted(raw):
        # The bound-binary hash is stored in the envelope as plaintext (it's the
        # AEAD associated data), so we can warn about a mismatch *before* asking
        # for the passphrase — the prompt then has context, and the user can
        # rekey instead of typing into an unexpectedly-rebound file.
        cur = _self_hash()
        stored = _stored_binhash(raw)
        # The file is legitimately read more than once per run (endpoint-name
        # resolution, then the backend), so warn at most once per process.
        global _tamper_warned
        if stored and cur and stored != cur and not _tamper_warned:
            _tamper_warned = True
            print(f"  {C.YELLOW}Note: this credentials file was bound to a "
                  f"different fast_copy.py (tamper check). If you just updated, "
                  f"run 'creds rekey' to re-bind.{C.RESET}")
        conns, _ = decrypt_conns(raw, _creds_passphrase())
        result = conns if isinstance(conns, dict) else {}
        _creds_cache[ckey] = result
        return result
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise SystemExit(f"Error: cannot parse credentials file {path}: {e}")
    conns = data.get("connections", data) if isinstance(data, dict) else {}
    if not isinstance(conns, dict):
        raise SystemExit(f"Error: credentials file {path} has no 'connections' map.")
    _creds_cache[ckey] = conns
    return conns


def resolve_connection(spec, args):
    """Merge credentials for one cloud endpoint. Priority: a named connection
    from the file (when 'name@' is used, or a connection literally named
    'default' whose type matches) → overlaid by any explicit CLI flags. Returns
    a dict the backend reads; empty dict means 'use flags/env as before'."""
    name = spec.connection
    explicit_file = getattr(args, "credentials_file", None)
    # Only touch the credentials file when it's actually wanted. A named
    # connection or an explicit --credentials-file → load it (decrypt/prompt as
    # needed). Otherwise we only *peek* at a default-path file for a "default"
    # connection — and if that file is encrypted with no passphrase available,
    # skip it silently rather than forcing a prompt on an unrelated transfer.
    conns = {}
    if name or explicit_file:
        conns = load_credentials_file(explicit_file)
    elif os.path.isfile(default_credentials_path()):
        if _file_is_encrypted(default_credentials_path()) and \
                not _have_creds_passphrase():
            conns = {}
        else:
            try:
                conns = load_credentials_file(None)
            except SystemExit:
                conns = {}
    creds = {}
    if not name and "default" in conns and \
            isinstance(conns["default"], dict) and \
            conns["default"].get("type") == spec.scheme:
        name = "default"
    if name:
        if name not in conns:
            raise SystemExit(f"Error: no connection named {name!r} in the "
                             f"credentials file.")
        conn = conns[name]
        ctype = conn.get("type")
        if ctype and ctype != spec.scheme:
            raise SystemExit(f"Error: connection {name!r} is type {ctype!r} but "
                             f"the URL is {spec.scheme}://.")
        creds = {k: v for k, v in conn.items() if k != "type"}
    # Explicit flags override file values for this invocation.
    flag_overlay = {
        "s3": {"endpoint_url": getattr(args, "endpoint_url", None),
               "region": getattr(args, "s3_region", None),
               "profile": getattr(args, "s3_profile", None)},
        "az": {"connection_string": getattr(args, "az_connection_string", None),
               "account": getattr(args, "az_account", None),
               "key": getattr(args, "az_key", None)},
        "gs": {"project": getattr(args, "gcs_project", None),
               "credentials": getattr(args, "gcs_credentials", None)},
    }.get(spec.scheme, {})
    for k, v in flag_overlay.items():
        if v:
            creds[k] = v
    return creds


CLOUD_SCHEME_NAMES = {"s3": "S3", "az": "Azure Blob", "gs": "Google Cloud Storage"}


def _quote_rel(rel):
    import urllib.parse
    return urllib.parse.quote(rel, safe="")


def _unquote_rel(val):
    import urllib.parse
    return urllib.parse.unquote(val)


def build_object_meta(entry, hash_hex, preserve=None):
    """fast-copy object metadata so a download restores the file faithfully and
    cross-run dedup works without re-reading bytes. All keys lowercase (S3
    lowercases user metadata regardless)."""
    preserve = preserve or set()
    meta = {
        "fc_relpath": _quote_rel(entry.rel),
        "fc_hash": hash_hex or "",
        "fc_hash_algo": _hash_name,
    }
    try:
        st = os.stat(entry.src)
        meta["fc_mtime"] = repr(st.st_mtime)
        meta["fc_mode"] = oct(stat.S_IMODE(st.st_mode))
        if "owner" in preserve:
            meta["fc_uid"] = str(getattr(st, "st_uid", 0))
            meta["fc_gid"] = str(getattr(st, "st_gid", 0))
    except OSError:
        pass
    return meta


def apply_object_meta_local(local_path, meta):
    """Restore mtime/mode (and owner when present + permitted) from object
    metadata onto a freshly downloaded local file. Best-effort, single-line
    warnings only."""
    if not meta:
        return
    mode = meta.get("fc_mode")
    if mode:
        try:
            os.chmod(local_path, int(mode, 8))
        except (OSError, ValueError):
            pass
    mtime = meta.get("fc_mtime")
    if mtime:
        try:
            t = float(mtime)
            os.utime(local_path, (t, t))
        except (OSError, ValueError):
            pass
    uid, gid = meta.get("fc_uid"), meta.get("fc_gid")
    if uid and gid and hasattr(os, "chown"):
        try:
            os.chown(local_path, int(uid), int(gid))
        except (OSError, ValueError, PermissionError):
            pass


# ── Backend base + provider implementations ─────────────────────────────────
class CloudBackend:
    """Common interface. Subclasses implement the primitives; the orchestrator
    is provider-agnostic."""
    scheme = ""

    def __init__(self, spec, args, creds=None):
        self.spec = spec
        self.container = spec.container
        self.prefix = spec.prefix
        self.args = args
        # Resolved credentials for this endpoint (from the credentials file /
        # name@bucket / flags). Empty → backend falls back to env, as before.
        self.creds = creds or {}

    # discovery
    def list_objects(self, prefix):
        """Return {full_key: {'size': int}} for every object under prefix."""
        raise NotImplementedError

    def head(self, key):
        """Return metadata dict for key, or None if it does not exist."""
        raise NotImplementedError

    # transfer
    def upload(self, local_path, key, metadata):
        raise NotImplementedError

    def download(self, key, local_path):
        """Download key to local_path; return its metadata dict."""
        raise NotImplementedError

    def server_side_copy(self, src_key, dst_key, metadata):
        """Copy within the same container/account without round-tripping bytes."""
        raise NotImplementedError

    def join_key(self, prefix, rel):
        rel = rel.replace(os.sep, "/")
        if not prefix:
            return rel
        return prefix.rstrip("/") + "/" + rel


class S3Backend(CloudBackend):
    scheme = "s3"

    def __init__(self, spec, args, creds=None):
        super().__init__(spec, args, creds)
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            raise SystemExit("Error: S3 transfers require boto3. "
                             "Install with: python -m pip install boto3")
        c = self.creds
        session_kwargs = {}
        if c.get("profile"):
            session_kwargs["profile_name"] = c["profile"]
        session = boto3.Session(**session_kwargs)
        cfg = Config(signature_version="s3v4",
                     retries={"max_attempts": 5, "mode": "standard"})
        client_kwargs = {
            "endpoint_url": c.get("endpoint_url"),
            "region_name": c.get("region") or "us-east-1",
            "config": cfg,
        }
        # Explicit keys from the credentials file (else boto3's default chain:
        # env / ~/.aws / instance profile).
        has_explicit_keys = bool(c.get("access_key_id")
                                 and c.get("secret_access_key"))
        if has_explicit_keys:
            client_kwargs["aws_access_key_id"] = c["access_key_id"]
            client_kwargs["aws_secret_access_key"] = c["secret_access_key"]
            if c.get("session_token"):
                client_kwargs["aws_session_token"] = c["session_token"]
        self.client = session.client("s3", **client_kwargs)
        # Fail fast with a clean message instead of a NoCredentialsError
        # traceback at the first API call. Only check the default chain when
        # no explicit keys were given (session creds don't reflect client keys).
        if not has_explicit_keys and session.get_credentials() is None:
            raise SystemExit(
                "Error: S3 needs credentials. Add them with `creds add`, use "
                "--s3-profile, or set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY "
                "(and AWS_SESSION_TOKEN if used).")

    def list_objects(self, prefix):
        out = {}
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.container, Prefix=prefix or ""):
            for obj in page.get("Contents", []):
                out[obj["Key"]] = {"size": obj["Size"]}
        return out

    def head(self, key):
        try:
            r = self.client.head_object(Bucket=self.container, Key=key)
            return dict(r.get("Metadata", {}))
        except Exception:
            return None

    def upload(self, local_path, key, metadata):
        self.client.upload_file(
            local_path, self.container, key,
            ExtraArgs={"Metadata": metadata,
                       "ChecksumAlgorithm": "SHA256"})

    def download(self, key, local_path):
        r = self.client.get_object(Bucket=self.container, Key=key)
        body = r["Body"]
        with open(local_path, "wb") as f:
            for chunk in body.iter_chunks(chunk_size=1024 * 1024):
                f.write(chunk)
        return dict(r.get("Metadata", {}))

    def server_side_copy(self, src_key, dst_key, metadata):
        self.client.copy_object(
            Bucket=self.container, Key=dst_key,
            CopySource={"Bucket": self.container, "Key": src_key},
            Metadata=metadata, MetadataDirective="REPLACE")


class AzureBackend(CloudBackend):
    scheme = "az"

    def __init__(self, spec, args, creds=None):
        super().__init__(spec, args, creds)
        try:
            from azure.storage.blob import BlobServiceClient
        except ImportError:
            raise SystemExit("Error: Azure transfers require azure-storage-blob. "
                             "Install with: python -m pip install azure-storage-blob")
        c = self.creds
        conn = c.get("connection_string") \
            or os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
        if conn:
            self.service = BlobServiceClient.from_connection_string(conn)
        else:
            account = c.get("account") \
                or os.environ.get("AZURE_STORAGE_ACCOUNT")
            key = c.get("key") \
                or os.environ.get("AZURE_STORAGE_KEY")
            if not account:
                raise SystemExit("Error: Azure needs --az-connection-string, or "
                                 "--az-account (+ --az-key), or the "
                                 "AZURE_STORAGE_* environment variables.")
            url = f"https://{account}.blob.core.windows.net"
            self.service = BlobServiceClient(account_url=url, credential=key)
        self.cc = self.service.get_container_client(self.container)

    def list_objects(self, prefix):
        out = {}
        for b in self.cc.list_blobs(name_starts_with=prefix or ""):
            out[b.name] = {"size": b.size}
        return out

    def head(self, key):
        try:
            props = self.cc.get_blob_client(key).get_blob_properties()
            return dict(props.metadata or {})
        except Exception:
            return None

    def upload(self, local_path, key, metadata):
        bc = self.cc.get_blob_client(key)
        with open(local_path, "rb") as f:
            bc.upload_blob(f, overwrite=True, metadata=metadata)

    def download(self, key, local_path):
        bc = self.cc.get_blob_client(key)
        stream = bc.download_blob()
        with open(local_path, "wb") as f:
            stream.readinto(f)
        try:
            meta = dict(bc.get_blob_properties().metadata or {})
        except Exception:
            meta = {}
        return meta

    def server_side_copy(self, src_key, dst_key, metadata):
        src = self.cc.get_blob_client(src_key)
        dst = self.cc.get_blob_client(dst_key)
        dst.start_copy_from_url(src.url, metadata=metadata)


class GCSBackend(CloudBackend):
    scheme = "gs"

    def __init__(self, spec, args, creds=None):
        super().__init__(spec, args, creds)
        try:
            from google.cloud import storage
        except ImportError:
            raise SystemExit("Error: GCS transfers require google-cloud-storage. "
                             "Install with: python -m pip install google-cloud-storage")
        c = self.creds
        client_kwargs = {}
        if c.get("project"):
            client_kwargs["project"] = c["project"]
        creds_path = c.get("credentials")
        emulator = os.environ.get("STORAGE_EMULATOR_HOST")
        if creds_path:
            self.client = storage.Client.from_service_account_json(
                creds_path, **client_kwargs)
        elif emulator:
            from google.auth.credentials import AnonymousCredentials
            self.client = storage.Client(
                credentials=AnonymousCredentials(),
                client_options={"api_endpoint": emulator},
                project=client_kwargs.get("project", "fast-copy"))
        else:
            try:
                self.client = storage.Client(**client_kwargs)
            except Exception as e:
                from google.auth.exceptions import DefaultCredentialsError
                if isinstance(e, DefaultCredentialsError):
                    raise SystemExit(
                        "Error: GCS needs credentials. Provide --gcs-credentials "
                        "<service-account.json>, set GOOGLE_APPLICATION_CREDENTIALS, "
                        "or run `gcloud auth application-default login`.")
                raise
        self.bucket = self.client.bucket(self.container)

    def list_objects(self, prefix):
        out = {}
        for b in self.client.list_blobs(self.container, prefix=prefix or ""):
            out[b.name] = {"size": b.size or 0}
        return out

    def head(self, key):
        b = self.bucket.get_blob(key)
        if b is None:
            return None
        return dict(b.metadata or {})

    def upload(self, local_path, key, metadata):
        blob = self.bucket.blob(key)
        blob.metadata = metadata
        blob.upload_from_filename(local_path)

    def download(self, key, local_path):
        blob = self.bucket.get_blob(key)
        if blob is None:
            raise FileNotFoundError(f"gs://{self.container}/{key}")
        blob.download_to_filename(local_path)
        return dict(blob.metadata or {})

    def server_side_copy(self, src_key, dst_key, metadata):
        src = self.bucket.blob(src_key)
        new = self.bucket.copy_blob(src, self.bucket, dst_key)
        if metadata:
            new.metadata = metadata
            new.patch()


def make_backend(spec, args):
    creds = resolve_connection(spec, args)
    cls = {"s3": S3Backend, "az": AzureBackend, "gs": GCSBackend}[spec.scheme]
    return cls(spec, args, creds)


# ── creds manager:  fast_copy.py creds add|list|remove|test ──────────────────
def _windows_clear_attrs(path):
    """Clear HIDDEN/READONLY/SYSTEM on an existing file so a rewrite can open it.
    On Windows, open(path,'wb') (CREATE_ALWAYS) FAILS with access-denied when the
    target is hidden — so we must strip the attribute before overwriting."""
    if _system != "Windows" or not os.path.exists(path):
        return
    try:
        import ctypes
        FILE_ATTRIBUTE_NORMAL = 0x80
        a = ctypes.windll.kernel32.GetFileAttributesW(path)
        if a != -1:
            cleared = a & ~0x1 & ~0x2 & ~0x4  # READONLY, HIDDEN, SYSTEM
            ctypes.windll.kernel32.SetFileAttributesW(
                path, cleared or FILE_ATTRIBUTE_NORMAL)
    except Exception:
        pass


def _entry_has_secret(c):
    """True if a connection entry holds a secret VALUE in cleartext (not a mere
    file-path reference). Used to warn before an unencrypted write. Note: an SSH
    'key' is a private-key file *path* (not a secret here), unlike an Azure
    'key' which is the account key itself."""
    t = c.get("type")
    if t == "s3":
        return bool(c.get("secret_access_key"))
    if t == "az":
        return bool(c.get("connection_string") or c.get("key"))
    if t == "ssh":
        return bool(c.get("password"))
    return False


def _save_credentials_file(path, conns, encrypt=False):
    """Write the connections file. encrypt=True → AES-256-GCM (prompts for the
    passphrase). Always 0600 + hidden.

    Auto-manages the immutable lock so a protected creds file stays protected
    without a manual unlock/lock dance: every mutating op (add/edit/remove/
    encrypt/decrypt/rekey) routes through here, so it transparently unlocks the
    file, rewrites it, then re-locks it — and a freshly created file is locked
    on first write. Best-effort, mirroring the audit log: where chattr/chflags
    is unavailable, the FS can't do it, or we lack root, it degrades silently —
    only warning if it had to remove protection it then couldn't restore.
    Read-only ops (list/test) don't come through here — an immutable file is
    still readable, so they need no unlock."""
    # The on-disk file is changing; drop any memoized decryption so a later read
    # in this same process sees the new contents (e.g. add → save → list).
    _creds_cache.clear()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if encrypt:
        blob = encrypt_conns(conns, _creds_passphrase(confirm=True))
    else:
        blob = json.dumps({"connections": conns}, indent=2).encode("utf-8")
        if any(isinstance(c, dict) and _entry_has_secret(c)
               for c in conns.values()):
            print(f"  {C.YELLOW}Warning: writing credentials UNENCRYPTED to "
                  f"{path} — passwords/keys are stored in cleartext, readable by "
                  f"anyone who can read the file. Encrypt with: "
                  f"fast_copy.py creds encrypt{C.RESET}", file=sys.stderr)

    # Guard against a symlink before any chattr — set_immutable() follows
    # symlinks, so a planted link could otherwise redirect the lock elsewhere.
    try:
        _lst = os.lstat(path)
    except FileNotFoundError:
        _lst = None
    if _lst is not None and stat.S_ISLNK(_lst.st_mode):
        raise SystemExit(f"Error: refusing to write {path}: it is a symlink.")
    existed = _lst is not None
    # Was the file actually protected before we touched it? Check the real flag
    # (not the unlock return value — clearing an already-clear flag 'succeeds'
    # for any owner). This is the only case where failing to re-lock below is a
    # genuine regression worth warning about.
    was_immutable = _is_immutable(path) if existed else False
    if existed:
        unlocked, _ = set_immutable(path, on=False)  # unlock so the rewrite proceeds
        if was_immutable and not unlocked:
            # A non-root user can't clear the immutable flag, so the rewrite
            # below would fail with a cryptic EPERM and we'd point at
            # 'creds unlock' — which also needs root, looping the user. Fail
            # now with a command that actually works. The explicit path matters:
            # under sudo the default-path lookup resolves to root's home, not
            # this file.
            raise SystemExit(
                f"Error: {path} is immutable; clearing the lock needs root. "
                f"Run: sudo fast_copy.py creds unlock {path}")

    _windows_clear_attrs(path)  # else overwriting a hidden file fails on Windows
    old = os.umask(0o077)  # owner-only from creation — no world-readable window
    _imm = (f"Error: cannot write {path} — it may be immutable or read-only. "
            f"Run: sudo fast_copy.py creds unlock {path}")
    try:
        if hasattr(os, "O_NOFOLLOW"):
            # Refuse to write through a symlink (parity with the dedup DB), so a
            # planted symlink at the creds path can't redirect the write.
            try:
                fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC
                             | os.O_NOFOLLOW, 0o600)
            except OSError as e:
                if e.errno in (errno.ELOOP, errno.EMLINK):
                    raise SystemExit(f"Error: refusing to write {path}: it is a "
                                     f"symlink.")
                if isinstance(e, PermissionError):
                    raise SystemExit(_imm)
                raise
            with os.fdopen(fd, "wb") as f:
                f.write(blob)
        else:
            with open(path, "wb") as f:
                f.write(blob)
    except PermissionError:
        raise SystemExit(_imm)
    finally:
        os.umask(old)

    # Re-lock (or lock-on-first-write) so the file is immutable at rest. Quiet by
    # design: confirm on success, warn ONLY if we removed protection and then
    # couldn't restore it, and stay silent in the benign "can't lock here" case
    # (non-root or a FS without immutability, where nothing was protected).
    ok, msg = set_immutable(path, on=True)
    if ok:
        print(f"  {C.DIM}creds: {'locked' if not existed else 're-locked'} "
              f"(immutable){C.RESET}")
    elif was_immutable:
        print(f"  {C.YELLOW}creds: WARNING — {path} was immutable but could not "
              f"be re-locked ({msg or 'unknown error'}). Run 'sudo fast_copy.py "
              f"creds lock' to restore protection.{C.RESET}", file=sys.stderr)
    if hasattr(os, "chmod"):
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    _set_hidden(path)


def _file_is_encrypted(path):
    try:
        with open(path, "rb") as f:
            return _is_encrypted(f.read())
    except OSError:
        return False


def _mask(v):
    if not v:
        return ""
    return (v[:3] + "…") if len(v) > 6 else "***"


def _prompt_secret(prompt):
    """Read a secret from the terminal, echoing '*' per character so you get
    length feedback while typing without exposing the value (getpass shows
    nothing at all). Backspace erases the last character. Falls back to getpass
    (no echo) when stdin isn't a real TTY or the terminal can't be put into raw
    mode — so pipes, CI, and odd terminals keep working unchanged."""
    if not sys.stdin.isatty():
        return getpass.getpass(prompt)
    try:
        if _system == "Windows":
            import msvcrt
            sys.stdout.write(prompt); sys.stdout.flush()
            buf = []
            while True:
                ch = msvcrt.getwch()
                if ch in ("\r", "\n"):
                    break
                if ch == "\x03":               # Ctrl-C
                    raise KeyboardInterrupt
                if ch in ("\x00", "\xe0"):     # function/arrow key: drop 2nd byte
                    msvcrt.getwch()
                elif ch == "\b":               # backspace
                    if buf:
                        buf.pop(); sys.stdout.write("\b \b")
                else:
                    buf.append(ch); sys.stdout.write("*")
                sys.stdout.flush()
            sys.stdout.write("\n"); sys.stdout.flush()
            return "".join(buf)
        import termios, tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        sys.stdout.write(prompt); sys.stdout.flush()
        buf = []
        try:
            tty.setraw(fd)
            while True:
                ch = sys.stdin.read(1)
                if ch in ("\r", "\n", ""):     # Enter or EOF ends input
                    break
                if ch == "\x03":               # raw mode delivers Ctrl-C as a byte
                    raise KeyboardInterrupt
                if ch in ("\x7f", "\b"):       # backspace / delete
                    if buf:
                        buf.pop(); sys.stdout.write("\b \b"); sys.stdout.flush()
                    continue
                buf.append(ch); sys.stdout.write("*"); sys.stdout.flush()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
            sys.stdout.write("\n"); sys.stdout.flush()
        return "".join(buf)
    except KeyboardInterrupt:
        raise
    except Exception:
        # Any terminal-control hiccup → degrade to a plain no-echo prompt.
        return getpass.getpass(prompt)


def creds_manager(argv):
    """Handle `fast_copy.py creds <sub> [name] [path]`. Returns an exit code."""
    if not argv or argv[0] in ("-h", "--help", "help"):
        print("Usage: fast_copy.py creds <sub> [NAME] [FILE]\n"
              "  list                       show connections (secrets masked)\n"
              "  add NAME [-y]              add a connection (s3/azure/gcs/ssh,\n"
              "                             interactive; prompts before overwrite,\n"
              "                             -y/--force to skip)\n"
              "  edit NAME                  edit a connection (Enter keeps current,\n"
              "                             '-' clears an optional field)\n"
              "  remove NAME                delete a connection\n"
              "  test NAME                  live connection check (cloud or ssh)\n"
              "  encrypt | decrypt          toggle AES-256-GCM encryption at rest\n"
              "  rekey                      re-bind an encrypted file to this binary\n"
              "  lock | unlock              set/clear OS immutability (needs root)\n"
              "  --use-sudo                 re-exec under sudo (e.g. for lock/\n"
              "                             unlock, which need root for chattr)\n"
              f"  Default file: {default_credentials_path()}\n"
              "  Passphrase: env FAST_COPY_CREDS_PASSPHRASE or a hidden prompt.\n"
              "  Note: on Linux, FAST_COPY_CREDS_PASSPHRASE is readable from\n"
              "  /proc/<pid>/environ by same-UID processes; prefer the hidden\n"
              "  prompt on shared/multi-user hosts.")
        return 0
    sub = argv[0]
    # Separate options from positionals so a flag (e.g. --use-sudo) is never
    # mistaken for the NAME or FILE positional. Only a small set of flags is
    # valid here; reject anything else instead of silently treating it as a path.
    opts = argv[1:]
    force = any(a in ("-y", "--yes", "--force") for a in opts)
    known = ("-y", "--yes", "--force", "--use-sudo")
    unknown = [a for a in opts if a.startswith("-") and a not in known]
    if unknown:
        print(f"{C.RED}Error: unknown option for 'creds {sub}': "
              f"{' '.join(unknown)}. Valid flags: -y/--force, --use-sudo. "
              f"NAME and FILE are positional.{C.RESET}", file=sys.stderr)
        return 2
    positionals = [a for a in opts if not a.startswith("-")]
    # list/encrypt/decrypt/rekey/lock/unlock take only an optional FILE;
    # add/remove/test take NAME [FILE].
    if sub in ("list", "encrypt", "decrypt", "rekey", "lock", "unlock"):
        name = None
        path = positionals[0] if positionals else default_credentials_path()
    else:
        name = positionals[0] if positionals else None
        path = positionals[1] if len(positionals) > 1 else default_credentials_path()
    encrypted = _file_is_encrypted(path) if os.path.isfile(path) else False
    conns = load_credentials_file(path) if os.path.isfile(path) else {}

    if sub == "list":
        if not conns:
            print(f"  No connections in {path}")
            return 0
        print(f"  Connections in {path}:")
        for n, c in sorted(conns.items()):
            t = c.get("type", "?")
            if t == "s3":
                extra = f"endpoint={c.get('endpoint_url') or 'AWS'} key={_mask(c.get('access_key_id'))}"
            elif t == "az":
                extra = (f"conn={_mask(c.get('connection_string'))}"
                         if c.get("connection_string")
                         else f"account={c.get('account')} key={_mask(c.get('key'))}")
            elif t == "gs":
                extra = f"project={c.get('project')} creds={c.get('credentials') or 'ADC'}"
            elif t == "ssh":
                auth = (f"key={c.get('key')}" if c.get("key")
                        else "password=***" if c.get("password") else "agent")
                extra = f"{c.get('user')}@{c.get('host')}:{c.get('port', 22)} {auth}"
                if c.get("path"):
                    extra += f" path={c.get('path')}"
            else:
                extra = ""
            if t in ("s3", "az", "gs") and c.get("container"):
                extra += f" default={c.get('container')}"
                if c.get("prefix"):
                    extra += "/" + c.get("prefix").strip("/")
            print(f"    {C.BOLD}{n}{C.RESET}  [{t}]  {extra}")
        return 0

    if sub == "remove":
        if not name or name not in conns:
            print(f"{C.RED}Error: no connection named {name!r} in {path}{C.RESET}")
            return 1
        del conns[name]
        _save_credentials_file(path, conns, encrypt=encrypted)
        print(f"  Removed connection {name!r}.")
        return 0

    if sub == "add":
        if not name:
            print(f"{C.RED}Error: 'creds add' needs a connection name.{C.RESET}")
            return 1
        t = (input("  Type [s3/azure/gcs/ssh]: ").strip() or "s3").lower()
        t = {"azure": "az", "gcs": "gs", "s3": "s3", "az": "az", "gs": "gs",
             "ssh": "ssh", "sftp": "ssh"}.get(t)
        if t not in ("s3", "az", "gs", "ssh"):
            print(f"{C.RED}Error: type must be s3, azure, gcs, or ssh.{C.RESET}")
            return 1
        entry = {"type": t}
        if t == "s3":
            ep = input("  Endpoint URL (blank = AWS): ").strip()
            if ep:
                entry["endpoint_url"] = ep
            entry["access_key_id"] = input("  Access key ID: ").strip()
            entry["secret_access_key"] = _prompt_secret("  Secret access key: ")
            region = input("  Region [us-east-1]: ").strip()
            if region:
                entry["region"] = region
        elif t == "az":
            cs = _prompt_secret("  Connection string (blank to use account+key): ")
            if cs:
                entry["connection_string"] = cs
            else:
                entry["account"] = input("  Account name: ").strip()
                entry["key"] = _prompt_secret("  Account key: ")
        elif t == "gs":
            entry["project"] = input("  GCP project id: ").strip()
            cp = input("  Service-account JSON path (blank = ADC): ").strip()
            if cp:
                entry["credentials"] = cp
        elif t == "ssh":
            entry["host"] = input("  Host (name or IP): ").strip()
            if not entry["host"]:
                print(f"{C.RED}Error: SSH connection needs a host.{C.RESET}")
                return 1
            u = input(f"  User [{getpass.getuser()}]: ").strip()
            entry["user"] = u or getpass.getuser()
            p = input("  Port [22]: ").strip()
            if p:
                try:
                    entry["port"] = int(p)
                except ValueError:
                    print(f"{C.RED}Error: port must be a number.{C.RESET}")
                    return 1
            key = input("  Private key path (blank for password/agent): ").strip()
            if key:
                entry["key"] = key
            else:
                pw = _prompt_secret("  Password (blank = use SSH agent/keys): ")
                if pw:
                    entry["password"] = pw
            dp = input("  Default remote path (blank = none): ").strip()
            if dp:
                entry["path"] = dp
        if t in ("s3", "az", "gs"):
            # A default bucket/container lets you copy to just `name` (no URL).
            cword = "container" if t == "az" else "bucket"
            dc = input(f"  Default {cword} (blank = none): ").strip()
            if dc:
                entry["container"] = dc
                pf = input("  Default prefix inside it (blank = none): ").strip()
                if pf:
                    entry["prefix"] = pf
        # Guard against silently clobbering an existing connection of the same
        # name (the classic "second profile overwrote my first" surprise).
        if name in conns and not force:
            if not sys.stdin.isatty():
                print(f"{C.RED}Error: connection {name!r} already exists. "
                      f"Pass --force to overwrite.{C.RESET}")
                return 1
            ex = conns[name].get("type", "?")
            ans = input(f"  {C.YELLOW}Connection {name!r} already exists "
                        f"(type={ex}). Overwrite? [y/N]: {C.RESET}").strip().lower()
            if ans not in ("y", "yes"):
                print("  Aborted — existing connection kept.")
                return 0
        # First time this file is created → offer encryption up front, so
        # secrets aren't written in plaintext by default. Existing files keep
        # whatever state they already have.
        if not os.path.isfile(path):
            if _have_creds_passphrase():
                encrypted = True  # a passphrase is already provided → encrypt
            else:
                ans = input("  Encrypt this new credentials file with a "
                            "passphrase? [Y/n]: ").strip().lower()
                encrypted = ans in ("", "y", "yes")
        conns[name] = entry
        _save_credentials_file(path, conns, encrypt=encrypted)  # prompts if encrypting
        enc_note = ", encrypted" if encrypted else ""
        print(f"  {C.GREEN}✓ Saved connection {name!r} to {path} "
              f"({_perms_note(path)}{enc_note}){C.RESET}")
        return 0

    if sub == "edit":
        if not name or name not in conns:
            print(f"{C.RED}Error: no connection named {name!r} in {path}{C.RESET}")
            return 1
        if not sys.stdin.isatty():
            print(f"{C.RED}Error: 'creds edit' is interactive; run it in a "
                  f"terminal (or use 'creds add {name} --force').{C.RESET}")
            return 1
        cur = conns[name]
        t = cur.get("type")
        print(f"  Editing {name!r} (type={t}). Enter = keep current; type a new "
              f"value to change; '-' to clear an optional field.")

        def ask(label, current, secret=False):
            if secret:
                shown = "set" if current else "unset"
                v = getpass.getpass(f"  {label} [{shown}]: ")
            else:
                v = input(f"  {label} [{current if current not in (None, '') else '—'}]: ").strip()
            if v == "":
                return current      # keep
            if v == "-":
                return None         # clear
            return v

        entry = {"type": t}

        def setif(k, v):
            if v not in (None, ""):
                entry[k] = v

        if t == "s3":
            setif("endpoint_url", ask("Endpoint URL (blank=AWS)", cur.get("endpoint_url")))
            setif("access_key_id", ask("Access key ID", cur.get("access_key_id")))
            setif("secret_access_key", ask("Secret access key", cur.get("secret_access_key"), secret=True))
            setif("region", ask("Region", cur.get("region")))
        elif t == "az":
            cs = ask("Connection string", cur.get("connection_string"), secret=True)
            if cs:
                setif("connection_string", cs)
            else:
                setif("account", ask("Account name", cur.get("account")))
                setif("key", ask("Account key", cur.get("key"), secret=True))
        elif t == "gs":
            setif("project", ask("GCP project id", cur.get("project")))
            setif("credentials", ask("Service-account JSON path (blank=ADC)", cur.get("credentials")))
        elif t == "ssh":
            setif("host", ask("Host", cur.get("host")))
            setif("user", ask("User", cur.get("user")))
            port = ask("Port", cur.get("port", 22))
            if port not in (None, ""):
                try:
                    entry["port"] = int(port)
                except (ValueError, TypeError):
                    print(f"{C.RED}Error: port must be a number.{C.RESET}")
                    return 1
            setif("key", ask("Private key path", cur.get("key")))
            setif("password", ask("Password", cur.get("password"), secret=True))
            setif("path", ask("Default remote path", cur.get("path")))
        else:
            print(f"{C.RED}Error: connection {name!r} has an unknown type {t!r}.{C.RESET}")
            return 1

        if t in ("s3", "az", "gs"):
            cword = "container" if t == "az" else "bucket"
            setif("container", ask(f"Default {cword}", cur.get("container")))
            if entry.get("container"):
                setif("prefix", ask("Default prefix inside it", cur.get("prefix")))

        conns[name] = entry
        _save_credentials_file(path, conns, encrypt=encrypted)
        enc_note = ", encrypted" if encrypted else ""
        print(f"  {C.GREEN}✓ Updated connection {name!r} in {path} "
              f"({_perms_note(path)}{enc_note}){C.RESET}")
        return 0

    if sub == "test":
        if not name or name not in conns:
            print(f"{C.RED}Error: no connection named {name!r} in {path}{C.RESET}")
            return 1
        scheme = conns[name].get("type")
        if scheme == "ssh":
            c = conns[name]
            spec = RemoteSpec(user=c.get("user") or getpass.getuser(),
                              host=c.get("host"), port=int(c.get("port", 22)),
                              path=c.get("path", ""))
            ssh = None
            try:
                ssh = SSHConnection(spec, port=spec.port, key_path=c.get("key"),
                                    password=c.get("password")).connect()
                ssh.exec_cmd("true")
            except Exception as e:
                print(f"  {C.RED}✗ {name}: connection failed — {e}{C.RESET}")
                return 1
            finally:
                if ssh:
                    try:
                        ssh.close()
                    except Exception:
                        pass
            print(f"  {C.GREEN}✓ {name}: SSH connection OK "
                  f"({spec.user}@{spec.host}:{spec.port}){C.RESET}")
            return 0
        if scheme not in ("s3", "az", "gs"):
            print(f"{C.RED}Error: connection {name!r} has no valid type.{C.RESET}")
            return 1
        probe_args = argparse.Namespace(credentials_file=path)
        spec = CloudSpec(scheme=scheme, container="_fc_probe_", prefix="",
                         connection=name)
        try:
            b = make_backend(spec, probe_args)
            if scheme == "s3":
                b.client.list_buckets()
            elif scheme == "az":
                b.service.get_service_properties()
            elif scheme == "gs":
                next(iter(b.client.list_buckets(page_size=1)), None)
        except SystemExit:
            raise
        except Exception as e:
            print(f"  {C.RED}✗ {name}: connection failed — {e}{C.RESET}")
            return 1
        print(f"  {C.GREEN}✓ {name}: connection OK{C.RESET}")
        return 0

    if sub == "encrypt":
        if not os.path.isfile(path):
            print(f"{C.RED}Error: no credentials file at {path}{C.RESET}")
            return 1
        if encrypted:
            print(f"  {path} is already encrypted.")
            return 0
        _save_credentials_file(path, conns, encrypt=True)  # prompts (confirm)
        print(f"  {C.GREEN}✓ Encrypted {path} (AES-256-GCM, bound to this "
              f"fast_copy.py).{C.RESET}")
        return 0

    if sub == "decrypt":
        if not encrypted:
            print(f"  {path} is not encrypted.")
            return 0
        _save_credentials_file(path, conns, encrypt=False)  # conns already decrypted
        print(f"  {C.GREEN}✓ Decrypted {path} (now plaintext, 0600).{C.RESET}")
        return 0

    if sub == "rekey":
        if not encrypted:
            print(f"{C.RED}Error: {path} is not encrypted; nothing to rekey.{C.RESET}")
            return 1
        _save_credentials_file(path, conns, encrypt=True)  # re-binds to current hash
        print(f"  {C.GREEN}✓ Re-bound {path} to this fast_copy.py.{C.RESET}")
        return 0

    if sub in ("lock", "unlock"):
        if not os.path.isfile(path):
            print(f"{C.RED}Error: no credentials file at {path}{C.RESET}")
            return 1
        want = (sub == "lock")
        state = "immutable" if want else "writable"
        # Short-circuit if it's already in the requested state — avoids a
        # misleading "✓ now writable" on a file that was never locked, and a
        # needless (possibly failing) chattr on an already-locked one.
        if _is_immutable(path) == want:
            print(f"  {C.DIM}{path} is already {state}; nothing to do.{C.RESET}")
            return 0
        ok, msg = set_immutable(path, on=want)
        if ok:
            print(f"  {C.GREEN}✓ {path} is now {state}.{C.RESET}")
            if want:
                print(f"  {C.DIM}Note: tamper-resistance only — root can reverse "
                      f"it. Run 'creds unlock' before editing.{C.RESET}")
            return 0
        print(f"  {C.RED}✗ could not {sub}: {msg}{C.RESET}")
        return 1

    print(f"{C.RED}Error: unknown creds subcommand {sub!r}{C.RESET}")
    return 1


def _cloud_hash_entries(entries, threads):
    """Hash a list of FileEntry locally (parallel). Returns {rel: hash}."""
    import concurrent.futures
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, threads)) as ex:
        futs = {ex.submit(hash_file, e.src): e for e in entries}
        for fut in concurrent.futures.as_completed(futs):
            e = futs[fut]
            try:
                results[e.rel] = fut.result()
            except Exception:
                results[e.rel] = None
    return results


def run_cloud_transfer(args):
    """Top-level driver for any transfer where the source or destination is an
    object-storage URL. Handles upload, download, and cloud-to-cloud."""
    if getattr(args, "extra_sources", None):
        print(f"{C.RED}Error: object-storage transfers take a single source "
              f"(got {len(args.extra_sources) + 1}). Point one source at a "
              f"common parent, or run separate copies.{C.RESET}")
        sys.exit(1)
    src_spec = parse_cloud_url(args.source)
    dst_spec = parse_cloud_url(args.destination)
    if src_spec and dst_spec:
        return _cloud_to_cloud(args, src_spec, dst_spec)
    if dst_spec:
        return _upload_to_cloud(args, dst_spec)
    return _download_from_cloud(args, src_spec)


def _preserve_set(args):
    val = (args.preserve or "")
    if val == "all":
        return {"mode", "times", "owner", "xattr", "acl"}
    if val == "none":
        return set()
    return set(t.strip() for t in val.split(",") if t.strip())


def _upload_to_cloud(args, dst_spec):
    backend = make_backend(dst_spec, args)
    pretty = CLOUD_SCHEME_NAMES[dst_spec.scheme]
    banner(f"UPLOAD → {pretty}")
    print(f"  Source:  {C.BOLD}{args.source}{C.RESET}")
    print(f"  Dest:    {C.BOLD}{dst_spec.scheme}://{dst_spec.container}/"
          f"{dst_spec.prefix}{C.RESET}")

    src = os.path.abspath(args.source)
    if not os.path.exists(src):
        print(f"\n{C.RED}Error: source not found: {args.source}{C.RESET}")
        sys.exit(1)
    # A single file uploads under <prefix>/<basename>; a directory mirrors the
    # tree (cp/SSH-backend semantics).
    if os.path.isfile(src):
        entries = [FileEntry(src=src, rel=os.path.basename(src),
                             size=os.path.getsize(src), physical_offset=0,
                             content_hash=None)]
    else:
        entries, scan_errors = scan_source(src, None, args.exclude)
        print()
        if scan_errors:
            print(f"  {C.YELLOW}{len(scan_errors)} file(s) could not be read "
                  f"and were skipped.{C.RESET}")
    if not entries:
        print(f"  {C.YELLOW}Nothing to upload.{C.RESET}")
        return

    preserve = _preserve_set(args)
    total_bytes = sum(e.size for e in entries)
    print(f"  Files:   {C.BOLD}{len(entries)}{C.RESET}  "
          f"({fmt_size(total_bytes)})")

    # Hash for verification + dedup (skip when dedup and verify both off).
    hashes = {}
    if not args.no_dedup or not args.no_verify:
        print(f"  {C.DIM}Hashing {len(entries)} files...{C.RESET}")
        hashes = _cloud_hash_entries(entries, args.threads)

    # Classify every entry BEFORE any network I/O so the parallel phases below
    # run without races. A duplicate is server-side-copied FROM its primary's
    # object, so all primaries must land first (Phase A) before any copy runs
    # (Phase B). first_key_for_hash is built here, single-threaded, so the
    # primary/duplicate decision is deterministic and lock-free.
    first_key_for_hash = {}
    uploaded = copied = skipped = errors = 0
    bytes_uploaded = bytes_deduped = 0

    # Cross-run dedup: read fc-hash from a manifest if present.
    manifest = {}
    if not args.no_cache:
        manifest = _load_cloud_manifest(backend, dst_spec.prefix)

    new_manifest = {}
    primaries = []          # (entry, key, meta) — upload the bytes
    dups = []               # (entry, src_key, key, meta) — server-side copy
    skip_bytes = 0
    for e in entries:
        key = backend.join_key(dst_spec.prefix, e.rel)
        h = hashes.get(e.rel)
        new_manifest[e.rel] = {"size": e.size, "hash": h}

        # Cross-run skip: same relpath + same hash already in the bucket.
        prev = manifest.get(e.rel)
        if (not args.overwrite and prev and h and prev.get("hash") == h):
            skipped += 1
            skip_bytes += e.size
            continue

        meta = build_object_meta(e, h, preserve)
        if h and not args.no_dedup and h in first_key_for_hash:
            dups.append((e, first_key_for_hash[h], key, meta))
        else:
            primaries.append((e, key, meta))
            if h and not args.no_dedup:
                first_key_for_hash[h] = key

    prog = Progress(total_bytes, len(entries))
    if skipped:
        prog.update(skip_bytes, skipped)
        prog.display()

    lock = threading.Lock()

    def _do_upload(item):
        nonlocal uploaded, bytes_uploaded, errors
        e, key, meta = item
        try:
            if not args.dry_run:
                backend.upload(e.src, key, meta)
            with lock:
                uploaded += 1
                bytes_uploaded += e.size
                prog.update(e.size, 1)
                prog.display()
        except Exception as ex:
            with lock:
                errors += 1
                print(f"\n  {C.RED}Error uploading {e.rel}: {ex}{C.RESET}")
                prog.update(e.size, 1)
                prog.display()

    def _do_copy(item):
        nonlocal copied, bytes_deduped, errors
        e, src_key, key, meta = item
        try:
            if not args.dry_run:
                backend.server_side_copy(src_key, key, meta)
            with lock:
                copied += 1
                bytes_deduped += e.size
                prog.update(e.size, 1)
                prog.display()
        except Exception as ex:
            with lock:
                errors += 1
                print(f"\n  {C.RED}Error copying {e.rel}: {ex}{C.RESET}")
                prog.update(e.size, 1)
                prog.display()

    workers = max(1, args.cloud_concurrency)
    # Phase A — upload primaries in parallel (moves the bytes).
    if primaries:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(as_completed([pool.submit(_do_upload, it) for it in primaries]))
    # Phase B — server-side-copy duplicates in parallel; primaries now exist.
    if dups:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(as_completed([pool.submit(_do_copy, it) for it in dups]))
    prog.finish()

    if not args.dry_run and not args.no_cache:
        _save_cloud_manifest(backend, dst_spec.prefix, new_manifest)

    # Verify: HEAD a sample and confirm fc-hash matches what we computed.
    if not args.dry_run and not args.no_verify and uploaded:
        _verify_uploads(backend, dst_spec.prefix, entries, hashes)

    banner("DONE")
    print(f"  Uploaded: {C.BOLD}{uploaded}{C.RESET} new  "
          f"({fmt_size(bytes_uploaded)})")
    if copied:
        print(f"  Deduped:  {C.BOLD}{copied}{C.RESET} via server-side copy  "
              f"({C.GREEN}{fmt_size(bytes_deduped)} bandwidth saved{C.RESET})")
    if skipped:
        print(f"  Skipped:  {C.BOLD}{skipped}{C.RESET} unchanged (cross-run)")
    if errors:
        print(f"  {C.RED}Errors:   {errors}{C.RESET}")
    if args.dry_run:
        print(f"  {C.YELLOW}(dry run — nothing uploaded){C.RESET}")
    if args.log_file:
        write_log_file(args.log_file, {
            "source": args.source, "destination": args.destination,
            "mode": f"upload_{dst_spec.scheme}", "total_files": len(entries),
            "copied": uploaded, "linked": copied, "skipped": skipped,
            "errors": errors, "total_bytes": total_bytes,
            "bytes_written": bytes_uploaded, "dedup_saved": bytes_deduped,
            "hash_algo": _hash_name,
        })
    if errors:
        sys.exit(1)


def _download_from_cloud(args, src_spec):
    backend = make_backend(src_spec, args)
    pretty = CLOUD_SCHEME_NAMES[src_spec.scheme]
    banner(f"DOWNLOAD ← {pretty}")
    print(f"  Source:  {C.BOLD}{src_spec.scheme}://{src_spec.container}/"
          f"{src_spec.prefix}{C.RESET}")
    print(f"  Dest:    {C.BOLD}{args.destination}{C.RESET}")

    objects = backend.list_objects(src_spec.prefix)
    # Never download our own manifest.
    objects = {k: v for k, v in objects.items()
               if not k.endswith(CLOUD_MANIFEST_NAME)}
    if not objects:
        print(f"  {C.YELLOW}No objects found under that prefix.{C.RESET}")
        return

    dst_root = os.path.abspath(args.destination)
    prefix = src_spec.prefix.rstrip("/")
    total_bytes = sum(v["size"] for v in objects.values())
    print(f"  Objects: {C.BOLD}{len(objects)}{C.RESET}  ({fmt_size(total_bytes)})")

    downloaded = skipped = errors = verified_fail = 0
    real_root = os.path.realpath(dst_root)
    prog = Progress(total_bytes, len(objects))
    lock = threading.Lock()

    # No dedup ordering on the way down, so every object is independent — one
    # flat parallel pool. os.makedirs(exist_ok=True) is safe under concurrency.
    def _do_download(item):
        nonlocal downloaded, errors, verified_fail
        key, info = item
        # Map key → local relative path. Prefer fc-relpath metadata when present.
        rel = key[len(prefix):].lstrip("/") if prefix and key.startswith(prefix) else key
        rel = rel.lstrip("/")
        if not rel:
            rel = key.rsplit("/", 1)[-1]
        # A remote bucket is untrusted: its keys/metadata can attempt traversal.
        # _safe_local_dest returns None for '..'/absolute/symlink-escape paths.
        local_path = _safe_local_dest(real_root, rel)
        if local_path is None:
            with lock:
                errors += 1
                print(f"\n  {C.RED}Skipping unsafe key: {key}{C.RESET}")
                prog.update(info["size"], 1); prog.display()
            return
        try:
            os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
            if args.dry_run:
                with lock:
                    prog.update(info["size"], 1); prog.display()
                return
            meta = backend.download(key, local_path)
            # If the object recorded its original relpath, honor it (rename) —
            # but only if it, too, stays safely inside the destination.
            recorded = meta.get("fc_relpath")
            if recorded:
                want = _unquote_rel(recorded)
                want_path = _safe_local_dest(real_root, want)
                if want_path is not None \
                        and os.path.abspath(want_path) != os.path.abspath(local_path):
                    os.makedirs(os.path.dirname(want_path) or ".", exist_ok=True)
                    os.replace(local_path, want_path)
                    local_path = want_path
            apply_object_meta_local(local_path, meta)
            # Verify: re-hash and compare to stored fc-hash (same algo only).
            vfail = False
            if not args.no_verify:
                want_hash = meta.get("fc_hash")
                if want_hash and meta.get("fc_hash_algo") == _hash_name:
                    vfail = hash_file(local_path) != want_hash
            with lock:
                if vfail:
                    verified_fail += 1
                    print(f"\n  {C.RED}VERIFY FAILED: {rel}{C.RESET}")
                downloaded += 1
                prog.update(info["size"], 1); prog.display()
        except Exception as ex:
            with lock:
                errors += 1
                print(f"\n  {C.RED}Error downloading {key}: {ex}{C.RESET}")
                prog.update(info["size"], 1); prog.display()

    workers = max(1, args.cloud_concurrency)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(as_completed([pool.submit(_do_download, it)
                           for it in sorted(objects.items())]))
    prog.finish()

    banner("DONE")
    print(f"  Downloaded: {C.BOLD}{downloaded}{C.RESET} ({fmt_size(total_bytes)})")
    if verified_fail:
        print(f"  {C.RED}Verification failures: {verified_fail}{C.RESET}")
    if errors:
        print(f"  {C.RED}Errors: {errors}{C.RESET}")
    if args.dry_run:
        print(f"  {C.YELLOW}(dry run — nothing downloaded){C.RESET}")
    if args.log_file:
        write_log_file(args.log_file, {
            "source": args.source, "destination": args.destination,
            "mode": f"download_{src_spec.scheme}", "total_files": len(objects),
            "copied": downloaded, "linked": 0, "skipped": skipped,
            "errors": errors + verified_fail, "total_bytes": total_bytes,
            "bytes_written": total_bytes, "dedup_saved": 0,
            "hash_algo": _hash_name,
        })
    if errors or verified_fail:
        sys.exit(1)


def _cloud_to_cloud(args, src_spec, dst_spec):
    """Cloud→cloud. Same provider + same container → server-side copy; otherwise
    relay through a local temp directory (download then upload)."""
    if (src_spec.scheme == dst_spec.scheme
            and src_spec.container == dst_spec.container):
        backend = make_backend(src_spec, args)
        banner(f"COPY (server-side) — {CLOUD_SCHEME_NAMES[src_spec.scheme]}")
        objects = {k: v for k, v in backend.list_objects(src_spec.prefix).items()
                   if not k.endswith(CLOUD_MANIFEST_NAME)}
        if not objects:
            print(f"  {C.YELLOW}No objects found under that prefix.{C.RESET}")
            return
        sp = src_spec.prefix.rstrip("/")
        dp = dst_spec.prefix.rstrip("/")
        copied = errors = 0
        prog = Progress(sum(v["size"] for v in objects.values()), len(objects))
        lock = threading.Lock()

        def _do_c2c(item):
            nonlocal copied, errors
            key, info = item
            rel = key[len(sp):].lstrip("/") if sp and key.startswith(sp) else key
            dst_key = (dp + "/" + rel).lstrip("/") if dp else rel
            try:
                if not args.dry_run:
                    meta = backend.head(key) or {}
                    backend.server_side_copy(key, dst_key, meta)
                with lock:
                    copied += 1
                    prog.update(info["size"], 1); prog.display()
            except Exception as ex:
                with lock:
                    errors += 1
                    print(f"\n  {C.RED}Error copying {key}: {ex}{C.RESET}")
                    prog.update(info["size"], 1); prog.display()

        workers = max(1, getattr(args, "cloud_concurrency",
                                 DEFAULT_CLOUD_CONCURRENCY))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(as_completed([pool.submit(_do_c2c, it)
                               for it in sorted(objects.items())]))
        prog.finish()
        banner("DONE")
        print(f"  Server-side copied: {C.BOLD}{copied}{C.RESET}")
        if errors:
            print(f"  {C.RED}Errors: {errors}{C.RESET}")
            sys.exit(1)
        return

    # Cross-provider / cross-container → relay through a temp dir.
    import tempfile
    relay = tempfile.mkdtemp(prefix="fast_copy_relay_")
    try:
        print(f"  {C.DIM}Relaying through local temp (cross-provider)...{C.RESET}")
        dl = argparse.Namespace(**vars(args))
        dl.destination = relay
        _download_from_cloud(dl, src_spec)
        up = argparse.Namespace(**vars(args))
        up.source = relay
        _upload_to_cloud(up, dst_spec)
    finally:
        shutil.rmtree(relay, ignore_errors=True)


def _load_cloud_manifest(backend, prefix):
    key = (prefix.rstrip("/") + "/" + CLOUD_MANIFEST_NAME).lstrip("/")
    import tempfile
    # mkstemp (not mktemp) creates the file atomically with an O_EXCL fd, closing
    # the TOCTOU window where an attacker could pre-plant a symlink at the path.
    fd, tmp = tempfile.mkstemp(prefix="fc_manifest_")
    os.close(fd)
    try:
        backend.download(key, tmp)
        with open(tmp, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        # Verify HMAC: an unsigned or tampered manifest is ignored, so a party
        # with write access to the bucket can't forge "already copied" entries
        # to make fast_copy silently skip uploading those files.
        stored_mac = data.pop("__hmac__", None)
        if stored_mac is None:
            return {}
        payload = json.dumps(data, sort_keys=True).encode()
        expected = _hmac_mod.new(_manifest_key(), payload, hashlib.sha256).hexdigest()
        if not _hmac_mod.compare_digest(stored_mac, expected):
            return {}
        return data
    except Exception:
        return {}
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _save_cloud_manifest(backend, prefix, manifest):
    key = (prefix.rstrip("/") + "/" + CLOUD_MANIFEST_NAME).lstrip("/")
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix="fc_manifest_")
    os.close(fd)
    try:
        # Sign with the same per-user HMAC key the SSH manifest uses.
        signed = dict(manifest)
        payload = json.dumps(signed, sort_keys=True).encode()
        signed["__hmac__"] = _hmac_mod.new(
            _manifest_key(), payload, hashlib.sha256).hexdigest()
        with open(tmp, "w") as f:
            json.dump(signed, f)
        backend.upload(tmp, key, {"fc_relpath": _quote_rel(CLOUD_MANIFEST_NAME)})
    except Exception as ex:
        print(f"  {C.YELLOW}Warning: could not write manifest: {ex}{C.RESET}")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _verify_uploads(backend, prefix, entries, hashes, sample=20):
    import random
    candidates = [e for e in entries if hashes.get(e.rel)]
    if not candidates:
        return
    sample_entries = random.sample(candidates, min(sample, len(candidates)))
    print(f"  {C.DIM}Verifying {len(sample_entries)} uploaded objects...{C.RESET}")
    bad = 0
    for e in sample_entries:
        key = backend.join_key(prefix, e.rel)
        meta = backend.head(key)
        if not meta or meta.get("fc_hash") != hashes.get(e.rel):
            bad += 1
            print(f"  {C.RED}VERIFY MISMATCH: {e.rel}{C.RESET}")
    if bad == 0:
        print(f"  {C.GREEN}✓ Verified {len(sample_entries)} objects OK{C.RESET}")


def _ssh_ls(target, overrides=None, cli_port=22, cli_key=None, cli_password=False):
    """List a remote directory over SFTP. `target` is a user@host:/path string.
    `overrides` (from a saved SSH profile) supplies port/key/password and takes
    precedence over the CLI fallbacks. Returns an exit code."""
    if not _has_paramiko:
        print(f"{C.RED}Error: SSH listing requires paramiko. "
              f"Install: python -m pip install paramiko{C.RESET}")
        return 1
    remote = parse_remote_path(target)
    if not remote:
        print(f"{C.RED}Error: {target!r} is not a valid user@host:/path.{C.RESET}")
        return 1
    ov = overrides or {}
    port = ov.get("port") or cli_port or 22
    key = ov.get("key") or cli_key
    password = ov.get("password")
    if not password and cli_password:
        password = getpass.getpass(
            f"  SSH password for {remote.user}@{remote.host}: ")
    ssh = SSHConnection(remote, port=port, key_path=key, password=password)
    where = f"{remote.user}@{remote.host}:{remote.path}"
    try:
        ssh.connect()
        sftp = ssh.open_sftp()
        # Stat first so we give an accurate message: a regular file is listed
        # like `ls file`, a missing path says so, only directories are walked.
        try:
            st = sftp.stat(remote.path)
        except IOError:
            print(f"{C.RED}Error: no such path: {where}{C.RESET}")
            ssh.close()
            return 1
        if not stat.S_ISDIR(st.st_mode):
            print(f"    {fmt_size(st.st_size or 0):>10}  {remote.path}")
            ssh.close()
            return 0
        entries = sftp.listdir_attr(remote.path)
    except SystemExit:
        raise
    except Exception as e:
        print(f"{C.RED}Error listing {where}: {e}{C.RESET}")
        ssh.close()
        return 1
    try:
        if not entries:
            print(f"  Empty directory: {where}")
            return 0
        # Directories first, then files, each sorted by name (like `ls`).
        entries.sort(key=lambda a: (0 if stat.S_ISDIR(a.st_mode) else 1, a.filename))
        files = sum(1 for a in entries if not stat.S_ISDIR(a.st_mode))
        total = sum(a.st_size or 0 for a in entries if not stat.S_ISDIR(a.st_mode))
        print(f"  Entries in {where}:")
        for a in entries:
            is_dir = stat.S_ISDIR(a.st_mode)
            shown = "<DIR>" if is_dir else fmt_size(a.st_size or 0)
            name = a.filename + ("/" if is_dir else "")
            print(f"    {shown:>10}  {name}")
        plural = "entry" if len(entries) == 1 else "entries"
        print(f"  {C.BOLD}{len(entries)} {plural}, {fmt_size(total)} "
              f"in {files} file(s){C.RESET}")
        return 0
    finally:
        ssh.close()


def cloud_ls(argv):
    """`fast_copy.py ls <connection[:folder] | scheme://bucket/prefix | user@host:/path>`
    — list objects in a cloud location, or files in a remote SSH directory, from
    the terminal. Returns an exit code."""
    if not argv or argv[0] in ("-h", "--help", "help"):
        print("Usage: fast_copy.py ls <connection[:folder] | s3://bucket/prefix | user@host:/path>\n"
              "  List objects under a cloud location, or files in a remote SSH directory.\n"
              "  Cloud: a saved cloud connection name, or an s3:// / az:// / gs:// URL.\n"
              "  SSH:   a saved ssh connection name, or a user@host:/path (listed via SFTP).\n"
              "  Examples:\n"
              "    fast_copy.py ls aws_fastcopies\n"
              "    fast_copy.py ls gcs_fastcopies:backup\n"
              "    fast_copy.py ls s3://bucket/prefix --credentials-file FILE\n"
              "    fast_copy.py ls user@host:/var/log --ssh-key ~/.ssh/id_ed25519\n"
              "  Encrypted creds: set FAST_COPY_CREDS_PASSPHRASE or run in a terminal.")
        return 0
    target, cred_file = None, None
    ssh_port, ssh_key, ssh_password = 22, None, False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--credentials-file", "--credentials"):
            cred_file = argv[i + 1] if i + 1 < len(argv) else None
            i += 2
            continue
        if a in ("--ssh-port", "--port"):
            val = argv[i + 1] if i + 1 < len(argv) else ""
            if not val.isdigit():
                print(f"{C.RED}Error: --ssh-port needs a number.{C.RESET}")
                return 1
            ssh_port = int(val)
            i += 2
            continue
        if a in ("--ssh-key", "--key"):
            ssh_key = argv[i + 1] if i + 1 < len(argv) else None
            i += 2
            continue
        if a in ("--ssh-password", "--password"):
            ssh_password = True
            i += 1
            continue
        if a == "--ssh-strict-host-key-checking":
            global _strict_host_keys
            _strict_host_keys = True
            i += 1
            continue
        if target is None and not a.startswith("-"):
            target = a
        i += 1
    if not target:
        print(f"{C.RED}Error: 'ls' needs a connection name, cloud URL, "
              f"or user@host:/path.{C.RESET}")
        return 1
    ns = argparse.Namespace(credentials_file=cred_file)
    spec = parse_cloud_url(target)
    if spec is None:
        # Only open/unlock the credentials file when the target actually looks
        # like a saved-profile name. A bare user@host:/path is listed directly
        # over SSH and must never trigger a creds-file passphrase prompt.
        if _looks_like_profile_ref(target):
            conns = _conns_for_named_endpoints(ns)
            new, overrides = resolve_named_endpoint(target, conns) if conns else (None, None)
            if new:
                cloud_spec = parse_cloud_url(new)
                if cloud_spec is not None:
                    spec = cloud_spec
                else:  # resolved to an SSH endpoint
                    return _ssh_ls(new, overrides, ssh_port, ssh_key, ssh_password)
        elif parse_remote_path(target):
            # A bare user@host:/path SSH target — no creds file involved.
            return _ssh_ls(target, None, ssh_port, ssh_key, ssh_password)
    if spec is None or spec.scheme not in ("s3", "az", "gs"):
        print(f"{C.RED}Error: {target!r} is not a cloud connection/URL "
              f"(s3://, az://, gs://) or an SSH path (user@host:/path).{C.RESET}")
        return 1
    try:
        backend = make_backend(spec, ns)
        objs = backend.list_objects(spec.prefix or "")
    except SystemExit:
        raise
    except Exception as e:
        print(f"{C.RED}Error listing {spec.scheme}://{spec.container}/"
              f"{spec.prefix or ''}: {e}{C.RESET}")
        return 1
    # Hide fast_copy's own manifest sidecar from the listing.
    keys = sorted(k for k in objs if not k.endswith(REMOTE_MANIFEST_NAME))
    where = f"{spec.scheme}://{spec.container}/{spec.prefix or ''}"
    if not keys:
        print(f"  No objects in {where}")
        return 0
    total = 0
    print(f"  Objects in {where}:")
    for k in keys:
        sz = objs[k].get("size", 0) or 0
        total += sz
        print(f"    {fmt_size(sz):>10}  {k}")
    print(f"  {C.BOLD}{len(keys)} object(s), {fmt_size(total)}{C.RESET}")
    return 0


# ════════════════════════════════════════════════════════════════════════════
# SSH-ONLY TRANSPORT (tar over the SSH exec channel — no SFTP subsystem)
# ════════════════════════════════════════════════════════════════════════════
# For servers that have SSH enabled but SFTP disabled (common on NAS appliances
# like Synology). This is a plain recursive copy — no dedup / incremental /
# verify — but it works wherever a shell + tar exist on the remote.

def _tar_ssh_connect(spec, key_path, password, compress):
    cli = paramiko.SSHClient()
    try:
        cli.load_system_host_keys()
    except Exception:
        pass
    kh = _user_known_hosts_path()
    if os.path.isfile(kh):
        try:
            cli.load_host_keys(kh)
        except Exception:
            pass
    cli.set_missing_host_key_policy(
        paramiko.RejectPolicy() if _strict_host_keys else _InteractiveHostKeyPolicy())
    kw = dict(hostname=spec.host, port=spec.port, username=spec.user,
              timeout=15, compress=compress)
    if key_path:
        kw["key_filename"] = os.path.expanduser(key_path)
    if password:
        kw["password"] = password
    cli.connect(**kw)
    tr = cli.get_transport()
    if tr:
        tr.set_keepalive(15)
    return cli


def _tar_emit(done, total, files_done, files_total, start, final=False):
    """Emit a progress event compatible with the --progress-json consumer."""
    el = time.time() - start
    sp = done / el if el > 0 else 0
    if PROGRESS_JSON:
        evt = {"t": "done" if final else "progress",
               "pct": round(min(100.0, done / total * 100) if total
                            else (100.0 if final else 0.0), 2),
               "bytes_done": done, "bytes_total": total or done, "speed_bps": sp,
               "files_done": files_done, "files_total": files_total}
        if final:
            evt["elapsed_s"] = el
        else:
            evt["eta_s"] = max(0.0, (total - done) / sp) if (total and sp > 0) else 0
        sys.stdout.write(json.dumps(evt) + "\n")
        sys.stdout.flush()
    elif total:
        pct = min(100.0, done / total * 100)
        sys.stdout.write(f"\r  {fmt_size(done)}/{fmt_size(total)}  {pct:5.1f}%  "
                         f"{fmt_speed(sp)}   ")
        sys.stdout.flush()


def _verify_emit(done, total):
    """Progress event for the verify phase — the GUI switches the bar/label to
    'Verifying…' and shows how many files have been verified."""
    if PROGRESS_JSON:
        sys.stdout.write(json.dumps({
            "t": "verify", "files_done": done, "files_total": total,
            "pct": round(min(100.0, done / total * 100) if total else 100.0, 2),
        }) + "\n")
        sys.stdout.flush()


def _remote_count_size(cli, rpath):
    files, size = 0, 0
    try:
        _i, o, _e = cli.exec_command(
            "find " + shlex.quote(rpath) + " -type f 2>/dev/null | wc -l")
        files = int((o.read().decode("utf-8", "replace").strip() or "0"))
    except Exception:
        pass
    try:
        _i, o, _e = cli.exec_command("du -sb " + shlex.quote(rpath) + " 2>/dev/null")
        if o.channel.recv_exit_status() == 0:
            size = int(o.read().split()[0])
        else:
            _i, o, _e = cli.exec_command("du -sk " + shlex.quote(rpath))
            size = int(o.read().split()[0]) * 1024
    except Exception:
        pass
    return files, size


def _local_tree_size(p):
    if os.path.isfile(p):
        try:
            return os.path.getsize(p)
        except OSError:
            return 0
    t = 0
    for r, _d, fs in os.walk(p):
        for f in fs:
            try:
                t += os.path.getsize(os.path.join(r, f))
            except OSError:
                pass
    return t


def _local_tree_files(p):
    if os.path.isfile(p):
        return 1
    return sum(len(fs) for _r, _d, fs in os.walk(p))


class _TarReadCounter:
    """Wraps a paramiko stdout stream, counting bytes + emitting progress."""

    def __init__(self, f, total, files_total, start):
        self.f, self.total, self.files_total, self.start = f, total, files_total, start
        self.n, self._last = 0, 0
        self.files_done = 0          # updated by _tar_extract_stream as files land

    def read(self, size):
        b = self.f.read(size)
        self.n += len(b)
        if self.n - self._last >= 2 * 1024 * 1024:
            self._last = self.n
            _tar_emit(self.n, self.total, self.files_done, self.files_total, self.start)
        return b


class _TarWriteCounter:
    def __init__(self, f, total, files_total, start):
        self.f, self.total, self.files_total, self.start = f, total, files_total, start
        self.n, self._last = 0, 0
        self.files_done = 0          # updated by the push loop as files are added

    def write(self, data):
        self.f.write(data)
        self.n += len(data)
        if self.n - self._last >= 2 * 1024 * 1024:
            self._last = self.n
            _tar_emit(self.n, self.total, self.files_done, self.files_total, self.start)
        return len(data)

    def flush(self):
        try:
            self.f.flush()
        except Exception:
            pass


def _tar_extract_stream(reader, dst):
    done = 0
    with tarfile.open(fileobj=reader, mode="r|") as tf:
        for m in tf:
            try:
                tf.extract(m, dst, filter="data")
            except TypeError:
                tf.extract(m, dst)
            if not m.isdir():
                done += 1
                # let the byte-progress emitter report the running file count
                if hasattr(reader, "files_done"):
                    reader.files_done = done
    return done


def _ssh_run(cli, cmd, timeout=1800):
    _i, o, e = cli.exec_command(cmd, timeout=timeout)
    out = o.read().decode("utf-8", "replace")
    err = e.read().decode("utf-8", "replace")
    return out, err, o.channel.recv_exit_status()


def _resolved_dst_pw(args):
    if args.ssh_password_env:
        return os.environ.get(args.ssh_password_env)
    return getattr(args, "_resolved_dst_password", None)


def _resolved_src_pw(args):
    if args.src_password_env:
        return os.environ.get(args.src_password_env)
    return getattr(args, "_resolved_src_password", None)


def _hash_local_file(path, algo, buf=4 * 1024 * 1024):
    # xxh128 isn't in hashlib — use the engine's xxhash hasher so local digests
    # match both the remote xxh128sum and the L2L block-order copy.
    h = new_hasher() if algo == "xxh128" else hashlib.new(algo)
    with open(path, "rb") as f:
        while True:
            b = f.read(buf)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _ssh_exec_caps(cli, dest_dir):
    """Detect what the remote can do over plain SSH: a hash tool, GNU find
    -printf, and (in dest_dir) hardlink/symlink support."""
    caps = {"hash": None, "halgo": None, "hardlink": False, "symlink": False,
            "find_printf": False}
    # Prefer the remote tool whose digest matches the LOCAL hash, so the dedup DB
    # is shared with L2L. xxh128sum is byte-identical to Python's xxhash; fall
    # back to sha256/md5 when the remote doesn't have it.
    candidates = [("sha256sum", "sha256"), ("md5sum", "md5")]
    if _hash_name == "xxh128":
        candidates = [("xxh128sum", "xxh128"), ("xxhsum -H2", "xxh128")] + candidates
    for tool, algo in candidates:
        o, _e, rc = _ssh_run(cli, "command -v " + shlex.quote(tool.split()[0]))
        if rc == 0 and o.strip():
            caps["hash"], caps["halgo"] = tool, algo
            break
    o, _e, rc = _ssh_run(cli, "cd / && find . -maxdepth 0 -printf x 2>/dev/null")
    caps["find_printf"] = (rc == 0 and "x" in o)
    q = shlex.quote(dest_dir)
    test = ("mkdir -p %s && cd %s && : > .fc_a 2>/dev/null && "
            "{ ln .fc_a .fc_h 2>/dev/null && echo HL; }; "
            "{ ln -s .fc_a .fc_s 2>/dev/null && echo SL; }; "
            "rm -f .fc_a .fc_h .fc_s") % (q, q)
    o, _e, _rc = _ssh_run(cli, test)
    toks = o.split()
    caps["hardlink"], caps["symlink"] = ("HL" in toks), ("SL" in toks)
    return caps


def _ssh_remote_listing(cli, root, caps):
    """{relpath: (size, mtime)} for every file under root (relpath has no ./)."""
    res = {}
    q = shlex.quote(root)
    if caps["find_printf"]:
        o, _e, rc = _ssh_run(
            cli, "cd %s 2>/dev/null && find . -type f -printf '%%s\\t%%T@\\t%%P\\n'" % q)
        if rc == 0:
            for line in o.splitlines():
                p = line.split("\t", 2)
                if len(p) == 3:
                    try:
                        res[p[2]] = (int(p[0]), float(p[1]))
                    except ValueError:
                        pass
            return res
    # portable fallback (busybox): find + wc -c (slower, no mtime)
    o, _e, rc = _ssh_run(
        cli, "cd %s 2>/dev/null && find . -type f | while IFS= read -r f; do "
             "printf '%%s\\t%%s\\n' \"$(wc -c < \"$f\")\" \"${f#./}\"; done" % q)
    if rc == 0:
        for line in o.splitlines():
            p = line.split("\t", 1)
            if len(p) == 2:
                try:
                    res[p[1]] = (int(p[0]), 0.0)
                except ValueError:
                    pass
    return res


def _ssh_remote_hashes(cli, root, rels, caps):
    """{relpath: hexdigest} computed on the remote via its hash tool."""
    res = {}
    if not caps["hash"] or not rels:
        return res
    q = shlex.quote(root)
    rels = list(rels)
    for i in range(0, len(rels), 100):
        files = " ".join(shlex.quote(r) for r in rels[i:i + 100])
        o, _e, _rc = _ssh_run(cli, "cd %s && %s %s 2>/dev/null" % (q, caps["hash"], files))
        for line in o.splitlines():
            m = re.match(r"([0-9a-fA-F]{8,})\s+\*?(.*)", line)
            if m:
                res[m.group(2)] = m.group(1).lower()
    return res


def _ssh_remote_free(cli, path):
    """(total, free) bytes for the filesystem holding `path` on the remote, via
    df. Walks up to an existing parent. Returns (None, None) if df is unusable."""
    check = path or "/"
    for _ in range(12):
        o, _e, rc = _ssh_run(
            cli, "df -kP %s 2>/dev/null || df -k %s 2>/dev/null"
            % (shlex.quote(check), shlex.quote(check)))
        lines = [l for l in o.strip().splitlines() if l.strip()]
        if rc == 0 and len(lines) >= 2:
            parts = lines[-1].split()
            try:
                return int(parts[1]) * 1024, int(parts[3]) * 1024
            except (IndexError, ValueError):
                return None, None
        parent = posixpath.dirname(check.rstrip("/"))
        if not parent or parent == check:
            break
        check = parent
    return None, None


def _print_space_block(total, free, required, force):
    """Shared Total/Free/Required/Headroom block (matches the SFTP/local modes)."""
    if total:
        print(f"  Destination disk:")
        print(f"    Total:     {C.BOLD}{fmt_size(total)}{C.RESET}")
        print(f"    Free:      {C.BOLD}{fmt_size(free)}{C.RESET} "
              f"({free / total * 100:.1f}% free)")
    else:
        print(f"  Destination free: {C.BOLD}{fmt_size(free)}{C.RESET}")
    print(f"    Required:  {C.BOLD}{fmt_size(required)}{C.RESET}")
    if required > free:
        print(f"\n  {C.RED}✗ NOT ENOUGH SPACE — need {fmt_size(required - free)} "
              f"more{C.RESET}")
        if force:
            print(f"  {C.YELLOW}Proceeding anyway (--force){C.RESET}")
            return True
        return False
    print(f"    Headroom:  {fmt_size(free - required)}")
    print(f"\n  {C.GREEN}✓ Enough space{C.RESET}")
    return True


def _check_space_remote(cli, path, required, force):
    total, free = _ssh_remote_free(cli, path)
    if free is None:
        print(f"  {C.YELLOW}Could not check destination free space — continuing{C.RESET}")
        return True
    return _print_space_block(total, free, required, force)


def _check_space_local(dst, required, force):
    try:
        target = dst if os.path.isdir(dst) else (os.path.dirname(dst) or ".")
        usage = shutil.disk_usage(target)
    except OSError:
        return True
    return _print_space_block(usage.total, usage.free, required, force)


def _ssh_tar_send(cli, cwd, rels):
    """Run `tar c` of an explicit file list on the remote, feeding the list via
    stdin (`-T -`) so it isn't limited by the SSH exec command-line length
    (thousands of files). Returns (stdout, stderr) streams for the archive."""
    stdin, out, err = cli.exec_command("cd %s && tar cf - -T -" % shlex.quote(cwd))
    data = ("\n".join(rels) + "\n").encode("utf-8")

    def _feed():
        try:
            stdin.write(data)
            stdin.flush()
        except Exception:
            pass
        finally:
            try:
                stdin.channel.shutdown_write()
            except Exception:
                pass

    threading.Thread(target=_feed, daemon=True).start()
    return out, err


def _ssh_done_summary(source, dest, total_files, nbytes, elapsed, verb,
                      copied=None, linked=0, skipped=0, saved=0, verified=None):
    """Standardized completion block (matches the SFTP/local modes), printed on
    every SSH-only transfer so the GUI and CLI show a consistent summary."""
    speed = nbytes / elapsed if elapsed > 0 else 0
    print()
    print(f"  Source:  {C.BOLD}{source}{C.RESET}")
    print(f"  Dest:    {C.BOLD}{dest}{C.RESET}")
    parts = []
    if copied is not None:
        parts.append(f"{copied} copied")
    if linked:
        parts.append(f"{linked} linked")
    if skipped:
        parts.append(f"{skipped} skipped")
    detail = f"  ({', '.join(parts)})" if parts else ""
    print(f"  Files:   {C.BOLD}{total_files}{C.RESET} total{detail}")
    sv = f"  ({fmt_size(saved)} saved by dedup)" if saved else ""
    print(f"  Data:    {C.BOLD}{fmt_size(nbytes)}{C.RESET} {verb}{sv}")
    print(f"  Time:    {C.BOLD}{fmt_time(elapsed)}{C.RESET}")
    print(f"  Speed:   {C.GREEN}{C.BOLD}{fmt_speed(speed)}{C.RESET}")
    if verified is not None:
        print(f"  Verify:  {C.GREEN}✓ passed{C.RESET}" if verified
              else f"  Verify:  {C.RED}✗ FAILED{C.RESET}")


def _local_dedup_db_path(dst):
    """Where a LOCAL destination's dedup DB lives — same place as the block-order
    copy: `.fast_copy_dedup.db` at the destination drive/mount root (fallback:
    inside the destination dir if the root isn't writable). So pull (R2L) shares
    the on-disk location convention with L2L, and the DB travels with the drive."""
    try:
        dst_root = os.path.realpath(dst)
        mount = _find_mount_point(dst_root)
        if mount == os.sep or not os.access(mount, os.W_OK):
            return os.path.join(dst_root, DEDUP_DB_NAME)
        return os.path.join(mount, DEDUP_DB_NAME)
    except Exception:
        return os.path.join(os.path.realpath(dst), DEDUP_DB_NAME)


def _local_listing(dst):
    """{relpath: (size, mtime)} for every file under a local directory."""
    res = {}
    for r, _d, fs in os.walk(dst):
        for f in fs:
            ap = os.path.join(r, f)
            rel = os.path.relpath(ap, dst).replace(os.sep, "/")
            try:
                st = os.stat(ap)
                res[rel] = (st.st_size, st.st_mtime)
            except OSError:
                pass
    return res


def _local_link_caps(dest):
    caps = {"hardlink": False, "symlink": False}
    try:
        os.makedirs(dest, exist_ok=True)
    except OSError:
        return caps
    a = os.path.join(dest, ".fc_a")
    b = os.path.join(dest, ".fc_h")
    s = os.path.join(dest, ".fc_s")
    try:
        open(a, "w").close()
        try:
            os.link(a, b)
            caps["hardlink"] = True
        except OSError:
            pass
        try:
            os.symlink(a, s)
            caps["symlink"] = True
        except OSError:
            pass
    except OSError:
        pass
    finally:
        for p in (a, b, s):
            try:
                os.remove(p)
            except OSError:
                pass
    return caps


def _dedup_groups(rels, size_of, hash_of):
    """Group by content. Only hashes files whose size collides (cheap).
    Returns (keep_rels, links) where links=[(dup_rel, target_rel)]."""
    bysize = {}
    for rel in rels:
        bysize.setdefault(size_of(rel), []).append(rel)
    keep, links, byhash = [], [], {}
    for rel in rels:
        sz = size_of(rel)
        if len(bysize[sz]) == 1 or sz == 0:
            keep.append(rel)
            continue
        h = hash_of(rel)
        if not h:
            keep.append(rel)
            continue
        if h in byhash:
            links.append((rel, byhash[h]))
        else:
            byhash[h] = rel
            keep.append(rel)
    return keep, links


SMALL_FILE_THRESHOLD = 1024 * 1024     # < 1 MB → bundled together


def _batch_rels(rels, size_of, max_bytes=TAR_CHUNK_SIZE, max_files=10000):
    """Split relpaths into ~max_bytes (default 100 MB) tar batches for the SSH
    transport. Small files (< 1 MB) are bundled together FIRST (so thousands of
    tiny files travel as one big tar instead of one-at-a-time), then the large
    files follow (a file ≥ max_bytes is its own batch). Per-batch streaming lets
    the file counter advance and a re-run resume at batch granularity."""
    smalls = [r for r in rels if size_of(r) < SMALL_FILE_THRESHOLD]
    larges = [r for r in rels if size_of(r) >= SMALL_FILE_THRESHOLD]

    def _pack(items):
        out, cur, cur_size = [], [], 0
        for r in items:
            sz = size_of(r)
            if cur and (cur_size + sz > max_bytes or len(cur) >= max_files):
                out.append(cur)
                cur, cur_size = [], 0
            cur.append(r)
            cur_size += sz
        if cur:
            out.append(cur)
        return out

    return _pack(smalls) + _pack(larges)


def _fc_config_dir():
    if _system == "Windows":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "fast_copy")
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    return os.path.join(xdg, "fast_copy")


class SshDedupCache:
    """Persistent, per-destination hash cache for SSH transfers, kept LOCALLY
    (no DB needed on the remote). Maps a destination file's content hash → its
    path, so a new file matching one ALREADY on the destination is linked
    instead of re-copied. Cached hashes are reused while a file's size+mtime are
    unchanged, so repeat runs only hash genuinely new remote files."""

    def __init__(self, host, dest_path, db_path=None):
        if db_path is None:
            # remote destinations → keep the DB locally (no SQLite needed on the NAS)
            d = os.path.join(_fc_config_dir(), "dedup")
            try:
                os.makedirs(d, exist_ok=True)
            except OSError:
                pass
            key = hashlib.sha1(("%s\x00%s" % (host, dest_path)).encode("utf-8")).hexdigest()[:16]
            db_path = os.path.join(d, "ssh-%s.db" % key)
        self.path = db_path
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("CREATE TABLE IF NOT EXISTS dest "
                          "(rel TEXT PRIMARY KEY, size INTEGER, mtime REAL, hash TEXT)")
        self.conn.commit()

    def build_index(self, listing, cand_sizes, hash_fn):
        """Return {hash: rel} for destination files whose size is a dedup
        candidate. Reuses cached hashes for unchanged files; hashes only new
        ones via hash_fn(rels)->{rel:hash}. Prunes vanished files."""
        cached = {r[0]: (r[1], r[2], r[3])
                  for r in self.conn.execute("SELECT rel,size,mtime,hash FROM dest")}
        index, need = {}, []
        for rel, (sz, mt) in listing.items():
            if sz not in cand_sizes:
                continue
            c = cached.get(rel)         # (size, mtime, hash)
            if c and c[0] == sz and c[2] and (mt == 0.0 or c[1] == 0.0 or abs(c[1] - mt) <= 2):
                index.setdefault(c[2], rel)
            else:
                need.append(rel)
        if need:
            for rel, h in (hash_fn(need) or {}).items():
                if h:
                    index.setdefault(h, rel)
                    sz, mt = listing[rel]
                    self.conn.execute("INSERT OR REPLACE INTO dest VALUES (?,?,?,?)",
                                      (rel, sz, mt, h))
            self.conn.commit()
        gone = [r for r in cached if r not in listing]
        if gone:
            self.conn.executemany("DELETE FROM dest WHERE rel=?", [(r,) for r in gone])
            self.conn.commit()
            print(f"  Cleaned {C.BOLD}{len(gone)}{C.RESET} stale dedup-DB "
                  f"entr{'y' if len(gone) == 1 else 'ies'} (deleted from destination)")
        return index

    def record(self, rel, size, mtime, h):
        self.conn.execute("INSERT OR REPLACE INTO dest VALUES (?,?,?,?)",
                          (rel, size, mtime, h))

    def close(self):
        try:
            self.conn.commit()
            self.conn.close()
        except Exception:
            pass


def _ssh_dedup_plan(rels, size_of, hash_many, rmap, can_link, algo,
                    cache_factory, dest_hash_fn):
    """Shared dedup planner for push/r2r. Returns (copy_rels, links, saved,
    cache, copy_hashes). Dedups within the transfer AND against files already on
    the destination (via a local hash cache). hash_many(rels)->{rel:hash} hashes
    the SOURCE candidates in one batch."""
    links, saved, cache, copy_hashes = [], 0, None, {}
    if not algo:
        return rels, links, saved, cache, copy_hashes
    # Always open the cache so the DB is created and PRUNED every run (build_index
    # drops entries for files no longer on the destination → self-cleanup).
    cache = cache_factory()
    ssize = {}
    for r in rels:
        ssize[size_of(r)] = ssize.get(size_of(r), 0) + 1
    dest_sizes = set(sz for sz, _m in rmap.values())
    cand = (set(s for s in ssize if (ssize[s] > 1 or s in dest_sizes) and s > 0)
            if (can_link and rels) else set())
    dest_index = cache.build_index(rmap, cand, dest_hash_fn)   # also prunes
    if not cand:
        return rels, links, saved, cache, copy_hashes
    cand_rels = [r for r in rels if size_of(r) in cand]
    shashes = hash_many(cand_rels) or {}
    seen = dict(dest_index)
    copy_rels = []
    for r in rels:
        if size_of(r) in cand:
            h = shashes.get(r)
            if h and h in seen:
                links.append((r, seen[h]))
                saved += size_of(r)
            else:
                if h:
                    seen[h] = r
                    copy_hashes[r] = h
                copy_rels.append(r)
        else:
            copy_rels.append(r)
    return copy_rels, links, saved, cache, copy_hashes


def _ssh_push_smart(dst_remote, dst, local_srcs, args, start):
    """local → remote with incremental skip, dedup (links honor the remote FS,
    incl. files already on the destination via a cached hash DB), and post-copy
    verify — all over plain SSH (no SFTP)."""
    dcli = _tar_ssh_connect(dst_remote, args.ssh_key, _resolved_dst_pw(args), args.compress)
    caps = _ssh_exec_caps(dcli, dst)
    algo = caps["halgo"] or "sha256"
    link_kind = "hardlink" if caps["hardlink"] else ("symlink" if caps["symlink"] else "none")
    banner("SSH (no SFTP) — local → remote  [dedup · incremental · verify]")
    print(f"  dest FS links: {link_kind} · hash: {caps['hash'] or 'none'}")

    banner("Phase 1 — Scanning source")
    # 1) enumerate local files: arcrel = <basename>/<relpath>
    files, ap_by_rel, size_by_rel = [], {}, {}
    for s in (local_srcs or []):
        s = os.path.abspath(s)
        if not os.path.exists(s):
            print(f"{C.RED}Error: source not found: {s}{C.RESET}")
            dcli.close()
            return 1
        base = os.path.basename(s.rstrip("/\\")) or s
        if os.path.isfile(s):
            st = os.stat(s)
            files.append((base, st.st_size, st.st_mtime))
            ap_by_rel[base], size_by_rel[base] = s, st.st_size
        else:
            for r, _d, fs in os.walk(s):
                for f in fs:
                    ap = os.path.join(r, f)
                    rel = base + "/" + os.path.relpath(ap, s).replace(os.sep, "/")
                    try:
                        st = os.stat(ap)
                    except OSError:
                        continue
                    files.append((rel, st.st_size, st.st_mtime))
                    ap_by_rel[rel], size_by_rel[rel] = ap, st.st_size

    mt_by_rel = {rel: mt for rel, sz, mt in files}
    print(f"  Found {C.BOLD}{len(files)}{C.RESET} local files  "
          f"({fmt_size(sum(size_by_rel.values()))})")

    # destination listing (used for both incremental and dedup-against-existing)
    rmap = _ssh_remote_listing(dcli, dst, caps)

    # incremental: skip files already present with matching size (+mtime)
    skipped = []
    rels = [f[0] for f in files]
    if not args.overwrite:
        keep = []
        for rel in rels:
            r = rmap.get(rel)
            if r and r[0] == size_by_rel[rel] and (r[1] == 0.0 or abs(r[1] - mt_by_rel[rel]) <= 2):
                skipped.append(rel)
            else:
                keep.append(rel)
        rels = keep

    # ── Phase 2 — Deduplication (within transfer + against files on the dest) ──
    banner("Phase 2 — Deduplication")
    can_link = caps["hardlink"] or caps["symlink"]
    cache = copy_hashes = None
    links, saved = [], 0
    if not args.no_dedup:
        rels, links, saved, cache, copy_hashes = _ssh_dedup_plan(
            rels, lambda r: size_by_rel[r],
            lambda rl: {r: _hash_local_file(ap_by_rel[r], algo) for r in rl},
            rmap, can_link, algo if caps["hash"] else None,
            lambda: SshDedupCache(dst_remote.host, dst),
            lambda rl: _ssh_remote_hashes(dcli, dst, rl, caps))
    print(f"  Unique files:    {C.BOLD}{len(rels)}{C.RESET}")
    print(f"  Duplicates:      {C.BOLD}{len(links)}{C.RESET}  (dest FS: {link_kind})")
    print(f"  Space saved:     {C.BOLD}{fmt_size(saved)}{C.RESET}")
    if skipped:
        print(f"  Already present: {C.BOLD}{len(skipped)}{C.RESET} (skipped)")

    if args.dry_run:
        if cache:
            cache.close()
        print(f"\n  {C.YELLOW}(dry run — would copy {len(rels)}, link {len(links)}, "
              f"skip {len(skipped)}){C.RESET}")
        dcli.close()
        return 0

    total = sum(size_by_rel[r] for r in rels) or 1

    # ── Phase 3 — Space check (default; override with --force) ──
    banner("Phase 3 — Space check")
    if rels and not _check_space_remote(dcli, dst, sum(size_by_rel[r] for r in rels), args.force):
        if cache:
            cache.close()
        dcli.close()
        return 1

    # ── Phase 4 — Local-to-remote copy ──
    banner("Phase 4 — Local-to-remote copy")
    print(f"  Strategy: tar stream for {len(rels)} files ({fmt_size(total)})\n")
    if rels:
        q = shlex.quote(dst)
        di, do, de = dcli.exec_command("mkdir -p %s && cd %s && tar xpf -" % (q, q))
        writer = _TarWriteCounter(di, total, len(rels), start)
        with tarfile.open(fileobj=writer, mode="w|") as tf:
            for i, rel in enumerate(rels):
                try:
                    tf.add(ap_by_rel[rel], arcname=rel, recursive=False)
                except OSError:
                    pass
                writer.files_done = i + 1
        di.channel.shutdown_write()
        rc = do.channel.recv_exit_status()
        if rc:
            print(f"{C.RED}Error: "
                  f"{de.read().decode('utf-8','replace').strip() or 'remote tar failed'}{C.RESET}")
            dcli.close()
            return 1

    # 5) create dedup links on the remote, honoring its FS capability
    if links:
        cmds = []
        for dup, target in links:
            dd = posixpath.dirname(dup)
            if dd:
                cmds.append("mkdir -p " + shlex.quote(dd))
            if caps["hardlink"]:
                cmds.append("ln -f %s %s" % (shlex.quote(target), shlex.quote(dup)))
            else:
                rt = posixpath.relpath(target, dd or ".")
                cmds.append("ln -sf %s %s" % (shlex.quote(rt), shlex.quote(dup)))
        _o, _e, rc = _ssh_run(dcli, "cd %s && " % shlex.quote(dst) + " && ".join(cmds))
        if rc:
            print(f"  {C.YELLOW}Warning: some dedup links could not be created{C.RESET}")

    # 6) verify copied + linked files by hashing both sides
    verified = True
    vhashes = {}
    if not args.no_verify and caps["hash"]:
        check = list(rels) + [d for d, _t in links]
        banner("Verifying")
        rh = _ssh_remote_hashes(dcli, dst, check, caps)
        bad = 0
        for k, rel in enumerate(check):
            lh = _hash_local_file(ap_by_rel[rel], algo) if rel in ap_by_rel else None
            if lh:
                vhashes[rel] = lh                     # reuse for the dedup DB
            if lh and rh.get(rel) and lh != rh[rel]:
                bad += 1
                print(f"  {C.RED}✗ verify mismatch: {rel}{C.RESET}")
            if (k + 1) % 16 == 0 or k + 1 == len(check):
                _verify_emit(k + 1, len(check))
        verified = (bad == 0)
        if verified:
            print(f"  {C.GREEN}✓ Verified {len(check)} file(s){C.RESET}")

    # 7) record EVERY copied file in the dedup DB (complete index of the dest),
    #    reusing the verify hashes; the next run dedups against all of it.
    if cache is None and not args.no_dedup and caps["hash"]:
        cache = SshDedupCache(dst_remote.host, dst)
    if cache is not None:
        all_h = dict(copy_hashes or {})
        all_h.update(vhashes)
        for rel in rels:
            h = all_h.get(rel) or (_hash_local_file(ap_by_rel[rel], algo)
                                   if rel in ap_by_rel else None)
            if h:
                cache.record(rel, size_by_rel[rel], mt_by_rel[rel], h)
        cache.close()

    _tar_emit(total, total, len(rels) + len(links), len(files), start, final=True)
    src_disp = (os.path.abspath(local_srcs[0]) if local_srcs and len(local_srcs) == 1
                else f"{len(local_srcs or [])} sources")
    _ssh_done_summary(
        src_disp, f"{dst_remote.user}@{dst_remote.host}:{dst}", len(files),
        sum(size_by_rel[r] for r in rels), time.time() - start, "uploaded",
        copied=len(rels), linked=len(links), skipped=len(skipped), saved=saved,
        verified=(verified if (not args.no_verify and caps["hash"]) else None))
    dcli.close()
    return 0 if verified else 1


def _ssh_pull_smart(src_remote, dst, args, start):
    """remote → local with incremental skip, dedup (links honor the LOCAL dest
    FS), and verify — all over plain SSH (no SFTP)."""
    scli = _tar_ssh_connect(src_remote, args.src_key, _resolved_src_pw(args), args.compress)
    rpath = src_remote.path.rstrip("/") or "/"
    parent = posixpath.dirname(rpath) or "/"
    base = posixpath.basename(rpath) or rpath
    caps = _ssh_exec_caps(scli, "/tmp")          # source-side hash/find caps
    lcaps = _local_link_caps(dst)                 # dest FS = local
    algo = caps["halgo"] or "sha256"
    link_kind = "hardlink" if lcaps["hardlink"] else ("symlink" if lcaps["symlink"] else "none")
    banner("SSH (no SFTP) — remote → local  [dedup · incremental · verify]")
    print(f"  dest FS links: {link_kind} · hash: {caps['hash'] or 'none'}")

    banner("Phase 1 — Scanning source")
    rmap = _ssh_remote_listing(scli, parent, caps)
    src_files = {rel: v for rel, v in rmap.items()
                 if rel == base or rel.startswith(base + "/")}
    if not src_files:
        print(f"{C.RED}Error: nothing to copy at {src_remote.host}:{rpath} "
              f"(empty, missing, or not a directory).{C.RESET}")
        scli.close()
        return 1
    _scan_bytes = sum(v[0] for v in src_files.values())
    print(f"  Found {C.BOLD}{len(src_files)}{C.RESET} files on remote  "
          f"({fmt_size(_scan_bytes)})")

    # incremental: skip files already present locally with matching size (+mtime)
    skipped = set()
    if not args.overwrite:
        for rel, (sz, mt) in src_files.items():
            lp = os.path.join(dst, rel.replace("/", os.sep))
            if os.path.isfile(lp):
                try:
                    lst = os.stat(lp)
                    if lst.st_size == sz and (mt == 0.0 or abs(lst.st_mtime - mt) <= 2):
                        skipped.add(rel)
                except OSError:
                    pass
    rels = [r for r in src_files if r not in skipped]

    # ── Phase 2 — Deduplication (within transfer + against existing LOCAL dest,
    #    via the cached hash DB). Hash on the remote source; link on the local FS.
    banner("Phase 2 — Deduplication")
    can_link = lcaps["hardlink"] or lcaps["symlink"]
    cache = ddb = None
    copy_hashes = {}
    links, saved = [], 0          # links: (dup_rel, ABSOLUTE target path)
    # When the remote hash equals the local hash algo (xxh128 via xxh128sum), share
    # the exact same DedupDB/dest_files index as L2L; otherwise fall back to the
    # local sha256 cache (separate state).
    shared = bool(caps["hash"]) and caps["halgo"] == _hash_name
    if not args.no_dedup and caps["hash"]:
        local_dest = _local_listing(dst)
        if shared and can_link:
            try:
                ddb = DedupDB(os.path.abspath(dst))
            except Exception:
                ddb = None
        if ddb is not None:
            ncleaned = ddb.prune_dest(local_dest.keys())     # self-cleanup
            if ncleaned:
                print(f"  Cleaned {C.BOLD}{ncleaned}{C.RESET} stale dedup-DB "
                      f"entr{'y' if ncleaned == 1 else 'ies'} (deleted from destination)")
            # candidate if its size collides within the source OR matches ANY file
            # already recorded on the drive (the shared DB spans all folders)
            known_sizes = set(sz for sz, _m in local_dest.values()) | ddb.dest_sizes()
            bysize = {}
            for r in rels:
                bysize.setdefault(src_files[r][0], []).append(r)
            cand = [r for r in rels if src_files[r][0] > 0
                    and (len(bysize[src_files[r][0]]) > 1 or src_files[r][0] in known_sizes)]
            shashes = _ssh_remote_hashes(scli, parent, cand, caps)
            seen, drop = {}, set()
            for r in cand:
                h = shashes.get(r)
                if not h:
                    continue
                ex = ddb.lookup_by_hash(h)            # already on this drive (any folder)?
                abs_t = (os.path.join(ddb.mount, ex[0][0].replace("/", os.sep))
                         if ex else None)
                if abs_t and os.path.isfile(abs_t):
                    links.append((r, abs_t)); drop.add(r); saved += src_files[r][0]
                elif h in seen:
                    links.append((r, os.path.join(dst, seen[h].replace("/", os.sep))))
                    drop.add(r); saved += src_files[r][0]
                else:
                    seen[h] = r; copy_hashes[r] = h
            rels = [r for r in rels if r not in drop]
        else:
            rels, links2, saved, cache, copy_hashes = _ssh_dedup_plan(
                rels, lambda r: src_files[r][0],
                lambda rl: _ssh_remote_hashes(scli, parent, rl, caps),
                local_dest, can_link, algo,
                lambda: SshDedupCache("local", dst, db_path=_local_dedup_db_path(dst)),
                lambda rl: {r: _hash_local_file(os.path.join(dst, r.replace("/", os.sep)), algo)
                            for r in rl})
            links = [(d, os.path.join(dst, t.replace("/", os.sep))) for d, t in links2]
    print(f"  Unique files:    {C.BOLD}{len(rels)}{C.RESET}")
    print(f"  Duplicates:      {C.BOLD}{len(links)}{C.RESET}  "
          f"(dest FS: {link_kind})")
    print(f"  Space saved:     {C.BOLD}{fmt_size(saved)}{C.RESET}")
    if skipped:
        print(f"  Already present: {C.BOLD}{len(skipped)}{C.RESET} (skipped)")

    if args.dry_run:
        print(f"\n  {C.YELLOW}(dry run — would copy {len(rels)}, link {len(links)}, "
              f"skip {len(skipped)}){C.RESET}")
        scli.close()
        return 0

    os.makedirs(dst, exist_ok=True)
    total = sum(src_files[r][0] for r in rels) or 1

    # ── Phase 3 — Space check (default; override with --force) ──
    banner("Phase 3 — Space check")
    if rels and not _check_space_local(dst, sum(src_files[r][0] for r in rels), args.force):
        scli.close()
        return 1

    # ── Phase 4 — Remote-to-local copy ──
    banner("Phase 4 — Remote-to-local copy")
    print(f"  Strategy: tar stream for {len(rels)} files ({fmt_size(total)})\n")
    if rels:
        so, se = _ssh_tar_send(scli, parent, rels)
        reader = _TarReadCounter(so, total, len(rels), start)
        _tar_extract_stream(reader, dst)
        rc = so.channel.recv_exit_status()
        if rc:
            print(f"{C.RED}Error: "
                  f"{se.read().decode('utf-8','replace').strip() or 'remote tar failed'}{C.RESET}")
            scli.close()
            return 1

    # local dedup links honoring the local FS (target is an ABSOLUTE path)
    for dup, tp in links:
        dp = os.path.join(dst, dup.replace("/", os.sep))
        try:
            os.makedirs(os.path.dirname(dp), exist_ok=True)
            if os.path.lexists(dp):
                os.remove(dp)
            if lcaps["hardlink"]:
                os.link(tp, dp)
            else:
                os.symlink(os.path.relpath(tp, os.path.dirname(dp)), dp)
        except OSError:
            pass

    # verify: hash local copies against remote hashes
    verified = True
    vhashes = {}
    if not args.no_verify and caps["hash"]:
        check = list(rels) + [d for d, _t in links]
        banner("Verifying")
        rh = _ssh_remote_hashes(scli, parent, check, caps)
        bad = 0
        for k, rel in enumerate(check):
            lp = os.path.join(dst, rel.replace("/", os.sep))
            try:
                lh = _hash_local_file(lp, algo)
            except OSError:
                lh = None
            if lh:
                vhashes[rel] = lh
            if lh and rh.get(rel) and lh != rh[rel]:
                bad += 1
                print(f"  {C.RED}✗ verify mismatch: {rel}{C.RESET}")
            if (k + 1) % 16 == 0 or k + 1 == len(check):
                _verify_emit(k + 1, len(check))
        verified = (bad == 0)
        if verified:
            print(f"  {C.GREEN}✓ Verified {len(check)} file(s){C.RESET}")

    # record EVERY copied file in the dedup DB (complete dest index, xxh128)
    all_h = dict(copy_hashes or {})
    all_h.update(vhashes)

    def _rel_hash(rel):
        lp = os.path.join(dst, rel.replace("/", os.sep))
        return all_h.get(rel) or (_hash_local_file(lp, algo)
                                  if os.path.isfile(lp) else None)

    if ddb is not None:
        rows = [(rel, src_files[rel][0], h) for rel in rels
                for h in (_rel_hash(rel),) if h]
        if rows:
            ddb.store_dest_batch(rows)         # shared dest_files index with L2L
        ddb.close()
    else:
        if cache is None and not args.no_dedup and caps["hash"]:
            cache = SshDedupCache("local", dst, db_path=_local_dedup_db_path(dst))
        if cache is not None:
            for rel in rels:
                h = _rel_hash(rel)
                if h:
                    cache.record(rel, src_files[rel][0], src_files[rel][1], h)
            cache.close()

    _tar_emit(total, total, len(rels) + len(links), len(src_files), start, final=True)
    _ssh_done_summary(
        f"{src_remote.user}@{src_remote.host}:{rpath}", os.path.abspath(dst),
        len(src_files), sum(src_files[r][0] for r in rels), time.time() - start,
        "downloaded", copied=len(rels), linked=len(links), skipped=len(skipped),
        saved=saved, verified=(verified if (not args.no_verify and caps["hash"]) else None))
    scli.close()
    return 0 if verified else 1


def _ssh_r2r_smart(src_remote, dst_remote, dst, args, start):
    """remote → remote with incremental skip, dedup (links honor the DESTINATION
    remote's FS) and verify (both remotes hashed) — all over plain SSH."""
    scli = _tar_ssh_connect(src_remote, args.src_key, _resolved_src_pw(args), args.compress)
    dcli = _tar_ssh_connect(dst_remote, args.ssh_key, _resolved_dst_pw(args), args.compress)
    rpath = src_remote.path.rstrip("/") or "/"
    parent = posixpath.dirname(rpath) or "/"
    base = posixpath.basename(rpath) or rpath
    scaps = _ssh_exec_caps(scli, "/tmp")        # source: hash/find
    dcaps = _ssh_exec_caps(dcli, dst)            # dest: link support (+hash/find)
    # a hash algorithm both ends share (needed for dedup + verify)
    algo = scaps["halgo"] if (scaps["halgo"] and scaps["halgo"] == dcaps["halgo"]) else None
    link_kind = "hardlink" if dcaps["hardlink"] else ("symlink" if dcaps["symlink"] else "none")
    banner("SSH (no SFTP) — remote → remote  [dedup · incremental · verify]")
    print(f"  dest FS links: {link_kind} · hash: {algo or 'none (no common tool)'}")

    banner("Phase 1 — Scanning source")
    smap = _ssh_remote_listing(scli, parent, scaps)
    src_files = {rel: v for rel, v in smap.items()
                 if rel == base or rel.startswith(base + "/")}
    if not src_files:
        print(f"{C.RED}Error: nothing to copy at {src_remote.host}:{rpath}.{C.RESET}")
        scli.close()
        dcli.close()
        return 1
    print(f"  Found {C.BOLD}{len(src_files)}{C.RESET} files on source  "
          f"({fmt_size(sum(v[0] for v in src_files.values()))})")

    # destination listing (incremental + dedup-against-existing)
    dmap = _ssh_remote_listing(dcli, dst, dcaps)

    # incremental against the destination
    skipped = set()
    if not args.overwrite:
        for rel, (sz, mt) in src_files.items():
            d = dmap.get(rel)
            if d and d[0] == sz and (mt == 0.0 or d[1] == 0.0 or abs(d[1] - mt) <= 2):
                skipped.add(rel)
    rels = [r for r in src_files if r not in skipped]

    # ── Phase 2 — Deduplication (hash on source; link on dest per its FS) ──
    banner("Phase 2 — Deduplication")
    can_link = dcaps["hardlink"] or dcaps["symlink"]
    cache = copy_hashes = None
    links, saved = [], 0
    if not args.no_dedup:
        rels, links, saved, cache, copy_hashes = _ssh_dedup_plan(
            rels, lambda r: src_files[r][0],
            lambda rl: _ssh_remote_hashes(scli, parent, rl, scaps),
            dmap, can_link, algo,
            lambda: SshDedupCache(dst_remote.host, dst),
            lambda rl: _ssh_remote_hashes(dcli, dst, rl, dcaps))
    print(f"  Unique files:    {C.BOLD}{len(rels)}{C.RESET}")
    print(f"  Duplicates:      {C.BOLD}{len(links)}{C.RESET}  (dest FS: {link_kind})")
    print(f"  Space saved:     {C.BOLD}{fmt_size(saved)}{C.RESET}")
    if skipped:
        print(f"  Already present: {C.BOLD}{len(skipped)}{C.RESET} (skipped)")

    if args.dry_run:
        if cache:
            cache.close()
        print(f"\n  {C.YELLOW}(dry run — would copy {len(rels)}, link {len(links)}, "
              f"skip {len(skipped)}){C.RESET}")
        scli.close()
        dcli.close()
        return 0

    # ── Phase 3 — Space check (default; override with --force) ──
    banner("Phase 3 — Space check")
    if rels and not _check_space_remote(dcli, dst, sum(src_files[r][0] for r in rels), args.force):
        if cache:
            cache.close()
        scli.close()
        dcli.close()
        return 1

    # ── Phase 4 — Remote-to-remote copy (in tar chunks) ──
    banner("Phase 4 — Remote-to-remote copy")
    total = sum(src_files[r][0] for r in rels) or 1
    done_bytes = done_files = last = 0
    chunk_mb = max(1, getattr(args, "chunk_size", 100))
    chunk_bytes = chunk_mb * 1024 * 1024
    batches = _batch_rels(rels, lambda r: src_files[r][0], max_bytes=chunk_bytes)
    print(f"  Strategy: tar stream for {len(rels)} files ({fmt_size(total)}) "
          f"in {len(batches)} chunk(s) of ~{chunk_mb} MB\n")
    for bi, batch in enumerate(batches):
        so, se = _ssh_tar_send(scli, parent, batch)
        di, do, de = dcli.exec_command(
            "mkdir -p %s && cd %s && tar xpf -" % (shlex.quote(dst), shlex.quote(dst)))
        while True:
            b = so.read(131072)
            if not b:
                break
            di.channel.sendall(b)
            done_bytes += len(b)
            if done_bytes - last >= 2 * 1024 * 1024:
                last = done_bytes
                _tar_emit(done_bytes, total, done_files, len(rels), start)
        di.channel.shutdown_write()
        rcs, rcd = so.channel.recv_exit_status(), do.channel.recv_exit_status()
        if rcs or rcd:
            err = (se.read() + de.read()).decode("utf-8", "replace").strip()
            print(f"{C.RED}Error: {err or 'remote tar failed'}{C.RESET}")
            scli.close()
            dcli.close()
            return 1
        done_files += len(batch)
        _tar_emit(done_bytes, total, done_files, len(rels), start)
        print(f"  chunk {bi + 1}/{len(batches)} done "
              f"({done_files}/{len(rels)} files, {fmt_size(done_bytes)})")

    # dedup links on the destination remote, honoring its FS
    if links:
        cmds = []
        for dup, target in links:
            dd = posixpath.dirname(dup)
            if dd:
                cmds.append("mkdir -p " + shlex.quote(dd))
            if dcaps["hardlink"]:
                cmds.append("ln -f %s %s" % (shlex.quote(target), shlex.quote(dup)))
            else:
                rt = posixpath.relpath(target, dd or ".")
                cmds.append("ln -sf %s %s" % (shlex.quote(rt), shlex.quote(dup)))
        _o, _e, rc = _ssh_run(dcli, "cd %s && " % shlex.quote(dst) + " && ".join(cmds))
        if rc:
            print(f"  {C.YELLOW}Warning: some dedup links could not be created{C.RESET}")

    # verify: hash the same files on BOTH remotes and compare
    verified = True
    vhashes = {}
    if not args.no_verify and algo:
        check = list(rels) + [d for d, _t in links]
        banner("Verifying")
        sh = _ssh_remote_hashes(scli, parent, check, scaps)
        dh = _ssh_remote_hashes(dcli, dst, check, dcaps)
        vhashes = dict(sh)                            # source == dest content
        bad = 0
        for k, rel in enumerate(check):
            if sh.get(rel) and dh.get(rel) and sh[rel] != dh[rel]:
                bad += 1
                print(f"  {C.RED}✗ verify mismatch: {rel}{C.RESET}")
            if (k + 1) % 16 == 0 or k + 1 == len(check):
                _verify_emit(k + 1, len(check))
        verified = (bad == 0)
        if verified:
            print(f"  {C.GREEN}✓ Verified {len(check)} file(s){C.RESET}")

    # record EVERY copied file in the dedup DB (complete index of the dest)
    if cache is None and not args.no_dedup and algo:
        cache = SshDedupCache(dst_remote.host, dst)
    if cache is not None:
        all_h = dict(copy_hashes or {})
        all_h.update(vhashes)
        for rel in rels:
            h = all_h.get(rel)
            if h:
                cache.record(rel, src_files[rel][0], src_files[rel][1], h)
        cache.close()

    _tar_emit(total, total, len(rels) + len(links), len(src_files), start, final=True)
    _ssh_done_summary(
        f"{src_remote.user}@{src_remote.host}:{rpath}",
        f"{dst_remote.user}@{dst_remote.host}:{dst}", len(src_files),
        sum(src_files[r][0] for r in rels), time.time() - start, "transferred",
        copied=len(rels), linked=len(links), skipped=len(skipped), saved=saved,
        verified=(verified if (not args.no_verify and algo) else None))
    scli.close()
    dcli.close()
    return 0 if verified else 1


def copy_via_tar_ssh(src_remote, dst_remote, dst, local_srcs, args):
    """SSH-only copy via tar over the exec channel (no SFTP). Returns exit code.
    Handles remote→local (pull), local→remote (push), remote→remote (r2r)."""
    if not _has_paramiko:
        print(f"{C.RED}Error: SSH transfers require paramiko.{C.RESET}")
        return 1
    start = time.time()

    try:
        if src_remote and dst_remote:
            return _ssh_r2r_smart(src_remote, dst_remote, dst, args, start)
        elif src_remote:
            return _ssh_pull_smart(src_remote, dst, args, start)
        elif dst_remote:
            return _ssh_push_smart(dst_remote, dst, local_srcs, args, start)
        print(f"{C.RED}Error: --ssh-no-sftp needs a remote source or destination.{C.RESET}")
        return 1
    except Exception as e:
        msg = str(e).strip().splitlines()[0] if str(e).strip() else e.__class__.__name__
        print(f"{C.RED}Error: {msg}{C.RESET}")
        return 1


# ════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════
def main():
    # Capture the creds passphrase out of the environment before we spawn any
    # child process (chattr/setfacl/getfacl/ACL helpers), so the secret is never
    # inherited into their environments. See _scrub_passphrase_env.
    _scrub_passphrase_env()
    parser = argparse.ArgumentParser(
        prog="fast_copy.py",
        description="Block-order fast copy with dedup — reads files in physical "
                    "disk order, deduplicates identical files. Copies between "
                    "local paths, SSH hosts (user@host:/path), and cloud object "
                    "storage (s3:// / az:// / gs://).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            subcommands:
              creds <sub>      Manage saved cloud/SSH connection profiles in the
                               credentials file: add, list, edit, remove, test,
                               encrypt/decrypt, lock. Run 'fast_copy.py creds' for help.
              ls <target>      List a cloud location (saved connection name or
                               s3:// / az:// / gs:// URL) or a remote SSH directory
                               (saved ssh connection or user@host:/path, via SFTP).
                               Alias: list-objects. Run 'fast_copy.py ls -h' for help.

            examples:
              fast_copy.py /data /media/usb/data           local  → local
              fast_copy.py /data user@host:/backup         local  → remote (SSH)
              fast_copy.py user@host:/data /local/backup   remote → local
              fast_copy.py /data s3://bucket/backup        local  → cloud
              fast_copy.py creds add aws-dev               save a connection profile
              fast_copy.py ls aws-dev                      list a saved cloud connection
              fast_copy.py ls user@host:/var/log           list a remote SSH directory
            """),
    )
    parser.add_argument("paths", nargs="*", metavar="SOURCE ... DESTINATION",
                        help="One or more sources, followed by the destination. "
                             "A source can be a folder, file, or glob pattern "
                             "(e.g. *.zip). Multiple sources are copied "
                             "side-by-side under destination, preserving their "
                             "basenames (cp -r style). Glob in the basename "
                             "also works for SSH sources, e.g. "
                             "user@host:/data/*.tar.gz")

    copy_grp = parser.add_argument_group("copy options")
    copy_grp.add_argument("--buffer", type=int, default=DEFAULT_BUFFER_MB,
                        help=f"Buffer size in MB (default: {DEFAULT_BUFFER_MB})")
    copy_grp.add_argument("--threads", type=int, default=DEFAULT_THREADS,
                        help=f"Threads for hashing/layout (default: {DEFAULT_THREADS})")
    copy_grp.add_argument("--cloud-concurrency", type=int,
                        default=DEFAULT_CLOUD_CONCURRENCY,
                        help=f"Concurrent uploads/downloads for object storage "
                             f"(s3/az/gs). Small-file transfers are latency-bound, "
                             f"so this defaults higher than --threads "
                             f"(default: {DEFAULT_CLOUD_CONCURRENCY})")
    copy_grp.add_argument("--dry-run", action="store_true",
                        help="Show copy plan without copying")
    copy_grp.add_argument("-v", "--verbose", action="store_true",
                        help="Verbose output (full FS detection details, etc.)")
    copy_grp.add_argument("--no-verify", action="store_true",
                        help="Skip post-copy verification")
    copy_grp.add_argument("--log-file", default=None,
                        help="Write structured JSON log to file")
    copy_grp.add_argument("--no-dedup", action="store_true",
                        help="Disable deduplication")
    copy_grp.add_argument("--hash", choices=["auto", "xxh128", "sha256"],
                        default="auto",
                        help="Hash algorithm for dedup/verify. "
                             "auto (default): xxh128 if installed, else sha256. "
                             "xxh128: force xxh128 (10x faster, non-cryptographic). "
                             "sha256: force sha256 (cryptographic; collision-resistant).")
    copy_grp.add_argument("--force", action="store_true",
                        help="Skip space check, copy even if not enough space")
    copy_grp.add_argument("--overwrite", action="store_true",
                        help="Overwrite all files, skip identical-file detection")
    copy_grp.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                        help="Exclude files/dirs by name or glob (e.g. .venv, "
                             "*.bat, .git*). Matching directories are pruned. "
                             "Repeatable.")
    copy_grp.add_argument("--no-cache", action="store_true",
                        help="Disable persistent hash cache (cross-run dedup database)")
    copy_grp.add_argument("--dedup-existing", action="store_true",
                        dest="dedup_existing", default=False,
                        help="When lazily hashing files from --index-existing, "
                             "deduplicate target files that share the same content "
                             "into reflinks via FIDEDUPERANGE. Only files that are "
                             "hashed anyway (due to size match) are considered. "
                             "Linux/btrfs/XFS only.")
    copy_grp.add_argument("--index-existing", action="append", default=[],
                        metavar="PATH", dest="index_existing",
                        help="Scan PATH and register files by size in the dedup index. "
                             "During sync, size-matched files are lazily hashed and "
                             "reflinkd instead of copied if content matches. "
                             "PATH must be on the same filesystem as the destination. "
                             "Repeatable for multiple paths.")
    copy_grp.add_argument("--ssh-no-sftp", action="store_true", dest="ssh_no_sftp",
                        help="Transfer over plain SSH using tar (no SFTP subsystem). "
                             "For servers with SSH enabled but SFTP disabled "
                             "(e.g. some NAS). Supports dedup/incremental/verify.")
    copy_grp.add_argument("--chunk-size", type=int, default=100, metavar="MB",
                        dest="chunk_size",
                        help="SSH (--ssh-no-sftp) tar batch size in MB (default: "
                             "100). Bigger = fewer round-trips; smaller = finer "
                             "resume granularity. This is NOT --buffer (that's "
                             "the block-copy I/O buffer).")
    copy_grp.add_argument("--use-sudo", action="store_true",
                        help="Re-exec self under sudo if not already root. "
                             "sudo will prompt for the password on the terminal. "
                             "Useful when source or destination needs root "
                             "(e.g. /var/lib/longhorn/replicas). Linux/macOS only.")
    copy_grp.add_argument("--preserve", default=None, metavar="TOKENS",
                        help="Comma-separated metadata kinds to preserve on the "
                             "destination: mode,times (default), owner, xattr, "
                             "acl. Special tokens: all, none. Examples: "
                             "--preserve=owner,xattr  --preserve=all. Under "
                             "--use-sudo, --preserve=all is implicit unless you "
                             "pass --preserve explicitly. Destination filesystems "
                             "that can't store some metadata (FAT32 for xattrs, "
                             "etc.) drop those attributes; a one-line summary "
                             "reports what was preserved vs dropped.")
    copy_grp.add_argument("--progress-json", action="store_true",
                        help=argparse.SUPPRESS)

    ssh_grp = parser.add_argument_group("ssh options (remote source / destination)")
    ssh_grp.add_argument("--ssh-dst-port", "--ssh-port", type=int, default=22,
                        dest="ssh_port",
                        help="SSH port for remote destination (default: 22)")
    ssh_grp.add_argument("--ssh-dst-key", "--ssh-key", default=None,
                        dest="ssh_key",
                        help="Path to SSH private key for remote destination")
    ssh_grp.add_argument("--ssh-dst-password", "--ssh-password", action="store_true",
                        dest="ssh_password",
                        help="Prompt for SSH password for remote destination")
    ssh_grp.add_argument("--ssh-dst-password-env", default=None, metavar="VAR",
                        dest="ssh_password_env", help=argparse.SUPPRESS)
    ssh_grp.add_argument("--ssh-strict-host-key-checking", action="store_true",
                        dest="ssh_strict_host_keys",
                        help="Reject SSH host keys not already in known_hosts "
                             "instead of the interactive trust-on-first-use "
                             "prompt. Use for CI/automated runs to prevent "
                             "first-connection MITM.")
    ssh_grp.add_argument("-z", "--compress", action="store_true",
                        help="Enable SSH compression (good for slow links)")
    ssh_grp.add_argument("--ssh-src-port", "--src-port", type=int, default=22,
                        dest="src_port",
                        help="SSH port for remote source (default: 22)")
    ssh_grp.add_argument("--ssh-src-key", "--src-key", default=None,
                        dest="src_key",
                        help="Path to SSH private key for remote source")
    ssh_grp.add_argument("--ssh-src-password", "--src-password", action="store_true",
                        dest="src_password",
                        help="Prompt for SSH password for remote source")
    ssh_grp.add_argument("--ssh-src-password-env", default=None, metavar="VAR",
                        dest="src_password_env", help=argparse.SUPPRESS)

    cloud_grp = parser.add_argument_group(
        "cloud / object storage options (s3:// az:// gs://)")
    cloud_grp.add_argument("--endpoint-url", default=None,
                        help="Custom S3-compatible endpoint (MinIO, Cloudflare "
                             "R2, Wasabi, Backblaze B2).")
    cloud_grp.add_argument("--s3-region", default=None,
                        help="AWS region for s3:// (default: us-east-1)")
    cloud_grp.add_argument("--s3-profile", default=None,
                        help="AWS named profile from ~/.aws/credentials")
    cloud_grp.add_argument("--az-connection-string", default=None,
                        help="Azure Blob connection string (or env "
                             "AZURE_STORAGE_CONNECTION_STRING)")
    cloud_grp.add_argument("--az-account", default=None,
                        help="Azure storage account name (with --az-key)")
    cloud_grp.add_argument("--az-key", default=None,
                        help="Azure storage account key. WARNING: a key passed "
                             "on the command line is visible to other users via "
                             "`ps` and is saved in shell history. Prefer a saved "
                             "connection (fast_copy.py creds add) or the "
                             "AZURE_STORAGE_CONNECTION_STRING env var.")
    cloud_grp.add_argument("--gcs-project", default=None,
                        help="Google Cloud project for gs://")
    cloud_grp.add_argument("--gcs-credentials", default=None,
                        help="Path to a GCS service-account JSON key file")
    cloud_grp.add_argument("--credentials-file", default=None, metavar="PATH",
                        help="Named-connection file for cloud credentials. "
                             "Reference a connection by name (e.g. aws-dev, "
                             "aws-dev:folder) or in a URL as s3://name@bucket/key. "
                             "Defaults to credentials.json next to fast_copy.py "
                             "(override with this flag or $FAST_COPY_CREDENTIALS). "
                             "Manage it with: fast_copy.py creds add/list/edit/"
                             "remove/test.")

    info_grp = parser.add_argument_group("info & self-update")
    info_grp.add_argument("--version", "-V", action="store_true",
                        help="Show version and exit")
    info_grp.add_argument("--check-update", action="store_true",
                        help="Show available updates and release notes")
    info_grp.add_argument("--update", nargs="?", const=True, default=False,
                        metavar="VERSION",
                        help="Download and install latest (or a specific version)")
    args = parser.parse_args()

    if args.progress_json:
        global PROGRESS_JSON
        PROGRESS_JSON = True

    if getattr(args, "ssh_strict_host_keys", False):
        global _strict_host_keys
        _strict_host_keys = True

    # Split positional paths into (sources..., destination). When 2+ positionals
    # are given, the last one is the destination and any earlier ones are
    # sources — matches `cp src1 src2 ... dst/` semantics.
    args.extra_sources = []
    if len(args.paths) >= 2:
        args.destination = args.paths[-1]
        args.source = args.paths[0]
        args.extra_sources = args.paths[1:-1]
    elif len(args.paths) == 1:
        args.source = args.paths[0]
        args.destination = None
    else:
        args.source = None
        args.destination = None

    # Windows cmd.exe/MSVCRT quirk: a trailing `\"` in a quoted path like
    # "\\host\share\" escapes the closing quote, leaving a literal `"` at
    # the end of the argument. `"` is never valid in a Windows path, so
    # strip it to recover the intended path.
    if _system == "Windows":
        if args.source and args.source.endswith('"'):
            args.source = args.source.rstrip('"')
        if args.destination and args.destination.endswith('"'):
            args.destination = args.destination.rstrip('"')
        args.extra_sources = [
            s.rstrip('"') if s.endswith('"') else s for s in args.extra_sources
        ]

    # These flags are handled in __main__ before main() is called,
    # but if someone somehow reaches here, handle gracefully
    if args.version or args.check_update or args.update:
        parser.exit(0)

    if not args.source or not args.destination:
        parser.error("the following arguments are required: source, destination")

    # Apply hash algorithm selection BEFORE any hashing happens.
    # Must come before dedup, verify, or cache lookups.
    _set_hash_algo(args.hash)

    # ── Named-connection endpoints (profiles) ─────────────────────────
    # Expand `name` / `name:path` endpoints that match a saved connection
    # (cloud or ssh) into their concrete form, and pull any SSH credentials
    # from the profile — before cloud/SSH routing below sees the paths.
    apply_named_endpoints(args)

    # ── Object storage routing (v4.0.0) ───────────────────────────────
    # If either endpoint is a cloud URL (s3:// / az:// / gs://), hand off to
    # the object-storage backends. The local/SSH engine below handles only
    # filesystem and SSH paths.
    if is_cloud_path(args.source) or is_cloud_path(args.destination):
        run_cloud_transfer(args)
        return

    # ── --preserve resolution ────────────────────────────────────────
    # Default: mode + times (matches v3.1.x). When --use-sudo and no
    # explicit --preserve, promote to 'all' since /etc backups without
    # ownership are usually useless. When user passed --preserve, honor
    # exactly what they asked.
    try:
        if args.preserve is None:
            if _is_under_sudo() or args.use_sudo:
                spec = PreserveSpec.from_tokens("all")
            else:
                spec = PreserveSpec()  # mode + times
        else:
            spec = PreserveSpec.from_tokens(args.preserve)
    except ValueError as e:
        parser.error(str(e))
    _set_preserve_spec(spec)

    global _log_enabled
    if args.log_file:
        _log_enabled = True
    # When running under sudo, always capture per-file entries so we can
    # write the hidden audit file at the end of the copy.
    if _is_under_sudo():
        _log_enabled = True

    src_arg = args.source
    buf_size = args.buffer * 1024 * 1024

    # ── Detect remote source and destination ──────────────────────────
    src_remote = parse_remote_path(src_arg)
    dst_remote = parse_remote_path(args.destination)

    # Check paramiko is installed if SSH is needed
    if (src_remote or dst_remote) and not _has_paramiko:
        print(f"\n  {C.RED}Error: SSH transfers require paramiko.{C.RESET}")
        print(f"  Install it with: {C.BOLD}python -m pip install paramiko{C.RESET}\n")
        sys.exit(1)

    # --preserve scope: owner/xattr/ACL now round-trip across L2L, L2R,
    # R2L, and R2R. Remote operations require python3 on the relevant
    # endpoint; at copy time we emit a clear message if it's missing.

    # One-time warning if xxhash is not installed
    if _hash_name != "xxh128":
        print(f"  {C.YELLOW}Note: xxhash not installed — using SHA-256 (slower).{C.RESET}")
        print(f"  {C.DIM}Install for ~10x faster hashing: python -m pip install xxhash{C.RESET}")

    if src_remote:
        src_remote = src_remote._replace(port=args.src_port)
    if dst_remote:
        dst_remote = dst_remote._replace(port=args.ssh_port)

    # Validate SSH key paths early
    for label, keypath in [("--ssh-key", args.ssh_key), ("--src-key", args.src_key)]:
        if keypath and not os.path.isfile(keypath):
            print(f"{C.RED}Error: {label} file not found: {keypath}{C.RESET}")
            sys.exit(1)

    # Keep 'remote' alias for backward compat with existing local→remote code
    remote = dst_remote

    if dst_remote:
        dst = dst_remote.path
    else:
        dst = os.path.abspath(args.destination)

    # ── Resolve source ───────────────────────────────────────────────
    src_mode = None  # "dir", "file", "glob", "multi", "remote", or "remote_glob"
    glob_files = []
    multi_sources = []  # absolute paths, set when src_mode == "multi"
    remote_glob_pattern = None  # set when src_mode == "remote_glob"

    if args.extra_sources and src_remote:
        print(f"{C.RED}Error: multi-source mode does not support SSH sources "
              f"(got remote source with {len(args.extra_sources)} additional path"
              f"{'s' if len(args.extra_sources) != 1 else ''}){C.RESET}")
        sys.exit(1)

    if args.extra_sources:
        # Multi-source: shell-expanded glob (e.g. `pvc-*`) or N explicit paths
        # on the command line. Each source becomes its own subtree under
        # destination, preserving its basename (cp -r style).
        all_src = [src_arg] + args.extra_sources
        for s in all_src:
            if parse_remote_path(s):
                print(f"{C.RED}Error: multi-source mode does not support SSH sources "
                      f"(got '{s}'){C.RESET}")
                sys.exit(1)
            if not os.path.exists(s):
                print(f"{C.RED}Error: source not found: {s}{C.RESET}")
                sys.exit(1)
        multi_sources = [os.path.abspath(s) for s in all_src]
        src_mode = "multi"
        src = os.path.commonpath(multi_sources)
        if not os.path.isdir(src):
            src = os.path.dirname(src)
        _shown = ", ".join(os.path.basename(p.rstrip(os.sep))
                           for p in multi_sources[:2])
        if len(multi_sources) > 2:
            _shown += f", +{len(multi_sources) - 2} more"
        src_display = f"{len(multi_sources)} sources ({_shown}) under {src}"
    elif src_remote:
        # Detect a glob pattern in the basename — the only place we support
        # it. A glob in a middle component (e.g. /foo/*/bar) would require
        # a recursive remote walk, which is out of scope here.
        rpath = src_remote.path
        rbase = posixpath.basename(rpath)
        rparent = posixpath.dirname(rpath)
        _glob_chars = set("*?[")
        if any(c in rbase for c in _glob_chars):
            if any(c in rparent for c in _glob_chars):
                print(f"{C.RED}Error: glob characters are only supported in the "
                      f"final path component for remote sources.{C.RESET}")
                sys.exit(1)
            src = rparent if rparent else "."
            src_mode = "remote_glob"
            remote_glob_pattern = rbase
            src_display = f"{src_remote.user}@{src_remote.host}:{rpath}"
        else:
            src = rpath
            src_mode = "remote"
            src_display = f"{src_remote.user}@{src_remote.host}:{src}"
    else:
        src = os.path.abspath(src_arg)
        if os.path.isdir(src):
            src_mode = "dir"
        elif os.path.isfile(src):
            src_mode = "file"
        else:
            # Try glob expansion (handles wildcards like *.zip)
            glob_files = sorted(globmod.glob(src_arg))
            if not glob_files:
                glob_files = sorted(globmod.glob(src))
            glob_files = [f for f in glob_files if os.path.isfile(f)]
            if glob_files:
                src_mode = "glob"
            else:
                print(f"{C.RED}Error: Source '{src_arg}' — no matching files or directory found{C.RESET}")
                sys.exit(1)

        if src_mode == "glob":
            src_display = src_arg
            src = os.path.commonpath([os.path.abspath(f) for f in glob_files])
            if os.path.isfile(src):
                src = os.path.dirname(src)
        elif src_mode == "file":
            src_display = src
        else:
            src_display = src

    # Detect destination filesystem early so we can fold the strategy
    # into the Dedup banner line and use it for Phase 2/3.
    fs_info = None
    fs_error = None
    if not dst_remote:
        try:
            fs_info = detect_capabilities(dst)
        except Exception as e:
            fs_error = str(e)
        # Probe destination for extended-metadata support upfront if the
        # user asked for xattr or ACL preservation, so we can warn early
        # and fall back gracefully on unsupported filesystems (FAT32, etc).
        if _preserve_spec.xattr:
            _probe_dst_xattr_support(dst)
        if _preserve_spec.acl:
            _probe_dst_acl_support(dst)

    banner("FAST BLOCK-ORDER COPY")
    print(f"  Source:      {C.BOLD}{src_display}{C.RESET}")
    if src_mode == "remote":
        print(f"               {C.DIM}(SSH remote, port {src_remote.port}){C.RESET}")
    elif src_mode == "remote_glob":
        print(f"               {C.DIM}(SSH remote glob, port {src_remote.port}){C.RESET}")
    elif src_mode == "glob":
        print(f"               {C.DIM}{len(glob_files)} files matched{C.RESET}")
    elif src_mode == "multi":
        print(f"               {C.DIM}{len(multi_sources)} sources "
              f"(cp -r style){C.RESET}")
    elif src_mode == "file":
        print(f"               {C.DIM}(single file){C.RESET}")
    if dst_remote:
        print(f"  Destination: {C.BOLD}{dst_remote.user}@{dst_remote.host}:{dst}{C.RESET}")
        print(f"               {C.DIM}(SSH remote, port {dst_remote.port}){C.RESET}")
    else:
        print(f"  Destination: {C.BOLD}{dst}{C.RESET}")
    if src_remote and dst_remote:
        print(f"  Mode:        {C.CYAN}remote → remote (relay through local){C.RESET}")
    elif src_remote:
        print(f"  Mode:        {C.CYAN}remote → local{C.RESET}")
    elif dst_remote:
        print(f"  Mode:        {C.CYAN}local → remote{C.RESET}")
    print(f"  Buffer:      {args.buffer} MB")
    # Dedup line: fold FS strategy in parens when detected
    if args.no_dedup:
        print(f"  Dedup:       disabled")
    elif fs_info is not None:
        print(f"  Dedup:       enabled ({C.CYAN}{fs_info.strategy}{C.RESET})")
    else:
        print(f"  Dedup:       enabled")
    # Hash algorithm + source (auto vs forced). Non-cryptographic xxh128
    # is marked so users understand the trust boundary.
    if not args.no_dedup:
        if _hash_name == "xxh128":
            _hash_note = (f"{C.DIM}(non-cryptographic; "
                          f"{'default' if _hash_source == 'auto' else 'forced'}){C.RESET}")
        else:  # sha256
            _hash_note = (f"{C.DIM}(cryptographic; "
                          f"{'fallback' if _hash_source == 'auto' else 'forced'}){C.RESET}")
        print(f"  Hash:        {C.BOLD}{_hash_name}{C.RESET} {_hash_note}")
    if not src_remote and not dst_remote:
        print(f"  Hash cache:  {'disabled' if args.no_cache else 'enabled'}")
    print(f"  Overwrite:   {'always' if args.overwrite else 'skip identical'}")
    # Show Preserve only when more than the default mode+times is requested,
    # so the banner stays quiet for typical copies.
    _spec_on = [k for k in PreserveSpec.KINDS if getattr(_preserve_spec, k)]
    _spec_extended = [k for k in ("owner", "xattr", "acl") if getattr(_preserve_spec, k)]
    if _spec_extended:
        _spec_note = ""
        if _preserve_spec.xattr and _preserve_dst_caps["xattr"] is False:
            _spec_note = f" {C.YELLOW}(dst FS does not support xattrs){C.RESET}"
        if _preserve_spec.acl and _preserve_dst_caps["acl"] is False:
            _spec_note += f" {C.YELLOW}(dst FS does not support ACLs){C.RESET}"
        print(f"  Preserve:    {C.BOLD}{','.join(_spec_on)}{C.RESET}{_spec_note}")
    if (src_remote or dst_remote) and args.compress:
        print(f"  Compression: {C.GREEN}enabled{C.RESET}")
    print(f"  Platform:    {_system}")

    # Verbose: full FS capability breakdown (with -v / --verbose)
    if getattr(args, "verbose", False) and fs_info is not None:
        caps = fs_info.capabilities
        print(f"  FS:          {C.BOLD}{fs_info.fs_type}{C.RESET} → "
              f"{C.CYAN}{fs_info.strategy}{C.RESET}")
        print(f"               {C.DIM}hardlink={'y' if caps.hardlink else 'n'} "
              f"symlink={'y' if caps.symlink else 'n'} "
              f"reflink={'y' if caps.reflink else 'n'} "
              f"case={'sens' if caps.case_sensitive else 'insens'}{C.RESET}")
        print(f"               {C.DIM}detect={fs_info.detection_ms:.1f}ms "
              f"probe={fs_info.probe_ms:.1f}ms "
              f"({len(fs_info.probes_run)} probe"
              f"{'s' if len(fs_info.probes_run) != 1 else ''}){C.RESET}")
    elif fs_error:
        print(f"  FS:          {C.YELLOW}detection failed: {fs_error}{C.RESET}")

    print()

    # ── SSH-only (no SFTP) fast path: tar over the exec channel ─────────
    if getattr(args, "ssh_no_sftp", False) and (src_remote or dst_remote):
        _tar_local_srcs = None
        if not src_remote:
            _tar_local_srcs = (multi_sources if args.extra_sources else [src_arg])
        sys.exit(copy_via_tar_ssh(src_remote, dst_remote, dst, _tar_local_srcs, args))

    # ── Connect to remote source if needed ─────────────────────────────
    src_ssh = None
    if src_remote:
        banner("SSH — Connecting to source")
        src_password = None
        if args.src_password_env:
            src_password = os.environ.get(args.src_password_env)
        elif args.src_password:
            src_password = getpass.getpass(f"Password for {src_remote.user}@{src_remote.host}: ")
        elif getattr(args, "_resolved_src_password", None):
            src_password = args._resolved_src_password
        src_ssh = SSHConnection(src_remote, port=src_remote.port, key_path=args.src_key,
                                password=src_password, compress=args.compress,
                                ).connect()
        print(f"  {C.GREEN}Connected to {src_remote.user}@{src_remote.host}:{src_remote.port}{C.RESET}")
        caps = [k for k, v in src_ssh.caps.items() if v]
        print(f"  {C.DIM}Remote tools: {', '.join(caps) or 'none detected'}{C.RESET}")

    # ── Phase 1: Scan ─────────────────────────────────────────────────
    banner("Phase 1 — Scanning source")

    if src_mode == "remote":
        entries, errors = scan_remote_source(src_ssh, src, args.exclude)
        # If the remote source was a single file (not a directory), the find
        # command returns rel="." because relpath(file, file) == ".".
        # Fix rel to the basename and adjust src to the parent directory,
        # mirroring the local "file" mode logic, so that tar cd works.
        if len(entries) == 1 and entries[0].rel == ".":
            fname = posixpath.basename(src)
            entries[0] = entries[0]._replace(rel=fname)
            src = posixpath.dirname(src)
    elif src_mode == "remote_glob":
        # Expand the basename glob via SFTP listdir + fnmatch. src is already
        # the parent directory; remote_glob_pattern is the basename pattern.
        sftp = src_ssh.open_sftp()
        try:
            attrs = sftp.listdir_attr(src)
        except IOError as e:
            print(f"\n  {C.RED}Error: cannot list remote directory "
                  f"{src!r}: {e}{C.RESET}")
            src_ssh.close()
            sys.exit(1)
        matched = [a for a in attrs
                   if fnmatch.fnmatch(a.filename, remote_glob_pattern)
                   and not stat.S_ISDIR(a.st_mode or 0)]
        # Honor --exclude on basename
        if args.exclude:
            matched = [a for a in matched
                       if not any(fnmatch.fnmatch(a.filename, p)
                                  for p in args.exclude)]
        if not matched:
            print(f"\n  {C.RED}Error: no remote files matched "
                  f"{remote_glob_pattern!r} in {src}{C.RESET}")
            src_ssh.close()
            sys.exit(1)
        entries = []
        errors = []
        for a in matched:
            entries.append(FileEntry(
                src=posixpath.join(src, a.filename),
                rel=a.filename,
                size=a.st_size or 0,
                physical_offset=0,
                content_hash=None,
            ))
        print(f"  {C.GREEN}Found {len(entries)} file(s){C.RESET} "
              f"matching {C.BOLD}{remote_glob_pattern}{C.RESET} in {src}")
    elif src_mode == "file":
        fname = os.path.basename(src)
        st = os.stat(src)
        entries = [FileEntry(src=src, rel=fname, size=st.st_size,
                             physical_offset=0, content_hash=None,
                             alloc_size=_detect_sparse_alloc(st))]
        errors = []
        print(f"  {C.GREEN}Found 1 file{C.RESET} ({fmt_size(st.st_size)})")
        src = os.path.dirname(src)
    elif src_mode == "glob":
        entries = []
        errors = []
        for fpath in glob_files:
            abs_f = os.path.abspath(fpath)
            rel = os.path.relpath(abs_f, src)
            try:
                st = os.stat(abs_f)
                entries.append(FileEntry(src=abs_f, rel=rel, size=st.st_size,
                                         physical_offset=0, content_hash=None,
                                         alloc_size=_detect_sparse_alloc(st)))
            except OSError as e:
                errors.append((abs_f, str(e)))
        print(f"  {C.GREEN}Found {len(entries)} files{C.RESET}")
    elif src_mode == "multi":
        # cp -r src1 src2 ... dst/  → each source preserves its basename
        # under destination. Directories are walked; files are added directly.
        entries = []
        errors = []
        for s in multi_sources:
            base = os.path.basename(s.rstrip(os.sep)) or s
            if os.path.isfile(s):
                try:
                    st = os.stat(s)
                    alloc = _detect_sparse_alloc(st)
                    entries.append(FileEntry(src=s, rel=base, size=st.st_size,
                                             physical_offset=0, content_hash=None,
                                             alloc_size=alloc))
                except OSError as e:
                    errors.append((s, str(e)))
            elif os.path.isdir(s):
                sub_entries, sub_errors = scan_source(
                    s, dst if not dst_remote else None, args.exclude
                )
                for e in sub_entries:
                    new_rel = f"{base}/{e.rel}" if e.rel and e.rel != "." else base
                    entries.append(e._replace(rel=new_rel))
                errors.extend(sub_errors)
        print(f"  {C.GREEN}Found {len(entries)} files{C.RESET} "
              f"across {len(multi_sources)} sources")
    else:
        entries, errors = scan_source(src, dst if not dst_remote else None, args.exclude)

    if not entries:
        print(f"  {C.YELLOW}No files found.{C.RESET}")
        if src_ssh:
            src_ssh.close()
        sys.exit(0)

    # ── Single-file destination rename ───────────────────────────────
    # When copying a single file, allow the destination to be a file path
    # (like cp/scp): fast_copy host:file.tar.gz /local/renamed.tar.gz
    # Detect this when the destination looks like a file (has a dot-extension
    # in the basename, or already exists as a file) and is not an existing
    # directory.  Adjust dst to parent dir and rename the entry.
    # For remote sources, we track the rename separately (via case_renames)
    # because entry.rel must stay as the original filename for tar to find it.
    _dst_file_rename = None  # (new_rel, old_rel) if file-destination detected
    if len(entries) == 1 and (src_mode in ("file", "remote", "remote_glob") or
                              (src_mode == "glob" and len(glob_files) == 1)):
        _detected = False
        if dst_remote:
            dst_base = posixpath.basename(dst)
            dst_parent = posixpath.dirname(dst)
            # Use splitext to require a real extension — avoids false positives
            # on hidden dirs like .outputs, .config (no extension after the dot).
            _, ext = posixpath.splitext(dst_base)
            if dst_parent and ext and not dst.endswith("/"):
                _detected = True
                dst = dst_parent
        else:
            if not os.path.isdir(dst) and not dst.endswith(("/", os.sep)):
                dst_base = os.path.basename(dst)
                dst_parent = os.path.dirname(dst)
                _, ext = os.path.splitext(dst_base)
                is_file_target = os.path.isfile(dst) or bool(ext)
                if is_file_target and dst_parent:
                    _detected = True
                    dst = dst_parent

        if _detected:
            old_rel = entries[0].rel
            if src_remote:
                # For remote sources, keep entry.rel as-is (tar needs it).
                # The rename is applied via case_renames during extraction.
                _dst_file_rename = (dst_base, old_rel)
            else:
                # For local sources, we can rename entry.rel directly since
                # the file is read by its absolute src path, not rel.
                entries[0] = entries[0]._replace(rel=dst_base)
            print(f"  {C.DIM}Destination is a file path — "
                  f"saving as {dst_base}{C.RESET}")

    total_size = sum(e.size for e in entries)
    total_files = len(entries)
    avg_size = total_size / total_files if total_files else 0
    print(f"  Total: {C.BOLD}{fmt_size(total_size)}{C.RESET} in "
          f"{C.BOLD}{total_files}{C.RESET} files  "
          f"(avg {fmt_size(avg_size)}/file)")

    # Sparse-file summary: surface VM-image / sparse-replica savings up front
    # so the user understands why "Data to write" is much smaller than "Total".
    sparse_entries = [e for e in entries if e.alloc_size is not None]
    if sparse_entries:
        sparse_logical = sum(e.size for e in sparse_entries)
        sparse_alloc = sum(e.alloc_size for e in sparse_entries)
        print(f"  Sparse: {C.BOLD}{len(sparse_entries)}{C.RESET} sparse files — "
              f"{fmt_size(sparse_logical)} logical, "
              f"{C.GREEN}{fmt_size(sparse_alloc)}{C.RESET} on disk "
              f"({fmt_size(sparse_logical - sparse_alloc)} skippable as holes)")

    # ── Phase 2: Deduplication ───────────────────────────────────────
    dedup_db = None
    if not src_remote and not dst_remote and not args.no_dedup and not args.no_cache:
        _makedirs_or_die(dst)
        try:
            dedup_db = DedupDB(dst)
        except Exception as e:
            print(f"  {C.YELLOW}Warning: could not open hash cache: {e}{C.RESET}")

    if dedup_db and getattr(args, 'index_existing', []):
        banner("Phase 1b — Indexing existing files")
        for idx_path in args.index_existing:
            dedup_db.index_existing(os.path.abspath(idx_path))

    link_map = {}
    saved_bytes = 0
    copy_entries = entries

    # Use the fs_info detected earlier (before the banner) to decide the
    # dedup strategy. On link-capable filesystems (ext4, btrfs, XFS, NTFS,
    # APFS, ReFS, etc.) dedup saves both bandwidth and disk. On
    # link-incapable filesystems (FAT32, exFAT) dedup saves only bandwidth
    # because each duplicate is materialized as a full copy.
    fs_strategy = fs_info.strategy if fs_info is not None else None

    if not args.no_dedup:
        banner("Phase 2 — Deduplication")
        if src_remote:
            copy_entries, link_map, saved_bytes = deduplicate_remote_source(
                entries, src_ssh, src, args.threads, fs_strategy=fs_strategy)
        else:
            copy_entries, link_map, saved_bytes = deduplicate(
                entries, args.threads, dedup_db, fs_strategy=fs_strategy,
                dedup_inplace=getattr(args, 'dedup_existing', False))

    unique_size = sum(e.size for e in copy_entries)

    # ── Case-conflict resolution for local destinations ─────────────
    case_renames = {}
    if not dst_remote:
        copy_entries, link_map, case_renames = resolve_case_conflicts(
            copy_entries, link_map, dst)

    # Inject file-destination rename for remote-source copies.
    # case_renames maps {original_rel: new_rel} — _stream_tar_batch_from_remote
    # uses this to fetch by original name and extract as new name.
    # For R2R: tar pipe has no rename mechanism, so rename after copy.
    if _dst_file_rename is not None:
        new_rel, old_rel = _dst_file_rename
        if src_remote and not dst_remote:
            # R2L: inject into case_renames (original → new) and update
            # entry.rel to new name for verify/incremental checks.
            case_renames[old_rel] = new_rel
            for i, e in enumerate(copy_entries):
                if e.rel == old_rel:
                    copy_entries[i] = e._replace(rel=new_rel)
                    break
        elif src_remote and dst_remote:
            # R2R: can't rename during tar pipe — will rename after copy.
            # _dst_file_rename stays set; handled after copy_hybrid_r2r.
            pass

    # ══════════════════════════════════════════════════════════════════
    # REMOTE → REMOTE FLOW
    # ══════════════════════════════════════════════════════════════════
    if src_remote and dst_remote:
        dst_ssh = None
        try:
            # ── Connect to destination ───────────────────────────────
            banner("SSH — Connecting to destination")
            dst_password = None
            if args.ssh_password_env:
                dst_password = os.environ.get(args.ssh_password_env)
            elif args.ssh_password:
                dst_password = getpass.getpass(f"Password for {dst_remote.user}@{dst_remote.host}: ")
            elif getattr(args, "_resolved_dst_password", None):
                dst_password = args._resolved_dst_password
            dst_ssh = SSHConnection(dst_remote, port=dst_remote.port, key_path=args.ssh_key,
                                    password=dst_password, compress=args.compress,
                                    ).connect()
            print(f"  {C.GREEN}Connected to {dst_remote.user}@{dst_remote.host}:{dst_remote.port}{C.RESET}")
            caps = [k for k, v in dst_ssh.caps.items() if v]
            print(f"  {C.DIM}Remote tools: {', '.join(caps) or 'none detected'}{C.RESET}")

            # ── Phase 2b: Incremental check against remote dest ──────
            skipped_count = 0
            skipped_bytes = 0

            if not args.overwrite:
                banner("Phase 2b — Remote incremental check")
                try:
                    copy_entries, link_map, skipped_count, skipped_bytes = \
                        filter_unchanged_remote(copy_entries, link_map, dst_ssh, dst,
                                                src_ssh=src_ssh, src_root=src)
                    unique_size = sum(e.size for e in copy_entries)
                except Exception as e:
                    print(f"  {C.YELLOW}Incremental check failed ({e}) — copying all files{C.RESET}")
                    # Reconnect destination in case the channel died
                    try:
                        dst_ssh.close()
                    except Exception:
                        pass
                    dst_ssh = SSHConnection(dst_remote, port=dst_remote.port, key_path=args.ssh_key,
                                            password=dst_password, compress=args.compress,
                                            ).connect()

                if not copy_entries and not link_map:
                    banner("DONE — Nothing to copy")
                    print(f"  All {skipped_count} files are already up to date on remote.")
                    if args.log_file:
                        write_log_file(args.log_file, {
                            "source": f"{src_remote.user}@{src_remote.host}:{src}",
                            "destination": f"{dst_remote.user}@{dst_remote.host}:{dst}",
                            "mode": "remote_to_remote", "total_files": total_files,
                            "copied": 0, "linked": 0, "skipped": skipped_count,
                            "errors": 0, "total_bytes": total_size, "bytes_written": 0,
                            "dedup_saved": saved_bytes, "elapsed_sec": 0,
                            "avg_speed_bps": 0, "hash_algo": _hash_name,
                        })
                    print()
                    src_ssh.close()
                    dst_ssh.close()
                    sys.exit(0)

            # ── Phase 3: Space check on dest ─────────────────────────
            banner("Phase 3 — Space check (remote destination)")
            required = unique_size
            print(f"  Data to write: {C.BOLD}{fmt_size(required)}{C.RESET}"
                  + (f" (after dedup saved {fmt_size(saved_bytes)})" if saved_bytes > 0 else ""))

            if not check_remote_space(dst_ssh, dst, required, args.force):
                src_ssh.close()
                dst_ssh.close()
                sys.exit(1)

            if args.dry_run:
                small, large = split_by_size(copy_entries)
                small_sz = sum(e.size for e in small)
                large_sz = sum(e.size for e in large)
                print(f"\n  {C.YELLOW}DRY RUN — Copy strategy:{C.RESET}\n")
                print(f"    Small files (<1MB): {C.BOLD}{len(small)}{C.RESET} files, "
                      f"{C.BOLD}{fmt_size(small_sz)}{C.RESET} → tar pipe relay")
                print(f"    Large files (≥1MB): {C.BOLD}{len(large)}{C.RESET} files, "
                      f"{C.BOLD}{fmt_size(large_sz)}{C.RESET} → SFTP relay")
                if link_map:
                    print(f"\n  Plus {len(link_map)} duplicate files to be linked on remote")
                print(f"\n  Unique data: {fmt_size(unique_size)}")
                src_ssh.close()
                dst_ssh.close()
                sys.exit(0)

            # ── Phase 5: Remote-to-remote copy ───────────────────────
            banner("Phase 5 — Remote-to-remote copy (relay)")

            # Check if buffer fits in available RAM
            try:
                import psutil
                avail = psutil.virtual_memory().available
            except ImportError:
                avail = None
                if _system == "Linux":
                    try:
                        with open("/proc/meminfo") as f:
                            for line in f:
                                if line.startswith("MemAvailable:"):
                                    avail = int(line.split()[1]) * 1024
                                    break
                    except (OSError, ValueError):
                        pass
                elif _system == "Darwin":
                    try:
                        import subprocess
                        # Get actual page size (4KB Intel, 16KB Apple Silicon)
                        page_size = int(subprocess.check_output(
                            ["sysctl", "-n", "hw.pagesize"], text=True, timeout=5
                        ).strip())
                        out = subprocess.check_output(
                            ["vm_stat"], text=True, timeout=5
                        )
                        free_pages = 0
                        for line in out.splitlines():
                            if "Pages free:" in line or "Pages speculative:" in line:
                                free_pages += int(line.split()[-1].rstrip("."))
                        avail = free_pages * page_size
                    except Exception:
                        pass
                elif _system == "Windows":
                    try:
                        import ctypes
                        class MEMORYSTATUSEX(ctypes.Structure):
                            _fields_ = [
                                ("dwLength", ctypes.c_ulong),
                                ("dwMemoryLoad", ctypes.c_ulong),
                                ("ullTotalPhys", ctypes.c_ulonglong),
                                ("ullAvailPhys", ctypes.c_ulonglong),
                                ("ullTotalPageFile", ctypes.c_ulonglong),
                                ("ullAvailPageFile", ctypes.c_ulonglong),
                                ("ullTotalVirtual", ctypes.c_ulonglong),
                                ("ullAvailVirtual", ctypes.c_ulonglong),
                                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                            ]
                        mem_stat = MEMORYSTATUSEX()
                        mem_stat.dwLength = ctypes.sizeof(mem_stat)
                        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem_stat))
                        avail = mem_stat.ullAvailPhys
                    except Exception:
                        pass

            if avail is not None:
                # Reserve 128MB headroom for Python/paramiko/OS
                headroom = 128 * 1024 * 1024
                safe = avail - headroom
                if buf_size > safe:
                    old_mb = buf_size // (1024 * 1024)
                    new_size = max(1 * 1024 * 1024, safe)  # floor at 1MB
                    new_mb = new_size // (1024 * 1024)
                    print(f"  {C.YELLOW}Warning: --buffer {old_mb}MB exceeds available RAM "
                          f"({fmt_size(avail)} free){C.RESET}")
                    print(f"  {C.YELLOW}Reducing buffer to {new_mb}MB to avoid MemoryError{C.RESET}")
                    buf_size = new_size
                    if buf_size < 1 * 1024 * 1024:
                        print(f"  {C.RED}Error: Not enough free RAM for even a 1MB buffer "
                              f"({fmt_size(avail)} available){C.RESET}")
                        src_ssh.close()
                        dst_ssh.close()
                        sys.exit(1)

            dst_ssh.exec_cmd(f"mkdir -p {shlex.quote(dst)}")

            progress = Progress(unique_size, len(copy_entries))
            t0 = time.time()
            copy_hybrid_r2r(copy_entries, src_ssh, dst_ssh, src, dst, progress, buf_size)
            progress.finish()

            # R2R extended-metadata: collect xattr/ACL/owner from src remote
            # via the existing collector, then apply to dst remote via the
            # same script L2R uses. Both endpoints need python3.
            if _preserve_spec.any_extended():
                want_o = _preserve_spec.owner
                want_x = _preserve_spec.xattr
                want_a = _preserve_spec.acl
                if not (src_ssh.caps.get("python3") and dst_ssh.caps.get("python3")):
                    print(f"  {C.YELLOW}Skipping R2R owner/xattr/ACL transfer "
                          f"— one of the remotes lacks python3.{C.RESET}")
                else:
                    print(f"  {C.DIM}Collecting + applying remote metadata "
                          f"for {len(copy_entries)} files...{C.RESET}",
                          end="", flush=True)
                    rel_paths = [e.rel for e in copy_entries]
                    meta = _remote_collect_metadata(src_ssh, src, rel_paths,
                                                    want_x, want_a,
                                                    want_owner=want_o)
                    _apply_collected_metadata_to_remote(
                        dst_ssh, dst, meta, want_o, want_x, want_a)
                    print(f"\r  {C.GREEN}R2R metadata: collected {len(meta)} "
                          f"records, applied to destination                    "
                          f"{C.RESET}")

            # Post-copy rename for R2R file-destination
            if _dst_file_rename is not None and copy_entries:
                new_rel, old_rel = _dst_file_rename
                old_path = posixpath.join(dst, old_rel)
                new_path = posixpath.join(dst, new_rel)
                _, mv_err, mv_rc = dst_ssh.exec_cmd(
                    f"mv {shlex.quote(old_path)} {shlex.quote(new_path)}")
                if mv_rc != 0:
                    print(f"  {C.YELLOW}Warning: rename failed on destination: "
                          f"{mv_err.strip()}{C.RESET}")
                else:
                    # Update entry.rel so verify sees the new name
                    for i, e in enumerate(copy_entries):
                        if e.rel == old_rel:
                            copy_entries[i] = e._replace(rel=new_rel)
                            break

            # Create links on dest
            if link_map:
                create_links_remote(dst_ssh, link_map, dst)

            elapsed = time.time() - t0
            speed = unique_size / elapsed if elapsed > 0 else 0

            # Save manifest on dest
            save_remote_manifest(dst_ssh, dst, copy_entries, link_map)

            # Verify on dest
            if not args.no_verify:
                verify_copy_remote(dst_ssh, copy_entries, link_map, dst)

            # Summary
            banner("DONE")
            print(f"  Source:  {C.BOLD}{src_remote.user}@{src_remote.host}:{src}{C.RESET}")
            print(f"  Dest:    {C.BOLD}{dst_remote.user}@{dst_remote.host}:{dst}{C.RESET}")
            print(f"  Files:   {C.BOLD}{total_files}{C.RESET} total"
                  + (f" ({len(copy_entries)} copied + {len(link_map)} linked)" if link_map else ""))
            if skipped_count:
                print(f"  Skipped: {C.BOLD}{skipped_count}{C.RESET} unchanged files "
                      f"({C.GREEN}{fmt_size(skipped_bytes)}{C.RESET})")
            print(f"  Data:    {C.BOLD}{fmt_size(unique_size)}{C.RESET} relayed"
                  + (f" ({fmt_size(saved_bytes)} saved by dedup)" if saved_bytes > 0 else ""))
            print(f"  Time:    {C.BOLD}{fmt_time(elapsed)}{C.RESET}")
            print(f"  Speed:   {C.GREEN}{C.BOLD}{fmt_speed(speed)}{C.RESET}")
            _print_preserve_summary()
            if args.log_file:
                write_log_file(args.log_file, {
                    "source": f"{src_remote.user}@{src_remote.host}:{src}",
                    "destination": f"{dst_remote.user}@{dst_remote.host}:{dst}",
                    "mode": "remote_to_remote",
                    "total_files": total_files, "copied": len(copy_entries),
                    "linked": len(link_map), "skipped": skipped_count,
                    "errors": sum(1 for e in _log_entries if e["action"] == "error"),
                    "total_bytes": total_size, "bytes_written": unique_size,
                    "dedup_saved": saved_bytes, "elapsed_sec": round(elapsed, 2),
                    "avg_speed_bps": round(speed), "hash_algo": _hash_name,
                })
            print()

        except KeyboardInterrupt:
            print(f"\n  {C.YELLOW}Interrupted.{C.RESET}")
            sys.exit(130)
        except (OSError, IOError) as e:
            print(f"\n{C.RED}Error: {e}{C.RESET}")
            sys.exit(1)
        except Exception as e:
            ename = type(e).__name__
            if "Authentication" in ename:
                print(f"\n{C.RED}Error: SSH authentication failed{C.RESET}")
            elif "SSH" in ename or "Socket" in ename or "paramiko" in type(e).__module__:
                print(f"\n{C.RED}Error: SSH connection failed: {e}{C.RESET}")
            elif "ConnectionReset" in ename or "BrokenPipe" in ename:
                print(f"\n{C.RED}Error: Connection lost: {e}{C.RESET}")
            else:
                raise
            sys.exit(1)
        finally:
            if src_ssh:
                src_ssh.close()
            if dst_ssh:
                dst_ssh.close()

        sys.exit(0)

    # ══════════════════════════════════════════════════════════════════
    # REMOTE → LOCAL FLOW
    # ══════════════════════════════════════════════════════════════════
    if src_remote and not dst_remote:
        try:
            # ── Phase 2b: Incremental check against local dest ───────
            skipped_count = 0
            skipped_bytes = 0

            if not args.overwrite and os.path.isdir(dst):
                banner("Phase 2b — Incremental check")
                copy_entries, link_map, skipped_count, skipped_bytes = \
                    filter_unchanged_remote_to_local(
                        copy_entries, link_map, src_ssh, src, dst, args.threads
                    )
                unique_size = sum(e.size for e in copy_entries)

                if not copy_entries and not link_map:
                    banner("DONE — Nothing to copy")
                    print(f"  All {skipped_count} files are already up to date.")
                    if args.log_file:
                        write_log_file(args.log_file, {
                            "source": f"{src_remote.user}@{src_remote.host}:{src}",
                            "destination": dst, "mode": "remote_to_local",
                            "total_files": total_files, "copied": 0, "linked": 0,
                            "skipped": skipped_count, "errors": 0,
                            "total_bytes": total_size, "bytes_written": 0,
                            "dedup_saved": saved_bytes, "elapsed_sec": 0,
                            "avg_speed_bps": 0, "hash_algo": _hash_name,
                        })
                    print()
                    src_ssh.close()
                    sys.exit(0)

            # ── Phase 3: Space check (local) ─────────────────────────
            banner("Phase 3 — Space check")
            required = unique_size
            # If the destination filesystem can't dedup via links (FAT32,
            # exFAT, etc.), each duplicate will be materialized as a full
            # copy. We need space for the FULL undeduplicated size.
            if fs_strategy == "none" and saved_bytes > 0:
                full_size = unique_size + saved_bytes
                print(f"  Data to write: {C.BOLD}{fmt_size(full_size)}{C.RESET}")
                print(f"  {C.YELLOW}⚠ Filesystem does not support links — "
                      f"dedup saves {fmt_size(saved_bytes)} on the wire only."
                      f"{C.RESET}")
                print(f"  {C.YELLOW}  Each duplicate becomes a full copy on "
                      f"disk; total disk usage will be "
                      f"{fmt_size(full_size)}.{C.RESET}")
                required = full_size
            else:
                print(f"  Data to write: {C.BOLD}{fmt_size(required)}{C.RESET}"
                      + (f" (after dedup saved {fmt_size(saved_bytes)})"
                         if saved_bytes > 0 else ""))

            if not check_destination_space(dst, required, args.force):
                src_ssh.close()
                sys.exit(1)

            if args.dry_run:
                small, large = split_by_size(copy_entries)
                small_sz = sum(e.size for e in small)
                large_sz = sum(e.size for e in large)
                print(f"\n  {C.YELLOW}DRY RUN — Copy strategy:{C.RESET}\n")
                print(f"    Small files (<1MB): {C.BOLD}{len(small)}{C.RESET} files, "
                      f"{C.BOLD}{fmt_size(small_sz)}{C.RESET} → tar stream from remote")
                print(f"    Large files (≥1MB): {C.BOLD}{len(large)}{C.RESET} files, "
                      f"{C.BOLD}{fmt_size(large_sz)}{C.RESET} → SFTP download")
                if link_map:
                    print(f"\n  Plus {len(link_map)} duplicate files to be linked")
                print(f"\n  Unique data: {fmt_size(unique_size)}")
                src_ssh.close()
                sys.exit(0)

            # ── Phase 5: Download from remote ────────────────────────
            banner("Phase 5 — Remote-to-local copy")
            os.makedirs(dst, exist_ok=True)

            progress = Progress(unique_size, len(copy_entries))
            t0 = time.time()
            copy_hybrid_remote_to_local(copy_entries, src_ssh, src, dst, progress, buf_size,
                                        case_renames=case_renames)
            progress.finish()

            # R2L extended-metadata preservation (xattr/ACL). Owner already
            # rode through tar headers via filter='tar' in _safe_tar_extract.
            # We collect xattrs/ACLs from the remote side via a single batched
            # python3 script and apply them locally.
            if _preserve_spec.xattr or _preserve_spec.acl:
                want_x = _preserve_spec.xattr and _preserve_dst_caps["xattr"] is not False
                want_a = _preserve_spec.acl and _preserve_dst_caps["acl"] is not False
                if want_x or want_a:
                    if not src_ssh.caps.get("python3"):
                        print(f"  {C.YELLOW}Skipping remote xattr/ACL "
                              f"collection — remote lacks python3.{C.RESET}")
                    else:
                        print(f"  {C.DIM}Collecting remote metadata for "
                              f"{len(copy_entries)} files...{C.RESET}",
                              end="", flush=True)
                        rel_paths = [e.rel for e in copy_entries]
                        meta = _remote_collect_metadata(src_ssh, src, rel_paths,
                                                        want_x, want_a)
                        _apply_remote_metadata_local(meta, dst, want_x, want_a)
                        # 'meta' may be {} when no files had any xattr/ACL —
                        # that's "nothing to preserve", not a failure.
                        if meta:
                            print(f"\r  {C.GREEN}Collected remote metadata "
                                  f"for {len(meta)} files                    "
                                  f"{C.RESET}")
                        else:
                            print(f"\r  {C.DIM}No remote xattrs/ACLs found "
                                  f"to preserve                    {C.RESET}")

            # Create links locally (reflink-aware when supported)
            if link_map:
                create_links(link_map, dst, fs_strategy=fs_strategy)

            elapsed = time.time() - t0
            speed = unique_size / elapsed if elapsed > 0 else 0

            # Verify
            if not args.no_verify:
                verify_copy(copy_entries, link_map, dst)

            # Summary
            banner("DONE")
            print(f"  Source:  {C.BOLD}{src_remote.user}@{src_remote.host}:{src}{C.RESET}")
            print(f"  Dest:    {C.BOLD}{dst}{C.RESET}")
            print(f"  Files:   {C.BOLD}{total_files}{C.RESET} total"
                  + (f" ({len(copy_entries)} copied + {len(link_map)} linked)" if link_map else ""))
            if skipped_count:
                print(f"  Skipped: {C.BOLD}{skipped_count}{C.RESET} unchanged files "
                      f"({C.GREEN}{fmt_size(skipped_bytes)}{C.RESET})")
            print(f"  Data:    {C.BOLD}{fmt_size(unique_size)}{C.RESET} downloaded"
                  + (f" ({fmt_size(saved_bytes)} saved by dedup)" if saved_bytes > 0 else ""))
            print(f"  Time:    {C.BOLD}{fmt_time(elapsed)}{C.RESET}")
            print(f"  Speed:   {C.GREEN}{C.BOLD}{fmt_speed(speed)}{C.RESET}")
            _print_preserve_summary()
            if args.log_file:
                write_log_file(args.log_file, {
                    "source": f"{src_remote.user}@{src_remote.host}:{src}",
                    "destination": dst, "mode": "remote_to_local",
                    "total_files": total_files, "copied": len(copy_entries),
                    "linked": len(link_map), "skipped": skipped_count,
                    "errors": sum(1 for e in _log_entries if e["action"] == "error"),
                    "total_bytes": total_size, "bytes_written": unique_size,
                    "dedup_saved": saved_bytes, "elapsed_sec": round(elapsed, 2),
                    "avg_speed_bps": round(speed), "hash_algo": _hash_name,
                })
            print()

        except KeyboardInterrupt:
            print(f"\n  {C.YELLOW}Interrupted.{C.RESET}")
            sys.exit(130)
        except (OSError, IOError) as e:
            print(f"\n{C.RED}Error: {e}{C.RESET}")
            sys.exit(1)
        except Exception as e:
            ename = type(e).__name__
            if "Authentication" in ename:
                print(f"\n{C.RED}Error: SSH authentication failed{C.RESET}")
            elif "SSH" in ename or "Socket" in ename or "paramiko" in type(e).__module__:
                print(f"\n{C.RED}Error: SSH connection failed: {e}{C.RESET}")
            elif "ConnectionReset" in ename or "BrokenPipe" in ename:
                print(f"\n{C.RED}Error: Connection lost: {e}{C.RESET}")
            else:
                raise
            sys.exit(1)
        finally:
            if src_ssh:
                src_ssh.close()

        sys.exit(0)

    # ══════════════════════════════════════════════════════════════════
    # LOCAL → REMOTE SSH FLOW
    # ══════════════════════════════════════════════════════════════════
    if remote:
        ssh = None
        try:
            # ── Connect ──────────────────────────────────────────────
            banner("SSH — Connecting")
            password = None
            if args.ssh_password_env:
                password = os.environ.get(args.ssh_password_env)
            elif args.ssh_password:
                password = getpass.getpass(f"Password for {remote.user}@{remote.host}: ")
            elif getattr(args, "_resolved_dst_password", None):
                password = args._resolved_dst_password
            ssh = SSHConnection(remote, port=remote.port, key_path=args.ssh_key,
                                password=password, compress=args.compress,
                                ).connect()
            print(f"  {C.GREEN}Connected to {remote.user}@{remote.host}:{remote.port}{C.RESET}")
            caps = [k for k, v in ssh.caps.items() if v]
            print(f"  {C.DIM}Remote tools: {', '.join(caps) or 'none detected'}{C.RESET}")

            # ── Phase 2b: Remote incremental check ───────────────────
            skipped_count = 0
            skipped_bytes = 0

            if not args.overwrite:
                banner("Phase 2b — Remote incremental check")
                copy_entries, link_map, skipped_count, skipped_bytes = \
                    filter_unchanged_remote(copy_entries, link_map, ssh, dst)
                unique_size = sum(e.size for e in copy_entries)

                if not copy_entries and not link_map:
                    banner("DONE — Nothing to copy")
                    print(f"  All {skipped_count} files are already up to date on remote.")
                    if args.log_file:
                        write_log_file(args.log_file, {
                            "source": src_display,
                            "destination": f"{remote.user}@{remote.host}:{dst}",
                            "mode": "local_to_remote", "total_files": total_files,
                            "copied": 0, "linked": 0, "skipped": skipped_count,
                            "errors": 0, "total_bytes": total_size, "bytes_written": 0,
                            "dedup_saved": saved_bytes, "elapsed_sec": 0,
                            "avg_speed_bps": 0, "hash_algo": _hash_name,
                        })
                    print()
                    ssh.close()
                    sys.exit(0)

            # ── Phase 3: Remote space check ──────────────────────────
            banner("Phase 3 — Space check (remote)")
            required = unique_size
            print(f"  Data to write: {C.BOLD}{fmt_size(required)}{C.RESET}"
                  + (f" (after dedup saved {fmt_size(saved_bytes)})" if saved_bytes > 0 else ""))

            if not check_remote_space(ssh, dst, required, args.force):
                ssh.close()
                sys.exit(1)

            # ── Phase 4: Resolve physical layout (local source) ──────
            banner("Phase 4 — Mapping physical disk layout")
            copy_entries = resolve_physical_offsets(copy_entries, args.threads)

            if args.dry_run:
                small, large = split_by_size(copy_entries)
                small_sz = sum(e.size for e in small)
                large_sz = sum(e.size for e in large)
                print(f"\n  {C.YELLOW}DRY RUN — Copy strategy:{C.RESET}\n")
                print(f"    Small files (<1MB): {C.BOLD}{len(small)}{C.RESET} files, "
                      f"{C.BOLD}{fmt_size(small_sz)}{C.RESET} → tar stream over SSH")
                print(f"    Large files (≥1MB): {C.BOLD}{len(large)}{C.RESET} files, "
                      f"{C.BOLD}{fmt_size(large_sz)}{C.RESET} → SFTP pipelined")
                if link_map:
                    print(f"\n  Plus {len(link_map)} duplicate files to be linked on remote")
                print(f"\n  Unique data: {fmt_size(unique_size)}")
                ssh.close()
                sys.exit(0)

            # ── Phase 5: Remote copy ─────────────────────────────────
            banner("Phase 5 — Remote copy")
            ssh.exec_cmd(f"mkdir -p {shlex.quote(dst)}")

            progress = Progress(unique_size, len(copy_entries))
            t0 = time.time()
            copy_hybrid_remote(copy_entries, ssh, dst, progress, buf_size)
            progress.finish()

            # L2R extended-metadata preservation (owner/xattr/ACL). Mode and
            # mtime ride through tar headers already; owner/xattr/ACL need
            # a separate push step because the tar producer strips them or
            # the tar format doesn't carry them. We collect locally and
            # ship a serialized payload to a python3 helper on the remote.
            if _preserve_spec.owner or _preserve_spec.xattr or _preserve_spec.acl:
                want_o = _preserve_spec.owner
                want_x = _preserve_spec.xattr
                want_a = _preserve_spec.acl
                if not ssh.caps.get("python3"):
                    print(f"  {C.YELLOW}Skipping remote owner/xattr/ACL "
                          f"apply — remote lacks python3.{C.RESET}")
                else:
                    print(f"  {C.DIM}Applying remote metadata for "
                          f"{len(copy_entries)} files...{C.RESET}",
                          end="", flush=True)
                    _push_metadata_to_remote(ssh, dst, copy_entries, src,
                                             want_o, want_x, want_a)
                    print(f"\r  {C.GREEN}Applied remote metadata for "
                          f"{len(copy_entries)} files                    "
                          f"{C.RESET}")

            # Create links on remote
            if link_map:
                create_links_remote(ssh, link_map, dst)

            elapsed = time.time() - t0
            speed = unique_size / elapsed if elapsed > 0 else 0

            # ── Save manifest on remote ──────────────────────────────
            save_remote_manifest(ssh, dst, copy_entries, link_map)

            # ── Verify on remote ─────────────────────────────────────
            if not args.no_verify:
                verify_copy_remote(ssh, copy_entries, link_map, dst)

            # ── Summary ──────────────────────────────────────────────
            banner("DONE")
            print(f"  Remote:  {C.BOLD}{remote.user}@{remote.host}:{dst}{C.RESET}")
            print(f"  Files:   {C.BOLD}{total_files}{C.RESET} total"
                  + (f" ({len(copy_entries)} copied + {len(link_map)} linked)" if link_map else ""))
            if skipped_count:
                print(f"  Skipped: {C.BOLD}{skipped_count}{C.RESET} unchanged files "
                      f"({C.GREEN}{fmt_size(skipped_bytes)}{C.RESET})")
            print(f"  Data:    {C.BOLD}{fmt_size(unique_size)}{C.RESET} sent"
                  + (f" ({fmt_size(saved_bytes)} saved by dedup)" if saved_bytes > 0 else ""))
            print(f"  Time:    {C.BOLD}{fmt_time(elapsed)}{C.RESET}")
            print(f"  Speed:   {C.GREEN}{C.BOLD}{fmt_speed(speed)}{C.RESET}")
            _print_preserve_summary()
            if args.log_file:
                write_log_file(args.log_file, {
                    "source": src_display, "destination": f"{remote.user}@{remote.host}:{dst}",
                    "mode": "local_to_remote",
                    "total_files": total_files, "copied": len(copy_entries),
                    "linked": len(link_map), "skipped": skipped_count,
                    "errors": sum(1 for e in _log_entries if e["action"] == "error"),
                    "total_bytes": total_size, "bytes_written": unique_size,
                    "dedup_saved": saved_bytes, "elapsed_sec": round(elapsed, 2),
                    "avg_speed_bps": round(speed), "hash_algo": _hash_name,
                })
            print()

        except KeyboardInterrupt:
            print(f"\n  {C.YELLOW}Interrupted.{C.RESET}")
            sys.exit(130)
        except (OSError, IOError) as e:
            print(f"\n{C.RED}Error: {e}{C.RESET}")
            sys.exit(1)
        except Exception as e:
            ename = type(e).__name__
            if "Authentication" in ename:
                print(f"\n{C.RED}Error: SSH authentication failed for "
                      f"{remote.user}@{remote.host}{C.RESET}")
            elif "SSH" in ename or "Socket" in ename or "paramiko" in type(e).__module__:
                print(f"\n{C.RED}Error: SSH connection failed: {e}{C.RESET}")
            elif "ConnectionReset" in ename or "BrokenPipe" in ename:
                print(f"\n{C.RED}Error: Connection lost: {e}{C.RESET}")
            else:
                raise
            sys.exit(1)
        finally:
            if ssh:
                ssh.close()

        sys.exit(0)

    # ══════════════════════════════════════════════════════════════════
    # LOCAL FLOW
    # ══════════════════════════════════════════════════════════════════
    try:
        _run_local_flow(args, dst, copy_entries, link_map, total_size, dedup_db,
                        total_files, unique_size, saved_bytes, buf_size,
                        fs_strategy)
    finally:
        if dedup_db:
            dedup_db.close()


def _run_local_flow(args, dst, copy_entries, link_map, total_bytes, dedup_db,
                    total_files, unique_size, saved_bytes, buf_size,
                    fs_strategy=None):
    # ── Phase 2b: Skip unchanged files ────────────────────────────────
    skipped_count = 0
    skipped_bytes = 0

    if not args.overwrite and os.path.isdir(dst):
        banner("Phase 2b — Incremental check")
        copy_entries, link_map, skipped_count, skipped_bytes = filter_unchanged(
            copy_entries, link_map, dst, args.threads
        )
        unique_size = sum(e.size for e in copy_entries)

        if not copy_entries and not link_map:
            banner("DONE — Nothing to copy")
            print(f"  All {skipped_count} files are already up to date.")
            if args.log_file:
                write_log_file(args.log_file, {
                    "source": args.source, "destination": dst,
                    "mode": "local_to_local", "total_files": total_files,
                    "copied": 0, "linked": 0, "skipped": skipped_count,
                    "errors": 0, "total_bytes": total_bytes,
                    "bytes_written": 0, "dedup_saved": saved_bytes,
                    "elapsed_sec": 0, "avg_speed_bps": 0, "hash_algo": _hash_name,
                })
            print()
            return

    # ── Phase 3: Space check ──────────────────────────────────────────
    banner("Phase 3 — Space check")
    required = unique_size
    # If the destination filesystem can't dedup via links (FAT32, exFAT,
    # etc.), each duplicate will be materialized as a full copy. We need
    # space for the FULL undeduplicated size.
    if fs_strategy == "none" and saved_bytes > 0:
        full_size = unique_size + saved_bytes
        print(f"  Data to write: {C.BOLD}{fmt_size(full_size)}{C.RESET}")
        print(f"  {C.YELLOW}⚠ Filesystem does not support links — "
              f"dedup saves {fmt_size(saved_bytes)} on the wire only."
              f"{C.RESET}")
        print(f"  {C.YELLOW}  Each duplicate becomes a full copy on disk; "
              f"total disk usage will be {fmt_size(full_size)}.{C.RESET}")
        required = full_size
    else:
        # Sparse files (e.g. VM disk images): if the destination FS supports
        # holes (anything except FAT32/exFAT), only the allocated extents
        # take disk space — adjust the requirement so a 1.5 TB sparse image
        # holding 12 GB of real data doesn't reject a 500 GB destination.
        if _HAS_SEEK_HOLE and fs_strategy != "none":
            alloc_total = sum(_effective_alloc(e) for e in copy_entries)
            sparse_saved = unique_size - alloc_total
        else:
            alloc_total = unique_size
            sparse_saved = 0
        required = alloc_total
        msg = f"  Data to write: {C.BOLD}{fmt_size(required)}{C.RESET}"
        notes = []
        if saved_bytes > 0:
            notes.append(f"dedup saved {fmt_size(saved_bytes)}")
        if sparse_saved > 0:
            notes.append(f"sparse holes skipped {fmt_size(sparse_saved)}")
        if notes:
            msg += " (after " + ", ".join(notes) + ")"
        print(msg)

    if not check_destination_space(dst, required, args.force):
        sys.exit(1)

    # ── Phase 4: Resolve physical layout ──────────────────────────────
    banner("Phase 4 — Mapping physical disk layout")
    copy_entries = resolve_physical_offsets(copy_entries, args.threads)

    if args.dry_run:
        small, large = split_by_size(copy_entries)
        small_sz = sum(e.size for e in small)
        large_sz = sum(e.size for e in large)
        print(f"\n  {C.YELLOW}DRY RUN — Copy strategy:{C.RESET}\n")
        print(f"    Small files (<1MB): {C.BOLD}{len(small)}{C.RESET} files, "
              f"{C.BOLD}{fmt_size(small_sz)}{C.RESET} → single block stream (tar)")
        print(f"    Large files (≥1MB): {C.BOLD}{len(large)}{C.RESET} files, "
              f"{C.BOLD}{fmt_size(large_sz)}{C.RESET} → individual copy")
        print(f"\n  {C.YELLOW}First 20 files in disk order:{C.RESET}\n")
        for i, e in enumerate(copy_entries[:20]):
            tag = "BLK" if e.size < SMALL_FILE_THRESHOLD else "IND"
            print(f"  {i+1:4d}. [{tag}] offset={e.physical_offset:>14d}  "
                  f"size={fmt_size(e.size):>10s}  {e.rel}")
        if len(copy_entries) > 20:
            print(f"  ... and {len(copy_entries) - 20} more files")
        if link_map:
            print(f"\n  Plus {len(link_map)} duplicate files to be linked")
        # Show the on-disk bytes that will actually be written, accounting
        # for sparse holes the sparse-aware copy will skip. Matches the
        # "Data to write" figure printed in Phase 3.
        if _HAS_SEEK_HOLE and fs_strategy != "none":
            alloc_total = sum(_effective_alloc(e) for e in copy_entries)
        else:
            alloc_total = unique_size
        print(f"\n  Data to write: {fmt_size(alloc_total)}"
              + (f"  {C.DIM}(logical {fmt_size(unique_size)}, "
                 f"sparse holes skipped {fmt_size(unique_size - alloc_total)})"
                 f"{C.RESET}" if alloc_total < unique_size else ""))
        return

    # ── Phase 5: Block copy ─────────────────────────────────────────
    banner("Phase 5 — Block copy")
    os.makedirs(dst, exist_ok=True)

    progress = Progress(unique_size, len(copy_entries))
    t0 = time.time()
    copy_hybrid(copy_entries, dst, progress, buf_size, fs_strategy=fs_strategy)
    progress.finish()

    # Create links for duplicates
    if link_map:
        create_links(link_map, dst, fs_strategy=fs_strategy)

    elapsed = time.time() - t0
    speed = unique_size / elapsed if elapsed > 0 else 0

    # ── Update dedup database with copied files ──────────────────────
    if dedup_db:
        dst_rows = []
        for e in copy_entries:
            if not e.content_hash:
                continue
            dst_rows.append((e.rel, e.size, e.content_hash))
        if dst_rows:
            dedup_db.store_dest_batch(dst_rows)

    # ── Verify ────────────────────────────────────────────────────────
    if not args.no_verify:
        verify_copy(copy_entries, link_map, dst)

    # ── Summary ───────────────────────────────────────────────────────
    banner("DONE")
    print(f"  Files:   {C.BOLD}{total_files}{C.RESET} total"
          + (f" ({len(copy_entries)} copied + {len(link_map)} linked)" if link_map else ""))
    if skipped_count:
        print(f"  Skipped: {C.BOLD}{skipped_count}{C.RESET} unchanged files "
              f"({C.GREEN}{fmt_size(skipped_bytes)}{C.RESET})")
    # Match Phase 3's "Data to write" — when sparse-aware copy elided holes,
    # report the actual on-disk byte count and keep the logical total in
    # parens so the savings are visible.
    if _HAS_SEEK_HOLE and fs_strategy != "none":
        alloc_total = sum(_effective_alloc(e) for e in copy_entries)
    else:
        alloc_total = unique_size
    data_line = f"  Data:    {C.BOLD}{fmt_size(alloc_total)}{C.RESET} written"
    notes = []
    if saved_bytes > 0:
        notes.append(f"{fmt_size(saved_bytes)} saved by dedup")
    if alloc_total < unique_size:
        notes.append(f"logical {fmt_size(unique_size)}, sparse holes "
                     f"skipped {fmt_size(unique_size - alloc_total)}")
    if notes:
        data_line += f"  {C.DIM}(" + ", ".join(notes) + f"){C.RESET}"
    print(data_line)
    print(f"  Time:    {C.BOLD}{fmt_time(elapsed)}{C.RESET}")
    print(f"  Speed:   {C.GREEN}{C.BOLD}{fmt_speed(speed)}{C.RESET}")
    _print_preserve_summary()
    # Hidden audit file when running under sudo — written BEFORE write_log_file
    # since the latter clears _log_entries.
    write_sudo_audit(args.source, dst, {
        "mode": "local_to_local",
        "total_files": total_files, "copied": len(copy_entries),
        "linked": len(link_map), "skipped": skipped_count,
        "errors": sum(1 for e in _log_entries if e["action"] == "error"),
        "total_bytes": total_bytes, "bytes_written": unique_size,
        "dedup_saved": saved_bytes, "elapsed_sec": round(elapsed, 2),
        "avg_speed_bps": round(speed), "hash_algo": _hash_name,
    })
    if args.log_file:
        write_log_file(args.log_file, {
            "source": args.source, "destination": dst,
            "mode": "local_to_local",
            "total_files": total_files, "copied": len(copy_entries),
            "linked": len(link_map), "skipped": skipped_count,
            "errors": sum(1 for e in _log_entries if e["action"] == "error"),
            "total_bytes": total_bytes,
            "bytes_written": unique_size,
            "dedup_saved": saved_bytes, "elapsed_sec": round(elapsed, 2),
            "avg_speed_bps": round(speed), "hash_algo": _hash_name,
        })
    print()


def _reexec_under_sudo():
    """If --use-sudo is present and we're not already root, re-exec the whole
    command under sudo — so privileged subcommands work without the user typing
    `sudo …`: the copy path (preserve-all) and `creds lock/unlock`, which need
    root for chattr. Strips --use-sudo from the elevated argv so it doesn't
    loop. Returns (caller continues unprivileged) when no elevation is needed:
    flag absent, already root, or a platform without geteuid."""
    if "--use-sudo" not in sys.argv:
        return
    if not hasattr(os, "geteuid"):
        print(f"Error: --use-sudo is not supported on {_system}", file=sys.stderr)
        sys.exit(1)
    if os.geteuid() == 0:
        return  # already root — nothing to elevate
    # Before elevating, refuse if the script file, its directory, or the Python
    # interpreter could be modified by anyone other than the invoker or root —
    # otherwise a non-root attacker with write access to any of them would own
    # the resulting root process.
    def _check_safe_for_sudo(path, label):
        try:
            rp = os.path.realpath(path)
            st = os.stat(rp)
        except OSError as e:
            print(f"Error: --use-sudo: cannot stat {label} ({path}): {e}",
                  file=sys.stderr)
            sys.exit(1)
        me = os.geteuid()
        if st.st_uid not in (0, me):
            print(f"Error: --use-sudo: {label} {rp} is owned by uid={st.st_uid} "
                  f"(not root or invoking user uid={me}). Refusing to elevate.",
                  file=sys.stderr)
            sys.exit(1)
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            print(f"Error: --use-sudo: {label} {rp} is group/world writable "
                  f"(mode={oct(st.st_mode & 0o777)}). Fix: chmod go-w {rp}",
                  file=sys.stderr)
            sys.exit(1)
        return rp
    script_real = _check_safe_for_sudo(_get_self_path(), "script")
    _check_safe_for_sudo(os.path.dirname(script_real), "script directory")
    _check_safe_for_sudo(sys.executable, "Python interpreter")
    new_argv = [a for a in sys.argv if a != "--use-sudo"]
    try:
        os.execvp("sudo", ["sudo", sys.executable] + new_argv)
    except (OSError, FileNotFoundError) as e:
        print(f"Error: cannot exec sudo: {e}", file=sys.stderr)
        sys.exit(1)


def cli_entry():
    """Full command-line entry point: the same dispatch as running fast_copy.py
    directly — sudo re-exec, the creds/ls/deps subcommands, --version/
    --check-update/--update, then main(). Exposed as a function (not inline under
    __main__) so the GUI's `--fc-core` passthrough behaves exactly like the
    standalone CLI, not just the copy path."""
    # Elevate early (when --use-sudo is given) so it covers every subcommand,
    # including `creds lock/unlock` — must run before the creds dispatch below.
    _reexec_under_sudo()
    # `creds` manager subcommand — handled before argparse (it has its own args).
    if len(sys.argv) > 1 and sys.argv[1] == "creds":
        try:
            sys.exit(creds_manager(sys.argv[2:]))
        except KeyboardInterrupt:
            sys.stderr.write("\n  Interrupted.\n")
            sys.exit(130)
    if len(sys.argv) > 1 and sys.argv[1] in ("ls", "list-objects"):
        try:
            sys.exit(cloud_ls(sys.argv[2:]))
        except KeyboardInterrupt:
            sys.stderr.write("\n  Interrupted.\n")
            sys.exit(130)
    if len(sys.argv) > 1 and sys.argv[1] in ("deps", "check-deps", "doctor"):
        install = any(a in ("--install", "-i") for a in sys.argv[2:])
        try:
            sys.exit(check_dependencies(install=install))
        except KeyboardInterrupt:
            sys.stderr.write("\n  Interrupted.\n")
            sys.exit(130)
    # Handle --version, --check-update, --update before argparse requires positional args
    if "--version" in sys.argv or "-V" in sys.argv:
        print(f"fast-copy v{__version__}")
        sys.exit(0)
    if "--check-update" in sys.argv:
        check_update_info()
        sys.exit(0)
    if "--update" in sys.argv:
        # Refuse self-update under sudo: a compromised release publisher would
        # otherwise install a trojan that runs as root the moment the user
        # invokes the next sudo run. Force the user to update as themselves
        # first, then re-elevate explicitly.
        if (hasattr(os, "geteuid") and os.geteuid() == 0) or os.environ.get("SUDO_USER"):
            print("Error: --update is not allowed under sudo. "
                  "Run as your normal user; the updated binary will only run "
                  "as root via a separate, deliberate sudo invocation.",
                  file=sys.stderr)
            sys.exit(1)
        # Windows: clean up .old file from previous update
        if _system == "Windows":
            try:
                old = _get_self_path() + ".old"
                if os.path.exists(old):
                    os.remove(old)
            except OSError:
                pass
        # Check for optional version argument: --update v2.4.1
        idx = sys.argv.index("--update")
        target_ver = None
        if idx + 1 < len(sys.argv) and not sys.argv[idx + 1].startswith("-"):
            target_ver = sys.argv[idx + 1]
        # Optional pinned SHA-256: --update-sha256 <hex>
        expected_sha = None
        if "--update-sha256" in sys.argv:
            sidx = sys.argv.index("--update-sha256")
            if sidx + 1 >= len(sys.argv):
                print("Error: --update-sha256 requires a hex value.", file=sys.stderr)
                sys.exit(1)
            expected_sha = sys.argv[sidx + 1].strip().lower()
            if len(expected_sha) != 64 or any(c not in "0123456789abcdef" for c in expected_sha):
                print("Error: --update-sha256 must be 64 hex characters.", file=sys.stderr)
                sys.exit(1)
        self_update(target_version=target_ver, expected_sha256=expected_sha)
        _post_update_dep_check()
        sys.exit(0)
    # --use-sudo elevation is handled at the top of __main__ (see
    # _reexec_under_sudo) so it also covers the `creds` subcommands.

    # Windows: clean up .old file from previous update on normal runs
    if _system == "Windows":
        try:
            old = _get_self_path() + ".old"
            if os.path.exists(old):
                os.remove(old)
        except OSError:
            pass
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n  {C.YELLOW}Interrupted.{C.RESET}")
        sys.exit(130)


if __name__ == "__main__":
    cli_entry()
