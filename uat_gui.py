#!/usr/bin/env python3
# Copyright 2026 George Kapellakis
# Licensed under the Apache License, Version 2.0
"""User-Acceptance Tests for the blitcp GUI (blitcp_gui.py).

Exercises the GUI's logic headless via offscreen Qt — no display needed. Covers
the connection manager (every backend incl. SMB credentials), required-field
validation, the auto-sized connection dialog, CLI argv assembly from the
transfer form (every toggle/flag), the startup update popup (notes rendering +
skip persistence + button actions), settings persistence, saved-transfer config
snapshots, path composition, and platform asset naming.

  python uat_gui.py            # run all, assert, exit 1 on any FAIL
  python uat_gui.py --list

Companion to uat_blitcp.py (which black-box tests the CLI surface).
"""
import os
import sys
import subprocess
import tempfile
import argparse

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="uat_gui_cfg_")
os.environ["NO_COLOR"] = os.environ.get("NO_COLOR", "")

# i18n guard (I18N_DESIGN.md, M0): assertions expect English UI strings.
# Must be set BEFORE the GUI module import — gettext will read the locale
# at import/bootstrap time once i18n lands.
os.environ["LC_ALL"] = "C"
os.environ["LANG"] = "C"
os.environ.pop("LANGUAGE", None)
os.environ.pop("BLITCP_LANG", None)

try:                                                 # noqa: E402
    from PySide6.QtWidgets import QApplication       # noqa: E402
except ImportError as _qt_err:                       # noqa: E402
    # This suite drives blitcp_gui.py through Qt's offscreen platform plugin,
    # so it needs PySide6 but neither a display nor xvfb. Without the package
    # there is simply nothing to drive. Exiting 0 with a summary uat.py can
    # parse beats a traceback that leaves the aggregate run reporting
    # "GUI - no summary (exit 1)" and a red verdict for a missing optional dep.
    print("UAT - blitcp GUI")
    print(f"  SKIP  every scenario - PySide6 not installed ({_qt_err})")
    print("\n GUI UAT SKIPPED - 0 pass, 0 fail, 1 skip")
    sys.exit(0)
import blitcp_gui as g                            # noqa: E402


class C:
    if sys.stdout.isatty() and not os.environ.get("NO_COLOR"):
        B = "\033[1m"; R = "\033[31m"; G = "\033[32m"; Y = "\033[33m"; X = "\033[0m"; GREY = "\033[90m"
    else:
        B = R = G = Y = X = GREY = ""


def _types():
    return [v for v, _ in g.TYPE_LABELS]


# ── Connection manager (credential saving) ───────────────────────────────────

def s_conn_s3(w):
    d = g.ConnectionDialog(w, "", {"type": "s3"})
    d.f_name.setText("aws")
    d.fields[("s3", "access_key_id")].setText("AKIAEXAMPLE")
    d.fields[("s3", "secret_access_key")].setText("secret")
    d.fields[("s3", "region")].setText("eu-west-1")
    d.fields[("s3", "container")].setText("bucket")
    name, e = d.result_data()
    ok = (name == "aws" and e.get("type") == "s3"
          and e.get("access_key_id") == "AKIAEXAMPLE"
          and e.get("secret_access_key") == "secret"
          and e.get("region") == "eu-west-1" and e.get("container") == "bucket")
    return ok, "S3 key/secret/region/bucket saved" if ok else f"got {e}"


def s_conn_ssh(w):
    d = g.ConnectionDialog(w, "", {"type": "ssh"})
    d.f_type.setCurrentIndex(_types().index("ssh")); d._sync_type()
    d.f_name.setText("srv")
    d.fields[("ssh", "host")].setText("box")
    d.fields[("ssh", "user")].setText("deploy")
    d.fields[("ssh", "port")].setText("2222")
    d.fields[("ssh", "password")].setText("pw")
    d.fields[("ssh", "path")].setText("/data")
    name, e = d.result_data()
    ok = (name == "srv" and e.get("host") == "box" and e.get("user") == "deploy"
          and e.get("port") == 2222 and e.get("password") == "pw" and e.get("path") == "/data")
    return ok, "SSH host/user/port/password/path saved" if ok else f"got {e}"


def s_conn_smb(w):
    d = g.ConnectionDialog(w, "", {"type": "smb"})
    d.f_type.setCurrentIndex(_types().index("smb")); d._sync_type()
    d.f_name.setText("win")
    for k, v in [("host", "192.168.1.225"), ("user", "g.kapellakis@infinitum.gr"),
                 ("password", "pw"), ("domain", ""), ("share", "Documents"), ("port", "445")]:
        d.fields[("smb", k)].setText(v)
    name, e = d.result_data()
    ok = (name == "win" and e.get("type") == "smb" and e.get("host") == "192.168.1.225"
          and e.get("user") == "g.kapellakis@infinitum.gr" and e.get("password") == "pw"
          and e.get("share") == "Documents" and e.get("port") == 445)
    return ok, "SMB host/user/password/share/port saved" if ok else f"got {e}"


def s_conn_validation(w):
    # SMB requires a host
    d = g.ConnectionDialog(w, "", {"type": "smb"})
    d.f_type.setCurrentIndex(_types().index("smb")); d._sync_type()
    d.f_name.setText("x")
    n1, _ = d.result_data()
    # S3 requires key + secret
    d2 = g.ConnectionDialog(w, "", {"type": "s3"})
    d2.f_name.setText("y")
    n2, _ = d2.result_data()
    # missing name
    d3 = g.ConnectionDialog(w, "", {"type": "s3"})
    n3, _ = d3.result_data()
    ok = n1 is None and n2 is None and n3 is None
    return ok, "rejects missing host / keys / name" if ok else "validation too lax"


def s_dialog_autosize(w):
    d = g.ConnectionDialog(w, "", None)
    d.f_type.setCurrentIndex(_types().index("smb")); d._sync_type()
    h_smb, need = d.height(), d.body.sizeHint().height()
    d.f_type.setCurrentIndex(_types().index("gs")); d._sync_type()
    h_gs = d.height()
    if h_smb + 8 < need:
        return False, f"SMB dialog too short ({h_smb}px < content {need}px) — buttons hidden"
    if not h_smb > h_gs:
        return False, f"SMB({h_smb}) not taller than GS({h_gs}) — not field-counting"
    return True, f"auto-height: smb={h_smb}px > gs={h_gs}px, buttons fit"


# ── CLI argv assembly from the transfer form ─────────────────────────────────

def _reset_opts(w):
    w.transfer_opts["dedup"].setProperty("active", True)
    w.transfer_opts["verify"].setProperty("active", True)
    for o in w.flag_opts.values():
        o.setProperty("active", False)
    for o in w.meta_opts.values():
        o.setProperty("active", o is w.meta_opts["mode"] or o is w.meta_opts["times"])
    w.exclude_patterns = []
    w.index_existing_paths = []
    w.hash_sel.setCurrentText("xxh128")


def s_argv_defaults(w):
    _reset_opts(w)
    argv = w._build_argv(False, ["/src"], "/dst")
    # threads defaults to auto since v4.0.2 → NO --threads flag by default
    # (the engine derives the count from the CPU).
    ok = ("/src" in argv and "/dst" in argv and "--progress-json" in argv
          and "--threads" not in argv and "--preserve" in argv
          and "--no-dedup" not in argv)
    return ok, "base argv (src/dst/threads auto/preserve, dedup on)" if ok else f"got {argv}"


def s_argv_toggles(w):
    _reset_opts(w)
    w.transfer_opts["dedup"].setProperty("active", False)
    w.transfer_opts["verify"].setProperty("active", False)
    w.hash_sel.setCurrentText("sha256")
    argv = w._build_argv(True, ["/s"], "/d")
    checks = {
        "--no-dedup": "--no-dedup" in argv,
        "--no-verify": "--no-verify" in argv,
        "--dry-run": "--dry-run" in argv,
        "--hash sha256": "--hash" in argv and "sha256" in argv,
    }
    _reset_opts(w)
    bad = [k for k, v in checks.items() if not v]
    return (not bad), "dedup/verify/dry-run/hash toggles wired" if not bad else f"missing {bad}"


def s_argv_advanced(w):
    _reset_opts(w)
    w.exclude_patterns = ["*.tmp", ".git"]
    w.index_existing_paths = ["/mnt/data/old"]
    w.flag_opts["dedup existing"].setProperty("active", True)
    w.flag_opts["force"].setProperty("active", True)
    w.flag_opts["no hash cache"].setProperty("active", True)
    w.flag_opts["overwrite all"].setProperty("active", True)
    w.flag_opts["--use-sudo"].setProperty("active", True)
    argv = w._build_argv(False, ["/s"], "/d")
    checks = {
        "--exclude*2": argv.count("--exclude") == 2,
        "--index-existing": "--index-existing" in argv and "/mnt/data/old" in argv,
        "--dedup-existing": "--dedup-existing" in argv,
        "--force": "--force" in argv,
        "--no-cache": "--no-cache" in argv,
        "--overwrite": "--overwrite" in argv,
        "--use-sudo": "--use-sudo" in argv,
    }
    _reset_opts(w)
    bad = [k for k, v in checks.items() if not v]
    return (not bad), "exclude/index-existing/dedup-existing/force/no-cache/overwrite/sudo wired" \
        if not bad else f"missing {bad}"


def s_argv_preserve(w):
    _reset_opts(w)
    for o in w.meta_opts.values():
        o.setProperty("active", False)
    a1 = w._build_argv(False, ["/s"], "/d")
    none_ok = a1[a1.index("--preserve") + 1] == "none"
    w.meta_opts["mode"].setProperty("active", True)
    w.meta_opts["owner"].setProperty("active", True)
    a2 = w._build_argv(False, ["/s"], "/d")
    val = a2[a2.index("--preserve") + 1]
    some_ok = "mode" in val and "owner" in val
    _reset_opts(w)
    ok = none_ok and some_ok
    return ok, f"--preserve none / '{val}'" if ok else "preserve mapping wrong"


# ── Startup update popup ─────────────────────────────────────────────────────

_RELS = [{"tag_name": "v3.10.0",
          "body": "### New Features\n- **SMB/CIFS** credentials in the GUI\n"
                  "- `--index-existing` reflink dedup\n### Bug Fixes\n- **Windows** crash fixed"}]


def s_update_notes(w):
    d = g.UpdateDialog(w, "v3.10.0", "3.9.0", _RELS, True)
    html = d._build_html(_RELS)
    ok = ("New Features" in html and "Bug Fixes" in html
          and "<b>SMB/CIFS</b>" in html and "<code>--index-existing</code>" in html)
    return ok, "categorized notes + markdown (bold/code) rendered" if ok else "notes render failed"


def s_update_actions(w):
    d = g.UpdateDialog(w, "v1", "v0", _RELS, True)
    a0 = d.action
    d._choose(g.UpdateDialog.DOWNLOAD_ONLY); a1 = d.action
    d2 = g.UpdateDialog(w, "v1", "v0", _RELS, True)
    d2._choose(g.UpdateDialog.DOWNLOAD_INSTALL); a2 = d2.action
    ok = a0 == g.UpdateDialog.CLOSE and a1 == g.UpdateDialog.DOWNLOAD_ONLY and a2 == g.UpdateDialog.DOWNLOAD_INSTALL
    return ok, "Close / Download-only / Download-&-install actions" if ok else f"actions {a0},{a1},{a2}"


def s_update_skip(w):
    w._save_gui_settings({})
    s = w._load_gui_settings()
    s["skip_versions"] = sorted(set(s.get("skip_versions", [])) | {"v3.10.0"})
    w._save_gui_settings(s)
    reloaded = w._load_gui_settings().get("skip_versions", [])
    ok = "v3.10.0" in reloaded
    w._save_gui_settings({})
    return ok, "'don't show again' version persisted to gui_settings.json" if ok else "skip not persisted"


# ── misc GUI logic ───────────────────────────────────────────────────────────

def s_asset_name(w):
    n = w._gui_asset_name()
    valid = {"blitcp_gui-windows.exe", "blitcp_gui-macos-intel.app.zip",
             "blitcp_gui-macos-arm64.app.zip", "blitcp_gui-linux"}
    ok = n in valid
    return ok, f"platform asset: {n}" if ok else f"unexpected asset {n}"


def s_config_snapshot(w):
    w.index_existing_paths = ["/q"]
    w.exclude_patterns = ["*.x"]
    w.flag_opts["force"].setProperty("active", True)
    cfg = w._current_config()
    ok = (cfg.get("index_existing") == ["/q"] and cfg.get("exclude") == ["*.x"]
          and "force" in cfg.get("flags", []) and "dedup" in cfg and "preserve" in cfg)
    w.index_existing_paths = []
    w.exclude_patterns = []
    w.flag_opts["force"].setProperty("active", False)
    return ok, "saved-transfer snapshot has index_existing/exclude/flags/preserve" if ok else f"cfg={list(cfg)}"


def s_path_compose(w):
    lp = w._compose_path({"type": "local", "name": "l"}, "/base", "f.txt")
    sp = w._compose_path({"type": "ssh", "name": "s", "user": "u", "host": "h"}, "/r", "x")
    mp = w._compose_path({"type": "smb", "name": "win"}, "/", "a/b")
    cp = w._compose_path({"type": "s3", "name": "aws", "container": "bk"}, "/p", "o")
    ok = (lp == os.path.join("/base", "f.txt") and sp == "u@h:/r/x"
          and mp == "win:a/b" and cp.startswith("s3://aws@bk"))
    return ok, f"local/ssh={sp}/smb={mp}/cloud ok" if ok else f"l={lp} s={sp} m={mp} c={cp}"


def s_log_colors(w):
    """Every colour branch of _add_log must render. Regression: an error line hit a
    non-existent theme key ('err' instead of 'danger') and crashed the whole
    _on_proc_stdout handler with KeyError on the user's machine."""
    for ln in ("  ✗ Verification failed:", "    MISSING: lost.txt",
               "────────────────────────", "  Phase 2 — Deduplication",
               "  ✓ Verified: all 10 files OK", "  Hashing 100 files...",
               "      → backup/: 3 files matched"):
        w._add_log(ln)   # raises if a colour branch references a missing key
    return True, "all _add_log colour branches render without error"


def s_log_crlf(w):
    """Windows \\r\\n engine output — after the GUI splits on \\n a trailing \\r
    remains on each line — must still reach the log. Regression: taking the last
    \\r-segment without rstrip-first collapsed every Windows line to '' and the log
    went completely blank."""
    import time
    w._run_meta = {"in_tb": False, "last": {}, "verified": None, "dedup_bytes": None,
                   "outbuf": "", "errtail": "", "fulllog": [], "speed_bps": 0,
                   "bytes_written": None, "files_total": None, "t0": time.monotonic()}
    n0 = w.log_lay.count()
    for ln in ("  Phase 1 — Scanning source\r", "  Found 10 files\r",
               "  Scanning... 100\r  Scanning... 200\r"):
        w._handle_out_line(ln)
    added = w.log_lay.count() - n0
    if added < 3:
        return False, f"Windows CRLF lines dropped — only {added}/3 reached the log"
    return True, "Windows \\r\\n log lines render"


def s_conn_http(w):
    """An http connection is matched by HOST, so a download behind a login
    needs no flags — the engine picks these credentials up automatically."""
    d = g.ConnectionDialog(w, "", {"type": "http"})
    d.f_type.setCurrentIndex(_types().index("http")); d._sync_type()
    d.f_name.setText("vendor")
    for k, v in [("host", "dl.example.com"), ("user", "u"), ("password", "pw"),
                 ("header", "Authorization: Bearer T"), ("cookies", "firefox")]:
        d.fields[("http", k)].setText(v)
    name, e = d.result_data()
    ok = (name == "vendor" and e.get("type") == "http"
          and e.get("host") == "dl.example.com" and e.get("user") == "u"
          and e.get("password") == "pw"
          and e.get("header") == "Authorization: Bearer T"
          and e.get("cookies") == "firefox")
    return ok, "http host/user/password/header/cookies saved" if ok else f"got {e}"


def s_conn_http_validation(w):
    # a host is required
    d = g.ConnectionDialog(w, "", {"type": "http"})
    d.f_type.setCurrentIndex(_types().index("http")); d._sync_type()
    d.f_name.setText("x")
    n1, m1 = d.result_data()
    # a header without ':' would become a header named after the whole string
    d2 = g.ConnectionDialog(w, "", {"type": "http"})
    d2.f_type.setCurrentIndex(_types().index("http")); d2._sync_type()
    d2.f_name.setText("x")
    d2.fields[("http", "host")].setText("h")
    d2.fields[("http", "header")].setText("no-colon")
    n2, m2 = d2.result_data()
    ok = n1 is None and n2 is None
    return ok, "rejects missing host and a malformed header" if ok else \
        f"validation too lax: {m1!r} / {m2!r}"


def _web_run(w, sources, dest, dest_type):
    """Drive run_transfer without a modal password prompt or a real child."""
    toasts, argv = [], []

    class _Sig:
        def connect(self, *a, **k):
            pass

    class _Proc:
        def __init__(self, *a, **k):
            pass

        def setArguments(self, a):
            argv.append(list(a))

        def start(self, *a, **k):
            pass

        def __getattr__(self, n):
            return _Sig() if n.startswith(("ready", "finished", "error", "state")) \
                else (lambda *a, **k: None)

    old_toast, old_auth, old_proc = w.show_toast, w._ssh_auth_flags, g.QProcess
    old_running, old_sources = w.running, w.sources
    old_dt, old_dest = w.dest_type, w.dst_input.text()
    try:
        w.show_toast = lambda m, *a, **k: toasts.append(str(m))
        w._ssh_auth_flags = lambda path, which: ([], {})
        g.QProcess = _Proc
        w.running = False
        w.sources = list(sources)
        w.dest_type = dest_type
        w.dst_input.setText(dest)
        w.run_transfer(dry=True)
    finally:
        w.show_toast, w._ssh_auth_flags, g.QProcess = old_toast, old_auth, old_proc
        w.running, w.sources = old_running, old_sources
        w.dest_type = old_dt
        w.dst_input.setText(old_dest)
    return toasts, (argv[0] if argv else None)


def s_web_source_refusals(w):
    """The GUI must refuse what run_http_transfer() refuses, before launching:
    a URL cannot be downloaded locally, cannot go to object storage, must name
    a file, must actually be a URL, and must be the only source."""
    url = "https://h/dir/file.iso"
    web = [{"p": url, "t": "Web"}]
    cases = [
        ("local dest", web, "/tmp/x", "Local", "SSH or SMB"),
        ("cloud dest", web, "s3://b/k/", "Cloud", "not supported yet"),
        ("no filename", [{"p": "https://h/dir/", "t": "Web"}], "u@h:/t/", "SSH",
         "names no file"),
        ("not a URL", [{"p": "/etc/hostname", "t": "Web"}], "u@h:/t/", "SSH",
         "http:// or https://"),
        ("two sources", web + [{"p": "/etc/hostname", "t": "Local"}], "u@h:/t/",
         "SSH", "single URL"),
    ]
    for label, srcs, dest, dt, needle in cases:
        toasts, argv = _web_run(w, srcs, dest, dt)
        if argv is not None:
            return False, f"{label}: launched instead of refusing"
        if not any(needle in t for t in toasts):
            return False, f"{label}: no message about {needle!r} (got {toasts})"
    return True, "all five web-source refusals reported before launch"


def s_web_source_argv(w):
    """A valid web source reaches the engine as a plain URL argument — the CLI
    already knows what to do with it, so the GUI adds nothing of its own."""
    url = "https://h/dir/file.iso"
    for dest, dt in (("u@h:/srv/", "SSH"), ("smb://nas/share/", "Cloud")):
        toasts, argv = _web_run(w, [{"p": url, "t": "Web"}], dest, dt)
        if argv is None:
            return False, f"{dt} destination was refused: {toasts}"
        if url not in argv or dest not in argv:
            return False, f"{dt}: url/dest missing from argv {argv[:4]}"
        if argv.index(url) > argv.index(dest):
            return False, f"{dt}: url must precede the destination"
    return True, "web → SSH and web → SMB both launch with the URL as source"


def s_http_never_a_destination(w):
    """An http(s):// URL must never be accepted as a destination.

    The chips offer no Web destination, but the field is free text, so a
    pasted URL has to be refused explicitly — whatever endpoint type is
    selected, and whatever case it is typed in. A legitimate smb:// share
    must still go through."""
    for dest, dt in (("https://h/up.bin", "Cloud"), ("http://h/up.bin", "Cloud"),
                     ("HTTPS://H/UP.BIN", "Cloud"), ("https://h/up.bin", "SSH")):
        toasts, argv = _web_run(w, [{"p": "/etc/hostname", "t": "Local"}], dest, dt)
        if argv is not None:
            return False, f"{dest} ({dt}) was accepted as a destination"
        if not any("only be the source" in t for t in toasts):
            return False, f"{dest}: no refusal message (got {toasts})"
    toasts, argv = _web_run(w, [{"p": "/etc/hostname", "t": "Local"}],
                            "smb://nas/share/", "Cloud")
    if argv is None:
        return False, f"a legitimate smb:// destination was refused: {toasts}"
    return True, "http(s) destinations refused in any case; smb:// still runs"


def s_sudo_askpass(w):
    """--use-sudo must ask HERE, not on whatever terminal launched the GUI.

    sudo with no controlling terminal cannot prompt at all; from the GUI it
    either failed with "a terminal is required" or went looking for the
    terminal the app happened to be started from and waited there, leaving
    this window on an empty log. The password is handed to sudo through
    SUDO_ASKPASS, never written to disk, and the helper is removed when the
    transfer ends.
    """
    real_needs, real_dlg = w._sudo_needs_password, g.PasswordDialog
    try:
        # passwordless: no prompt, nothing to pass
        w._sudo_needs_password = lambda: False
        if w._sudo_askpass_env() != {}:
            return False, "prompted even though sudo needs no password"

        w._sudo_needs_password = lambda: True

        class _Dlg:
            def __init__(self, *a, **k):
                class _I:
                    def text(self_inner):
                        return "s3cret"
                self.input = _I()

            def exec(self):
                return True

        g.PasswordDialog = _Dlg
        env = w._sudo_askpass_env()
        if sorted(env) != ["BLITCP_SUDO_PW", "SUDO_ASKPASS"]:
            return False, f"unexpected env {sorted(env)}"
        helper = env["SUDO_ASKPASS"]
        if os.stat(helper).st_mode & 0o077:
            return False, "askpass helper is readable by others"
        if os.stat(os.path.dirname(helper)).st_mode & 0o077:
            return False, "askpass directory is readable by others"
        if "s3cret" in open(helper).read():
            return False, "the password was written into the helper script"
        out = subprocess.run(["/bin/sh", helper], capture_output=True, text=True,
                             encoding="utf-8", errors="replace",
                             env={**os.environ,
                                  "BLITCP_SUDO_PW": env["BLITCP_SUDO_PW"]})
        if out.stdout.strip() != "s3cret":
            return False, f"helper returned {out.stdout.strip()!r}"

        class _Cancel(_Dlg):
            def exec(self):
                return False

        g.PasswordDialog = _Cancel
        if w._sudo_askpass_env() is not None:
            return False, "cancelling the prompt did not abort the transfer"

        w._clear_askpass()
        if os.path.exists(helper):
            return False, "the askpass helper survived the transfer"
    finally:
        w._sudo_needs_password, g.PasswordDialog = real_needs, real_dlg
    return True, "asks in-app, passes via SUDO_ASKPASS, nothing left on disk"


def s_web_source_icon(w):
    """Every icon name must exist in TI — ic() silently renders '?' otherwise."""
    missing = [t for t in ("Local", "SSH", "Cloud", "Web")
               if g.ic(w._src_icon(t)) == "?"]
    if missing:
        return False, f"source types with no glyph: {missing}"
    if "http" not in _types():
        return False, "http is not offered as a connection type"
    return True, "Web source type has a real glyph; http is an offered type"


SCENARIOS = [
    ("GUI-CONN-1", "S3 connection saves credentials", s_conn_s3),
    ("GUI-CONN-2", "SSH connection saves credentials", s_conn_ssh),
    ("GUI-CONN-3", "SMB connection saves credentials", s_conn_smb),
    ("GUI-CONN-4", "required-field validation (host/keys/name)", s_conn_validation),
    ("GUI-CONN-5", "HTTP connection saves credentials", s_conn_http),
    ("GUI-CONN-6", "HTTP required-field / header validation", s_conn_http_validation),
    ("GUI-WEB-1", "web source refusals happen before launch", s_web_source_refusals),
    ("GUI-WEB-2", "web source reaches the engine as a URL argument", s_web_source_argv),
    ("GUI-WEB-3", "web source type has a real icon", s_web_source_icon),
    ("GUI-WEB-4", "http(s) is never accepted as a destination", s_http_never_a_destination),
    ("GUI-SUDO-1", "--use-sudo asks in-app, not on the launching terminal", s_sudo_askpass),
    ("GUI-DLG-1", "connection dialog auto-sizes to fit fields + buttons", s_dialog_autosize),
    ("GUI-ARGV-1", "base argv (src/dst/threads/preserve)", s_argv_defaults),
    ("GUI-ARGV-2", "dedup/verify/dry-run/hash toggles", s_argv_toggles),
    ("GUI-ARGV-3", "exclude/index-existing/dedup-existing/force/no-cache/overwrite/sudo", s_argv_advanced),
    ("GUI-ARGV-4", "--preserve none vs selected metadata", s_argv_preserve),
    ("GUI-UPD-1", "update popup renders categorized notes + markdown", s_update_notes),
    ("GUI-UPD-2", "Close / Download-only / Download-&-install actions", s_update_actions),
    ("GUI-UPD-3", "'don't show again' version persisted", s_update_skip),
    ("GUI-MISC-1", "platform GUI asset name", s_asset_name),
    ("GUI-MISC-2", "saved-transfer config snapshot", s_config_snapshot),
    ("GUI-MISC-3", "remote/local path composition", s_path_compose),
    ("GUI-MISC-4", "log line colouring renders every branch", s_log_colors),
    ("GUI-MISC-5", "Windows CRLF output reaches the log", s_log_crlf),
]


def _force_utf8_stdout():
    """Windows consoles are cp1252 and these suites print '✓' / '→'. The
    UnicodeEncodeError lands mid-report, turning a run whose checks passed into
    a crash — so retarget the streams before the first line is written."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if (getattr(stream, "encoding", "") or "").lower() not in ("utf-8", "utf8"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                  # noqa: BLE001
            pass


def main(argv=None):
    _force_utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args(argv)
    if args.list:
        for sid, title, _ in SCENARIOS:
            print(f"  {sid:<12} {title}")
        return 0

    app = QApplication(sys.argv[:1])
    try:
        w = g.BlitcpGUI()
    except Exception as e:
        print(f"{C.R}error:{C.X} could not construct BlitcpGUI: {e}")
        return 2

    print(f"{C.B}UAT — blitcp GUI{C.X}  (offscreen Qt)")
    npass = nfail = 0
    for sid, title, fn in SCENARIOS:
        try:
            ok, detail = fn(w)
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {e}"
        tag = f"{C.G}PASS{C.X}" if ok else f"{C.R}FAIL{C.X}"
        if ok:
            npass += 1
        else:
            nfail += 1
        print(f"  {tag}  {sid:<12} {title}")
        if not ok:
            print(f"        {C.GREY}{detail}{C.X}")
        elif detail:
            print(f"        {C.GREY}{detail}{C.X}")

    print(f"\n{C.B}{'='*60}{C.X}")
    verdict = f"{C.R}UAT FAILED{C.X}" if nfail else f"{C.G}UAT PASSED{C.X}"
    print(f" {verdict} — {npass} pass, {nfail} fail")
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
