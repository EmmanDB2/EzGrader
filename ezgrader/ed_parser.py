"""Parse Ed's lesson results export (the undocumented results.csv endpoint).

Layout, verified against real exports:
  row 0   group labels (USERS, RESULTS, SLIDES) and slide titles
  row 1   field names: EMAIL, NAME, FIRST VIEWED, ..., [MARK, COMMENT,] NUMBER, 1, 2, ...
  row 2+  one row per student

Columns are found by name in row 1, because the layout varies between lessons
(some exports have a RESULTS section, some don't). Slides are the columns after
the NUMBER marker and are identified by position, never by title, since titles
repeat.
"""

from __future__ import annotations

import csv
import io
import math
import re
from dataclasses import dataclass, field

POINTS_RE = re.compile(r"\[\s*(\d+(?:\.\d+)?)\s*points", re.IGNORECASE)
BRACKET_RE = re.compile(r"\[([^\]]*points[^\]]*)\]?", re.IGNORECASE)
AUTO_RE = re.compile(r"(\d+(?:\.\d+)?)\s*auto\b", re.IGNORECASE)

# How far MARK may drift from our own sum before we flag it.
MARK_TOLERANCE = 0.01


class ParseError(ValueError):
    """The export isn't in the shape we expect."""


@dataclass
class Slide:
    column: int
    number: str
    title: str
    max_points: float
    auto_points: float | None = None


@dataclass
class StudentResult:
    email: str
    name: str
    first_viewed: str
    best_attempt_at: str
    scores: list[float]
    total: float
    mark: float | None


@dataclass
class LessonResults:
    slides: list[Slide]
    students: list[StudentResult]
    has_mark: bool
    warnings: list[str] = field(default_factory=list)

    @property
    def total_max(self) -> float:
        return round(sum(s.max_points for s in self.slides), 6)


def parse_max_points(title: str) -> float:
    """`Debug Code [100 points: 70 auto, 30 style]` -> 100. No label -> 0."""
    m = POINTS_RE.search(title or "")
    return float(m.group(1)) if m else 0.0


def parse_auto_points(title: str) -> float | None:
    """`[100 points: 70 auto, 30 style]` -> 70. None when there's no auto part."""
    bracket = BRACKET_RE.search(title or "")
    if not bracket:
        return None
    m = AUTO_RE.search(bracket.group(1))
    return float(m.group(1)) if m else None


def parse_score(cell: str) -> float | None:
    """Blank -> 0. Returns None for anything that isn't a number."""
    text = (cell or "").strip()
    if not text:
        return 0.0
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _norm(cell: str) -> str:
    return " ".join((cell or "").split()).upper()


def looks_like_results_csv(text: str) -> bool:
    head = (text or "").lstrip("﻿ \t\r\n")[:1]
    return bool(head) and head not in ("<", "{", "[")


def parse_results_csv(text: str) -> LessonResults:
    if not looks_like_results_csv(text):
        raise ParseError(
            "Ed didn't send back a CSV (it looks like HTML or JSON). "
            "The results endpoint may have changed, or the token can't see this lesson."
        )

    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    if len(rows) < 2:
        raise ParseError("The Ed export has fewer than two header rows.")

    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    groups, fields = rows[0], [_norm(c) for c in rows[1]]

    def find(name: str) -> int | None:
        return fields.index(name) if name in fields else None

    email_col, name_col = find("EMAIL"), find("NAME")
    viewed_col, attempt_col = find("FIRST VIEWED"), find("BEST ATTEMPT AT")
    mark_col = find("MARK")
    number_col = find("NUMBER")
    if number_col is None and "SLIDES" in [_norm(c) for c in groups]:
        number_col = [_norm(c) for c in groups].index("SLIDES")

    if email_col is None:
        raise ParseError("No EMAIL column in the Ed export. Was it requested with students=1?")
    if number_col is None:
        raise ParseError("Couldn't find the slide columns (no NUMBER marker in the Ed export).")

    slides = []
    for col in range(number_col + 1, width):
        title, number = groups[col].strip(), rows[1][col].strip()
        if not title and not number:
            continue
        max_points = parse_max_points(title)
        auto = parse_auto_points(title) if max_points else None
        slides.append(Slide(col, number or str(len(slides) + 1), title, max_points, auto))

    warnings: list[str] = []
    if not slides:
        warnings.append("The Ed export has no slide columns.")
    elif not any(s.max_points for s in slides):
        warnings.append("No slide titles had a [N points] label, so max points is 0. Set it by hand.")

    def cell(row: list[str], col: int | None) -> str:
        return row[col].strip() if col is not None else ""

    bad_cells = 0
    students = []
    for row in rows[2:]:
        if not any(c.strip() for c in row):
            continue
        scores = []
        for slide in slides:
            value = parse_score(row[slide.column])
            if value is None:
                bad_cells += 1
                value = 0.0
            scores.append(value)
        mark = parse_score(row[mark_col]) if mark_col is not None and row[mark_col].strip() else None
        students.append(StudentResult(
            email=cell(row, email_col),
            name=cell(row, name_col),
            first_viewed=cell(row, viewed_col),
            best_attempt_at=cell(row, attempt_col),
            scores=scores,
            total=round(sum(scores), 6),
            mark=mark,
        ))

    if bad_cells:
        warnings.append(f"{bad_cells} score cell(s) weren't numbers and were counted as 0.")

    return LessonResults(slides=slides, students=students, has_mark=mark_col is not None, warnings=warnings)
