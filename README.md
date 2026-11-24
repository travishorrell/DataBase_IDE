# DB IDE

A local desktop IDE for **SQLite** and **MySQL / MariaDB**, written in Python with PySide6 (Qt).

## Run

```bash
uv sync          # first time: creates .venv with Python 3.12 and dependencies
uv run dbide     # launch the app
uv run pytest    # run the tests
```

## Features

- **Connections**: saved profiles (`~/.dbide/connections.json`). Passwords are stored only in the macOS Keychain, and only if you choose "Save password". Includes a Test Connection button. For SQLite, *New…* creates a new database file.
- **Sidebar**: databases → tables / views → columns (🔑 = primary key) and indexes, loaded on demand. Right-click for actions: open data, SELECT top 100, show DDL, design table, import/export, empty, drop, create or drop database.
- **SQL editor**: syntax highlighting, line numbers, autocomplete for keywords, tables and columns, including `alias.` column completion.
  - `⌘↩` runs the selection or the statement under the cursor
  - `⌘⇧↩` or `F5` runs the whole script
  - `⌘.` stops a running query
  - `⌘E` shows the query plan (EXPLAIN)
  - `⌘/` toggles comments on the selected lines
  - `⌃Space` opens autocomplete

  Each tab has its own connection, so `USE db`, temp tables and manual `BEGIN … COMMIT` persist between runs. Multiple result sets appear as separate tabs, and every run is logged to the query history (`⌘Y`).
- **Data grid**: page through table rows with a `WHERE` filter and `ORDER BY`. Edit cells, add and delete rows, or set cells to NULL. Changes are staged, color-coded, and applied in a single transaction (`⌘S`). Tables without a primary key are read-only.
- **Table designer**: create or alter tables visually (columns, types, NOT NULL, PK, auto-increment, defaults, comments, indexes, foreign keys). It shows a live SQL preview, and asks you to confirm the exact statements before running them. In SQLite, changes that `ALTER TABLE` can't make are done with the standard rebuild-and-copy procedure inside a transaction; indexes, triggers, inline UNIQUE constraints and dependent views are preserved.
- **Import / export**: export tables or query results as CSV, JSON or SQL `INSERT` statements. Import CSV or JSON into a table with column mapping; the import runs in one transaction.

## Look & layout

- **Light, dark or match macOS.** Toggle with `⌘⇧L` or the sun/moon icon in the activity bar.
- **Eight themes**, all shown in **View → Appearance…** (`⌘,`) with live previews:
  - light: Daylight, Paper, Arctic
  - dark: Graphite, Midnight, Forest, Ember, High Contrast

  Appearance also lets you choose an accent colour, the interface and editor fonts, the density (compact, comfortable, spacious) and the zoom level. **Cancel** restores your previous settings.
- **Fonts** default to the system's own: SF Pro and SF Mono on macOS, falling back to JetBrains Mono, Cascadia, Fira or Menlo. The default size follows your screen size, and `⌘+` / `⌘−` / `⌘0` zoom the whole interface.
- **Layout**: an activity bar on the left switches the sidebar between Connections (with a table filter) and Lessons. Click the active icon or press `⌘B` to fold the sidebar. Tabs sit in the middle, and the Help panel is on the right (`⌘⌥H`).
- **Responsive**:
  - Below 1240 px wide, toolbars switch to icons only.
  - Below 1120 px, the Help panel folds away.
  - Below 820 px, the sidebar folds away.
  - A panel you open yourself on a narrow window stays open.
  - The results pane grows with the number of rows returned, unless you've dragged it.
  - Grid columns size to their content and the available width.
  - Diagrams keep themselves fitted while you resize, until you zoom.

## Learning features

- **Beginner mode** (View menu, on by default): the Help panel explains every button you hover over, the SQL keyword under your cursor, and the designer field you're editing. Statements are explained in plain English when you run them, and it asks before risky ones such as `DELETE` or `UPDATE` without `WHERE`, or `DROP`.
- **Help panel**: plain-English explanations of database errors (unknown table, duplicate key, foreign key violation, …) with suggested fixes, plus a set of concept explainers.
- **Explain in English** (`⌘⇧E`): describes what the current statement will do before you run it.
- **Visualize JOIN** (`⌘J`): draws the joined tables with the linking columns and a Venn diagram of the join type. It counts matched and unmatched rows from your real data and shows sample rows.
- **Lessons** (`F1`): five guided tutorials on a practice database: tables, keys, JOINs, password storage, and media. Each step can be checked automatically, or done for you.

## Diagram (ERD)

Right-click a database and choose **Diagram (ERD)**, or press `⌘D`. Tables show PK/FK badges and column roles. Relationship lines have a single bar on the "one" side and a crow's foot on the "many" side.

- Hover over a line to see the relationship explained; double-click it to open a ready-made JOIN query.
- Media columns connect to a **storage box** showing where their files physically live: inside the database file (with size), or in folders on disk (with counts of linked, **missing** and **orphan** files). Double-click a folder box to open it in Finder.
- Boxes can be dragged, and positions are remembered. Diagrams export to PNG or SVG.

## Column roles & security

Set a column's **Role** in the table designer. Roles are stored by the IDE (`~/.dbide/column_roles.json`), so the database itself stays plain.

| Role | What happens |
|---|---|
| 🔑 Password hash: Argon2id, bcrypt, scrypt or PBKDF2-SHA256 | Values typed into the grid or imported are hashed before saving. Right-click a cell to check a password against it. |
| 🔒 Encrypted: AES-256-GCM, ChaCha20-Poly1305 or Fernet | Values are encrypted with a per-connection 256-bit key. Right-click → Reveal decrypts them. Export the key from Tools → Password & Encryption Tool; if it's lost, the data can't be recovered. |
| 🖼 Media in database (BLOB) / 📁 Media file path | Right-click → Insert Image/Video…; double-click to view the image or play the video and see where it's stored. File paths are copied into `<db>_media/` next to an SQLite file, stored relative to it. |

**Tools → Security Settings** chooses where the IDE keeps its own secrets (saved logins and field keys):

- **System keychain**
- **Encrypted vault file**: AES-256-GCM, with a key derived from a master password via scrypt
- **Don't save**: memory only

Switching backends moves the existing secrets.

## Layout

```
src/dbide/
  core/       config, session, SQL splitter, schema model, history, import/export,
              crypto, secrets, roles, media, erd_model, sql_explain, knowledge, lessons
  drivers/    one module per engine: URL, quoting, introspection, CREATE/ALTER generation, cancel
  ui/         main window, sidebar, query/data/designer tabs, ERD, JOIN explainer,
              help panel, lessons, media viewer, security dialogs
```

## Adding PostgreSQL

1. Run `uv sync --extra postgres` to install psycopg.
2. Add `drivers/postgres.py` with a `Driver` subclass (URL, `alter_table_sql`, `cancel` via `pg_cancel_backend`).
3. Register it in `drivers/__init__.py`.
