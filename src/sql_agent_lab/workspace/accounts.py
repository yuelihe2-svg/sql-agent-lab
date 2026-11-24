"""Opt-in accounts for the loopback-only workspace."""
from dataclasses import dataclass
import hashlib
from http.cookies import CookieError, SimpleCookie
import json
import re
import secrets
import threading
import time
from pathlib import Path
from uuid import UUID

from .database import LOCAL_USER_ID

COOKIE = "sql_studio_session"
TTL = 8 * 60 * 60


@dataclass(frozen=True)
class Account:
    id: str
    name: str
    sources: frozenset[str]


class AccessDenied(ValueError):
    pass


class Accounts:
    def __init__(self, config, store, sources):
        self.by_id = {}
        self._tokens = {}
        self._sessions = {}
        self._lock = threading.Lock()
        entries = json.loads(Path(config).read_text(encoding="utf-8-sig"))
        if not isinstance(entries, list) or not entries:
            raise ValueError("Accounts config must be a nonempty JSON array")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("Invalid account")
            try:
                key = str(UUID(entry.get("id", "")))
            except (ValueError, TypeError, AttributeError):
                raise ValueError("Account ID must be a UUID") from None
            name, digest, grants = (entry.get(field) for field in ("name", "token_sha256", "sources"))
            if (key == LOCAL_USER_ID or key in self.by_id or not isinstance(name, str)
                    or not 1 <= len(name.strip()) <= 100 or not isinstance(digest, str)
                    or not re.fullmatch(r"[a-f0-9]{64}", digest) or digest in self._tokens
                    or not isinstance(grants, list)
                    or any(not isinstance(source, str) or source not in sources for source in grants)):
                raise ValueError("Invalid account, reused credential, or unknown source grant")
            self.by_id[key] = self._tokens[digest] = Account(key, name.strip(), frozenset(grants))
        for account in self.by_id.values():
            store.register_team_user(account.id, account.name)

    @staticmethod
    def _cookie(header):
        try:
            cookies = SimpleCookie(header or "")
        except CookieError:
            return None
        item = cookies.get(COOKIE)
        return item.value if item else None

    def sign_in(self, token, cookie):
        if not isinstance(token, str) or not 32 <= len(token) <= 256:
            return None
        account = self._tokens.get(hashlib.sha256(token.encode()).hexdigest())
        if account is None:
            return None
        with self._lock:
            now = time.monotonic()
            self._sessions = {key: row for key, row in self._sessions.items() if row[1] > now}
            self._sessions.pop(self._cookie(cookie), None)
            if len(self._sessions) >= 256:
                self._sessions.pop(next(iter(self._sessions)))
            session = secrets.token_urlsafe(32)
            self._sessions[session] = (account, now + TTL)
        return session

    def identify(self, cookie):
        with self._lock:
            row = self._sessions.get(self._cookie(cookie))
            return row[0] if row and row[1] > time.monotonic() else None

    def sign_out(self, cookie):
        with self._lock:
            self._sessions.pop(self._cookie(cookie), None)
