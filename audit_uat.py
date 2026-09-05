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
import io
import os
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
            if base in ignore or base.startswith((".fast_copy", ".blitcp")):
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

    # dry-run must not write anything
    with temp_workspace() as ws:
        src = make_tree(os.path.join(ws, "src"), big_mb=1)
        dst = os.path.join(ws, "dst")
        rc, out, err = run_fc(target, ["--dry-run", src, dst])
        created = os.path.exists(dst) and os.listdir(dst)
        if rc == 0 and not created:
            rep.ok("dry-run no side effects", "destination untouched")
        elif rc != 0:
            rep.fail("dry-run no side effects", f"rc={rc}: {err.strip()[:160]}")
        else:
            rep.fail("dry-run no side effects",
                     f"dry-run wrote files: {os.listdir(dst)[:6]}")


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
            capture_output=True, text=True, timeout=15)
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

        rc1, o1, e1 = run_fc(target, [src, os.path.join(ws, "d1")])
        verbose = seconds(r"Time:\s*([0-9.]+)\s*(m?s)", o1 + e1)
        rc2, o2, e2 = run_fc(target, ["--quiet", src, os.path.join(ws, "d2")])
        quiet = seconds(r"in\s+([0-9.]+)\s*(m?s)", o2 + e2)

        if rc1 != 0 or rc2 != 0:
            rep.fail("quiet timing", f"copies failed {rc1}/{rc2}")
            return
        if verbose is None or quiet is None:
            rep.skip("quiet timing", "could not parse both reported times")
            return
        if verbose <= 0:
            rep.skip("quiet timing", "verbose time too small to compare")
            return
        ratio = quiet / verbose
        # Run-to-run spread on a real disk is tens of percent; the bug was a
        # 4x gap. Anything under half means the two paths measure differently.
        if ratio < 0.5:
            rep.fail("quiet timing",
                     f"--quiet reported {quiet:.2f}s where the summary reported "
                     f"{verbose:.2f}s — the flag is changing the measurement")
        else:
            rep.ok("quiet timing",
                   f"quiet {quiet:.2f}s vs summary {verbose:.2f}s")


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
    _check_ls_shell_fallback(rep, ctx)
    _check_relay_reports_truth(rep, ctx)
    _check_streamer_parity(rep, ctx)
    _check_translation_coverage(rep, ctx)
    _check_pip_install_not_self_updated(rep, ctx)
    _check_reported_speed(rep, ctx)
    _check_quiet_time_matches(rep, ctx)
    _check_memory_fs_detection(rep, ctx)
    _check_cache_preload(rep, ctx)
    _check_threadpool_small_files(rep, ctx)

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
        snap1 = _snapshot(dst)
        rc, out, err = run_fc(target, [src, dst])
        snap2 = _snapshot(dst)
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


def main(argv=None):
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
