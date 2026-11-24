"""Where the IDE keeps its own secrets: connection passwords and field-encryption keys.

Backends (chosen in Settings → Security):
  keychain  the OS keychain via `keyring` (macOS Keychain, Windows Credential Manager)
  vault     a file (~/.dbide/vault.bin) encrypted with AES-256-GCM using a key
            derived from a master password with scrypt
  session   kept in memory only; you re-enter passwords every launch
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from dbide.core import crypto
from dbide.core.config import app_dir

KEYRING_SERVICE = "dbide"

BACKENDS = {
    "keychain": "System keychain",
    "vault": "Encrypted vault file (master password)",
    "session": "Don't save (ask every time)",
}


class VaultLocked(Exception):
    pass


class SecretStore:
    name = ""

    def get(self, key: str) -> str | None:
        raise NotImplementedError

    def set(self, key: str, value: str) -> None:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError

    def items(self) -> dict[str, str]:
        """All secrets this app stored (used when switching backend)."""
        raise NotImplementedError


class SessionStore(SecretStore):
    name = "session"

    def __init__(self):
        self._data: dict[str, str] = {}

    def get(self, key):
        return self._data.get(key)

    def set(self, key, value):
        self._data[key] = value

    def delete(self, key):
        self._data.pop(key, None)

    def items(self):
        return dict(self._data)


class KeychainStore(SecretStore):
    """Secrets live in the OS keychain; an index file remembers which names exist."""

    name = "keychain"

    def __init__(self, index_path: Path | None = None):
        self.index_path = index_path or app_dir() / "keychain_index.json"

    def _index(self) -> list[str]:
        try:
            return json.loads(self.index_path.read_text())
        except (OSError, json.JSONDecodeError):
            return []

    def _save_index(self, names: list[str]) -> None:
        self.index_path.write_text(json.dumps(sorted(set(names))))

    def get(self, key):
        try:
            import keyring

            return keyring.get_password(KEYRING_SERVICE, key)
        except Exception:
            return None

    def set(self, key, value):
        import keyring

        keyring.set_password(KEYRING_SERVICE, key, value)
        self._save_index(self._index() + [key])

    def delete(self, key):
        try:
            import keyring

            keyring.delete_password(KEYRING_SERVICE, key)
        except Exception:
            pass
        self._save_index([k for k in self._index() if k != key])

    def items(self):
        out = {}
        for k in self._index():
            v = self.get(k)
            if v is not None:
                out[k] = v
        return out


class VaultStore(SecretStore):
    name = "vault"

    def __init__(self, path: Path | None = None):
        self.path = path or app_dir() / "vault.bin"
        self._key: bytes | None = None
        self._salt: bytes | None = None
        self._data: dict[str, str] | None = None

    @property
    def exists(self) -> bool:
        return self.path.exists()

    @property
    def unlocked(self) -> bool:
        return self._data is not None

    def create(self, master_password: str) -> None:
        if len(master_password) < 8:
            raise ValueError("Use a master password of at least 8 characters.")
        self._salt = os.urandom(16)
        self._key = crypto.derive_key(master_password, self._salt)
        self._data = {}
        self._write()

    def unlock(self, master_password: str) -> None:
        blob = json.loads(self.path.read_text())
        salt = base64.b64decode(blob["salt"])
        key = crypto.derive_key(master_password, salt)
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        try:
            raw = AESGCM(key).decrypt(base64.b64decode(blob["nonce"]), base64.b64decode(blob["data"]), b"dbide-vault")
        except Exception as exc:
            raise ValueError("Wrong master password.") from exc
        self._salt, self._key, self._data = salt, key, json.loads(raw)

    def lock(self) -> None:
        self._key = self._data = None

    def _write(self) -> None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = os.urandom(12)
        ct = AESGCM(self._key).encrypt(nonce, json.dumps(self._data).encode(), b"dbide-vault")
        blob = {
            "format": "dbide-vault-1",
            "kdf": "scrypt(n=32768,r=8,p=1)",
            "cipher": "AES-256-GCM",
            "salt": base64.b64encode(self._salt).decode(),
            "nonce": base64.b64encode(nonce).decode(),
            "data": base64.b64encode(ct).decode(),
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(blob))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def _require(self):
        if self._data is None:
            raise VaultLocked("The vault is locked.")

    def get(self, key):
        return None if self._data is None else self._data.get(key)

    def set(self, key, value):
        self._require()
        self._data[key] = value
        self._write()

    def delete(self, key):
        if self._data is not None and key in self._data:
            del self._data[key]
            self._write()

    def items(self):
        self._require()
        return dict(self._data)


_store: SecretStore | None = None


def make_store(backend: str) -> SecretStore:
    return {"keychain": KeychainStore, "vault": VaultStore, "session": SessionStore}[backend]()


def store() -> SecretStore:
    global _store
    if _store is None:
        from dbide.core.settings import settings

        _store = make_store(settings().get("secret_backend"))
    return _store


def set_store(s: SecretStore) -> None:
    global _store
    _store = s


def switch_backend(new: SecretStore) -> int:
    """Move every secret from the current backend into `new`. Returns how many moved."""
    from dbide.core.settings import settings

    old = store()
    moved = old.items()
    for k, v in moved.items():
        new.set(k, v)
    for k in moved:
        old.delete(k)
    set_store(new)
    settings().set("secret_backend", new.name)
    return len(moved)


# ---- field-encryption keys ----------------------------------------------------


def field_key(conn_id: str, create: bool = False) -> bytes | None:
    name = f"fieldkey:{conn_id}"
    v = store().get(name)
    if v:
        return base64.b64decode(v)
    if not create:
        return None
    key = crypto.new_key()
    store().set(name, base64.b64encode(key).decode())
    return key


def import_field_key(conn_id: str, b64: str) -> None:
    raw = base64.b64decode(b64.strip())
    if len(raw) != 32:
        raise ValueError("A field key is 32 bytes (44 characters of base64).")
    store().set(f"fieldkey:{conn_id}", base64.b64encode(raw).decode())
