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
import testtmp  # test data under ~/.cache/blitcp-tests, never /tmp (RAM)
testtmp.use_test_tmp()
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
    #
    # Where the GUI is REQUIRED (CI sets BLITCP_REQUIRE_GUI=1), a Qt that does
    # not import is a broken runner, not an absent option: from the private
    # CI split (ead23d1) the Ubuntu job skipped every scenario, green, over a
    # missing libEGL.
    print("UAT - blitcp GUI")
    if os.environ.get("BLITCP_REQUIRE_GUI") == "1":
        print(f"  FAIL  every scenario - PySide6 does not import ({_qt_err}) "
              f"and BLITCP_REQUIRE_GUI=1")
        print("\n UAT FAILED - 0 pass, 1 fail")
        sys.exit(1)
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
    for k, v in [("host", "192.0.2.25"), ("user", "j.doe@example.com"),
                 ("password", "pw"), ("domain", ""), ("share", "Documents"), ("port", "445")]:
        d.fields[("smb", k)].setText(v)
    name, e = d.result_data()
    ok = (name == "win" and e.get("type") == "smb" and e.get("host") == "192.0.2.25"
          and e.get("user") == "j.doe@example.com" and e.get("password") == "pw"
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
# The real engine writes UTF-8 to a pipe (blitcp.py reconfigures its streams);
# a Windows pipe would otherwise be cp1252, which has no '⚠'.
for _s in (sys.stdout, sys.stderr):
    _s.reconfigure(encoding="utf-8", errors="replace")
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
    # The run ends only after both pipes are drained. stdout and stderr are
    # two pipes with no order between them (the Windows runner read a stderr
    # warning after the last stdout chunk), so completeness is checked per
    # channel in the diagnostic transcript: stdout ends with FLOOD-END, and
    # every stderr line the engine wrote is there.
    full = w._run_meta["fulllog"].lines()
    out = [ln for ln in full if not ln.startswith("[stderr]")]
    if not out or out[-1].strip() != "FLOOD-END":
        return False, f"stdout not drained to its end: {out[-3:]!r}"
    want = [f"[stderr] warning: slow SMB response at file {i}"
            for i in range(0, n, 5000)]
    got = [ln for ln in full if ln.startswith("[stderr]")]
    if got != want:
        return False, (f"stderr not drained: {len(got)} of {len(want)} lines, "
                       f"last {got[-1:]!r}")
    if not any(ln.strip() == "FLOOD-END" for ln in lines):
        return False, f"last engine line missing from the log: {lines[-3:]!r}"
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


_CRASH_ENGINE = r'''
import os, signal, sys
for _s in (sys.stdout, sys.stderr):
    _s.reconfigure(encoding="utf-8", errors="replace")
print("  Phase 1 — Scanning source", flush=True)
for i in range(2000):
    sys.stdout.write(f"  copied dir/file_{i:05d}.dat\n")
sys.stdout.write("  CRASH-LAST\n"); sys.stdout.flush()
sys.stderr.write("Error: simulated engine crash\n"); sys.stderr.flush()
os.kill(os.getpid(), getattr(signal, "SIGKILL", signal.SIGTERM))
'''


def _drive_stub(w, engine_src, argv, argv_fn=None):
    """run_transfer() against a real child running engine_src; returns
    (history records, toasts, elapsed). The run must end on its own."""
    import time
    from PySide6.QtCore import QEventLoop, QTimer
    tmp = tempfile.mkdtemp(prefix="uat_gui_stub_")
    eng = os.path.join(tmp, "stub_engine.py")
    with open(eng, "w", encoding="utf-8") as f:
        f.write(engine_src)
    src, dst = os.path.join(tmp, "src"), os.path.join(tmp, "dst")
    os.makedirs(src); os.makedirs(dst)
    saved = (w.sources, w.dest_type, w.dst_input.text(), w.running)
    history, toasts = [], []
    w._fc_script_path = lambda: eng
    w._build_argv = (argv_fn if argv_fn is not None
                     else (lambda dry, s, d: list(argv)))
    w.show_toast = lambda msg, *a, **k: toasts.append(msg)
    w._write_history = lambda rec: history.append(rec)
    try:
        w.sources = [{"t": "Local", "p": src}]
        w.dest_type = "Local"
        w.dst_input.setText(dst)
        w.running = False
        QApplication.processEvents()
        t0 = time.monotonic()
        w.run_transfer(dry=False)
        loop = QEventLoop()
        poll = QTimer()
        poll.timeout.connect(lambda: (not w.running or time.monotonic() - t0 > 120)
                             and loop.quit())
        poll.start(50)
        loop.exec()
        poll.stop()
        # Anything that would still arrive after "finished" arrives now.
        end = time.monotonic() + 1.0
        while time.monotonic() < end:
            QApplication.processEvents()
        elapsed = time.monotonic() - t0
    finally:
        for k in ("_fc_script_path", "_build_argv", "show_toast", "_write_history"):
            w.__dict__.pop(k, None)
        w.sources, w.dest_type = saved[0], saved[1]
        w.dst_input.setText(saved[2])
        still = w.running
        w.running = saved[3]
    return history, toasts, elapsed, still


def s_engine_crash_ends_once(w):
    """An engine killed mid-run ends the run once — after its output, as a
    failed run with the engine's last error — never as 'could not start'.
    Regression: QProcess reports a crash as errorOccurred(Crashed) BEFORE
    finished; the error handler marked the run over (running=False, 'Could
    not start blitcp.py') ahead of the pipe drain that only finished does."""
    history, toasts, elapsed, still = _drive_stub(w, _CRASH_ENGINE, [])
    if still:
        return False, f"crashed run never ended ({elapsed:.0f}s)"
    bad = [t for t in toasts if "start" in str(t).lower()]
    if bad:
        return False, f"a crash was reported as a start failure: {bad!r}"
    if len(history) != 1:
        return False, f"run recorded {len(history)} times (expected once)"
    rec = history[0]
    if rec.get("status") != "failed":
        return False, f"history status {rec.get('status')!r}, expected 'failed'"
    if "simulated engine crash" not in str(rec.get("error")):
        return False, f"history error {rec.get('error')!r} lost the engine's last error"
    # The transcript is raw: on Windows a text-mode child ends lines \r\n.
    full = [ln.rstrip("\r") for ln in w._run_meta["fulllog"].lines()]
    if "  CRASH-LAST" not in full:
        return False, "the engine's last stdout line is not in the transcript"
    if "[stderr] Error: simulated engine crash" not in full:
        return False, "the engine's last stderr line is not in the transcript"
    if not w.pstate.text().startswith("Failed"):
        return False, f"status reads {w.pstate.text()!r}"
    return True, f"one failed record, last error kept, {elapsed:.1f}s"


_REAL_ENGINE = r"""
import runpy, sys
sys.argv = ["blitcp"] + sys.argv[1:]
runpy.run_path(%r, run_name="__main__")
"""


def s_one_destination_on_the_unified_engine(w):
    """E1: a copy to one destination from the GUI (SRC DST) runs the
    4.2.15 engine (4.4.0b2 on: the fan-out engine's N=1 case is gone). The
    GUI must end it as it always ended them: done, verified, the bar at
    100%, and a history record with the run's files and bytes — and no
    several-destination result (fanout_result) for it."""
    import shutil
    here = os.path.dirname(os.path.abspath(__file__))
    eng = _REAL_ENGINE % os.path.join(here, "blitcp.py")
    holder = {}
    orig_build = type(w)._build_argv

    def argv_for(dry, sources, dest):
        src = sources[0]
        for i in range(3):
            with open(os.path.join(src, "f%d.bin" % i), "wb") as f:
                f.write(os.urandom(5000 + i))
        holder["dst"] = dest
        return orig_build(w, dry, sources, dest)

    saved_env = {k: os.environ.get(k) for k in ("BLITCP_ENGINE",
                                                "BLITCP_DEDUP_DB_DIR")}
    # "auto": a host without the xxhash package (a CI job) has no xxh128.
    saved_hash = w.hash_sel.currentText()
    w.hash_sel.setCurrentText("auto")
    dbdir = tempfile.mkdtemp(prefix="uat_gui_db_")
    os.environ.pop("BLITCP_ENGINE", None)          # the default: unified
    os.environ["BLITCP_DEDUP_DB_DIR"] = dbdir
    try:
        history, toasts, elapsed, still = _drive_stub(
            w, eng, None, argv_fn=argv_for)
    finally:
        w.hash_sel.setCurrentText(saved_hash)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    if still:
        return False, f"run never ended ({elapsed:.0f}s)"
    if len(history) != 1:
        return False, f"run recorded {len(history)} times"
    rec = history[0]
    st = rec.get("stats") or {}
    if rec.get("status") != "ok":
        return False, f"status {rec.get('status')!r}: {rec.get('error')!r}"
    # The engine's own verdict, from the transcript. On APFS (macOS CI) the
    # source and the destination share a volume, so the files are cloned
    # (clonefile) and verified by existence + size: content false, and the
    # GUI must then leave "verified" unset rather than claim it.
    import json as _json
    vr = None
    for ln in w._run_meta["fulllog"].lines():
        ln = ln.strip()
        if ln.startswith('{"t": "verify_result"'):
            vr = _json.loads(ln)
    if vr is None:
        return False, "the engine sent no verify_result"
    clones = vr.get("levels") == {"links": 3}
    want = None if clones else True
    if st.get("verified") is not want:
        return False, (f"verified {st.get('verified')!r} for verify_result "
                       f"{vr.get('content')!r} {vr.get('levels')!r}")
    if st.get("files") != 3 or not st.get("bytes"):
        return False, f"history stats {st!r} (expected 3 files and their bytes)"
    if w.stat_vals["Progress"].text() != "100%":
        return False, f"progress reads {w.stat_vals['Progress'].text()!r}"
    if not w.pstate.text().startswith("Done"):
        return False, f"status reads {w.pstate.text()!r}"
    if w._run_meta.get("fanout_result"):
        return False, "a fanout_result for one destination: not the 4.2.15 route"
    got = sorted(os.listdir(holder["dst"]))
    if [n for n in got if n.startswith("f")] != ["f0.bin", "f1.bin", "f2.bin"]:
        return False, f"destination holds {got!r}"
    shutil.rmtree(dbdir, ignore_errors=True)
    how = "cloned, size-checked, not claimed verified" if clones else "verified"
    return True, f"done, {how}, 3 files in history, {elapsed:.1f}s"


def s_pyside_known_bad_refused(w):
    """PySide6 6.12.0 drops a reference to None on every void Qt call; on
    Python < 3.12 the GUI aborts mid-run ("none_dealloc"). The GUI refuses
    to start on that combination with one clear line — no traceback — and
    starts on anything else. Driven through the real entry point against a
    PySide6 that reports 6.12.0."""
    import subprocess
    if not hasattr(g, "_pyside_refusal"):
        return False, "blitcp_gui has no PySide6 version guard"
    if g._pyside_refusal((3, 11), "6.12.0") is None:
        return False, "6.12.0 on Python 3.11 is not refused"
    for py, qt in (((3, 12), "6.12.0"), ((3, 11), "6.11.2"), ((3, 11), "6.12.1")):
        if g._pyside_refusal(py, qt) is not None:
            return False, f"PySide6 {qt} on Python {py} refused (it should start)"
    if sys.version_info >= (3, 12):
        return True, ("predicate checked; the end-to-end refusal needs a Python "
                      "older than 3.12 (CI's GUI suite runs on 3.11)")
    tmp = tempfile.mkdtemp(prefix="uat_gui_pyside_")
    os.makedirs(os.path.join(tmp, "PySide6"))
    with open(os.path.join(tmp, "PySide6", "__init__.py"), "w") as f:
        f.write('__version__ = "6.12.0"\n')
    env = dict(os.environ, PYTHONPATH=tmp)
    r = subprocess.run([sys.executable, os.path.join(os.path.dirname(
        os.path.abspath(g.__file__)), "blitcp_gui.py")], env=env,
        capture_output=True, text=True, timeout=120)
    out = r.stdout + r.stderr
    if r.returncode != 1:
        return False, f"exit {r.returncode}, expected 1: {out[-300:]!r}"
    if "Traceback" in out:
        return False, f"refused with a traceback: {out[-300:]!r}"
    if "6.12.0" not in out or len(out.strip().splitlines()) != 1:
        return False, f"not one clear line: {out[-300:]!r}"
    return True, "refused in one line: " + out.strip()[:90]


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
    if not sys.platform.startswith("linux"):
        return None, "the GIO hook acts on Linux only"
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


def s_picker_sidebar(w):
    """The Linux folder picker's sidebar (KDE: Qt's own dialog lists no
    drives): home, each mounted user volume from /proc/self/mountinfo —
    labels with spaces decoded — the udisks2 parent folders, "/" last;
    never a snap, squashfs, tmpfs or /boot mount."""
    mi = "\n".join([
        "22 1 8:2 / / rw,relatime shared:1 - ext4 /dev/sda2 rw",
        "23 22 8:1 / /boot rw shared:2 - ext4 /dev/sda1 rw",
        "24 22 8:3 / /boot/efi rw shared:3 - vfat /dev/sda3 rw",
        "25 22 7:0 / /snap/core22/1380 ro shared:4 - squashfs /dev/loop0 ro",
        "26 22 0:25 / /run rw shared:5 - tmpfs tmpfs rw",
        "27 22 0:26 / /media/kai/scratch rw shared:6 - tmpfs tmpfs rw",
        "28 22 8:17 / /run/media/kai/CF\\040AVA\\040Beetle\\0403 rw,nosuid "
        "shared:7 - exfat /dev/sdb1 rw",
        "29 22 8:33 / /media/kai/My\\040Backup rw shared:8 master:1 - ntfs3 "
        "/dev/sdc1 rw",
        "30 22 8:49 / /media/USBSTICK rw shared:9 - vfat /dev/sdd1 rw",
        "31 22 8:65 / /mnt/usb1 rw shared:10 - xfs /dev/sde1 rw",
        "32 31 8:66 / /mnt/usb1/inner rw shared:11 - ext4 /dev/sde2 rw",
        "33 22 0:40 / /proc rw shared:12 - proc proc rw",
        "34 22 0:41 / /run/user/1000/doc rw shared:13 - fuse.portal portal rw",
        "35 22 7:1 / /media/snapd-thing ro shared:14 - squashfs /dev/loop1 ro",
    ])
    have = {"/run/media/kai", "/media/kai"}
    got = g._picker_sidebar_paths(mi, "kai", "/home/kai",
                                  isdir=lambda p: p in have)
    want = ["/home/kai", "/media/USBSTICK", "/media/kai/My Backup",
            "/mnt/usb1", "/run/media/kai/CF AVA Beetle 3",
            "/run/media/kai", "/media/kai", "/"]
    if got != want:
        return False, f"sidebar {got!r}, expected {want!r}"
    # Nothing mounted, no udisks2 folders: home and "/".
    bare = g._picker_sidebar_paths("", "kai", "/home/kai",
                                   isdir=lambda p: False)
    if bare != ["/home/kai", "/"]:
        return False, f"empty mountinfo: {bare!r}"
    return True, "spaces decoded; snap/squashfs/tmpfs/boot/portal left out; '/' last"


def s_picker_one_path(w):
    """Every local folder picker goes through _pick_local_dir; on KDE it is
    Qt's own dialog with the drives in its sidebar, and the source row gets
    the picked folder as before."""
    import inspect
    src = inspect.getsource(g)
    if src.count("QFileDialog.getExistingDirectory(") != 1:
        return False, "a folder picker calls QFileDialog directly"
    if not sys.platform.startswith("linux"):
        return None, "the KDE sidebar is Linux only"
    seen = {}
    real = g.QFileDialog
    pick = os.path.realpath(tempfile.mkdtemp(prefix="uat_pick_"))

    class Probe(real):
        def exec(self):
            seen["native"] = not self.testOption(
                real.Option.DontUseNativeDialog)
            seen["dirs"] = self.fileMode() == real.FileMode.Directory
            seen["side"] = [u.toLocalFile() for u in self.sidebarUrls()]
            self.selectFile(pick)
            return 1

        def selectedFiles(self):
            return [pick]
    old_de = os.environ.get("XDG_CURRENT_DESKTOP")
    os.environ["XDG_CURRENT_DESKTOP"] = "KDE"
    g.QFileDialog = Probe
    kept = w.sources
    try:
        w.sources = [{"t": "Local", "p": ""}]
        w.browse_source_path(0)
        got = w.sources[0]["p"]
    finally:
        g.QFileDialog = real
        w.sources = kept
        w.render_sources()
        if old_de is None:
            os.environ.pop("XDG_CURRENT_DESKTOP", None)
        else:
            os.environ["XDG_CURRENT_DESKTOP"] = old_de
    if seen.get("native") is not False or not seen.get("dirs"):
        return False, f"KDE dialog: {seen}"
    side = seen.get("side") or []
    if side[:1] != [os.path.expanduser("~")] or side[-1:] != ["/"]:
        return False, f"sidebar {side!r}"
    if got != pick:
        return False, f"source row got {got!r}"
    return True, f"KDE: Qt dialog, folders only, sidebar {len(side)} places"


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
    files. Takes the engine's Host as the real backends do (keyword-only,
    required): a GUI that builds a backend without it fails here too."""
    def __init__(self, spec, args, creds=None, *, host):
        if host is not g.fc.HOST:
            raise TypeError("the backend was not given the engine's Host")

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

        @staticmethod
        def nullDevice():
            # The engine's stdin is the null device (_start_engine): it can
            # never wait for input nobody can give.
            return os.devnull

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
        ("local dest", web, "/tmp/x", "Local", "SSH or SMB"),  # nosemgrep: blitcp-test-literal-tmp — a destination the test expects to be REFUSED; nothing is written
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


# ── section 5: one source, several destinations (--dest fan-out) ──────────

def _fan_reset(w):
    _reset_opts(w)
    w.extra_dests = []
    w.dest_type = "Local"
    w.dst_input.setText("/d")
    w.render_extra_dests()


def s_fanout_argv(w):
    """Every destination becomes a --dest, and nothing --dest refuses is
    sent — --deep-verify included (it went with the fan-out engine), even
    for a destination saved with the old "deep" mark."""
    _fan_reset(w)
    dests = [{"t": "Local", "p": "/d", "deep": False},
             {"t": "SSH", "p": "h:/x", "deep": False},
             {"t": "Cloud", "p": "az://c/v", "deep": True}]
    argv = w._build_fanout_argv(["/s"], dests)
    pairs = [argv[i + 1] for i, a in enumerate(argv) if a == "--dest"]
    bad = [f for f in ("--dry-run", "--use-sudo", "--index-existing",
                       "--dedup-existing", "--ssh-no-sftp", "--deep-verify")
           if f in argv]
    ok = (pairs == ["/d", "h:/x", "az://c/v"]
          and "--progress-json" in argv and argv[0] == "/s" and not bad)
    return ok, "3 × --dest, no --deep-verify" if ok else f"got {argv}"


def s_fanout_refusals(w):
    """What --dest refuses is said in the GUI, before any process starts:
    sudo, and a source that is not local (owner's decision (b),
    2026-11-23). A preview (dry run) of several destinations is NOT refused:
    the GUI starts it."""
    _fan_reset(w)
    toasts, started = [], []
    w.show_toast = lambda msg, *a, **k: toasts.append(msg)
    w._start_engine = lambda *a, **k: started.append(a)
    saved_src = w.sources
    try:
        w.sources = [{"t": "Local", "p": "/s"}]
        w.extra_dests = [{"t": "Local", "p": "/e", "deep": False}]
        w.run_transfer(dry=True)
        n_dry = len(toasts)
        w.flag_opts["--use-sudo"].setProperty("active", True)
        w.run_transfer(dry=False)
        w.flag_opts["--use-sudo"].setProperty("active", False)
        n_sudo = len(toasts) - n_dry
        w.sources = [{"t": "SSH", "p": "u@h:/data"}]
        w.run_transfer(dry=False)
        n_remote = len(toasts) - n_dry - n_sudo
        said = toasts[-1] if toasts else ""
    finally:
        w.sources = saved_src
        w.__dict__.pop("show_toast", None)
        w.__dict__.pop("_start_engine", None)
        _fan_reset(w)
    ok = (n_dry == 0 and n_sudo == 1 and n_remote == 1 and len(started) == 1
          and "several destinations take a local source" in said)
    return ok, ("preview started; sudo and a remote source refused before "
                "any process" if ok
                else f"toasts={toasts} started={len(started)}")


def _ev(w, obj):
    import json as _json
    w._handle_out_line(_json.dumps(obj))


def s_fanout_rows(w):
    """One row per destination from fanout_start; progress, ETA and phase
    from sink_progress; the verdict, the level and the actions from
    fanout_result — and "Verified" only where content was compared."""
    _fan_reset(w)
    w._run_meta = {"dry": False, "verified": None, "fanout": True,
                   "last": None, "fulllog": None}
    _ev(w, {"t": "fanout_start", "files": 4, "total_bytes": 4000,
            "sinks": [{"sink": 1, "dest": "/a", "kind": "local",
                       "level": "direct", "deep_verify": False,
                       "stalled": None},
                      {"sink": 2, "dest": "az://c/v", "kind": "cloud",
                       "level": "size-nosum", "deep_verify": False,
                       "stalled": None}]})
    rows = getattr(w, "sink_rows", {})
    if sorted(rows) != [1, 2]:
        return False, f"rows {sorted(rows)}"
    _ev(w, {"t": "sink_progress", "sink": 1, "dest": "/a", "phase": "copying",
            "bytes_done": 1000, "bytes_total": 4000, "files_done": 1,
            "files_total": 4, "rate": 500.0, "eta": 6.0, "reason": None})
    txt = rows[1]["stats"].text()
    if "25%" not in txt or "6s" not in txt:
        return False, f"progress row: {txt!r}"
    _ev(w, {"t": "fanout_result", "exit": 0, "sinks": [
        {"sink": 1, "dest": "/a", "state": "ok", "verdict": "verified",
         "manifest": "0001_a.mhl", "levels": {"direct": 4}, "content": True,
         "written": 4, "failed": 0, "verify_failed": 0, "refused": 0},
        {"sink": 2, "dest": "az://c/v", "state": "ok", "verdict": "verified",
         "manifest": "0001_v.mhl", "levels": {"size-nosum": 4},
         "content": False, "written": 4, "failed": 0, "verify_failed": 0,
         "refused": 0}]})
    v1, v2 = rows[1]["verdict"].text(), rows[2]["verdict"].text()
    ok = ("Verified" in v1 and "Verified" not in v2
          and "content not compared" in v2
          and rows[1]["verify_later"].isVisibleTo(w)
          and rows[1]["manifest_btn"].isVisibleTo(w))
    return ok, "rows, ETA, honest verdicts, actions" if ok else f"{v1!r} / {v2!r}"


def s_fanout_presets(w):
    """A destination list saved as a preset comes back as it was; a preset
    holds paths only, never a credential."""
    _fan_reset(w)
    store = {}
    w._load_gui_settings = lambda: dict(store)
    w._save_gui_settings = lambda data: store.update(data)
    try:
        w.dest_type = "Local"
        w.dst_input.setText("/mnt/a")
        w.extra_dests = [{"t": "SSH", "p": "nas:/backup", "deep": False},
                         {"t": "Cloud", "p": "az://media/x", "deep": True}]
        w.save_dest_preset("card offload")
        w.extra_dests = []
        w.dst_input.setText("/elsewhere")
        w.load_dest_preset("card offload")
    finally:
        w.__dict__.pop("_load_gui_settings", None)
        w.__dict__.pop("_save_gui_settings", None)
    got = [(d["t"], d["p"], d["deep"]) for d in w.extra_dests]
    # A local path is saved in the native form (_normalize_local): on
    # Windows "/mnt/a" comes back as "\\mnt\\a".
    dst = w.dst_input.text()
    ok = (dst == "/mnt/a".replace("/", os.sep)
          and got == [("SSH", "nas:/backup", False),
                      ("Cloud", "az://media/x", True)]
          and "password" not in repr(store).lower())
    _fan_reset(w)
    return ok, "preset round-trips" if ok else f"dst={dst} extras={got}"


def s_fanout_history(w):
    """A fan-out run in history comes back with every destination."""
    _fan_reset(w)
    w.extra_dests = [{"t": "SMB", "p": "smb://nas/share/x", "deep": False}]
    cfg = w._current_config()
    w.extra_dests = []
    w.navigate = lambda *a, **k: None
    try:
        w._run_again(cfg)
    finally:
        w.__dict__.pop("navigate", None)
    ok = [d["p"] for d in w.extra_dests] == ["smb://nas/share/x"]
    _fan_reset(w)
    return ok, "history keeps the destination list" if ok else f"got {w.extra_dests}"


def s_fanout_end_to_end(w):
    """A fan-out run through the real process path: a stub engine that
    speaks the fan-out events; the rows fill in, the run ends once, the
    toast says verified only because every destination compared content."""
    import json as _json
    import time
    from PySide6.QtCore import QEventLoop, QTimer
    _fan_reset(w)
    tmp = tempfile.mkdtemp(prefix="uat_gui_fan_")
    eng = os.path.join(tmp, "stub_engine.py")
    evs = [
        {"t": "fanout_start", "files": 2, "total_bytes": 20, "sinks": [
            {"sink": 1, "dest": "/a", "kind": "local", "level": "direct",
             "deep_verify": False, "stalled": None},
            {"sink": 2, "dest": "s3://b/x", "kind": "cloud",
             "level": "server-crc", "deep_verify": False, "stalled": None}]},
        {"t": "sink_progress", "sink": 1, "dest": "/a", "phase": "copying",
         "bytes_done": 20, "bytes_total": 20, "files_done": 2,
         "files_total": 2, "rate": 10.0, "eta": 0.0, "reason": None},
        {"t": "fanout_result", "exit": 0, "sinks": [
            {"sink": 1, "dest": "/a", "state": "ok", "verdict": "verified",
             "manifest": "m1.mhl", "levels": {"direct": 2}, "content": True,
             "written": 2},
            {"sink": 2, "dest": "s3://b/x", "state": "ok",
             "verdict": "verified", "manifest": "m2.mhl",
             "levels": {"server-crc": 2}, "content": True, "written": 2}]}]
    with open(eng, "w", encoding="utf-8") as f:
        f.write("import json, sys\n")
        for e in evs:
            f.write("print(json.dumps(%r), flush=True)\n" % (e,))
    toasts, history = [], []
    w._fc_script_path = lambda: eng
    w._build_fanout_argv = lambda s, d: []
    w.show_toast = lambda msg, *a, **k: toasts.append(msg)
    w._write_history = lambda rec: history.append(rec)
    saved = w.sources
    try:
        w.sources = [{"t": "Local", "p": tmp}]
        w.extra_dests = [{"t": "Cloud", "p": "s3://b/x", "deep": False}]
        w.running = False
        t0 = time.monotonic()
        w.run_transfer(dry=False)
        loop = QEventLoop()
        poll = QTimer()
        poll.timeout.connect(lambda: (not w.running or
                                      time.monotonic() - t0 > 60) and loop.quit())
        poll.start(50)
        loop.exec()
        poll.stop()
    finally:
        for k in ("_fc_script_path", "_build_fanout_argv", "show_toast",
                  "_write_history"):
            w.__dict__.pop(k, None)
        w.sources = saved
    rows = dict(w.sink_rows)
    ok = (sorted(rows) == [1, 2] and not w.running and len(history) == 1
          and "Verified" in rows[2]["verdict"].text()
          and any("verified" in t for t in toasts)
          and history[0]["config"].get("extra_dests"))
    _fan_reset(w)
    return ok, "rows filled from the engine, one finish, verified" if ok else \
        f"rows={sorted(rows)} toasts={toasts} hist={len(history)}"


def _fan_rows(w, sinks):
    w._run_meta = {"dry": False, "verified": None, "fanout": True,
                   "last": None, "fulllog": None}
    _ev(w, {"t": "fanout_start", "files": 1, "total_bytes": 1, "sinks": [
        {"sink": i, "dest": d, "kind": "cloud" if "://" in d else "local",
         "level": "direct", "deep_verify": deep, "stalled": None}
        for i, (d, deep) in enumerate(sinks, 1)]})


def s_fanout_verdict_honest(w):
    """An exit-0 fan-out run is never "verify failed" for being up to date
    or not verified; only a destination that failed verification (or a
    source that changed) is (section-5 review)."""
    _fan_reset(w)
    got = {}
    for name, verdicts, contents in (
            ("up_to_date", ["up_to_date", "up_to_date"], [False, False]),
            ("not_verified", ["not_verified", "verified"], [False, True]),
            ("size_only", ["verified", "verified"], [True, False]),
            ("all_content", ["verified", "verified"], [True, True]),
            ("one_bad", ["verified", "verify_failed"], [True, True])):
        _fan_rows(w, [("/a", False), ("/b", False)])
        _ev(w, {"t": "fanout_result", "exit": 0 if name != "one_bad" else 1,
                "sinks": [{"sink": i, "dest": d, "state": "ok", "verdict": v,
                           "content": c, "levels": {}, "written": 1,
                           "manifest": "m.mhl"}
                          for i, (d, v, c) in enumerate(
                              zip(("/a", "/b"), verdicts, contents), 1)]})
        got[name] = w._run_meta["verified"]
    want = {"up_to_date": None, "not_verified": None, "size_only": None,
            "all_content": True, "one_bad": False}
    ok = got == want
    _fan_reset(w)
    return ok, "verified only when every destination compared content" \
        if ok else f"got {got}"


def s_fanout_verify_later_honest(w):
    """Verify again later: --deep for a destination marked for it, and an
    exit 0 that compared no content is said as such — never "match"."""
    _fan_reset(w)
    _fan_rows(w, [("/a", False), ("az://c/v", True)])
    a1, a2 = w._verify_again_args(1), w._verify_again_args(2)
    m_sizes = w._verify_again_message(
        {"exit": 0, "files": 1, "ok": 1, "content": False,
         "levels": {"size-nosum": 1}}, 0)
    m_match = w._verify_again_message(
        {"exit": 0, "files": 1, "ok": 1, "content": True,
         "levels": {"direct": 1}}, 0)
    ok = ("--deep" not in a1 and "--deep" in a2
          and "match" not in m_sizes and "content not compared" in m_sizes
          and "match the manifest" in m_match)
    _fan_reset(w)
    return ok, "--deep where marked; size-only never a match" if ok else \
        f"{a1} {a2} {m_sizes!r} {m_match!r}"


def s_fanout_bar_ignores_failed(w):
    """A destination that fails mid-run no longer holds the overall bar."""
    _fan_reset(w)
    _fan_rows(w, [("/a", False), ("/b", False)])
    for sink, phase, done in ((1, "copying", 10), (2, "failed", 10),
                              (1, "copying", 90)):
        _ev(w, {"t": "sink_progress", "sink": sink, "dest": "x",
                "phase": phase, "bytes_done": done, "bytes_total": 100,
                "files_done": 0, "files_total": 1, "rate": 0, "eta": None,
                "reason": None})
    got = w.stat_vals["Progress"].text()
    _fan_reset(w)
    return got == "90%", "overall follows the live destinations" \
        if got == "90%" else f"got {got}"


def s_fanout_preset_keeps_first_deep(w):
    """A preset keeps its first destination; one saved with the old
    --deep-verify mark sends no --deep-verify (it went with the fan-out
    engine, owner's decision (a), 2026-11-23)."""
    _fan_reset(w)
    store = {}
    w._load_gui_settings = lambda: dict(store)
    w._save_gui_settings = lambda data: store.update(data)
    try:
        w.dst_input.setText("")
        w.extra_dests = [{"t": "Cloud", "p": "az://media/run", "deep": True},
                         {"t": "Local", "p": "/mnt/b", "deep": False}]
        w.save_dest_preset("card")
        w.extra_dests = []
        w.load_dest_preset("card")
        argv = w._build_fanout_argv(["/s"], w._all_dests())
    finally:
        w.__dict__.pop("_load_gui_settings", None)
        w.__dict__.pop("_save_gui_settings", None)
    dests = [argv[i + 1] for i, a in enumerate(argv) if a == "--dest"]
    _fan_reset(w)
    ok = (dests[:1] == ["az://media/run"] and "/mnt/b" in dests
          and "--deep-verify" not in argv)
    return ok, "the first destination kept, no --deep-verify" if ok \
        else f"argv={argv}"


def _xn_workspace():
    """A source and two local destinations under ~/.cache/blitcp-tests (never
    /tmp), and the engine's own state kept there too."""
    import shutil
    base = os.path.join(os.path.expanduser("~/.cache/blitcp-tests"),
                        "uat-gui-xn-%d" % os.getpid())
    shutil.rmtree(base, ignore_errors=True)
    os.makedirs(os.path.join(base, "src", "sub"))
    os.makedirs(os.path.join(base, "db"))
    for i, n in enumerate(("a.txt", os.path.join("sub", "b.bin"))):
        with open(os.path.join(base, "src", n), "wb") as f:
            f.write(os.urandom(2000 + i))
    env = {"BLITCP_DEDUP_DB_DIR": os.path.join(base, "db"),
           "BLITCP_CREDENTIALS": os.path.join(base, "none.json"),
           "XDG_CONFIG_HOME": os.path.join(base, "cfg"),
           "BLITCP_NO_UPDATE_CHECK": "1"}
    return base, env


def _xn_run(w, dry):
    """run_transfer with the real engine (blitcp.py) to two local folders;
    (toasts, history, log text, base) — the caller removes base."""
    import time
    from PySide6.QtCore import QEventLoop, QTimer
    _fan_reset(w)
    base, env = _xn_workspace()
    old_env = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    toasts, history = [], []
    w.show_toast = lambda msg, *a, **k: toasts.append(msg)
    w._write_history = lambda rec: history.append(rec)
    saved, saved_dst = w.sources, w.dst_input.text()
    w.log_view.clear()
    try:
        w.sources = [{"t": "Local", "p": os.path.join(base, "src")}]
        w.dest_type = "Local"
        w.dst_input.setText(os.path.join(base, "A"))
        w.extra_dests = [{"t": "Local", "p": os.path.join(base, "B"),
                          "deep": False}]
        w.running = False
        t0 = time.monotonic()
        w.run_transfer(dry=dry)
        loop = QEventLoop()
        poll = QTimer()
        poll.timeout.connect(lambda: (not w.running or
                                      time.monotonic() - t0 > 120)
                             and loop.quit())
        poll.start(50)
        loop.exec()
        poll.stop()
    finally:
        for k in ("show_toast", "_write_history"):
            w.__dict__.pop(k, None)
        w.sources = saved
        w.dst_input.setText(saved_dst)
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return toasts, history, w.log_view.toPlainText(), base


def s_xn_real_engine_rows(w):
    """Several destinations on the 4.2.15 engine (owner's decision (d)),
    through the real process path and the real engine: a row per
    destination, each filled and verified, one finish, the files there."""
    import shutil
    toasts, history, _log, base = _xn_run(w, dry=False)
    try:
        rows = dict(w.sink_rows)
        there = all(os.path.isfile(os.path.join(base, d, "sub", "b.bin"))
                    for d in ("A", "B"))
        # The engine's own verdict per destination. On APFS (macOS CI) the
        # source and both destinations share a volume, so every file is
        # cloned and checked by existence + size: no content compared, and
        # the GUI must then say so instead of "Verified".
        sinks = (w._run_meta.get("fanout_result") or {}).get("sinks") or []
        clones = len(sinks) == 2 and all(
            sk.get("levels") == {"links": 2} for sk in sinks)
        if clones:
            said = (all("content not compared" in rows[k]["verdict"].text()
                        for k in rows)
                    and not any("verified" in t for t in toasts))
        else:
            said = (all("Verified" in rows[k]["verdict"].text() for k in rows)
                    and any("verified" in t for t in toasts))
        ok = (sorted(rows) == [1, 2] and not w.running and len(history) == 1
              and said and all(rows[k].get("pct") == 100.0 for k in rows)
              and there)
        what = (f"rows={[(k, r['verdict'].text(), r.get('pct')) for k, r in rows.items()]} "
                f"history={len(history)} toasts={toasts} there={there} "
                f"levels={[sk.get('levels') for sk in sinks]}")
    finally:
        _fan_reset(w)
        shutil.rmtree(base, ignore_errors=True)
    how = "cloned, size-checked, not claimed verified" if clones else "verified"
    return ok, f"two rows filled by the real engine, {how}, one finish" \
        if ok else what


def s_xn_real_engine_preview(w):
    """The preview of several destinations (owner's decision (d)): the real
    engine's dry run of each destination in the log, under its [k]
    heading; nothing written."""
    import shutil
    _toasts, _history, log, base = _xn_run(w, dry=True)
    try:
        a, b = os.path.join(base, "A"), os.path.join(base, "B")
        ok = (log.count("DRY RUN — Copy strategy") == 2
              and ("[1] " + a) in log and ("[2] " + b) in log
              and not os.path.exists(a) and not os.path.exists(b)
              and not w.running)
        what = f"log tail={log[-600:]!r}"
    finally:
        _fan_reset(w)
        shutil.rmtree(base, ignore_errors=True)
    return ok, "each destination's dry run, nothing written" if ok else what


def s_fanout_verify_later_after_new_run(w):
    """A re-check that ends after a new run began still says its result."""
    _fan_reset(w)
    _fan_rows(w, [("/a", False)])
    toasts = []
    w.show_toast = lambda msg, *a, **k: toasts.append(msg)
    try:
        row = w.sink_rows[1]
        w._fanout_reset_rows(show=True)          # a new run replaced the rows
        QApplication.processEvents()
        w._verify_again_done(1, row, {"exit": 0, "files": 1, "ok": 1,
                                      "content": True,
                                      "levels": {"direct": 1}}, 0)
    finally:
        w.__dict__.pop("show_toast", None)
    ok = len(toasts) == 1 and "match" in toasts[0]
    _fan_reset(w)
    return ok, "result still said, as a toast" if ok else f"toasts={toasts}"


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
    ("GUI-EOF-1", "engine crash: run ends once, after its output, as failed", s_engine_crash_ends_once),
    ("GUI-E1-1", "one destination runs the 4.2.15 engine: done, verified, history counts", s_one_destination_on_the_unified_engine),
    ("GUI-QT-1", "PySide6 6.12.0 on Python < 3.12: refused in one line", s_pyside_known_bad_refused),
    ("GUI-GIO-1", "frozen GUI's GIO hook isolates host gio modules", s_gio_hook),
    ("GUI-PICK-1", "Linux folder picker: mounted drives in the sidebar", s_picker_sidebar),
    ("GUI-PICK-2", "one folder picker; KDE gets Qt's dialog with drives", s_picker_one_path),
    ("GUI-VER-1", "one version everywhere; pin matches blitcp.py", s_version_single_source),
    ("GUI-BROWSE-1", "SMB browse: one level, off the UI thread, flat RSS", s_browse_smb_offthread),
    ("GUI-BROWSE-2", "silent SSH host: UI responsive, error after timeout", s_browse_ssh_unresponsive),
    ("GUI-BROWSE-5", "silent SSH host: both paramiko errors read as the timeout", s_browse_ssh_silent_both_spellings),
    ("GUI-BROWSE-3", "navigating away mid-listing drops the stale result", s_browse_stale_dropped),
    ("GUI-BROWSE-4", "listing capped with a count; Cancel drops a late answer", s_browse_cap_and_cancel),
    ("GUI-FAN-1", "several destinations: one --dest each, no --deep-verify", s_fanout_argv),
    ("GUI-FAN-2", "what --dest refuses is refused in the GUI, before any process", s_fanout_refusals),
    ("GUI-FAN-3", "one row per destination: progress, ETA, honest verdict, actions", s_fanout_rows),
    ("GUI-FAN-4", "destination presets round-trip, paths only", s_fanout_presets),
    ("GUI-FAN-5", "a fan-out run in history keeps every destination", s_fanout_history),
    ("GUI-FAN-6", "a fan-out run end to end through the real process path", s_fanout_end_to_end),
    ("GUI-FAN-7", "exit 0 is never 'verify failed' for up-to-date / not verified", s_fanout_verdict_honest),
    ("GUI-FAN-8", "Verify again later: --deep where marked, size-only never a match", s_fanout_verify_later_honest),
    ("GUI-FAN-9", "a destination failed mid-run no longer holds the overall bar", s_fanout_bar_ignores_failed),
    ("GUI-FAN-10", "a preset keeps its first destination; an old deep mark sends no --deep-verify", s_fanout_preset_keeps_first_deep),
    ("GUI-FAN-11", "a re-check ending after a new run still says its result", s_fanout_verify_later_after_new_run),
    ("GUI-FAN-12", "several destinations, the real engine: rows filled, verified, one finish", s_xn_real_engine_rows),
    ("GUI-FAN-13", "several destinations, the real engine's preview: each dry run, nothing written", s_xn_real_engine_preview),
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
