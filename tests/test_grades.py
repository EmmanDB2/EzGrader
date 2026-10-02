import pytest

from ezgrader import grades
from ezgrader.ed_parser import parse_results_csv

CANVAS_STUDENTS = [
    {"id": 101, "name": "Alice Nguyen", "sortable_name": "Nguyen, Alice", "email": "alice.nguyen@example.edu", "login_id": "anguyen"},
    # Email hidden by permissions; login_id is the email.
    {"id": 102, "name": "Bob Smith", "sortable_name": "Smith, Bob", "email": "", "login_id": "bob.smith@example.edu"},
    {"id": 103, "name": "Carol Diaz", "sortable_name": "Diaz, Carol", "email": "CAROL.DIAZ@example.edu", "login_id": ""},
    {"id": 104, "name": "Dan Okafor", "sortable_name": "Okafor, Dan", "email": "dan.okafor@example.edu", "login_id": ""},
    {"id": 105, "name": "Erin Park", "sortable_name": "Park, Erin", "email": "erin.park@example.edu", "login_id": ""},
    {"id": 106, "name": "Frank Lee", "sortable_name": "Lee, Frank", "email": "frank.lee@example.edu", "login_id": ""},
    {"id": 107, "name": "Grace Kim", "sortable_name": "Kim, Grace", "email": "grace.kim@example.edu", "login_id": ""},
    {"id": 199, "name": "Zoe Canvasonly", "sortable_name": "Canvasonly, Zoe", "email": "zoe@example.edu", "login_id": ""},
]

ASSIGNMENT = {"id": 9, "name": "HW1", "points_possible": 100.0, "grading_type": "points", "published": True}


@pytest.mark.parametrize("x, expected", [
    (85.728155, 85.73), (2.675, 2.68), (1.005, 1.01), (3.125, 3.13), (74.0384615, 74.04), (100.0, 100.0), (0.0, 0.0),
])
def test_round_half_up(x, expected):
    assert grades.round_half_up(x) == expected


def test_score_100():
    assert grades.score_100(883, 1030) == 85.73
    assert grades.score_100(836.33000183, 1030) == 81.2
    assert grades.score_100(0, 520) == 0
    assert grades.score_100(10, 0) is None


@pytest.mark.parametrize("score, pp, expected", [
    (85.73, 100, "85.73"), (100.0, 100, "100"), (0, 100, "0"), (85.73, 10, "85.73%"), (90.5, 20, "90.5%"),
])
def test_posted_grade(score, pp, expected):
    assert grades.posted_grade(score, pp) == expected


def test_matching_case_insensitive_trimmed_with_login_fallback(homework_csv):
    lesson = parse_results_csv(homework_csv)
    matched, ed_only, canvas_only = grades.match_students(lesson.students, CANVAS_STUDENTS)
    pairs = {ed.name: cs["id"] for ed, cs in matched}
    assert pairs["Bob Smith"] == 102  # "  Bob.Smith@Example.EDU " -> login_id
    assert pairs["Carol Diaz"] == 103  # upper-case Canvas email
    reasons = {s.name: r for s, r in ed_only}
    assert reasons["Hank Ito"] == "No Canvas student with this email"
    assert reasons["No Email Person"] == "No email in the Ed export"
    assert [c["id"] for c in canvas_only] == [199]


def test_ambiguous_canvas_match_is_not_used(homework_csv):
    lesson = parse_results_csv(homework_csv)
    students = CANVAS_STUDENTS + [{"id": 300, "name": "Alice Two", "email": "", "login_id": "alice.nguyen@example.edu"}]
    matched, ed_only, _ = grades.match_students(lesson.students, students)
    assert "Alice Nguyen" not in {ed.name for ed, _ in matched}
    assert dict((s.name, r) for s, r in ed_only)["Alice Nguyen"] == "Matches more than one Canvas student"


def test_flags(homework_csv, lab_csv):
    hw = parse_results_csv(homework_csv)
    by_name = {s.name: s for s in hw.students}

    codes = lambda s, lesson=hw: {f["code"] for f in grades.student_flags(s, lesson)}
    assert codes(by_name["Alice Nguyen"]) == set()
    # Opened the lesson but never submitted.
    carol = grades.student_flags(by_name["Carol Diaz"], hw)
    assert carol[0]["code"] == "viewed_no_score" and carol[0]["label"] == "Viewed, no submission"
    # Never opened: 0 but no flag.
    assert codes(by_name["Dan Okafor"]) == set()
    # Code slides sitting exactly at 70 auto points.
    erin = [f for f in grades.student_flags(by_name["Erin Park"], hw) if f["code"] == "at_auto"][0]
    assert erin["slides"] == ["5", "6", "10", "11", "12", "13"]
    # MARK 900 vs sum 888.
    assert "mark_mismatch" in codes(by_name["Frank Lee"])
    # Float noise within tolerance is not a mismatch.
    assert "mark_mismatch" not in codes(by_name["Bob Smith"])

    lab = parse_results_csv(lab_csv)
    bob = {s.name: s for s in lab.students}["Bob Smith"]
    at_auto = [f for f in grades.student_flags(bob, lab) if f["code"] == "at_auto"][0]
    assert at_auto["slides"] == ["8", "10", "12"]  # 70/70/30-auto


def test_canvas_current_converts_to_100():
    cur = grades.canvas_current({"score": 8.5, "entered_score": 8.5, "grade": "8.5", "workflow_state": "graded"}, 10)
    assert cur["points"] == 8.5 and cur["score_100"] == 85.0
    assert grades.canvas_current({"score": None, "workflow_state": "unsubmitted"}, 10)["score_100"] is None
    # Late policy: compare against what was entered, not the penalized score.
    late = grades.canvas_current({"score": 70, "entered_score": 80, "points_deducted": 10}, 100)
    assert late["points"] == 80
    assert grades.canvas_current(None, 100)["points"] is None


def test_build_review(homework_csv):
    lesson = parse_results_csv(homework_csv)
    subs = [
        {"user_id": 101, "score": 80.0, "entered_score": 80.0, "workflow_state": "graded"},
        {"user_id": 102, "score": None, "workflow_state": "unsubmitted"},
        {"user_id": 105, "excused": True, "score": None},
    ]
    review = grades.build_review(lesson, CANVAS_STUDENTS, subs, ASSIGNMENT)
    assert review["lesson"]["total_max"] == 1030
    rows = {r["ed"]["name"]: r for r in review["rows"]}
    assert rows["Alice Nguyen"]["key"] == "101"
    assert rows["Alice Nguyen"]["canvas"]["current"]["score_100"] == 80.0
    assert rows["Bob Smith"]["canvas"]["current"]["points"] is None
    assert rows["Erin Park"]["canvas"]["current"]["excused"] is True
    assert {s["name"] for s in review["ed_only"]} == {"Hank Ito", "No Email Person"}
    assert [s["name"] for s in review["canvas_only"]] == ["Zoe Canvasonly"]
    # Sorted by Canvas sortable name.
    assert [r["canvas"]["sortable_name"] for r in review["rows"]][:2] == ["Diaz, Carol", "Kim, Grace"]
    assert not any("login ID" in w for w in review["warnings"])  # some emails present


def test_assignment_warnings():
    assert grades.assignment_warnings(ASSIGNMENT) == []
    warnings = grades.assignment_warnings({"points_possible": 0, "grading_type": "pass_fail", "published": False})
    assert len(warnings) == 3
