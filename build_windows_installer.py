#!/usr/bin/env python3
"""
Build PDFRedactOCR_Setup.exe on macOS (Apple Silicon or Intel) for Windows 10/11 x64.

Requirements:
    brew install nsis
    (sevenzip is installed automatically if not present)

What it does:
  1. Downloads Python 3.11 for Windows x64 (python-build-standalone; includes tkinter)
  2. Downloads PyMuPDF, pytesseract, and Pillow wheels for Windows x64
  3. Downloads and extracts Tesseract OCR (UB-Mannheim) for Windows x64
  4. Stages everything into _win_stage/staging/
  5. Compiles PDFRedactOCR_Setup.exe with makensis — fully self-contained, no extra installs
"""

import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

HERE  = Path(__file__).parent
STAGE = HERE / "_win_stage"
DIST  = HERE / "dist_windows"

PYTHON_URL    = (
    "https://github.com/astral-sh/python-build-standalone/releases/download"
    "/20260610/cpython-3.11.15%2B20260610-x86_64-pc-windows-msvc-install_only.tar.gz"
)
PYTHON_SHA256 = "3300c38edb37f73114cf553e6ffd8e6b6ea47226fa07ef83184fcd4e4e81e776"


# ── Helpers ───────────────────────────────────────────────────────────────────

def download(url: str, dest: Path, label: str, expected_sha256: str = "") -> None:
    print(f"  Downloading {label}...")
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as resp:
        data = resp.read()
    if expected_sha256:
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected_sha256:
            print(f"  WARNING: SHA256 mismatch for {label}")
            print(f"    expected: {expected_sha256}")
            print(f"    got:      {actual}")
    dest.write_bytes(data)
    print(f"  Saved {len(data) // 1024 // 1024} MB -> {dest.name}")


def pip_download_wheels(packages: list, dest_dir: Path) -> list:
    """Download wheels for all packages including transitive dependencies."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    print(f"  Downloading {', '.join(packages)} + dependencies for Windows x64...")
    subprocess.run(
        [sys.executable, "-m", "pip", "download",
         "--platform", "win_amd64",
         "--python-version", "311",
         "--only-binary", ":all:",
         "-d", str(dest_dir),
         ] + packages,
        check=True, capture_output=True,
    )
    wheels = list(dest_dir.glob("*.whl"))
    if not wheels:
        raise RuntimeError(f"No wheels downloaded for {packages}")
    print(f"  Downloaded {len(wheels)} wheel(s)")
    return wheels


def extract_wheel(whl: Path, site_packages: Path) -> None:
    with zipfile.ZipFile(whl) as z:
        z.extractall(site_packages)


def ensure_7z() -> str:
    for cmd in ("7zz", "7z", "7za"):
        path = shutil.which(cmd)
        if path:
            return path

    # Auto-install via Homebrew (already required for nsis)
    if shutil.which("brew"):
        print("  7-Zip not found — installing via Homebrew (one-time)...")
        result = subprocess.run(
            ["brew", "install", "sevenzip"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print("  brew install sevenzip failed:")
            print(result.stderr[-1000:])
            sys.exit(1)
        for cmd in ("7zz", "7z", "7za"):
            path = shutil.which(cmd)
            if path:
                print(f"  7-Zip installed: {path}")
                return path

    print("ERROR: Could not find or install 7-Zip.")
    print("       Run:  brew install sevenzip")
    sys.exit(1)


def fetch_tesseract_installer_url() -> str:
    """Return the latest UB-Mannheim Tesseract Windows 64-bit installer URL."""
    api = "https://api.github.com/repos/UB-Mannheim/tesseract/releases/latest"
    req = urllib.request.Request(api, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as resp:
        data = json.load(resp)
    for asset in data["assets"]:
        name = asset["name"]
        if "w64-setup" in name and name.endswith(".exe"):
            return asset["browser_download_url"]
    raise RuntimeError("Could not find Tesseract Windows 64-bit installer in latest release.\n"
                       f"  Release tag: {data.get('tag_name')}\n"
                       f"  Assets: {[a['name'] for a in data['assets']]}")


def stage_tesseract(tools_dir: Path, staging_dir: Path) -> None:
    """Download Tesseract for Windows, extract with 7z, stage into staging/tesseract/."""
    print("  Fetching latest Tesseract release info from GitHub...")
    tess_url  = fetch_tesseract_installer_url()
    tess_name = tess_url.split("/")[-1]
    tess_exe  = tools_dir / tess_name

    if not tess_exe.exists():
        download(tess_url, tess_exe, f"Tesseract OCR Windows x64 ({tess_name})")

    extract_dir = tools_dir / "tesseract_extracted"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir()

    sevenzip = ensure_7z()
    print("  Extracting Tesseract with 7-Zip...")
    result = subprocess.run(
        [sevenzip, "x", str(tess_exe), f"-o{extract_dir}", "-y"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print("7-Zip ERROR:")
        print(result.stdout[-2000:])
        print(result.stderr[-500:])
        sys.exit(1)

    # Locate tesseract.exe in the extracted tree
    tess_bin = next(extract_dir.rglob("tesseract.exe"), None)
    if not tess_bin:
        print("ERROR: tesseract.exe not found in extracted Tesseract installer.")
        print(f"  Extracted to: {extract_dir}")
        print(f"  Contents: {list(extract_dir.iterdir())}")
        sys.exit(1)

    tess_root = tess_bin.parent
    dest      = staging_dir / "tesseract"

    # Copy the Tesseract tree, excluding NSIS plugin dir and uninstaller
    shutil.copytree(
        tess_root, dest,
        ignore=shutil.ignore_patterns("$PLUGINSDIR", "$*", "unins*"),
    )

    # Ensure tessdata/eng.traineddata is present
    eng_data = dest / "tessdata" / "eng.traineddata"
    if not eng_data.exists():
        # Some extractions put .traineddata at the root — move it
        root_traineddata = list(dest.glob("*.traineddata"))
        if root_traineddata:
            (dest / "tessdata").mkdir(exist_ok=True)
            for td in root_traineddata:
                shutil.move(str(td), dest / "tessdata" / td.name)
            print(f"  Moved {len(root_traineddata)} traineddata file(s) to tessdata/")

    eng_data = dest / "tessdata" / "eng.traineddata"
    if eng_data.exists():
        print(f"  Tesseract staged with English language data  ({eng_data.stat().st_size // 1024 // 1024} MB)")
    else:
        print(f"  WARNING: eng.traineddata not found at {eng_data}")
        print("  OCR may not work correctly.")


def write_nsis_script(staging_dir: Path, nsis_script: Path) -> None:
    script = f"""\
; PDF Redactor — NSIS Installer Script
Unicode True

!define APP_NAME   "PDF Redactor"
!define INST_DIR   "$LOCALAPPDATA\\PDFRedactor"
!define UNINST_KEY "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\PDFRedactor"

Name          "${{APP_NAME}}"
OutFile       "PDFRedactOCR_Setup.exe"
InstallDir    "${{INST_DIR}}"
RequestExecutionLevel user
SetCompressor /SOLID lzma

; ── Pages ─────────────────────────────────────────────────────────────────────
Page instfiles

; ── Installer ─────────────────────────────────────────────────────────────────
Section "Install"
  SetOutPath "$INSTDIR"
  File /r "{staging_dir}\\*.*"

  ; Desktop shortcut (pythonw.exe = no console window)
  CreateShortcut \\
    "$DESKTOP\\PDF Redactor.lnk" \\
    "$INSTDIR\\python\\pythonw.exe" \\
    '"$INSTDIR\\pdf_redactor_gui.py"' \\
    "$INSTDIR\\python\\pythonw.exe" 0 \\
    SW_SHOWNORMAL "" "PDF Redactor"

  ; Start Menu shortcuts
  CreateDirectory "$SMPROGRAMS\\PDF Redactor"
  CreateShortcut \\
    "$SMPROGRAMS\\PDF Redactor\\PDF Redactor.lnk" \\
    "$INSTDIR\\python\\pythonw.exe" \\
    '"$INSTDIR\\pdf_redactor_gui.py"' \\
    "$INSTDIR\\python\\pythonw.exe" 0 \\
    SW_SHOWNORMAL "" "PDF Redactor"

  WriteUninstaller "$INSTDIR\\Uninstall.exe"
  CreateShortcut \\
    "$SMPROGRAMS\\PDF Redactor\\Uninstall PDF Redactor.lnk" \\
    "$INSTDIR\\Uninstall.exe"

  ; Add/Remove Programs entry
  WriteRegStr   HKCU "${{UNINST_KEY}}" "DisplayName"     "${{APP_NAME}}"
  WriteRegStr   HKCU "${{UNINST_KEY}}" "UninstallString" "$INSTDIR\\Uninstall.exe"
  WriteRegStr   HKCU "${{UNINST_KEY}}" "InstallLocation" "$INSTDIR"
  WriteRegStr   HKCU "${{UNINST_KEY}}" "Publisher"       "PDF Redactor"
  WriteRegDWORD HKCU "${{UNINST_KEY}}" "NoModify"        1
  WriteRegDWORD HKCU "${{UNINST_KEY}}" "NoRepair"        1

  MessageBox MB_OK "PDF Redactor has been installed.$\\n$\\nShortcuts added to Desktop and Start Menu."
SectionEnd

; ── Uninstaller ───────────────────────────────────────────────────────────────
Section "Uninstall"
  Delete "$DESKTOP\\PDF Redactor.lnk"
  RMDir /r "$SMPROGRAMS\\PDF Redactor"
  RMDir /r "$INSTDIR"
  DeleteRegKey HKCU "${{UNINST_KEY}}"
SectionEnd
"""
    nsis_script.write_text(script, encoding="utf-8")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("\n=== PDF Redactor — Windows Installer Builder ===\n")

    if not shutil.which("makensis"):
        print("ERROR: makensis not found.")
        print("       Run:  brew install nsis")
        sys.exit(1)

    app_source = HERE / "pdf_redactor_gui.py"
    if not app_source.exists():
        print(f"ERROR: pdf_redactor_gui.py not found in {HERE}")
        sys.exit(1)

    # Clean staging, keep dist
    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)
    DIST.mkdir(parents=True, exist_ok=True)

    staging_dir = STAGE / "staging"
    staging_dir.mkdir()
    tools_dir = STAGE / "tools"
    tools_dir.mkdir()

    # ── 1. Python for Windows ─────────────────────────────────────────────────
    print("[1/5] Fetching Python 3.11 for Windows x64 (includes tkinter)...")
    py_archive = STAGE / "python_win.tar.gz"
    download(PYTHON_URL, py_archive, "Python 3.11 Windows x64", PYTHON_SHA256)
    print("  Extracting Python...")
    with tarfile.open(py_archive, "r:gz") as tf:
        tf.extractall(STAGE)

    py_src = STAGE / "python"
    if not py_src.exists():
        candidates = [
            d for d in STAGE.iterdir()
            if d.is_dir() and d.name not in ("staging", "tools")
        ]
        if not candidates:
            print("ERROR: Could not locate extracted Python directory.")
            sys.exit(1)
        py_src = candidates[0]

    shutil.copytree(py_src, staging_dir / "python")
    site_packages = staging_dir / "python" / "Lib" / "site-packages"
    site_packages.mkdir(parents=True, exist_ok=True)
    print("  Python staged.")

    # ── 2. Python packages ────────────────────────────────────────────────────
    print("\n[2/5] Fetching Python packages for Windows x64...")
    wheels_dir = tools_dir / "wheels"
    wheels_dir.mkdir()
    wheels = pip_download_wheels(["pymupdf", "pytesseract", "pillow"], wheels_dir)
    for whl in wheels:
        extract_wheel(whl, site_packages)
        print(f"  Extracted {whl.name}")
    print("  All packages staged.")

    # ── 3. Tesseract OCR ─────────────────────────────────────────────────────
    print("\n[3/5] Bundling Tesseract OCR for Windows x64...")
    stage_tesseract(tools_dir, staging_dir)

    # ── 4. App source ─────────────────────────────────────────────────────────
    print("\n[4/5] Staging application...")
    shutil.copy(app_source, staging_dir / "pdf_redactor_gui.py")
    print("  Copied pdf_redactor_gui.py")

    # ── 5. Compile installer ──────────────────────────────────────────────────
    print("\n[5/5] Compiling installer with NSIS...")
    nsis_script = STAGE / "installer.nsi"
    write_nsis_script(staging_dir, nsis_script)

    result = subprocess.run(
        ["makensis", str(nsis_script)],
        cwd=str(STAGE),
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print("NSIS ERROR:")
        print(result.stdout[-3000:])
        print(result.stderr[-1000:])
        sys.exit(1)

    built_exe = STAGE / "PDFRedactOCR_Setup.exe"
    if not built_exe.exists():
        print("ERROR: PDFRedactOCR_Setup.exe was not created.")
        sys.exit(1)

    final_exe = DIST / "PDFRedactOCR_Setup.exe"
    shutil.move(str(built_exe), str(final_exe))

    size_mb = final_exe.stat().st_size / 1_048_576
    print(f"\n{'='*55}")
    print(f"  SUCCESS!")
    print(f"  {final_exe}")
    print(f"  Size: {size_mb:.1f} MB")
    print(f"{'='*55}\n")
    print("Transfer dist_windows/PDFRedactOCR_Setup.exe to the Windows machine.")
    print("Double-click to install — no Python, no Tesseract, no internet required.\n")


if __name__ == "__main__":
    main()
