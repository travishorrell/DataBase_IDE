"""Saved connection profiles and app data paths.

Profiles live in ~/.dbide/connections.json. Passwords are never written there;
they go to the OS keychain via `keyring` when the user opts to save them.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

def app_dir() -> Path:
    path = Path(os.environ.get("DBIDE_HOME", Path.home() / ".dbide"))
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class ConnectionConfig:
    name: str
    driver: str  # key in dbide.drivers registry, e.g. "sqlite", "mysql"
    host: str = ""
    port: int | None = None
    user: str = ""
    database: str = ""  # MySQL default schema, or SQLite file path
    options: dict = field(default_factory=dict)
    save_password: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @classmethod
    def from_dict(cls, data: dict) -> "ConnectionConfig":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def summary(self) -> str:
        if self.driver == "sqlite":
            return self.database
        target = f"{self.user}@{self.host}" if self.user else self.host
        if self.port:
            target += f":{self.port}"
        if self.database:
            target += f"/{self.database}"
        return target


def get_password(cfg: ConnectionConfig) -> str | None:
    if not cfg.save_password:
        return None
    from dbide.core.secrets import store

    try:
        return store().get(cfg.id)
    except Exception:
        return None


def set_password(cfg: ConnectionConfig, password: str | None) -> None:
    """Save (or forget) a connection password in the configured secret backend."""
    from dbide.core.secrets import store

    try:
        if password and cfg.save_password:
            store().set(cfg.id, password)
        else:
            store().delete(cfg.id)
    except Exception:
        pass


class ConnectionStore:
    def __init__(self, path: Path | None = None):
        self.path = path or app_dir() / "connections.json"
        self._items: dict[str, ConnectionConfig] = {}
        self.load()

    def load(self) -> None:
        self._items.clear()
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        for raw in data.get("connections", []):
            cfg = ConnectionConfig.from_dict(raw)
            self._items[cfg.id] = cfg

    def save(self) -> None:
        payload = {"connections": [asdict(c) for c in self._items.values()]}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(self.path)

    def all(self) -> list[ConnectionConfig]:
        return sorted(self._items.values(), key=lambda c: c.name.lower())

    def get(self, conn_id: str) -> ConnectionConfig | None:
        return self._items.get(conn_id)

    def upsert(self, cfg: ConnectionConfig) -> None:
        self._items[cfg.id] = cfg
        self.save()

    def remove(self, conn_id: str) -> None:
        cfg = self._items.pop(conn_id, None)
        if cfg:
            cfg.save_password = False
            set_password(cfg, None)
        self.save()
