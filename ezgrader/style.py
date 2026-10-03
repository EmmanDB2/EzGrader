"""Style grading: a lesson's code slides, their Ed rubrics, and each student's marking status."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

from .ed_client import EdError

BLOCK_TAGS = {"paragraph", "heading", "pre", "callout", "blockquote"}
ITEM_TAGS = {"list-item", "item", "li"}


def rich_text(value) -> str:
    """Ed's rich text (<document><paragraph>...</paragraph></document>) as plain text."""
    text = str(value or "").strip()
    if not text.startswith("<"):
        return text
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        stripped = re.sub(r"<[^>]*$", " ", re.sub(r"<[^>]+>", " ", text))  # tags, then a cut-off tag
        return re.sub(r"\s+", " ", stripped).strip()

    blocks: list[str] = []

    def visit(el) -> None:
        tag = el.tag.lower()
        if tag in BLOCK_TAGS:
            blocks.append("".join(el.itertext()).strip())
        elif tag in ITEM_TAGS:
            blocks.append("• " + " ".join(t.strip() for t in el.itertext() if t.strip()))
        else:
            if el.text and el.text.strip() and el is not root:
                blocks.append(el.text.strip())
            for child in el:
                visit(child)

    visit(root)
    found = [b for b in blocks if b]
    return "\n".join(found) if found else "".join(root.itertext()).strip()


def code_slides(lesson: dict) -> list[dict]:
    """Slides that have an Ed code challenge, numbered like the results CSV."""
    slides = sorted(lesson.get("slides") or [],
                    key=lambda s: s.get("index") if isinstance(s.get("index"), (int, float)) else 0)
    return [{
        "number": number,
        "slide_id": s.get("id"),
        "challenge_id": s["challenge_id"],
        "title": s.get("title") or f"Slide {number}",
    } for number, s in enumerate(slides, start=1) if isinstance(s.get("challenge_id"), int)]


def _by_index(items):
    return sorted(items or [], key=lambda i: i.get("index") if isinstance(i.get("index"), (int, float)) else 0)


def parse_rubric(raw: dict) -> dict:
    def item(i: dict) -> dict:
        return {
            "id": int(i["id"]),
            "points": i.get("points") or 0,
            "title": rich_text(i.get("title")),
            "description": rich_text(i.get("staff_description")),
        }

    return {
        "id": raw.get("id"),
        "positive_grading": bool(raw.get("positive_grading", True)),
        "floor": bool(raw.get("floor")),
        "ceiling": bool(raw.get("ceiling")),
        "sections": [{
            "id": s.get("id"),
            "title": rich_text(s.get("title")),
            "select_one": bool(s.get("select_one")),
            "items": [item(i) for i in _by_index(s.get("items")) if i.get("id") is not None],
        } for s in _by_index(raw.get("sections"))],
        "loose_items": [item(i) for i in _by_index(raw.get("unsectioned_items")) if i.get("id") is not None],
    }


def rubric_item_ids(rubric: dict) -> set[int]:
    ids = {i["id"] for i in rubric["loose_items"]}
    for section in rubric["sections"]:
        ids |= {i["id"] for i in section["items"]}
    return ids


def validate_selection(rubric: dict, selected: set[int]) -> str | None:
    """Why a selection can't be saved, or None if it's fine."""
    if selected - rubric_item_ids(rubric):
        return "That selection includes an item that isn't in this slide's rubric."
    for section in rubric["sections"]:
        if section["select_one"] and len(selected & {i["id"] for i in section["items"]}) > 1:
            return f"Only one item can be chosen in “{section['title'] or 'this section'}”."
    return None


def change_items(current: set[int], wanted: set[int]) -> dict[int, bool]:
    """The PUT body Ed expects: select what's new, clear what's no longer wanted."""
    return {**{i: True for i in wanted - current}, **{i: False for i in current - wanted}}


# ---------- students

def _submission_summary(s: dict) -> dict:
    return {
        "id": s.get("id"),
        "created_at": s.get("created_at"),
        "status": s.get("status") or "",
        "passed": s.get("testcase_pass_count"),
        "total": s.get("testcase_total_count"),
    }


def student_status(ed, user: dict, challenge_id: int) -> dict:
    uid = user.get("id")
    row = {
        "user_id": uid,
        "name": user.get("name") or "",
        "email": user.get("email") or "",
        "submissions": [],
        "lesson_mark_id": None,
        "auto_mark": None,
        "rubric_mark": None,
        "mark_override": None,
        "selected_ids": [],
        "graded": False,
    }
    try:
        subs = sorted(ed.user_submissions(uid, challenge_id),
                      key=lambda s: (s.get("created_at") or "", s.get("id") or 0), reverse=True)
        row["submissions"] = [_submission_summary(s) for s in subs if s.get("id")]
        row["lesson_mark_id"] = next((s["lesson_mark_id"] for s in subs if s.get("lesson_mark_id")), None)
        if row["lesson_mark_id"]:
            record = ed.lesson_mark(row["lesson_mark_id"])
            mark = record.get("lesson_mark") or {}
            row.update(
                auto_mark=mark.get("auto_mark"),
                rubric_mark=mark.get("rubric_mark"),
                mark_override=mark.get("mark_override"),
                selected_ids=sorted(int(i) for i in record.get("selected_rubric_items") or []),
            )
            row["graded"] = row["rubric_mark"] is not None or bool(row["selected_ids"])
    except EdError as e:
        row["error"] = str(e)
    return row


def load_students(make_ed, users: list[dict], challenge_id: int, workers: int = 4) -> tuple[list[dict], int]:
    """Every student with at least one submission, with their marking status.

    make_ed() returns a fresh client per worker so threads never share an HTTP session.
    Returns (students sorted by name, number of students with no submission).
    """
    students = [u for u in users if isinstance(u, dict) and u.get("id")
                and str(u.get("course_role") or "student").lower() == "student"]
    with_work = [u for u in students if u.get("submissions") is None or u.get("submissions") > 0]

    def one(user: dict) -> dict:
        return student_status(make_ed(), user, challenge_id)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(one, with_work))
    rows.sort(key=lambda r: (r["name"] or r["email"]).lower())
    return rows, len(students) - len(with_work)
