"""Browse and edit the rows of one table."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from dbide.core import crypto, secrets
from dbide.core.knowledge import friendly_error
from dbide.core.roles import prepare_row, role_store
from dbide.core.transfer import export_rows
from dbide.ui.connections import ConnectionManager
from dbide.ui.grid import CheckHeader, EditableTableModel, GridView
from dbide.ui.help_panel import HelpCenter, tag
from dbide.ui.theme import ThemeManager, responsive_toolbar
from dbide.ui.media_viewer import InsertMediaDialog, MediaViewer
from dbide.ui.query_tab import export_dialog
from dbide.ui.worker import run_async


class DataTab(QWidget):
    def __init__(self, manager: ConnectionManager, conn_id: str, table: str, schema: str | None, is_view: bool = False):
        super().__init__()
        self.manager, self.conn_id, self.table, self.schema = manager, conn_id, table, schema
        self.is_view = is_view
        self.offset = 0
        self.total = 0
        self.loading = False

        tb = QToolBar()
        tb.setObjectName("TabToolbar")
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        responsive_toolbar(tb, self, 880)
        tm = ThemeManager.get()

        def act(text, slot, shortcut=None, tip=None, help_key=None, icon=None):
            a = QAction(text, self)
            if icon:
                tm.bind_icon(a, icon, "on_accent" if icon == "apply" else "text")
            if help_key:
                tag(a, help_key)
            a.triggered.connect(slot)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
                a.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
                self.addAction(a)
            if tip:
                a.setToolTip(tip)
            tb.addAction(a)
            return a

        act("Refresh", self.reload, "Ctrl+R", help_key="cmd:refresh", icon="refresh")
        tb.addSeparator()
        self.add_act = act("Add Row", self.add_row, tip="Add a new row", help_key="cmd:add_row", icon="plus")
        self.del_act = act("Delete Row", self.delete_rows, tip="Mark selected rows for deletion (click again to undo)", help_key="cmd:del_row", icon="trash")
        self.null_act = act("Set NULL", self.set_null, tip="Set selected cells to NULL", help_key="cmd:set_null", icon="null")
        tb.addSeparator()
        # row checkboxes: pick many rows, then delete or update them together
        self.check_all_box = QCheckBox("Select all")
        self.check_all_box.setToolTip("Tick every row on this page (click a row's box in the left column to tick it alone)")
        self.check_all_box.clicked.connect(lambda on: self.model.check_all(on))
        tb.addWidget(self.check_all_box)
        self.checked_label = QLabel("")
        self.checked_label.setObjectName("Muted")
        tb.addWidget(self.checked_label)
        self.del_checked_act = act("Delete Checked", self.delete_checked, help_key="cmd:check_rows", icon="trash",
                                   tip="Mark every checked row for deletion (saved when you Apply)")
        self.upd_checked_act = act("Update Checked…", self.update_checked, help_key="cmd:check_rows", icon="designer",
                                   tip="Set one column to the same value on every checked row (saved when you Apply)")
        tb.addSeparator()
        self.apply_act = act("Apply", self.apply, "Ctrl+S", "Save staged changes to the database (⌘S)", "cmd:apply", icon="apply")
        self.discard_act = act("Discard", self.discard, help_key="cmd:discard", icon="discard")
        tb.addSeparator()
        from dbide.ui.features import Gate

        self.export_act = act("Export…", Gate.get().guarded("export", self.export, self), help_key="cmd:export", icon="export")
        Gate.get().bind_tool(tb, self.export_act, "export")
        tb.addSeparator()
        self.prev_act = act("Previous", self.prev_page, tip="Previous page", icon="prev")
        self.page_label = QLabel(" ")
        tb.addWidget(self.page_label)
        self.next_act = act("Next", self.next_page, tip="Next page", icon="next")
        self.page_size = QComboBox()
        for n in (100, 500, 1000, 5000):
            self.page_size.addItem(f"{n} / page", n)
        self.page_size.setCurrentIndex(1)
        self.page_size.currentIndexChanged.connect(lambda _: self.reload(reset_offset=True))
        tb.addWidget(self.page_size)
        apply_btn = tb.widgetForAction(self.apply_act)
        if apply_btn is not None:
            apply_btn.setObjectName("Primary")
        for a in (self.prev_act, self.next_act):
            w = tb.widgetForAction(a)
            if w is not None:
                w.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)

        filt = QHBoxLayout()
        filt.setContentsMargins(10, 8, 10, 8)
        filt.setSpacing(8)
        self.where = QLineEdit()
        self.where.setPlaceholderText("WHERE …  e.g. status = 'active' AND id > 10")
        self.where.returnPressed.connect(lambda: self.reload(reset_offset=True))
        self.order = QLineEdit()
        self.order.setPlaceholderText("ORDER BY …  e.g. created_at DESC")
        self.order.returnPressed.connect(lambda: self.reload(reset_offset=True))
        tag(self.where, "cmd:filter")
        tag(self.order, "cmd:sort")
        self.where.addAction(tm.icon("filter", "muted"), QLineEdit.ActionPosition.LeadingPosition)
        self.order.addAction(tm.icon("sort", "muted"), QLineEdit.ActionPosition.LeadingPosition)
        self.where.setClearButtonEnabled(True)
        self.order.setClearButtonEnabled(True)
        filt.addWidget(self.where, 3)
        filt.addWidget(self.order, 1)

        self.model = EditableTableModel(self)
        self.model.dataChanged.connect(lambda *_: self._update_actions())
        self.model.rowsInserted.connect(lambda *_: self._update_actions())
        self.model.rowsRemoved.connect(lambda *_: self._update_actions())
        self.grid = GridView()
        self.grid.setVerticalHeader(CheckHeader(self.grid))
        self.grid.setModel(self.model)
        self.model.checks_changed.connect(self._checks_changed)
        null_ctx = QAction("Set NULL", self.grid)
        null_ctx.triggered.connect(self.set_null)
        del_ctx = QAction("Delete / Undelete Row", self.grid)
        del_ctx.triggered.connect(self.delete_rows)
        self.media_ctx = QAction("Insert Image/Video…", self.grid)
        self.media_ctx.triggered.connect(self.insert_media)
        tag(self.media_ctx, "cmd:insert_media")
        self.view_ctx = QAction("View Media", self.grid)
        self.view_ctx.triggered.connect(self.view_media)
        tag(self.view_ctx, "cmd:view_media")
        self.reveal_ctx = QAction("Reveal Decrypted Value", self.grid)
        self.reveal_ctx.triggered.connect(self.reveal)
        self.verify_ctx = QAction("Check a Password Against This Hash…", self.grid)
        self.verify_ctx.triggered.connect(self.verify_password)
        self.folder_ctx = QAction("Open Media Folder", self.grid)
        self.folder_ctx.triggered.connect(self.open_media_folder)
        self.grid.extra_actions = [null_ctx, del_ctx, None, self.media_ctx, self.view_ctx, self.folder_ctx, None,
                                   self.reveal_ctx, self.verify_ctx]
        from dbide.core import media as _media

        self.model.resolve_media = lambda v: _media.resolve(v, self.session.cfg)
        self.grid.doubleClicked.connect(self._double_clicked)
        self.grid.selectionModel().currentChanged.connect(lambda cur, _prev: self._explain_cell(cur))

        self.status = QLabel("")
        self.status.setObjectName("Muted")
        self.status.setContentsMargins(10, 4, 10, 4)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(tb)
        lay.addLayout(filt)
        lay.addWidget(self.grid)
        lay.addWidget(self.status)
        self.reload()

    @property
    def session(self):
        return self.manager.session(self.conn_id)

    def title(self) -> str:
        return self.table + (" •" if self.model.dirty else "")

    def is_dirty(self) -> bool:
        return self.model.dirty

    def _confirm_discard(self) -> bool:
        if not self.model.dirty:
            return True
        return (
            QMessageBox.question(self, "Unsaved changes", "Discard the staged changes?")
            == QMessageBox.StandardButton.Yes
        )

    def reload(self, reset_offset: bool = False) -> None:
        if self.loading or not self.session or not self._confirm_discard():
            return
        if reset_offset:
            self.offset = 0
        self.loading = True
        self.status.setText("Loading…")
        s, limit, offset = self.session, self.page_size.currentData(), self.offset
        where, order = self.where.text(), self.order.text()

        def done(page):
            self.loading = False
            self.total = page.total
            self.model.reset_page(page.columns, page.rows, [] if self.is_view else page.primary_key)
            self.model.set_roles(role_store().get(self.conn_id, self.schema, self.table))
            self.grid.fit_columns()
            self._load_header_tips()
            end = offset + len(page.rows)
            self.page_label.setText(f"  {offset + 1 if page.rows else 0}–{end} of {page.total}  ")
            if self.is_view:
                note = "View: read-only."
            elif not page.primary_key:
                note = "Read-only: this table has no primary key, so rows can't be identified for editing."
            else:
                note = "Double-click a cell to edit. Changes are staged until you Apply."
            self.status.setText(note)
            self._update_actions()

        def failed(msg):
            self.loading = False
            self.status.setText(msg)
            QMessageBox.warning(self, "Could not load data", msg)

        run_async(lambda: s.fetch_page(self.table, self.schema, limit, offset, where, order), done, failed)

    def _load_header_tips(self) -> None:
        s, table, schema = self.session, self.table, self.schema
        roles = role_store().get(self.conn_id, schema, table)

        def work():
            td = s.table_def(table, schema)
            return td

        def done(td):
            import html as _h

            fks = {fk.columns[0]: fk for fk in td.foreign_keys if len(fk.columns) == 1}
            uniq = {ix.columns[0] for ix in td.indexes if ix.unique and len(ix.columns) == 1}
            tips = {}
            if not HelpCenter.get().beginner:
                for c in td.columns:
                    bits = [_h.escape(c.type)]
                    if c.primary_key:
                        bits.append("PK")
                    if c.name in fks:
                        bits.append(f"FK → {_h.escape(fks[c.name].ref_table)}.{_h.escape(fks[c.name].ref_columns[0])}")
                    if not c.nullable and not c.primary_key:
                        bits.append("NOT NULL")
                    if c.name in uniq:
                        bits.append("UNIQUE")
                    if c.default:
                        bits.append(f"DEFAULT {_h.escape(c.default)}")
                    if c.name in roles:
                        bits.append(_h.escape(roles[c.name].label()))
                    tips[c.name] = f"<b>{_h.escape(c.name)}</b> · " + " · ".join(bits)
                self.model.header_tips = tips
                return
            for c in td.columns:
                lines = [f"<b>{_h.escape(c.name)}</b> · {_h.escape(c.type)}"]
                if c.primary_key:
                    lines.append("🔑 <b>Primary key</b>: uniquely identifies each row; other tables point here.")
                if c.name in fks:
                    fk = fks[c.name]
                    lines.append(f"🔗 <b>Foreign key</b> → {_h.escape(fk.ref_table)}.{_h.escape(fk.ref_columns[0])}: each row points to one "
                                 f"{_h.escape(fk.ref_table)} row, and the value must exist there.")
                if not c.nullable and not c.primary_key:
                    lines.append("Required (NOT NULL): every row must have a value.")
                if c.name in uniq:
                    lines.append("Unique: no two rows may share a value.")
                if c.default:
                    lines.append(f"Default: <code>{_h.escape(c.default)}</code> when left empty.")
                if c.name in roles:
                    lines.append(_h.escape(roles[c.name].label()))
                tips[c.name] = "<br>".join(lines)
            self.model.header_tips = tips

        run_async(work, done)

    def _update_actions(self) -> None:
        editable = self.model.editable
        for a in (self.add_act, self.del_act, self.null_act):
            a.setEnabled(editable)
        self.check_all_box.setEnabled(editable)
        self._checks_changed()
        self.apply_act.setEnabled(self.model.dirty)
        self.discard_act.setEnabled(self.model.dirty)
        limit = self.page_size.currentData()
        self.prev_act.setEnabled(self.offset > 0)
        self.next_act.setEnabled(self.offset + limit < self.total)
        tabs = self.parent()
        while tabs is not None and not hasattr(tabs, "refresh_titles"):
            tabs = tabs.parent()
        if tabs is not None:
            tabs.refresh_titles()

    def prev_page(self) -> None:
        if not self._confirm_discard():
            return
        self.model.reset_page(self.model.columns, self.model.rows, self.model.primary_key)
        self.offset = max(0, self.offset - self.page_size.currentData())
        self.reload()

    def next_page(self) -> None:
        if not self._confirm_discard():
            return
        self.model.reset_page(self.model.columns, self.model.rows, self.model.primary_key)
        self.offset += self.page_size.currentData()
        self.reload()

    def add_row(self) -> None:
        r = self.model.add_row()
        self.grid.scrollToBottom()
        self.grid.setCurrentIndex(self.model.index(r, 0))

    def delete_rows(self) -> None:
        rows = sorted({i.row() for i in self.grid.selectedIndexes()})
        if rows and self.model.editable:
            self.model.toggle_delete(rows)
            self._update_actions()

    # ---- checked rows -------------------------------------------------------------------
    def _checks_changed(self) -> None:
        n = len(self.model.checked)
        total = len(self.model.rows)
        self.checked_label.setText(f" {n} checked " if n else "")
        self.del_checked_act.setEnabled(self.model.editable and n > 0)
        self.upd_checked_act.setEnabled(self.model.editable and n > 0)
        self.check_all_box.blockSignals(True)
        self.check_all_box.setCheckState(Qt.CheckState.Checked if n and n == total else
                                         Qt.CheckState.PartiallyChecked if n else Qt.CheckState.Unchecked)
        self.check_all_box.blockSignals(False)
        self.grid.verticalHeader().viewport().update()

    def delete_checked(self) -> None:
        rows = [r for r in self.model.checked_rows() if r not in self.model.deleted]
        if not rows:
            return
        self.model.toggle_delete(rows)
        self.model.check_all(False)
        self.status.setText(f"{len(rows)} row(s) marked for deletion. Click Apply to delete them, or Discard to cancel.")
        self._update_actions()

    def update_checked(self) -> None:
        rows = [r for r in self.model.checked_rows() if r not in self.model.deleted]
        if not rows:
            return
        cols = [c for i, c in enumerate(self.model.columns)
                if c not in self.model.primary_key and not (self.model.roles.get(i) and self.model.roles[i].kind == "media")]
        dlg = BulkUpdateDialog(cols, len(rows), self.model.roles, self.model.columns, self)
        if not dlg.exec():
            return
        col = self.model.columns.index(dlg.column)
        for r in rows:
            self.model.set_cell(r, col, dlg.value)
        shown = "NULL" if dlg.value is None else repr(dlg.value)
        self.status.setText(f"{dlg.column} = {shown} staged on {len(rows)} row(s). Click Apply to save, or Discard to cancel.")
        self._update_actions()

    def set_null(self) -> None:
        if not self.model.editable:
            return
        for i in self.grid.selectedIndexes():
            if i.row() not in self.model.deleted:
                self.model.set_cell(i.row(), i.column(), None)

    def discard(self) -> None:
        self.model.reset_page(
            self.model.columns,
            [r for i, r in enumerate(self.model.rows) if i not in self.model.new_rows],
            self.model.primary_key,
        )
        self._update_actions()

    def apply(self) -> None:
        if not self.model.dirty:
            return
        updates, inserts, deletes = self.model.changes()
        if deletes and QMessageBox.question(
            self, "Delete rows", f"Delete {len(deletes)} row(s) from {self.table}?"
        ) != QMessageBox.StandardButton.Yes:
            return
        s = self.session
        roles = role_store().get(self.conn_id, self.schema, self.table)
        cid = self.conn_id

        def work():
            # hash / encrypt values according to column roles before they leave the app
            ups = [(keys, prepare_row(roles, vals, cid)) for keys, vals in updates]
            ins = [prepare_row(roles, vals, cid) for vals in inserts]
            return s.apply_changes(self.table, self.schema, ups, ins, deletes)

        def done(n):
            from dbide.core.progress import progress

            progress().record("edit_data")
            secured = [c for c, r in roles.items() if r.kind in ("password", "encrypted")]
            note = f" Values in {', '.join(secured)} were hashed/encrypted automatically." if secured else ""
            self.status.setText(f"Saved: {n} row(s) affected.{note}")
            self.model.reset_page(self.model.columns, [], self.model.primary_key)
            self.reload()

        def failed(msg):
            hint = friendly_error(msg)
            if hint:
                HelpCenter.get().show_html("Why weren't the changes saved?", f"<p><code>{msg}</code></p><p>{hint}</p>")
            QMessageBox.critical(self, "Changes not saved", f"The database rejected the changes; nothing was saved.\n\n{msg}")

        run_async(work, done, failed)

    def export(self) -> None:
        choice = export_dialog(self, self.table)
        if not choice:
            return
        path, fmt = choice
        s = self.session

        def work():
            cols, rows = s.iter_rows(self.table, self.schema)
            return export_rows(path, fmt, cols, rows, s.driver, table=self.table)

        self.status.setText("Exporting…")
        run_async(
            work,
            lambda n: self.status.setText(f"Exported {n} row(s) to {path}"),
            lambda m: QMessageBox.critical(self, "Export failed", m),
        )

    # ---- roles: media, encryption, passwords ----------------------------------
    def _current(self):
        idx = self.grid.currentIndex()
        if not idx.isValid():
            return None, None, None
        return idx.row(), idx.column(), self.model.roles.get(idx.column())

    def _explain_cell(self, idx) -> None:
        if not idx.isValid():
            return
        rob = self.model.roles.get(idx.column())
        name = self.model.columns[idx.column()]
        if rob is not None:
            HelpCenter.get().show_html(f"Column {name}", f"<p><b>Role:</b> {rob.label()}</p>"
                                       + {"password": "<p>Type a new password to replace it; it is hashed when you Apply. "
                                                      "Right-click → <i>Check a Password</i> to test one.</p>",
                                          "encrypted": "<p>Type a value; it's encrypted with this connection's key when you Apply. "
                                                       "Right-click → <i>Reveal</i> to decrypt.</p>",
                                          "media": "<p>Right-click → <i>Insert Image/Video…</i> to add a file; double-click to view it "
                                                   "and see where it is stored.</p>"}[rob.kind], teaching=False)

    def _double_clicked(self, idx) -> None:
        rob = self.model.roles.get(idx.column())
        v = self.model.value(idx.row(), idx.column())
        if (rob is not None and rob.kind == "media") or isinstance(v, (bytes, bytearray, memoryview)):
            if v is not None:
                self.view_media()
            elif self.model.editable:
                self.insert_media()

    def insert_media(self) -> None:
        r, c, rob = self._current()
        if r is None or not self.model.editable:
            QMessageBox.information(self, "Insert media", "Select a cell in an editable table first.")
            return
        col = self.model.columns[c]
        dlg = InsertMediaDialog(self.session.cfg, col, rob, self)
        if not dlg.exec():
            return
        if dlg.new_role is not None:
            roles = role_store().get(self.conn_id, self.schema, self.table)
            roles[col] = dlg.new_role
            role_store().set_table(self.conn_id, self.schema, self.table, roles)
            self.model.set_roles(roles)
        self.model.set_cell(r, c, dlg.value)
        self.status.setText("Media staged. Click Apply to save it.")

    def open_media_folder(self) -> None:
        """Show where this column's files live (the folder in Finder)."""
        from pathlib import Path

        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        from dbide.core import media as _media

        r, c, rob = self._current()
        folder = None
        if r is not None:
            v = self.model.value(r, c)
            if isinstance(v, str) and v:
                folder = _media.resolve(v, self.session.cfg).parent
        if folder is None:
            folder = Path(rob.folder) if rob is not None and rob.folder else _media.default_folder(self.session.cfg)
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
        self.status.setText(f"Media folder: {folder}")

    def view_media(self) -> None:
        r, c, _ = self._current()
        if r is None:
            return
        v = self.model.value(r, c)
        if v is None:
            return
        keys = ", ".join(f"{k}={self.model.rows[r][self.model.columns.index(k)]}" for k in self.model.primary_key)
        where = f"{self.table}.{self.model.columns[c]}" + (f" (row {keys})" if keys else "")
        MediaViewer(v, self.session.cfg, where, self).exec()

    def reveal(self) -> None:
        r, c, _ = self._current()
        if r is None:
            return
        v = self.model.value(r, c)
        if not crypto.is_encrypted(v):
            self.status.setText("That cell isn't encrypted.")
            return
        key = secrets.field_key(self.conn_id)
        if not key:
            QMessageBox.warning(self, "Reveal", "No encryption key is stored for this connection. Import it via Tools → Password & Encryption Tool.")
            return
        try:
            self.model.revealed[(r, c)] = crypto.decrypt_value(v, key)
        except ValueError as exc:
            QMessageBox.warning(self, "Reveal", str(exc))
            return
        idx = self.model.index(r, c)
        self.model.dataChanged.emit(idx, idx)

    def verify_password(self) -> None:
        r, c, _ = self._current()
        if r is None:
            return
        v = self.model.value(r, c)
        if not crypto.detect_hash(v):
            self.status.setText("That cell doesn't contain a password hash.")
            return
        pw, ok = QInputDialog.getText(self, "Check password", "Password to test:", QLineEdit.EchoMode.Password)
        if ok:
            good = crypto.verify_password(pw, v)
            QMessageBox.information(self, "Check password", "✅ That password matches." if good else "❌ That password does not match.")

    def shutdown(self) -> None:
        pass


class BulkUpdateDialog(QDialog):
    """Set one column to the same value on every checked row."""

    def __init__(self, columns: list[str], n_rows: int, roles: dict, all_columns: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Update {n_rows} checked row(s)")
        self.setMinimumWidth(440)
        self.column = columns[0] if columns else ""
        self.value = None
        form = QFormLayout(self)
        self.col = QComboBox()
        self.col.addItems(columns)
        self.val = QLineEdit()
        self.val.setPlaceholderText("new value (text, number or date as you'd type it in a cell)")
        self.null = QCheckBox("Set to NULL (no value)")
        self.null.toggled.connect(lambda on: self.val.setEnabled(not on))
        self.note = QLabel()
        self.note.setObjectName("Muted")
        self.note.setWordWrap(True)
        role_by_name = {all_columns[i]: r for i, r in roles.items()}

        def sync(*_):
            r = role_by_name.get(self.col.currentText())
            extra = f" Values are {('hashed' if r.kind == 'password' else 'encrypted')} automatically." if r is not None and r.kind in ("password", "encrypted") else ""
            self.note.setText(f"Changes {n_rows} row(s). Nothing is saved until you click Apply.{extra}")

        self.col.currentTextChanged.connect(sync)
        sync()
        form.addRow("Column", self.col)
        form.addRow("Value", self.val)
        form.addRow("", self.null)
        form.addRow("", self.note)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Stage Update")
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def _ok(self) -> None:
        self.column = self.col.currentText()
        self.value = None if self.null.isChecked() else self.val.text()
        self.accept()
