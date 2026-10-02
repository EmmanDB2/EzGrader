# Ed → Canvas Grade Sync — Build Spec

A local web tool that pulls lesson scores from EdStem, converts them to a score out of 100, lets a TA review and adjust them in a nice browser UI, and pushes them to a Canvas assignment only after explicit confirmation.

Runs **locally only** (bind to `127.0.0.1`). Handles student grade data (FERPA): nothing leaves the machine except calls to Ed and Canvas.

Existing working scripts in this folder: `ed_probe.py` (token check, lesson list, results fetch) and `ed_sweep.py` (param sweep). Reuse their logic.

---

## What we already know about Ed (verified)

- **Auth:** `Authorization: Bearer <ED_API_TOKEN>`. Tokens from `https://edstem.org/us/settings/api-tokens`. Base URL `https://us.edstem.org/api`.
- **Who am I / courses:** `GET /api/user` → `user`, `courses[].course.{id,code,name}`, `courses[].role.role` (the TA's role is `staff`, and that's enough to read results).
- **Lessons in a course:** `GET /api/courses/{course_id}/lessons` → `lessons[]` (id, title, module_id), `modules[]`.
- **Results (undocumented endpoint):** `POST /api/lessons/{lesson_id}/results.csv` with query params:
  `numbers=0, scores=1, students=1, completions=0, strategy=best, ignore_late=0, late_no_points=1, tz=America/New_York`
  - **`completions` MUST be `0`.** With `completions=1` the cells are completion timestamps, not scores. (`numbers`/`scores` didn't change anything in testing.)
  - Treat this endpoint as fragile: handle non-CSV responses and show a clear error.

### Results CSV shape

- **Two header rows.**
  - Row 0 = group labels (`USERS`, `RESULTS`, `SLIDES`) and slide titles.
  - Row 1 = field names: `EMAIL, NAME, FIRST VIEWED, BEST ATTEMPT AT, BEST SUBMISSION TYPE, …`, then sometimes `MARK, COMMENT`, then `NUMBER, 1, 2, 3…` for slides.
- **Find columns by header names in row 1, not fixed indexes.** The column layout varies between lessons: the homework export had a `RESULTS` section (`MARK`, `COMMENT`) and the lab export did not.
- **Slide score columns:** the columns after the `NUMBER` marker. Each has its title in row 0 and its number in row 1.
- **Slide titles repeat** (e.g. two "Debug Code (1)" columns), so identify slides by column/slide number, never by title.
- **Max points** come from the slide titles, which use inconsistent formats:
  `[5 points]`, `[ 30  points ]`, `[100 points: 70 auto, 30 style ]`.
  Use a regex like `\[\s*(\d+(?:\.\d+)?)\s*points` (case-insensitive). Slides with no points label (Instructions, Using GenAI, surveys) count as 0 max.
  - Examples: the homework was 30 + 10×100 = **1030**; the lab was 4×5 + 5×100 = **520**.
- **Scores** are plain numbers, sometimes with float noise (`33.33000183`). Blank = 0.
- **`MARK`, when present,** equals the sum of slide scores. Still compute the sum ourselves, and warn if it disagrees with MARK by more than 0.01.
- **Manual style points are included** in slide scores (values like 80/90/100 on 70-auto questions).

## Grade formula

`score_100 = round(total_earned / total_max * 100, 2)`

The TA confirmed this is the formula they already use.

## Canvas (verify endpoints against current Canvas API docs before relying on them)

- **Auth:** Bearer token. The Canvas base URL is configurable (the school's Canvas domain).
- **Pick course + assignment from dropdowns:** list the TA's courses, then that course's assignments.
- **Students:** the course users endpoint, filtered to students, including email.
  - If emails come back hidden because of permissions, fall back to `login_id`.
  - Matching: case-insensitive, trimmed.
- **Current grades:** fetch the existing submissions so the UI can show old vs new.
- **Push:** per-student submission grade update (`posted_grade`), or the bulk `update_grades` endpoint with progress polling. Pick whichever is more reliable; report per-student success/failure either way.

---

## Features

### 1. API keys panel
- Paste fields for the **Ed token**, the **Canvas token**, and the **Canvas base URL**.
- On save: store in the **macOS Keychain** (via the `keyring` package), clear the input, and show only `••••last4` plus a "Test" button (Ed: `/api/user`; Canvas: the self/user endpoint).
- Support multiple **profiles** so other TAs can use the tool on the same machine. Allow deleting a profile.
- Never log tokens, never return them to the frontend after saving, and never write them to disk in plain text.

### 2. Review table (the main screen)
- **Flow:** select Ed course → lesson → Canvas course → assignment → "Load".
- **Columns:** name, email, per-slide scores (collapsible), total earned / max, **computed /100**, **final /100 (editable)**, current Canvas grade, status.
- **Editable final score** per student; edited cells are visibly highlighted, with a reset-to-computed button.
- **Editable max points** at the top (defaults to the parsed value) that recalculates every row live.
- **Flags to show:**
  - Students in Ed but not in Canvas, and vice versa (show these as their own list).
  - 0 totals where the student opened the lesson ("FIRST VIEWED" is set) but never submitted.
  - Code questions sitting at exactly their auto points (e.g. exactly 70 on a "70 auto, 30 style" slide), since style might not be graded yet.
  - MARK/sum disagreement.
- Sort and filter (e.g. "only changed", "only flagged").

### 3. Confirm and push
- **"Review changes"** opens a summary: N grades will change, M unchanged (skipped), K unmatched. List the old → new values.
- **The push requires an explicit confirm.** Show live progress and per-student results.
- **Before pushing,** save a backup of the current Canvas grades for those students to a local JSON file (gitignored) and offer **"Revert last push"**.
- Push only changed grades by default.

### 4. Pretty UI
- Clean, modern, and readable at half-screen width (side-by-side with Canvas). Light/dark mode.
- Sticky header and table header, good spacing, subtle color for flags and edits, toast notifications.
- No heavy framework is needed. Plain HTML/CSS/JS served by the backend is fine.

---

## Tech
- **Backend:** Python 3 (FastAPI or Flask), `requests`, `keyring`.
- **Frontend:** a single page served by the backend. Open the browser automatically on start.
- **Security:**
  - Bind to `127.0.0.1` only.
  - Add a `.gitignore` covering exports, backups, `.venv`, and `.env`.
  - Don't keep CSVs on disk unless the user exports one.
- **Startup:** one command (e.g. `python app.py`), plus a README with setup steps.

## Build order
1. Ed client + CSV parser, with unit tests using an anonymized fixture (the two lesson shapes above).
2. Canvas client: list courses/assignments/students, read current grades. Read-only first.
3. Review UI, with no push button yet.
4. Push + backup + revert. Test on a single student before using it on the whole class.
5. Keys panel + profiles.
