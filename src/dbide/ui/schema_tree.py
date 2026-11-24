"""Sidebar: saved connections and their databases, tables, views, columns and indexes."""
from __future__ import annotations

from typing import TYPE_CHECKING

import shiboken6

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import QAbstractItemView, QMenu, QTreeWidget, QTreeWidgetItem, QTreeWidgetItemIterator

from dbide.ui.worker import run_async

if TYPE_CHECKING:
    from dbide.ui.main_window import MainWindow

ROLE = Qt.ItemDataRole.UserRole


class SchemaTree(QTreeWidget):
    def __init__(self, main: "MainWindow"):
        super().__init__()
        self.main = main
        self.manager = main.manager
        self.setHeaderHidden(True)
        self.setExpandsOnDoubleClick(False)  # double-click connects / opens data instead
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.itemExpanded.connect(self._expanded)
        self.itemDoubleClicked.connect(self._double_clicked)
        self.setIndentation(14)
        self.setAnimated(True)
        self.setUniformRowHeights(True)
        self._load_icons(None)
        from dbide.ui.theme import ThemeManager

        ThemeManager.get().on_change(self, self._load_icons)
        self.manager.connected.connect(self._on_connected)
        self.manager.disconnected.connect(self._on_disconnected)
        self.reload_profiles()

    ICON_FOR = {"server": ("database", "muted"), "file": ("table", "muted"), "db": ("database", "accent"),
                "folder": ("folder", "muted"), "table": ("table", "accent"), "view": ("columns", "success"),
                "column": ("minus", "muted"), "index": ("sort", "muted")}

    def _load_icons(self, _theme) -> None:
        from dbide.ui.theme import ThemeManager

        tm = ThemeManager.get()
        alias = {"columns": "ph.columns", "minus": "minus", "sort": "sort"}
        self.icons = {k: tm.icon(alias.get(n, n), role) for k, (n, role) in self.ICON_FOR.items()}
        self.icons["server_on"] = tm.icon("database", "success")
        self.icons["file_on"] = tm.icon("table", "success")
        it = QTreeWidgetItemIterator(self) if hasattr(self, "topLevelItemCount") else None
        while it is not None and it.value():
            item = it.value()
            key = item.data(0, ROLE + 1)
            if key in self.icons:
                item.setIcon(0, self.icons[key])
            it += 1

    def _set_icon(self, item: QTreeWidgetItem, key: str) -> None:
        item.setData(0, ROLE + 1, key)
        item.setIcon(0, self.icons[key])

    def filter(self, text: str) -> None:
        """Show only tables/views whose name contains `text` (loaded items only)."""
        needle = text.strip().lower()
        it = QTreeWidgetItemIterator(self)
        while it.value():
            item = it.value()
            d = self.info(item)
            if d.get("kind") in ("table", "view"):
                item.setHidden(bool(needle) and needle not in d["table"].lower())
            it += 1
        if needle:
            for i in range(self.topLevelItemCount()):
                top = self.topLevelItem(i)
                if self.manager.is_open(self.info(top).get("conn", "")):
                    top.setExpanded(True)

    # ---- helpers ----------------------------------------------------------
    @staticmethod
    def info(item: QTreeWidgetItem | None) -> dict:
        return (item.data(0, ROLE) or {}) if item else {}

    def _item(self, parent, text: str, icon: str, **data) -> QTreeWidgetItem:
        it = QTreeWidgetItem([text])
        self._set_icon(it, icon)
        it.setData(0, ROLE, data)
        if data.get("lazy"):
            it.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
        if isinstance(parent, QTreeWidgetItem):
            parent.addChild(it)
        else:
            self.addTopLevelItem(it)
        return it

    def _conn_item(self, conn_id: str) -> QTreeWidgetItem | None:
        for i in range(self.topLevelItemCount()):
            it = self.topLevelItem(i)
            if self.info(it).get("conn") == conn_id:
                return it
        return None

    def _style_conn(self, it: QTreeWidgetItem, connected: bool) -> None:
        f = QFont(it.font(0))
        f.setBold(connected)
        it.setFont(0, f)
        cfg = self.manager.store.get(self.info(it)["conn"])
        if cfg:
            base = "file" if cfg.driver == "sqlite" else "server"
            self._set_icon(it, base + "_on" if connected else base)
        it.setText(0, cfg.name if cfg else "?")
        it.setToolTip(0, f"{cfg.summary()}\n{'Connected' if connected else 'Double-click to connect'}" if cfg else "")
        it.setForeground(0, QBrush() if connected else QBrush(self.palette().color(self.palette().ColorRole.PlaceholderText)))

    # ---- building ---------------------------------------------------------
    def reload_profiles(self) -> None:
        self.clear()
        for cfg in self.manager.store.all():
            it = self._item(None, cfg.name, "file" if cfg.driver == "sqlite" else "server", kind="conn", conn=cfg.id, lazy=True)
            self._style_conn(it, self.manager.is_open(cfg.id))

    def _on_connected(self, conn_id: str) -> None:
        it = self._conn_item(conn_id)
        if it:
            self._style_conn(it, True)
            self._reset(it)
            it.setExpanded(True)

    def _on_disconnected(self, conn_id: str) -> None:
        it = self._conn_item(conn_id)
        if it:
            it.setExpanded(False)
            self._reset(it)
            self._style_conn(it, False)

    def _reset(self, it: QTreeWidgetItem) -> None:
        it.takeChildren()
        d = dict(self.info(it))
        d["loaded"] = False
        it.setData(0, ROLE, d)
        it.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)

    def refresh(self, conn_id: str, schema=None) -> None:
        """Reload a connection's tree (or one database under it), keeping it expanded."""
        root = self._conn_item(conn_id)
        if not root or not self.manager.is_open(conn_id):
            return
        target = root
        if schema is not None:
            for i in range(root.childCount()):
                ch = root.child(i)
                if self.info(ch).get("kind") == "db" and self.info(ch).get("schema") == schema:
                    target = ch
        expanded = target.isExpanded()
        self._reset(target)
        if expanded or target is root:
            target.setExpanded(True)
            self._expanded(target)

    def _expanded(self, it: QTreeWidgetItem) -> None:
        d = self.info(it)
        if not d.get("lazy") or d.get("loaded"):
            return
        conn_id = d["conn"]
        session = self.manager.session(conn_id)
        if not session:
            if d["kind"] == "conn":
                it.setExpanded(False)
                self.main.connect_profile(conn_id)
            return
        d = dict(d)
        d["loaded"] = True
        it.setData(0, ROLE, d)
        loading = QTreeWidgetItem(["Loading…"])
        loading.setDisabled(True)
        it.addChild(loading)
        kind, schema = d["kind"], d.get("schema")

        if kind == "conn" and session.driver.supports_databases:
            work = session.databases
        elif kind in ("conn", "db"):
            work = lambda: (session.tables(schema), session.views(schema))  # noqa: E731
        elif kind in ("table", "view"):
            work = lambda: (session.columns(d["table"], schema), session.indexes(d["table"], schema) if kind == "table" else [])  # noqa: E731
        else:
            return

        def done(result):
            if not shiboken6.isValid(it):
                return  # the tree was rebuilt while this was loading
            it.takeChildren()
            if kind == "conn" and session.driver.supports_databases:
                default = session.cfg.database
                for name in result:
                    db = self._item(it, name, "db", kind="db", conn=conn_id, schema=name, lazy=True)
                    if name in session.driver.system_schemas:
                        db.setForeground(0, QBrush(self.palette().color(self.palette().ColorRole.PlaceholderText)))
                    if name == default:
                        db.setExpanded(True)
            elif kind in ("conn", "db"):
                tables, views = result
                ft = self._item(it, f"Tables ({len(tables)})", "folder", kind="folder", conn=conn_id, schema=schema, folder="tables")
                for t in tables:
                    self._item(ft, t, "table", kind="table", conn=conn_id, schema=schema, table=t, lazy=True)
                fv = self._item(it, f"Views ({len(views)})", "folder", kind="folder", conn=conn_id, schema=schema, folder="views")
                for v in views:
                    self._item(fv, v, "view", kind="view", conn=conn_id, schema=schema, table=v, lazy=True)
                ft.setExpanded(True)
            else:
                cols, idxs = result
                for c in cols:
                    label = f"{c['name']}  {c['type']}" + ("" if c["nullable"] else "  NOT NULL")
                    ci = self._item(it, label, "column", kind="column", conn=conn_id, schema=schema, table=d["table"], column=c["name"])
                    if c["pk"]:
                        f = QFont(ci.font(0))
                        f.setBold(True)
                        ci.setFont(0, f)
                        ci.setText(0, "🔑 " + label)
                if idxs:
                    fi = self._item(it, f"Indexes ({len(idxs)})", "folder", kind="folder", conn=conn_id, schema=schema, folder="indexes")
                    for ix in idxs:
                        cols_txt = ", ".join(str(x) for x in ix["column_names"] if x)
                        self._item(fi, f"{ix['name']} ({cols_txt}){' UNIQUE' if ix.get('unique') else ''}", "index", kind="index", conn=conn_id)
            if it.childCount() == 0:
                it.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicatorWhenChildless)

        def failed(msg):
            if not shiboken6.isValid(it):
                return
            it.takeChildren()
            err = QTreeWidgetItem([f"Error: {msg[:200]}"])
            err.setForeground(0, QBrush(QColor("#d9534f")))
            err.setToolTip(0, msg)
            it.addChild(err)
            dd = dict(self.info(it))
            dd["loaded"] = False
            it.setData(0, ROLE, dd)

        run_async(work, done, failed)

    # ---- interaction ------------------------------------------------------
    def _double_clicked(self, it: QTreeWidgetItem, _col: int) -> None:
        d = self.info(it)
        kind = d.get("kind")
        if kind == "conn" and not self.manager.is_open(d["conn"]):
            self.main.connect_profile(d["conn"])
        elif kind in ("table", "view"):
            self.main.open_data(d["conn"], d.get("schema"), d["table"], kind == "view")

    def current_context(self) -> tuple[str | None, str | None]:
        d = self.info(self.currentItem())
        return d.get("conn"), d.get("schema")

    def _menu(self, pos) -> None:
        it = self.itemAt(pos)
        d = self.info(it)
        m = QMenu(self)
        main = self.main
        kind = d.get("kind")
        if not kind:
            m.addAction("New Connection…", main.new_connection)
            m.exec(self.viewport().mapToGlobal(pos))
            return
        conn = d["conn"]
        schema = d.get("schema")
        is_open = self.manager.is_open(conn)
        session = self.manager.session(conn)
        if kind == "conn":
            if is_open:
                m.addAction("New Query", lambda: main.new_query(conn))
                if session.driver.supports_databases:
                    m.addAction("Create Database…", lambda: main.create_database(conn))
                else:
                    m.addAction("Create Table…", lambda: main.design_table(conn, None, None))
                    m.addAction("Diagram (ERD)", lambda: main.open_erd(conn, None))
                m.addAction("Refresh", lambda: self.refresh(conn))
                m.addSeparator()
                m.addAction("Disconnect", lambda: self.manager.close(conn))
            else:
                m.addAction("Connect", lambda: main.connect_profile(conn))
            m.addSeparator()
            m.addAction("Edit Connection…", lambda: main.edit_connection(conn))
            m.addAction("Duplicate Connection", lambda: main.duplicate_connection(conn))
            m.addAction("Remove Connection", lambda: main.remove_connection(conn))
        elif not is_open:
            return
        elif kind == "db":
            m.addAction("New Query", lambda: main.new_query(conn, schema))
            m.addAction("Create Table…", lambda: main.design_table(conn, schema, None))
            m.addAction("Diagram (ERD)", lambda: main.open_erd(conn, schema))
            m.addAction("Refresh", lambda: self.refresh(conn, schema))
            m.addSeparator()
            m.addAction("Drop Database…", lambda: main.drop_database(conn, schema))
        elif kind == "folder":
            if d.get("folder") == "tables":
                m.addAction("Create Table…", lambda: main.design_table(conn, schema, None))
                m.addAction("Diagram (ERD)", lambda: main.open_erd(conn, schema))
            m.addAction("Refresh", lambda: self.refresh(conn, schema))
        elif kind in ("table", "view"):
            t = d["table"]
            view = kind == "view"
            q = session.driver.qualify(t, schema)
            m.addAction("Open Data", lambda: main.open_data(conn, schema, t, view))
            m.addAction("SELECT Top 100", lambda: main.new_query(conn, schema, f"SELECT *\nFROM {q}\nLIMIT 100;", run=True))
            m.addAction("Show DDL", lambda: main.show_ddl(conn, schema, t))
            if not view:
                m.addSeparator()
                m.addAction("Design Table…", lambda: main.design_table(conn, schema, t))
                m.addAction("Import Data…", lambda: main.import_data(conn, schema, t))
            m.addAction("Export Data…", lambda: main.export_table(conn, schema, t))
            m.addSeparator()
            if not view:
                m.addAction("Empty Table…", lambda: main.truncate_table(conn, schema, t))
            m.addAction(f"Drop {'View' if view else 'Table'}…", lambda: main.drop_table(conn, schema, t, view))
        elif kind == "column":
            name = d["column"]
            m.addAction("Copy Name", lambda: main.clipboard_text(name))
        if not m.isEmpty():
            m.exec(self.viewport().mapToGlobal(pos))
