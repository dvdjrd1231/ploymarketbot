# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for QC LEAN Bridge - SINGLE-FILE build.
# Build:  pyinstaller build.spec --noconfirm
# Output: dist/QC-LEAN-Bridge.exe   (one self-contained file - nothing else to copy)
#
# One-file is deliberate: a one-FOLDER build fails the moment someone copies just
# the .exe out of its folder (it can't find _internal\pythonXY.dll). One-file has
# no external dependencies, so it runs from anywhere on its own.

block_cipher = None

datas = [
    ('config/default_config.yaml', 'config'),   # bundled default (seeds user copy)
]

hiddenimports = [
    'core.sample_data_gen',
    'core.pipeline',
    'core.optimizers',
    'core.persistence',   # imported lazily inside functions
    'core.benchmark',     # imported lazily inside functions
    # Autonomous platform modules (imported lazily by the CLI/controller):
    'core.logging_setup',
    'core.events',
    'core.tick_source',
    'core.nonprint_engine',
    'core.linebreak_engine',
    'core.structure_features',
    'core.event_store',
    'core.research_export',
    'core.ingest',
    'core.prop_firm',
    'core.strategy_registry',
    'core.live_manager',
    'core.openclaw',
    'core.api_server',
]

# We build against PyQt6; keep PyQt5 (and other unused heavies) out of the bundle.
excludes = [
    'PyQt5', 'PySide2', 'PySide6', 'tkinter', 'matplotlib', 'scipy',
    'PIL', 'IPython', 'notebook', 'pytest',
]

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# Single-file: bundle scripts + binaries + datas all into one EXE.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='QC-LEAN-Bridge',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # GUI app (no console window)
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
