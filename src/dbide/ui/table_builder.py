"""Visual CREATE / ALTER TABLE designer with live SQL preview."""
from __future__ import annotations

import shiboken6
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from dbide.core.roles import ColumnRole, from_smart, looks_like_password, role_choices, role_store, suggested_type, type_choices
from dbide.core.schema_model import FK_ACTIONS, ColumnDef, ForeignKeyDef, IndexDef, TableDef
from dbide.ui.connections import ConnectionManager
from dbide.ui.help_panel import HelpCenter, tag
from dbide.ui.sql_editor import SqlEditor
from dbide.ui.worker import run_async

ORIG_ROLE = Qt.ItemDataRole.UserRole + 1
CONSTRAINT_ROLE = Qt.ItemDataRole.UserRole + 2


def open_dropdown(view, index) -> bool:
    """Start editing `index`, first finishing any other cell being edited.

    Qt refuses to open a second editor, and the old editor's focus-out arrives a
    moment later and would close the new dropdown again, so when another cell is
    being edited we finish it and open the dropdown on the next event-loop turn.
    """
    if view.state() == view.State.EditingState:
        # open cell editors are visible children of the viewport (works even if the window isn't focused)
        for w in [c for c in view.viewport().children() if isinstance(c, QWidget) and c.isVisible()]:
            view.commitData(w)
            view.closeEditor(w, QStyledItemDelegate.EndEditHint.NoHint)
        view.setCurrentIndex(index)
        QTimer.singleShot(0, lambda: shiboken6.isValid(view) and view.edit(index))
        return True
    view.setCurrentIndex(index)
    view.edit(index)
    return view.state() == view.State.EditingState


class CellCombo(QComboBox):
    """Dropdown used inside table cells.

    When its list closes, focus returns to the combo itself, not the table, so the
    cell editor isn't treated as finished and doesn't vanish before you choose.
    """

    picked = Signal()  # a real choice by the user (not the spurious 'activated' Qt fires while opening)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._opened_at = 0.0
        self.activated.connect(self._maybe_picked)

    def showPopup(self) -> None:
        import time

        self._opened_at = time.monotonic()
        super().showPopup()

    def _maybe_picked(self, _index: int) -> None:
        import time

        # Qt can emit 'activated' from inside showPopup(); only accept choices made after the list was open
        if self._opened_at and time.monotonic() - self._opened_at > 0.15:
            self.picked.emit()

    def keyPressEvent(self, e) -> None:
        super().keyPressEvent(e)
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.picked.emit()

    def hidePopup(self) -> None:
        super().hidePopup()
        if shiboken6.isValid(self) and self.isVisible():
            self.setFocus()


def _popup_soon(cb: QComboBox) -> None:
    """Open the list as soon as the editor appears, so one click is enough."""
    QTimer.singleShot(0, lambda: shiboken6.isValid(cb) and cb.isVisible() and cb.showPopup())


class _ComboDelegate(QStyledItemDelegate):
    """A dropdown editor; '' entries become separators. Choosing an item commits immediately."""

    def __init__(self, items: list[str], editable: bool, parent=None, on_pick=None):
        super().__init__(parent)
        self.items, self.editable, self.on_pick = items, editable, on_pick

    def createEditor(self, parent, option, index):
        cb = CellCombo(parent)
        cb.setEditable(self.editable)
        cb.setMaxVisibleItems(20)
        for it in self.items:
            if it == "":
                cb.insertSeparator(cb.count())
            else:
                cb.addItem(it)
        cb.picked.connect(lambda cb=cb: (self.commitData.emit(cb), self.closeEditor.emit(cb)))
        _popup_soon(cb)
        return cb

    def setEditorData(self, editor, index):
        editor.setCurrentText(index.data() or "")

    def setModelData(self, editor, model, index):
        text = editor.currentText()
        if self.on_pick and self.on_pick(index.row(), text):
            return  # a special type: the hook filled in the type and role
        model.setData(index, text)


class _RoleDelegate(QStyledItemDelegate):
    """Combo of column roles; stores the role code in UserRole and shows its label."""

    def createEditor(self, parent, option, index):
        cb = CellCombo(parent)
        for label, code in role_choices():
            cb.addItem(label, code)
        cb.picked.connect(lambda cb=cb: (self.commitData.emit(cb), self.closeEditor.emit(cb)))
        _popup_soon(cb)
        return cb

    def setEditorData(self, editor, index):
        editor.setCurrentIndex(max(0, editor.findData(index.data(Qt.ItemDataRole.UserRole) or "")))

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentData(), Qt.ItemDataRole.UserRole)
        model.setData(index, "" if not editor.currentData() else editor.currentText())


def _check_item(checked: bool) -> QTableWidgetItem:
    it = QTableWidgetItem()
    it.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
    it.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
    return it


def _checked(table: QTableWidget, r: int, c: int) -> bool:
    it = table.item(r, c)
    return bool(it and it.checkState() == Qt.CheckState.Checked)


def _text(table: QTableWidget, r: int, c: int) -> str:
    it = table.item(r, c)
    return it.text().strip() if it else ""


def _split(s: str) -> list[str]:
    return [p.strip() for p in s.split(",") if p.strip()]


class _Grid(QWidget):
    """A QTableWidget with add / remove / move buttons."""

    def __init__(self, headers: list[str], on_add):
        super().__init__()
        self.table = QTableWidget(0, len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        btns = QHBoxLayout()
        for label, slot in (("Add", on_add), ("Remove", self.remove), ("Move Up", lambda: self.move(-1)), ("Move Down", lambda: self.move(1))):
            b = QPushButton(label)
            b.clicked.connect(slot)
            btns.addWidget(b)
        btns.addStretch()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addWidget(self.table)
        lay.addLayout(btns)

    def remove(self):
        for r in sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(r)

    def move(self, delta: int):
        r = self.table.currentRow()
        t = r + delta
        if r < 0 or t < 0 or t >= self.table.rowCount():
            return
        for c in range(self.table.columnCount()):
            a, b = self.table.takeItem(r, c), self.table.takeItem(t, c)
            self.table.setItem(r, c, b)
            self.table.setItem(t, c, a)
        self.table.setCurrentCell(t, self.table.currentColumn())


class TableBuilderTab(QWidget):
    applied = Signal(str, object)  # conn id, schema

    C_NAME, C_TYPE, C_NOTNULL, C_PK, C_AI, C_DEFAULT, C_COMMENT, C_ROLE = range(8)
    FIELD_HELP = {0: "field:name", 1: "field:type", 2: "field:notnull", 3: "field:pk", 4: "field:ai",
                  5: "field:default", 6: "field:comment", 7: "field:role"}

    def __init__(self, manager: ConnectionManager, conn_id: str, schema: str | None, table: str | None = None):
        super().__init__()
        self.manager, self.conn_id, self.schema = manager, conn_id, schema
        self.original: TableDef | None = None
        self._dirty = False
        drv = self.session.driver
        self.driver = drv

        self.name_edit = QLineEdit()
        self.name_edit.textChanged.connect(self._changed)
        form = QFormLayout()
        form.addRow("Table name", self.name_edit)
        self.engine_edit = QComboBox()
        self.engine_edit.setEditable(True)
        self.engine_edit.addItems(["", "InnoDB", "MyISAM", "MEMORY", "ARCHIVE"])
        self.engine_edit.currentTextChanged.connect(self._changed)
        self.table_comment = QLineEdit()
        self.table_comment.textChanged.connect(self._changed)
        if drv.key == "mysql":
            form.addRow("Engine", self.engine_edit)
            form.addRow("Comment", self.table_comment)

        self.cols = _Grid(["Name", "Type", "Not Null", "PK", "Auto Inc", "Default", "Comment", "Role"], self.add_column)
        self.cols.table.setItemDelegateForColumn(self.C_TYPE, _ComboDelegate(type_choices(drv.common_types), True, self, self._pick_type))
        self.cols.table.setItemDelegateForColumn(self.C_ROLE, _RoleDelegate(self))
        # one click on a Type or Role cell opens its dropdown
        self.cols.table.clicked.connect(lambda ix: ix.column() in (self.C_TYPE, self.C_ROLE) and open_dropdown(self.cols.table, ix))
        if drv.key != "mysql":
            self.cols.table.setColumnHidden(self.C_COMMENT, True)
        self.cols.table.itemChanged.connect(self._changed)
        self.cols.table.itemChanged.connect(self._column_item_changed)
        self.cols.table.currentCellChanged.connect(lambda r, c, *_: HelpCenter.get().show_topic(self.FIELD_HELP.get(c)))
        tag(self.cols.table, "field:pk")

        self.idx = _Grid(["Name", "Columns (comma-separated)", "Unique"], self.add_index)
        self.idx.table.itemChanged.connect(self._changed)
        tag(self.idx, "field:index")
        self.fks = _Grid(["Name", "Columns", "References Table", "Referenced Columns", "On Delete", "On Update"], self.add_fk)
        tag(self.fks, "field:fk")
        self.fks.table.currentCellChanged.connect(lambda r, c, *_: self._explain_fk(r, c))
        self.fks.table.itemChanged.connect(lambda it: self._explain_fk(it.row(), it.column()))
        action_delegate = _ComboDelegate(FK_ACTIONS, False, self)
        self.fks.table.clicked.connect(lambda ix: ix.column() in (4, 5) and open_dropdown(self.fks.table, ix))
        self.fks.table.setItemDelegateForColumn(4, action_delegate)
        self.fks.table.setItemDelegateForColumn(5, action_delegate)
        self.fks.table.itemChanged.connect(self._changed)

        self.preview = SqlEditor()
        self.preview.setReadOnly(True)
        self.preview.set_dialect(drv.keywords(), drv.functions(), drv.hash_comments)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("SubTabs")
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self.cols, "Columns")
        self.tabs.addTab(self.idx, "Indexes")
        self.tabs.addTab(self.fks, "Foreign Keys")
        from dbide.ui.features import Gate

        Gate.get().bind_tab(self.tabs, self.idx, "table_advanced")
        Gate.get().bind_tab(self.tabs, self.fks, "table_advanced")
        self._gate_role_column()
        Gate.get().on(Gate.get().changed, self, lambda _l: self._gate_role_column())
        self.tabs.addTab(self.preview, "SQL Preview")
        self.tabs.currentChanged.connect(lambda i: self.tabs.widget(i) is self.preview and self.update_preview())

        self._roles: dict[str, ColumnRole] = {}
        self.apply_btn = QPushButton("Create Table")
        self.apply_btn.setObjectName("Primary")
        tag(self.apply_btn, "cmd:builder_apply")
        self.apply_btn.setDefault(True)
        self.apply_btn.clicked.connect(self.apply)
        self.reset_btn = QPushButton("Revert")
        self.reset_btn.clicked.connect(self.revert)
        self.msg = QLabel("")
        self.msg.setWordWrap(True)
        bottom = QHBoxLayout()
        bottom.addWidget(self.msg, 1)
        bottom.addWidget(self.reset_btn)
        bottom.addWidget(self.apply_btn)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(10)
        lay.addLayout(form)
        lay.addWidget(self.tabs, 1)
        lay.addLayout(bottom)

        if table:
            self.msg.setText("Loading table definition…")
            self.setEnabled(False)
            s = self.session
            run_async(lambda: s.table_def(table, schema), self._loaded, self._load_failed)
        else:
            td = TableDef(name="new_table", schema=schema)
            if drv.key == "mysql":
                td.columns = [ColumnDef("id", "INT", nullable=False, primary_key=True, auto_increment=True)]
                td.options = {"engine": "InnoDB"}
            else:
                td.columns = [ColumnDef("id", "INTEGER", nullable=False, primary_key=True, auto_increment=True)]
            self._fill(td)
            self.name_edit.selectAll()
            self.name_edit.setFocus()

    @property
    def session(self):
        return self.manager.session(self.conn_id)

    def title(self) -> str:
        base = f"Design: {self.original.name}" if self.original else "New Table"
        return base + (" •" if self._dirty else "")

    def is_dirty(self) -> bool:
        return self._dirty

    def _changed(self, *_):
        if not self._filling:
            self._dirty = True
            self._title_refresh()

    _filling = False

    def _title_refresh(self):
        w = self.parent()
        while w is not None and not hasattr(w, "refresh_titles"):
            w = w.parent()
        if w is not None:
            w.refresh_titles()

    def _loaded(self, td: TableDef) -> None:
        self.setEnabled(True)
        self.original = td
        self._roles = role_store().get(self.conn_id, self.schema, td.name)
        self.apply_btn.setText("Apply Changes")
        self.msg.setText("")
        self._fill(td)
        self._dirty = False
        self._title_refresh()

    def _load_failed(self, msg: str) -> None:
        self.setEnabled(True)
        self.msg.setText(f"Could not load table: {msg}")

    # ---- form <-> TableDef -------------------------------------------------
    def _fill(self, td: TableDef) -> None:
        self._filling = True
        self.name_edit.setText(td.name)
        self.engine_edit.setCurrentText(td.options.get("engine", ""))
        self.table_comment.setText(td.options.get("comment", ""))
        t = self.cols.table
        t.setRowCount(0)
        for c in td.columns:
            self._add_column_row(c)
        t.resizeColumnsToContents()
        t.setColumnWidth(self.C_NAME, max(170, t.columnWidth(self.C_NAME)))
        t.setColumnWidth(self.C_TYPE, max(150, t.columnWidth(self.C_TYPE)))
        t.setColumnWidth(self.C_DEFAULT, max(120, t.columnWidth(self.C_DEFAULT)))
        self.idx.table.setRowCount(0)
        for ix in td.indexes:
            self._add_index_row(ix)
        self.fks.table.setRowCount(0)
        for fk in td.foreign_keys:
            self._add_fk_row(fk)
        self._filling = False
        self.update_preview()

    def _add_column_row(self, c: ColumnDef) -> None:
        t = self.cols.table
        r = t.rowCount()
        t.insertRow(r)
        name = QTableWidgetItem(c.name)
        name.setData(ORIG_ROLE, c.original_name)
        t.setItem(r, self.C_NAME, name)
        t.setItem(r, self.C_TYPE, QTableWidgetItem(c.type))
        t.setItem(r, self.C_NOTNULL, _check_item(not c.nullable))
        t.setItem(r, self.C_PK, _check_item(c.primary_key))
        t.setItem(r, self.C_AI, _check_item(c.auto_increment))
        t.setItem(r, self.C_DEFAULT, QTableWidgetItem("" if c.default is None else c.default))
        t.setItem(r, self.C_COMMENT, QTableWidgetItem(c.comment))
        role = self._roles.get(c.original_name or c.name) if hasattr(self, "_roles") else None
        ri = QTableWidgetItem(role.label() if role else "")
        ri.setData(Qt.ItemDataRole.UserRole, role.code() if role else "")
        t.setItem(r, self.C_ROLE, ri)

    def _add_index_row(self, ix: IndexDef) -> None:
        t = self.idx.table
        r = t.rowCount()
        t.insertRow(r)
        name = QTableWidgetItem(ix.name)
        name.setData(ORIG_ROLE, ix.original_name)
        name.setData(CONSTRAINT_ROLE, ix.constraint)
        if ix.constraint:
            name.setToolTip("Inline UNIQUE constraint from the table definition")
        t.setItem(r, 0, name)
        t.setItem(r, 1, QTableWidgetItem(", ".join(ix.columns)))
        t.setItem(r, 2, _check_item(ix.unique))

    def _add_fk_row(self, fk: ForeignKeyDef) -> None:
        t = self.fks.table
        r = t.rowCount()
        t.insertRow(r)
        name = QTableWidgetItem(fk.name)
        name.setData(ORIG_ROLE, fk.original_name)
        t.setItem(r, 0, name)
        t.setItem(r, 1, QTableWidgetItem(", ".join(fk.columns)))
        t.setItem(r, 2, QTableWidgetItem(fk.ref_table))
        t.setItem(r, 3, QTableWidgetItem(", ".join(fk.ref_columns)))
        t.setItem(r, 4, QTableWidgetItem(fk.on_delete))
        t.setItem(r, 5, QTableWidgetItem(fk.on_update))

    def add_column(self) -> None:
        n = self.cols.table.rowCount() + 1
        default_type = {"mysql": "VARCHAR(255)", "postgresql": "VARCHAR(255)", "mssql": "NVARCHAR(255)"}.get(self.driver.key, "TEXT")
        self._add_column_row(ColumnDef(f"column_{n}", default_type))
        r = self.cols.table.rowCount() - 1
        self.cols.table.setCurrentCell(r, self.C_NAME)
        self.cols.table.editItem(self.cols.table.item(r, self.C_NAME))
        self._changed()

    def _gate_role_column(self) -> None:
        import shiboken6 as _sh

        from dbide.ui.features import Gate

        if _sh.isValid(self.cols.table):
            # beginners use the special types in the Type dropdown; the Role column is the advanced view of the same thing
            self.cols.table.setColumnHidden(self.C_ROLE, not Gate.get().allowed("table_advanced"))

    def _pick_type(self, row: int, text: str) -> bool:
        """A special type ('Password (hashed)', 'Image (file on disk)'…) sets the SQL type and the role together."""
        role = from_smart(text)
        if role is None:
            return False
        t = self.cols.table
        t.item(row, self.C_TYPE).setText(suggested_type(role, self.driver.key))
        ri = t.item(row, self.C_ROLE)
        ri.setData(Qt.ItemDataRole.UserRole, role.code())
        ri.setText(role.label())  # triggers _column_item_changed → explanation in the Help panel
        return True

    def current_roles(self) -> dict[str, ColumnRole]:
        t = self.cols.table
        out = {}
        for r in range(t.rowCount()):
            it = t.item(r, self.C_ROLE)
            code = it.data(Qt.ItemDataRole.UserRole) if it else ""
            role = ColumnRole.from_code(code) if code else None
            if role:
                old = self._roles.get(_text(t, r, self.C_NAME))
                if old and old.kind == role.kind and role.kind == "media":
                    role.folder = old.folder
                out[_text(t, r, self.C_NAME)] = role
        return out

    def _column_item_changed(self, item: QTableWidgetItem) -> None:
        if self._filling:
            return
        t = self.cols.table
        r, c = item.row(), item.column()
        if c == self.C_ROLE:
            code = item.data(Qt.ItemDataRole.UserRole)
            role = ColumnRole.from_code(code) if code else None
            name_item = t.item(r, self.C_NAME)
            if role and name_item and name_item.data(ORIG_ROLE) is None:
                t.item(r, self.C_TYPE).setText(suggested_type(role, self.driver.key))
            if role:
                from dbide.core import crypto

                extra = (crypto.ALGORITHMS[role.algo].summary if role.algo in crypto.ALGORITHMS else "")
                HelpCenter.get().show_html(role.label(), f"<p>{extra}</p>" + (
                    "<p>Hashed and encrypted values are long text, so the column type was set to TEXT/VARCHAR(255).</p>"
                    if role.kind != "media" else "<p><a href='topic:concept:media'>BLOB vs file path</a></p>"))
        elif c == self.C_NAME and looks_like_password(item.text()):
            role_item = t.item(r, self.C_ROLE)
            if role_item and not role_item.data(Qt.ItemDataRole.UserRole):
                HelpCenter.get().show_html(
                    "Storing a password?",
                    f"<p><code>{item.text()}</code> looks like a password column. <b>Never store passwords as plain text.</b></p>"
                    "<p>Set its <b>Role</b> to <b>🔑 Password hash (Argon2id)</b>: the IDE will hash every value before saving it.</p>"
                    "<p><a href='topic:concept:hash'>Hashing vs encryption</a></p>")
                if HelpCenter.get().beginner:
                    self.msg.setText("💡 Tip: set the Role of this column to a password hash (see the Help panel).")

    def _explain_fk(self, r: int, c: int) -> None:
        if r < 0:
            return
        t = self.fks.table
        cols, ref, refcols = _text(t, r, 1), _text(t, r, 2), _text(t, r, 3)
        if c in (4, 5):
            HelpCenter.get().show_topic("field:fk_action")
            return
        me = self.name_edit.text().strip() or "this table"
        if not (cols and ref):
            HelpCenter.get().show_topic("field:fk")
            return
        refcols = refcols or "id"
        HelpCenter.get().show_html(
            f"{me}.{cols} → {ref}.{refcols}",
            f"<p>Each <b>{me}</b> row will point to one <b>{ref}</b> row: <code>{me}.{cols}</code> must equal an existing "
            f"<code>{ref}.{refcols}</code>.</p><p>One {ref} can have <b>many</b> {me} rows (one-to-many). "
            f"If <code>{cols}</code> is also UNIQUE, it becomes one-to-one.</p>"
            f"<p>You'll combine them with a JOIN like:</p><pre>SELECT *\nFROM {me} x\nJOIN {ref} y ON y.{refcols} = x.{cols};</pre>"
            "<p>After you apply, open the <b>Diagram</b> to see the line between the tables.</p>")

    def add_index(self) -> None:
        tname = self.name_edit.text().strip() or "table"
        self._add_index_row(IndexDef(f"idx_{tname}_{self.idx.table.rowCount() + 1}", []))
        self._changed()

    def add_fk(self) -> None:
        tname = self.name_edit.text().strip() or "table"
        self._add_fk_row(ForeignKeyDef(f"fk_{tname}_{self.fks.table.rowCount() + 1}", [], "", []))
        self._changed()

    def current_def(self) -> TableDef:
        td = TableDef(
            name=self.name_edit.text().strip(),
            schema=self.schema,
            original_name=self.original.name if self.original else None,
        )
        if self.driver.key == "mysql":
            td.options = dict(self.original.options) if self.original else {}
            td.options["engine"] = self.engine_edit.currentText().strip()
            td.options["comment"] = self.table_comment.text().strip()
        t = self.cols.table
        for r in range(t.rowCount()):
            pk = _checked(t, r, self.C_PK)
            default = _text(t, r, self.C_DEFAULT)
            td.columns.append(
                ColumnDef(
                    name=_text(t, r, self.C_NAME),
                    type=_text(t, r, self.C_TYPE),
                    nullable=not (_checked(t, r, self.C_NOTNULL) or pk),
                    primary_key=pk,
                    auto_increment=_checked(t, r, self.C_AI),
                    default=default or None,
                    comment=_text(t, r, self.C_COMMENT),
                    original_name=t.item(r, self.C_NAME).data(ORIG_ROLE) if t.item(r, self.C_NAME) else None,
                )
            )
        t = self.idx.table
        for r in range(t.rowCount()):
            item = t.item(r, 0)
            td.indexes.append(
                IndexDef(
                    _text(t, r, 0),
                    _split(_text(t, r, 1)),
                    _checked(t, r, 2),
                    item.data(ORIG_ROLE) if item else None,
                    constraint=bool(item and item.data(CONSTRAINT_ROLE)),
                )
            )
        t = self.fks.table
        for r in range(t.rowCount()):
            td.foreign_keys.append(
                ForeignKeyDef(
                    name=_text(t, r, 0),
                    columns=_split(_text(t, r, 1)),
                    ref_table=_text(t, r, 2),
                    ref_columns=_split(_text(t, r, 3)),
                    on_delete=_text(t, r, 4),
                    on_update=_text(t, r, 5),
                    original_name=t.item(r, 0).data(ORIG_ROLE) if t.item(r, 0) else None,
                )
            )
        return td

    def statements(self) -> tuple[list[str], list[str]]:
        td = self.current_def()
        problems = td.validate()
        ai = [c for c in td.columns if c.auto_increment]
        if len(ai) > 1:
            problems.append("Only one column can be auto-increment.")
        if self.driver.key == "sqlite" and ai and not (ai[0].primary_key and len(td.primary_key) == 1):
            problems.append("In SQLite, auto-increment needs a single-column INTEGER primary key.")
        if problems:
            return [], problems
        if self.original:
            return self.driver.alter_table_sql(self.original, td), []
        return self.driver.create_table_sql(td), []

    def update_preview(self) -> None:
        stmts, problems = self.statements()
        if problems:
            self.preview.setPlainText("-- Fix these first:\n" + "\n".join(f"-- • {p}" for p in problems))
        elif not stmts:
            self.preview.setPlainText("-- No changes")
        else:
            self.preview.setPlainText(";\n\n".join(stmts) + ";")

    def revert(self) -> None:
        if self.original:
            self._fill(self.original)
            self._dirty = False
            self._title_refresh()

    def apply(self) -> None:
        stmts, problems = self.statements()
        if problems:
            QMessageBox.warning(self, "Table definition has problems", "\n".join(problems))
            return
        if not stmts:
            roles = self.current_roles()
            if self.original and roles != self._roles:
                role_store().set_table(self.conn_id, self.schema, self.original.name, roles)
                self._roles = roles
                self._dirty = False
                self._title_refresh()
                self.msg.setText("Column roles saved (no SQL needed: roles are stored by the IDE, not in the database).")
                self.applied.emit(self.conn_id, self.schema)
                return
            self.msg.setText("No changes to apply.")
            return
        self.update_preview()
        preview = ";\n".join(stmts) + ";"
        box = QMessageBox(self)
        box.setWindowTitle("Run these statements?")
        box.setText(f"{len(stmts)} statement(s) will run against {self.session.cfg.name}.")
        box.setDetailedText(preview)
        box.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        if self.original and self.driver.key == "sqlite" and any(s.startswith("PRAGMA") for s in stmts):
            box.setInformativeText("SQLite can't make this change in place, so the table will be rebuilt and its rows copied inside a transaction. Indexes and triggers are recreated; CHECK constraints are not carried over.")
        if box.exec() != QMessageBox.StandardButton.Ok:
            return
        s, td = self.session, self.current_def()
        self.apply_btn.setEnabled(False)
        self.msg.setText("Applying…")

        roles = self.current_roles()
        old_name = self.original.name if self.original else None

        def done(_):
            from dbide.core.progress import progress

            progress().record("create_table" if not old_name else "edit_data")
            if td.foreign_keys:
                progress().record("relationship")
            if old_name and old_name != td.name:
                role_store().drop_table(self.conn_id, self.schema, old_name)
            role_store().set_table(self.conn_id, self.schema, td.name, roles)
            self.apply_btn.setEnabled(True)
            self.msg.setText("Done." + (" Column roles saved." if roles else ""))
            self.applied.emit(self.conn_id, self.schema)
            run_async(lambda: s.table_def(td.name, self.schema), self._loaded, self._load_failed)

        def failed(m):
            self.apply_btn.setEnabled(True)
            self.msg.setText(m)
            QMessageBox.critical(self, "Failed", m)

        run_async(lambda: s.execute_ddl(stmts), done, failed)

    def shutdown(self) -> None:
        pass
