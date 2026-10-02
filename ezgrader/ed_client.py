"""Read-only client for the EdStem API. Adapted from ed_probe.py."""

from __future__ import annotations

import os

import requests

from .ed_parser import looks_like_results_csv

ED_REGION = os.environ.get("ED_REGION", "us")
ED_BASE = f"https://{ED_REGION}.edstem.org/api"

# completions MUST be 0: with completions=1 the cells are timestamps, not scores.
RESULTS_PARAMS = {
    "numbers": "0",
    "scores": "1",
    "students": "1",
    "completions": "0",
    "strategy": "best",
    "ignore_late": "0",
    "late_no_points": "1",
    "tz": "America/New_York",
}

HINTS = {
    401: "Ed rejected the token. It may be expired, revoked, or copied wrong.",
    403: "Ed accepted the token, but your role can't access this.",
    404: "Ed says this doesn't exist. Wrong ID, or Ed changed the endpoint.",
    429: "Ed is rate limiting. Wait a minute and try again.",
}


class EdError(Exception):
    pass


def explain(resp: requests.Response) -> str:
    return HINTS.get(resp.status_code, f"Ed returned HTTP {resp.status_code}.")


class EdClient:
    def __init__(self, token: str, session: requests.Session | None = None, base: str = ED_BASE):
        self.base = base
        self.session = session or requests.Session()
        self._headers = {"Authorization": f"Bearer {token}"}

    def _call(self, method: str, path: str, **kwargs) -> requests.Response:
        try:
            resp = self.session.request(method, f"{self.base}{path}", headers=self._headers,
                                        timeout=kwargs.pop("timeout", 30), **kwargs)
        except requests.RequestException as e:
            raise EdError(f"Couldn't reach Ed ({type(e).__name__}).") from None
        if not resp.ok:
            raise EdError(explain(resp))
        return resp

    def _json(self, path: str) -> dict:
        try:
            return self._call("GET", path).json()
        except ValueError:
            raise EdError("Ed sent back something that isn't JSON.") from None

    def whoami(self) -> dict:
        user = self._json("/user").get("user") or {}
        return {"name": user.get("name", ""), "email": user.get("email", "")}

    def courses(self) -> list[dict]:
        out = []
        for entry in self._json("/user").get("courses", []):
            c = entry.get("course") or {}
            if not c.get("id"):
                continue
            out.append({
                "id": c["id"],
                "code": c.get("code") or "",
                "name": c.get("name") or "",
                "year": c.get("year") or "",
                "session": c.get("session") or "",
                "status": c.get("status") or "",
                "role": (entry.get("role") or {}).get("role", ""),
            })
        staff = [c for c in out if c["role"] and c["role"] != "student"]
        courses = staff or out
        courses.sort(key=lambda c: (c["status"] == "archived", -int(c["id"])))
        return courses

    def lessons(self, course_id: int) -> list[dict]:
        data = self._json(f"/courses/{int(course_id)}/lessons")
        modules = {m.get("id"): m.get("name") or "" for m in data.get("modules", [])}
        return [{
            "id": l["id"],
            "title": l.get("title") or f"Lesson {l['id']}",
            "module": modules.get(l.get("module_id"), ""),
            "due_at": l.get("due_at"),
            "state": l.get("state") or "",
        } for l in data.get("lessons", []) if l.get("id")]

    def results_csv(self, lesson_id: int) -> str:
        resp = self._call("POST", f"/lessons/{int(lesson_id)}/results.csv", params=RESULTS_PARAMS, timeout=60)
        content_type = resp.headers.get("Content-Type", "").lower()
        # Decode explicitly: requests assumes ISO-8859-1 for text/* without a charset,
        # which mangles titles like "Parson’s problems".
        text = resp.content.decode("utf-8-sig", errors="replace")
        if "html" in content_type or "json" in content_type or not looks_like_results_csv(text):
            raise EdError(
                "Ed's results endpoint didn't return a CSV. It's undocumented and may have changed. "
                "Check that you can open this lesson's results in Ed."
            )
        return text
