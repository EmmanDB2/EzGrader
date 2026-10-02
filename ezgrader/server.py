"""Flask app: the JSON API plus the single-page UI. Meant to be bound to 127.0.0.1 only.

Request guards (a page on another site could otherwise drive this local server):
  * Host header must be 127.0.0.1:<port> or localhost:<port>  (blocks DNS rebinding)
  * every /api/ call must carry X-EzGrader-Session, a random per-launch token that
    is only embedded in the page we serve  (blocks cross-site requests)
"""

from __future__ import annotations

import hmac
import logging
import math
import secrets
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Flask, Response, request
from werkzeug.exceptions import HTTPException

from . import grades
from .backups import BackupError, BackupStore
from .canvas_client import CanvasClient, CanvasError, normalize_base_url
from .ed_client import EdClient, EdError
from .ed_parser import ParseError, parse_results_csv
from .keystore import KeyStore, ProfileError

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
BACKUP_DIR = ROOT / "backups"

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")

log = logging.getLogger("ezgrader")


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---------- background jobs (push / revert)

class Jobs:
    """One push or revert at a time, run on a background thread, polled by the UI."""

    def __init__(self):
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def reserve(self, kind: str) -> str:
        with self._lock:
            if any(j["status"] in ("preparing", "running") for j in self._jobs.values()):
                raise ApiError("Another push or revert is still running.", 409)
            job_id = uuid.uuid4().hex
            self._jobs[job_id] = {"id": job_id, "kind": kind, "status": "preparing",
                                  "total": 0, "done": 0, "results": []}
            return job_id

    def cancel(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)

    def run(self, job_id: str, items: list[dict], work, on_done=None) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.update(status="running", total=len(items))
        threading.Thread(target=self._run, args=(job, items, work, on_done), daemon=True).start()

    def _run(self, job: dict, items: list[dict], work, on_done) -> None:
        for item in items:
            try:
                result = work(item)
            except CanvasError as e:
                result = {"ok": False, "error": str(e)}
            except Exception as e:  # keep going; report it on this student
                log.exception("Grade update failed")
                result = {"ok": False, "error": f"Unexpected error ({type(e).__name__})"}
            result = {"user_id": item["user_id"], "name": item.get("name", ""), **result}
            with self._lock:
                job["results"].append(result)
                job["done"] += 1
        if on_done:
            try:
                on_done(job)
            except Exception:
                log.exception("Job completion hook failed")
        with self._lock:
            job["status"] = "done"

    def get(self, job_id: str) -> dict:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise ApiError("Unknown job.", 404)
            return {**job, "results": list(job["results"])}


# ---------- helpers

def snapshot(student: dict, sub: dict | None) -> dict:
    """What a backup stores for one student: enough to put their grade back exactly."""
    sub = sub or {}
    return {
        "user_id": student["id"],
        "name": student.get("name") or "",
        "email": student.get("email") or student.get("login_id") or "",
        "score": sub.get("score"),
        "entered_score": sub.get("entered_score"),
        "grade": sub.get("grade"),
        "entered_grade": sub.get("entered_grade"),
        "excused": bool(sub.get("excused")),
        "workflow_state": sub.get("workflow_state"),
    }


def restore_points(entry: dict) -> float | None:
    return entry["entered_score"] if entry.get("entered_score") is not None else entry.get("score")


def same_points(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) < 0.005


def gradable_points(assignment: dict) -> float:
    if assignment.get("grading_type") not in grades.GRADABLE_TYPES:
        raise ApiError(f"This assignment is graded as '{assignment.get('grading_type')}', "
                       "which can't take a score out of 100.")
    pp = assignment.get("points_possible")
    if not pp or pp <= 0:
        raise ApiError("This Canvas assignment has no points possible. Set points in Canvas first.")
    return float(pp)


def landed(sub: dict, expected: float | None, pp: float) -> dict:
    """Read back what Canvas stored and compare with what we meant to store."""
    pts = sub.get("entered_score") if sub.get("entered_score") is not None else sub.get("score")
    result = {
        "ok": True,
        "new_points": pts,
        "new_score_100": grades.round_half_up(pts / pp * 100, 2) if pts is not None else None,
        "excused": bool(sub.get("excused")),
    }
    if expected is None:
        if pts is not None and not sub.get("excused"):
            result["warning"] = f"Expected no grade, but Canvas shows {grades.fmt_number(pts)} pts."
    elif pts is None or abs(pts - expected) > 0.011:
        shown = "no grade" if pts is None else f"{grades.fmt_number(pts)} pts"
        result["warning"] = f"Canvas stored {shown}; expected {grades.fmt_number(expected)} pts."
    return result


# ---------- app

def create_app(*, allowed_hosts: set[str], keystore: KeyStore | None = None,
               backup_dir: Path | None = None, session_token: str | None = None,
               ed_factory=None, canvas_factory=None) -> Flask:
    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
    app.json.sort_keys = False

    keystore = keystore or KeyStore()
    backups = BackupStore(backup_dir or BACKUP_DIR)
    session_token = session_token or secrets.token_urlsafe(32)
    allowed_hosts = {h.lower() for h in allowed_hosts}
    ed_factory = ed_factory or (lambda token: EdClient(token))
    canvas_factory = canvas_factory or (lambda base, token: CanvasClient(base, token))
    jobs = Jobs()
    app.config["SESSION_TOKEN"] = session_token

    # ----- guards and headers

    @app.before_request
    def guard():
        if request.host.lower() not in allowed_hosts:
            return {"error": "Unexpected Host header."}, 403
        if request.path.startswith("/api/"):
            sent = request.headers.get("X-EzGrader-Session", "")
            if not hmac.compare_digest(sent.encode(), session_token.encode()):
                return {"error": "EzGrader was restarted. Reload this page.", "code": "session"}, 403
        return None

    @app.after_request
    def security_headers(resp: Response):
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = CSP
        return resp

    # ----- errors -> JSON

    @app.errorhandler(ApiError)
    def on_api_error(e: ApiError):
        return {"error": str(e)}, e.status

    @app.errorhandler(ProfileError)
    @app.errorhandler(BackupError)
    def on_bad_input(e):
        return {"error": str(e)}, 400

    @app.errorhandler(EdError)
    @app.errorhandler(CanvasError)
    @app.errorhandler(ParseError)
    def on_upstream(e):
        return {"error": str(e)}, 502

    @app.errorhandler(Exception)
    def on_unexpected(e):
        if isinstance(e, HTTPException):
            if request.path.startswith("/api/"):
                return {"error": e.description or e.name}, e.code
            return e
        log.exception("Unhandled error")
        return {"error": f"Something went wrong ({type(e).__name__}). See the terminal for details."}, 500

    # ----- request helpers

    def body() -> dict:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ApiError("Expected a JSON body.")
        return data

    def int_field(data: dict, name: str) -> int:
        value = data.get(name)
        if isinstance(value, bool):
            raise ApiError(f"Missing or invalid {name}.")
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise ApiError(f"Missing or invalid {name}.") from None
        if value <= 0:
            raise ApiError(f"Missing or invalid {name}.")
        return value

    def profile_name() -> str:
        name = request.headers.get("X-EzGrader-Profile", "").strip()
        if not name:
            raise ApiError("Pick a profile first (Keys, top right).")
        if not keystore.exists(name):
            raise ApiError(f"There's no profile named '{name}'.", 404)
        return name

    def ed_for(name: str) -> EdClient:
        token = keystore.get_secret(name, "ed_token")
        if not token:
            raise ApiError("This profile has no Ed token yet. Add one under Keys.")
        return ed_factory(token)

    def canvas_for(name: str) -> CanvasClient:
        base = keystore.get_canvas_base_url(name)
        token = keystore.get_secret(name, "canvas_token")
        if not base:
            raise ApiError("This profile has no Canvas address yet. Add one under Keys.")
        if not token:
            raise ApiError("This profile has no Canvas token yet. Add one under Keys.")
        return canvas_factory(base, token)

    # ----- page

    @app.get("/")
    def index():
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return Response(html.replace("{{SESSION_TOKEN}}", session_token), mimetype="text/html")

    # ----- profiles and keys (tokens go in, never come back out)

    @app.get("/api/profiles")
    def list_profiles():
        return {"profiles": keystore.list_profiles()}

    @app.post("/api/profiles")
    def create_profile():
        return keystore.create(str(body().get("name", ""))), 201

    @app.put("/api/profiles/<name>")
    def update_profile(name: str):
        data = body()
        if not keystore.exists(name):
            raise ApiError(f"There's no profile named '{name}'.", 404)
        if "canvas_base_url" in data:
            try:
                url = normalize_base_url(str(data["canvas_base_url"]))
            except ValueError as e:
                raise ApiError(str(e)) from None
            keystore.set_canvas_base_url(name, url)
        for kind in ("ed_token", "canvas_token"):
            if kind in data:
                keystore.set_secret(name, kind, str(data[kind]))
        return keystore.public_info(name)

    @app.delete("/api/profiles/<name>")
    def delete_profile(name: str):
        keystore.delete(name)
        return {"ok": True}

    @app.post("/api/profiles/<name>/test")
    def test_profile(name: str):
        service = body().get("service")
        if not keystore.exists(name):
            raise ApiError(f"There's no profile named '{name}'.", 404)
        try:
            if service == "ed":
                me = ed_for(name).whoami()
                who = me["name"] + (f" ({me['email']})" if me.get("email") else "")
            elif service == "canvas":
                who = canvas_for(name).whoami()["name"]
            else:
                raise ApiError("Unknown service.")
        except (EdError, CanvasError, ApiError) as e:
            return {"ok": False, "message": str(e)}
        return {"ok": True, "message": f"Signed in as {who}"}

    # ----- pickers

    @app.get("/api/ed/courses")
    def ed_courses():
        return {"courses": ed_for(profile_name()).courses()}

    @app.get("/api/ed/courses/<int:course_id>/lessons")
    def ed_lessons(course_id: int):
        return {"lessons": ed_for(profile_name()).lessons(course_id)}

    @app.get("/api/canvas/courses")
    def canvas_courses():
        return {"courses": canvas_for(profile_name()).courses()}

    @app.get("/api/canvas/courses/<int:course_id>/assignments")
    def canvas_assignments(course_id: int):
        return {"assignments": canvas_for(profile_name()).assignments(course_id)}

    # ----- review data

    @app.post("/api/load")
    def load():
        data = body()
        lesson_id = int_field(data, "lesson_id")
        course_id = int_field(data, "canvas_course_id")
        assignment_id = int_field(data, "assignment_id")
        name = profile_name()
        ed, cv = ed_for(name), canvas_for(name)

        def canvas_side():
            return (cv.assignment(course_id, assignment_id), cv.students(course_id),
                    cv.submissions(course_id, assignment_id))

        # Ed and Canvas in parallel; each client keeps its own HTTP session.
        with ThreadPoolExecutor(max_workers=2) as pool:
            csv_future = pool.submit(ed.results_csv, lesson_id)
            canvas_future = pool.submit(canvas_side)
            text = csv_future.result()
            assignment, students, submissions = canvas_future.result()

        lesson = parse_results_csv(text)  # parsed in memory, never written to disk
        review = grades.build_review(lesson, students, submissions, assignment)
        review["lesson"]["id"] = lesson_id
        review["course_id"] = course_id
        return review

    # ----- push

    @app.post("/api/push")
    def push():
        data = body()
        name = profile_name()
        course_id = int_field(data, "canvas_course_id")
        assignment_id = int_field(data, "assignment_id")
        items = data.get("grades")
        if not isinstance(items, list) or not items:
            raise ApiError("No grades to push.")
        wanted: dict[int, float] = {}
        for g in items:
            if not isinstance(g, dict):
                raise ApiError("Malformed grade list.")
            uid = int_field(g, "user_id")
            score = g.get("score_100")
            if isinstance(score, bool) or not isinstance(score, (int, float)) \
                    or not math.isfinite(score) or score < 0:
                raise ApiError(f"Invalid score for Canvas user {uid}.")
            if uid in wanted:
                raise ApiError(f"Canvas user {uid} appears twice.")
            wanted[uid] = grades.round_half_up(float(score), 2)

        cv = canvas_for(name)
        job_id = jobs.reserve("push")
        try:
            assignment = cv.assignment(course_id, assignment_id)
            pp = gradable_points(assignment)
            students = {s["id"]: s for s in cv.students(course_id)}
            subs = {s.get("user_id"): s for s in cv.submissions(course_id, assignment_id)}
            entries, skipped = [], []
            for uid, score in wanted.items():
                student, sub = students.get(uid), subs.get(uid)
                if student is None or sub is None:
                    skipped.append({"user_id": uid, "name": "", "reason": "Not a student on this assignment"})
                elif sub.get("excused"):
                    skipped.append({"user_id": uid, "name": student["name"], "reason": "Excused in Canvas"})
                else:
                    entries.append({**snapshot(student, sub), "new_score_100": score,
                                    "posted_grade": grades.posted_grade(score, pp)})
            if not entries:
                raise ApiError("None of these students can be graded on this assignment.")
            # The backup is on disk before the first write to Canvas.
            backup = backups.save("push", name, course_id, assignment, entries)
        except Exception:
            jobs.cancel(job_id)
            raise

        def work(entry: dict) -> dict:
            sub = cv.set_grade(course_id, assignment_id, entry["user_id"], entry["posted_grade"])
            return landed(sub, grades.expected_points(entry["new_score_100"], pp), pp)

        jobs.run(job_id, entries, work)
        return {"job_id": job_id, "backup_id": backup["id"], "count": len(entries), "skipped": skipped}

    @app.get("/api/jobs/<job_id>")
    def job_status(job_id: str):
        return jobs.get(job_id)

    # ----- backups and revert

    @app.get("/api/backups")
    def list_backups():
        course_id = request.args.get("course_id", type=int)
        assignment_id = request.args.get("assignment_id", type=int)
        return {"backups": backups.list(course_id, assignment_id)}

    def revert_plan(name: str, backup_id: str):
        backup = backups.load(backup_id)
        if backup.get("kind") != "push":
            raise ApiError("Only pushes can be reverted.")
        cv = canvas_for(name)
        course_id, assignment_id = backup["course_id"], backup["assignment_id"]
        assignment = cv.assignment(course_id, assignment_id)
        pp = assignment.get("points_possible")
        subs = {s.get("user_id"): s for s in cv.submissions(course_id, assignment_id)}
        rows = []
        for e in backup["entries"]:
            current = grades.canvas_current(subs.get(e["user_id"]), pp)
            points = None if e.get("excused") else restore_points(e)
            pushed_points = grades.expected_points(e["new_score_100"], pp) if pp and e.get("new_score_100") is not None else None
            rows.append({
                "user_id": e["user_id"],
                "name": e.get("name", ""),
                "email": e.get("email", ""),
                "current": current,
                "restore": {
                    "excused": bool(e.get("excused")),
                    "points": points,
                    "score_100": grades.round_half_up(points / pp * 100, 2) if points is not None and pp else None,
                },
                "differs": current["excused"] != bool(e.get("excused"))
                           or (not e.get("excused") and not same_points(current["points"], points)),
                "changed_since_push": pushed_points is not None and not same_points(current["points"], pushed_points),
            })
        return backup, assignment, cv, subs, rows

    def backup_summary(backup: dict) -> dict:
        return {k: backup.get(k) for k in ("id", "kind", "created_at", "profile", "assignment_name", "reverted_at")}

    @app.post("/api/revert/preview")
    def revert_preview():
        backup, assignment, _, _, rows = revert_plan(profile_name(), str(body().get("backup_id", "")))
        return {"backup": backup_summary(backup), "assignment": assignment, "rows": rows}

    @app.post("/api/revert")
    def revert():
        name = profile_name()
        backup_id = str(body().get("backup_id", ""))
        job_id = jobs.reserve("revert")
        try:
            backup, assignment, cv, subs, rows = revert_plan(name, backup_id)
            todo = [r for r in rows if r["differs"]]
            if not todo:
                backups.mark_reverted(backup_id, "already-matching")
                raise ApiError("Canvas already matches the backup. Nothing to revert.", 409)
            course_id, assignment_id = backup["course_id"], backup["assignment_id"]
            current = [snapshot({"id": r["user_id"], "name": r["name"], "email": r["email"]},
                                subs.get(r["user_id"])) for r in todo]
            revert_backup = backups.save("revert", name, course_id, assignment, current, reverts=backup_id)
            pp = assignment.get("points_possible") or 0
        except Exception:
            jobs.cancel(job_id)
            raise

        def work(row: dict) -> dict:
            restore = row["restore"]
            if restore["excused"]:
                sub = cv.excuse(course_id, assignment_id, row["user_id"])
                return {"ok": bool(sub.get("excused")), "excused": True,
                        **({} if sub.get("excused") else {"error": "Canvas didn't mark it excused."})}
            posted = "" if restore["points"] is None else grades.fmt_number(restore["points"])
            sub = cv.set_grade(course_id, assignment_id, row["user_id"], posted)
            return landed(sub, restore["points"], pp) if pp else {"ok": True}

        def on_done(job: dict) -> None:
            if all(r.get("ok") for r in job["results"]):
                backups.mark_reverted(backup_id, revert_backup["id"])

        jobs.run(job_id, todo, work, on_done)
        return {"job_id": job_id, "backup_id": revert_backup["id"], "count": len(todo)}

    return app
