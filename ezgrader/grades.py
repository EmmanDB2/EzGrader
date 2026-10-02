"""Turn Ed results + Canvas data into review rows: scores, matching, and flags."""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal

from .ed_parser import MARK_TOLERANCE, LessonResults, StudentResult

# Canvas grading types where "score out of 100 -> percentage" makes sense.
GRADABLE_TYPES = {"points", "percent", "letter_grade", "gpa_scale"}


def round_half_up(x: float, places: int = 2) -> float:
    """Round like a person would (2.675 -> 2.68). The frontend uses the same rule."""
    q = Decimal(1).scaleb(-places)
    return float(Decimal(repr(x)).quantize(q, rounding=ROUND_HALF_UP))


def score_100(total: float, total_max: float) -> float | None:
    """score_100 = round(total_earned / total_max * 100, 2)"""
    if not total_max:
        return None
    return round_half_up(total / total_max * 100, 2)


def norm_key(value: str | None) -> str:
    return (value or "").strip().lower()


def fmt_number(x: float) -> str:
    """85.7300 -> '85.73', 100.0 -> '100'."""
    return format(round(x, 4), "f").rstrip("0").rstrip(".") or "0"


def posted_grade(score: float, points_possible: float) -> str:
    """What to send as submission[posted_grade]. Out of 100 -> points; otherwise a percentage."""
    if math.isclose(points_possible, 100):
        return fmt_number(score)
    return f"{fmt_number(score)}%"


def expected_points(score: float, points_possible: float) -> float:
    return score / 100 * points_possible


# ---------- flags

def student_flags(student: StudentResult, lesson: LessonResults) -> list[dict]:
    flags = []

    if student.total == 0 and student.first_viewed:
        submitted = bool(student.best_attempt_at)
        flags.append({
            "code": "viewed_no_score",
            "label": "Submitted, scored 0" if submitted else "Viewed, no submission",
            "detail": f"Opened the lesson {student.first_viewed}"
                      + ("" if submitted else " but never submitted."),
        })

    at_auto = [
        slide.number for slide, score in zip(lesson.slides, student.scores)
        if slide.auto_points and slide.auto_points < slide.max_points
        and math.isclose(score, slide.auto_points, abs_tol=1e-6)
    ]
    if at_auto:
        flags.append({
            "code": "at_auto",
            "label": "At auto points",
            "detail": f"Slide(s) {', '.join(at_auto)} sit exactly at their auto points. Style may not be graded yet.",
            "slides": at_auto,
        })

    if student.mark is not None and abs(student.mark - student.total) > MARK_TOLERANCE:
        flags.append({
            "code": "mark_mismatch",
            "label": "MARK ≠ sum",
            "detail": f"Ed's MARK is {fmt_number(student.mark)} but the slides add up to {fmt_number(student.total)}.",
        })

    return flags


# ---------- Canvas side

def canvas_current(sub: dict | None, points_possible: float | None) -> dict:
    """A Canvas submission reduced to what the review needs."""
    if not sub:
        return {"points": None, "score_100": None, "grade": None, "excused": False,
                "state": None, "points_deducted": None}
    points = sub.get("entered_score")
    if points is None:
        points = sub.get("score")
    return {
        "points": points,
        "score_100": round_half_up(points / points_possible * 100, 2) if points is not None and points_possible else None,
        "grade": sub.get("entered_grade") or sub.get("grade"),
        "excused": bool(sub.get("excused")),
        "state": sub.get("workflow_state"),
        "points_deducted": sub.get("points_deducted"),
    }


def match_students(ed_students: list[StudentResult], canvas_students: list[dict]):
    """Match Ed rows to Canvas students by email (falling back to login_id).

    Case-insensitive and trimmed. Returns (matched, ed_only, canvas_only), where
    matched is a list of (ed_student, canvas_student) and ed_only is a list of
    (ed_student, reason).
    """
    index: dict[str, dict] = {}
    ambiguous = set()
    for cs in canvas_students:
        for key in {norm_key(cs.get("email")), norm_key(cs.get("login_id"))} - {""}:
            if key in index and index[key]["id"] != cs["id"]:
                ambiguous.add(key)
            index[key] = cs
    for key in ambiguous:
        index.pop(key, None)

    matched, ed_only = [], []
    seen_ed, used_canvas = set(), set()
    for s in ed_students:
        key = norm_key(s.email)
        if not key:
            ed_only.append((s, "No email in the Ed export"))
        elif key in seen_ed:
            ed_only.append((s, "Duplicate email in the Ed export (first row was used)"))
        elif key in ambiguous:
            ed_only.append((s, "Matches more than one Canvas student"))
        elif key not in index:
            ed_only.append((s, "No Canvas student with this email"))
        elif index[key]["id"] in used_canvas:
            ed_only.append((s, "Canvas student already matched to another Ed row"))
        else:
            used_canvas.add(index[key]["id"])
            matched.append((s, index[key]))
        if key:
            seen_ed.add(key)

    canvas_only = [cs for cs in canvas_students if cs["id"] not in used_canvas]
    return matched, ed_only, canvas_only


def assignment_warnings(assignment: dict) -> list[str]:
    warnings = []
    pp = assignment.get("points_possible")
    gt = assignment.get("grading_type")
    if not pp:
        warnings.append("This Canvas assignment has no points possible, so grades can't be pushed to it.")
    if gt not in GRADABLE_TYPES:
        warnings.append(f"This Canvas assignment is graded as '{gt}', which can't take a score out of 100.")
    elif gt in ("letter_grade", "gpa_scale"):
        warnings.append("This Canvas assignment shows letter grades. Canvas will convert each score.")
    if not assignment.get("published", True):
        warnings.append("This Canvas assignment is unpublished. Canvas may refuse grades until it's published.")
    if assignment.get("moderated_grading"):
        warnings.append("This assignment uses moderated grading. Pushed grades may land as provisional grades.")
    if assignment.get("anonymous_grading"):
        warnings.append("This assignment uses anonymous grading.")
    return warnings


def ed_student_dict(s: StudentResult) -> dict:
    return {
        "email": s.email,
        "name": s.name,
        "first_viewed": s.first_viewed,
        "best_attempt_at": s.best_attempt_at,
        "scores": s.scores,
        "total": s.total,
        "mark": s.mark,
    }


def build_review(lesson: LessonResults, canvas_students: list[dict],
                 submissions: list[dict], assignment: dict) -> dict:
    pp = assignment.get("points_possible")
    subs = {s.get("user_id"): s for s in submissions}
    matched, ed_only, canvas_only = match_students(lesson.students, canvas_students)

    rows = []
    for ed, cs in matched:
        current = canvas_current(subs.get(cs["id"]), pp)
        flags = student_flags(ed, lesson)
        if current["points_deducted"]:
            flags.append({
                "code": "late_deduction",
                "label": "Canvas late penalty",
                "detail": f"Canvas is deducting {fmt_number(current['points_deducted'])} pts for lateness.",
            })
        rows.append({
            "key": str(cs["id"]),
            "ed": ed_student_dict(ed),
            "canvas": {
                "user_id": cs["id"],
                "name": cs.get("name") or "",
                "sortable_name": cs.get("sortable_name") or cs.get("name") or "",
                "email": cs.get("email") or "",
                "login_id": cs.get("login_id") or "",
                "current": current,
            },
            "flags": flags,
        })
    rows.sort(key=lambda r: r["canvas"]["sortable_name"].lower())

    warnings = list(lesson.warnings) + assignment_warnings(assignment)
    if canvas_students and not any(cs.get("email") for cs in canvas_students):
        warnings.append("Canvas didn't return student emails (permissions), so students were matched on login ID.")

    return {
        "lesson": {
            "total_max": lesson.total_max,
            "has_mark": lesson.has_mark,
            "slides": [{"number": s.number, "title": s.title, "max": s.max_points, "auto": s.auto_points}
                       for s in lesson.slides],
        },
        "assignment": assignment,
        "rows": rows,
        "ed_only": [{**ed_student_dict(s), "reason": reason} for s, reason in ed_only],
        "canvas_only": [{"user_id": cs["id"], "name": cs.get("name") or "",
                         "sortable_name": cs.get("sortable_name") or cs.get("name") or "",
                         "email": cs.get("email") or "", "login_id": cs.get("login_id") or ""}
                        for cs in sorted(canvas_only, key=lambda c: (c.get("sortable_name") or c.get("name") or "").lower())],
        "warnings": warnings,
    }
