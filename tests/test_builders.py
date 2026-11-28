"""Plain-English → SQL, chart selection and the schema designer's plan (no GUI)."""
import pytest

from dbide.core.chart_model import auto_spec, build, compact, nice_ticks
from dbide.core.config import ConnectionConfig
from dbide.core.nl2sql import NotUnderstood, load_schema, singular, translate
from dbide.core.schema_plan import DesignerModel
from dbide.core.session import DatabaseSession
from dbide.drivers import get_driver

SHOP = """
CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT, country TEXT, created_at TEXT);
CREATE TABLE products (id INTEGER PRIMARY KEY, title TEXT, price REAL, category TEXT);
CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id), status TEXT, total REAL, created_at TEXT);
CREATE TABLE order_items (id INTEGER PRIMARY KEY, order_id INTEGER REFERENCES orders(id), product_id INTEGER REFERENCES products(id), quantity INTEGER);
INSERT INTO customers VALUES (1,'Ada','ada@x.com','UK','2023-02-01'),(2,'Grace','g@x.com','USA','2024-05-03'),
  (3,'Linus','l@x.com','Finland','2022-01-09'),(4,'Nobody',NULL,'UK','2024-07-07');
INSERT INTO products VALUES (1,'Keyboard',49.5,'hardware'),(2,'Mouse',19.0,'hardware'),(3,'IDE License',120,'software'),(4,'Cable',5,'hardware');
INSERT INTO orders VALUES (1,1,'paid',69.0,'2024-01-05'),(2,1,'shipped',120,'2024-03-01'),(3,2,'paid',19,'2023-11-11'),(4,3,'cancelled',49.5,'2024-02-02');
INSERT INTO order_items VALUES (1,1,1,1),(2,1,2,1),(3,2,3,1),(4,3,2,1),(5,4,1,1);
"""


@pytest.fixture(scope="module")
def shop(tmp_path_factory):
    path = tmp_path_factory.mktemp("shop") / "shop.db"
    s = DatabaseSession(ConnectionConfig(name="shop", driver="sqlite", database=str(path), id="shop"))
    q = s.query_connection()
    assert all(r.error is None for r in q.run(SHOP))
    q.close()
    info = load_schema(s)
    yield s, info
    s.close()


def ask(shop, text):
    s, info = shop
    g = translate(info, text, s.driver.quote, s.driver.qualify)
    q = s.query_connection()
    (r,) = q.run(g.sql)
    q.close()
    assert r.error is None, (text, g.sql, r.error)
    assert g.sql.lstrip().upper().startswith("SELECT")
    return g, r.columns, r.rows


@pytest.mark.parametrize("text,check", [
    ("show customers in UK", lambda c, rows: sorted(r[1] for r in rows) == ["Ada", "Nobody"]),
    ("customers from Finland sorted by name", lambda c, rows: [r[1] for r in rows] == ["Linus"]),
    ("how many orders", lambda c, rows: rows == [(4,)]),
    ("count orders per customer", lambda c, rows: rows[0] == ("Ada", 2) and ("Nobody", 0) in rows),
    ("orders by status", lambda c, rows: rows[0] == ("paid", 2)),
    ("total of orders per customer", lambda c, rows: rows[0] == ("Ada", 189.0)),
    ("average price of products", lambda c, rows: rows[0][0] == pytest.approx(48.375)),
    ("paid orders over 50", lambda c, rows: [r[0] for r in rows] == [1]),
    ("orders in 2024", lambda c, rows: sorted(r[0] for r in rows) == [1, 2, 4]),
    ("orders after 2023", lambda c, rows: sorted(r[0] for r in rows) == [1, 2, 4]),
    ("top 2 most expensive products", lambda c, rows: [r[1] for r in rows] == ["IDE License", "Keyboard"]),
    ("customers without orders", lambda c, rows: [r[1] for r in rows] == ["Nobody"]),
    ("customers with more than 1 order", lambda c, rows: [r[1] for r in rows] == ["Ada"]),
    ("customers that never ordered", lambda c, rows: [r[1] for r in rows] == ["Nobody"]),
    ("products never ordered", lambda c, rows: [r[1] for r in rows] == ["Cable"]),
    ("the 3 newest orders", lambda c, rows: [r[0] for r in rows] == [2, 4, 1]),
    ("products whose title contains 'key'", lambda c, rows: [r[1] for r in rows] == ["Keyboard"]),
    ("orders that are not paid", lambda c, rows: sorted(r[2] for r in rows) == ["cancelled", "shipped"]),
    ("customers where email is empty", lambda c, rows: [r[1] for r in rows] == ["Nobody"]),
    ("unique countries of customers", lambda c, rows: sorted(r[0] for r in rows) == ["Finland", "UK", "USA"]),
    ("orders for Ada", lambda c, rows: sorted(r[0] for r in rows) == [1, 2]),
    ("products between 10 and 60", lambda c, rows: sorted(r[1] for r in rows) == ["Keyboard", "Mouse"]),
    ("number of products by category", lambda c, rows: rows[0] == ("hardware", 3)),
    ("custmers in usa", lambda c, rows: [r[1] for r in rows] == ["Grace"]),
    ("products ordered by Ada", lambda c, rows: sorted(r[1] for r in rows) == ["IDE License", "Keyboard", "Mouse"]),
    ("total quantity per product", lambda c, rows: dict(rows)["Keyboard"] == 2 and dict(rows)["Cable"] is None),
    ("average order total by country", lambda c, rows: rows[0] == ("UK", 94.5)),
    ("orders where total is at least 100", lambda c, rows: [r[0] for r in rows] == [2]),
    ("cancelled orders from Linus", lambda c, rows: [r[0] for r in rows] == [4]),
    ("list names and emails of customers", lambda c, rows: c == ["name", "email"] and len(rows) == 4),
])
def test_plain_english_queries(shop, text, check):
    g, cols, rows = ask(shop, text)
    assert check(cols, rows), (text, g.sql, rows)
    assert not g.ignored, (text, g.ignored)
    assert g.steps


def test_not_understood_and_ignored(shop):
    s, info = shop
    with pytest.raises(NotUnderstood, match="which table"):
        translate(info, "show me everything", s.driver.quote, s.driver.qualify)
    g = translate(info, "orders flibbertigibbet", s.driver.quote, s.driver.qualify)
    assert "flibbertigibbet" in g.ignored and not g.confident


def test_protected_columns_not_indexed(shop):
    s, _ = shop
    info = load_schema(s, protected={"customers": {"email"}})
    assert "ada@x.com" not in info.values and "uk" in info.values


def test_mysql_quoting():
    from dbide.core.nl2sql import Col, FK, SchemaInfo

    info = SchemaInfo({"users": [Col("users", "id", "INT", True), Col("users", "name", "VARCHAR(50)")],
                       "posts": [Col("posts", "id", "INT", True), Col("posts", "user_id", "INT")]},
                      [FK("posts", ["user_id"], "users", ["id"])])
    drv = get_driver("mysql")
    g = translate(info, "count posts per user", drv.quote, drv.qualify, "blog")
    assert "FROM `blog`.`users` AS u" in g.sql and "LEFT JOIN `blog`.`posts` AS p" in g.sql


def test_singular():
    assert [singular(w) for w in ("categories", "boxes", "people", "status", "orders")] == ["category", "box", "person", "status", "order"]


# ---- charts ------------------------------------------------------------------
@pytest.mark.parametrize("cols,rows,kind", [
    (["count"], [(4,)], "stat"),
    (["name", "orders_count"], [("Ada", 2), ("Grace", 1)], "bar"),
    (["country", "sum_total", "avg_total"], [("UK", 189, 94.5), ("USA", 19, 19)], "bar"),
    (["created_at", "total"], [("2024-01-05", 69), ("2023-11-11", 19), ("2024-03-01", 120)], "line"),
    (["year", "books"], [(1813, 1), (1815, 1), (1987, 2)], "line"),
    (["price", "quantity"], [(1.0, 2), (3.0, 4), (5.0, 1)], "scatter"),
    (["name", "email"], [("a", "b")], "none"),
    ([], [], "none"),
])
def test_auto_chart(cols, rows, kind):
    spec = auto_spec(cols, rows)
    assert spec.kind == kind and spec.reason
    data = build(spec, cols, rows)
    if kind == "bar":
        assert data.series[0].points[0][1] >= data.series[-1].points[-1][1] or len(data.series) > 1
    if kind == "line":
        xs = [x for x, _ in data.series[0].points]
        assert xs == sorted(xs)


def test_bars_are_ranked_and_capped():
    rows = [(f"c{i}", i) for i in range(40)]
    spec = auto_spec(["cat", "n"], rows)
    data = build(spec, ["cat", "n"], rows)
    assert len(data.series[0].points) == 24 and data.series[0].points[0] == ("c39", 39.0) and "top 24" in spec.note


def test_ticks_and_compact_numbers():
    assert nice_ticks(0, 189) == [0, 50, 100, 150, 200]
    assert compact(1284) == "1,284" and compact(12_900) == "12.9K" and compact(4_200_000) == "4.2M"
    assert compact(5000, 20_000) == "5K"  # one style per axis


# ---- schema designer plan ---------------------------------------------------------
@pytest.fixture
def lib(tmp_path):
    s = DatabaseSession(ConnectionConfig(name="lib", driver="sqlite", database=str(tmp_path / "lib.db")))
    q = s.query_connection()
    q.run("CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT NOT NULL); INSERT INTO authors (name) VALUES ('A'),('B');")
    q.close()
    yield s
    s.close()


def apply_plan(s, model):
    for step in model.plan(s.driver):
        s.execute_ddl(step.statements)


def test_designer_builds_relationships(lib):
    defs = {n: lib.table_def(n) for n in lib.tables()}
    m = DesignerModel("sqlite", defs)
    assert not m.dirty
    m.add_table("Books")
    m.add_column("books", "title", "TEXT", nullable=False)
    summary = m.add_relationship("books", "authors", "one_to_many", required=True, on_delete="CASCADE")
    assert "books.author_id" in summary and m.dirty
    m.add_table("tags")
    m.add_column("tags", "label", "TEXT", unique=True)
    m.add_relationship("books", "tags", "many_to_many")
    m.add_table("profiles")
    m.add_relationship("profiles", "authors", "one_to_one")
    steps = m.plan(lib.driver)
    titles = [s.title for s in steps]
    # parents are created before the tables that point at them
    assert titles.index(next(t for t in titles if t.startswith("Create table books"))) < titles.index(next(t for t in titles if t.startswith("Create table books_tags")))
    apply_plan(lib, m)
    assert set(lib.tables()) == {"authors", "books", "tags", "books_tags", "profiles"}
    fk = lib.table_def("books").foreign_keys[0]
    assert (fk.columns, fk.ref_table, fk.on_delete) == (["author_id"], "authors", "CASCADE")
    junction = lib.table_def("books_tags")
    assert junction.primary_key == ["book_id", "tag_id"] and len(junction.foreign_keys) == 2
    prof = lib.table_def("profiles")
    assert any(ix.unique and ix.columns == ["author_id"] for ix in prof.indexes)
    # the data in the existing table survived
    assert lib.fetch_page("authors", None, 10, 0).total == 2
    # reload: nothing pending
    again = DesignerModel("sqlite", {n: lib.table_def(n) for n in lib.tables()})
    assert not again.dirty


def test_designer_links_existing_table_and_drops(lib):
    q = lib.query_connection()
    q.run("CREATE TABLE posts (id INTEGER PRIMARY KEY, body TEXT); INSERT INTO posts (body) VALUES ('hi');")
    q.close()
    m = DesignerModel("sqlite", {n: lib.table_def(n) for n in lib.tables()})
    m.add_relationship("posts", "authors", "one_to_many")
    steps = m.plan(lib.driver)
    assert len(steps) == 1 and steps[0].title.startswith("Change table posts") and "link author_id → authors" in steps[0].title
    apply_plan(lib, m)
    assert lib.table_def("posts").foreign_keys[0].ref_table == "authors"
    assert lib.fetch_page("posts", None, 10, 0).rows[0][1] == "hi"
    m = DesignerModel("sqlite", {n: lib.table_def(n) for n in lib.tables()})
    m.drop_table("authors")
    assert not m.tables["posts"].foreign_keys  # links to a dropped table go too
    titles = [s.title for s in m.plan(lib.driver)]
    assert titles[-1] == "Delete table authors and all its rows"
    apply_plan(lib, m)
    assert lib.tables() == ["posts"]


def test_designer_errors_and_mysql_sql():
    m = DesignerModel("mysql", {})
    m.add_table("customers")
    m.add_table("orders")
    m.add_relationship("orders", "customers", "one_to_many")
    with pytest.raises(ValueError, match="already linked"):
        m.add_relationship("orders", "customers", "one_to_many")
    with pytest.raises(ValueError, match="already has a column"):
        m.add_column("orders", "id", "INT")
    sql = [st for s in m.plan(get_driver("mysql")) for st in s.statements]
    assert sql[0].startswith("CREATE TABLE `customers`")
    assert "FOREIGN KEY (`customer_id`) REFERENCES `customers` (`id`)" in sql[1]
    assert m.unique_name("orders") == "orders_2"
