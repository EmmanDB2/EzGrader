#!/usr/bin/env python3
"""
ed_probe.py - READ-ONLY test of Ed API access before building the Ed -> Canvas pipeline.

Nothing here writes to Ed or Canvas. It only checks:
  1. that your token works          (whoami)
  2. which lessons a course has     (lessons <course_id>)
  3. whether the results endpoint gives you per-question scores (results <lesson_id>)

Setup:
  pip install requests
  export ED_API_TOKEN="..."      # already done, apparently
  export ED_REGION="us"          # optional, defaults to us

Usage:
  python ed_probe.py whoami
  python ed_probe.py lessons 12345
  python ed_probe.py results 67890
"""

import argparse
import csv
import io
import os
import sys
from pathlib import Path

import requests

TOKEN = os.environ.get("ED_API_TOKEN")
REGION = os.environ.get("ED_REGION", "us")
BASE = f"https://{REGION}.edstem.org/api"
OUT_DIR = Path("ed_exports")  # add this folder to .gitignore - it holds student data

if not TOKEN:
    sys.exit("ED_API_TOKEN isn't set. Export it first.")

HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def explain(resp):
    """Turn common failure codes into something useful."""
    hints = {
        401: "Token rejected - expired, revoked, or copied wrong.",
        403: "Token works but your role can't access this. Ask the course admin about permissions.",
        404: "Endpoint or ID not found - wrong ID, or Ed changed the endpoint.",
        429: "Rate limited. Wait a minute and retry.",
    }
    return hints.get(resp.status_code, f"HTTP {resp.status_code}: {resp.text[:200]}")


def whoami():
    r = requests.get(f"{BASE}/user", headers=HEADERS, timeout=30)
    if not r.ok:
        sys.exit(explain(r))
    data = r.json()
    user = data.get("user", {})
    print(f"Token OK - logged in as {user.get('name')} ({user.get('email')})\n")
    print(f"{'COURSE ID':<10} {'ROLE':<10} CODE / NAME")
    for entry in data.get("courses", []):
        c = entry.get("course", {})
        role = (entry.get("role") or {}).get("role", "?")
        print(f"{c.get('id', '?'):<10} {role:<10} {c.get('code', '')} - {c.get('name', '')}")


def lessons(course_id):
    r = requests.get(f"{BASE}/courses/{course_id}/lessons", headers=HEADERS, timeout=30)
    if not r.ok:
        sys.exit(explain(r))
    data = r.json()
    modules = {m.get("id"): m.get("name") for m in data.get("modules", [])}
    items = data.get("lessons", [])
    if not items:
        print("No lessons came back. Raw keys in response:", list(data.keys()))
        return
    print(f"{'LESSON ID':<10} {'MODULE':<20} TITLE")
    for l in items:
        mod = modules.get(l.get("module_id"), "") or ""
        print(f"{l.get('id', '?'):<10} {mod[:19]:<20} {l.get('title', '')}")


def looks_like_csv(text):
    head = text.lstrip()[:1]
    return bool(text.strip()) and head not in ("<", "{", "[")


def results(lesson_id):
    url = f"{BASE}/lessons/{lesson_id}/results.csv"
    params = {
        "numbers": "0",
        "scores": "1",          # we want actual scores, not just completion
        "students": "1",
        "completions": "0",
        "strategy": "best",
        "ignore_late": "0",
        "late_no_points": "1",
        "tz": "America/New_York",
    }

    # This endpoint is undocumented, so try the auth styles people have used.
    attempts = [
        ("POST + Bearer header", lambda: requests.post(url, params=params, headers=HEADERS, timeout=60)),
        ("POST + _token form field", lambda: requests.post(url, params=params, data={"_token": TOKEN}, timeout=60)),
        ("GET + Bearer header", lambda: requests.get(url, params=params, headers=HEADERS, timeout=60)),
    ]

    for label, call in attempts:
        r = call()
        if r.ok and looks_like_csv(r.text):
            print(f"Success via {label}.\n")
            save_and_summarize(lesson_id, r.text)
            return
        print(f"x {label}: {explain(r) if not r.ok else 'response was not CSV'}")

    print("\nAll attempts failed. Next step: open the lesson's results/marking page in Ed,")
    print("open DevTools > Network, reload, and look for the request that loads scores.")


def save_and_summarize(lesson_id, text):
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / f"lesson_{lesson_id}_results.csv"
    path.write_text(text, encoding="utf-8")

    rows = list(csv.reader(io.StringIO(text)))
    header, body = rows[0], rows[1:]
    print(f"Saved {len(body)} student rows to {path}")
    print("\nColumns (safe to paste back to Claude - no student info):")
    for i, col in enumerate(header):
        print(f"  [{i}] {col}")


def main():
    p = argparse.ArgumentParser(description="Read-only Ed API probe")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("whoami")
    sub.add_parser("lessons").add_argument("course_id")
    sub.add_parser("results").add_argument("lesson_id")
    args = p.parse_args()

    if args.cmd == "whoami":
        whoami()
    elif args.cmd == "lessons":
        lessons(args.course_id)
    elif args.cmd == "results":
        results(args.lesson_id)


if __name__ == "__main__":
    main()
