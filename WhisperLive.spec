# -*- mode: python ; coding: utf-8 -*-

import glob
import importlib.util
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

# Optional ML extras (A10A11 s2 / D8): SAME artifact either way — when the
# extras are installed the native libs/data are bundled; when absent the frozen
# build simply lacks them and LOCAL hides with its reason at runtime (never a
# crash).  Spec-time find_spec only (no ML import executed here).
ml_datas = []
ml_binaries = []
if importlib.util.find_spec('faster_whisper') is not None:
    hiddenimports += ['faster_whisper', 'ctranslate2', 'ctranslate2.models',
                      'av', 'tokenizers', 'huggingface_hub']
    from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs
    ml_datas += collect_data_files('faster_whisper')     # bundles its internal ONNX (V)
    ml_binaries += collect_dynamic_libs('ctranslate2')   # ctranslate2.dll + _ext*.pyd (V)
if importlib.util.find_spec('onnxruntime') is not None:
    hiddenimports.append('onnxruntime')
    from PyInstaller.utils.hooks import collect_dynamic_libs as _collect_ort
    ml_binaries += _collect_ort('onnxruntime')           # capi DLL + pybind state (V)

# On Linux, bundle a PortAudio copy staged in packaging/portaudio (see the
# CI workflow) so the artifacts are self-contained.  audio_recorder knows how
# to find it inside the frozen app.
binaries = []
for path in sorted(glob.glob(os.path.join('packaging', 'portaudio', 'libportaudio.so*'))):
    binaries.append((path, '.'))
binaries += ml_binaries

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=[('assets/whisperlive.ico', 'assets'), ('assets/whisperlive.png', 'assets'),
           # D7: the pinned model manifest ships IN the binary's data (model_store
           # parses it from _MEIPASS/assets — users can never edit pins via config).
           ('assets/models_manifest.json', 'assets')] + ml_datas,
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
    # UPX stays off AVX-heavy natives (annex: UPX on these DLLs is historically
    # flaky — cheap insurance, marked [U] and exercised by the build gates).
    upx_exclude=['ctranslate2.dll', 'onnxruntime*.dll', '*.pyd', 'av.libs/*'],
    runtime_tmpdir=None,
    console=False,  # No console window (silent background app)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
