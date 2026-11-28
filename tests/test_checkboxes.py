"""Row checkboxes: tick many rows, then delete or update them together."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QDialog, QMessageBox  # noqa: E402

from dbide.core.config import ConnectionConfig, ConnectionStore  # noqa: E402
from dbide.core.history import QueryHistory  # noqa: E402
from dbide.core.roles import ColumnRole, role_store  # noqa: E402
from dbide.core.session import DatabaseSession  # noqa: E402
from dbide.core.templates import TEMPLATES, apply_template  # noqa: E402
from dbide.ui.connections import ConnectionManager  # noqa: E402
from dbide.ui.data_tab import BulkUpdateDialog, DataTab  # noqa: E402
from dbide.ui.grid import CheckHeader  # noqa: E402
from dbide.ui.main_window import MainWindow  # noqa: E402


@pytest.fixture
def tab(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    store = ConnectionStore(tmp_path / "c.json")
    cfg = ConnectionConfig(name="Tasks", driver="sqlite", database=str(tmp_path / "t.db"), id="t")
    store.upsert(cfg)
    s = DatabaseSession(cfg)
    apply_template(s, TEMPLATES["tasks"])
    s.close()
    mgr = ConnectionManager(store, QueryHistory(tmp_path / "h.sqlite"))
    w = MainWindow(mgr)
    qtbot.addWidget(w)
    w.resize(1400, 900)
    w.show()
    with qtbot.waitSignal(mgr.connected, timeout=5000):
        w.connect_profile("t")
    w.open_data("t", None, "tasks")
    dt: DataTab = w.tabs.currentWidget()
    qtbot.waitUntil(lambda: dt.model.rowCount() == 50, timeout=5000)
    dt.win = w
    yield dt
    mgr.close_all()


def count(dt, where="1=1"):
    with dt.session.engine.connect() as c:
        return c.exec_driver_sql(f"SELECT COUNT(*) FROM tasks WHERE {where}").scalar()


def test_click_row_boxes_and_delete(tab, qtbot):
    m = tab.model
    header = tab.grid.verticalHeader()
    assert isinstance(header, CheckHeader)
    assert not tab.del_checked_act.isEnabled()
    for row in (0, 2, 4):  # click the checkbox in front of rows 1, 3 and 5
        y = header.sectionViewportPosition(row) + header.sectionSize(row) // 2
        QTest.mouseClick(header.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(12, y))
    assert m.checked_rows() == [0, 2, 4] and "3 checked" in tab.checked_label.text()
    assert tab.check_all_box.checkState() == Qt.CheckState.PartiallyChecked and tab.del_checked_act.isEnabled()
    QTest.mouseClick(header.viewport(), Qt.MouseButton.LeftButton,
                     pos=QPoint(12, header.sectionViewportPosition(2) + header.sectionSize(2) // 2))
    assert m.checked_rows() == [0, 4]  # clicking again unticks
    ids = [m.rows[r][0] for r in m.checked_rows()]
    tab.del_checked_act.trigger()
    assert m.deleted == {0, 4} and not m.checked
    assert count(tab) == 50  # staged only
    tab.apply()
    qtbot.waitUntil(lambda: count(tab) == 48, timeout=5000)
    assert count(tab, f"id IN ({ids[0]}, {ids[1]})") == 0


def test_select_all_and_bulk_update(tab, qtbot, monkeypatch):
    m = tab.model
    tab.check_all_box.click()
    assert len(m.checked) == 50 and tab.check_all_box.checkState() == Qt.CheckState.Checked
    m.set_checked(range(10, 50), False)
    assert len(m.checked) == 10

    def fake_exec(self):
        assert self.col.findText("id") == -1  # the primary key isn't offered
        self.col.setCurrentText("status")
        self.val.setText("done")
        self._ok()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(BulkUpdateDialog, "exec", fake_exec)
    tab.update_checked()
    # rows that were already 'done' need no change, so only the others are staged
    assert all(m.value(r, m.columns.index("status")) == "done" for r in range(10)) and 0 < len(m.edits) <= 10
    tab.apply()
    qtbot.waitUntil(lambda: count(tab, "id <= 10 AND status = 'done'") == 10, timeout=5000)

    # set to NULL
    qtbot.waitUntil(lambda: m.rowCount() == 50 and not m.dirty, timeout=5000)
    m.set_checked([0, 1], True)

    def null_exec(self):
        self.col.setCurrentText("assignee_id")
        self.null.setChecked(True)
        self._ok()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(BulkUpdateDialog, "exec", null_exec)
    tab.update_checked()
    tab.apply()
    qtbot.waitUntil(lambda: count(tab, "id IN (1, 2) AND assignee_id IS NULL") == 2, timeout=5000)


def test_bulk_update_hashes_passwords(qtbot, tmp_path):
    cols = ["id", "username", "password"]
    roles = {2: ColumnRole("password", "argon2id")}
    dlg = BulkUpdateDialog(["username", "password"], 3, roles, cols)
    qtbot.addWidget(dlg)
    dlg.col.setCurrentText("password")
    assert "hashed automatically" in dlg.note.text()
    _ = role_store


def test_read_only_tables_have_no_checkboxes(tab, qtbot):
    s = tab.session
    s.execute("CREATE TABLE log (msg TEXT)")  # no primary key → read-only grid
    s.execute("INSERT INTO log VALUES ('a')")
    tab.win.open_data("t", None, "log")
    lt = tab.win.tabs.currentWidget()
    qtbot.waitUntil(lambda: lt.model.rowCount() == 1, timeout=5000)
    assert not lt.check_all_box.isEnabled()
    lt.grid.verticalHeader().mousePressEvent  # header ignores clicks: model not editable
    assert lt.grid.verticalHeader()._model() is None
