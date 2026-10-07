"""
Build script to compile ez-reset into a standalone Windows executable (.exe).
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
SRC_DIR = ROOT_DIR / "src"
DIST_DIR = ROOT_DIR / "dist"
BUILD_DIR = ROOT_DIR / "build"
ASSETS_DIR = ROOT_DIR / "assets"

print(f"Building ez-reset from {SRC_DIR}...")

# 1. Clean previous build artifacts
for p in [DIST_DIR, BUILD_DIR]:
    if p.exists():
        print(f"Cleaning {p}...")
        shutil.rmtree(p, ignore_errors=True)

# 2. PyInstaller command
cmd = [
    sys.executable,
    "-m",
    "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onefile",
    "--windowed",
    "--name=ez-reset",
    f"--icon={ASSETS_DIR / 'icon.ico'}",
    f"--paths={SRC_DIR}",
    f"--add-data={SRC_DIR / 'ez_reset' / 'devices.xml'};ez_reset",
    f"--add-data={SRC_DIR / 'ez_reset' / 'devices.xml'};.",
    f"--add-data={ASSETS_DIR};assets",
    "--hidden-import=ez_reset",
    "--hidden-import=ez_reset.devices",
    "--hidden-import=ez_reset.control",
    "--hidden-import=ez_reset.d4",
    "--hidden-import=ez_reset.printer",
    "--hidden-import=ez_reset.status",
    "--hidden-import=ez_reset.transport",
    "--hidden-import=ez_reset.utils",
    "--hidden-import=ez_reset.win_usbprint",
    "--hidden-import=ez_reset.win_usbprint.transport",
    "--hidden-import=ez_reset.win_usbprint.winapi",
    "--hidden-import=ez_reset.l5190",
    "--hidden-import=ez_reset.l5190.constants",
    "--hidden-import=ez_reset.l5190.port_resolver",
    "--hidden-import=ez_reset.l5190.transport",
    "--hidden-import=ez_reset.l5190.ctrl",
    "--hidden-import=ez_reset.l5190.workflow",
    "--hidden-import=win32file",
    "--hidden-import=tkinter",
    "--hidden-import=tkinter.ttk",
    "--hidden-import=tkinter.messagebox",
    "--hidden-import=tkinter.filedialog",
    "--exclude-module=PIL",
    "--exclude-module=numpy",
    "--exclude-module=win32print",
    "--exclude-module=win32api",
    "--exclude-module=win32gui",
    "--exclude-module=win32con",
    "--exclude-module=ssl",
    "--exclude-module=socket",
    "--exclude-module=bz2",
    "--exclude-module=lzma",
    "--exclude-module=email",
    "--exclude-module=http",
    "--exclude-module=urllib",
    "--exclude-module=html",
    "--exclude-module=unittest",
    "--exclude-module=pydoc",
    "--exclude-module=difflib",
    "--exclude-module=doctest",
    "--exclude-module=multiprocessing",
    "--exclude-module=setuptools",
    "--exclude-module=distutils",
    "--exclude-module=asyncio",
    "--exclude-module=test",
    "--exclude-module=sqlite3",
    "--exclude-module=xmlrpc",
    str(SRC_DIR / "ez_reset" / "__main__.py"),
]

# Ensure icon and thumbnail are strictly formatted
try:
    from PIL import Image
    png_icon = ASSETS_DIR / "icon.png"
    ico_icon = ASSETS_DIR / "icon.ico"
    thumb_icon = ASSETS_DIR / "icon_42.png"
    if png_icon.exists():
        im = Image.open(png_icon).convert("RGBA")
        sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
        im.save(ico_icon, format="ICO", sizes=sizes, bitmap_format="bmp")
        im_42 = im.resize((42, 42), Image.Resampling.LANCZOS)
        im_42.save(thumb_icon, format="PNG")
        print(f"Generated Windows DIB Standard ICO and Thumbnail: {ico_icon}, {thumb_icon}")
except Exception as e:
    print(f"Warning: Could not regenerate icon: {e}")

print("Running command:")
print(" ".join(cmd))
res = subprocess.run(cmd, cwd=str(ROOT_DIR))

if res.returncode == 0:
    exe_path = DIST_DIR / "ez-reset.exe"
    if exe_path.exists():
        size_mb = exe_path.stat().st_size / (1024 * 1024)
        print(f"\nSUCCESS: Standalone executable created at:")
        print(f"  {exe_path} ({size_mb:.2f} MB)")

        # Create copy with display name EZ_Reset_Epson.exe
        epson_exe = DIST_DIR / "EZ_Reset_Epson.exe"
        try:
            shutil.copy2(exe_path, epson_exe)
            print(f"  Copied display executable: {epson_exe}")
        except Exception as e:
            print(f"  Note: {e}")

        # Copy assets folder next to dist for direct access fallback
        dist_assets = DIST_DIR / "assets"
        dist_assets.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ASSETS_DIR / "icon.ico", dist_assets / "icon.ico")
        shutil.copy2(ASSETS_DIR / "icon.png", dist_assets / "icon.png")
        shutil.copy2(ASSETS_DIR / "icon_42.png", dist_assets / "icon_42.png")

        # Refresh Windows Shell Icon Cache
        try:
            import ctypes
            ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x0000, None, None)
            print("Refreshed Windows Shell Icon Cache.")
        except Exception:
            pass
    else:
        print("\nERROR: PyInstaller completed but ez-reset.exe not found.")
        sys.exit(1)
else:
    print(f"\nERROR: PyInstaller failed with exit code {res.returncode}.")
    sys.exit(res.returncode)
