# -*- mode: python ; coding: utf-8 -*-

import glob
import os
import sys

hiddenimports = [
    'keyboard',
    'sounddevice',
    'soundfile',
    'pyperclip',
    'tkinter',
    'tkinter.ttk',
    '_tkinter',
    'cffi',
    'numpy',
    'pystray',
    'PIL',
]
if sys.platform == 'win32':
    hiddenimports.append('winsound')
else:
    hiddenimports.append('Xlib')  # pystray's X11 tray backend

# On Linux, bundle a PortAudio copy staged in packaging/portaudio (see the
# CI workflow) so the artifacts are self-contained.  audio_recorder knows how
# to find it inside the frozen app.
binaries = []
for path in sorted(glob.glob(os.path.join('packaging', 'portaudio', 'libportaudio.so*'))):
    binaries.append((path, '.'))

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=[('assets/whisperlive.ico', 'assets'), ('assets/whisperlive.png', 'assets')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='WhisperLive',
    icon='assets/whisperlive.ico' if sys.platform == 'win32' else None,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # No console window (silent background app)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
