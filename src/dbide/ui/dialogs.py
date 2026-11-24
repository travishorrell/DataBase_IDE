"""Smaller dialogs: query history and data import."""
from __future__ import annotations

import datetime as dt
import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from dbide.core.history import QueryHistory
from dbide.core.roles import prepare_value, role_store
from dbide.core.session import DatabaseSession
from dbide.core.transfer import read_csv, read_json
from dbide.ui.worker import run_async


class HistoryDialog(QDialog):
    open_sql = Signal(str)

    def __init__(self, history: QueryHistory, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Query History")
        self.resize(900, 520)
        self.history = history
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search history…")
        self.search.textChanged.connect(self.reload)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["When", "Connection", "SQL", "Rows", "ms"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.doubleClicked.connect(self._open)
        open_btn = QPushButton("Open in New Query")
        open_btn.clicked.connect(self._open)
        clear_btn = QPushButton("Clear History")
        clear_btn.clicked.connect(self._clear)
        btns = QHBoxLayout()
        btns.addWidget(clear_btn)
        btns.addStretch()
        btns.addWidget(open_btn)
        lay = QVBoxLayout(self)
        lay.addWidget(self.search)
        lay.addWidget(self.table)
        lay.addLayout(btns)
        self.reload()

    def reload(self) -> None:
        rows = self.history.recent(self.search.text())
        self.table.setRowCount(len(rows))
        for r, (ts, conn, sql, ok, elapsed, nrows) in enumerate(rows):
            when = dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
            vals = [when, conn or "", " ".join(sql.split())[:300], str(nrows or 0), f"{(elapsed or 0) * 1000:.1f}"]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if c == 2:
                    it.setData(Qt.ItemDataRole.UserRole, sql)
                    it.setToolTip(sql[:2000])
                if not ok:
                    it.setForeground(Qt.GlobalColor.red)
                self.table.setItem(r, c, it)
        self.table.resizeColumnToContents(0)
        self.table.resizeColumnToContents(1)

    def _open(self, *_):
        r = self.table.currentRow()
        if r >= 0:
            self.open_sql.emit(self.table.item(r, 2).data(Qt.ItemDataRole.UserRole))
            self.accept()

    def _clear(self):
        if QMessageBox.question(self, "Clear history", "Delete all query history?") == QMessageBox.StandardButton.Yes:
            self.history.clear()
            self.reload()


class ImportDialog(QDialog):
    """Import CSV or JSON rows into an existing table, mapping file columns to table columns."""

    def __init__(self, session: DatabaseSession, table: str, schema: str | None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Import into {table}")
        self.resize(640, 480)
        self.session, self.table_name, self.schema = session, table, schema
        self.file_cols: list[str] = []
        self.rows: list[list] = []
        self.table_cols = [c["name"] for c in session.columns(table, schema)]

        self.path = QLineEdit()
        self.path.setReadOnly(True)
        browse = QPushButton("Choose File…")
        browse.clicked.connect(self._choose)
        top = QHBoxLayout()
        top.addWidget(self.path, 1)
        top.addWidget(browse)
        self.empty_null = QCheckBox("Treat empty CSV values as NULL")
        self.empty_null.setChecked(True)
        self.empty_null.toggled.connect(lambda _: self._load())
        self.info = QLabel("Choose a .csv or .json file. JSON must be a list of objects.")
        self.info.setWordWrap(True)
        self.mapping = QTableWidget(0, 2)
        self.mapping.setHorizontalHeaderLabels(["File column", "Table column"])
        self.mapping.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.mapping.verticalHeader().setVisible(False)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Import")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self.buttons.accepted.connect(self._import)
        self.buttons.rejected.connect(self.reject)
        form = QFormLayout()
        form.addRow("File", top)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self.empty_null)
        lay.addWidget(self.info)
        lay.addWidget(self.mapping, 1)
        lay.addWidget(self.buttons)

    def _choose(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import file", "", "Data files (*.csv *.tsv *.txt *.json);;All files (*)")
        if path:
            self.path.setText(path)
            self._load()

    def _load(self):
        path = self.path.text()
        if not path:
            return
        try:
            if os.path.splitext(path)[1].lower() == ".json":
                self.file_cols, self.rows = read_json(path)
            else:
                self.file_cols, self.rows = read_csv(path, self.empty_null.isChecked())
        except Exception as exc:  # noqa: BLE001
            self.info.setText(f"Could not read file: {exc}")
            return
        lower = {c.lower(): c for c in self.table_cols}
        self.mapping.setRowCount(len(self.file_cols))
        for r, fc in enumerate(self.file_cols):
            self.mapping.setItem(r, 0, QTableWidgetItem(fc))
            cb = QComboBox()
            cb.addItem("(skip)", None)
            for tc in self.table_cols:
                cb.addItem(tc, tc)
            match = lower.get(fc.lower().strip())
            cb.setCurrentIndex(cb.findData(match) if match else 0)
            self.mapping.setCellWidget(r, 1, cb)
        self.info.setText(f"{len(self.rows)} row(s) found. Check the column mapping, then Import.")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(self.rows))

    def _import(self):
        pairs = []
        for r in range(self.mapping.rowCount()):
            target = self.mapping.cellWidget(r, 1).currentData()
            if target:
                pairs.append((r, target))
        targets = [t for _, t in pairs]
        if not pairs:
            QMessageBox.warning(self, "Nothing to import", "Map at least one column.")
            return
        if len(set(targets)) != len(targets):
            QMessageBox.warning(self, "Duplicate mapping", "Each table column can only be mapped once.")
            return
        data = [[row[i] for i, _ in pairs] for row in self.rows]
        self.buttons.setEnabled(False)
        s = self.session
        roles = role_store().get(s.cfg.id, self.schema, self.table_name)
        secured = [t for t in targets if t in roles and roles[t].kind in ("password", "encrypted")]
        self.info.setText("Importing…" + (f" (hashing/encrypting {', '.join(secured)}: this can take a while)" if secured else ""))

        def work():
            rows = data
            if secured:
                rows = [[prepare_value(roles.get(t), v, s.cfg.id) for t, v in zip(targets, r)] for r in data]
            return s.insert_rows(self.table_name, self.schema, targets, rows)

        def done(n):
            QMessageBox.information(self, "Import complete", f"Imported {n} row(s) into {self.table_name}.")
            self.accept()

        def failed(m):
            self.buttons.setEnabled(True)
            self.info.setText("Import failed; no rows were added.")
            QMessageBox.critical(self, "Import failed", f"No rows were added.\n\n{m}")

        run_async(work, done, failed)
