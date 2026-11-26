#!/usr/bin/env python3
"""UAT — i18n (see I18N_DESIGN.md §6).

Per language: run a tiny real copy with --lang XX and assert
  1. exit code 0,
  2. a translated pre-copy sentinel appears (catalog loaded & applied),
  3. the --log-file JSON stays English (machine output is never translated),
  4. no traceback leaked.
Plus a --help smoke test per language. English default asserts the absence
of translation (guards against accidental auto-activation).
"""
import os
import testtmp  # test data under ~/.cache/blitcp-tests, never /tmp (RAM)
testtmp.use_test_tmp()
import sys
import json
import shutil
import tempfile
import subprocess

# NOTE: unlike the other suites this one does NOT pin LC_ALL=C globally —
# testing languages is its entire job. Each invocation sets --lang explicitly,
# which beats every environment variable by design.

HERE = os.path.dirname(os.path.abspath(__file__))
FC = os.path.join(HERE, "blitcp.py")

SENTINELS = {
    # `blitcp SRC DST` takes the 4.2.15 route again (owner, 2026-11-20):
    # its Phase-1 banner, as 4.2.15's sentinel was.
    "en": "Phase 1 — Scanning source",
    "el": "Φάση 1 — Σάρωση πηγής",
    "de": "Phase 1 — Quelle wird gescannt",
    "it": "Fase 1 — Scansione della sorgente",
    "es": "Fase 1 — Escaneando el origen",
    "zh_CN": "阶段 1 — 扫描源",
    "ja": "フェーズ 1 — ソースをスキャン中",
}


class C:
    G, R, Y, X = "\033[92m", "\033[91m", "\033[93m", "\033[0m"


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


def main():
    _force_utf8_stdout()
    results = {"pass": 0, "fail": 0, "skip": 0}

    def check(name, ok, detail=""):
        results["pass" if ok else "fail"] += 1
        mark = f"{C.G}✓{C.X}" if ok else f"{C.R}✗{C.X}"
        print(f"  {mark} {name}" + (f"  — {detail}" if detail and not ok else ""))

    work = tempfile.mkdtemp(prefix="uat_i18n_")
    src = os.path.join(work, "src")
    os.makedirs(src)
    with open(os.path.join(src, "a.txt"), "w") as f:
        f.write("i18n test payload\n")

    env = dict(os.environ)
    # A hostile environment must not leak through an explicit --lang.
    env["LANG"] = "fr_FR.UTF-8"
    env.pop("LC_ALL", None)
    env.pop("BLITCP_LANG", None)

    for lang, sentinel in SENTINELS.items():
        dst = os.path.join(work, f"dst_{lang}")
        log = os.path.join(work, f"log_{lang}.json")
        p = subprocess.run(
            [sys.executable, FC, "--lang", lang, src, dst,
             "--no-cache", "--log-file", log],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120, env=env)
        # A reader thread that dies (an undecodable byte under a locale
        # codec, say) leaves the stream as None. Adding that to a string
        # raised TypeError three frames from the cause and hid it; an empty
        # capture is now a finding of its own, named as one.
        out = (p.stdout or "") + (p.stderr or "")
        check(f"[{lang}] output captured", p.stdout is not None,
              "the child's output could not be read at all "
              "(reader thread failed — check the decoding)")
        check(f"[{lang}] exit 0", p.returncode == 0, f"rc={p.returncode}")
        if lang == "en":
            ok = sentinel in out and SENTINELS["el"] not in out
            check("[en] output is English (no accidental translation)", ok)
        else:
            check(f"[{lang}] sentinel translated", sentinel in out,
                  "sentinel not found")
        check(f"[{lang}] no traceback", "Traceback" not in out)
        def _keys_ascii(obj):
            if isinstance(obj, dict):
                return all(k.isascii() and _keys_ascii(v) for k, v in obj.items())
            if isinstance(obj, list):
                return all(_keys_ascii(v) for v in obj)
            return True
        try:
            with open(log) as f:
                doc = json.load(f)
            english_log = (_keys_ascii(doc)
                           and str(doc.get("summary", {}).get("mode", "")).isascii())
        except (OSError, ValueError):
            english_log = False
        check(f"[{lang}] JSON log stays English", bool(english_log))

        h = subprocess.run([sys.executable, FC, "--lang", lang, "--help"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60, env=env)
        check(f"[{lang}] --help renders", h.returncode == 0 and "usage" in h.stdout)

    shutil.rmtree(work, ignore_errors=True)

    # Every _tr("…") literal in the engine and the GUI has a non-empty
    # translation in every catalog. Nothing else noticed a missing one: the
    # string simply rendered in English. Fails closed on a stub target.
    import ast
    import glob
    sys.path.insert(0, HERE)
    import build
    catalogs = sorted(glob.glob(os.path.join(HERE, "locales", "*",
                                             "LC_MESSAGES", "blitcp.po")))
    check("catalogs found", len(catalogs) >= 6, f"{len(catalogs)} catalogs")
    # The engine's modules (blitcp_cloud.py, blitcp_iface.py) translate
    # through the same _tr; each must hold at least the strings it has.
    for target, least in (("blitcp.py", 50), ("blitcp_cloud.py", 10),
                          ("blitcp_iface.py", 1), ("blitcp_gui.py", 50),
                          ("blitcp_dashboard.py", 50)):
        try:
            tree = ast.parse(open(os.path.join(HERE, target),
                                  encoding="utf-8").read())
        except (OSError, SyntaxError) as e:
            check(f"{target}: every _tr string translated", False, str(e))
            continue
        used = {n.args[0].value for n in ast.walk(tree)
                if isinstance(n, ast.Call)
                and (getattr(n.func, "id", None)
                     or getattr(n.func, "attr", None)) == "_tr" and n.args
                and isinstance(n.args[0], ast.Constant)
                and isinstance(n.args[0].value, str)}
        if len(used) < least:
            check(f"{target}: every _tr string translated", False,
                  f"only {len(used)} _tr strings — not the real {target}")
            continue
        for po in catalogs:
            lang = po.split(os.sep)[-3]
            try:
                cat = build._po_parse(po)
            except (ValueError, SystemExit) as e:   # build.py refuses a broken catalog
                check(f"[{lang}] {target}: catalog readable", False, str(e)[:200])
                continue
            miss = sorted(m for m in used if not cat.get(m))
            check(f"[{lang}] {target}: all {len(used)} _tr strings translated",
                  not miss, "missing: " + "; ".join(repr(m[:60]) for m in miss[:5]))

    total = results["pass"] + results["fail"]
    print("\n" + "=" * 62)
    tag = (f"{C.G} i18n UAT PASSED{C.X}" if results["fail"] == 0
           else f"{C.R} i18n UAT FAILED{C.X}")
    print(f"{tag} — {results['pass']} pass, {results['fail']} fail, "
          f"{results['skip']} skip")
    return 1 if results["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
