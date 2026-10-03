# EzGrader — Ed → Canvas grade sync

A small local web app that pulls lesson scores from EdStem, turns them into a score out of 100, lets you review and adjust them, and pushes them to a Canvas assignment only after you confirm.

- Runs on **127.0.0.1 only**. Nothing leaves your machine except calls to Ed and Canvas, and the page loads nothing from the internet.
- API tokens live in the **macOS Keychain**. The page only ever sees the last 4 characters.
- Every push is **backed up first**, and **Revert last push** puts the old grades back.

## Easiest: Mac app

No Python or setup needed (Apple Silicon Macs).

1. Get `EzGrader-<version>-macOS-arm64.zip`, unzip it, and drag **EzGrader.app** into **Applications**.
2. **First time only:** double-click EzGrader. macOS says it can't check the app for malware, because the app isn't signed with an Apple Developer account. Open **System Settings → Privacy & Security**, scroll down to the message about EzGrader, and click **Open Anyway**.
3. From then on, double-click **EzGrader** and your browser opens on it.

- There's no Dock icon. Quit with the **⏻** button at the top right, or just close the tab: EzGrader quits by itself a few minutes later (never in the middle of a push or write).
- Opening EzGrader while it's already running brings its tab back instead of starting a second copy.
- macOS may ask once whether EzGrader can use its Keychain items. Choose **Always Allow** (it may ask again after you update the app).
- Backups and the app's log are in `~/Library/Application Support/EzGrader`.
- To update, replace EzGrader.app with the new version. Your keys and backups stay.

### Build the Mac app

```bash
scripts/build-mac.sh
```

This takes about 20 seconds and makes `dist/EzGrader.app` plus `dist/EzGrader-<version>-macOS-arm64.zip` to share. It reuses the `.venv` from the setup below, or creates one. A Windows version has to be built on Windows with `pyinstaller EzGrader.spec`.

## Run from source

### Setup (once)

Needs Python 3.10+ (`python3 --version`).

```bash
cd EzGrader
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Run

```bash
source .venv/bin/activate
python app.py
```

Your browser opens at `http://127.0.0.1:8765/`. Stop the app with Ctrl+C. Options: `--port 9000`, `--no-browser`, `--verbose` (logs request paths, never tokens).

**Try it first without touching anything real:**

```bash
python app.py --demo
```

Demo mode uses fake students, a fake Ed, a fake Canvas, and an in-memory keyring. Canvas pushes, style grading, retries, and reverts all work, and nothing real is touched.

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

## Style grading (Ed rubrics)

Use the **Style grading** switch in the top bar to grade the style points on Ed code slides (the "30 style" in "70 auto, 30 style") without clicking through Ed's marking panel. Style grading only needs your Ed token.

1. Pick the **Ed course → lesson → code slide**, then **Load**. EzGrader lists every student with a submission on that slide and reads their marking record from Ed.
2. The **To grade** queue holds students with no style mark in Ed yet. Open one to see their code (read from Ed's code server, exactly as Ed's site opens it) next to the slide's rubric.
3. Click rubric items, or press **1–9**. In a "pick one" section, choosing an item replaces the previous one. Each choice is a **draft**; nothing is saved yet. **J / K** move to the next or previous student.
4. **Review drafts** lists every student's selection in Ed now → your draft. Tick the confirmation and **Write to Ed**: EzGrader selects those rubric items on each student's marking record, the same as ticking them in Ed, then shows what Ed saved.
5. **Revert last write** puts back the selections Ed had before the write.

Safety:

- Ed's current selections are backed up (in `backups/`) before anything is written.
- Right before writing, EzGrader re-reads each student's selection and skips anyone graded in Ed since you loaded the slide, so it never overwrites another TA's marks.
- Students' code stays in memory only. It isn't written to disk.
- Opening a student's code makes Ed create a temporary copy of that submission, as it does when you click on it in Ed. EzGrader only ever lists and opens files there; anything else is refused in code.

Once the style marks are in Ed, switch to **Canvas sync** and load the lesson as usual: the style points are part of Ed's scores.

## Files and data

| Path | What | In git? |
|---|---|---|
| `backups/` | JSON snapshots taken before each push, style write, and revert (names, emails, grades; mode 600). The Mac app keeps them in `~/Library/Application Support/EzGrader/backups` | **ignored** |
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
  style.py              style grading: code slides, rubrics, students' marking status
  code_server.py        reads a submission's files from Ed's code server (list + open only)
  keystore.py           profiles + tokens in the macOS Keychain
  backups.py            pre-push backups for revert
  server.py             Flask JSON API + request guards + push/revert jobs
  demo.py               --demo fakes
  paths.py              where files live (project folder, or the Mac app's support folder)
static/                 index.html, app.css, app.js (no framework, no CDN)
tests/                  pytest suite + synthetic fixtures
EzGrader.spec           PyInstaller recipe for the app (scripts/build-mac.sh)
assets/                 app icon (scripts/make_icons.py redraws it)
ed_probe.py, ed_sweep.py  original exploration scripts
ed_code_probe.py        read-only check of what Ed returns for code-slide submissions
```
