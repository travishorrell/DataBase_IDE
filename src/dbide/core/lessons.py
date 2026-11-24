"""Guided lessons on a practice SQLite database. Each step can check the learner's work."""
from __future__ import annotations

import re
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from sqlalchemy import inspect

from dbide.core import crypto
from dbide.core.config import app_dir

PRACTICE_ID = "practice-lessons"
PRACTICE_NAME = "Practice (Lessons)"


def practice_path() -> Path:
    d = app_dir() / "practice"
    d.mkdir(parents=True, exist_ok=True)
    return d / "practice.sqlite"


@dataclass
class Context:
    """What the learner has done recently: [(sql, succeeded), ...], newest first."""

    recent: list[tuple[str, bool]] = field(default_factory=list)

    def ran(self, pattern: str, ok: bool = True) -> bool:
        rx = re.compile(pattern, re.I | re.S)
        return any(rx.search(sql) and success == ok for sql, success in self.recent)


Check = Callable[["object", Context], tuple[bool, str]]


@dataclass
class Step:
    title: str
    body: str
    sql: str = ""  # example the learner can paste or run with "Do it for me"
    check: Check | None = None
    action: str = ""  # UI action offered by a button: open_erd, open_designer:<t>, open_data:<t>, visualize_join, password_tool
    action_label: str = ""
    roles: dict = field(default_factory=dict)  # "table.column" -> role code, applied by "Do it for me"
    sample_media: str = ""  # table name: "Do it for me" inserts a generated image into it


@dataclass
class Lesson:
    id: str
    title: str
    steps: list[Step]


# ---- check helpers ------------------------------------------------------------


def _tables(s) -> list[str]:
    return inspect(s.engine).get_table_names()


def _count(s, table: str) -> int:
    with s.engine.connect() as c:
        return int(c.exec_driver_sql(f'SELECT COUNT(*) FROM "{table}"').scalar() or 0)


def table_exists(name: str, pk: bool = True, not_null: tuple[str, ...] = ()) -> Check:
    def check(s, ctx):
        if name not in _tables(s):
            return False, f"There's no table called <b>{name}</b> yet. Run the CREATE TABLE statement (or use the designer)."
        insp = inspect(s.engine)
        if pk and not (insp.get_pk_constraint(name) or {}).get("constrained_columns"):
            return False, f"<b>{name}</b> exists but has no primary key. Mark the <code>id</code> column as PRIMARY KEY."
        cols = {c["name"]: c for c in insp.get_columns(name)}
        for col in not_null:
            if col not in cols:
                return False, f"<b>{name}</b> needs a column <code>{col}</code>."
            if cols[col]["nullable"]:
                return False, f"<code>{name}.{col}</code> should be NOT NULL (required)."
        return True, f"✅ Table <b>{name}</b> looks right."

    return check


def min_rows(table: str, n: int) -> Check:
    def check(s, ctx):
        if table not in _tables(s):
            return False, f"Create the <b>{table}</b> table first."
        c = _count(s, table)
        return (c >= n, f"✅ {table} has {c} rows." if c >= n else f"{table} has {c} row(s); add at least {n}.")

    return check


def has_fk(child: str, col: str, parent: str) -> Check:
    def check(s, ctx):
        if child not in _tables(s):
            return False, f"Create the <b>{child}</b> table first."
        for fk in inspect(s.engine).get_foreign_keys(child):
            if col in fk["constrained_columns"] and fk["referred_table"] == parent:
                return True, f"✅ <code>{child}.{col}</code> is a foreign key to <b>{parent}</b>. The database now guards the link."
        return False, f"<code>{child}.{col}</code> isn't a foreign key to <b>{parent}</b> yet (use <code>REFERENCES {parent}(id)</code>)."

    return check


def ran(pattern: str, ok: bool, success: str, hint: str) -> Check:
    def check(s, ctx):
        return (True, success) if ctx.ran(pattern, ok) else (False, hint)

    return check


def has_role(table: str, column: str, kind: str) -> Check:
    def check(s, ctx):
        from dbide.core.roles import role_store

        r = role_store().get(s.cfg.id, None, table).get(column)
        if not r or r.kind != kind:
            return False, f"Set the <b>Role</b> of <code>{table}.{column}</code> in Design Table (or click Do it for me)."
        return True, f"✅ <code>{table}.{column}</code> has the role {r.label()}."

    return check


def all_of(*checks: Check) -> Check:
    def check(s, ctx):
        msgs = []
        for c in checks:
            ok, msg = c(s, ctx)
            if not ok:
                return False, msg
            msgs.append(msg)
        return True, "<br>".join(msgs)

    return check


def hashed_rows(table: str, column: str) -> Check:
    def check(s, ctx):
        if table not in _tables(s):
            return False, f"Create the <b>{table}</b> table first."
        with s.engine.connect() as c:
            vals = [r[0] for r in c.exec_driver_sql(f'SELECT "{column}" FROM "{table}"').fetchall()]
        if not vals:
            return False, f"Add a row to <b>{table}</b> in the data grid and type a password into <code>{column}</code>, then Apply."
        if all(crypto.detect_hash(v) for v in vals if v):
            algo = crypto.ALGORITHMS[crypto.detect_hash(next(v for v in vals if v))].label
            return True, f"✅ Every password is stored as a {algo} hash. Nobody reading the table can see the real passwords."
        return False, "Some passwords are stored as plain text. Delete them and re-enter them in the grid so they get hashed."

    return check


def media_rows(table: str) -> Check:
    def check(s, ctx):
        if table not in _tables(s):
            return False, f"Create the <b>{table}</b> table first."
        with s.engine.connect() as c:
            n = c.exec_driver_sql(
                f'SELECT COUNT(*) FROM "{table}" WHERE image IS NOT NULL OR image_path IS NOT NULL').scalar()
        if n:
            return True, f"✅ {n} row(s) have media. Open the diagram to see where each file is stored."
        return False, "Right-click an <code>image</code> or <code>image_path</code> cell in the grid → <b>Insert Image/Video…</b>, then Apply."

    return check


# ---- a tiny image for the media lesson -------------------------------------------


def sample_png(w: int = 64, h: int = 64) -> bytes:
    """A small gradient PNG built without any imaging library."""
    rows = b""
    for y in range(h):
        rows += b"\x00" + b"".join(bytes((x * 4 % 256, y * 4 % 256, 200)) for x in range(w))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


# ---- the lessons -----------------------------------------------------------------

LESSONS: list[Lesson] = [
    Lesson("tables", "1. Your first table", [
        Step("What is a database?",
             "<p>A <b>database</b> stores information in <b>tables</b>. A table is like a spreadsheet with strict rules:</p>"
             "<ul><li>Each <b>column</b> has a name and a type (text, number, date…)</li>"
             "<li>Each <b>row</b> is one record, such as one author</li>"
             "<li>One column, the <b>primary key</b>, uniquely identifies each row</li></ul>"
             "<p>You'll build a small library database: <b>authors</b>, then <b>books</b> that point to authors.</p>"
             "<p>Everything happens in a practice database, so experiment freely. Click <b>Next</b>.</p>"),
        Step("Create the authors table",
             "<p>Run this in the SQL editor (put the cursor in it and press ⌘↩), or click <b>Open Designer</b> to build it visually.</p>"
             "<ul><li><code>id INTEGER PRIMARY KEY AUTOINCREMENT</code>: a unique number the database assigns</li>"
             "<li><code>name TEXT NOT NULL</code>: required text</li>"
             "<li><code>country TEXT</code>: optional text</li></ul>",
             sql="CREATE TABLE authors (\n  id INTEGER PRIMARY KEY AUTOINCREMENT,\n  name TEXT NOT NULL,\n  country TEXT\n);",
             check=table_exists("authors", not_null=("name",)), action="open_designer:", action_label="Open Designer"),
        Step("Add some rows",
             "<p><code>INSERT</code> adds rows. Notice we don't give an <code>id</code>: AUTOINCREMENT fills it in.</p>"
             "<p>You can also double-click <b>authors</b> in the sidebar, press <b>+ Row</b>, type, and <b>Apply</b>.</p>",
             sql="INSERT INTO authors (name, country) VALUES\n  ('Jane Austen', 'UK'),\n  ('Toni Morrison', 'USA'),\n  ('Chinua Achebe', 'Nigeria'),\n  ('Ursula K. Le Guin', 'USA');",
             check=min_rows("authors", 3), action="open_data:authors", action_label="Open in Grid"),
        Step("Ask questions with SELECT",
             "<p><code>SELECT</code> reads data and never changes it. <code>WHERE</code> filters rows; <code>ORDER BY</code> sorts.</p>"
             "<p>Try changing <code>'USA'</code> to another country.</p>",
             sql="SELECT name, country\nFROM authors\nWHERE country = 'USA'\nORDER BY name;",
             check=ran(r"SELECT.*FROM\s+\W?authors.*WHERE", True, "✅ You filtered rows with WHERE.",
                       "Run a SELECT on authors that includes a WHERE clause.")),
    ]),
    Lesson("keys", "2. Primary & foreign keys", [
        Step("Primary keys stop duplicates",
             "<p>The primary key must be unique. Try inserting an author with an <code>id</code> that already exists. "
             "<b>It's supposed to fail!</b> Read the error explanation in the Help panel.</p>",
             sql="INSERT INTO authors (id, name) VALUES (1, 'Duplicate Person');",
             check=ran(r"INSERT\s+INTO\s+\W?authors", False, "✅ The database refused the duplicate id. That's the primary key at work.",
                       "Run the INSERT. It should fail with a UNIQUE / PRIMARY KEY error.")),
        Step("Link tables with a foreign key",
             "<p>Each book is written by one author. Instead of typing the author's name into every book (and risking typos), "
             "store the author's <b>id</b>, which is a <b>foreign key</b>.</p>"
             "<p><code>author_id INTEGER NOT NULL REFERENCES authors(id)</code> means: this value must be an existing authors.id.</p>"
             "<p>This is a <b>one-to-many</b> relationship: one author → many books.</p>",
             sql="CREATE TABLE books (\n  id INTEGER PRIMARY KEY AUTOINCREMENT,\n  title TEXT NOT NULL,\n  year INTEGER,\n"
                 "  author_id INTEGER NOT NULL REFERENCES authors(id)\n);",
             check=all_of(table_exists("books", not_null=("title",)), has_fk("books", "author_id", "authors"))),
        Step("Add books",
             "<p>Each book's <code>author_id</code> refers to an author's <code>id</code>. "
             "Check the ids in the authors grid if you changed them.</p>",
             sql="INSERT INTO books (title, year, author_id) VALUES\n  ('Pride and Prejudice', 1813, 1),\n  ('Emma', 1815, 1),\n"
                 "  ('Beloved', 1987, 2),\n  ('Things Fall Apart', 1958, 3);",
             check=min_rows("books", 3)),
        Step("Foreign keys protect you",
             "<p>Try adding a book by author <b>999</b>, who doesn't exist. The foreign key blocks it, so you can never have a book pointing to nobody.</p>",
             sql="INSERT INTO books (title, author_id) VALUES ('Ghost Book', 999);",
             check=ran(r"INSERT\s+INTO\s+\W?books", False, "✅ Blocked by the foreign key. Your data stays consistent.",
                       "Run the INSERT with author_id 999. It should fail.")),
        Step("See it drawn",
             "<p>Open the <b>diagram</b>. Each table is a box; <b>PK</b> and <b>FK</b> mark the keys. The line runs from "
             "<code>books.author_id</code> to <code>authors.id</code>: the forked end (crow's foot) is the <i>many</i> side.</p>"
             "<p>Hover over the line for an explanation, or double-click it to write a JOIN.</p>",
             action="open_erd", action_label="Open Diagram"),
    ]),
    Lesson("joins", "3. Combining tables with JOIN", [
        Step("INNER JOIN",
             "<p>A JOIN follows the foreign key at query time: for each book, find the author whose <code>id</code> equals the book's <code>author_id</code>.</p>"
             "<p>Run it, then click <b>Visualize JOIN</b> to see the matching rows and the Venn diagram.</p>",
             sql="SELECT b.title, b.year, a.name AS author\nFROM books b\nJOIN authors a ON a.id = b.author_id\nORDER BY b.year;",
             check=ran(r"JOIN\s+\W?authors|JOIN\s+\W?books", True, "✅ You joined books to authors.",
                       "Run a SELECT that JOINs books and authors."),
             action="visualize_join", action_label="Visualize JOIN"),
        Step("LEFT JOIN finds what's missing",
             "<p>An INNER JOIN hides authors who have no books. A <b>LEFT JOIN</b> keeps every author and fills the book columns with NULL. "
             "Filtering on <code>IS NULL</code> shows <i>only</i> the authors without books.</p>",
             sql="SELECT a.name, b.title\nFROM authors a\nLEFT JOIN books b ON b.author_id = a.id\nWHERE b.id IS NULL;",
             check=ran(r"LEFT\s+(OUTER\s+)?JOIN", True, "✅ LEFT JOIN kept the authors with no books.",
                       "Run a query with LEFT JOIN."),
             action="visualize_join", action_label="Visualize JOIN"),
        Step("Count per group",
             "<p><code>GROUP BY</code> makes one row per author, and <code>COUNT</code> counts their books. "
             "Using LEFT JOIN means authors with zero books still appear.</p>",
             sql="SELECT a.name, COUNT(b.id) AS books\nFROM authors a\nLEFT JOIN books b ON b.author_id = a.id\nGROUP BY a.id, a.name\nORDER BY books DESC;",
             check=ran(r"GROUP\s+BY", True, "✅ You grouped and counted.", "Run a query with GROUP BY.")),
    ]),
    Lesson("passwords", "4. Storing passwords safely", [
        Step("Never store plain passwords",
             "<p>If a database leaks and passwords are plain text, every account is compromised. Instead store a <b>hash</b>: "
             "a one-way fingerprint. At login, hash what the user typed and compare.</p>"
             "<p>Good password hashes are <b>slow on purpose</b> and <b>salted</b>: Argon2id, bcrypt, scrypt, PBKDF2. "
             "Plain SHA-256 or MD5 are <b>not</b> safe for passwords; they're too fast to guess against.</p>"),
        Step("Create users with a password-hash column",
             "<p>Create the table, then give <code>password_hash</code> the role <b>🔑 Password hash (Argon2id)</b> in "
             "<b>Design Table → Role</b>. <b>Do it for me</b> does both.</p>"
             "<p>With that role, anything you type in the grid is hashed before it is saved.</p>",
             sql="CREATE TABLE users (\n  id INTEGER PRIMARY KEY AUTOINCREMENT,\n  username TEXT NOT NULL UNIQUE,\n  password_hash TEXT NOT NULL\n);",
             check=all_of(table_exists("users", not_null=("username", "password_hash")), has_role("users", "password_hash", "password")),
             roles={"users.password_hash": "password:argon2id"}, action="open_designer:users", action_label="Open Designer"),
        Step("Add a user",
             "<p>Open users in the grid, click <b>+ Row</b>, type a username and a password such as <code>hunter2</code>, then <b>Apply</b>. "
             "After it reloads you'll see <code>$argon2id$…</code> instead of your password.</p>",
             check=hashed_rows("users", "password_hash"), action="open_data:users", action_label="Open in Grid"),
        Step("Check a login",
             "<p>Open the <b>Password & Encryption Tool</b>, paste the stored hash into <i>Check a password</i>, and try the right and a wrong password. "
             "This is what your app does at login.</p>"
             "<p>Need to read a value back later (an API token, say)? Use an <b>🔒 Encrypted</b> role instead: AES-256-GCM with a key "
             "kept in your secret store. Export that key, because losing it means losing the data.</p>",
             action="password_tool", action_label="Open Tool"),
    ]),
    Lesson("media", "5. Images & videos", [
        Step("Two ways to store a file",
             "<p><b>Inside the database (BLOB)</b>: the file's bytes go into the row. One file to back up; but the database grows fast.</p>"
             "<p><b>As a file path</b>: the file is copied into a media folder and the row stores its path. The database stays small; "
             "but the folder must move with the database or the links break.</p>"
             "<p>We'll do both so you can compare them in the diagram.</p>"),
        Step("Create a photos table",
             "<p><code>image</code> is a BLOB (role 🖼 Image in database); <code>image_path</code> is TEXT (role 📁 Image file path). "
             "<code>author_id</code> links each photo to an author.</p>",
             sql="CREATE TABLE photos (\n  id INTEGER PRIMARY KEY AUTOINCREMENT,\n  author_id INTEGER REFERENCES authors(id),\n"
                 "  caption TEXT,\n  image BLOB,\n  image_path TEXT\n);",
             check=all_of(table_exists("photos"), has_role("photos", "image", "media"), has_role("photos", "image_path", "media")),
             roles={"photos.image": "media:blob:image", "photos.image_path": "media:path:image"}),
        Step("Insert an image",
             "<p>Open photos in the grid, add a row, right-click the <code>image</code> cell → <b>Insert Image/Video…</b> and pick a picture. "
             "Do the same for <code>image_path</code>, then <b>Apply</b>. Double-click a media cell to view it.</p>"
             "<p><b>Do it for me</b> inserts a generated sample picture both ways.</p>",
             check=media_rows("photos"), action="open_data:photos", action_label="Open in Grid", sample_media="photos"),
        Step("Where is each file?",
             "<p>Open the diagram. Dashed purple boxes show where each media column keeps its files:</p>"
             "<ul><li><code>image</code> → <b>inside the database file</b></li>"
             "<li><code>image_path</code> → <b>the practice_media folder</b> on disk, with counts of missing and orphaned files</li></ul>"
             "<p>Hover a box for details; double-click the folder to open it in Finder.</p>",
             action="open_erd", action_label="Open Diagram"),
    ]),
]


def lesson(lesson_id: str) -> Lesson:
    return next(lsn for lsn in LESSONS if lsn.id == lesson_id)
