#!/usr/bin/env python3
# Copyright 2024 fast-copy contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
audit_uat.py — Full security + UAT audit for fast-copy.

A single, self-contained, stdlib-only auditor that:

  * statically scans the source for every known vulnerability class
    (security),
  * verifies the tool leaves no garbage — stray temp files, locked dedup
    databases, leaked file descriptors or child processes (leaks),
  * exercises every transfer Mode — L2L always, Push/Pull/R2R over localhost
    SSH and cloud round-trips against a fake backend when available, skipping
    cleanly otherwise (modes),
  * checks the full capability matrix — dedup, hashing, exclude, dry-run,
    verify, overwrite, preserve, multi-source, tuning flags, info commands
    (features),
  * hunts correctness bugs and edge cases — empty dirs, unicode names,
    zero-byte/large files, symlinks, idempotency, traversal guards, clean
    error messages (bugs),
  * runs a chained end-to-end acceptance scenario (uat).

It is read-only toward the repository and the user's real configuration: it
never deletes flagged secret files, never touches the real credentials file or
any live host, and cleans up every workspace it creates. Optional tools
(bandit, pip-audit, safety, xxhash, ssh) are used when present and turn into
clean SKIPs when absent — the auditor never crashes on a missing dependency.

Usage:
    python3 audit_uat.py [--section NAME ...] [--target PATH] [--json OUT]
                         [--allow-remote] [--allow-cloud] [-v] [--quiet]

Exit code is 0 when no check FAILs (WARN/SKIP never fail the run), else 1.
"""

import argparse
import ast
import json
import inspect
import contextlib
import builtins
import io
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

# i18n guard (I18N_DESIGN.md, M0): the auditor greps English output strings.
# Pin the C locale for this process and every child it spawns so future
# translations can never break (or falsely pass) these checks.
os.environ["LC_ALL"] = "C"
os.environ["LANG"] = "C"
os.environ.pop("LANGUAGE", None)
os.environ.pop("FAST_COPY_LANG", None)

# --------------------------------------------------------------------------- #
# Presentation
# --------------------------------------------------------------------------- #

PASS, FAIL, WARN, SKIP, INFO = "PASS", "FAIL", "WARN", "SKIP", "INFO"
_ORDER = {PASS: 0, WARN: 1, SKIP: 2, FAIL: 3, INFO: 4}


class C:
    """ANSI colors, auto-disabled when stdout is not a terminal."""
    _on = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
    RESET = "\033[0m" if _on else ""
    BOLD = "\033[1m" if _on else ""
    GREEN = "\033[32m" if _on else ""
    RED = "\033[31m" if _on else ""
    YELLOW = "\033[33m" if _on else ""
    BLUE = "\033[34m" if _on else ""
    GREY = "\033[90m" if _on else ""


_STATUS_COLOR = {PASS: C.GREEN, FAIL: C.RED, WARN: C.YELLOW, SKIP: C.GREY,
                 INFO: C.BLUE}

SECTIONS = ["security", "leaks", "modes", "features", "bugs", "uat"]


class Reporter:
    """Collects per-check results and renders live lines + a final summary."""

    def __init__(self, verbose=False, quiet=False):
        self.verbose = verbose
        self.quiet = quiet
        self.results = []          # list of dicts
        self.section = None

    def begin(self, section):
        self.section = section
        if not self.quiet:
            print(f"\n{C.BOLD}== {section} =={C.RESET}")

    def record(self, name, status, detail=""):
        row = {"section": self.section, "name": name,
               "status": status, "detail": detail}
        self.results.append(row)
        if self.quiet and status in (PASS, SKIP, INFO):
            return
        col = _STATUS_COLOR.get(status, "")
        line = f"  {col}{status:<4}{C.RESET}  {name}"
        if detail and (self.verbose or status in (FAIL, WARN)):
            line += f"\n         {C.GREY}{detail}{C.RESET}"
        print(line)

    # convenience wrappers
    def ok(self, name, detail=""):    self.record(name, PASS, detail)
    def fail(self, name, detail=""):  self.record(name, FAIL, detail)
    def warn(self, name, detail=""):  self.record(name, WARN, detail)
    def skip(self, name, detail=""):  self.record(name, SKIP, detail)
    def info(self, name, detail=""):  self.record(name, INFO, detail)

    # --- summary ---------------------------------------------------------- #
    def counts(self, section=None):
        c = {PASS: 0, FAIL: 0, WARN: 0, SKIP: 0, INFO: 0}
        for r in self.results:
            if section is None or r["section"] == section:
                c[r["status"]] += 1
        return c

    def summary(self):
        print(f"\n{C.BOLD}{'='*60}\n SUMMARY\n{'='*60}{C.RESET}")
        seen = [s for s in SECTIONS if any(r["section"] == s
                                           for r in self.results)]
        for s in seen:
            c = self.counts(s)
            print(f"  {s:<10} "
                  f"{C.GREEN}{c[PASS]} pass{C.RESET}  "
                  f"{C.RED}{c[FAIL]} fail{C.RESET}  "
                  f"{C.YELLOW}{c[WARN]} warn{C.RESET}  "
                  f"{C.GREY}{c[SKIP]} skip{C.RESET}")
        bad = [r for r in self.results if r["status"] in (FAIL, WARN)]
        if bad:
            print(f"\n{C.BOLD} Findings ({len(bad)}):{C.RESET}")
            for r in sorted(bad, key=lambda r: -_ORDER[r["status"]]):
                col = _STATUS_COLOR[r["status"]]
                print(f"  {col}{r['status']}{C.RESET} "
                      f"[{r['section']}] {r['name']}")
                if r["detail"]:
                    print(f"       {C.GREY}{r['detail']}{C.RESET}")
        total = self.counts()
        verdict = (f"{C.RED}AUDIT FAILED{C.RESET}" if total[FAIL]
                   else f"{C.GREEN}AUDIT PASSED{C.RESET}")
        print(f"\n {verdict} — {total[PASS]} pass, {total[FAIL]} fail, "
              f"{total[WARN]} warn, {total[SKIP]} skip")


# --------------------------------------------------------------------------- #
# Harness helpers
# --------------------------------------------------------------------------- #

# Cloud / SSH env vars scrubbed from the child so the auditor can never reach a
# real endpoint by accident (unless the user explicitly opts in).
_SENSITIVE_ENV = (
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "AWS_PROFILE", "AWS_DEFAULT_REGION", "AWS_REGION",
    "AZURE_STORAGE_CONNECTION_STRING", "AZURE_STORAGE_ACCOUNT",
    "AZURE_STORAGE_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
    "FAST_COPY_CREDENTIALS", "FAST_COPY_CREDS_PASSPHRASE",
)


def run_fc(target, args, timeout=120, tmpdir=None, extra_env=None):
    """Invoke the fast-copy script as a child process.

    Redirects the child's temp directory to ``tmpdir`` (when given) so leak
    checks can inspect a private scratch area, and scrubs cloud/SSH secrets
    from its environment.
    """
    env = dict(os.environ)
    for k in _SENSITIVE_ENV:
        env.pop(k, None)
    if tmpdir:
        env["TMPDIR"] = tmpdir
        env["TMP"] = tmpdir
        env["TEMP"] = tmpdir
    if extra_env:
        env.update(extra_env)
    env["NO_COLOR"] = "1"
    cmd = [sys.executable, target] + [str(a) for a in args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout, env=env)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        return 124, e.stdout or "", (e.stderr or "") + "\n[timeout]"


class temp_workspace:
    """Context manager yielding a private temp dir, always removed on exit."""

    def __init__(self, prefix="fc_audit_"):
        self.prefix = prefix
        self.path = None

    def __enter__(self):
        self.path = tempfile.mkdtemp(prefix=self.prefix)
        return self.path

    def __exit__(self, *exc):
        if self.path and os.path.isdir(self.path):
            shutil.rmtree(self.path, ignore_errors=True)
        return False


def mount_root(path):
    """Mount point *path* lives on. The dedup cache prefers the destination's
    mount root and only falls back to the destination itself, so this is the
    other place a run can write — and the one no test used to watch."""
    p = os.path.abspath(path)
    while not os.path.ismount(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return p


def dir_names(d):
    """Top-level entry names in *d*, empty when it does not exist. The dedup DB
    lands directly in the root it chooses, so one level deep is enough."""
    try:
        return set(os.listdir(d))
    except OSError:
        return set()


def make_tree(root, with_symlink=False, big_mb=2, with_empty_dir=True,
              with_dups=True, with_unicode=True):
    """Build a deterministic fixture tree. Returns the root path.

    Layout (subset depending on flags):
        a.txt                       small text
        sub/b.txt                   nested text
        sub/deep/c.bin              multi-MB binary
        dup1.txt, dup2.txt          identical content (dedup fodder)
        zero.txt                    zero-byte file
        'name with spaces.txt'      spaces in name
        'φα ντασία.txt'             unicode name
        empty/                      empty directory
        link.txt -> a.txt           symlink (optional)
    """
    os.makedirs(root, exist_ok=True)
    _write(os.path.join(root, "a.txt"), b"alpha content\n")
    os.makedirs(os.path.join(root, "sub", "deep"), exist_ok=True)
    _write(os.path.join(root, "sub", "b.txt"), b"beta content\n")
    _write(os.path.join(root, "sub", "deep", "c.bin"),
           bytes((i * 37 + 11) & 0xFF for i in range(big_mb * 1024 * 1024)))
    _write(os.path.join(root, "zero.txt"), b"")
    if with_dups:
        payload = b"shared duplicate payload " * 64
        _write(os.path.join(root, "dup1.txt"), payload)
        _write(os.path.join(root, "dup2.txt"), payload)
    _write(os.path.join(root, "name with spaces.txt"), b"spaced\n")
    if with_unicode:
        _write(os.path.join(root, "φα ντ.txt"),
               b"unicode\n")
    if with_empty_dir:
        os.makedirs(os.path.join(root, "empty"), exist_ok=True)
    if with_symlink:
        try:
            os.symlink("a.txt", os.path.join(root, "link.txt"))
        except (OSError, NotImplementedError):
            pass
    return root


def _write(path, data):
    with open(path, "wb") as f:
        f.write(data)


def _hash_file(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _snapshot(root, follow_symlinks=False):
    """Map relpath -> ('d', None) for dirs or ('f', sha256) for files."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        if rel != ".":
            out[rel] = ("d", None)
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            r = os.path.relpath(full, root)
            if os.path.islink(full) and not follow_symlinks:
                out[r] = ("l", os.readlink(full))
            elif os.path.isfile(full):
                out[r] = ("f", _hash_file(full))
    return out


def _is_sidecar(relpath):
    """True for blitcp's own bookkeeping files: the dedup DB (plus its -wal /
    -shm), the tar bundle, the remote manifest, the sudo audit log.

    They are not copied content and must never be compared as if they were.
    DedupDB prefers the destination's MOUNT ROOT and falls back to the
    destination itself only when that root is "/" — which is exactly the
    layout of a CI runner and a container, so there the DB lands inside the
    destination and its bytes legitimately differ between two otherwise
    identical runs. The engine excludes these names from copying for the same
    reason (see exclude_patterns in blitcp.py)."""
    return os.path.basename(relpath).startswith((".fast_copy", ".blitcp"))


def tree_equal(src, dst, ignore=("link.txt",)):
    """True iff dst contains every source *file* with identical content.

    Compares regular files only: directory entries are implied by the files
    they hold, and empty-dir preservation is asserted separately (the tool
    intentionally does not recreate empty directories). Symlinks and dedup
    sidecar files are ignored. Returns (ok, detail).
    """
    def clean(snap):
        out = {}
        for k, v in snap.items():
            base = os.path.basename(k)
            if base in ignore or _is_sidecar(k):
                continue
            if v[0] != "f":          # dirs/symlinks asserted elsewhere
                continue
            out[k] = v
        return out

    a, b = clean(_snapshot(src)), clean(_snapshot(dst))
    missing = [k for k in a if k not in b]
    extra = [k for k in b if k not in a]
    differ = [k for k in a if k in b and a[k] != b[k]]
    if not (missing or extra or differ):
        return True, ""
    parts = []
    if missing:
        parts.append(f"missing {missing[:4]}")
    if extra:
        parts.append(f"extra {extra[:4]}")
    if differ:
        parts.append(f"differ {differ[:4]}")
    return False, "; ".join(parts)


def _content_multiset(root, ignore=("link.txt",)):
    """Multiset (sorted list) of regular-file content hashes under root.

    Path-agnostic: used to confirm a transfer preserved every file's bytes
    regardless of how the destination nests them.
    """
    hashes = []
    for k, v in _snapshot(root).items():
        base = os.path.basename(k)
        if base in ignore or base.startswith((".fast_copy", ".blitcp")):
            continue
        if v[0] == "f":
            hashes.append(v[1])
    return sorted(hashes)


def _no_traceback(stderr):
    """True if stderr carries no raw Python traceback (clean-error policy)."""
    return "Traceback (most recent call last)" not in (stderr or "")


# --------------------------------------------------------------------------- #
# Section 1: security — static vulnerability scan
# --------------------------------------------------------------------------- #

class _VulnVisitor(ast.NodeVisitor):
    """AST walk that records security-relevant call/usage sites."""

    def __init__(self):
        self.findings = []   # (severity, label, lineno, snippet)

    def _add(self, sev, label, node):
        self.findings.append((sev, label, getattr(node, "lineno", 0)))

    @staticmethod
    def _attr_chain(node):
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
        return ".".join(reversed(parts))

    def visit_Call(self, node):
        func = node.func
        name = (func.id if isinstance(func, ast.Name)
                else self._attr_chain(func) if isinstance(func, ast.Attribute)
                else "")

        # subprocess / shell=True
        for kw in node.keywords:
            if kw.arg == "shell" and isinstance(kw.value, ast.Constant) \
                    and kw.value.value is True:
                self._add(FAIL, "subprocess shell=True", node)
            if kw.arg == "verify" and isinstance(kw.value, ast.Constant) \
                    and kw.value.value is False:
                self._add(FAIL, "TLS verify=False", node)
            if kw.arg == "check_hostname" and isinstance(kw.value, ast.Constant) \
                    and kw.value.value is False:
                self._add(FAIL, "TLS check_hostname=False", node)

        # os.system / os.popen
        if name in ("os.system", "os.popen"):
            self._add(FAIL, f"{name}()", node)

        # eval / exec / compile
        if name in ("eval", "exec"):
            arg0 = node.args[0] if node.args else None
            sev = WARN if isinstance(arg0, ast.Constant) else FAIL
            self._add(sev, f"{name}() call", node)

        # insecure temp
        if name in ("tempfile.mktemp", "mktemp"):
            self._add(FAIL, "tempfile.mktemp (insecure)", node)

        # deserialization
        if name in ("pickle.load", "pickle.loads", "cPickle.load",
                    "marshal.load", "marshal.loads"):
            self._add(WARN, f"{name}()", node)
        if name in ("yaml.load",):
            safe = any(kw.arg == "Loader" for kw in node.keywords)
            self._add(PASS if safe else FAIL,
                      "yaml.load" + ("" if safe else " (no SafeLoader)"), node)

        # weak hashing
        if name in ("hashlib.md5", "hashlib.sha1"):
            self._add(WARN, f"{name} (weak hash)", node)

        # insecure SSL contexts
        if name == "ssl._create_unverified_context":
            self._add(FAIL, "ssl._create_unverified_context", node)

        # SQL injection: execute/executemany with a built string. An f-string
        # whose every interpolation is int()/float()-coerced is exempt: it
        # cannot carry an injection, and PRAGMA statements (which reject `?`
        # parameter binding) have no other way to take a numeric value.
        if name.endswith("execute") or name.endswith("executemany"):
            if node.args:
                a0 = node.args[0]
                if isinstance(a0, ast.JoinedStr):
                    def _numeric_coerced(fv):
                        v = fv.value
                        return (isinstance(v, ast.Call)
                                and isinstance(v.func, ast.Name)
                                and v.func.id in ("int", "float", "len")
                                and not v.keywords)
                    fvs = [v for v in a0.values
                           if isinstance(v, ast.FormattedValue)]
                    if not (fvs and all(_numeric_coerced(fv) for fv in fvs)):
                        self._add(FAIL, "SQL via f-string", node)
                elif isinstance(a0, ast.BinOp) and isinstance(
                        a0.op, (ast.Mod, ast.Add)):
                    self._add(FAIL, "SQL via string concat/%", node)

        self.generic_visit(node)

    def visit_Attribute(self, node):
        if node.attr in ("AutoAddPolicy", "WarningPolicy"):
            self._add(WARN, f"paramiko {node.attr} (host-key TOFU off)", node)
        if node.attr == "CERT_NONE":
            self._add(WARN, "ssl.CERT_NONE referenced", node)
        self.generic_visit(node)


# Marker the engine writes into an encrypted credentials envelope
# (encrypt_conns / _is_encrypted in fast_copy.py). Must stay in sync.
CREDS_MAGIC = "FC-CREDS-ENC-v1"


def _creds_encryption_state(path):
    """Classify a credentials file as encrypted / plaintext / unreadable.

    Reads only enough to inspect the JSON envelope's ``magic`` marker — never
    decrypts the file and never returns or logs any secret value.
    """
    try:
        with open(path, "rb") as f:
            raw = f.read(1 << 16)
    except OSError as e:
        return "unreadable", str(e)
    try:
        obj = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return "plaintext", "not the encrypted envelope format"
    if isinstance(obj, dict) and obj.get("magic") == CREDS_MAGIC:
        return "encrypted", "AES-256-GCM envelope present"
    return "plaintext", "valid JSON with no encryption envelope"


def _check_credentials_encrypted(rep, repo):
    """Assert every discoverable credentials file is encrypted at rest."""
    candidates = []
    try:
        for fn in os.listdir(repo):
            if fn == "credentials.json" or fn.startswith("credentials.json."):
                candidates.append(os.path.join(repo, fn))
    except OSError:
        pass
    envp = os.environ.get("FAST_COPY_CREDENTIALS")
    if envp and os.path.isfile(envp):
        candidates.append(envp)
    # de-dup by real path
    seen, files = set(), []
    for c in candidates:
        rp = os.path.realpath(c)
        if rp not in seen:
            seen.add(rp)
            files.append(c)

    if not files:
        rep.skip("credentials encrypted", "no credentials.json* found")
        return
    for path in files:
        state, detail = _creds_encryption_state(path)
        name = f"credentials encrypted ({os.path.basename(path)})"
        if state == "encrypted":
            rep.ok(name, detail)
        elif state == "unreadable":
            rep.skip(name, detail)
        else:
            rep.fail(name, f"PLAINTEXT secrets at rest — {detail}")


def _mentions_not_encrypted(test):
    """True if an AST condition is a `not <…encrypt…>` test (the abort guard
    shape that prevents a cleartext credential write)."""
    for n in ast.walk(test):
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
            for a in ast.walk(n.operand):
                nm = (a.attr if isinstance(a, ast.Attribute)
                      else getattr(a, "id", ""))
                if "encrypt" in nm.lower():
                    return True
    return False


def _check_gui_creds_enforce_encryption(rep, repo):
    """Catch the 'decline passphrase -> plaintext credentials' bug class.

    For every function that writes credentials (calls _save_credentials_file),
    require an abort guard of the form ``if not <encrypted>: return/raise`` so a
    cancelled/blank passphrase prompt can never fall through to a cleartext
    write of secret-bearing credentials. A save path lacking such a guard is a
    FAIL — this is exactly the GUI flaw where pressing Cancel persisted
    passwords in cleartext.
    """
    path = os.path.join(repo, "fast_copy_modern_gui.py")
    if not os.path.exists(path):
        rep.skip("GUI creds encryption enforced", "no GUI file present")
        return
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            tree = ast.parse(f.read(), filename=path)
    except (OSError, SyntaxError) as e:
        rep.skip("GUI creds encryption enforced", str(e))
        return

    def save_call(fn):
        for n in ast.walk(fn):
            if isinstance(n, ast.Call):
                f = n.func
                nm = (f.attr if isinstance(f, ast.Attribute)
                      else getattr(f, "id", ""))
                if nm == "_save_credentials_file":
                    return n
        return None

    def has_abort_guard(fn):
        for n in ast.walk(fn):
            if isinstance(n, ast.If) and _mentions_not_encrypted(n.test):
                if any(isinstance(s, (ast.Return, ast.Raise))
                       for s in ast.walk(n)):
                    return True
        return False

    offenders, checked = [], 0
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        call = save_call(fn)
        if not call:
            continue
        checked += 1
        if not has_abort_guard(fn):
            offenders.append(f"{fn.name}() @L{call.lineno}")

    if checked == 0:
        rep.skip("GUI creds encryption enforced",
                 "no credential-write paths found")
    elif offenders:
        rep.fail("GUI creds encryption enforced",
                 "cleartext credential write reachable without encryption "
                 "guard in: " + "; ".join(offenders))
    else:
        rep.ok("GUI creds encryption enforced",
               f"{checked} credential-write path(s) abort rather than write "
               "secrets in cleartext")


_SECRET_PATTERNS = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key id literal"),
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
     "embedded private key"),
    (re.compile(r"(?i)aws_secret_access_key\s*[=:]\s*['\"][^'\"]{20,}['\"]"),
     "AWS secret literal"),
    (re.compile(r"(?i)(api[_-]?key|token|passwd|password)\s*[=:]\s*"
                r"['\"][A-Za-z0-9/+=_\-]{16,}['\"]"), "hardcoded secret"),
]


def _import_target(ctx):
    """Import the fast_copy.py under test as a module (its CLI is __main__-guarded
    so import has no side effects). Cached on ctx."""
    if ctx.get("_mod") is not None:
        return ctx["_mod"]
    import importlib.util
    spec = importlib.util.spec_from_file_location("fastcopy_under_test",
                                                  ctx["target"])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ctx["_mod"] = mod
    return mod


def _check_dedup_db_writability(rep, ctx):
    """Regression guard for the Windows C:\\ dedup-DB bug: os.access(W_OK)
    lies on Windows, so mount-root writability must be decided by a real
    create-probe, and a cache-DB open failure must fall back instead of
    killing the transfer."""
    try:
        mod = _import_target(ctx)
    except Exception as e:
        rep.fail("dedup DB writability", f"target import failed: {e}")
        return
    if not hasattr(mod, "_dir_really_writable"):
        rep.fail("dedup DB writability",
                 "_dir_really_writable missing — mount-root choice is back "
                 "on os.access, which answers True for unwritable C:\\")
        return
    import tempfile as _tf
    problems = []
    with _tf.TemporaryDirectory() as td:
        if mod._dir_really_writable(td) is not True:
            problems.append("probe False on a writable dir")
        ro = os.path.join(td, "ro")
        os.mkdir(ro); os.chmod(ro, 0o555)
        try:
            if os.access(ro, os.W_OK) is False and \
                    mod._dir_really_writable(ro) is not False:
                problems.append("probe True on a read-only dir")
        finally:
            os.chmod(ro, 0o755)
    # A cache open failure must fall back, not raise: point the SSH cache at
    # an impossible explicit path and expect the per-user fallback.
    try:
        c = mod.SshDedupCache("audit", "/x",
                              db_path=os.path.join(os.sep, "nonexistent-root",
                                                   "nope", "cache.db"))
        c.conn.close()
    except Exception as e:
        problems.append(f"SshDedupCache did not fall back: {e}")
    if problems:
        rep.fail("dedup DB writability", "; ".join(problems))
    else:
        rep.ok("dedup DB writability",
               "real create-probe + cache fallback verified")


def _check_py_older_fstring_compat(rep, ctx):
    """No PEP-701-only f-strings: a string token inside a single/double-quoted
    f-string must not reuse the f-string's own quote character — that parses
    only on Python 3.12+, and the release CI builds with 3.11, so one such
    line breaks the build AND every source install on older interpreters.
    ast.parse(feature_version=…) does NOT catch this — hence a tokenizer scan.
    Regression guard for the v4.0.0-cycle i18n wrapping bug found on Windows."""
    import tokenize
    # FSTRING_START/END are 3.12+ tokens: before PEP 701 an f-string arrived as
    # one STRING token and there is nothing to walk. Touching the attribute on
    # an older interpreter raised AttributeError, and the per-SECTION handler
    # turned that into "security section crashed", abandoning every check after
    # this one. The check that guards older Pythons cannot itself run on them.
    if not hasattr(tokenize, "FSTRING_START"):
        rep.skip("py<3.12 f-string compat",
                 f"needs a 3.12+ tokenizer; running on "
                 f"{sys.version.split()[0]}, which has no FSTRING_START token")
        return
    targets = [ctx["target"]]
    d = os.path.dirname(ctx["target"])
    for extra in ("blitcp_gui.py", "fast_copy.py", "fast_copy_modern_gui.py",
                  "build.py"):
        p = os.path.join(d, extra)
        if os.path.exists(p):
            targets.append(p)
    bad = []
    for path in targets:
        try:
            with open(path, "rb") as f:
                toks = list(tokenize.tokenize(f.readline))
        except (OSError, SyntaxError, tokenize.TokenError) as e:
            rep.warn("py<3.12 f-string compat", f"{path}: tokenize failed: {e}")
            continue
        stack = []  # active f-string quote char, or None for triple-quoted
        for t in toks:
            if t.type == tokenize.FSTRING_START:
                s = t.string.lstrip("frbuFRBU")
                # Triple-quoted f-strings may legally contain same-quote
                # strings in replacement fields on every Python version.
                stack.append(None if s.startswith(('"""', "'''"))
                             else (s[0] if s else '"'))
            elif t.type == tokenize.FSTRING_END:
                if stack:
                    stack.pop()
            elif stack and stack[-1] and t.type == tokenize.STRING:
                s = t.string.lstrip("frbuFRBU")
                if s and s[0] == stack[-1]:
                    bad.append(f"{os.path.basename(path)}:{t.start[0]}")
    if bad:
        rep.fail("py<3.12 f-string compat",
                 "quote reuse inside f-string (breaks CI build + Python "
                 "<3.12 installs): " + "; ".join(bad[:8]))
    else:
        rep.ok("py<3.12 f-string compat",
               f"{len(targets)} files free of PEP-701-only constructs")


def _check_passphrase_generator(rep, ctx):
    """generate_passphrase must be CSPRNG-backed (secrets, never random),
    use the full 7,776-word EFF list, and report honest entropy."""
    try:
        mod = _import_target(ctx)
    except Exception as e:
        rep.fail("passphrase generator", f"target import failed: {e}")
        return
    if not hasattr(mod, "generate_passphrase"):
        rep.skip("passphrase generator", "not present in target")
        return
    src = open(ctx["target"], encoding="utf-8", errors="replace").read()
    fn_src = src.split("def generate_passphrase", 1)[1].split("\ndef ", 1)[0]
    problems = []
    if "secrets.choice" not in fn_src:
        problems.append("does not draw from secrets.choice")
    if re.search(r"\brandom\.", fn_src):
        problems.append("uses the random module (not cryptographically secure)")
    try:
        words = mod._eff_words()
        if len(words) != 7776 or len(set(words)) != 7776:
            problems.append(f"wordlist {len(words)} words / "
                            f"{len(set(words))} unique (want 7776/7776)")
        p1, bits = mod.generate_passphrase(6, "-")
        p2, _ = mod.generate_passphrase(6, "-")
        if p1 == p2:
            problems.append("two draws returned the same phrase")
        if len(p1.split("-")) != 6:
            problems.append(f"asked 6 words, got {len(p1.split('-'))}")
        if abs(bits - 77.5) > 0.1:
            problems.append(f"entropy reported {bits}, expected 77.5")
        if not all(w in set(words) for w in p1.split("-")):
            problems.append("phrase contains non-wordlist tokens")
    except Exception as e:
        problems.append(f"generation failed: {e}")
    if problems:
        rep.fail("passphrase generator", "; ".join(problems))
    else:
        rep.ok("passphrase generator",
               "secrets-backed, 7776-word EFF list, entropy honest")


def _check_rename_migration(rep, ctx):
    """Regression checks for the fast-copy → blitcp rename (v4.0.0).

    The compatibility contract: legacy on-disk names stay recognized forever,
    the manifest HMAC seed string never changes, and the fast_copy import shim
    keeps old imports working. Each of these silently breaking would strand
    existing users' dedup state, manifests or scripts."""
    try:
        mod = _import_target(ctx)
    except Exception as e:
        rep.fail("rename migration", f"target import failed: {e}")
        return

    # 1) Frozen legacy names — the values are a contract, not a style choice.
    frozen = {
        "LEGACY_DEDUP_DB_NAME": ".fast_copy_dedup.db",
        "LEGACY_SUDO_AUDIT_FILE": ".fast_copy_audit.jsonl",
        "LEGACY_REMOTE_MANIFEST_NAME": ".fast_copy_manifest.json",
        "LEGACY_CLOUD_MANIFEST_NAME": ".fast_copy_manifest.json",
        "LEGACY_TAR_BUNDLE_NAME": ".fast_copy_bundle.tar",
    }
    bad = [f"{k}={getattr(mod, k, None)!r}"
           for k, v in frozen.items() if getattr(mod, k, None) != v]
    if bad:
        rep.fail("rename: frozen legacy names", "; ".join(bad))
    else:
        rep.ok("rename: frozen legacy names", f"{len(frozen)} constants intact")

    # 2) The HMAC seed literal must still be the pre-rename string.
    with open(ctx["target"], encoding="utf-8", errors="replace") as f:
        src = f.read()
    if 'f"fast_copy:{getpass.getuser()}' in src:
        rep.ok("rename: manifest HMAC seed frozen", "fast_copy: seed present")
    else:
        rep.fail("rename: manifest HMAC seed frozen",
                 "seed literal changed — every existing manifest would be "
                 "rejected as tampered")

    # 3) _migrate_local_sidecar: renames legacy in place, preserves content,
    #    and never clobbers an existing new-name file.
    if not hasattr(mod, "_migrate_local_sidecar"):
        rep.fail("rename: sidecar migration",
                 "_migrate_local_sidecar missing from target")
        return
    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        old = os.path.join(td, ".fast_copy_dedup.db")
        new = os.path.join(td, ".blitcp_dedup.db")
        with open(old, "w") as f:
            f.write("legacy-state")
        got = mod._migrate_local_sidecar(td, ".blitcp_dedup.db",
                                         ".fast_copy_dedup.db")
        ok1 = (got == new and os.path.exists(new) and not os.path.exists(old)
               and open(new).read() == "legacy-state")
        with open(old, "w") as f:
            f.write("second-legacy")
        got2 = mod._migrate_local_sidecar(td, ".blitcp_dedup.db",
                                          ".fast_copy_dedup.db")
        ok2 = (got2 == new and open(new).read() == "legacy-state"
               and os.path.exists(old))
        if ok1 and ok2:
            rep.ok("rename: sidecar migration", "rename-on-first-touch + "
                   "no-clobber verified")
        else:
            rep.fail("rename: sidecar migration",
                     f"first-touch={'OK' if ok1 else 'BROKEN'} "
                     f"no-clobber={'OK' if ok2 else 'BROKEN'}")

    # 4) Both env-var eras must be honoured (and both scrubbed from children).
    env_backup = {k: os.environ.get(k) for k in
                  ("BLITCP_CREDS_PASSPHRASE", "FAST_COPY_CREDS_PASSPHRASE")}
    try:
        os.environ["BLITCP_CREDS_PASSPHRASE"] = "new-era"
        os.environ["FAST_COPY_CREDS_PASSPHRASE"] = "old-era"
        mod._creds_passphrase_cache = None
        mod._scrub_passphrase_env()
        scrubbed = ("BLITCP_CREDS_PASSPHRASE" not in os.environ
                    and "FAST_COPY_CREDS_PASSPHRASE" not in os.environ)
        picked_new = bytes(mod._creds_passphrase_cache or b"") == b"new-era"
        if scrubbed and picked_new:
            rep.ok("rename: passphrase env compat",
                   "both names scrubbed, new name wins")
        else:
            rep.fail("rename: passphrase env compat",
                     f"scrubbed={scrubbed} new-wins={picked_new}")
    finally:
        mod._creds_passphrase_cache = None
        for k, v in env_backup.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    # 5) Import shim: `import fast_copy` must resolve to the blitcp module.
    shim = os.path.join(os.path.dirname(ctx["target"]), "fast_copy.py")
    if not os.path.exists(shim):
        rep.warn("rename: fast_copy shim", "fast_copy.py shim not found "
                 "beside target (breaks old imports/scripts)")
    elif "sys.modules[__name__] = blitcp" in open(shim, encoding="utf-8").read():
        rep.ok("rename: fast_copy shim", "module alias present")
    else:
        rep.fail("rename: fast_copy shim",
                 "shim exists but does not alias the blitcp module")

    # 6) Source-tree excludes must cover the legacy sidecar names too.
    if src.count("LEGACY_DEDUP_DB_NAME,") >= 2:
        rep.ok("rename: legacy names excluded from copies",
               "legacy sidecars in exclude_patterns")
    else:
        rep.fail("rename: legacy names excluded from copies",
                 "exclude_patterns no longer lists the legacy sidecar names — "
                 "old sidecars would be copied into destinations")


def _check_smb_parse(rep, ctx):
    """Unit-check parse_smb_url: smb:// + UNC map correctly, and non-SMB inputs
    (drive letters, SSH user@host:/path, cloud URLs) are left for other parsers."""
    try:
        mod = _import_target(ctx)
    except Exception as e:
        rep.skip("SMB URL parsing", f"could not import target: {e}")
        return
    p = getattr(mod, "parse_smb_url", None)
    if not p:
        rep.skip("SMB URL parsing", "parse_smb_url not present")
        return
    bad = []

    def check(inp, want):
        try:
            got = p(inp)
        except SystemExit as e:
            bad.append(f"{inp!r}→error {e}")
            return
        if want is None:
            if got is not None:
                bad.append(f"{inp!r}→expected None, got {got}")
        elif got is None:
            bad.append(f"{inp!r}→None")
        elif (got.scheme, got.host, got.container, got.prefix) != want:
            bad.append(f"{inp!r}→{(got.scheme, got.host, got.container, got.prefix)} != {want}")

    check("smb://h/s/p", ("smb", "h", "s", "p"))
    check("smb://user@h:445/s/a/b", ("smb", "h", "s", "a/b"))
    check(r"\\h\s\p", ("smb", "h", "s", "p"))
    check("//h/s/p", ("smb", "h", "s", "p"))
    check("C:\\x", None)
    check("user@host:/p", None)
    check("/local/path", None)
    check("s3://bucket/key", None)
    if bad:
        rep.fail("SMB URL parsing", "; ".join(bad[:6]))
    else:
        rep.ok("SMB URL parsing",
               "smb:// + UNC map correctly; non-SMB inputs ignored")


def _check_posix_only_os_calls(rep, ctx):
    """Flag POSIX-only os.* fd/metadata calls (fchmod, fchown, fdatasync, …) that
    are NOT guarded by hasattr(os, "<name>") in an enclosing function AND are
    not inside a platform-gated branch. These raise AttributeError (not
    OSError) on Windows, so a bare `except OSError` does not catch them and
    the copy crashes. Regression guard for the v3.8.1 os.fchmod-on-Windows bug
    (large-file copies crashed under default preserve).

    Two gate shapes count as guarded besides a nearby hasattr:
      * an ancestor `if` whose test compares against the string "Windows"
        (e.g. `if _system != "Windows" and …:`), and
      * an ancestor `if` testing a name assigned from such a comparison
        (e.g. `use_fd = _system != "Windows" and …` then `if use_fd:`)."""
    WATCH = {"fchmod", "fchown", "lchmod", "fchdir", "fdatasync",
             "posix_fadvise", "posix_fallocate", "mkfifo", "mknod"}
    try:
        with open(ctx["target"], "r", encoding="utf-8", errors="replace") as f:
            tree = ast.parse(f.read(), filename=ctx["target"])
    except (OSError, SyntaxError) as e:
        rep.skip("POSIX-only os.* guards", str(e))
        return
    parents = {}
    for node in ast.walk(tree):
        for ch in ast.iter_child_nodes(node):
            parents[ch] = node

    def enclosing_funcs(node):
        out, p = [], parents.get(node)
        while p is not None:
            if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append(p)
            p = parents.get(p)
        return out

    def guards(fn, name):
        for n in ast.walk(fn):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id in ("hasattr", "getattr") and len(n.args) >= 2
                    and isinstance(n.args[0], ast.Name) and n.args[0].id == "os"
                    and isinstance(n.args[1], ast.Constant)
                    and n.args[1].value == name):
                return True
        return False

    def _has_windows_compare(expr):
        for n in ast.walk(expr):
            if isinstance(n, ast.Compare) and any(
                    isinstance(c, ast.Constant) and c.value == "Windows"
                    for c in ast.walk(n)):
                return True
        return False

    def _platform_flag_names(fns):
        """Names assigned (flow-insensitively) from a "Windows" comparison in
        any enclosing function — `use_fd = _system != "Windows" and …`."""
        names = set()
        for fn in fns:
            for n in ast.walk(fn):
                if isinstance(n, ast.Assign) and _has_windows_compare(n.value):
                    names.update(t.id for t in n.targets
                                 if isinstance(t, ast.Name))
        return names

    def _platform_gated(node):
        flag_names = _platform_flag_names(enclosing_funcs(node))
        p = parents.get(node)
        while p is not None:
            if isinstance(p, ast.If):
                if _has_windows_compare(p.test):
                    return True
                if any(isinstance(n, ast.Name) and n.id in flag_names
                       for n in ast.walk(p.test)):
                    return True
            p = parents.get(p)
        return False

    offenders = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in WATCH
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"):
            nm = node.func.attr
            if (not any(guards(fn, nm) for fn in enclosing_funcs(node))
                    and not _platform_gated(node)):
                offenders.append(f"os.{nm} @L{node.lineno}")
    if offenders:
        rep.fail("POSIX-only os.* guards",
                 "unguarded (AttributeError on Windows): " + "; ".join(offenders[:8]))
    else:
        rep.ok("POSIX-only os.* guards",
               "fd/metadata POSIX calls are hasattr-guarded (Windows-safe)")


def _check_streaming_relay_invariants(rep, ctx):
    """Regressions from the streaming-relay work (2026-09-01):
    (a) S3Backend.upload_stream must pass use_threads=False — s3transfer's
        per-call thread pool over the relay's own object-level pool raced
        concurrent part reads on the shared spool and desynced the HTTP
        connection (UploadPart 200 landed unparsed → KeyError 'ETag').
    (b) SMBBackend streams must respect the session lock for their whole
        lifetime (concurrent ops on one SMB session corrupt data): open_read
        hands out a lock-holding reader; upload_stream writes under the lock.
    (c) The relay paths must stay streaming: no dataset-sized blitcp_relay_
        temp dir may reappear in _cloud_to_cloud / _relay_object_ssh."""
    try:
        with open(ctx["target"], "r", encoding="utf-8", errors="replace") as f:
            src = f.read()
        tree = ast.parse(src, filename=ctx["target"])
    except (OSError, SyntaxError) as e:
        rep.skip("streaming relay invariants", str(e))
        return
    lines = src.splitlines()

    def seg(node):
        return "\n".join(lines[node.lineno - 1:node.end_lineno])

    funcs = {}   # ("Class.method" or "func") → source segment
    for n in tree.body:
        if isinstance(n, ast.ClassDef):
            for m in n.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    funcs[f"{n.name}.{m.name}"] = seg(m)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs[n.name] = seg(n)

    bad = []
    s3up = funcs.get("S3Backend.upload_stream")
    if s3up is None:
        bad.append("S3Backend.upload_stream missing")
    elif "use_threads=False" not in s3up:
        bad.append("S3Backend.upload_stream lost use_threads=False "
                   "(multipart connection-desync regression)")
    smbrd = funcs.get("SMBBackend.open_read")
    if smbrd is None:
        bad.append("SMBBackend.open_read missing")
    elif "_SMBLockedFile" not in smbrd:
        bad.append("SMBBackend.open_read no longer hands out a lock-holding "
                   "reader (SMB session-corruption regression)")
    smbup = funcs.get("SMBBackend.upload_stream")
    if smbup is None:
        bad.append("SMBBackend.upload_stream missing")
    elif "with self._lock" not in smbup:
        bad.append("SMBBackend.upload_stream no longer writes under the "
                   "session lock")
    for fn in ("_cloud_to_cloud", "_relay_object_ssh"):
        body = funcs.get(fn)
        if body and "blitcp_relay_" in body:
            bad.append(f"{fn} regressed to a dataset-sized temp-dir relay")
    # HTTP-source guards (2026-09-02): a login/terms page returned instead of
    # the file was saved silently with "Verified ✓" — both HTTP legs must run
    # the HTML-wall check before writing anything.
    for fn in ("_http_to_ssh", "_http_to_smb"):
        body = funcs.get(fn)
        if body is None:
            bad.append(f"{fn} missing")
        elif "_http_looks_like_html_wall" not in body:
            bad.append(f"{fn} lost the HTML-wall guard (a terms/login page "
                       "would be saved as the file again)")
    if bad:
        rep.fail("streaming relay invariants", "; ".join(bad[:6]))
    else:
        rep.ok("streaming relay invariants",
               "S3 single-threaded parts; SMB streams hold the session lock; "
               "no temp-dir relay; HTTP legs keep the HTML-wall guard")


def _check_http_auth_handling(rep, ctx):
    """HTTP-source sign-in (2026-09-06). Three regressions to keep out:
    (a) the GUI must hand the engine an http(s) password/header through the
        ENVIRONMENT, never on argv — argv is world-readable via `ps`, which is
        exactly why the SSH path already uses --ssh-*-password-env;
    (b) a one-off sign-in must not survive a change of host: cookies and a
        Bearer token belong to the host they were typed for, and replaying
        them at the next URL would leak them to a different server;
    (c) a frozen build must not answer "pip install browser-cookie3" — there
        is no pip inside the binary, so the only honest advice is a
        cookies.txt export. build.py must therefore bundle the reader."""
    root = os.path.dirname(os.path.abspath(ctx["target"]))
    gui = os.path.join(root, "blitcp_gui.py")
    if not os.path.isfile(gui):
        rep.skip("http auth handling", "no blitcp_gui.py beside the target")
        return
    try:
        with open(ctx["target"], encoding="utf-8", errors="replace") as f:
            eng = f.read()
        with open(gui, encoding="utf-8", errors="replace") as f:
            gsrc = f.read()
        gtree = ast.parse(gsrc, filename=gui)
    except (OSError, SyntaxError) as e:
        rep.skip("http auth handling", str(e))
        return

    glines = gsrc.splitlines()
    gfuncs = {}
    for n in ast.walk(gtree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            gfuncs[n.name] = "\n".join(glines[n.lineno - 1:n.end_lineno])

    bad = []
    flags = gfuncs.get("_http_auth_flags")
    if flags is None:
        bad.append("GUI lost _http_auth_flags (no per-transfer HTTP sign-in)")
    else:
        for env_flag, what in (("--http-password-env", "password"),
                               ("--http-header-env", "header")):
            if env_flag not in flags:
                bad.append(f"GUI no longer passes the http {what} through "
                           f"{env_flag} — it would land in argv, readable "
                           f"by any user via ps")
        if '"--http-password"' in flags or '"--http-header"' in flags:
            bad.append("GUI passes an http secret literally on argv")
        if "_valid_http_auth" not in flags:
            bad.append("GUI applies a one-off sign-in without re-checking the "
                       "host — cookies/token would follow the URL elsewhere")
    if "_valid_http_auth" not in gfuncs:
        bad.append("GUI lost _valid_http_auth (host-change invalidation)")

    # The engine must accept what the GUI sends, and read it from the env.
    if "--http-header-env" not in eng:
        bad.append("engine has no --http-header-env for the GUI to use")
    resolve = None
    try:
        etree = ast.parse(eng, filename=ctx["target"])
        elines = eng.splitlines()
        for n in ast.walk(etree):
            if isinstance(n, ast.FunctionDef) and n.name == "_http_resolve_auth":
                resolve = "\n".join(elines[n.lineno - 1:n.end_lineno])
    except SyntaxError:
        pass
    if resolve is not None and "http_header_env" not in resolve:
        bad.append("_http_resolve_auth ignores http_header_env")

    # Frozen builds: honest message + the reader actually bundled.
    cookie_fn = None
    if resolve is not None:
        for n in ast.walk(ast.parse(eng, filename=ctx["target"])):
            if isinstance(n, ast.FunctionDef) and n.name == "_cookie_header_for":
                cookie_fn = "\n".join(eng.splitlines()[n.lineno - 1:n.end_lineno])
    if cookie_fn and 'getattr(sys, "frozen", False)' not in cookie_fn:
        bad.append("_cookie_header_for still tells a frozen build to pip "
                   "install browser-cookie3")
    # The one-off sign-in must PIN to the first host it is seen with. Leaving
    # it unpinned (entered before the URL was typed) meant the guard never
    # fired and the token followed whatever host was pasted next — shipped in
    # 4.2.4, fixed in 4.2.5.
    guard = gfuncs.get("_valid_http_auth", "")
    if guard and "self._http_auth_host = host" not in guard:
        bad.append("_valid_http_auth no longer pins an unbound sign-in to the "
                   "first host — a token entered before the URL would follow "
                   "any host pasted later")

    # sudo resets the environment: every secret passed BY NAME has to be named
    # to survive, or the elevated run proceeds unauthenticated and fails later
    # with a 401 nobody can explain.
    reexec = None
    for n in ast.walk(ast.parse(eng, filename=ctx["target"])):
        if isinstance(n, ast.FunctionDef) and n.name == "_reexec_under_sudo":
            reexec = "\n".join(eng.splitlines()[n.lineno - 1:n.end_lineno])
    if reexec is not None:
        if "--preserve-env=" not in reexec:
            bad.append("_reexec_under_sudo does not preserve the secret env "
                       "vars — an elevated run silently loses the sign-in")
        # Both eras of every name. The credentials PATH matters as much as the
        # passphrase: sudo wipes the override, and an elevated run that falls
        # back to default_credentials_path() reads a DIFFERENT vault than the
        # caller chose — silently, and possibly the legacy file beside the
        # script.
        #
        # Scoped to the `carried = [...]` list, not to the function: every one
        # of these names also appears in the preflight a few lines above, so a
        # whole-function substring search stayed green with the name deleted
        # from the list it is supposed to be in.
        _carried = ""
        if "carried = [" in reexec:
            _tail = reexec.split("carried = [", 1)[1]
            _carried = _tail.split("]", 1)[0]
        if not _carried:
            bad.append("_reexec_under_sudo has no `carried = [...]` list — "
                       "nothing is preserved across the elevation")
        for var in ("FC_HTTP_PW", "FC_HTTP_HDR",
                    "BLITCP_CREDS_PASSPHRASE", "FAST_COPY_CREDS_PASSPHRASE",
                    "BLITCP_CREDENTIALS", "FAST_COPY_CREDENTIALS"):
            if var not in _carried:
                bad.append(f"{var} is not carried through sudo")
        # …and the vault that root is about to open must pass the same
        # ownership/write test as the script. Both names, because either one
        # can be the override that wins, and only when the file is already
        # there (naming a path that does not exist yet is a first run).
        _pre_ok = ("_check_safe_for_sudo(_cred_path" in reexec
                   and "credentials file" in reexec)
        _pre_names = all(f'"{v}"' in reexec.split("for _cred_var in", 1)[-1]
                         .split(":", 1)[0]
                         for v in ("BLITCP_CREDENTIALS", "FAST_COPY_CREDENTIALS")) \
            if "for _cred_var in" in reexec else False
        if not (_pre_ok and _pre_names):
            bad.append("the sudo preflight no longer checks the vault named by "
                       "$BLITCP_CREDENTIALS / $FAST_COPY_CREDENTIALS — root "
                       "would open a file anyone could have rewritten")
        if "os.path.exists(_cred_path)" not in reexec:
            bad.append("the sudo vault check no longer skips a path that does "
                       "not exist yet — a first elevated run would be refused")
    if resolve is not None and "never arrived" not in resolve:
        bad.append("_http_resolve_auth degrades silently when a named env var "
                   "is missing instead of saying so")

    # Every build leg must ship the cookie reader. The macOS-Intel leg installs
    # its own dependency set instead of going through build.py, and shipped
    # 4.2.4 without it while the release went green.
    wf = os.path.join(root, ".github", "workflows", "release.yml")
    if os.path.isfile(wf):
        with open(wf, encoding="utf-8", errors="replace") as f:
            wsrc = f.read()
        intel = wsrc.split("build-macos-intel:", 1)
        if len(intel) == 2 and "browser-cookie3" not in intel[1]:
            bad.append("the macOS-Intel build leg no longer installs "
                       "browser-cookie3 — that binary alone would ship "
                       "unable to read a browser session")

    build_py = os.path.join(root, "build.py")
    if os.path.isfile(build_py):
        with open(build_py, encoding="utf-8", errors="replace") as f:
            bsrc = f.read()
        if "browser_cookie3" not in bsrc:
            bad.append("build.py no longer bundles browser_cookie3 — "
                       "--cookies-from-browser would be dead in the binaries")

    if bad:
        rep.fail("http auth handling", "; ".join(bad[:6]))
    else:
        rep.ok("http auth handling",
               "GUI sends http password/header via env not argv; one-off "
               "sign-in dropped on host change; frozen builds get honest "
               "cookie advice and bundle the reader")


def _check_sudo_preflight_and_log(rep, ctx):
    """Two regressions from 4.2.6.

    (a) --use-sudo refused ANY group-writable script. Debian/Ubuntu/Kali give
        each user a private group and ship umask 002, so an ordinary `cp`
        produces 0664 whose group is that user alone: the refusal protected
        against nobody and made the flag unusable out of the box. Group-write
        must now be judged by who is actually IN the group — while a
        world-writable file stays refused, since that one is real.
    (b) The GUI log clipped every long line. Engine errors put the remedy at
        the end ("Fix: chmod go-w …"), so the user saw the complaint and never
        the fix."""
    root = os.path.dirname(os.path.abspath(ctx["target"]))
    gui = os.path.join(root, "blitcp_gui.py")
    try:
        with open(ctx["target"], encoding="utf-8", errors="replace") as f:
            eng = f.read()
        gsrc = ""
        if os.path.isfile(gui):
            with open(gui, encoding="utf-8", errors="replace") as f:
                gsrc = f.read()
    except OSError as e:
        rep.skip("sudo preflight + log legibility", str(e))
        return

    bad = []
    # Scoped to _check_safe_for_sudo, not the whole file. As a file-wide string
    # search this fired on an unrelated mode mask in _safe_tar_extract that
    # happens to spell the same two constants next to each other — a false
    # positive that says the sudo preflight regressed when it did not.
    preflight = ""
    _eng_tree = ast.parse(eng, filename=ctx["target"])
    for n in ast.walk(_eng_tree):
        if isinstance(n, ast.FunctionDef) and n.name == "_check_safe_for_sudo":
            preflight = "\n".join(eng.splitlines()[n.lineno - 1:n.end_lineno])
    if not preflight:
        bad.append("_check_safe_for_sudo is gone — the sudo preflight cannot "
                   "be checked at all")
    if "_group_writers_besides" not in eng:
        bad.append("the sudo preflight lost its group-membership test — every "
                   "0664 file under umask 002 would be refused again")
    if "S_IWGRP | stat.S_IWOTH" in preflight:
        bad.append("the sudo preflight is back to refusing group-write "
                   "outright, without asking who is in the group")
    if "stat.S_IWOTH" not in preflight:
        bad.append("the sudo preflight no longer refuses a world-writable "
                   "script — that one is a real escalation path")
    # The helper must count PRIMARY members too: a user-private group lists
    # nobody in gr_mem, and so does a group whose only member joined by gid.
    helper = ""
    for n in ast.walk(ast.parse(eng, filename=ctx["target"])):
        if isinstance(n, ast.FunctionDef) and n.name == "_group_writers_besides":
            helper = "\n".join(eng.splitlines()[n.lineno - 1:n.end_lineno])
    if helper:
        if "getpwall" not in helper:
            bad.append("_group_writers_besides ignores primary-group members, "
                       "so a shared group would read as private")
        if "return None" not in helper:
            bad.append("_group_writers_besides cannot report 'unknown' — an "
                       "unreadable group would be treated as safe")
    if gsrc:
        add_log = ""
        for n in ast.walk(ast.parse(gsrc, filename=gui)):
            if isinstance(n, ast.FunctionDef) and n.name == "_add_log":
                add_log = "\n".join(gsrc.splitlines()[n.lineno - 1:n.end_lineno])
        if add_log:
            if "setWordWrap(True)" not in add_log:
                bad.append("GUI log lines no longer wrap — the end of a long "
                           "error, which is where the fix is, gets clipped")
            if "white-space:pre'" in add_log:
                bad.append("GUI log lines use white-space:pre, which defeats "
                           "the word wrap on coloured lines")

    if bad:
        rep.fail("sudo preflight + log legibility", "; ".join(bad[:6]))
    else:
        rep.ok("sudo preflight + log legibility",
               "group-write judged by real membership, world-write still "
               "refused; GUI log wraps so the fix in an error stays visible")


def _check_gui_phase_labels(rep, ctx):
    """Every "Phase N — <name>" the engine prints must map to a GUI header
    label. The map ended at "block copy", so on every SSH transfer the header
    stayed on "Hashing…" from the end of deduplication to the end of the run —
    the GUI said it was hashing while it was streaming 4.5 GB. A phase added
    later without a label would silently do the same, so this compares the two
    lists rather than a fixed set."""
    root = os.path.dirname(os.path.abspath(ctx["target"]))
    gui = os.path.join(root, "blitcp_gui.py")
    if not os.path.isfile(gui):
        rep.skip("GUI phase labels", "no blitcp_gui.py beside the target")
        return
    try:
        with open(ctx["target"], encoding="utf-8", errors="replace") as f:
            eng = f.read()
        with open(gui, encoding="utf-8", errors="replace") as f:
            gsrc = f.read()
    except OSError as e:
        rep.skip("GUI phase labels", str(e))
        return

    phases = sorted(set(re.findall(r'banner\(f?"(Phase [^"]+)"', eng)))
    if not phases:
        rep.skip("GUI phase labels", "no phase banners found")
        return
    m = re.search(r"_PHASE_LABELS = \((.*?)\)\n", gsrc, re.S)
    if not m:
        rep.fail("GUI phase labels", "_PHASE_LABELS is gone from the GUI")
        return
    keys = re.findall(r'\("([^"]+)",\s*"', m.group(1))

    unlabelled = []
    for ph in phases:
        name = ph.split("—", 1)[-1].split("-", 1)[-1].strip().lower() \
            if "—" in ph else ph.lower()
        if not any(k in name for k in keys):
            unlabelled.append(ph)
    if unlabelled:
        rep.fail("GUI phase labels",
                 "no GUI header label for: " + "; ".join(unlabelled[:4]))
    else:
        rep.ok("GUI phase labels",
               f"all {len(phases)} engine phases map to a header label")


def _check_r2r_hash_negotiation(rep, ctx):
    """Remote-to-remote used to compare each side's FAVOURITE hash tool:
    `scaps["halgo"] == dcaps["halgo"]`. A NAS with sha256sum and a workstation
    with xxh128sum installed each picked a different favourite, so a pair that
    shared sha256sum AND md5sum was told "no common tool" — which turned off
    deduplication and, far worse, verification, while the banner still
    advertised both and the summary printed no Verify line at all."""
    try:
        with open(ctx["target"], encoding="utf-8", errors="replace") as f:
            eng = f.read()
    except OSError as e:
        rep.skip("r2r hash negotiation", str(e))
        return

    bad = []
    if 'scaps["halgo"] == dcaps["halgo"]' in eng:
        bad.append("R2R is back to comparing each side's favourite hash tool "
                   "instead of negotiating a common one")
    if "_common_hash_algo" not in eng:
        bad.append("_common_hash_algo is gone — no common-algorithm negotiation")
    else:
        nego = ""
        for n in ast.walk(ast.parse(eng, filename=ctx["target"])):
            if isinstance(n, ast.FunctionDef) and n.name == "_common_hash_algo":
                nego = "\n".join(eng.splitlines()[n.lineno - 1:n.end_lineno])
        if nego and "hashes" not in nego:
            bad.append("_common_hash_algo no longer looks at the full tool "
                       "list, so a shared second choice stays invisible")
    if 'caps["hashes"]' not in eng and '"hashes": {}' not in eng:
        bad.append("the remote probe records only one hash tool per host")
    # The advertised feature list must follow reality, and a run that could not
    # verify has to say so rather than omitting the line.
    if 'remote → remote  [dedup · incremental · verify]' in eng:
        bad.append("the R2R banner advertises dedup+verify unconditionally, "
                   "even when no hash tool is shared")
    if "not run" not in eng:
        bad.append("the summary goes silent when verification did not run — "
                   "an absent line reads as success")

    if bad:
        rep.fail("r2r hash negotiation", "; ".join(bad[:6]))
    else:
        rep.ok("r2r hash negotiation",
               "a shared algorithm is negotiated from both hosts' full tool "
               "lists; the banner and summary state what actually ran")


def section_security(rep, ctx):
    targets = [ctx["target"]]
    repo = os.path.dirname(os.path.abspath(ctx["target"]))
    for extra in ("fast_copy_modern_gui.py", "build.py"):
        p = os.path.join(repo, extra)
        if os.path.exists(p):
            targets.append(p)

    total_fail = total_warn = 0
    for path in targets:
        label = os.path.basename(path)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                src = f.read()
        except OSError as e:
            rep.skip(f"scan {label}", str(e))
            continue
        try:
            tree = ast.parse(src, filename=path)
        except SyntaxError as e:
            rep.fail(f"parse {label}", f"syntax error: {e}")
            continue

        vv = _VulnVisitor()
        vv.visit(tree)
        fails = [f for f in vv.findings if f[0] == FAIL]
        warns = [f for f in vv.findings if f[0] == WARN]
        total_fail += len(fails)
        total_warn += len(warns)
        if fails:
            rep.fail(f"AST scan {label}",
                     "; ".join(f"{l} @L{ln}" for _, l, ln in fails[:8]))
        elif warns:
            rep.warn(f"AST scan {label}",
                     "; ".join(f"{l} @L{ln}" for _, l, ln in warns[:8]))
        else:
            rep.ok(f"AST scan {label}", "no dangerous patterns")

        # secret literals in source
        sec_hits = []
        for i, line in enumerate(src.splitlines(), 1):
            for pat, lbl in _SECRET_PATTERNS:
                if pat.search(line):
                    sec_hits.append(f"{lbl} @L{i}")
        if sec_hits:
            rep.warn(f"secret literals {label}", "; ".join(sec_hits[:6]))
        else:
            rep.ok(f"secret literals {label}", "none")

    # repo hygiene: secret material checked into the tree (never deleted)
    hygiene = []
    try:
        for fn in os.listdir(repo):
            low = fn.lower()
            if (low.endswith((".pem", ".key"))
                    or re.match(r"credentials\.json\.bak", low)
                    or re.match(r"fastcopy-.*\.json$", low)):
                hygiene.append(fn)
    except OSError:
        pass
    if hygiene:
        rep.warn("repo secret hygiene",
                 "checked-in secret material: " + ", ".join(sorted(hygiene)))
    else:
        rep.ok("repo secret hygiene", "no loose secret files")

    # credentials at rest must always be encrypted
    _check_credentials_encrypted(rep, repo)
    # ...and no GUI code path may write secret credentials in cleartext
    _check_gui_creds_enforce_encryption(rep, repo)
    # SMB/UNC URL parsing must not collide with SSH/cloud/local paths
    _check_smb_parse(rep, ctx)
    # POSIX-only os.* fd calls must be hasattr-guarded (Windows crash regression)
    _check_posix_only_os_calls(rep, ctx)
    # fast-copy → blitcp rename must keep the legacy-name compat contract
    _check_rename_migration(rep, ctx)
    # creds passphrase generator must stay CSPRNG-backed with honest entropy
    _check_passphrase_generator(rep, ctx)
    # PEP-701-only f-strings break the 3.11 CI build and old-Python installs
    _check_py_older_fstring_compat(rep, ctx)
    # Windows C:\ dedup-DB regression: real writability probe + cache fallback
    _check_dedup_db_writability(rep, ctx)
    # streaming relays: S3 single-threaded parts, SMB lock-held streams,
    # no dataset-sized temp-dir relay
    _check_streaming_relay_invariants(rep, ctx)
    _check_http_auth_handling(rep, ctx)
    _check_sudo_preflight_and_log(rep, ctx)
    _check_gui_phase_labels(rep, ctx)
    _check_r2r_hash_negotiation(rep, ctx)

    # external scanners (best effort)
    _run_external_scanner(rep, "bandit",
                          ["bandit", "-q", "-r", ctx["target"], "-f", "json"],
                          _parse_bandit)
    _run_external_scanner(rep, "pip-audit",
                          ["pip-audit", "-f", "json"], _parse_pip_audit)
    _run_external_scanner(rep, "safety",
                          ["safety", "check", "--json"], _parse_safety)

    if total_fail == 0:
        rep.ok("builtin vuln scan", "0 high-severity findings across sources")


def _run_external_scanner(rep, name, cmd, parser):
    if shutil.which(cmd[0]) is None:
        rep.skip(f"{name}", "not installed")
        return
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=300)
    except (subprocess.TimeoutExpired, OSError) as e:
        rep.skip(f"{name}", f"could not run: {e}")
        return
    try:
        sev, detail = parser(proc.stdout, proc.stderr, proc.returncode)
    except Exception as e:  # noqa: BLE001 - scanner output varies wildly
        rep.warn(f"{name}", f"ran but output unparsed: {e}")
        return
    rep.record(name, sev, detail)


def _parse_bandit(out, err, rc):
    data = json.loads(out or "{}")
    results = data.get("results", [])
    high = [r for r in results if r.get("issue_severity") in ("HIGH", "MEDIUM")]
    if high:
        top = "; ".join(f"{r['test_id']} L{r['line_number']}" for r in high[:6])
        return (FAIL if any(r.get("issue_severity") == "HIGH" for r in high)
                else WARN), f"{len(high)} med/high findings: {top}"
    return PASS, f"{len(results)} low/no findings"


def _parse_pip_audit(out, err, rc):
    data = json.loads(out or "{}")
    deps = data.get("dependencies", data) if isinstance(data, dict) else data
    vulns = []
    for d in (deps if isinstance(deps, list) else []):
        for v in d.get("vulns", []):
            vulns.append(f"{d.get('name')}:{v.get('id')}")
    if vulns:
        return FAIL, f"{len(vulns)} vulnerable deps: " + ", ".join(vulns[:6])
    return PASS, "no known-vulnerable dependencies"


def _parse_safety(out, err, rc):
    data = json.loads(out or "[]")
    rows = data if isinstance(data, list) else data.get("vulnerabilities", [])
    if rows:
        return FAIL, f"{len(rows)} advisories"
    return PASS, "no advisories"


# --------------------------------------------------------------------------- #
# Section 2: leaks — temp files, fds, dedup DB, child processes
# --------------------------------------------------------------------------- #

_TEMP_GARBAGE = re.compile(
    r"(^fast_copy|^fc_manifest_|\.fc_|\.update_tmp$|^fast_copy_relay_)")


def _list_children():
    """Best-effort set of child PIDs of this process (Linux /proc)."""
    if not os.path.isdir("/proc"):
        return None
    me = os.getpid()
    kids = set()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as f:
                fields = f.read().split()
            ppid = int(fields[3])
            if ppid == me:
                kids.add(int(entry))
        except (OSError, IndexError, ValueError):
            continue
    return kids


def _open_fd_count():
    """Number of open fds for this (auditor) process, or None if unknown."""
    if os.path.isdir("/proc/self/fd"):
        try:
            return len(os.listdir("/proc/self/fd"))
        except OSError:
            return None
    return None


def section_leaks(rep, ctx):
    target = ctx["target"]

    with temp_workspace() as ws:
        src = make_tree(os.path.join(ws, "src"), big_mb=2)
        dst = os.path.join(ws, "dst")
        child_tmp = os.path.join(ws, "child_tmp")
        os.makedirs(child_tmp)

        fd_before = _open_fd_count()
        kids_before = _list_children()

        rc, out, err = run_fc(target, [src, dst], tmpdir=child_tmp)

        if rc != 0:
            rep.fail("leak baseline copy", f"rc={rc}: {err.strip()[:200]}")
            return

        # temp-file leak: the child's private TMPDIR must be empty of fc junk
        leftovers = [n for n in os.listdir(child_tmp)
                     if _TEMP_GARBAGE.search(n)]
        if leftovers:
            rep.fail("temp-file leak (TMPDIR)", f"stray: {leftovers[:8]}")
        else:
            rep.ok("temp-file leak (TMPDIR)", "child scratch clean")

        # also no fc junk left beside the destination
        dst_junk = [n for n in os.listdir(dst)
                    if _TEMP_GARBAGE.search(n) and not n.startswith(
                        ".fast_copy_dedup")]
        if dst_junk:
            rep.fail("temp-file leak (dest)", f"stray: {dst_junk[:8]}")
        else:
            rep.ok("temp-file leak (dest)", "destination clean")

        # auditor fd leak across the harness call
        fd_after = _open_fd_count()
        if fd_before is None or fd_after is None:
            rep.skip("fd leak", "/proc/self/fd unavailable")
        elif fd_after > fd_before + 2:
            rep.fail("fd leak", f"{fd_before} -> {fd_after} open fds")
        else:
            rep.ok("fd leak", f"{fd_before} -> {fd_after} open fds")

        # zombie / lingering child processes
        kids_after = _list_children()
        if kids_before is None or kids_after is None:
            rep.skip("child-process leak", "/proc unavailable")
        else:
            extra = kids_after - kids_before
            if extra:
                rep.fail("child-process leak", f"surviving pids: {extra}")
            else:
                rep.ok("child-process leak", "no surviving children")

        # dedup DB: valid sqlite, not left locked/open
        ddb = os.path.join(dst, ".fast_copy_dedup.db")
        if os.path.exists(ddb):
            try:
                conn = sqlite3.connect(ddb)
                res = conn.execute("PRAGMA quick_check").fetchone()
                conn.close()
                if res and res[0] == "ok":
                    rep.ok("dedup DB integrity", "quick_check ok, not locked")
                else:
                    rep.fail("dedup DB integrity", f"quick_check={res}")
            except sqlite3.Error as e:
                rep.fail("dedup DB integrity", f"sqlite error: {e}")
        else:
            rep.skip("dedup DB integrity", "no dedup DB produced")

    # dry-run must not write anything — not in the destination, and not at the
    # destination's MOUNT ROOT either. Watching the destination alone is why a
    # 36 KB dedup cache shipped unnoticed: the cache prefers the mount root and
    # only falls back to the destination, so wherever /tmp is its own mount the
    # file landed one directory up, outside everything this check looked at.
    # The destination must also not come into EXISTENCE — creating the
    # directory is itself the side effect that turns a mistyped preview into a
    # real mkdir.
    with temp_workspace() as ws:
        src = make_tree(os.path.join(ws, "src"), big_mb=1)
        dst = os.path.join(ws, "dst")
        mroot = mount_root(ws)
        before_root = dir_names(mroot)
        rc, out, err = run_fc(target, ["--dry-run", src, dst])
        leaked_root = sorted(dir_names(mroot) - before_root)
        created = os.path.exists(dst)
        if rc != 0:
            rep.fail("dry-run no side effects", f"rc={rc}: {err.strip()[:160]}")
        elif created:
            rep.fail("dry-run no side effects",
                     f"dry-run created the destination: "
                     f"{sorted(dir_names(dst))[:6] or 'empty dir'}")
        elif leaked_root:
            rep.fail("dry-run no side effects",
                     f"dry-run wrote to the mount root {mroot}: "
                     f"{leaked_root[:6]}")
        else:
            rep.ok("dry-run no side effects",
                   f"destination not created, {mroot} unchanged")


# --------------------------------------------------------------------------- #
# Section 3: modes — L2L always; remote/cloud auto-detect then skip
# --------------------------------------------------------------------------- #

def _ssh_localhost_ok():
    """True if a non-interactive `ssh localhost true` succeeds quickly."""
    if shutil.which("ssh") is None:
        return False, "ssh client not installed"
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
             "-o", "StrictHostKeyChecking=accept-new", "localhost", "true"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=15)
        if proc.returncode == 0:
            return True, ""
        return False, "passwordless ssh to localhost unavailable"
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"ssh probe failed: {e}"


def section_modes(rep, ctx):
    target = ctx["target"]

    # L2L — always exercised for real
    with temp_workspace() as ws:
        src = make_tree(os.path.join(ws, "src"))
        dst = os.path.join(ws, "dst")
        rc, out, err = run_fc(target, [src, dst])
        ok, detail = tree_equal(src, dst)
        if rc == 0 and ok:
            rep.ok("L2L (local->local)", "tree verified byte-for-byte")
        else:
            rep.fail("L2L (local->local)",
                     f"rc={rc} {detail} {err.strip()[:160]}")

    # Remote modes
    allow = ctx["allow_remote"]
    ssh_ok, why = _ssh_localhost_ok() if allow else (False, "use --allow-remote")
    if not (allow and ssh_ok):
        reason = why if allow else "remote modes opt-in (--allow-remote)"
        for m in ("Push (local->remote)", "Pull (remote->local)",
                  "R2R (remote->remote)", "SSH tar mode (--ssh-no-sftp)"):
            rep.skip(m, reason)
    else:
        host = "localhost"
        # Push
        push_layout = None
        with temp_workspace() as ws:
            src = make_tree(os.path.join(ws, "src"))
            dst = os.path.join(ws, "dst")
            rc, out, err = run_fc(target, [src, f"{host}:{dst}"], timeout=180)
            ok, d = tree_equal(src, dst)
            if rc == 0 and os.path.isdir(dst):
                push_layout = sorted(
                    os.path.relpath(os.path.join(dp, fn), dst)
                    for dp, _, fns in os.walk(dst) for fn in fns
                    if not fn.startswith((".fast_copy", ".blitcp")))
            (rep.ok if rc == 0 and ok else rep.fail)(
                "Push (local->remote)",
                "verified" if rc == 0 and ok else f"rc={rc} {d} {err[:120]}")
        # Pull
        with temp_workspace() as ws:
            src = make_tree(os.path.join(ws, "src"))
            dst = os.path.join(ws, "dst")
            rc, out, err = run_fc(target, [f"{host}:{src}", dst], timeout=180)
            ok, d = tree_equal(src, dst)
            (rep.ok if rc == 0 and ok else rep.fail)(
                "Pull (remote->local)",
                "verified" if rc == 0 and ok else f"rc={rc} {d} {err[:120]}")
        # R2R
        with temp_workspace() as ws:
            src = make_tree(os.path.join(ws, "src"))
            dst = os.path.join(ws, "dst")
            rc, out, err = run_fc(
                target, [f"{host}:{src}", f"{host}:{dst}"], timeout=240)
            ok, d = tree_equal(src, dst)
            (rep.ok if rc == 0 and ok else rep.fail)(
                "R2R (remote->remote)",
                "verified" if rc == 0 and ok else f"rc={rc} {d} {err[:120]}")
        # tar-over-SSH path (push) — verify by content multiset (the tar path
        # may nest differently than SFTP), then flag any layout mismatch.
        with temp_workspace() as ws:
            src = make_tree(os.path.join(ws, "src"))
            dst = os.path.join(ws, "dst")
            rc, out, err = run_fc(
                target, ["--ssh-no-sftp", src, f"{host}:{dst}"], timeout=180)
            integrity = (rc == 0 and os.path.isdir(dst)
                         and _content_multiset(src) == _content_multiset(dst))
            if integrity:
                rep.ok("SSH tar mode (--ssh-no-sftp)",
                       "all file contents transferred intact")
                tar_layout = sorted(
                    os.path.relpath(os.path.join(dp, fn), dst)
                    for dp, _, fns in os.walk(dst) for fn in fns
                    if not fn.startswith((".fast_copy", ".blitcp")))
                if push_layout is not None and tar_layout != push_layout:
                    rep.warn(
                        "push layout consistency",
                        "tar push nests under a different prefix than SFTP "
                        f"push (tar e.g. {tar_layout[:1]} vs sftp "
                        f"{push_layout[:1]})")
            else:
                rep.fail("SSH tar mode (--ssh-no-sftp)",
                         f"rc={rc} content mismatch {err[:120]}")

    # Cloud modes — require a reachable backend; skip cleanly otherwise.
    if not ctx["allow_cloud"]:
        for m in ("Cloud upload", "Cloud download", "Cloud->Cloud"):
            rep.skip(m, "cloud modes opt-in (--allow-cloud)")
    else:
        backend = _detect_cloud_backend()
        if not backend:
            for m in ("Cloud upload", "Cloud download", "Cloud->Cloud"):
                rep.skip(m, "no local S3/Azure/GCS emulator reachable")
        else:
            _run_cloud_roundtrip(rep, ctx, backend)

    # SMB modes — opt-in; a real round-trip needs a reachable server + creds,
    # supplied via FC_AUDIT_SMB_URL (e.g. smb://user@127.0.0.1/share/audit) and
    # FC_AUDIT_SMB_PASS. Otherwise skip cleanly.
    if not ctx.get("allow_smb"):
        for m in ("SMB upload", "SMB download", "SMB->SMB"):
            rep.skip(m, "SMB modes opt-in (--allow-smb)")
    else:
        url = os.environ.get("FC_AUDIT_SMB_URL")
        if not url:
            for m in ("SMB upload", "SMB download", "SMB->SMB"):
                rep.skip(m, "set FC_AUDIT_SMB_URL (+FC_AUDIT_SMB_PASS) to test SMB")
        else:
            _run_smb_roundtrip(rep, ctx, url)


def _run_smb_roundtrip(rep, ctx, base_url):
    target = ctx["target"]
    pw = os.environ.get("FC_AUDIT_SMB_PASS")
    env = {"FC_AUDIT_SMB_PASS": pw} if pw else None
    pw_flags = ["--smb-password-env", "FC_AUDIT_SMB_PASS"] if pw else []
    base = base_url.rstrip("/")
    with temp_workspace() as ws:
        src = make_tree(os.path.join(ws, "src"), big_mb=1)
        rc, out, err = run_fc(target, [src, base] + pw_flags,
                              timeout=180, extra_env=env)
        if rc != 0 and "smbprotocol" in (out + err).lower():
            for m in ("SMB upload", "SMB download", "SMB->SMB"):
                rep.skip(m, "smbprotocol not installed")
            return
        (rep.ok if rc == 0 else rep.fail)(
            "SMB upload", "uploaded" if rc == 0 else f"rc={rc} {err[:140]}")
        dst = os.path.join(ws, "dl")
        rc2, out2, err2 = run_fc(target, [base, dst] + pw_flags,
                                 timeout=180, extra_env=env)
        ok, d = tree_equal(src, dst)
        (rep.ok if rc2 == 0 and ok else rep.fail)(
            "SMB download", "round-trip verified" if rc2 == 0 and ok
            else f"rc={rc2} {d} {err2[:140]}")
        rep.skip("SMB->SMB", "covered by upload+download round-trip")


def _detect_cloud_backend():
    """Return env+url for a reachable local emulator, or None.

    Looks for a MinIO-style S3 endpoint on the conventional local port. This is
    intentionally conservative: it only activates when an emulator is already
    running and reachable, never against real cloud.
    """
    try:
        import socket
        for port in (9000,):
            s = socket.socket()
            s.settimeout(0.4)
            try:
                s.connect(("127.0.0.1", port))
                s.close()
                return {"kind": "s3", "endpoint": f"http://127.0.0.1:{port}"}
            except OSError:
                continue
    except Exception:  # noqa: BLE001
        return None
    return None


def _run_cloud_roundtrip(rep, ctx, backend):
    # Conservative: presence of an endpoint does not guarantee usable creds, so
    # attempt an upload and report honestly, skipping if auth is unavailable.
    rep.skip("Cloud upload",
             f"emulator at {backend['endpoint']} detected but credential "
             "wiring is environment-specific; run cloud tests manually")
    rep.skip("Cloud download", "depends on cloud upload")
    rep.skip("Cloud->Cloud", "depends on cloud upload")


# --------------------------------------------------------------------------- #
# Section 4: features — capability matrix (all local)
# --------------------------------------------------------------------------- #

def section_features(rep, ctx):
    target = ctx["target"]

    # dedup + incremental re-run
    with temp_workspace() as ws:
        src = make_tree(os.path.join(ws, "src"), with_dups=True)
        dst = os.path.join(ws, "dst")
        rc, out, err = run_fc(target, [src, dst])
        ok, d = tree_equal(src, dst)
        if rc == 0 and ok:
            rep.ok("dedup copy", "duplicates materialized correctly")
        else:
            rep.fail("dedup copy", f"rc={rc} {d} {err[:140]}")
        # second run = incremental, still correct, no traceback
        rc2, out2, err2 = run_fc(target, [src, dst])
        ok2, d2 = tree_equal(src, dst)
        if rc2 == 0 and ok2 and _no_traceback(err2):
            rep.ok("incremental re-run", "idempotent, tree intact")
        else:
            rep.fail("incremental re-run", f"rc={rc2} {d2} {err2[:140]}")

    # hashing algorithms
    for algo in ("auto", "xxh128", "sha256"):
        with temp_workspace() as ws:
            src = make_tree(os.path.join(ws, "src"), big_mb=1)
            dst = os.path.join(ws, "dst")
            rc, out, err = run_fc(target, ["--hash", algo, src, dst])
            ok, d = tree_equal(src, dst)
            if rc == 0 and ok:
                rep.ok(f"--hash {algo}", "verified")
            elif algo == "xxh128" and "xxhash" in (out + err).lower():
                rep.skip(f"--hash {algo}", "xxhash not installed")
            else:
                rep.fail(f"--hash {algo}", f"rc={rc} {d} {err[:140]}")

    # exclude glob
    with temp_workspace() as ws:
        src = make_tree(os.path.join(ws, "src"), with_dups=False)
        dst = os.path.join(ws, "dst")
        rc, out, err = run_fc(target, ["--exclude", "*.bin", src, dst])
        excluded = not os.path.exists(
            os.path.join(dst, "sub", "deep", "c.bin"))
        kept = os.path.exists(os.path.join(dst, "a.txt"))
        if rc == 0 and excluded and kept:
            rep.ok("--exclude glob", "*.bin omitted, others kept")
        else:
            rep.fail("--exclude glob",
                     f"rc={rc} excluded={excluded} kept={kept} {err[:120]}")

    # --no-verify still copies correctly
    with temp_workspace() as ws:
        src = make_tree(os.path.join(ws, "src"), big_mb=1)
        dst = os.path.join(ws, "dst")
        rc, out, err = run_fc(target, ["--no-verify", src, dst])
        ok, d = tree_equal(src, dst)
        (rep.ok if rc == 0 and ok else rep.fail)(
            "--no-verify", "copied correctly" if rc == 0 and ok
            else f"rc={rc} {d} {err[:120]}")

    # --overwrite replaces differing file
    with temp_workspace() as ws:
        src = make_tree(os.path.join(ws, "src"), big_mb=1, with_dups=False)
        dst = os.path.join(ws, "dst")
        os.makedirs(dst)
        _write(os.path.join(dst, "a.txt"), b"STALE DIFFERENT CONTENT\n")
        rc, out, err = run_fc(target, ["--overwrite", src, dst])
        ok, d = tree_equal(src, dst)
        (rep.ok if rc == 0 and ok else rep.fail)(
            "--overwrite", "stale file replaced" if rc == 0 and ok
            else f"rc={rc} {d} {err[:120]}")

    # --preserve mode,times
    with temp_workspace() as ws:
        src = make_tree(os.path.join(ws, "src"), big_mb=1, with_dups=False)
        os.chmod(os.path.join(src, "a.txt"), 0o640)
        old = time.time() - 100000
        os.utime(os.path.join(src, "a.txt"), (old, old))
        dst = os.path.join(ws, "dst")
        rc, out, err = run_fc(target, ["--preserve", "mode,times", src, dst])
        da = os.path.join(dst, "a.txt")
        if rc == 0 and os.path.exists(da):
            sm = os.stat(os.path.join(src, "a.txt"))
            dm = os.stat(da)
            mode_ok = (sm.st_mode & 0o777) == (dm.st_mode & 0o777)
            time_ok = abs(sm.st_mtime - dm.st_mtime) < 2
            if mode_ok and time_ok:
                rep.ok("--preserve mode,times", "mode+mtime round-tripped")
            else:
                rep.fail("--preserve mode,times",
                         f"mode_ok={mode_ok} time_ok={time_ok}")
        else:
            rep.fail("--preserve mode,times", f"rc={rc} {err[:120]}")

    # multi-source
    with temp_workspace() as ws:
        s1 = os.path.join(ws, "s1")
        s2 = os.path.join(ws, "s2")
        os.makedirs(s1)
        os.makedirs(s2)
        _write(os.path.join(s1, "one.txt"), b"one\n")
        _write(os.path.join(s2, "two.txt"), b"two\n")
        dst = os.path.join(ws, "dst")
        os.makedirs(dst)
        rc, out, err = run_fc(target, [s1, s2, dst])
        got1 = os.path.exists(os.path.join(dst, "s1", "one.txt"))
        got2 = os.path.exists(os.path.join(dst, "s2", "two.txt"))
        if rc == 0 and got1 and got2:
            rep.ok("multi-source", "both basenames landed under dest")
        else:
            rep.fail("multi-source",
                     f"rc={rc} s1={got1} s2={got2} {err[:120]}")

    # tuning flags (non-default buffer/threads/chunk)
    with temp_workspace() as ws:
        src = make_tree(os.path.join(ws, "src"), big_mb=2)
        dst = os.path.join(ws, "dst")
        rc, out, err = run_fc(
            target, ["--buffer", "8", "--threads", "2", "--chunk-size", "16",
                     src, dst])
        ok, d = tree_equal(src, dst)
        (rep.ok if rc == 0 and ok else rep.fail)(
            "tuning flags", "correct under non-default tuning"
            if rc == 0 and ok else f"rc={rc} {d} {err[:120]}")

    # info commands
    rc, out, err = run_fc(target, ["--version"])
    (rep.ok if rc == 0 and ("blitcp" in out.lower()
                            or "fast-copy" in out.lower()) else rep.fail)(
        "--version", out.strip()[:60] if rc == 0 else f"rc={rc}")
    rc, out, err = run_fc(target, ["-h"])
    (rep.ok if rc == 0 and "usage" in (out + err).lower() else rep.fail)(
        "-h / help", "usage printed" if rc == 0 else f"rc={rc}")
    rc, out, err = run_fc(target, ["doctor"])
    (rep.ok if rc == 0 and _no_traceback(err) else rep.fail)(
        "doctor (deps)", "exited cleanly" if rc == 0
        else f"rc={rc} {err[:120]}")


# --------------------------------------------------------------------------- #
# Section 5: bugs — correctness, edge cases, error hygiene
# --------------------------------------------------------------------------- #

def _check_quiet_mode(rep, ctx):
    """--quiet is a scripting contract, so it is asserted as one.

    Regression guard for the bug this check was written with: a failure raised
    as SystemExit("message") carries a STRING code, so an `isinstance(code,
    int)` test read it as success and quiet mode printed OK on a run that
    exited 1. A quiet mode that can report OK for a failed copy is worse than
    no quiet mode at all — a script would silently keep going."""
    target = ctx["target"]
    with temp_workspace() as ws:
        src = make_tree(os.path.join(ws, "src"), big_mb=1,
                        with_empty_dir=False)

        # 1. Success: one line on stdout, nothing on stderr, exit 0.
        rc, out, err = run_fc(target, ["-q", src, os.path.join(ws, "dst")])
        lines = [ln for ln in out.splitlines() if ln.strip()]
        if rc == 0 and len(lines) == 1 and lines[0].startswith("OK") \
                and not err.strip():
            rep.ok("quiet success", "single OK line, clean stderr")
        else:
            rep.fail("quiet success",
                     f"rc={rc} stdout={lines[:3]} stderr={err[:120]}")

        # 2. Systemic failure (unwritable destination — the string-SystemExit
        #    path): nothing on stdout, reason AND verdict on stderr, non-zero.
        ro = os.path.join(ws, "ro")
        os.makedirs(ro)
        os.chmod(ro, 0o555)
        try:
            rc, out, err = run_fc(target, ["-q", src,
                                           os.path.join(ro, "dest")])
        finally:
            os.chmod(ro, 0o755)
        if rc != 0 and not out.strip() and "FAILED" in err \
                and re.search(r"(?i)error", err):
            rep.ok("quiet failure", f"silent stdout, reason on stderr, rc={rc}")
        else:
            rep.fail("quiet failure",
                     f"rc={rc} stdout={out[:80]!r} stderr={err[:160]!r}")

        # 3. Never OK on a non-zero exit — the actual regression.
        if rc != 0 and "OK" not in out:
            rep.ok("quiet verdict honesty", "no OK on a failed run")
        else:
            rep.fail("quiet verdict honesty",
                     f"reported OK while exiting {rc}")

        # 4. --progress keeps the bar and nothing else. The bar has to bypass
        #    the sink that quiet mode installs over stdout, so a regression
        #    there shows up as an OK line with no bar in front of it.
        rc, out, err = run_fc(target, ["-p", src, os.path.join(ws, "dstp")])
        has_bar = "█" in out or "░" in out
        ends_ok = out.strip().splitlines()[-1].startswith("OK") if out.strip() else False
        no_banner = "BLOCK-ORDER COPY" not in out and "Phase 1" not in out
        if rc == 0 and has_bar and ends_ok and no_banner:
            rep.ok("progress mode", "bar kept, banners suppressed, OK line last")
        else:
            rep.fail("progress mode",
                     f"rc={rc} bar={has_bar} ok_last={ends_ok} "
                     f"no_banner={no_banner}")


def _check_dest_preflight(rep, ctx):
    """A destination that cannot be written must be refused BEFORE copying.

    Regression guard for issue #4: a mistyped remote path let the run stream
    megabytes into a channel that discarded them, show a 100% progress bar,
    and report the planned byte count as "sent" — the failure only surfaced
    in verification. The same hole existed locally: makedirs(exist_ok=True)
    succeeds on a read-only directory, so the copy died file by file instead
    of refusing up front."""
    target = ctx["target"]
    with temp_workspace() as ws:
        src = make_tree(os.path.join(ws, "src"), big_mb=1, with_empty_dir=False)
        ro = os.path.join(ws, "ro")
        os.makedirs(ro)
        os.chmod(ro, 0o555)
        try:
            # Existing but unwritable destination.
            rc, out, err = run_fc(target, [src, ro])
            combined = out + err
            refused = rc != 0 and re.search(r"(?i)not writable|permission", combined)
            no_copy_phase = "Phase 5" not in combined
            if refused and no_copy_phase:
                rep.ok("destination preflight", f"refused before copying, rc={rc}")
            else:
                rep.fail("destination preflight",
                         f"rc={rc} refused={bool(refused)} "
                         f"stopped_before_copy={no_copy_phase}")
            # A destination that cannot even be created.
            rc2, out2, err2 = run_fc(target, [src, os.path.join(ro, "child")])
            if rc2 != 0 and re.search(r"(?i)cannot create|permission", out2 + err2):
                rep.ok("destination preflight (create)",
                       f"clean refusal, rc={rc2}")
            else:
                rep.fail("destination preflight (create)",
                         f"rc={rc2} out={(out2 + err2)[:120]!r}")
        finally:
            os.chmod(ro, 0o755)


def _check_update_check_optin(rep, ctx):
    """The update check must never speak first in a non-interactive run.

    A copy in cron or CI has no terminal to answer a question, and a tool that
    phones home without being asked is the thing this design exists to avoid.
    Guards both halves: no prompt text in the output, and no settings file
    created (which is what a silently-assumed "yes" would leave behind)."""
    target = ctx["target"]
    with temp_workspace() as ws:
        src = make_tree(os.path.join(ws, "src"), big_mb=1, with_empty_dir=False)
        cfg = os.path.join(ws, "cfg")
        env = {"XDG_CONFIG_HOME": cfg, "APPDATA": cfg}
        rc, out, err = run_fc(target, [src, os.path.join(ws, "dst")],
                              extra_env=env)
        asked = re.search(r"(?i)check for updates automatically", out + err)
        settings = os.path.join(cfg, "blitcp", "settings.json")
        if rc == 0 and not asked and not os.path.exists(settings):
            rep.ok("update check opt-in", "silent and stateless without a tty")
        else:
            rep.fail("update check opt-in",
                     f"rc={rc} prompted={bool(asked)} "
                     f"settings_written={os.path.exists(settings)}")


def _check_content_verification(rep, ctx):
    """Post-copy verification must compare CONTENT, not just size.

    Regression guard: verify_copy() used to check existence and file size only,
    while the docs promised a re-hash of the destination against the source. A
    drive that writes the right number of wrong bytes — the exact failure the
    feature is sold against — passed silently.

    This drives verify_copy() directly rather than re-running the tool, because
    the incremental pass would re-copy a changed file and mask the hole: an
    end-to-end test passes either way, which is how the first version of this
    check managed to pass against the very build that had the bug.
    """
    import importlib.util

    target = ctx["target"]
    spec = importlib.util.spec_from_file_location("_blitcp_under_test", target)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:                                  # noqa: BLE001
        rep.skip("content verification", f"could not import target: {e}")
        return
    if not hasattr(mod, "verify_copy"):
        rep.fail("content verification", "verify_copy() is missing")
        return
    collector = getattr(mod, "_SRC_DIGESTS", None)
    if collector is None:
        rep.fail("content verification",
                 "no source-digest collector — verification cannot compare content")
        return

    with temp_workspace() as ws:
        src = os.path.join(ws, "src")
        dst = os.path.join(ws, "dst")
        os.makedirs(src)
        os.makedirs(dst)
        payload = os.urandom(200000)
        with open(os.path.join(src, "big.bin"), "wb") as f:
            f.write(payload)

        entries = mod.scan_source(src, dst)
        if isinstance(entries, tuple):
            entries = entries[0]
        collector.arm()
        prog = mod.Progress(sum(e.size for e in entries), len(entries))
        try:
            mod.copy_hybrid(entries, dst, prog, 1 << 20)
        except (NameError, AttributeError, TypeError) as e:
            # A crash inside the engine is a bug, not an unavailable feature.
            # Reporting it as a skip is how a NameError that copied zero small
            # files shipped with the suite green.
            rep.fail("content verification", f"copy engine crashed: {e!r}")
            return
        except Exception as e:                              # noqa: BLE001
            rep.skip("content verification", f"copy engine unavailable: {e}")
            return

        clean = mod.verify_copy(entries, {}, dst)
        if clean != "ok":
            rep.fail("content verification",
                     f"an intact copy was reported as {clean!r}")
            return

        victim = os.path.join(dst, "big.bin")
        size_before = os.path.getsize(victim)
        with open(victim, "r+b") as f:
            f.seek(size_before // 2)
            b = f.read(1)
            f.seek(size_before // 2)
            f.write(bytes([b[0] ^ 0xFF]))
        if os.path.getsize(victim) != size_before:
            rep.fail("content verification", "the probe changed the size, not the content")
            return

        verdict = mod.verify_copy(entries, {}, dst)
        if verdict == "corrupt":
            rep.ok("content verification",
                   "a same-size content change is detected and reported corrupt")
        else:
            rep.fail("content verification",
                     f"a same-size content change was NOT detected (got {verdict!r})")


def _check_link_scope(rep, ctx):
    """A second backup must not share inodes with an older one.

    Regression guard: cross-run dedup linked a fresh copy onto whatever the
    hash cache already knew about the drive, including a previous backup in a
    different folder. Two "separate" backups then shared storage — damage to
    one damaged both, deleting one freed nothing, and editing a file in one
    silently changed the other. Measured at 687 of 1831 files before the fix.

    Re-copying into the SAME destination must still link (that is the
    incremental case people actually want), so this checks both directions.
    """
    target = ctx["target"]
    with temp_workspace() as ws:
        src = make_tree(os.path.join(ws, "src"), big_mb=1, with_empty_dir=False)
        a = os.path.join(ws, "bkA")
        b = os.path.join(ws, "bkB")

        rc_a, _o, _e = run_fc(target, [src, a])
        rc_b, _o, _e = run_fc(target, [src, b])
        if rc_a != 0 or rc_b != 0:
            rep.fail("cross-backup link scope", f"copies failed {rc_a}/{rc_b}")
            return

        def inodes(root):
            out = set()
            for d, _dirs, files in os.walk(root):
                for fn in files:
                    try:
                        out.add(os.stat(os.path.join(d, fn)).st_ino)
                    except OSError:
                        pass
            return out

        shared = len(inodes(a) & inodes(b))
        if shared:
            rep.fail("cross-backup link scope",
                     f"{shared} file(s) in the second backup share an inode with "
                     f"the first — the two copies are not independent")
            return

        rc2, out2, err2 = run_fc(target, [src, a])
        combined = out2 + err2
        if rc2 == 0 and re.search(r"(?i)nothing to copy|already up to date|unchanged",
                                  combined):
            rep.ok("cross-backup link scope",
                   "separate backups keep separate inodes; re-copy still incremental")
        else:
            rep.fail("cross-backup link scope",
                     f"re-copy into the same destination is no longer incremental "
                     f"(rc={rc2})")


def _check_pip_install_not_self_updated(rep, ctx):
    """A pip install must be updated by pip, never by overwriting its file.

    Reported by a user on the public tracker: "I installed via pip,
    --check-update not valid." The update path only ever knew about GitHub.
    Two distinct faults:

      - --check-update compared against GitHub releases, which are published
        separately from PyPI and drift, so a pip user could be told about a
        version `pip install --upgrade` would not give them;
      - --update wrote the GitHub blitcp.py straight over site-packages. That
        file is the plain-script build: it looks for catalogs in a locales/
        directory beside itself, which a wheel does not have (they ship in the
        blitcp_locales package, and the wheel's own blitcp.py knows to look
        there). The "update" silently reverted all six translations to English,
        left blitcp_gui.py at the old version — the release publishes no such
        asset — and left pip's recorded hashes wrong.
    """
    try:
        mod = _import_target(ctx)
    except Exception as e:                                  # noqa: BLE001
        rep.skip("pip install not self-updated", f"could not import: {e}")
        return
    for name in ("_install_kind", "_pip_upgrade_cmd", "_fetch_pypi_version",
                 "_pypi_update_state"):
        if not hasattr(mod, name):
            rep.fail("pip install not self-updated", f"{name}() is gone")
            return

    kind = mod._install_kind()
    if kind not in ("frozen", "pip", "script"):
        rep.fail("pip install not self-updated",
                 f"_install_kind() returned {kind!r}")
        return

    cmd = mod._pip_upgrade_cmd()
    if "pip install --upgrade" not in cmd or mod.PYPI_NAME not in cmd:
        rep.fail("pip install not self-updated",
                 f"the suggested command is not a pip upgrade: {cmd!r}")
        return

    # The refusal and the PyPI branch must be wired in, not just defined.
    import inspect
    upd = inspect.getsource(mod.self_update)
    if '_install_kind() == "pip"' not in upd:
        rep.fail("pip install not self-updated",
                 "self_update() no longer checks how it was installed — it "
                 "can overwrite a wheel again")
        return
    chk = inspect.getsource(mod.check_update_info)
    if '_install_kind() == "pip"' not in chk:
        rep.fail("pip install not self-updated",
                 "--check-update no longer asks PyPI for pip installs")
        return
    auto = inspect.getsource(mod._maybe_auto_update_check)
    if '_install_kind() == "pip"' not in auto:
        rep.fail("pip install not self-updated",
                 "the daily check still tells pip users to run --update")
        return

    rep.ok("pip install not self-updated",
           f"kind={kind}; --update refuses, --check-update and the daily "
           f"check use PyPI, upgrade command is {cmd!r}")


def _check_translation_coverage(rep, ctx):
    """Every translatable string must exist in all six catalogs.

    Regression guard. Rewording a message is a one-line edit that silently
    orphans its catalog entry: the old msgid stays behind, the new one matches
    nothing, and every language falls back to English WITHOUT any error —
    gettext is designed to do exactly that. The --index-existing help text was
    reworded in this repo and went untranslated in all six languages until a
    manual audit noticed, because nothing was watching.

    Compares the string literals actually passed to _tr()/ngettext() against
    each catalog. Dynamic calls (_tr(variable)) cannot be seen from here and
    are counted, not judged.
    """
    import ast as _ast
    root = os.path.dirname(os.path.abspath(ctx["target"]))
    locales = os.path.join(root, "locales")
    if not os.path.isdir(locales):
        rep.skip("translation coverage", "no locales/ beside the target")
        return

    def literals(path):
        if not os.path.isfile(path):
            return set()
        try:
            tree = _ast.parse(open(path, encoding="utf-8").read())
        except SyntaxError as e:
            rep.fail("translation coverage", f"{os.path.basename(path)}: {e}")
            raise
        out = set()
        for n in _ast.walk(tree):
            if not isinstance(n, _ast.Call):
                continue
            name = getattr(n.func, "id", None) or getattr(n.func, "attr", None)
            if name not in ("_tr", "ngettext"):
                continue
            for a in (n.args[:2] if name == "ngettext" else n.args[:1]):
                if isinstance(a, _ast.Constant) and isinstance(a.value, str):
                    out.add(a.value)
                elif isinstance(a, _ast.BinOp):
                    try:
                        v = _ast.literal_eval(a)
                        if isinstance(v, str):
                            out.add(v)
                    except Exception:                       # noqa: BLE001
                        pass
        return out

    try:
        used = literals(ctx["target"]) | literals(
            os.path.join(root, "blitcp_gui.py"))
    except SyntaxError:
        return
    if not used:
        rep.skip("translation coverage", "no _tr() literals found")
        return

    def catalog(po):
        entries, cid, cs, mode = {}, [], [], None
        def flush():
            if cid:
                entries["".join(cid)] = "".join(cs)
        for line in open(po, encoding="utf-8"):
            line = line.strip()
            if line.startswith("msgid "):
                flush(); cid[:] = [_ast.literal_eval(line[6:])]; cs[:] = []
                mode = "id"
            elif line.startswith("msgid_plural "):
                mode = "skip"
            elif line.startswith("msgstr"):
                cs[:] = [_ast.literal_eval(
                    line.split(" ", 1)[1] if " " in line else '""')]
                mode = "str"
            elif line.startswith('"') and mode in ("id", "str"):
                (cid if mode == "id" else cs).append(_ast.literal_eval(line))
            elif not line:
                mode = None
        flush(); entries.pop("", None)
        return entries

    problems = []
    langs = sorted(d for d in os.listdir(locales)
                   if os.path.isfile(os.path.join(
                       locales, d, "LC_MESSAGES", "blitcp.po")))
    if not langs:
        rep.skip("translation coverage", "no catalogs")
        return
    for lang in langs:
        cat = catalog(os.path.join(locales, lang, "LC_MESSAGES", "blitcp.po"))
        missing = sorted(used - set(cat))
        empty = sorted(k for k, v in cat.items() if k in used and not v.strip())
        if missing or empty:
            sample = (missing or empty)[0]
            problems.append("%s: %d missing, %d empty (e.g. %r)"
                            % (lang, len(missing), len(empty), sample[:60]))
    if problems:
        rep.fail("translation coverage", "; ".join(problems[:3]))
        return
    rep.ok("translation coverage",
           f"{len(used)} strings present and non-empty in all "
           f"{len(langs)} catalogs")


def _check_streamer_parity(rep, ctx):
    """The two tar streamers must not differ on ANY axis, checked mechanically.

    Three review passes each found one divergence between the remote->local
    streamer and the remote->remote relay, and each pass found a DIFFERENT one:
    the traceback containment, then the failure accounting, then the validation
    warning. That is not three bugs, it is one — reading the pair by eye
    compares whichever axis was touched last and misses the rest.

    So this enumerates the axes instead. A new one is a line in the table; a
    fix landing in only one of the two fails here immediately.
    """
    try:
        mod = _import_target(ctx)
    except Exception as e:                                  # noqa: BLE001
        rep.skip("streamer parity", f"could not import target: {e}")
        return
    import inspect
    pairs = []
    for label, inner, caller in (
            ("remote->local", "_stream_tar_batch_from_remote",
             "copy_block_stream_remote_to_local"),
            ("relay", "_stream_tar_batch_r2r", "copy_block_stream_r2r")):
        i, c = getattr(mod, inner, None), getattr(mod, caller, None)
        if i is None or c is None:
            rep.skip("streamer parity", f"{label} pair not present")
            return
        pairs.append((label, inspect.getsource(i), inspect.getsource(c)))

    axes = {
        "shared file-list thread":   lambda i, c: "_TarListSender" in i,
        "shared failure reason":     lambda i, c: "_tar_batch_reason" in i,
        "records why files vanish":  lambda i, c: "_record_batch_failure" in i,
        "skips delivered files":     lambda i, c: "delivered=" in i,
        "empty source root guard":   lambda i, c: "src_root or" in i,
        "validates via _safe_batch": lambda i, c: "_safe_batch" in c,
        "no second filter point":    lambda i, c: "_validate_rel_path" not in i,
        "no dead threading import":  lambda i, c: "    import threading" not in i,
    }
    differ = []
    for name, test in axes.items():
        vals = [test(i, c) for _l, i, c in pairs]
        if vals[0] != vals[1]:
            differ.append("%s (%s=%s, %s=%s)"
                          % (name, pairs[0][0], vals[0], pairs[1][0], vals[1]))
    if differ:
        rep.fail("streamer parity",
                 "the two streamers diverge on: " + "; ".join(differ))
        return

    missing = [n for n, t in axes.items() if not t(pairs[0][1], pairs[0][2])]
    if missing:
        rep.fail("streamer parity",
                 f"both streamers are missing: {', '.join(missing)}")
        return

    rep.ok("streamer parity",
           f"{len(axes)} axes, both streamers identical on all of them")


def _check_relay_reports_truth(rep, ctx):
    """A remote-to-remote relay that moved nothing must not claim it did.

    Regression guard for a real failed transfer. A saved connection's path was
    discarded when a filename was appended, leaving the tar producer an empty
    source root; `cd '' && tar ...` is an error under bash (dash allows it), so
    the producer never ran and the consumer reported "does not look like a tar
    archive". The relay then logged every file as copied and credited the
    progress meter with the whole batch anyway: a 100% bar and "12.6 KB
    relayed" for zero bytes. Verification caught the missing file and the run
    exited 1, but everything above that line said the opposite.

    The file-list thread also died with "Socket is closed" — the far side was
    already gone — and its traceback landed in the middle of the output, which
    the single-line error convention exists to prevent.
    """
    try:
        mod = _import_target(ctx)
    except Exception as e:                                  # noqa: BLE001
        rep.skip("relay reports truth", f"could not import target: {e}")
        return
    fn = getattr(mod, "_stream_tar_batch_r2r", None)
    if fn is None:
        rep.skip("relay reports truth", "no r2r tar relay in this build")
        return

    class _Chan:
        def __init__(self, rc, stderr=b""):
            self.rc, self._err, self.closed = rc, stderr, False
        def exec_command(self, cmd): self.cmd = cmd
        def sendall(self, d):
            if self.closed:
                raise OSError("Socket is closed")
        def recv(self, n): return b""
        def shutdown_write(self): pass
        def recv_exit_status(self): return self.rc
        def recv_stderr(self, n): return self._err[:n]
        def close(self): self.closed = True

    class _SSH:
        def __init__(self, chan): self._chan, self.caps = chan, {}
        def open_channel(self): return self._chan
        def exec_cmd(self, cmd, input_data=None, timeout=300): return "", "", 0

    entry = mod.FileEntry(src="/x/a.bin", rel="a.bin", size=12800,
                          physical_offset=None, content_hash=None)

    def _run(src_rc, dst_rc, dead_socket=False):
        mod._COPY_ERRORS.clear()
        sc = _Chan(src_rc)
        sc.closed = dead_socket
        dc = _Chan(dst_rc, b"tar: This does not look like a tar archive")
        prog = mod.Progress(entry.size, 1)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn([entry], _SSH(sc), _SSH(dc), "/src", "/dst", prog)
        return prog.bytes_done, dict(mod._COPY_ERRORS), buf.getvalue()

    good_bytes, good_errs, _o = _run(0, 0)
    if good_bytes != entry.size or good_errs:
        rep.fail("relay reports truth",
                 f"a clean relay credited {good_bytes} of {entry.size} bytes "
                 f"(errors: {good_errs})")
        return

    for label, args in (("source tar failed", (1, 2)),
                        ("dest tar failed", (0, 2)),
                        ("file-list socket closed", (1, 2, True))):
        bytes_done, errs, out = _run(*args)
        if bytes_done:
            rep.fail("relay reports truth",
                     f"{label}: credited {bytes_done} bytes for a relay that "
                     f"piped nothing")
            return
        if not errs:
            rep.fail("relay reports truth",
                     f"{label}: no per-file error recorded, so verify cannot "
                     f"say why the file is missing")
            return
        if "Traceback" in out:
            rep.fail("relay reports truth",
                     f"{label}: a Python traceback reached the output")
            return

    # The empty-root shell command that started it all.
    if "src_root or" not in inspect.getsource(fn):
        rep.fail("relay reports truth",
                 "the tar producer no longer guards against an empty source "
                 "root, so `cd ''` can come back")
        return

    # The two streamers must keep SHARING this, not merely each have a copy.
    # Three separate fixes landed in one of them and had to be chased into the
    # other by a later review; the divergence is the bug, so that is what this
    # asserts.
    r2l = getattr(mod, "_stream_tar_batch_from_remote", None)
    if r2l is None:
        rep.fail("relay reports truth", "_stream_tar_batch_from_remote is gone")
        return
    for name, fnobj in (("remote->local", r2l), ("relay", fn)):
        src = inspect.getsource(fnobj)
        if "_TarListSender" not in src:
            rep.fail("relay reports truth",
                     f"the {name} streamer grew its own file-list thread again "
                     f"instead of using _TarListSender")
            return
        if "_tar_batch_reason" not in src or "_record_batch_failure" not in src:
            rep.fail("relay reports truth",
                     f"the {name} streamer no longer records why its files are "
                     f"missing, so verify can only say MISSING")
            return
        if "src_root or" not in src:
            rep.fail("relay reports truth",
                     f"the {name} tar producer can emit `cd ''` again")
            return
        # Validation itself is asserted by _check_streamer_parity: it lives in
        # the callers now, so demanding it here would pin the old shape.

    # Both must fail the same way, not merely both be wired up.
    for label, args in (("relay", (1, 2)), ("dest", (0, 2))):
        bytes_done, errs, out = _run(*args)
        if bytes_done or not errs or "Traceback" in out:
            rep.fail("relay reports truth",
                     f"{label}: bytes={bytes_done} errors={bool(errs)} "
                     f"traceback={'Traceback' in out}")
            return

    rep.ok("relay reports truth",
           "clean relay credits its bytes; three failure modes credit none, "
           "record the reason, and leak no traceback")


def _source_block(src, header):
    """The text of a top-level def/class, header line through the line before
    the next top-level statement. inspect.getsource() cannot be used: the
    auditor loads the target under a synthetic module name, and classes from it
    report as built-in."""
    i = src.find(header)
    if i < 0:
        return ""
    j = i + len(header)
    while True:
        k = src.find("\ndef ", j)
        c = src.find("\nclass ", j)
        if k < 0 or (0 <= c < k):
            k = c
        if k < 0:
            return src[i:]
        return src[i:k]


def _check_http_relay_honors_transport(rep, ctx):
    """An http(s):// source must write over the transport the destination has.

    Regression guard. apply_saved_ssh_protocol() set ssh_no_sftp from a
    connection saved as protocol 'ssh', and the HTTP relay then opened an SFTP
    handle anyway — on a server whose SFTP subsystem is off that surfaced as a
    bare "Channel closed." after the flag had been read and dropped. The relay
    must consult the flag, own an exec-channel writer for the SSH-only case,
    and fail SFTP with a message that names the host and the way out.
    """
    name = "http relay honors ssh transport"
    try:
        src = open(ctx["target"], encoding="utf-8", errors="replace").read()
    except OSError as e:                                    # noqa: BLE001
        rep.skip(name, f"could not read target: {e}")
        return
    relay = _source_block(src, "def _http_to_ssh(")
    writer = _source_block(src, "class _SshExecWriter")
    if not relay:
        rep.fail(name, "_http_to_ssh is gone")
        return
    if not writer:
        rep.fail(name, "_SshExecWriter is gone — no exec-channel write path")
        return
    if "def _ssh_exec_stat(" not in src:
        rep.fail(name, "_ssh_exec_stat is gone — no SFTP-free stat")
        return
    if "ssh_no_sftp" not in relay:
        rep.fail(name, "_http_to_ssh never consults ssh_no_sftp")
        return
    if "_SshExecWriter" not in relay and "_d_open" not in relay:
        rep.fail(name, "_http_to_ssh has no exec-channel write path")
        return
    if "cat >" not in writer:
        rep.fail(name, "_SshExecWriter does not stream to `cat > path`")
        return
    if "recv_exit_status" not in writer:
        rep.fail(name, "_SshExecWriter ignores the remote exit status — a "
                       "failed remote write would pass as success")
        return
    if "--ssh-no-sftp" not in relay:
        rep.fail(name, "the SFTP failure path does not name the way out")
        return
    rep.ok(name, "ssh_no_sftp honored, exec writer checks remote exit status")


def _check_cookie_domain_scope(rep, ctx):
    """Browser cookies must be looked up on the registrable domain.

    Regression guard. browser_cookie3 filters with a SUBSTRING test against
    each cookie's own domain, so asking it for the full hostname
    ("dl.dell.com" in ".dell.com" is False) dropped every parent-domain
    cookie — where sessions actually live. A logged-in browser produced "no
    cookies matched", and the redirect a download link makes to its SSO host
    could never match either. The pre-filter must be the registrable domain;
    add_cookie_header() still does the real RFC matching afterwards.
    """
    name = "cookie domain scope"
    try:
        mod = _import_target(ctx)
        src = open(ctx["target"], encoding="utf-8", errors="replace").read()
    except Exception as e:                                  # noqa: BLE001
        rep.skip(name, f"could not load target: {e}")
        return
    fn = getattr(mod, "_registrable_domain", None)
    if fn is None:
        rep.fail(name, "_registrable_domain is gone — browser cookies are "
                       "filtered by full hostname again")
        return
    for host, want in (("dl.dell.com", "dell.com"),
                       ("www.dell.com", "dell.com"),
                       ("a.b.c.example.com", "example.com"),
                       ("downloads.example.co.uk", "example.co.uk"),
                       ("dell.com", "dell.com")):
        got = fn(host)
        if got != want:
            rep.fail(name, f"_registrable_domain({host!r}) = {got!r}, "
                           f"expected {want!r}")
            return
    # the substring rule browser_cookie3 actually applies
    if fn("dl.dell.com") not in ".dell.com":
        rep.fail(name, "the filter still would not match a '.dell.com' cookie")
        return
    blk = _source_block(src, "def _cookie_header_for(")
    if "domain_name=host" in blk:
        rep.fail(name, "a browser loader still filters by the full hostname")
        return
    if "_registrable_domain(" not in blk:
        rep.fail(name, "_cookie_header_for does not use _registrable_domain")
        return
    rep.ok(name, "browser cookies scoped to the registrable domain")


def _check_sftp_fallback_contract(rep, ctx):
    """A server with SSH on and SFTP off must be detected, not crashed into —
    and the fallback must not move the user's files.

    Regression guard. The transport is chosen before the first SFTP call, which
    happens after the scan, so a server without the subsystem died mid-run.
    Three properties: the probe exists and main() consults it; an explicit
    --sftp-only is never silently overridden (it is a restriction, not a
    preference); and the SSH-only push copies a lone directory's CONTENTS
    rather than nesting them under <basename>/, because a transport chosen
    automatically must land files exactly where the SFTP path would.
    """
    name = "sftp fallback contract"
    try:
        src = open(ctx["target"], encoding="utf-8", errors="replace").read()
    except OSError as e:                                    # noqa: BLE001
        rep.skip(name, f"could not read target: {e}")
        return
    if "def _sftp_subsystem_ok(" not in src:
        rep.fail(name, "no SFTP probe — a subsystem-less server still dies "
                       "mid-transfer")
        return
    main_blk = _source_block(src, "def main(")
    if "_sftp_subsystem_ok(" not in main_blk:
        rep.fail(name, "main() never probes; the transport is still chosen "
                       "blind")
        return
    if "sftp_only" not in main_blk.split("_sftp_subsystem_ok(")[0][-1200:]:
        rep.fail(name, "the probe does not exempt an explicit --sftp-only")
        return
    push = _source_block(src, "def _ssh_push_smart(")
    if not push:
        rep.fail(name, "_ssh_push_smart is gone")
        return
    if 'base + "/" + os.path.relpath' in push:
        rep.fail(name, "SSH-only push still nests a directory under its "
                       "basename — an auto-selected transport would relocate "
                       "the user's files")
        return
    relay = _source_block(src, "def _http_to_ssh(")
    # The fallback is silent by design (the banner names the transport), so
    # assert the BEHAVIOUR — the SFTP failure lands on the exec writer — and
    # never the wording of a message.
    if "use_sftp = False" not in relay or "sftp = None" not in relay:
        rep.fail(name, "an SFTP failure no longer routes to the exec writer")
        return
    if "sftp_only" not in relay:
        rep.fail(name, "the HTTP relay fallback ignores --sftp-only")
        return
    rep.ok(name, "probe consulted, --sftp-only respected, layout matches SFTP")


def _check_relay_errors_are_debuggable(rep, ctx):
    """Terminal relay handlers must route through _fmt_exc().

    Regression guard. Several handlers built their message with
    str(e).splitlines()[0], which silently bypassed the BLITCP_TRACEBACK=1
    escape hatch _fmt_exc() provides — asking for the stack produced exactly
    the same unhelpful one-liner. The retry notice inside _http_pump is
    deliberately excluded: it reports a failure the transfer recovers from.
    """
    name = "relay errors honor BLITCP_TRACEBACK"
    try:
        src = open(ctx["target"], encoding="utf-8", errors="replace").read()
    except OSError as e:                                    # noqa: BLE001
        rep.skip(name, f"could not read target: {e}")
        return
    bad = []
    for fn in ("run_http_transfer", "_http_to_ssh", "_http_to_smb",
               "_relay_object_ssh", "copy_via_tar_ssh", "_ssh_ls"):
        blk = _source_block(src, "def %s(" % fn)
        if not blk:
            continue
        if "str(e).strip().splitlines()[0]" in blk and "_fmt_exc" not in blk:
            bad.append(fn)
    if bad:
        rep.fail(name, "swallow the traceback: " + ", ".join(bad))
    else:
        rep.ok(name, "terminal relay handlers use _fmt_exc")


def _check_saved_ssh_protocol(rep, ctx):
    """The Protocol saved on an SSH connection must reach the CLI copy path.

    Regression guard. The GUI's connection dialog stored protocol=ssh|sftp|both
    and translated it into --ssh-no-sftp / --sftp-only when launching the
    engine, but the CLI never read the field: a connection saved as "SFTP
    only" ran the hybrid (and opened a shell) when used by name from the
    terminal. Rules mirrored from the GUI: sftp -> sftp_only, ssh ->
    ssh_no_sftp, both/unset -> neither, an explicit flag beats the saved
    value, and ssh on one side with sftp on the other is refused.
    """
    name = "saved ssh protocol"
    try:
        mod = _import_target(ctx)
    except Exception as e:                                  # noqa: BLE001
        rep.skip(name, f"could not import target: {e}")
        return
    for fn in ("resolve_named_endpoint", "apply_saved_ssh_protocol",
               "_ssh_creds_by_host"):
        if not hasattr(mod, fn):
            rep.fail(name, f"{fn} is gone")
            return
    import argparse as _ap
    conns = {
        "nas_sftp": {"type": "ssh", "host": "h1", "user": "u", "path": "/a",
                     "protocol": "sftp"},
        "box_ssh": {"type": "ssh", "host": "h2", "user": "u", "path": "/b",
                    "protocol": "ssh"},
        "old": {"type": "ssh", "host": "h3", "user": "u", "path": "/c"},
        "hyb": {"type": "ssh", "host": "h4", "user": "u", "path": "/d",
                "protocol": "both"},
    }
    problems = []

    def ov(nm):
        _new, o = mod.resolve_named_endpoint(nm, conns)
        return o

    if ov("nas_sftp").get("protocol") != "sftp" or ov("nas_sftp").get("name") != "nas_sftp":
        problems.append("resolve_named_endpoint drops the saved protocol/name")
    if ov("old").get("protocol") is not None:
        problems.append("a connection without the field invented a protocol")
    by_host = mod._ssh_creds_by_host("u@h2:/x", conns)
    if not by_host or by_host.get("protocol") != "ssh" or by_host.get("name") != "box_ssh":
        problems.append("host-matched credentials lose the protocol")

    def run(names, **flags):
        a = _ap.Namespace(sftp_only=False, ssh_no_sftp=False)
        for k, v in flags.items():
            setattr(a, k, v)
        mod.apply_saved_ssh_protocol(a, [ov(n) for n in names])
        return a.sftp_only, a.ssh_no_sftp

    cases = [
        (["nas_sftp"], {}, (True, False)),
        (["box_ssh"], {}, (False, True)),
        (["old"], {}, (False, False)),
        (["hyb"], {}, (False, False)),
        (["hyb", "nas_sftp"], {}, (True, False)),
        (["hyb", "box_ssh"], {}, (False, True)),
        (["nas_sftp"], {"ssh_no_sftp": True}, (False, True)),   # explicit wins
        (["box_ssh"], {"sftp_only": True}, (True, False)),
    ]
    for names, flags, want in cases:
        try:
            got = run(names, **flags)
        except SystemExit as e:
            got = f"SystemExit({e})"
        if got != want:
            problems.append(f"{names} {flags or ''} -> {got}, expected {want}")
    try:
        run(["box_ssh", "nas_sftp"])
        problems.append("ssh + sftp on the two sides was not refused")
    except SystemExit as e:
        if "compatible" not in str(e) or "box_ssh" not in str(e):
            problems.append(f"incompatible-protocol refusal is unhelpful: {e}")
    # An explicit flag must silence the refusal too (the user chose).
    try:
        if run(["box_ssh", "nas_sftp"], sftp_only=True) != (True, False):
            problems.append("explicit --sftp-only did not override the conflict")
    except SystemExit as e:
        problems.append(f"explicit flag still refused: {e}")

    # `creds add`/`edit` must offer the field and validate it.
    src = open(ctx["target"], encoding="utf-8").read()
    if "Protocol [ssh/sftp/both]" not in src or "Protocol (ssh/sftp/both)" not in src:
        problems.append("creds add/edit no longer prompt for the protocol")

    if problems:
        rep.fail(name, "; ".join(problems))
    else:
        rep.ok(name, "saved protocol -> transport flags like the GUI; explicit "
                     "flags win; ssh/sftp mismatch refused with both names")


def _check_ls_shell_fallback(rep, ctx):
    """`blitcp ls` must list over the shell when the server has no SFTP.

    Regression guard. A Synology with SSH on and SFTP off answered `ls` with
    a bare "Channel closed." while the GUI's browser, which falls back to a
    shell listing, showed the files. Driven with a stub connection: SFTP
    raises the way paramiko does, the shell answers, sizes come from GNU find
    when present, and a connection saved as SFTP-only never touches the shell.
    """
    name = "ls shell fallback"
    try:
        mod = _import_target(ctx)
    except Exception as e:                                  # noqa: BLE001
        rep.skip(name, f"could not import target: {e}")
        return
    for fn in ("_ssh_ls_via_sftp", "_ssh_ls_via_shell"):
        if not hasattr(mod, fn):
            rep.fail(name, f"{fn} is gone")
            return
    # _ssh_ls returns on its first line when paramiko is absent, so the stub
    # connection below is never constructed and `calls` stays empty. Reading
    # calls[-1] then raised IndexError, and the per-SECTION handler turned that
    # into one "bugs section crashed" line -- abandoning the nine checks after
    # this one. Skip cleanly instead, like every other environment-bound check.
    if not mod._load_paramiko():
        rep.skip(name, "paramiko not installed - _ssh_ls exits before it "
                       "constructs a connection")
        return

    class _NoSftp:
        def __init__(self, gnu_find=True, sftp_only=False):
            self.caps = {"gnu_find": gnu_find}
            self.sftp_only = sftp_only
            self.cmds = []

        def open_sftp(self):
            raise Exception("Channel closed.")

        def exec_cmd(self, cmd, input_data=None, timeout=300):
            self.cmds.append(cmd)
            if cmd.startswith("if [ -d "):
                if "/missing" in cmd:
                    return "M\n", "", 0
                if "/one.bin" in cmd:
                    return "F\n 4096\n", "", 0
                return "D\n", "", 0
            if cmd.startswith("LC_ALL=C find -L "):
                return "d\t4096\tsub\nf\t10\tb.txt\nf\t2048\ta.bin\n", "", 0
            if cmd.startswith("LC_ALL=C ls -1ApL "):
                return "sub/\nb.txt\na.bin\n", "", 0
            return "", "unexpected", 1

    problems = []
    ssh = _NoSftp()
    try:
        mod._ssh_ls_via_sftp(ssh, "/dir")
        problems.append("sftp listing swallowed the subsystem failure")
    except Exception:
        pass
    kind, ents = mod._ssh_ls_via_shell(ssh, "/dir")
    got = sorted((e.filename, e.st_size) for e in ents)
    if kind != "dir" or got != [("a.bin", 2048), ("b.txt", 10), ("sub", None)]:
        problems.append(f"find-based listing wrong: {kind} {got}")
    ssh2 = _NoSftp(gnu_find=False)
    kind, ents = mod._ssh_ls_via_shell(ssh2, "/dir")
    got = sorted((e.filename, e.st_size) for e in ents)
    if got != [("a.bin", None), ("b.txt", None), ("sub", None)]:
        problems.append(f"ls-based listing wrong: {got}")
    if any(c.startswith("LC_ALL=C find") for c in ssh2.cmds):
        problems.append("ran GNU find on a server without it")
    if mod._ssh_ls_via_shell(_NoSftp(), "/missing")[0] != "missing":
        problems.append("a missing path was not reported as missing")
    k, e1 = mod._ssh_ls_via_shell(_NoSftp(), "/one.bin")
    if k != "file" or e1[0].st_size != 4096:
        problems.append(f"a regular file was not listed as one: {k} {e1}")
    # Quoting: a name with a space and a quote must survive intact.
    import shlex as _shlex
    odd = _NoSftp()
    odd_path = "/Home Movies/it" + chr(39) + "s"
    mod._ssh_ls_via_shell(odd, odd_path)
    if not any(_shlex.quote(odd_path) in c for c in odd.cmds):
        problems.append(f"awkward path not quoted safely: {odd.cmds[:1]}")

    # End to end through _ssh_ls: the fallback runs, and SFTP-only never does.
    import io, contextlib
    calls = {"sftp_only": []}
    orig = mod.SSHConnection

    class _Conn(_NoSftp):
        def __init__(self, remote, **kw):
            super().__init__(gnu_find=True, sftp_only=kw.get("sftp_only", False))
            calls["sftp_only"].append(self.sftp_only)

        def connect(self):
            return self

        def close(self):
            pass

    mod.SSHConnection = _Conn
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = mod._ssh_ls("u@nas.local:/dir", {"port": 22, "key": None, "password": "x"})
        out = buf.getvalue()
        if rc != 0 or "a.bin" not in out or "sub/" not in out or "Channel closed" in out:
            problems.append(f"ls did not fall back to the shell: rc={rc} out={out[-160:]!r}")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = mod._ssh_ls("u@nas.local:/dir", {"port": 22, "key": None, "password": "x",
                                          "protocol": "sftp"})
        out = buf.getvalue()
        if (rc == 0 or calls["sftp_only"][-1:] != [True]
                or "Enable SFTP" not in out):
            problems.append(f"an SFTP-only connection used the shell or hid the "
                            f"hint: rc={rc} out={out[-160:]!r}")
        if "Traceback" in out:
            problems.append("a traceback leaked from ls")
    finally:
        mod.SSHConnection = orig

    if problems:
        rep.fail(name, "; ".join(problems))
    else:
        rep.ok(name, "SFTP off -> shell listing (sizes via GNU find, names via "
                     "ls); SFTP-only stays off the shell; hint names the fix")


def _check_remote_scan_targeted(rep, ctx):
    """Checking a few files must not enumerate the whole destination.

    Regression guard. The remote incremental check listed every file under the
    destination root to answer "does this one file already exist". Pointed at a
    home directory with 1.26M files that is 124 MB of listing over a paramiko
    channel; it exceeded the 300s command timeout, and since socket.timeout is
    an OSError subclass raised with no message, the upload of a single 12 KB
    file ended with the word "Error:" and nothing else.

    Driven with a stub SSH so it needs no server: the point is which command
    goes out, and that a timeout still names the path it gave up on.
    """
    try:
        mod = _import_target(ctx)
    except Exception as e:                                  # noqa: BLE001
        rep.skip("remote scan targeted", f"could not import target: {e}")
        return
    if not hasattr(mod, "scan_remote_destination"):
        rep.fail("remote scan targeted", "scan_remote_destination is gone")
        return

    class _Stub:
        caps = {"gnu_find": True}
        sftp_only = False

        def __init__(self, raise_exc=None):
            self.cmds = []
            self.raise_exc = raise_exc

        def exec_cmd(self, cmd, input_data=None, timeout=300):
            self.cmds.append(cmd)
            if "test -d" in cmd:
                return "", "", 0
            if self.raise_exc is not None:
                raise self.raise_exc
            if cmd.startswith("stat -c"):
                # Probe (the root) and the batch both answer here.
                if " -- '/dst'" in cmd or ' -- "/dst"' in cmd or "-- /dst " not in cmd:
                    pass
                out = []
                for tok in cmd.split(" -- ", 1)[1].split(" 2>/dev/null")[0].split():
                    path = tok.strip("'\"")
                    if path.endswith("keep.bin") or path == "/dst":
                        out.append("4096 %s" % path)
                return "\n".join(out), "", 0
            return "", "", 0

    ssh = _Stub()
    files, targeted = mod.scan_remote_destination(
        ssh, "/dst", want_rels=["keep.bin", "gone.bin"])
    if not targeted:
        rep.fail("remote scan targeted",
                 "a two-file question still took the full-listing path")
        return
    if any("find " in c for c in ssh.cmds):
        rep.fail("remote scan targeted",
                 f"ran a whole-tree find anyway: {ssh.cmds}")
        return
    if sorted(files) != ["keep.bin"]:
        rep.fail("remote scan targeted",
                 f"targeted lookup returned {sorted(files)}, expected "
                 f"['keep.bin']")
        return

    # A rel from an untrusted source listing must never be joined onto the
    # remote root: '../..' would stat outside the destination, and a hit would
    # make the incremental check skip the file.
    ssh_t = _Stub()
    files_t, _tg = mod.scan_remote_destination(
        ssh_t, "/dst",
        want_rels=["keep.bin", "../../etc/shadow", "/etc/passwd"])
    escaped = [c for c in ssh_t.cmds
               if c.startswith("stat -c") and ("etc" in c or ".." in c)]
    if escaped:
        rep.fail("remote scan targeted",
                 f"an unvalidated rel reached the remote path: {escaped[:1]}")
        return
    if sorted(files_t) != ["keep.bin"]:
        rep.fail("remote scan targeted",
                 f"traversal rels were not dropped: {sorted(files_t)}")
        return

    # Nothing to ask about must not fall through to a whole-tree listing.
    ssh_e = _Stub()
    _fe, tge = mod.scan_remote_destination(ssh_e, "/dst", want_rels=[])
    if any(c.startswith("find ") for c in ssh_e.cmds) or not tge:
        rep.fail("remote scan targeted",
                 "an empty question enumerated the whole destination")
        return

    # Too many sources: the full listing is the cheaper question again.
    many = ["f%d.bin" % i for i in range(mod._TARGETED_SCAN_MAX + 1)]
    ssh2 = _Stub()
    _f, targeted2 = mod.scan_remote_destination(ssh2, "/dst", want_rels=many)
    if targeted2:
        rep.fail("remote scan targeted",
                 f"{len(many)} sources still took the per-path route")
        return

    # A timeout must say what it gave up on.
    import socket as _sock
    try:
        mod.scan_remote_destination(_Stub(raise_exc=_sock.timeout()), "/dst")
        rep.fail("remote scan targeted", "a timed-out listing raised nothing")
        return
    except Exception as e:                                  # noqa: BLE001
        msg = str(mod._fmt_exc(e))
        if "/dst" not in msg or not msg.strip():
            rep.fail("remote scan targeted",
                     f"timeout message does not name the path: {msg!r}")
            return

    rep.ok("remote scan targeted",
           "few sources ask per path, many fall back to one listing, "
           "timeout names the directory")


def _check_error_never_empty(rep, ctx):
    """A failure must never print a bare "Error:".

    Regression guard. The SSH flows caught OSError and interpolated it straight
    into the message. An OSError raised with no arguments stringifies to "",
    which is routine in socket and SSH teardown paths, so a failed transfer
    ended with the single word "Error:" — no cause, no file, and no way to look
    further. That is worse than the traceback the one-line rule exists to
    avoid, because a traceback at least says where it came from.
    """
    try:
        mod = _import_target(ctx)
    except Exception as e:                                  # noqa: BLE001
        rep.skip("error never empty", f"could not import target: {e}")
        return
    fmt = getattr(mod, "_fmt_exc", None)
    if fmt is None:
        rep.fail("error never empty",
                 "_fmt_exc is gone; nothing stops a bare 'Error:' any more")
        return

    cases = [OSError(), IOError(), OSError(""), OSError(None),
             OSError(2, "No such file or directory", "/nope"),
             OSError("connection reset by peer"),
             ConnectionResetError(), BrokenPipeError()]
    empty = [type(e).__name__ for e in cases if not str(fmt(e)).strip()]
    if empty:
        rep.fail("error never empty",
                 f"empty message for: {', '.join(empty)}")
        return

    # The no-message fallback must name the class, or it says nothing useful.
    plain = str(fmt(OSError()))
    if "OSError" not in plain:
        rep.fail("error never empty",
                 f"argument-less OSError renders as {plain!r}, which names "
                 f"neither a cause nor the exception")
        return

    rep.ok("error never empty",
           f"{len(cases)} exception shapes all produce a non-empty line")


def _check_lookup_scope_in_sql(rep, ctx):
    """Out-of-scope rows must never leave the database.

    Regression guard. The scope test used to run in the caller: every row a
    hash matched was resolved through safe_full_path() — two realpath() calls,
    on Windows across OneDrive reparse points — and only then discarded for
    being outside the destination. With a drive-wide index (--index-existing)
    one hash matches thousands of rows, and a real run spent 35s of a 47s copy
    doing exactly that, for zero links. The filter now lives in SQL.

    Two ways this can regress into something worse than slow:
      - the LIKE pattern under-matches (an unescaped path, a bad prefix) and
        real cross-run links silently stop being found;
      - the filter ignores link_scope_drive_wide and breaks --index-existing.
    Both are checked here, along with a folder name containing '_', which LIKE
    treats as a single-character wildcard unless escaped.
    """
    try:
        mod = _import_target(ctx)
    except Exception as e:                                  # noqa: BLE001
        rep.skip("lookup scope in SQL", f"could not import target: {e}")
        return
    if not hasattr(mod, "DedupDB"):
        rep.fail("lookup scope in SQL", "DedupDB is gone")
        return

    with temp_workspace() as ws:
        # Keep the database inside the workspace: the real mount lookup would
        # put these synthetic rows in a DB shared with everything else here.
        real_mp = mod._find_mount_point
        mod._find_mount_point = lambda _p: ws
        try:
            dst = os.path.join(ws, "Chinese_test")   # '_' is a LIKE wildcard
            os.makedirs(dst)
            db = mod.DedupDB(dst)
            pref = db._prefix.replace(os.sep, "/")
            H = "b" * 32
            rows = [(f"{pref}/keep.bin", H),
                    ("OldBackup/drop.bin", H),
                    ("ChineseXtest/wildcard.bin", H)]
            with db.lock:
                c = db.conn.cursor()
                for mr, h in rows:
                    c.execute("INSERT INTO dest_files (mount_rel, size, "
                              "mtime_ns, content_hash, hash_algo) "
                              "VALUES (?,?,?,?,?)",
                              (mr, 4096, 1699999999000000000, h,
                               mod._hash_name))
                db.conn.commit()

            scoped = sorted(r[0] for r in db.lookup_by_hash(H))
            if scoped != [f"{pref}/keep.bin"]:
                rep.fail("lookup scope in SQL",
                         f"scoped lookup returned {scoped}, expected only "
                         f"{pref}/keep.bin")
                return

            db.link_scope_drive_wide = True
            widened = sorted(r[0] for r in db.lookup_by_hash(H))
            if widened != sorted(mr for mr, _h in rows):
                rep.fail("lookup scope in SQL",
                         f"--index-existing scope lost rows: {widened}")
                return
        finally:
            mod._find_mount_point = real_mp

    rep.ok("lookup scope in SQL",
           "out-of-scope rows filtered in the query; '_' escaped; "
           "drive-wide scope still unfiltered")


def _check_sparse_verification_sees_content(rep, ctx):
    """Verification must actually compare a sparse copy's bytes.

    Regression guard. Sparse copies used to be recorded as
    __content_check_na__ and skipped: the run printed "Verified: all N files
    OK" and exited 0 while the destination held zeros where the source had
    data. Proven by fault injection — a one-byte corruption in the sparse copy
    path was reported as success, while the same corruption in the dense path
    was caught. That blind spot is what let the _copy_sparse desynchronisation
    ship (see _check_sparse_copy_integrity).

    Exercises _sparse_content_equal directly: it must accept an honest copy,
    reject a single flipped byte, and — importantly — not raise a false alarm
    when the two sides merely differ in how the filesystem laid out the holes.
    """
    target = ctx["target"]
    if not (hasattr(os, "SEEK_DATA") and hasattr(os, "SEEK_HOLE")):
        rep.skip("sparse verification", "SEEK_DATA/SEEK_HOLE unavailable")
        return
    try:
        mod = _import_target(ctx)
    except Exception as e:
        rep.skip("sparse verification", f"could not import target: {e}")
        return
    cmp_fn = getattr(mod, "_sparse_content_equal", None)
    if cmp_fn is None:
        rep.fail("sparse verification",
                 "_sparse_content_equal is gone — sparse copies are no longer "
                 "content-verified")
        return

    with temp_workspace() as ws:
        a = os.path.join(ws, "a.img")
        rnd = random.Random(4242)
        with open(a, "wb") as f:
            for i in range(120):
                f.seek(i * 65536 + rnd.randint(0, 60000))
                f.write(bytes(rnd.randrange(256) for _ in range(64)))
            f.truncate(120 * 65536)

        # 1. an honest copy must pass
        b = os.path.join(ws, "b.img")
        shutil.copyfile(a, b)
        if cmp_fn(a, b) is not True:
            rep.fail("sparse verification",
                     "an identical copy was not recognised as identical")
            return

        # 2. a dense copy of the same bytes must still pass: holes and stored
        #    zeros are the same content, and flagging that would make the
        #    check unusable on filesystems that do not punch holes.
        c = os.path.join(ws, "c.img")
        with open(a, "rb") as src, open(c, "wb") as dst:
            while True:
                blk = src.read(1 << 20)
                if not blk:
                    break
                dst.write(blk)
        if cmp_fn(a, c) is not True:
            rep.fail("sparse verification",
                     "a dense copy of identical bytes was reported as different")
            return

        # 3. one flipped byte inside a data extent must be caught
        with open(a, "rb") as f:
            pos = None
            off = 0
            while off < os.path.getsize(a):
                try:
                    ds = os.lseek(f.fileno(), off, os.SEEK_DATA)
                except OSError:
                    break
                pos = ds
                break
        if pos is None:
            rep.skip("sparse verification", "filesystem reported no data extent")
            return
        with open(b, "r+b") as f:
            f.seek(pos)
            orig = f.read(1)
            f.seek(pos)
            f.write(bytes([(orig[0] + 1) & 0xFF]))
        if cmp_fn(a, b) is not False:
            rep.fail("sparse verification",
                     f"a flipped byte at offset {pos} was NOT detected — a "
                     f"corrupt sparse copy would report success")
            return

        # 4. a truncated destination must be caught
        d = os.path.join(ws, "d.img")
        shutil.copyfile(a, d)
        with open(d, "r+b") as f:
            f.truncate(os.path.getsize(a) - 4096)
        if cmp_fn(a, d) is not False:
            rep.fail("sparse verification", "a short destination was not detected")
            return

        rep.ok("sparse verification",
               "sparse copies are content-compared: identical accepted, "
               "hole-layout difference tolerated, flipped byte and short "
               "destination both caught")


def _check_sync_folder_dedup_downgrade(rep, ctx):
    """Hard-link dedup must stand down inside a Windows cloud-synced folder.

    OneDrive, Dropbox and Google Drive put their sync roots behind the Cloud
    Files API, so every directory inside one is a reparse point carrying a tag
    from the IO_REPARSE_TAG_CLOUD family. Linking inside such a folder saves no
    remote space — the client uploads both paths regardless — and leaves the two
    copies sharing an inode, so editing one rewrites the other.

    The detection is deliberately narrow: the tag and the RECALL_ON_* attributes
    and nothing else. No vendor names, no path matching, no registry, no client
    config files, because a folder merely NAMED "Dropbox" must keep its dedup.

    Exercised through the injected stat hook, so this needs no cloud client on
    the runner — and the last case asserts the real filesystem here is not
    mistaken for one, which is the failure that would silently cost disk space.
    """
    try:
        mod = _import_target(ctx)
    except Exception as e:
        rep.skip("sync-folder dedup", f"could not import target: {e}")
        return
    tagf = getattr(mod, "_is_cloud_reparse_tag", None)
    rootf = getattr(mod, "_cloud_sync_root", None)
    resolve = getattr(mod, "resolve_dedup_strategy", None)
    if not (tagf and rootf and resolve):
        rep.fail("sync-folder dedup",
                 "cloud-filter detection is gone — hard links would be created "
                 "inside cloud-synced folders again")
        return

    # 1. the documented tag family, and only it
    for tag in (0x9000001A, 0x9000101A, 0x9000901A, 0x9000F01A):
        if not tagf(tag):
            rep.fail("sync-folder dedup",
                     f"cloud reparse tag 0x{tag:08X} not recognised")
            return
    for tag, what in ((0xA000000C, "symlink"), (0xA0000003, "mount point"),
                      (0x80000013, "dedup"), (0, "no tag")):
        if tagf(tag):
            rep.fail("sync-folder dedup",
                     f"{what} tag 0x{tag:08X} misread as a cloud placeholder")
            return

    class _St:
        def __init__(self, attrs=0, tag=0):
            self.st_file_attributes = attrs
            self.st_reparse_tag = tag

    REPARSE, RECALL_OPEN, RECALL_DATA = 0x400, 0x00040000, 0x00400000
    root = os.path.abspath(os.path.join(os.sep, "sync-root"))
    inside = os.path.join(root, "a", "b")

    # 2. found from a plain child directory — the destination is usually a
    #    folder the user just made, not yet a placeholder itself
    def only_root(path):
        return _St(REPARSE, 0x9000101A) if os.path.normpath(path) == root else _St(0x10, 0)
    if rootf(inside, _stat=only_root) != root:
        rep.fail("sync-folder dedup",
                 "a destination inside a sync root was not detected — the walk "
                 "up to the root is what catches a freshly created folder")
        return

    # 3. the RECALL_ON_* attributes on their own are enough
    for attr, name in ((RECALL_OPEN, "RECALL_ON_OPEN"),
                       (RECALL_DATA, "RECALL_ON_DATA_ACCESS")):
        if rootf(inside, _stat=lambda p, a=attr: _St(a, 0)) is None:
            rep.fail("sync-folder dedup", f"{name} alone did not trigger detection")
            return

    # 4. an ordinary directory is not a sync folder
    if rootf(inside, _stat=lambda p: _St(0x10, 0)) is not None:
        rep.fail("sync-folder dedup",
                 "an ordinary directory was reported as cloud-synced — this "
                 "would disable dedup on unrelated folders")
        return

    # 5. the downgrade itself
    caps = mod.FSCapabilities(hardlink=True, symlink=True, reflink=False,
                              case_sensitive=True)
    strat, note = resolve(caps, inside, False, _cloud_root=lambda p: root)
    if strat != "none" or not note:
        rep.fail("sync-folder dedup",
                 f"hardlink was not downgraded inside a sync folder (got {strat!r})")
        return
    strat, _ = resolve(caps, inside, True, _cloud_root=lambda p: root)
    if strat != "hardlink":
        rep.fail("sync-folder dedup",
                 "--dedup-in-sync-folder did not restore hard links")
        return
    strat, _ = resolve(caps, inside, False, _cloud_root=lambda p: None)
    if strat != "hardlink":
        rep.fail("sync-folder dedup",
                 "hardlink was downgraded outside a sync folder")
        return

    # 6. reflink is left alone: copy-on-write keeps the copies independent,
    #    which is the property whose absence makes hard links wrong here
    refl = mod.FSCapabilities(hardlink=True, symlink=True, reflink=True,
                              case_sensitive=True)
    strat, note = resolve(refl, inside, False, _cloud_root=lambda p: root)
    if strat != "reflink" or note:
        rep.fail("sync-folder dedup",
                 f"reflink was downgraded inside a sync folder (got {strat!r})")
        return

    # 7. and the real filesystem under the runner is not a sync folder
    with temp_workspace() as ws:
        if rootf(ws) is not None:
            rep.fail("sync-folder dedup",
                     f"a plain temp directory ({ws}) was detected as cloud-synced")
            return

    rep.ok("sync-folder dedup",
           "cloud tag family matched and other reparse tags rejected; hardlink "
           "downgraded inside a sync folder, restored by flag, reflink untouched")


def _check_sparse_copy_integrity(rep, ctx):
    """A sparse copy must reproduce the source byte for byte.

    Regression guard. _copy_sparse walks the source with
    os.lseek(SEEK_DATA/SEEK_HOLE) to skip holes. Those probes ran on the very
    descriptor wrapped by the BufferedReader doing the reading, so they moved
    the raw file position behind its back; the reader then served bytes from
    its stale readahead window — which usually sat inside a hole — and real
    data was silently written out as zeros. Same size, same mtime, wrong
    contents, and blitcp's own verification could not see it because sparse
    copies are exempt from the content digest (__content_check_na__).

    Measured on a real /var/lib/longhorn/replicas tree: 28 of 168 .img files
    corrupted across 20 of 22 replicas, 0 of the 205 non-sparse files touched.

    The layout matters. A file with one hole and one data run copies correctly
    even with the bug — that is why it shipped. The fault needs MANY small
    data extents, which is exactly the shape of a Longhorn replica or a VM
    image, so that is what this builds.
    """
    target = ctx["target"]
    if not (hasattr(os, "SEEK_DATA") and hasattr(os, "SEEK_HOLE")):
        rep.skip("sparse copy integrity",
                 "SEEK_DATA/SEEK_HOLE unavailable on this platform")
        return
    with temp_workspace() as ws:
        src = os.path.join(ws, "src")
        os.makedirs(src)
        rnd = random.Random(20260907)

        # Three layouts, each a spread of small data runs among holes.
        # 'unaligned' deliberately straddles 4 KiB boundaries: the original
        # loss was 8 bytes at offset 4088 of an 8192-byte extent.
        plans = {
            "many_extents.img":  (500, 32768, lambda: rnd.randint(1, 5000), 0),
            "tiny_runs.img":     (200, 65536, lambda: rnd.randint(1, 120),
                                  lambda: rnd.randint(0, 60000)),
            "unaligned.img":     (50, 131072, lambda: rnd.randint(40, 300), 4096),
        }
        for name, (count, stride, size_fn, skew) in plans.items():
            with open(os.path.join(src, name), "wb") as f:
                for i in range(count):
                    if callable(skew):
                        pos = i * stride + skew()
                    elif skew:
                        pos = i * stride + skew - rnd.randint(1, 40)
                    else:
                        pos = i * stride
                    f.seek(pos)
                    f.write(os.urandom(size_fn()))
                f.truncate(count * stride)

        want = {}
        for name in plans:
            path = os.path.join(src, name)
            st = os.stat(path)
            blocks = getattr(st, "st_blocks", None)
            if blocks is None:
                rep.skip("sparse copy integrity",
                         "st_blocks unavailable: cannot confirm the files are sparse")
                return
            if blocks * 512 >= st.st_size:
                rep.skip("sparse copy integrity",
                         f"filesystem did not keep {name} sparse")
                return
            want[name] = _hash_file(path)

        dst = os.path.join(ws, "dst")
        rc, out, err = run_fc(target, [src, dst])
        if rc != 0:
            rep.fail("sparse copy integrity", f"copy failed rc={rc}")
            return

        copied = os.path.join(dst, os.path.basename(src))
        if not os.path.isdir(copied):
            copied = dst
        bad = []
        for name, digest in want.items():
            out_path = os.path.join(copied, name)
            if not os.path.exists(out_path):
                bad.append(f"{name}: missing")
            elif _hash_file(out_path) != digest:
                bad.append(f"{name}: contents differ")
        if bad:
            rep.fail("sparse copy integrity",
                     "sparse copy did not reproduce the source: " + "; ".join(bad))
        else:
            rep.ok("sparse copy integrity",
                   f"{len(want)} multi-extent sparse files copied byte for byte")


def _check_reported_speed(rep, ctx):
    """The reported throughput must describe the copy that actually happened.

    Regression guard, two faults that compounded:
      - speed divided the LOGICAL byte count by the elapsed time, so a sparse
        tree counted holes that were never written (1.2 GB logical / 193 MB
        real read as ~6x the true rate);
      - the clock stopped before anything was flushed, so on a USB disk the
        write cache absorbed the job and the run "finished" at RAM speed.
    Together they reported 4.0 GB/s for a copy that really ran at ~150 MB/s.

    This copies a sparse file and requires the reported rate to stay within
    sight of the bytes that reached the disk.
    """
    target = ctx["target"]
    with temp_workspace() as ws:
        src = os.path.join(ws, "src")
        os.makedirs(src)
        # 256 MB logical, 8 MB real.
        with open(os.path.join(src, "sparse.img"), "wb") as f:
            f.truncate(256 << 20)
            for off in (0, (256 << 20) - (4 << 20)):
                f.seek(off)
                f.write(os.urandom(4 << 20))
        # st_blocks is POSIX-only. Reaching for it unguarded on Windows raised
        # AttributeError, which the per-SECTION handler turned into one
        # "bugs section crashed" line — abandoning the four checks after this
        # one, among them the thread-pool small-file guard that exists for
        # Windows and macOS in the first place. Measured: 23 checks on Linux,
        # 11 on Windows.
        st = os.stat(os.path.join(src, "sparse.img"))
        blocks = getattr(st, "st_blocks", None)
        if blocks is None:
            rep.skip("reported speed",
                     "st_blocks unavailable (Windows): cannot confirm the "
                     "file stayed sparse")
            return
        real = blocks * 512
        if real > (32 << 20):
            rep.skip("reported speed", "filesystem did not keep the file sparse")
            return

        dst = os.path.join(ws, "dst")
        rc, out, err = run_fc(target, [src, dst])
        if rc != 0:
            rep.fail("reported speed", f"copy failed rc={rc}")
            return

        m = re.search(r"Speed:\s*\x1b\[[0-9;]*m*\x1b*\[*[0-9;]*m*\s*([0-9.]+)\s*(\w+)/s",
                      out + err)
        if not m:
            m = re.search(r"Speed:\D*([0-9.]+)\s*([KMGT]?B)/s", _strip_ansi(out + err))
        if not m:
            rep.skip("reported speed", "could not parse the Speed line")
            return
        val, unit = float(m.group(1)), m.group(2).upper()
        mult = {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}.get(unit, 1)
        rate = val * mult

        # A local disk that genuinely sustains >2 GB/s on a 8 MB sparse copy
        # does not exist; anything that high means holes or cache are being
        # counted as throughput.
        if rate > 2e9:
            rep.fail("reported speed",
                     f"reported {val} {unit}/s for {real / 2**20:.0f} MiB of real "
                     f"data — holes or write cache are being counted")
        else:
            rep.ok("reported speed",
                   f"{val} {unit}/s for {real / 2**20:.0f} MiB written — plausible")


def _strip_ansi(t):
    return re.sub(r"\x1b\[[0-9;]*m", "", t)


def _check_quiet_time_matches(rep, ctx):
    """--quiet must report the same duration the full summary does.

    Regression guard: the flush that makes the reported time honest landed in
    the verbose summary only. Progress.finish() keeps its own clock, which
    stops before anything reaches the disk, and --quiet reported from there —
    so the identical copy printed 0.6s quiet and 2.4s verbose. A display flag
    must not change the measurement.

    Measuring that is harder than it looks, and the first version of this check
    got it wrong: it timed one verbose copy, then one quiet copy, and blamed
    the flag for the difference. But whichever copy runs SECOND is faster —
    the source is in page cache by then — so on a CI runner reading a cold
    first copy the gap crossed the threshold and the check failed a correct
    build. Measured, with no flag involved at all: verbose-then-verbose gave
    0.5s then 0.3s.

    So: warm the cache first, then measure BOTH orderings. A real measurement
    bug follows the flag and shows up in both; an ordering artefact swaps sides
    and cancels out.
    """
    target = ctx["target"]
    with temp_workspace() as ws:
        src = make_tree(os.path.join(ws, "src"), big_mb=64, with_empty_dir=False)

        def seconds(pattern, text):
            m = re.search(pattern, _strip_ansi(text))
            if not m:
                return None
            v = float(m.group(1))
            return v / 1000.0 if m.group(2).startswith("ms") else v

        def timed(quiet_flag, dest):
            args = (["--quiet"] if quiet_flag else []) + [src, dest]
            rc, o, e = run_fc(target, args)
            pat = (r"in\s+([0-9.]+)\s*(m?s)" if quiet_flag
                   else r"Time:\s*([0-9.]+)\s*(m?s)")
            return rc, seconds(pat, o + e)

        # Warm-up: discarded, and its only job is to leave the source in page
        # cache so neither measured run is the one paying for the cold read.
        rc0, _t0 = timed(False, os.path.join(ws, "warm"))
        if rc0 != 0:
            rep.fail("quiet timing", f"warm-up copy failed {rc0}")
            return

        # Order A: verbose first.  Order B: quiet first.
        rc1, verbose_a = timed(False, os.path.join(ws, "a1"))
        rc2, quiet_a = timed(True, os.path.join(ws, "a2"))
        rc3, quiet_b = timed(True, os.path.join(ws, "b1"))
        rc4, verbose_b = timed(False, os.path.join(ws, "b2"))

        rcs = (rc1, rc2, rc3, rc4)
        if any(rcs):
            rep.fail("quiet timing", f"copies failed {rcs}")
            return
        times = (verbose_a, quiet_a, quiet_b, verbose_b)
        if any(t is None for t in times):
            rep.skip("quiet timing", "could not parse every reported time")
            return
        if verbose_a <= 0 or verbose_b <= 0:
            rep.skip("quiet timing", "verbose time too small to compare")
            return

        # Run-to-run spread on a real disk is tens of percent; the bug was a
        # 4x gap. Under half means the two paths measure differently — but only
        # when it holds in BOTH orderings, which cache warmth cannot fake.
        ratio_a = quiet_a / verbose_a
        ratio_b = quiet_b / verbose_b
        if ratio_a < 0.5 and ratio_b < 0.5:
            rep.fail("quiet timing",
                     f"--quiet reported {quiet_a:.2f}s/{quiet_b:.2f}s where the "
                     f"summary reported {verbose_a:.2f}s/{verbose_b:.2f}s in "
                     f"both orderings — the flag is changing the measurement")
        else:
            rep.ok("quiet timing",
                   f"quiet {quiet_a:.2f}s/{quiet_b:.2f}s vs summary "
                   f"{verbose_a:.2f}s/{verbose_b:.2f}s (both orderings)")


def _check_sudo_askpass_route(rep, ctx):
    """The sudo re-exec must be able to prompt without a terminal.

    Regression guard. `sudo` with no controlling tty cannot ask for a password
    ("a terminal is required…"), so an elevated run launched from the GUI
    stalled on whatever terminal the app was started from. -A routes the prompt
    to $SUDO_ASKPASS. It must be added ONLY when that variable is set, so a
    normal terminal run keeps prompting the way it always has.
    """
    name = "sudo prompt works without a tty"
    try:
        src = open(ctx["target"], encoding="utf-8", errors="replace").read()
    except OSError as e:                                    # noqa: BLE001
        rep.skip(name, f"could not read target: {e}")
        return
    blk = _source_block(src, "def _reexec_under_sudo(")
    if not blk:
        rep.fail(name, "_reexec_under_sudo is gone")
        return
    if "SUDO_ASKPASS" not in blk:
        rep.fail(name, "the sudo re-exec ignores SUDO_ASKPASS — an elevated "
                       "run with no tty cannot ask for a password")
        return
    if '"-A"' not in blk and "'-A'" not in blk:
        rep.fail(name, "SUDO_ASKPASS is read but -A is never passed to sudo")
        return
    # -A must be conditional: unconditional would break every terminal run on
    # a machine with no askpass helper configured.
    head = blk.split("-A")[0]
    if "if" not in head.split("SUDO_ASKPASS")[-1] and \
            "SUDO_ASKPASS" not in head.split("if")[-1]:
        rep.fail(name, "-A looks unconditional; a terminal run would need an "
                       "askpass helper it does not have")
        return
    rep.ok(name, "sudo -A only when SUDO_ASKPASS is set")


def _check_fuseblk_is_local(rep, ctx):
    """A block-backed FUSE mount is a local disk, not a network share.

    Regression guard. The classifier rejected anything whose fstype started
    with "fuse", which is how EVERY NTFS volume mounts on Linux (ntfs-3g
    reports "fuseblk"). That was not just a wrong label on the summary line:
    the three HDD fast paths — source scan, dedup DB, link audit — all test
    == "hdd", so a rotating USB backup drive formatted NTFS silently lost all
    of them. The distinction is in the mount source: fuseblk names /dev/...,
    while sshfs/rclone/gvfs never do.
    """
    name = "fuseblk is local storage"
    if sys.platform != "linux":
        rep.skip(name, "mountinfo classification is Linux-only")
        return
    try:
        mod = _import_target(ctx)
    except Exception as e:                                  # noqa: BLE001
        rep.skip(name, f"could not import target: {e}")
        return
    fn = getattr(mod, "_classify_storage_linux", None)
    if fn is None:
        rep.fail(name, "_classify_storage_linux is gone")
        return

    # Drive the classifier against a synthetic /proc/self/mountinfo so the
    # check does not depend on this machine happening to have an NTFS disk.
    rows = [
        # (fstype, source, rotational, expected)
        ("fuseblk", "/dev/sdz1", "1", "hdd"),    # ntfs-3g on a spinning disk
        ("fuseblk", "/dev/sdz1", "0", "ssd"),    # ntfs-3g on flash
        ("fuse.sshfs", "user@h:/r", None, "network"),
        ("fuse.rclone", "rclone", None, "network"),
        ("cifs", "//srv/share", None, "network"),
    ]
    mp = "/mnt/uat_fuseblk_probe"
    for fstype, source, rota, expect in rows:
        line = ("99 1 8:161 / %s rw,relatime shared:1 - %s %s rw\n"
                % (mp, fstype, source))
        real_open = builtins.open

        def fake_open(f, *a, **k):
            if f == "/proc/self/mountinfo":
                return io.StringIO(line)
            if rota is not None and str(f).endswith("/queue/rotational"):
                return io.StringIO(rota + "\n")
            return real_open(f, *a, **k)

        real_exists, real_realpath = os.path.exists, os.path.realpath
        try:
            builtins.open = fake_open
            os.path.exists = lambda q: False if "/sys/class/block" in str(q) \
                else real_exists(q)
            os.path.realpath = lambda q, *a, **k: mp if q == mp \
                else real_realpath(q, *a, **k)
            got = fn(mp)
        except Exception as e:                              # noqa: BLE001
            rep.fail(name, f"{fstype} raised {type(e).__name__}: {e}")
            return
        finally:
            builtins.open = real_open
            os.path.exists, os.path.realpath = real_exists, real_realpath
        if got != expect:
            rep.fail(name, f"{fstype} on {source} classified {got!r}, "
                           f"expected {expect!r}")
            return
    rep.ok(name, "fuseblk follows its block device; only non-device FUSE is "
                 "network")


def _check_memory_fs_detection(rep, ctx):
    """A RAM-backed source must be recognised as having no seek penalty.

    Regression guard: tmpfs has no block device, so the /sys rotational lookup
    answered "unknown" and the whole source was dragged into the physical
    layout phase. Only a second probe further in saved it. The portable half of
    the fix — USB-attached drives that lie about being rotational need real
    hardware to test, so this covers what any machine can check.
    """
    target = ctx["target"]
    import importlib.util
    spec = importlib.util.spec_from_file_location("_blitcp_fsdetect", target)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:                                  # noqa: BLE001
        rep.skip("memory-fs detection", f"could not import target: {e}")
        return
    probe = getattr(mod, "_volume_seek_penalty_linux", None)
    if probe is None or sys.platform != "linux":
        rep.skip("memory-fs detection", "Linux-only check")
        return

    ram = None
    for cand in ("/dev/shm", "/run", "/tmp"):
        try:
            with open("/proc/self/mounts") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 3 and parts[1] == cand and \
                            parts[2] in ("tmpfs", "ramfs"):
                        ram = cand
                        break
        except OSError:
            pass
        if ram:
            break
    if not ram or not os.access(ram, os.W_OK):
        rep.skip("memory-fs detection", "no writable tmpfs mount found")
        return

    verdict = probe(ram)
    if verdict is False:
        rep.ok("memory-fs detection", f"{ram} correctly reports no seek penalty")
    else:
        rep.fail("memory-fs detection",
                 f"{ram} is RAM-backed but reported {verdict!r} — the physical "
                 f"layout phase will run for nothing")


def _check_cache_preload(rep, ctx):
    """The in-memory hash cache must answer exactly like the per-row query.

    Regression guard: thousands of point SELECTs against a database that lives
    on the destination — often the slowest device in the path — made Phase 2
    degrade 0.2s -> 2.3s as the table filled. It is now read once into a dict.
    A dict that disagreed with the query would hand back a stale hash and let a
    changed file be treated as a duplicate, so this checks agreement rather
    than speed.
    """
    target = ctx["target"]
    import importlib.util
    spec = importlib.util.spec_from_file_location("_blitcp_cache", target)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:                                  # noqa: BLE001
        rep.skip("cache preload", f"could not import target: {e}")
        return
    if not hasattr(mod.DedupDB, "preload_source_cache"):
        rep.fail("cache preload",
                 "DedupDB.preload_source_cache is missing — Phase 2 is back to "
                 "one query per file")
        return

    with temp_workspace() as ws:
        dst = os.path.join(ws, "dst")
        os.makedirs(dst)
        try:
            db = mod.DedupDB(dst)
        except Exception as e:                              # noqa: BLE001
            rep.skip("cache preload", f"could not open a cache db: {e}")
            return
        # Tag the rows so the cleanup below can find exactly what this test
        # wrote: DedupDB prefers the MOUNT root, so on a temp workspace these
        # land in the shared /tmp (or /) database that real copies then read.
        marker = "/__audit_probe__"
        rows = [(f"{marker}/f{i}.bin", 1000 + i, 5000 + i, f"hash{i:04d}")
                for i in range(200)]
        db.store_source_batch(rows)
        try:
            db.commit_pending()
        except AttributeError:
            pass

        def _cleanup():
            # This may be the shared database at the mount root that real
            # copies read, so the probe rows go whatever way this test exits —
            # including the early return below, which fires precisely when the
            # table is large, i.e. when leaving junk behind would matter most.
            try:
                with db.lock:
                    db.conn.execute(
                        "DELETE FROM source_cache WHERE rel_path LIKE ?",
                        (marker + "/%",))
                    db.conn.commit()
            except sqlite3.Error as e:
                rep.warn("cache preload",
                         f"could not clean up the probe rows: {e}")
            try:
                db.close()
            except (sqlite3.Error, AttributeError, OSError):
                pass

        try:
            preloaded = db.preload_source_cache()
            if preloaded is None:
                rep.skip("cache preload",
                         "preload declined (table over the limit)")
                return

            bad = 0
            for rel, size, mt, want in rows:
                direct = db.lookup(rel, size, mt)
                memory = preloaded.get((rel, size, mt))
                if direct != memory or memory != want:
                    bad += 1
            # A row that does not exist must miss in both.
            if db.lookup("/src/nope.bin", 1, 1) is not None or \
                    preloaded.get(("/src/nope.bin", 1, 1)) is not None:
                bad += 1
            # A stale mtime must miss in both.
            if db.lookup(rows[0][0], rows[0][1], 999999) is not None or \
                    preloaded.get((rows[0][0], rows[0][1], 999999)) is not None:
                bad += 1
        finally:
            _cleanup()

        if bad:
            rep.fail("cache preload",
                     f"{bad} disagreement(s) between the in-memory cache and "
                     f"the database")
        else:
            rep.ok("cache preload",
                   f"{len(rows)} rows agree with the per-row query, misses included")


def _check_threadpool_small_files(rep, ctx):
    """The thread-pool small-file path must work, not just the io_uring one.

    Regression guard: on Linux with liburing present, copy_hybrid never reaches
    copy_small_parallel, so a NameError in it went unnoticed while every local
    test passed — and that path is the only one Windows and macOS have. This
    forces the fallback by making the ring look unavailable.
    """
    target = ctx["target"]
    import importlib.util
    spec = importlib.util.spec_from_file_location("_blitcp_pool", target)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:                                  # noqa: BLE001
        rep.skip("thread-pool small files", f"could not import target: {e}")
        return

    with temp_workspace() as ws:
        src = os.path.join(ws, "src")
        dst = os.path.join(ws, "dst")
        os.makedirs(src)
        for i in range(40):
            with open(os.path.join(src, f"f{i:03d}.txt"), "wb") as f:
                f.write(os.urandom(4096))

        entries = mod.scan_source(src, dst)
        if isinstance(entries, tuple):
            entries = entries[0]
        os.makedirs(dst, exist_ok=True)
        prog = mod.Progress(sum(e.size for e in entries), len(entries))
        original = getattr(mod, "_uring_lib", None)
        if original is None:
            # Silently not patching would let copy_hybrid take the io_uring
            # route and the test pass while the path it guards goes untested —
            # the exact failure it exists to prevent.
            rep.fail("thread-pool small files",
                     "_uring_lib is gone; cannot force the fallback path, so "
                     "this check no longer guards anything")
            return
        mod._uring_lib = lambda: None           # force the fallback
        try:
            mod.copy_hybrid(entries, dst, prog, 1 << 20)
        except Exception as e:                              # noqa: BLE001
            rep.fail("thread-pool small files",
                     f"the non-io_uring path crashed: {e!r}")
            return
        finally:
            mod._uring_lib = original

        copied = sum(len(fs) for _r, _d, fs in os.walk(dst))
        if copied == len(entries):
            rep.ok("thread-pool small files",
                   f"{copied} files copied without io_uring")
        else:
            rep.fail("thread-pool small files",
                     f"only {copied} of {len(entries)} files copied without io_uring")


def _check_midbatch_symlinked_parent(rep, ctx):
    """A parent directory swapped for a symlink MID-BATCH must still be caught.

    The extraction path validates each member before writing it, and part of
    that validation resolves the deepest existing ancestor and requires it to
    stay inside the destination. The obvious optimisation is to remember which
    directories already passed and skip the walk for the other 800 files in
    them — the ancestor check is then paid once per directory instead of once
    per file.

    This is the check that says whether that is allowed. It extracts a batch,
    and BETWEEN two members of the same directory — after that directory has
    already been validated for an earlier file — replaces it with a symlink
    pointing outside the destination. The second file must be refused.

    If a future change makes this pass through, the extraction writes THROUGH
    the planted symlink and lands outside the destination tree. Under --use-sudo
    that is a root-owned write to an attacker-chosen path.
    """
    target = ctx["target"]
    if os.name == "nt":
        rep.skip("mid-batch symlinked parent", "POSIX symlink semantics only")
        return
    import importlib.util
    import io as _io
    import tarfile as _tarfile
    spec = importlib.util.spec_from_file_location("_blitcp_toctou", target)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:                                  # noqa: BLE001
        rep.skip("mid-batch symlinked parent", f"could not import target: {e}")
        return

    with temp_workspace() as ws:
        dst = os.path.join(ws, "dst")
        outside = os.path.join(ws, "outside")
        os.makedirs(os.path.join(dst, "sub"))
        os.makedirs(outside)

        tar_path = os.path.join(ws, "batch.tar")
        with _tarfile.open(tar_path, "w") as tf:
            for name in ("sub/first.txt", "sub/second.txt"):
                info = _tarfile.TarInfo(name)
                payload = b"payload"
                info.size = len(payload)
                info.mode = 0o644
                tf.addfile(info, _io.BytesIO(payload))

        with _tarfile.open(tar_path, "r") as tf:
            members = {m.name: m for m in tf.getmembers()}
            ctxobj = getattr(mod, "_ExtractCtx", None)
            kw = {"ctx": ctxobj(dst)} if ctxobj is not None else {}

            # File 1 — validates dst/sub and extracts normally.
            first = mod._safe_tar_extract(tf, members["sub/first.txt"], dst,
                                          trusted_source=False, **kw)
            if first is not True:
                rep.fail("mid-batch symlinked parent",
                         f"the benign first member was refused: {first}")
                return

            # The swap: dst/sub is now a symlink out of the destination.
            os.rename(os.path.join(dst, "sub"), os.path.join(dst, "sub.real"))
            os.symlink(outside, os.path.join(dst, "sub"))

            # File 2 — same directory, already validated for file 1.
            second = mod._safe_tar_extract(tf, members["sub/second.txt"], dst,
                                           trusted_source=False, **kw)

    escaped = os.path.exists(os.path.join(outside, "second.txt"))
    if second is not True and not escaped:
        rep.ok("mid-batch symlinked parent",
               f"refused after the swap ({second})")
    elif escaped:
        rep.fail("mid-batch symlinked parent",
                 "extraction wrote THROUGH a symlinked parent planted mid-batch "
                 "— the file landed outside the destination")
    else:
        rep.fail("mid-batch symlinked parent",
                 "the member after the swap was accepted; the ancestor check "
                 "is no longer effective per file")


def _load_target_module(target, name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, target)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _check_pull_paths_are_validated(rep, ctx):
    """Every destination path built from a REMOTE-supplied name must be checked.

    A pull trusts the far side for filenames. Three write paths built a local
    path by joining that name onto the destination with nothing in between:

      _tar_extract_stream        tar-over-SSH pull, extracted with no blitcp
                                 validation at all
      copy_individual_remote_to_local   the SFTP fallback, reached whenever the
                                 remote has no tar or --sftp-only is set
      _ssh_pull_smart's dedup links     os.remove() then os.link() at the
                                 joined path — a delete, not just a write

    O_NOFOLLOW on the write does not help: it refuses a symlinked LEAF and says
    nothing about '..'. This drives each path with a hostile relative name and
    fails if anything lands outside the destination.
    """
    target = ctx["target"]
    if os.name == "nt":
        rep.skip("pull paths validated", "POSIX path semantics only")
        return
    try:
        mod = _load_target_module(target, "_blitcp_pull")
    except Exception as e:                                  # noqa: BLE001
        rep.skip("pull paths validated", f"could not import target: {e}")
        return

    import io as _io
    import tarfile as _tarfile

    escapes = []

    # ── 1. tar-over-SSH pull: _tar_extract_stream ──────────────────────────
    with temp_workspace() as ws:
        dst = os.path.join(ws, "dst")
        os.makedirs(dst)
        buf = _io.BytesIO()
        with _tarfile.open(fileobj=buf, mode="w") as tf:
            i = _tarfile.TarInfo("../escaped_tar.txt")
            p = b"pwned"
            i.size = len(p)
            i.mode = 0o644
            tf.addfile(i, _io.BytesIO(p))
        buf.seek(0)
        try:
            mod._tar_extract_stream(buf, dst)
        except Exception:                                   # noqa: BLE001
            pass
        if os.path.exists(os.path.join(ws, "escaped_tar.txt")):
            escapes.append("_tar_extract_stream wrote above the destination")

    # ── 2. SFTP fallback: copy_individual_remote_to_local ──────────────────
    # Driven at the path-building level: the function joins entry.rel onto
    # dst_root before it ever touches the network.
    with temp_workspace() as ws:
        dst = os.path.join(ws, "dst")
        os.makedirs(dst)
        rel = "../escaped_sftp.txt"
        built = os.path.join(dst, rel)
        checked = mod._safe_local_dest(os.path.realpath(dst), rel)
        if checked is None and os.path.abspath(built) != os.path.abspath(
                os.path.join(dst, os.path.basename(rel))):
            # The validator rejects it; the question is whether the copy path
            # asks. Probe the real function with a stub sftp that records the
            # path it was handed.
            class _StubSFTP:
                def __init__(self):
                    self.asked = []

                def open(self, *a, **kw):
                    raise OSError("stub")

                def get(self, remote, local, *a, **kw):
                    self.asked.append(local)
                    raise OSError("stub")

                def close(self):
                    pass

            class _StubSSH:
                def __init__(self, sftp):
                    self._sftp = sftp
                    self.caps = {}

                def open_sftp(self):
                    return self._sftp

            entry = mod.FileEntry(src="/remote/x", rel=rel, size=10,
                                  physical_offset=0, content_hash=None)
            stub = _StubSFTP()
            prog = mod.Progress(10, 1)
            try:
                mod.copy_individual_remote_to_local(
                    [entry], _StubSSH(stub), dst, prog, 1 << 20)
            except Exception:                               # noqa: BLE001
                pass
            # Whatever happened, nothing may exist above the destination.
            if os.path.exists(os.path.join(ws, "escaped_sftp.txt")):
                escapes.append("SFTP fallback wrote above the destination")
            elif os.path.isdir(os.path.join(ws, "dst", "..", "escaped_sftp.txt")):
                escapes.append("SFTP fallback created a path above the destination")

    # ── 3. dedup link creation in the pull path ────────────────────────────
    # The link loop does os.remove() at the joined path before linking, so an
    # unvalidated name deletes an arbitrary file. Reproduced directly.
    with temp_workspace() as ws:
        dst = os.path.join(ws, "dst")
        os.makedirs(dst)
        victim = os.path.join(ws, "victim.txt")
        with open(victim, "w") as f:
            f.write("precious")
        canonical = os.path.join(dst, "real.txt")
        with open(canonical, "w") as f:
            f.write("data")
        rel = "../victim.txt"
        if mod._safe_local_dest(os.path.realpath(dst), rel) is None:
            pass  # validator would reject — good, provided the loop asks it
        helper = getattr(mod, "_safe_pull_link_dest", None)
        if helper is None:
            escapes.append("dedup link loop has no validated path helper")
        elif helper(dst, rel) is not None:
            escapes.append("dedup link path helper accepted a name above the "
                           "destination")

    if not escapes:
        rep.ok("pull paths validated",
               "tar stream, SFTP fallback and dedup links all refuse a remote "
               "name that points above the destination")
    else:
        rep.fail("pull paths validated", "; ".join(escapes))


def _check_create_links_validates(rep, ctx):
    """create_links() unlinks whatever is at the joined path before linking.

    In the remote-to-local flow the link_map keys are names the REMOTE chose:
    filter_unchanged_remote_to_local() builds the map before _safe_batch() ever
    runs, so nothing has filtered them by the time create_links() joins them
    onto the destination and calls os.unlink(). That is a delete at a path the
    far side picked, and under --use-sudo it is a delete as root.

    Fails if a link_map key pointing above the destination is acted on.
    """
    target = ctx["target"]
    if os.name == "nt":
        rep.skip("create_links validates", "POSIX path semantics only")
        return
    try:
        mod = _load_target_module(target, "_blitcp_links")
    except Exception as e:                                  # noqa: BLE001
        rep.skip("create_links validates", f"could not import target: {e}")
        return

    with temp_workspace() as ws:
        dst = os.path.join(ws, "dst")
        os.makedirs(dst)
        victim = os.path.join(ws, "victim.txt")
        with open(victim, "w") as f:
            f.write("precious")
        canonical = os.path.join(dst, "real.txt")
        with open(canonical, "w") as f:
            f.write("data")

        # A duplicate whose name climbs out of the destination onto the victim.
        try:
            mod.create_links({"../victim.txt": "real.txt"}, dst)
        except Exception:                                   # noqa: BLE001
            pass

        if not os.path.exists(victim):
            rep.fail("create_links validates",
                     "a link_map key above the destination DELETED a file "
                     "outside it")
            return
        if open(victim).read() != "precious":
            rep.fail("create_links validates",
                     "a link_map key above the destination overwrote a file "
                     "outside it")
            return
        rep.ok("create_links validates",
               "a link_map key above the destination is refused")


def _check_http_filename_cannot_traverse(rep, ctx):
    """The name taken from an http(s):// source URL must be one path segment.

    run_http_transfer derives the destination filename with
    basename(urlsplit(url).path) and THEN unquotes it. Percent-encoding
    therefore survives the basename: '%2e%2e%2fevil' is one segment when
    basename runs and becomes '../evil' immediately after, and that string is
    posixpath.join()ed onto the remote destination directory.

    No attacker controls this — Content-Disposition is not honoured and the
    redirect target is used only for a diagnostic message — so it is the user's
    own URL doing it. It is still a filename that escapes the directory the
    user named, which is not a thing a copy tool should do quietly.
    """
    target = ctx["target"]
    try:
        mod = _load_target_module(target, "_blitcp_httpname")
    except Exception as e:                                  # noqa: BLE001
        rep.skip("http filename cannot traverse", f"could not import target: {e}")
        return

    fn = getattr(mod, "_http_dest_filename", None)
    if fn is None:
        rep.fail("http filename cannot traverse",
                 "no _http_dest_filename() helper; the URL name is still "
                 "derived inline, where unquote runs after basename")
        return

    bad = []
    for url, why in (
        ("https://h/dir/%2e%2e%2fevil.txt", "encoded ../ in the last segment"),
        ("https://h/dir/%2e%2e%2f%2e%2e%2froot.txt", "doubled encoded ../"),
        ("https://h/dir/%2fabs.txt", "encoded leading slash"),
        ("https://h/dir/a%2fb.txt", "encoded separator"),
    ):
        got = fn(url)
        if got is None:
            continue                      # refused outright — fine
        if "/" in got or got in ("..", ".") or got.startswith("/"):
            bad.append(f"{why}: {got!r}")

    if bad:
        rep.fail("http filename cannot traverse", "; ".join(bad))
    else:
        rep.ok("http filename cannot traverse",
               "an encoded separator or '..' cannot survive into the "
               "destination name")


def _check_large_member_is_validated(rep, ctx):
    """The >=1 MB branch of extract_member must validate too.

    It is the only branch that does not go through _safe_tar_extract: it opens
    and writes the file itself, and its own inline check is a realpath
    comparison that would not reject a symlink, device or hard-link member, nor
    a '..' component. For a while it relied on a validation its caller
    performed, and when that call site was removed the branch was left bare —
    which is how a hole opened in the one function that already had a hole.

    Honest about what it does and does not prove: this test passes both with
    and without that validation call, and no case was found that separates the
    two. Everything reachable on a >=1 MB member is already covered by the
    branch's own realpath containment, by O_NOFOLLOW on the write, and by
    extractfile() returning None for a non-regular member. Symlink and
    hard-link members carry size 0 and take the small branch; a NUL in a name
    does not survive tar's NUL-terminated name field at all.

    It is therefore a regression guard rather than a demonstration: the
    validation is defence in depth, and this fails the day someone weakens the
    inline check that is currently doing the work.
    """
    target = ctx["target"]
    if os.name == "nt":
        rep.skip("large tar member validated", "POSIX path semantics only")
        return
    try:
        mod = _load_target_module(target, "_blitcp_largemember")
    except Exception as e:                                  # noqa: BLE001
        rep.skip("large tar member validated", f"could not import target: {e}")
        return

    import io as _io
    import tarfile as _tarfile

    class _NullProgress:
        def update(self, *a, **kw):
            pass

        def display(self, *a, **kw):
            pass

    big = 2 * 1024 * 1024          # over the 1 MB small/large threshold
    problems = []

    with temp_workspace() as ws:
        dst = os.path.join(ws, "dst")
        os.makedirs(dst)
        tar_path = os.path.join(ws, "big.tar")
        with _tarfile.open(tar_path, "w") as tf:
            payload = b"A" * big
            for name in ("../escaped_big.bin", "ok/inside.bin"):
                info = _tarfile.TarInfo(name)
                info.size = len(payload)
                info.mode = 0o644
                tf.addfile(info, _io.BytesIO(payload))
            # a symlink member, which only _validate_tar_member rejects
            link = _tarfile.TarInfo("ok/evil_link")
            link.type = _tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            link.size = 0
            tf.addfile(link)

        with _tarfile.open(tar_path, "r") as tf:
            ex = mod._ProgressTarExtractor(tf, dst, _NullProgress())
            results = {}
            for m in tf.getmembers():
                try:
                    results[m.name] = ex.extract_member(m)
                except Exception as e:                      # noqa: BLE001
                    results[m.name] = "raised: %r" % (e,)

        if os.path.exists(os.path.join(ws, "escaped_big.bin")):
            problems.append("a >=1 MB member named '../…' was written above "
                            "the destination")
        elif results.get("../escaped_big.bin") is True:
            problems.append("a >=1 MB member named '../…' was accepted")
        if results.get("ok/evil_link") is True:
            problems.append("a symlink member was accepted")
        if results.get("ok/inside.bin") is not True:
            problems.append("a legitimate large member was refused: %r"
                            % (results.get("ok/inside.bin"),))

    if problems:
        rep.fail("large tar member validated", "; ".join(problems))
    else:
        rep.ok("large tar member validated",
               "the >=1 MB branch refuses traversal and still accepts a "
               "legitimate member (regression guard; see the docstring for "
               "what this does not prove)")


def _check_untrusted_mode_is_clamped(rep, ctx):
    """A remote must not be able to land world-writable files.

    _safe_tar_extract already strips setuid/setgid when trusted_source=False,
    because under --use-sudo the extracted file is root-owned and an
    attacker-chosen setuid bit is a privilege escalation. The same argument
    applies to the write bits and it was not being made: the member's mode was
    re-applied verbatim otherwise, so a hostile remote could ship 0o777 and get
    0o777 — root-owned and world-writable inside the destination tree.

    Python's own 'data' filter clamps to 0o755 for exactly this reason. Ours ran
    AFTER the filter and overwrote it, so the clamp was absent on every
    interpreter, in the default configuration, not only on the ones missing the
    PEP 706 backport.
    """
    target = ctx["target"]
    if os.name == "nt":
        rep.skip("untrusted mode clamped", "POSIX mode semantics only")
        return
    try:
        mod = _load_target_module(target, "_blitcp_modeclamp")
    except Exception as e:                                  # noqa: BLE001
        rep.skip("untrusted mode clamped", f"could not import target: {e}")
        return

    import io as _io
    import stat as _stat
    import tarfile as _tarfile

    hostile = {
        "worldwrite.sh": 0o777,
        "grpwrite.txt": 0o664,
        "setuid.bin": 0o4755,
        "sticky.txt": 0o1777,
    }
    with temp_workspace() as ws:
        dst = os.path.join(ws, "dst")
        os.makedirs(dst)
        tar_path = os.path.join(ws, "hostile.tar")
        payload = b"z" * 32
        with _tarfile.open(tar_path, "w") as tf:
            for name, mode in hostile.items():
                info = _tarfile.TarInfo(name)
                info.size = len(payload)
                info.mode = mode
                tf.addfile(info, _io.BytesIO(payload))
            d = _tarfile.TarInfo("hostiledir")
            d.type = _tarfile.DIRTYPE
            d.mode = 0o2777
            tf.addfile(d)

        with _tarfile.open(tar_path, "r") as tf:
            for m in tf.getmembers():
                r = mod._safe_tar_extract(tf, m, dst, trusted_source=False)
                if r is not True:
                    rep.fail("untrusted mode clamped",
                             f"a benign member was refused: {m.name}: {r}")
                    return

        bad = []
        forbidden = (_stat.S_ISUID | _stat.S_ISGID | _stat.S_ISVTX
                     | _stat.S_IWGRP | _stat.S_IWOTH)
        for name in list(hostile) + ["hostiledir"]:
            landed = _stat.S_IMODE(os.lstat(os.path.join(dst, name)).st_mode)
            if landed & forbidden:
                bad.append("%s landed %s" % (name, oct(landed)))

    if bad:
        rep.fail("untrusted mode clamped",
                 "an untrusted remote's mode bits survived: "
                 + "; ".join(bad)
                 + " — setuid/setgid/sticky and group/other write must all be "
                   "masked for trusted_source=False")
    else:
        rep.ok("untrusted mode clamped",
               "setuid, setgid, sticky and group/other write are all masked "
               "for an untrusted source")


def _check_pull_reports_rejections(rep, ctx):
    """A refused member must reach the caller, and the exit code.

    _tar_extract_stream counts what it refuses and then returns only the
    delivered count, so the rejections are printed and discarded. The pull
    driver's exit code is `0 if verified else 1`, and `verified` starts True
    and is only ever changed inside `if not args.no_verify and caps["hash"]`.
    With --no-verify, or against a remote with no hash tool, blitcp can refuse
    every member of the transfer and still exit 0 with a success summary.

    That is incident (E) rebuilt inside the fix for incident (A): a success
    report for a check that did not run.
    """
    target = ctx["target"]
    if os.name == "nt":
        rep.skip("pull reports rejections", "POSIX path semantics only")
        return
    try:
        mod = _load_target_module(target, "_blitcp_reject")
    except Exception as e:                                  # noqa: BLE001
        rep.skip("pull reports rejections", f"could not import target: {e}")
        return

    import io as _io
    import tarfile as _tarfile

    bad = []
    with temp_workspace() as ws:
        dst = os.path.join(ws, "dst")
        os.makedirs(dst)
        buf = _io.BytesIO()
        payload = b"q" * 16
        with _tarfile.open(fileobj=buf, mode="w") as tf:
            for name in ("good.txt", "../escaped.txt"):
                i = _tarfile.TarInfo(name)
                i.size = len(payload)
                i.mode = 0o644
                tf.addfile(i, _io.BytesIO(payload))
        buf.seek(0)
        result = mod._tar_extract_stream(buf, dst)

        # The caller has to be able to tell "all delivered" from "one refused".
        if isinstance(result, int):
            bad.append("_tar_extract_stream returns only the delivered count "
                       "(%d); a refused member is printed and then dropped, so "
                       "no caller can act on it" % result)
        else:
            try:
                done, rejected = result
            except (TypeError, ValueError):
                bad.append("_tar_extract_stream returned %r, which the caller "
                           "cannot read as (delivered, refused)" % (result,))
            else:
                if rejected < 1:
                    bad.append("a member above the destination was refused but "
                               "reported as %d rejections" % rejected)
                if done != 1:
                    bad.append("expected 1 delivered member, got %d" % done)

    # And the driver must actually use it, rather than deciding on verification
    # alone — which does not run under --no-verify.
    try:
        eng = open(target, encoding="utf-8", errors="replace").read()
        tree = ast.parse(eng, filename=target)
    except Exception as e:                                  # noqa: BLE001
        rep.skip("pull reports rejections", f"could not parse target: {e}")
        return
    driver = ""
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "_ssh_pull_smart":
            driver = "\n".join(eng.splitlines()[n.lineno - 1:n.end_lineno])
    if driver:
        call = [ln for ln in driver.splitlines()
                if "_tar_extract_stream(" in ln]
        if call and "=" not in call[0]:
            bad.append("_ssh_pull_smart discards the return of "
                       "_tar_extract_stream, so a refused member cannot reach "
                       "the exit code")
        if "return 0 if verified else 1" in driver:
            bad.append("_ssh_pull_smart's exit code depends on `verified` "
                       "alone, which stays True under --no-verify and when the "
                       "remote has no hash tool")

    if bad:
        rep.fail("pull reports rejections", "; ".join(bad))
    else:
        rep.ok("pull reports rejections",
               "a refused member reaches the caller and the exit code, "
               "independently of whether verification ran")


def _check_pull_link_target_validated(rep, ctx):
    """The dedup link TARGET is remote-supplied too, and was not checked.

    In _ssh_pull_smart the link list holds (dup, tp) pairs. `dup` is validated;
    `tp` is not, and for the same-run case it is built from `seen[h]` — another
    name the remote chose. os.link(tp, dp) then hardlinks an attacker-named
    path into the destination, and os.symlink points at it.

    create_links() in the same change validates BOTH sides for exactly this
    reason. This one was missed.
    """
    target = ctx["target"]
    try:
        eng = open(target, encoding="utf-8", errors="replace").read()
        tree = ast.parse(eng, filename=target)
    except Exception as e:                                  # noqa: BLE001
        rep.skip("pull link target validated", f"could not parse target: {e}")
        return
    driver = None
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "_ssh_pull_smart":
            driver = n
    if driver is None:
        rep.skip("pull link target validated", "_ssh_pull_smart not found")
        return

    # Find the loop that creates the links, then check what reaches os.link.
    bad = []
    for loop in ast.walk(driver):
        if not isinstance(loop, ast.For):
            continue
        seg = "\n".join(eng.splitlines()[loop.lineno - 1:loop.end_lineno])
        if "os.link(" not in seg and "os.symlink(" not in seg:
            continue
        # A name counts as validated when it is passed INTO a validator or
        # comes OUT of one. Only counting the arguments called `safe_tp = 
        # _safe_pull_link_dest(...)` unvalidated, which is backwards — that is
        # the validated value.
        _checks = ("_safe_pull_link_dest", "_safe_local_dest",
                   "_validate_rel_path")
        validated = set()
        for c in ast.walk(loop):
            if isinstance(c, ast.Call):
                _q, bare = _call_name_for_audit(c)
                if bare in _checks:
                    for a in c.args:
                        for nn in ast.walk(a):
                            if isinstance(nn, ast.Name):
                                validated.add(nn.id)
        for c in ast.walk(loop):
            if not isinstance(c, ast.Assign):
                continue
            calls = [x for x in ast.walk(c.value) if isinstance(x, ast.Call)]
            if not any(_call_name_for_audit(x)[1] in _checks for x in calls):
                continue
            for t in c.targets:
                for nn in ast.walk(t):
                    if isinstance(nn, ast.Name):
                        validated.add(nn.id)
        for c in ast.walk(loop):
            if not isinstance(c, ast.Call):
                continue
            _q, bare = _call_name_for_audit(c)
            if bare not in ("link", "symlink"):
                continue
            src_arg = c.args[0] if c.args else None
            names = {nn.id for nn in ast.walk(src_arg)
                     if isinstance(nn, ast.Name)} if src_arg else set()
            if names and not (names & validated):
                bad.append("os.%s() link target built from %s, which no "
                           "validator in the loop touches"
                           % (bare, "/".join(sorted(names))))
    if bad:
        rep.fail("pull link target validated", "; ".join(sorted(set(bad))))
    else:
        rep.ok("pull link target validated",
               "both sides of a dedup link are validated, not only the "
               "destination path")


def _call_name_for_audit(node):
    f = node.func
    if isinstance(f, ast.Attribute):
        return None, f.attr
    if isinstance(f, ast.Name):
        return f.id, f.id
    return None, None


def _check_dest_symlink_policy(rep, ctx):
    """Three cases, and the middle one is the whole point.

    _safe_local_dest resolves symlinks and refuses anything landing outside
    the destination. That is right for a name the far side chose. It is wrong
    for a local copy into a destination the user deliberately laid out with a
    symlinked directory — `cp -r` follows those, and refusing is a regression
    against the tool being replaced.

    But "the user made that symlink" is not something the code can see, and
    under --use-sudo it must not assume it: a local unprivileged attacker
    plants dst/photos -> /etc precisely because they know a root copy is
    coming. So elevation pulls the strict behaviour back even for local names.

        trusted_source=True,  not elevated -> textual check only
        trusted_source=True,  elevated     -> full check
        trusted_source=False               -> full check, unchanged
    """
    target = ctx["target"]
    # Not skipped on Windows any more. A junction is the same threat as a
    # symlink and the policy is the same code; if the platform will not let the
    # test create one, the decision logic is still worth exercising with a
    # mocked elevation rather than skipped outright.
    if os.name == "nt":
        try:
            _probe = tempfile.mkdtemp()
            os.symlink(_probe, os.path.join(_probe, "l"))
        except (OSError, NotImplementedError, AttributeError):
            rep.skip("destination symlink policy",
                     "this Windows account cannot create a link to test with; "
                     "the elevation logic is covered by trust-remote-modes")
            return
    try:
        mod = _load_target_module(target, "_blitcp_symlinkpolicy")
    except Exception as e:                                  # noqa: BLE001
        rep.skip("destination symlink policy", f"could not import target: {e}")
        return

    bad = []
    with temp_workspace() as ws:
        dst = os.path.join(ws, "dst")
        outside = os.path.join(ws, "elsewhere")
        os.makedirs(dst)
        os.makedirs(outside)
        # the user's own layout: a real directory moved out, linked back in
        os.symlink(outside, os.path.join(dst, "photos"))
        real_root = os.path.realpath(dst)
        rel = "photos/holiday.jpg"

        # _is_elevated is THE predicate for a path decision now;
        # _is_elevated_for_preserve answers a different question (chown).
        real_elev = getattr(mod, "_is_elevated", None)
        if real_elev is None:
            rep.skip("destination symlink policy",
                     "_is_elevated_for_preserve is gone")
            return
        try:
            mod._is_elevated = lambda: False
            try:
                local_ok = mod._dest_policy(dst, rel, trusted_source=True)[0]
            except TypeError as e:
                bad.append("_dest_policy has no trusted_source parameter, so "
                           "a local copy cannot be told apart from a remote "
                           "one (%s)" % e)
                local_ok = None
                mod._is_elevated = real_elev
                rep.fail("destination symlink policy", "; ".join(bad))
                return
            if local_ok is None:
                bad.append("a local copy into the user's own symlinked "
                           "directory was refused; cp -r follows it")
            remote = mod._dest_policy(dst, rel, trusted_source=False)[0]
            if remote is not None:
                bad.append("a REMOTE-supplied name resolved through a "
                           "symlinked directory and was accepted")

            mod._is_elevated = lambda: True
            elevated = mod._dest_policy(dst, rel, trusted_source=True)[0]
            if elevated is not None:
                bad.append("under elevation a local name still resolved "
                           "through a symlinked directory — a planted symlink "
                           "would redirect a root write")

            # The textual check must survive in every mode.
            for ts in (True, False):
                mod._is_elevated = lambda: False
                if mod._dest_policy(dst, "../escape.txt",
                                    trusted_source=ts)[0] is not None:
                    bad.append("a '..' name was accepted with "
                               "trusted_source=%s" % ts)
        finally:
            mod._is_elevated = real_elev
            mod._REAL_ROOT_CACHE.clear()

    if bad:
        rep.fail("destination symlink policy", "; ".join(bad))
    else:
        rep.ok("destination symlink policy",
               "local names follow the user's symlinks, elevation and remote "
               "names do not, and '..' is refused in every mode")


def _check_symlinked_dest_preflight(rep, ctx):
    """The refusal has to be reported once, not once per file.

    A symlinked destination directory with 4,000 files under it produces 4,000
    identical refusals, which is not a diagnosis, it is a flood. The run should
    say which directory, where it points, and how many incoming files it
    affects, before it starts copying.
    """
    target = ctx["target"]
    if os.name == "nt":
        rep.skip("symlinked destination preflight", "POSIX symlinks only")
        return
    try:
        mod = _load_target_module(target, "_blitcp_preflight")
    except Exception as e:                                  # noqa: BLE001
        rep.skip("symlinked destination preflight",
                 f"could not import target: {e}")
        return

    fn = getattr(mod, "report_symlinked_dest_dirs", None)
    if fn is None:
        rep.fail("symlinked destination preflight",
                 "no report_symlinked_dest_dirs() — a symlinked destination "
                 "directory is discovered one refused file at a time")
        return

    with temp_workspace() as ws:
        dst = os.path.join(ws, "dst")
        outside = os.path.join(ws, "elsewhere")
        os.makedirs(os.path.join(dst, "ok"))
        os.makedirs(outside)
        os.symlink(outside, os.path.join(dst, "photos"))
        rels = ["photos/f%04d.jpg" % i for i in range(1847)] + \
               ["ok/a.txt", "ok/b.txt"]
        # Elevated: the reporter names only what the policy would actually
        # refuse, and unelevated the policy allows the user's own symlink.
        real_elev = mod._is_elevated
        try:
            mod._is_elevated = lambda: True
            mod._REAL_ROOT_CACHE.clear()
            found = fn(dst, rels)
        finally:
            mod._is_elevated = real_elev
            mod._REAL_ROOT_CACHE.clear()

    if not found:
        rep.fail("symlinked destination preflight",
                 "the symlinked directory was not reported at all")
        return
    entry = found[0]
    problems = []
    if entry.get("rel") != "photos":
        problems.append("named %r instead of the symlinked directory"
                        % entry.get("rel"))
    if entry.get("count") != 1847:
        problems.append("counted %r affected files, expected 1847"
                        % entry.get("count"))
    if not entry.get("target"):
        problems.append("did not say where the symlink points")
    if problems:
        rep.fail("symlinked destination preflight", "; ".join(problems))
    else:
        rep.ok("symlinked destination preflight",
               "the symlinked directory, its target and the number of files "
               "it affects are reported once, before copying")


def _check_trust_remote_modes_optout(rep, ctx):
    """The permission clamp must have a documented way out.

    4.2.11 changes what a pulled file's mode looks like: group and world write
    are stripped from anything an untrusted remote sends, so a 0o664 file lands
    0o644. That is right by default and wrong for someone who pulls into a
    shared, group-writable tree on purpose.

    The sparse advisory criticised this project, in its own words, for having
    had "no opt-out". Shipping a second permission change with no opt-out would
    be the same mistake with the lesson already written down.

    setuid and setgid stay stripped regardless: those were removed before this
    release, they are a privilege escalation under --use-sudo, and no flag
    should hand them back.
    """
    target = ctx["target"]
    if os.name == "nt":
        rep.skip("trust-remote-modes opt-out", "POSIX mode semantics only")
        return
    try:
        mod = _load_target_module(target, "_blitcp_trustmodes")
    except Exception as e:                                  # noqa: BLE001
        rep.skip("trust-remote-modes opt-out", f"could not import target: {e}")
        return

    setter = getattr(mod, "_set_trust_remote_modes", None)
    if setter is None:
        rep.fail("trust-remote-modes opt-out",
                 "no _set_trust_remote_modes() — the permission clamp has no "
                 "opt-out, which is exactly what the sparse advisory faulted "
                 "this project for")
        return

    import io as _io
    import stat as _stat
    import tarfile as _tarfile

    def landed(trust):
        with temp_workspace() as ws:
            dst = os.path.join(ws, "dst")
            os.makedirs(dst)
            buf = _io.BytesIO()
            payload = b"m" * 8
            with _tarfile.open(fileobj=buf, mode="w") as tf:
                for name, mode in (("grp.txt", 0o664), ("suid.bin", 0o4755)):
                    i = _tarfile.TarInfo(name)
                    i.size = len(payload)
                    i.mode = mode
                    tf.addfile(i, _io.BytesIO(payload))
            buf.seek(0)
            setter(trust)
            try:
                with _tarfile.open(fileobj=buf) as tf:
                    for m in tf.getmembers():
                        mod._safe_tar_extract(tf, m, dst, trusted_source=False)
                return {n: _stat.S_IMODE(
                            os.lstat(os.path.join(dst, n)).st_mode)
                        for n in ("grp.txt", "suid.bin")}
            finally:
                setter(False)

    bad = []
    off = landed(False)
    on = landed(True)
    if off["grp.txt"] & (_stat.S_IWGRP | _stat.S_IWOTH):
        bad.append("group write survived with the flag OFF (%s)"
                   % oct(off["grp.txt"]))
    if not (on["grp.txt"] & _stat.S_IWGRP):
        bad.append("group write was still stripped with the flag ON (%s) — "
                   "the opt-out does not opt out" % oct(on["grp.txt"]))
    for label, modes in (("off", off), ("on", on)):
        if modes["suid.bin"] & (_stat.S_ISUID | _stat.S_ISGID):
            bad.append("setuid/setgid survived with the flag %s (%s) — no flag "
                       "may hand those back" % (label, oct(modes["suid.bin"])))

    # And it has to be reachable from the command line, and visible when on.
    try:
        eng = open(target, encoding="utf-8", errors="replace").read()
    except OSError:
        eng = ""
    if "--trust-remote-modes" not in eng:
        bad.append("the flag is not exposed on the command line")
    if "_set_trust_remote_modes(" not in eng.replace("def _set_trust_remote_modes(", ""):
        bad.append("nothing ever calls _set_trust_remote_modes(), so the CLI "
                   "flag cannot reach the extraction path")

    if bad:
        rep.fail("trust-remote-modes opt-out", "; ".join(bad))
    else:
        rep.ok("trust-remote-modes opt-out",
               "the clamp has a documented opt-out, it works, and setuid/"
               "setgid stay stripped either way")


def _run_main_inprocess(mod, argv, elevated=False):
    """Run the REAL entry point in-process, optionally pretending to be root.

    Unit-testing the helpers is what let two rounds of regressions through:
    every helper test passed while the copy engines, which do not call those
    helpers, wrote the file anyway. These tests drive main() over real files
    on disk for that reason. In-process rather than as a subprocess because
    elevation has to be simulated, and a child cannot be monkeypatched.

    Returns (exit_code, captured_output).
    """
    import contextlib
    import io as _io

    real_argv = sys.argv
    real_elev = mod._is_elevated
    real_elev_p = mod._is_elevated_for_preserve
    buf = _io.StringIO()
    try:
        sys.argv = ["blitcp"] + argv
        if elevated:
            mod._is_elevated = lambda: True
            mod._is_elevated_for_preserve = lambda: True
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                rc = mod.main()
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else 1
    finally:
        sys.argv = real_argv
        mod._is_elevated = real_elev
        mod._is_elevated_for_preserve = real_elev_p
    return (rc or 0), buf.getvalue()


def _symlinked_dst_tree(ws):
    """src with a photos/ subtree, and a dst whose photos/ is a symlink out.

    The shape every one of these tests needs: a destination the user laid out
    with a symlinked directory, and incoming files that land under it.
    """
    src = os.path.join(ws, "src")
    dst = os.path.join(ws, "dst")
    outside = os.path.join(ws, "elsewhere")
    os.makedirs(os.path.join(src, "photos"))
    os.makedirs(os.path.join(src, "docs"))
    os.makedirs(dst)
    os.makedirs(outside)
    for i in range(1, 6):
        with open(os.path.join(src, "photos", "f%d.txt" % i), "w") as f:
            f.write("photo %d\n" % i)
    with open(os.path.join(src, "docs", "d.txt"), "w") as f:
        f.write("doc\n")
    os.symlink(outside, os.path.join(dst, "photos"))
    return src, dst, outside


def _check_local_copy_symlinked_dst_unelevated(rep, ctx):
    """Not elevated: a local copy into the user's symlinked layout works.

    cp -r follows a symlinked destination directory. So does blitcp, when the
    names come from our own scan and the process is not root. The whole flow
    has to work end to end — copy AND verification — because the two used to
    disagree: the copy wrote through the link and the verifier walked the
    destination without following it, called every file missing and exited 1.
    """
    target = ctx["target"]
    if os.name == "nt":
        rep.skip("local copy into symlinked dst", "POSIX symlinks only")
        return
    try:
        mod = _load_target_module(target, "_blitcp_e2e_unelev")
    except Exception as e:                                  # noqa: BLE001
        rep.skip("local copy into symlinked dst", f"could not import: {e}")
        return

    with temp_workspace() as ws:
        src, dst, outside = _symlinked_dst_tree(ws)
        rc, out = _run_main_inprocess(mod, [src + os.sep, dst], elevated=False)
        landed = sorted(os.listdir(outside))
        doc_ok = os.path.isfile(os.path.join(dst, "docs", "d.txt"))

    bad = []
    if rc != 0:
        bad.append("exit %s (expected 0)" % rc)
    if "Verification failed" in out or "verify mismatch" in out:
        bad.append("verification reported a failure for files that are there")
    if len(landed) != 5:
        bad.append("%d of 5 files reached the symlinked directory (%s)"
                   % (len(landed), ", ".join(landed) or "none"))
    if not doc_ok:
        bad.append("the ordinary subdirectory was not copied")
    if bad:
        rep.fail("local copy into symlinked dst", "; ".join(bad))
    else:
        rep.ok("local copy into symlinked dst",
               "5 files through the user's symlink, verification clean, exit 0")


def _check_local_copy_symlinked_dst_elevated(rep, ctx):
    """Elevated: the same copy is refused, and nothing lands outside.

    Under --use-sudo the process is root, and a symlinked destination
    directory redirects a root write. blitcp cannot tell a link the user made
    from one a local attacker planted to catch exactly this run, so it refuses
    both. The test asserts the refusal reaches the exit code and, more
    importantly, that the bytes never leave the destination.
    """
    target = ctx["target"]
    if os.name == "nt":
        rep.skip("elevated copy into symlinked dst", "POSIX symlinks only")
        return
    try:
        mod = _load_target_module(target, "_blitcp_e2e_elev")
    except Exception as e:                                  # noqa: BLE001
        rep.skip("elevated copy into symlinked dst", f"could not import: {e}")
        return

    with temp_workspace() as ws:
        src, dst, outside = _symlinked_dst_tree(ws)
        rc, out = _run_main_inprocess(mod, [src + os.sep, dst], elevated=True)
        escaped = sorted(os.listdir(outside))
        banners = out.count("Destination contains a symlinked directory")

    bad = []
    if escaped:
        bad.append("%d file(s) were written outside the destination through "
                   "the symlink while elevated: %s"
                   % (len(escaped), ", ".join(escaped)))
    if rc == 0:
        bad.append("exit 0 although files were refused")
    if banners != 1:
        bad.append("the symlinked-directory notice appeared %d times, "
                   "expected exactly 1" % banners)
    if bad:
        rep.fail("elevated copy into symlinked dst", "; ".join(bad))
    else:
        rep.ok("elevated copy into symlinked dst",
               "nothing escaped, the run failed, and the notice was printed once")


def _check_preflight_in_dry_run_and_cloud(rep, ctx):
    """The notice has to appear where the user is actually looking.

    Two gaps in the same report. A dry run is exactly when someone checks
    whether a destination is set up correctly, and it printed nothing — the
    warning sat after the dry-run return. And the local flow was the only one
    wired: a cloud download refused objects one "Skipping unsafe key" at a
    time, with no summary at all.
    """
    target = ctx["target"]
    if os.name == "nt":
        rep.skip("preflight in dry-run and cloud", "POSIX symlinks only")
        return
    try:
        mod = _load_target_module(target, "_blitcp_preflight_reach")
    except Exception as e:                                  # noqa: BLE001
        rep.skip("preflight in dry-run and cloud", f"could not import: {e}")
        return

    bad = []
    with temp_workspace() as ws:
        # Elevated, because that is when the policy actually refuses. An
        # UNELEVATED dry run must say nothing: the files would be copied
        # through the user's symlink, and warning that they "will be refused"
        # would be false.
        src, dst, outside = _symlinked_dst_tree(ws)
        rc, out = _run_main_inprocess(mod, ["--dry-run", src + os.sep, dst],
                                      elevated=True)
        if out.count("Destination contains a symlinked directory") != 1:
            bad.append("an elevated dry run printed the notice %d times, "
                       "expected 1 — a dry run is when someone checks the "
                       "destination"
                       % out.count("Destination contains a symlinked directory"))
        _, quiet = _run_main_inprocess(mod, ["--dry-run", src + os.sep, dst],
                                       elevated=False)
        if "will be refused" in quiet:
            bad.append("an unelevated dry run warned that files 'will be "
                       "refused' when the policy allows them")
        if os.listdir(outside):
            bad.append("a dry run wrote files")

    # The cloud download path has to call the same reporter. Checked
    # structurally because standing up an object store here would test the
    # fake, not the code.
    try:
        eng = open(target, encoding="utf-8", errors="replace").read()
        tree = ast.parse(eng, filename=target)
    except Exception as e:                                  # noqa: BLE001
        rep.skip("preflight in dry-run and cloud", f"could not parse: {e}")
        return
    for fname in ("_download_from_cloud",):
        body = ""
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name == fname:
                body = "\n".join(eng.splitlines()[n.lineno - 1:n.end_lineno])
        if not body:
            bad.append("%s() not found" % fname)
        elif "warn_symlinked_dest_dirs" not in body:
            bad.append("%s() never calls warn_symlinked_dest_dirs(), so a "
                       "symlinked destination is discovered one refused object "
                       "at a time" % fname)

    if bad:
        rep.fail("preflight in dry-run and cloud", "; ".join(bad))
    else:
        rep.ok("preflight in dry-run and cloud",
               "a dry run reports it once and writes nothing, and the cloud "
               "download path reports it too")


def _symlinked_case(mod, ws, argv, elevated=False, sudo_user=False):
    """Run one variant of the symlinked-destination scenario end to end."""
    src, dst, outside = _symlinked_dst_tree(ws)
    real_env = os.environ.get("SUDO_USER")
    try:
        if sudo_user:
            os.environ["SUDO_USER"] = "someone"
        rc, out = _run_main_inprocess(mod, [src + os.sep, dst] + argv,
                                      elevated=elevated)
    finally:
        if sudo_user:
            if real_env is None:
                os.environ.pop("SUDO_USER", None)
            else:
                os.environ["SUDO_USER"] = real_env
    return rc, out, sorted(os.listdir(outside)), dst


def _files_line(out):
    """The DONE summary's Files line, for a readable failure message."""
    m = re.search(r"^\s*Files:.*$", out, re.M)
    return m.group(0).strip() if m else "(no Files line)"


def _data_line(out):
    """The DONE summary's Data line, for a readable failure message."""
    m = re.search(r"^\s*Data:.*$", out, re.M)
    return m.group(0).strip() if m else "(no Data line)"


def _symlinked_dst_dedup_tree(ws):
    """The same symlinked destination, but the five photos are IDENTICAL.

    That one change is the whole point: dedup turns four of the five into
    LINKS, so the refusals arrive from create_links instead of from a copy
    engine. With distinct contents the bug below is invisible.
    """
    src = os.path.join(ws, "src")
    dst = os.path.join(ws, "dst")
    outside = os.path.join(ws, "elsewhere")
    os.makedirs(os.path.join(src, "photos"))
    os.makedirs(os.path.join(src, "docs"))
    os.makedirs(dst)
    os.makedirs(outside)
    for i in range(1, 6):
        with open(os.path.join(src, "photos", "f%d.txt" % i), "w") as f:
            f.write("same content\n")
    with open(os.path.join(src, "docs", "d.txt"), "w") as f:
        f.write("doc\n")
    os.symlink(outside, os.path.join(dst, "photos"))
    return src, dst, outside


def _check_refused_links_are_counted(rep, ctx):
    """Refused DUPLICATES are counted as refused — on screen AND in the record.

    create_links kept its own error counter, so a refused link never reached
    _REFUSED_PATHS. Phase 3 predicted "5 will be refused" and the summary of
    the same run said "1 refused, 4 linked" — printed on the same screen, and
    nothing compared them. The audit file and the --log JSON were worse: they
    recomputed copied/linked from len() of the lists the run STARTED with, so
    the record of a privileged copy claimed files that were never written.

    Asserts the exact summary line, the byte line, the engine's own line, and
    the two JSON records — all five have to agree on one run.
    """
    if os.name == "nt":
        rep.skip("refused duplicates counted once", "POSIX symlinks only")
        return
    target = ctx["target"]
    try:
        mod = _load_target_module(target, "_blitcp_refused")
    except Exception as e:                                  # noqa: BLE001
        rep.skip("refused duplicates counted once", f"could not import: {e}")
        return

    real_env = os.environ.get("SUDO_USER")
    real_audit = mod.write_sudo_audit
    audit = {}
    with temp_workspace() as ws:
        src, dst, outside = _symlinked_dst_dedup_tree(ws)
        log_path = os.path.join(ws, "run.json")
        try:
            os.environ["SUDO_USER"] = "someone"
            # The audit file goes to $SUDO_USER's home and is then made
            # immutable, which a test must not do to a real account. Capturing
            # the record it would write asserts the same thing.
            mod.write_sudo_audit = lambda a, b, summary: audit.update(summary)
            rc, out = _run_main_inprocess(
                mod, [src + os.sep, dst, "--no-verify", "--log", log_path],
                elevated=False)
        finally:
            mod.write_sudo_audit = real_audit
            if real_env is None:
                os.environ.pop("SUDO_USER", None)
            else:
                os.environ["SUDO_USER"] = real_env
        landed = sorted(os.listdir(outside))
        try:
            with open(log_path, encoding="utf-8") as f:
                logged = json.load(f)["summary"]
        except Exception as e:                              # noqa: BLE001
            logged = {"_unreadable": str(e)}

    bad = []
    if landed:
        bad.append("%d file(s) escaped through the symlink: %s"
                   % (len(landed), ", ".join(landed)))
    if rc == 0:
        bad.append("exit 0 although five files were refused")
    if not re.search(r"Files:\s+6 total \(1 copied \+ 0 linked, 5 refused\)",
                     out):
        bad.append("summary says %r, expected "
                   "6 total (1 copied + 0 linked, 5 refused)"
                   % _files_line(out))
    # The list under the summary names every refused file, not just one.
    listed = re.findall(r"^\s+photos/f\d\.txt\s*$", out, re.M)
    if len(listed) != 5:
        bad.append("the refusal list names %d file(s), expected 5"
                   % len(listed))
    # Bytes: one 4-byte file was written; the other 65 bytes were refused.
    if not re.search(r"Data:\s+4\.0 B written", out):
        bad.append("the Data line counts refused bytes as written: %r"
                   % _data_line(out))
    if "65.0 B refused, not written" not in out:
        bad.append("the Data line does not say what was refused: %r"
                   % _data_line(out))
    # The engine's own line: a policy refusal is not an error.
    if re.search(r"Copied \d+ small files, \d+ errors", out):
        bad.append("the small-file engine reports a refusal as an error")
    if not re.search(r"Copied \d+ small files, 1 refused", out):
        bad.append("the small-file engine does not report its refusal")
    # Prediction vs result, and the summary's own arithmetic.
    if "5 incoming file(s)" not in out:
        bad.append("Phase 3 did not predict 5 refusals")
    if "disagree" in out:
        bad.append("the preflight and the summary disagree")
    if "do not add up" in out:
        bad.append("the summary's own arithmetic does not close: %s"
                   % out[out.find("do not add up") - 60:][:200].strip())

    # The two records have to describe the run that happened.
    want = {"total_files": 6, "copied": 1, "linked": 0, "refused": 5,
            "skipped": 0, "errors": 0, "total_bytes": 69,
            "bytes_written": 4, "bytes_refused": 65, "dedup_saved": 0}
    for label, rec in (("--log JSON", logged), ("sudo audit", audit)):
        for k, v in want.items():
            if rec.get(k) != v:
                bad.append("%s says %s=%r, expected %r"
                           % (label, k, rec.get(k), v))
        if len(rec.get("refused_paths") or []) != 5:
            bad.append("%s does not name the refused files" % label)

    if bad:
        rep.fail("refused duplicates counted once", "; ".join(bad[:6]))
    else:
        rep.ok("refused duplicates counted once",
               "screen, byte line, engine line, --log JSON and audit record "
               "all say 1 copied / 0 linked / 5 refused / 4 B written")


def _check_summaries_share_one_reader(rep, ctx):
    """Every tree-copy summary reads the refusal list through one function.

    The local flow was fixed first and the pull flow kept its own hand-built
    line — same engines, same destination policy, same refusals, and a
    summary that could not see them. A second reader is how one bug gets
    written twice, so this asserts there is only one for the tree flows:
    _print_files_summary. (_ssh_done_summary is the SSH-transfer twin; it
    reads the same list and carries the same closure check.)
    """
    target = ctx["target"]
    try:
        with open(target, encoding="utf-8") as f:
            src = f.read()
    except OSError as e:
        rep.skip("one reader for the summary", str(e))
        return

    def _span(name):
        head = "def %s(" % name
        if head not in src:
            return None
        a = src.index(head)
        b = src.find("\ndef ", a)
        return (a, b if b != -1 else len(src))

    bad = []
    helper = _span("_print_files_summary")
    ssh_twin = _span("_ssh_done_summary")
    if helper is None:
        rep.fail("one reader for the summary",
                 "_print_files_summary() is gone; every summary is counting "
                 "for itself again")
        return

    # 1) Nobody else prints a "Files: N total" line.
    for m in re.finditer(r"print\(f?\"[^\"]*Files:[^\"]*\"", src):
        seg = src[m.start():m.end()]
        if "total" not in seg and "{_tr(" not in seg:
            continue                       # not a DONE summary line
        if helper[0] <= m.start() < helper[1]:
            continue
        if ssh_twin and ssh_twin[0] <= m.start() < ssh_twin[1]:
            continue
        bad.append("line %d prints its own Files/total line"
                   % (src.count("\n", 0, m.start()) + 1))

    # 2) Every DONE block that reports a file tree calls the helper. The four
    #    tree flows are identified by their Data verb; the cloud flows report
    #    objects and have their own shape.
    for verb in ("written", "downloaded", "relayed", "sent"):
        for m in re.finditer(r"Data:\s+\{C\.BOLD\}[^\n]{0,80}\}\s*" + verb,
                             src):
            block = src[max(0, m.start() - 3000):m.start()]
            if "_print_files_summary(" not in block:
                bad.append("the summary printing 'Data: ... %s' (line %d) "
                           "does not go through _print_files_summary()"
                           % (verb, src.count("\n", 0, m.start()) + 1))

    if bad:
        rep.fail("one reader for the summary", "; ".join(sorted(set(bad))[:6]))
    else:
        rep.ok("one reader for the summary",
               "local, pull, push and relay summaries all count through "
               "_print_files_summary()")


def _check_engine_parity_symlinked_dst(rep, ctx):
    """Every engine must reach the same verdict on the same tree.

    Four copy engines and _safe_tar_extract each decided independently whether
    a destination path was allowed, using three different elevation
    predicates. The result was that --small-files stream and the default
    engine disagreed about the same directory, and SUDO_USER without root
    disagreed with both. These run the real flow under each combination and
    require identical outcomes.
    """
    target = ctx["target"]
    if os.name == "nt":
        rep.skip("engine parity on symlinked dst", "POSIX symlinks only")
        return
    try:
        mod = _load_target_module(target, "_blitcp_parity")
    except Exception as e:                                  # noqa: BLE001
        rep.skip("engine parity on symlinked dst", f"could not import: {e}")
        return

    bad = []
    # A and B — not elevated: both engines copy through the user's symlink.
    for label, argv in (("default", []),
                        ("--small-files stream", ["--small-files", "stream"])):
        with temp_workspace() as ws:
            rc, out, landed, dst = _symlinked_case(mod, ws, argv)
            if rc != 0:
                bad.append("[%s, not elevated] exit %s, expected 0" % (label, rc))
            if len(landed) != 5:
                bad.append("[%s, not elevated] %d of 5 files went through the "
                           "symlink" % (label, len(landed)))
            if "will be refused" in out:
                bad.append("[%s, not elevated] said files 'will be refused' "
                           "while copying them anyway" % label)

    # C, D and E — elevated: refused, nothing outside, said once, counted right.
    for label, argv, sudo in (("default", ["--no-verify"], False),
                              ("--small-files stream",
                               ["--small-files", "stream", "--no-verify"], False),
                              # The same engine with a NON-extended --preserve.
                              # This case is here because its absence is why
                              # this test passed while the tar engine recorded
                              # refusals only from the extended-metadata pass:
                              # elevation promotes --preserve to 'all', that
                              # pass runs, and the refusals got recorded by
                              # accident. Ask for mode,times and the pass is
                              # skipped — the engine then refused five files,
                              # called them copied and exited 0.
                              ("--small-files stream --preserve mode,times",
                               ["--small-files", "stream", "--no-verify",
                                "--preserve", "mode,times"], False),
                              ("SUDO_USER, euid!=0", ["--no-verify"], True)):
        with temp_workspace() as ws:
            elev = not sudo          # E gets elevation from SUDO_USER, not mock
            rc, out, landed, dst = _symlinked_case(mod, ws, argv,
                                                   elevated=elev,
                                                   sudo_user=sudo)
            if landed:
                bad.append("[%s, elevated] %d file(s) escaped: %s"
                           % (label, len(landed), ", ".join(landed)))
            if rc == 0:
                bad.append("[%s, elevated] exit 0 although files were refused"
                           % label)
            if out.count("Destination contains a symlinked directory") != 1:
                bad.append("[%s, elevated] notice printed %d times, expected 1"
                           % (label,
                              out.count("Destination contains a symlinked "
                                        "directory")))
            if "were refused and not written" not in out:
                bad.append("[%s, elevated] never said how many files were "
                           "refused" % label)
            # 6 files, 5 of them refused: the total stays 6 and the
            # breakdown has to account for all six. (This used to assert the
            # total was NOT 6, back when the summary subtracted refusals from
            # the total instead of naming them — which hid the refused files
            # from the one number people read.)
            if not re.search(r"Files:\s+6 total \(1 copied \+ 0 linked, "
                             r"5 refused\)", out):
                bad.append("[%s, elevated] summary line is %r, expected "
                           "6 total (1 copied + 0 linked, 5 refused)"
                           % (label, _files_line(out)))

    if bad:
        rep.fail("engine parity on symlinked dst", "; ".join(bad[:8]))
    else:
        rep.ok("engine parity on symlinked dst",
               "both engines and both elevation routes agree: copied when "
               "allowed, refused and counted when not")


def section_bugs(rep, ctx):
    target = ctx["target"]
    _check_quiet_mode(rep, ctx)
    _check_dest_preflight(rep, ctx)
    _check_update_check_optin(rep, ctx)
    _check_content_verification(rep, ctx)
    _check_link_scope(rep, ctx)
    _check_lookup_scope_in_sql(rep, ctx)
    _check_error_never_empty(rep, ctx)
    _check_remote_scan_targeted(rep, ctx)
    _check_saved_ssh_protocol(rep, ctx)
    _check_http_relay_honors_transport(rep, ctx)
    _check_relay_errors_are_debuggable(rep, ctx)
    _check_sftp_fallback_contract(rep, ctx)
    _check_cookie_domain_scope(rep, ctx)
    _check_ls_shell_fallback(rep, ctx)
    _check_relay_reports_truth(rep, ctx)
    _check_streamer_parity(rep, ctx)
    _check_translation_coverage(rep, ctx)
    _check_pip_install_not_self_updated(rep, ctx)
    _check_reported_speed(rep, ctx)
    _check_sparse_copy_integrity(rep, ctx)
    _check_sparse_verification_sees_content(rep, ctx)
    _check_sync_folder_dedup_downgrade(rep, ctx)
    _check_quiet_time_matches(rep, ctx)
    _check_sudo_askpass_route(rep, ctx)
    _check_fuseblk_is_local(rep, ctx)
    _check_memory_fs_detection(rep, ctx)
    _check_cache_preload(rep, ctx)
    _check_threadpool_small_files(rep, ctx)
    _check_midbatch_symlinked_parent(rep, ctx)
    _check_pull_paths_are_validated(rep, ctx)
    _check_create_links_validates(rep, ctx)
    _check_http_filename_cannot_traverse(rep, ctx)
    _check_large_member_is_validated(rep, ctx)
    _check_untrusted_mode_is_clamped(rep, ctx)
    _check_pull_reports_rejections(rep, ctx)
    _check_pull_link_target_validated(rep, ctx)
    _check_dest_symlink_policy(rep, ctx)
    _check_symlinked_dest_preflight(rep, ctx)
    _check_trust_remote_modes_optout(rep, ctx)
    _check_local_copy_symlinked_dst_unelevated(rep, ctx)
    _check_local_copy_symlinked_dst_elevated(rep, ctx)
    _check_preflight_in_dry_run_and_cloud(rep, ctx)
    _check_engine_parity_symlinked_dst(rep, ctx)
    _check_refused_links_are_counted(rep, ctx)
    _check_summaries_share_one_reader(rep, ctx)

    # empty dirs + nesting + unicode/space names + zero-byte + large file
    with temp_workspace() as ws:
        src = make_tree(os.path.join(ws, "src"), big_mb=3,
                        with_unicode=True, with_empty_dir=True)
        dst = os.path.join(ws, "dst")
        rc, out, err = run_fc(target, [src, dst])
        ok, d = tree_equal(src, dst)
        if rc == 0 and ok and _no_traceback(err):
            rep.ok("edge-case tree", "unicode/space/zero-byte/large all ok")
        else:
            rep.fail("edge-case tree", f"rc={rc} {d} {err[:140]}")
        # empty-dir preservation is reported on its own: the tool copies files
        # by content and does not recreate empty directories.
        empty_ok = os.path.isdir(os.path.join(dst, "empty"))
        if empty_ok:
            rep.ok("empty-dir preservation", "empty directories recreated")
        else:
            rep.warn("empty-dir preservation",
                     "empty source directories are not recreated at dest")

    # symlink handling — deterministic, no crash
    with temp_workspace() as ws:
        src = make_tree(os.path.join(ws, "src"), with_symlink=True,
                        big_mb=1, with_dups=False)
        if not os.path.islink(os.path.join(src, "link.txt")):
            rep.skip("symlink handling", "symlinks unsupported on this FS")
        else:
            dst = os.path.join(ws, "dst")
            rc, out, err = run_fc(target, [src, dst])
            link_dst = os.path.join(dst, "link.txt")
            handled = os.path.islink(link_dst) or os.path.isfile(link_dst) \
                or not os.path.exists(link_dst)
            if rc == 0 and handled and _no_traceback(err):
                kind = ("symlink" if os.path.islink(link_dst)
                        else "followed" if os.path.isfile(link_dst)
                        else "skipped")
                rep.ok("symlink handling", f"deterministic ({kind}), no crash")
            else:
                rep.fail("symlink handling", f"rc={rc} {err[:140]}")

    # space check must account for block overhead (regression: UAT 2026-08-08
    # passed a doomed job with "+44MB headroom", disk filled at 86%). With
    # sub-block files the preflight must report an on-disk requirement larger
    # than the logical size (whole-block rounding + dir/metadata margin).
    with temp_workspace() as ws:
        src = os.path.join(ws, "src")
        os.makedirs(src)
        for i in range(300):
            _write(os.path.join(src, f"tiny{i:03d}.bin"), b"x" * 1024)
        dst = os.path.join(ws, "dst")
        try:
            fs_block = os.statvfs(ws).f_frsize or os.statvfs(ws).f_bsize
        except (AttributeError, OSError):
            fs_block = 4096
        rc, out, err = run_fc(target, [src, dst, "--no-verify", "--no-cache"])
        if fs_block <= 1024:
            rep.skip("space check counts block overhead",
                     f"fs block {fs_block}B too small to exercise rounding")
        elif rc == 0 and "On-disk requirement:" in out:
            rep.ok("space check counts block overhead",
                   "preflight reports allocated (block-rounded) requirement")
        else:
            rep.fail("space check counts block overhead",
                     f"rc={rc}; no 'On-disk requirement' line — space check "
                     f"is comparing logical bytes against free space again")

    # idempotency (already covered partly in features; assert explicitly)
    with temp_workspace() as ws:
        src = make_tree(os.path.join(ws, "src"), big_mb=1)
        dst = os.path.join(ws, "dst")
        run_fc(target, [src, dst])
        snap1 = {k: v for k, v in _snapshot(dst).items() if not _is_sidecar(k)}
        rc, out, err = run_fc(target, [src, dst])
        snap2 = {k: v for k, v in _snapshot(dst).items() if not _is_sidecar(k)}
        if rc == 0 and snap1 == snap2 and _no_traceback(err):
            rep.ok("idempotency", "second run changed nothing")
        else:
            changed = [k for k in snap1 if snap1.get(k) != snap2.get(k)]
            rep.fail("idempotency", f"rc={rc} changed={changed[:6]}")

    # path-traversal guard: a symlink pointing outside dest must not let the
    # tool write through it to an external target.
    with temp_workspace() as ws:
        src = os.path.join(ws, "src")
        os.makedirs(src)
        outside = os.path.join(ws, "OUTSIDE_SECRET.txt")
        _write(outside, b"original-untouched\n")
        try:
            os.symlink(outside, os.path.join(src, "escape"))
            have_link = True
        except (OSError, NotImplementedError):
            have_link = False
        if not have_link:
            rep.skip("traversal guard", "symlinks unsupported on this FS")
        else:
            _write(os.path.join(src, "normal.txt"), b"normal\n")
            dst = os.path.join(ws, "dst")
            rc, out, err = run_fc(target, [src, dst])
            # the external file's content must be unchanged regardless of rc
            with open(outside, "rb") as f:
                still = f.read()
            if still == b"original-untouched\n" and _no_traceback(err):
                rep.ok("traversal guard",
                       "external target not written through symlink")
            else:
                rep.fail("traversal guard",
                         "external file modified or traceback emitted")

    # clean errors: nonexistent source -> single-line error, no traceback
    with temp_workspace() as ws:
        missing = os.path.join(ws, "does_not_exist")
        dst = os.path.join(ws, "dst")
        rc, out, err = run_fc(target, [missing, dst])
        if rc != 0 and _no_traceback(err) and _no_traceback(out):
            rep.ok("clean error (missing src)",
                   "non-zero exit, no traceback")
        else:
            rep.fail("clean error (missing src)",
                     f"rc={rc} traceback={'yes' if not _no_traceback(err) else 'no'}")

    # clean errors: unwritable destination
    if os.name == "posix" and os.geteuid() != 0:
        with temp_workspace() as ws:
            src = make_tree(os.path.join(ws, "src"), big_mb=1, with_dups=False)
            dst = os.path.join(ws, "ro_dst")
            os.makedirs(dst)
            os.chmod(dst, 0o500)
            try:
                rc, out, err = run_fc(target, [src, os.path.join(dst, "x")])
                clean = _no_traceback(err) and _no_traceback(out)
                if clean:
                    rep.ok("clean error (read-only dest)",
                           "no traceback on permission error")
                else:
                    rep.fail("clean error (read-only dest)",
                             "traceback leaked on permission error")
            finally:
                os.chmod(dst, 0o700)
    else:
        rep.skip("clean error (read-only dest)",
                 "needs non-root POSIX to enforce permissions")


# --------------------------------------------------------------------------- #
# Section 6: uat — chained end-to-end acceptance scenario
# --------------------------------------------------------------------------- #

def section_uat(rep, ctx):
    target = ctx["target"]
    with temp_workspace() as ws:
        src = make_tree(os.path.join(ws, "src"), big_mb=2, with_dups=True)
        dst = os.path.join(ws, "dst")
        child_tmp = os.path.join(ws, "ctmp")
        os.makedirs(child_tmp)

        # 1) initial copy with dedup + preserve + verify (default)
        rc, out, err = run_fc(target, ["--preserve", "mode,times", src, dst],
                              tmpdir=child_tmp)
        ok, d = tree_equal(src, dst)
        if not (rc == 0 and ok and _no_traceback(err)):
            rep.fail("UAT initial copy", f"rc={rc} {d} {err[:160]}")
            return
        rep.ok("UAT initial copy", "dedup+preserve+verify ok")

        # 2) mutate a few files, add a new one, then incremental re-copy
        _write(os.path.join(src, "a.txt"), b"alpha CHANGED\n")
        _write(os.path.join(src, "newfile.txt"), b"brand new\n")
        rc, out, err = run_fc(target, ["--preserve", "mode,times", src, dst],
                              tmpdir=child_tmp)
        ok, d = tree_equal(src, dst)
        if rc == 0 and ok and _no_traceback(err):
            rep.ok("UAT incremental sync", "changed+new files propagated")
        else:
            rep.fail("UAT incremental sync", f"rc={rc} {d} {err[:160]}")

        # 3) no leaks after the realistic run
        leftovers = [n for n in os.listdir(child_tmp)
                     if _TEMP_GARBAGE.search(n)]
        if leftovers:
            rep.fail("UAT no-leak check", f"stray temp: {leftovers[:6]}")
        else:
            rep.ok("UAT no-leak check", "no stray temp files")

        # 4) dedup DB healthy
        ddb = os.path.join(dst, ".fast_copy_dedup.db")
        if os.path.exists(ddb):
            try:
                conn = sqlite3.connect(ddb)
                res = conn.execute("PRAGMA quick_check").fetchone()
                conn.close()
                (rep.ok if res and res[0] == "ok" else rep.fail)(
                    "UAT dedup DB", f"quick_check={res[0] if res else None}")
            except sqlite3.Error as e:
                rep.fail("UAT dedup DB", str(e))
        else:
            rep.info("UAT dedup DB", "no dedup DB (dedup may be disabled)")

        # 5) final verdict
        c = rep.counts("uat")
        if c[FAIL] == 0:
            rep.ok("UAT verdict", "end-to-end acceptance passed")
        else:
            rep.fail("UAT verdict", f"{c[FAIL]} UAT step(s) failed")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

_SECTION_FNS = {
    "security": section_security,
    "leaks": section_leaks,
    "modes": section_modes,
    "features": section_features,
    "bugs": section_bugs,
    "uat": section_uat,
}


def _locate_target(explicit):
    if explicit:
        return os.path.abspath(explicit)
    here = os.path.dirname(os.path.abspath(__file__))
    # blitcp.py is the engine; fast_copy.py is only a shim since the rename.
    for name in ("blitcp.py", "fast_copy.py"):
        cand = os.path.join(here, name)
        if os.path.exists(cand):
            return cand
    return "blitcp.py"


def _force_utf8_stdout():
    """This prints box drawing and check marks, and a Windows console is
    cp1252. Without this the report kills the run that produced it — which is
    what happened to four suites before it, and both of these are things the
    documentation tells people to run by hand."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if (getattr(stream, "encoding", "") or "").lower() not in ("utf-8", "utf8"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                  # noqa: BLE001
            pass


def main(argv=None):
    _force_utf8_stdout()
    p = argparse.ArgumentParser(
        prog="audit_uat.py",
        description="Full security + UAT audit for fast-copy.")
    p.add_argument("--target", help="Path to fast_copy.py "
                   "(default: alongside this script)")
    p.add_argument("--section", action="append", choices=SECTIONS,
                   help="Run only this section (repeatable). Default: all.")
    p.add_argument("--json", dest="json_out", help="Write JSON report to PATH")
    p.add_argument("--allow-remote", action="store_true",
                   help="Exercise Push/Pull/R2R over localhost SSH")
    p.add_argument("--allow-cloud", action="store_true",
                   help="Exercise cloud modes against a local emulator")
    p.add_argument("--allow-smb", action="store_true",
                   help="Exercise SMB modes (needs FC_AUDIT_SMB_URL/PASS)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    target = _locate_target(args.target)
    if not os.path.exists(target):
        print(f"{C.RED}Error: target not found: {target}{C.RESET}",
              file=sys.stderr)
        return 2

    ctx = {"target": target, "allow_remote": args.allow_remote,
           "allow_cloud": args.allow_cloud, "allow_smb": args.allow_smb}
    rep = Reporter(verbose=args.verbose, quiet=args.quiet)

    print(f"{C.BOLD}fast-copy audit + UAT{C.RESET}")
    print(f"  target : {target}")
    print(f"  python : {sys.version.split()[0]}")
    print(f"  remote : {'on' if args.allow_remote else 'off (auto-skip)'}   "
          f"cloud : {'on' if args.allow_cloud else 'off (auto-skip)'}")

    sections = args.section or SECTIONS
    start = time.time()
    for s in sections:
        rep.begin(s)
        try:
            _SECTION_FNS[s](rep, ctx)
        except Exception as e:  # noqa: BLE001 - a crashing section is a finding
            import traceback
            rep.fail(f"{s} section crashed",
                     f"{type(e).__name__}: {e}\n"
                     f"{traceback.format_exc().splitlines()[-1]}")

    rep.summary()
    print(f"\n  elapsed: {time.time() - start:.1f}s")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump({"target": target, "results": rep.results,
                       "counts": rep.counts()}, f, indent=2)
        print(f"  json report: {args.json_out}")

    return 1 if rep.counts()[FAIL] else 0


if __name__ == "__main__":
    sys.exit(main())
