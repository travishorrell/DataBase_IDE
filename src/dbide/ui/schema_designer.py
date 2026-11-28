"""Visual schema designer: add tables, drag between them to create relationships,
review every change in plain English (and the exact SQL), then apply."""
from __future__ import annotations

import html
import json

from PySide6.QtCore import QLineF, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPainterPathStroker, QPen
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsObject,
    QGraphicsPathItem,
    QGraphicsScene,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from dbide.core.config import app_dir
from dbide.core.erd_model import ErdModel, ErdRelation, layout
from dbide.core.nl2sql import singular
from dbide.core.schema_model import FK_ACTIONS, ColumnDef, IndexDef, TableDef
from dbide.core.roles import ColumnRole, from_smart, role_store, smart_label, suggested_type, type_choices
from dbide.core.schema_plan import REL_KINDS, DesignerModel, safe_name
from dbide.ui import erd as E
from dbide.ui.help_panel import HelpCenter, tag
from dbide.ui.theme import ThemeManager, responsive_toolbar
from dbide.ui.worker import run_async

HANDLE_R = 6


class DesignNode(QGraphicsObject):
    def __init__(self, td: TableDef, status: str, tab: "DesignerTab"):
        super().__init__()
        self.td, self.status, self.tab = td, status, tab  # status: "", "new", "changed"
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
                      | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.setAcceptHoverEvents(True)
        self.edges: list = []
        self.hovered = False
        self.hover_row = -1
        self.setZValue(2)
        self.setToolTip(f"{td.name}\nDrag the ● handle onto another table to link them. Double-click to edit.")

    @property
    def name(self) -> str:
        return self.td.name

    def rows(self) -> int:
        return len(self.td.columns) + 1  # + "add column"

    def boundingRect(self) -> QRectF:
        return QRectF(-HANDLE_R - 2, -2, E.WIDTH + 2 * HANDLE_R + 4, E.HEAD_H + E.ROW_H * self.rows() + 10)

    def body(self) -> QRectF:
        return QRectF(0, 0, E.WIDTH, E.HEAD_H + E.ROW_H * self.rows() + 4)

    def row_y(self, col: str) -> float:
        for i, c in enumerate(self.td.columns):
            if c.name == col:
                return E.HEAD_H + E.ROW_H * i + E.ROW_H / 2
        return E.HEAD_H / 2

    def anchor(self, col: str | None, right: bool) -> QPointF:
        return self.mapToScene(QPointF(E.WIDTH if right else 0, self.row_y(col) if col else E.HEAD_H / 2))

    def handle_at(self, pos: QPointF) -> tuple[bool, str | None] | None:
        """(hit, column) if pos is on a link handle: the header one or a column's."""
        hx = E.WIDTH
        if QLineF(pos, QPointF(hx, E.HEAD_H / 2)).length() <= HANDLE_R + 4:
            return True, None
        for i, c in enumerate(self.td.columns):
            if QLineF(pos, QPointF(hx, E.HEAD_H + E.ROW_H * i + E.ROW_H / 2)).length() <= HANDLE_R + 3:
                return True, c.name
        return None

    def paint(self, p: QPainter, option, widget=None):
        c = E._colors()
        t = ThemeManager.get().theme
        r = self.body()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 60 if t.dark else 22))
        p.drawRoundedRect(r.translated(0, 3), 9, 9)
        border = QColor(c["hi"] if self.isSelected() else (t.accent if self.status == "new" else c["border"]))
        pen = QPen(border, 2 if self.isSelected() or self.status == "new" else 1)
        if self.status == "new":
            pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.setBrush(QColor(c["box"]))
        p.drawRoundedRect(r, 9, 9)
        head = QPainterPath()
        head.setFillRule(Qt.FillRule.WindingFill)
        head.addRoundedRect(QRectF(0, 0, E.WIDTH, E.HEAD_H), 9, 9)
        head.addRect(QRectF(0, E.HEAD_H - 9, E.WIDTH, 9))
        p.fillPath(head.simplified(), QColor(c["head"]))
        f = QFont()
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(c["head_text"]))
        title = self.td.name + ("  · new" if self.status == "new" else "  ●" if self.status == "changed" else "")
        p.drawText(QRectF(10, 0, E.WIDTH - 34, E.HEAD_H), Qt.AlignmentFlag.AlignVCenter, title)
        small = QFont()
        small.setPointSizeF(small.pointSizeF() * 0.9)
        fk_cols = {col for fk in self.td.foreign_keys for col in fk.columns}
        uniq = {ix.columns[0] for ix in self.td.indexes if ix.unique and len(ix.columns) == 1}
        for i, col in enumerate(self.td.columns):
            y = E.HEAD_H + E.ROW_H * i
            if i == self.hover_row:
                p.fillRect(QRectF(1, y, E.WIDTH - 2, E.ROW_H), QColor(t.hover))
            badge, color = ("PK", c["pk"]) if col.primary_key else ("FK", c["fk"]) if col.name in fk_cols else ("", c["text"])
            if badge:
                bf = QFont(small)
                bf.setBold(True)
                p.setFont(bf)
                p.setPen(QColor(color))
                p.drawText(QRectF(8, y, 26, E.ROW_H), Qt.AlignmentFlag.AlignVCenter, badge)
            p.setFont(f if col.primary_key else QFont())
            p.setPen(QColor(c["text"]))
            label = col.name + ("" if col.nullable or col.primary_key else " *") + (" (unique)" if col.name in uniq else "")
            name_w = int(E.WIDTH - (180 if self.tab.model and self.tab.model.roles.get(self.td.name, {}).get(col.name) else 140))
            p.drawText(QRectF(36, y, name_w, E.ROW_H), Qt.AlignmentFlag.AlignVCenter,
                       QFontMetrics(p.font()).elidedText(label, Qt.TextElideMode.ElideRight, name_w))
            p.setFont(small)
            role = self.tab.model.roles.get(self.td.name, {}).get(col.name) if self.tab.model else None
            p.setPen(QColor(t.accent if role else c["sub"]))
            shown = ({"password": "🔑 ", "encrypted": "🔒 "}.get(role.kind, "🖼 " if role.storage == "blob" else "📁 ") + role.short()) if role else col.type
            type_w = 130 if role else 98
            p.drawText(QRectF(E.WIDTH - type_w - 12, y, type_w, E.ROW_H), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                       QFontMetrics(small).elidedText(shown, Qt.TextElideMode.ElideRight, type_w))
        # "+ add column" row
        y = E.HEAD_H + E.ROW_H * len(self.td.columns)
        p.setFont(QFont())
        p.setPen(QColor(t.accent if self.hover_row == len(self.td.columns) else c["sub"]))
        p.drawText(QRectF(36, y, E.WIDTH - 40, E.ROW_H), Qt.AlignmentFlag.AlignVCenter, "+ add column")
        # link handles: header always, columns on hover
        p.setPen(QPen(QColor(c["box"]), 2))
        p.setBrush(QColor(t.accent))
        p.drawEllipse(QPointF(E.WIDTH, E.HEAD_H / 2), HANDLE_R, HANDLE_R)
        if self.hovered:
            p.setBrush(QColor(t.mix(t.accent, t.surface, 0.35)))
            for i in range(len(self.td.columns)):
                p.drawEllipse(QPointF(E.WIDTH, E.HEAD_H + E.ROW_H * i + E.ROW_H / 2), HANDLE_R - 1.5, HANDLE_R - 1.5)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            for e in self.edges:
                e.update_path()
            self.tab.positions_changed()
        return super().itemChange(change, value)

    def hoverEnterEvent(self, ev):
        self.hovered = True
        self.update()

    def hoverLeaveEvent(self, ev):
        self.hovered = False
        self.hover_row = -1
        self.update()

    def hoverMoveEvent(self, ev):
        i = int((ev.pos().y() - E.HEAD_H) // E.ROW_H) if ev.pos().y() > E.HEAD_H else -1
        if i != self.hover_row:
            self.hover_row = i
            self.update()
        self.setCursor(Qt.CursorShape.CrossCursor if self.handle_at(ev.pos()) else Qt.CursorShape.OpenHandCursor)

    def mousePressEvent(self, ev):
        h = self.handle_at(ev.pos())
        if h and ev.button() == Qt.MouseButton.LeftButton:
            self.tab.begin_link(self, h[1], self.mapToScene(ev.pos()))
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseDoubleClickEvent(self, ev):
        y = ev.pos().y()
        if y > E.HEAD_H + E.ROW_H * len(self.td.columns):
            self.tab.add_column_to(self.name)
        else:
            self.tab.edit_table(self.name)

    def contextMenuEvent(self, ev):
        m = QMenu()
        m.addAction("Edit Table…", lambda: self.tab.edit_table(self.name))
        m.addAction("Add Column…", lambda: self.tab.add_column_to(self.name))
        m.addAction("Rename…", lambda: self.tab.rename_table(self.name))
        if self.td.original_name and self.status != "new":
            m.addSeparator()
            m.addAction("Open Data", lambda: self.tab.open_table.emit(self.name))
            m.addAction("Advanced Designer…", lambda: self.tab.advanced_requested.emit(self.name))
        m.addSeparator()
        m.addAction("Delete Table", lambda: self.tab.delete_table(self.name))
        m.exec(ev.screenPos())


class DesignEdge(QGraphicsPathItem):
    def __init__(self, child: DesignNode, parent: DesignNode, fk, one_to_one: bool, tab: "DesignerTab"):
        super().__init__()
        self.child, self.parent_node, self.fk, self.one_to_one, self.tab = child, parent, fk, one_to_one, tab
        child.edges.append(self)
        parent.edges.append(self)
        self.setAcceptHoverEvents(True)
        self.hover = False
        self.setZValue(1)
        self.update_path()

    def update_path(self):
        c, pn = self.child, self.parent_node
        col, pcol = self.fk.columns[0], self.fk.ref_columns[0]
        if c is pn:
            a, b = c.anchor(col, True), c.anchor(pcol, True)
            path = QPainterPath(a)
            path.cubicTo(a + QPointF(70, 0), b + QPointF(70, 0), b)
            E._arrow_marks(path, a, -1, not self.one_to_one)
            E._arrow_marks(path, b, -1, False)
            self.setPath(path)
            return
        right = pn.sceneBoundingRect().center().x() > c.sceneBoundingRect().center().x()
        a, b = c.anchor(col, right), pn.anchor(pcol, not right)
        s = 1 if right else -1
        dx = max(40.0, abs(b.x() - a.x()) / 2)
        path = QPainterPath(a)
        path.cubicTo(a + QPointF(s * dx, 0), b - QPointF(s * dx, 0), b)
        E._arrow_marks(path, a, -s, not self.one_to_one)
        E._arrow_marks(path, b, s, False)
        self.setPath(path)

    def shape(self):
        st = QPainterPathStroker()
        st.setWidth(12)
        return st.createStroke(self.path())

    def paint(self, p, option, widget=None):
        c = E._colors()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(c["hi"] if self.hover else c["line"]), 2.6 if self.hover else 1.6))
        p.drawPath(self.path())

    def hoverEnterEvent(self, ev):
        self.hover = True
        self.update()
        kind = "one-to-one" if self.one_to_one else "one-to-many"
        ch, pa = self.child.name, self.parent_node.name
        HelpCenter.get().show_html(
            f"{ch} → {pa} ({kind})",
            f"<p>Each <b>{singular(ch)}</b> points to one <b>{singular(pa)}</b> through "
            f"<code>{ch}.{self.fk.columns[0]}</code> → <code>{pa}.{self.fk.ref_columns[0]}</code>.</p>"
            + (f"<p>A {singular(pa)} has at most one {singular(ch)} (the column is UNIQUE).</p>" if self.one_to_one
               else f"<p>One {singular(pa)} can have many {ch}.</p>")
            + f"<p>Deleting a {singular(pa)}: <b>{html.escape(self.fk.on_delete or 'NO ACTION')}</b>. Right-click the line to change it or remove the link.</p>")

    def hoverLeaveEvent(self, ev):
        self.hover = False
        self.update()

    def contextMenuEvent(self, ev):
        m = QMenu()
        od = m.addMenu("When the parent is deleted")
        for action in [a for a in FK_ACTIONS if a]:
            act = od.addAction(action, lambda a=action: self.tab.set_on_delete(self.child.name, self.fk.name, a))
            act.setCheckable(True)
            act.setChecked((self.fk.on_delete or "NO ACTION") == action)
        m.addSeparator()
        m.addAction("Remove Link", lambda: self.tab.remove_link(self.child.name, self.fk.name))
        m.exec(ev.screenPos())


class DesignerView(E.ErdView):
    def __init__(self, scene, tab: "DesignerTab"):
        super().__init__(scene)
        self.tab = tab
        self.setAcceptDrops(False)

    def mouseMoveEvent(self, ev):
        if self.tab.link_line is not None:
            self.tab.move_link(self.mapToScene(ev.position().toPoint()))
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if self.tab.link_line is not None:
            self.tab.end_link(self.mapToScene(ev.position().toPoint()))
            return
        super().mouseReleaseEvent(ev)

    def contextMenuEvent(self, ev):
        if self.itemAt(ev.pos()) is not None:
            return super().contextMenuEvent(ev)
        m = QMenu(self)
        pos = self.mapToScene(ev.pos())
        m.addAction("Add Table Here…", lambda: self.tab.add_table(pos))
        m.exec(ev.globalPos())


class RelationshipDialog(QDialog):
    """Choose what a dragged link means, with plain-English explanations."""

    def __init__(self, model: DesignerModel, child: str, parent_table: str, column: str | None, advanced: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Link tables")
        self.setMinimumWidth(700)
        self.model, self.child, self.parent_table = model, child, parent_table
        c1, p1 = singular(child), singular(parent_table)
        lay = QVBoxLayout(self)
        head = QLabel(f"How are <b>{html.escape(child)}</b> and <b>{html.escape(parent_table)}</b> related?")
        head.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(head)
        self.group = QButtonGroup(self)
        options = [
            ("one_to_many", f"One-to-many: each {c1} belongs to one {p1}",
             f"Most common: a {p1} can have many {child}. Adds a column to {child} that stores the {p1}'s id (like orders → customer)."),
            ("one_to_one", f"One-to-one: each {p1} has at most one {c1}",
             "Same column, but UNIQUE, e.g. user → profile."),
            ("many_to_many", f"Many-to-many: many {child} relate to many {parent_table}",
             f"e.g. students ↔ courses. Creates a junction table {child}_{parent_table} with a row per pair."),
        ]
        for key, label, desc in options:
            rb = QRadioButton(label)
            rb.setProperty("kind", key)
            self.group.addButton(rb)
            lay.addWidget(rb)
            d = QLabel(desc)
            d.setObjectName("Muted")
            d.setWordWrap(True)
            d.setContentsMargins(26, 0, 0, 6)
            lay.addWidget(d)
            rb.toggled.connect(self._update)

        form = QFormLayout()
        self.column = QLineEdit(column or model.default_fk_column(parent_table))
        self.column.textChanged.connect(self._update)
        self.required = QCheckBox(f"Required: every {c1} must have a {p1}")
        self.required.toggled.connect(self._update)
        form.addRow(f"Column in {child}", self.column)
        form.addRow("", self.required)
        self.col_row = QWidget()
        self.col_row.setLayout(form)
        lay.addWidget(self.col_row)

        self.adv_btn = QToolButton()
        self.adv_btn.setText("Advanced options")
        self.adv_btn.setCheckable(True)
        self.adv = QWidget()
        av = QVBoxLayout(self.adv)
        av.setContentsMargins(0, 0, 0, 0)
        af = QFormLayout()
        self.on_delete = QComboBox()
        for a in FK_ACTIONS:
            self.on_delete.addItem(a or "(database default: refuse)", a)
        self.on_delete.currentIndexChanged.connect(self._delete_chosen)
        af.addRow(f"When a {p1} is deleted", self.on_delete)
        av.addLayout(af)
        sql_label = QLabel("SQL that will run")
        sql_label.setObjectName("Muted")
        av.addWidget(sql_label)
        self.sql = QPlainTextEdit()
        self.sql.setReadOnly(True)
        self.sql.setMinimumHeight(150)
        from dbide.ui.sql_editor import monospace_font

        self.sql.setFont(monospace_font(11))
        av.addWidget(self.sql)
        self._user_on_delete = False
        self.adv_btn.toggled.connect(lambda on: (self.adv.setVisible(on), self.adjustSize()))
        self.adv.setVisible(advanced)
        self.adv_btn.setChecked(advanced)
        lay.addWidget(self.adv_btn)
        lay.addWidget(self.adv)
        self.preview = QLabel()
        self.preview.setWordWrap(True)
        self.preview.setObjectName("Muted")
        lay.addWidget(self.preview)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Link")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        first = self.group.buttons()[0]
        first.blockSignals(True)  # everything above must exist before _update runs
        first.setChecked(True)
        first.blockSignals(False)
        self._update()
        self.adjustSize()

    @property
    def kind(self) -> str:
        return self.group.checkedButton().property("kind")

    def _delete_chosen(self, *_):
        self._user_on_delete = True
        self._update()

    def _update(self, *_):
        m2m = self.kind == "many_to_many"
        self.col_row.setVisible(not m2m)
        if not self._user_on_delete:
            # show the default that will really be used: junction rows go with their parents
            self.on_delete.blockSignals(True)
            self.on_delete.setCurrentIndex(self.on_delete.findData("CASCADE" if m2m else ""))
            self.on_delete.blockSignals(False)
        trial = DesignerModel(self.model.driver_key)
        trial.tables = {k: v.clone() for k, v in self.model.tables.items()}
        trial.originals = self.model.originals
        try:
            summary = trial.add_relationship(self.child, self.parent_table, self.kind, self.column.text() or None,
                                             self.required.isChecked(), self.on_delete.currentData() or "")
            self.preview.setText(summary)
            from dbide.drivers import get_driver

            steps = trial.plan(get_driver(self.model.driver_key))
            changed = [s for s in steps if s.table in (self.child, self.parent_table) or s.table not in self.model.tables]
            self.sql.setPlainText(";\n\n".join(st for s in changed for st in s.statements if not st.startswith(("PRAGMA", "BEGIN", "COMMIT"))) + ";")
        except ValueError as exc:
            self.preview.setText(f"⚠ {exc}")


class TableDialog(QDialog):
    """Edit a table's name and columns (simple fields; advanced types are free text)."""

    def __init__(self, td: TableDef, types: list[str], parent=None, roles: dict | None = None, driver_key: str = "sqlite"):
        super().__init__(parent)
        self.roles_in = dict(roles or {})
        self.driver_key = driver_key
        self.roles_out: dict = {}
        self.setWindowTitle(f"Table {td.name}")
        self.resize(640, 420)
        self.td = td.clone()
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(td.name)
        form.addRow("Table name", self.name)
        lay.addLayout(form)
        self.grid = QTableWidget(0, 5)
        self.grid.setHorizontalHeaderLabels(["Name", "Type", "Primary key", "Required", "Unique"])
        self.grid.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.grid.verticalHeader().setVisible(False)
        self.types = types
        uniq = {ix.columns[0] for ix in td.indexes if ix.unique and len(ix.columns) == 1}
        for c in self.td.columns:
            self._add_row(c, c.name in uniq)
        lay.addWidget(self.grid, 1)
        row = QHBoxLayout()
        add = QPushButton("Add Column")
        add.clicked.connect(lambda: self._add_row(ColumnDef(f"column_{self.grid.rowCount() + 1}", types[1] if len(types) > 1 else "TEXT"), False))
        rm = QPushButton("Remove Column")
        rm.clicked.connect(lambda: self.grid.removeRow(self.grid.currentRow()) if self.grid.currentRow() >= 0 else None)
        row.addWidget(add)
        row.addWidget(rm)
        row.addStretch()
        lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        tag(self.grid, "field:type")

    def _add_row(self, c: ColumnDef, unique: bool) -> None:
        r = self.grid.rowCount()
        self.grid.insertRow(r)
        it = QTableWidgetItem(c.name)
        it.setData(Qt.ItemDataRole.UserRole, c.original_name)
        self.grid.setItem(r, 0, it)
        cb = QComboBox()
        cb.setEditable(True)
        cb.setMaxVisibleItems(20)
        for t in type_choices(self.types):
            if t == "":
                cb.insertSeparator(cb.count())
            else:
                cb.addItem(t)
        label = smart_label(self.roles_in.get(c.original_name or c.name))
        cb.setCurrentText(label or c.type)
        self.grid.setCellWidget(r, 1, cb)
        for col, val in ((2, c.primary_key), (3, not c.nullable), (4, unique)):
            ch = QTableWidgetItem()
            ch.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            ch.setCheckState(Qt.CheckState.Checked if val else Qt.CheckState.Unchecked)
            self.grid.setItem(r, col, ch)

    def result_def(self) -> TableDef:
        td = self.td
        old_cols = {c.original_name or c.name: c for c in td.columns}
        cols, uniques = [], []
        for r in range(self.grid.rowCount()):
            name = safe_name(self.grid.item(r, 0).text())
            orig = self.grid.item(r, 0).data(Qt.ItemDataRole.UserRole)
            base = old_cols.get(orig) if orig else None
            type_text = self.grid.cellWidget(r, 1).currentText().strip() or "TEXT"
            role = from_smart(type_text)
            if role is not None:
                old_role = self.roles_in.get(orig) if orig else None
                if old_role is not None and old_role.kind == role.kind and old_role.storage == role.storage:
                    role = old_role  # keep its algorithm / media folder
                self.roles_out[name] = role
                type_text = base.type if base is not None and old_role is not None else suggested_type(role, self.driver_key)
            c = ColumnDef(name, type_text,
                          nullable=self.grid.item(r, 3).checkState() != Qt.CheckState.Checked,
                          primary_key=self.grid.item(r, 2).checkState() == Qt.CheckState.Checked,
                          auto_increment=bool(base and base.auto_increment),
                          default=base.default if base else None, comment=base.comment if base else "",
                          original_name=orig)
            cols.append(c)
            if self.grid.item(r, 4).checkState() == Qt.CheckState.Checked:
                uniques.append(name)
        renames = {c.original_name: c.name for c in cols if c.original_name}
        td.columns = cols
        names = {c.name for c in cols}
        td.foreign_keys = [fk for fk in td.foreign_keys if all(renames.get(x, x) in names for x in fk.columns)]
        for fk in td.foreign_keys:
            fk.columns = [renames.get(x, x) for x in fk.columns]
        kept = []
        for ix in td.indexes:
            ix.columns = [renames.get(x, x) for x in ix.columns]
            if not all(x in names for x in ix.columns):
                continue
            if ix.unique and len(ix.columns) == 1 and ix.columns[0] not in uniques and not ix.constraint:
                continue  # unticked
            kept.append(ix)
        have = {ix.columns[0] for ix in kept if ix.unique and len(ix.columns) == 1}
        for u in uniques:
            if u not in have:
                kept.append(IndexDef(f"ux_{td.name}_{u}", [u], True))
        td.indexes = kept
        td.name = safe_name(self.name.text())
        return td


class PlanDialog(QDialog):
    def __init__(self, steps, advanced: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Review & Apply")
        self.resize(720, 520)
        lay = QVBoxLayout(self)
        intro = QLabel(f"These {len(steps)} step(s) will run in order. Each step either completes fully or is rolled back where the database allows.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        lst = QListWidget()
        for i, s in enumerate(steps, 1):
            lst.addItem(f"{i}. {s.title}")
        lay.addWidget(lst, 1)
        btn = QToolButton()
        btn.setText("Show SQL")
        btn.setCheckable(True)
        sql = QPlainTextEdit()
        sql.setReadOnly(True)
        from dbide.ui.sql_editor import monospace_font

        sql.setFont(monospace_font())
        sql.setPlainText("\n\n".join(f"-- {i}. {s.title}\n" + ";\n".join(s.statements) + ";" for i, s in enumerate(steps, 1)))
        btn.toggled.connect(sql.setVisible)
        sql.setVisible(advanced)
        btn.setChecked(advanced)
        lay.addWidget(btn)
        lay.addWidget(sql, 2)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Apply")
        bb.button(QDialogButtonBox.StandardButton.Ok).setObjectName("Primary")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


class DesignerTab(QWidget):
    open_table = Signal(str)
    advanced_requested = Signal(str)
    applied = Signal(str, object)

    def __init__(self, manager, conn_id: str, schema: str | None):
        super().__init__()
        self.manager, self.conn_id, self.schema = manager, conn_id, schema
        self.model: DesignerModel | None = None
        self.nodes: dict[str, DesignNode] = {}
        self.positions: dict[str, list[float]] = self._load_positions()
        self.link_line: QGraphicsLineItem | None = None
        self.link_from: tuple[DesignNode, str | None] | None = None
        self.scene = QGraphicsScene(self)
        self.view = DesignerView(self.scene, self)
        self.view.on_resize = None
        self.view.auto_fit = False
        self._save_timer = QTimer(self, singleShot=True, interval=600)
        self._save_timer.timeout.connect(self._write_positions)

        tm = ThemeManager.get()
        tb = QToolBar()
        tb.setObjectName("TabToolbar")
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        responsive_toolbar(tb, self, 900)

        def act(text, slot, icon, help_key=None, role="text"):
            a = QAction(text, self)
            a.triggered.connect(slot)
            tm.bind_icon(a, icon, role)
            tb.addAction(a)
            if help_key:
                tag(a, help_key)
            return a

        act("Add Table", lambda: self.add_table(None), "plus", "cmd:designer")
        act("Auto Layout", self.auto_layout, "layout")
        act("Fit", self.fit, "fit")
        tb.addSeparator()
        self.sql_act = act("Show SQL", self._toggle_sql, "query")
        self.sql_act.setCheckable(True)
        self.discard_act = act("Discard", self.discard, "discard")
        self.apply_act = act("Review && Apply", self.review_apply, "apply", "cmd:designer", role="on_accent")
        w = tb.widgetForAction(self.apply_act)
        if w is not None:
            w.setObjectName("Primary")

        self.changes = QListWidget()
        self.changes.setWordWrap(True)
        self.changes.setSpacing(2)
        self.sql_view = QPlainTextEdit()
        self.sql_view.setReadOnly(True)
        side = QWidget()
        sl = QVBoxLayout(side)
        sl.setContentsMargins(10, 8, 10, 8)
        t1 = QLabel("PENDING CHANGES")
        t1.setObjectName("PanelTitle")
        sl.addWidget(t1)
        sl.addWidget(self.changes, 1)
        self.sql_title = QLabel("SQL THAT WILL RUN")
        self.sql_title.setObjectName("PanelTitle")
        sl.addWidget(self.sql_title)
        sl.addWidget(self.sql_view, 2)
        side.setMinimumWidth(240)
        self.hint = QLabel()
        self.hint.setObjectName("Muted")
        self.hint.setWordWrap(True)
        self.hint.setContentsMargins(12, 6, 12, 6)

        split = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(0)
        ll.addWidget(self.hint)
        ll.addWidget(self.view, 1)
        split.addWidget(left)
        split.addWidget(side)
        split.setStretchFactor(0, 1)
        split.setSizes([900, 300])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(tb)
        lay.addWidget(split, 1)
        from dbide.ui.features import Gate

        advanced = not HelpCenter.get().beginner or Gate.get().allowed("sql_preview")
        self.sql_act.setChecked(advanced)
        self._toggle_sql()
        ThemeManager.get().on_change(self, lambda _t: self.scene.update())
        self.view.setProperty("dbide_help", "cmd:designer")
        self.reload()

    @property
    def session(self):
        return self.manager.session(self.conn_id)

    def title(self) -> str:
        dirty = self.model is not None and self.model.dirty
        return "Designer: " + (self.schema or (self.session.cfg.name if self.session else "")) + (" •" if dirty else "")

    def is_dirty(self) -> bool:
        return self.model is not None and self.model.dirty

    # ---- loading --------------------------------------------------------------
    def reload(self) -> None:
        s = self.session
        if not s:
            return
        schema = self.schema

        def work():
            names = s.tables(schema)
            return {n: s.table_def(n, schema) for n in names}

        roles = role_store().all_tables(self.conn_id, schema)

        def done(defs):
            self.model = DesignerModel(s.cfg.driver, defs, {t: r for t, r in roles.items() if t in defs})
            self.rebuild()
            self.fit(readable=True)

        run_async(work, done, lambda m: QMessageBox.warning(self, "Designer", m))

    def rebuild(self) -> None:
        E._sync_metrics()
        m = self.model
        for n, node in self.nodes.items():
            self.positions[n] = [node.pos().x(), node.pos().y()]
        self.scene.clear()
        self.nodes = {}
        self.link_line = None
        auto = layout(ErdModel({n: td.columns for n, td in m.tables.items()}, [],
                               [ErdRelation(n, fk.columns, fk.ref_table, fk.ref_columns) for n, td in m.tables.items() for fk in td.foreign_keys],
                               [], ""), E.WIDTH + 150, E.ROW_H, E.HEAD_H)
        for n, td in m.tables.items():
            status = "new" if not td.original_name or td.original_name not in m.originals else (
                "changed" if self._changed(td) else "")
            node = DesignNode(td, status, self)
            x, y = self.positions.get(n) or self.positions.get(td.original_name or "") or auto.get(n, (0, 0))
            node.setPos(x, y)
            self.scene.addItem(node)
            self.nodes[n] = node
        for n, td in m.tables.items():
            uniq = {ix.columns[0] for ix in td.indexes if ix.unique and len(ix.columns) == 1}
            for fk in td.foreign_keys:
                if fk.ref_table in self.nodes:
                    self.scene.addItem(DesignEdge(self.nodes[n], self.nodes[fk.ref_table], fk, fk.columns[0] in uniq, self))
        self.scene.setSceneRect(self.scene.itemsBoundingRect().adjusted(-400, -300, 400, 300))
        self._refresh_side()

    def _changed(self, td: TableDef) -> bool:
        from dbide.core.schema_plan import _changed

        o = self.model.originals.get(td.original_name or "")
        return bool(o and _changed(o, td))

    def _refresh_side(self) -> None:
        m = self.model
        self.changes.clear()
        for line in m.log:
            self.changes.addItem(line)
        if not m.log:
            self.changes.addItem("No changes yet.")
        try:
            steps = m.plan(self.session.driver)
            self.sql_view.setPlainText("\n\n".join(f"-- {s.title}\n" + ";\n".join(s.statements) + ";" for s in steps) or "-- nothing to apply")
        except Exception as exc:  # noqa: BLE001
            self.sql_view.setPlainText(f"-- can't generate SQL yet: {exc}")
        self.apply_act.setEnabled(m.dirty)
        self.discard_act.setEnabled(m.dirty)
        if not m.tables:
            self.hint.setText("Empty database. Click <b>Add Table</b> to start, then drag from a table's ● handle onto another table to link them.")
        else:
            self.hint.setText("Drag the ● handle on a table (or on a column) onto another table to create a relationship. "
                              "Double-click a table to edit it. Nothing changes in the database until <b>Review &amp; Apply</b>.")
        self.hint.setTextFormat(Qt.TextFormat.RichText)
        w = self.parent()
        while w is not None and not hasattr(w, "refresh_titles"):
            w = w.parent()
        if w is not None:
            w.refresh_titles()

    # ---- positions --------------------------------------------------------------
    def _key(self) -> str:
        return f"{self.conn_id}|{self.schema or ''}|designer"

    def _load_positions(self) -> dict:
        try:
            return json.loads((app_dir() / "erd_layouts.json").read_text()).get(self._key(), {})
        except (OSError, json.JSONDecodeError):
            return {}

    def positions_changed(self) -> None:
        self._save_timer.start()

    def _write_positions(self) -> None:
        path = app_dir() / "erd_layouts.json"
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            data = {}
        data[self._key()] = {n: [nd.pos().x(), nd.pos().y()] for n, nd in self.nodes.items()}
        path.write_text(json.dumps(data))

    def auto_layout(self) -> None:
        self.positions = {}
        for n in list(self.nodes):
            self.nodes[n].setPos(0, 0)
        self.nodes = {}
        self.rebuild()
        self.fit()

    def fit(self, readable: bool = False) -> None:
        """Show the whole design; with readable=True (on open) never shrink below 70% so text stays legible."""
        r = self.scene.itemsBoundingRect().adjusted(-40, -40, 40, 40)
        if r.isEmpty():
            return
        self.view.fitInView(r, Qt.AspectRatioMode.KeepAspectRatio)
        z = self.view.transform().m11()
        if z > 1.1 or (readable and z < 0.7):
            self.view.resetTransform()
            if z < 0.7:
                self.view.scale(0.7, 0.7)
            self.view.centerOn(r.center())

    def _toggle_sql(self) -> None:
        on = self.sql_act.isChecked()
        self.sql_view.setVisible(on)
        self.sql_title.setVisible(on)

    # ---- edits ------------------------------------------------------------------
    def _types(self) -> list[str]:
        return self.session.driver.common_types

    def add_table(self, pos: QPointF | None) -> None:
        name, ok = QInputDialog.getText(self, "Add table", "Table name (plural is conventional, e.g. customers):")
        if not ok or not name.strip():
            return
        td = self.model.add_table(name)
        if pos is None:
            pos = self.view.mapToScene(self.view.viewport().rect().center()) - QPointF(E.WIDTH / 2, 40)
        self.positions[td.name] = [pos.x(), pos.y()]
        self.rebuild()
        HelpCenter.get().show_html(f"New table {td.name}",
                                   f"<p>It starts with an <code>id</code> primary key that numbers rows automatically.</p>"
                                   "<p>Double-click <i>+ add column</i> to add fields, then drag the ● handle onto another table to link them.</p>")

    def add_column_to(self, table: str) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Add column to {table}")
        f = QFormLayout(dlg)
        name = QLineEdit()
        typ = QComboBox()
        typ.setEditable(True)
        typ.setMaxVisibleItems(20)
        for t in type_choices(self._types()):
            if t == "":
                typ.insertSeparator(typ.count())
            else:
                typ.addItem(t)
        typ.setCurrentText(self.model.text_type)
        req = QCheckBox("Required (NOT NULL)")
        uniq = QCheckBox("Unique (no duplicates)")
        f.addRow("Name", name)
        f.addRow("Type", typ)
        f.addRow("", req)
        f.addRow("", uniq)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        tag(typ, "field:type")
        if dlg.exec() and name.text().strip():
            is_new = self.nodes[table].status == "new"
            required = req.isChecked()
            if required and not is_new:
                # existing rows would have no value: keep it optional unless a default is given later
                QMessageBox.information(self, "Required column", "Existing rows need a value for a required column, so it was "
                                        "added as optional. Give it a default in the Advanced Designer to make it required.")
                required = False
            try:
                role = from_smart(typ.currentText())
                self.model.add_column(table, name.text(), typ.currentText(), nullable=not required, unique=uniq.isChecked(), role=role)
                if role is not None:
                    HelpCenter.get().show_html(role.label(), "<p>Stored as <code>" + html.escape(suggested_type(role, self.model.driver_key))
                                               + "</code>. " + ("Values typed in the data grid are hashed before saving." if role.kind == "password"
                                               else "Values are encrypted with this connection's key." if role.kind == "encrypted"
                                               else "Right-click a cell in the data grid → <b>Insert Image/Video…</b> to add files.") + "</p>")
            except ValueError as exc:
                QMessageBox.warning(self, "Add column", str(exc))
                return
            self.rebuild()

    def edit_table(self, table: str) -> None:
        dlg = TableDialog(self.model.tables[table], self._types(), self, self.model.roles.get(table, {}), self.model.driver_key)
        if not dlg.exec():
            return
        new = dlg.result_def()
        self.model.roles[table] = dict(dlg.roles_out)
        if new.name != table:
            try:
                self.model.rename_table(table, new.name)
            except ValueError as exc:
                QMessageBox.warning(self, "Rename", str(exc))
                return
            self.positions[new.name] = self.positions.pop(table, None) or [self.nodes[table].pos().x(), self.nodes[table].pos().y()]
        self.model.tables[new.name] = new
        self.model.log.append(f"Edit table {new.name}")
        self.rebuild()

    def rename_table(self, table: str) -> None:
        name, ok = QInputDialog.getText(self, "Rename table", "New name:", text=table)
        if ok and name.strip():
            try:
                new = self.model.rename_table(table, name)
            except ValueError as exc:
                QMessageBox.warning(self, "Rename", str(exc))
                return
            self.positions[new] = [self.nodes[table].pos().x(), self.nodes[table].pos().y()]
            self.rebuild()

    def delete_table(self, table: str) -> None:
        td = self.model.tables[table]
        existing = td.original_name in self.model.originals
        msg = (f"Delete table {table}? When you apply, the table and all its rows are permanently removed." if existing
               else f"Remove the new table {table} from the design?")
        if QMessageBox.question(self, "Delete table", msg) == QMessageBox.StandardButton.Yes:
            self.model.drop_table(table)
            self.rebuild()

    def remove_link(self, child: str, fk_name: str) -> None:
        self.model.remove_relationship(child, fk_name)
        self.rebuild()

    def set_on_delete(self, child: str, fk_name: str, action: str) -> None:
        for fk in self.model.tables[child].foreign_keys:
            if fk.name == fk_name:
                fk.on_delete = "" if action == "NO ACTION" else action
        self.model.log.append(f"{fk_name}: when the parent is deleted → {action}")
        self.rebuild()

    # ---- drag to link --------------------------------------------------------------
    def begin_link(self, node: DesignNode, column: str | None, scene_pos: QPointF) -> None:
        self.link_from = (node, column)
        start = node.anchor(column, True)
        self.link_line = QGraphicsLineItem(QLineF(start, scene_pos))
        t = ThemeManager.get().theme
        self.link_line.setPen(QPen(QColor(t.accent), 2, Qt.PenStyle.DashLine))
        self.link_line.setZValue(5)
        self.scene.addItem(self.link_line)
        HelpCenter.get().show_html("Linking tables", "<p>Drop on the table this one <b>belongs to</b> "
                                   "(e.g. drag from <i>orders</i> onto <i>customers</i>). You'll pick the kind of relationship next.</p>",
                                   teaching=False)

    def move_link(self, scene_pos: QPointF) -> None:
        line = self.link_line.line()
        line.setP2(scene_pos)
        self.link_line.setLine(line)
        for n in self.nodes.values():
            n.setOpacity(1.0 if n.sceneBoundingRect().contains(scene_pos) or n is self.link_from[0] else 0.85)

    def end_link(self, scene_pos: QPointF) -> None:
        source, column = self.link_from
        self.scene.removeItem(self.link_line)
        self.link_line = None
        self.link_from = None
        for n in self.nodes.values():
            n.setOpacity(1.0)
        target = next((n for n in self.nodes.values() if n.sceneBoundingRect().contains(scene_pos)), None)
        if target is None:
            return
        self.link(source.name, target.name, column)

    def link(self, child: str, parent: str, column: str | None = None) -> None:
        dlg = RelationshipDialog(self.model, child, parent, column, advanced=not HelpCenter.get().beginner, parent=self)
        if not dlg.exec():
            return
        try:
            summary = self.model.add_relationship(child, parent, dlg.kind, dlg.column.text() or None,
                                                  dlg.required.isChecked(), dlg.on_delete.currentData() or "")
        except ValueError as exc:
            QMessageBox.warning(self, "Link tables", str(exc))
            return
        if dlg.kind == "many_to_many":
            jn = [n for n in self.model.tables if n not in self.nodes][0]
            a, b = self.nodes[child].pos(), self.nodes[parent].pos()
            self.positions[jn] = [(a.x() + b.x()) / 2, max(a.y(), b.y()) + 220]
        self.rebuild()
        HelpCenter.get().show_html(REL_KINDS[dlg.kind], f"<p>{html.escape(summary)}</p>"
                                   "<p>Nothing has changed in the database yet. Click <b>Review &amp; Apply</b> when you're ready.</p>"
                                   "<p><a href='topic:concept:fk'>More about relationships</a></p>")

    # ---- apply ------------------------------------------------------------------------
    def discard(self) -> None:
        if QMessageBox.question(self, "Discard", "Throw away all pending changes?") == QMessageBox.StandardButton.Yes:
            self.reload()

    def review_apply(self) -> None:
        steps = self.model.plan(self.session.driver)
        if not steps:
            if self.model.roles_dirty:
                self._save_roles()
                QMessageBox.information(self, "Saved", "Column types saved. Special types (passwords, media…) are kept by the IDE, so no SQL was needed.")
                self.reload()
            return
        if not PlanDialog(steps, advanced=not HelpCenter.get().beginner, parent=self).exec():
            return
        s = self.session

        def work():
            for i, st in enumerate(steps, 1):
                try:
                    s.execute_ddl(st.statements)
                except Exception as exc:  # noqa: BLE001
                    orig = getattr(exc, "orig", None)
                    return i, st.title, f"{type(orig or exc).__name__}: {orig or exc}"
            return None

        def done(err):
            if not err:
                self._save_roles()
                from dbide.core.progress import progress

                if any(st.title.startswith("Create table") for st in steps):
                    progress().record("create_table")
                if any("link" in st.title or "links" in st.title for st in steps):
                    progress().record("relationship")
            if err:
                i, title, msg = err
                QMessageBox.critical(self, "Apply stopped", f"Step {i} failed: {title}\n\n{msg}\n\n"
                                     f"Steps before it were applied. The designer will reload from the database.")
            self.positions = {n: [nd.pos().x(), nd.pos().y()] for n, nd in self.nodes.items()}
            self._write_positions()
            self.applied.emit(self.conn_id, self.schema)
            self.reload()

        run_async(work, done, lambda m: QMessageBox.critical(self, "Apply failed", m))

    def _save_roles(self) -> None:
        m = self.model
        for orig in set(m.original_roles) - set(m.tables):  # renamed or dropped tables
            role_store().drop_table(self.conn_id, self.schema, orig)
        for t in m.tables:
            role_store().set_table(self.conn_id, self.schema, t, m.roles.get(t, {}))

    def shutdown(self) -> None:
        pass
