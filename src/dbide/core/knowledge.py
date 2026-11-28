"""Plain-English explanations shown in the Help panel.

TOPICS maps a key to (title, html). Keys:
  kw:<KEYWORD>   SQL keywords, shown when the cursor sits on them
  cmd:<name>     buttons and menu commands, shown on hover
  field:<name>   table-designer fields
  concept:<name> ideas such as primary keys or hashing
"""
from __future__ import annotations

import re

T = dict[str, tuple[str, str]]


def _ex(sql: str) -> str:
    return f"<pre>{sql}</pre>"


KEYWORDS: T = {
    "SELECT": ("SELECT: read data", "Chooses which <b>columns</b> to return. <code>*</code> means all columns."
               + _ex("SELECT name, email FROM customers;")
               + "Selecting never changes data, so it's always safe to experiment with."),
    "FROM": ("FROM: which table", "Names the table (or tables) the rows come from."
             + _ex("SELECT * FROM orders;")),
    "WHERE": ("WHERE: filter rows", "Keeps only the rows where the condition is true. Works with SELECT, UPDATE and DELETE."
              + _ex("SELECT * FROM orders WHERE total > 100 AND status = 'paid';")
              + "Text goes in 'single quotes'. Use <code>IS NULL</code>, not <code>= NULL</code>, to find empty values."),
    "JOIN": ("JOIN: combine tables", "Puts rows from two tables side by side wherever the <code>ON</code> condition matches, "
             "usually a foreign key matching a primary key."
             + _ex("SELECT o.id, c.name\nFROM orders o\nJOIN customers c ON c.id = o.customer_id;")
             + "Plain <code>JOIN</code> means <b>INNER JOIN</b>: rows without a match on either side are left out. "
               "Click <b>Visualize JOIN</b> to see which rows match."),
    "INNER": ("INNER JOIN: only matches", "Returns only rows that have a partner in <b>both</b> tables. Same as plain <code>JOIN</code>."),
    "LEFT": ("LEFT JOIN: keep everything on the left", "Returns <b>every</b> row from the left (first) table. Where the right table has no match, its columns come back as <code>NULL</code>."
             + _ex("-- customers who never ordered:\nSELECT c.name FROM customers c\nLEFT JOIN orders o ON o.customer_id = c.id\nWHERE o.id IS NULL;")),
    "RIGHT": ("RIGHT JOIN: keep everything on the right", "The mirror of LEFT JOIN: every row from the right table is kept. Most people rewrite it as a LEFT JOIN with the tables swapped. SQLite supports it from version 3.39."),
    "FULL": ("FULL OUTER JOIN", "Keeps every row from <b>both</b> tables, matching where possible. MySQL doesn't support it; combine a LEFT JOIN and a RIGHT JOIN with UNION instead."),
    "CROSS": ("CROSS JOIN: every combination", "Pairs every row of one table with every row of the other: 10 × 10 rows gives 100. Rarely what you want by accident!"),
    "ON": ("ON: the join condition", "Says which columns must match for two rows to be joined, typically <code>child.parent_id = parent.id</code>."),
    "AS": ("AS: give a nickname", "Renames a column or table in this query only. <code>FROM customers AS c</code> lets you write <code>c.name</code>."),
    "GROUP": ("GROUP BY: one row per group", "Collapses rows that share a value so you can count or sum them."
              + _ex("SELECT status, COUNT(*) FROM orders GROUP BY status;")
              + "Every selected column must be either in GROUP BY or inside an aggregate such as COUNT, SUM, AVG, MIN or MAX."),
    "HAVING": ("HAVING: filter groups", "Like WHERE, but runs <i>after</i> GROUP BY, so it can use aggregates."
               + _ex("SELECT customer_id, COUNT(*) FROM orders\nGROUP BY customer_id HAVING COUNT(*) > 5;")),
    "ORDER": ("ORDER BY: sort", "Sorts the result. Add <code>DESC</code> for largest or newest first."
              + _ex("SELECT * FROM orders ORDER BY created_at DESC;")
              + "Without ORDER BY the database may return rows in any order."),
    "LIMIT": ("LIMIT: at most N rows", "Returns only the first N rows. Pair it with ORDER BY to get a 'top N'. <code>OFFSET</code> skips rows, which is useful for paging."),
    "DISTINCT": ("DISTINCT: remove duplicates", "Returns each different combination of values only once."),
    "INSERT": ("INSERT: add rows", "Adds new rows to a table."
               + _ex("INSERT INTO customers (name, email)\nVALUES ('Ada', 'ada@example.com');")
               + "Leave out an auto-increment id and the database picks the next one."),
    "INTO": ("INTO", "Used with INSERT to name the target table."),
    "VALUES": ("VALUES", "The list of values for an INSERT, in the same order as the columns you named."),
    "UPDATE": ("UPDATE: change rows", "Changes values in existing rows."
               + _ex("UPDATE orders SET status = 'shipped' WHERE id = 42;")
               + "⚠️ <b>Without WHERE, every row changes.</b> Run the WHERE part as a SELECT first to see which rows you'll hit."),
    "SET": ("SET", "In UPDATE, lists the columns to change and their new values."),
    "DELETE": ("DELETE: remove rows", "Removes rows from a table."
               + _ex("DELETE FROM orders WHERE id = 42;")
               + "⚠️ <b>Without WHERE, every row is deleted.</b> There is no undo unless you're inside a transaction."),
    "CREATE": ("CREATE: make something new", "Creates a table, index, view or database. The <b>Create Table</b> designer writes this for you and shows the SQL in its preview tab."),
    "TABLE": ("TABLE", "A table stores rows of data. Every row has the same columns, like a spreadsheet with strict rules."),
    "ALTER": ("ALTER TABLE: change a table", "Adds, renames or removes columns and constraints of an existing table. Use <b>Design Table</b> to do it visually."),
    "DROP": ("DROP: delete permanently", "⚠️ Deletes a whole table, view, index or database, <b>including all its data</b>."),
    "TRUNCATE": ("TRUNCATE: empty a table", "Deletes every row but keeps the table. It's faster than DELETE, and in MySQL it also resets AUTO_INCREMENT."),
    "INDEX": ("INDEX: faster lookups", "Works like a book's index: the database can find matching rows without reading the whole table. "
              "Add one to columns you often filter or join on. Each index slightly slows down writes."),
    "PRIMARY": ("PRIMARY KEY", "The column (or columns) that uniquely identify each row. It can't be NULL and can't repeat. See <i>Primary keys</i> in the concepts."),
    "KEY": ("KEY", "In MySQL, KEY is a synonym for INDEX. PRIMARY KEY and FOREIGN KEY are constraints."),
    "FOREIGN": ("FOREIGN KEY", "Links a column to the primary key of another table, and the database refuses values that don't exist there."
                + _ex("author_id INTEGER REFERENCES authors(id)")),
    "REFERENCES": ("REFERENCES", "Names the parent table and column a foreign key points to."),
    "UNIQUE": ("UNIQUE", "No two rows may have the same value in this column (NULLs are allowed). Good for emails and usernames."),
    "NOT": ("NOT", "Negates a condition: <code>NOT NULL</code>, <code>NOT IN (...)</code>, <code>NOT LIKE</code>."),
    "NULL": ("NULL: no value", "NULL means 'unknown' or 'missing'. It isn't zero or an empty string. Compare with <code>IS NULL</code> / <code>IS NOT NULL</code>."),
    "DEFAULT": ("DEFAULT", "The value used when an INSERT doesn't provide one, e.g. <code>DEFAULT 0</code> or <code>DEFAULT CURRENT_TIMESTAMP</code>."),
    "AUTOINCREMENT": ("AUTOINCREMENT (SQLite)", "Makes an INTEGER PRIMARY KEY never reuse ids of deleted rows. Without it SQLite still assigns ids automatically."),
    "AUTO_INCREMENT": ("AUTO_INCREMENT (MySQL)", "The database fills in the next number for this column on INSERT. Usually used for the <code>id</code> primary key."),
    "LIKE": ("LIKE: pattern match", "<code>%</code> matches any text and <code>_</code> matches one character. <code>name LIKE 'A%'</code> finds names starting with A."),
    "IN": ("IN: one of a list", "<code>status IN ('paid', 'shipped')</code> is shorter than several ORs."),
    "BETWEEN": ("BETWEEN", "An inclusive range: <code>total BETWEEN 10 AND 20</code>."),
    "AND": ("AND", "Both conditions must be true."),
    "OR": ("OR", "At least one condition must be true. Use parentheses when you mix it with AND."),
    "COUNT": ("COUNT()", "Counts rows. <code>COUNT(*)</code> counts all rows; <code>COUNT(col)</code> skips NULLs."),
    "SUM": ("SUM()", "Adds up numbers in a column or group."),
    "AVG": ("AVG()", "The average of numbers in a column or group."),
    "UNION": ("UNION", "Stacks the results of two SELECTs. UNION removes duplicates; UNION ALL keeps them."),
    "BEGIN": ("BEGIN: start a transaction", "Groups statements so they all succeed or all fail. End with <code>COMMIT</code> to keep the changes or <code>ROLLBACK</code> to undo them."),
    "COMMIT": ("COMMIT", "Saves everything done since BEGIN."),
    "ROLLBACK": ("ROLLBACK", "Undoes everything done since BEGIN."),
    "VIEW": ("VIEW: a saved query", "A view looks like a table but is really a stored SELECT that runs every time you use it."),
    "EXPLAIN": ("EXPLAIN: how the database runs a query", "Shows the plan: which indexes it uses and whether it scans whole tables. A 'SCAN' of a big table often means an index is missing."),
    "CASE": ("CASE: if/else", "<code>CASE WHEN total > 100 THEN 'big' ELSE 'small' END</code>"),
    "WITH": ("WITH: named sub-queries (CTE)", "Lets you name a query and use it like a table in the main query. Great for breaking up complex SQL."),
    "USE": ("USE (MySQL)", "Switches the current database for this tab, like picking it in the Database box."),
    "SHOW": ("SHOW (MySQL)", "Lists server objects: <code>SHOW TABLES</code>, <code>SHOW DATABASES</code>, <code>SHOW CREATE TABLE t</code>."),
    "PRAGMA": ("PRAGMA (SQLite)", "SQLite settings and introspection, e.g. <code>PRAGMA table_info(users)</code> or <code>PRAGMA foreign_keys = ON</code>."),
}

COMMANDS: T = {
    "cmd:run": ("Run (⌘↩)", "Runs the <b>selected text</b>. With nothing selected, it runs the statement your cursor is in (statements end with <code>;</code>)."),
    "cmd:run_all": ("Run All (⌘⇧↩ / F5)", "Runs every statement in the editor from top to bottom and stops at the first error."),
    "cmd:stop": ("Stop (⌘.)", "Asks the database to cancel the running query."),
    "cmd:explain": ("Explain (⌘E)", "Shows how the database will execute the statement (its <i>query plan</i>) without changing anything."),
    "cmd:explain_english": ("Explain in English", "Describes what the current statement will do in plain words, before you run it."),
    "cmd:visualize_join": ("Visualize JOIN", "Draws the tables in your JOIN, the columns that link them, and how many rows match or don't."),
    "cmd:max_rows": ("Max rows", "The most rows the result grid will show. The query still runs fully; extra rows just aren't displayed."),
    "cmd:connection": ("Connection", "Which database connection this tab runs against. Each query tab has its own session."),
    "cmd:database": ("Database (MySQL)", "The database (schema) that unqualified table names refer to. Same as running <code>USE name;</code>."),
    "cmd:refresh": ("Refresh", "Reloads the rows from the database. Staged changes you haven't applied are lost."),
    "cmd:add_row": ("+ Row", "Adds a blank row (green) at the bottom. Fill it in, then <b>Apply</b>."),
    "cmd:del_row": ("− Row", "Marks the selected rows for deletion (red). Click again to unmark. Nothing is deleted until you Apply."),
    "cmd:set_null": ("Set NULL", "Clears the selected cells to NULL, meaning 'no value'."),
    "cmd:apply": ("Apply (⌘S)", "Sends all staged changes to the database in one <b>transaction</b>: either all of them succeed or none do. "
                  "Password columns are hashed and encrypted columns are encrypted automatically."),
    "cmd:discard": ("Discard", "Throws away the staged changes. The database is untouched."),
    "cmd:export": ("Export", "Saves rows to CSV (spreadsheets), JSON (programs) or SQL INSERT statements (re-creating the data elsewhere)."),
    "cmd:filter": ("Filter (WHERE)", "Type a condition and press Enter, e.g. <code>status = 'paid'</code>. It's inserted after <code>WHERE</code> in the query."),
    "cmd:sort": ("Sort (ORDER BY)", "Type columns to sort by and press Enter, e.g. <code>created_at DESC</code>."),
    "cmd:insert_media": ("Insert Image/Video…", "Pick a file to store in this cell. Depending on the column's role it is saved <b>inside the database</b> (BLOB) or <b>copied into the media folder</b>, with its path saved in the row."),
    "cmd:view_media": ("View Media", "Shows the image or plays the video in this cell, and where it is physically stored."),
    "cmd:new_connection": ("New Connection (⌘N)", "Connects to a database. Pick SQLite for a single file on your computer, or MySQL/MariaDB for a database server."),
    "cmd:new_query": ("New Query (⌘T)", "Opens a SQL editor tab for the selected connection."),
    "cmd:create_table": ("Create Table", "Opens the visual table designer. You describe columns and keys and it writes the CREATE TABLE statement."),
    "cmd:history": ("History (⌘Y)", "Every statement you've run, with timing. Double-click one to reopen it."),
    "cmd:diagram": ("Diagram (ERD)", "Draws your tables as boxes with lines for foreign keys, and shows where media files are stored."),
    "cmd:lessons": ("Lessons", "Step-by-step tutorials on a practice database. They check your work as you go."),
    "cmd:beginner": ("Beginner mode", "Turns on extra explanations and safety checks, such as warning before UPDATE or DELETE without WHERE and explaining statements before they run."),
    "cmd:password_tool": ("Password & Encryption Tool", "Hash a password with bcrypt/Argon2id/PBKDF2/scrypt, check a password against a hash, or encrypt/decrypt a value with this connection's key."),
    "cmd:ask": ("Ask in plain English", "Type what you want, like <i>customers in UK with more than 2 orders</i>, and press Enter. "
                "The IDE writes the SQL (offline, using your table names and stored values), shows how it read each phrase, "
                "puts the SQL in the editor and runs it. Generated SQL only reads data. Edit it freely; it's a starting point."),
    "cmd:chart": ("Chart", "An automatic chart of the result: a single number becomes a stat tile, categories with a number become "
                  "ranked bars, dates become a line, two numbers become a scatter. Hover for exact values. "
                  "<b>Customize</b> lets you pick the chart type and columns. The Result tab always has the full table."),
    "cmd:designer": ("Schema Designer", "Build your database visually. <b>Add Table</b>, then drag from the ● handle on one table to another "
                     "to link them. You'll choose one-to-many, one-to-one or many-to-many and see what that means. Nothing changes in the "
                     "database until <b>Review &amp; Apply</b>, which lists every step in plain English with the exact SQL."),
    "cmd:playground": ("Playground", "A private copy of a sample database (an online store, blog, school…). Run anything, even DELETE or DROP TABLE: "
                       "every change is snapshotted first, so <b>Undo</b> brings it back and <b>Reset</b> starts fresh. Long queries stop after 5 seconds."),
    "cmd:undo": ("Undo", "Puts the playground database back exactly as it was before your last change. You can undo many steps."),
    "cmd:templates": ("Templates", "Ready-made projects with tables, relationships and realistic sample data. Open one in the Playground to explore safely, "
                      "or create it for real as a new SQLite file or inside a MySQL database."),
    "cmd:whats_this": ("Explain anything", "Click <b>Explain</b> (⇧F1), then click any button, field or panel and a short explanation pops up."),
    "cmd:progress": ("Your progress", "The IDE notices the skills you use (filtering, joins, creating tables…). Each level reveals more tools, so beginners "
                     "see a simpler screen. Turn on <b>Show all features</b> any time."),
    "cmd:check_rows": ("Checked rows", "Tick the box at the left of each row (or <b>Select all</b>) to work on many rows at once. "
                       "<b>Delete Checked</b> marks them for deletion; <b>Update Checked…</b> sets one column to the same value on all of them. "
                       "Like every grid edit, nothing is saved until you click <b>Apply</b>, and <b>Discard</b> cancels."),
    "cmd:mode": ("Learning vs Power user mode", "<b>Learning mode</b> teaches you to be a database admin: lessons, a safe playground, "
                 "explanations of SQL and database concepts, safety prompts, and tools that unlock as you learn. "
                 "<b>Power user mode</b> is for working admins: every tool at once, no prompts, and help about the software only. "
                 "Switch with View → Mode (⌃⌥1 / ⌃⌥2)."),
    "cmd:admin": ("Admin Console", "Everyday database administration: <b>Overview</b> (size, version, rows and indexes per table), "
                  "<b>Maintenance</b> (integrity check, Analyze, Vacuum/Optimize, Backup), <b>Activity</b> (running queries on a "
                  "MySQL server, with Stop), <b>Users</b> (accounts and GRANTs) and <b>Settings</b> (server variables / PRAGMAs). ⌘⇧A."),
    "cmd:appearance": ("Appearance", "Switch between light and dark mode, pick a theme and accent colour, and change fonts, density and zoom. ⌘⇧L toggles light/dark; ⌘+ and ⌘− zoom."),
    "cmd:security": ("Security Settings", "Choose where the IDE keeps its own secrets (saved logins and encryption keys): the system keychain, an encrypted vault file with a master password, or nowhere."),
    "cmd:design": ("Design Table", "Change columns, keys, indexes and column roles of an existing table."),
    "cmd:col_add": ("Add", "Adds a row to this list."),
    "cmd:builder_apply": ("Create / Apply", "Shows the exact SQL that will run and asks for confirmation before changing the database."),
}

FIELDS: T = {
    "field:name": ("Column name", "Use lowercase_with_underscores. Avoid spaces and SQL words like <code>order</code> or <code>group</code>."),
    "field:type": ("Data type", "What kind of value the column stores:<ul>"
                   "<li><b>INTEGER/INT</b>: whole numbers (ids, counts)</li>"
                   "<li><b>DECIMAL(10,2)</b>: exact money amounts</li>"
                   "<li><b>REAL/DOUBLE</b>: measurements (approximate)</li>"
                   "<li><b>TEXT/VARCHAR(n)</b>: text (n = max length in MySQL)</li>"
                   "<li><b>DATE/DATETIME</b>: points in time</li>"
                   "<li><b>BLOB</b>: raw bytes, e.g. images</li></ul>"),
    "field:notnull": ("Not Null", "Every row must have a value here. Tick it for required fields such as a name or an email."),
    "field:pk": ("Primary Key (PK)", "This column uniquely identifies each row, like a student number. Every table should have one; usually an <code>id</code> that auto-increments."),
    "field:ai": ("Auto Increment", "The database picks the next number automatically when you insert a row, so you never type ids yourself."),
    "field:default": ("Default", "The value used when an insert leaves this column out. Write it as SQL: <code>0</code>, <code>'active'</code> (quotes for text), <code>CURRENT_TIMESTAMP</code>."),
    "field:comment": ("Comment", "A note stored with the column (MySQL), to document what it means."),
    "field:role": ("Role", "Extra behaviour the IDE adds to this column:<ul>"
                   "<li><b>🔑 Password hash</b>: values are hashed before saving and can never be read back</li>"
                   "<li><b>🔒 Encrypted</b>: values are encrypted with a key and can be decrypted</li>"
                   "<li><b>🖼 Media in database</b>: an image/video stored as bytes in the row</li>"
                   "<li><b>📁 Media file path</b>: the file is stored in a folder and the row keeps its path</li></ul>"),
    "field:index": ("Indexes", "Speed up searches on a column. Tick <b>Unique</b> to also forbid duplicates (e.g. emails)."),
    "field:fk": ("Foreign Keys", "Link this table to another. Example: <code>books.author_id → authors.id</code> means each book belongs to one author, and an author can have many books (one-to-many)."),
    "field:fk_action": ("On Delete / On Update", "What happens to child rows when the parent row is deleted or its key changes:<ul>"
                        "<li><b>RESTRICT / NO ACTION</b>: refuse (safest)</li>"
                        "<li><b>CASCADE</b>: delete/update the children too</li>"
                        "<li><b>SET NULL</b>: keep children, clear their link</li></ul>"),
}

CONCEPTS: T = {
    "concept:playground": ("Safe experimenting", "Real databases have no undo button. In the playground you get one: every change is copied first, "
                           "so mistakes are free. In real projects, transactions (BEGIN … ROLLBACK) and backups play that role."),
    "concept:pk": ("Primary keys", "A primary key is the unique ID of a row. Other tables use it to point at that row. Rules: unique, never NULL, and ideally never changes. "
                   "A simple auto-incrementing <code>id</code> is the most common choice."),
    "concept:fk": ("Foreign keys & relationships", "A foreign key column holds the primary key of a row in another table. That creates a <b>relationship</b>:<ul>"
                   "<li><b>One-to-many</b>: one author → many books (FK on the 'many' side: <code>books.author_id</code>)</li>"
                   "<li><b>One-to-one</b>: FK that is also UNIQUE (user → profile)</li>"
                   "<li><b>Many-to-many</b>: a 'junction' table with two FKs (students ↔ courses via <code>enrollments</code>)</li></ul>"
                   "In the diagram, the line's fork (crow's foot) sits on the 'many' side."),
    "concept:join": ("JOINs", "A JOIN follows a relationship at query time: for each row on the left, find the matching rows on the right using the ON condition. "
                     "INNER keeps only matches; LEFT keeps every left row even without a match."),
    "concept:hash": ("Hashing vs encryption", "<b>Hashing</b> is one-way: you can check a password against the hash but never recover it. Use it for passwords. "
                     "Each hash includes a random <i>salt</i>, so two users with the same password get different hashes.<br><br>"
                     "<b>Encryption</b> is two-way: with the key you get the original back. Use it for data you must read later, such as API tokens. "
                     "If you lose the key, the data is gone for good, so export and back it up."),
    "concept:media": ("Storing images & videos", "<b>Inside the database (BLOB)</b>: one file holds everything, which is easy to copy and keeps changes transactional, "
                      "but the database gets big and slow to back up.<br><br>"
                      "<b>As a file path</b>: the file sits in a media folder and the row stores where. The database stays small and files are easy to serve, "
                      "but you must keep the folder with the database. The diagram shows missing files and orphan files.<br><br>"
                      "Rule of thumb: small images can be BLOBs; videos and large files should be paths."),
    "concept:normalization": ("Avoid repeating data", "If you find yourself typing the same customer name into many order rows, move customers into their own table "
                              "and store <code>customer_id</code> in orders instead. This is called <i>normalization</i>."),
    "concept:transaction": ("Transactions", "A group of changes that succeeds or fails as a unit. The data grid's Apply uses one, so a half-saved edit can't happen."),
}

TOPICS: T = {**{f"kw:{k}": v for k, v in KEYWORDS.items()}, **COMMANDS, **FIELDS, **CONCEPTS}


def topic(key: str) -> tuple[str, str] | None:
    return TOPICS.get(key)


def short_tip(key: str, max_len: int = 260) -> str | None:
    """A compact HTML tooltip: title + the first sentence or two of the explanation."""
    t = topic(key)
    if not t:
        return None
    title, body = t
    plain = re.sub(r"<pre>.*?</pre>", " ", body, flags=re.S)
    plain = re.sub(r"<li>", " • ", plain)
    plain = re.sub(r"<[^>]+>", "", plain)
    plain = " ".join(plain.split())
    if len(plain) > max_len:
        cut = plain[:max_len]
        plain = cut[: cut.rfind(". ") + 1] if ". " in cut else cut.rstrip() + "…"
    return f"<b>{title}</b><br>{plain}<br><i>⇧F1 or the Help panel for more</i>"


def keyword_topic(word: str) -> str | None:
    key = f"kw:{word.upper()}"
    return key if key in TOPICS else None


# ---- friendly error messages ---------------------------------------------------

ERROR_HINTS: list[tuple[str, str]] = [
    (r"no such table: ([\w.]+)|Table '([\w.]+)' doesn't exist|\(1146",
     "The table <b>{0}</b> doesn't exist. Check the spelling in the sidebar. In MySQL, make sure the right database is selected."),
    (r"no such column: ([\w.]+)|Unknown column '([^']+)'|\(1054",
     "There's no column called <b>{0}</b>. Expand the table in the sidebar to see its columns, or press ⌃Space for suggestions. "
     "Text values need 'single quotes'; without quotes they're read as column names."),
    (r"UNIQUE constraint failed: ([\w.]+)|Duplicate entry '([^']*)'|\(1062",
     "That value is already used, and <b>{0}</b> must be unique (a primary key or UNIQUE column). Choose a different value or update the existing row."),
    (r"FOREIGN KEY constraint failed|a foreign key constraint fails|\(1451|\(1452",
     "A <b>foreign key</b> blocked this. Either you referenced a parent row that doesn't exist (insert the parent first), "
     "or you tried to delete a parent that still has child rows (delete the children first, or use ON DELETE CASCADE)."),
    (r"NOT NULL constraint failed: ([\w.]+)|Column '([^']+)' cannot be null|doesn't have a default value|\(1048|\(1364",
     "<b>{0}</b> is required (NOT NULL) and no value was given. Provide a value or give the column a DEFAULT."),
    (r"syntax error|You have an error in your SQL syntax|\(1064",
     "The database couldn't read the SQL. Common causes are a missing comma between columns, a missing closing quote or parenthesis, "
     "or a reserved word used as a name (wrap it in quotes). The error points to where it got confused."),
    (r"Access denied|\(1045",
     "The server rejected the username or password. Edit the connection to fix them."),
    (r"Can't connect|\(2003|Connection refused",
     "Couldn't reach the database server. Is it running, and are the host and port right? (MySQL's default port is 3306.)"),
    (r"database is locked",
     "Another program (or another tab) is writing to this SQLite file. Wait a moment, or finish the open transaction with COMMIT or ROLLBACK."),
    (r"ambiguous column name: ([\w.]+)|Column '([^']+)' in field list is ambiguous|\(1052",
     "<b>{0}</b> exists in more than one joined table. Prefix it with the table or alias, e.g. <code>c.id</code>."),
    (r"misuse of aggregate|isn't in GROUP BY|not in GROUP BY|\(1055",
     "When using COUNT/SUM/... every other selected column must appear in GROUP BY."),
]


def friendly_error(message: str) -> str | None:
    for pattern, hint in ERROR_HINTS:
        m = re.search(pattern, message, re.IGNORECASE)
        if m:
            name = next((g for g in m.groups() if g), "") if m.groups() else ""
            return hint.format(name or "that column")
    return None
