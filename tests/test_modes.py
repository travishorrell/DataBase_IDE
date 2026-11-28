"""Learning vs Power user mode, and the Admin Console."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QMessageBox  # noqa: E402

from dbide.core import admin as A  # noqa: E402
from dbide.core import mode  # noqa: E402
from dbide.core.config import ConnectionConfig, ConnectionStore  # noqa: E402
from dbide.core.history import QueryHistory  # noqa: E402
from dbide.core.progress import LEVELS, progress  # noqa: E402
from dbide.core.session import DatabaseSession  # noqa: E402
from dbide.core.settings import settings  # noqa: E402
from dbide.core.templates import TEMPLATES, apply_template  # noqa: E402
from dbide.ui.connections import ConnectionManager  # noqa: E402
from dbide.ui.help_panel import HelpCenter  # noqa: E402
from dbide.ui.main_window import MainWindow  # noqa: E402


def test_mode_setting_and_levels():
    assert mode.current() == "learning"
    settings().set("progress", {"skills": {}, "override": None})
    assert progress().level == 0
    mode.set_mode("power")
    assert mode.is_power() and progress().level == len(LEVELS) - 1 and progress().allowed("admin")
    mode.set_mode("learning")
    assert progress().level == 0 and not progress().allowed("admin")
    settings().set("mode", None)
    settings().set("beginner_mode", False)  # older settings files map onto the new modes
    assert mode.current() == "power"
    with pytest.raises(ValueError):
        mode.set_mode("expert")


def test_help_center_filters_by_mode(qapp):
    hc = HelpCenter.get()
    seen = []
    hc.changed.connect(lambda t, b: seen.append(t))
    hc.set_mode("power")
    hc.show_topic("kw:SELECT")
    hc.show_topic("concept:fk")
    hc.show_html("JOINs", "<p>teaching</p>")
    assert seen == []
    hc.show_topic("cmd:admin")
    hc.show_html("How I read your request", "<p>software</p>", teaching=False)
    assert seen == ["Admin Console", "How I read your request"]
    hc.set_mode("learning")
    hc.show_topic("kw:SELECT")
    assert seen[-1].startswith("SELECT")


@pytest.fixture
def shop(tmp_path):
    cfg = ConnectionConfig(name="Shop", driver="sqlite", database=str(tmp_path / "shop.db"), id="shop")
    s = DatabaseSession(cfg)
    s.connect()
    apply_template(s, TEMPLATES["store"])
    yield s
    s.close()


def test_admin_sqlite(shop, tmp_path):
    ov = A.overview(shop)
    assert ov.version.startswith("SQLite") and ov.size_bytes > 0
    assert {t.name: t.rows for t in ov.tables}["orders"] == 120 and any("Foreign keys enforced: yes" in n for n in ov.notes)
    r = A.integrity_check(shop)
    assert r.ok and r.message == "No problems found."
    assert A.analyze(shop).ok
    assert "compacted" in A.optimize(shop).message
    out = tmp_path / "backup.sqlite"
    assert "Copied" in A.backup(shop, out).message
    copy = DatabaseSession(ConnectionConfig(name="b", driver="sqlite", database=str(out)))
    assert A.overview(copy).tables == ov.tables or len(A.overview(copy).tables) == 4
    copy.close()
    assert "no server" in A.processes(shop).message and "no user accounts" in A.users(shop).message
    names = [r[0] for r in A.settings_list(shop).rows]
    assert "journal_mode" in names and "foreign_keys" in names
    with pytest.raises(ValueError):
        A.kill(shop, 1)


def test_admin_integrity_finds_broken_foreign_keys(shop):
    with shop.engine.connect() as c:
        c.exec_driver_sql("PRAGMA foreign_keys = OFF")
        c.exec_driver_sql("INSERT INTO orders (customer_id, status, total) VALUES (9999, 'paid', 1)")
        c.commit()
    r = A.integrity_check(shop)
    assert not r.ok and any("orders" in str(row[1]) for row in r.rows)


@pytest.fixture
def win(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.StandardButton.Ok)
    store = ConnectionStore(tmp_path / "c.json")
    cfg = ConnectionConfig(name="Shop", driver="sqlite", database=str(tmp_path / "shop.db"), id="shop")
    store.upsert(cfg)
    s = DatabaseSession(cfg)
    apply_template(s, TEMPLATES["store"])
    s.close()
    mgr = ConnectionManager(store, QueryHistory(tmp_path / "h.sqlite"))
    w = MainWindow(mgr)
    qtbot.addWidget(w)
    w.resize(1500, 900)
    w.show()
    with qtbot.waitSignal(mgr.connected, timeout=5000):
        w.connect_profile("shop")
    yield w
    mgr.close_all()


def test_switching_modes_changes_the_ui(win, qtbot):
    settings().set("progress", {"skills": {}, "override": None})
    from dbide.ui.features import Gate

    Gate.get().refresh()
    tb = win.main_toolbar
    assert win.lessons_act.isVisible() and win.activity_buttons["lessons"].isVisibleTo(win)
    assert win.learning_act.isChecked() and win.mode_badge.text() == "Learning mode"
    assert win.admin_act not in tb.actions() and win.admin_act.text().endswith("🔒")
    win.power_act.trigger()
    assert mode.is_power() and win.power_act.isChecked() and win.mode_button.text() == "Power user"
    assert not win.lessons_act.isVisible() and not win.activity_buttons["lessons"].isVisibleTo(win)
    assert not win.activity_buttons["progress"].isVisibleTo(win)
    assert win.admin_act in tb.actions() and not win.admin_act.text().endswith("🔒")  # everything unlocked
    assert not win.right_panel.isVisible()  # help panel closes in power mode
    qtbot.waitUntil(lambda: "Power user mode" in win.help_panel.view.toPlainText(), timeout=2000)
    win.learning_act.trigger()
    assert win.lessons_act.isVisible() and win.right_panel.isVisible() and win.admin_act not in tb.actions()


def test_power_mode_query_tab_and_grid(win, qtbot):
    HelpCenter.get().set_mode("power")
    q = win.new_query("shop", sql="SELECT nope FROM customers;")
    q.run(all_=True)
    qtbot.waitUntil(lambda: not q.running, timeout=5000)
    text = q.messages.toPlainText()
    assert "no such column" in text and "💡" not in text  # raw errors, no SQL tutoring
    # deleting everything doesn't ask in power mode
    asked = []
    from PySide6.QtWidgets import QMessageBox as MB

    orig = MB.exec
    MB.exec = lambda self: asked.append(1) or MB.StandardButton.Cancel
    try:
        q.editor.setPlainText("DELETE FROM order_items")
        q.run(all_=True)
        qtbot.waitUntil(lambda: not q.running, timeout=5000)
    finally:
        MB.exec = orig
    assert asked == []
    win.open_data("shop", None, "orders")
    dt = win.tabs.currentWidget()
    qtbot.waitUntil(lambda: "customer_id" in dt.model.header_tips, timeout=5000)
    assert "FK → customers.id" in dt.model.header_tips["customer_id"] and "each row points" not in dt.model.header_tips["customer_id"]


def test_admin_tab(win, qtbot, tmp_path):
    HelpCenter.get().set_mode("power")
    win.admin_act.trigger()
    from dbide.ui.admin_tab import AdminTab

    tab = win.tabs.currentWidget()
    assert isinstance(tab, AdminTab)
    qtbot.waitUntil(lambda: tab.table_grid.model() is not None and tab.table_grid.model().rowCount() == 4, timeout=5000)
    assert tab.c_tables.value_label.text() == "4 / 0"
    tab.run_integrity()
    qtbot.waitUntil(lambda: "Integrity check: No problems found." in tab.task_log.toPlainText(), timeout=5000)
    dest = tmp_path / "copy.sqlite"
    tab.run_backup(str(dest))
    qtbot.waitUntil(lambda: dest.exists() and "Copied the database" in tab.task_log.toPlainText(), timeout=5000)
    for i in (2, 3, 4):
        tab.pages.setCurrentIndex(i)
    qtbot.waitUntil(lambda: tab.settings_grid.model() is not None and tab.settings_grid.model().rowCount() > 3, timeout=5000)
    assert "no server" in tab.act_note.text() and "no user accounts" in tab.users_note.text()


def test_first_run_power_choice_skips_playground(qtbot, tmp_path, monkeypatch):
    from dbide.ui.main_window import ModeChooser
    from dbide.ui.playground_tab import PlaygroundTab

    monkeypatch.setattr(ModeChooser, "exec", lambda self: (setattr(self, "choice", "power"), 1)[1])
    settings().set("first_run_done", False)
    mgr = ConnectionManager(ConnectionStore(tmp_path / "c2.json"), QueryHistory(tmp_path / "h2.sqlite"))
    w = MainWindow(mgr)
    qtbot.addWidget(w)
    qtbot.waitUntil(lambda: mode.is_power(), timeout=3000)
    qtbot.wait(100)
    assert not any(isinstance(w.tabs.widget(i), PlaygroundTab) for i in range(w.tabs.count()))
    assert not w.lessons_act.isVisible()
    mgr.close_all()


def test_power_mode_hides_playground(win, qtbot):
    tb = win.main_toolbar
    assert win.playground_act.isVisible() and win.activity_buttons["playground"].isVisibleTo(win)
    win.power_act.trigger()
    assert not win.playground_act.isVisible() and not win.activity_buttons["playground"].isVisibleTo(win)
    assert not any(c.isVisibleTo(win) for c in win._learning_cards)  # no Playground card on the welcome page
    from dbide.ui.templates_dialog import TemplateGallery

    g = TemplateGallery(win.manager, win)
    qtbot.addWidget(g)
    assert not g.play_btn.isVisibleTo(g) and g.create_btn.isVisibleTo(g)
    win.learning_act.trigger()
    assert win.playground_act.isVisible() and win.playground_act in tb.actions()
