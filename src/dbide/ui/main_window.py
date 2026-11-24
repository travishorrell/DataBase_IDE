from __future__ import annotations

from dataclasses import replace

import shiboken6
from PySide6.QtCore import QByteArray, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from dbide import __version__
from dbide.core.config import ConnectionConfig, get_password, set_password
from dbide.core.transfer import export_rows
from dbide.ui.connection_dialog import ConnectionDialog
from dbide.ui.connections import ConnectionManager
from dbide.ui.data_tab import DataTab
from dbide.core import lessons as L
from dbide.core import secrets
from dbide.core.roles import role_store
from dbide.core.settings import settings
from dbide.ui.dialogs import HistoryDialog, ImportDialog
from dbide.ui.erd import ErdTab
from dbide.ui.help_panel import HelpCenter, HelpPanel, tag
from dbide.ui.lessons import LessonPanel
from dbide.ui.query_tab import QueryTab, export_dialog
from dbide.ui.security_dialogs import PasswordToolDialog, SecuritySettingsDialog, unlock_vault
from dbide.ui.schema_tree import SchemaTree
from dbide.ui.table_builder import TableBuilderTab
from dbide.ui.theme import ThemeManager
from dbide.ui.worker import run_async


class Tabs(QTabWidget):
    def __init__(self):
        super().__init__()
        self.setTabsClosable(True)
        self.setMovable(True)
        self.setDocumentMode(True)
        self.setElideMode(Qt.TextElideMode.ElideRight)
        self.setUsesScrollButtons(True)

    def refresh_titles(self) -> None:
        for i in range(self.count()):
            w = self.widget(i)
            if hasattr(w, "title"):
                self.setTabText(i, w.title())


def _panel_header(title: str, buttons: list[tuple[str, str, object]]) -> tuple[QWidget, list[QToolButton]]:
    """A slim header: TITLE ............ [icon] [icon]"""
    w = QWidget()
    w.setObjectName("PanelHeader")
    lay = QHBoxLayout(w)
    lay.setContentsMargins(12, 8, 6, 4)
    lab = QLabel(title.upper())
    lab.setObjectName("PanelTitle")
    lab.setMinimumWidth(lab.sizeHint().width())
    lay.addWidget(lab)
    lay.addStretch()
    out = []
    tm = ThemeManager.get()
    for icon, tip, slot in buttons:
        b = QToolButton()
        b.setToolTip(tip)
        b.setAutoRaise(True)
        tm.bind_icon(b, icon, "muted")
        b.clicked.connect(slot)
        lay.addWidget(b)
        out.append(b)
    return w, out


class Card(QFrame):
    """A clickable card for the welcome page."""

    def __init__(self, icon: str, title: str, text: str, on_click):
        super().__init__()
        self.setObjectName("Card")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.on_click = on_click
        self.setMinimumWidth(220)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        top = QHBoxLayout()
        ic = QLabel()
        ic.setFixedSize(26, 26)
        self._icon, self._ic = icon, ic
        top.addWidget(ic)
        t = QLabel(title)
        t.setObjectName("CardTitle")
        top.addWidget(t, 1)
        lay.addLayout(top)
        d = QLabel(text)
        d.setObjectName("CardText")
        d.setWordWrap(True)
        d.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.MinimumExpanding)
        lay.addWidget(d)
        lay.addStretch()
        self._paint_icon(ThemeManager.get().theme)
        ThemeManager.get().on_change(self, self._paint_icon)

    def _paint_icon(self, _theme) -> None:
        if shiboken6.isValid(self._ic):
            self._ic.setPixmap(ThemeManager.get().icon(self._icon, "accent").pixmap(24, 24))

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and self.rect().contains(ev.position().toPoint()):
            self.on_click()


class FlowGrid(QWidget):
    """Lays cards out in as many columns as fit, reflowing when resized."""

    def __init__(self, min_col: int = 240):
        super().__init__()
        self.min_col = min_col
        self.items: list[QWidget] = []
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(12)
        self._cols = 0

    def set_items(self, items: list[QWidget]) -> None:
        for w in self.items:
            self.grid.removeWidget(w)
            w.setParent(None)
        self.items = items
        self._cols = 0
        self._reflow()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._reflow()

    def _reflow(self) -> None:
        cols = max(1, min(len(self.items) or 1, self.width() // self.min_col))
        if cols == self._cols:
            return
        self._cols = cols
        for w in self.items:
            self.grid.removeWidget(w)
        for i, w in enumerate(self.items):
            self.grid.addWidget(w, i // cols, i % cols)
        for c in range(4):
            self.grid.setColumnStretch(c, 1 if c < cols else 0)


class MainWindow(QMainWindow):
    SIDE_BREAKPOINT = 820  # narrower than this: the sidebar folds away
    HELP_BREAKPOINT = 1120  # narrower than this: the help panel folds away
    TEXT_BREAKPOINT = 1240  # narrower than this: toolbar buttons show icons only

    def __init__(self, manager: ConnectionManager | None = None):
        super().__init__()
        self.setWindowTitle("DB IDE")
        self.resize(1400, 860)
        self.setMinimumSize(640, 420)
        self.manager = manager or ConnectionManager()
        self.manager.failed.connect(self._connect_failed)
        self.manager.connected.connect(self._connected)
        self.manager.disconnected.connect(lambda _: self._update_status())
        tm = ThemeManager.get()
        if not tm._applied:
            tm.apply()

        self.tabs = Tabs()
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.currentChanged.connect(lambda _: self._update_status())

        # ---- left: activity bar + side panel (Connections / Lessons) ----
        self.tree = SchemaTree(self)
        self.lesson_panel = LessonPanel(self)
        self.side_stack = QStackedWidget()
        conn_page = QWidget()
        cl = QVBoxLayout(conn_page)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        header, _ = _panel_header("Connections", [
            ("connect", "New connection (⌘N)", self.new_connection),
            ("refresh", "Refresh (⌘⇧R)", self._refresh_here),
            ("sidebar", "Hide sidebar (⌘B)", lambda: self.set_sidebar(False)),
        ])
        cl.addWidget(header)
        self.tree_filter = QLineEdit()
        self.tree_filter.setPlaceholderText("Filter tables…")
        self.tree_filter.setClearButtonEnabled(True)
        self.tree_filter.textChanged.connect(self.tree.filter)
        fw = QWidget()
        fl = QHBoxLayout(fw)
        fl.setContentsMargins(10, 2, 10, 8)
        fl.addWidget(self.tree_filter)
        cl.addWidget(fw)
        cl.addWidget(self.tree, 1)
        lesson_page = QWidget()
        ll = QVBoxLayout(lesson_page)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(0)
        lh, _ = _panel_header("Lessons", [("sidebar", "Hide sidebar (⌘B)", lambda: self.set_sidebar(False))])
        ll.addWidget(lh)
        ll.addWidget(self.lesson_panel, 1)
        self.side_stack.addWidget(conn_page)
        self.side_stack.addWidget(lesson_page)
        self.side_panel = QWidget()
        self.side_panel.setObjectName("SidePanel")
        sp = QVBoxLayout(self.side_panel)
        sp.setContentsMargins(0, 0, 0, 0)
        sp.addWidget(self.side_stack)
        self.side_panel.setMinimumWidth(240)

        # ---- right: help panel ----
        self.help_panel = HelpPanel()
        self.right_panel = QWidget()
        self.right_panel.setObjectName("RightPanel")
        rp = QVBoxLayout(self.right_panel)
        rp.setContentsMargins(0, 0, 0, 0)
        rp.setSpacing(0)
        rh, _ = _panel_header("Help", [("panel_right", "Hide help panel (⌘⌥H)", lambda: self.set_help(False))])
        rp.addWidget(rh)
        rp.addWidget(self.help_panel, 1)
        self.right_panel.setMinimumWidth(240)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.side_panel)
        self.splitter.addWidget(self.tabs)
        self.splitter.addWidget(self.right_panel)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 0)

        self.activity = self._build_activity_bar()
        central = QWidget()
        hl = QHBoxLayout(central)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(0)
        hl.addWidget(self.activity)
        hl.addWidget(self.splitter, 1)
        self.setCentralWidget(central)

        self._practice_waiters: list = []
        self.manager.connected.connect(self._practice_connected)
        self._want_side = True
        self._want_help = bool(settings().get("help_panel_open") if settings().get("help_panel_open") is not None else True)
        self._help_forced_at: int | None = None
        self._side_forced_at: int | None = None

        self._build_menus()
        self._build_status_bar()
        self._welcome = self._welcome_widget()
        self.tabs.addTab(self._welcome, "Welcome")

        geo = settings().get("window_geometry")
        if geo:
            self.restoreGeometry(QByteArray.fromBase64(geo.encode()))
        sizes = settings().get("splitter_sizes")
        self._restored_sizes = sizes if isinstance(sizes, list) and len(sizes) == 3 else None
        hc = HelpCenter.get()
        hc.beginner_changed.connect(self._beginner_changed)
        tm.on_change(self, lambda _t: self._update_status())
        if hc.beginner:
            self._want_help = True
        if not self.manager.store.all():
            self.show_lessons()  # first launch: offer the lessons
        self._apply_responsive()
        QTimer.singleShot(0, self._unlock_secrets)

    # ---- activity bar / panels ------------------------------------------------
    def _build_activity_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("ActivityBar")
        lay = QVBoxLayout(bar)
        lay.setContentsMargins(0, 6, 0, 6)
        lay.setSpacing(2)
        tm = ThemeManager.get()
        self.activity_buttons: dict[str, QToolButton] = {}

        def button(key, icon, tip, slot, checkable=True):
            b = QToolButton()
            b.setToolTip(tip)
            b.setCheckable(checkable)
            b.setIconSize(QSize(22, 22))
            if key != "mode":
                tm.bind_icon(b, icon, "muted")
            b.clicked.connect(slot)
            lay.addWidget(b, 0, Qt.AlignmentFlag.AlignHCenter)
            self.activity_buttons[key] = b
            return b

        button("connections", "connections", "Connections (⌘1)", lambda: self._activity("connections"))
        button("lessons", "lessons", "Lessons (F1)", lambda: self._activity("lessons"))
        lay.addStretch()
        button("help", "help", "Help panel (⌘⌥H)", lambda: self.set_help(not self.right_panel.isVisible(), user=True))
        button("mode", "moon", "Switch light / dark", lambda: ThemeManager.get().toggle_mode(), checkable=False)
        button("appearance", "appearance", "Appearance…", self.appearance_settings, checkable=False)
        mode_btn = self.activity_buttons["mode"]
        tm.on_change(mode_btn, lambda t: mode_btn.setIcon(tm.icon("sun" if t.dark else "moon", "muted")))
        mode_btn.setIcon(tm.icon("sun" if tm.theme.dark else "moon", "muted"))
        tag(self.activity_buttons["connections"], "cmd:new_connection")
        tag(self.activity_buttons["lessons"], "cmd:lessons")
        tag(self.activity_buttons["appearance"], "cmd:appearance")
        return bar

    def _activity(self, key: str) -> None:
        page = {"connections": 0, "lessons": 1}[key]
        if self.side_panel.isVisible() and self.side_stack.currentIndex() == page:
            self.set_sidebar(False)  # clicking the active icon folds the sidebar, like VS Code
        else:
            self.side_stack.setCurrentIndex(page)
            self.set_sidebar(True)
        self._sync_activity()

    def _sync_activity(self) -> None:
        vis = self.side_panel.isVisible()
        self.activity_buttons["connections"].setChecked(vis and self.side_stack.currentIndex() == 0)
        self.activity_buttons["lessons"].setChecked(vis and self.side_stack.currentIndex() == 1)
        self.activity_buttons["help"].setChecked(self.right_panel.isVisible())

    def set_sidebar(self, on: bool) -> None:
        self._want_side = on
        self._side_forced_at = self.width() if on and self.width() < self.SIDE_BREAKPOINT else None
        self._apply_responsive()

    def set_help(self, on: bool, user: bool = True) -> None:
        self._want_help = on
        if user:
            settings().set("help_panel_open", on)
        self._help_forced_at = self.width() if on and self.width() < self.HELP_BREAKPOINT else None
        self._apply_responsive()

    def show_help(self) -> None:
        self.set_help(True)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        # a panel the user opened while the window was narrow stays open until it gets narrower still
        if self._help_forced_at is not None and self.width() < self._help_forced_at - 40:
            self._help_forced_at = None
        if self._side_forced_at is not None and self.width() < self._side_forced_at - 40:
            self._side_forced_at = None
        self._apply_responsive()

    def _apply_responsive(self) -> None:
        w = self.width()
        side = self._want_side and (w >= self.SIDE_BREAKPOINT or self._side_forced_at is not None)
        help_ = self._want_help and (w >= self.HELP_BREAKPOINT or self._help_forced_at is not None)
        changed = side != self.side_panel.isVisible() or help_ != self.right_panel.isVisible()
        self.side_panel.setVisible(side)
        self.right_panel.setVisible(help_)
        if changed or not getattr(self, "_sized", False):
            self._size_panels()
        style = Qt.ToolButtonStyle.ToolButtonTextBesideIcon if w >= self.TEXT_BREAKPOINT else Qt.ToolButtonStyle.ToolButtonIconOnly
        if hasattr(self, "main_toolbar"):
            self.main_toolbar.setToolButtonStyle(style)
        if hasattr(self, "activity_buttons"):
            self._sync_activity()

    def _size_panels(self) -> None:
        w = max(self.splitter.width(), self.width() - 50)
        default = [min(360, max(250, int(w * 0.2))), 0, min(420, max(260, int(w * 0.25)))]
        sizes = list(default)
        if not getattr(self, "_sized", False) and self._restored_sizes:
            # reuse the user's panel widths, but never a collapsed (0) one
            sizes = [r if isinstance(r, int) and r >= 150 else d for r, d in zip(self._restored_sizes, default)]
        sizes[0] = sizes[0] if self.side_panel.isVisible() else 0
        sizes[2] = sizes[2] if self.right_panel.isVisible() else 0
        sizes[1] = max(300, w - sizes[0] - sizes[2])
        self.splitter.setSizes(sizes)
        self._sized = True

    # ---- status bar ------------------------------------------------------------
    def _build_status_bar(self) -> None:
        sb = self.statusBar()
        sb.setSizeGripEnabled(False)
        self.status_chip = QLabel()
        self.status_chip.setObjectName("StatusChip")
        self.status_chip.setTextFormat(Qt.TextFormat.RichText)
        self.zoom_label = QLabel()
        self.zoom_label.setObjectName("Muted")
        self.beginner_badge = QLabel("Beginner mode")
        self.beginner_badge.setObjectName("Badge")
        self.beginner_badge.setToolTip("Extra explanations and safety checks are on (View → Beginner Mode)")
        sb.addPermanentWidget(self.beginner_badge)
        sb.addPermanentWidget(self.zoom_label)
        sb.addPermanentWidget(self.status_chip)
        self.beginner_badge.setVisible(HelpCenter.get().beginner)

    # ---- welcome page ----------------------------------------------------------
    def _welcome_widget(self) -> QWidget:
        outer = QScrollArea()
        outer.setWidgetResizable(True)
        outer.setFrameShape(QFrame.Shape.NoFrame)
        page = QWidget()
        page.setObjectName("Welcome")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(40, 36, 40, 36)
        lay.setSpacing(10)
        hero = QLabel("Welcome to DB IDE")
        hero.setObjectName("Hero")
        f = hero.font()
        f.setPointSizeF(f.pointSizeF() * 2.0)
        hero.setFont(f)
        sub = QLabel("Open, build and query SQLite and MySQL databases. New to databases? Start with the lessons.")
        sub.setObjectName("Muted")
        sub.setWordWrap(True)
        lay.addWidget(hero)
        lay.addWidget(sub)
        lay.addSpacing(16)
        start = QLabel("Start")
        start.setObjectName("PanelTitle")
        lay.addWidget(start)
        cards = FlowGrid(250)
        cards.set_items([
            Card("connect", "New connection", "Connect to a MySQL server or open / create a SQLite file.", self.new_connection),
            Card("lessons", "Learn databases", "Five guided lessons on a practice database: tables, keys, JOINs, passwords, media.", self.show_lessons),
            Card("query", "New query", "Open a SQL editor for the selected connection (⌘T).", lambda: self.new_query()),
            Card("appearance", "Appearance", "Light or dark mode, themes, accent colour, fonts and zoom.", self.appearance_settings),
        ])
        lay.addWidget(cards)
        lay.addSpacing(16)
        self._recent_title = QLabel("Saved connections")
        self._recent_title.setObjectName("PanelTitle")
        lay.addWidget(self._recent_title)
        self._recent = FlowGrid(250)
        lay.addWidget(self._recent)
        lay.addSpacing(16)
        tips = QLabel(
            "<b>Shortcuts</b> &nbsp; ⌘T new query · ⌘↩ run statement · ⌘⇧↩ run all · ⌃Space autocomplete · "
            "⌘D diagram · ⌘B toggle sidebar · ⌘+ / ⌘− zoom · ⌘⇧L light/dark"
        )
        tips.setObjectName("Muted")
        tips.setWordWrap(True)
        lay.addWidget(tips)
        lay.addStretch()
        outer.setWidget(page)
        outer.title = lambda: "Welcome"  # type: ignore[attr-defined]
        self._fill_recent()
        return outer

    def _fill_recent(self) -> None:
        if not hasattr(self, "_recent"):
            return
        items = []
        for cfg in self.manager.store.all()[:8]:
            icon = "database" if cfg.driver != "sqlite" else "table"
            from pathlib import Path

            summary = f"SQLite · {Path(cfg.database).name}" if cfg.driver == "sqlite" else f"MySQL · {cfg.summary()}"
            items.append(Card(icon, cfg.name, summary, lambda cid=cfg.id: self.connect_profile(cid)))
        self._recent.set_items(items)
        self._recent_title.setVisible(bool(items))
        self._recent.setVisible(bool(items))

    # ---- menus ------------------------------------------------------------
    def _build_menus(self) -> None:
        mb = self.menuBar()
        tm = ThemeManager.get()

        def act(menu, text, slot, shortcut=None, icon=None):
            a = QAction(text, self)
            a.triggered.connect(slot)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            if icon:
                tm.bind_icon(a, icon)
            menu.addAction(a)
            return a

        f = mb.addMenu("&File")
        self.new_conn_act = act(f, "New Connection…", self.new_connection, "Ctrl+N", "connect")
        self.new_query_act = act(f, "New Query", lambda: self.new_query(), "Ctrl+T", "query")
        act(f, "Open SQL File…", self.open_sql_file, "Ctrl+O")
        act(f, "Save SQL", lambda: self._save_current(False), QKeySequence.StandardKey.Save, "save")
        act(f, "Save SQL As…", lambda: self._save_current(True), "Ctrl+Shift+S")
        f.addSeparator()
        act(f, "Close Tab", lambda: self.close_tab(self.tabs.currentIndex()), "Ctrl+W")
        f.addSeparator()
        act(f, "Quit", self.close, QKeySequence.StandardKey.Quit)

        d = mb.addMenu("&Database")
        self.create_table_act = act(d, "Create Table…", self._create_table_here, None, "table")
        self.erd_act = act(d, "Diagram (ERD)", self._erd_here, "Ctrl+D", "erd")
        act(d, "Refresh Sidebar", self._refresh_here, "Ctrl+Shift+R", "refresh")
        d.addSeparator()
        act(d, "Disconnect All", self.manager.close_all)

        v = mb.addMenu("&View")
        self.beginner_act = act(v, "Beginner Mode", lambda on: HelpCenter.get().set_beginner(on), None, "beginner")
        self.beginner_act.setCheckable(True)
        self.beginner_act.setChecked(HelpCenter.get().beginner)
        v.addSeparator()
        act(v, "Connections", lambda: self._activity("connections") if not (self.side_panel.isVisible() and self.side_stack.currentIndex() == 0) else None, "Ctrl+1")
        act(v, "Toggle Sidebar", lambda: self.set_sidebar(not self.side_panel.isVisible()), "Ctrl+B", "sidebar")
        self.help_toggle_act = act(v, "Toggle Help Panel", lambda: self.set_help(not self.right_panel.isVisible()), "Ctrl+Alt+H", "panel_right")
        v.addSeparator()
        self.mode_act = act(v, "Toggle Light / Dark", tm.toggle_mode, "Ctrl+Shift+L", "moon")
        act(v, "Appearance…", self.appearance_settings, "Ctrl+,", "appearance")
        v.addSeparator()
        act(v, "Zoom In", lambda: tm.zoom(+0.1), QKeySequence.StandardKey.ZoomIn)
        act(v, "Zoom In ", lambda: tm.zoom(+0.1), "Ctrl+=").setVisible(False)
        act(v, "Zoom Out", lambda: tm.zoom(-0.1), QKeySequence.StandardKey.ZoomOut)
        act(v, "Actual Size", lambda: tm.zoom(None), "Ctrl+0")

        t = mb.addMenu("&Tools")
        self.history_act = act(t, "Query History…", self.show_history, "Ctrl+Y", "history")
        self.pw_tool_act = act(t, "Password && Encryption Tool…", lambda: self.password_tool(None), None, "key")
        self.security_act = act(t, "Security Settings…", self.security_settings, None, "security")

        h = mb.addMenu("&Help")
        self.lessons_act = act(h, "Lessons", self.show_lessons, "F1", "lessons")
        act(h, "Concepts", lambda: (self.show_help(), HelpCenter.get().show_html("Help", HelpPanel.intro())), None, "book")
        act(h, "About DB IDE", lambda: QMessageBox.about(self, "DB IDE", f"DB IDE {__version__}\nA local IDE for SQLite and MySQL/MariaDB."))

        tb = QToolBar("Main")
        tb.setObjectName("main_toolbar")
        tb.setMovable(False)
        tb.setIconSize(QSize(18, 18))
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        for a in (self.new_conn_act, self.new_query_act, self.create_table_act, self.erd_act, self.history_act):
            tb.addAction(a)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        tb.addAction(self.lessons_act)
        tb.addAction(self.beginner_act)
        self.main_toolbar = tb
        self.addToolBar(tb)
        for a, key in ((self.new_conn_act, "cmd:new_connection"), (self.new_query_act, "cmd:new_query"),
                       (self.create_table_act, "cmd:create_table"), (self.erd_act, "cmd:diagram"),
                       (self.history_act, "cmd:history"), (self.lessons_act, "cmd:lessons"),
                       (self.beginner_act, "cmd:beginner"), (self.pw_tool_act, "cmd:password_tool"),
                       (self.security_act, "cmd:security")):
            tag(a, key)
        tm.on_change(self.mode_act, lambda th: self.mode_act.setIcon(tm.icon("sun" if th.dark else "moon")))
        self.mode_act.setIcon(tm.icon("sun" if tm.theme.dark else "moon"))

    def appearance_settings(self) -> None:
        from dbide.ui.appearance import AppearanceDialog

        AppearanceDialog(self).exec()

    def _save_current(self, save_as: bool) -> None:
        w = self.tabs.currentWidget()
        if isinstance(w, QueryTab):
            w.save(save_as)
        elif isinstance(w, DataTab) and not save_as:
            w.apply()
        elif isinstance(w, TableBuilderTab) and not save_as:
            w.apply()

    def _context_conn(self) -> tuple[str | None, str | None]:
        w = self.tabs.currentWidget()
        if isinstance(w, (QueryTab, DataTab, TableBuilderTab, ErdTab)) and w.conn_id:
            return w.conn_id, w.schema
        return self.tree.current_context()

    def _create_table_here(self) -> None:
        conn, schema = self._context_conn()
        if not conn or not self.manager.is_open(conn):
            QMessageBox.information(self, "Create Table", "Connect to a database first, then select it in the sidebar.")
            return
        s = self.manager.session(conn)
        if s.driver.supports_databases and not schema:
            schema = s.cfg.database or None
            if not schema:
                QMessageBox.information(self, "Create Table", "Select a database in the sidebar first.")
                return
        self.design_table(conn, schema, None)

    def _erd_here(self) -> None:
        conn, schema = self._context_conn()
        if not conn or not self.manager.is_open(conn):
            QMessageBox.information(self, "Diagram", "Connect to a database and select it in the sidebar first.")
            return
        s = self.manager.session(conn)
        if s.driver.supports_databases and not schema:
            schema = s.cfg.database or None
            if not schema:
                QMessageBox.information(self, "Diagram", "Select a database in the sidebar first.")
                return
        self.open_erd(conn, schema)

    # ---- teaching / security ------------------------------------------------
    def _beginner_changed(self, on: bool) -> None:
        self.beginner_act.setChecked(on)
        self.beginner_badge.setVisible(on)
        if on:
            self.show_help()
        self.statusBar().showMessage("Beginner mode on: explanations and safety checks enabled." if on
                                     else "Beginner mode off.", 5000)

    def show_lessons(self) -> None:
        self.side_stack.setCurrentIndex(1)
        self.set_sidebar(True)

    def open_erd(self, conn_id: str, schema: str | None) -> None:
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, ErdTab) and (w.conn_id, w.schema) == (conn_id, schema):
                self.tabs.setCurrentIndex(i)
                w.reload()
                return
        tab = ErdTab(self.manager, conn_id, schema)
        tab.open_table.connect(lambda t: self.open_data(conn_id, schema, t))
        tab.join_requested.connect(lambda sql: self.new_query(conn_id, schema, sql))
        self._add_tab(tab)

    def visualize_join(self, conn_id: str, sql: str) -> None:
        from dbide.ui.join_viz import JoinVisualizer

        s = self.manager.session(conn_id)
        if s:
            dlg = JoinVisualizer(s, sql, self)
            dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dlg.show()

    def password_tool(self, conn_id: str | None) -> None:
        if conn_id is None:
            conn_id, _ = self._context_conn()
        PasswordToolDialog(self.manager, conn_id, self).exec()

    def security_settings(self) -> None:
        SecuritySettingsDialog(self).exec()

    def _unlock_secrets(self) -> None:
        st = secrets.store()
        if isinstance(st, secrets.VaultStore) and st.exists and not st.unlocked:
            if not unlock_vault(self, st):
                self.statusBar().showMessage("Vault locked: saved passwords and encryption keys are unavailable this session.", 10000)

    def ensure_practice(self, then) -> None:
        """Create/connect the lessons' practice database, then call `then()`."""
        if self.manager.is_open(L.PRACTICE_ID):
            then()
            return
        if not self.manager.store.get(L.PRACTICE_ID):
            self.manager.store.upsert(ConnectionConfig(name=L.PRACTICE_NAME, driver="sqlite",
                                                       database=str(L.practice_path()), id=L.PRACTICE_ID))
            self.tree.reload_profiles()
        self._practice_waiters.append(then)
        self.manager.open(self.manager.store.get(L.PRACTICE_ID), None)

    def _practice_connected(self, conn_id: str) -> None:
        if conn_id == L.PRACTICE_ID:
            waiters, self._practice_waiters = self._practice_waiters, []
            for fn in waiters:
                fn()

    def reset_practice(self) -> None:
        import shutil

        for i in reversed(range(self.tabs.count())):
            w = self.tabs.widget(i)
            if getattr(w, "conn_id", None) == L.PRACTICE_ID:
                if hasattr(w, "shutdown"):
                    w.shutdown()
                self.tabs.removeTab(i)
                w.deleteLater()
        self.manager.close(L.PRACTICE_ID)
        path = L.practice_path()
        path.unlink(missing_ok=True)
        shutil.rmtree(path.parent / f"{path.stem}_media", ignore_errors=True)
        role_store().data.pop(L.PRACTICE_ID, None)
        role_store().set_table(L.PRACTICE_ID, None, "_", {})
        self.tree.reload_profiles()

    def _refresh_here(self) -> None:
        conn, _ = self._context_conn()
        if conn:
            self.tree.refresh(conn)

    # ---- connections -------------------------------------------------------
    def new_connection(self) -> None:
        dlg = ConnectionDialog(self)
        if dlg.exec():
            cfg = dlg.result_cfg
            self.manager.store.upsert(cfg)
            set_password(cfg, dlg.password)
            self.tree.reload_profiles()
            self.manager.open(cfg, dlg.password)

    def edit_connection(self, conn_id: str) -> None:
        cfg = self.manager.store.get(conn_id)
        if not cfg:
            return
        dlg = ConnectionDialog(self, cfg)
        if dlg.exec():
            new = dlg.result_cfg
            self.manager.store.upsert(new)
            set_password(new, dlg.password)
            if self.manager.is_open(conn_id):
                self.manager.close(conn_id)
                self.manager.open(new, dlg.password)
            self.tree.reload_profiles()

    def duplicate_connection(self, conn_id: str) -> None:
        cfg = self.manager.store.get(conn_id)
        if cfg:
            import uuid

            copy = replace(cfg, id=uuid.uuid4().hex, name=cfg.name + " (copy)", save_password=cfg.save_password)
            self.manager.store.upsert(copy)
            set_password(copy, get_password(cfg))
            self.tree.reload_profiles()

    def remove_connection(self, conn_id: str) -> None:
        cfg = self.manager.store.get(conn_id)
        if cfg and QMessageBox.question(self, "Remove connection", f"Remove the saved connection “{cfg.name}”?\n(The database itself is not touched.)") == QMessageBox.StandardButton.Yes:
            self.manager.close(conn_id)
            self.manager.store.remove(conn_id)
            self.tree.reload_profiles()

    def connect_profile(self, conn_id: str) -> None:
        cfg = self.manager.store.get(conn_id)
        if not cfg or self.manager.is_open(conn_id):
            return
        password = None
        if cfg.driver != "sqlite":
            password = get_password(cfg)
            if password is None and not cfg.save_password:
                pw, ok = QInputDialog.getText(self, f"Connect to {cfg.name}", f"Password for {cfg.user or 'user'}@{cfg.host}:", QLineEdit.EchoMode.Password)
                if not ok:
                    return
                password = pw or None
        self.statusBar().showMessage(f"Connecting to {cfg.name}…")
        self.manager.open(cfg, password)

    def _connected(self, conn_id: str) -> None:
        s = self.manager.session(conn_id)
        self.statusBar().showMessage(f"Connected to {s.cfg.name} ({s.server_version})", 5000)
        self._update_status()

    def _connect_failed(self, conn_id: str, msg: str) -> None:
        cfg = self.manager.store.get(conn_id)
        self.statusBar().clearMessage()
        QMessageBox.critical(self, "Connection failed", f"Could not connect to {cfg.name if cfg else 'database'}.\n\n{msg}")

    def _update_status(self) -> None:
        if not hasattr(self, "status_chip"):
            return
        conn, schema = self._context_conn()
        s = self.manager.session(conn) if conn else None
        t = ThemeManager.get().theme
        if s:
            where = f" · {schema}" if schema else ""
            self.status_chip.setText(f"<span style='color:{t.success}'>●</span>&nbsp; {s.cfg.name}{where} · {s.server_version}")
        else:
            n = len(self.manager.sessions)
            self.status_chip.setText(f"<span style='color:{t.muted}'>●</span>&nbsp; "
                                     + (f"{n} connection(s) open" if n else "Not connected"))
        from dbide.ui.theme import pref

        z = pref("zoom")
        self.zoom_label.setText(f"{round(z * 100)}%" if abs(z - 1) > 0.01 else "")
        self._fill_recent()

    # ---- tabs ---------------------------------------------------------------
    def _add_tab(self, w: QWidget) -> None:
        if self.tabs.indexOf(self._welcome) >= 0 and self.tabs.count() == 1:
            self.tabs.removeTab(0)
        i = self.tabs.addTab(w, w.title())
        self.tabs.setCurrentIndex(i)
        if hasattr(w, "title_changed"):
            w.title_changed.connect(self.tabs.refresh_titles)

    def close_tab(self, index: int) -> None:
        if index < 0:
            return
        w = self.tabs.widget(index)
        if getattr(w, "is_dirty", lambda: False)():
            ans = QMessageBox.question(
                self, "Unsaved changes", f"“{w.title().rstrip(' •')}” has unsaved changes. Close anyway?",
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
        if hasattr(w, "shutdown"):
            w.shutdown()
        self.tabs.removeTab(index)
        if w is not self._welcome:
            w.deleteLater()

    def new_query(self, conn_id: str | None = None, schema: str | None = None, sql: str = "", run: bool = False) -> QueryTab:
        if conn_id is None:
            conn_id, schema = self._context_conn()
        tab = QueryTab(self.manager, conn_id, schema, sql)
        tab.schema_maybe_changed.connect(lambda cid: self.tree.refresh(cid))
        self._add_tab(tab)
        if run:
            tab.run(all_=True)
        return tab

    def open_sql_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open SQL", "", "SQL files (*.sql);;All files (*)")
        if path:
            tab = self.new_query()
            tab.load_file(path)

    def open_data(self, conn_id: str, schema: str | None, table: str, is_view: bool = False) -> None:
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, DataTab) and (w.conn_id, w.schema, w.table) == (conn_id, schema, table):
                self.tabs.setCurrentIndex(i)
                return
        self._add_tab(DataTab(self.manager, conn_id, table, schema, is_view))

    def design_table(self, conn_id: str, schema: str | None, table: str | None) -> None:
        tab = TableBuilderTab(self.manager, conn_id, schema, table)
        tab.applied.connect(lambda cid, sch: self.tree.refresh(cid, sch))
        self._add_tab(tab)

    def show_ddl(self, conn_id: str, schema: str | None, table: str) -> None:
        s = self.manager.session(conn_id)
        run_async(
            lambda: s.ddl(table, schema),
            lambda ddl: self.new_query(conn_id, schema, f"-- DDL for {table}\n{ddl}\n"),
            lambda m: QMessageBox.warning(self, "Show DDL", m),
        )

    def clipboard_text(self, text: str) -> None:
        QGuiApplication.clipboard().setText(text)

    # ---- destructive / bulk actions --------------------------------------------
    def _run_and_refresh(self, conn_id: str, schema, sql: str, done_msg: str) -> None:
        s = self.manager.session(conn_id)

        def ok(_):
            self.statusBar().showMessage(done_msg, 5000)
            self.tree.refresh(conn_id, schema)

        run_async(lambda: s.execute(sql), ok, lambda m: QMessageBox.critical(self, "Failed", m))

    def drop_table(self, conn_id: str, schema, table: str, view: bool) -> None:
        kind = "view" if view else "table"
        if QMessageBox.warning(
            self, f"Drop {kind}", f"Permanently drop {kind} “{table}”? This can't be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        s = self.manager.session(conn_id)
        if not view:
            role_store().drop_table(conn_id, schema, table)
        self._run_and_refresh(conn_id, schema, s.driver.drop_table_sql(table, schema, view), f"Dropped {table}")

    def truncate_table(self, conn_id: str, schema, table: str) -> None:
        if QMessageBox.warning(
            self, "Empty table", f"Delete every row in “{table}”? This can't be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        s = self.manager.session(conn_id)
        self._run_and_refresh(conn_id, schema, s.driver.truncate_table_sql(table, schema), f"Emptied {table}")

    def create_database(self, conn_id: str) -> None:
        name, ok = QInputDialog.getText(self, "Create Database", "Database name:")
        if ok and name.strip():
            s = self.manager.session(conn_id)
            self._run_and_refresh(conn_id, None, s.driver.create_database_sql(name.strip()), f"Created database {name}")

    def drop_database(self, conn_id: str, schema: str) -> None:
        typed, ok = QInputDialog.getText(
            self, "Drop Database", f"This permanently deletes database “{schema}” and all its tables.\nType its name to confirm:"
        )
        if ok and typed == schema:
            s = self.manager.session(conn_id)
            self._run_and_refresh(conn_id, None, s.driver.drop_database_sql(schema), f"Dropped database {schema}")
        elif ok:
            QMessageBox.information(self, "Drop Database", "Name didn't match; nothing was dropped.")

    def import_data(self, conn_id: str, schema, table: str) -> None:
        s = self.manager.session(conn_id)
        try:
            dlg = ImportDialog(s, table, schema, self)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Import", str(exc))
            return
        if dlg.exec():
            for i in range(self.tabs.count()):
                w = self.tabs.widget(i)
                if isinstance(w, DataTab) and (w.conn_id, w.schema, w.table) == (conn_id, schema, table):
                    w.reload()

    def export_table(self, conn_id: str, schema, table: str) -> None:
        choice = export_dialog(self, table)
        if not choice:
            return
        path, fmt = choice
        s = self.manager.session(conn_id)

        def work():
            cols, rows = s.iter_rows(table, schema)
            return export_rows(path, fmt, cols, rows, s.driver, table=table)

        run_async(
            work,
            lambda n: self.statusBar().showMessage(f"Exported {n} row(s) to {path}", 8000),
            lambda m: QMessageBox.critical(self, "Export failed", m),
        )

    def show_history(self) -> None:
        dlg = HistoryDialog(self.manager.history, self)
        dlg.open_sql.connect(lambda sql: self.new_query(sql=sql))
        dlg.exec()

    # ---- shutdown ---------------------------------------------------------
    def closeEvent(self, e) -> None:
        dirty = [self.tabs.widget(i) for i in range(self.tabs.count()) if getattr(self.tabs.widget(i), "is_dirty", lambda: False)()]
        if dirty and QMessageBox.question(
            self, "Quit", f"{len(dirty)} tab(s) have unsaved changes. Quit anyway?"
        ) != QMessageBox.StandardButton.Yes:
            e.ignore()
            return
        settings().set("window_geometry", bytes(self.saveGeometry().toBase64()).decode())
        settings().set("splitter_sizes", self.splitter.sizes())
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if hasattr(w, "shutdown"):
                w.shutdown()
        self.manager.close_all()
        e.accept()
