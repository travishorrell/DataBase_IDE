import pytest

from dbide.core.config import ConnectionConfig, ConnectionStore
from dbide.core.schema_model import ColumnDef, ForeignKeyDef, IndexDef, TableDef
from dbide.core.session import DatabaseSession
from dbide.core.sqlsplit import split_sql, statement_at
from dbide.core.transfer import export_rows, read_csv, read_json
from dbide.drivers import get_driver


# ---- splitter ---------------------------------------------------------------
def test_split_respects_quotes_and_comments():
    sql = "SELECT 'a;b'; -- c;d\nSELECT \"x;y\" /* ; */ FROM t;\n\n  SELECT 3"
    assert [s.text for s in split_sql(sql)] == [
        "SELECT 'a;b'",
        "-- c;d\nSELECT \"x;y\" /* ; */ FROM t",
        "SELECT 3",
    ]


def test_split_mysql_hash_comment_and_escapes():
    sql = "SELECT 'it\\'s;'; # x;y\nSELECT `a;b`"
    assert [s.text for s in split_sql(sql, hash_comments=True)] == ["SELECT 'it\\'s;'", "# x;y\nSELECT `a;b`"]


def test_split_ignores_comment_only_tail():
    assert [s.text for s in split_sql("SELECT 1; -- done\n")] == ["SELECT 1"]


def test_split_keeps_trigger_body_whole():
    sql = (
        "CREATE TRIGGER trg AFTER INSERT ON t BEGIN\n"
        "  UPDATE t SET n = CASE WHEN 1 THEN 2 END;\n"
        "  INSERT INTO log VALUES (1);\n"
        "END;\nSELECT 1;"
    )
    parts = split_sql(sql)
    assert len(parts) == 2 and parts[0].text.endswith("END") and parts[1].text == "SELECT 1"


def test_statement_at_cursor():
    sql = "SELECT 1;\nSELECT 2;\nSELECT 3;"
    assert statement_at(sql, sql.index("2")).text == "SELECT 2"
    assert statement_at(sql, len(sql)).text == "SELECT 3"


# ---- config -----------------------------------------------------------------
def test_store_roundtrip(tmp_path):
    store = ConnectionStore(tmp_path / "c.json")
    cfg = ConnectionConfig(name="x", driver="mysql", host="h", port=3307, user="u")
    store.upsert(cfg)
    again = ConnectionStore(tmp_path / "c.json")
    assert again.get(cfg.id) == cfg
    assert "\"password\"" not in (tmp_path / "c.json").read_text()


# ---- SQLite session -----------------------------------------------------------
@pytest.fixture
def session(tmp_path):
    cfg = ConnectionConfig(name="t", driver="sqlite", database=str(tmp_path / "t.db"))
    s = DatabaseSession(cfg)
    s.connect()
    yield s
    s.close()


def _users(schema=None):
    return TableDef(
        name="users",
        schema=schema,
        columns=[
            ColumnDef("id", "INTEGER", nullable=False, primary_key=True, auto_increment=True),
            ColumnDef("email", "TEXT", nullable=False),
            ColumnDef("age", "INTEGER", default="0"),
        ],
        indexes=[IndexDef("ux_users_email", ["email"], unique=True)],
    )


def test_query_connection_runs_scripts(session):
    q = session.query_connection()
    res = q.run("CREATE TABLE t (a INTEGER); INSERT INTO t VALUES (1),(2),(3); SELECT * FROM t ORDER BY a;")
    assert [r.error for r in res] == [None, None, None]
    assert res[1].rowcount == 3
    assert res[2].columns == ["a"] and res[2].rows == [(1,), (2,), (3,)]
    trunc = q.run("SELECT * FROM t", max_rows=2)[0]
    assert trunc.truncated and len(trunc.rows) == 2
    err = q.run("SELECT nope FROM t; SELECT 1")
    assert len(err) == 1 and "no such column" in err[0].error
    # session state persists across runs (manual transaction)
    q.run("BEGIN; INSERT INTO t VALUES (9)")
    q.run("ROLLBACK")
    assert q.run("SELECT COUNT(*) FROM t")[0].rows == [(3,)]
    q.close()


def test_create_and_introspect(session):
    drv = session.driver
    session.execute_ddl(drv.create_table_sql(_users()))
    assert session.tables() == ["users"]
    td = session.table_def("users")
    assert td.primary_key == ["id"]
    assert td.columns[0].auto_increment
    assert td.columns[1].nullable is False
    assert td.columns[2].default == "0"
    assert [(i.name, i.columns, i.unique) for i in td.indexes] == [("ux_users_email", ["email"], True)]
    assert "AUTOINCREMENT" in session.ddl("users")


def test_grid_edit_cycle(session):
    session.execute_ddl(session.driver.create_table_sql(_users()))
    n = session.apply_changes("users", None, [], [{"email": "a@x"}, {"email": "b@x", "age": "30"}], [])
    assert n == 2
    page = session.fetch_page("users", None, 100, 0)
    assert page.total == 2 and page.primary_key == ["id"]
    assert page.rows == [[1, "a@x", 0], [2, "b@x", 30]]
    session.apply_changes("users", None, [({"id": 1}, {"email": "z@x", "age": None})], [], [{"id": 2}])
    page = session.fetch_page("users", None, 100, 0, where="email LIKE 'z%'")
    assert page.rows == [[1, "z@x", None]] and page.total == 1
    with pytest.raises(Exception):  # unique index enforced; transaction rolled back
        session.apply_changes("users", None, [], [{"email": "new@x"}, {"email": "z@x"}], [])
    assert session.fetch_page("users", None, 100, 0).total == 1


def test_simple_alter_uses_alter_statements(session):
    drv = session.driver
    session.execute_ddl(drv.create_table_sql(_users()))
    old = session.table_def("users")
    new = old.clone()
    new.columns[1].name = "mail"
    new.columns.append(ColumnDef("nickname", "TEXT"))
    new.indexes[0].columns = ["mail"]
    stmts = drv.alter_table_sql(old, new)
    assert not any(s.startswith("PRAGMA") for s in stmts), stmts
    session.execute_ddl(stmts)
    td = session.table_def("users")
    assert [c.name for c in td.columns] == ["id", "mail", "age", "nickname"]
    assert td.indexes[0].columns == ["mail"]


def test_rebuild_alter_keeps_data(session):
    drv = session.driver
    session.execute_ddl(drv.create_table_sql(_users()))
    session.apply_changes("users", None, [], [{"email": "a@x", "age": "5"}], [])
    old = session.table_def("users")
    new = old.clone()
    new.columns[2].type = "REAL"  # type change forces a rebuild
    new.columns[2].nullable = False
    new.columns.insert(1, ColumnDef("name", "TEXT", default="'anon'"))
    new.columns = [c for c in new.columns if c.name != "email"]
    new.indexes = []
    stmts = drv.alter_table_sql(old, new)
    assert stmts[0] == "PRAGMA foreign_keys = OFF"
    session.execute_ddl(stmts)
    page = session.fetch_page("users", None, 10, 0)
    assert page.columns == ["id", "name", "age"]
    assert page.rows == [[1, "anon", 5.0]]
    td = session.table_def("users")
    assert td.columns[0].auto_increment and td.columns[2].nullable is False


def test_failed_ddl_rolls_back(session):
    drv = session.driver
    session.execute_ddl(drv.create_table_sql(_users()))
    with pytest.raises(Exception):
        session.execute_ddl(['ALTER TABLE "users" ADD COLUMN "x" TEXT', "THIS IS NOT SQL"])
    assert [c.name for c in session.table_def("users").columns] == ["id", "email", "age"]


def test_foreign_keys(session):
    drv = session.driver
    session.execute_ddl(drv.create_table_sql(_users()))
    posts = TableDef(
        "posts",
        columns=[ColumnDef("id", "INTEGER", primary_key=True), ColumnDef("user_id", "INTEGER")],
        foreign_keys=[ForeignKeyDef("fk_posts_user", ["user_id"], "users", ["id"], on_delete="CASCADE")],
    )
    session.execute_ddl(drv.create_table_sql(posts))
    fk = session.table_def("posts").foreign_keys[0]
    assert (fk.columns, fk.ref_table, fk.ref_columns, fk.on_delete) == (["user_id"], "users", ["id"], "CASCADE")
    with pytest.raises(Exception):  # PRAGMA foreign_keys is on
        session.apply_changes("posts", None, [], [{"user_id": "99"}], [])


def test_import_export_roundtrip(session, tmp_path):
    session.execute_ddl(session.driver.create_table_sql(_users()))
    session.insert_rows("users", None, ["email", "age"], [["a@x", "1"], ["b,c@x", None]])
    for fmt in ("csv", "json", "sql"):
        cols, rows = session.iter_rows("users", None)
        path = tmp_path / f"out.{fmt}"
        assert export_rows(str(path), fmt, cols, rows, session.driver, "users") == 2
    header, rows = read_csv(str(tmp_path / "out.csv"))
    assert header == ["id", "email", "age"] and rows[1] == ["2", "b,c@x", None]
    cols, rows = read_json(str(tmp_path / "out.json"))
    assert rows[0] == [1, "a@x", 1]
    sql = (tmp_path / "out.sql").read_text()
    assert "INSERT INTO \"users\" (\"id\", \"email\", \"age\") VALUES (2, 'b,c@x', NULL);" in sql


def test_completion_catalog(session):
    session.execute_ddl(session.driver.create_table_sql(_users()))
    session.execute("CREATE VIEW adults AS SELECT * FROM users WHERE age >= 18")
    cat = session.completion_catalog()
    assert cat["users"] == ["id", "email", "age"] and "adults" in cat
    assert session.views() == ["adults"]


# ---- MySQL DDL (no server needed) -------------------------------------------------
def test_mysql_create_table_sql():
    drv = get_driver("mysql")
    td = _users("app")
    td.options = {"engine": "InnoDB"}
    td.columns[1].type = "VARCHAR(255)"
    (sql,) = drv.create_table_sql(td)
    assert sql == (
        "CREATE TABLE `app`.`users` (\n"
        "  `id` INTEGER NOT NULL AUTO_INCREMENT,\n"
        "  `email` VARCHAR(255) NOT NULL,\n"
        "  `age` INTEGER NULL DEFAULT 0,\n"
        "  PRIMARY KEY (`id`),\n"
        "  UNIQUE KEY `ux_users_email` (`email`)\n"
        ") ENGINE=InnoDB"
    )


def test_mysql_alter_table_sql():
    drv = get_driver("mysql")
    old = _users("app")
    for c in old.columns:
        c.original_name = c.name
    for i in old.indexes:
        i.original_name = i.name
    old.original_name = "users"
    new = old.clone()
    new.name = "members"
    new.columns[1].name = "mail"
    new.columns[2].type = "SMALLINT"
    new.columns.append(ColumnDef("bio", "TEXT"))
    new.indexes[0].columns = ["mail"]  # only follows the rename -> index is kept
    new.foreign_keys.append(ForeignKeyDef("fk_x", ["age"], "ages", ["id"]))
    assert drv.alter_table_sql(old, new) == [
        "ALTER TABLE `app`.`users` CHANGE COLUMN `email` `mail` TEXT NOT NULL",
        "ALTER TABLE `app`.`users` CHANGE COLUMN `age` `age` SMALLINT NULL DEFAULT 0",
        "ALTER TABLE `app`.`users` ADD COLUMN `bio` TEXT NULL AFTER `age`",
        "ALTER TABLE `app`.`users` ADD CONSTRAINT `fk_x` FOREIGN KEY (`age`) REFERENCES `app`.`ages` (`id`)",
        "RENAME TABLE `app`.`users` TO `app`.`members`",
    ]


def test_mysql_url_and_quoting():
    drv = get_driver("mysql")
    cfg = ConnectionConfig(name="m", driver="mysql", host="db", user="root", database="shop")
    url = drv.url(cfg, "p@ss")
    assert url.drivername == "mysql+pymysql" and url.port == 3306 and url.password == "p@ss"
    assert drv.quote("we`ird") == "`we``ird`"
    assert drv.literal("it's \\") == "'it''s \\\\'"


def test_rebuild_keeps_inline_unique_views_and_triggers(session):
    session.execute(
        "CREATE TABLE c (id INTEGER PRIMARY KEY, email TEXT UNIQUE, n INTEGER)"
    )
    session.execute("CREATE VIEW v AS SELECT * FROM c")
    session.execute("CREATE TABLE log (msg TEXT)")
    session.execute("CREATE TRIGGER t_ins AFTER INSERT ON c BEGIN INSERT INTO log VALUES (NEW.email); END")
    old = session.table_def("c")
    assert [(i.columns, i.unique, i.constraint) for i in old.indexes] == [(["email"], True, True)]
    new = old.clone()
    new.columns[2].type = "REAL"
    stmts = session.driver.alter_table_sql(old, new)
    session.execute_ddl(stmts)
    session.insert_rows("c", None, ["email"], [["a@x"]])
    with pytest.raises(Exception):
        session.insert_rows("c", None, ["email"], [["a@x"]])  # uniqueness survived
    assert session.fetch_page("log", None, 10, 0).rows == [["a@x"]]  # trigger survived
    assert session.fetch_page("v", None, 10, 0).total == 1  # view still works
    # leaving the constraint untouched while adding a column stays a simple ALTER
    old = session.table_def("c")
    new = old.clone()
    new.columns.append(ColumnDef("extra", "TEXT"))
    assert session.driver.alter_table_sql(old, new) == ['ALTER TABLE "c" ADD COLUMN "extra" TEXT']
