#!/usr/bin/env python3
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
import subprocess


def install_deps():
    """Install build dependencies."""
    deps = {
        "pyinstaller": "PyInstaller",
        "xxhash": "xxhash",
        "paramiko": "paramiko",
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


def build_target(name, script):
    """Build a single target with PyInstaller."""
    ext = ".exe" if platform.system() == "Windows" else ""
    out = f"{name}{ext}"

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--name", name,
        "--clean",
        "--noupx",
        "--console",
        "--hidden-import=xxhash",
        "--hidden-import=paramiko",
    ]

    cmd.append(script)

    print(f"\n  Building {out}...")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode == 0:
        binary = os.path.join("dist", out)
        size_mb = os.path.getsize(binary) / (1024 * 1024)
        print(f"  OK: {binary} ({size_mb:.1f} MB)")
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

    # Build
    print("\nBuilding CLI executable...")
    success = build_target("fast_copy", "fast_copy.py")

    # Summary
    ext = ".exe" if platform.system() == "Windows" else ""
    print(f"\n{'-' * 50}")
    if success:
        print(f"Build complete:\n")
        print(f'  dist/fast_copy{ext}')
        print(f'  Usage: fast_copy "C:\\Source" "E:\\Dest"')
        print(f'         fast_copy /source /dest')
    else:
        print("Build failed.")
    print()


if __name__ == "__main__":
    main()
