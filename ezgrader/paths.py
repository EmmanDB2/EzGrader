"""Where EzGrader's files live, whether it runs from source or as the packaged app.

From source, everything stays in the project folder (backups/ is gitignored). As the
packaged app, the bundled files are read-only, so EzGrader keeps its own files in the
usual per-user folder instead:
  macOS    ~/Library/Application Support/EzGrader
  Windows  %APPDATA%\\EzGrader
  Linux    ~/.local/share/EzGrader
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def frozen() -> bool:
    """True when running as the packaged app (PyInstaller)."""
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> Path:
    """Read-only files that ship with EzGrader (static/, demo fixtures)."""
    if frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return PROJECT_ROOT


def data_dir() -> Path:
    """EzGrader's own files: backups, the app's log, the running-instance note."""
    if not frozen():
        return PROJECT_ROOT
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home())
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "EzGrader"
