import json
import time

import pytest

from ezgrader.canvas_client import CanvasError
from ezgrader.keystore import KeyStore
from ezgrader.server import create_app

TOKEN = "test-session-token"
HOST = "127.0.0.1:8765"


class FakeEd:
    def __init__(self, csv_text):
        self.csv_text = csv_text

    def whoami(self):
        return {"name": "Tess TA", "email": "tess@example.edu"}

    def courses(self):
        return [{"id": 1, "code": "CS2", "name": "Data Structures", "role": "staff"}]

    def lessons(self, course_id):
        return [{"id": 178455, "title": "Homework 1", "module": "Week 1"}]

    def results_csv(self, lesson_id):
        return self.csv_text


class FakeCanvas:
    """Stateful Canvas: set_grade changes what submissions() returns."""

    def __init__(self, points_possible=100.0):
        self.assignment_data = {"id": 9, "name": "HW1", "points_possible": points_possible,
                                "grading_type": "points", "published": True}
        self.students_data = [
            {"id": 101, "name": "Alice Nguyen", "sortable_name": "Nguyen, Alice", "email": "alice.nguyen@example.edu", "login_id": ""},
            {"id": 102, "name": "Bob Smith", "sortable_name": "Smith, Bob", "email": "bob.smith@example.edu", "login_id": ""},
            {"id": 103, "name": "Carol Diaz", "sortable_name": "Diaz, Carol", "email": "carol.diaz@example.edu", "login_id": ""},
            {"id": 105, "name": "Erin Park", "sortable_name": "Park, Erin", "email": "erin.park@example.edu", "login_id": ""},
        ]
        self.subs = {
            101: {"user_id": 101, "score": 80.0, "entered_score": 80.0, "grade": "80", "workflow_state": "graded"},
            102: {"user_id": 102, "score": None, "entered_score": None, "grade": None, "workflow_state": "unsubmitted"},
            103: {"user_id": 103, "score": 50.0, "entered_score": 50.0, "grade": "50", "workflow_state": "graded"},
            105: {"user_id": 105, "score": None, "excused": True, "workflow_state": "graded"},
        }
        self.writes = []
        self.fail_for = set()

    def whoami(self):
        return {"name": "Tess TA", "login_id": "tess"}

    def courses(self):
        return [{"id": 1, "name": "CS2", "code": "CS2", "term": "Fall", "roles": ["ta"]}]

    def assignments(self, course_id):
        return [self.assignment_data]

    def assignment(self, course_id, assignment_id):
        return dict(self.assignment_data)

    def students(self, course_id):
        return list(self.students_data)

    def submissions(self, course_id, assignment_id):
        return [dict(s) for s in self.subs.values()]

    def set_grade(self, course_id, assignment_id, user_id, posted_grade):
        self.writes.append((user_id, posted_grade))
        if user_id in self.fail_for:
            raise CanvasError("Canvas returned HTTP 500.")
        pp = self.assignment_data["points_possible"]
        if posted_grade == "":
            score = None
        elif posted_grade.endswith("%"):
            score = float(posted_grade[:-1]) / 100 * pp
        else:
            score = float(posted_grade)
        sub = self.subs[user_id]
        sub.update(score=score, entered_score=score, grade=None if score is None else str(score), excused=False)
        return dict(sub)

    def excuse(self, course_id, assignment_id, user_id):
        self.subs[user_id].update(excused=True, score=None, entered_score=None)
        return dict(self.subs[user_id])


@pytest.fixture
def env(tmp_path, homework_csv):
    ks = KeyStore()
    ks.create("Tess")
    ks.set_secret("Tess", "ed_token", "ed-secret-token-1111")
    ks.set_secret("Tess", "canvas_token", "canvas-secret-token-2222")
    ks.set_canvas_base_url("Tess", "https://canvas.example.edu")
    canvas = FakeCanvas()
    app = create_app(allowed_hosts={HOST}, keystore=ks, backup_dir=tmp_path / "backups",
                     session_token=TOKEN, ed_factory=lambda token: FakeEd(homework_csv),
                     canvas_factory=lambda base, token: canvas)
    client = app.test_client()
    return {"client": client, "canvas": canvas, "backups": tmp_path / "backups", "ks": ks}


def call(client, method, path, body=None, profile="Tess", token=TOKEN, host=HOST):
    headers = {"X-EzGrader-Session": token}
    if profile:
        headers["X-EzGrader-Profile"] = profile
    return client.open(path, method=method, json=body, headers=headers, base_url=f"http://{host}")


def wait_for(client, job_id):
    for _ in range(200):
        job = call(client, "GET", f"/api/jobs/{job_id}").get_json()
        if job["status"] == "done":
            return job
        time.sleep(0.01)
    raise AssertionError("job didn't finish")


# ---------- guards

def test_api_requires_session_token(env):
    assert call(env["client"], "GET", "/api/profiles", token="wrong").status_code == 403
    assert call(env["client"], "GET", "/api/profiles", token="").status_code == 403
    assert call(env["client"], "GET", "/api/profiles").status_code == 200


def test_rejects_foreign_host_header(env):
    # DNS rebinding: attacker.com resolving to 127.0.0.1
    assert call(env["client"], "GET", "/api/profiles", host="attacker.example.com:8765").status_code == 403
    assert env["client"].get("/", base_url="http://attacker.example.com:8765").status_code == 403


def test_index_embeds_session_token_and_security_headers(env):
    resp = env["client"].get("/", base_url=f"http://{HOST}")
    assert resp.status_code == 200
    assert TOKEN in resp.get_data(as_text=True)
    assert "default-src 'self'" in resp.headers["Content-Security-Policy"]
    assert resp.headers["Cache-Control"] == "no-store"


# ---------- keys

def test_tokens_never_returned(env):
    c = env["client"]
    resp = call(c, "GET", "/api/profiles")
    assert "secret-token" not in resp.get_data(as_text=True)
    profile = resp.get_json()["profiles"][0]
    assert (profile["ed_token"], profile["canvas_token"]) == ("••••1111", "••••2222")
    resp = call(c, "PUT", "/api/profiles/Tess", {"ed_token": "new-ed-token-9999"})
    assert "new-ed-token" not in resp.get_data(as_text=True)
    assert resp.get_json()["ed_token"] == "••••9999"
    assert env["ks"].get_secret("Tess", "ed_token") == "new-ed-token-9999"


def test_profile_crud_and_test(env):
    c = env["client"]
    assert call(c, "POST", "/api/profiles", {"name": "Second TA"}).status_code == 201
    assert call(c, "PUT", "/api/profiles/Second TA", {"canvas_base_url": "canvas.example.edu/"}).get_json()[
        "canvas_base_url"] == "https://canvas.example.edu"
    assert call(c, "PUT", "/api/profiles/Second TA", {"canvas_base_url": "http://insecure.example.edu"}).status_code == 400
    result = call(c, "POST", "/api/profiles/Second TA/test", {"service": "ed"}).get_json()
    assert result["ok"] is False and "no Ed token" in result["message"]
    assert call(c, "POST", "/api/profiles/Tess/test", {"service": "canvas"}).get_json() == {
        "ok": True, "message": "Signed in as Tess TA"}
    assert call(c, "DELETE", "/api/profiles/Second TA").status_code == 200
    assert [p["name"] for p in call(c, "GET", "/api/profiles").get_json()["profiles"]] == ["Tess"]


# ---------- load

def test_load_builds_review(env):
    resp = call(env["client"], "POST", "/api/load", {"lesson_id": 178455, "canvas_course_id": 1, "assignment_id": 9})
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()
    assert data["lesson"]["total_max"] == 1030
    rows = {r["ed"]["name"]: r for r in data["rows"]}
    assert set(rows) == {"Alice Nguyen", "Bob Smith", "Carol Diaz", "Erin Park"}
    assert rows["Alice Nguyen"]["canvas"]["current"]["score_100"] == 80.0
    assert any(f["code"] == "viewed_no_score" for f in rows["Carol Diaz"]["flags"])
    assert "Hank Ito" in {s["name"] for s in data["ed_only"]}


def test_load_requires_profile_and_ids(env):
    assert call(env["client"], "POST", "/api/load", {"lesson_id": 1}, profile=None).status_code == 400
    assert call(env["client"], "POST", "/api/load", {"lesson_id": "x", "canvas_course_id": 1, "assignment_id": 9}).status_code == 400


# ---------- push, backup, revert

def test_push_backs_up_first_then_reports_per_student(env):
    c, canvas = env["client"], env["canvas"]
    canvas.fail_for = {103}
    resp = call(c, "POST", "/api/push", {"canvas_course_id": 1, "assignment_id": 9, "grades": [
        {"user_id": 101, "score_100": 85.73},
        {"user_id": 102, "score_100": 81.2},
        {"user_id": 103, "score_100": 0},
        {"user_id": 105, "score_100": 90},    # excused: skipped
        {"user_id": 999, "score_100": 90},    # not in course: skipped
    ]})
    assert resp.status_code == 200, resp.get_json()
    started = resp.get_json()
    assert started["count"] == 3
    assert {s["user_id"] for s in started["skipped"]} == {105, 999}

    backup = json.loads((env["backups"] / f"{started['backup_id']}.json").read_text())
    assert backup["kind"] == "push"
    assert {e["user_id"]: e["entered_score"] for e in backup["entries"]} == {101: 80.0, 102: None, 103: 50.0}
    assert (env["backups"] / f"{started['backup_id']}.json").stat().st_mode & 0o777 == 0o600

    job = wait_for(c, started["job_id"])
    results = {r["user_id"]: r for r in job["results"]}
    assert results[101]["ok"] and results[101]["new_score_100"] == 85.73
    assert results[102]["ok"]
    assert not results[103]["ok"] and "500" in results[103]["error"]
    assert canvas.writes == [(101, "85.73"), (102, "81.2"), (103, "0")]


def test_push_posts_percentages_when_not_out_of_100(env):
    c, canvas = env["client"], env["canvas"]
    canvas.assignment_data["points_possible"] = 10.0
    started = call(c, "POST", "/api/push", {"canvas_course_id": 1, "assignment_id": 9,
                                            "grades": [{"user_id": 101, "score_100": 85.73}]}).get_json()
    job = wait_for(c, started["job_id"])
    assert canvas.writes == [(101, "85.73%")]
    assert job["results"][0]["new_points"] == pytest.approx(8.573)
    assert "warning" not in job["results"][0]


@pytest.mark.parametrize("bad", [
    [{"user_id": 101, "score_100": -1}],
    [{"user_id": 101, "score_100": "85"}],
    [{"user_id": 101, "score_100": float("nan")}],
    [{"user_id": 101, "score_100": 85}, {"user_id": 101, "score_100": 86}],
    [],
])
def test_push_validates_input(env, bad):
    resp = env["client"].open("/api/push", method="POST", base_url=f"http://{HOST}",
                              headers={"X-EzGrader-Session": TOKEN, "X-EzGrader-Profile": "Tess",
                                       "Content-Type": "application/json"},
                              data=json.dumps({"canvas_course_id": 1, "assignment_id": 9, "grades": bad}))
    assert resp.status_code == 400
    assert env["canvas"].writes == []


def test_push_refuses_pass_fail(env):
    env["canvas"].assignment_data["grading_type"] = "pass_fail"
    resp = call(env["client"], "POST", "/api/push", {"canvas_course_id": 1, "assignment_id": 9,
                                                     "grades": [{"user_id": 101, "score_100": 85}]})
    assert resp.status_code == 400
    assert not env["backups"].exists() or not list(env["backups"].iterdir())


def test_revert_restores_previous_grades(env):
    c, canvas = env["client"], env["canvas"]
    started = call(c, "POST", "/api/push", {"canvas_course_id": 1, "assignment_id": 9, "grades": [
        {"user_id": 101, "score_100": 85.73}, {"user_id": 102, "score_100": 81.2}]}).get_json()
    wait_for(c, started["job_id"])
    assert canvas.subs[101]["score"] == 85.73

    backups = call(c, "GET", "/api/backups?course_id=1&assignment_id=9").get_json()["backups"]
    assert backups[0]["id"] == started["backup_id"] and backups[0]["reverted_at"] is None

    preview = call(c, "POST", "/api/revert/preview", {"backup_id": started["backup_id"]}).get_json()
    rows = {r["user_id"]: r for r in preview["rows"]}
    assert rows[101]["current"]["points"] == 85.73 and rows[101]["restore"]["points"] == 80.0
    assert rows[102]["restore"]["points"] is None and rows[102]["differs"]

    reverted = call(c, "POST", "/api/revert", {"backup_id": started["backup_id"]}).get_json()
    job = wait_for(c, reverted["job_id"])
    assert all(r["ok"] for r in job["results"]), job
    assert canvas.subs[101]["score"] == 80.0
    assert canvas.subs[102]["score"] is None  # cleared back to "no grade"
    assert canvas.writes[-2:] == [(101, "80"), (102, "")]

    backups = call(c, "GET", "/api/backups?course_id=1&assignment_id=9").get_json()["backups"]
    original = next(b for b in backups if b["id"] == started["backup_id"])
    assert original["reverted_at"] is not None
    assert any(b["kind"] == "revert" and b["reverts"] == started["backup_id"] for b in backups)


def test_backup_ids_cannot_escape_directory(env):
    resp = call(env["client"], "POST", "/api/revert/preview", {"backup_id": "../../etc/passwd"})
    assert resp.status_code == 400
