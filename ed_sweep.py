#!/usr/bin/env python3
"""
ed_sweep.py - READ-ONLY. Tries every combo of the results.csv flags and reports
what kind of values land in the score cells (dates? numbers? blanks?).
Prints NO names or emails - only value types and a few sample cell values.

Usage:
  python3 ed_sweep.py 178455
"""

import csv
import io
import itertools
import os
import re
import sys
import time

import requests

TOKEN = os.environ.get("ED_API_TOKEN") or sys.exit("ED_API_TOKEN isn't set.")
REGION = os.environ.get("ED_REGION", "us")
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}|\d{1,2}:\d{2}")
NUM_RE = re.compile(r"^\s*-?\d+(\.\d+)?\s*(/\s*\d+(\.\d+)?)?\s*%?\s*$")


def classify(cell):
    c = cell.strip()
    if not c:
        return "blank"
    if DATE_RE.search(c):
        return "date"
    if NUM_RE.match(c):
        return "number"
    return "other"


def sweep(lesson_id):
    url = f"https://{REGION}.edstem.org/api/lessons/{lesson_id}/results.csv"
    flags = ["numbers", "scores", "completions"]

    for combo in itertools.product(["0", "1"], repeat=3):
        params = dict(zip(flags, combo))
        params.update({"students": "1", "strategy": "best", "ignore_late": "0",
                       "late_no_points": "1", "tz": "America/New_York"})
        label = " ".join(f"{k}={v}" for k, v in zip(flags, combo))

        r = requests.post(url, params=params, headers=HEADERS, timeout=60)
        if not r.ok:
            print(f"{label}  ->  HTTP {r.status_code}")
            time.sleep(1)
            continue

        rows = list(csv.reader(io.StringIO(r.text)))
        cells = [c for row in rows[2:] for c in row[6:]]
        counts = {"blank": 0, "date": 0, "number": 0, "other": 0}
        samples = {"number": set(), "other": set()}
        for c in cells:
            kind = classify(c)
            counts[kind] += 1
            if kind in samples and len(samples[kind]) < 5:
                samples[kind].add(c.strip())

        flag = "  <-- SCORES?" if counts["number"] else ""
        print(f"{label}  ->  {counts}{flag}")
        for kind, vals in samples.items():
            if vals:
                print(f"    sample {kind}: {sorted(vals)}")
        time.sleep(1)  # be polite, avoid rate limits


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python3 ed_sweep.py <lesson_id>")
    sweep(sys.argv[1])
