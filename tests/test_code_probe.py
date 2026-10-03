import io
import json
import zipfile

import pytest

import ed_code_probe as probe
from ezgrader.ed_client import ED_BASE

from conftest import FakeResponse, FakeSession

# Values that must never reach the report.
SECRETS = ["Alice", "Nguyen", "Bob", "Smith", "example.edu", "811001", "811002", "905501",
           "bobsSecretVariable", "https://static.us.edusercontent.com", "Mary J.R"]

STARTER = "public class Main {\n    // TODO: find the max\n}\n"
BOB_CODE = "public class Main {\n    int bobsSecretVariable = 1;\n    // done\n}\n"

LESSON = {"lesson": {"id": 178455, "title": "Homework 1", "slides": [
    {"id": 9002, "index": 2, "type": "code", "title": "Modify Code (1) [100 points: 70 auto, 30 style]",
     "points": 100, "challenge_id": 42, "user_id": 811001},
    {"id": 9001, "index": 1, "type": "document", "title": "Instructions", "points": 0},
    {"id": 9003, "index": 3, "type": "code", "title": "Write Code (1) [100 points: 70 auto, 30 style]",
     "points": 100, "challenge_id": 43},
    {"id": 9004, "index": 4, "type": "quiz", "title": "Explain code and determine output [ 15 points ]",
     "points": 15},
]}}
CHALLENGE = {"challenge": {"id": 42, "type": "code", "settings": {"language": "java"},
                           "scaffold": {"files": [{"name": "Main.java", "content": STARTER}]},
                           "tickets": {"connect": {"user": {"id": 7, "name": ""}, "readonly": False}},
                           "rubric_id": 44518}}
RUBRIC = {"rubric": {"id": 44518, "sections": [{"title": "Style", "items": [
    {"id": 322985, "title": "Clean, readable code", "points": 30},
    {"id": 322986, "title": "Some naming issues", "points": 15}]}]}}
SELECTED = {"ids": [322985]}
USERS = {"users": [
    {"id": 811001, "name": "Alice Nguyen", "email": "alice.nguyen@example.edu", "role": "student"},
    {"id": 811002, "name": "Bob Smith", "email": "bob.smith@example.edu", "role": "student",
     "avatar": "https://static.us.edusercontent.com/avatars/bob.png"},
    {"id": 7, "name": "Mary J.R", "email": "ta@example.edu", "role": "staff"},
]}
SUBMISSION = {
    "id": 905501, "user_id": 811002, "created_at": "2026-09-08T17:32:35Z", "is_final": True,
    "result": {"score": 70, "passed": 7, "total": 10},
    "feedback": {"mark": None, "criteria": [], "content": '<document version="2.0"><paragraph/></document>'},
    "code": {"files": [{"name": "Main.java", "content": BOB_CODE}]},
    "markers": [811001],
    "lesson_mark_id": 556462,
}
LESSON_MARK = {"lesson_mark": {"id": 556462, "user_id": 811002, "updated_by": 7, "rubric_mark": 30, "auto_mark": 70,
                               "comment": "Nice work Bob"},
               "rubric_items": [{"id": 1, "name": "Meaningful names", "points": 10, "selected": True},
                                {"id": 2, "name": "Comments", "points": 20, "selected": False}]}
CONNECT_REPLY = {"ticket": "SECRET-TICKET-abc123", "region": "sahara", "user": {"id": 7, "name": "Tess TA"}}
FRUIT_CODE = "def catalog(fruits):\n    bobs_private_note = 'mine'\n    return sorted(fruits)\n\nprint(catalog([]))\n"


def zip_bytes(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def fake_ed(results_csv="Name,Email,Score,Feedback Mark,Comment\nBob Smith,bob.smith@example.edu,70,,\n"):
    bulk = zip_bytes({
        "Bob Smith (bob.smith@example.edu)/Main.java": BOB_CODE,
        "Alice Nguyen (alice.nguyen@example.edu)/Main.java": STARTER,
        "Alice Nguyen (alice.nguyen@example.edu)/alice_notes.txt": "hi",
    })
    session = FakeSession([
        ("GET", f"{ED_BASE}/lessons/178455", FakeResponse(200, LESSON)),
        ("GET", f"{ED_BASE}/challenges/42/users", FakeResponse(200, USERS)),
        ("GET", f"{ED_BASE}/challenges/42", FakeResponse(200, CHALLENGE)),
        ("GET", f"{ED_BASE}/users/811001/challenges/42/submissions", FakeResponse(200, {"submissions": []})),
        ("GET", f"{ED_BASE}/users/811002/challenges/42/submissions", FakeResponse(200, {"submissions": [SUBMISSION]})),
        ("GET", f"{ED_BASE}/challenges/submissions/905501",
         FakeResponse(200, {"submission": {**SUBMISSION, "testcases": [{"name": "max", "passed": True}]}})),
        ("POST", f"{ED_BASE}/challenges/42/submissions",
         FakeResponse(200, content=bulk, text="", headers={"Content-Type": "application/zip"})),
        ("POST", f"{ED_BASE}/challenges/42/results",
         FakeResponse(200, text=results_csv, content=results_csv.encode(), headers={"Content-Type": "text/csv"})),
        ("POST", f"{ED_BASE}/challenges/submissions/905501/connect", FakeResponse(201, CONNECT_REPLY)),
        ("GET", f"{ED_BASE}/lesson_marks/556462", FakeResponse(200, LESSON_MARK)),
        ("GET", f"{ED_BASE}/rubrics/selected/556462", FakeResponse(200, SELECTED)),
        ("GET", f"{ED_BASE}/rubrics/44518", FakeResponse(200, RUBRIC)),
    ])
    return probe.Ed("tok", session=session, delay=0), session


def run_probe(**kwargs):
    ed, session = fake_ed(**{k: kwargs.pop(k) for k in ["results_csv"] if k in kwargs})
    out = io.StringIO()
    found = probe.run(ed, 178455, out=out, **kwargs)
    return out.getvalue(), found, session


def test_report_answers_the_questions():
    report, found, _ = run_probe()
    assert found["challenge_id"] == 42  # the Modify Code slide, not the first code slide by position
    assert found["challenge_field"] == "challenge_id"
    assert found["submissions"] == 1 and found["students_checked"] == 2
    assert found["code_paths"] == ["submissions[].code.files[].content", "detail.code.files[].content"]
    assert found["code_differs"] is True
    assert "detail.result.score" in found["mark_paths"] and "detail.feedback.criteria" in found["mark_paths"]
    assert found["bulk"] == "zip, 3 files"
    assert found["results_columns"] == ["Name", "Email", "Score", "Feedback Mark", "Comment"]
    assert "Student code in submissions:    yes" in report
    assert "differs from the starter code" in report
    # Slides are listed in Ed's order with their challenge IDs.
    assert report.index("Instructions") < report.index("Modify Code (1)") < report.index("Write Code (1)")


def test_report_contains_no_student_data():
    report, _, _ = run_probe()
    for secret in SECRETS:
        assert secret not in report, secret
    # ...but keeps what's useful: scores, enums, challenge file names, structure.
    for useful in ['"Main.java"', "70", '"student"', '"staff"', '"java"', "id hidden", "contains email",
                   "datetime", "Ed rich text", "<dir:email>/Main.java  x2", "<dir:email>/<file:text>.txt  x1"]:
        assert useful in report, useful


def test_probe_never_writes():
    _, _, session = run_probe()
    methods = {c["method"] for c in session.calls}
    assert methods <= {"GET", "POST"}
    posts = [c["url"] for c in session.calls if c["method"] == "POST"]
    assert posts == [f"{ED_BASE}/challenges/42/submissions", f"{ED_BASE}/challenges/42/results"]
    assert not any(word in c["url"] for c in session.calls for word in ("feedback", "submit_all", "connect"))


def test_slide_option_and_missing_challenge():
    report, found, _ = run_probe(slide_no=3)
    assert found["challenge_id"] == 43
    report, found, _ = run_probe(slide_no=1)  # a document slide: no challenge, and its details 404
    assert "challenge_id" not in found
    assert "No challenge ID found for slide 1" in report


def test_email_option_picks_that_student():
    _, found, session = run_probe(email="BOB.SMITH@example.edu ")
    assert found["students_checked"] == 1
    assert not any("/users/811001/" in c["url"] for c in session.calls)


def test_results_csv_without_header_is_not_printed():
    report, found, _ = run_probe(results_csv="Bob Smith,bob.smith@example.edu,70\n")
    assert "Bob" not in report and found["results_columns"] == ["<first row is data, not shown>"] * 3


def test_second_csv_row_shown_only_when_it_is_a_header():
    header, rows = probe.csv_header("USERS,,RESULTS\nEMAIL,NAME,MARK\na@x.edu,Ann Lee,5\n")
    assert header == [["USERS", "", "RESULTS"], ["EMAIL", "NAME", "MARK"]] and rows == 1
    header, rows = probe.csv_header("Name,Score\nAnn Lee,5\n")
    assert header == [["Name", "Score"]] and rows == 1


def test_shape_hides_identifying_keys_and_values():
    text = probe.shape_of({
        "by_user": {"811001": {"score": 5}, "bob@example.edu": {"score": 7}, "Alice Nguyen": {"score": 9}},
        "author": 42, "big": 1694188800, "member_ids": [1, 2], "scores": [70, 100],
        "name": "Mary J.R", "file": "Main.java",
    })
    assert "<key>" in text and "811001" not in text and "bob@" not in text and "Alice" not in text
    assert "e.g. 5, 7, 9" in text           # scores under collapsed keys still useful
    assert "1694188800" not in text and "Mary" not in text
    assert "e.g. 70, 100" in text and '"Main.java"' in text


def test_find_challenge_id():
    assert probe.find_challenge_id({"challenge_id": 42}) == ("challenge_id", 42)
    assert probe.find_challenge_id({"challenge": {"id": 7}}) == ("challenge.id", 7)
    assert probe.find_challenge_id({"content": {"code": {"challenge_id": 9}}}) == ("content.code.challenge_id", 9)
    assert probe.find_challenge_id({"id": 1, "is_challenge": True}) is None


def test_token_sources(monkeypatch):
    import pytest
    from ezgrader.keystore import KeyStore

    monkeypatch.setenv("ED_API_TOKEN", "env-token")
    assert probe.get_token(None) == "env-token"

    monkeypatch.delenv("ED_API_TOKEN")
    with pytest.raises(SystemExit, match="No Ed token"):
        probe.get_token(None)
    store = KeyStore()
    store.create("Tess")
    store.set_secret("Tess", "ed_token", "tess-token")
    assert probe.get_token(None) == "tess-token"  # the only profile with an Ed token
    store.create("Sam")
    store.set_secret("Sam", "ed_token", "sam-token")
    with pytest.raises(SystemExit, match="--profile"):
        probe.get_token(None)
    assert probe.get_token("Sam") == "sam-token"


# ---------- --files: Ed's code server

class FakeCodeServer:
    """Replays the code-server protocol from the DevTools capture."""

    def __init__(self, files):
        self.files = files
        self.queue = [{"type": "init", "data": {"read_only": False, "temporary": True, "replay": False,
                                                "last_commits": [{"hash": "f7404abc"}]}}]
        self.sent, self.url, self.closed = [], None, False

    def settimeout(self, seconds):
        pass

    def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        if message["type"] == "fsop":
            self.queue += [
                {"type": "client_join", "data": {"id": 10, "user": {"id": 1765122, "name": "Tess TA"}}},
                {"type": "list_reply", "data": {"listing": [
                    *({"name": n, "type": "file", "created_at": "2026-10-01T10:00:00Z"} for n in self.files),
                    {"name": "bob_smith_extra", "type": "folder"}]}},
            ]
        elif message["type"] == "file_open":
            name = message["data"]["path"].rsplit("/", 1)[1]
            self.queue += [
                {"type": "ping", "data": {"sequence": 0, "timestamp": "2026-10-03T17:17:02.881Z"}},
                {"type": "file_ot_init", "data": {"fid": 1, "path": message["data"]["path"], "type": "",
                                                  "opened_by": 9, "soft": False, "buffer": self.files[name]}},
            ]

    def recv(self):
        if not self.queue:
            raise TimeoutError
        return json.dumps(self.queue.pop(0))

    def close(self):
        self.closed = True


def run_files_probe(files=None):
    server = FakeCodeServer(files if files is not None else {"fruit_catalog.py": FRUIT_CODE, "bob_helpers.py": "x = 1\n"})

    def connect(url):
        server.url = url
        return server

    report, found, session = run_probe(files=True, connect_ws=connect)
    return report, found, session, server


def test_files_mode_reads_code_without_leaking_it():
    report, found, session, server = run_files_probe()
    assert found["connected"] and found["code_server"]
    assert found["files_listed"] == 2 and found["files_read"] == 2
    assert found["max_lines"] == 5 and found["content_field"] == "buffer"
    assert "Code server:                    works; read 2 of 2 file(s) (.py x2), up to 5 lines" in report
    # Connected the way the browser does: same payload, ticket on the default host.
    connect_call = next(c for c in session.calls if c["url"].endswith("/connect"))
    assert connect_call["method"] == "POST" and connect_call["json"] == {"user_id": None, "password": None, "i": None}
    assert server.url == "wss://sahara.us.edstem.org/connect?ticket=SECRET-TICKET-abc123"
    assert "wss://sahara.us.edstem.org/connect (from a ticket in the reply" in report
    # Nothing identifying, no code, no ticket, no file names.
    for secret in SECRETS + ["SECRET-TICKET", "bobs_private_note", "fruit_catalog", "bob_helpers",
                             "bob_smith_extra", "Tess TA", "Nice work", "Meaningful names"]:
        assert secret not in report, secret
    assert "Opened 1 temporary copy of a submission" in report
    assert server.closed


def test_files_mode_only_lists_and_opens():
    _, _, _, server = run_files_probe()
    kinds = [(m["type"], m["data"].get("type") if m["type"] == "fsop" else None) for m in server.sent]
    assert set(kinds) <= {("fsop", "list_folder"), ("file_open", None)}
    assert kinds.count(("file_open", None)) == 2


def test_safe_socket_refuses_anything_else():
    sock = probe.SafeSocket(FakeCodeServer({}))
    for message in ({"type": "file_ot_op", "data": {"fid": 1, "ops": ["x"]}},
                    {"type": "fsop", "data": {"type": "delete", "param1": "/home/a.py"}},
                    {"type": "run", "data": {}}):
        with pytest.raises(probe.ProbeError, match="refusing"):
            sock.send(message)
    assert sock.ws.sent == []


def test_files_not_touched_without_the_flag():
    report, found, session = run_probe()
    assert not any(c["url"].endswith("/connect") for c in session.calls)
    assert "Code server:                    not tried (run with --files)" in report
    assert "Opened 1 temporary copy" not in report


def test_files_mode_survives_a_silent_code_server():
    class Silent(FakeCodeServer):
        def send(self, raw):
            self.sent.append(json.loads(raw))

    server = Silent({})
    report, found, _ = run_probe(files=True, connect_ws=lambda url: server)
    assert "No file listing came back" in report
    assert "connected, but no file contents came back" in report


def test_rubric_record_shape_is_reported_safely():
    report, found, _ = run_probe()
    assert found["rubric_record"] is True
    assert "  lesson_mark: object" in report and "rubric_mark: number  e.g. 30" in report
    assert "  rubric_items: list[2]" in report and "e.g. 10, 20" in report  # siblings of lesson_mark kept
    assert "Nice work" not in report and "Meaningful names" not in report


@pytest.mark.parametrize("reply, url", [
    ({"url": "wss://sahara.us.edstem.org/connect?ticket=T1"}, "wss://sahara.us.edstem.org/connect?ticket=T1"),
    ({"ticket": "T2", "url": "wss://x.us.edstem.org/connect"}, "wss://x.us.edstem.org/connect?ticket=T2"),
    ({"ticket": "T3", "host": "https://y.us.edstem.org/"}, "wss://y.us.edstem.org/connect?ticket=T3"),
    ({"data": {"connect_ticket": "T4"}}, "wss://sahara.us.edstem.org/connect?ticket=T4"),
])
def test_find_ws_url(reply, url):
    assert probe.find_ws_url(reply)[0] == url


def test_find_ws_url_without_ticket():
    with pytest.raises(probe.ProbeError, match="no ticket"):
        probe.find_ws_url({"ok": True})


def test_report_trims_noise():
    report, found, _ = run_probe()
    assert "tickets: object  (nested settings collapsed)" in report
    assert "readonly" not in report
    assert found["code_slides"] == 2  # the quiz titled "Explain code ..." isn't a code slide


def test_code_server_verifies_tls_with_certifi(monkeypatch):
    import certifi
    import websocket

    seen = {}
    monkeypatch.setattr(websocket, "create_connection", lambda url, **kw: seen.update(url=url, **kw) or "ws")
    assert probe.open_code_server("wss://sahara.us.edstem.org/connect?ticket=T") == "ws"
    assert seen["sslopt"] == {"ca_certs": certifi.where()}  # verification on, with a real CA list
    assert seen["origin"] == "https://edstem.org"


def test_code_server_errors_never_include_the_ticket(monkeypatch):
    import websocket

    def boom(url, **kw):
        raise websocket.WebSocketBadStatusException(f"Handshake status 403 for {url}", 403)

    monkeypatch.setattr(websocket, "create_connection", boom)
    with pytest.raises(probe.ProbeError) as exc:
        probe.open_code_server("wss://sahara.us.edstem.org/connect?ticket=SECRET-TICKET")
    assert "SECRET-TICKET" not in str(exc.value) and "403" in str(exc.value)


def test_rubric_definition_and_selection_are_reported_safely():
    report, found, session = run_probe()
    assert found["rubric"] is True and found["rubric_selected"] is True
    assert "=== Rubric ===" in report and "points: number  e.g. 30, 15" in report
    assert "selected: object" in report and "ids: list[1]  of number  [id hidden]" in report
    assert "Clean, readable" not in report and "naming issues" not in report
    assert "Rubric definition (rubrics):    works" in report
    assert "Rubric record (lesson_marks):   works; selected items: works" in report
    # Only reads: the rubric save (PUT /rubrics/selected/...) is never sent by the probe.
    assert {c["method"] for c in session.calls} <= {"GET", "POST"}
