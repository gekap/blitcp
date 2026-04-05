#!/usr/bin/env python3
# Copyright 2026 George Kapellakis
# Licensed under the Apache License, Version 2.0
# See LICENSE file for details.
"""
FAST BLOCK-ORDER COPY — Copies files and folders at maximum speed via SSH.

Supports all four copy modes:
  • local  → local   (block-order copy with dedup)
  • local  → remote  (SFTP + tar stream to SSH server)
  • remote → local   (SFTP + tar stream from SSH server)
  • remote → remote  (relay through local machine: src SSH → dst SSH)

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

Usage:
  python fast_copy.py <source> <destination>

  Source and destination can be local paths or remote SSH paths (user@host:/path).

Examples:
  # Local to local
  python fast_copy.py /home/user/data /media/usb/data

  # Local to remote
  python fast_copy.py /data user@server:/backup/data

  # Remote to local
  python fast_copy.py user@server:/data /local/backup

  # Remote to remote (relay through local machine)
  python fast_copy.py user@src-host:/data user@dst-host:/backup/data

  # With options
  python fast_copy.py user@src:/data user@dst:/backup -z --no-dedup
  python fast_copy.py user@host:/data /local --src-port 2222

Options:
  --buffer MB       Read/write buffer size in MB (default: 64)
  --threads N       Threads for hashing & layout resolution (default: 4)
  --dry-run         Show copy plan without copying
  --no-verify       Skip post-copy verification
  --no-dedup        Disable deduplication (copy all files even if identical)
  --no-cache        Disable persistent hash cache (cross-run dedup database)
  --force           Skip space check and copy anyway
  --ssh-dst-port PORT   SSH port for remote destination (default: 22)
  --ssh-dst-key PATH    SSH private key for remote destination
  --ssh-dst-password    Prompt for SSH password for destination
  --ssh-src-port PORT   SSH port for remote source (default: 22)
  --ssh-src-key PATH    SSH private key for remote source
  --ssh-src-password    Prompt for SSH password for source
  -z, --compress    Enable SSH compression (good for slow links)
  --version, -V     Show version and exit
  --check-update    Show available updates and release notes
  --update [VERSION]  Download and install (latest, or a specific version)

Requires: python -m pip install paramiko
"""

import os
import sys
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
import sqlite3
import tempfile
import re
import getpass
import posixpath
import shlex
import argparse
import platform
import threading
from pathlib import Path
from collections import namedtuple, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# ════════════════════════════════════════════════════════════════════════════
# VERSION
# ════════════════════════════════════════════════════════════════════════════
__version__ = "2.4.3"
GITHUB_REPO = "gekap/fast-copy"

# ════════════════════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════════════════════
DEFAULT_BUFFER_MB = 64
DEFAULT_THREADS = 4
HASH_CHUNK = 1048571            # ~1MB chunks for hashing (prime for alignment)
HASH_ALGO = "xxh128"            # try xxhash first, fallback to sha256

FileEntry = namedtuple("FileEntry", ["src", "rel", "size", "physical_offset", "content_hash"])

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
        "files": _log_entries,
    }
    with open(path, "w") as f:
        json.dump(log, f, indent=2)
    print(f"  Log:     {C.BOLD}{path}{C.RESET}")


# ════════════════════════════════════════════════════════════════════════════
# TERMINAL OUTPUT
# ════════════════════════════════════════════════════════════════════════════
_is_tty = sys.stdout.isatty()

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
# ════════════════════════════════════════════════════════════════════════════
try:
    import xxhash
    def new_hasher():
        return xxhash.xxh128()
    _hash_name = "xxh128"
except ImportError:
    def new_hasher():
        return hashlib.sha256()
    _hash_name = "sha256"


_EMPTY_HASH = new_hasher().hexdigest()  # hash of zero bytes for active algorithm


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
    # Resolve final path and verify it stays within dst_root
    resolved = os.path.realpath(os.path.join(dst_root, member.name))
    real_dst = os.path.realpath(dst_root)
    if not resolved.startswith(real_dst + os.sep) and resolved != real_dst:
        return "blocked: resolves outside destination"
    return True


def _safe_tar_extract(tar, member, dst_root):
    """Extract a single tar member safely. Returns True on success, error string on failure."""
    check = _validate_tar_member(member, dst_root)
    if check is not True:
        return check
    # Safe to extract — sanitize member metadata
    member.uid = member.gid = 0
    member.uname = member.gname = ""
    extract_path = _long_path(dst_root) if _system == "Windows" else dst_root
    try:
        tar.extract(member, path=extract_path, filter='data')
    except TypeError:
        # Python <3.12: filter not supported, but member is already sanitized
        tar.extract(member, path=extract_path)
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

    Two tables:
      source_cache  — keyed on (source rel_path, size, mtime_ns)
                      Speeds up hashing: same source file → same hash
                      regardless of which destination subfolder you copy to.
      dest_files    — keyed on mount-relative path
                      Tracks what's actually on the drive for cross-run dedup.
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
        # Reject if db_path is a symlink (prevents symlink attack to write elsewhere)
        if os.path.islink(db_path):
            raise OSError(f"Refusing to open dedup DB: {db_path} is a symlink")
        # Create DB with restrictive permissions (owner-only) to avoid race
        old_umask = os.umask(0o077)
        try:
            self.conn = sqlite3.connect(db_path, check_same_thread=False)
        finally:
            os.umask(old_umask)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")  # WAL default, crash-safe
        self.conn.execute("PRAGMA user_version=4718")  # schema v2
        self.lock = threading.Lock()
        self._init_schema()

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
    user = m.group(1) or getpass.getuser()
    host = m.group(2)
    path = m.group(3)
    return RemoteSpec(user=user, host=host, port=22, path=path)


_ParamikoHostKeyBase = paramiko.MissingHostKeyPolicy if _has_paramiko else object


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
        # Save to ~/.ssh/known_hosts
        known_hosts = os.path.expanduser("~/.ssh/known_hosts")
        os.makedirs(os.path.dirname(known_hosts), exist_ok=True)
        try:
            host_keys = paramiko.HostKeys(known_hosts)
        except (IOError, OSError):
            host_keys = paramiko.HostKeys()
        host_keys.add(hostname, key_type, key)
        try:
            host_keys.save(known_hosts)
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

    def connect(self):
        self.client = paramiko.SSHClient()
        # Load system known_hosts for host key verification
        try:
            self.client.load_system_host_keys()
        except IOError:
            pass
        known_hosts = os.path.expanduser("~/.ssh/known_hosts")
        if os.path.isfile(known_hosts):
            try:
                self.client.load_host_keys(known_hosts)
            except IOError:
                pass
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


def filter_unchanged_remote(entries, link_map, ssh, remote_root):
    """Incremental check against remote. Uses manifest or find+hash."""
    print(f"  {C.DIM}Checking remote for existing files...{C.RESET}", end="", flush=True)

    # Try manifest first (instant), fall back to find
    manifest = load_remote_manifest(ssh, remote_root)
    if manifest:
        print(f"\r  {C.DIM}Loaded manifest ({len(manifest)} entries){C.RESET}          ")
        remote_files = {k: v["size"] for k, v in manifest.items()}
        remote_hashes = {k: v.get("hash") for k, v in manifest.items()}
    else:
        print(f"\r  {C.DIM}No manifest, scanning remote...{C.RESET}          ")
        remote_files = scan_remote_destination(ssh, remote_root)
        remote_hashes = {}

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
        for entry in need_hash:
            rh = remote_h.get(entry.rel)
            # Remote used sha256, local may have xxh128 — compare if same algo
            # For cross-algo: re-hash locally with sha256 for comparison
            if rh and entry.content_hash:
                # If hashes match (same algo) skip; otherwise must copy
                # Since remote always uses sha256, re-hash source with sha256
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
def check_destination_space(dst, required_bytes, force=False):
    """
    Check if destination has enough free space.
    Creates the destination directory if needed to query its filesystem.
    Returns True if OK to proceed, False to abort.
    """
    # Create dst if it doesn't exist so we can stat its filesystem
    os.makedirs(dst, exist_ok=True)

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


def _long_path(p):
    """On Windows, prefix paths with \\\\?\\ to bypass the 260-char MAX_PATH limit.
    Use ONLY for actual file I/O (open, makedirs, walk), NOT for path comparison."""
    if _system == "Windows" and not p.startswith("\\\\?\\"):
        return "\\\\?\\" + os.path.abspath(p)
    return p


def _strip_long_path(p):
    """Strip the \\\\?\\ prefix if present (for path comparison/relpath)."""
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

    # Compile exclude patterns
    exclude_names = {TAR_BUNDLE_NAME, DEDUP_DB_NAME, REMOTE_MANIFEST_NAME}  # skip our own files
    if excludes:
        for ex in excludes:
            exclude_names.add(ex)

    def on_walk_error(err):
        """Called by os.walk when it can't list a directory."""
        dir_errors.append((err.filename, str(err)))

    # followlinks=True so we traverse Windows junctions & symlinks
    # Use _long_path on Windows to see files beyond 260-char MAX_PATH
    walk_src = _long_path(src_root)
    symlink_warnings = []
    for root, dirs, files in os.walk(walk_src, followlinks=True, onerror=on_walk_error):
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

        for fname in files:
            # Skip excluded files
            if fname in exclude_names:
                continue

            src_path = os.path.join(root, fname)
            rel_path = os.path.relpath(_strip_long_path(src_path), src_root).replace(os.sep, "/")
            try:
                sz = os.path.getsize(src_path)
                entries.append(FileEntry(
                    src=src_path, rel=rel_path, size=sz,
                    physical_offset=0, content_hash=None,
                ))
                scan_count += 1
                if scan_count % 1000 == 0:
                    print(f"\r  {C.DIM}Scanning... {scan_count} files{C.RESET}", end="", flush=True)
            except OSError as e:
                errors.append((_strip_long_path(src_path), str(e)))

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
        FileEntry(e.src, e.rel, e.size, offsets[i], e.content_hash)
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
def deduplicate(entries, threads=DEFAULT_THREADS, dedup_db=None):
    """
    Content-aware deduplication:
      1. Hash ALL files (using cache when available)
      2. Group by (size, hash) — same-size pre-filter for within-run dedup
      3. Cross-run dedup: check if drive already has matching content
      4. Return (unique_entries, link_map)
    """
    print(f"  Using hash: {C.BOLD}{_hash_name}{C.RESET}")
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
        FileEntry(e.src, e.rel, e.size, e.physical_offset, hashes[i])
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

        if canonical is not None:
            # Normal dedup: first file is canonical, rest are linked
            unique_entries.append(canonical)
            for dup in group[1:]:
                link_map[dup.rel] = canonical.rel
                saved_bytes += dup.size

    dup_count = len(link_map)
    within_run = dup_count - crossrun_count
    total_files = len(entries)

    print(f"\r  {C.GREEN}Dedup complete:{C.RESET}                              ")
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
    print(f"    Total duplicates:{C.BOLD} {dup_count}{C.RESET} "
          f"({fmt_pct(dup_count, total_files)} of files)")
    print(f"    Space saved:     {C.GREEN}{C.BOLD}{fmt_size(saved_bytes)}{C.RESET} "
          f"({fmt_pct(saved_bytes, sum(e.size for e in entries))} reduction)")

    return unique_entries, link_map, saved_bytes


def create_links(link_map, dst_root):
    """
    Create hard links for deduplicated files.
    Falls back to symlinks if hard links fail (e.g. FAT32/exFAT USB).
    Falls back to actual copy as last resort.
    """
    if not link_map:
        return

    print(f"  {C.DIM}Creating {len(link_map)} links for duplicates...{C.RESET}", end="", flush=True)
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

        # Try hard link first (fastest, no extra space)
        try:
            os.link(dst_canonical, dst_dup)
            _log("linked", dup_rel, _link_size, method="hardlink", link_target=_link_target)
            hardlink_ok += 1
            continue
        except OSError:
            pass

        # Try symlink (works on most filesystems)
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

        # Last resort: actual copy
        try:
            shutil.copy2(dst_canonical, dst_dup)
            _log("linked", dup_rel, _link_size, method="copy_fallback", link_target=_link_target)
            copy_fallback += 1
        except OSError as e:
            _log("error", dup_rel, _link_size, error=str(e))
            errors += 1

    results = []
    if hardlink_ok:
        results.append(f"{hardlink_ok} hard links")
    if symlink_ok:
        results.append(f"{symlink_ok} symlinks")
    if copy_fallback:
        results.append(f"{copy_fallback} copies (fallback)")
    if errors:
        results.append(f"{C.RED}{errors} errors{C.RESET}")

    print(f"\n  {C.GREEN}Links created: {', '.join(results)}{C.RESET}                    ")


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
            new_entries.append(FileEntry(e.src, renames[e.rel], e.size,
                                        e.physical_offset, e.content_hash))
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

        pct = (self.bytes_done / self.total_bytes * 100) if self.total_bytes else 100
        speed = self.bytes_done / elapsed
        eta = (self.total_bytes - self.bytes_done) / speed if speed > 0 else 0

        bar_w = 30
        filled = int(bar_w * min(pct, 100) / 100)
        bar = "█" * filled + "░" * (bar_w - filled)

        sys.stdout.write(
            f"\r  {C.CYAN}{bar}{C.RESET} {pct:5.1f}%  "
            f"{fmt_size(self.bytes_done)}/{fmt_size(self.total_bytes)}  "
            f"{C.GREEN}{fmt_speed(speed)}{C.RESET}  "
            f"{self.files_done}/{self.total_files} files  "
            f"ETA {fmt_time(eta)}   "
        )
        sys.stdout.flush()

    def finish(self):
        elapsed = time.time() - self.start
        speed = self.bytes_done / elapsed if elapsed > 0 else 0
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
                        with open(entry.src, "rb") as f:
                            data = f.read(SMALL_FILE_THRESHOLD + 1)

                        info = tarfile.TarInfo(name=entry.rel)
                        info.size = len(data)
                        try:
                            st = os.stat(entry.src)
                            info.mtime = st.st_mtime
                            info.mode = st.st_mode
                        except OSError:
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


def copy_individual(entries, dst_root, progress, buf, cancel_check=None):
    """Copy large files individually with big buffers in physical disk order."""
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
                with open(dst_path, "wb"):
                    pass
                try:
                    st = os.stat(entry.src)
                    os.utime(dst_path, (st.st_atime, st.st_mtime))
                    os.chmod(dst_path, stat.S_IMODE(st.st_mode))
                except OSError:
                    pass
                _log("copied", entry.rel, entry.size, method="individual")
                progress.update(0, 1)
                progress.display()
                continue

            with open(entry.src, "rb") as fin, open(dst_path, "wb") as fout:
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

            # Preserve timestamps and permissions
            try:
                st = os.stat(entry.src)
                os.utime(dst_path, (st.st_atime, st.st_mtime))
                os.chmod(dst_path, stat.S_IMODE(st.st_mode))
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


def copy_hybrid(entries, dst_root, progress, buf_size, cancel_check=None):
    """
    Hybrid block copy engine:
      - Small files (<1MB): bundled into tar → single block write → extract
      - Large files (>=1MB): individual copy with large buffers
    """
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
        copy_individual(large, dst_root, progress, buf, cancel_check)
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

            with open(entry.src, "rb") as fin:
                with sftp.open(remote_path, "wb") as fout:
                    fout.set_pipelined(True)
                    while True:
                        n = fin.readinto(buf)
                        if not n:
                            break
                        fout.write(mv[:n])
                        progress.update(n)
                        progress.display()

            # Preserve timestamps
            try:
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
    """Split entries into batches of approximately max_bytes or max_files each."""
    batches = []
    current = []
    current_size = 0
    for e in entries:
        current.append(e)
        current_size += e.size
        if current_size >= max_bytes or len(current) >= max_files:
            batches.append(current)
            current = []
            current_size = 0
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
                    st = os.stat(entry.src)
                    actual_size = st.st_size
                    info = tarfile.TarInfo(name=entry.rel)
                    info.size = actual_size
                    info.mtime = st.st_mtime
                    info.mode = st.st_mode & 0o7777

                    with open(entry.src, "rb") as f:
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
    mismatches = []

    for entry in entries:
        if entry.rel not in remote_files:
            missing.append(entry.rel)
        elif remote_files[entry.rel] != entry.size:
            mismatches.append((entry.rel, entry.size, remote_files[entry.rel]))

    for dup_rel in link_map:
        if dup_rel not in remote_files:
            missing.append(f"{dup_rel} (link)")

    total_checked = total_to_check

    # Hash spot-check: verify a sample of files by hashing on remote
    # remote_hash_files always uses sha256, so re-hash locally with sha256
    hash_failures = []
    if not missing and not mismatches and entries:
        hashed_entries = [e for e in entries if e.content_hash]
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
        shown = min(len(missing), 10) + min(len(mismatches), 10) + min(len(hash_failures), 10)
        remain = len(missing) + len(mismatches) + len(hash_failures) - shown
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
            rel = os.path.relpath(_strip_long_path(full), rel_base).replace(os.sep, "/")
            if rel in expected:
                try:
                    found[rel] = os.path.getsize(full)
                except OSError:
                    pass

    mismatches = []
    missing = []
    for rel, exp_size in expected.items():
        if rel not in found:
            tag = " (link)" if exp_size is None else ""
            missing.append(f"{rel}{tag}")
        elif exp_size is not None and found[rel] != exp_size:
            mismatches.append((rel, exp_size, found[rel]))

    total_checked = len(expected)

    if not missing and not mismatches:
        print(f"\r  {C.GREEN}✓ Verified: all {total_checked} files OK{C.RESET}               ")
        return True
    else:
        print(f"\r  {C.RED}✗ Verification failed:{C.RESET}")
        for m in missing[:10]:
            print(f"    {C.RED}MISSING: {m}{C.RESET}")
        for rel, exp, act in mismatches[:10]:
            print(f"    {C.RED}SIZE MISMATCH: {rel} ({exp} → {act}){C.RESET}")
        shown = min(len(missing), 10) + min(len(mismatches), 10)
        remain = len(missing) + len(mismatches) - shown
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

    exclude_names = {TAR_BUNDLE_NAME, DEDUP_DB_NAME, REMOTE_MANIFEST_NAME}
    if excludes:
        for ex in excludes:
            exclude_names.add(ex)

    exclude_args = " ".join(
        f"-not -name {shlex.quote(n)}" for n in exclude_names
    )

    if ssh.caps.get("gnu_find"):
        cmd = (f'find {shlex.quote(src_root)} -type f {exclude_args} '
               f'-printf "%s\\t%p\\n" 2>/dev/null')
    else:
        cmd = (f'find {shlex.quote(src_root)} -type f {exclude_args} '
               f'-exec stat -c "%s %n" {{}} + 2>/dev/null || '
               f'find {shlex.quote(src_root)} -type f {exclude_args} '
               f'-exec stat -f "%z %N" {{}} + 2>/dev/null')

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


def deduplicate_remote_source(entries, ssh, src_root, threads=DEFAULT_THREADS):
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
        hashed_entries.append(FileEntry(e.src, e.rel, e.size, e.physical_offset, h))
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
    print(f"  {C.GREEN}Dedup complete:{C.RESET}")
    print(f"    Unique files:    {C.BOLD}{len(unique_entries)}{C.RESET}")
    print(f"    Duplicates:      {C.BOLD}{dup_count}{C.RESET} "
          f"({fmt_pct(dup_count, len(entries))} of files)")
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
                with open(dst_path, "wb"):
                    pass
                try:
                    rstat = sftp.stat(remote_path)
                    os.utime(dst_path, (rstat.st_atime, rstat.st_mtime))
                    os.chmod(dst_path, stat.S_IMODE(rstat.st_mode))
                except (OSError, IOError):
                    pass
                _log("copied", entry.rel, entry.size, method="sftp")
                progress.update(0, 1)
                progress.display()
                continue

            with sftp.open(remote_path, "rb") as fin, open(dst_path, "wb") as fout:
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
        # Validate with plain paths, use _long_path only for I/O
        resolved = os.path.realpath(os.path.join(self._dst_root, member.name))
        real_dst = os.path.realpath(self._dst_root)
        if not resolved.startswith(real_dst + os.sep) and resolved != real_dst:
            return "blocked: resolves outside destination"

        io_path = _long_path(resolved)
        os.makedirs(os.path.dirname(io_path), exist_ok=True)
        fileobj = self._tar.extractfile(member)
        if fileobj is None:
            return "blocked: cannot extract"

        written = 0
        try:
            with open(io_path, "wb") as fout:
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
        f"cd {shlex.quote(src_root)} && tar cf - --null -T /dev/stdin"
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
        f"cd {shlex.quote(src_root)} && tar cf - --null -T /dev/stdin"
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
        allowed_set = set(e.rel for e in safe_entries)
        # Also allow parent directories of allowed files
        for e in safe_entries:
            parts = e.rel.split("/")
            for i in range(1, len(parts)):
                allowed_set.add("/".join(parts[:i]))
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


def self_update(target_version=None):
    """Download and install a release. If target_version is None, install latest."""
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
        print(f"\n  Downloading...", end="", flush=True)
        req = urllib.request.Request(download_url, headers={
            "User-Agent": f"fast-copy/{__version__}",
        })
        ssl_ctx = _get_ssl_context()
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

        # Compute SHA-256 of download for audit trail
        dl_hash = hashlib.sha256(data).hexdigest()
        print(f"  SHA-256: {C.DIM}{dl_hash}{C.RESET}")

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
# MAIN
# ════════════════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(
        description="Block-order fast copy with dedup — reads files in physical "
                    "disk order, deduplicates identical files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("source", help="Source folder, file, or glob pattern (e.g. *.zip)")
    parser.add_argument("destination", help="Destination (USB drive path, etc)")
    parser.add_argument("--buffer", type=int, default=DEFAULT_BUFFER_MB,
                        help=f"Buffer size in MB (default: {DEFAULT_BUFFER_MB})")
    parser.add_argument("--threads", type=int, default=DEFAULT_THREADS,
                        help=f"Threads for hashing/layout (default: {DEFAULT_THREADS})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show copy plan without copying")
    parser.add_argument("--no-verify", action="store_true",
                        help="Skip post-copy verification")
    parser.add_argument("--log-file", default=None,
                        help="Write structured JSON log to file")
    parser.add_argument("--no-dedup", action="store_true",
                        help="Disable deduplication")
    parser.add_argument("--force", action="store_true",
                        help="Skip space check, copy even if not enough space")
    parser.add_argument("--overwrite", action="store_true",
                        help="Overwrite all files, skip identical-file detection")
    parser.add_argument("--exclude", action="append", default=[],
                        help="Exclude files/dirs by name (can use multiple times)")
    parser.add_argument("--no-cache", action="store_true",
                        help="Disable persistent hash cache (cross-run dedup database)")
    # Destination SSH options
    parser.add_argument("--ssh-dst-port", "--ssh-port", type=int, default=22,
                        dest="ssh_port",
                        help="SSH port for remote destination (default: 22)")
    parser.add_argument("--ssh-dst-key", "--ssh-key", default=None,
                        dest="ssh_key",
                        help="Path to SSH private key for remote destination")
    parser.add_argument("--ssh-dst-password", "--ssh-password", action="store_true",
                        dest="ssh_password",
                        help="Prompt for SSH password for remote destination")
    parser.add_argument("-z", "--compress", action="store_true",
                        help="Enable SSH compression (good for slow links)")
    # Source SSH options
    parser.add_argument("--ssh-src-port", "--src-port", type=int, default=22,
                        dest="src_port",
                        help="SSH port for remote source (default: 22)")
    parser.add_argument("--ssh-src-key", "--src-key", default=None,
                        dest="src_key",
                        help="Path to SSH private key for remote source")
    parser.add_argument("--ssh-src-password", "--src-password", action="store_true",
                        dest="src_password",
                        help="Prompt for SSH password for remote source")
    args = parser.parse_args()

    global _log_enabled
    if args.log_file:
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
    src_mode = None  # "dir", "file", "glob", or "remote"
    glob_files = []

    if src_remote:
        src = src_remote.path
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

    banner("FAST BLOCK-ORDER COPY")
    print(f"  Source:      {C.BOLD}{src_display}{C.RESET}")
    if src_mode == "remote":
        print(f"               {C.DIM}(SSH remote, port {src_remote.port}){C.RESET}")
    elif src_mode == "glob":
        print(f"               {C.DIM}{len(glob_files)} files matched{C.RESET}")
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
    print(f"  Dedup:       {'disabled' if args.no_dedup else 'enabled'}")
    if not src_remote and not dst_remote:
        print(f"  Hash cache:  {'disabled' if args.no_cache else 'enabled'}")
    print(f"  Overwrite:   {'always' if args.overwrite else 'skip identical'}")
    if (src_remote or dst_remote) and args.compress:
        print(f"  Compression: {C.GREEN}enabled{C.RESET}")
    print(f"  Platform:    {_system}")
    print()

    # ── Connect to remote source if needed ─────────────────────────────
    src_ssh = None
    if src_remote:
        banner("SSH — Connecting to source")
        src_password = None
        if args.src_password:
            src_password = getpass.getpass(f"Password for {src_remote.user}@{src_remote.host}: ")
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
    elif src_mode == "file":
        fname = os.path.basename(src)
        sz = os.path.getsize(src)
        entries = [FileEntry(src=src, rel=fname, size=sz,
                             physical_offset=0, content_hash=None)]
        errors = []
        print(f"  {C.GREEN}Found 1 file{C.RESET} ({fmt_size(sz)})")
        src = os.path.dirname(src)
    elif src_mode == "glob":
        entries = []
        errors = []
        for fpath in glob_files:
            abs_f = os.path.abspath(fpath)
            rel = os.path.relpath(abs_f, src)
            try:
                sz = os.path.getsize(abs_f)
                entries.append(FileEntry(src=abs_f, rel=rel, size=sz,
                                         physical_offset=0, content_hash=None))
            except OSError as e:
                errors.append((abs_f, str(e)))
        print(f"  {C.GREEN}Found {len(entries)} files{C.RESET}")
    else:
        entries, errors = scan_source(src, dst if not dst_remote else None, args.exclude)

    if not entries:
        print(f"  {C.YELLOW}No files found.{C.RESET}")
        if src_ssh:
            src_ssh.close()
        sys.exit(0)

    total_size = sum(e.size for e in entries)
    total_files = len(entries)
    avg_size = total_size / total_files if total_files else 0
    print(f"  Total: {C.BOLD}{fmt_size(total_size)}{C.RESET} in "
          f"{C.BOLD}{total_files}{C.RESET} files  "
          f"(avg {fmt_size(avg_size)}/file)")

    # ── Phase 2: Deduplication ───────────────────────────────────────
    dedup_db = None
    if not src_remote and not dst_remote and not args.no_dedup and not args.no_cache:
        os.makedirs(dst, exist_ok=True)
        try:
            dedup_db = DedupDB(dst)
        except Exception as e:
            print(f"  {C.YELLOW}Warning: could not open hash cache: {e}{C.RESET}")

    link_map = {}
    saved_bytes = 0
    copy_entries = entries

    if not args.no_dedup:
        banner("Phase 2 — Deduplication")
        if src_remote:
            copy_entries, link_map, saved_bytes = deduplicate_remote_source(
                entries, src_ssh, src, args.threads)
        else:
            copy_entries, link_map, saved_bytes = deduplicate(entries, args.threads, dedup_db)

    unique_size = sum(e.size for e in copy_entries)

    # ── Case-conflict resolution for local destinations ─────────────
    case_renames = {}
    if not dst_remote:
        copy_entries, link_map, case_renames = resolve_case_conflicts(
            copy_entries, link_map, dst)

    # ══════════════════════════════════════════════════════════════════
    # REMOTE → REMOTE FLOW
    # ══════════════════════════════════════════════════════════════════
    if src_remote and dst_remote:
        dst_ssh = None
        try:
            # ── Connect to destination ───────────────────────────────
            banner("SSH — Connecting to destination")
            dst_password = None
            if args.ssh_password:
                dst_password = getpass.getpass(f"Password for {dst_remote.user}@{dst_remote.host}: ")
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
                        filter_unchanged_remote(copy_entries, link_map, dst_ssh, dst)
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
                        stat = MEMORYSTATUSEX()
                        stat.dwLength = ctypes.sizeof(stat)
                        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
                        avail = stat.ullAvailPhys
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
            print(f"  Data to write: {C.BOLD}{fmt_size(required)}{C.RESET}"
                  + (f" (after dedup saved {fmt_size(saved_bytes)})" if saved_bytes > 0 else ""))

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

            # Create links locally
            if link_map:
                create_links(link_map, dst)

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
            if args.ssh_password:
                password = getpass.getpass(f"Password for {remote.user}@{remote.host}: ")
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
        _run_local_flow(args, dst, copy_entries, link_map, entries, dedup_db,
                        total_files, unique_size, saved_bytes, buf_size)
    finally:
        if dedup_db:
            dedup_db.close()


def _run_local_flow(args, dst, copy_entries, link_map, entries, dedup_db,
                    total_files, unique_size, saved_bytes, buf_size):
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
                    "errors": 0, "total_bytes": sum(e.size for e in entries),
                    "bytes_written": 0, "dedup_saved": saved_bytes,
                    "elapsed_sec": 0, "avg_speed_bps": 0, "hash_algo": _hash_name,
                })
            print()
            return

    # ── Phase 3: Space check ──────────────────────────────────────────
    banner("Phase 3 — Space check")
    required = unique_size
    print(f"  Data to write: {C.BOLD}{fmt_size(required)}{C.RESET}"
          + (f" (after dedup saved {fmt_size(saved_bytes)})" if saved_bytes > 0 else ""))

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
        print(f"\n  Unique data: {fmt_size(unique_size)}")
        return

    # ── Phase 5: Block copy ─────────────────────────────────────────
    banner("Phase 5 — Block copy")
    os.makedirs(dst, exist_ok=True)

    progress = Progress(unique_size, len(copy_entries))
    t0 = time.time()
    copy_hybrid(copy_entries, dst, progress, buf_size)
    progress.finish()

    # Create links for duplicates
    if link_map:
        create_links(link_map, dst)

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
    print(f"  Data:    {C.BOLD}{fmt_size(unique_size)}{C.RESET} written"
          + (f" ({fmt_size(saved_bytes)} saved by dedup)" if saved_bytes > 0 else ""))
    print(f"  Time:    {C.BOLD}{fmt_time(elapsed)}{C.RESET}")
    print(f"  Speed:   {C.GREEN}{C.BOLD}{fmt_speed(speed)}{C.RESET}")
    if args.log_file:
        write_log_file(args.log_file, {
            "source": args.source, "destination": dst,
            "mode": "local_to_local",
            "total_files": total_files, "copied": len(copy_entries),
            "linked": len(link_map), "skipped": skipped_count,
            "errors": sum(1 for e in _log_entries if e["action"] == "error"),
            "total_bytes": sum(e.size for e in entries),
            "bytes_written": unique_size,
            "dedup_saved": saved_bytes, "elapsed_sec": round(elapsed, 2),
            "avg_speed_bps": round(speed), "hash_algo": _hash_name,
        })
    print()


if __name__ == "__main__":
    # Handle --version, --check-update, --update before argparse requires positional args
    if "--version" in sys.argv or "-V" in sys.argv:
        print(f"fast-copy v{__version__}")
        sys.exit(0)
    if "--check-update" in sys.argv:
        check_update_info()
        sys.exit(0)
    if "--update" in sys.argv:
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
        self_update(target_version=target_ver)
        sys.exit(0)
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
