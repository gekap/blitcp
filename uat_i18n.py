#!/usr/bin/env python3
"""UAT — i18n (see I18N_DESIGN.md §6).

Per language: run a tiny real copy with --lang XX and assert
  1. exit code 0,
  2. the translated Phase-1 sentinel appears (catalog loaded & applied),
  3. the --log-file JSON stays English (machine output is never translated),
  4. no traceback leaked.
Plus a --help smoke test per language. English default asserts the absence
of translation (guards against accidental auto-activation).
"""
import os
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


def main():
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
            capture_output=True, text=True, timeout=120, env=env)
        out = p.stdout + p.stderr
        check(f"[{lang}] exit 0", p.returncode == 0, f"rc={p.returncode}")
        if lang == "en":
            ok = sentinel in out and "Φάση" not in out
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
                           capture_output=True, text=True, timeout=60, env=env)
        check(f"[{lang}] --help renders", h.returncode == 0 and "usage" in h.stdout)

    shutil.rmtree(work, ignore_errors=True)

    total = results["pass"] + results["fail"]
    print("\n" + "=" * 62)
    tag = (f"{C.G} i18n UAT PASSED{C.X}" if results["fail"] == 0
           else f"{C.R} i18n UAT FAILED{C.X}")
    print(f"{tag} — {results['pass']} pass, {results['fail']} fail, "
          f"{results['skip']} skip")
    return 1 if results["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
