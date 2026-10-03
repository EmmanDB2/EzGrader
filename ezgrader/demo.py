"""`python app.py --demo`: the full UI on synthetic data.

Uses fake in-memory Ed and Canvas clients, an in-memory keyring (the real Keychain
is never touched), and a throwaway backup folder. Nothing leaves the machine.
"""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

import keyring
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

from .canvas_client import CanvasError
from .keystore import KeyStore

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

LESSONS = {
    178455: ("Homework 1", "homework_with_results.csv"),
    176875: ("Lab 1.1.1: Intro to OOP", "lab_without_results.csv"),
}

# Style grading: code slides with Ed-shaped rubrics, students, and submissions.
STYLE_SLIDES = {
    178455: [("Instructions", "document", None), ("Parson's problems [ 30  points ]", "quiz", None),
             ("Debug Code (1)  [100 points: 70 auto, 30 style ]", "code", 290685),
             ("Modify Code (1)  [100 points: 70 auto, 30 style ]", "code", 290688),
             ("Write Code (1)  [100 points: 70 auto, 30 style ]", "code", 290690)],
    176875: [("Instructions", "document", None),
             ("Debug Code 1 [100 points: 70 auto, 30 style]", "code", 300001),
             ("Write Code 1 [100 points: 70 auto, 30 style]", "code", 300005)],
}
STYLE_STUDENTS = ["Alice Nguyen", "Bob Smith", "Carol Diaz", "Dan Okafor", "Erin Park", "Frank Lee",
                  "Grace Kim", "Ivy Chen"]


def _doc(text: str) -> str:
    return f'<document version="2.0"><paragraph>{text}</paragraph></document>'


DEMO_RUBRIC = {
    "id": 44518, "course_id": 9001, "positive_grading": True, "floor": True, "ceiling": True,
    "unsectioned_items": [{"id": 322980, "points": 0, "index": 0, "staff_description": "",
                           "title": _doc("Style is graded on naming, structure, comments, and following the instructions.")}],
    "sections": [{"id": 9101, "title": "Style", "select_one": True, "mark_clamp": None, "index": 1, "items": [
        {"id": 322985, "points": 30, "index": 0, "title": _doc("Clean, readable code: meaningful names, consistent spacing, docstrings where useful."),
         "staff_description": _doc("Full marks. Small nitpicks are fine.")},
        {"id": 322986, "points": 20, "index": 1, "title": _doc("Minor style issues: a few unclear names or inconsistent formatting."),
         "staff_description": ""},
        {"id": 322987, "points": 10, "index": 2, "title": _doc("Major style issues: single-letter names, no structure, hard to follow."),
         "staff_description": ""},
        {"id": 322988, "points": -50, "index": 3, "title": _doc("Did not follow the instructions (e.g. changed the required function names)."),
         "staff_description": _doc("Use only when the solution ignores what the slide asked for.")},
        {"id": 322989, "points": -70, "index": 4, "title": _doc("Hard-coded output instead of solving the problem."),
         "staff_description": _doc("Removes the auto points too.")},
    ]}],
}
DEMO_POINTS = {322980: 0, 322985: 30, 322986: 20, 322987: 10, 322988: -50, 322989: -70}

DEMO_CODE = [
    '''def build_catalog(fruits):
    """Return a dict mapping each fruit name to its price."""
    catalog = {}
    for name, price in fruits:
        catalog[name.lower()] = round(price, 2)
    return catalog


def cheapest(catalog):
    """Return the name of the cheapest fruit."""
    return min(catalog, key=catalog.get)
''',
    '''def build_catalog(fruits):
    catalog = {}
    for fruit in fruits:
        catalog[fruit[0].lower()] = round(fruit[1], 2)  # store price
    return catalog

def cheapest(catalog):
    lowest = None
    for name in catalog:
        if lowest == None or catalog[name] < catalog[lowest]:
            lowest = name
    return lowest
''',
    '''def f(l):
    d={}
    for x in l:
        d[x[0].lower()]=round(x[1],2)
    return d
def c(d):
  m=None
  for k in d:
    if m==None or d[k]<d[m]: m=k
  return m
''',
    '''def build_catalog(fruits):
    return {"apple": 0.5, "banana": 0.25, "cherry": 3.0}


def cheapest(catalog):
    return "banana"
''',
]


def demo_code_reader(ed, submission_id: int) -> list[dict]:
    """Stands in for Ed's code server in demo mode."""
    time.sleep(0.5)
    code = DEMO_CODE[(submission_id // 10) % len(DEMO_CODE)]
    files = [{"path": "fruit_catalog.py", "content": code, "lines": len(code.splitlines())}]
    if submission_id % 3 == 0:
        notes = "# Scratch work\nprint(build_catalog([(\"Apple\", 0.499)]))\n"
        files.append({"path": "scratch.py", "content": notes, "lines": len(notes.splitlines())})
    return files


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.items = {}

    def get_password(self, service, username):
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        if self.items.pop((service, username), None) is None:
            raise PasswordDeleteError("missing")


class DemoEd:
    def __init__(self):
        self.lock = threading.Lock()
        # Two students already have a style mark on Modify Code (1).
        self.selected = {self._mark_id(290688, 2000): {322985}, self._mark_id(290688, 2006): {322986}}

    @staticmethod
    def _mark_id(challenge_id: int, user_id: int) -> int:
        return int(challenge_id) * 100 + (int(user_id) - 2000)

    def whoami(self):
        return {"name": "Demo TA", "email": "demo.ta@example.edu"}

    def courses(self):
        return [{"id": 9001, "code": "CS2", "name": "Data Structures (demo)", "role": "staff", "status": "active"}]

    def lessons(self, course_id):
        return [{"id": lid, "title": title, "module": "Week 1"} for lid, (title, _) in LESSONS.items()]

    def results_csv(self, lesson_id):
        time.sleep(0.4)
        return (FIXTURES / LESSONS[int(lesson_id)][1]).read_text(encoding="utf-8")

    # ----- style grading

    def lesson(self, lesson_id):
        slides = STYLE_SLIDES.get(int(lesson_id), [])
        return {"id": lesson_id, "slides": [
            {"id": 9000 + i, "index": i, "title": title, "type": kind, **({"challenge_id": cid} if cid else {})}
            for i, (title, kind, cid) in enumerate(slides)]}

    def challenge(self, challenge_id):
        title = next((t for slides in STYLE_SLIDES.values() for t, _, cid in slides if cid == int(challenge_id)),
                     "Code challenge")
        return {"id": int(challenge_id), "course_id": 9001, "title": title, "rubric_id": 44518,
                "rubric_points": 30, "auto_points": 70}

    def challenge_users(self, challenge_id):
        users = [{"id": 2000 + i, "name": name, "email": f"{name.split()[0].lower()}.{name.split()[1].lower()}@example.edu",
                  "course_role": "student", "submissions": 0 if name.startswith("Carol") else 1 + i % 3}
                 for i, name in enumerate(STYLE_STUDENTS)]
        return users + [{"id": 7, "name": "Demo TA", "email": "demo.ta@example.edu", "course_role": "staff", "submissions": 2}]

    def user_submissions(self, user_id, challenge_id):
        i = int(user_id) - 2000
        if not 0 <= i < len(STYLE_STUDENTS) or STYLE_STUDENTS[i].startswith("Carol"):
            return []
        return [{"id": int(challenge_id) * 1000 + i * 10 + k, "created_at": f"2026-09-0{k + 7}T1{i}:15:00Z",
                 "status": "passed" if k == i % 3 else "failed", "testcase_pass_count": 5 if k == i % 3 else 3,
                 "testcase_total_count": 5, "lesson_mark_id": self._mark_id(challenge_id, user_id)}
                for k in range(1 + i % 3)]

    def rubric(self, rubric_id):
        import copy
        return copy.deepcopy(DEMO_RUBRIC)

    def _lesson_mark(self, mark_id):
        ids = self.selected.get(int(mark_id), set())
        return {"id": int(mark_id), "auto_mark": 70, "mark_override": None,
                "rubric_mark": sum(DEMO_POINTS[i] for i in ids) if ids else None}

    def lesson_mark(self, mark_id):
        with self.lock:
            return {"lesson_mark": self._lesson_mark(mark_id),
                    "selected_rubric_items": sorted(self.selected.get(int(mark_id), set()))}

    def selected_rubric_items(self, mark_id):
        with self.lock:
            return sorted(self.selected.get(int(mark_id), set()))

    def set_rubric_items(self, mark_id, items):
        time.sleep(0.2)
        with self.lock:
            chosen = self.selected.setdefault(int(mark_id), set())
            for item_id, on in items.items():
                (chosen.add if on else chosen.discard)(int(item_id))
            return {"lesson_mark": self._lesson_mark(mark_id), "ids": sorted(chosen)}


class DemoCanvas:
    def __init__(self):
        self.lock = threading.Lock()
        self.failed_once = False
        self.assignments_data = {
            501: {"id": 501, "name": "Homework 1", "points_possible": 100.0, "grading_type": "points",
                  "published": True, "group": "Homework"},
            502: {"id": 502, "name": "Lab 1", "points_possible": 10.0, "grading_type": "points",
                  "published": True, "group": "Labs"},
        }
        names = ["Alice Nguyen", "Bob Smith", "Carol Diaz", "Dan Okafor", "Erin Park", "Frank Lee",
                 "Grace Kim", "Ivy Chen", "Zoe Canvasonly"]
        self.students_data = []
        for i, name in enumerate(names):
            first, last = name.split()
            email = f"{first.lower()}.{last.lower()}@example.edu"
            self.students_data.append({
                "id": 1000 + i, "name": name, "sortable_name": f"{last}, {first}",
                # Bob's email is hidden, as if by permissions: matched through login_id.
                "email": "" if first == "Bob" else email, "login_id": email if first == "Bob" else first.lower(),
            })
        self.subs = {aid: {s["id"]: {"user_id": s["id"], "score": None, "entered_score": None,
                                     "workflow_state": "unsubmitted"} for s in self.students_data}
                     for aid in self.assignments_data}
        hw = self.subs[501]
        hw[1000].update(score=85.73, entered_score=85.73, workflow_state="graded")   # already up to date
        hw[1002].update(score=40.0, entered_score=40.0, workflow_state="graded")
        hw[1004].update(excused=True, workflow_state="graded")
        hw[1006].update(score=90.0, entered_score=95.0, points_deducted=5.0, workflow_state="graded")

    def whoami(self):
        return {"name": "Demo TA", "login_id": "demo"}

    def courses(self):
        return [{"id": 7001, "name": "CS2 Data Structures (demo)", "code": "CS2", "term": "Fall 2026", "roles": ["ta"]}]

    def assignments(self, course_id):
        return [dict(a) for a in self.assignments_data.values()]

    def assignment(self, course_id, assignment_id):
        a = dict(self.assignments_data[int(assignment_id)])
        a.pop("group", None)
        return {**a, "moderated_grading": False, "anonymous_grading": False, "post_manually": False, "html_url": ""}

    def students(self, course_id):
        time.sleep(0.2)
        return [dict(s) for s in self.students_data]

    def submissions(self, course_id, assignment_id):
        with self.lock:
            return [dict(s) for s in self.subs[int(assignment_id)].values()]

    def set_grade(self, course_id, assignment_id, user_id, posted_grade):
        time.sleep(0.15)
        if user_id == 1005 and not self.failed_once:  # Frank's first push fails, to show Retry
            self.failed_once = True
            raise CanvasError("Canvas returned HTTP 500. (Demo failure. Retry will work.)")
        pp = self.assignments_data[int(assignment_id)]["points_possible"]
        if posted_grade == "":
            score = None
        elif posted_grade.endswith("%"):
            score = round(float(posted_grade[:-1]) / 100 * pp, 4)
        else:
            score = float(posted_grade)
        with self.lock:
            sub = self.subs[int(assignment_id)][int(user_id)]
            sub.update(score=score, entered_score=score, excused=False, points_deducted=None,
                       workflow_state="graded" if score is not None else "unsubmitted")
            return dict(sub)

    def excuse(self, course_id, assignment_id, user_id):
        with self.lock:
            sub = self.subs[int(assignment_id)][int(user_id)]
            sub.update(excused=True, score=None, entered_score=None)
            return dict(sub)


def demo_app_kwargs() -> dict:
    keyring.set_keyring(MemoryKeyring())
    ks = KeyStore()
    ks.create("Demo")
    ks.set_secret("Demo", "ed_token", "demo-ed-token-ED42")
    ks.set_secret("Demo", "canvas_token", "demo-canvas-token-CV42")
    ks.set_canvas_base_url("Demo", "https://canvas.example.edu")
    canvas = DemoCanvas()
    ed = DemoEd()
    return {
        "keystore": ks,
        "backup_dir": Path(tempfile.mkdtemp(prefix="ezgrader-demo-")),
        "ed_factory": lambda token: ed,
        "code_reader": demo_code_reader,
        "canvas_factory": lambda base, token: canvas,
    }
