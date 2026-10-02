# EzGrader — Ed → Canvas grade sync

A small local web app that pulls lesson scores from EdStem, turns them into a score out of 100, lets you review and adjust them, and pushes them to a Canvas assignment only after you confirm.

- Runs on **127.0.0.1 only**. Nothing leaves your machine except calls to Ed and Canvas, and the page loads nothing from the internet.
- API tokens live in the **macOS Keychain**. The page only ever sees the last 4 characters.
- Every push is **backed up first**, and **Revert last push** puts the old grades back.

## Setup (once)

Needs Python 3.10+ (`python3 --version`).

```bash
cd EzGrader
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
source .venv/bin/activate
python app.py
```

Your browser opens at `http://127.0.0.1:8765/`. Stop the app with Ctrl+C. Options: `--port 9000`, `--no-browser`, `--verbose` (logs request paths, never tokens).

**Try it first without touching anything real:**

```bash
python app.py --demo
```

Demo mode uses fake students, a fake Ed, a fake Canvas, and an in-memory keyring. Push, retry, and revert all work, and nothing real is touched.

## First time: add your keys

Click **Keys** (top right), create a profile (e.g. your name), then:

| Field | Where to get it |
|---|---|
| Ed API token | <https://edstem.org/us/settings/api-tokens> |
| Canvas address | Your school's Canvas URL, e.g. `https://canvas.school.edu` |
| Canvas API token | Canvas → Account → Settings → **+ New Access Token** |

Press **Test** after saving each one. Other TAs on the same Mac can add their own profiles. Deleting a profile removes its tokens from the Keychain.

## Using it

1. Pick the **Ed course → lesson** and the **Canvas course → assignment**, then **Load**.
2. Review the table:
   - **Computed /100** = `round(earned / max × 100, 2)`, rounding halves up.
   - **Max pts** defaults to the sum of the `[N points]` labels in Ed's slide titles. Change it and every row recalculates.
   - Type in **Final /100** to override a score. Edited cells turn purple. Press ↺ (or Esc) to go back to the computed value. Enter / ↑ / ↓ move between rows.
   - **Slides** shows per-slide scores. Amber cells are code questions sitting at exactly their auto points (style may not be graded yet).
   - Flags: *Viewed, no submission* · *At auto points* · *MARK ≠ sum* · *Canvas late penalty* · *Excused*.
   - **Unmatched** lists students in Ed but not Canvas and the other way round. They're never pushed.
3. **Review changes** shows old → new for every grade that would change. Untick students to leave them out. It's a good idea to push **one student first**, check them in Canvas, then push the rest.
4. Tick the confirmation and **Push**. You'll see live progress and a result for each student, and can **Retry** any failures.
5. **Revert last push** puts the saved grades back (it warns you if someone changed a grade in Canvas since then). Each revert backs up first too, and repeated reverts walk back through earlier pushes.

### How grades are sent to Canvas

- Assignment out of **100 points**: the score is sent as points (`85.73`).
- Out of anything else (e.g. 10): it's sent as a percentage (`85.73%` → 8.573 / 10).
- Only grades that differ from Canvas are pushed, unless you tick *Also re-send unchanged grades*.
- Excused students and students not on the assignment are always skipped.
- Pass/fail and ungraded assignments are refused.

## Files and data

| Path | What | In git? |
|---|---|---|
| `backups/` | JSON snapshot of Canvas grades before each push/revert (names, emails, grades; mode 600) | **ignored** |
| `fixtures/` | Your real (anonymized) Ed exports | **ignored** |
| `tests/fixtures/` | Synthetic exports with fake students | yes |

Ed CSVs are parsed in memory and never written to disk. Grade edits live only in the open page. Reloading the page drops them, and the browser warns you first.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The tests cover the CSV parser (both lesson layouts, odd point labels, repeated titles, blank/non-numeric cells), matching and flags, the Canvas client (pagination, rate limits, refusing to send the token to other hosts), the Keychain store, and the server end to end (session/Host guards, tokens never returned, push → backup → revert).

## Troubleshooting

- **"EzGrader was restarted. Reload this page."** The page belongs to an earlier run. Reload it.
- **"Ed's results endpoint didn't return a CSV"** The results endpoint is undocumented. Check you can open the lesson's results in Ed. If Ed changed the endpoint, `ezgrader/ed_client.py` has the request.
- **Students unmatched because Canvas hides emails** EzGrader falls back to the Canvas login ID. If that isn't the email either, those students show under *Unmatched*.
- **Keychain prompt** macOS may ask once whether Python can use the "EzGrader" Keychain items. Choose *Always Allow*.

## Layout

```
app.py                  start the server (127.0.0.1) and open the browser
ezgrader/
  ed_client.py          Ed API (read-only)
  ed_parser.py          results.csv → slides, students, totals
  canvas_client.py      Canvas API (reads + grade updates)
  grades.py             score formula, matching, flags, review rows
  keystore.py           profiles + tokens in the macOS Keychain
  backups.py            pre-push backups for revert
  server.py             Flask JSON API + request guards + push/revert jobs
  demo.py               --demo fakes
static/                 index.html, app.css, app.js (no framework, no CDN)
tests/                  pytest suite + synthetic fixtures
ed_probe.py, ed_sweep.py  original exploration scripts
```
