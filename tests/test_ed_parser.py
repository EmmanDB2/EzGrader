from pathlib import Path

import pytest

from ezgrader.ed_client import EdClient, EdError
from ezgrader.ed_parser import (ParseError, parse_auto_points, parse_max_points,
                                parse_results_csv)
from ezgrader.grades import score_100

from conftest import FakeResponse, FakeSession

REAL_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.mark.parametrize("title, expected", [
    ("Explain Code and determine Output 1 [5 points]", 5),
    ("Parson's problems [ 30  points ]", 30),
    ("Debug Code 1 [100 points: 70 auto, 30 style]", 100),
    ("Debug Code (1)  [100 points: 70 auto, 30 style ]", 100),
    ("Weird [2.5 POINTS]", 2.5),
    ("Instructions", 0),
    ("Using GenAI", 0),
    ("[CS2] Lab 1.1.1: Introduction to object-oriented programming", 0),
    ("Zone Survey Homework #1 - Review", 0),
])
def test_max_points(title, expected):
    assert parse_max_points(title) == expected


@pytest.mark.parametrize("title, expected", [
    ("Debug Code 1 [100 points: 70 auto, 30 style]", 70),
    ("Write Code 1 [100 points: 30 auto, 70 style]", 30),
    ("Write Code (3) [100 points: 70 auto, 30 style ]", 70),
    ("Parson's problems [ 30  points ]", None),
    ("Instructions", None),
])
def test_auto_points(title, expected):
    assert parse_auto_points(title) == expected


def test_homework_shape_with_results_section(homework_csv):
    lesson = parse_results_csv(homework_csv)
    assert lesson.has_mark
    assert len(lesson.slides) == 14
    assert lesson.total_max == 1030  # 30 + 10 x 100
    # Titles repeat; slides are identified by number.
    titles = [s.title for s in lesson.slides]
    assert titles.count("Debug Code (1)  [100 points: 70 auto, 30 style ]") == 2
    assert [s.number for s in lesson.slides] == [str(n) for n in range(1, 15)]

    by_name = {s.name: s for s in lesson.students}
    assert by_name["Alice Nguyen"].total == 883
    assert by_name["Alice Nguyen"].mark == 883
    assert score_100(by_name["Alice Nguyen"].total, lesson.total_max) == 85.73
    # Float noise is summed, not rounded away early.
    assert by_name["Bob Smith"].total == pytest.approx(836.33000183)
    # Blank cells count as 0.
    assert by_name["Dan Okafor"].total == 0
    assert by_name["Grace Kim"].total == 983
    assert by_name["Hank Ito"].total == 1030
    assert score_100(by_name["Hank Ito"].total, lesson.total_max) == 100
    # Email is kept raw; matching normalizes it later.
    assert by_name["Bob Smith"].email == "Bob.Smith@Example.EDU"
    assert by_name["Carol Diaz"].first_viewed and not by_name["Carol Diaz"].best_attempt_at
    assert lesson.warnings == []


def test_lab_shape_without_results_section(lab_csv):
    lesson = parse_results_csv(lab_csv)
    assert not lesson.has_mark
    assert len(lesson.slides) == 12
    assert lesson.total_max == 520  # 4 x 5 + 5 x 100
    assert lesson.slides[11].auto_points == 30
    by_name = {s.name: s for s in lesson.students}
    assert by_name["Alice Nguyen"].total == 520
    assert by_name["Alice Nguyen"].mark is None
    assert score_100(by_name["Bob Smith"].total, lesson.total_max) == 74.04  # 385 / 520
    # A non-numeric cell counts as 0 and is reported.
    assert by_name["Ivy Chen"].total == 505
    assert any("weren't numbers" in w for w in lesson.warnings)
    assert "Parson’s problems 1 [5 points]" in [s.title for s in lesson.slides]


def test_columns_found_by_name_not_position(homework_csv):
    lines = homework_csv.splitlines()
    # Insert an extra unknown column before EMAIL in both header rows and every data row.
    shifted = "\n".join("X," + line for line in lines)
    lesson = parse_results_csv(shifted)
    assert lesson.total_max == 1030
    assert {s.name: s.total for s in lesson.students}["Alice Nguyen"] == 883


def test_bom_and_crlf(lab_csv):
    lesson = parse_results_csv("\ufeff" + lab_csv.replace("\n", "\r\n"))
    assert lesson.total_max == 520


@pytest.mark.parametrize("text", [
    "<!doctype html><html><body>Login</body></html>",
    '{"code": "bad_token"}',
    "",
])
def test_rejects_non_csv(text):
    with pytest.raises(ParseError):
        parse_results_csv(text)


def test_rejects_csv_without_email_or_slides():
    with pytest.raises(ParseError, match="EMAIL"):
        parse_results_csv("USERS,,\nNAME,FOO,BAR\n")
    with pytest.raises(ParseError, match="slide"):
        parse_results_csv("USERS,,\nEMAIL,NAME,FOO\n")


def test_no_points_labels_warns():
    lesson = parse_results_csv("USERS,,,SLIDES,Intro\nEMAIL,NAME,,NUMBER,1\na@x.edu,A,,,0\n")
    assert lesson.total_max == 0
    assert any("max points is 0" in w for w in lesson.warnings)


@pytest.mark.skipif(not (REAL_FIXTURES / "lesson_178455_results.csv").exists(), reason="real export not present")
def test_real_anonymized_homework_export():
    lesson = parse_results_csv((REAL_FIXTURES / "lesson_178455_results.csv").read_text(encoding="utf-8"))
    assert lesson.total_max == 1030
    assert lesson.has_mark
    # MARK agrees with our own sum on every row of the real export.
    assert all(s.mark is None or abs(s.mark - s.total) <= 0.01 for s in lesson.students)


@pytest.mark.skipif(not (REAL_FIXTURES / "lesson_176875_results.csv").exists(), reason="real export not present")
def test_real_anonymized_lab_export():
    lesson = parse_results_csv((REAL_FIXTURES / "lesson_176875_results.csv").read_text(encoding="utf-8"))
    assert lesson.total_max == 520


# ---------- Ed client

def test_results_csv_uses_scores_not_completions(homework_csv):
    session = FakeSession([("POST", "https://us.edstem.org/api/lessons/42/results.csv",
                            FakeResponse(200, text=homework_csv, headers={"Content-Type": "text/csv"},
                                         content=homework_csv.encode("utf-8")))])
    text = EdClient("tok", session=session).results_csv(42)
    call = session.calls[0]
    assert call["params"]["completions"] == "0"
    assert call["params"]["students"] == "1"
    assert call["headers"]["Authorization"] == "Bearer tok"
    assert parse_results_csv(text).total_max == 1030


def test_results_csv_decodes_utf8_regardless_of_header(lab_csv):
    body = lab_csv.encode("utf-8")
    session = FakeSession([("POST", "https://us.edstem.org/api/lessons/7/results.csv",
                            FakeResponse(200, text=body.decode("latin-1"), headers={"Content-Type": "text/csv"},
                                         content=body))])
    assert "Parson’s" in EdClient("tok", session=session).results_csv(7)


def test_results_csv_html_is_an_error():
    session = FakeSession([("POST", "https://us.edstem.org/api/lessons/7/results.csv",
                            FakeResponse(200, text="<html>", headers={"Content-Type": "text/html"}))])
    with pytest.raises(EdError, match="didn't return a CSV"):
        EdClient("tok", session=session).results_csv(7)


def test_ed_errors_are_explained_without_echoing_token():
    session = FakeSession([("GET", "https://us.edstem.org/api/user", FakeResponse(401, text="nope"))])
    with pytest.raises(EdError) as exc:
        EdClient("secret-token", session=session).whoami()
    assert "rejected the token" in str(exc.value)
    assert "secret-token" not in str(exc.value)


def test_ed_courses_prefers_staff_roles():
    data = {"user": {"name": "TA"}, "courses": [
        {"course": {"id": 1, "code": "CS1", "name": "Intro"}, "role": {"role": "student"}},
        {"course": {"id": 2, "code": "CS2", "name": "Data"}, "role": {"role": "staff"}},
    ]}
    session = FakeSession([("GET", "https://us.edstem.org/api/user", FakeResponse(200, data))])
    courses = EdClient("tok", session=session).courses()
    assert [c["id"] for c in courses] == [2]
