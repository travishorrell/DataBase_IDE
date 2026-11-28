"""The Playground tab: sample data, plain-English and SQL, and Undo for everything."""
from __future__ import annotations

import html
from pathlib import Path

import shiboken6
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from dbide.core import playground as P
from dbide.core.progress import progress
from dbide.core.templates import TEMPLATES
from dbide.ui.help_panel import HelpCenter, tag
from dbide.ui.theme import ThemeManager
from dbide.ui.worker import connect_owned, run_async


class _Bridge(QObject):
    """Playground snapshots happen on worker threads; this hops the notification to the UI thread."""

    changed = Signal(str)


class PlaygroundTab(QWidget):
    def __init__(self, main, key: str):
        super().__init__()
        self.main, self.key = main, key
        self.manager = main.manager
        self.pg = P.playground()
        self.bridge = _Bridge(self)
        self.bridge.changed.connect(self._undo_changed)
        self._listener = lambda k: shiboken6.isValid(self.bridge) and self.bridge.changed.emit(k)
        self.pg.listeners.append(self._listener)
        self.destroyed.connect(lambda *_: self.pg.listeners.remove(self._listener) if self._listener in self.pg.listeners else None)
        self.query_tab = None
        self.data_tabs: dict[str, QWidget] = {}
        tm = ThemeManager.get()

        # ---- banner
        banner = QFrame()
        banner.setObjectName("Card")
        outer = QVBoxLayout(banner)
        outer.setContentsMargins(14, 10, 14, 10)
        outer.setSpacing(8)
        bl = QHBoxLayout()
        msg = QLabel("🧪 <b>Playground</b>: sample data you can't break. Experiment freely; every change can be undone.")
        msg.setTextFormat(Qt.TextFormat.RichText)
        msg.setWordWrap(True)
        self.dataset = QComboBox()
        for k, t in TEMPLATES.items():
            self.dataset.addItem(tm.icon(t.icon, "accent"), t.name, k)
        self.dataset.setCurrentIndex(self.dataset.findData(key))
        self.dataset.currentIndexChanged.connect(lambda _i: self.main.open_playground(self.dataset.currentData()))
        self.undo_btn = QPushButton("Undo")
        tm.bind_icon(self.undo_btn, "ph.arrow-counter-clockwise")
        self.undo_btn.setObjectName("Primary")
        self.undo_btn.clicked.connect(self.undo)
        tag(self.undo_btn, "cmd:undo")
        reset = QPushButton("Reset")
        reset.setToolTip("Rebuild the sample data from scratch")
        reset.clicked.connect(self.reset)
        save = QPushButton("Save as Project…")
        save.setToolTip("Copy this database (with your changes) to a real file you can keep")
        save.clicked.connect(self.save_as)
        outer.addWidget(msg)
        outer.addLayout(bl)
        bl.addWidget(QLabel("Dataset"))
        bl.addWidget(self.dataset)
        bl.addWidget(self.undo_btn)
        bl.addWidget(reset)
        bl.addStretch()
        bl.addWidget(save)
        tag(banner, "concept:playground")

        # ---- left: tables and things to try
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(10, 8, 6, 8)
        t1 = QLabel("TABLES")
        t1.setObjectName("PanelTitle")
        self.tables = QListWidget()
        self.tables.setToolTip("Double-click a table to browse and edit its rows")
        self.tables.itemDoubleClicked.connect(lambda it: self.open_table(it.data(Qt.ItemDataRole.UserRole)))
        t2 = QLabel("TRY THIS")
        t2.setObjectName("PanelTitle")
        self.tries = QListWidget()
        self.tries.setWordWrap(True)
        self.tries.itemClicked.connect(self._try)
        self.tries.setToolTip("Click to run it. Plain-English ones go through the Ask bar so you can see the SQL they become.")
        self.about = QLabel()
        self.about.setWordWrap(True)
        self.about.setObjectName("Muted")
        ll.addWidget(t1)
        ll.addWidget(self.tables, 2)
        ll.addWidget(t2)
        ll.addWidget(self.tries, 3)
        ll.addWidget(self.about)
        left.setMinimumWidth(230)

        self.work = QTabWidget()
        self.work.setObjectName("SubTabs")
        self.work.setDocumentMode(True)
        self.work.setTabsClosable(True)
        self.work.tabCloseRequested.connect(self._close_work_tab)
        self.status = QLabel("Preparing sample data…")
        self.status.setObjectName("Muted")
        self.status.setContentsMargins(12, 4, 12, 4)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(left)
        split.addWidget(self.work)
        split.setStretchFactor(1, 1)
        split.setSizes([260, 900])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 0)
        lay.setSpacing(8)
        lay.addWidget(banner)
        lay.addWidget(split, 1)
        lay.addWidget(self.status)
        self._fill_static()
        self._undo_changed(self.key)
        connect_owned(self.manager.connected, self, self._maybe_ready)
        connect_owned(self.manager.disconnected, self, self._maybe_gone)
        if self.manager.is_open(self.conn_id):
            self._ready()

    schema = None  # playgrounds are single-file SQLite databases

    @property
    def conn_id(self) -> str:
        return P.conn_id(self.key)

    def title(self) -> str:
        return f"Playground: {TEMPLATES[self.key].name}"

    # ---- setup -----------------------------------------------------------------
    def _fill_static(self) -> None:
        tpl = TEMPLATES[self.key]
        self.tries.clear()
        for tt in tpl.try_this:
            first = tt.text.splitlines()[0]
            label = (("SQL: " + first + (" …" if "\n" in tt.text else "")) if tt.sql else f"“{tt.text}”")
            it = QListWidgetItem(label + (f"\n   {tt.note}" if tt.note else ""))
            it.setData(Qt.ItemDataRole.UserRole, tt)
            it.setToolTip(tt.text)
            self.tries.addItem(it)
        self.about.setText(tpl.description)

    def _maybe_ready(self, cid: str) -> None:
        if cid == self.conn_id:
            self._ready()

    def _maybe_gone(self, cid: str) -> None:
        if cid == self.conn_id:
            self.status.setText("Rebuilding…")

    def _ready(self) -> None:
        s = self.manager.session(self.conn_id)
        if s is None:
            return
        self.pg.attach(s, self.key)
        if self.query_tab is None or not shiboken6.isValid(self.query_tab):
            from dbide.ui.query_tab import QueryTab

            self.query_tab = QueryTab(self.manager, self.conn_id, None, "-- Ask a question above, click an idea on the left, or write SQL here.\n")
            self.query_tab.schema_maybe_changed.connect(lambda _cid: self.refresh_tables())
            self.work.insertTab(0, self.query_tab, "Query")
            self.work.tabBar().setTabButton(0, self.work.tabBar().ButtonPosition.RightSide, None)  # the query tab stays
            self.work.setCurrentIndex(0)
        else:
            self.query_tab.refresh_connections(prefer=self.conn_id)
        for dt_ in list(self.data_tabs.values()):
            if shiboken6.isValid(dt_):
                dt_.reload()
        self.refresh_tables()
        self.status.setText("Ready. Nothing here can break; use Undo or Reset any time.")

    def refresh_tables(self) -> None:
        s = self.manager.session(self.conn_id)
        if s is None:
            return

        def work():
            out = []
            with s.engine.connect() as c:
                for t in s.tables():
                    out.append((t, c.exec_driver_sql(f'SELECT COUNT(*) FROM "{t}"').scalar()))
            return out

        def done(rows):
            if not shiboken6.isValid(self.tables):
                return
            self.tables.clear()
            tm = ThemeManager.get()
            for t, n in rows:
                it = QListWidgetItem(tm.icon("table", "accent"), f"{t}   ·  {n} rows")
                it.setData(Qt.ItemDataRole.UserRole, t)
                self.tables.addItem(it)

        run_async(work, done)

    # ---- actions -------------------------------------------------------------------
    def _try(self, item: QListWidgetItem) -> None:
        tt = item.data(Qt.ItemDataRole.UserRole)
        q = self.query_tab
        if q is None:
            return
        self.work.setCurrentWidget(q)
        if tt.sql:
            q.editor.setPlainText(tt.text)
            q.run(all_=True)
        else:
            q.ask.setText(tt.text)
            q.generate_sql()
        if tt.note:
            HelpCenter.get().show_html("Try this", f"<p><b>{html.escape(tt.text.splitlines()[0])}</b></p><p>This shows: {html.escape(tt.note)}.</p>")

    def open_table(self, table: str) -> None:
        if table in self.data_tabs and shiboken6.isValid(self.data_tabs[table]):
            self.work.setCurrentWidget(self.data_tabs[table])
            return
        from dbide.ui.data_tab import DataTab

        tab = DataTab(self.manager, self.conn_id, table, None)
        self.data_tabs[table] = tab
        self.work.addTab(tab, table)
        self.work.setCurrentWidget(tab)

    def _close_work_tab(self, i: int) -> None:
        w = self.work.widget(i)
        if w is self.query_tab:
            return
        self.work.removeTab(i)
        for k, v in list(self.data_tabs.items()):
            if v is w:
                del self.data_tabs[k]
        w.deleteLater()

    def refresh_titles(self) -> None:  # DataTab looks for this to update its title
        for i in range(self.work.count()):
            w = self.work.widget(i)
            if hasattr(w, "title") and w is not self.query_tab:
                self.work.setTabText(i, w.title())

    def _undo_changed(self, key: str) -> None:
        if key != self.key or not shiboken6.isValid(self.undo_btn):
            return
        n = self.pg.undo_depth(self.key)
        last = self.pg.last_label(self.key)
        self.undo_btn.setEnabled(n > 0)
        self.undo_btn.setText(f"Undo ({n})" if n else "Undo")
        self.undo_btn.setToolTip(f"Undo: {last}" if last else "Nothing to undo yet")

    def undo(self) -> None:
        key = self.key

        def done(label):
            if label is None:
                return
            progress().record("undo")
            self.status.setText(f"↶ Undone: {label}")
            HelpCenter.get().show_html("Undone", f"<p>The database is back to how it was before:</p><pre>{html.escape(label)}</pre>"
                                       "<p>In a real database there's no undo button. That's what transactions (BEGIN … ROLLBACK) and backups are for.</p>")
            for dt_ in list(self.data_tabs.values()):
                if shiboken6.isValid(dt_):
                    dt_.model.reset_page(dt_.model.columns, [], dt_.model.primary_key)
                    dt_.reload()
            self.refresh_tables()
            if self.query_tab is not None:
                self.query_tab._nl_cache.clear()

        run_async(lambda: self.pg.undo(key), done, lambda m: QMessageBox.warning(self, "Undo", m))

    def reset(self) -> None:
        if QMessageBox.question(self, "Reset playground", f"Rebuild the {TEMPLATES[self.key].name} sample data from scratch? "
                                "Your changes and undo history are cleared.") != QMessageBox.StandardButton.Yes:
            return
        self.main.reset_playground(self.key)

    def save_as(self) -> None:
        default = str(Path.home() / f"{self.key}_project.sqlite")
        path, _ = QFileDialog.getSaveFileName(self, "Save playground as a project", default, "SQLite (*.sqlite *.db)")
        if not path:
            return
        key = self.key

        def done(dest):
            self.main.add_sqlite_project(Path(dest), TEMPLATES[key].name, copy_roles_from=self.conn_id)

        run_async(lambda: self.pg.save_as(key, Path(path)), done, lambda m: QMessageBox.critical(self, "Save", m))

    def shutdown(self) -> None:
        if self.query_tab is not None and shiboken6.isValid(self.query_tab):
            self.query_tab.shutdown()
