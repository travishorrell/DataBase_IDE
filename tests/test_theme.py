"""Themes, light/dark mode, zoom, the Appearance dialog and responsive layout."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QPalette  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from dbide.core.config import ConnectionConfig, ConnectionStore  # noqa: E402
from dbide.core.history import QueryHistory  # noqa: E402
from dbide.core.settings import settings  # noqa: E402
from dbide.ui.connections import ConnectionManager  # noqa: E402
from dbide.ui.main_window import MainWindow  # noqa: E402
from dbide.ui.theme import THEMES, ThemeManager, build_stylesheet, make_icon, pref  # noqa: E402


@pytest.fixture
def tm(qapp):
    t = ThemeManager.get()
    t.apply(qapp)
    yield t
    settings().set("appearance_mode", "system")
    settings().set("zoom", 1.0)


@pytest.mark.parametrize("key", list(THEMES))
def test_every_theme_applies(tm, qapp, key):
    t = THEMES[key]
    settings().set("appearance_mode", "dark" if t.dark else "light")
    settings().set("dark_theme" if t.dark else "light_theme", key)
    tm.apply()
    assert tm.theme.key == key
    assert qapp.palette().color(QPalette.ColorRole.Base).name() == t.surface
    css = qapp.styleSheet()
    assert t.accent in css and "QTabBar::tab:selected" in css
    assert set(t.syntax) == {"kw", "fn", "str", "num", "cmt", "ident"}
    assert not make_icon("run", t.text).isNull()


def test_mode_toggle_accent_and_zoom(tm):
    settings().set("appearance_mode", "light")
    tm.apply()
    assert not tm.theme.dark
    seen = []
    tm.changed.connect(seen.append)
    tm.toggle_mode()
    assert tm.theme.dark and pref("appearance_mode") == "dark" and seen
    tm.set_pref("accent", "#db2777")
    assert tm.theme.accent == "#db2777"
    tm.set_pref("accent", "")
    base = tm.ui_font().pointSizeF()
    tm.zoom(+0.2)
    assert tm.ui_font().pointSizeF() == pytest.approx(base * 1.2, rel=0.01)
    assert tm.editor_font().pointSizeF() > 0
    tm.zoom(None)
    assert pref("zoom") == 1.0
    for _ in range(30):
        tm.zoom(+0.1)
    assert pref("zoom") == 2.0  # clamped


def test_stylesheet_density_changes_padding(tm):
    t = THEMES["daylight"]
    settings().set("density", "compact")
    compact = build_stylesheet(t, tm)
    settings().set("density", "spacious")
    spacious = build_stylesheet(t, tm)
    assert compact != spacious
    settings().set("density", "comfortable")


@pytest.fixture
def win(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    store = ConnectionStore(tmp_path / "c.json")
    cfg = ConnectionConfig(name="Shop", driver="sqlite", database=str(tmp_path / "shop.db"), id="shop")
    store.upsert(cfg)
    mgr = ConnectionManager(store, QueryHistory(tmp_path / "h.sqlite"))
    w = MainWindow(mgr)
    qtbot.addWidget(w)
    w.resize(1500, 900)
    w.show()
    with qtbot.waitSignal(mgr.connected, timeout=5000):
        w.connect_profile("shop")
    q = mgr.session("shop").query_connection()
    q.run("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT); CREATE TABLE orders (id INTEGER PRIMARY KEY);"
          "INSERT INTO customers (name) VALUES ('a'),('b'),('c');")
    q.close()
    w.tree.refresh("shop")
    yield w
    mgr.close_all()


def test_responsive_panels_and_toolbar(win, qtbot):
    qtbot.waitUntil(lambda: win.side_panel.isVisible() and win.right_panel.isVisible(), timeout=2000)
    assert win.main_toolbar.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextBesideIcon
    win.resize(1000, 800)
    qtbot.waitUntil(lambda: not win.right_panel.isVisible(), timeout=2000)
    assert win.side_panel.isVisible()
    assert win.main_toolbar.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
    win.resize(700, 600)
    qtbot.waitUntil(lambda: not win.side_panel.isVisible(), timeout=2000)
    # the user can still open the help panel on a narrow window
    win.set_help(True)
    assert win.right_panel.isVisible()
    win.resize(1500, 900)
    qtbot.waitUntil(lambda: win.side_panel.isVisible(), timeout=2000)
    # activity bar: clicking the active icon folds the sidebar
    win._activity("connections")
    assert not win.side_panel.isVisible()
    win._activity("lessons")
    assert win.side_panel.isVisible() and win.side_stack.currentIndex() == 1
    assert win.activity_buttons["lessons"].isChecked()


def test_tree_filter(win, qtbot):
    root = win.tree._conn_item("shop")
    qtbot.waitUntil(lambda: root.childCount() and root.child(0).childCount() == 2, timeout=5000)
    tables = root.child(0)
    win.tree_filter.setText("cust")
    assert [tables.child(i).isHidden() for i in range(2)] == [False, True]
    win.tree_filter.clear()
    assert not any(tables.child(i).isHidden() for i in range(2))


def test_theme_switch_restyles_open_tabs(win, qtbot, tm):
    q = win.new_query("shop", sql="SELECT * FROM customers;")
    q.run(all_=True)
    qtbot.waitUntil(lambda: not q.running, timeout=5000)
    win.open_erd("shop", None)
    erd = win.tabs.currentWidget()
    qtbot.waitUntil(lambda: len(erd.nodes) == 2, timeout=5000)
    settings().set("appearance_mode", "dark")
    settings().set("dark_theme", "midnight")
    tm.apply()
    assert q.editor.highlighter.kw_fmt.foreground().color().name() == THEMES["midnight"].syntax["kw"]
    from dbide.ui.erd import _colors

    assert _colors()["head"] == THEMES["midnight"].accent
    assert "midnight" not in win.help_panel.view.document().defaultStyleSheet()  # styled with colours, not names
    assert THEMES["midnight"].accent in win.help_panel.view.document().defaultStyleSheet()


def test_appearance_dialog_live_and_cancel(win, qtbot, tm):
    from dbide.ui.appearance import AppearanceDialog

    settings().set("appearance_mode", "light")
    tm.apply()
    dlg = AppearanceDialog(win)
    qtbot.addWidget(dlg)
    dark_picker = dlg.pickers[1]
    ember = next(c for c in dark_picker.cards if c.t.key == "ember")
    dark_picker._pick("ember")
    assert tm.theme.key == "ember" and pref("appearance_mode") == "dark" and ember.selected
    dlg.zoom.setValue(130)
    assert pref("zoom") == 1.3
    dlg.reject()  # cancel restores everything
    assert pref("appearance_mode") == "light" and pref("zoom") == 1.0 and not tm.theme.dark


def test_query_results_pane_grows_with_content(win, qtbot):
    q = win.new_query("shop", sql="SELECT 1;")
    qtbot.waitUntil(lambda: q.split.height() > 0, timeout=2000)
    before = q.split.sizes()[1]
    q.editor.setPlainText("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x < 40) SELECT x FROM n;")
    q.run(all_=True)
    qtbot.waitUntil(lambda: not q.running, timeout=5000)
    assert q.split.sizes()[1] > before
    # once the user drags the splitter, it stays where they put it
    q._user_split = True
    sizes = q.split.sizes()
    q._fit_split(True, 1)
    assert q.split.sizes() == sizes


def test_app_font_follows_settings(win, tm):
    settings().set("ui_font_size", 15)
    tm.apply()
    assert QApplication.font().pointSizeF() == pytest.approx(15)
    settings().set("ui_font_size", 0)
    tm.apply()
