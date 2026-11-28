# DB IDE

A local desktop IDE for **SQLite**, **MySQL / MariaDB**, **PostgreSQL** and **Microsoft SQL Server**, written in Python with PySide6 (Qt).

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
  - **Checkboxes** next to each row number select several rows at once (click a box, or use **Select all**). **Delete Checked** stages all of them for deletion, and **Update Checked…** sets one column to the same value, or to NULL, on every checked row. Both are still staged until you Apply.
- **Table designer**: create or alter tables visually (columns, types, NOT NULL, PK, auto-increment, defaults, comments, indexes, foreign keys). It shows a live SQL preview, and asks you to confirm the exact statements before running them. In SQLite, changes that `ALTER TABLE` can't make are done with the standard rebuild-and-copy procedure inside a transaction; indexes, triggers, inline UNIQUE constraints and dependent views are preserved.
- **Import / export**: export tables or query results as CSV, JSON or SQL `INSERT` statements. Import CSV or JSON into a table with column mapping; the import runs in one transaction.

## Two modes

Pick one on first launch. Switch any time from **View → Mode** (⌃⌥1 / ⌃⌥2), the mode button in the toolbar, or the badge in the status bar.

| | **Learning mode**: become a database admin | **Power user mode**: work as a database admin |
|---|---|---|
| Lessons | 10 lessons: SQL basics, then admin skills (indexes and query plans, transactions, constraints, health checks and backups, users and permissions) | Hidden |
| Help | Explains SQL and database concepts, plain-English error tips, keyword tooltips, "are you sure?" prompts | About the software only: tools and shortcuts. Raw errors, no prompts |
| Tools | Revealed as your skills grow (Beginner → Explorer → Builder → Pro) | All available immediately |
| First screen | Playground with sample data | Your connections |
| Playground | ⌘⇧P, toolbar and activity bar | Hidden (not needed) |

**Admin Console** (⌘⇧A, or right-click a database). It's available immediately in Power user mode and at the Pro level in Learning mode.
- **Overview:** version, size, location, and rows and indexes per table
- **Maintenance:** integrity check, Analyze, Vacuum/Optimize, and Backup (an exact file copy for SQLite, a SQL dump for the server engines)
- **Activity:** running queries (MySQL, PostgreSQL, SQL Server), with **Stop**
- **Users:** accounts and their GRANTs
- **Settings:** PRAGMAs or server variables

## Playground, templates and growing with you

**Playground** (first thing you see on a fresh install; `⌘⇧P`, or the flask icon) is a private copy of a sample database:
- It runs **SQL, the grid and the designers**; anything they change can be undone.
- **Undo safety:** before every change, the whole database is snapshotted. **Undo** steps back, even after `DROP TABLE` (up to 40 steps), and **Reset** rebuilds the sample.
- **Guard rails:** queries stop after 5 seconds, and `ATTACH` is blocked, so nothing outside the sandbox can be touched.
- **Try This:** each dataset comes with suggestions (plain English or SQL), each noting what it teaches.
- **Save as Project…** keeps your work as a real file.

**Templates** (File → New Project from Template…, `⌘⇧N`):
- **Six projects:** Online Store, Blog, School, Library, Task Tracker, Recipes.
- **Each includes:** keys, indexes, `ON DELETE` rules, realistic sample data, real image files (stored in the database or on disk), and Argon2id-hashed passwords.
- **Two ways to use one:** open it in the Playground, or create it as a new SQLite file or inside a MySQL/SQLite database.

**Progressive disclosure.** The IDE notices skills as you use them (your first query, WHERE, ORDER BY, JOIN, GROUP BY, changing data, Undo, creating tables, relationships, lessons, templates). Skills add up to four levels:

| Level | Skills needed | Tools it reveals |
|---|---|---|
| Beginner | 0 | Playground, Ask, Run, charts, grid editing, lessons |
| Explorer | 4 | History, Export/Import, Diagram, Visualize JOIN, chart customization, Schema Designer |
| Builder | 8 | Table Designer indexes, foreign keys and roles; Run All and Max rows; Password Tool |
| Pro | 11 | Query plan (EXPLAIN), Security Settings, SQL previews open by default |

- Locked toolbar buttons are hidden. Locked menu items show 🔒 and explain how to unlock them, with **Unlock now**.
- The trophy icon opens **Your Progress**, which shows your skills, what to try next, and **Show all features now** for experienced developers.
- SQL the IDE writes for a plain-English question counts as *asking*, not as writing a JOIN yourself.

**Concept tips on demand**
- **Explain (⇧F1):** click it, then click anything for an explanation.
- **SQL keywords:** hover over any keyword in the editor for a short explanation.
- **Column headers:** hover in a data grid to learn what *primary key*, *foreign key → table*, *required* and *unique* mean for that column.
- **Sidebar:** tables and columns have tips too.

## Visual building, plain-English queries and charts

**Schema Designer** (`⌘⇧D`, or right-click a database → Schema Designer, or **Edit in Designer** on a diagram)
- Click **Add Table** (it starts with an auto-numbered `id` primary key), then double-click *+ add column* to add fields.
- To link two tables, drag the ● handle on one table (or on a specific column) onto another. You then choose the relationship:
  - **One-to-many** adds `parent_id` plus a foreign key and an index.
  - **One-to-one** does the same, but makes the column UNIQUE.
  - **Many-to-many** builds a junction table with two foreign keys and a composite primary key.

  Each choice is explained in plain English, and **Advanced options** shows the on-delete rule and the exact SQL.
- The pending changes list describes every edit in words. Nothing touches the database until **Review & Apply**, which lists the steps (parents before children) and shows the SQL behind **Show SQL**. On SQLite, changes to existing tables use the safe rebuild-and-copy procedure, so existing data is kept.
- New tables have a dashed border, changed tables show ●. Right-click a line to change the on-delete rule or remove the link.

**Ask in plain English** (the bar above every SQL editor). It runs **fully offline**:
- It reads the request against your schema and the values stored in your short text columns, so "customers in UK" knows that `UK` is a `country`. Password, encrypted and media columns are never read.
- It handles:
  - filters (`over 50`, `between 10 and 60`, `contains 'pro'`, `is empty`, `not paid`) and dates (`in 2024`, `after 2023`)
  - counts, totals, averages and min/max, with grouping (`per customer`, `by status`)
  - `top 5` and `newest`
  - joins along foreign-key paths (`products ordered by Ada` crosses three tables)
  - `customers without orders`, `products never ordered` and `authors with more than 2 books`
- It shows how each phrase was understood, flags any words it didn't use, puts the SQL in the editor for you to edit, and runs it. Generated SQL is always read-only (`SELECT`).

**Charts**: every result gets a **Chart** tab. The form is chosen from the data:
- a single number becomes a stat tile
- categories with a number become ranked bars
- dates become a line
- two numbers become a scatter

It uses a colour-blind-validated palette in both light and dark themes, thin marks, hover readouts that put the value first, and a legend whenever there are two or more series. **Customize** lets you pick the chart type and X/Y columns, and **Save PNG** exports the chart.

**Simple by default, powerful when needed.** In Learning mode, advanced controls (SQL previews, on-delete rules, chart customization) start folded behind one click. In Power user mode they start open.

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

- **Learning mode** (View → Mode): the Help panel explains every button you hover over, the SQL keyword under your cursor, and the designer field you're editing. Statements are explained in plain English when you run them, and it asks before risky ones such as `DELETE` or `UPDATE` without `WHERE`, or `DROP`.
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

**Special types in every Type dropdown.** Below the normal SQL types, every Type dropdown also offers these, in both the Table Designer and the Schema Designer:

- 🔑 Password (hashed)
- 🔒 Encrypted text
- 🖼 / 📁 Image, 🎬 Video or 📎 File, each either **stored in the database** or saved as a **file on disk**

Picking one sets the right SQL type and the column's role together. One click on a Type or Role cell opens its dropdown.

**Where media files go.** Files saved "on disk" are copied to `<database name>_media/` next to an SQLite file (or `~/.dbide/media/<connection>/` for MySQL). The **Insert Image/Video…** dialog shows the exact folder, with **Change…** (remembered per column) and **Open Folder**. Hovering over a media cell shows the file's full location and warns if it's missing; right-click → **Open Media Folder** opens it in Finder. Paths inside the database's own folder are stored relative to it, so the database and its media can be moved together.

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

## Supported engines

| Engine | Python driver | Defaults in New Connection | Notes |
|---|---|---|---|
| SQLite | built in | a file | Backup is an exact file copy |
| MySQL / MariaDB | PyMySQL | port 3306, user `root` | SSL option |
| PostgreSQL | psycopg 3 | port 5432, user and database `postgres` | Schemas shown in the sidebar; SSL option (`sslmode=require`); a host starting with `/` is a Unix socket folder |
| SQL Server | pymssql | port 1433, user `sa`, database `master` | `[ ]` quoting, `TOP`/`OFFSET…FETCH` paging, `IDENTITY` columns. EXPLAIN isn't available |

PostgreSQL is tested against a real server (the dev-only `pgserver` package starts a throwaway one during `uv run pytest`). SQL Server is tested through the SQL the app generates, without a live server.

Adding another engine means writing a `Driver` subclass in `drivers/` (URL, quoting, paging, CREATE/ALTER generation, cancel) and registering it in `drivers/__init__.py`.
