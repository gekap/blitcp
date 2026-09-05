#!/usr/bin/env python3
# Copyright 2026 George Kapellakis
# Licensed under the Apache License, Version 2.0
"""Unified UAT runner — runs the CLI, GUI and security suites and aggregates.

  python uat.py            # run all three, aggregate, exit 1 on any FAIL
  python uat.py --quiet    # only each suite's summary + the grand total

Auto-enables the SMB round-trip in the CLI suite when a local Samba share is
reachable (so it runs instead of skipping). Other skips are genuinely
environment-bound — see the table this prints at the end:
  * reflink/FIDEDUPERANGE  → needs a btrfs/XFS destination
  * off-mount index path   → needs a second filesystem mount
  * cloud (s3/az/gs)       → needs cloud credentials + endpoint
  * NTFS credential delete → needs an NTFS volume (Windows)
To exercise those, run the suites on a host that provides them (or set
FC_UAT_SMB_URL / cloud creds in the environment).
"""
import os
import re
import sys
import shutil
import argparse
import subprocess

# i18n guard (I18N_DESIGN.md, M0): this suite asserts on English output.
# Pin the C locale for this process and every child it spawns so future
# translations can never break (or falsely pass) these checks.
os.environ["LC_ALL"] = "C"
os.environ["LANG"] = "C"
os.environ.pop("LANGUAGE", None)
os.environ.pop("BLITCP_LANG", None)

HERE = os.path.dirname(os.path.abspath(__file__))
SUITES = [("CLI", "uat_blitcp.py"),
          ("GUI", "uat_gui.py"),
          ("Security", "uat_security.py"),
          ("i18n", "uat_i18n.py")]
_SUMMARY = re.compile(r"(\d+)\s+pass,\s+(\d+)\s+fail(?:,\s+(\d+)\s+skip)?", re.I)


class C:
    if sys.stdout.isatty() and not os.environ.get("NO_COLOR"):
        B = "\033[1m"; R = "\033[31m"; G = "\033[32m"; Y = "\033[33m"; CY = "\033[36m"; X = "\033[0m"
    else:
        B = R = G = Y = CY = X = ""


def _detect_mounts(env):
    """Maximise coverage by pointing the suites at real filesystems:
      • TMPDIR  → a reflink-capable (xfs/btrfs) writable mount, so the
                  FIDEDUPERANGE in-place dedup test runs instead of skipping.
      • FC_UAT_OTHER_MOUNT → a second, different-device mount, so the off-mount
                  --index-existing test runs.
    Also moves aside any existing dedup DB on the TMPDIR mount so the index
    tests start clean, and restores it afterwards (returns a restore callback).
    Honors pre-set FC_UAT_OTHER_MOUNT / TMPDIR from the environment."""
    import glob
    import shutil
    noop = lambda: None
    if env.get("FC_UAT_OTHER_MOUNT") and env.get("TMPDIR"):
        return "from environment", noop
    cands = []
    try:
        with open("/proc/mounts") as f:
            for ln in f:
                p = ln.split()
                if len(p) >= 3 and p[1].startswith("/") and p[1] not in ("/", "/boot"):
                    if os.access(p[1], os.W_OK) and p[2] in (
                            "xfs", "btrfs", "ext4", "ext3", "ext2", "f2fs"):
                        try:
                            cands.append((p[1], p[2], os.stat(p[1]).st_dev))
                        except OSError:
                            pass
    except OSError:
        return None, noop
    if not cands:
        return None, noop
    primary = ([c for c in cands if c[1] in ("xfs", "btrfs")] or cands)[0]
    other = next((c for c in cands if c[2] != primary[2]), None)
    tmpdir = os.path.join(primary[0], "uat_tmp")
    os.makedirs(tmpdir, exist_ok=True)
    env["TMPDIR"] = tmpdir
    if other:
        env["FC_UAT_OTHER_MOUNT"] = other[0]
    saved = []
    for q in glob.glob(os.path.join(primary[0], ".blitcp_dedup.db*")):
        bak = q + ".uatbak"
        shutil.move(q, bak)
        saved.append((bak, q))

    def restore():
        for q in glob.glob(os.path.join(primary[0], ".blitcp_dedup.db*")):
            if not q.endswith(".uatbak"):
                try:
                    os.remove(q)
                except OSError:
                    pass
        for bak, orig in saved:
            shutil.move(bak, orig)
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass

    label = f"TMPDIR={tmpdir} ({primary[1]})"
    if other:
        label += f" · off-mount={other[0]} ({other[1]})"
    return label, restore


def _detect_smb(env):
    """Set FC_UAT_SMB_* if a local Samba share answers, so the CLI suite runs the
    SMB round-trip rather than skipping. Returns a label or None."""
    if env.get("FC_UAT_SMB_URL"):
        return env["FC_UAT_SMB_URL"] + " (from environment)"
    if not shutil.which("smbclient"):
        return None
    url, user, pw = "smb://127.0.0.1/fcshare", "kai", "kapellakis"
    try:
        r = subprocess.run(
            ["smbclient", "//127.0.0.1/fcshare", "-U", f"{user}%{pw}", "-c", "ls"],
            capture_output=True, timeout=15)
        if r.returncode == 0:
            env["FC_UAT_SMB_URL"] = url
            env["FC_UAT_SMB_USER"] = user
            env["FC_UAT_SMB_PASS"] = pw
            return url + " (local Samba)"
    except Exception:
        pass
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quiet", action="store_true",
                    help="only print each suite's summary line + the grand total")
    args = ap.parse_args(argv)

    env = dict(os.environ)
    smb = _detect_smb(env)
    mounts, _restore_db = _detect_mounts(env)

    print(f"{C.B}{'='*66}{C.X}")
    print(f"{C.B} blitcp — unified UAT  (CLI + GUI + Security){C.X}")
    print(f" SMB: {C.G + smb + C.X if smb else C.Y + 'not reachable — SMB round-trip will skip' + C.X}")
    print(f" FS:  {C.G + mounts + C.X if mounts else C.Y + 'single-FS — off-mount & reflink tests will skip' + C.X}")
    print(f"{C.B}{'='*66}{C.X}")

    tot = {"pass": 0, "fail": 0, "skip": 0}
    rows = []
    for name, script in SUITES:
        path = os.path.join(HERE, script)
        if not os.path.isfile(path):
            rows.append((name, None, None, None, "missing"))
            print(f"\n{C.Y}▶ {name}: {script} not found{C.X}")
            continue
        print(f"\n{C.CY}{'─'*66}{C.X}\n{C.CY}▶ {name}  ({script}){C.X}\n{C.CY}{'─'*66}{C.X}")
        # stdin=DEVNULL for the same reason the individual runs use it: a
        # child that reaches /dev/tty for a passphrase stalls the suite behind
        # a prompt no pipe can see.
        proc = subprocess.run([sys.executable, path], env=env,
                              capture_output=True, text=True,
                              stdin=subprocess.DEVNULL)
        out = proc.stdout + proc.stderr
        m = None
        for line in out.splitlines():
            mm = _SUMMARY.search(line)
            if mm:
                m = mm
        if args.quiet:
            # print just the suite's summary section
            for line in out.splitlines():
                if "PASS" in line or "FAIL" in line or _SUMMARY.search(line) or "===" in line:
                    pass
            tail = [l for l in out.splitlines() if _SUMMARY.search(l)]
            print("  " + (tail[-1].strip() if tail else "(no summary)"))
        else:
            print(out.rstrip())
        if m:
            p, f, s = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
            tot["pass"] += p; tot["fail"] += f; tot["skip"] += s
            rows.append((name, p, f, s, ""))
        else:
            rows.append((name, None, None, None, f"no summary (exit {proc.returncode})"))
            tot["fail"] += 1 if proc.returncode else 0

    try:
        _restore_db()          # put back any dedup DB we moved aside on the test mount
    except Exception:
        pass

    print(f"\n{C.B}{'='*66}{C.X}")
    print(f"{C.B} GRAND TOTAL{C.X}")
    print(f"{C.B}{'='*66}{C.X}")
    for name, p, f, s, note in rows:
        if p is None:
            print(f"  {C.Y}{name:<10} — {note}{C.X}")
        else:
            col = C.R if f else C.G
            print(f"  {col}{name:<10} {p:>3} pass  {f:>2} fail  {s:>2} skip{C.X}")
    verdict = f"{C.R}UAT FAILED{C.X}" if tot["fail"] else f"{C.G}ALL UAT PASSED{C.X}"
    print(f"\n {verdict} — {tot['pass']} pass, {tot['fail']} fail, {tot['skip']} skip")
    if tot["skip"]:
        print(f" {C.Y}(skips need real infra: btrfs/XFS · 2nd mount · cloud creds · "
              f"NTFS/Windows){C.X}")
    return 1 if tot["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
