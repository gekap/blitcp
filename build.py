#!/usr/bin/env python3
# Copyright 2026 George Kapellakis
# Licensed under the Apache License, Version 2.0
# See LICENSE file for details.
"""
Build script — compiles fast_copy into a standalone executable.

Usage:
  python build.py              # build CLI executable
  python build.py --clean      # clean build artifacts first

Output:
  dist/fast_copy       — CLI executable
"""

import os
import sys
import shutil
import platform
import importlib.util
import subprocess

# Optional cloud backends. When importable in the build environment they are
# bundled into the frozen binary (see cloud_collect_flags) so the standalone
# GUI/CLI — whose --fc-core engine runs inside the same embedded interpreter —
# can do s3://, az:// and gs:// transfers without a separate Python install.
# Maps the probe-import to the top packages PyInstaller must collect whole
# (these carry data files / metadata that import analysis alone would miss).
CLOUD_COLLECT = {
    "boto3": ["boto3", "botocore", "s3transfer"],
    "azure.storage.blob": ["azure.storage.blob", "azure.core"],
    "google.cloud.storage": ["google.cloud.storage", "google.cloud",
                             "google.api_core", "google.auth",
                             "google.resumable_media"],
    "smbclient": ["smbprotocol", "smbclient", "spnego", "pyasn1"],
}


def install_deps():
    """Install build dependencies."""
    deps = {
        "pyinstaller": "PyInstaller",
        "xxhash": "xxhash",
        "paramiko": "paramiko",
        "PySide6": "PySide6",
    }
    for pip_name, import_name in deps.items():
        try:
            __import__(import_name)
            print(f"  OK: {pip_name}")
        except ImportError:
            print(f"  Installing {pip_name}...")
            try:
                subprocess.check_call([
                    sys.executable, "-m", "pip", "install", pip_name, "--quiet",
                    "--disable-pip-version-check",
                ])
            except subprocess.CalledProcessError:
                subprocess.check_call([
                    sys.executable, "-m", "pip", "install", pip_name, "--quiet",
                    "--disable-pip-version-check", "--break-system-packages",
                ])
    # Optional cloud SDKs — best-effort so the binary ships with cloud support.
    # A failure here is non-fatal: the build proceeds and simply omits cloud.
    if os.path.exists("requirements-cloud.txt"):
        try:
            __import__("boto3")
            print("  OK: cloud SDKs (boto3/azure/gcs)")
        except ImportError:
            print("  Installing cloud SDKs (boto3/azure/gcs)...")
            for extra in ([], ["--break-system-packages"]):
                try:
                    subprocess.check_call([
                        sys.executable, "-m", "pip", "install", "-r",
                        "requirements-cloud.txt", "--quiet",
                        "--disable-pip-version-check", *extra,
                    ])
                    break
                except subprocess.CalledProcessError:
                    continue
            else:
                print("  WARN: cloud SDKs not installed — binary will omit "
                      "s3://, az:// and gs:// support")


def cloud_collect_flags():
    """`--collect-all` flags for whichever cloud SDKs are importable here."""
    flags, seen = [], set()
    for probe, pkgs in CLOUD_COLLECT.items():
        if importlib.util.find_spec(probe) is None:
            continue
        for pkg in pkgs:
            if pkg not in seen:
                seen.add(pkg)
                flags.append("--collect-all=" + pkg)
    return flags


def build_target(name, script, windowed=False, icon=None):
    """Build a single target with PyInstaller.

    windowed=True builds a GUI app (no console window; a .app bundle on macOS).
    icon is a path to a .ico (Windows) / .icns (macOS) for the executable icon.
    """
    ext = ".exe" if platform.system() == "Windows" else ""
    out = f"{name}{ext}"

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--name", name,
        "--clean",
        "--noupx",
        "--windowed" if windowed else "--console",
        "--hidden-import=xxhash",
        "--hidden-import=paramiko",
    ]
    cmd += cloud_collect_flags()
    if icon and os.path.exists(icon):
        cmd += ["--icon", icon]

    cmd.append(script)

    print(f"\n  Building {out}...")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode == 0:
        binary = os.path.join("dist", out)
        if not os.path.exists(binary):
            # macOS --windowed produces a .app bundle, not a loose file.
            app = os.path.join("dist", f"{name}.app")
            binary = app if os.path.exists(app) else binary
        if os.path.exists(binary):
            size_mb = os.path.getsize(binary) / (1024 * 1024) if os.path.isfile(binary) else 0.0
            print(f"  OK: {binary}" + (f" ({size_mb:.1f} MB)" if size_mb else ""))
        else:
            print(f"  OK: {name} (built)")
        return True
    else:
        print(f"  FAILED: {name}")
        if result.stderr:
            for line in result.stderr.strip().split('\n')[-5:]:
                print(f"    {line}")
        return False


def main():
    clean = "--clean" in sys.argv

    print(f"Fast Copy Builder - {platform.system()} ({platform.machine()})")
    print("-" * 50)

    if not os.path.exists("fast_copy.py"):
        print("  Error: fast_copy.py not found in current directory")
        sys.exit(1)

    # Install deps
    print("\nDependencies:")
    install_deps()

    # Clean
    if clean:
        for d in ("build", "dist", "__pycache__"):
            if os.path.exists(d):
                shutil.rmtree(d)
        for f in os.listdir("."):
            if f.endswith(".spec"):
                os.remove(f)
        print("\nCleaned build artifacts.")

    # App icon (green bolt): .ico on Windows, .icns on macOS, ignored on Linux.
    icon = None
    if platform.system() == "Windows" and os.path.exists("assets/fast-copy.ico"):
        icon = "assets/fast-copy.ico"
    elif platform.system() == "Darwin" and os.path.exists("assets/fast-copy.icns"):
        icon = "assets/fast-copy.icns"

    # Build
    print("\nBuilding CLI executable...")
    success = build_target("fast_copy", "fast_copy.py", icon=icon)

    # Build the GUI too, when its source is present (skippable via --no-gui).
    gui_src = "fast_copy_modern_gui.py"
    gui_built = False
    if "--no-gui" not in sys.argv and os.path.exists(gui_src):
        print("\nBuilding GUI executable...")
        # Best-effort: a GUI build failure must not sink the required CLI release.
        gui_built = build_target("fast_copy_gui", gui_src, windowed=True, icon=icon)
        if not gui_built:
            print("  WARNING: GUI build failed — continuing with CLI only.")

    # Summary
    ext = ".exe" if platform.system() == "Windows" else ""
    print(f"\n{'-' * 50}")
    if success:
        print(f"Build complete:\n")
        print(f'  dist/fast_copy{ext}')
        if gui_built:
            print(f'  dist/fast_copy_gui{ext}' +
                  ("  (or dist/fast_copy_gui.app on macOS)"
                   if platform.system() == "Darwin" else ""))
        print(f'  Usage: fast_copy "C:\\Source" "E:\\Dest"')
        print(f'         fast_copy /source /dest')
    else:
        print("Build failed.")
    print()


if __name__ == "__main__":
    main()
