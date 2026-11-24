"""Tracks open sessions so every panel shares one engine per connection."""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from dbide.core.config import ConnectionConfig, ConnectionStore
from dbide.core.history import QueryHistory
from dbide.core.session import DatabaseSession
from dbide.ui.worker import run_async


class ConnectionManager(QObject):
    connected = Signal(str)  # conn id
    disconnected = Signal(str)
    failed = Signal(str, str)  # conn id, message
    profiles_changed = Signal()
    schema_changed = Signal(str, object)  # conn id, schema (None = whole connection)

    def __init__(self, store: ConnectionStore | None = None, history: QueryHistory | None = None):
        super().__init__()
        self.store = store or ConnectionStore()
        self.history = history or QueryHistory()
        self.sessions: dict[str, DatabaseSession] = {}
        self._pending: set[str] = set()

    def session(self, conn_id: str) -> DatabaseSession | None:
        return self.sessions.get(conn_id)

    def is_open(self, conn_id: str) -> bool:
        return conn_id in self.sessions

    def open(self, cfg: ConnectionConfig, password: str | None) -> None:
        if cfg.id in self.sessions or cfg.id in self._pending:
            return
        self._pending.add(cfg.id)

        def work():
            s = DatabaseSession(cfg, password)
            try:
                s.connect()
            except Exception:
                s.close()
                raise
            return s

        def ok(session):
            self._pending.discard(cfg.id)
            self.sessions[cfg.id] = session
            self.connected.emit(cfg.id)

        def err(msg):
            self._pending.discard(cfg.id)
            self.failed.emit(cfg.id, msg)

        run_async(work, ok, err)

    def close(self, conn_id: str) -> None:
        s = self.sessions.pop(conn_id, None)
        if s:
            s.close()
            self.disconnected.emit(conn_id)

    def close_all(self) -> None:
        for cid in list(self.sessions):
            self.close(cid)

    def open_sessions(self) -> list[tuple[str, DatabaseSession]]:
        return sorted(self.sessions.items(), key=lambda kv: kv[1].cfg.name.lower())
