"""Templates, the playground sandbox and progress levels (no GUI)."""
import sqlite3

import pytest

from dbide.core import playground as P
from dbide.core.config import ConnectionConfig
from dbide.core.nl2sql import load_schema, translate
from dbide.core.progress import FEATURES, LEVELS, analyze_sql, progress
from dbide.core.roles import role_store
from dbide.core.session import DatabaseSession
from dbide.core.settings import settings
from dbide.core.templates import TEMPLATES, apply_template
from dbide.drivers import get_driver


@pytest.mark.parametrize("key", list(TEMPLATES))
def test_template_builds_and_try_this_works(key, tmp_path):
    tpl = TEMPLATES[key]
    cfg = ConnectionConfig(name=key, driver="sqlite", database=str(tmp_path / f"{key}.db"), id=key)
    s = DatabaseSession(cfg)
    counts = apply_template(s, tpl)
    assert set(s.tables()) == {t.name for t in tpl.tables}
    assert all(n > 0 for n in counts.values()), counts
    for t, fks in ((t.name, t.foreign_keys) for t in tpl.tables):
        assert len(s.table_def(t).foreign_keys) == len(fks)
    # media files really exist for path-stored media
    from dbide.core import media

    for table, roles in tpl.roles.items():
        for col, role in roles.items():
            assert role_store().get(key, None, table)[col] == role
            if role.kind == "media" and role.storage == "path":
                with s.engine.connect() as c:
                    v = c.exec_driver_sql(f'SELECT "{col}" FROM "{table}" LIMIT 1').scalar()
                assert media.resolve(v, cfg).exists()
    info = load_schema(s, protected={t: set(r) for t, r in tpl.roles.items()})
    q = s.query_connection()
    for tt in tpl.try_this:
        sql = tt.text if tt.sql else translate(info, tt.text, s.driver.quote, s.driver.qualify).sql
        if not tt.sql:
            g = translate(info, tt.text, s.driver.quote, s.driver.qualify)
            assert not g.ignored, (key, tt.text, g.ignored, g.sql)
        (r,) = q.run(sql)[-1:]
        assert r.error is None, (key, tt.text, sql, r.error)
        if r.returns_rows:
            assert r.rows, (key, tt.text, sql)
    q.close()
    s.close()


def test_templates_generate_mysql():
    drv = get_driver("mysql")
    for tpl in TEMPLATES.values():
        for td in tpl.table_defs("mysql"):
            (sql,) = drv.create_table_sql(td)
            assert sql.startswith(f"CREATE TABLE `{td.name}`") and "ENGINE=InnoDB" in sql
            assert "TEXT NOT NULL" not in sql or td.name in ("comments",)  # short text maps to VARCHAR(255)


def test_template_refuses_existing_tables(tmp_path):
    s = DatabaseSession(ConnectionConfig(name="x", driver="sqlite", database=str(tmp_path / "x.db")))
    s.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY)")
    with pytest.raises(ValueError, match="already exist"):
        apply_template(s, TEMPLATES["store"])


@pytest.fixture
def pg(tmp_path):
    pg = P.Playground(tmp_path / "pg")
    pg.create("store")
    s = DatabaseSession(pg.config("store"))
    pg.attach(s, "store")
    yield pg, s
    s.close()


def count(s, table):
    with s.engine.connect() as c:
        return c.exec_driver_sql(f'SELECT COUNT(*) FROM "{table}"').scalar()


def test_playground_undo_everything(pg):
    pg, s = pg
    before = count(s, "customers")
    q = s.query_connection()
    assert q.run("SELECT * FROM customers LIMIT 1")[0].error is None
    assert pg.undo_depth("store") == 0  # reads don't snapshot
    q.run("DELETE FROM customers WHERE id NOT IN (SELECT customer_id FROM orders)")
    q.run("DROP TABLE order_items")
    assert pg.undo_depth("store") == 2 and "order_items" not in s.tables()
    assert pg.undo("store").startswith("DROP TABLE")
    assert "order_items" in s.tables()
    assert pg.undo("store").startswith("DELETE")
    assert count(s, "customers") == before
    assert pg.undo("store") is None
    # grid edits and DDL snapshot too
    s.apply_changes("customers", None, [({"id": 1}, {"name": "Changed"})], [], [])
    assert pg.undo_depth("store") == 1
    pg.undo("store")
    with s.engine.connect() as c:
        assert c.exec_driver_sql("SELECT name FROM customers WHERE id = 1").scalar() != "Changed"
    q.close()


def test_playground_is_sandboxed(pg, tmp_path):
    pg, s = pg
    other = tmp_path / "secret.db"
    sqlite3.connect(other).execute("CREATE TABLE t (x)").connection.commit()
    q = s.query_connection()
    (r,) = q.run(f"ATTACH DATABASE '{other}' AS other")
    assert r.error and "not authorized" in r.error.lower()
    s.time_limit = 0.3
    (r,) = q.run("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n) SELECT COUNT(*) FROM n")
    assert r.error and "stopped after" in r.error
    assert q.run("SELECT 1")[0].error is None  # the connection still works afterwards
    q.close()


def test_playground_reset_and_save_as(pg, tmp_path):
    pg, s = pg
    s.close()
    pg.create("store")  # reset
    s2 = DatabaseSession(pg.config("store"))
    assert count(s2, "products") == 25
    out = pg.save_as("store", tmp_path / "myshop.sqlite")
    real = DatabaseSession(ConnectionConfig(name="r", driver="sqlite", database=str(out)))
    assert count(real, "orders") == 120 and (tmp_path / "store_media").exists()
    s2.close()
    real.close()


def test_progress_levels_and_features():
    pr = progress()
    settings().set("progress", {"skills": {}, "override": None})
    events = []
    pr.listeners.append(lambda e, p: events.append((e, p)))
    assert pr.level == 0 and pr.level_name == "Beginner" and not pr.allowed("diagram") and pr.allowed("unknown_feature")
    assert pr.record("first_query", "filter") == ["first_query", "filter"]
    assert pr.record("filter") == []  # already known
    assert pr.level == 0 and pr.next_level() == ("Explorer", 2)
    pr.record_sql("SELECT * FROM t ORDER BY x", True)
    assert pr.level == 0
    pr.record("ask")
    assert pr.level == 1 and ("level", 1) in events and pr.allowed("diagram") and not pr.allowed("query_plan")
    pr.set_override(3)
    assert pr.level == 3 and pr.allowed("security") and pr.next_level() is None
    pr.reset()
    assert pr.level == 0 and pr.points == 0
    assert {f.level for f in FEATURES.values()} <= set(range(len(LEVELS)))


def test_analyze_sql():
    assert analyze_sql("SELECT a FROM t JOIN u ON u.id = t.u WHERE x = 'GROUP BY' ORDER BY a", True) == {"first_query", "join", "filter", "sort"}
    assert analyze_sql("select status, count(*) from orders group by status", True) == {"first_query", "group"}
    assert analyze_sql("CREATE TABLE b (a_id INT REFERENCES a(id))", True) == {"create_table", "relationship"}
    assert analyze_sql("UPDATE t SET x = 1", True) == {"edit_data"}
    assert analyze_sql("SELECT 1", False) == set()
