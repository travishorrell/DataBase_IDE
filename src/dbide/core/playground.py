"""The Playground: disposable copies of sample databases where nothing can go wrong.

Before every write the whole database is copied into memory (SQLite's online
backup API), so any change, even DROP TABLE, can be undone step by step, and
Reset rebuilds the sample from its template.
"""
from __future__ import annotations

import shutil
import sqlite3
import threading
from pathlib import Path

from dbide.core.config import ConnectionConfig, app_dir
from dbide.core.templates import TEMPLATES, apply_template

PREFIX = "playground:"
MAX_UNDO = 40
TIME_LIMIT = 5.0  # seconds before a playground query is stopped


def conn_id(key: str) -> str:
    return PREFIX + key


def is_playground(conn_id_: str | None) -> bool:
    return bool(conn_id_) and conn_id_.startswith(PREFIX)


class Playground:
    def __init__(self, root: Path | None = None):
        self.root = root or app_dir() / "playground"
        self.root.mkdir(parents=True, exist_ok=True)
        self._undo: dict[str, list[tuple[str, sqlite3.Connection]]] = {}
        self._lock = threading.Lock()
        self.listeners: list = []  # fn(key) called after a snapshot / undo / reset

    def path(self, key: str) -> Path:
        return self.root / f"{key}.sqlite"

    def config(self, key: str) -> ConnectionConfig:
        tpl = TEMPLATES[key]
        return ConnectionConfig(name=f"Playground · {tpl.name}", driver="sqlite", database=str(self.path(key)),
                                id=conn_id(key), options={"sandbox": True, "time_limit": TIME_LIMIT})

    def exists(self, key: str) -> bool:
        return self.path(key).exists()

    def create(self, key: str) -> Path:
        """(Re)build the sample database for a template."""
        from dbide.core.session import DatabaseSession

        path = self.path(key)
        self._remove_files(key)
        s = DatabaseSession(self.config(key))
        try:
            apply_template(s, TEMPLATES[key], with_data=True)
        finally:
            s.close()
        self.clear_undo(key)
        return path

    def _remove_files(self, key: str) -> None:
        path = self.path(key)
        for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
            p.unlink(missing_ok=True)
        shutil.rmtree(path.parent / f"{path.stem}_media", ignore_errors=True)

    # ---- snapshots ------------------------------------------------------------
    def snapshot(self, key: str, label: str) -> None:
        mem = sqlite3.connect(":memory:", check_same_thread=False)
        src = sqlite3.connect(self.path(key))
        try:
            src.backup(mem)
        finally:
            src.close()
        with self._lock:
            stack = self._undo.setdefault(key, [])
            stack.append((label, mem))
            while len(stack) > MAX_UNDO:
                stack.pop(0)[1].close()
        self._notify(key)

    def undo_depth(self, key: str) -> int:
        return len(self._undo.get(key, []))

    def last_label(self, key: str) -> str | None:
        stack = self._undo.get(key)
        return stack[-1][0] if stack else None

    def undo(self, key: str) -> str | None:
        """Restore the state before the last change. Returns its label, or None if nothing to undo."""
        with self._lock:
            stack = self._undo.get(key)
            if not stack:
                return None
            label, mem = stack.pop()
        dst = sqlite3.connect(self.path(key), timeout=5)
        try:
            mem.backup(dst)  # pages are copied into the live file; other connections see the old state again
        finally:
            dst.close()
            mem.close()
        self._notify(key)
        return label

    def clear_undo(self, key: str) -> None:
        with self._lock:
            for _label, mem in self._undo.pop(key, []):
                mem.close()
        self._notify(key)

    def _notify(self, key: str) -> None:
        for fn in list(self.listeners):
            try:
                fn(key)
            except Exception:
                pass

    def attach(self, session, key: str) -> None:
        """Snapshot before every write the session makes."""
        session.before_write = lambda what: self.snapshot(key, _short(what))

    def save_as(self, key: str, dest: Path) -> Path:
        """Copy the playground database (and its media folder) into a real project file."""
        src = self.path(key)
        conn = sqlite3.connect(src)
        out = sqlite3.connect(dest)
        try:
            conn.backup(out)
        finally:
            conn.close()
            out.close()
        media_src = src.parent / f"{src.stem}_media"
        if media_src.exists():
            # stored paths are relative ("store_media/images/…"), so keep the folder name to keep the links working
            shutil.copytree(media_src, dest.parent / media_src.name, dirs_exist_ok=True)
        return dest


def _short(what: str) -> str:
    s = " ".join(what.split())
    return s if len(s) <= 70 else s[:69] + "…"


_pg: Playground | None = None


def playground() -> Playground:
    global _pg
    if _pg is None:
        _pg = Playground()
    return _pg


def reset_for_tests() -> None:
    global _pg
    _pg = None
