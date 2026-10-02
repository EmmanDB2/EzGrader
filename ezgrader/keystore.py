"""Profiles and API tokens, kept in the macOS Keychain via `keyring`.

Nothing here touches the filesystem. Keychain items (service "EzGrader"):
  __profiles__            JSON index: profile names and their Canvas base URLs (not secret)
  <profile>::ed_token     Ed API token
  <profile>::canvas_token Canvas API token

Tokens never leave this module except to build API clients. The frontend only
ever sees `public_info`, which masks them to ••••last4.
"""

from __future__ import annotations

import json
import re
import threading

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

SERVICE = "EzGrader"
INDEX_ACCOUNT = "__profiles__"
SECRET_KINDS = ("ed_token", "canvas_token")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,39}$")


class ProfileError(ValueError):
    pass


def mask(secret: str | None) -> str | None:
    if not secret:
        return None
    return "••••" + secret[-4:]


class KeyStore:
    def __init__(self, service: str = SERVICE):
        self.service = service
        self._lock = threading.RLock()

    # ---------- keychain access

    def _get(self, account: str) -> str | None:
        try:
            return keyring.get_password(self.service, account)
        except KeyringError as e:
            raise ProfileError(f"Couldn't read the Keychain: {type(e).__name__}") from None

    def _set(self, account: str, value: str) -> None:
        try:
            keyring.set_password(self.service, account, value)
        except KeyringError as e:
            raise ProfileError(f"Couldn't write to the Keychain: {type(e).__name__}") from None

    def _delete(self, account: str) -> None:
        try:
            keyring.delete_password(self.service, account)
        except PasswordDeleteError:
            pass

    def _index(self) -> dict:
        raw = self._get(INDEX_ACCOUNT)
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}
        profiles = data.get("profiles") if isinstance(data, dict) else None
        return profiles if isinstance(profiles, dict) else {}

    def _save_index(self, profiles: dict) -> None:
        self._set(INDEX_ACCOUNT, json.dumps({"version": 1, "profiles": profiles}))

    @staticmethod
    def _account(name: str, kind: str) -> str:
        return f"{name}::{kind}"

    def _require(self, name: str) -> dict:
        index = self._index()
        if name not in index:
            raise ProfileError(f"There's no profile named '{name}'.")
        return index

    # ---------- public API

    def list_profiles(self) -> list[dict]:
        with self._lock:
            index = self._index()
            return [self.public_info(n, index) for n in sorted(index, key=str.lower)]

    def exists(self, name: str) -> bool:
        return name in self._index()

    def create(self, name: str) -> dict:
        name = (name or "").strip()
        if not NAME_RE.match(name):
            raise ProfileError("Profile names can use letters, numbers, spaces, . _ - (up to 40 characters).")
        with self._lock:
            index = self._index()
            if any(n.lower() == name.lower() for n in index):
                raise ProfileError(f"A profile named '{name}' already exists.")
            index[name] = {"canvas_base_url": ""}
            self._save_index(index)
            return self.public_info(name, index)

    def delete(self, name: str) -> None:
        with self._lock:
            index = self._require(name)
            for kind in SECRET_KINDS:
                self._delete(self._account(name, kind))
            index.pop(name)
            self._save_index(index)

    def set_secret(self, name: str, kind: str, value: str) -> None:
        if kind not in SECRET_KINDS:
            raise ProfileError("Unknown key type.")
        value = (value or "").strip()
        if not value:
            raise ProfileError("The token is empty.")
        if any(c.isspace() for c in value) or len(value) > 4096:
            raise ProfileError("That doesn't look like a token (it has spaces or is far too long).")
        with self._lock:
            self._require(name)
            self._set(self._account(name, kind), value)

    def get_secret(self, name: str, kind: str) -> str | None:
        if kind not in SECRET_KINDS:
            raise ProfileError("Unknown key type.")
        return self._get(self._account(name, kind))

    def set_canvas_base_url(self, name: str, url: str) -> None:
        with self._lock:
            index = self._require(name)
            index[name]["canvas_base_url"] = url
            self._save_index(index)

    def get_canvas_base_url(self, name: str) -> str:
        return (self._index().get(name) or {}).get("canvas_base_url") or ""

    def public_info(self, name: str, index: dict | None = None) -> dict:
        index = index if index is not None else self._index()
        info = {"name": name, "canvas_base_url": (index.get(name) or {}).get("canvas_base_url") or ""}
        for kind in SECRET_KINDS:
            info[kind] = mask(self.get_secret(name, kind))
        return info
