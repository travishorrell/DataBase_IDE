"""The Lessons panel: step-by-step tutorials that check your work."""
from __future__ import annotations

import html
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from dbide.core import lessons as L
from dbide.core import media
from dbide.core.roles import ColumnRole, role_store
from dbide.core.settings import settings
from dbide.ui.sql_editor import monospace_font
from dbide.ui.worker import run_async

if TYPE_CHECKING:
    from dbide.ui.main_window import MainWindow


class LessonPanel(QWidget):
    def __init__(self, main: "MainWindow"):
        super().__init__()
        self.main = main
        self.combo = QComboBox()
        for lsn in L.LESSONS:
            self.combo.addItem(lsn.title, lsn.id)
        self.combo.currentIndexChanged.connect(lambda _: self._goto(self._saved_step()))
        self.progress = QLabel()
        self.progress.setObjectName("Badge")
        self.body = QTextBrowser()
        self.body.setOpenLinks(False)
        self.sql = QPlainTextEdit()
        self.sql.setReadOnly(True)
        self.sql.setFont(monospace_font(12))
        self.sql.setMaximumHeight(130)
        self.result = QLabel()
        self.result.setWordWrap(True)
        self.result.setTextFormat(Qt.TextFormat.RichText)

        self.paste_btn = QPushButton("Put in Editor")
        self.paste_btn.setToolTip("Open the example SQL in a query tab so you can run it yourself (⌘↩)")
        self.paste_btn.clicked.connect(self.put_in_editor)
        self.do_btn = QPushButton("Do It for Me")
        self.do_btn.setToolTip("Run the example (and set roles) automatically")
        self.do_btn.clicked.connect(self.do_it)
        self.action_btn = QPushButton()
        self.action_btn.clicked.connect(self.run_action)
        self.check_btn = QPushButton("Check My Work")
        self.check_btn.setObjectName("Primary")
        self.check_btn.clicked.connect(self.check)
        self.back_btn = QPushButton("Back")
        self.back_btn.clicked.connect(lambda: self._goto(self.step_index - 1))
        self.next_btn = QPushButton("Next")
        self.next_btn.clicked.connect(lambda: self._goto(self.step_index + 1))
        reset = QPushButton("Reset Practice DB")
        reset.clicked.connect(self.reset_practice)

        top = QHBoxLayout()
        top.addWidget(self.combo, 1)
        top.addWidget(self.progress)
        row1 = QHBoxLayout()
        row1.addWidget(self.paste_btn)
        row1.addWidget(self.do_btn)
        row1.addWidget(self.action_btn)
        row2 = QHBoxLayout()
        row2.addWidget(self.back_btn)
        row2.addWidget(self.check_btn)
        row2.addWidget(self.next_btn)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 4, 10, 10)
        lay.setSpacing(8)
        self.body.setFrameShape(QTextBrowser.Shape.NoFrame)
        lay.addLayout(top)
        lay.addWidget(self.body, 1)
        lay.addWidget(self.sql)
        lay.addLayout(row1)
        lay.addWidget(self.result)
        lay.addLayout(row2)
        lay.addWidget(reset)
        self.step_index = 0
        self._goto(self._saved_step())

    # ---- navigation -------------------------------------------------------
    @property
    def lesson(self) -> L.Lesson:
        return L.lesson(self.combo.currentData())

    @property
    def step(self) -> L.Step:
        return self.lesson.steps[self.step_index]

    def _saved_step(self) -> int:
        return int((settings().get("lesson_progress") or {}).get(self.combo.currentData(), 0))

    def _goto(self, i: int) -> None:
        steps = self.lesson.steps
        if i >= len(steps):
            idx = self.combo.currentIndex()
            if idx + 1 < self.combo.count():
                self.combo.setCurrentIndex(idx + 1)  # triggers _goto for the next lesson
                return
            i = len(steps) - 1
            self.result.setText("🎉 You've finished every lesson! Try building your own database next.")
        self.step_index = max(0, i)
        prog = dict(settings().get("lesson_progress") or {})
        prog[self.lesson.id] = self.step_index
        settings().set("lesson_progress", prog)
        st = self.step
        self.progress.setText(f"Step {self.step_index + 1} / {len(steps)}")
        self.body.setHtml(f"<h3>{html.escape(st.title)}</h3>{st.body}")
        self.sql.setPlainText(st.sql)
        self.sql.setVisible(bool(st.sql))
        self.paste_btn.setVisible(bool(st.sql))
        self.do_btn.setVisible(bool(st.sql or st.roles or st.sample_media))
        self.action_btn.setVisible(bool(st.action))
        self.action_btn.setText(st.action_label or "Open")
        self.check_btn.setVisible(st.check is not None)
        self.back_btn.setEnabled(self.step_index > 0)
        if not (i >= len(steps)):
            self.result.setText("")

    # ---- practice connection -------------------------------------------------
    def _with_practice(self, fn) -> None:
        self.main.ensure_practice(lambda: fn(self.main.manager.session(L.PRACTICE_ID)))

    def put_in_editor(self) -> None:
        self._with_practice(lambda s: self.main.new_query(L.PRACTICE_ID, None, self.step.sql + "\n"))

    def do_it(self) -> None:
        st = self.step

        def go(session):
            sql = st.sql
            name = session.cfg.name

            def work():
                results = []
                if sql:
                    qc = session.query_connection()
                    try:
                        results = qc.run(sql)
                    finally:
                        qc.close()
                for key, code in st.roles.items():
                    t, c = key.split(".")
                    roles = role_store().get(L.PRACTICE_ID, None, t)
                    roles[c] = ColumnRole.from_code(code)
                    role_store().set_table(L.PRACTICE_ID, None, t, roles)
                if st.sample_media:
                    png = L.sample_png()
                    tmp = L.practice_path().parent / "sample_picture.png"
                    tmp.write_bytes(png)
                    stored = media.store_file(str(tmp), session.cfg)
                    with session.engine.begin() as conn:
                        conn.exec_driver_sql(
                            f'INSERT INTO "{st.sample_media}" (caption, image, image_path) VALUES (?, ?, ?)',
                            ("Sample picture", png, stored),
                        )
                return results

            def done(results):
                for r in results:
                    self.main.manager.history.add(name, r.sql, r.error is None, r.elapsed, max(r.rowcount, 0))
                errors = [r.error for r in results if r.error]
                self.main.tree.refresh(L.PRACTICE_ID)
                if errors:
                    from dbide.core.knowledge import friendly_error
                    from dbide.ui.help_panel import HelpCenter

                    hint = friendly_error(errors[0])
                    if hint:
                        HelpCenter.get().show_html("What went wrong?", f"<p><code>{html.escape(errors[0])}</code></p><p>{hint}</p>")
                self.check()

            run_async(work, done, lambda m: self.result.setText(f"Error: {html.escape(m)}"))

        self._with_practice(go)

    def run_action(self) -> None:
        act = self.step.action
        m = self.main

        def go(session):
            if act == "open_erd":
                m.open_erd(L.PRACTICE_ID, None)
            elif act.startswith("open_designer:"):
                table = act.split(":", 1)[1] or None
                if table and table not in session.tables():
                    table = None
                m.design_table(L.PRACTICE_ID, None, table)
            elif act.startswith("open_data:"):
                table = act.split(":", 1)[1]
                if table in session.tables():
                    m.open_data(L.PRACTICE_ID, None, table)
                else:
                    self.result.setText(f"Create the <b>{table}</b> table first (previous step).")
            elif act == "visualize_join":
                m.visualize_join(L.PRACTICE_ID, self.step.sql)
            elif act == "password_tool":
                m.password_tool(L.PRACTICE_ID)

        self._with_practice(go)

    def check(self) -> None:
        st = self.step
        if st.check is None:
            return

        def go(session):
            recent = [(sql, bool(ok)) for _ts, conn, sql, ok, *_ in self.main.manager.history.recent(limit=40)
                      if conn == session.cfg.name]
            ctx = L.Context(recent)
            run_async(lambda: st.check(session, ctx), self._checked, lambda m: self.result.setText(html.escape(m)))

        self._with_practice(go)

    def _checked(self, result) -> None:
        ok, msg = result
        self.result.setText(msg + ("<br><b>Click Next ▶ to continue.</b>" if ok else ""))
        self.next_btn.setDefault(ok)

    def reset_practice(self) -> None:
        if QMessageBox.question(self, "Reset practice database",
                                "Delete the practice database and its media folder and start the lessons over?") != QMessageBox.StandardButton.Yes:
            return
        self.main.reset_practice()
        settings().set("lesson_progress", {})
        self.combo.setCurrentIndex(0)
        self._goto(0)
