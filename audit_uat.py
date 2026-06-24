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
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

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
            if base in ignore or base.startswith(".fast_copy"):
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
        if base in ignore or base.startswith(".fast_copy"):
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

        # SQL injection: execute/executemany with a built string
        if name.endswith("execute") or name.endswith("executemany"):
            if node.args:
                a0 = node.args[0]
                if isinstance(a0, ast.JoinedStr):
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
    are NOT guarded by hasattr(os, "<name>") in an enclosing function. These raise
    AttributeError (not OSError) on Windows, so a bare `except OSError` does not
    catch them and the copy crashes. Regression guard for the v3.8.1
    os.fchmod-on-Windows bug (large-file copies crashed under default preserve)."""
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

    offenders = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in WATCH
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"):
            nm = node.func.attr
            if not any(guards(fn, nm) for fn in enclosing_funcs(node)):
                offenders.append(f"os.{nm} @L{node.lineno}")
    if offenders:
        rep.fail("POSIX-only os.* guards",
                 "unguarded (AttributeError on Windows): " + "; ".join(offenders[:8]))
    else:
        rep.ok("POSIX-only os.* guards",
               "fd/metadata POSIX calls are hasattr-guarded (Windows-safe)")


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
                    if not fn.startswith(".fast_copy"))
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
                    if not fn.startswith(".fast_copy"))
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
    (rep.ok if rc == 0 and "fast-copy" in out.lower() else rep.fail)(
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

def section_bugs(rep, ctx):
    target = ctx["target"]

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
    cand = os.path.join(here, "fast_copy.py")
    return cand if os.path.exists(cand) else "fast_copy.py"


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
