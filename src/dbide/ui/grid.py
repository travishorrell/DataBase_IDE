"""Table models and a grid view shared by query results and the table data editor."""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QGuiApplication, QKeySequence, QPalette
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QMenu, QTableView

from dbide.core import crypto
from dbide.core.media import detect_mime, human_size, kind_from_mime
from dbide.core.transfer import to_text

MAX_CELL_CHARS = 500


def _display(value: Any) -> str:
    if isinstance(value, (bytes, bytearray, memoryview)):
        b = bytes(value)
        mime = detect_mime(b)
        if kind_from_mime(mime) in ("image", "video"):
            return f"{'🖼' if mime.startswith('image') else '🎬'} {mime} · {human_size(len(b))}"
    s = to_text(value)
    if len(s) > MAX_CELL_CHARS:
        s = s[:MAX_CELL_CHARS] + "…"
    return s.replace("\n", "↵ ")


class ResultModel(QAbstractTableModel):
    """Read-only rows."""

    def __init__(self, columns: list[str] | None = None, rows: list | None = None, parent=None):
        super().__init__(parent)
        self.columns = columns or []
        self.rows = rows or []
        self._null_font = QFont()
        self._null_font.setItalic(True)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.columns)

    def value(self, row: int, col: int):
        return self.rows[row][col]

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        v = self.value(index.row(), index.column())
        if role == Qt.ItemDataRole.DisplayRole:
            return "NULL" if v is None else _display(v)
        if role == Qt.ItemDataRole.EditRole:
            return "" if v is None else to_text(v)
        if role == Qt.ItemDataRole.ToolTipRole and v is not None:
            s = to_text(v)
            return s[:4000] if len(s) > 60 else None
        if role == Qt.ItemDataRole.FontRole and v is None:
            return self._null_font
        if role == Qt.ItemDataRole.ForegroundRole and v is None:
            return QGuiApplication.palette().brush(QPalette.ColorRole.PlaceholderText)
        if role == Qt.ItemDataRole.TextAlignmentRole and isinstance(v, (int, float)) and not isinstance(v, bool):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self.columns[section] if section < len(self.columns) else None
        return str(section + 1)

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        self.layoutAboutToBeChanged.emit()

        def key(r):
            v = r[column]
            return (v is None, (0, v) if isinstance(v, (int, float)) else (1, to_text(v).lower()))

        try:
            self.rows.sort(key=key, reverse=order == Qt.SortOrder.DescendingOrder)
        except TypeError:
            self.rows.sort(key=lambda r: to_text(r[column]), reverse=order == Qt.SortOrder.DescendingOrder)
        self.layoutChanged.emit()


class EditableTableModel(ResultModel):
    """Rows of one table with staged edits, inserts and deletes."""

    NEW_COLOR = QColor(80, 180, 90, 60)
    EDIT_COLOR = QColor(230, 180, 40, 70)
    DEL_COLOR = QColor(220, 70, 70, 60)

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.primary_key: list[str] = []
        self.editable = False
        self.roles: dict[int, Any] = {}  # column index -> ColumnRole
        self.revealed: dict[tuple[int, int], str] = {}  # decrypted values shown on request
        self.reset_page([], [], [])

    def set_roles(self, roles_by_name: dict) -> None:
        self.roles = {i: roles_by_name[c] for i, c in enumerate(self.columns) if c in roles_by_name}
        if self.rows:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.rows) - 1, len(self.columns) - 1))

    def _role_display(self, r: int, c: int, role_obj, role: int):
        v = self.value(r, c)
        pending = (r, c) in self.edits
        if role_obj.kind == "password":
            if role == Qt.ItemDataRole.EditRole:
                return ""  # type a new password; the hash is never edited by hand
            if v is None:
                return None
            if pending and not crypto.detect_hash(v):
                return "•••••••• (will be hashed on Apply)"
            algo = crypto.detect_hash(v)
            if role == Qt.ItemDataRole.ToolTipRole:
                return f"{v}\n\nOne-way hash: the real password can't be recovered. Type here to set a new password."
            return f"🔑 {crypto.ALGORITHMS[algo].label} hash" if algo else f"⚠ not hashed: {to_text(v)}"
        if role_obj.kind == "encrypted":
            if (r, c) in self.revealed:
                return self.revealed[(r, c)]
            if role == Qt.ItemDataRole.EditRole:
                return "" if crypto.is_encrypted(v) else to_text(v) if v is not None else ""
            if v is None:
                return None
            if crypto.is_encrypted(v):
                algo = crypto.encrypted_algo(v)
                label = crypto.ALGORITHMS[algo].label if algo in crypto.ALGORITHMS else algo
                if role == Qt.ItemDataRole.ToolTipRole:
                    return f"Encrypted with {label}. Right-click → Reveal to decrypt with this connection's key."
                return f"🔒 encrypted ({label})"
            return f"{to_text(v)}  (will be encrypted on Apply)" if pending else f"⚠ not encrypted: {to_text(v)}"
        if role_obj.kind == "media":
            if v is None:
                return None
            if role == Qt.ItemDataRole.ToolTipRole:
                return "Double-click to view. Right-click → Insert Image/Video… to replace."
            if isinstance(v, (bytes, bytearray, memoryview)):
                return _display(v) if role == Qt.ItemDataRole.DisplayRole else None
            if role == Qt.ItemDataRole.DisplayRole:
                return "📁 " + str(v).replace("\\", "/").rsplit("/", 1)[-1]
        return "__default__"

    def reset_page(self, columns, rows, primary_key) -> None:
        self.beginResetModel()
        self.columns = columns
        self.rows = rows
        self.primary_key = primary_key
        self.editable = bool(primary_key)
        self.edits: dict[tuple[int, int], Any] = {}  # (row, col) -> new value
        self.new_rows: set[int] = set()
        self.deleted: set[int] = set()
        self.revealed = {}
        self.endResetModel()

    @property
    def dirty(self) -> bool:
        return bool(self.edits or self.new_rows or self.deleted)

    def value(self, row, col):
        return self.edits.get((row, col), self.rows[row][col])

    def flags(self, index):
        base = super().flags(index)
        rob = self.roles.get(index.column())
        if rob is not None and rob.kind == "media":
            return base  # media cells are filled with Insert Image/Video…, not typed
        if self.editable and index.row() not in self.deleted:
            return base | Qt.ItemFlag.ItemIsEditable
        return base

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.BackgroundRole and index.isValid():
            r = index.row()
            if r in self.deleted:
                return QBrush(self.DEL_COLOR)
            if r in self.new_rows:
                return QBrush(self.NEW_COLOR)
            if (r, index.column()) in self.edits:
                return QBrush(self.EDIT_COLOR)
            return None
        rob = self.roles.get(index.column()) if index.isValid() else None
        if rob is not None and role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole, Qt.ItemDataRole.ToolTipRole):
            out = self._role_display(index.row(), index.column(), rob, role)
            if out != "__default__":
                if out is None and role == Qt.ItemDataRole.DisplayRole:
                    return "NULL"
                return out
        return super().data(index, role)

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if role != Qt.ItemDataRole.EditRole or not index.isValid():
            return False
        self.set_cell(index.row(), index.column(), value)
        return True

    def set_cell(self, row: int, col: int, value) -> None:
        rob = self.roles.get(col)
        if rob is not None and rob.kind in ("password", "encrypted") and value == "":
            return  # leaving a secret cell empty keeps the stored value
        self.revealed.pop((row, col), None)
        original = self.rows[row][col]
        if row not in self.new_rows and value is not None and original is not None and to_text(original) == value:
            self.edits.pop((row, col), None)
        elif row not in self.new_rows and value is None and original is None:
            self.edits.pop((row, col), None)
        else:
            self.edits[(row, col)] = value
        idx = self.index(row, col)
        self.dataChanged.emit(idx, idx)

    def add_row(self) -> int:
        r = len(self.rows)
        self.beginInsertRows(QModelIndex(), r, r)
        self.rows.append([None] * len(self.columns))
        self.new_rows.add(r)
        self.endInsertRows()
        return r

    def toggle_delete(self, rows: list[int]) -> None:
        for r in sorted(rows, reverse=True):
            if r in self.new_rows:
                self.beginRemoveRows(QModelIndex(), r, r)
                self.rows.pop(r)
                self.new_rows = {x - 1 if x > r else x for x in self.new_rows if x != r}
                self.edits = {
                    (x - 1 if x > r else x, c): v for (x, c), v in self.edits.items() if x != r
                }
                self.deleted = {x - 1 if x > r else x for x in self.deleted}
                self.endRemoveRows()
            elif r in self.deleted:
                self.deleted.discard(r)
            else:
                self.deleted.add(r)
        if self.rows:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.rows) - 1, len(self.columns) - 1))

    def changes(self) -> tuple[list[tuple[dict, dict]], list[dict], list[dict]]:
        pk_idx = [self.columns.index(c) for c in self.primary_key]

        def keys(r):
            return {self.columns[i]: self.rows[r][i] for i in pk_idx}

        updates, inserts, deletes = [], [], []
        by_row: dict[int, dict] = {}
        for (r, c), v in self.edits.items():
            by_row.setdefault(r, {})[self.columns[c]] = v
        for r in sorted(self.deleted):
            deletes.append(keys(r))
        for r, vals in sorted(by_row.items()):
            if r not in self.new_rows and r not in self.deleted:
                updates.append((keys(r), vals))
        for r in sorted(self.new_rows):
            inserts.append(by_row.get(r, {}))
        return updates, inserts, deletes


class GridView(QTableView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setAlternatingRowColors(True)
        self.setWordWrap(False)
        self.setSortingEnabled(False)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.horizontalHeader().setStretchLastSection(False)
        self.setShowGrid(False)
        self.setFrameShape(QTableView.Shape.NoFrame)
        self.verticalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._sync_row_height()
        from dbide.ui.theme import ThemeManager

        ThemeManager.get().on_change(self, lambda _t: self._sync_row_height())
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.extra_actions: list[QAction] = []
        copy = QAction("Copy", self)
        copy.setShortcut(QKeySequence.StandardKey.Copy)
        copy.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        copy.triggered.connect(self.copy_selection)
        self.addAction(copy)
        self._copy = copy

    def _sync_row_height(self) -> None:
        import shiboken6

        if shiboken6.isValid(self):
            self.verticalHeader().setDefaultSectionSize(self.fontMetrics().height() + 10)

    def fit_columns(self) -> None:
        """Size columns to their content; if they don't fill the view, stretch the last one."""
        self.resizeColumnsToContents()
        n = self.model().columnCount() if self.model() else 0
        cap = max(220, int(self.viewport().width() * 0.45))
        for c in range(n):
            if self.columnWidth(c) > cap:
                self.setColumnWidth(c, cap)
        for c in range(n):  # a little breathing room
            self.setColumnWidth(c, self.columnWidth(c) + 12)

    def copy_selection(self, with_headers: bool = False) -> None:
        idxs = self.selectedIndexes()
        if not idxs:
            return
        model = self.model()
        rows = sorted({i.row() for i in idxs})
        cols = sorted({i.column() for i in idxs})
        chosen = {(i.row(), i.column()) for i in idxs}
        lines = []
        if with_headers:
            lines.append("\t".join(str(model.headerData(c, Qt.Orientation.Horizontal)) for c in cols))
        for r in rows:
            vals = []
            for c in cols:
                if (r, c) in chosen:
                    v = model.value(r, c) if hasattr(model, "value") else model.index(r, c).data()
                    vals.append("" if v is None else to_text(v).replace("\t", " ").replace("\n", " "))
                else:
                    vals.append("")
            lines.append("\t".join(vals))
        QGuiApplication.clipboard().setText("\n".join(lines))

    def _menu(self, pos) -> None:
        menu = QMenu(self)
        menu.addAction(self._copy)
        menu.addAction("Copy with Headers", lambda: self.copy_selection(True))
        if self.extra_actions:
            menu.addSeparator()
            for a in self.extra_actions:
                if a is None:
                    menu.addSeparator()
                else:
                    menu.addAction(a)
        menu.exec(self.viewport().mapToGlobal(pos))
