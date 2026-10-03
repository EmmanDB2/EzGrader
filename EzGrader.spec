# PyInstaller recipe for the packaged EzGrader app.
#   macOS:   dist/EzGrader.app           (scripts/build-mac.sh builds it and makes a zip)
#   Windows: dist/EzGrader/EzGrader.exe  (build on Windows; PyInstaller can't cross-build)
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import copy_metadata

ROOT = Path(SPECPATH)
VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "ezgrader" / "__init__.py").read_text()).group(1)

datas = [
    (str(ROOT / "static"), "static"),
    (str(ROOT / "tests" / "fixtures"), "tests/fixtures"),  # synthetic data for --demo
    *copy_metadata("keyring"),
]
hiddenimports = ["keyring.backends.macOS", "keyring.backends.Windows", "keyring.backends.fail",
                 "keyring.backends.null", "keyring.backends.chainer"]

a = Analysis(
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "PIL", "PyInstaller"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="EzGrader",
    console=False,  # no terminal window: EzGrader lives in the browser tab
    icon=str(ROOT / "assets" / ("EzGrader.icns" if sys.platform == "darwin" else "EzGrader.ico")),
)
coll = COLLECT(exe, a.binaries, a.datas, name="EzGrader")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="EzGrader.app",
        icon=str(ROOT / "assets" / "EzGrader.icns"),
        bundle_identifier="io.github.emmandb2.ezgrader",
        version=VERSION,
        info_plist={
            "CFBundleDisplayName": "EzGrader",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSUIElement": True,  # no Dock icon; quit from the page or close the tab
            "NSHighResolutionCapable": True,
        },
    )
