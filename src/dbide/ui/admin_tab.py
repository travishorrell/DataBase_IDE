"""Admin Console: everyday database administration in one tab."""
from __future__ import annotations

import html
from pathlib import Path

import shiboken6
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from dbide.core import admin as A
from dbide.core.media import human_size
from dbide.ui.grid import GridView, ResultModel
from dbide.ui.help_panel import tag
from dbide.ui.sql_editor import monospace_font
from dbide.ui.theme import ThemeManager
from dbide.ui.worker import run_async


def _stat(title: str, value: str) -> QFrame:
    f = QFrame()
    f.setObjectName("Card")
    v = QVBoxLayout(f)
    v.setContentsMargins(14, 10, 14, 10)
    t = QLabel(title)
    t.setObjectName("Muted")
    val = QLabel(value)
    val.setObjectName("CardTitle")
    val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    val.setWordWrap(True)
    v.addWidget(t)
    v.addWidget(val)
    f.value_label = val  # type: ignore[attr-defined]
    return f


class AdminTab(QWidget):
    def __init__(self, manager, conn_id: str, schema: str | None):
        super().__init__()
        self.manager, self.conn_id, self.schema = manager, conn_id, schema
        tm = ThemeManager.get()
        self.pages = QTabWidget()
        self.pages.setObjectName("SubTabs")
        self.pages.setDocumentMode(True)

        # ---- Overview
        ov = QWidget()
        ol = QVBoxLayout(ov)
        cards = QGridLayout()
        self.c_engine, self.c_size, self.c_tables, self.c_where = (_stat("Engine", "…"), _stat("Size", "…"),
                                                                    _stat("Tables / views", "…"), _stat("Location", "…"))
        for i, c in enumerate((self.c_engine, self.c_size, self.c_tables, self.c_where)):
            cards.addWidget(c, 0, i)
        cards.setColumnStretch(3, 2)
        self.notes = QLabel()
        self.notes.setObjectName("Muted")
        self.notes.setWordWrap(True)
        self.table_grid = GridView()
        self.table_grid.setSortingEnabled(True)
        refresh = QPushButton("Refresh")
        tm.bind_icon(refresh, "refresh")
        refresh.clicked.connect(self.load_overview)
        top = QHBoxLayout()
        top.addLayout(cards, 1)
        top.addWidget(refresh, 0, Qt.AlignmentFlag.AlignTop)
        ol.addLayout(top)
        ol.addWidget(self.notes)
        ol.addWidget(self.table_grid, 1)
        self.pages.addTab(ov, "Overview")

        # ---- Maintenance
        mt = QWidget()
        ml = QVBoxLayout(mt)
        sqlite = self.session.cfg.driver == "sqlite"
        tasks = [
            ("Integrity Check", "Verify every page, index and foreign key." if sqlite else "CHECK TABLE on every table.", self.run_integrity),
            ("Analyze", "Refresh the statistics the query planner uses to pick indexes.", self.run_analyze),
            ("Vacuum" if sqlite else "Optimize", "Rebuild the file and reclaim free space." if sqlite else "OPTIMIZE TABLE: defragment and reclaim space.",
             self.run_optimize),
            ("Backup…", "Exact copy of the database file." if sqlite else "SQL dump (CREATE + INSERT) of this database.", self.run_backup),
        ]
        grid = QGridLayout()
        self.task_buttons = {}
        for i, (name, desc, slot) in enumerate(tasks):
            b = QPushButton(name)
            b.setMinimumWidth(150)
            b.clicked.connect(slot)
            d = QLabel(desc)
            d.setObjectName("Muted")
            d.setWordWrap(True)
            grid.addWidget(b, i, 0)
            grid.addWidget(d, i, 1)
            self.task_buttons[name] = b
        grid.setColumnStretch(1, 1)
        ml.addLayout(grid)
        self.task_log = QPlainTextEdit()
        self.task_log.setReadOnly(True)
        self.task_log.setFont(monospace_font(12))
        self.task_grid = GridView()
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.task_log)
        split.addWidget(self.task_grid)
        ml.addWidget(split, 1)
        self.pages.addTab(mt, "Maintenance")

        # ---- Activity
        ac = QWidget()
        al = QVBoxLayout(ac)
        row = QHBoxLayout()
        self.act_note = QLabel()
        self.act_note.setObjectName("Muted")
        self.act_note.setWordWrap(True)
        ref = QPushButton("Refresh")
        ref.clicked.connect(self.load_activity)
        self.kill_btn = QPushButton("Stop Selected Query")
        self.kill_btn.clicked.connect(self.kill_selected)
        row.addWidget(self.act_note, 1)
        row.addWidget(ref)
        row.addWidget(self.kill_btn)
        self.proc_grid = GridView()
        al.addLayout(row)
        al.addWidget(self.proc_grid, 1)
        self.pages.addTab(ac, "Activity")

        # ---- Users
        us = QWidget()
        ul = QVBoxLayout(us)
        self.users_note = QLabel()
        self.users_note.setObjectName("Muted")
        self.users_note.setWordWrap(True)
        self.users_grid = GridView()
        self.users_grid.clicked.connect(self._user_clicked)
        self.grants = QPlainTextEdit()
        self.grants.setReadOnly(True)
        self.grants.setFont(monospace_font(12))
        self.grants.setPlaceholderText("Select a user to see their GRANTs")
        usplit = QSplitter(Qt.Orientation.Vertical)
        usplit.addWidget(self.users_grid)
        usplit.addWidget(self.grants)
        ul.addWidget(self.users_note)
        ul.addWidget(usplit, 1)
        self.pages.addTab(us, "Users")

        # ---- Settings
        st = QWidget()
        sl = QVBoxLayout(st)
        self.settings_grid = GridView()
        sl.addWidget(self.settings_grid, 1)
        self.pages.addTab(st, "Settings")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        head = QLabel(f"<b>Admin Console</b> · {html.escape(self.session.cfg.name)}" + (f" · {html.escape(schema)}" if schema else ""))
        head.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(head)
        lay.addWidget(self.pages, 1)
        tag(self, "cmd:admin")
        self.pages.currentChanged.connect(self._page_changed)
        self._loaded: set[int] = set()
        self.load_overview()

    @property
    def session(self):
        return self.manager.session(self.conn_id)

    def title(self) -> str:
        return f"Admin: {self.schema or (self.session.cfg.name if self.session else '')}"

    def _page_changed(self, i: int) -> None:
        if i in self._loaded:
            return
        self._loaded.add(i)
        {1: lambda: None, 2: self.load_activity, 3: self.load_users, 4: self.load_settings}.get(i, lambda: None)()

    @staticmethod
    def _fill(grid: GridView, res) -> None:
        grid.setModel(ResultModel(list(res.columns), [tuple(r) for r in res.rows], grid))
        grid.fit_columns()

    def _err(self, title):
        return lambda m: QMessageBox.warning(self, title, m)

    # ---- overview -------------------------------------------------------------------
    def load_overview(self) -> None:
        s, schema = self.session, self.schema

        def done(ov: A.Overview):
            if not shiboken6.isValid(self.table_grid):
                return
            self.c_engine.value_label.setText(ov.version or ov.engine)
            self.c_size.value_label.setText(human_size(ov.size_bytes) if ov.size_bytes is not None else "?")
            self.c_tables.value_label.setText(f"{len(ov.tables)} / {ov.views}")
            self.c_where.value_label.setText(ov.location)
            self.notes.setText(" ".join(ov.notes))
            cols = ["table", "rows", "indexes"] + (["data", "index size"] if any(t.data_bytes is not None for t in ov.tables) else [])
            rows = []
            for t in ov.tables:
                r = [t.name, t.rows, t.indexes]
                if len(cols) > 3:
                    r += [human_size(t.data_bytes), human_size(t.index_bytes)]
                rows.append(tuple(r))
            self._fill(self.table_grid, A.Result("", cols, rows))

        run_async(lambda: A.overview(s, schema), done, self._err("Overview"))

    # ---- maintenance ------------------------------------------------------------------
    def _task(self, name: str, fn) -> None:
        self.task_log.appendPlainText(f"▶ {name}…")
        for b in self.task_buttons.values():
            b.setEnabled(False)

        def done(res: A.Result):
            for b in self.task_buttons.values():
                b.setEnabled(True)
            self.task_log.appendPlainText(f"{'✓' if res.ok else '⚠'} {res.title}: {res.message or 'done'}\n")
            if res.columns:
                self._fill(self.task_grid, res)
            self.load_overview()

        def failed(m):
            for b in self.task_buttons.values():
                b.setEnabled(True)
            self.task_log.appendPlainText(f"✗ {name} failed: {m}\n")

        run_async(fn, done, failed)

    def run_integrity(self) -> None:
        s, schema = self.session, self.schema
        self._task("Integrity check", lambda: A.integrity_check(s, schema))

    def run_analyze(self) -> None:
        s, schema = self.session, self.schema
        self._task("Analyze", lambda: A.analyze(s, schema))

    def run_optimize(self) -> None:
        s, schema = self.session, self.schema
        self._task("Vacuum / optimize", lambda: A.optimize(s, schema))

    def run_backup(self, dest: str | None = None) -> None:
        s, schema = self.session, self.schema
        if not dest:
            sqlite = s.cfg.driver == "sqlite"
            base = Path(s.cfg.database).stem if sqlite else (schema or s.cfg.database or "database")
            suggestion = str(Path.home() / f"{base}_backup.{'sqlite' if sqlite else 'sql'}")
            dest, _ = QFileDialog.getSaveFileName(self, "Back up to", suggestion,
                                                  "SQLite database (*.sqlite *.db)" if sqlite else "SQL dump (*.sql)")
            if not dest:
                return
        self._task("Backup", lambda: A.backup(s, dest, schema))

    # ---- activity / users / settings ------------------------------------------------------
    def load_activity(self) -> None:
        s = self.session

        def done(res: A.Result):
            self.act_note.setText(res.message or f"{len(res.rows)} connection(s). Select one and click Stop to cancel its query.")
            self.kill_btn.setEnabled(bool(res.columns))
            self._fill(self.proc_grid, res)

        run_async(lambda: A.processes(s), done, self._err("Activity"))

    def kill_selected(self) -> None:
        idx = self.proc_grid.currentIndex()
        if not idx.isValid():
            return
        pid = self.proc_grid.model().value(idx.row(), 0)
        if QMessageBox.question(self, "Stop query", f"Stop the query running on connection {pid}?") != QMessageBox.StandardButton.Yes:
            return
        s = self.session
        run_async(lambda: A.kill(s, int(pid)), lambda _r: self.load_activity(), self._err("Stop query"))

    def load_users(self) -> None:
        s = self.session

        def done(res: A.Result):
            self.users_note.setText(res.message or f"{len(res.rows)} account(s). Select one to see its privileges.")
            self._fill(self.users_grid, res)

        run_async(lambda: A.users(s), done, self._err("Users"))

    def _user_clicked(self, idx) -> None:
        m = self.users_grid.model()
        if m is None or m.columnCount() < 2:
            return
        user, host = m.value(idx.row(), 0), m.value(idx.row(), 1)
        s = self.session
        run_async(lambda: A.grants(s, user, host), lambda g: self.grants.setPlainText(";\n".join(g) + (";" if g else "")),
                  lambda msg: self.grants.setPlainText(msg))

    def load_settings(self) -> None:
        s = self.session
        run_async(lambda: A.settings_list(s), lambda res: self._fill(self.settings_grid, res), self._err("Settings"))

    def shutdown(self) -> None:
        pass
