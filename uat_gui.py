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
    n0 = len(_log_lines(w))
    for ln in ("  Phase 1 — Scanning source\r", "  Found 10 files\r",
               "  Scanning... 100\r  Scanning... 200\r"):
        w._handle_out_line(ln)
    added = len(_log_lines(w)) - n0
    if added < 3:
        return False, f"Windows CRLF lines dropped — only {added}/3 reached the log"
    return True, "Windows \\r\\n log lines render"


def _log_lines(w):
    """The visible transfer log, one string per line."""
    view = getattr(w, "log_view", None)
    if view is not None:
        doc = view.document()
        if doc.isEmpty():
            return []
        return [doc.findBlockByNumber(i).text() for i in range(doc.blockCount())]
    # QLabel-per-line log (before the bounded view): every widget but the stretch.
    return [w.log_lay.itemAt(i).widget().text()
            for i in range(w.log_lay.count()) if w.log_lay.itemAt(i).widget()]


def _rss_mb():
    try:
        with open("/proc/self/status") as f:
            for ln in f:
                if ln.startswith("VmRSS:"):
                    return int(ln.split()[1]) // 1024
    except OSError:
        pass
    return None


_FLOOD_ENGINE = r'''
import json, sys
n = int(sys.argv[1])
print("  Phase 1 — Scanning source", flush=True)
for i in range(n):
    k = i % 4
    if k == 0:
        sys.stdout.write(f"  ⚠ Skipped (unreadable): share/dir{i//1000:04d}/file_{i:07d}.dat"
                         f" — [Errno 13] Permission denied\n")
    elif k == 1:
        sys.stdout.write(f"  ✗ Refused name: share/dir{i//1000:04d}/con_{i:07d}:s.txt\n")
    elif k == 2:
        sys.stdout.write(f"  copied share/dir{i//1000:04d}/photo_{i:07d}.jpg  1.2 MB\n")
    else:
        sys.stdout.write(json.dumps({"t": "progress", "pct": 100.0 * i / n,
                                     "files_done": i, "files_total": n,
                                     "speed_bps": 1000, "eta_s": 1}) + "\n")
    if i % 5000 == 0:
        sys.stderr.write(f"warning: slow SMB response at file {i}\n"); sys.stderr.flush()
sys.stdout.write("  ✓ Verified: all 10 files OK\n  FLOOD-END\n")
'''


def s_log_flood_bounded(w):
    """An engine that prints a line per file must not grow the GUI without
    bound. Regression: every non-JSON line became a new word-wrapped QLabel that
    was never removed, ~20 KB each — 30,000 lines took the GUI from 99 MB to
    660 MB (and 12 minutes), and on Ubuntu a large SMB copy ended with the
    kernel's OOM killer taking the GUI. Drives run_transfer() against a real
    child process that floods more lines than the log keeps."""
    import time
    from PySide6.QtCore import QEventLoop, QTimer
    cap = getattr(g, "LOG_MAX_LINES", 10000)
    n = cap + 6000
    tmp = tempfile.mkdtemp(prefix="uat_gui_flood_")
    eng = os.path.join(tmp, "flood_engine.py")
    with open(eng, "w", encoding="utf-8") as f:
        f.write(_FLOOD_ENGINE)
    src, dst = os.path.join(tmp, "src"), os.path.join(tmp, "dst")
    os.makedirs(src); os.makedirs(dst)
    saved = (w.sources, w.dest_type, w.dst_input.text(), w.running)
    w._fc_script_path = lambda: eng
    w._build_argv = lambda dry, s, d: [str(n)]
    w.show_toast = lambda *a, **k: None
    try:
        w.sources = [{"t": "Local", "p": src}]
        w.dest_type = "Local"
        w.dst_input.setText(dst)
        w.running = False
        QApplication.processEvents()
        r0 = _rss_mb()
        t0 = time.monotonic()
        w.run_transfer(dry=False)
        loop = QEventLoop()
        poll = QTimer()
        poll.timeout.connect(lambda: (not w.running or time.monotonic() - t0 > 300)
                             and loop.quit())
        poll.start(100)
        loop.exec()
        poll.stop()
        for _ in range(20):
            QApplication.processEvents()
        r1 = _rss_mb()
        elapsed = time.monotonic() - t0
    finally:
        for k in ("_fc_script_path", "_build_argv", "show_toast"):
            w.__dict__.pop(k, None)
        w.sources, w.dest_type = saved[0], saved[1]
        w.dst_input.setText(saved[2])
        w.running = saved[3]
    if w.running:
        return False, f"flood run did not finish in {elapsed:.0f}s"
    lines = _log_lines(w)
    if len(lines) > cap:
        return False, (f"log holds {len(lines):,} lines after {n:,} engine lines "
                       f"(cap {cap:,}) — the visible log is unbounded")
    if not lines or lines[-1].strip() != "FLOOD-END":
        return False, f"last engine line missing from the log: {lines[-1:]!r}"
    if any("file_0000000" in ln for ln in lines):
        return False, "oldest lines were kept — the cap must drop from the top"
    grew = None if r0 is None or r1 is None else r1 - r0
    # [measured: 2026-10-04, Kali, offscreen Qt 6.11] bounded view +70 MB here,
    # flat past the cap (169 MB RSS at both 30k and 200k lines); the QLabel log
    # was +187 MB at 10k lines and +561 MB at 30k.
    if grew is not None and grew > 120:
        return False, f"GUI RSS grew {grew} MB over {n:,} lines"
    return True, (f"{n:,} engine lines → {len(lines):,} kept (cap {cap:,}), "
                  f"RSS +{grew} MB, {elapsed:.1f}s")


def s_log_colours_kept(w):
    """The bounded log keeps the terminal colouring: phase banners accent, ✓
    green, errors red, rules faint, plain lines in the default colour."""
    view = getattr(w, "log_view", None)
    if view is None:
        return False, "no bounded log view (log is still a QLabel per line)"
    v = g.DARK if w.dark else g.LIGHT
    w._clear_log()
    want = [("  Phase 2 — Deduplication", v["accent"]),
            ("  ✓ Verified: all 10 files OK", v["ok"]),
            ("  ✗ Verification failed:", v["danger"]),
            ("────────────────────────", v["faint"]),
            ("  Hashing 100 files...", None)]
    for text, _ in want:
        w._add_log(text)
    doc = view.document()
    bad = []
    for i, (text, colour) in enumerate(want):
        blk = doc.findBlockByNumber(i)
        fmt = blk.begin().fragment().charFormat()
        got = fmt.foreground().color().name() if fmt.hasProperty(
            fmt.Property.ForegroundBrush) else None
        if blk.text() != text or (got or None) != (colour.lower() if colour else None):
            bad.append(f"{text.strip()[:20]!r}: colour {got} want {colour}")
    if bad:
        return False, "; ".join(bad)
    return True, "phase/✓/✗/rule/plain colours survive the bounded view"


def s_log_scroll_coalesced(w):
    """One pending scroll-to-bottom however many lines arrive in a burst, not
    a queued timer per line."""
    from PySide6.QtCore import QTimer
    if getattr(w, "log_view", None) is None:
        return False, "no bounded log view"
    w._clear_log()
    QApplication.processEvents()
    t = getattr(w, "_log_scroll_timer", None)
    if not isinstance(t, QTimer):
        return False, "no single coalescing scroll timer"
    for i in range(2000):
        w._add_log(f"  line {i}")
    if not t.isActive():
        return False, "scroll timer not armed after a burst"
    for _ in range(10):
        QApplication.processEvents()
    sb = w.log_view.verticalScrollBar()
    if sb.value() != sb.maximum():
        return False, f"log not scrolled to the end ({sb.value()}/{sb.maximum()})"
    return True, "2,000 lines → one pending scroll, log ends at the bottom"


def s_outbuf_cr_bounded(w):
    """\\r-only progress (no newline until the phase ends — e.g. the per-file
    'Hashing existing [N]: path' line) must not pile up in the GUI's partial-
    line buffer, which was re-copied on every read. A CRLF split across two
    reads must still deliver the line."""
    import time

    class _P:
        chunks = []

        def readAllStandardOutput(self):
            return self.chunks.pop(0).encode() if self.chunks else b""

    w._run_meta = {"in_tb": False, "last": {}, "verified": None, "dedup_bytes": None,
                   "outbuf": "", "errtail": "", "fulllog": g._BoundedLog(),
                   "speed_bps": 0, "bytes_written": None, "files_total": None,
                   "t0": time.monotonic(), "skipped_unreadable": 0}
    w._clear_log()
    old = getattr(w, "_proc", None)
    p = _P()
    w._proc = p
    try:
        peak = 0
        for i in range(20000):
            p.chunks.append(f"\r  Hashing existing [{i}]: /mnt/data/some/long/path/file_{i:07d}.bin   ")
            w._on_proc_stdout()
            peak = max(peak, len(w._run_meta["outbuf"]))
        p.chunks.append("\r  Dedup complete:   \n")
        w._on_proc_stdout()
        for part in ("  CRLF line\r", "\n"):       # Windows CRLF split across reads
            p.chunks.append(part)
            w._on_proc_stdout()
    finally:
        w._proc = old
    lines = [ln.strip() for ln in _log_lines(w)]
    longest = max((len(x) for x in w._run_meta["fulllog"].lines()), default=0)
    if peak > 4096:
        return False, f"partial-line buffer reached {peak:,} chars on \\r-only progress"
    if longest > 4096:
        return False, f"diagnostic log kept a {longest:,}-char line"
    if lines != ["Dedup complete:", "CRLF line"]:
        return False, f"log reads {lines!r}"
    return True, f"20,000 \\r updates → buffer peak {peak} chars; CRLF split kept"


def s_gio_hook(w):
    """The frozen Linux GUI's runtime hook keeps the bundled (older) libgio
    away from the host's gio modules — on Ubuntu 26.04 each one failed with
    'undefined symbol' — while a user's own setting wins, and _host_env()
    hands launched programs (browser, file manager) the session's GIO back."""
    import json
    import build
    if "runtime_hooks=[{hook!r}]" not in build.GUI_SPEC_TEMPLATE:
        return False, "GUI spec does not install the GIO runtime hook"
    tmp = tempfile.mkdtemp(prefix="uat_gio_")
    hook = os.path.join(tmp, "rth.py")
    with open(hook, "w", encoding="utf-8") as f:
        f.write(build.GIO_HOOK)
    probe = ("import json, os, sys; sys._MEIPASS = sys.argv[1]; "
             "exec(open(sys.argv[2]).read()); "
             "print(json.dumps({k: os.environ.get(k) for k in "
             "('GIO_MODULE_DIR', 'GIO_USE_VFS', 'GSETTINGS_BACKEND', '_BLITCP_GIO_SAVED')}))")
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIO_MODULE_DIR", "GSETTINGS_BACKEND", "BLITCP_HOST_GIO")}
    env["GIO_USE_VFS"] = "gvfs"                           # the user's own choice
    r = subprocess.run([sys.executable, "-c", probe, tmp, hook], env=env,
                       capture_output=True, text=True)
    if r.returncode != 0:
        return False, f"hook failed: {r.stderr.strip()[-200:]}"
    got = json.loads(r.stdout)
    empty = os.path.join(tmp, "gio-modules-none")
    bad = []
    if got["GIO_MODULE_DIR"] != empty or os.listdir(empty):
        bad.append(f"GIO_MODULE_DIR={got['GIO_MODULE_DIR']!r}, not an empty dir")
    if got["GSETTINGS_BACKEND"] != "memory":
        bad.append(f"GSETTINGS_BACKEND={got['GSETTINGS_BACKEND']!r}")
    if got["GIO_USE_VFS"] != "gvfs":
        bad.append("the user's GIO_USE_VFS was overridden")
    old = dict(os.environ)
    try:
        os.environ.update({k: v for k, v in got.items() if v is not None})
        host = g._host_env()
    finally:
        os.environ.clear()
        os.environ.update(old)
    if "GIO_MODULE_DIR" in host or "GSETTINGS_BACKEND" in host \
            or host.get("GIO_USE_VFS") != "gvfs" or "_BLITCP_GIO_SAVED" in host:
        bad.append(f"_host_env leaks the hook: { {k: host.get(k) for k in got} }")
    if bad:
        return False, "; ".join(bad)
    return True, "empty module dir + memory settings; user value kept; launched programs get the host env"


def s_version_single_source(w):
    """The window title, the Settings version card and the engine agree.
    Regression: GUI_VERSION was a literal "4.2.3" that the release flow never
    bumped, so v4.2.13 showed "blitcp 4.2.3" — and _download_core() fetched the
    v4.2.3 engine. The pin used without an engine must match blitcp.py too."""
    import blitcp
    pin = getattr(g, "_GUI_PIN", None)
    bad = []
    if pin != blitcp.__version__:
        bad.append(f"_GUI_PIN {pin!r} != blitcp.__version__ {blitcp.__version__!r} "
                   f"— bump both on release")
    if g.FC_OK and g.GUI_VERSION != g.fc.__version__:
        bad.append(f"GUI_VERSION {g.GUI_VERSION!r} != engine {g.fc.__version__!r}")
    if not w.windowTitle().endswith(g.fc.__version__ if g.FC_OK else str(pin)):
        bad.append(f"window title {w.windowTitle()!r}")
    from PySide6.QtWidgets import QLabel
    shown = {lb.text() for lb in w.findChildren(QLabel)}
    if "blitcp " + g.GUI_VERSION not in shown:
        bad.append("Settings card does not show 'blitcp " + g.GUI_VERSION + "'")
    if bad:
        return False, "; ".join(bad)
    return True, f"title, Settings card, engine and pin all {g.GUI_VERSION}"


# ── remote browsing stays off the UI thread ─────────────────────────────────
# The field report: every remote connection in the browser froze the window
# ("not responding"), and GNOME's Force Quit is a SIGKILL. The listing ran on
# the UI thread, and for SMB it was a recursive walk with a stat per file.

def _pump(pred, timeout_s, action=None, tick_ms=100):
    """Tick a QTimer every tick_ms, run `action`, then spin the event loop
    until pred() or timeout_s. Returns (pred(), seconds, max_gap): max_gap is
    the longest the UI thread went without serving the tick — the time a
    user would see a frozen window."""
    import time
    from PySide6.QtCore import QTimer, QCoreApplication, QEventLoop
    ticks = [time.monotonic()]
    t = QTimer()
    t.setInterval(tick_ms)
    t.timeout.connect(lambda: ticks.append(time.monotonic()))
    t.start()
    start = time.monotonic()
    if action:
        action()
    ok = False
    while time.monotonic() - start < timeout_s:
        QCoreApplication.processEvents(QEventLoop.AllEvents, 20)
        if pred():
            ok = True
            break
        time.sleep(0.005)
    t.stop()
    ticks.append(time.monotonic())
    gap = max(b - a for a, b in zip(ticks, ticks[1:]))
    return ok, time.monotonic() - start, gap


def _rows(lay):
    """Texts of the browser rows in a layout: file names ('fn') and messages."""
    from PySide6.QtWidgets import QLabel
    out = []
    for i in range(lay.count()):
        wd = lay.itemAt(i).widget()
        if wd is None:
            continue
        if isinstance(wd, QLabel):
            out.append(wd.text())
            continue
        for lb in wd.findChildren(QLabel):
            if lb.objectName() in ("fn", "sub"):
                out.append(lb.text())
    return out


class _FakeSMB:
    """smbclient + smbprotocol.exceptions stand-ins: a share with `ndirs`
    folders of `per_dir` files under its root, and `latency` seconds on every
    connect and directory query."""
    def __init__(self, ndirs=20, per_dir=10000, latency=2.0):
        import types
        self.ndirs, self.per_dir, self.latency = ndirs, per_dir, latency
        self.calls, self.stats = [], 0
        sc = types.ModuleType("smbclient")
        sc.register_session = self.register_session
        sc.scandir = self.scandir
        sc.walk = self.walk
        sc.stat = self.stat
        sc.delete_session = lambda server, port=445, **kw: self.calls.append(("delete", server))
        ex = types.ModuleType("smbprotocol.exceptions")
        ex.SMBOSError = type("SMBOSError", (OSError,), {})
        ex.NtStatus = types.SimpleNamespace(STATUS_OBJECT_NAME_INVALID=1,
                                            STATUS_OBJECT_PATH_INVALID=2)
        ex.ObjectNameInvalid = ex.ObjectPathInvalid = type("X", (Exception,), {})
        pkg = types.ModuleType("smbprotocol")
        pkg.exceptions = ex
        self.mods = {"smbclient": sc, "smbprotocol": pkg, "smbprotocol.exceptions": ex}

    def __enter__(self):
        self.saved = {k: sys.modules.get(k) for k in self.mods}
        sys.modules.update(self.mods)
        return self

    def __exit__(self, *a):
        for k, v in self.saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    def _level(self, unc):
        rel = unc.split("\\", 4)[4] if unc.count("\\") >= 4 else ""
        rel = rel.strip("\\")
        if not rel:
            return [(f"d{i:02d}", True, 0) for i in range(self.ndirs)] + \
                [("readme.txt", False, 1234)]
        return [(f"f{j:05d}.bin", False, 4096) for j in range(self.per_dir)]

    def register_session(self, server, **kw):
        import time
        self.calls.append(("register", server, kw))
        time.sleep(self.latency)

    def scandir(self, path, **kw):
        import time, types
        self.calls.append(("scandir", path))
        time.sleep(self.latency)
        for name, is_dir, size in self._level(path):
            yield types.SimpleNamespace(name=name, path=path + "\\" + name,
                                        smb_info=types.SimpleNamespace(
                                            file_attributes=0x10 if is_dir else 0x20,
                                            end_of_file=size))

    def walk(self, top, **kw):
        import time
        self.calls.append(("walk", top))
        time.sleep(self.latency)
        stack = [top]
        while stack:
            d = stack.pop()
            lv = self._level(d)
            dirs = [n for n, isd, _ in lv if isd]
            yield d, dirs, [n for n, isd, _ in lv if not isd]
            stack.extend(d + "\\" + n for n in dirs)

    def stat(self, path, **kw):
        import stat as st, types
        self.stats += 1
        leaf = path.rsplit("\\", 1)[-1]
        is_file = "." in leaf
        return types.SimpleNamespace(st_mode=(st.S_IFREG if is_file else st.S_IFDIR),
                                     st_size=4096 if is_file else 0, st_mtime=0)


def _browse_conn(w, name, entry):
    """Select one saved connection in the main browser; returns a restorer."""
    saved = w.conns
    w.conns = {name: entry}
    w.fill_conn_sel()
    def restore():
        w.conns = saved
        w.fill_conn_sel()
        w.render_browse()
    return restore


def s_browse_smb_offthread(w):
    if not g.FC_OK:
        return None, "blitcp.py not importable"
    fake = _FakeSMB(ndirs=20, per_dir=10000, latency=2.0)        # 200k files
    with fake:
        restore = _browse_conn(w, "nas", {"type": "smb", "host": "nas.example",
                                          "share": "refs", "user": "u",
                                          "password": "p", "port": 445})
        try:
            rss0 = _rss_mb()
            idx = w.conn_sel.findData("nas")
            ok, secs, gap = _pump(lambda: any(r.startswith("d19") for r in _rows(w.files_box)),
                                  30, action=lambda: w.conn_sel.setCurrentIndex(idx))
            rss1 = _rss_mb()
            rows = _rows(w.files_box)
        finally:
            restore()
    walked = [c for c in fake.calls if c[0] == "walk"]
    scans = [c for c in fake.calls if c[0] == "scandir"]
    reg = [c for c in fake.calls if c[0] == "register"]
    probs = []
    if not ok:
        probs.append(f"listing never shown in {secs:.1f}s: {rows[:5]}")
    if gap > 0.5:
        probs.append(f"UI thread blocked {gap:.2f}s (tick every 0.1s)")
    if walked or fake.stats:
        probs.append(f"recursive listing: {len(walked)} walk(s), {fake.stats} stat(s)")
    if len(scans) != 1:
        probs.append(f"{len(scans)} directory queries for one level")
    items = [r for r in rows if r.startswith(("d", "readme"))]
    if ok and len(items) != 21:
        probs.append(f"{len(items)} rows, want 20 folders + 1 file")
    want_t = getattr(g, "SMB_BROWSE_TIMEOUT", None)
    if want_t is None or not reg or reg[0][2].get("connection_timeout") != want_t:
        probs.append(f"no browse connection timeout: {reg[0][2] if reg else None}")
    if rss0 and rss1 and rss1 - rss0 > 40:
        probs.append(f"RSS +{rss1 - rss0} MB")
    return (not probs, "; ".join(probs) or
            f"root of a 200k-file share: one scandir, 21 rows in {secs:.1f}s, "
            f"UI max gap {gap:.2f}s, RSS {rss0}→{rss1} MB")


def s_browse_ssh_unresponsive(w):
    import logging
    import socket
    try:
        import paramiko  # noqa: F401
    except ImportError:
        return None, "paramiko not installed"
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)                        # accepts, never sends an SSH banner
    port = srv.getsockname()[1]
    old_t = getattr(g, "SSH_BROWSE_TIMEOUT", None)
    g.SSH_BROWSE_TIMEOUT = 2
    restore = _browse_conn(w, "deadssh", {"type": "ssh", "host": "127.0.0.1",
                                          "port": port, "user": "u",
                                          "password": "x", "path": "/"})
    try:
        idx = w.conn_sel.findData("deadssh")
        err = lambda: any("no SSH answer within 2 s" in r for r in _rows(w.files_box))
        # paramiko logs the transport thread's banner error with no handler
        # attached; keep that traceback out of the suite's output.
        plog = logging.getLogger("paramiko")
        quiet = logging.NullHandler()
        plog.addHandler(quiet)
        try:
            ok, secs, gap = _pump(err, 30, action=lambda: w.conn_sel.setCurrentIndex(idx))
        finally:
            plog.removeHandler(quiet)
        rows = _rows(w.files_box)
    finally:
        restore()
        w._ssh_close("deadssh")
        srv.close()
        if old_t is not None:
            g.SSH_BROWSE_TIMEOUT = old_t
    probs = []
    if not ok:
        probs.append(f"no timeout error after {secs:.1f}s: {rows[:3]}")
    elif secs < 1.5:
        probs.append(f"error after {secs:.1f}s — before the 2 s timeout")
    if gap > 0.5:
        probs.append(f"UI thread blocked {gap:.2f}s")
    return (not probs, "; ".join(probs) or
            f"silent SSH host: error after {secs:.1f}s, UI max gap {gap:.2f}s — {rows[-1][:70]}")


def s_browse_ssh_silent_both_spellings(w):
    """A host that never sends a banner reaches the GUI as either of two
    paramiko errors, depending on which thread notices first — 6 and 6 in 12
    connects to a silent socket. Both must read as the timeout. Regression:
    only "No existing session" was mapped, so half the time the user saw
    "Error reading SSH protocol banner" and GUI-BROWSE-2 failed at random."""
    try:
        import paramiko
    except ImportError:
        return None, "paramiko not installed"
    conn = {"name": "silentssh", "type": "ssh", "host": "192.0.2.7", "port": 2222}
    want = f"192.0.2.7 port 2222: no SSH answer within {g.SSH_BROWSE_TIMEOUT} s"
    real = w._ssh_client
    bad = []
    try:
        for text in ("No existing session", "Error reading SSH protocol banner"):
            def _raise(_conn, _t=text):
                raise paramiko.SSHException(_t)
            w._ssh_client = _raise
            _, err = w._ssh_entries_all(conn, "/")
            if err != want:
                bad.append(f"{text!r} → {err!r}")
    finally:
        w._ssh_client = real
    if bad:
        return False, "; ".join(bad)
    return True, "both paramiko spellings of a silent host read as the timeout"


class _FakeS3:
    """S3Backend stand-in: root has slow/, fast/, hang/, big/ and a.txt; slow/
    answers after 1.5 s with a STALE entry, hang/ after 3 s, big/ holds 6,000
    files."""
    def __init__(self, spec, args, creds=None):
        pass

    def _level(self, prefix):
        import time
        if prefix == "slow/":
            time.sleep(1.5)
            return ["STALE"], []
        if prefix == "hang/":
            time.sleep(3)
            return ["LATE"], []
        if prefix == "big/":
            return [], [(f"o{i:05d}", 1) for i in range(6000)]
        return ["slow", "fast", "hang", "big"], [("a.txt", 10)]

    def list_dir(self, prefix, limit=None, cancelled=None):
        d, f = self._level(prefix)
        n = len(d) + len(f)
        if limit is not None:
            d = d[:limit]
            f = f[:max(0, limit - len(d))]
        return d, f, n

    def list_objects(self, prefix):                  # the old full listing
        d, f = self._level(prefix)
        out = {prefix + x + "/k": {"size": 1} for x in d}
        out.update({prefix + n: {"size": sz} for n, sz in f})
        return out


def _with_fake_s3(fn):
    saved_cls = g.fc.S3Backend
    g.fc.S3Backend = _FakeS3
    try:
        return fn({"bkt": {"type": "s3", "container": "b"}})
    finally:
        g.fc.S3Backend = saved_cls


def s_browse_stale_dropped(w):
    import time
    if not g.FC_OK:
        return None, "blitcp.py not importable"

    def run(conns):
        saved = w.conns
        w.conns = conns
        d = None
        try:
            d = g.RemoteBrowseDialog(w, "cloud", start_conn="bkt")
            root = lambda: any(r.startswith("slow") for r in _rows(d.files_lay))
            ok0, _, _ = _pump(root, 10)
            t0 = time.monotonic()
            d._enter("slow")
            enter_s = time.monotonic() - t0
            d._up()                                     # navigate away mid-listing
            ok1, _, _ = _pump(root, 10)
            _pump(lambda: False, 2.5)                   # let the slow answer land
            rows = _rows(d.files_lay)
            return ok0, ok1, enter_s, rows, d.cwd, len(getattr(w, "_list_waiters", {}))
        finally:
            if d is not None:
                d.done(0)
                d.deleteLater()
            w.conns = saved

    ok0, ok1, enter_s, rows, cwd, waiting = _with_fake_s3(run)
    probs = []
    if not ok0:
        probs.append(f"root never listed: {rows[:4]}")
    if enter_s > 0.3:
        probs.append(f"entering a folder blocked the UI {enter_s:.2f}s")
    if any("STALE" in r for r in rows) or cwd != "/" or not ok1:
        probs.append(f"stale listing shown in {cwd!r}: {rows[:6]}")
    return (not probs, "; ".join(probs) or
            f"picker: slow folder left mid-listing, its answer dropped "
            f"({len(rows)} root rows kept, {waiting} still waiting)")


def s_browse_cap_and_cancel(w):
    if not g.FC_OK:
        return None, "blitcp.py not importable"
    from PySide6.QtWidgets import QPushButton

    def run(conns):
        restore = _browse_conn(w, "bkt", conns["bkt"])
        try:
            w.conn_sel.setCurrentIndex(w.conn_sel.findData("bkt"))
            _pump(lambda: any(r.startswith("big") for r in _rows(w.files_box)), 10)
            w._enter_dir("big")
            okb, _, _ = _pump(lambda: any(r.startswith("o0") for r in _rows(w.files_box)), 20)
            big = _rows(w.files_box)
            w._go_up()
            _pump(lambda: any(r.startswith("hang") for r in _rows(w.files_box)), 10)
            w._enter_dir("hang")
            btns = [b for i in range(w.files_box.count())
                    for b in ((w.files_box.itemAt(i).widget().findChildren(QPushButton))
                              if w.files_box.itemAt(i).widget() else [])
                    if b.text().strip() == "Cancel"]
            if btns:
                btns[0].click()
            _pump(lambda: False, 3.5)                   # the late answer arrives
            hang = _rows(w.files_box)
            return okb, big, bool(btns), hang
        finally:
            restore()

    okb, big, had_cancel, hang = _with_fake_s3(run)
    files = [r for r in big if r.startswith("o0")]
    note = [r for r in big if r.startswith("Showing the first")]
    probs = []
    if not okb or len(files) > 5000 or not note or "6,000" not in note[0]:
        probs.append(f"{len(files)} rows shown of 6,000, note {note[:1]}")
    if not had_cancel:
        probs.append("no Cancel on the loading row")
    if any("LATE" in r for r in hang) or not any("Cancelled" in r for r in hang):
        probs.append(f"after Cancel: {hang[:4]}")
    return (not probs, "; ".join(probs) or
            f"{len(files)} of 6,000 rows + '{note[0]}'; Cancel drops the late answer")


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
    if os.name != "posix":
        return None, "sudo does not exist on this platform"
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
    ("GUI-LOG-1", "log colours survive the bounded view", s_log_colours_kept),
    ("GUI-LOG-2", "one coalesced scroll per burst of log lines", s_log_scroll_coalesced),
    ("GUI-LOG-3", "\\r-only progress does not pile up in the line buffer", s_outbuf_cr_bounded),
    ("GUI-MEM-1", "per-file engine flood keeps GUI memory bounded", s_log_flood_bounded),
    ("GUI-GIO-1", "frozen GUI's GIO hook isolates host gio modules", s_gio_hook),
    ("GUI-VER-1", "one version everywhere; pin matches blitcp.py", s_version_single_source),
    ("GUI-BROWSE-1", "SMB browse: one level, off the UI thread, flat RSS", s_browse_smb_offthread),
    ("GUI-BROWSE-2", "silent SSH host: UI responsive, error after timeout", s_browse_ssh_unresponsive),
    ("GUI-BROWSE-5", "silent SSH host: both paramiko errors read as the timeout", s_browse_ssh_silent_both_spellings),
    ("GUI-BROWSE-3", "navigating away mid-listing drops the stale result", s_browse_stale_dropped),
    ("GUI-BROWSE-4", "listing capped with a count; Cancel drops a late answer", s_browse_cap_and_cancel),
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
    nskip = 0
    for sid, title, fn in SCENARIOS:
        try:
            ok, detail = fn(w)
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {e}"
        # ok is None when the scenario asks for something this platform does
        # not have — sudo, say. That is a skip, not a failure: the alternative
        # is a suite that reports the operating system as broken.
        if ok is None:
            tag = f"{C.Y}SKIP{C.X}"
            nskip += 1
            print(f"  {tag}  {sid:<12} {title}")
            if detail:
                print(f"        {C.GREY}{detail}{C.X}")
            continue
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
    print(f" {verdict} — {npass} pass, {nfail} fail"
          + (f", {nskip} skip" if nskip else ""))
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
