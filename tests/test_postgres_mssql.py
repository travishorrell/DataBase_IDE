"""PostgreSQL against a real (private, throwaway) server; SQL Server SQL generation."""
import threading
import time
import uuid

import pytest

from dbide.core import admin as A
from dbide.core.config import ConnectionConfig
from dbide.core.erd_model import load_erd
from dbide.core.nl2sql import load_schema, translate
from dbide.core.schema_model import ColumnDef, ForeignKeyDef, IndexDef, TableDef
from dbide.core.schema_plan import DesignerModel
from dbide.core.session import DatabaseSession
from dbide.core.templates import TEMPLATES, apply_template
from dbide.drivers import get_driver


@pytest.fixture(scope="module")
def pg_server(tmp_path_factory):
    pgserver = pytest.importorskip("pgserver")
    srv = pgserver.get_server(str(tmp_path_factory.mktemp("pgdata")), cleanup_mode="stop")
    host = srv.get_uri().split("host=")[1]
    yield host
    srv.cleanup()


@pytest.fixture
def pg(pg_server):
    """A session plus a fresh schema of its own for each test."""
    cfg = ConnectionConfig(name="pg", driver="postgresql", host=pg_server, user="postgres", database="postgres",
                           id="pg-" + uuid.uuid4().hex[:6])
    s = DatabaseSession(cfg)
    assert "PostgreSQL" in s.connect()
    schema = "t_" + uuid.uuid4().hex[:8]
    s.execute(f'CREATE SCHEMA "{schema}"')
    yield s, schema
    s.execute(f'DROP SCHEMA "{schema}" CASCADE')
    s.close()


def scalar(s, sql):
    with s.engine.connect() as c:
        return c.exec_driver_sql(sql).scalar()


@pytest.mark.parametrize("key", list(TEMPLATES))
def test_pg_templates(pg, key):
    s, schema = pg
    tpl = TEMPLATES[key]
    counts = apply_template(s, tpl, schema)
    assert set(s.tables(schema)) == {t.name for t in tpl.tables}
    assert all(n > 0 for n in counts.values())
    for t in tpl.tables:
        assert len(s.table_def(t.name, schema).foreign_keys) == len(t.foreign_keys)
    # plain English → SQL in the Postgres dialect, run for real
    info = load_schema(s, schema, protected={t: set(r) for t, r in tpl.roles.items()})
    q = s.query_connection()
    assert q.run(f'SET search_path TO "{schema}"')[0].error is None
    for tt in tpl.try_this:
        sql = tt.text if tt.sql else translate(info, tt.text, s.driver.quote, s.driver.qualify, schema, s.driver.select_limit).sql
        res = q.run(sql)
        assert all(r.error is None for r in res), (key, tt.text, sql, [r.error for r in res])
    q.close()


def test_pg_grid_paging_and_identity(pg):
    s, schema = pg
    apply_template(s, TEMPLATES["store"], schema)
    page = s.fetch_page("customers", schema, 10, 10)
    assert page.total == 40 and len(page.rows) == 10 and page.rows[0][0] == 11 and page.primary_key == ["id"]
    # explicit ids were inserted: the identity sequence must have moved past them
    s.apply_changes("customers", schema, [({"id": 1}, {"country": "Iceland"})], [{"name": "New", "email": "new@x"}], [])
    assert scalar(s, f'SELECT MAX(id) FROM "{schema}".customers') == 41
    assert scalar(s, f'SELECT country FROM "{schema}".customers WHERE id = 1') == "Iceland"
    with pytest.raises(Exception):  # foreign keys enforced
        s.apply_changes("orders", schema, [], [{"customer_id": 9999, "status": "paid"}], [])


def test_pg_designer_alter(pg):
    s, schema = pg
    drv = s.driver
    td = TableDef("authors", schema, [ColumnDef("id", "INTEGER", False, True, True), ColumnDef("name", "TEXT", False)])
    s.execute_ddl(drv.create_table_sql(td))
    s.insert_rows("authors", schema, ["name"], [["A"], ["B"]])
    defs = {t: s.table_def(t, schema) for t in s.tables(schema)}
    assert defs["authors"].columns[0].auto_increment and defs["authors"].columns[0].default is None
    m = DesignerModel("postgresql", defs)
    for t in m.tables.values():
        t.schema = schema
    m.add_table("books").schema = schema
    m.add_column("books", "title", "VARCHAR(200)", nullable=False)
    m.add_relationship("books", "authors", "one_to_many", required=True, on_delete="CASCADE")
    a = m.tables["authors"]
    a.columns[1].name = "full_name"  # rename
    a.columns.append(ColumnDef("born", "INTEGER", default="1900"))
    a.indexes.append(IndexDef("ux_authors_full_name", ["full_name"], True))
    for step in m.plan(drv):
        s.execute_ddl(step.statements)
    td = s.table_def("authors", schema)
    assert [c.name for c in td.columns] == ["id", "full_name", "born"] and td.columns[2].default.startswith("1900")
    assert any(i.unique and i.columns == ["full_name"] for i in td.indexes)
    fk = s.table_def("books", schema).foreign_keys[0]
    assert (fk.columns, fk.ref_table, fk.on_delete) == (["author_id"], "authors", "CASCADE")
    # type change, NOT NULL, default removal, drop column, rename table: in one ALTER plan
    old = s.table_def("authors", schema)
    new = old.clone()
    new.columns[2].type = "BIGINT"
    new.columns[2].nullable = False
    new.columns[2].default = None
    new.name = "writers"
    s.execute_ddl(drv.alter_table_sql(old, new))
    td = s.table_def("writers", schema)
    assert td.columns[2].type.upper() == "BIGINT" and not td.columns[2].nullable and td.columns[2].default is None
    assert s.fetch_page("writers", schema, 10, 0).total == 2
    # a failing DDL batch rolls back completely (Postgres DDL is transactional)
    with pytest.raises(Exception):
        s.execute_ddl([f'ALTER TABLE "{schema}".writers ADD COLUMN extra INT', "NOT VALID SQL"])
    assert "extra" not in [c.name for c in s.table_def("writers", schema).columns]


def test_pg_cancel_long_query(pg):
    s, _ = pg
    q = s.query_connection()
    q.run("SELECT 1")
    threading.Timer(0.5, q.cancel).start()
    t0 = time.time()
    (r,) = q.run("SELECT pg_sleep(10)")
    assert r.error and "cancel" in r.error.lower() and time.time() - t0 < 5
    assert q.run("SELECT 2")[0].rows == [(2,)]
    q.close()


def test_pg_admin(pg, tmp_path):
    s, schema = pg
    apply_template(s, TEMPLATES["library"], schema)
    ov = A.overview(s, schema)
    assert ov.version and ov.size_bytes > 0 and {t.name: t.rows for t in ov.tables}["loans"] == 40
    assert A.integrity_check(s, schema).ok
    assert A.analyze(s, schema).ok and "VACUUM" in A.optimize(s, schema).title
    procs = A.processes(s)
    assert "pid" in procs.columns and procs.rows
    users = A.users(s)
    assert any(r[0] == "postgres" for r in users.rows)
    assert any(r[0] == "max_connections" for r in A.settings_list(s).rows)
    out = tmp_path / "dump.sql"
    msg = A.backup(s, out, schema).message
    text = out.read_text()
    assert "rows" in msg and "CREATE TABLE" in text and "INSERT INTO" in text and "setval" in text
    # media in BYTEA is measured with OCTET_LENGTH for the diagram
    from dbide.core.roles import role_store

    roles = role_store().all_tables(s.cfg.id, schema)
    erd = load_erd(s, schema, roles)
    cover = next(m for m in erd.media if m.column == "cover")
    assert cover.storage == "blob" and cover.total_bytes > 0


# ---- SQL Server (generation only: no server available here) -------------------------------------
def test_mssql_sql():
    drv = get_driver("mssql")
    assert drv.quote("we]ird") == "[we]]ird]" and drv.qualify("t", "dbo") == "[dbo].[t]"
    assert drv.select_limit("SELECT * FROM t", 5) == "SELECT TOP 5 * FROM t"
    assert drv.select_limit("SELECT DISTINCT a FROM t", 5) == "SELECT DISTINCT TOP 5 a FROM t"
    assert drv.select_limit("SELECT * FROM t", 10, 20, "ORDER BY id").endswith("ORDER BY id\nOFFSET 20 ROWS FETCH NEXT 10 ROWS ONLY")
    assert "ORDER BY (SELECT NULL)" in drv.select_limit("SELECT * FROM t", 10, 20)
    td = TEMPLATES["store"].table_defs("mssql")[2]
    td.schema = "dbo"
    (sql,) = drv.create_table_sql(td)[:1]
    assert "[id] INT IDENTITY(1,1) NOT NULL" in sql and "CONSTRAINT [pk_orders] PRIMARY KEY ([id])" in sql
    assert "CONSTRAINT [df_orders_status] DEFAULT 'paid'" in sql and "REFERENCES [dbo].[customers] ([id])" in sql
    old = TableDef("t", "dbo", [ColumnDef("id", "INT", False, True, True, original_name="id"),
                                 ColumnDef("a", "NVARCHAR(50)", True, default="'x'", original_name="a")], original_name="t")
    new = old.clone()
    new.columns[1].name = "b"
    new.columns[1].type = "NVARCHAR(100)"
    new.columns[1].default = None
    new.columns.append(ColumnDef("c", "INT"))
    new.foreign_keys.append(ForeignKeyDef("fk_t_c", ["c"], "other", ["id"]))
    new.name = "t2"
    stmts = drv.alter_table_sql(old, new)
    assert stmts[0] == "EXEC sp_rename N'dbo.t.a', N'b', 'COLUMN'"
    assert stmts[1] == "ALTER TABLE [dbo].[t] ALTER COLUMN [b] NVARCHAR(100) NULL"
    assert "sys.default_constraints" in stmts[2] and "DROP CONSTRAINT" in stmts[2]
    assert stmts[3] == "ALTER TABLE [dbo].[t] ADD [c] INT NULL"
    assert stmts[4].startswith("ALTER TABLE [dbo].[t] ADD CONSTRAINT [fk_t_c] FOREIGN KEY ([c])")
    assert stmts[5] == "EXEC sp_rename N'dbo.t', N't2'"
    with pytest.raises(ValueError, match="IDENTITY"):
        bad = old.clone()
        bad.columns[1].auto_increment = True
        drv.alter_table_sql(old, bad)
    assert drv.before_explicit_ids("[dbo].[t]") == ["SET IDENTITY_INSERT [dbo].[t] ON"]
    from dbide.drivers.mssql import _strip_parens

    assert _strip_parens("((0))") == "0" and _strip_parens("('paid')") == "'paid'" and _strip_parens("(getdate())") == "getdate()"
    url = drv.url(ConnectionConfig(name="m", driver="mssql", host="db", user="sa", database="shop"), "pw")
    assert url.drivername == "mssql+pymssql" and url.port == 1433 and url.database == "shop"


def test_mssql_translator_uses_top():
    from dbide.core.nl2sql import FK, Col, SchemaInfo

    drv = get_driver("mssql")
    info = SchemaInfo({"users": [Col("users", "id", "INT", True), Col("users", "name", "NVARCHAR(50)")],
                       "posts": [Col("posts", "id", "INT", True), Col("posts", "user_id", "INT")]},
                      [FK("posts", ["user_id"], "users", ["id"])])
    g = translate(info, "count posts per user", drv.quote, drv.qualify, "dbo", drv.select_limit)
    assert "LIMIT" not in g.sql and "OFFSET 0 ROWS FETCH NEXT 1000 ROWS ONLY" in g.sql and "[dbo].[users]" in g.sql
    g = translate(info, "users", drv.quote, drv.qualify, "dbo", drv.select_limit)
    assert g.sql.startswith("SELECT TOP 1000 u.*")


def test_percent_signs_in_raw_sql(pg):
    """Regression: psycopg treated every % as a placeholder, so LIKE '%a%' and 10 % 3 failed."""
    s, schema = pg
    q = s.query_connection()
    (r,) = q.run("SELECT 'abc' LIKE '%b%' AS ok, 10 % 3 AS modulo")
    assert r.error is None and r.rows == [(True, 1)]
    q.close()
    apply_template(s, TEMPLATES["tasks"], schema)
    assert s.fetch_page("tasks", schema, 5, 0, where="title LIKE '%e%'").total > 0
