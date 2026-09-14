# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the NCM converter GUI.

Build it from the directory that contains this file, which is also where the entry script
lives — PyInstaller chdir()s to the spec's directory before evaluating it, so keeping both
together is what makes the relative paths below resolve:

    cd src
    python -m PyInstaller --noconfirm --clean --distpath ../dist --workpath ../build ncm_gui.spec

numpy is deliberately NOT bundled: the GUI works without it (ncm_core falls back to a
pure-Python XOR, measured at ~160 MB/s) and pulling it in would add roughly 23 MB of
OpenBLAS to satisfy an optional accelerator.
"""

block_cipher = None

a = Analysis(
    ["ncmdump_gui.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=[
        "ncmdump",
        "ncmdump.metadata",
        "ncmdump.ncm_core",
        "ncmdump.ncm2mp3",
        "ncmdump.tags",
        "ncmdump.aes_lite",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Qt ships far more than this app uses; dropping the unused modules keeps the build
    # around a third of the size.
    excludes=[
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
        "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DAnimation",
        "PySide6.Qt3DExtras", "PySide6.Qt3DInput", "PySide6.Qt3DLogic",
        "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtMultimedia",
        "PySide6.QtMultimediaWidgets", "PySide6.QtQuick", "PySide6.QtQuick3D",
        "PySide6.QtQml", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtBluetooth",
        "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtLocation", "PySide6.QtNfc",
        "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtPdf",
        "PySide6.QtPdfWidgets", "PySide6.QtPositioning", "PySide6.QtRemoteObjects",
        "PySide6.QtScxml", "PySide6.QtSensors", "PySide6.QtSerialPort",
        "PySide6.QtSpatialAudio", "PySide6.QtStateMachine", "PySide6.QtSvg",
        "PySide6.QtSvgWidgets", "PySide6.QtTextToSpeech", "PySide6.QtUiTools",
        "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.QtNetworkAuth",
        # optional accelerator: not shipped, see the module docstring
        "numpy",
        # unrelated to this app
        "tkinter", "matplotlib", "scipy", "pandas", "PIL", "cryptography",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="NCMConverter",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX is optional and changes the product depending on whether it happens to be
    # installed, which makes builds non-reproducible — so it stays off on purpose.
    upx=False,
    console=False,          # windowed app: no console flash
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,              # the icon is drawn at runtime (ncmdump_gui.make_icon)
)

# onedir rather than onefile: no per-launch self-extraction, so startup is ~1 s instead of
# several, and security software has far less to object to.
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="NCMConverter",
)
