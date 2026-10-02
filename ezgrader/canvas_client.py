"""Canvas LMS client: courses, assignments, students, submissions, and grade updates.

Endpoints checked against https://canvas.instructure.com/doc/api/ (Oct 2026):
  GET  /api/v1/users/self
  GET  /api/v1/courses?enrollment_state=active&include[]=term
  GET  /api/v1/courses/:id/assignment_groups?include[]=assignments
  GET  /api/v1/courses/:id/assignments/:aid
  GET  /api/v1/courses/:id/users?enrollment_type[]=student
       (include[]=email is no longer documented; we still ask and fall back to login_id)
  GET  /api/v1/courses/:id/assignments/:aid/submissions
  PUT  /api/v1/courses/:id/assignments/:aid/submissions/:user_id
       submission[posted_grade]=<points | NN%>, or submission[excuse]=true
"""

from __future__ import annotations

import time
from urllib.parse import urlparse

import requests

STAFF_ROLES = {"teacher", "ta", "designer"}

HINTS = {
    401: "Canvas rejected the token. It may be expired, revoked, or copied wrong.",
    403: "Canvas accepted the token, but you don't have permission for this.",
    404: "Canvas says this doesn't exist (wrong ID or no access).",
}


class CanvasError(Exception):
    pass


def normalize_base_url(url: str) -> str:
    """'canvas.school.edu/' or 'https://canvas.school.edu/api/v1' -> 'https://canvas.school.edu'."""
    url = (url or "").strip()
    if not url:
        raise ValueError("Enter your school's Canvas address, e.g. https://canvas.school.edu")
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("The Canvas address must start with https://")
    if not parsed.hostname or "." not in parsed.hostname or parsed.username or parsed.password:
        raise ValueError("That doesn't look like a Canvas address.")
    port = f":{parsed.port}" if parsed.port else ""
    return f"https://{parsed.hostname.lower()}{port}"


def _error_message(resp: requests.Response) -> str:
    detail = ""
    try:
        body = resp.json()
        errors = body.get("errors") if isinstance(body, dict) else None
        if isinstance(errors, list) and errors:
            detail = "; ".join(str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in errors)
        elif isinstance(errors, dict):
            detail = "; ".join(f"{k}: {v}" for k, v in errors.items())
        elif isinstance(body, dict) and body.get("message"):
            detail = str(body["message"])
    except ValueError:
        pass
    base = HINTS.get(resp.status_code, f"Canvas returned HTTP {resp.status_code}.")
    return f"{base} ({detail[:200]})" if detail else base


class CanvasClient:
    def __init__(self, base_url: str, token: str, session: requests.Session | None = None,
                 sleep=time.sleep, max_retries: int = 4):
        self.base_url = normalize_base_url(base_url)
        self.api = f"{self.base_url}/api/v1"
        self.session = session or requests.Session()
        self._headers = {"Authorization": f"Bearer {token}"}
        self._sleep = sleep
        self._max_retries = max_retries

    # ---------- plumbing

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        if not url.startswith("http"):
            url = f"{self.api}{url}"
        # Never send the token anywhere except the configured Canvas host.
        if not url.startswith(self.base_url + "/"):
            raise CanvasError("Canvas pointed us at a different host. Refusing to send the token there.")
        for attempt in range(self._max_retries + 1):
            try:
                resp = self.session.request(method, url, headers=self._headers, timeout=30, **kwargs)
            except requests.RequestException as e:
                raise CanvasError(f"Couldn't reach Canvas ({type(e).__name__}).") from None
            throttled = resp.status_code == 429 or (
                resp.status_code == 403 and "rate limit" in (resp.text or "").lower())
            if throttled and attempt < self._max_retries:
                self._sleep(2 ** attempt)
                continue
            if not resp.ok:
                raise CanvasError(_error_message(resp))
            return resp
        raise CanvasError("Canvas kept rate limiting. Try again in a minute.")

    def _json(self, method: str, path: str, **kwargs):
        resp = self._request(method, path, **kwargs)
        try:
            return resp.json()
        except ValueError:
            raise CanvasError("Canvas sent back something that isn't JSON. Check the Canvas address.") from None

    def _get_all(self, path: str, params: dict | list | None = None) -> list:
        """GET every page, following Link: rel="next"."""
        params = list(params.items()) if isinstance(params, dict) else list(params or [])
        params.append(("per_page", "100"))
        out, url, first = [], path, True
        while url:
            resp = self._request("GET", url, params=params if first else None)
            try:
                page = resp.json()
            except ValueError:
                raise CanvasError("Canvas sent back something that isn't JSON. Check the Canvas address.") from None
            if not isinstance(page, list):
                raise CanvasError("Canvas sent back an unexpected response.")
            out.extend(page)
            url = (resp.links.get("next") or {}).get("url")
            first = False
        return out

    # ---------- reads

    def whoami(self) -> dict:
        me = self._json("GET", "/users/self")
        return {"name": me.get("name", ""), "login_id": me.get("login_id", "")}

    def courses(self) -> list[dict]:
        raw = self._get_all("/courses", [("enrollment_state", "active"), ("include[]", "term")])
        out = []
        for c in raw:
            if c.get("access_restricted_by_date") or not c.get("name"):
                continue
            roles = sorted({e.get("type") for e in c.get("enrollments") or [] if e.get("type")})
            out.append({
                "id": c["id"],
                "name": c["name"],
                "code": c.get("course_code") or "",
                "term": (c.get("term") or {}).get("name") or "",
                "roles": roles,
            })
        staff = [c for c in out if STAFF_ROLES & set(c["roles"])]
        return staff or out

    def assignments(self, course_id: int) -> list[dict]:
        groups = self._get_all(f"/courses/{int(course_id)}/assignment_groups", [("include[]", "assignments")])
        out = []
        for g in sorted(groups, key=lambda g: g.get("position") or 0):
            for a in sorted(g.get("assignments") or [], key=lambda a: a.get("position") or 0):
                out.append({
                    "id": a["id"],
                    "name": a.get("name") or f"Assignment {a['id']}",
                    "group": g.get("name") or "",
                    "points_possible": a.get("points_possible"),
                    "grading_type": a.get("grading_type"),
                    "published": a.get("published", True),
                    "due_at": a.get("due_at"),
                })
        return out

    def assignment(self, course_id: int, assignment_id: int) -> dict:
        a = self._json("GET", f"/courses/{int(course_id)}/assignments/{int(assignment_id)}")
        return {
            "id": a["id"],
            "name": a.get("name") or "",
            "points_possible": a.get("points_possible"),
            "grading_type": a.get("grading_type"),
            "published": a.get("published", True),
            "moderated_grading": bool(a.get("moderated_grading")),
            "anonymous_grading": bool(a.get("anonymous_grading")),
            "post_manually": bool(a.get("post_manually")),
            "html_url": a.get("html_url") or "",
        }

    def students(self, course_id: int) -> list[dict]:
        raw = self._get_all(f"/courses/{int(course_id)}/users",
                            [("enrollment_type[]", "student"), ("include[]", "email")])
        return [{
            "id": u["id"],
            "name": u.get("name") or "",
            "sortable_name": u.get("sortable_name") or u.get("name") or "",
            "email": u.get("email") or "",
            "login_id": u.get("login_id") or "",
        } for u in raw if u.get("id")]

    def submissions(self, course_id: int, assignment_id: int) -> list[dict]:
        return self._get_all(f"/courses/{int(course_id)}/assignments/{int(assignment_id)}/submissions")

    # ---------- writes

    def _update_submission(self, course_id: int, assignment_id: int, user_id: int, data: dict) -> dict:
        return self._json(
            "PUT", f"/courses/{int(course_id)}/assignments/{int(assignment_id)}/submissions/{int(user_id)}",
            data=data)

    def set_grade(self, course_id: int, assignment_id: int, user_id: int, posted_grade: str) -> dict:
        """posted_grade: '85.5' (points), '85.5%' (percent of points_possible), or '' (clear)."""
        return self._update_submission(course_id, assignment_id, user_id, {"submission[posted_grade]": posted_grade})

    def excuse(self, course_id: int, assignment_id: int, user_id: int) -> dict:
        return self._update_submission(course_id, assignment_id, user_id, {"submission[excuse]": "true"})
