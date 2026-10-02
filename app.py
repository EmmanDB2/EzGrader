#!/usr/bin/env python3
"""Start EzGrader on 127.0.0.1 and open it in the browser.

    python app.py              # default port 8765, or a free one if that's taken
    python app.py --port 9000
    python app.py --no-browser
    python app.py --demo       # try the UI on fake data; never touches Ed, Canvas, or the Keychain
"""

import argparse
import logging
import socket
import sys
import threading
import webbrowser

if sys.version_info < (3, 10):
    sys.exit("EzGrader needs Python 3.10 or newer.")

from werkzeug.serving import make_server

from ezgrader.server import create_app

HOST = "127.0.0.1"  # local only: never bind to 0.0.0.0


def pick_port(preferred: int) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((HOST, preferred))
            return preferred
        except OSError:
            s.bind((HOST, 0))
            return s.getsockname()[1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Ed → Canvas grade sync (local only)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    parser.add_argument("--verbose", action="store_true", help="log every request (paths only, never tokens)")
    parser.add_argument("--demo", action="store_true", help="fake Ed/Canvas data; nothing real is touched")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("werkzeug").setLevel(logging.INFO if args.verbose else logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    port = pick_port(args.port)
    extra = {}
    if args.demo:
        from ezgrader.demo import demo_app_kwargs
        extra = demo_app_kwargs()
    app = create_app(allowed_hosts={f"{HOST}:{port}", f"localhost:{port}"}, **extra)
    server = make_server(HOST, port, app, threaded=True)

    url = f"http://{HOST}:{port}/"
    mode = "  DEMO MODE: fake data, nothing real is touched.\n" if args.demo else ""
    print(f"\n  EzGrader is running at {url}\n{mode}  Press Ctrl+C to stop.\n", flush=True)
    if not args.no_browser:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
