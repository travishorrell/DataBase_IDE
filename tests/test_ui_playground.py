"""GUI: playground (undo/reset/try-this), templates, progressive disclosure, concept tips."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtWidgets import QMessageBox, QToolTip  # noqa: E402

from dbide.core import playground as P  # noqa: E402
from dbide.core.config import ConnectionConfig, ConnectionStore  # noqa: E402
from dbide.core.history import QueryHistory  # noqa: E402
from dbide.core.progress import progress  # noqa: E402
from dbide.core.settings import settings  # noqa: E402
from dbide.ui.connections import ConnectionManager  # noqa: E402
from dbide.ui.features import Gate  # noqa: E402
from dbide.ui.main_window import MainWindow  # noqa: E402


@pytest.fixture
def win(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.StandardButton.Ok)
    settings().set("first_run_done", True)
    mgr = ConnectionManager(ConnectionStore(tmp_path / "c.json"), QueryHistory(tmp_path / "h.sqlite"))
    w = MainWindow(mgr)
    qtbot.addWidget(w)
    w.resize(1400, 900)
    w.show()
    yield w
    mgr.close_all()


def open_pg(win, qtbot, key="store"):
    from dbide.ui.playground_tab import PlaygroundTab

    win.open_playground(key)
    tab = win.tabs.currentWidget()
    assert isinstance(tab, PlaygroundTab)
    qtbot.waitUntil(lambda: tab.query_tab is not None and tab.tables.count() > 0, timeout=15000)
    return tab


def rows(win, key, sql):
    with win.manager.session(P.conn_id(key)).engine.connect() as c:
        return c.exec_driver_sql(sql).fetchall()


def test_playground_undo_sql_and_grid(win, qtbot):
    tab = open_pg(win, qtbot)
    assert "customers" in [tab.tables.item(i).data(Qt.ItemDataRole.UserRole) for i in range(tab.tables.count())]
    n = rows(win, "store", "SELECT COUNT(*) FROM order_items")[0][0]
    q = tab.query_tab
    q.editor.setPlainText("DELETE FROM customers;")  # refused: orders still point at customers (foreign key)
    q.run(all_=True)
    qtbot.waitUntil(lambda: not q.running, timeout=5000)
    assert "FOREIGN KEY" in q.messages.toPlainText().upper()
    q.editor.setPlainText("DELETE FROM order_items;")
    q.run(all_=True)
    qtbot.waitUntil(lambda: not q.running, timeout=5000)
    assert rows(win, "store", "SELECT COUNT(*) FROM order_items")[0][0] == 0
    qtbot.waitUntil(lambda: tab.undo_btn.isEnabled() and tab.undo_btn.text() == "Undo (2)", timeout=3000)  # the refused DELETE snapshotted too
    assert "Undo" in q.messages.toPlainText()
    tab.undo()
    qtbot.waitUntil(lambda: rows(win, "store", "SELECT COUNT(*) FROM order_items")[0][0] == n, timeout=5000)
    tab.undo()
    qtbot.waitUntil(lambda: not tab.undo_btn.isEnabled(), timeout=3000)
    assert "undo" in progress().skills
    # grid edits are undoable too
    tab.open_table("products")
    dt = tab.data_tabs["products"]
    qtbot.waitUntil(lambda: dt.model.rowCount() > 0, timeout=5000)
    original = dt.model.rows[0][1]
    dt.model.setData(dt.model.index(0, 1), "Renamed!")
    dt.apply()
    qtbot.waitUntil(lambda: rows(win, "store", "SELECT title FROM products WHERE id = 1")[0][0] == "Renamed!", timeout=5000)
    qtbot.waitUntil(lambda: tab.undo_btn.isEnabled(), timeout=3000)
    tab.undo()
    qtbot.waitUntil(lambda: rows(win, "store", "SELECT title FROM products WHERE id = 1")[0][0] == original, timeout=5000)
    qtbot.waitUntil(lambda: dt.model.rowCount() > 0 and dt.model.rows[0][1] == original, timeout=5000)


def test_playground_try_this_and_reset(win, qtbot):
    tab = open_pg(win, qtbot)
    item = next(tab.tries.item(i) for i in range(tab.tries.count()) if "per customer" in tab.tries.item(i).text())
    tab._try(item)
    q = tab.query_tab
    qtbot.waitUntil(lambda: q.chart_panel is not None and not q.running, timeout=8000)
    assert "SUM(" in q.editor.toPlainText() and q.chart_panel.spec.kind == "bar"
    q.editor.setPlainText("DROP TABLE order_items;")
    q.run(all_=True)
    qtbot.waitUntil(lambda: not q.running, timeout=5000)
    win.reset_playground("store")
    qtbot.waitUntil(lambda: win.manager.is_open(P.conn_id("store")) and
                    "order_items" in win.manager.session(P.conn_id("store")).tables(), timeout=15000)
    assert P.playground().undo_depth("store") == 0
    # switching dataset replaces the tab
    win.open_playground("library")
    from dbide.ui.playground_tab import PlaygroundTab

    pgs = [win.tabs.widget(i) for i in range(win.tabs.count()) if isinstance(win.tabs.widget(i), PlaygroundTab)]
    assert len(pgs) == 1 and pgs[0].key == "library"


def test_create_project_from_template(win, qtbot, tmp_path):
    path = tmp_path / "blog.sqlite"
    win.create_from_template("blog", {"target": "file", "path": str(path), "sample": True})
    cfg = next(c for c in win.manager.store.all() if c.database == str(path))
    qtbot.waitUntil(lambda: win.manager.is_open(cfg.id) and len(win.manager.session(cfg.id).tables()) == 5, timeout=15000)
    from dbide.ui.erd import ErdTab

    qtbot.waitUntil(lambda: any(isinstance(win.tabs.widget(i), ErdTab) for i in range(win.tabs.count())), timeout=5000)
    s = win.manager.session(cfg.id)
    with s.engine.connect() as c:
        pw = c.exec_driver_sql("SELECT password FROM users LIMIT 1").scalar()
    assert pw.startswith("$argon2id$")
    # into an existing connection, schema only
    other = ConnectionConfig(name="Mine", driver="sqlite", database=str(tmp_path / "mine.db"), id="mine")
    win.manager.store.upsert(other)
    with qtbot.waitSignal(win.manager.connected, timeout=5000):
        win.manager.open(other, None)
    win.create_from_template("tasks", {"target": "conn", "conn_id": "mine", "schema": None, "sample": False})
    qtbot.waitUntil(lambda: set(win.manager.session("mine").tables()) == {"projects", "people", "tasks"}, timeout=8000)
    assert rows_count(win.manager.session("mine"), "tasks") == 0


def rows_count(s, t):
    with s.engine.connect() as c:
        return c.exec_driver_sql(f'SELECT COUNT(*) FROM "{t}"').scalar()


def test_progressive_disclosure(win, qtbot, monkeypatch):
    pr = progress()
    pr.reset()
    Gate.get().refresh()
    tb = win.main_toolbar
    assert win.designer_act not in tb.actions() and win.history_act not in tb.actions()
    assert win.designer_act.text().endswith("🔒") and win.playground_act in tb.actions()
    assert win.designer_act.iconText() == "Designer"  # the short toolbar label never shows the lock
    # locked: explains, and does nothing unless the user unlocks
    asked = []
    monkeypatch.setattr(Gate, "explain_locked", lambda self, f, parent=None: asked.append(f) or False)
    win.history_act.trigger()
    assert asked == ["history"]
    # earning skills reaches Explorer: tools appear and a notice is shown
    pr.record("first_query", "filter", "sort", "ask")
    qtbot.waitUntil(lambda: win.history_act in tb.actions(), timeout=2000)
    assert not win.history_act.text().endswith("🔒") and win._toast is not None
    acts = tb.actions()  # unlocked items come back in their original order
    assert acts.index(win.new_query_act) < acts.index(win.designer_act) < acts.index(win.erd_act) < acts.index(win.history_act)
    # the Table Designer hides advanced tabs and the Role column below Builder
    win.open_playground("store")
    qtbot.waitUntil(lambda: win.manager.is_open(P.conn_id("store")), timeout=15000)
    win.design_table(P.conn_id("store"), None, None)
    b = win.tabs.currentWidget()
    assert not b.tabs.isTabVisible(b.tabs.indexOf(b.idx)) and b.cols.table.isColumnHidden(b.C_ROLE)
    # "Unlock now" jumps straight to the needed level
    monkeypatch.setattr(Gate, "explain_locked", lambda self, f, parent=None: (progress().set_override(3), True)[1])
    from dbide.ui.security_dialogs import SecuritySettingsDialog

    opened = []
    monkeypatch.setattr(SecuritySettingsDialog, "exec", lambda self: opened.append(1) or 0)
    win.security_act.trigger()
    assert opened == [1]  # after unlocking, the tool actually opens
    assert pr.level == 3 and b.tabs.isTabVisible(b.tabs.indexOf(b.idx)) and not b.cols.table.isColumnHidden(b.C_ROLE)


def test_concept_tips(win, qtbot):
    from PySide6.QtGui import QHelpEvent
    from PySide6.QtCore import QEvent

    assert "Playground" in win.playground_act.whatsThis() and "Diagram" in win.erd_act.whatsThis()
    tab = open_pg(win, qtbot)
    ed = tab.query_tab.editor
    ed.setPlainText("SELECT * FROM customers")
    pos = ed.cursorRect(ed.textCursor()).center()
    cur = ed.textCursor()
    cur.setPosition(2)
    rect = ed.cursorRect(cur)
    ev = QHelpEvent(QEvent.Type.ToolTip, rect.center(), ed.viewport().mapToGlobal(rect.center()))
    ed.event(ev)
    assert "SELECT" in QToolTip.text() and "read data" in QToolTip.text()
    tab.open_table("orders")
    dt = tab.data_tabs["orders"]
    qtbot.waitUntil(lambda: "customer_id" in dt.model.header_tips, timeout=5000)
    assert "Foreign key" in dt.model.header_tips["customer_id"] and "Primary key" in dt.model.header_tips["id"]
    col = dt.model.columns.index("customer_id")
    assert "customers.id" in dt.model.headerData(col, Qt.Orientation.Horizontal, Qt.ItemDataRole.ToolTipRole)
    _ = pos, QPoint


def test_first_launch_opens_playground(qtbot, tmp_path, monkeypatch):
    from dbide.ui.main_window import ModeChooser
    from dbide.ui.playground_tab import PlaygroundTab

    monkeypatch.setattr(ModeChooser, "exec", lambda self: (setattr(self, "choice", "learning"), 1)[1])
    settings().set("first_run_done", False)
    mgr = ConnectionManager(ConnectionStore(tmp_path / "c2.json"), QueryHistory(tmp_path / "h2.sqlite"))
    w = MainWindow(mgr)
    qtbot.addWidget(w)
    qtbot.waitUntil(lambda: any(isinstance(w.tabs.widget(i), PlaygroundTab) for i in range(w.tabs.count())), timeout=5000)
    assert w.side_stack.currentIndex() == 1  # lessons alongside
    qtbot.waitUntil(lambda: mgr.is_open(P.conn_id("store")), timeout=15000)
    mgr.close_all()
