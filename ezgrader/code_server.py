"""Read a submission's files from Ed's code server, the way Ed's site does.

POST /challenges/submissions/{id}/connect returns a one-time ticket, and a websocket to
wss://sahara.<region>.edstem.org/connect?ticket=... then serves the workspace. Ed opens a
temporary copy of the submission that isn't read-only, so this module can only ever send
"list a folder" and "open a file"; anything else is refused before it leaves the machine.

File contents are returned to the caller and never written to disk.
"""

from __future__ import annotations

import json
import socket
import time
from urllib.parse import quote

from .ed_client import ED_WS_BASE, EdClient

TIMEOUT = 15
MAX_FILES = 25
MAX_FOLDERS = 6
MAX_FILE_CHARS = 200_000
ROOT = "/home"
WS_ORIGIN = "https://edstem.org"  # the browser's Origin; the ticket is the actual credential
ALLOWED_SENDS = {("fsop", "list_folder"), ("file_open", None)}
FOLDER_TYPES = {"folder", "dir", "directory"}
# Not worth opening (and the code server may never answer for them).
BINARY_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".pdf", ".zip", ".gz", ".tar",
                     ".jar", ".class", ".pyc", ".so", ".o", ".exe", ".dll", ".mp3", ".mp4", ".wav"}


class CodeServerError(Exception):
    pass


def _timeout_errors() -> tuple:
    errors = [TimeoutError, socket.timeout]
    try:
        import websocket
        errors.append(websocket.WebSocketTimeoutException)
    except ImportError:
        pass
    return tuple(errors)


TIMEOUT_ERRORS = _timeout_errors()


class SafeSocket:
    """A code-server connection that can only list folders and open files."""

    def __init__(self, ws, timeout: float = TIMEOUT):
        self.ws = ws
        self.timeout = timeout

    def send(self, message: dict) -> None:
        kind = message.get("type")
        sub_type = (message.get("data") or {}).get("type") if kind == "fsop" else None
        if (kind, sub_type) not in ALLOWED_SENDS:
            raise CodeServerError(f"Refusing to send a '{kind}' message to Ed's code server.")
        self.ws.send(json.dumps(message))

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
                raise CodeServerError(f"Ed's code server closed the connection ({type(e).__name__}).") from None
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(message, dict) and matches(message):
                return message
        return None

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


def open_socket(url: str):
    try:
        import certifi
        import websocket
    except ImportError:
        raise CodeServerError("The websocket-client package is missing. Run: pip install -r requirements.txt") from None
    try:
        # Verify TLS against certifi's CA list, as requests does: python.org Python on
        # macOS has no CA list of its own.
        return websocket.create_connection(url, timeout=TIMEOUT, origin=WS_ORIGIN,
                                           sslopt={"ca_certs": certifi.where()})
    except Exception as e:  # never echo the URL: it contains the ticket
        status = getattr(e, "status_code", None)
        raise CodeServerError(f"Couldn't connect to Ed's code server ({type(e).__name__}"
                              + (f", HTTP {status}" if status else "") + ").") from None


def read_submission_files(ed: EdClient, submission_id: int, connect=open_socket) -> list[dict]:
    """[{"path": "fruit_catalog.py", "content": "...", "lines": 10}, ...] for one submission."""
    ticket = ed.connect_submission(submission_id)
    sock = SafeSocket(connect(f"{ED_WS_BASE}/connect?ticket={quote(ticket)}"))
    try:
        if sock.wait_for(lambda m: m.get("type") == "init") is None:
            raise CodeServerError("Ed's code server didn't respond. Try again in a moment.")

        files: list[dict] = []
        folders, listed = [ROOT], 0
        while folders and listed < MAX_FOLDERS and len(files) < MAX_FILES:
            folder = folders.pop(0)
            listed += 1
            sock.send({"type": "fsop", "data": {"type": "list_folder", "param1": folder}})
            reply = sock.wait_for(lambda m: m.get("type") == "list_reply"
                                  and (m.get("data") or {}).get("dir", folder) == folder)
            listing = ((reply or {}).get("data") or {}).get("listing")
            for entry in sorted(listing if isinstance(listing, list) else [], key=lambda e: str(e.get("name"))):
                name = str(entry.get("name") or "")
                if not name or name.startswith("."):
                    continue
                path = f"{folder}/{name}"
                kind = str(entry.get("type") or "").lower()
                if kind == "file":
                    if not any(name.lower().endswith(ext) for ext in BINARY_EXTENSIONS):
                        files.append({"path": path})
                elif kind in FOLDER_TYPES:
                    folders.append(path)

        out = []
        for f in files[:MAX_FILES]:
            sock.send({"type": "file_open", "data": {"path": f["path"], "soft": False}})
            msg = sock.wait_for(lambda m: m.get("type") == "file_ot_init"
                                and (m.get("data") or {}).get("path") == f["path"])
            content = ((msg or {}).get("data") or {}).get("buffer")
            rel = f["path"][len(ROOT) + 1:]
            if not isinstance(content, str):
                out.append({"path": rel, "content": None, "lines": 0, "error": "Couldn't read this file."})
                continue
            truncated = len(content) > MAX_FILE_CHARS
            content = content[:MAX_FILE_CHARS]
            out.append({"path": rel, "content": content, "lines": len(content.splitlines()),
                        **({"error": "Only the first part of this file is shown."} if truncated else {})})
        return out
    finally:
        sock.close()
