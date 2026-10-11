# -*- mode: python ; coding: utf-8 -*-
# 可移植的打包配置：路径都相对于本 spec 文件所在目录，
# 在任何机器上执行 python -m PyInstaller DiskReport.spec 都能直接打包。

from pathlib import Path

BASE = Path(SPECPATH)  # SPECPATH 由 PyInstaller 注入


a = Analysis(
    [str(BASE / 'DiskReport.py')],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='DiskReport',
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
    version=str(BASE / 'version_info.txt'),
    icon=[str(BASE / 'app.ico')],
)
