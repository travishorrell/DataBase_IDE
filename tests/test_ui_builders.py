"""GUI tests: Ask bar (plain English → SQL → chart), chart rendering/hover, schema designer."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QDialog, QInputDialog, QMessageBox  # noqa: E402

from dbide.core.chart_model import auto_spec, build  # noqa: E402
from dbide.core.config import ConnectionConfig, ConnectionStore  # noqa: E402
from dbide.core.history import QueryHistory  # noqa: E402
from dbide.ui.chart_view import ChartPanel, ChartWidget  # noqa: E402
from dbide.ui.connections import ConnectionManager  # noqa: E402
from dbide.ui.main_window import MainWindow  # noqa: E402
from tests.test_builders import SHOP  # noqa: E402


@pytest.fixture
def win(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.StandardButton.Ok)
    store = ConnectionStore(tmp_path / "c.json")
    cfg = ConnectionConfig(name="Shop", driver="sqlite", database=str(tmp_path / "shop.db"), id="shop")
    store.upsert(cfg)
    mgr = ConnectionManager(store, QueryHistory(tmp_path / "h.sqlite"))
    w = MainWindow(mgr)
    qtbot.addWidget(w)
    w.resize(1400, 900)
    w.show()
    with qtbot.waitSignal(mgr.connected, timeout=5000):
        w.connect_profile("shop")
    q = mgr.session("shop").query_connection()
    assert all(r.error is None for r in q.run(SHOP))
    q.close()
    yield w
    mgr.close_all()


def test_ask_bar_generates_runs_and_charts(win, qtbot):
    tab = win.new_query("shop")
    tab.ask.setText("total of orders per customer")
    tab.generate_sql()
    qtbot.waitUntil(lambda: tab.chart_panel is not None and not tab.running, timeout=8000)
    assert tab.editor.toPlainText().startswith("SELECT c.\"name\", SUM(o.\"total\")")
    assert tab.results.currentWidget() is tab.chart_panel  # instant visual feedback
    assert tab.chart_panel.spec.kind == "bar"
    assert "one row per" in tab.ask_info.text()
    # a second question replaces the generated SQL (the user hadn't edited it)
    tab.ask.setText("how many customers")
    tab.generate_sql()
    qtbot.waitUntil(lambda: "COUNT(*)" in tab.editor.toPlainText() and not tab.running, timeout=8000)
    assert "SUM(" not in tab.editor.toPlainText()
    qtbot.waitUntil(lambda: tab.chart_panel is not None and tab.chart_panel.spec.kind == "stat", timeout=5000)
    # user edits are kept: a new question is appended instead
    tab.editor.setPlainText("SELECT 1; -- my own work")
    tab.ask.setText("customers in UK")
    tab.generate_sql()
    qtbot.waitUntil(lambda: "'UK'" in tab.editor.toPlainText() and not tab.running, timeout=8000)
    assert tab.editor.toPlainText().startswith("SELECT 1; -- my own work")


def test_ask_bar_reports_what_it_cannot_do(win, qtbot):
    tab = win.new_query("shop")
    tab.ask.setText("make me a sandwich")
    tab.generate_sql()
    qtbot.waitUntil(lambda: "which table" in tab.ask_info.text(), timeout=5000)
    assert not tab.editor.toPlainText().strip()


@pytest.mark.parametrize("cols,rows", [
    (["count"], [(4,)]),
    (["name", "orders"], [("Ada", 2), ("Grace", 1), ("Linus", 1)]),
    (["country", "sum_total", "avg_total"], [("UK", 189, 94.5), ("USA", 19, 19)]),
    (["created_at", "total", "items"], [("2024-01-05", 69, 2), ("2023-11-11", 19, 1), ("2024-03-01", 120, 1)]),
    (["price", "qty"], [(1.0, 2), (3.0, 4), (5.0, 1), (2.5, 3)]),
    (["name", "email"], [("a", "b")]),
])
def test_chart_widget_paints_and_hovers(qtbot, cols, rows):
    w = ChartWidget()
    qtbot.addWidget(w)
    w.resize(640, 320)
    w.show()
    w.set_data(build(auto_spec(cols, rows), cols, rows))
    assert not w.grab().isNull()
    for x in (200, 320, 500):
        QTest.mouseMove(w, QPoint(x, 160))
        w.grab()
    if auto_spec(cols, rows).kind in ("bar", "line", "scatter"):
        QTest.mouseMove(w, QPoint(330, 150))
        w.grab()
        assert w._hover is None or w._hover[1] is not None


def test_chart_customize(qtbot):
    cols, rows = ["created_at", "total"], [("2024-01-05", 69), ("2023-11-11", 19), ("2024-03-01", 120)]
    panel = ChartPanel(cols, rows, advanced=True)
    qtbot.addWidget(panel)
    panel.resize(700, 400)
    panel.show()
    assert panel.spec.kind == "line" and panel.controls.isVisible()
    panel.kind.setCurrentIndex(panel.kind.findData("bar"))
    assert panel.spec.kind == "bar" and "Custom" in panel.summary.text()
    panel.kind.setCurrentIndex(panel.kind.findData("auto"))
    assert panel.spec is panel.auto
    beginner = ChartPanel(cols, rows, advanced=False)
    qtbot.addWidget(beginner)
    assert not beginner.controls.isVisibleTo(beginner)


def test_schema_designer_drag_link_apply(win, qtbot, monkeypatch):
    from dbide.ui.schema_designer import DesignerTab, PlanDialog, RelationshipDialog

    win.open_designer("shop", None)
    tab: DesignerTab = win.tabs.currentWidget()
    qtbot.waitUntil(lambda: tab.model is not None and len(tab.nodes) == 4, timeout=5000)
    assert not tab.apply_act.isEnabled()
    names = iter(["reviews", "tags"])
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: (next(names), True))
    tab.add_table(QPointF(0, 600))
    tab.add_table(QPointF(400, 600))
    assert tab.nodes["reviews"].status == "new" and tab.apply_act.isEnabled()

    # drag from reviews' header handle onto products → one-to-many
    monkeypatch.setattr(RelationshipDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    src = tab.nodes["reviews"]
    tab.begin_link(src, None, src.anchor(None, True))
    target = tab.nodes["products"]
    drop = target.sceneBoundingRect().center()
    tab.move_link(drop)
    tab.end_link(drop)
    assert tab.model.tables["reviews"].foreign_keys[0].ref_table == "products"

    # many-to-many between products and tags creates a junction table
    def m2m(self):
        self.group.buttons()[2].setChecked(True)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(RelationshipDialog, "exec", m2m)
    tab.link("products", "tags")
    assert "products_tags" in tab.nodes
    assert any("Create table products_tags" in s.title for s in tab.model.plan(tab.session.driver))

    monkeypatch.setattr(PlanDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    tab.review_apply()
    qtbot.waitUntil(lambda: tab.model is not None and not tab.model.dirty and "products_tags" in tab.model.originals, timeout=8000)
    tables = win.manager.session("shop").tables()
    assert {"reviews", "tags", "products_tags"} <= set(tables)
    assert win.manager.session("shop").table_def("reviews").foreign_keys[0].ref_table == "products"
    # the pre-existing data is intact
    assert win.manager.session("shop").fetch_page("products", None, 10, 0).total == 4


def test_designer_on_empty_database(win, qtbot, tmp_path, monkeypatch):
    from dbide.ui.schema_designer import DesignerTab

    cfg = ConnectionConfig(name="Empty", driver="sqlite", database=str(tmp_path / "empty.db"), id="empty")
    win.manager.store.upsert(cfg)
    with qtbot.waitSignal(win.manager.connected, timeout=5000):
        win.manager.open(cfg, None)
    win.open_designer("empty", None)
    tab: DesignerTab = win.tabs.currentWidget()
    qtbot.waitUntil(lambda: tab.model is not None, timeout=5000)
    assert "Empty database" in tab.hint.text()
