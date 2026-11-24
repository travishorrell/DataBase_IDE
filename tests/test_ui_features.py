"""Headless GUI tests for roles, media, ERD, JOIN explainer, help, beginner mode and lessons."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QMessageBox  # noqa: E402

from dbide.core import crypto, lessons, secrets  # noqa: E402
from dbide.core.config import ConnectionConfig, ConnectionStore  # noqa: E402
from dbide.core.history import QueryHistory  # noqa: E402
from dbide.core.roles import ColumnRole, role_store  # noqa: E402
from dbide.ui.connections import ConnectionManager  # noqa: E402
from dbide.ui.data_tab import DataTab  # noqa: E402
from dbide.ui.erd import ErdTab, RelationEdge, StorageNode, TableNode  # noqa: E402
from dbide.ui.help_panel import HelpCenter  # noqa: E402
from dbide.ui.join_viz import JoinStepView, JoinVisualizer  # noqa: E402
from dbide.ui.main_window import MainWindow  # noqa: E402
from dbide.ui.table_builder import TableBuilderTab  # noqa: E402

SCHEMA = (
    "CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT NOT NULL);"
    "CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, author_id INTEGER REFERENCES authors(id));"
    "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT, api_token TEXT, avatar BLOB);"
    "INSERT INTO authors VALUES (1,'A'),(2,'B'),(3,'C');"
    "INSERT INTO books VALUES (1,'x',1),(2,'y',1),(3,'z',2);"
)


@pytest.fixture
def win(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.StandardButton.Ok)
    store = ConnectionStore(tmp_path / "conns.json")
    cfg = ConnectionConfig(name="Lib", driver="sqlite", database=str(tmp_path / "lib.db"), id="lib")
    store.upsert(cfg)
    mgr = ConnectionManager(store, QueryHistory(tmp_path / "h.sqlite"))
    w = MainWindow(mgr)
    qtbot.addWidget(w)
    with qtbot.waitSignal(mgr.connected, timeout=5000):
        w.connect_profile("lib")
    q = mgr.session("lib").query_connection()
    assert all(r.error is None for r in q.run(SCHEMA))
    q.close()
    w.tree.refresh("lib")
    yield w
    mgr.close_all()


def _help_text(win):
    return win.help_panel.view.toPlainText()


def test_password_and_encrypted_roles_in_grid(win, qtbot):
    role_store().set_table("lib", None, "users", {
        "password_hash": ColumnRole("password", "argon2id"),
        "api_token": ColumnRole("encrypted", "aes256gcm"),
    })
    win.open_data("lib", None, "users")
    tab: DataTab = win.tabs.currentWidget()
    qtbot.waitUntil(lambda: tab.model.columns == ["id", "username", "password_hash", "api_token", "avatar"], timeout=5000)
    r = tab.model.add_row()
    m = tab.model
    m.setData(m.index(r, 1), "ada")
    m.setData(m.index(r, 2), "hunter2")
    m.setData(m.index(r, 3), "tok_123")
    assert m.index(r, 2).data() == "•••••••• (will be hashed on Apply)"
    tab.apply()
    qtbot.waitUntil(lambda: m.rowCount() == 1 and not m.dirty, timeout=8000)
    stored_hash, stored_tok = m.rows[0][2], m.rows[0][3]
    assert crypto.detect_hash(stored_hash) == "argon2id" and crypto.verify_password("hunter2", stored_hash)
    assert crypto.is_encrypted(stored_tok)
    assert m.index(0, 2).data() == "🔑 Argon2id hash"
    assert m.index(0, 3).data() == "🔒 encrypted (AES-256-GCM)"
    tab.grid.setCurrentIndex(m.index(0, 3))
    tab.reveal()
    assert m.index(0, 3).data() == "tok_123"
    # editing a secret cell to "" keeps the old value
    m.setData(m.index(0, 2), "")
    assert not m.dirty


def test_media_cell_and_viewer(win, qtbot, tmp_path):
    from dbide.ui.media_viewer import InsertMediaDialog, MediaViewer

    win.open_data("lib", None, "users")
    tab: DataTab = win.tabs.currentWidget()
    qtbot.waitUntil(lambda: bool(tab.model.columns), timeout=5000)
    png = tmp_path / "face.png"
    png.write_bytes(lessons.sample_png())
    dlg = InsertMediaDialog(win.manager.session("lib").cfg, "avatar", None)
    dlg.set_file(str(png))
    dlg.blob.setChecked(True)
    dlg._accept()
    assert dlg.value == png.read_bytes() and dlg.new_role.storage == "blob"
    r = tab.model.add_row()
    tab.model.set_cell(r, 4, dlg.value)
    assert tab.model.index(r, 4).data().startswith("🖼 image/png")
    viewer = MediaViewer(dlg.value, win.manager.session("lib").cfg, "users.avatar")
    qtbot.addWidget(viewer)
    path_dlg = InsertMediaDialog(win.manager.session("lib").cfg, "avatar", None)
    path_dlg.set_file(str(png))
    path_dlg.pathopt.setChecked(True)
    path_dlg._accept()
    assert path_dlg.value.startswith("lib_media/images/face_")


def test_builder_role_column(win, qtbot):
    win.design_table("lib", None, "users")
    b: TableBuilderTab = win.tabs.currentWidget()
    qtbot.waitUntil(lambda: b.original is not None, timeout=5000)
    t = b.cols.table
    row = next(r for r in range(t.rowCount()) if t.item(r, 0).text() == "password_hash")
    item = t.item(row, b.C_ROLE)
    item.setData(Qt.ItemDataRole.UserRole, "password:bcrypt")
    item.setText(ColumnRole("password", "bcrypt").label())
    assert b.statements() == ([], [])  # roles need no SQL
    b.apply()
    assert role_store().get("lib", None, "users")["password_hash"] == ColumnRole("password", "bcrypt")
    # typing a password-like column name triggers a tip
    b.add_column()
    t.item(t.rowCount() - 1, 0).setText("user_password")
    qtbot.waitUntil(lambda: "Storing a password?" in _help_text(win), timeout=2000)


def test_erd_tab(win, qtbot):
    role_store().set_table("lib", None, "users", {"avatar": ColumnRole("media", storage="blob", media_kind="image")})
    win.open_erd("lib", None)
    tab: ErdTab = win.tabs.currentWidget()
    qtbot.waitUntil(lambda: len(tab.nodes) == 3, timeout=5000)
    items = tab.scene.items()
    edges = [i for i in items if isinstance(i, RelationEdge)]
    stores = [i for i in items if isinstance(i, StorageNode)]
    assert len(edges) == 1 and edges[0].rel.child == "books" and edges[0].rel.parent == "authors"
    assert len(stores) == 1 and "INSIDE the database" in stores[0].lines[0][1]
    edges[0].tab.explain_relation(edges[0].rel)
    qtbot.waitUntil(lambda: "one-to-many" in _help_text(win), timeout=2000)
    sql = tab.join_sql(edges[0].rel)
    assert "JOIN" in sql and "author_id" in sql
    node: TableNode = tab.nodes["books"]
    node.setPos(node.pos().x() + 50, node.pos().y())
    tab._write_positions()
    assert "books" in tab._saved_positions()


def test_join_visualizer_counts(win, qtbot):
    s = win.manager.session("lib")
    dlg = JoinVisualizer(s, "SELECT * FROM authors a LEFT JOIN books b ON b.author_id = a.id")
    qtbot.addWidget(dlg)
    view = dlg.findChild(JoinStepView)
    qtbot.waitUntil(lambda: view.diagram is not None and view.diagram.stats is not None, timeout=5000)
    text = view.stats_label.text()
    assert "returns <b>4</b> rows" in text and view.samples.count() == 3


def test_query_tab_teaching(win, qtbot, monkeypatch):
    tab = win.new_query("lib", sql="SELECT * FROM nope;")
    tab.run(all_=True)
    qtbot.waitUntil(lambda: not tab.running, timeout=5000)
    assert "💡" in tab.messages.toPlainText()
    qtbot.waitUntil(lambda: "doesn't exist" in _help_text(win), timeout=2000)

    tab.editor.setPlainText("UPDATE books SET title = 'x' WHERE id = 1")
    tab.explain_english()
    qtbot.waitUntil(lambda: "Only rows where" in _help_text(win), timeout=2000)

    # Beginner mode asks before a DELETE without WHERE; cancelling runs nothing
    HelpCenter.get().set_beginner(True)
    asked = []
    monkeypatch.setattr(QMessageBox, "exec", lambda self: asked.append(self.text()) or QMessageBox.StandardButton.Cancel)
    tab.editor.setPlainText("DELETE FROM books")
    tab.run(all_=True)
    assert asked and not tab.running
    with win.manager.session("lib").engine.connect() as c:
        assert c.exec_driver_sql("SELECT COUNT(*) FROM books").scalar() == 3


def test_lesson_panel_do_it_and_check(win, qtbot):
    panel = win.lesson_panel
    panel.combo.setCurrentIndex(0)
    panel._goto(1)  # "Create the authors table"
    assert panel.step.title == "Create the authors table"
    panel.do_it()
    qtbot.waitUntil(lambda: "✅" in panel.result.text(), timeout=8000)
    assert win.manager.is_open(lessons.PRACTICE_ID)
    assert "authors" in win.manager.session(lessons.PRACTICE_ID).tables()
    panel._goto(panel.step_index + 1)
    assert panel.step.title == "Add some rows"


def test_secret_store_used_for_connection_passwords(win):
    cfg = ConnectionConfig(name="m", driver="mysql", host="h", user="u", save_password=True)
    from dbide.core.config import get_password, set_password

    set_password(cfg, "pw1")
    assert secrets.store().get(cfg.id) == "pw1" and get_password(cfg) == "pw1"
