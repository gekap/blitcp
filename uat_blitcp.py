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
           http   — http(s):// SOURCE relays to SSH/SMB (refusals run anywhere;
                    the transfers need localhost sshd / FC_UAT_SMB_URL, and the
                    https ones a self-signed cert this suite generates)
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


def _mount_root(path):
    """Mount point *path* lives on — the other place a run can write. The dedup
    cache prefers the destination's mount root and falls back to the
    destination only when that root is /, so a check that watches the
    destination alone passes on any box where /tmp is its own mount."""
    p = os.path.abspath(path)
    while not os.path.ismount(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return p


def _dir_names(d):
    """Top-level entry names in *d*, empty when it does not exist."""
    try:
        return set(os.listdir(d))
    except OSError:
        return set()


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


def run_fc(target, args, timeout=240, env_extra=None):
    env = dict(os.environ)
    env["NO_COLOR"] = "1"
    env["BLITCP_CREDENTIALS"] = _NULL_CREDS
    # Scenario-supplied variables (SSL_CERT_FILE for the throwaway origin cert,
    # PYTHONPATH for the sitecustomize that refuses SFTP, a password env var).
    env.update(env_extra or {})
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


# ── HTTP(S) origin for the relay scenarios ───────────────────────────────────
# The http(s):// source needs a real origin: something that speaks Range,
# Last-Modified and Content-Disposition, and that can misbehave deliberately.
# Stdlib only, always bound to 127.0.0.1 on port 0 — a fixed port turns a busy
# machine into a suite failure instead of a skip.

import email.utils
import http.server
import socket
import ssl
import struct
import threading
import time
import urllib.parse


class _OriginHandler(http.server.BaseHTTPRequestHandler):
    """One handler, several personalities, chosen by server.mode."""
    protocol_version = "HTTP/1.1"
    server_version = "uat-origin/1"

    def log_message(self, *a):
        pass                        # a UAT run is not a web-server access log

    def _disk_path(self):
        rel = urllib.parse.unquote(self.path.split("?")[0]).lstrip("/")
        # Never let a crafted path escape the served directory.
        full = os.path.normpath(os.path.join(self.server.root, rel))
        root = os.path.normpath(self.server.root)
        return full if full == root or full.startswith(root + os.sep) else None

    def _send(self, code, body=b"", ctype="application/octet-stream",
              extra=None, last_modified=True):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        if last_modified:
            self.send_header("Last-Modified",
                             email.utils.formatdate(self.server.mtime,
                                                    usegmt=True))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):                                       # noqa: N802
        srv = self.server
        srv.hits.append((self.path, dict(self.headers)))
        mode = srv.mode

        if mode == "wall":
            # A captive login page returned with 200 for a URL that names a
            # binary — what a vendor download link does behind SSO.
            self._send(200, b"<html><head><title>Sign in</title></head>"
                            b"<body>Please log in to continue.</body></html>",
                       ctype="text/html; charset=utf-8")
            return

        if mode == "auth" and not srv.authorized(self.headers):
            self._send(401, b"denied", extra={"WWW-Authenticate": "Basic realm=uat"})
            return

        if mode == "hostile" and self.path.startswith("/downloads/"):
            # Redirect to a DIFFERENTLY named path; the response there also
            # claims another name via Content-Disposition. Neither may be
            # allowed to decide where the bytes land.
            self.send_response(302)
            self.send_header("Location", "/real/decoy.bin")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        path = self._disk_path()
        if path is None or not os.path.isfile(path):
            self._send(404, b"not found", last_modified=False)
            return
        with open(path, "rb") as f:
            data = f.read()
        total = len(data)

        start = 0
        rng = self.headers.get("Range") or ""
        if rng.startswith("bytes="):
            try:
                start = int(rng.split("=", 1)[1].split("-")[0] or 0)
            except ValueError:
                start = 0
        if start:
            srv.ranged.append(start)

        if mode in ("drop", "drop_fin") and not srv.dropped:
            # Fail the FIRST attempt part-way through, with a RESET rather than
            # a graceful close: a clean FIN mid-body surfaces as an empty read
            # and is caught by the truncation guard, never by the resume path,
            # so it would exercise the wrong branch (see UAT-HTTP-13).
            srv.dropped = True
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(total))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Last-Modified",
                             email.utils.formatdate(srv.mtime, usegmt=True))
            self.end_headers()
            try:
                self.wfile.write(data[:srv.drop_at])
                self.wfile.flush()
                # Give the client time to actually consume a 1 MB chunk before
                # the connection dies; a reset issued immediately discards the
                # buffered bytes, the pump resumes from offset 0 and never
                # sends a Range header at all.
                time.sleep(0.5)
                if mode == "drop":
                    # RST: surfaces as ConnectionResetError, an OSError, which
                    # is what the resume path catches.
                    self.connection.setsockopt(
                        socket.SOL_SOCKET, socket.SO_LINGER,
                        struct.pack("ii", 1, 0))
            except OSError:
                pass
            self.close_connection = True
            try:
                self.connection.close()
            except OSError:
                pass
            return

        extra = dict(srv.extra_headers)
        if start:
            self.send_response(206)
            self.send_header("Content-Range",
                             "bytes %d-%d/%d" % (start, total - 1, total))
            body = data[start:]
        else:
            self.send_response(200)
            body = data
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Last-Modified",
                         email.utils.formatdate(srv.mtime, usegmt=True))
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass


class _Origin:
    """A throwaway origin. start() reports success instead of raising, so a
    machine with no free port SKIPS the scenario rather than failing it."""

    def __init__(self, root, mode="plain", certfile=None, drop_at=0,
                 extra_headers=None, basic=None, want_header=None,
                 mtime=1_700_000_000):
        self.root = root
        self.mode = mode
        self.certfile = certfile
        self.drop_at = drop_at
        self.extra_headers = extra_headers or {}
        self.basic = basic                  # (user, password) or None
        self.want_header = want_header      # (name, value) or None
        self.mtime = mtime
        self.srv = None
        self.base = None
        self.error = None

    def _authorized(self, headers):
        import base64
        if self.basic:
            got = headers.get("Authorization") or ""
            want = "Basic " + base64.b64encode(
                ("%s:%s" % self.basic).encode()).decode()
            if got != want:
                return False
        if self.want_header:
            k, v = self.want_header
            if (headers.get(k) or "") != v:
                return False
        return True

    def start(self):
        try:
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                                  _OriginHandler)
        except OSError as e:
            self.error = "could not bind a local port: %s" % e
            return False
        srv.root = self.root
        srv.mode = self.mode
        srv.hits = []
        srv.ranged = []
        srv.dropped = False
        srv.drop_at = self.drop_at
        srv.extra_headers = self.extra_headers
        srv.mtime = self.mtime
        srv.authorized = self._authorized
        scheme = "http"
        if self.certfile:
            try:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain(self.certfile)
                srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
                scheme = "https"
            except Exception as e:                          # noqa: BLE001
                srv.server_close()
                self.error = "could not start TLS: %s" % e
                return False
        self.srv = srv
        self.base = "%s://127.0.0.1:%d" % (scheme, srv.server_address[1])
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return True

    def stop(self):
        if self.srv is not None:
            try:
                self.srv.shutdown()
            except Exception:                               # noqa: BLE001
                pass
            try:
                self.srv.server_close()
            except Exception:                               # noqa: BLE001
                pass
            self.srv = None

    # assertions read these
    @property
    def hits(self):
        return self.srv.hits if self.srv else []

    @property
    def ranged(self):
        return self.srv.ranged if self.srv else []


def _cert_tooling():
    """Why a self-signed cert cannot be made, or None when one can."""
    try:
        import cryptography                                 # noqa: F401
        return None
    except ImportError:
        pass
    try:
        subprocess.run(["openssl", "version"], capture_output=True, timeout=10)
        return None
    except FileNotFoundError:
        return "neither the cryptography package nor an openssl binary"
    except Exception as e:                                  # noqa: BLE001
        return "openssl not usable: %s" % e


CERT_MISSING = _cert_tooling()


def _make_selfsigned(ws):
    """A throwaway cert+key PEM for 127.0.0.1, or None.

    blitcp never gets an --insecure flag for this: _http_open() uses the stdlib
    default SSL context, which honours SSL_CERT_FILE, so the scenario trusts
    exactly this one certificate through the child's environment instead."""
    pem = os.path.join(ws, "origin.pem")
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        import datetime
        import ipaddress
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder()
                .subject_name(name).issuer_name(name)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName(
                    [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                    critical=False)
                .sign(key, hashes.SHA256()))
        with open(pem, "wb") as f:
            f.write(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption()))
            f.write(cert.public_bytes(serialization.Encoding.PEM))
        return pem
    except ImportError:
        pass
    except Exception:                                       # noqa: BLE001
        return None
    try:
        key = os.path.join(ws, "origin.key")
        crt = os.path.join(ws, "origin.crt")
        r = subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", key, "-out", crt, "-days", "1",
             "-subj", "/CN=127.0.0.1",
             "-addext", "subjectAltName=IP:127.0.0.1"],
            capture_output=True, timeout=60)
        if r.returncode != 0 or not os.path.isfile(crt):
            return None
        with open(pem, "wb") as out:
            for part in (key, crt):
                with open(part, "rb") as f:
                    out.write(f.read())
        return pem
    except FileNotFoundError:
        return None
    except Exception:                                       # noqa: BLE001
        return None


def _refuse_sftp_dir(ws):
    """A directory to put on the child's PYTHONPATH so that the real blitcp.py,
    run unmodified, meets a server whose SFTP subsystem is refused.

    sitecustomize is imported by the interpreter itself, so nothing about the
    tool under test changes — only the paramiko it happens to import. There is
    no other way to test the SFTP-refused branches against a stock sshd."""
    d = os.path.join(ws, "_nosftp")
    os.makedirs(d, exist_ok=True)
    _write(os.path.join(d, "sitecustomize.py"), (
        "try:\n"
        "    import paramiko\n"
        "    def _refuse(self):\n"
        "        raise paramiko.SSHException('Channel closed.')\n"
        "    paramiko.SSHClient.open_sftp = _refuse\n"
        "except Exception:\n"
        "    pass\n").encode())
    return d

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
    """True if two files share physical storage — a reflink on btrfs/XFS or a
    clone on APFS.

    filefrag answers directly, but it is e2fsprogs and exists on Linux only, so
    on macOS every clone read as "not deduplicated" and the dedup scenarios
    failed on a platform where dedup had in fact worked. macOS can be asked
    properly: F_LOG2PHYS maps a file's first logical byte to a device offset,
    and two clones report the same one."""
    try:
        import subprocess as _sp
        o = _sp.run(["filefrag", "-v", a, b], capture_output=True,
                    text=True, timeout=15).stdout
        if o.strip():
            return "shared" in o
    except Exception:                                      # noqa: BLE001
        pass
    if sys.platform == "darwin":
        pa, pb = _phys_offset(a), _phys_offset(b)
        if pa is not None and pb is not None:
            return pa == pb
    return False


def _phys_offset(path):
    """Device offset of a file's first byte, or None if it cannot be asked.
    macOS F_LOG2PHYS fills struct log2phys {u32 flags; off_t contigbytes;
    off_t devoffset;}."""
    try:
        import fcntl
        import struct
        buf = bytearray(struct.calcsize("=IQQ"))
        with open(path, "rb") as f:
            fcntl.fcntl(f.fileno(), 49, buf)               # F_LOG2PHYS
        return struct.unpack("=IQQ", bytes(buf))[2]
    except Exception:                                      # noqa: BLE001
        return None


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
    # Snapshot the destination's mount root before the run: the dedup cache
    # lands there by preference and only inside the destination as a fallback,
    # so watching the destination alone is what let a 36 KB write go unseen.
    mroot = _mount_root(ws)
    return [os.path.join(ws, "src") + "/", os.path.join(ws, "dst") + "/",
            "--dry-run"], {"mroot": mroot, "root_before": _dir_names(mroot)}


def c_dryrun(ws, rc, out, info):
    if rc != 0:
        return False, f"exit {rc}"
    dst = os.path.join(ws, "dst")
    mroot = info["mroot"]
    leaked = sorted(_dir_names(mroot) - info["root_before"])
    if leaked:
        return False, f"--dry-run wrote to the mount root {mroot}: {leaked[:6]}"
    # Not "wrote no files" but "did not exist afterwards": creating the
    # directory is itself the side effect that leaves a mistyped path behind.
    if os.path.exists(dst):
        return False, ("--dry-run created the destination: "
                       + (str(sorted(_dir_names(dst))[:6]) or "empty dir"))
    if "DRY RUN" not in out:
        return False, "no DRY RUN plan printed"
    return True, f"plan printed, destination absent, {mroot} unchanged"


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
    # The dedup database is blitcp's own bookkeeping, and where it lands
    # depends on whether the per-user cache directory is usable — on the CI
    # runners it is not, so it sits beside the copy. It is not a copied file
    # and must not count as one.
    got = sorted(f for _r, _d, fs in os.walk(dst) for f in fs
                 if not f.startswith(".blitcp_") and not f.startswith(".fast_copy_"))
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
    # macOS has no setfacl (it spells this chmod +a), and minimal images ship
    # without acl at all. Unguarded, the FileNotFoundError escaped the scenario
    # builder and killed the whole suite mid-run -- the macOS CI job died here
    # after ten seconds with sixteen scenarios still unreported. The checker
    # already knows how to skip on acl_ok=False; let it.
    try:
        r = subprocess.run(["setfacl", "-m", "u:12345:rwx", fa],
                           capture_output=True)
        ok = r.returncode == 0
    except (FileNotFoundError, OSError):
        ok = False
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


# ── HTTP(S) SOURCE RELAY scenarios ───────────────────────────────────────────
# An http(s):// SOURCE streams one file to an SSH or SMB destination; nothing
# lands on the local disk. The refusals need no infrastructure at all and run
# everywhere, including CI.

_HTTP_PAYLOAD = _rand(3 * 1024 * 1024, b"http-relay")


def _origin_up(ws, **kw):
    """Serve ws/www. Returns (origin, info_bits) — info_bits carries _stop so
    the runner tears the server down even if the scenario explodes."""
    root = os.path.join(ws, "www")
    os.makedirs(root, exist_ok=True)
    o = _Origin(root, **kw)
    if not o.start():
        return None, {"_skip": o.error or "could not start a local origin"}
    return o, {"_stop": [o.stop], "origin": o}


def _origin_file(ws, name="payload.bin", data=None):
    """Put a file where the origin serves it. Deliberately NOT named _payload:
    that name already belongs to the index-group helper, and shadowing it
    silently emptied every index scenario's source tree."""
    return _write(os.path.join(ws, "www", name), data or _HTTP_PAYLOAD)


def _dst_ssh(ws, rel):
    return f"kai@localhost:{os.path.join(ws, 'dest', rel)}".replace(
        "kai@", os.environ.get("USER", "") + "@" if os.environ.get("USER") else "")


def _ssh_dest(ws, rel):
    """localhost destination in the scenario workspace."""
    os.makedirs(os.path.join(ws, "dest"), exist_ok=True)
    return f"localhost:{os.path.join(ws, 'dest', rel)}"


def _refused(out, needle):
    return needle in out


# ── refusals (needs=None) ────────────────────────────────────────────────────

def b_http_dest_refused(ws):
    return ["https://example.invalid/a.bin", "https://other.invalid/b.bin"], {}


def c_http_dest_refused(ws, rc, out, info):
    if rc == 0:
        return False, "an http(s) destination was accepted"
    if not _refused(out, "can only be the source"):
        return False, f"wrong message: {out.strip()[-140:]}"
    return True, "http(s) destination refused"


def b_http_multi_src_refused(ws):
    local = _write(os.path.join(ws, "extra.txt"), b"x")
    return ["https://example.invalid/a.bin", local,
            "localhost:" + os.path.join(ws, "dest") + "/"], {}


def c_http_multi_src_refused(ws, rc, out, info):
    if rc == 0:
        return False, "a URL plus extra sources was accepted"
    if not _refused(out, "takes a single URL"):
        return False, f"wrong message: {out.strip()[-140:]}"
    if os.path.exists(os.path.join(ws, "dest")):
        return False, "destination was created by a refused run"
    return True, "URL + extra sources refused"


def b_http_no_filename(ws):
    return ["https://example.invalid/dir/",
            "localhost:" + os.path.join(ws, "dest") + "/"], {}


def c_http_no_filename(ws, rc, out, info):
    if rc == 0:
        return False, "a URL naming no file was accepted"
    if not _refused(out, "names no file"):
        return False, f"wrong message: {out.strip()[-140:]}"
    return True, "URL without a filename refused"


def b_http_to_cloud_refused(ws):
    return ["https://example.invalid/a.bin", "s3://bucket/prefix/"], {}


def c_http_to_cloud_refused(ws, rc, out, info):
    if rc == 0:
        return False, "http → cloud was accepted"
    if not _refused(out, "relay via an SSH or SMB destination"):
        return False, f"wrong message: {out.strip()[-140:]}"
    return True, "http → cloud refused with the relay hint"


def b_http_to_local_refused(ws):
    return ["https://example.invalid/a.bin", os.path.join(ws, "dest") + "/"], {}


def c_http_to_local_refused(ws, rc, out, info):
    if rc == 0:
        return False, "http → local was accepted"
    if not _refused(out, "curl or wget"):
        return False, f"wrong message: {out.strip()[-140:]}"
    if os.path.exists(os.path.join(ws, "dest")):
        return False, "a refused run created the destination"
    return True, "http → local refused, nothing created"


# ── SSH relays (needs=ssh) ───────────────────────────────────────────────────

def _b_relay_ssh(ws, extra=None, origin_kw=None, name="payload.bin",
                 dest_rel=None, env=None):
    _origin_file(ws, name)
    o, bits = _origin_up(ws, **(origin_kw or {}))
    if o is None:
        return [], bits
    dest_rel = name if dest_rel is None else dest_rel
    args = [o.base + "/" + name, _ssh_dest(ws, dest_rel)] + list(extra or [])
    bits["dest"] = os.path.join(ws, "dest", dest_rel)
    bits["_env"] = dict(env or {})
    return args, bits


def _c_relay_content(ws, rc, out, info, want_suffix=None, data=None):
    if rc != 0:
        return False, f"exit {rc}: {out.strip()[-200:]}"
    dest = info["dest"]
    if not os.path.isfile(dest):
        return False, f"nothing at {os.path.basename(dest)}"
    with open(dest, "rb") as f:
        got = f.read()
    want = data if data is not None else _HTTP_PAYLOAD
    if got != want:
        return False, f"content differs ({len(got)} vs {len(want)} bytes)"
    if want_suffix is not None:
        has = "(SSH only, cat over exec)" in out
        if want_suffix and not has:
            return False, "banner does not announce the exec transport"
        if not want_suffix and has:
            return False, "banner claims exec transport on the SFTP path"
    return True, f"{len(got)} bytes identical"


def b_http_ssh_sftp(ws):
    return _b_relay_ssh(ws)


def c_http_ssh_sftp(ws, rc, out, info):
    if info.get("_skip"):
        return None, info["_skip"]
    ok, det = _c_relay_content(ws, rc, out, info, want_suffix=False)
    if not ok:
        return ok, det
    if "HTTP → SSH" not in out:
        return False, "no HTTP → SSH banner"
    return True, det + ", SFTP transport"


def b_http_ssh_exec(ws):
    return _b_relay_ssh(ws, extra=["--ssh-no-sftp"])


def c_http_ssh_exec(ws, rc, out, info):
    if info.get("_skip"):
        return None, info["_skip"]
    return _c_relay_content(ws, rc, out, info, want_suffix=True)


def b_https_ssh_sftp(ws):
    pem = _make_selfsigned(ws)
    if pem is None:
        return [], {"_skip": "could not generate a self-signed certificate"}
    return _b_relay_ssh(ws, origin_kw={"certfile": pem},
                        env={"SSL_CERT_FILE": pem})


def c_https_ssh_sftp(ws, rc, out, info):
    if info.get("_skip"):
        return None, info["_skip"]
    ok, det = _c_relay_content(ws, rc, out, info, want_suffix=False)
    return (ok, det + ", over TLS") if ok else (ok, det)


def b_https_ssh_exec(ws):
    pem = _make_selfsigned(ws)
    if pem is None:
        return [], {"_skip": "could not generate a self-signed certificate"}
    return _b_relay_ssh(ws, extra=["--ssh-no-sftp"],
                        origin_kw={"certfile": pem},
                        env={"SSL_CERT_FILE": pem})


def c_https_ssh_exec(ws, rc, out, info):
    if info.get("_skip"):
        return None, info["_skip"]
    ok, det = _c_relay_content(ws, rc, out, info, want_suffix=True)
    return (ok, det + ", over TLS") if ok else (ok, det)


def b_http_sftp_only_refuses_fallback(ws):
    # --sftp-only is a restriction: when the server refuses the SFTP channel it
    # must fail, never quietly open a shell instead.
    return _b_relay_ssh(ws, extra=["--sftp-only"],
                        env={"PYTHONPATH": _refuse_sftp_dir(ws)})


def c_http_sftp_only_refuses_fallback(ws, rc, out, info):
    if info.get("_skip"):
        return None, info["_skip"]
    if rc == 0:
        return False, "--sftp-only succeeded against a server with no SFTP"
    if "forbids the shell fallback" not in out:
        return False, f"wrong message: {out.strip()[-200:]}"
    if "(SSH only, cat over exec)" in out:
        return False, "--sftp-only fell back to the exec writer anyway"
    if os.path.exists(info["dest"]):
        return False, "a refused run left a partial file behind"
    return True, "refused, no shell fallback, nothing written"


def b_http_autofallback(ws):
    # No flag at all: SFTP refused must land on the exec writer, announced only
    # by the banner.
    return _b_relay_ssh(ws, env={"PYTHONPATH": _refuse_sftp_dir(ws)})


def c_http_autofallback(ws, rc, out, info):
    if info.get("_skip"):
        return None, info["_skip"]
    return _c_relay_content(ws, rc, out, info, want_suffix=True)


def b_http_filename_provenance(ws):
    # The name must come from the URL the user typed — not from the redirect
    # target (/real/decoy.bin) and not from Content-Disposition (evil.sh).
    _write(os.path.join(ws, "www", "real", "decoy.bin"), _HTTP_PAYLOAD)
    o, bits = _origin_up(ws, mode="hostile", extra_headers={
        "Content-Disposition": 'attachment; filename="evil.sh"'})
    if o is None:
        return [], bits
    os.makedirs(os.path.join(ws, "dest"), exist_ok=True)
    args = [o.base + "/downloads/wanted.bin",
            "localhost:" + os.path.join(ws, "dest") + "/"]
    bits["dest"] = os.path.join(ws, "dest", "wanted.bin")
    return args, bits


def c_http_filename_provenance(ws, rc, out, info):
    if info.get("_skip"):
        return None, info["_skip"]
    ok, det = _c_relay_content(ws, rc, out, info)
    if not ok:
        return ok, det
    stray = []
    for root, _d, files in os.walk(os.path.join(ws, "dest")):
        for f in files:
            if f in ("evil.sh", "decoy.bin"):
                stray.append(os.path.join(root, f))
    if stray:
        return False, f"a name from the server was obeyed: {stray[:3]}"
    return True, "name taken from the URL, not the server"


def b_http_dir_and_rename(ws):
    _origin_file(ws)
    o, bits = _origin_up(ws)
    if o is None:
        return [], bits
    os.makedirs(os.path.join(ws, "dest", "into"), exist_ok=True)
    bits["url"] = o.base + "/payload.bin"
    bits["dest"] = os.path.join(ws, "dest", "into", "payload.bin")
    # first run: an EXISTING directory receives <dir>/<url filename>
    return [bits["url"], "localhost:" + os.path.join(ws, "dest", "into")], bits


def c_http_dir_and_rename(ws, rc, out, info):
    if info.get("_skip"):
        return None, info["_skip"]
    ok, det = _c_relay_content(ws, rc, out, info)
    if not ok:
        return False, "directory form: " + det
    # second run: a plain path IS the target name (rename-on-copy)
    renamed = os.path.join(ws, "dest", "renamed.iso")
    rc2, out2 = run_fc(info_target[0],
                       [info["url"], "localhost:" + renamed])
    if rc2 != 0:
        return False, f"rename form exit {rc2}: {out2.strip()[-160:]}"
    if not os.path.isfile(renamed):
        return False, "rename-on-copy did not produce the named file"
    with open(renamed, "rb") as f:
        if f.read() != _HTTP_PAYLOAD:
            return False, "renamed file content differs"
    return True, "directory receives <dir>/<name>; plain path renames"


def b_http_range_resume(ws):
    _origin_file(ws)
    # Drop mid-body AFTER a full 1 MB chunk has been consumed, so the retry
    # asks for a non-zero offset and the 206/Content-Range path is real.
    o, bits = _origin_up(ws, mode="drop", drop_at=1024 * 1024 + 4096)
    if o is None:
        return [], bits
    os.makedirs(os.path.join(ws, "dest"), exist_ok=True)
    bits["dest"] = os.path.join(ws, "dest", "payload.bin")
    return [o.base + "/payload.bin",
            "localhost:" + os.path.join(ws, "dest") + "/"], bits


def c_http_range_resume(ws, rc, out, info):
    if info.get("_skip"):
        return None, info["_skip"]
    ok, det = _c_relay_content(ws, rc, out, info)
    if not ok:
        return ok, det
    o = info["origin"]
    if not o.ranged:
        return False, "the retry never sent a Range request"
    if max(o.ranged) <= 0:
        return False, f"Range offsets were all zero: {o.ranged}"
    return True, f"resumed at byte {max(o.ranged)}, content identical"


def b_http_incremental_skip(ws):
    _origin_file(ws)
    o, bits = _origin_up(ws)
    if o is None:
        return [], bits
    os.makedirs(os.path.join(ws, "dest"), exist_ok=True)
    a = [o.base + "/payload.bin", "localhost:" + os.path.join(ws, "dest") + "/"]
    rc0, out0 = run_fc(info_target[0], a)               # populate
    bits["dest"] = os.path.join(ws, "dest", "payload.bin")
    bits["first"] = (rc0, out0)
    return a, bits


def c_http_incremental_skip(ws, rc, out, info):
    if info.get("_skip"):
        return None, info["_skip"]
    rc0, out0 = info["first"]
    if rc0 != 0:
        return False, f"first run exit {rc0}: {out0.strip()[-160:]}"
    ok, det = _c_relay_content(ws, rc, out, info)
    if not ok:
        return ok, det
    if "Up to date" not in out:
        return False, f"second run did not skip: {out.strip()[-200:]}"
    return True, "second run reported up to date (size + Last-Modified)"


def b_http_auth_headers(ws):
    _origin_file(ws)
    o, bits = _origin_up(ws, mode="auth", basic=("uatuser", "uatpass"),
                         want_header=("X-Uat-Token", "shibboleth"))
    if o is None:
        return [], bits
    os.makedirs(os.path.join(ws, "dest"), exist_ok=True)
    bits["dest"] = os.path.join(ws, "dest", "payload.bin")
    bits["_env"] = {"UAT_HTTP_PW": "uatpass"}
    return [o.base + "/payload.bin",
            "localhost:" + os.path.join(ws, "dest") + "/",
            "--http-user", "uatuser", "--http-password-env", "UAT_HTTP_PW",
            "--http-header", "X-Uat-Token: shibboleth"], bits


def c_http_auth_headers(ws, rc, out, info):
    if info.get("_skip"):
        return None, info["_skip"]
    ok, det = _c_relay_content(ws, rc, out, info)
    if not ok:
        return ok, det
    seen = [h for _p, h in info["origin"].hits]
    if not any((h.get("Authorization") or "").startswith("Basic ") for h in seen):
        return False, "no Basic Authorization header reached the origin"
    if not any((h.get("X-Uat-Token") or "") == "shibboleth" for h in seen):
        return False, "--http-header did not reach the origin"
    return True, "Basic auth and --http-header both arrived"


def b_http_html_wall(ws):
    _origin_file(ws)
    o, bits = _origin_up(ws, mode="wall")
    if o is None:
        return [], bits
    os.makedirs(os.path.join(ws, "dest"), exist_ok=True)
    bits["dest"] = os.path.join(ws, "dest", "payload.bin")
    return [o.base + "/payload.bin",
            "localhost:" + os.path.join(ws, "dest") + "/"], bits


def c_http_html_wall(ws, rc, out, info):
    if info.get("_skip"):
        return None, info["_skip"]
    if rc == 0:
        return False, "a login page was accepted as the download"
    if "HTML page" not in out:
        return False, f"wrong message: {out.strip()[-200:]}"
    if os.path.exists(info["dest"]):
        with open(info["dest"], "rb") as f:
            head = f.read(64)
        return False, f"the login page was written as the file: {head[:40]!r}"
    return True, "login page refused, not written"


def b_http_graceful_close_truncation(ws):
    # A graceful FIN mid-body is NOT a socket error, so it never reaches the
    # resume path (that is what UAT-HTTP-9 covers). What must hold is that it
    # fails loudly rather than leaving a short file that looks complete.
    _origin_file(ws)
    o, bits = _origin_up(ws, mode="drop_fin", drop_at=1024 * 1024)
    if o is None:
        return [], bits
    os.makedirs(os.path.join(ws, "dest"), exist_ok=True)
    bits["dest"] = os.path.join(ws, "dest", "payload.bin")
    return [o.base + "/payload.bin",
            "localhost:" + os.path.join(ws, "dest") + "/"], bits


def c_http_graceful_close_truncation(ws, rc, out, info):
    if info.get("_skip"):
        return None, info["_skip"]
    if rc == 0 and os.path.isfile(info["dest"]):
        with open(info["dest"], "rb") as f:
            if f.read() == _HTTP_PAYLOAD:
                return True, "resumed and completed"
        return False, "exit 0 with a short file — truncation went unreported"
    if "truncated" not in out and "resuming" not in out:
        return False, f"neither resumed nor reported truncation: {out.strip()[-200:]}"
    return True, "a broken stream is reported, never silently short"


# ── SMB relays (needs=smb) ───────────────────────────────────────────────────

def _smb_args(base_extra=None):
    user = os.environ.get("FC_UAT_SMB_USER", "")
    a = list(base_extra or [])
    if user:
        a += ["--smb-user", user, "--smb-password-env", "FC_UAT_SMB_PASS"]
    return a


def b_http_smb(ws):
    _origin_file(ws)
    o, bits = _origin_up(ws)
    if o is None:
        return [], bits
    base = os.environ["FC_UAT_SMB_URL"].rstrip("/") + "/uat_http"
    bits["base"] = base
    return [o.base + "/payload.bin", base + "/"] + _smb_args(), bits


def _smb_readback(ws, info, name="payload.bin", want=None):
    """Pull the relayed file back down and compare it byte for byte."""
    back = os.path.join(ws, "back")
    rc, out = run_fc(info_target[0],
                     [info["base"] + "/" + name, back + "/"] + _smb_args())
    if rc != 0:
        return False, f"read-back exit {rc}: {out.strip()[-160:]}"
    got = os.path.join(back, name)
    if not os.path.isfile(got):
        return False, "read-back produced no file"
    with open(got, "rb") as f:
        if f.read() != (want if want is not None else _HTTP_PAYLOAD):
            return False, "read-back content differs"
    return True, "content identical after read-back"


def c_http_smb(ws, rc, out, info):
    if info.get("_skip"):
        return None, info["_skip"]
    if rc != 0:
        return False, f"exit {rc}: {out.strip()[-200:]}"
    if "HTTP → SMB" not in out:
        return False, "no HTTP → SMB banner"
    return _smb_readback(ws, info)


def b_https_smb(ws):
    pem = _make_selfsigned(ws)
    if pem is None:
        return [], {"_skip": "could not generate a self-signed certificate"}
    _origin_file(ws)
    o, bits = _origin_up(ws, certfile=pem)
    if o is None:
        return [], bits
    base = os.environ["FC_UAT_SMB_URL"].rstrip("/") + "/uat_https"
    bits["base"] = base
    bits["_env"] = {"SSL_CERT_FILE": pem}
    return [o.base + "/payload.bin", base + "/"] + _smb_args(), bits


def c_https_smb(ws, rc, out, info):
    if info.get("_skip"):
        return None, info["_skip"]
    if rc != 0:
        return False, f"exit {rc}: {out.strip()[-200:]}"
    return _smb_readback(ws, info)


def b_http_smb_filename(ws):
    _write(os.path.join(ws, "www", "real", "decoy.bin"), _HTTP_PAYLOAD)
    o, bits = _origin_up(ws, mode="hostile", extra_headers={
        "Content-Disposition": 'attachment; filename="evil.sh"'})
    if o is None:
        return [], bits
    base = os.environ["FC_UAT_SMB_URL"].rstrip("/") + "/uat_httpname"
    bits["base"] = base
    return [o.base + "/downloads/wanted.bin", base + "/"] + _smb_args(), bits


def c_http_smb_filename(ws, rc, out, info):
    if info.get("_skip"):
        return None, info["_skip"]
    if rc != 0:
        return False, f"exit {rc}: {out.strip()[-200:]}"
    ok, det = _smb_readback(ws, info, name="wanted.bin")
    if not ok:
        return False, det
    for bad in ("evil.sh", "decoy.bin"):
        rcb, _o = run_fc(info_target[0],
                         [info["base"] + "/" + bad,
                          os.path.join(ws, "stray") + "/"] + _smb_args())
        if rcb == 0 and os.path.isfile(os.path.join(ws, "stray", bad)):
            return False, f"a name from the server was obeyed: {bad}"
    return True, "name taken from the URL, not the server"


def b_http_smb_incremental(ws):
    _origin_file(ws)
    o, bits = _origin_up(ws)
    if o is None:
        return [], bits
    base = os.environ["FC_UAT_SMB_URL"].rstrip("/") + "/uat_httpinc"
    a = [o.base + "/payload.bin", base + "/"] + _smb_args()
    rc0, out0 = run_fc(info_target[0], a)
    bits["base"] = base
    bits["first"] = (rc0, out0)
    return a, bits


def c_http_smb_incremental(ws, rc, out, info):
    if info.get("_skip"):
        return None, info["_skip"]
    rc0, out0 = info["first"]
    if rc0 != 0:
        return False, f"first run exit {rc0}: {out0.strip()[-160:]}"
    if rc != 0:
        return False, f"second run exit {rc}: {out.strip()[-200:]}"
    if "Up to date" not in out:
        return False, f"second run did not skip: {out.strip()[-200:]}"
    return True, "second run reported up to date"


SCENARIOS = [
    S("UAT-LOCAL-1", "local", "basic tree copy preserves all content", b_basic, c_basic),
    S("UAT-LOCAL-2", "local", "incremental re-run skips/links identical files", b_incremental, c_incremental),
    S("UAT-LOCAL-3", "local", "within-run dedup shares one inode", b_dedup, c_dedup),
    S("UAT-LOCAL-4", "local", "--no-dedup keeps independent copies", b_nodedup, c_nodedup),
    S("UAT-LOCAL-5", "local", "--dry-run writes nothing", b_dryrun, c_dryrun),
    S("UAT-LOCAL-6", "local", "--exclude drops matching files", b_exclude, c_exclude),
    S("UAT-LOCAL-7", "local", "--overwrite replaces a stale destination file", b_overwrite, c_overwrite),
    S("UAT-LOCAL-8", "local", "--hash sha256 copies with integrity", b_sha256, c_sha256),
    S("UAT-LOCAL-9", "local", "--preserve mode keeps file permissions", b_preserve_mode, c_preserve_mode, needs="posix"),
    S("UAT-LOCAL-10", "local", "--log-file writes a structured JSON log", b_logfile, c_logfile),
    S("UAT-LOCAL-11", "local", "glob source selects only matches", b_glob, c_glob),
    S("UAT-LOCAL-12", "local", "symlink handled without error", b_symlink, c_symlink, needs="symlink"),
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

    S("UAT-HTTP-1", "http", "http(s):// destination is refused", b_http_dest_refused, c_http_dest_refused),
    S("UAT-HTTP-2", "http", "a URL plus extra sources is refused", b_http_multi_src_refused, c_http_multi_src_refused),
    S("UAT-HTTP-3", "http", "a URL naming no file is refused", b_http_no_filename, c_http_no_filename),
    S("UAT-HTTP-4", "http", "http → cloud is refused with the relay hint", b_http_to_cloud_refused, c_http_to_cloud_refused),
    S("UAT-HTTP-5", "http", "http → local is refused (use curl/wget)", b_http_to_local_refused, c_http_to_local_refused),
    S("UAT-HTTP-6", "http", "http:// → SSH over SFTP", b_http_ssh_sftp, c_http_ssh_sftp, needs="ssh"),
    S("UAT-HTTP-7", "http", "http:// → SSH with --ssh-no-sftp (cat over exec)", b_http_ssh_exec, c_http_ssh_exec, needs="ssh"),
    S("UAT-HTTP-8", "http", "--sftp-only never falls back to the shell", b_http_sftp_only_refuses_fallback, c_http_sftp_only_refuses_fallback, needs="ssh"),
    S("UAT-HTTP-9", "http", "Range resume after a broken stream", b_http_range_resume, c_http_range_resume, needs="ssh"),
    S("UAT-HTTP-10", "http", "filename comes from the URL, not the server", b_http_filename_provenance, c_http_filename_provenance, needs="ssh"),
    S("UAT-HTTP-11", "http", "directory receives <dir>/<name>; plain path renames", b_http_dir_and_rename, c_http_dir_and_rename, needs="ssh"),
    S("UAT-HTTP-12", "http", "incremental skip on an unchanged second run", b_http_incremental_skip, c_http_incremental_skip, needs="ssh"),
    S("UAT-HTTP-13", "http", "a broken stream is never silently short", b_http_graceful_close_truncation, c_http_graceful_close_truncation, needs="ssh"),
    S("UAT-HTTP-14", "http", "--http-user / --http-header reach the origin", b_http_auth_headers, c_http_auth_headers, needs="ssh"),
    S("UAT-HTTP-15", "http", "an HTML login page is refused, not written", b_http_html_wall, c_http_html_wall, needs="ssh"),
    S("UAT-HTTP-16", "http", "SFTP refused → exec writer, announced by the banner", b_http_autofallback, c_http_autofallback, needs="ssh"),
    S("UAT-HTTP-17", "http", "https:// → SSH over SFTP", b_https_ssh_sftp, c_https_ssh_sftp, needs="ssh,https"),
    S("UAT-HTTP-18", "http", "https:// → SSH with --ssh-no-sftp", b_https_ssh_exec, c_https_ssh_exec, needs="ssh,https"),
    S("UAT-HTTP-19", "http", "http:// → SMB share", b_http_smb, c_http_smb, needs="smb"),
    S("UAT-HTTP-20", "http", "https:// → SMB share", b_https_smb, c_https_smb, needs="smb,https"),
    S("UAT-HTTP-21", "http", "SMB: filename comes from the URL", b_http_smb_filename, c_http_smb_filename, needs="smb"),
    S("UAT-HTTP-22", "http", "SMB: incremental skip on a second run", b_http_smb_incremental, c_http_smb_incremental, needs="smb"),

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


def _force_utf8_stdout():
    """Windows consoles default to cp1252, and the scenario titles contain a
    '→'. Printing one raised UnicodeEncodeError and took the whole run down
    AFTER the scenarios had already been decided — a suite that had passed
    reported as a crash."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if (getattr(stream, "encoding", "") or "").lower() not in ("utf-8", "utf8"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                  # noqa: BLE001
            pass


def _can_symlink():
    """Windows grants symlink creation only with Developer Mode or elevation,
    and that is a property of the machine rather than of the platform name —
    so ask the OS once instead of guessing."""
    if os.name == "posix":
        return True
    d = tempfile.mkdtemp(prefix="uat_symcap_")
    try:
        tgt = os.path.join(d, "t")
        open(tgt, "w").close()
        os.symlink(tgt, os.path.join(d, "l"))
        return True
    except (OSError, NotImplementedError, AttributeError):
        return False
    finally:
        shutil.rmtree(d, ignore_errors=True)


_CAN_SYMLINK = _can_symlink()


def _indent(text, p="      | "):
    return "\n".join(p + ln for ln in text.splitlines()[-25:])


def _run_one(sc, target, manual, keep):
    # `needs` is a comma-separated set of tokens so a scenario can require more
    # than one thing (an https relay needs sshd AND cert tooling).
    needs = set((sc["needs"] or "").split(",")) - {""}
    if not manual:
        if sc["manual_only"]:
            return None, "manual-only (needs live endpoint/credentials)"
        if "ssh" in needs and not HAVE_SSH:
            return None, "no localhost sshd"
        if "ssh" in needs and not HAVE_PARAMIKO:
            return None, (f"paramiko not importable by {sys.executable} "
                          f"- SSH transfers cannot run")
        if "smb" in needs and not os.environ.get("FC_UAT_SMB_URL"):
            return None, "set FC_UAT_SMB_URL (+USER/PASS) to test SMB"
        if "cloud" in needs and not os.environ.get("FC_UAT_CLOUD_URL"):
            return None, "set FC_UAT_CLOUD_URL (+BLITCP_CREDS_PASSPHRASE) to test cloud"
        if "https" in needs and CERT_MISSING:
            return None, f"no way to make a test certificate: {CERT_MISSING}"
        # Platform tokens. These are not dependencies that could be installed —
        # they are properties Windows does not have, so asserting them there
        # tests the operating system rather than blitcp.
        if "posix" in needs and os.name != "posix":
            return None, "POSIX file modes do not exist on this platform"
        if "symlink" in needs and not _CAN_SYMLINK:
            return None, ("creating a symlink needs Developer Mode or "
                          "administrator rights here")
    ws = tempfile.mkdtemp(prefix=f"{sc['id']}_")
    info = {}
    try:
        args, info = sc["build"](ws)
        # A builder that could not stand up its infrastructure says so here
        # rather than raising, so a missing port or cert is a SKIP.
        if info.get("_skip"):
            return None, info["_skip"]
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
        rc, out = run_fc(target, args, env_extra=info.get("_env"))
        if manual:
            print(_indent(out.strip()))
        ok, detail = sc["check"](ws, rc, out, info)
        if manual:
            ans = input(f"  Accept {sc['id']}? [y/n] (auto: {_verdict(ok)} — {detail}) ").strip().lower()
            if ans in ("y", "n"):
                ok = ans == "y"
        return ok, detail
    finally:
        # Stop any origin the builder started, whatever happened above — a
        # surviving listening socket is a real leak and audit_uat.py will
        # (rightly) report it.
        for _srv in (info.get("_stop") or []):
            try:
                _srv()
            except Exception:                               # noqa: BLE001
                pass
        if keep:
            print(f"  {C.GREY}kept: {ws}{C.X}")
        elif not manual:
            shutil.rmtree(ws, ignore_errors=True)


def main(argv=None):
    _force_utf8_stdout()   # before the first '→' reaches a cp1252 console
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", default=os.path.join(HERE, "blitcp.py"))
    ap.add_argument("--manual", action="store_true")
    ap.add_argument("--group", nargs="+",
                    choices=["local", "index", "ssh", "http", "cloud", "smb",
                             "info"])
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
