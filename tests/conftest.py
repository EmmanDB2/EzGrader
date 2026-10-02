import sys
from pathlib import Path

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT))


class MemoryKeyring(KeyringBackend):
    """In-memory stand-in so tests never touch the real Keychain."""
    priority = 1

    def __init__(self):
        super().__init__()
        self.items = {}

    def get_password(self, service, username):
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self.items:
            raise PasswordDeleteError("missing")
        del self.items[(service, username)]


@pytest.fixture(autouse=True)
def memory_keyring():
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


@pytest.fixture
def homework_csv():
    return (FIXTURES / "homework_with_results.csv").read_text(encoding="utf-8")


@pytest.fixture
def lab_csv():
    return (FIXTURES / "lab_without_results.csv").read_text(encoding="utf-8")


class FakeResponse:
    def __init__(self, status=200, json_data=None, text=None, headers=None, links=None, content=None):
        self.status_code = status
        self._json = json_data
        self.text = text if text is not None else ("" if json_data is None else "json")
        self.headers = headers or {}
        self.links = links or {}
        self.content = content if content is not None else self.text.encode()

    @property
    def ok(self):
        return 200 <= self.status_code < 400

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeSession:
    """Records requests; answers from a list of (method, url_prefix, response) rules."""

    def __init__(self, rules=None):
        self.rules = list(rules or [])
        self.calls = []

    def request(self, method, url, params=None, data=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params, "data": data, "headers": headers})
        for i, (m, prefix, resp) in enumerate(self.rules):
            if m == method and url.startswith(prefix):
                if isinstance(resp, list):  # a sequence of responses, consumed in order
                    return resp.pop(0) if len(resp) > 1 else resp[0]
                return resp
        return FakeResponse(404, {"errors": [{"message": "not found"}]})
