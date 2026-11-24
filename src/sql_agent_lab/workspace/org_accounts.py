"""Trust boundary for a private HTTPS organization-login gateway."""
import hashlib
import json
import re
import secrets
import threading
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from .accounts import Account
from .database import LOCAL_USER_ID


class OrganizationAccounts:
    logout_path = "/auth/logout"

    def __init__(self, config, store, sources, gateway_key):
        if not isinstance(gateway_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", gateway_key):
            raise ValueError("Set SQL_STUDIO_GATEWAY_KEY to a random gateway key")
        self.config = Path(config)
        self.store = store
        self.sources = frozenset(sources)
        self._key = gateway_key.encode("ascii")
        self._lock = threading.Lock()
        self._fingerprint = None
        document = self._document()
        self.origin = self._https_url(document.get("origin"), is_origin=True)
        self.issuer = self._https_url(document.get("issuer"))
        self._reload()

    @staticmethod
    def _https_url(value, *, is_origin=False):
        if not isinstance(value, str) or any(char.isspace() for char in value):
            raise ValueError("Organization gateway URLs must use HTTPS")
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment):
            raise ValueError("Organization gateway URLs must use HTTPS")
        if is_origin and (parsed.path or parsed.port == 443 or value != f"https://{parsed.netloc}"):
            raise ValueError("Organization origin must be a browser origin without a path or default port")
        return value

    def _document(self):
        return self._parse(self.config.read_bytes())

    @staticmethod
    def _parse(content):
        doc = json.loads(content.decode("utf-8-sig"))
        if not isinstance(doc, dict) or not isinstance(doc.get("accounts"), list):
            raise ValueError("Organization config must include an accounts array")
        return doc

    def _reload(self):
        # Read on each authorization. A missing or malformed replacement fails closed.
        with self._lock:
            content = self.config.read_bytes()
            fingerprint = hashlib.sha256(content).digest()
            if fingerprint == self._fingerprint:
                return self._accounts
            doc = self._parse(content)
            if doc.get("origin") != self.origin or doc.get("issuer") != self.issuer:
                raise ValueError("Changing organization issuer or origin requires a restart")
            accounts, ids = {}, set()
            for entry in doc["accounts"]:
                if not isinstance(entry, dict):
                    raise ValueError("Invalid organization account")
                try:
                    key = str(UUID(entry.get("id", "")))
                except (ValueError, TypeError, AttributeError):
                    raise ValueError("Organization account ID must be a UUID") from None
                subject, name, grants = (entry.get(field) for field in ("subject", "name", "sources"))
                if (key == LOCAL_USER_ID or key in ids or not isinstance(subject, str)
                        or not re.fullmatch(r"[!-~]{1,255}", subject) or subject in accounts
                        or not isinstance(name, str) or not 1 <= len(name.strip()) <= 100
                        or not isinstance(grants, list)
                        or any(not isinstance(grant, str) or grant not in self.sources for grant in grants)):
                    raise ValueError("Invalid organization account or source grant")
                accounts[subject] = Account(key, name.strip(), frozenset(grants))
                ids.add(key)
            self.store.register_organization_users(self.issuer, accounts)
            self._accounts = accounts
            self._fingerprint = fingerprint
            return accounts

    @property
    def by_id(self):
        return {account.id: account for account in self._reload().values()}

    @staticmethod
    def _one(headers, name):
        values = headers.get_all(name, [])
        return values[0] if len(values) == 1 else None

    def trusts(self, headers):
        key = self._one(headers, "X-SQL-Studio-Gateway-Key")
        return (key is not None and key.isascii() and secrets.compare_digest(key.encode("ascii"), self._key)
                and self._one(headers, "X-Forwarded-Proto") == "https")

    def identify_gateway(self, headers):
        if self._one(headers, "X-SQL-Studio-Issuer") != self.issuer:
            return None
        return self._reload().get(self._one(headers, "X-SQL-Studio-Subject"))
