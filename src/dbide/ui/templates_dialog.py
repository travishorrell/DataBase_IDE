"""Template gallery: open a sample project in the Playground or create it for real."""
from __future__ import annotations

import html
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from dbide.core.templates import TEMPLATES, Template
from dbide.ui.theme import ThemeManager


class TemplateGallery(QDialog):
    """Pick a template. result: ('playground', key) or ('create', key, options)."""

    def __init__(self, manager, parent=None, preselect: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("Start from a Template")
        self.resize(900, 600)
        self.manager = manager
        self.choice = None
        tm = ThemeManager.get()
        self.list = QListWidget()
        self.list.setIconSize(QSize(28, 28))
        self.list.setSpacing(4)
        for key, t in TEMPLATES.items():
            it = QListWidgetItem(tm.icon(t.icon, "accent"), f"{t.name}\n{len(t.tables)} tables")
            it.setData(Qt.ItemDataRole.UserRole, key)
            self.list.addItem(it)
        self.list.currentItemChanged.connect(lambda cur, _p: cur and self._show(cur.data(Qt.ItemDataRole.UserRole)))
        self.preview = QTextBrowser()
        self.preview.setFrameShape(QTextBrowser.Shape.NoFrame)
        split = QSplitter()
        split.addWidget(self.list)
        split.addWidget(self.preview)
        split.setSizes([260, 640])
        self.play_btn = QPushButton("Open in Playground")
        self.play_btn.setToolTip("A safe copy with sample data. Experiment freely; everything can be undone.")
        self.play_btn.clicked.connect(self._playground)
        self.create_btn = QPushButton("Create Project…")
        self.create_btn.setObjectName("Primary")
        self.create_btn.clicked.connect(self._create)
        cancel = QPushButton("Cancel")
        cancel.setAutoDefault(False)
        self.play_btn.setAutoDefault(False)
        from dbide.ui.help_panel import HelpCenter

        self.play_btn.setVisible(HelpCenter.get().beginner)  # the playground is part of Learning mode
        self.create_btn.setDefault(True)
        cancel.clicked.connect(self.reject)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(cancel)
        row.addWidget(self.play_btn)
        row.addWidget(self.create_btn)
        lay = QVBoxLayout(self)
        lay.addWidget(split, 1)
        lay.addLayout(row)
        idx = list(TEMPLATES).index(preselect) if preselect in TEMPLATES else 0
        self.list.setCurrentRow(idx)

    @property
    def key(self) -> str:
        return self.list.currentItem().data(Qt.ItemDataRole.UserRole)

    def _show(self, key: str) -> None:
        t: Template = TEMPLATES[key]
        rel = "".join(f"<li><code>{html.escape(c)}.{html.escape(col)}</code> → <code>{html.escape(p)}</code></li>"
                      for c, col, p in t.relationships())
        tables = "".join(f"<li><b>{html.escape(td.name)}</b>: {html.escape(', '.join(c.name for c in td.columns))}</li>" for td in t.tables)
        learn = "".join(f"<li>{html.escape(x)}</li>" for x in t.learn)
        tries = "".join(f"<li>{html.escape(x.text.splitlines()[0])}</li>" for x in t.try_this if not x.sql)
        self.preview.setHtml(
            f"<h2>{html.escape(t.name)}</h2><p>{html.escape(t.description)}</p>"
            f"<p><b>Tables</b></p><ul>{tables}</ul><p><b>Relationships</b></p><ul>{rel}</ul>"
            f"<p><b>Shows you</b></p><ul>{learn}</ul><p><b>Questions to try</b></p><ul>{tries}</ul>")

    def _playground(self) -> None:
        self.choice = ("playground", self.key)
        self.accept()

    def _create(self) -> None:
        dlg = CreateProjectDialog(self.manager, TEMPLATES[self.key], self)
        if dlg.exec():
            self.choice = ("create", self.key, dlg.options())
            self.accept()


class CreateProjectDialog(QDialog):
    def __init__(self, manager, tpl: Template, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Create “{tpl.name}” project")
        self.setMinimumWidth(560)
        self.manager, self.tpl = manager, tpl
        self.new_file = QRadioButton("A new SQLite database file")
        self.existing = QRadioButton("Inside a database I'm connected to")
        grp = QButtonGroup(self)
        grp.addButton(self.new_file)
        grp.addButton(self.existing)
        self.path = QLineEdit(str(Path.home() / "Documents" / f"{tpl.key}.sqlite"))
        browse = QPushButton("Choose…")
        browse.clicked.connect(self._browse)
        prow = QHBoxLayout()
        prow.addWidget(self.path, 1)
        prow.addWidget(browse)
        self.path_box = QWidget()
        self.path_box.setLayout(prow)
        self.conn = QComboBox()
        for cid, s in manager.open_sessions():
            if not cid.startswith("playground:"):
                self.conn.addItem(s.cfg.name, cid)
        self.db = QLineEdit()
        self.db.setPlaceholderText("database / schema")
        crow = QHBoxLayout()
        crow.addWidget(self.conn, 1)
        crow.addWidget(self.db)
        self.conn_box = QWidget()
        self.conn_box.setLayout(crow)
        self.sample = QCheckBox("Include sample data (you can delete it later)")
        self.sample.setChecked(True)
        self.existing.setEnabled(self.conn.count() > 0)
        self.new_file.setChecked(True)
        self.new_file.toggled.connect(self._sync)
        self.conn.currentIndexChanged.connect(self._sync)
        form = QFormLayout()
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Where should the tables be created?"))
        lay.addWidget(self.new_file)
        lay.addWidget(self.path_box)
        lay.addWidget(self.existing)
        lay.addWidget(self.conn_box)
        lay.addLayout(form)
        lay.addWidget(self.sample)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Create")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._sync()

    def _sync(self, *_):
        self.path_box.setEnabled(self.new_file.isChecked())
        self.conn_box.setEnabled(self.existing.isChecked())
        s = self.manager.session(self.conn.currentData()) if self.conn.count() else None
        self.db.setVisible(bool(s and s.driver.supports_databases))
        if s and s.driver.supports_databases and not self.db.text():
            self.db.setText(s.driver.default_schema(s.cfg) or s.driver.tree_default_schema or "")

    def _browse(self):
        p, _ = QFileDialog.getSaveFileName(self, "New database file", self.path.text(), "SQLite (*.sqlite *.db)")
        if p:
            self.path.setText(p)

    def options(self) -> dict:
        if self.new_file.isChecked():
            return {"target": "file", "path": self.path.text().strip(), "sample": self.sample.isChecked()}
        return {"target": "conn", "conn_id": self.conn.currentData(), "schema": self.db.text().strip() or None,
                "sample": self.sample.isChecked()}
