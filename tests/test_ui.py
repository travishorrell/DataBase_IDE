"""Headless smoke tests of the GUI against a real SQLite file."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QMessageBox  # noqa: E402

from dbide.core.config import ConnectionConfig, ConnectionStore  # noqa: E402
from dbide.core.history import QueryHistory  # noqa: E402
from dbide.ui.connections import ConnectionManager  # noqa: E402
from dbide.ui.data_tab import DataTab  # noqa: E402
from dbide.ui.main_window import MainWindow  # noqa: E402
from dbide.ui.query_tab import QueryTab  # noqa: E402
from dbide.ui.table_builder import TableBuilderTab  # noqa: E402


@pytest.fixture
def win(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("DBIDE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    store = ConnectionStore(tmp_path / "conns.json")
    cfg = ConnectionConfig(name="Test DB", driver="sqlite", database=str(tmp_path / "ui.db"))
    store.upsert(cfg)
    mgr = ConnectionManager(store, QueryHistory(tmp_path / "h.sqlite"))
    w = MainWindow(mgr)
    qtbot.addWidget(w)
    w.cfg = cfg
    with qtbot.waitSignal(mgr.connected, timeout=5000):
        w.connect_profile(cfg.id)
    yield w
    mgr.close_all()


def test_query_builder_and_grid(win, qtbot):
    tab = win.new_query(win.cfg.id)
    assert isinstance(tab, QueryTab) and tab.qconn is not None
    tab.editor.setPlainText(
        "CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT NOT NULL);\n"
        "INSERT INTO items (name) VALUES ('a'), ('b');\nSELECT * FROM items;"
    )
    tab.run(all_=True)
    qtbot.waitUntil(lambda: not tab.running, timeout=5000)
    assert tab.results.count() == 2  # Messages + Result
    grid = tab.results.widget(1)
    assert grid.model().rowCount() == 2
    assert "3 statement(s)" in tab.status.text()
    assert win.manager.history.recent()[0][2].startswith("SELECT")

    # sidebar picks up the new table
    root = win.tree._conn_item(win.cfg.id)
    qtbot.waitUntil(lambda: root.childCount() and root.child(0).text(0) == "Tables (1)", timeout=5000)

    # edit data in the grid
    win.open_data(win.cfg.id, None, "items")
    data = win.tabs.currentWidget()
    assert isinstance(data, DataTab)
    qtbot.waitUntil(lambda: data.model.rowCount() == 2, timeout=5000)
    data.model.setData(data.model.index(0, 1), "changed")
    r = data.model.add_row()
    data.model.setData(data.model.index(r, 1), "c")
    assert data.is_dirty()
    data.apply()
    qtbot.waitUntil(lambda: data.model.rowCount() == 3 and not data.model.dirty, timeout=5000)
    assert [data.model.value(i, 1) for i in range(3)] == ["changed", "b", "c"]

    # design: add a column through the builder
    win.design_table(win.cfg.id, None, "items")
    b = win.tabs.currentWidget()
    assert isinstance(b, TableBuilderTab)
    qtbot.waitUntil(lambda: b.original is not None, timeout=5000)
    b.add_column()
    stmts, problems = b.statements()
    assert not problems and stmts == ['ALTER TABLE "items" ADD COLUMN "column_3" TEXT']
    b.update_preview()
    assert "ADD COLUMN" in b.preview.toPlainText()


def test_new_table_builder(win, qtbot):
    win.design_table(win.cfg.id, None, None)
    b = win.tabs.currentWidget()
    b.name_edit.setText("people")
    b.add_column()
    b.add_index()
    b.idx.table.item(0, 1).setText("column_2")
    stmts, problems = b.statements()
    assert not problems
    assert stmts[0].startswith('CREATE TABLE "people"') and "AUTOINCREMENT" in stmts[0]
    assert stmts[1] == 'CREATE INDEX "idx_people_1" ON "people" ("column_2")'
    win.manager.session(win.cfg.id).execute_ddl(stmts)
    assert win.manager.session(win.cfg.id).tables() == ["people"]
