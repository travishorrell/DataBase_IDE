"""Pre-built project templates with realistic sample data.

Each template is defined once, dialect-neutrally (TableDef objects), so the same
template creates a SQLite file or tables inside a MySQL database. Templates
power both the Playground (a disposable copy) and "New Project from Template".
"""
from __future__ import annotations

import datetime as dt
import random
import struct
import tempfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from dbide.core.roles import ColumnRole
from dbide.core.schema_model import ColumnDef, ForeignKeyDef, IndexDef, TableDef

# --------------------------------------------------------------------------------
# helpers


def _pk() -> ColumnDef:
    return ColumnDef("id", "int", nullable=False, primary_key=True, auto_increment=True)


def _c(name: str, kind: str = "text", required: bool = False, default: str | None = None) -> ColumnDef:
    """Dialect-neutral column; kind is mapped to a real type when the template is applied."""
    return ColumnDef(name, kind, nullable=not required, default=default)


def _fk(col: str, table: str, on_delete: str = "") -> ForeignKeyDef:
    return ForeignKeyDef(f"fk_{col}_{table}", [col], table, ["id"], on_delete=on_delete)


TYPE_MAP = {
    "sqlite": {"int": "INTEGER", "text": "TEXT", "long": "TEXT", "money": "DECIMAL(10,2)", "real": "REAL",
               "date": "DATE", "datetime": "DATETIME", "bool": "BOOLEAN", "blob": "BLOB", "path": "TEXT"},
    "mysql": {"int": "INT", "text": "VARCHAR(255)", "long": "TEXT", "money": "DECIMAL(10,2)", "real": "DOUBLE",
              "date": "DATE", "datetime": "DATETIME", "bool": "TINYINT(1)", "blob": "LONGBLOB", "path": "VARCHAR(1024)"},
    "postgresql": {"int": "INTEGER", "text": "VARCHAR(255)", "long": "TEXT", "money": "NUMERIC(10,2)", "real": "DOUBLE PRECISION",
                   "date": "DATE", "datetime": "TIMESTAMP", "bool": "BOOLEAN", "blob": "BYTEA", "path": "TEXT"},
    "mssql": {"int": "INT", "text": "NVARCHAR(255)", "long": "NVARCHAR(MAX)", "money": "DECIMAL(10,2)", "real": "FLOAT",
              "date": "DATE", "datetime": "DATETIME2", "bool": "BIT", "blob": "VARBINARY(MAX)", "path": "NVARCHAR(1024)"},
}


def color_png(rgb: tuple[int, int, int], w: int = 96, h: int = 72) -> bytes:
    """A small shaded image in one colour (for sample product photos, book covers…)."""
    r0, g0, b0 = rgb
    rows = b""
    for y in range(h):
        shade = 0.65 + 0.35 * (y / h)
        rows += b"\x00" + bytes(
            v for x in range(w) for v in (int(r0 * shade) % 256, int(g0 * shade) % 256, int((b0 * shade + x)) % 256))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


@dataclass
class TryThis:
    text: str  # plain English (goes to the Ask bar) or SQL
    sql: bool = False
    note: str = ""  # what it teaches


@dataclass
class Template:
    key: str
    name: str
    icon: str  # theme icon alias
    description: str
    tables: list[TableDef]
    roles: dict[str, dict[str, ColumnRole]] = field(default_factory=dict)
    try_this: list[TryThis] = field(default_factory=list)
    learn: list[str] = field(default_factory=list)  # concepts the template shows off
    data: Callable[[random.Random], dict[str, list[dict]]] | None = None

    def table_defs(self, driver_key: str) -> list[TableDef]:
        types = TYPE_MAP.get(driver_key, TYPE_MAP["sqlite"])
        out = []
        for td in self.tables:
            t = td.clone()
            for c in t.columns:
                c.type = types.get(c.type, c.type)
            if driver_key == "mysql":
                t.options = {"engine": "InnoDB"}
            out.append(t)
        return out

    def relationships(self) -> list[tuple[str, str, str]]:
        return [(t.name, fk.columns[0], fk.ref_table) for t in self.tables for fk in t.foreign_keys]


def apply_template(session, tpl: Template, schema: str | None = None, with_data: bool = True, seed: int = 7) -> dict[str, int]:
    """Create the template's tables (parents first) and optionally fill them. Returns row counts."""
    from dbide.core import media
    from dbide.core.roles import role_store

    drv = session.driver
    existing = set(session.tables(schema))
    clash = [t.name for t in tpl.tables if t.name in existing]
    if clash:
        raise ValueError(f"These tables already exist: {', '.join(clash)}. Pick another database or rename them.")
    for td in tpl.table_defs(session.cfg.driver):
        td.schema = schema
        session.execute_ddl(drv.create_table_sql(td))
    for table, roles in tpl.roles.items():
        role_store().set_table(session.cfg.id, schema, table, roles)
    counts = {t.name: 0 for t in tpl.tables}
    if not with_data or tpl.data is None:
        return counts
    rng = random.Random(seed)
    data = tpl.data(rng)
    tmpdir = Path(tempfile.mkdtemp(prefix="dbide_tpl_"))
    for td in tpl.tables:  # parents first: tables are listed in dependency order
        rows = data.get(td.name, [])
        if not rows:
            continue
        roles = tpl.roles.get(td.name, {})
        cols = list(rows[0].keys())
        values = []
        for r in rows:
            row = []
            for c in cols:
                v = r[c]
                role = roles.get(c)
                if isinstance(v, bytes) and role is not None and role.kind == "media" and role.storage == "path":
                    f = tmpdir / f"{td.name}_{r.get('id', len(values))}.png"
                    f.write_bytes(v)
                    v = media.store_file(str(f), session.cfg)  # real files in the media folder
                row.append(v)
            values.append(row)
        session.insert_rows(td.name, schema, cols, values)
        counts[td.name] = len(values)
    return counts


# --------------------------------------------------------------------------------
# sample-data vocab

FIRST = ["Ada", "Grace", "Alan", "Linus", "Margaret", "Katherine", "Tim", "Barbara", "Dennis", "Radia", "Ken", "Frances",
         "Guido", "Hedy", "John", "Anita", "Bjarne", "Sophie", "Yukihiro", "Shafi", "Leslie", "Fei-Fei", "Donald", "Joan"]
LAST = ["Lovelace", "Hopper", "Turing", "Torvalds", "Hamilton", "Johnson", "Berners-Lee", "Liskov", "Ritchie", "Perlman",
        "Thompson", "Allen", "van Rossum", "Lamarr", "McCarthy", "Borg", "Stroustrup", "Wilson", "Matsumoto", "Goldwasser"]
COUNTRIES = ["UK", "USA", "Canada", "Germany", "France", "Japan", "Brazil", "India", "Kenya", "Australia"]
PALETTE = [(42, 120, 214), (235, 104, 52), (27, 175, 122), (237, 161, 0), (232, 123, 164), (98, 80, 214), (227, 73, 72)]


def _day(rng, start=dt.date(2023, 1, 1), span=700) -> str:
    return (start + dt.timedelta(days=rng.randrange(span))).isoformat()


def _when(rng, start=dt.date(2023, 1, 1), span=700) -> str:
    d = dt.datetime.combine(start, dt.time()) + dt.timedelta(days=rng.randrange(span), minutes=rng.randrange(1440))
    return d.strftime("%Y-%m-%d %H:%M:%S")


def _people(rng, n):
    seen, out = set(), []
    while len(out) < n:
        name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _email(name: str, i: int) -> str:
    return name.lower().replace(" ", ".").replace("-", "") + f"{i}@example.com"


# --------------------------------------------------------------------------------
# the templates


def _store_data(rng):
    names = _people(rng, 40)
    customers = [{"id": i + 1, "name": n, "email": _email(n, i), "country": rng.choice(COUNTRIES), "joined_on": _day(rng)}
                 for i, n in enumerate(names)]
    catalog = [("Mechanical Keyboard", "hardware", 89), ("Wireless Mouse", "hardware", 25), ("USB-C Cable", "hardware", 9),
               ("27in Monitor", "hardware", 249), ("Laptop Stand", "hardware", 39), ("Noise-cancelling Headphones", "audio", 199),
               ("Desk Microphone", "audio", 79), ("Webcam HD", "hardware", 59), ("SQL Pocket Guide", "books", 19),
               ("Database Design Book", "books", 45), ("Python Crash Course", "books", 35), ("IDE Pro License", "software", 120),
               ("Cloud Backup (1 yr)", "software", 60), ("VPN (1 yr)", "software", 48), ("Standing Desk", "furniture", 399),
               ("Ergonomic Chair", "furniture", 299), ("Desk Lamp", "furniture", 29), ("Sticker Pack", "merch", 5),
               ("Hoodie", "merch", 49), ("Coffee Mug", "merch", 12), ("Smart Speaker", "audio", 99), ("Phone Charger", "hardware", 19),
               ("Mouse Pad XL", "hardware", 15), ("Notebook", "merch", 8), ("Graphics Tablet", "hardware", 179)]
    products = [{"id": i + 1, "title": t, "category": c, "price": float(p), "stock": rng.choice([0, 2, 4, 8, 15, 30, 60]),
                 "image": color_png(PALETTE[i % len(PALETTE)])} for i, (t, c, p) in enumerate(catalog)]
    orders, items = [], []
    for oid in range(1, 121):
        cust = rng.randrange(1, 36)  # the last few customers never order
        picks = rng.sample(products, rng.randint(1, 4))
        lines = [(p, rng.randint(1, 3)) for p in picks]
        total = round(sum(p["price"] * q for p, q in lines), 2)
        orders.append({"id": oid, "customer_id": cust, "status": rng.choice(["paid", "paid", "shipped", "delivered", "cancelled"]),
                       "ordered_at": _when(rng), "total": total})
        for p, q in lines:
            items.append({"id": len(items) + 1, "order_id": oid, "product_id": p["id"], "quantity": q, "unit_price": p["price"]})
    return {"customers": customers, "products": products, "orders": orders, "order_items": items}


STORE = Template(
    "store", "Online Store", "ph.shopping-cart",
    "Customers, products with photos, orders and order lines. The classic one-to-many and many-to-many example.",
    [
        TableDef("customers", columns=[_pk(), _c("name", required=True), _c("email", required=True), _c("country"), _c("joined_on", "date")],
                 indexes=[IndexDef("ux_customers_email", ["email"], True)]),
        TableDef("products", columns=[_pk(), _c("title", required=True), _c("category"), _c("price", "money", True),
                                      _c("stock", "int", True, "0"), _c("image", "path")]),
        TableDef("orders", columns=[_pk(), _c("customer_id", "int", True), _c("status", required=True, default="'paid'"),
                                    _c("ordered_at", "datetime"), _c("total", "money")],
                 foreign_keys=[_fk("customer_id", "customers")], indexes=[IndexDef("ix_orders_customer", ["customer_id"])]),
        TableDef("order_items", columns=[_pk(), _c("order_id", "int", True), _c("product_id", "int", True), _c("quantity", "int", True, "1"),
                                         _c("unit_price", "money", True)],
                 foreign_keys=[_fk("order_id", "orders", "CASCADE"), _fk("product_id", "products")]),
    ],
    roles={"products": {"image": ColumnRole("media", storage="path", media_kind="image")}},
    try_this=[
        TryThis("customers in UK", note="filter rows with a value"),
        TryThis("top 5 most expensive products", note="sort and limit"),
        TryThis("total of orders per customer", note="join + group + sum"),
        TryThis("orders by status", note="count per group"),
        TryThis("customers without orders", note="find missing matches"),
        TryThis("products with stock under 5", note="compare numbers"),
        TryThis("SELECT c.name, COUNT(o.id) AS orders\nFROM customers c\nLEFT JOIN orders o ON o.customer_id = c.id\nGROUP BY c.id\nORDER BY orders DESC;", True, "LEFT JOIN + GROUP BY"),
        TryThis("UPDATE products SET price = price * 0.9 WHERE category = 'books';", True, "change data (then Undo!)"),
    ],
    learn=["One-to-many: a customer has many orders", "Many-to-many through order_items", "Images stored as files on disk"],
    data=_store_data,
)


def _blog_data(rng):
    from dbide.core.crypto import hash_password

    names = _people(rng, 10)
    users = [{"id": i + 1, "username": n.split()[0].lower() + str(i), "email": _email(n, i),
              "password": hash_password(f"password{i}", "argon2id"), "joined_at": _when(rng)} for i, n in enumerate(names)]
    topics = ["Why indexes matter", "JOINs explained", "Normalization in plain English", "My first database", "SQLite tips",
              "MySQL vs SQLite", "Designing a schema", "Hashing passwords properly", "Backups you can trust", "Query plans 101",
              "When to use a view", "Transactions and you", "Foreign keys save the day", "CSV imports", "Charts from SQL"]
    posts = [{"id": i + 1, "user_id": rng.randint(1, 8), "title": t, "body": f"{t}. " + "Lorem ipsum dolor sit amet. " * rng.randint(2, 6),
              "status": rng.choice(["published", "published", "draft"]), "published_at": _when(rng)} for i, t in enumerate(topics)]
    tags = [{"id": i + 1, "name": n} for i, n in enumerate(["sql", "design", "security", "beginner", "performance", "sqlite", "mysql"])]
    post_tags = []
    for p in posts:
        for t in rng.sample(tags, rng.randint(1, 3)):
            post_tags.append({"post_id": p["id"], "tag_id": t["id"]})
    comments = [{"id": i + 1, "post_id": rng.randint(1, len(posts) - 2), "user_id": rng.randint(1, 10),
                 "body": rng.choice(["Great post!", "Thanks, this helped.", "Could you explain more?", "Bookmarked.", "I disagree a little."]),
                 "created_at": _when(rng)} for i in range(45)]
    return {"users": users, "posts": posts, "tags": tags, "post_tags": post_tags, "comments": comments}


BLOG = Template(
    "blog", "Blog", "ph.article" ,
    "Users (with safely hashed passwords), posts, comments and tags. Shows a junction table and why passwords are never stored as text.",
    [
        TableDef("users", columns=[_pk(), _c("username", required=True), _c("email", required=True), _c("password", required=True),
                                   _c("joined_at", "datetime")],
                 indexes=[IndexDef("ux_users_username", ["username"], True)]),
        TableDef("posts", columns=[_pk(), _c("user_id", "int", True), _c("title", required=True), _c("body", "long"),
                                   _c("status", required=True, default="'draft'"), _c("published_at", "datetime")],
                 foreign_keys=[_fk("user_id", "users", "CASCADE")]),
        TableDef("tags", columns=[_pk(), _c("name", required=True)], indexes=[IndexDef("ux_tags_name", ["name"], True)]),
        TableDef("post_tags", columns=[ColumnDef("post_id", "int", nullable=False, primary_key=True),
                                       ColumnDef("tag_id", "int", nullable=False, primary_key=True)],
                 foreign_keys=[_fk("post_id", "posts", "CASCADE"), _fk("tag_id", "tags", "CASCADE")]),
        TableDef("comments", columns=[_pk(), _c("post_id", "int", True), _c("user_id", "int", True), _c("body", "long", True),
                                      _c("created_at", "datetime")],
                 foreign_keys=[_fk("post_id", "posts", "CASCADE"), _fk("user_id", "users")]),
    ],
    roles={"users": {"password": ColumnRole("password", "argon2id")}},
    try_this=[
        TryThis("published posts", note="filter by a value"),
        TryThis("count posts per user", note="group along a foreign key"),
        TryThis("posts without comments", note="anti-join"),
        TryThis("posts with more than 3 comments", note="count related rows"),
        TryThis("SELECT p.title, t.name\nFROM posts p\nJOIN post_tags pt ON pt.post_id = p.id\nJOIN tags t ON t.id = pt.tag_id\nORDER BY p.title;", True, "many-to-many through a junction table"),
        TryThis("SELECT username, password FROM users LIMIT 3;", True, "see what a password hash looks like"),
    ],
    learn=["Passwords stored as Argon2id hashes", "Many-to-many: posts ↔ tags", "ON DELETE CASCADE removes a post's comments"],
    data=_blog_data,
)


def _school_data(rng):
    teachers = [{"id": i + 1, "name": n, "subject": s} for i, (n, s) in enumerate(
        zip(_people(rng, 5), ["Math", "Science", "History", "Art", "Computing"]))]
    courses = [{"id": i + 1, "title": t, "teacher_id": tid, "credits": c} for i, (t, tid, c) in enumerate(
        [("Algebra", 1, 4), ("Geometry", 1, 3), ("Biology", 2, 4), ("Chemistry", 2, 4), ("World History", 3, 3),
         ("Painting", 4, 2), ("Intro to Databases", 5, 3), ("Programming", 5, 4)])]
    students = [{"id": i + 1, "name": n, "grade_level": rng.randint(9, 12), "email": _email(n, i)} for i, n in enumerate(_people(rng, 30))]
    enroll = []
    for s in students[:-3]:  # three students haven't enrolled yet
        for c in rng.sample(courses, rng.randint(2, 4)):
            enroll.append({"student_id": s["id"], "course_id": c["id"], "grade": round(rng.uniform(55, 100), 1)})
    return {"teachers": teachers, "courses": courses, "students": students, "enrollments": enroll}


SCHOOL = Template(
    "school", "School", "ph.student",
    "Students, teachers, courses and enrollments with grades. A textbook many-to-many relationship with extra data on the link.",
    [
        TableDef("teachers", columns=[_pk(), _c("name", required=True), _c("subject")]),
        TableDef("courses", columns=[_pk(), _c("title", required=True), _c("teacher_id", "int"), _c("credits", "int", True, "3")],
                 foreign_keys=[_fk("teacher_id", "teachers", "SET NULL")]),
        TableDef("students", columns=[_pk(), _c("name", required=True), _c("grade_level", "int"), _c("email")]),
        TableDef("enrollments", columns=[ColumnDef("student_id", "int", nullable=False, primary_key=True),
                                         ColumnDef("course_id", "int", nullable=False, primary_key=True), _c("grade", "real")],
                 foreign_keys=[_fk("student_id", "students", "CASCADE"), _fk("course_id", "courses", "CASCADE")]),
    ],
    try_this=[
        TryThis("average grade per course", note="group + average"),
        TryThis("students without enrollments", note="who hasn't signed up"),
        TryThis("students with more than 3 enrollments", note="count related rows"),
        TryThis("courses by credits", note="group by a column"),
        TryThis("SELECT s.name, c.title, e.grade\nFROM enrollments e\nJOIN students s ON s.id = e.student_id\nJOIN courses c ON c.id = e.course_id\nWHERE e.grade >= 90\nORDER BY e.grade DESC;", True, "two JOINs through a junction table"),
    ],
    learn=["Junction table with its own data (grade)", "Composite primary key", "SET NULL when a teacher leaves"],
    data=_school_data,
)


def _library_data(rng):
    authors = [{"id": i + 1, "name": n, "country": c} for i, (n, c) in enumerate(
        [("Jane Austen", "UK"), ("Toni Morrison", "USA"), ("Chinua Achebe", "Nigeria"), ("Ursula K. Le Guin", "USA"),
         ("Haruki Murakami", "Japan"), ("Gabriel García Márquez", "Colombia"), ("Chimamanda Ngozi Adichie", "Nigeria"), ("Italo Calvino", "Italy")])]
    titles = [("Pride and Prejudice", 1, 1813, "classic"), ("Emma", 1, 1815, "classic"), ("Beloved", 2, 1987, "fiction"),
              ("Song of Solomon", 2, 1977, "fiction"), ("Things Fall Apart", 3, 1958, "classic"), ("A Wizard of Earthsea", 4, 1968, "fantasy"),
              ("The Left Hand of Darkness", 4, 1969, "sci-fi"), ("Kafka on the Shore", 5, 2002, "fiction"), ("Norwegian Wood", 5, 1987, "fiction"),
              ("One Hundred Years of Solitude", 6, 1967, "classic"), ("Half of a Yellow Sun", 7, 2006, "fiction"), ("Invisible Cities", 8, 1972, "fiction")]
    books = [{"id": i + 1, "title": t, "author_id": a, "year": y, "genre": g, "cover": color_png(PALETTE[i % len(PALETTE)], 60, 90)}
             for i, (t, a, y, g) in enumerate(titles)]
    members = [{"id": i + 1, "name": n, "joined_on": _day(rng)} for i, n in enumerate(_people(rng, 15))]
    loans = []
    for i in range(40):
        out = dt.date(2024, 1, 1) + dt.timedelta(days=rng.randrange(300))
        back = (out + dt.timedelta(days=rng.randint(5, 30))).isoformat() if rng.random() < 0.8 else None
        loans.append({"id": i + 1, "book_id": rng.randint(1, len(books) - 1), "member_id": rng.randint(1, 13),
                      "loaned_on": out.isoformat(), "returned_on": back})
    return {"authors": authors, "books": books, "members": members, "loans": loans}


LIBRARY = Template(
    "library", "Library", "ph.books",
    "Authors, books with cover images stored in the database, members and loans. Great for dates and 'not returned yet' (NULL) questions.",
    [
        TableDef("authors", columns=[_pk(), _c("name", required=True), _c("country")]),
        TableDef("books", columns=[_pk(), _c("title", required=True), _c("author_id", "int", True), _c("year", "int"), _c("genre"), _c("cover", "blob")],
                 foreign_keys=[_fk("author_id", "authors")]),
        TableDef("members", columns=[_pk(), _c("name", required=True), _c("joined_on", "date")]),
        TableDef("loans", columns=[_pk(), _c("book_id", "int", True), _c("member_id", "int", True), _c("loaned_on", "date", True), _c("returned_on", "date")],
                 foreign_keys=[_fk("book_id", "books"), _fk("member_id", "members", "CASCADE")]),
    ],
    roles={"books": {"cover": ColumnRole("media", storage="blob", media_kind="image")}},
    try_this=[
        TryThis("books by Jane Austen", note="filter through a join"),
        TryThis("loans where returned on is empty", note="NULL means not returned"),
        TryThis("count books per author", note="group along a foreign key"),
        TryThis("books never loaned", note="anti-join"),
        TryThis("members with more than 3 loans", note="count related rows"),
        TryThis("books in 1987", note="filter by year"),
    ],
    learn=["NULL as 'not yet'", "Images stored inside the database (BLOB)", "Dates and ranges"],
    data=_library_data,
)


def _tasks_data(rng):
    people = [{"id": i + 1, "name": n} for i, n in enumerate(_people(rng, 6))]
    projects = [{"id": i + 1, "name": n, "due_on": _day(rng, dt.date(2025, 1, 1), 200)} for i, n in enumerate(
        ["Website relaunch", "Mobile app", "Data migration", "Onboarding docs"])]
    verbs = ["Design", "Build", "Test", "Review", "Write", "Fix", "Plan", "Ship"]
    things = ["login page", "API", "schema", "backups", "search", "dashboard", "emails", "invoices", "help text", "reports"]
    tasks = [{"id": i + 1, "project_id": rng.randint(1, 4), "assignee_id": rng.choice([None] + [p["id"] for p in people[:-1]]),  # the last person is free
              "title": f"{rng.choice(verbs)} {rng.choice(things)}", "status": rng.choice(["todo", "doing", "done", "done"]),
              "priority": rng.randint(1, 3), "due_on": _day(rng, dt.date(2025, 1, 1), 200)} for i in range(50)]
    return {"projects": projects, "people": people, "tasks": tasks}


TASKS = Template(
    "tasks", "Task Tracker", "ph.check-square",
    "Projects, people and tasks with status, priority and due dates. Tasks can be unassigned (NULL).",
    [
        TableDef("projects", columns=[_pk(), _c("name", required=True), _c("due_on", "date")]),
        TableDef("people", columns=[_pk(), _c("name", required=True)]),
        TableDef("tasks", columns=[_pk(), _c("project_id", "int", True), _c("assignee_id", "int"), _c("title", required=True),
                                   _c("status", required=True, default="'todo'"), _c("priority", "int", True, "2"), _c("due_on", "date")],
                 foreign_keys=[_fk("project_id", "projects", "CASCADE"), _fk("assignee_id", "people", "SET NULL")]),
    ],
    try_this=[
        TryThis("tasks by status", note="count per group"),
        TryThis("tasks that are not done", note="!= filter"),
        TryThis("count tasks per person", note="group along a foreign key"),
        TryThis("tasks where assignee id is empty", note="unassigned = NULL"),
        TryThis("people without tasks", note="anti-join"),
    ],
    learn=["Optional relationship (NULL assignee)", "ON DELETE CASCADE vs SET NULL"],
    data=_tasks_data,
)


def _recipes_data(rng):
    ingredients = [{"id": i + 1, "name": n} for i, n in enumerate(
        ["flour", "sugar", "egg", "butter", "milk", "salt", "tomato", "basil", "garlic", "olive oil", "pasta", "rice", "chicken", "lemon", "chili"])]
    recipes = [{"id": i + 1, "name": n, "cuisine": c, "minutes": m, "photo": color_png(PALETTE[i % len(PALETTE)])} for i, (n, c, m) in enumerate(
        [("Pancakes", "American", 20), ("Tomato Pasta", "Italian", 25), ("Lemon Chicken", "Greek", 45), ("Fried Rice", "Chinese", 20),
         ("Garlic Bread", "Italian", 15), ("Chili Rice Bowl", "Mexican", 30), ("Butter Cookies", "Danish", 35), ("Bruschetta", "Italian", 10)])]
    link = []
    for r in recipes:
        for ing in rng.sample(ingredients, rng.randint(3, 6)):
            link.append({"recipe_id": r["id"], "ingredient_id": ing["id"], "amount": rng.choice(["1 cup", "2 tbsp", "a pinch", "200 g", "3", "to taste"])})
    return {"recipes": recipes, "ingredients": ingredients, "recipe_ingredients": link}


RECIPES = Template(
    "recipes", "Recipes", "ph.cooking-pot",
    "Recipes with photos, ingredients and amounts. A friendly many-to-many example.",
    [
        TableDef("recipes", columns=[_pk(), _c("name", required=True), _c("cuisine"), _c("minutes", "int"), _c("photo", "path")]),
        TableDef("ingredients", columns=[_pk(), _c("name", required=True)], indexes=[IndexDef("ux_ingredients_name", ["name"], True)]),
        TableDef("recipe_ingredients", columns=[ColumnDef("recipe_id", "int", nullable=False, primary_key=True),
                                                ColumnDef("ingredient_id", "int", nullable=False, primary_key=True), _c("amount")],
                 foreign_keys=[_fk("recipe_id", "recipes", "CASCADE"), _fk("ingredient_id", "ingredients")]),
    ],
    roles={"recipes": {"photo": ColumnRole("media", storage="path", media_kind="image")}},
    try_this=[
        TryThis("recipes in Italian", note="filter"),
        TryThis("recipes under 30", note="compare numbers"),
        TryThis("count recipes by cuisine", note="group"),
        TryThis("SELECT r.name, i.name AS ingredient, ri.amount\nFROM recipes r\nJOIN recipe_ingredients ri ON ri.recipe_id = r.id\nJOIN ingredients i ON i.id = ri.ingredient_id\nWHERE r.name = 'Pancakes';", True, "walk a many-to-many"),
    ],
    learn=["Many-to-many with an amount on the link", "Photos stored as files"],
    data=_recipes_data,
)

TEMPLATES: dict[str, Template] = {t.key: t for t in (STORE, BLOG, SCHOOL, LIBRARY, TASKS, RECIPES)}
