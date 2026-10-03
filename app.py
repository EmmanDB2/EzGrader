#!/usr/bin/env python3
"""Start EzGrader on 127.0.0.1 and open it in the browser.

    python app.py              # default port 8765, or a free one if that's taken
    python app.py --port 9000
    python app.py --no-browser
    python app.py --demo       # try the UI on fake data; never touches Ed, Canvas, or the Keychain

The packaged app (EzGrader.app) runs this same file in "app mode": opening it again
reopens the running copy instead of starting a second one, and it quits by itself a
few minutes after its last browser tab closes.
"""

import argparse
import json
import logging
import logging.handlers
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

if sys.version_info < (3, 10):
    sys.exit("EzGrader needs Python 3.10 or newer.")

from werkzeug.serving import make_server

from ezgrader.keystore import configure_keyring
from ezgrader.paths import data_dir, frozen
from ezgrader.server import create_app

HOST = "127.0.0.1"  # local only: never bind to 0.0.0.0
DEFAULT_PORT = 8765
IDLE_LIMIT = 180          # app mode: quit this long after the last page stops checking in
FIRST_PAGE_LIMIT = 600    # ...or this long after starting if no page ever opens

log = logging.getLogger("ezgrader")


def say(message: str) -> None:
    """Print when there's a terminal; the packaged app has none."""
    if sys.stdout:
        print(message, flush=True)


def pick_port(preferred: int) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((HOST, preferred))
            return s.getsockname()[1]  # the real port, even when asked for 0
        except OSError:
            s.bind((HOST, 0))
            return s.getsockname()[1]


# ---------- app mode: one running copy per user

def instance_file():
    return data_dir() / "running.json"


def running_instance() -> str | None:
    """The URL of an EzGrader this user already has running, if any."""
    try:
        port = int(json.loads(instance_file().read_text())["port"])
        url = f"http://{HOST}:{port}/"
        with urllib.request.urlopen(url + "health", timeout=2) as resp:
            if json.load(resp).get("app") == "EzGrader":
                return url
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def record_instance(port: int) -> None:
    path = instance_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": os.getpid(), "port": port}))


def forget_instance() -> None:
    try:
        if json.loads(instance_file().read_text()).get("pid") == os.getpid():
            instance_file().unlink()
    except (OSError, ValueError):
        pass


def watch_idle(app, server, idle_limit: float = IDLE_LIMIT, first_page_limit: float = FIRST_PAGE_LIMIT,
               check_every: float = 10) -> None:
    """App mode: stop once no page has checked in for a while. Never mid-push."""
    activity, jobs = app.config["ACTIVITY"], app.config["JOBS"]
    while True:
        time.sleep(check_every)
        limit = idle_limit if activity.ever_seen else first_page_limit
        if activity.idle_seconds() > limit and not jobs.busy():
            log.info("No EzGrader page has been open for a while, so EzGrader is quitting.")
            server.shutdown()
            return


def configure_logging(app_mode: bool, verbose: bool) -> None:
    if app_mode:
        folder = data_dir()
        folder.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(folder / "ezgrader.log", maxBytes=1_000_000,
                                                       backupCount=2, encoding="utf-8")
        logging.basicConfig(level=logging.INFO, handlers=[handler],
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    else:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("werkzeug").setLevel(logging.INFO if verbose else logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ed → Canvas grade sync (local only)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    parser.add_argument("--verbose", action="store_true", help="log every request (paths only, never tokens)")
    parser.add_argument("--demo", action="store_true", help="fake Ed/Canvas data; nothing real is touched")
    parser.add_argument("--app", action="store_true", help="behave like the packaged app (one copy, quit when idle)")
    args, _ = parser.parse_known_args()  # macOS can add its own arguments when it launches an app
    app_mode = args.app or frozen()

    configure_logging(app_mode, args.verbose)
    configure_keyring()

    if app_mode and not args.demo:
        url = running_instance()
        if url:
            log.info("EzGrader is already running at %s; reopening it.", url)
            if not args.no_browser:
                webbrowser.open(url)
            return

    port = pick_port(args.port)
    extra = {}
    if args.demo:
        from ezgrader.demo import demo_app_kwargs
        extra = demo_app_kwargs()

    def quit_server() -> None:
        log.info("Quit from the page.")
        server.shutdown()

    app = create_app(allowed_hosts={f"{HOST}:{port}", f"localhost:{port}"}, on_quit=quit_server, **extra)
    server = make_server(HOST, port, app, threaded=True)

    url = f"http://{HOST}:{port}/"
    mode = "  DEMO MODE: fake data, nothing real is touched.\n" if args.demo else ""
    say(f"\n  EzGrader is running at {url}\n{mode}  Press Ctrl+C to stop.\n")
    log.info("EzGrader %s started on %s", "(app mode)" if app_mode else "", url)
    if app_mode:
        if not args.demo:
            record_instance(port)
        threading.Thread(target=watch_idle, args=(app, server), daemon=True).start()
    if not args.no_browser:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        say("\nStopped.")
    finally:
        if app_mode and not args.demo:
            forget_instance()
        server.server_close()
        log.info("EzGrader stopped.")


if __name__ == "__main__":
    main()
