"""SQL editor tab with result grids."""
from __future__ import annotations

import html
import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from dbide.core.knowledge import friendly_error
from dbide.core.sql_explain import explain as explain_sql
from dbide.core.sqlsplit import split_sql, statement_at
from dbide.core.transfer import export_rows
from dbide.ui.help_panel import HelpCenter, follow_editor, tag
from dbide.ui.theme import ThemeManager, responsive_toolbar
from dbide.ui.connections import ConnectionManager
from dbide.ui.grid import GridView, ResultModel
from dbide.ui.sql_editor import SqlEditor, monospace_font
from dbide.ui.worker import run_async

EXPORT_FILTER = "CSV (*.csv);;JSON (*.json);;SQL INSERT statements (*.sql)"


def export_dialog(parent, default_name: str) -> tuple[str, str] | None:
    path, flt = QFileDialog.getSaveFileName(parent, "Export", default_name + ".csv", EXPORT_FILTER)
    if not path:
        return None
    fmt = "json" if "JSON" in flt else "sql" if "SQL" in flt else "csv"
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    if ext in ("csv", "json", "sql"):
        fmt = ext
    return path, fmt


class QueryTab(QWidget):
    title_changed = Signal()
    schema_maybe_changed = Signal(str)  # conn id; emitted after DDL runs

    _counter = 0

    def __init__(self, manager: ConnectionManager, conn_id: str | None = None, schema: str | None = None, sql: str = ""):
        super().__init__()
        QueryTab._counter += 1
        self.manager = manager
        self.conn_id: str | None = None
        self.qconn = None
        self.schema = schema
        self._applied_schema = None
        self.file_path: str | None = None
        self.untitled = f"Query {QueryTab._counter}"
        self.running = False

        self.editor = SqlEditor()
        self.editor.setPlainText(sql)
        self.editor.document().setModified(False)
        self.editor.document().modificationChanged.connect(lambda _: self.title_changed.emit())

        tb = QToolBar()
        tb.setObjectName("TabToolbar")
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.conn_combo = QComboBox()
        self.conn_combo.setMinimumWidth(180)
        self.conn_combo.currentIndexChanged.connect(self._conn_chosen)
        self.db_combo = QComboBox()
        self.db_combo.setMinimumWidth(140)
        self.db_combo.currentIndexChanged.connect(self._db_chosen)
        tb.addWidget(QLabel(" Connection "))
        tb.addWidget(self.conn_combo)
        self.db_label = tb.addWidget(QLabel("  Database "))
        self.db_action = tb.addWidget(self.db_combo)
        tb.addSeparator()

        self.run_act = QAction("Run", self)
        self.run_act.setShortcut(QKeySequence("Ctrl+Return"))
        self.run_act.setToolTip("Run the selection or the statement under the cursor (⌘↩)")
        self.run_act.triggered.connect(lambda: self.run(all_=False))
        self.run_all_act = QAction("Run All", self)
        self.run_all_act.setShortcuts([QKeySequence("Ctrl+Shift+Return"), QKeySequence("F5")])
        self.run_all_act.setToolTip("Run the whole script (⌘⇧↩ / F5)")
        self.run_all_act.triggered.connect(lambda: self.run(all_=True))
        self.cancel_act = QAction("Stop", self)
        self.cancel_act.setShortcut(QKeySequence("Ctrl+."))
        self.cancel_act.setEnabled(False)
        self.cancel_act.triggered.connect(self.cancel)
        self.explain_act = QAction("Explain", self)
        self.explain_act.setShortcut(QKeySequence("Ctrl+E"))
        self.explain_act.triggered.connect(self.explain)
        self.english_act = QAction("Explain in English", self)
        self.english_act.setShortcut(QKeySequence("Ctrl+Shift+E"))
        self.english_act.triggered.connect(self.explain_english)
        self.join_act = QAction("Visualize JOIN", self)
        self.join_act.setShortcut(QKeySequence("Ctrl+J"))
        self.join_act.triggered.connect(self.visualize_join)
        for a in (self.run_act, self.run_all_act, self.cancel_act, self.explain_act, self.english_act, self.join_act):
            a.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            self.addAction(a)
            tb.addAction(a)
        tm = ThemeManager.get()
        for a, icon, role in ((self.run_act, "run", "on_accent"), (self.run_all_act, "run_all", "text"),
                              (self.cancel_act, "stop", "danger"), (self.explain_act, "explain", "text"),
                              (self.english_act, "english", "text"), (self.join_act, "join", "text")):
            tm.bind_icon(a, icon, role)
        run_btn = tb.widgetForAction(self.run_act)
        if run_btn is not None:
            run_btn.setObjectName("Primary")
        responsive_toolbar(tb, self, 860)
        for a, key in ((self.run_act, "cmd:run"), (self.run_all_act, "cmd:run_all"), (self.cancel_act, "cmd:stop"),
                       (self.explain_act, "cmd:explain"), (self.english_act, "cmd:explain_english"),
                       (self.join_act, "cmd:visualize_join")):
            tag(a, key)
        tag(self.conn_combo, "cmd:connection")
        tag(self.db_combo, "cmd:database")
        tb.addSeparator()
        tb.addWidget(QLabel(" Max rows "))
        self.max_rows = QSpinBox()
        self.max_rows.setRange(1, 1_000_000)
        self.max_rows.setValue(5000)
        self.max_rows.setSingleStep(1000)
        tb.addWidget(self.max_rows)
        tag(self.max_rows, "cmd:max_rows")

        self.results = QTabWidget()
        self.results.setObjectName("Results")
        self.results.setDocumentMode(True)
        self.messages = QPlainTextEdit()
        self.messages.setReadOnly(True)
        self.messages.setFont(monospace_font(12))
        self.results.addTab(self.messages, "Messages")
        self.status = QLabel("Ready")
        self.status.setContentsMargins(6, 2, 6, 2)

        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.editor)
        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(0)
        bl.addWidget(self.results)
        bl.addWidget(self.status)
        split.addWidget(bottom)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setChildrenCollapsible(False)
        self.split = split
        self._user_split = False
        split.splitterMoved.connect(lambda *_: setattr(self, "_user_split", True))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(tb)
        lay.addWidget(split)
        self._fit_split(has_rows=False)

        follow_editor(self.editor)
        manager.connected.connect(lambda _: self.refresh_connections())
        manager.disconnected.connect(self._on_disconnected)
        self.refresh_connections(prefer=conn_id)
        self.editor.setFocus()

    # ---- title / files ----------------------------------------------------
    def title(self) -> str:
        name = os.path.basename(self.file_path) if self.file_path else self.untitled
        return name + (" •" if self.editor.document().isModified() else "")

    def is_dirty(self) -> bool:
        return self.editor.document().isModified() and bool(self.editor.toPlainText().strip())

    def save(self, save_as: bool = False) -> bool:
        path = self.file_path
        if save_as or not path:
            path, _ = QFileDialog.getSaveFileName(self, "Save SQL", (path or self.untitled) + ("" if path else ".sql"), "SQL files (*.sql);;All files (*)")
            if not path:
                return False
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.editor.toPlainText())
        self.file_path = path
        self.editor.document().setModified(False)
        self.title_changed.emit()
        return True

    def load_file(self, path: str) -> None:
        with open(path, encoding="utf-8") as f:
            self.editor.setPlainText(f.read())
        self.file_path = path
        self.editor.document().setModified(False)
        self.title_changed.emit()

    # ---- connection binding ------------------------------------------------
    def refresh_connections(self, prefer: str | None = None) -> None:
        keep = prefer or self.conn_id
        self.conn_combo.blockSignals(True)
        self.conn_combo.clear()
        for cid, s in self.manager.open_sessions():
            self.conn_combo.addItem(s.cfg.name, cid)
        self.conn_combo.blockSignals(False)
        idx = self.conn_combo.findData(keep) if keep else (0 if self.conn_combo.count() else -1)
        if idx < 0 and self.conn_combo.count():
            idx = 0
        self.conn_combo.setCurrentIndex(idx)
        if idx >= 0 and self.conn_combo.itemData(idx) != self.conn_id:
            self._conn_chosen(idx)
        self._update_enabled()

    def _on_disconnected(self, cid: str) -> None:
        if cid == self.conn_id:
            self.qconn = None
            self.conn_id = None
        self.refresh_connections()

    def _conn_chosen(self, idx: int) -> None:
        cid = self.conn_combo.itemData(idx) if idx >= 0 else None
        if cid == self.conn_id:
            return
        if self.qconn:
            old = self.qconn
            run_async(old.close)
        self.conn_id = cid
        self.qconn = None
        session = self.manager.session(cid) if cid else None
        if not session:
            self._update_enabled()
            return
        self.qconn = session.query_connection()
        drv = session.driver
        self.editor.set_dialect(drv.keywords(), drv.functions(), drv.hash_comments)
        self.db_combo.blockSignals(True)
        self.db_combo.clear()
        self.db_label.setVisible(drv.supports_databases)
        self.db_action.setVisible(drv.supports_databases)
        self.db_combo.blockSignals(False)
        self._applied_schema = None
        if drv.supports_databases:
            want = self.schema or session.cfg.database or None

            def fill(names, want=want, cid=cid):
                if cid != self.conn_id:
                    return
                self.db_combo.blockSignals(True)
                self.db_combo.clear()
                self.db_combo.addItem("(default)", None)
                for n in names:
                    self.db_combo.addItem(n, n)
                i = self.db_combo.findData(want) if want else 0
                self.db_combo.setCurrentIndex(max(i, 0))
                self.db_combo.blockSignals(False)
                self._db_chosen(self.db_combo.currentIndex())

            run_async(session.databases, fill, lambda m: self._log(f"Could not list databases: {m}"))
        else:
            self._load_catalog()
        self._update_enabled()

    def _db_chosen(self, idx: int) -> None:
        self.schema = self.db_combo.itemData(idx) if idx >= 0 else None
        self._load_catalog()

    def _load_catalog(self) -> None:
        session = self.manager.session(self.conn_id) if self.conn_id else None
        if not session:
            return
        schema, cid = self.schema, self.conn_id
        if session.driver.supports_databases and not schema:
            schema = session.cfg.database or None
            if not schema:
                self.editor.set_catalog({})
                return

        def done(cat):
            if cid == self.conn_id:
                self.editor.set_catalog(cat)

        run_async(lambda: session.completion_catalog(schema), done)

    def _update_enabled(self) -> None:
        has = self.qconn is not None
        self.run_act.setEnabled(has and not self.running)
        self.run_all_act.setEnabled(has and not self.running)
        self.explain_act.setEnabled(has and not self.running)
        self.english_act.setEnabled(has)
        self.join_act.setEnabled(has and not self.running)
        self.cancel_act.setEnabled(has and self.running)
        if not has:
            self.status.setText("Not connected. Open a connection in the sidebar.")

    # ---- running ----------------------------------------------------------
    def _selected_sql(self, all_: bool) -> str:
        cur = self.editor.textCursor()
        if not all_ and cur.hasSelection():
            return cur.selectedText().replace(" ", "\n")
        text = self.editor.toPlainText()
        if all_:
            return text
        session = self.manager.session(self.conn_id)
        st = statement_at(text, cur.position(), session.driver.hash_comments if session else False)
        return st.text if st else ""

    def explain(self) -> None:
        sql = self._selected_sql(False).strip()
        session = self.manager.session(self.conn_id) if self.conn_id else None
        if sql and session:
            self.run(sql_override=session.driver.explain_prefix + sql)

    def explain_english(self) -> None:
        session = self.manager.session(self.conn_id) if self.conn_id else None
        sql = self._selected_sql(False)
        if not session or not sql.strip():
            return
        parts = []
        for st in split_sql(sql, session.driver.hash_comments)[:10]:
            ex = explain_sql(st.text, session.cfg.driver)
            if ex is None:
                parts.append(f"<p><code>{html.escape(st.text[:120])}</code>: I couldn't parse this statement. Check the syntax.</p>")
                continue
            parts.append(_explanation_html(ex))
        HelpCenter.get().show_html("What this SQL does", "".join(parts) or "<p>Nothing to explain.</p>")

    def visualize_join(self) -> None:
        session = self.manager.session(self.conn_id) if self.conn_id else None
        sql = self._selected_sql(False)
        if not session or not sql.strip():
            return
        from dbide.ui.join_viz import JoinVisualizer

        dlg = JoinVisualizer(session, sql, self)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.show()

    def _beginner_check(self, sql: str, session) -> bool:
        """In beginner mode, explain each statement and confirm anything destructive."""
        risky = []
        explained = []
        for st in split_sql(sql, session.driver.hash_comments)[:20]:
            ex = explain_sql(st.text, session.cfg.driver)
            if ex is None:
                continue
            explained.append(ex)
            if ex.destructive:
                risky.append((st.text, ex))
        if explained:
            HelpCenter.get().show_html("What you just ran", "".join(_explanation_html(e) for e in explained[:5]))
        if not risky:
            return True
        text = "\n\n".join(f"{t[:200]}\n→ {_strip(e.summary)}\n" + "\n".join(_strip(w) for w in e.warnings) for t, e in risky)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Are you sure?")
        box.setText("This will change or delete a lot of data and can't be undone.")
        box.setInformativeText(text + "\n\n(Beginner mode asks before risky statements. Turn it off in the View menu.)")
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        return box.exec() == QMessageBox.StandardButton.Yes

    def run(self, all_: bool = False, sql_override: str | None = None) -> None:
        if self.running or not self.qconn:
            return
        sql = sql_override or self._selected_sql(all_)
        if not sql.strip():
            return
        session = self.manager.session(self.conn_id)
        if HelpCenter.get().beginner and not sql_override and not self._beginner_check(sql, session):
            return
        qconn, max_rows = self.qconn, self.max_rows.value()
        use_sql = None
        if session.driver.supports_databases and self.schema and self.schema != self._applied_schema:
            use_sql = f"USE {session.driver.quote(self.schema)}"
        self.running = True
        self._update_enabled()
        self.status.setText("Running…")
        conn_name = session.cfg.name

        def work():
            if use_sql:
                r = qconn.run(use_sql)
                if r and r[0].error:
                    return r
            return qconn.run(sql, max_rows=max_rows)

        def done(results):
            if use_sql and not (results and results[0].sql == use_sql and results[0].error):
                self._applied_schema = self.schema
            self.running = False
            self._update_enabled()
            self._show_results(results)
            for r in results:
                self.manager.history.add(conn_name, r.sql, r.error is None, r.elapsed, max(r.rowcount, 0))
            if any(_is_ddl(r.sql) and not r.error for r in results):
                self.schema_maybe_changed.emit(self.conn_id)
                self._load_catalog()

        def failed(msg):
            self.running = False
            self._update_enabled()
            self._log(msg)
            self.status.setText("Error")

        run_async(work, done, failed)

    def cancel(self) -> None:
        if self.qconn and self.running:
            q = self.qconn
            run_async(q.cancel, None, lambda m: self._log(f"Cancel failed: {m}"))
            self.status.setText("Cancelling…")

    def _log(self, line: str) -> None:
        self.messages.appendPlainText(line)

    def _show_results(self, results) -> None:
        while self.results.count() > 1:
            w = self.results.widget(1)
            self.results.removeTab(1)
            w.deleteLater()
        self.messages.clear()
        total_time = sum(r.elapsed for r in results)
        first_grid = None
        n = 0
        for r in results:
            short = " ".join(r.sql.split())
            short = short[:160] + ("…" if len(short) > 160 else "")
            if r.error:
                hint = friendly_error(r.error)
                self._log(f"✗ {short}\n  {r.error}\n" + (f"  💡 {_strip(hint)}\n" if hint else ""))
                if hint:
                    HelpCenter.get().show_html("What went wrong?", f"<p><code>{html.escape(r.error)}</code></p><p>{hint}</p>")
                continue
            if r.returns_rows:
                n += 1
                grid = GridView()
                grid.setSortingEnabled(True)
                grid.setModel(ResultModel(r.columns, list(r.rows), grid))
                grid.horizontalHeader().setSortIndicatorShown(False)
                grid.fit_columns()
                exp = QAction("Export Results…", grid)
                exp.triggered.connect(lambda _=False, res=r, g=grid: self._export(res))
                grid.extra_actions = [exp]
                label = f"Result {n}" if len(results) > 1 else "Result"
                self.results.addTab(grid, label)
                first_grid = first_grid or grid
                note = " (truncated: raise Max rows to see more)" if r.truncated else ""
                self._log(f"✓ {short}\n  {r.rowcount} row(s) in {r.elapsed * 1000:.1f} ms{note}\n")
            else:
                affected = f"{r.rowcount} row(s) affected" if r.rowcount >= 0 else "OK"
                self._log(f"✓ {short}\n  {affected} in {r.elapsed * 1000:.1f} ms\n")
        errors = [r for r in results if r.error]
        if errors:
            self.results.setCurrentWidget(self.messages)
            self.status.setText(f"Error after {len(results) - 1} statement(s): {errors[0].error.splitlines()[0][:200]}")
        else:
            if first_grid:
                self.results.setCurrentWidget(first_grid)
                self._fit_split(True, first_grid.model().rowCount())
                first_grid.fit_columns()
            else:
                self.results.setCurrentWidget(self.messages)
            if HelpCenter.get().beginner and any(" JOIN " in f" {r.sql.upper()} " for r in results if r.returns_rows):
                self._log("💡 Tip: put the cursor in the query and click Visualize JOIN (⌘J) to see how the rows were matched.")
            rows = sum(r.rowcount for r in results if r.returns_rows)
            self.status.setText(
                f"{len(results)} statement(s) · {rows} row(s) · {total_time * 1000:.1f} ms"
                if results else "No statements to run"
            )

    def _fit_split(self, has_rows: bool, rows: int = 0) -> None:
        """Give the results pane room that matches what came back, unless the user resized it."""
        if self._user_split:
            return
        h = max(self.split.height(), 400)
        if not has_rows:
            results = int(h * 0.28)
        else:
            row_h = self.fontMetrics().height() + 10
            wanted = 90 + row_h * min(rows, 30)
            results = max(int(h * 0.3), min(int(h * 0.62), wanted))
        self.split.setSizes([h - results, results])

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if not self._user_split and hasattr(self, "split"):
            grid = self.results.currentWidget()
            rows = grid.model().rowCount() if isinstance(grid, GridView) and grid.model() else 0
            self._fit_split(has_rows=rows > 0, rows=rows)

    def _export(self, res) -> None:
        choice = export_dialog(self, "results")
        if not choice:
            return
        path, fmt = choice
        session = self.manager.session(self.conn_id)
        try:
            n = export_rows(path, fmt, res.columns, res.rows, session.driver, table="results")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        self.status.setText(f"Exported {n} row(s) to {path}")

    def shutdown(self) -> None:
        if self.qconn:
            q = self.qconn
            self.qconn = None
            run_async(q.close)


def _is_ddl(sql: str) -> bool:
    head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
    return head in {"CREATE", "DROP", "ALTER", "RENAME", "TRUNCATE"}


def _strip(h: str) -> str:
    import re

    return html.unescape(re.sub(r"<[^>]+>", "", h))


def _explanation_html(ex) -> str:
    out = f"<p>{ex.summary}</p>"
    if ex.details:
        out += "<ul>" + "".join(f"<li>{d}</li>" for d in ex.details) + "</ul>"
    for w in ex.warnings:
        out += f"<p style='color:#cb2431'>{w}</p>"
    return out
