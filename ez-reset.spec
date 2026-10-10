# -*- mode: python ; coding: utf-8 -*-
import os

ROOT = SPECPATH

a = Analysis(
    [os.path.join(ROOT, 'src', 'ez_reset', '__main__.py')],
    pathex=[os.path.join(ROOT, 'src')],
    binaries=[],
    datas=[
        (os.path.join(ROOT, 'src', 'ez_reset', 'devices.xml'), 'ez_reset'),
        (os.path.join(ROOT, 'src', 'ez_reset', 'devices.xml'), '.'),
        (os.path.join(ROOT, 'assets'), 'assets'),
    ],
    hiddenimports=[
        'ez_reset',
        'ez_reset.devices',
        'ez_reset.control',
        'ez_reset.d4',
        'ez_reset.exceptions',
        'ez_reset.printer',
        'ez_reset.status',
        'ez_reset.transport',
        'ez_reset.utils',
        'ez_reset.win_usbprint',
        'ez_reset.win_usbprint.transport',
        'ez_reset.win_usbprint.winapi',
        'ez_reset.l5190',
        'ez_reset.l5190.constants',
        'ez_reset.l5190.port_resolver',
        'ez_reset.l5190.transport',
        'ez_reset.l5190.ctrl',
        'ez_reset.l5190.workflow',
        'win32file',
        'tkinter',
        'tkinter.ttk',
        'tkinter.messagebox',
        'tkinter.filedialog',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'PIL', 'numpy', 'win32print', 'win32api', 'win32gui', 'win32con',
        'ssl', 'socket', 'bz2', 'lzma', 'email', 'http', 'urllib', 'html',
        'unittest', 'pydoc', 'difflib', 'doctest', 'multiprocessing',
        'setuptools', 'distutils', 'asyncio', 'test', 'sqlite3', 'xmlrpc',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ez-reset',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[os.path.join(ROOT, 'assets', 'icon.ico')],
)
