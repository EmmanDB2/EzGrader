"""Local JSON backups of Canvas grades, written before every push so it can be reverted.

Backups hold student names, emails, and grades, so the folder is gitignored and
the files are readable by the current user only.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9]+-[0-9]+-(push|revert)-[0-9a-f]{6}$")


class BackupError(Exception):
    pass


class BackupStore:
    def __init__(self, directory: Path):
        self.dir = Path(directory)

    def _path(self, backup_id: str) -> Path:
        if not ID_RE.match(backup_id or ""):
            raise BackupError("Invalid backup id.")
        return self.dir / f"{backup_id}.json"

    def _write(self, path: Path, data: dict) -> None:
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def save(self, kind: str, profile: str, course_id: int, assignment: dict,
             entries: list[dict], reverts: str | None = None) -> dict:
        now = datetime.now(timezone.utc)
        backup_id = f"{now:%Y%m%d-%H%M%S}-{int(course_id)}-{int(assignment['id'])}-{kind}-{secrets.token_hex(3)}"
        data = {
            "id": backup_id,
            "kind": kind,
            "created_at": now.isoformat(),
            "profile": profile,
            "course_id": int(course_id),
            "assignment_id": int(assignment["id"]),
            "assignment_name": assignment.get("name", ""),
            "points_possible": assignment.get("points_possible"),
            "reverts": reverts,
            "reverted_at": None,
            "reverted_by": None,
            "entries": entries,
        }
        self._write(self._path(backup_id), data)
        return data

    def load(self, backup_id: str) -> dict:
        path = self._path(backup_id)
        if not path.exists():
            raise BackupError("That backup doesn't exist anymore.")
        return json.loads(path.read_text(encoding="utf-8"))

    def mark_reverted(self, backup_id: str, reverted_by: str) -> None:
        data = self.load(backup_id)
        data["reverted_at"] = datetime.now(timezone.utc).isoformat()
        data["reverted_by"] = reverted_by
        self._write(self._path(backup_id), data)

    def list(self, course_id: int | None = None, assignment_id: int | None = None) -> list[dict]:
        if not self.dir.exists():
            return []
        out = []
        for path in self.dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if course_id is not None and data.get("course_id") != course_id:
                continue
            if assignment_id is not None and data.get("assignment_id") != assignment_id:
                continue
            out.append({k: data.get(k) for k in (
                "id", "kind", "created_at", "profile", "course_id", "assignment_id",
                "assignment_name", "reverts", "reverted_at")} | {"count": len(data.get("entries") or [])})
        out.sort(key=lambda b: b["created_at"] or "", reverse=True)
        return out
