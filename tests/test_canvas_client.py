import pytest

from ezgrader.canvas_client import CanvasClient, CanvasError, normalize_base_url

from conftest import FakeResponse, FakeSession

BASE = "https://canvas.example.edu"


@pytest.mark.parametrize("raw, expected", [
    ("canvas.example.edu", BASE),
    ("https://canvas.example.edu/", BASE),
    ("https://Canvas.Example.edu/api/v1", BASE),
    ("  https://canvas.example.edu/courses/123  ", BASE),
])
def test_normalize_base_url(raw, expected):
    assert normalize_base_url(raw) == expected


@pytest.mark.parametrize("raw", ["", "http://canvas.example.edu", "https://user:pw@canvas.example.edu", "localhost"])
def test_normalize_base_url_rejects(raw):
    with pytest.raises(ValueError):
        normalize_base_url(raw)


def client(rules, **kw):
    session = FakeSession(rules)
    return CanvasClient(BASE, "tok", session=session, sleep=lambda s: None, **kw), session


def test_pagination_follows_next_links():
    page2 = f"{BASE}/api/v1/courses/1/users?page=2&per_page=100"
    c, session = client([
        ("GET", page2, FakeResponse(200, [{"id": 2, "name": "B", "login_id": "b@x.edu"}])),
        ("GET", f"{BASE}/api/v1/courses/1/users",
         FakeResponse(200, [{"id": 1, "name": "A", "email": "a@x.edu"}], links={"next": {"url": page2}})),
    ])
    students = c.students(1)
    assert [s["id"] for s in students] == [1, 2]
    assert students[1]["login_id"] == "b@x.edu"
    first = session.calls[0]
    assert ("enrollment_type[]", "student") in first["params"]
    assert ("per_page", "100") in first["params"]
    assert first["headers"]["Authorization"] == "Bearer tok"


def test_refuses_to_follow_links_to_other_hosts():
    c, session = client([
        ("GET", f"{BASE}/api/v1/courses/1/users",
         FakeResponse(200, [{"id": 1}], links={"next": {"url": "https://evil.example.com/steal"}})),
    ])
    with pytest.raises(CanvasError, match="different host"):
        c.students(1)
    assert all(call["url"].startswith(BASE) for call in session.calls)


def test_rate_limit_is_retried():
    throttled = FakeResponse(403, text="403 Forbidden (Rate Limit Exceeded)")
    ok = FakeResponse(200, {"id": 5, "name": "Me", "login_id": "me"})
    c, session = client([("GET", f"{BASE}/api/v1/users/self", [throttled, ok])])
    assert c.whoami()["name"] == "Me"
    assert len(session.calls) == 2


def test_permission_error_is_not_retried():
    c, session = client([("GET", f"{BASE}/api/v1/users/self",
                          FakeResponse(403, {"errors": [{"message": "user not authorized"}]}, text="unauthorized"))])
    with pytest.raises(CanvasError, match="permission"):
        c.whoami()
    assert len(session.calls) == 1


def test_courses_prefers_staff_enrollments_and_skips_restricted():
    c, _ = client([("GET", f"{BASE}/api/v1/courses", FakeResponse(200, [
        {"id": 1, "name": "Taking", "enrollments": [{"type": "student"}]},
        {"id": 2, "name": "TAing", "course_code": "CS2", "term": {"name": "Fall"}, "enrollments": [{"type": "ta"}]},
        {"id": 3, "access_restricted_by_date": True},
    ]))])
    assert [(x["id"], x["term"]) for x in c.courses()] == [(2, "Fall")]


def test_assignments_flattened_from_groups():
    c, _ = client([("GET", f"{BASE}/api/v1/courses/1/assignment_groups", FakeResponse(200, [
        {"name": "Labs", "position": 2, "assignments": [{"id": 20, "name": "Lab 1", "points_possible": 10, "grading_type": "points", "position": 1}]},
        {"name": "Homework", "position": 1, "assignments": [{"id": 10, "name": "HW1", "points_possible": 100, "grading_type": "points", "position": 1}]},
    ]))])
    assert [(a["id"], a["group"]) for a in c.assignments(1)] == [(10, "Homework"), (20, "Labs")]


def test_set_grade_request_shape():
    c, session = client([("PUT", f"{BASE}/api/v1/courses/1/assignments/9/submissions/101",
                          FakeResponse(200, {"user_id": 101, "score": 85.73}))])
    assert c.set_grade(1, 9, 101, "85.73")["score"] == 85.73
    assert session.calls[0]["data"] == {"submission[posted_grade]": "85.73"}


def test_canvas_error_messages_include_detail():
    c, _ = client([("PUT", f"{BASE}/api/v1/courses/1/assignments/9/submissions/101",
                    FakeResponse(400, {"errors": {"base": "Cannot grade unpublished assignment"}}))])
    with pytest.raises(CanvasError, match="unpublished"):
        c.set_grade(1, 9, 101, "85")
