"""Entity-Relationship Diagram: tables, PK/FK relationships, and where media is stored."""
from __future__ import annotations

import html
import json
import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QDesktopServices,
    QFont,
    QFontMetrics,
    QImage,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
)
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QGraphicsItem,
    QGraphicsObject,
    QGraphicsPathItem,
    QGraphicsScene,
    QGraphicsView,
    QLabel,
    QMessageBox,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from dbide.core import media
from dbide.core.config import app_dir
from dbide.core.erd_model import ErdColumn, ErdModel, ErdRelation, MediaStorage, layout, load_erd
from dbide.core.roles import role_store
from dbide.ui.help_panel import HelpCenter, tag
from dbide.ui.theme import ThemeManager, responsive_toolbar
from dbide.ui.worker import run_async

ROW_H = 22
HEAD_H = 30
WIDTH = 260


def _sync_metrics() -> None:
    """Size table rows from the current font so zoom and font changes never clip text."""
    global ROW_H, HEAD_H, WIDTH
    fm = QFontMetrics(QFont())
    ROW_H = fm.height() + 8
    HEAD_H = fm.height() + 14
    WIDTH = max(260, fm.horizontalAdvance("x") * 40)


def _dark() -> bool:
    from dbide.ui.theme import theme

    return theme().dark


def _colors():
    from dbide.ui.theme import theme

    t = theme()
    return dict(bg=t.mix(t.surface, t.bg, 0.5), box=t.raised, head=t.accent, head_text=t.accent_text, text=t.text,
                sub=t.muted, line=t.mix(t.accent, t.muted, 0.35), pk=t.warning, fk=t.accent,
                media=t.syntax.get("ident", "#8a3ab9"), hi=t.warning, warn=t.danger,
                store=t.mix(t.raised, t.syntax.get("ident", "#8a3ab9"), 0.08), grid=t.mix(t.surface, t.text, 0.06),
                border=t.border)


class TableNode(QGraphicsObject):
    moved = Signal()

    def __init__(self, name: str, columns: list[ErdColumn], tab: "ErdTab"):
        super().__init__()
        self.name, self.columns, self.tab = name, columns, tab
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
                      | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.highlight: set[str] = set()
        self.edges: list = []
        self.setZValue(2)
        self.setToolTip(f"{name}\nDouble-click to open its data. Drag to move.")

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, WIDTH, HEAD_H + ROW_H * len(self.columns) + 6)

    def row_y(self, col: str) -> float:
        for i, c in enumerate(self.columns):
            if c.name == col:
                return HEAD_H + ROW_H * i + ROW_H / 2
        return HEAD_H / 2

    def anchor(self, col: str, right: bool) -> QPointF:
        return self.mapToScene(QPointF(WIDTH if right else 0, self.row_y(col)))

    def paint(self, p: QPainter, option, widget=None) -> None:
        c = _colors()
        r = self.boundingRect()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        sel = self.isSelected()
        shadow = QColor(0, 0, 0, 60 if _dark() else 22)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(shadow)
        p.drawRoundedRect(r.translated(0, 3), 9, 9)
        p.setPen(QPen(QColor(c["hi"] if sel else c["border"]), 2 if sel else 1))
        p.setBrush(QColor(c["box"]))
        p.drawRoundedRect(r, 9, 9)
        head = QPainterPath()
        head.setFillRule(Qt.FillRule.WindingFill)  # union the two shapes instead of cancelling the overlap
        head.addRoundedRect(QRectF(0, 0, WIDTH, HEAD_H), 9, 9)
        head.addRect(QRectF(0, HEAD_H - 9, WIDTH, 9))
        p.fillPath(head.simplified(), QColor(c["head"]))
        f = QFont()
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(c["head_text"]))
        p.drawText(QRectF(10, 0, WIDTH - 20, HEAD_H), Qt.AlignmentFlag.AlignVCenter, self.name)
        f.setBold(False)
        small = QFont(f)
        small.setPointSizeF(f.pointSizeF() * 0.9)
        for i, col in enumerate(self.columns):
            y = HEAD_H + ROW_H * i
            if col.name in self.highlight:
                hl = QColor(c["hi"])
                hl.setAlpha(55)
                p.fillRect(QRectF(1, y, WIDTH - 2, ROW_H), hl)
            badge, color = "", c["text"]
            if col.pk:
                badge, color = "PK", c["pk"]
            elif col.fk:
                badge, color = "FK", c["fk"]
            if badge:
                bf = QFont(small)
                bf.setBold(True)
                p.setFont(bf)
                p.setPen(QColor(color))
                p.drawText(QRectF(8, y, 26, ROW_H), Qt.AlignmentFlag.AlignVCenter, badge)
            p.setFont(f)
            p.setPen(QColor(c["text"]))
            if col.pk:
                bf = QFont(f)
                bf.setBold(True)
                p.setFont(bf)
            icon = ""
            if col.role is not None:
                icon = {"password": "🔑 ", "encrypted": "🔒 "}.get(col.role.kind, "🖼 " if col.role.storage == "blob" else "📁 ")
            elif col.media_guess:
                icon = "? "
            label = icon + col.name + ("" if col.nullable or col.pk else " *")
            p.drawText(QRectF(36, y, WIDTH - 130, ROW_H), Qt.AlignmentFlag.AlignVCenter, QFontMetrics(p.font()).elidedText(label, Qt.TextElideMode.ElideRight, WIDTH - 130))
            p.setFont(small)
            p.setPen(QColor(c["sub"]))
            p.drawText(QRectF(WIDTH - 100, y, 92, ROW_H), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                       QFontMetrics(small).elidedText(col.type, Qt.TextElideMode.ElideRight, 92))

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            for e in self.edges:
                e.update_path()
            self.moved.emit()
        return super().itemChange(change, value)

    def hoverMoveEvent(self, ev):
        i = int((ev.pos().y() - HEAD_H) // ROW_H)
        if 0 <= i < len(self.columns):
            self.tab.explain_column(self.name, self.columns[i])

    def mouseDoubleClickEvent(self, ev):
        self.tab.open_table.emit(self.name)


def _arrow_marks(p: QPainterPath, end: QPointF, direction: float, many: bool, size: float = 10) -> None:
    """Crow's-foot (many) or a bar (one) at `end`, pointing along `direction` (+1 right, -1 left)."""
    x, y = end.x(), end.y()
    back = x - direction * size * 1.4
    if many:
        p.moveTo(back, y)
        p.lineTo(x, y - size * 0.7)
        p.moveTo(back, y)
        p.lineTo(x, y + size * 0.7)
        p.moveTo(back, y)
        p.lineTo(x, y)
    else:
        p.moveTo(back, y - size * 0.7)
        p.lineTo(back, y + size * 0.7)
        p.moveTo(back - direction * 4, y - size * 0.7)
        p.lineTo(back - direction * 4, y + size * 0.7)


class RelationEdge(QGraphicsPathItem):
    def __init__(self, rel: ErdRelation, child: TableNode, parent: TableNode, tab: "ErdTab"):
        super().__init__()
        self.rel, self.child, self.parent_node, self.tab = rel, child, parent, tab
        child.edges.append(self)
        parent.edges.append(self)
        self.setAcceptHoverEvents(True)
        self.setZValue(1)
        self.hover = False
        many = "one-to-one" if rel.one_to_one else "one-to-many"
        self.setToolTip(
            f"{rel.child}.{', '.join(rel.child_cols)} → {rel.parent}.{', '.join(rel.parent_cols)}  ({many})\n"
            "Double-click to write a JOIN query for this relationship."
        )
        self.update_path()

    def update_path(self) -> None:
        c, pn = self.child, self.parent_node
        cx = c.sceneBoundingRect().center().x()
        px = pn.sceneBoundingRect().center().x()
        if c is pn:  # self-reference: loop on the right side
            a = c.anchor(self.rel.child_cols[0], True)
            b = c.anchor(self.rel.parent_cols[0], True)
            path = QPainterPath(a)
            path.cubicTo(a + QPointF(60, 0), b + QPointF(60, 0), b)
            _arrow_marks(path, a, -1, not self.rel.one_to_one)
            _arrow_marks(path, b, -1, False)
            self.setPath(path)
            return
        child_right = px > cx
        a = c.anchor(self.rel.child_cols[0], child_right)
        b = pn.anchor(self.rel.parent_cols[0], not child_right)
        dx = max(40.0, abs(b.x() - a.x()) / 2)
        s = 1 if child_right else -1
        path = QPainterPath(a)
        path.cubicTo(a + QPointF(s * dx, 0), b - QPointF(s * dx, 0), b)
        _arrow_marks(path, a, -s, not self.rel.one_to_one)  # child end: many
        _arrow_marks(path, b, s, False)  # parent end: one
        self.setPath(path)

    def paint(self, p, option, widget=None):
        c = _colors()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(c["hi"] if self.hover else c["line"]), 2.6 if self.hover else 1.6))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(self.path())

    def shape(self):
        from PySide6.QtGui import QPainterPathStroker

        s = QPainterPathStroker()
        s.setWidth(10)
        return s.createStroke(self.path())

    def hoverEnterEvent(self, ev):
        self.hover = True
        self.child.highlight = set(self.rel.child_cols)
        self.parent_node.highlight = set(self.rel.parent_cols)
        self.child.update()
        self.parent_node.update()
        self.update()
        self.tab.explain_relation(self.rel)

    def hoverLeaveEvent(self, ev):
        self.hover = False
        self.child.highlight = set()
        self.parent_node.highlight = set()
        self.child.update()
        self.parent_node.update()
        self.update()

    def mouseDoubleClickEvent(self, ev):
        self.tab.join_requested.emit(self.tab.join_sql(self.rel))


class StorageNode(QGraphicsObject):
    """Where a media column's files physically live."""

    def __init__(self, info: MediaStorage, tab: "ErdTab"):
        super().__init__()
        self.info, self.tab = info, tab
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.setAcceptHoverEvents(True)
        self.edges: list = []
        self.lines = self._lines()
        self.setZValue(2)

    def _lines(self) -> list[tuple[str, str]]:
        i = self.info
        out = []
        if i.storage == "blob":
            out.append(("title", "🗄  Stored INSIDE the database"))
            out.append(("sub", i.location))
            out.append(("text", f"{i.rows} file(s) · {media.human_size(i.total_bytes)} in column {i.column}"))
        else:
            if not i.folders:
                out.append(("title", "📁  Files on disk"))
                out.append(("sub", "no paths stored yet"))
            for f in i.folders[:3]:
                out.append(("title", "📁  " + _short(str(f.folder))))
                out.append(("text", f"{f.referenced} file(s) linked · {media.human_size(f.total_bytes)}"))
                if f.missing:
                    out.append(("warn", f"⚠ {len(f.missing)} row(s) point to missing files"))
                if f.orphans:
                    out.append(("warn", f"⚠ {len(f.orphans)} file(s) on disk no row uses"))
            if len(i.folders) > 3:
                out.append(("sub", f"+ {len(i.folders) - 3} more folder(s)"))
        out.append(("sub", f"called from: {i.table}.{i.column}" + ("  (guessed: set a role)" if i.guessed else "")))
        return out

    def boundingRect(self):
        lh = ROW_H - 2
        return QRectF(0, 0, 300, 14 + lh * len(self.lines))

    def paint(self, p, option, widget=None):
        c = _colors()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(c["media"]), 1.4, Qt.PenStyle.DashLine))
        p.setBrush(QColor(c["store"]))
        p.drawRoundedRect(self.boundingRect(), 10, 10)
        y = 7
        for kind, text in self.lines:
            f = QFont()
            if kind == "title":
                f.setBold(True)
            if kind in ("sub",):
                f.setPointSizeF(f.pointSizeF() * 0.88)
            p.setFont(f)
            p.setPen(QColor({"title": c["media"], "sub": c["sub"], "warn": c["warn"]}.get(kind, c["text"])))
            p.drawText(QRectF(10, y, 280, ROW_H - 2), Qt.AlignmentFlag.AlignVCenter,
                       QFontMetrics(f).elidedText(text, Qt.TextElideMode.ElideMiddle, 280))
            y += ROW_H - 2

    def anchor(self) -> QPointF:
        return self.mapToScene(QPointF(0, 17))

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            for e in self.edges:
                e.update_path()
            self.tab.save_positions()
        return super().itemChange(change, value)

    def hoverEnterEvent(self, ev):
        self.tab.explain_storage(self.info)

    def mouseDoubleClickEvent(self, ev):
        if self.info.storage == "path" and self.info.folders:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.info.folders[0].folder)))


class StorageEdge(QGraphicsPathItem):
    def __init__(self, table: TableNode, column: str, store: StorageNode):
        super().__init__()
        self.table, self.column, self.store = table, column, store
        table.edges.append(self)
        store.edges.append(self)
        self.setZValue(0)
        self.update_path()

    def update_path(self):
        a = self.table.anchor(self.column, True)
        b = self.store.anchor()
        if b.x() < a.x():
            a = self.table.anchor(self.column, False)
            b = self.store.mapToScene(QPointF(300, 17))
        path = QPainterPath(a)
        dx = max(30.0, abs(b.x() - a.x()) / 2) * (1 if b.x() >= a.x() else -1)
        path.cubicTo(a + QPointF(dx, 0), b - QPointF(dx, 0), b)
        ang = math.atan2(0, 1 if dx > 0 else -1)
        tip = QPainterPath(b)
        tip.lineTo(b - QPointF(9 * math.cos(ang) - 5 * math.sin(ang), 5))
        tip.moveTo(b)
        tip.lineTo(b - QPointF(9 * math.cos(ang) + 5 * math.sin(ang), -5))
        path.addPath(tip)
        self.setPath(path)

    def paint(self, p, option, widget=None):
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(_colors()["media"]), 1.4, Qt.PenStyle.DashLine))
        p.drawPath(self.path())


def _short(path: str, n: int = 46) -> str:
    return path if len(path) <= n else "…" + path[-(n - 1):]


class ErdView(QGraphicsView):
    def __init__(self, scene):
        super().__init__(scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.auto_fit = True  # keep the whole diagram in view while resizing, until the user zooms
        self.on_resize = None

    def drawBackground(self, p, rect):
        c = _colors()
        p.fillRect(rect, QColor(c["bg"]))
        step = 24
        p.setPen(QPen(QColor(c["grid"]), 2))
        x0 = int(rect.left()) - int(rect.left()) % step
        y0 = int(rect.top()) - int(rect.top()) % step
        pts = [QPointF(x, y) for x in range(x0, int(rect.right()) + step, step) for y in range(y0, int(rect.bottom()) + step, step)]
        if len(pts) < 20000:
            p.drawPoints(pts)

    def wheelEvent(self, ev):
        if ev.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier):
            f = 1.15 if ev.angleDelta().y() > 0 else 1 / 1.15
            self.scale(f, f)
            self.auto_fit = False
        else:
            super().wheelEvent(ev)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self.auto_fit and self.on_resize:
            self.on_resize()


class ErdTab(QWidget):
    open_table = Signal(str)
    join_requested = Signal(str)
    designer_requested = Signal()

    def __init__(self, manager, conn_id: str, schema: str | None):
        super().__init__()
        self.manager, self.conn_id, self.schema = manager, conn_id, schema
        self.model: ErdModel | None = None
        self.nodes: dict[str, TableNode] = {}
        self.stores: dict[str, StorageNode] = {}
        self.show_media = True
        self.scene = QGraphicsScene(self)
        self.view = ErdView(self.scene)
        self.view.on_resize = self.fit
        self._save_timer = QTimer(self, singleShot=True, interval=600)
        self._save_timer.timeout.connect(self._write_positions)

        tb = QToolBar()
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)

        def act(text, slot, help_key=None, checkable=False, icon=None):
            a = QAction(text, self)
            if icon:
                ThemeManager.get().bind_icon(a, icon)
            a.setCheckable(checkable)
            a.triggered.connect(slot)
            tb.addAction(a)
            if help_key:
                tag(a, help_key)
            return a

        act("Refresh", self.reload, icon="refresh")
        act("Auto Layout", self.auto_layout, icon="layout")
        act("Zoom In", lambda: (self.view.scale(1.2, 1.2), setattr(self.view, "auto_fit", False)), icon="zoom_in")
        act("Zoom Out", lambda: (self.view.scale(1 / 1.2, 1 / 1.2), setattr(self.view, "auto_fit", False)), icon="zoom_out")
        act("Fit", lambda: (setattr(self.view, "auto_fit", True), self.fit()), icon="fit")
        self.media_act = act("Media Storage", self._toggle_media, checkable=True, icon="folder")
        self.media_act.setChecked(True)
        self.media_act.setToolTip("Show where image/video columns keep their files")
        act("Export…", self.export, icon="export")
        tb.addSeparator()
        from dbide.ui.features import Gate

        edit = act("Edit in Designer", Gate.get().guarded("designer", lambda: self.designer_requested.emit(), self), icon="designer")
        tag(edit, "cmd:designer")
        tb.setObjectName("TabToolbar")
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        responsive_toolbar(tb, self, 760)
        self.legend = QLabel()
        self.legend.setTextFormat(Qt.TextFormat.RichText)
        self.legend.setContentsMargins(12, 6, 12, 6)
        self.legend.setObjectName("Muted")
        self.legend.setWordWrap(True)
        self.legend.setText(
            "<b>PK</b> primary key · <b>FK</b> foreign key · * required · "
            "line: <b>|</b> one side, <b>crow's foot</b> many side · 🔑 password hash · 🔒 encrypted · "
            "🖼 media in DB · 📁 media file path · ? looks like media. "
            "Hover anything for an explanation; double-click a line to write the JOIN."
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(tb)
        lay.addWidget(self.legend)
        lay.addWidget(self.view, 1)
        self.view.setProperty("dbide_help", "concept:fk")
        ThemeManager.get().on_change(self, lambda _t: (self.scene.update(), self.view.viewport().update()))
        self.reload()

    @property
    def session(self):
        return self.manager.session(self.conn_id)

    def title(self) -> str:
        return f"Diagram: {self.schema or self.session.cfg.name if self.session else 'ERD'}"

    # ---- loading -----------------------------------------------------------
    def reload(self) -> None:
        s = self.session
        if not s:
            return
        roles = role_store().all_tables(self.conn_id, self.schema)
        self.scene.clear()
        self.nodes, self.stores = {}, {}
        loading = self.scene.addText("Loading diagram…")
        loading.setDefaultTextColor(QColor(_colors()["sub"]))

        def failed(m):
            self.scene.clear()
            self.scene.addText(f"Could not load: {m}")

        run_async(lambda: load_erd(s, self.schema, roles, with_media=True), self._build, failed)

    def _layout_key(self) -> str:
        return f"{self.conn_id}|{self.schema or ''}"

    def _saved_positions(self) -> dict:
        try:
            return json.loads((app_dir() / "erd_layouts.json").read_text()).get(self._layout_key(), {})
        except (OSError, json.JSONDecodeError):
            return {}

    def save_positions(self) -> None:
        if getattr(self, "_building", False):
            return
        self._save_timer.start()

    def _write_positions(self) -> None:
        path = app_dir() / "erd_layouts.json"
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            data = {}
        pos = {n: [node.pos().x(), node.pos().y()] for n, node in self.nodes.items()}
        pos.update({f"@store:{k}": [s.pos().x(), s.pos().y()] for k, s in self.stores.items()})
        data[self._layout_key()] = pos
        path.write_text(json.dumps(data))

    def _build(self, model: ErdModel) -> None:
        self._building = True
        try:
            self._build_scene(model)
        finally:
            self._building = False

    def _build_scene(self, model: ErdModel) -> None:
        _sync_metrics()
        self.model = model
        self.scene.clear()
        self.nodes, self.stores = {}, {}
        self.scene.setBackgroundBrush(QBrush(QColor(_colors()["bg"])))
        if not model.tables:
            t = self.scene.addText("No tables yet. Use Create Table to make one, or try Help → Lessons.")
            t.setDefaultTextColor(QColor(_colors()["sub"]))
            return
        saved = self._saved_positions()
        auto = layout(model, WIDTH + 150, ROW_H, HEAD_H)
        for name, cols in model.tables.items():
            node = TableNode(name, cols, self)
            x, y = saved.get(name, auto[name])
            node.setPos(x, y)
            node.moved.connect(self.save_positions)
            self.scene.addItem(node)
            self.nodes[name] = node
        for rel in model.relations:
            if rel.child in self.nodes and rel.parent in self.nodes:
                self.scene.addItem(RelationEdge(rel, self.nodes[rel.child], self.nodes[rel.parent], self))
        right = max(n.sceneBoundingRect().right() for n in self.nodes.values())
        y = 0.0
        for info in model.media:
            key = f"{info.table}.{info.column}"
            sn = StorageNode(info, self)
            x0, y0 = saved.get(f"@store:{key}", (right + 120, y))
            sn.setPos(x0, y0)
            y += sn.boundingRect().height() + 30
            self.scene.addItem(sn)
            self.stores[key] = sn
            self.scene.addItem(StorageEdge(self.nodes[info.table], info.column, sn))
            sn.setVisible(self.show_media)
        self._apply_media_visibility()
        self.fit()

    def _toggle_media(self) -> None:
        self.show_media = self.media_act.isChecked()
        self._apply_media_visibility()

    def _apply_media_visibility(self) -> None:
        for it in self.scene.items():
            if isinstance(it, (StorageNode, StorageEdge)):
                it.setVisible(self.show_media)

    def auto_layout(self) -> None:
        if not self.model:
            return
        auto = layout(self.model, WIDTH + 150, ROW_H, HEAD_H)
        for n, node in self.nodes.items():
            node.setPos(*auto[n])
        right = max((n.sceneBoundingRect().right() for n in self.nodes.values()), default=0)
        y = 0.0
        for sn in self.stores.values():
            sn.setPos(right + 120, y)
            y += sn.boundingRect().height() + 30
        self.save_positions()
        self.fit()

    def fit(self) -> None:
        r = self.scene.itemsBoundingRect().adjusted(-40, -40, 40, 40)
        self.scene.setSceneRect(r)
        self.view.fitInView(r, Qt.AspectRatioMode.KeepAspectRatio)
        z = self.view.transform().m11()
        if z > 1.2 or z < 0.6:  # keep labels readable; very large diagrams scroll instead
            self.view.resetTransform()
            if z < 0.6:
                self.view.scale(0.6, 0.6)
            self.view.centerOn(r.center())

    def export(self) -> None:
        path, flt = QFileDialog.getSaveFileName(self, "Export diagram", "diagram.png", "PNG image (*.png);;SVG (*.svg)")
        if not path:
            return
        r = self.scene.itemsBoundingRect().adjusted(-20, -20, 20, 20)
        if path.lower().endswith(".svg") or "SVG" in flt:
            from PySide6.QtSvg import QSvgGenerator

            gen = QSvgGenerator()
            gen.setFileName(path)
            gen.setSize(QSize(int(r.width()), int(r.height())))
            gen.setViewBox(QRectF(0, 0, r.width(), r.height()))
            p = QPainter(gen)
            self.scene.render(p, QRectF(0, 0, r.width(), r.height()), r)
            p.end()
        else:
            img = QImage(int(r.width() * 2), int(r.height() * 2), QImage.Format.Format_ARGB32)
            img.fill(QColor(_colors()["bg"]))
            p = QPainter(img)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            self.scene.render(p, QRectF(0, 0, img.width(), img.height()), r)
            p.end()
            if not img.save(path):
                QMessageBox.warning(self, "Export", "Could not save the image.")

    # ---- teaching ------------------------------------------------------------
    def join_sql(self, rel: ErdRelation) -> str:
        drv = self.session.driver
        c, p = rel.child[:1].lower() or "c", rel.parent[:1].lower() or "p"
        if c == p:
            p = p + "2"
        on = " AND ".join(f"{c}.{drv.quote(cc)} = {p}.{drv.quote(pc)}" for cc, pc in zip(rel.child_cols, rel.parent_cols))
        body = (f"SELECT {c}.*, {p}.*\nFROM {drv.qualify(rel.child, self.schema)} AS {c}\n"
                f"JOIN {drv.qualify(rel.parent, self.schema)} AS {p} ON {on}")
        return (
            f"-- Each {rel.child} row points to one {rel.parent} row via {', '.join(rel.child_cols)}.\n"
            f"-- INNER JOIN keeps only matching pairs; change to LEFT JOIN to keep every {rel.child} row.\n"
            + drv.select_limit(body, 100) + ";\n"
        )

    def explain_relation(self, rel: ErdRelation) -> None:
        cc, pc = ", ".join(rel.child_cols), ", ".join(rel.parent_cols)
        kind = "one-to-one" if rel.one_to_one else "one-to-many"
        many = (f"Each <b>{rel.parent}</b> row can have <b>at most one</b> {rel.child} row"
                if rel.one_to_one else f"One <b>{rel.parent}</b> row can have <b>many</b> {rel.child} rows")
        od = rel.on_delete or "NO ACTION"
        body = (
            f"<p><code>{html.escape(rel.child)}.{html.escape(cc)}</code> → <code>{html.escape(rel.parent)}.{html.escape(pc)}</code></p>"
            f"<p>This is a <b>{kind}</b> relationship. {many}, and every {rel.child} row points to one {rel.parent} row.</p>"
            f"<p>The database refuses a {cc} value that doesn't exist in {rel.parent}.{pc}. "
            f"Deleting a {rel.parent} row that still has {rel.child} rows: <b>{html.escape(od)}</b>.</p>"
            "<p>To combine them in a query, follow the line with a JOIN:</p>"
            f"<pre>{html.escape(self.join_sql(rel))}</pre><p><i>Double-click the line to open this query.</i></p>"
        )
        HelpCenter.get().show_html(f"Relationship: {rel.child} → {rel.parent}", body)

    def explain_column(self, table: str, col: ErdColumn) -> None:
        bits = [f"<p><code>{html.escape(table)}.{html.escape(col.name)}</code>: {html.escape(col.type)}"
                + ("" if col.nullable else ", required") + (", unique" if col.unique else "") + "</p>"]
        if col.pk:
            bits.append("<p><b>Primary key</b>: uniquely identifies each row. Other tables point here with foreign keys.</p>")
        if col.fk and self.model:
            for r in self.model.relations:
                if r.child == table and col.name in r.child_cols:
                    bits.append(f"<p><b>Foreign key</b> → <code>{html.escape(r.parent)}.{html.escape(', '.join(r.parent_cols))}</code>: "
                                "its value must match an existing row there.</p>")
        if col.role is not None:
            bits.append(f"<p><b>Role:</b> {html.escape(col.role.label())}</p>")
        elif col.media_guess:
            bits.append("<p>This column looks like it stores media. Set its role in <b>Design Table</b> so the IDE "
                        "can preview files and track where they're stored.</p>")
        key = f"{table}|{col.name}"
        if key != getattr(self, "_last_col", None):
            self._last_col = key
            HelpCenter.get().show_html(f"Column {col.name}", "".join(bits))

    def explain_storage(self, info: MediaStorage) -> None:
        if info.storage == "blob":
            where = (f"<p>The bytes of every file are stored <b>inside the database</b>, in column "
                     f"<code>{html.escape(info.table)}.{html.escape(info.column)}</code>.</p>"
                     f"<p>Physical location: <code>{html.escape(info.location)}</code></p>"
                     f"<p>{info.rows} file(s), {media.human_size(info.total_bytes)} in total.</p>")
        else:
            rows = []
            for f in info.folders:
                rows.append(
                    f"<li><code>{html.escape(str(f.folder))}</code>: {f.referenced} linked file(s), "
                    f"{media.human_size(f.total_bytes)}"
                    + (f"; <b>{len(f.missing)} missing</b> (the row points to a file that's gone)" if f.missing else "")
                    + (f"; <b>{len(f.orphans)} orphan(s)</b> (a file on disk no row uses)" if f.orphans else "")
                    + "</li>"
                )
            where = (f"<p>Rows store a <b>path</b> in <code>{html.escape(info.table)}.{html.escape(info.column)}</code>; "
                     "the actual files live on disk:</p><ul>" + ("".join(rows) or "<li>No paths saved yet.</li>") + "</ul>"
                     "<p>Double-click the box to open the folder.</p>")
        HelpCenter.get().show_html("Where is this media stored?", where + (
            "<p><i>This was guessed from the column name/type. Set a role in Design Table to confirm.</i></p>" if info.guessed else "")
            + "<p><a href='topic:concept:media'>BLOB vs file path: which should I use?</a></p>", teaching=False)
