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
    def whoami(self):
        return {"name": "Demo TA", "email": "demo.ta@example.edu"}

    def courses(self):
        return [{"id": 9001, "code": "CS2", "name": "Data Structures (demo)", "role": "staff", "status": "active"}]

    def lessons(self, course_id):
        return [{"id": lid, "title": title, "module": "Week 1"} for lid, (title, _) in LESSONS.items()]

    def results_csv(self, lesson_id):
        time.sleep(0.4)
        return (FIXTURES / LESSONS[int(lesson_id)][1]).read_text(encoding="utf-8")


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
    return {
        "keystore": ks,
        "backup_dir": Path(tempfile.mkdtemp(prefix="ezgrader-demo-")),
        "ed_factory": lambda token: DemoEd(),
        "canvas_factory": lambda base, token: canvas,
    }
