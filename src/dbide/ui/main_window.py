from __future__ import annotations

from dataclasses import replace

import shiboken6
from PySide6.QtCore import QByteArray, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QDialog,
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
from dbide.core import playground as P
from dbide.core.progress import progress
from dbide.core.templates import TEMPLATES, apply_template
from dbide.ui.features import Gate, LevelToast
from dbide.ui.progress_panel import ProgressPanel
from dbide.ui.worker import connect_owned, run_async


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

    def __init__(self, icon: str, title: str, text: str, on_click, parent=None):
        super().__init__(parent)
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
            w.hide()
            w.deleteLater()  # detaching with setParent(None) would leave stray top-level windows behind
        for w in items:
            if w.parent() is not self:
                w.setParent(self)
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
        connect_owned(self.manager.failed, self, self._connect_failed)
        connect_owned(self.manager.connected, self, self._connected)
        connect_owned(self.manager.disconnected, self, lambda _: self._update_status())
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
        progress_page = QWidget()
        pl = QVBoxLayout(progress_page)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(0)
        ph_, _ = _panel_header("Your Progress", [("sidebar", "Hide sidebar (⌘B)", lambda: self.set_sidebar(False))])
        pl.addWidget(ph_)
        self.progress_panel = ProgressPanel()
        pl.addWidget(self.progress_panel, 1)
        self.side_stack.addWidget(conn_page)
        self.side_stack.addWidget(lesson_page)
        self.side_stack.addWidget(progress_page)
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
        self._conn_waiters: dict[str, list] = {}
        connect_owned(self.manager.connected, self, self._practice_connected)
        connect_owned(self.manager.connected, self, self._run_conn_waiters)
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
        connect_owned(hc.mode_changed, self, self._mode_changed)
        tm.on_change(self, lambda _t: self._update_status())
        if hc.beginner:
            self._want_help = True
        else:
            self._want_help = False  # power users open help when they want it (the ? button / ⌘⌥H)
        gate = Gate.get()
        gate.on(gate.level_up, self, self._level_up)
        self._apply_mode(initial=True)
        if not settings().get("first_run_done"):
            settings().set("first_run_done", True)
            QTimer.singleShot(0, self._first_run)
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

        button("playground", "ph.flask", "Playground: safe sample data (⌘⇧P)", lambda: self.open_playground(), checkable=False)
        button("connections", "connections", "Connections (⌘1)", lambda: self._activity("connections"))
        button("lessons", "lessons", "Lessons (F1)", lambda: self._activity("lessons"))
        button("progress", "ph.trophy", "Your progress and unlocked tools", lambda: self._activity("progress"))
        lay.addStretch()
        button("help", "help", "Help panel (⌘⌥H)", lambda: self.set_help(not self.right_panel.isVisible(), user=True))
        button("mode", "moon", "Switch light / dark", lambda: ThemeManager.get().toggle_mode(), checkable=False)
        button("appearance", "appearance", "Appearance…", self.appearance_settings, checkable=False)
        mode_btn = self.activity_buttons["mode"]
        tm.on_change(mode_btn, lambda t: mode_btn.setIcon(tm.icon("sun" if t.dark else "moon", "muted")))
        mode_btn.setIcon(tm.icon("sun" if tm.theme.dark else "moon", "muted"))
        tag(self.activity_buttons["connections"], "cmd:new_connection")
        tag(self.activity_buttons["lessons"], "cmd:lessons")
        tag(self.activity_buttons["playground"], "cmd:playground")
        tag(self.activity_buttons["progress"], "cmd:progress")
        tag(self.activity_buttons["appearance"], "cmd:appearance")
        return bar

    def _activity(self, key: str) -> None:
        page = {"connections": 0, "lessons": 1, "progress": 2}[key]
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
        self.mode_badge = QLabel()
        self.mode_badge.setObjectName("Badge")
        self.mode_badge.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mode_badge.mousePressEvent = lambda _e: self.mode_button.showMenu()  # type: ignore[method-assign]
        sb.addPermanentWidget(self.mode_badge)
        sb.addPermanentWidget(self.zoom_label)
        sb.addPermanentWidget(self.status_chip)


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
        self._welcome_cards = cards
        cards.set_items([
            Card("ph.flask", "Playground", "Try SQL on realistic sample data. Nothing can break, and every change can be undone.", lambda: self.open_playground(), cards),
            Card("ph.squares-four", "Start from a template", "Online store, blog, school, library, tasks or recipes: tables, keys and sample data ready to go.", lambda: self.templates_dialog(), cards),
            Card("connect", "New connection", "Connect to a MySQL server or open / create a SQLite file.", self.new_connection, cards),
            Card("lessons", "Learn databases", "Ten guided lessons: tables, keys and JOINs, then the DBA essentials: indexes, transactions, backups, permissions.", self.show_lessons, cards),
            Card("security", "Admin Console", "Health checks, Analyze, Vacuum/Optimize, backups, running queries, users and grants.", lambda: self._admin_here(), cards),
            Card("query", "New query", "Open a SQL editor for the selected connection (⌘T).", lambda: self.new_query(), cards),
            Card("appearance", "Appearance", "Light or dark mode, themes, accent colour, fonts and zoom.", self.appearance_settings, cards),
        ])
        lay.addWidget(cards)
        by_title = {c.findChild(QLabel, "CardTitle").text(): c for c in cards.items}
        self._learning_cards = [by_title[t] for t in ("Playground", "Learn databases") if t in by_title]
        self._power_cards = [by_title[t] for t in ("Admin Console",) if t in by_title]
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
        configs = self.manager.store.all()[:8]
        signature = [(c.id, c.name, c.database, c.host) for c in configs]
        if signature == getattr(self, "_recent_signature", None):
            return  # nothing changed: don't rebuild the cards
        self._recent_signature = signature
        items = []
        for cfg in configs:
            icon = "database" if cfg.driver != "sqlite" else "table"
            from pathlib import Path

            from dbide.drivers import get_driver

            summary = f"SQLite · {Path(cfg.database).name}" if cfg.driver == "sqlite" else f"{get_driver(cfg.driver).label} · {cfg.summary()}"
            items.append(Card(icon, cfg.name, summary, lambda cid=cfg.id: self.connect_profile(cid), parent=self._recent))
        self._recent.set_items(items)
        self._recent_title.setVisible(bool(items))
        self._recent.setVisible(bool(items))

    # ---- menus ------------------------------------------------------------
    def _build_menus(self) -> None:
        mb = self.menuBar()
        tm = ThemeManager.get()

        gate = Gate.get()

        def act(menu, text, slot, shortcut=None, icon=None, feature=None):
            a = QAction(text, self)
            a.triggered.connect(gate.guarded(feature, slot, self) if feature else slot)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            if icon:
                tm.bind_icon(a, icon)
            menu.addAction(a)
            return a

        f = mb.addMenu("&File")
        self.new_conn_act = act(f, "New Connection…", self.new_connection, "Ctrl+N", "connect")
        self.new_query_act = act(f, "New Query", lambda: self.new_query(), "Ctrl+T", "query")
        self.template_act = act(f, "New Project from Template…", lambda: self.templates_dialog(), "Ctrl+Shift+N", "ph.squares-four")
        self.playground_act = act(f, "Open Playground", lambda: self.open_playground(), "Ctrl+Shift+P", "ph.flask")
        act(f, "Open SQL File…", self.open_sql_file, "Ctrl+O")
        act(f, "Save SQL", lambda: self._save_current(False), QKeySequence.StandardKey.Save, "save")
        act(f, "Save SQL As…", lambda: self._save_current(True), "Ctrl+Shift+S")
        f.addSeparator()
        act(f, "Close Tab", lambda: self.close_tab(self.tabs.currentIndex()), "Ctrl+W")
        f.addSeparator()
        act(f, "Quit", self.close, QKeySequence.StandardKey.Quit)

        d = mb.addMenu("&Database")
        self.create_table_act = act(d, "Create Table…", self._create_table_here, None, "table")
        self.erd_act = act(d, "Diagram (ERD)", self._erd_here, "Ctrl+D", "erd", "diagram")
        self.designer_act = act(d, "Schema Designer", self._designer_here, "Ctrl+Shift+D", "designer", "designer")
        self.admin_act = act(d, "Admin Console", self._admin_here, "Ctrl+Shift+A", "security", "admin")
        act(d, "Refresh Sidebar", self._refresh_here, "Ctrl+Shift+R", "refresh")
        d.addSeparator()
        act(d, "Disconnect All", self.manager.close_all)

        v = mb.addMenu("&View")
        mode_menu = v.addMenu("Mode")
        self.mode_group = QActionGroup(self)
        self.mode_group.setExclusive(True)
        self.learning_act = act(mode_menu, "Learning Mode: teaches you to be a database admin",
                                lambda: HelpCenter.get().set_mode("learning"), "Ctrl+Alt+1", "lessons")
        self.power_act = act(mode_menu, "Power User Mode: tools for database admins",
                             lambda: HelpCenter.get().set_mode("power"), "Ctrl+Alt+2", "ph.lightning")
        for a in (self.learning_act, self.power_act):
            a.setCheckable(True)
            self.mode_group.addAction(a)
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
        self.history_act = act(t, "Query History…", self.show_history, "Ctrl+Y", "history", "history")
        self.pw_tool_act = act(t, "Password && Encryption Tool…", lambda: self.password_tool(None), None, "key", "password_tool")
        self.security_act = act(t, "Security Settings…", self.security_settings, None, "security", "security")

        h = mb.addMenu("&Help")
        self.lessons_act = act(h, "Lessons", self.show_lessons, "F1", "lessons")
        self.concepts_act = act(h, "Help Home", lambda: (self.show_help(), HelpCenter.get().show_html("Help", HelpPanel.intro(), teaching=False)), None, "book")
        self.whats_this_act = act(h, "Explain Anything…", self.whats_this, "Shift+F1", "help")
        self.whats_this_act.setToolTip("Click this, then click any button, field or panel to learn what it does")
        act(h, "About DB IDE", lambda: QMessageBox.about(self, "DB IDE", f"DB IDE {__version__}\nA local IDE for SQLite and MySQL/MariaDB."))

        tb = QToolBar("Main")
        tb.setObjectName("main_toolbar")
        tb.setMovable(False)
        tb.setIconSize(QSize(18, 18))
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        for a in (self.playground_act, self.template_act, self.new_conn_act, self.new_query_act, self.designer_act, self.erd_act,
                  self.admin_act, self.history_act):
            tb.addAction(a)
        # short toolbar labels (menus keep the full names)
        self.admin_act.setIconText("Admin")
        for a, short in ((self.playground_act, "Playground"), (self.template_act, "Templates"), (self.new_conn_act, "Connect"),
                         (self.new_query_act, "Query"), (self.designer_act, "Designer"), (self.erd_act, "Diagram"),
                         (self.history_act, "History")):
            a.setIconText(short)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        tb.addAction(self.lessons_act)
        self.mode_button = QToolButton()
        self.mode_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.mode_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.mode_button.setMenu(mode_menu)
        self.mode_button.setToolTip("Switch between Learning mode and Power user mode")
        tag(self.mode_button, "cmd:mode")
        self.mode_button_action = tb.addWidget(self.mode_button)
        tb.addAction(self.whats_this_act)
        self.whats_this_act.setIconText("Explain")
        # progressive disclosure: hide locked toolbar buttons, lock-mark their menu items
        for a, feature in ((self.designer_act, "designer"), (self.erd_act, "diagram"), (self.history_act, "history"), (self.admin_act, "admin")):
            gate.bind_tool(tb, a, feature)
        for a, feature in ((self.designer_act, "designer"), (self.erd_act, "diagram"), (self.history_act, "history"), (self.admin_act, "admin"),
                           (self.pw_tool_act, "password_tool"), (self.security_act, "security")):
            gate.bind_menu(a, feature)
        self.main_toolbar = tb
        self.addToolBar(tb)
        for a, key in ((self.new_conn_act, "cmd:new_connection"), (self.new_query_act, "cmd:new_query"),
                       (self.create_table_act, "cmd:create_table"), (self.erd_act, "cmd:diagram"),
                       (self.designer_act, "cmd:designer"),
                       (self.history_act, "cmd:history"), (self.lessons_act, "cmd:lessons"),
                       (self.learning_act, "cmd:mode"), (self.power_act, "cmd:mode"), (self.pw_tool_act, "cmd:password_tool"),
                       (self.admin_act, "cmd:admin"),
                       (self.security_act, "cmd:security"), (self.playground_act, "cmd:playground"),
                       (self.template_act, "cmd:templates"), (self.whats_this_act, "cmd:whats_this")):
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

    def _target_conn(self) -> tuple[str | None, str | None]:
        """The connection a toolbar tool should act on: the current tab's or sidebar selection,
        or, if nothing is selected, the only open connection (ignoring the playground)."""
        conn, schema = self._context_conn()
        if conn and self.manager.is_open(conn):
            return conn, schema
        open_ = [cid for cid, _s in self.manager.open_sessions() if not P.is_playground(cid)]
        if len(open_) == 1:
            return open_[0], None
        return conn, schema

    def _context_conn(self) -> tuple[str | None, str | None]:
        w = self.tabs.currentWidget()
        if hasattr(w, "conn_id") and getattr(w, "conn_id", None):
            return w.conn_id, w.schema
        return self.tree.current_context()

    def _create_table_here(self) -> None:
        conn, schema = self._target_conn()
        if not conn or not self.manager.is_open(conn):
            QMessageBox.information(self, "Create Table", "Connect to a database first, then select it in the sidebar.")
            return
        s = self.manager.session(conn)
        if s.driver.supports_databases and not schema:
            schema = s.driver.default_schema(s.cfg)
            if not schema and s.driver.schema_required:
                QMessageBox.information(self, "Create Table", "Select a database in the sidebar first.")
                return
        self.design_table(conn, schema, None)

    def _designer_here(self) -> None:
        conn, schema = self._target_conn()
        if not conn or not self.manager.is_open(conn):
            QMessageBox.information(self, "Schema Designer", "Connect to a database and select it in the sidebar first.")
            return
        s = self.manager.session(conn)
        if s.driver.supports_databases and not schema:
            schema = s.driver.default_schema(s.cfg)
            if not schema and s.driver.schema_required:
                QMessageBox.information(self, "Schema Designer", "Select a database in the sidebar first.")
                return
        self.open_designer(conn, schema)

    def open_designer(self, conn_id: str, schema: str | None) -> None:
        from dbide.ui.schema_designer import DesignerTab

        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, DesignerTab) and (w.conn_id, w.schema) == (conn_id, schema):
                self.tabs.setCurrentIndex(i)
                return
        tab = DesignerTab(self.manager, conn_id, schema)
        tab.open_table.connect(lambda t: self.open_data(conn_id, schema, t))
        tab.advanced_requested.connect(lambda t: self.design_table(conn_id, schema, t))
        tab.applied.connect(lambda cid, sch: self.tree.refresh(cid, sch))
        self._add_tab(tab)

    def _erd_here(self) -> None:
        conn, schema = self._target_conn()
        if not conn or not self.manager.is_open(conn):
            QMessageBox.information(self, "Diagram", "Connect to a database and select it in the sidebar first.")
            return
        s = self.manager.session(conn)
        if s.driver.supports_databases and not schema:
            schema = s.driver.default_schema(s.cfg)
            if not schema and s.driver.schema_required:
                QMessageBox.information(self, "Diagram", "Select a database in the sidebar first.")
                return
        self.open_erd(conn, schema)

    # ---- teaching / security ------------------------------------------------
    def _mode_changed(self, m: str) -> None:
        self._apply_mode()
        learning = m == "learning"
        if learning:
            self.show_help()
        self.statusBar().showMessage(
            "Learning mode: lessons, explanations and safety prompts are on." if learning
            else "Power user mode: every tool is available; help covers the software only.", 6000)

    def _apply_mode(self, initial: bool = False) -> None:
        """Show what belongs to the current mode: lessons/progress/teaching in Learning, the full toolset in Power."""
        learning = HelpCenter.get().beginner
        self.learning_act.setChecked(learning)
        self.power_act.setChecked(not learning)
        self.mode_button.setText("Learning mode" if learning else "Power user")
        self.mode_button.setIcon(ThemeManager.get().icon("lessons" if learning else "ph.lightning", "accent"))
        self.mode_badge.setText("Learning mode" if learning else "Power user mode")
        self.mode_badge.setToolTip("Click to switch modes (also View → Mode)")
        # lessons, progress and the playground are learning tools; power users get a cleaner workspace
        self.lessons_act.setVisible(learning)
        self.playground_act.setVisible(learning)
        for key in ("lessons", "progress", "playground"):
            self.activity_buttons[key].setVisible(learning)
        if not learning and self.side_stack.currentIndex() in (1, 2):
            self.side_stack.setCurrentIndex(0)
            self._sync_activity()
        for card in getattr(self, "_learning_cards", []):
            if shiboken6.isValid(card):
                card.setVisible(learning)
        for card in getattr(self, "_power_cards", []):
            if shiboken6.isValid(card):
                card.setVisible(not learning)
        if not initial:
            self._want_help = learning
            self._apply_responsive()
            HelpCenter.get().show_html("Help", HelpPanel.intro(), teaching=False)
        Gate.get().refresh()

    def _first_run(self) -> None:
        dlg = ModeChooser(self)
        dlg.exec()
        HelpCenter.get().set_mode(dlg.choice)
        self._apply_mode()
        if dlg.choice == "learning":
            # straight into a safe playground, with the lessons alongside
            self.open_playground("store")
            self.show_lessons()

    def _admin_here(self) -> None:
        conn, schema = self._target_conn()
        if not conn or not self.manager.is_open(conn):
            QMessageBox.information(self, "Admin Console", "Connect to a database and select it in the sidebar first.")
            return
        s = self.manager.session(conn)
        if s.driver.supports_databases and not schema:
            schema = s.driver.default_schema(s.cfg)
        self.open_admin(conn, schema)

    def open_admin(self, conn_id: str, schema: str | None) -> None:
        from dbide.ui.admin_tab import AdminTab

        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, AdminTab) and (w.conn_id, w.schema) == (conn_id, schema):
                self.tabs.setCurrentIndex(i)
                return
        self._add_tab(AdminTab(self.manager, conn_id, schema))

    # ---- playground, templates, progress ----------------------------------------
    def open_playground(self, key: str | None = None) -> None:
        from dbide.ui.playground_tab import PlaygroundTab

        key = key if key in TEMPLATES else (settings().get("playground_last") or "store")
        settings().set("playground_last", key)
        progress().record("template")
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, PlaygroundTab):
                if w.key == key:
                    self.tabs.setCurrentIndex(i)
                    return
                # one playground at a time: switching dataset replaces the tab
                w.shutdown()
                self.tabs.removeTab(i)
                w.deleteLater()
                break
        tab = PlaygroundTab(self, key)
        self._add_tab(tab)
        self._start_playground_session(key)

    def _start_playground_session(self, key: str) -> None:
        pg = P.playground()
        cfg = pg.config(key)
        if self.manager.is_open(cfg.id):
            return

        def open_it(_=None):
            self.manager.open(cfg, None)

        if pg.exists(key):
            open_it()
        else:
            run_async(lambda: pg.create(key), open_it, lambda m: QMessageBox.critical(self, "Playground", m))

    def reset_playground(self, key: str) -> None:
        cid = P.conn_id(key)
        self.manager.close(cid)
        pg = P.playground()
        run_async(lambda: pg.create(key), lambda _p: self.manager.open(pg.config(key), None),
                  lambda m: QMessageBox.critical(self, "Reset", m))

    def when_connected(self, conn_id: str, fn) -> None:
        """Run fn() once conn_id is connected (now, if it already is)."""
        if self.manager.is_open(conn_id):
            fn()
            return
        self._conn_waiters.setdefault(conn_id, []).append(fn)

    def _run_conn_waiters(self, conn_id: str) -> None:
        for fn in self._conn_waiters.pop(conn_id, []):
            fn()

    def templates_dialog(self, preselect: str | None = None) -> None:
        from dbide.ui.templates_dialog import TemplateGallery

        dlg = TemplateGallery(self.manager, self, preselect)
        if not dlg.exec() or not dlg.choice:
            return
        if dlg.choice[0] == "playground":
            self.open_playground(dlg.choice[1])
        else:
            self.create_from_template(dlg.choice[1], dlg.choice[2])

    def create_from_template(self, key: str, opts: dict) -> None:
        tpl = TEMPLATES[key]

        def build(conn_id, schema):
            s = self.manager.session(conn_id)

            def done(counts):
                progress().record("template", "create_table", "relationship")
                self.tree.refresh(conn_id, schema)
                self.statusBar().showMessage(f"Created {tpl.name}: " + ", ".join(f"{t} ({n} rows)" for t, n in counts.items()), 10000)
                self.open_erd(conn_id, schema)
                ideas = "\n".join(f"--   {t.text}" for t in tpl.try_this if not t.sql)
                self.new_query(conn_id, schema, f"-- {tpl.name} is ready. Ask in plain English above, e.g.:\n{ideas}\n")

            run_async(lambda: apply_template(s, tpl, schema, opts.get("sample", True)), done,
                      lambda m: QMessageBox.critical(self, "Create project", m))

        if opts["target"] == "file":
            from pathlib import Path

            path = Path(opts["path"]).expanduser()
            if path.exists():
                QMessageBox.warning(self, "Create project", f"{path} already exists. Choose a new file name.")
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            cfg = ConnectionConfig(name=tpl.name if not any(c.name == tpl.name for c in self.manager.store.all()) else path.stem,
                                   driver="sqlite", database=str(path))
            self.manager.store.upsert(cfg)
            self.tree.reload_profiles()
            self.when_connected(cfg.id, lambda: build(cfg.id, None))
            self.manager.open(cfg, None)
        else:
            build(opts["conn_id"], opts.get("schema"))

    def add_sqlite_project(self, path, name: str, copy_roles_from: str | None = None) -> None:
        cfg = ConnectionConfig(name=f"{name} (from Playground)", driver="sqlite", database=str(path))
        self.manager.store.upsert(cfg)
        if copy_roles_from:
            data = role_store().data.get(copy_roles_from, {})
            role_store().data[cfg.id] = {k: dict(v) for k, v in data.items()}
            role_store()._save()
        self.tree.reload_profiles()
        self.manager.open(cfg, None)
        self.statusBar().showMessage(f"Saved to {path} and added to your connections.", 8000)

    def whats_this(self) -> None:
        from PySide6.QtWidgets import QWhatsThis

        QWhatsThis.enterWhatsThisMode()
        self.statusBar().showMessage("Click anything to learn what it is. Press Esc to cancel.", 6000)

    def show_progress(self) -> None:
        self.side_stack.setCurrentIndex(2)
        self.set_sidebar(True)

    def _level_up(self, level: int) -> None:
        if hasattr(self, "_toast") and self._toast is not None and shiboken6.isValid(self._toast):
            self._toast.close()
        self._toast = LevelToast(self, level, self.show_progress)
        from dbide.core.progress import FEATURES, LEVELS

        feats = "".join(f"<li><b>{f.title}</b>: {f.why}</li>" for f in FEATURES.values() if f.level == level)
        HelpCenter.get().show_html(f"New level: {LEVELS[level][1]}", f"<p>You've learned enough to use more tools:</p><ul>{feats}</ul>"
                                   "<p>See everything in <b>Your Progress</b> (trophy icon).</p>", hold=10)

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
        tab.designer_requested.connect(lambda: self.open_designer(conn_id, schema))
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


class ModeChooser(QDialog):
    """First launch: how do you want to use DB IDE?"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Welcome to DB IDE")
        self.setMinimumWidth(640)
        self.choice = "learning"
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(14)
        hero = QLabel("How would you like to use DB IDE?")
        hero.setObjectName("Hero")
        f = hero.font()
        f.setPointSizeF(f.pointSizeF() * 1.5)
        hero.setFont(f)
        lay.addWidget(hero)
        sub = QLabel("You can switch any time from <b>View → Mode</b> or the mode button in the toolbar.")
        sub.setObjectName("Muted")
        lay.addWidget(sub)
        row = QHBoxLayout()
        row.setSpacing(14)
        self.cards = []
        row.addWidget(Card("lessons", "Learning mode",
                           "Learn to be a database admin: a safe playground, ten lessons from your first table to indexes, "
                           "transactions, backups and permissions, explanations everywhere, and tools that unlock as you grow.",
                           lambda: self._pick("learning")))
        row.addWidget(Card("ph.lightning", "Power user mode",
                           "For database admins: every tool from the start (Admin Console, designers, query plans), "
                           "no prompts, help about the software only.", lambda: self._pick("power")))
        lay.addLayout(row)
        for i in range(row.count()):
            card = row.itemAt(i).widget()
            card.setMinimumHeight(190)  # room for the wrapped description
            card.setMinimumWidth(280)
        self.setMinimumHeight(330)

    def _pick(self, m: str) -> None:
        self.choice = m
        self.accept()
