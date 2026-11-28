"""Type dropdowns (incl. special password/media types) and media storage locations."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QComboBox, QDialog, QMessageBox  # noqa: E402

from dbide.core import crypto, lessons, media  # noqa: E402
from dbide.core.config import ConnectionConfig, ConnectionStore  # noqa: E402
from dbide.core.history import QueryHistory  # noqa: E402
from dbide.core.roles import SMART_TYPES, ColumnRole, from_smart, role_store, smart_label, type_choices  # noqa: E402
from dbide.core.schema_plan import DesignerModel  # noqa: E402
from dbide.ui.connections import ConnectionManager  # noqa: E402
from dbide.ui.main_window import MainWindow  # noqa: E402


def test_special_types_roundtrip():
    choices = type_choices(["INTEGER", "TEXT"])
    assert choices[:2] == ["INTEGER", "TEXT"] and choices[2] == "" and "🔑 Password (hashed)" in choices
    for label, code in SMART_TYPES:
        role = from_smart(label)
        assert role.code() == code and smart_label(role) == label
    assert smart_label(ColumnRole("password", "bcrypt")) == "🔑 Password (hashed)"
    assert from_smart("TEXT") is None


def test_stylesheet_draws_combo_arrows(qapp):
    from dbide.ui.theme import ThemeManager

    ThemeManager.get().apply(qapp)
    css = qapp.styleSheet()
    assert "QComboBox::down-arrow" in css and "caret_down" in css and "QSpinBox::down-arrow" in css
    arrow = css.split("QComboBox::down-arrow {")[1].split("url(")[1].split(")")[0]
    assert Path(arrow).exists()


@pytest.fixture
def win(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.StandardButton.Ok)
    store = ConnectionStore(tmp_path / "c.json")
    cfg = ConnectionConfig(name="Pics", driver="sqlite", database=str(tmp_path / "pics.db"), id="pics")
    store.upsert(cfg)
    mgr = ConnectionManager(store, QueryHistory(tmp_path / "h.sqlite"))
    w = MainWindow(mgr)
    qtbot.addWidget(w)
    w.resize(1400, 900)
    w.show()
    with qtbot.waitSignal(mgr.connected, timeout=5000):
        w.connect_profile("pics")
    w.png = tmp_path / "my photo.png"
    w.png.write_bytes(lessons.sample_png())
    yield w
    mgr.close_all()


def test_builder_type_dropdown_has_special_types(win, qtbot):
    win.design_table("pics", None, None)
    b = win.tabs.currentWidget()
    t = b.cols.table
    b.add_column()
    row = t.rowCount() - 1
    idx = t.model().index(row, b.C_TYPE)
    assert t.state() == t.State.EditingState  # "Add" leaves the new name being edited…
    from dbide.ui.table_builder import open_dropdown

    assert open_dropdown(t, idx)  # …and the Type dropdown still opens
    assert t.item(row, b.C_NAME).text().startswith("column_")
    for _ in range(5):  # and stays open once pending focus events are processed
        qtbot.wait(20)
    assert t.state() == t.State.EditingState and t.currentIndex() == idx
    editor = next(c for c in t.findChildren(QComboBox) if c.isVisible())
    items = [editor.itemText(i) for i in range(editor.count())]
    assert "INTEGER" in items and "🔑 Password (hashed)" in items and "📁 Image (file on disk)" in items
    editor.setCurrentText("🔑 Password (hashed)")
    t.itemDelegateForColumn(b.C_TYPE).setModelData(editor, t.model(), idx)
    assert t.item(row, b.C_TYPE).text() == "TEXT"
    assert t.item(row, b.C_ROLE).data(Qt.ItemDataRole.UserRole) == "password:argon2id"
    # an ordinary type still just sets the type
    editor.setCurrentText("REAL")
    t.itemDelegateForColumn(b.C_TYPE).setModelData(editor, t.model(), idx)
    assert t.item(row, b.C_TYPE).text() == "REAL"
    # a single click on a Type cell opens the editor, even while another cell is being edited
    t.editItem(t.item(0, b.C_NAME))
    t.clicked.emit(t.model().index(0, b.C_TYPE))
    for _ in range(5):
        qtbot.wait(20)
    assert t.state() == t.State.EditingState and t.currentIndex().column() == b.C_TYPE
    assert any(c.isVisible() for c in t.findChildren(QComboBox))


def test_designer_special_types_saved(win, qtbot, monkeypatch):
    from dbide.ui.schema_designer import DesignerTab, PlanDialog

    win.open_designer("pics", None)
    tab: DesignerTab = win.tabs.currentWidget()
    qtbot.waitUntil(lambda: tab.model is not None, timeout=5000)
    tab.model.add_table("users")
    tab.model.add_column("users", "username", "TEXT", nullable=False)
    tab.model.add_column("users", "password", "x", role=from_smart("🔑 Password (hashed)"))
    tab.model.add_column("users", "avatar", "x", role=from_smart("📁 Image (file on disk)"))
    assert tab.model.tables["users"].columns[-1].type == "TEXT"
    monkeypatch.setattr(PlanDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    tab.review_apply()
    qtbot.waitUntil(lambda: tab.model is not None and "users" in tab.model.originals, timeout=8000)
    saved = role_store().get("pics", None, "users")
    assert saved["password"].kind == "password" and saved["avatar"].storage == "path"
    assert not tab.model.dirty
    # changing only a column's special type needs no SQL but is still saved
    from dbide.ui.schema_designer import TableDialog

    dlg = TableDialog(tab.model.tables["users"], tab._types(), tab, tab.model.roles["users"], "sqlite")
    combos = [dlg.grid.cellWidget(r, 1) for r in range(dlg.grid.rowCount())]
    assert combos[2].currentText() == "🔑 Password (hashed)" and combos[3].currentText() == "📁 Image (file on disk)"
    combos[1].setCurrentText("🔒 Encrypted text")
    tab.model.tables["users"] = dlg.result_def()
    tab.model.roles["users"] = dict(dlg.roles_out)
    assert tab.model.roles_dirty and not tab.model.plan(tab.session.driver)
    tab.review_apply()
    assert role_store().get("pics", None, "users")["username"].kind == "encrypted"


def test_media_insert_custom_folder_view_and_erd(win, qtbot, tmp_path):
    from dbide.ui.data_tab import DataTab
    from dbide.ui.media_viewer import InsertMediaDialog, MediaViewer

    s = win.manager.session("pics")
    q = s.query_connection()
    q.run("CREATE TABLE photos (id INTEGER PRIMARY KEY, img BLOB, img_path TEXT, doc_path TEXT)")
    q.close()
    role_store().set_table("pics", None, "photos", {"img_path": ColumnRole("media", storage="path", media_kind="image")})
    win.open_data("pics", None, "photos")
    tab: DataTab = win.tabs.currentWidget()
    qtbot.waitUntil(lambda: bool(tab.model.columns), timeout=5000)
    m = tab.model
    r = m.add_row()
    # default location: next to the database file
    dlg = InsertMediaDialog(s.cfg, "img_path", m.roles[2])
    dlg.set_file(str(win.png))
    assert dlg.folder == str(media.default_folder(s.cfg)) and dlg.folder_box.isVisibleTo(dlg)
    dlg._accept()
    assert dlg.value.startswith("pics_media/images/my_photo_") and dlg.new_role is None
    m.set_cell(r, 2, dlg.value)
    tip = m.index(r, 2).data(Qt.ItemDataRole.ToolTipRole)
    assert "pics_media" in tip and "MISSING" not in tip
    # custom location chosen in the dialog is used and remembered for the column (new column, no role yet)
    custom = tmp_path / "elsewhere"
    dlg2 = InsertMediaDialog(s.cfg, "doc_path", None)
    dlg2.set_file(str(win.png))
    dlg2.pathopt.setChecked(True)
    dlg2.folder = str(custom)
    dlg2._sync_folder()
    dlg2._accept()
    stored = media.resolve(dlg2.value, s.cfg)
    assert stored.parent.parent == custom and stored.exists()
    assert not Path(dlg2.value).is_absolute()  # inside the database's folder → stored relative, so they move together
    assert dlg2.new_role.folder == str(custom)
    role_store().set_table("pics", None, "photos", {**role_store().get("pics", None, "photos"), "doc_path": dlg2.new_role})
    m.set_roles(role_store().get("pics", None, "photos"))
    m.set_cell(r, 3, dlg2.value)
    # blob
    dlg3 = InsertMediaDialog(s.cfg, "img", None)
    dlg3.set_file(str(win.png))
    dlg3.blob.setChecked(True)
    assert not dlg3.folder_box.isVisibleTo(dlg3)
    dlg3._accept()
    m.set_cell(r, 1, dlg3.value)
    tab.apply()
    qtbot.waitUntil(lambda: not m.dirty and len(m.rows) == 1, timeout=5000)
    row = m.rows[0]
    assert isinstance(row[1], (bytes, memoryview)) and media.resolve(row[2], s.cfg).exists() and media.resolve(row[3], s.cfg).exists()
    for col in (1, 2, 3):
        v = MediaViewer(row[col], s.cfg, "photos")
        qtbot.addWidget(v)
        from PySide6.QtWidgets import QLabel

        assert any(lbl.pixmap() is not None and not lbl.pixmap().isNull() for lbl in v.findChildren(QLabel)), col
    # the diagram reports the custom folder with its file
    win.open_erd("pics", None)
    erd = win.tabs.currentWidget()
    qtbot.waitUntil(lambda: erd.model is not None, timeout=5000)
    info = {x.column: x for x in erd.model.media}
    assert info["doc_path"].folders[0].folder == custom and info["doc_path"].folders[0].referenced == 1
    assert info["img_path"].folders[0].referenced == 1


def test_no_stray_windows_steal_focus(win, qtbot):
    """Welcome-page cards used to be detached (not deleted) on every status update and
    turned into invisible-then-visible top-level windows that stole focus from editors."""
    from PySide6.QtWidgets import QApplication

    for _ in range(3):
        win._update_status()
        win.design_table("pics", None, None)
        qtbot.wait(30)
    assert [type(w).__name__ for w in QApplication.topLevelWidgets() if w.isVisible()] == ["MainWindow"]
