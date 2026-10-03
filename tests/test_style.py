import json
import time

import pytest

from ezgrader import code_server, style
from ezgrader.ed_client import EdClient, EdError
from ezgrader.keystore import KeyStore
from ezgrader.server import create_app

from conftest import FakeResponse, FakeSession

TOKEN = "test-session-token"
HOST = "127.0.0.1:8765"


def doc(text):
    return f'<document version="2.0"><paragraph>{text}</paragraph></document>'


# Same shape as the real rubric the probe reported: one pick-one "Style" section, one loose item.
RUBRIC = {"id": 44518, "positive_grading": True, "floor": True, "ceiling": True, "course_id": 1,
          "unsectioned_items": [{"id": 322980, "points": 0, "title": doc("Grader notes"), "staff_description": "",
                                 "index": 0}],
          "sections": [{"id": 9001, "select_one": True, "mark_clamp": None, "title": "Style", "index": 1, "items": [
              {"id": 322986, "points": 20, "title": doc("Minor style issues"), "staff_description": "", "index": 1},
              {"id": 322985, "points": 30, "title": doc("Clean, readable code"),
               "staff_description": doc("Names are <bold>meaningful</bold>"), "index": 0},
              {"id": 322987, "points": 10, "title": doc("Major style issues"), "staff_description": "", "index": 2},
              {"id": 322988, "points": -50, "title": doc("Didn't follow instructions"), "staff_description": "",
               "index": 3},
              {"id": 322989, "points": -70, "title": doc("Hard-coded output"), "staff_description": "", "index": 4},
          ]}]}
POINTS = {322980: 0, 322985: 30, 322986: 20, 322987: 10, 322988: -50, 322989: -70}


class FakeStyleEd:
    """In-memory Ed with marking state, so writes and reverts can be checked."""

    def __init__(self):
        self.selected = {5501: {322985}, 5502: set(), 5503: set()}
        self.puts = []
        self.fail_marks = set()
        self.users = [
            {"id": 811001, "name": "Alice Nguyen", "email": "alice@example.edu", "course_role": "student", "submissions": 2},
            {"id": 811002, "name": "Bob Smith", "email": "bob@example.edu", "course_role": "student", "submissions": 1},
            {"id": 811003, "name": "Carol Diaz", "email": "carol@example.edu", "course_role": "student", "submissions": 0},
            {"id": 811004, "name": "Dan Okafor", "email": "dan@example.edu", "course_role": "student", "submissions": 1},
            {"id": 7, "name": "Tess TA", "email": "tess@example.edu", "course_role": "staff", "submissions": 4},
        ]
        self.subs = {
            811001: [{"id": 7001, "created_at": "2026-09-08T10:00:00Z", "status": "failed", "testcase_pass_count": 3,
                      "testcase_total_count": 5, "lesson_mark_id": 5501},
                     {"id": 7002, "created_at": "2026-09-09T10:00:00Z", "status": "passed", "testcase_pass_count": 5,
                      "testcase_total_count": 5, "lesson_mark_id": 5501}],
            811002: [{"id": 7101, "created_at": "2026-09-09T11:00:00Z", "status": "passed", "testcase_pass_count": 5,
                      "testcase_total_count": 5, "lesson_mark_id": 5502}],
            811004: [{"id": 7301, "created_at": "2026-09-09T12:00:00Z", "status": "passed", "testcase_pass_count": 5,
                      "testcase_total_count": 5, "lesson_mark_id": 5503}],
        }

    def whoami(self):
        return {"name": "Tess TA", "email": "tess@example.edu"}

    def lesson(self, lesson_id):
        return {"id": lesson_id, "slides": [
            {"id": 2, "index": 2, "type": "code", "title": "Modify Code (1)", "challenge_id": 290688},
            {"id": 1, "index": 1, "type": "document", "title": "Instructions"},
            {"id": 3, "index": 3, "type": "quiz", "title": "Explain code"},
        ]}

    def challenge(self, challenge_id):
        return {"id": challenge_id, "course_id": 1, "title": "Modify Code (1)", "rubric_id": 44518,
                "rubric_points": 30, "auto_points": 70}

    def challenge_users(self, challenge_id):
        return [dict(u) for u in self.users]

    def user_submissions(self, user_id, challenge_id):
        if user_id == 811004 and "dan" in self.fail_marks:
            raise EdError("Ed returned HTTP 500.")
        return [dict(s) for s in self.subs.get(user_id, [])]

    def rubric(self, rubric_id):
        return json.loads(json.dumps(RUBRIC))

    def _mark(self, mark_id):
        ids = self.selected[mark_id]
        return {"id": mark_id, "auto_mark": 70, "rubric_mark": sum(POINTS[i] for i in ids) if ids else None,
                "mark_override": None}

    def lesson_mark(self, mark_id):
        return {"lesson_mark": self._mark(mark_id), "selected_rubric_items": sorted(self.selected[mark_id])}

    def selected_rubric_items(self, mark_id):
        return sorted(self.selected[mark_id])

    def set_rubric_items(self, mark_id, items):
        if mark_id in self.fail_marks:
            raise EdError("Ed returned HTTP 500.")
        self.puts.append((mark_id, dict(items)))
        for item_id, on in items.items():
            (self.selected[mark_id].add if on else self.selected[mark_id].discard)(int(item_id))
        return {"lesson_mark": self._mark(mark_id), "ids": sorted(self.selected[mark_id])}


# ---------- pure logic

def test_rich_text():
    assert style.rich_text(doc("Clean, readable code")) == "Clean, readable code"
    assert style.rich_text(doc("Names are <bold>meaningful</bold>")) == "Names are meaningful"
    assert style.rich_text('<document version="2.0"><paragraph>Intro</paragraph><list style="bullet">'
                           '<list-item><paragraph>one</paragraph></list-item><list-item>two</list-item></list>'
                           '</document>') == "Intro\n• one\n• two"
    assert style.rich_text("plain") == "plain"
    assert style.rich_text("<document><broken") == "broken" or "<" not in style.rich_text("<document><broken")
    assert style.rich_text(None) == ""


def test_parse_rubric_sorts_and_flattens():
    rubric = style.parse_rubric(RUBRIC)
    section = rubric["sections"][0]
    assert section["select_one"] and section["title"] == "Style"
    assert [i["points"] for i in section["items"]] == [30, 20, 10, -50, -70]
    assert section["items"][0] == {"id": 322985, "points": 30, "title": "Clean, readable code",
                                   "description": "Names are meaningful"}
    assert rubric["loose_items"][0]["title"] == "Grader notes"
    assert style.rubric_item_ids(rubric) == set(POINTS)


def test_validate_selection():
    rubric = style.parse_rubric(RUBRIC)
    assert style.validate_selection(rubric, {322985}) is None
    assert style.validate_selection(rubric, {322985, 322980}) is None  # loose item alongside the pick-one
    assert "Only one" in style.validate_selection(rubric, {322985, 322986})
    assert "isn't in" in style.validate_selection(rubric, {999})


def test_change_items():
    assert style.change_items({322985}, {322986}) == {322986: True, 322985: False}
    assert style.change_items(set(), {322985}) == {322985: True}
    assert style.change_items({322985}, {322985}) == {}


def test_code_slides_numbered_like_results_csv():
    slides = style.code_slides(FakeStyleEd().lesson(178455))
    assert slides == [{"number": 2, "slide_id": 2, "challenge_id": 290688, "title": "Modify Code (1)"}]


def test_load_students():
    ed = FakeStyleEd()
    rows, no_submission = style.load_students(lambda: ed, ed.challenge_users(290688), 290688)
    assert [r["name"] for r in rows] == ["Alice Nguyen", "Bob Smith", "Dan Okafor"]  # no staff, no zero-submission
    assert no_submission == 1
    alice, bob, _ = rows
    assert alice["graded"] and alice["rubric_mark"] == 30 and alice["selected_ids"] == [322985]
    assert [s["id"] for s in alice["submissions"]] == [7002, 7001]  # newest first
    assert alice["lesson_mark_id"] == 5501 and alice["auto_mark"] == 70
    assert not bob["graded"] and bob["rubric_mark"] is None


def test_load_students_reports_per_student_errors():
    ed = FakeStyleEd()
    ed.fail_marks.add("dan")
    rows, _ = style.load_students(lambda: ed, ed.challenge_users(290688), 290688)
    dan = next(r for r in rows if r["name"] == "Dan Okafor")
    assert "500" in dan["error"] and not dan["graded"]


# ---------- code server

class FakeCodeServer:
    def __init__(self, tree):
        self.tree = tree  # {"/home": [entries], "/home/sub": [...]} and file contents in self.files
        self.files = {}
        self.queue = [{"type": "init", "data": {"read_only": False, "temporary": True}}]
        self.sent, self.closed = [], False

    def settimeout(self, seconds):
        pass

    def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        if msg["type"] == "fsop":
            folder = msg["data"]["param1"]
            self.queue += [{"type": "ping", "data": {}},
                           {"type": "list_reply", "data": {"dir": folder, "listing": self.tree.get(folder, [])}}]
        elif msg["type"] == "file_open":
            path = msg["data"]["path"]
            self.queue.append({"type": "file_ot_init", "data": {"fid": 1, "path": path, "buffer": self.files.get(path)}})

    def recv(self):
        if not self.queue:
            raise TimeoutError
        return json.dumps(self.queue.pop(0))

    def close(self):
        self.closed = True


class TicketEd:
    def connect_submission(self, sid):
        return "TICKET-abc"


def test_read_submission_files():
    server = FakeCodeServer({
        "/home": [{"name": "main.py", "type": "file"}, {"name": ".hidden", "type": "file"},
                  {"name": "helpers", "type": "folder"}, {"name": "diagram.png", "type": "file"},
                  {"name": "weird-link", "type": "symlink"}],
        "/home/helpers": [{"name": "util.py", "type": "file"}],
    })
    server.files = {"/home/main.py": "def f():\n    return 1\n", "/home/helpers/util.py": "x = 1\n"}
    urls = []
    files = code_server.read_submission_files(TicketEd(), 7002, connect=lambda url: urls.append(url) or server)
    assert urls == ["wss://sahara.us.edstem.org/connect?ticket=TICKET-abc"]
    assert files == [{"path": "main.py", "content": "def f():\n    return 1\n", "lines": 2},
                     {"path": "helpers/util.py", "content": "x = 1\n", "lines": 1}]
    kinds = {(m["type"], m["data"].get("type") if m["type"] == "fsop" else None) for m in server.sent}
    assert kinds <= code_server.ALLOWED_SENDS
    # Never listed the symlink, never opened the image.
    assert [m["data"].get("param1") for m in server.sent if m["type"] == "fsop"] == ["/home", "/home/helpers"]
    assert not any("diagram.png" in m["data"].get("path", "") for m in server.sent)
    assert server.closed


def test_read_submission_files_errors():
    silent = FakeCodeServer({})
    silent.queue = []
    with pytest.raises(code_server.CodeServerError, match="didn't respond"):
        code_server.read_submission_files(TicketEd(), 1, connect=lambda url: silent)
    assert silent.closed

    sock = code_server.SafeSocket(FakeCodeServer({}))
    with pytest.raises(code_server.CodeServerError, match="Refusing"):
        sock.send({"type": "file_ot_op", "data": {"fid": 1, "ops": ["x"]}})
    with pytest.raises(code_server.CodeServerError, match="Refusing"):
        sock.send({"type": "fsop", "data": {"type": "delete", "param1": "/home/a.py"}})


def test_open_socket_verifies_tls(monkeypatch):
    import certifi
    import websocket

    seen = {}
    monkeypatch.setattr(websocket, "create_connection", lambda url, **kw: seen.update(kw) or "ws")
    assert code_server.open_socket("wss://sahara.us.edstem.org/connect?ticket=T") == "ws"
    assert seen["sslopt"] == {"ca_certs": certifi.where()} and seen["origin"] == "https://edstem.org"


def test_ed_client_style_calls():
    session = FakeSession([
        ("PUT", "https://us.edstem.org/api/rubrics/selected/5501",
         FakeResponse(200, {"lesson_mark": {"rubric_mark": 20}, "ids": [322986]})),
        ("POST", "https://us.edstem.org/api/challenges/submissions/7002/connect", FakeResponse(201, {"ticket": "T"})),
        ("GET", "https://us.edstem.org/api/rubrics/selected/5501", FakeResponse(200, [322985])),
    ])
    ed = EdClient("tok", session=session)
    assert ed.selected_rubric_items(5501) == [322985]
    assert ed.set_rubric_items(5501, {322986: True, 322985: False})["ids"] == [322986]
    put = next(c for c in session.calls if c["method"] == "PUT")
    assert put["json"] == {"items": {"322986": True, "322985": False}}
    assert ed.connect_submission(7002) == "T"
    post = next(c for c in session.calls if c["method"] == "POST")
    assert post["json"] == {"user_id": None, "password": None, "i": None}


# ---------- server endpoints

@pytest.fixture
def env(tmp_path):
    ks = KeyStore()
    ks.create("Tess")
    ks.set_secret("Tess", "ed_token", "ed-secret-token-1111")
    ed = FakeStyleEd()
    reads = []

    def reader(client, sid):
        reads.append(sid)
        return [{"path": "main.py", "content": f"# submission {sid}\nprint('hi')\n", "lines": 2}]

    app = create_app(allowed_hosts={HOST}, keystore=ks, backup_dir=tmp_path / "backups", session_token=TOKEN,
                     ed_factory=lambda token: ed, code_reader=reader)
    return {"client": app.test_client(), "ed": ed, "reads": reads, "backups": tmp_path / "backups"}


def call(client, method, path, body=None):
    return client.open(path, method=method, json=body, base_url=f"http://{HOST}",
                       headers={"X-EzGrader-Session": TOKEN, "X-EzGrader-Profile": "Tess"})


def wait_for(client, job_id):
    for _ in range(200):
        job = call(client, "GET", f"/api/jobs/{job_id}").get_json()
        if job["status"] == "done":
            return job
        time.sleep(0.01)
    raise AssertionError("job didn't finish")


def test_style_slides_and_challenge(env):
    c = env["client"]
    assert call(c, "GET", "/api/style/lessons/178455/slides").get_json()["slides"][0]["challenge_id"] == 290688
    data = call(c, "GET", "/api/style/challenges/290688").get_json()
    assert data["challenge"]["rubric_points"] == 30
    assert data["rubric"]["sections"][0]["items"][0]["title"] == "Clean, readable code"
    assert [s["name"] for s in data["students"]] == ["Alice Nguyen", "Bob Smith", "Dan Okafor"]
    assert data["no_submission"] == 1


def test_style_files_are_cached_in_memory(env):
    c = env["client"]
    first = call(c, "GET", "/api/style/submissions/7002/files").get_json()
    second = call(c, "GET", "/api/style/submissions/7002/files").get_json()
    assert first == second and first["files"][0]["path"] == "main.py"
    assert env["reads"] == [7002]


def test_style_write_backs_up_then_writes(env):
    c, ed = env["client"], env["ed"]
    resp = call(c, "POST", "/api/style/write", {"challenge_id": 290688, "grades": [
        {"user_id": 811002, "lesson_mark_id": 5502, "select": [322986], "expected": []},
        {"user_id": 811001, "lesson_mark_id": 5501, "select": [322985], "expected": [322985]},  # unchanged
    ]})
    assert resp.status_code == 200, resp.get_json()
    started = resp.get_json()
    assert started["count"] == 1 and started["skipped"][0]["reason"] == "Already matches Ed"
    backup = json.loads((env["backups"] / f"{started['backup_id']}.json").read_text())
    assert backup["kind"] == "style" and backup["entries"][0]["previous_ids"] == []
    assert backup["entries"][0]["name"] == "Bob Smith"
    job = wait_for(c, started["job_id"])
    assert job["results"][0]["ok"] and job["results"][0]["rubric_mark"] == 20
    assert ed.puts == [(5502, {322986: True})]
    assert ed.selected[5502] == {322986}


def test_style_write_skips_marks_changed_in_ed(env):
    c, ed = env["client"], env["ed"]
    ed.selected[5502] = {322987}  # another TA graded Bob after we loaded
    resp = call(c, "POST", "/api/style/write", {"challenge_id": 290688, "grades": [
        {"user_id": 811002, "lesson_mark_id": 5502, "select": [322986], "expected": []}]})
    assert resp.status_code == 409 and "Changed in Ed" in resp.get_json()["error"]
    assert ed.puts == [] and not (env["backups"].exists() and list(env["backups"].iterdir()))


@pytest.mark.parametrize("select, message", [
    ([322985, 322986], "Only one"),
    ([999], "isn't in"),
])
def test_style_write_validates_against_the_rubric(env, select, message):
    resp = call(env["client"], "POST", "/api/style/write", {"challenge_id": 290688, "grades": [
        {"user_id": 811002, "lesson_mark_id": 5502, "select": select, "expected": []}]})
    assert resp.status_code == 400 and message in resp.get_json()["error"]
    assert env["ed"].puts == []


def test_style_write_rejects_malformed_input(env):
    for grades in ([], [{"user_id": 1, "lesson_mark_id": 5502, "select": ["x"], "expected": []}],
                   [{"user_id": 1, "lesson_mark_id": 5502, "select": [True], "expected": []}]):
        resp = call(env["client"], "POST", "/api/style/write", {"challenge_id": 290688, "grades": grades})
        assert resp.status_code == 400
    assert env["ed"].puts == []


def test_style_write_reports_failures_per_student(env):
    c, ed = env["client"], env["ed"]
    ed.fail_marks.add(5503)
    started = call(c, "POST", "/api/style/write", {"challenge_id": 290688, "grades": [
        {"user_id": 811002, "lesson_mark_id": 5502, "select": [322986], "expected": []},
        {"user_id": 811004, "lesson_mark_id": 5503, "select": [322985], "expected": []},
    ]}).get_json()
    results = {r["user_id"]: r for r in wait_for(c, started["job_id"])["results"]}
    assert results[811002]["ok"] and not results[811004]["ok"] and "500" in results[811004]["error"]


def test_style_revert_restores_previous_selection(env):
    c, ed = env["client"], env["ed"]
    started = call(c, "POST", "/api/style/write", {"challenge_id": 290688, "grades": [
        {"user_id": 811001, "lesson_mark_id": 5501, "select": [322987], "expected": [322985]}]}).get_json()
    wait_for(c, started["job_id"])
    assert ed.selected[5501] == {322987}

    backups = call(c, "GET", "/api/backups?assignment_id=290688&kind=style").get_json()["backups"]
    assert [b["id"] for b in backups] == [started["backup_id"]]
    preview = call(c, "POST", "/api/style/revert/preview", {"backup_id": started["backup_id"]}).get_json()
    assert preview["rows"][0]["current"] == [322987] and preview["rows"][0]["restore"] == [322985]
    reverted = call(c, "POST", "/api/style/revert", {"backup_id": started["backup_id"]}).get_json()
    job = wait_for(c, reverted["job_id"])
    assert all(r["ok"] for r in job["results"])
    assert ed.selected[5501] == {322985}
    assert ed.puts[-1] == (5501, {322985: True, 322987: False})
    backups = call(c, "GET", "/api/backups?assignment_id=290688&kind=style").get_json()["backups"]
    assert backups[0]["reverted_at"] is not None


def test_style_routes_need_the_session_token(env):
    resp = env["client"].get("/api/style/lessons/1/slides", base_url=f"http://{HOST}")
    assert resp.status_code == 403
