import json
import sys
import threading
import time
from pathlib import Path

import keyring
import pytest
from werkzeug.serving import make_server

import app as launcher
from ezgrader import __version__, paths
from ezgrader.keystore import KeyStore, configure_keyring
from ezgrader.server import Activity, create_app

TOKEN = "test-session-token"


def make_app(on_quit=None, host="127.0.0.1:8765"):
    return create_app(allowed_hosts={host}, keystore=KeyStore(), session_token=TOKEN, on_quit=on_quit)


# ---------- paths

def test_paths_from_source():
    assert not paths.frozen()
    assert paths.resource_dir() == paths.PROJECT_ROOT
    assert paths.data_dir() == paths.PROJECT_ROOT
    assert (paths.resource_dir() / "static" / "index.html").exists()


def test_paths_when_packaged(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "bundle"), raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    assert paths.resource_dir() == tmp_path / "bundle"
    assert paths.data_dir() == tmp_path / "home" / "Library" / "Application Support" / "EzGrader"


def test_configure_keyring(monkeypatch):
    before = keyring.get_keyring()
    configure_keyring()  # from source: leaves keyring alone
    assert keyring.get_keyring() is before
    if sys.platform == "darwin":
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        configure_keyring()
        assert type(keyring.get_keyring()).__module__ == "keyring.backends.macOS"


# ---------- lifecycle endpoints

def call(client, method, path, token=TOKEN):
    headers = {"X-EzGrader-Session": token} if token else {}
    return client.open(path, method=method, headers=headers, base_url="http://127.0.0.1:8765")


def test_health_is_public_but_host_checked():
    client = make_app().test_client()
    assert call(client, "GET", "/health", token=None).get_json() == {"app": "EzGrader", "version": __version__}
    resp = client.get("/health", base_url="http://evil.example.com:8765")
    assert resp.status_code == 403


def test_ping_needs_the_session_and_records_activity():
    app = make_app()
    client = app.test_client()
    activity = app.config["ACTIVITY"]
    assert not activity.ever_seen
    assert call(client, "POST", "/api/ping", token="wrong").status_code == 403
    assert not activity.ever_seen
    assert call(client, "POST", "/api/ping").get_json() == {"ok": True}
    assert activity.ever_seen and activity.idle_seconds() < 1


def test_quit_calls_back_unless_busy():
    quit_called = threading.Event()
    app = make_app(on_quit=quit_called.set)
    client = app.test_client()
    job_id = app.config["JOBS"].reserve("push")  # something is running
    resp = call(client, "POST", "/api/quit")
    assert resp.status_code == 409 and "still running" in resp.get_json()["error"]
    app.config["JOBS"].cancel(job_id)
    assert call(client, "POST", "/api/quit").get_json() == {"ok": True}
    assert quit_called.wait(2)


def test_quit_without_a_handler():
    resp = call(make_app().test_client(), "POST", "/api/quit")
    assert resp.status_code == 400 and "Ctrl+C" in resp.get_json()["error"]


def test_activity_clock():
    activity = Activity()
    assert not activity.ever_seen and activity.idle_seconds() >= 0
    activity.touch()
    assert activity.ever_seen and activity.idle_seconds() < 0.5


# ---------- one running copy, and quitting when idle

@pytest.fixture
def live_server():
    port = launcher.pick_port(0)
    app = create_app(allowed_hosts={f"127.0.0.1:{port}"}, keystore=KeyStore(), session_token=TOKEN)
    server = make_server("127.0.0.1", port, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield app, server, port
    server.shutdown()
    thread.join(2)


def test_running_instance_found_and_forgotten(monkeypatch, tmp_path, live_server):
    _, _, port = live_server
    monkeypatch.setattr(launcher, "data_dir", lambda: tmp_path)
    assert launcher.running_instance() is None  # no note yet
    launcher.record_instance(port)
    assert json.loads((tmp_path / "running.json").read_text())["port"] == port
    assert launcher.running_instance() == f"http://127.0.0.1:{port}/"
    launcher.forget_instance()
    assert not (tmp_path / "running.json").exists()


def test_stale_note_is_ignored(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "data_dir", lambda: tmp_path)
    (tmp_path / "running.json").write_text(json.dumps({"pid": 1, "port": launcher.pick_port(0)}))
    assert launcher.running_instance() is None  # nothing answers on that port


def test_watch_idle_quits_after_the_last_page(live_server):
    app, server, _ = live_server
    app.config["ACTIVITY"].touch()
    stopped = threading.Event()
    real_shutdown = server.shutdown

    def shutdown():
        stopped.set()
        real_shutdown()

    server.shutdown = shutdown
    threading.Thread(target=launcher.watch_idle, args=(app, server, 0.2, 5, 0.05), daemon=True).start()
    assert stopped.wait(3)


def test_watch_idle_waits_for_a_running_push(live_server):
    app, server, _ = live_server
    app.config["ACTIVITY"].touch()
    job_id = app.config["JOBS"].reserve("push")
    stopped = threading.Event()
    server.shutdown = stopped.set
    threading.Thread(target=launcher.watch_idle, args=(app, server, 0.1, 5, 0.05), daemon=True).start()
    assert not stopped.wait(0.5)  # busy: keeps running
    app.config["JOBS"].cancel(job_id)
    assert stopped.wait(2)
