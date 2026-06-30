#!/usr/bin/env python3
"""
Build PDF Redactor.app and a drag-to-install .dmg for macOS.

Lightweight build: bundles Python + the app and its Python dependencies
(PyMuPDF, pytesseract, Pillow, tkinter). OCR uses Tesseract from the system
(install once with `brew install tesseract`); the app finds it automatically
and disables OCR gracefully if it is missing.

Requirements:
    pip install pyinstaller
    (uses the same Python you run this with — its tkinter is bundled)

Output:
    dist_macos/PDF Redactor.app
    dist_macos/PDFRedactor.dmg
"""

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
APP_NAME = "PDF Redactor"
BUNDLE_ID = "com.retromad.pdfredactor"
DIST = HERE / "dist_macos"
BUILD = HERE / "build_macos"


def main() -> None:
    app_source = HERE / "pdf_redactor_gui.py"
    if not app_source.exists():
        print(f"ERROR: {app_source} not found", file=sys.stderr)
        sys.exit(1)

    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True)

    # ── 1. Build the .app with PyInstaller ────────────────────────────────────
    print("[1/2] Building PDF Redactor.app with PyInstaller...")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--windowed",                 # produce a .app bundle, no console
        "--noconfirm",
        "--clean",
        "--name", APP_NAME,
        "--osx-bundle-identifier", BUNDLE_ID,
        "--distpath", str(DIST),
        "--workpath", str(BUILD),
        "--specpath", str(BUILD),
        "--hidden-import", "fitz",
        "--hidden-import", "fitz._fitz",
        "--hidden-import", "pytesseract",
        "--hidden-import", "PIL",
        str(app_source),
    ]
    result = subprocess.run(cmd, cwd=str(HERE))
    if result.returncode != 0:
        print("\nPyInstaller build FAILED — see output above.", file=sys.stderr)
        sys.exit(result.returncode)

    app_path = DIST / f"{APP_NAME}.app"
    if not app_path.exists():
        print(f"ERROR: {app_path} was not created", file=sys.stderr)
        sys.exit(1)

    # ── 2. Package the .app into a drag-to-install .dmg ───────────────────────
    print("\n[2/2] Packaging .dmg...")
    dmg_path = DIST / "PDFRedactor.dmg"
    staging = DIST / "dmg_staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    # Copy the .app and add an /Applications symlink so users can drag-to-install
    shutil.copytree(app_path, staging / app_path.name, symlinks=True)
    (staging / "Applications").symlink_to("/Applications")

    if dmg_path.exists():
        dmg_path.unlink()
    result = subprocess.run(
        ["hdiutil", "create",
         "-volname", APP_NAME,
         "-srcfolder", str(staging),
         "-ov", "-format", "UDZO",
         str(dmg_path)],
        capture_output=True, text=True,
    )
    shutil.rmtree(staging)
    if result.returncode != 0:
        print("hdiutil ERROR:\n" + result.stdout + result.stderr, file=sys.stderr)
        sys.exit(1)

    size_mb = dmg_path.stat().st_size / 1_048_576
    print(f"\n{'='*55}")
    print("  SUCCESS!")
    print(f"  {app_path}")
    print(f"  {dmg_path}  ({size_mb:.1f} MB)")
    print(f"{'='*55}\n")
    print("OCR requires Tesseract:  brew install tesseract")
    print("First launch: right-click the app → Open (unsigned app).")


if __name__ == "__main__":
    main()
