#!/usr/bin/env python3
"""
ed_code_probe.py - READ-ONLY check of what Ed's API returns for code-slide submissions.

Before building AI style grading we need to know: can we pull each student's code for
a lesson's code slides, and where do marks and feedback live?

The report (stdout) contains only the *shape* of Ed's responses: field names, types,
text sizes, scores, and short values like "graded" or "Main.java". Names, emails,
IDs, URLs, and code are never printed, so the report is safe to paste back to Claude.

Read-only by default: it sends GET requests, plus the POST "download" requests that
Ed's own export buttons use. With --files it also does what Ed's site does when you
click on a submission: asks Ed to open a temporary copy of it on Ed's code server,
then lists and opens the files. Sending anything else to the code server (such as an
edit) is blocked in code. It never calls Ed's feedback, submit_all, or delete endpoints.

Token: the ED_API_TOKEN environment variable, or the Ed token saved in an EzGrader
profile (macOS Keychain).

Usage:
  python ed_code_probe.py LESSON_ID                          # first "Modify Code" slide
  python ed_code_probe.py LESSON_ID --slide 5                # slide number, as in the results CSV
  python ed_code_probe.py LESSON_ID --email you@school.edu   # sample this student
  python ed_code_probe.py LESSON_ID > probe.txt              # report to a file; progress stays on screen
  python ed_code_probe.py LESSON_ID --files > probe.txt      # also read one submission's files
                                                             # (needs: pip install -r requirements.txt)
  python ed_code_probe.py LESSON_ID --save                   # ALSO save one raw submission to ed_exports/
                                                             # for your eyes only (it's student data)
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import socket
import sys
import time
import zipfile
from collections import Counter
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests

from ezgrader.ed_client import ED_BASE, ED_REGION, explain

MAX_USERS_TO_TRY = 25
MAX_DEPTH = 7
TZ = "America/New_York"
SAVE_DIR = Path(__file__).resolve().parent / "ed_exports"  # gitignored

# Ed's code server, as seen in the browser: POST .../connect returns a one-time ticket,
# then a websocket carries the files.
CONNECT_PAYLOAD = {"user_id": None, "password": None, "i": None}  # what the browser sends
DEFAULT_WS_HOST = f"sahara.{ED_REGION}.edstem.org"  # Ed's workspaceWebSocketUrl for the region
WS_ORIGIN = "https://edstem.org"  # the browser's Origin; the ticket is the actual credential
WS_TIMEOUT = 15
MAX_FILES = 10
# The only messages the probe may send to the code server: list a folder, open a file.
ALLOWED_SENDS = {("fsop", "list_folder"), ("file_open", None)}
# Text fields in a file_ot_init message that can't be the file's contents.
NOT_CONTENT_KEYS = {"path", "type", "uri", "name", "hash", "language", "encoding"}
# Runner configuration that's noise for our purposes.
COLLAPSE_KEYS = {"tickets"}

CODE_SLIDE_TYPES = {"code", "jupyter", "rstudio", "sql", "challenge", "workspace"}
CODE_EXTENSIONS = {
    "py", "java", "c", "cc", "cpp", "h", "hpp", "js", "ts", "jsx", "tsx", "txt", "md", "json", "csv",
    "ipynb", "rb", "go", "rs", "kt", "cs", "sql", "html", "css", "r", "sh", "xml", "yml", "yaml",
    "scala", "swift", "m", "pl", "php", "lua", "hs", "ml", "s", "asm",
}
# Numbers under keys with these words are identifiers: hidden. So are numbers in lists
# (often ID lists) unless the key is about marks, and any integer >= 100000.
ID_WORDS = {"id", "ids", "uid", "sid", "uuid", "user", "student", "number", "phone", "sis", "login",
            "author", "owner", "creator", "marker", "grader", "tutor", "staff", "member", "members", "by"}
LARGE_NUMBER = 100_000
# Short strings under keys with these words are shown ("graded", "java").
ENUM_WORDS = {"type", "status", "state", "kind", "mode", "language", "lang", "strategy", "format", "role",
              "visibility", "scheme", "runtime", "extension", "ext", "method", "outcome", "verdict", "level",
              "category", "stage"}
# File names under keys with these words are shown when they end in a code extension.
FILE_WORDS = {"name", "filename", "file", "path", "basename"}
MARK_WORDS = {"mark", "marks", "score", "scores", "feedback", "criteria", "grade", "grades", "points",
              "result", "results", "passed", "failed", "total", "comment", "comments", "rubric"}

EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
URL_RE = re.compile(r"^(https?:)?//", re.IGNORECASE)
DATETIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
SIMPLE_RE = re.compile(r"[\w .:/+#-]{1,40}")
FILE_RE = re.compile(r"[\w.\-/]{1,80}\.(\w{1,8})")
HEADER_CELL_RE = re.compile(r"[A-Z0-9 _#()/%.:\-\[\]]*")


class ProbeError(Exception):
    pass


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# ---------- Ed access (read-only on purpose: no PUT/DELETE helpers exist)

class Ed:
    def __init__(self, token: str, session: requests.Session | None = None, base: str = ED_BASE,
                 delay: float = 0.25):
        self.base = base
        self.session = session or requests.Session()
        self.delay = delay
        self.calls = Counter()
        self._headers = {"Authorization": f"Bearer {token}"}

    def _send(self, method: str, path: str, params: dict | None = None,
              json_body: dict | None = None) -> requests.Response:
        if self.calls:
            time.sleep(self.delay)
        self.calls[method] += 1
        try:
            resp = self.session.request(method, f"{self.base}{path}", params=params, json=json_body,
                                        headers=self._headers, timeout=90)
        except requests.RequestException as e:
            raise ProbeError(f"couldn't reach Ed ({type(e).__name__})") from None
        if not resp.ok:
            message = explain(resp)
            raise ProbeError(message if str(resp.status_code) in message else f"HTTP {resp.status_code}: {message}")
        return resp

    def get_json(self, path: str, params: dict | None = None):
        resp = self._send("GET", path, params)
        try:
            return resp.json()
        except ValueError:
            raise ProbeError("the response wasn't JSON") from None

    def download(self, path: str, params: dict | None = None) -> requests.Response:
        """The POST that Ed's export/download buttons send. Reads data; changes nothing."""
        return self._send("POST", path, params)

    def connect_submission(self, submission_id: int) -> dict:
        """Ask Ed to open a temporary copy of a submission on its code server, as the browser
        does when you click on one. Returns the reply holding the one-time ticket."""
        resp = self._send("POST", f"/challenges/submissions/{int(submission_id)}/connect",
                          json_body=CONNECT_PAYLOAD)
        try:
            return resp.json()
        except ValueError:
            raise ProbeError("the connect reply wasn't JSON") from None


# ---------- privacy-safe description of JSON

def key_words(key) -> set[str]:
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(key))
    return {w for w in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if w}


def identifying_key(key) -> bool:
    """Dict keys that are themselves data (user IDs, emails) rather than field names."""
    k = str(key)
    return bool(re.fullmatch(r"\d+", k) or "@" in k or re.search(r"\s", k)
                or re.fullmatch(r"[0-9a-fA-F-]{16,}", k))


class Shape:
    """Merged description of every value seen at one spot in a JSON document."""

    def __init__(self, show_file_names: bool = True):
        self.show_file_names = show_file_names
        self.count = 0
        self.kinds = Counter()
        self.fields: dict[str, Shape] = {}
        self.item: Shape | None = None
        self.sizes: list[int] = []
        self.shown: list = []
        self.notes: set[str] = set()
        self.min_len: int | None = None
        self.max_len = 0
        self.max_lines = 0

    def add(self, value, key: str = "", in_list: bool = False) -> None:
        self.count += 1
        if isinstance(value, dict):
            self.kinds["object"] += 1
            for k, v in value.items():
                name = "<key>" if identifying_key(k) else str(k)
                self.fields.setdefault(name, Shape(self.show_file_names)).add(v, "" if name == "<key>" else str(k))
        elif isinstance(value, list):
            self.kinds["list"] += 1
            self.sizes.append(len(value))
            for v in value[:200]:
                self.item = self.item or Shape(self.show_file_names)
                self.item.add(v, key, in_list=True)
        elif isinstance(value, bool):
            self.kinds["bool"] += 1
        elif value is None:
            self.kinds["null"] += 1
        elif isinstance(value, (int, float)):
            self.kinds["number"] += 1
            words = key_words(key)
            if words & ID_WORDS:
                self.notes.add("id hidden")
            elif abs(value) >= LARGE_NUMBER or (in_list and not words & MARK_WORDS):
                self.notes.add("values hidden")
            else:
                self._show(value)
        elif isinstance(value, str):
            self._add_text(value, key)
        else:
            self.kinds[type(value).__name__] += 1

    def _show(self, value) -> None:
        if value not in self.shown and len(self.shown) < 5:
            self.shown.append(value)

    def _add_text(self, text: str, key: str) -> None:
        self.kinds["text"] += 1
        self.min_len = len(text) if self.min_len is None else min(self.min_len, len(text))
        self.max_len = max(self.max_len, len(text))
        self.max_lines = max(self.max_lines, len(text.splitlines()))
        words = key_words(key)
        if EMAIL_RE.search(text):
            self.notes.add("contains email")
        elif URL_RE.match(text):
            self.notes.add("url")
        elif DATETIME_RE.match(text):
            self.notes.add("datetime")
        elif text.lstrip().startswith("<document"):
            self.notes.add("Ed rich text")
        elif (self.show_file_names and words & FILE_WORDS and (m := FILE_RE.fullmatch(text))
              and m.group(1).lower() in CODE_EXTENSIONS):
            self._show(text)
        elif words & ENUM_WORDS and SIMPLE_RE.fullmatch(text):
            self._show(text)

    def describe(self) -> str:
        parts = []
        for kind, _ in self.kinds.most_common():
            if kind == "list":
                lo, hi = min(self.sizes), max(self.sizes)
                parts.append(f"list[{lo}]" if lo == hi else f"list[{lo}-{hi}]")
            elif kind == "text":
                size = f"{self.min_len}" if self.min_len == self.max_len else f"{self.min_len}-{self.max_len}"
                lines = f", up to {self.max_lines} lines" if self.max_lines > 1 else ""
                parts.append(f"text ({size} chars{lines})")
            else:
                parts.append(kind)
        desc = " | ".join(parts)
        if self.shown:
            desc += "  e.g. " + ", ".join(json.dumps(v) for v in self.shown)
        if self.notes:
            desc += "  [" + ", ".join(sorted(self.notes)) + "]"
        return desc

    def render(self, name: str, depth: int = 0, parent_objects: int | None = None) -> list[str]:
        optional = "  (optional)" if parent_objects is not None and self.count < parent_objects else ""
        lines = [f"{'  ' * depth}{name}: {self.describe()}{optional}"]
        if name in COLLAPSE_KEYS and (self.fields or self.item):
            return [lines[0] + "  (nested settings collapsed)"]
        if depth >= MAX_DEPTH:
            if self.fields or self.item:
                lines.append(f"{'  ' * (depth + 1)}...")
            return lines
        for field_name, child in self.fields.items():
            lines += child.render(field_name, depth + 1, self.kinds["object"])
        if self.item is not None:
            if self.item.fields or self.item.item:
                lines += self.item.render("[]", depth + 1)
            else:
                lines[0] += f"  of {self.item.describe()}"
        return lines


def shape_of(value, name: str = "response", show_file_names: bool = True) -> str:
    return shape_of_all([value], name, show_file_names)


def shape_of_all(values, name: str, show_file_names: bool = True) -> str:
    """One merged shape for several values of the same kind (e.g. messages of one type)."""
    shape = Shape(show_file_names)
    for value in values:
        shape.add(value)
    return "\n".join(shape.render(name))


def walk(value, path: str = "", key: str = ""):
    """Yield (path, key, value) for every node. List indexes become []."""
    yield path, key, value
    if isinstance(value, dict):
        for k, v in value.items():
            name = "<key>" if identifying_key(k) else str(k)
            yield from walk(v, f"{path}.{name}" if path else name, str(k))
    elif isinstance(value, list):
        for v in value:
            yield from walk(v, f"{path}[]", key)


def code_candidates(value) -> list[tuple[str, str]]:
    """Multi-line strings that aren't Ed rich-text documents: probably code (or test output)."""
    return [(path, v) for path, _, v in walk(value)
            if isinstance(v, str) and "\n" in v.strip() and not v.lstrip().startswith("<document")]


def normalize_code(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


def mark_paths(value) -> list[str]:
    paths = []
    for path, key, _ in walk(value):
        if key and key_words(key) & MARK_WORDS and path not in paths:
            paths.append(path)
    return paths


def find_challenge_id(obj, depth: int = 0) -> tuple[str, int] | None:
    """Look for a challenge ID inside a slide: a key mentioning 'challenge' holding an int or {id}."""
    if not isinstance(obj, dict) or depth > 3:
        return None
    for k, v in obj.items():
        if "challenge" in str(k).lower():
            if isinstance(v, int) and not isinstance(v, bool):
                return str(k), v
            if isinstance(v, dict) and isinstance(v.get("id"), int):
                return f"{k}.id", v["id"]
    for k, v in obj.items():
        if isinstance(v, dict) and (found := find_challenge_id(v, depth + 1)):
            return f"{k}.{found[0]}", found[1]
    return None


def zip_shapes(data: bytes) -> dict:
    """Describe a zip's layout with student-specific folder and file names replaced by tags."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        files = [i for i in zf.infolist() if not i.is_dir()]
    names = Counter(Path(i.filename).name for i in files)
    common = {n for n, count in names.items() if count >= 2}  # same name for several students: a challenge file

    def redact(part: str, is_file: bool) -> str:
        if is_file and part in common:
            return part
        stem, ext = os.path.splitext(part) if is_file else (part, "")
        tag = "email" if "@" in stem else "number" if stem.isdigit() else "text"
        return f"<{'file' if is_file else 'dir'}:{tag}>{ext if ext[1:].lower() in CODE_EXTENSIONS else ''}"

    shapes = Counter()
    for info in files:
        parts = [p for p in info.filename.split("/") if p]
        shapes["/".join([redact(p, False) for p in parts[:-1]] + [redact(parts[-1], True)])] += 1
    sizes = [i.file_size for i in files]
    return {
        "files": len(files),
        "top_level": len({i.filename.split("/")[0] for i in files}),
        "extensions": Counter(Path(i.filename).suffix.lower() or "(none)" for i in files).most_common(8),
        "shapes": shapes.most_common(10),
        "bytes": (min(sizes), max(sizes)) if sizes else (0, 0),
    }


def csv_header(text: str) -> tuple[list[list[str]], int]:
    """First row (and a second row only if it's clearly another header row), plus the data row count."""
    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    if not rows:
        return [], 0
    if any("@" in c for c in rows[0]):  # no header row: the first row is a student
        return [["<first row is data, not shown>"] * len(rows[0])], len(rows) - 1
    safe = lambda row: [c.strip()[:60] for c in row]
    header = [safe(rows[0])]
    if len(rows) > 1 and all(HEADER_CELL_RE.fullmatch(c.strip()) and "@" not in c for c in rows[1]):
        header.append(safe(rows[1]))
    return header, len(rows) - len(header)


# ---------- Ed's code server (only with --files)

def _timeout_errors() -> tuple:
    errors = [TimeoutError, socket.timeout]
    try:
        import websocket
        errors.append(websocket.WebSocketTimeoutException)
    except ImportError:
        pass
    return tuple(errors)


TIMEOUT_ERRORS = _timeout_errors()


def find_ws_url(reply) -> tuple[str, str]:
    """Where to open the code-server connection, from the connect reply. Returns (url, how)."""
    strings = [(key.lower(), v) for _, key, v in walk(reply) if isinstance(v, str) and v]
    for _, v in strings:
        if v.startswith(("wss://", "ws://")) and "ticket=" in v:
            return v, "a full URL in the reply"
    ticket = next((v for k, v in strings if "ticket" in k), None)
    if not ticket:
        raise ProbeError("the connect reply had no ticket or code-server URL")
    base = next((v for _, v in strings if v.startswith(("wss://", "ws://"))), None)
    if base:
        return f"{base}{'&' if '?' in base else '?'}ticket={quote(ticket)}", "a URL plus a ticket in the reply"
    host = next((v for k, v in strings if k in ("host", "hostname", "server", "domain")), None)
    if host:
        host = re.sub(r"^\w+://", "", host).rstrip("/")
        return f"wss://{host}/connect?ticket={quote(ticket)}", "a host plus a ticket in the reply"
    return (f"wss://{DEFAULT_WS_HOST}/connect?ticket={quote(ticket)}",
            "a ticket in the reply, with the host from Ed's region settings")


class SafeSocket:
    """The code-server connection, limited to listing folders and opening files.

    Ed opens a temporary copy of the submission that isn't read-only, so anything that
    could change it is refused before it's sent.
    """

    def __init__(self, ws, timeout: float = WS_TIMEOUT):
        self.ws = ws
        self.timeout = timeout
        self.sent = Counter()
        self.received = Counter()
        self.samples: dict[str, list] = {}

    def send(self, message: dict) -> None:
        kind = message.get("type")
        sub_type = (message.get("data") or {}).get("type") if kind == "fsop" else None
        if (kind, sub_type) not in ALLOWED_SENDS:
            raise ProbeError(f"refusing to send a '{kind}' message to the code server")
        self.ws.send(json.dumps(message))
        self.sent[kind] += 1

    def wait_for(self, matches) -> dict | None:
        """Read messages until one matches; None if nothing matching arrives in time."""
        deadline = time.monotonic() + self.timeout
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                self.ws.settimeout(remaining)
                raw = self.ws.recv()
            except TIMEOUT_ERRORS:
                return None
            except Exception as e:
                raise ProbeError(f"the code-server connection dropped ({type(e).__name__})") from None
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                self.received["(not JSON)"] += 1
                continue
            if not isinstance(message, dict):
                self.received["(not an object)"] += 1
                continue
            kind = str(message.get("type"))
            self.received[kind] += 1
            if len(self.samples.setdefault(kind, [])) < 3:
                self.samples[kind].append(message)
            if matches(message):
                return message
        return None

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


def open_code_server(url: str):
    try:
        import certifi
        import websocket
    except ImportError:
        raise ProbeError("the websocket-client package is missing. Run: pip install -r requirements.txt") from None
    try:
        # Verify the certificate against certifi's CA list, as requests does. Python from
        # python.org on macOS ships without a CA list of its own.
        return websocket.create_connection(url, timeout=WS_TIMEOUT, origin=WS_ORIGIN,
                                           sslopt={"ca_certs": certifi.where()})
    except Exception as e:  # never echo the URL: it contains the ticket
        status = getattr(e, "status_code", None)
        raise ProbeError(f"couldn't open the code-server connection ({type(e).__name__}"
                         + (f", HTTP {status}" if status else "") + ")") from None


def probe_files(ed: Ed, submission_id: int, connect_ws, say, found: dict) -> None:
    """Open one submission the way the browser does, list /home, and open each file.

    Reports counts, line counts, and which field holds the contents. File names, the
    ticket, and the code itself are never printed.
    """
    log("Asking Ed to open the submission on its code server...")
    reply = ed.connect_submission(submission_id)
    found["connected"] = True
    say(shape_of(reply, "connect reply"))
    url, how = find_ws_url(reply)
    parts = urlsplit(url)
    path = parts.path if len(parts.path) <= 30 else "/..."  # a long path might carry the ticket
    say(f"\nCode server: {parts.scheme}://{parts.netloc}{path} (from {how}; ticket not shown)")

    log("Connecting to the code server...")
    sock = SafeSocket(connect_ws(url))
    found["code_server"] = True
    try:
        if sock.wait_for(lambda m: m.get("type") == "init") is None:
            say("No 'init' message arrived.")
        sock.send({"type": "fsop", "data": {"type": "list_folder", "param1": "/home"}})
        listing_msg = sock.wait_for(lambda m: m.get("type") == "list_reply")
        listing = ((listing_msg or {}).get("data") or {}).get("listing")
        if not isinstance(listing, list):
            say("No file listing came back for /home.")
            listing = []
        files = [e for e in listing if isinstance(e, dict) and e.get("type") == "file" and e.get("name")]
        others = len(listing) - len(files)
        found["files_listed"] = len(files)
        say(f"/home: {len(files)} file(s), {others} other entr{'y' if others == 1 else 'ies'}")

        opened = []
        for number, entry in enumerate(files[:MAX_FILES], start=1):
            path = f"/home/{entry['name']}"
            ext = Path(str(entry["name"])).suffix.lower() or "(none)"
            sock.send({"type": "file_open", "data": {"path": path, "soft": False}})
            msg = sock.wait_for(lambda m: m.get("type") == "file_ot_init"
                                and (m.get("data") or {}).get("path") == path)
            if msg is None:
                say(f"  file {number} ({ext}): no contents came back")
                continue
            texts = [(p, v) for p, k, v in walk(msg.get("data"))
                     if isinstance(v, str) and k.lower() not in NOT_CONTENT_KEYS]
            if not texts:
                say(f"  file {number} ({ext}): file_ot_init has no text field besides path/type")
                continue
            field, content = max(texts, key=lambda t: len(t[1]))
            lines = len(content.splitlines())
            opened.append((ext, field, lines))
            say(f"  file {number} ({ext}): {lines} line{'' if lines == 1 else 's'}, {len(content):,} chars, "
                f"in file_ot_init data.{field}")

        found["files_read"] = len(opened)
        found["file_types"] = ", ".join(f"{ext} x{n}" for ext, n in Counter(o[0] for o in opened).items())
        found["max_lines"] = max((o[2] for o in opened), default=0)
        found["content_field"] = Counter(o[1] for o in opened).most_common(1)[0][0] if opened else None

        say("\nMessages received: " + ", ".join(f"{k} x{n}" for k, n in sock.received.items()))
        say("Messages sent: " + ", ".join(f"{k} x{n}" for k, n in sock.sent.items()))
        for kind in ("init", "list_reply", "file_ot_init"):
            if sock.samples.get(kind):
                say(shape_of_all(sock.samples[kind], kind, show_file_names=False))
    finally:
        sock.close()


# ---------- the probe

def unwrap(data, *keys):
    """Ed wraps payloads ({"lesson": {...}}); return the first matching key, or the data itself."""
    if isinstance(data, dict):
        for k in keys:
            if k in data:
                return data[k]
    return data


def run(ed: Ed, lesson_id: int, slide_no: int | None = None, challenge_id: int | None = None,
        email: str | None = None, save: bool = False, files: bool = False, connect_ws=None,
        out=sys.stdout) -> dict:
    found: dict = {}

    def say(text: str = "") -> None:
        print(text, file=out, flush=True)

    def section(title: str) -> None:
        say(f"\n=== {title} ===")

    say("Ed code-submission probe. Field names, types, sizes and scores only.")
    say("No names, emails, IDs, URLs or code. Safe to paste back to Claude.")

    # 1. Lesson and slides
    section(f"Lesson {lesson_id}: slides")
    log("Fetching the lesson...")
    lesson = unwrap(ed.get_json(f"/lessons/{int(lesson_id)}"), "lesson")
    slides = lesson.get("slides") if isinstance(lesson, dict) else None
    if not isinstance(slides, list):
        say("The lesson response has no 'slides' list. Lesson shape:")
        say(shape_of(lesson, "lesson"))
        return found
    slides = sorted(slides, key=lambda s: s.get("index") if isinstance(s.get("index"), (int, float)) else 0)

    challenge_field = None
    rows = []
    for number, slide in enumerate(slides, start=1):
        hit = find_challenge_id(slide)
        if hit:
            challenge_field = challenge_field or hit[0]
        rows.append((number, slide, hit[1] if hit else None))
    say(f"{'#':>3}  {'type':<10} {'pts':>4}  {'challenge':>9}  title")
    for number, slide, cid in rows:
        say(f"{number:>3}  {str(slide.get('type', '?'))[:10]:<10} {str(slide.get('points', '')):>4}  "
            f"{cid if cid else '-':>9}  {str(slide.get('title', ''))[:60]}")

    def code_like(slide: dict) -> bool:
        kind = str(slide.get("type") or "").lower()
        return kind in CODE_SLIDE_TYPES or (not kind and "code" in str(slide.get("title", "")).lower())

    code_slides = [r for r in rows if code_like(r[1])]
    found["code_slides"] = len(code_slides)
    found["challenge_field"] = challenge_field
    say(f"\nCode slides: {len(code_slides)}. Challenge ID field in the lesson listing: "
        f"{challenge_field or 'not found'}")
    if code_slides:
        merged = Shape()
        for _, slide, _ in code_slides:
            merged.add(slide)
        say("\nCode slide fields (merged across code slides):")
        say("\n".join(merged.render("slide")))

    # 2. Pick the slide and its challenge
    target_slide = None
    if challenge_id is None:
        if slide_no is not None:
            target = next((r for r in rows if r[0] == slide_no), None)
            if target is None:
                say(f"\nThere's no slide {slide_no}.")
                return found
        else:
            modify = lambda r: "modify" in str(r[1].get("title", "")).lower()
            target = next((r for r in rows if r[2] and modify(r)), None) \
                or next((r for r in rows if r[2]), None) \
                or next((r for r in code_slides if modify(r)), None) \
                or (code_slides[0] if code_slides else None)
        if target is None:
            say("\nThis lesson has no code slides.")
            return found
        target_slide, _, challenge_id = target
        if challenge_id is None:
            log(f"Looking for slide {target_slide}'s challenge ID in its details...")
            try:
                detail = unwrap(ed.get_json(f"/lessons/slides/{int(target[1]['id'])}"), "slide")
                section(f"Slide {target_slide} details")
                say(shape_of(detail, "slide"))
                if hit := find_challenge_id(detail):
                    challenge_id = hit[1]
                    found["challenge_field"] = f"(slide details) {hit[0]}"
            except (ProbeError, KeyError, TypeError, ValueError) as e:
                say(f"\nSlide {target_slide} details: failed ({e})")
        if challenge_id is None:
            say(f"\nNo challenge ID found for slide {target_slide}, so submissions can't be fetched.")
            say("Re-run with --challenge ID if you know it (it may be in the URL when you open the "
                "slide's submissions in Ed).")
            return found
    found["challenge_id"] = challenge_id
    section(f"Challenge {challenge_id}" + (f" (slide {target_slide})" if target_slide else ""))

    challenge = None
    try:
        log("Fetching the challenge...")
        challenge = unwrap(ed.get_json(f"/challenges/{int(challenge_id)}"), "challenge")
        say(shape_of(challenge, "challenge"))
        found["starter_files"] = len(code_candidates(challenge))
    except ProbeError as e:
        say(f"GET /challenges/{challenge_id}: failed ({e})")
    starter = {normalize_code(text) for _, text in code_candidates(challenge or {})}

    rubric_id = challenge.get("rubric_id") if isinstance(challenge, dict) else None
    if isinstance(rubric_id, int):
        section("Rubric")
        try:
            log("Fetching the rubric...")
            say(shape_of(ed.get_json(f"/rubrics/{rubric_id}"), "response"))
            found["rubric"] = True
        except ProbeError as e:
            say(f"GET /rubrics/<id>: failed ({e})")
            found["rubric"] = False

    # 3. Who's on the challenge
    section("Challenge users")
    users = []
    try:
        log("Fetching the challenge's users...")
        raw = unwrap(ed.get_json(f"/challenges/{int(challenge_id)}/users"), "users")
        users = raw if isinstance(raw, list) else []
        say(f"{len(users)} users")
        if users:
            say(shape_of(users, "users"))
    except ProbeError as e:
        say(f"GET /challenges/{challenge_id}/users: failed ({e})")
    found["users"] = len(users)

    def flat(user: dict) -> dict:
        return {**user, **user["user"]} if isinstance(user.get("user"), dict) else user

    def is_student(user: dict) -> bool:
        role = next((user.get(k) for k in ("role", "course_role") if user.get(k)), None)
        return role is None or "student" in str(role).lower()

    people = [flat(u) for u in users if isinstance(u, dict)]
    students = [u for u in people if is_student(u)]
    if email:
        chosen = [u for u in people if str(u.get("email", "")).strip().lower() == email.strip().lower()]
        if chosen:
            students = chosen
        else:
            say("That email isn't in the challenge's user list (or emails aren't included). Sampling others.")

    # 4. One student's submissions
    section("One student's submissions")
    subs = []
    tried = 0
    for user in students[:MAX_USERS_TO_TRY]:
        uid = user.get("user_id") or user.get("id")
        if not isinstance(uid, int):
            continue
        tried += 1
        log(f"Checking student {tried} for submissions...")
        try:
            raw = unwrap(ed.get_json(f"/users/{uid}/challenges/{int(challenge_id)}/submissions"), "submissions")
        except ProbeError as e:
            say(f"GET /users/<id>/challenges/{challenge_id}/submissions: failed ({e})")
            break
        if isinstance(raw, list) and raw:
            subs = raw
            break
    found["students_checked"] = tried
    found["submissions"] = len(subs)
    if not subs:
        say(f"No submissions found ({tried} students checked). The lesson may not have been attempted yet; "
            "try a lesson students have done, or --email for a student who submitted.")
    else:
        say(f"Found a student with {len(subs)} submission(s) after checking {tried}.")
        say(shape_of(subs, "submissions"))

    # 5. One submission in detail
    detail = None
    sample, sid = None, None
    if subs:
        section("One submission in detail")
        sample = next((s for s in subs if code_candidates(s)), subs[0])
        sid = sample.get("id") if isinstance(sample, dict) else None
        if isinstance(sid, int):
            try:
                log("Fetching one submission's details...")
                detail = unwrap(ed.get_json(f"/challenges/submissions/{sid}"), "submission")
                say(shape_of(detail, "submission"))
                found["detail_endpoint"] = True
            except ProbeError as e:
                say(f"GET /challenges/submissions/<id>: failed ({e})")
                found["detail_endpoint"] = False
        else:
            say("Submissions have no numeric 'id', so the detail endpoint wasn't tried.")

        code = code_candidates({"submissions": subs, "detail": detail})
        if code:
            by_path: dict[str, list[str]] = {}
            for path, text in code:
                by_path.setdefault(path, []).append(text)
            say("\nMulti-line text (likely code or test output):")
            for path, texts in by_path.items():
                lines = max(len(t.splitlines()) for t in texts)
                if starter:
                    same = sum(normalize_code(t) in starter for t in texts)
                    vs = f"{same} of {len(texts)} identical to a starter file" if same else "differs from the starter files"
                else:
                    vs = "no starter files to compare"
                say(f"  {path}: up to {lines} lines; {vs}")
            found["code_paths"] = list(by_path)
            found["code_differs"] = any(normalize_code(t) not in starter for _, t in code) if starter else None
        else:
            say("\nNo multi-line text in the submission responses, so no code found there.")
            found["code_paths"] = []
        found["mark_paths"] = mark_paths({"submissions": subs, "detail": detail})

        if save:
            SAVE_DIR.mkdir(mode=0o700, exist_ok=True)
            for name, data in ((f"probe_submission_{challenge_id}.json", {"submissions": subs, "detail": detail}),
                               (f"probe_challenge_{challenge_id}.json", challenge)):
                path = SAVE_DIR / name
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
            log(f"Saved raw data to {SAVE_DIR}/. It's student data: for your eyes only. "
                "Don't paste or commit it, and delete it when you're done.")

    # 6. That submission's marking record (where the rubric lives)
    if isinstance(sample, dict):
        section("Marking record (rubric)")
        mark_id = sample.get("lesson_mark_id")
        if isinstance(mark_id, int):
            try:
                log("Fetching the marking record...")
                # The whole response: rubric items may sit next to the lesson_mark object.
                say(shape_of(ed.get_json(f"/lesson_marks/{mark_id}", {"rubric_items": "true"}), "response"))
                found["rubric_record"] = True
                say(shape_of(ed.get_json(f"/rubrics/selected/{mark_id}"), "selected"))
                found["rubric_selected"] = True
            except ProbeError as e:
                say(f"GET /lesson_marks/<id>?rubric_items=true: failed ({e})")
                found["rubric_record"] = False
        else:
            say("The sampled submission has no lesson_mark_id.")

    # 7. Its files, through Ed's code server (only with --files)
    if files and isinstance(sid, int):
        section("Student files (code server)")
        try:
            probe_files(ed, sid, connect_ws or open_code_server, say, found)
        except ProbeError as e:
            say(f"Code server: failed ({e})")

    # 8. Bulk download (Ed's "Download submissions")
    section("Bulk submissions download")
    try:
        log("Requesting the bulk submissions download...")
        resp = ed.download(f"/challenges/{int(challenge_id)}/submissions",
                           {"students": "1", "type": "optimised", "tz": TZ})
        body = resp.content or b""
        ctype = resp.headers.get("Content-Type", "?")
        say(f"Content-Type: {ctype}; {len(body):,} bytes")
        if body[:4] == b"PK\x03\x04":
            z = zip_shapes(body)
            say(f"Zip with {z['files']} files under {z['top_level']} top-level entries; "
                f"file sizes {z['bytes'][0]:,}-{z['bytes'][1]:,} bytes")
            say("Extensions: " + ", ".join(f"{ext} x{n}" for ext, n in z["extensions"]))
            say("Layout (student-specific names replaced):")
            for shape, n in z["shapes"]:
                say(f"  {shape}  x{n}")
            found["bulk"] = f"zip, {z['files']} files"
        else:
            try:
                payload = resp.json()
                say(shape_of(payload, "download"))
                found["bulk"] = "JSON"
            except ValueError:
                header, count = csv_header(body.decode("utf-8-sig", errors="replace"))
                say(f"Not zip or JSON. First row: {header[0] if header else '(empty)'}; {count} more rows")
                found["bulk"] = ctype
    except ProbeError as e:
        say(f"POST /challenges/{challenge_id}/submissions: failed ({e})")
        found["bulk"] = None
    except zipfile.BadZipFile:
        say("Looked like a zip but couldn't be read.")
        found["bulk"] = "unreadable zip"

    # 9. Results export with feedback columns
    section("Challenge results export (with feedback columns)")
    found["results_columns"] = None
    params = {"students": "1", "feedback": "1", "type": "optimised", "score_type": "pertestcase", "tz": TZ}
    for suffix in ("results", "results.csv"):
        try:
            log(f"Requesting the challenge {suffix} export...")
            resp = ed.download(f"/challenges/{int(challenge_id)}/{suffix}", params)
        except ProbeError as e:
            say(f"POST /challenges/{challenge_id}/{suffix}: failed ({e})")
            continue
        header, count = csv_header(resp.content.decode("utf-8-sig", errors="replace"))
        if header:
            say(f"/{suffix}: {len(header[0])} columns, {count} data rows")
            for i, row in enumerate(header, start=1):
                say(f"  header row {i}: {row}")
            found["results_columns"] = header[0]
        else:
            say(f"/{suffix}: empty response")
        break

    # 10. Summary
    section("Summary")
    say(f"Code slides with a challenge ID: {sum(1 for r in rows if r[2] and code_like(r[1]))} of "
        f"{found['code_slides']} (field: {found.get('challenge_field') or 'not found'})")
    say(f"Starter files in the challenge:  {found.get('starter_files', 'unknown')}")
    if found.get("submissions"):
        say(f"Per-student submissions:        yes (sampled student had {found['submissions']})")
        if found.get("code_paths"):
            differs = {True: "differs from the starter code", False: "same as the starter code",
                       None: "couldn't compare with starter code"}[found.get("code_differs")]
            say(f"Student code in submissions:    yes, at {', '.join(found['code_paths'][:3])} ({differs})")
        else:
            say("Student code in submissions:    not found")
        if "detail_endpoint" in found:
            say(f"Submission detail endpoint:     {'works' if found['detail_endpoint'] else 'failed'}")
        say(f"Fields that look like marking:  {', '.join(found.get('mark_paths', [])[:12]) or 'none'}")
    else:
        say(f"Per-student submissions:        none found ({found.get('students_checked', 0)} students checked)")
    status = lambda key: "works" if found.get(key) else "failed" if found.get(key) is False else "not tried"
    say(f"Rubric definition (rubrics):    {status('rubric')}")
    say(f"Rubric record (lesson_marks):   {status('rubric_record')}"
        + (f"; selected items: {status('rubric_selected')}" if "rubric_selected" in found else ""))
    if not files:
        say("Code server:                    not tried (run with --files)")
    elif found.get("files_read"):
        say(f"Code server:                    works; read {found['files_read']} of {found['files_listed']} file(s) "
            f"({found['file_types']}), up to {found['max_lines']} lines, in file_ot_init data.{found['content_field']}")
    elif found.get("code_server"):
        say("Code server:                    connected, but no file contents came back")
    else:
        say("Code server:                    failed")
    say(f"Bulk download:                  {found.get('bulk') or 'failed'}")
    say(f"Results export:                 {'works' if found['results_columns'] else 'failed'}")
    opened = ("Opened 1 temporary copy of a submission on Ed's code server, as viewing it in Ed does. "
              if found.get("connected") else "")
    say(f"Requests made: {sum(ed.calls.values())} ("
        + ", ".join(f"{m} {n}" for m, n in sorted(ed.calls.items())) + f"). {opened}Nothing was written to Ed.")
    return found


def get_token(profile: str | None) -> str:
    env = os.environ.get("ED_API_TOKEN")
    if env and not profile:
        log("Using ED_API_TOKEN.")
        return env
    try:
        from ezgrader.keystore import KeyStore
        store = KeyStore()
        if profile:
            token = store.get_secret(profile, "ed_token")
            if not token:
                sys.exit(f"EzGrader profile '{profile}' has no Ed token.")
            return token
        with_ed = [p["name"] for p in store.list_profiles() if p.get("ed_token")]
    except Exception as e:  # keyring missing or Keychain unavailable
        sys.exit(f"Set ED_API_TOKEN, or run inside the EzGrader venv to use a saved profile ({type(e).__name__}).")
    if len(with_ed) == 1:
        log(f"Using the Ed token from EzGrader profile '{with_ed[0]}'.")
        return store.get_secret(with_ed[0], "ed_token")
    if with_ed:
        sys.exit(f"Several EzGrader profiles have Ed tokens ({', '.join(with_ed)}). Pick one with --profile NAME.")
    sys.exit("No Ed token. Set ED_API_TOKEN or save one in EzGrader (Keys).")


def main() -> None:
    p = argparse.ArgumentParser(description="Read-only probe of Ed code-slide submissions")
    p.add_argument("lesson_id", type=int)
    p.add_argument("--slide", type=int, help="slide number to probe (as in the results CSV)")
    p.add_argument("--challenge", type=int, help="probe this challenge ID directly")
    p.add_argument("--email", help="sample this student (e.g. a test student account)")
    p.add_argument("--profile", help="EzGrader profile whose Ed token to use")
    p.add_argument("--save", action="store_true", help="also save one raw submission to ed_exports/ (yours only)")
    p.add_argument("--files", action="store_true",
                   help="also open one submission on Ed's code server and read its files (counts only)")
    args = p.parse_args()

    ed = Ed(get_token(args.profile))
    try:
        run(ed, args.lesson_id, args.slide, args.challenge, args.email, args.save, files=args.files)
    except ProbeError as e:
        sys.exit(f"Stopped: {e}")
    log("\nDone. Paste the report (everything printed to stdout) back to Claude.")


if __name__ == "__main__":
    main()
