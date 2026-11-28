"""Database administration: overview, health checks, maintenance, backups,
running queries, users/grants and settings. SQLite, MySQL/MariaDB, PostgreSQL and SQL Server."""
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import inspect

from dbide.core.transfer import sql_literal


@dataclass
class TableStat:
    name: str
    rows: int | None
    indexes: int
    data_bytes: int | None = None
    index_bytes: int | None = None


@dataclass
class Overview:
    engine: str
    version: str
    location: str
    size_bytes: int | None
    tables: list[TableStat] = field(default_factory=list)
    views: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass
class Result:
    """Rows plus a one-line summary, for maintenance and listings."""

    title: str
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    ok: bool = True
    message: str = ""


def _is_sqlite(s) -> bool:
    return s.cfg.driver == "sqlite"


def _qual(s, table, schema):
    return s.driver.qualify(table, schema)


def overview(s, schema: str | None = None) -> Overview:
    if s.cfg.driver in _OTHER:
        return _OTHER[s.cfg.driver]['overview'](s, schema)
    insp = inspect(s.engine)
    ov = Overview(engine=s.driver.label, version=s.server_version or "", location="", size_bytes=None)
    with s.engine.connect() as c:
        if _is_sqlite(s):
            path = Path(s.cfg.database).expanduser()
            ov.location = str(path)
            ov.version = ov.version or "SQLite " + c.exec_driver_sql("SELECT sqlite_version()").scalar()
            ov.size_bytes = path.stat().st_size if path.exists() else None
            for t in sorted(insp.get_table_names()):
                rows = c.exec_driver_sql(f"SELECT COUNT(*) FROM {_qual(s, t, None)}").scalar()
                ov.tables.append(TableStat(t, rows, len(insp.get_indexes(t))))
            ov.views = len(insp.get_view_names())
            mode = c.exec_driver_sql("PRAGMA journal_mode").scalar()
            fk = c.exec_driver_sql("PRAGMA foreign_keys").scalar()
            ov.notes.append(f"Journal mode: {mode}. Foreign keys enforced: {'yes' if fk else 'no'}.")
        else:
            db = schema or s.cfg.database or c.exec_driver_sql("SELECT DATABASE()").scalar()
            ov.location = f"{s.cfg.host}:{s.cfg.port or 3306} / {db or '(no database selected)'}"
            ov.version = ov.version or c.exec_driver_sql("SELECT VERSION()").scalar()
            if db:
                rows = c.exec_driver_sql(
                    "SELECT TABLE_NAME, TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH, TABLE_TYPE FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME", (db,)).fetchall()
                idx = dict(c.exec_driver_sql(
                    "SELECT TABLE_NAME, COUNT(DISTINCT INDEX_NAME) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = %s GROUP BY TABLE_NAME",
                    (db,)).fetchall())
                total = 0
                for name, trows, data, index, ttype in rows:
                    if ttype == "VIEW":
                        ov.views += 1
                        continue
                    ov.tables.append(TableStat(name, trows, int(idx.get(name, 0)), data, index))
                    total += (data or 0) + (index or 0)
                ov.size_bytes = total
                ov.notes.append("Row counts for InnoDB tables are estimates (from information_schema).")
    return ov


def integrity_check(s, schema: str | None = None) -> Result:
    if s.cfg.driver in _OTHER:
        return _OTHER[s.cfg.driver]['integrity'](s, schema)
    with s.engine.connect() as c:
        if _is_sqlite(s):
            res = [r[0] for r in c.exec_driver_sql("PRAGMA integrity_check").fetchall()]
            fk = c.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            rows = [("integrity_check", r) for r in res] + [("foreign_key_check", f"{t} row {rid} → {parent}") for t, rid, parent, _ in fk]
            ok = res == ["ok"] and not fk
            msg = "No problems found." if ok else f"{len(rows)} problem(s) found."
            return Result("Integrity check", ["check", "result"], rows or [("integrity_check", "ok")], ok, msg)
        tables = inspect(s.engine).get_table_names(schema=schema)
        rows = []
        for t in tables:
            rows += [tuple(r) for r in c.exec_driver_sql(f"CHECK TABLE {_qual(s, t, schema)}").fetchall()]
        ok = all(str(r[-1]).upper() in ("OK", "TABLE IS ALREADY UP TO DATE") for r in rows if str(r[2]).lower() == "status")
        return Result("CHECK TABLE", ["table", "op", "msg_type", "msg_text"], rows, ok,
                      "All tables OK." if ok else "Some tables reported problems.")


def analyze(s, schema: str | None = None) -> Result:
    if s.cfg.driver in _OTHER:
        return _OTHER[s.cfg.driver]['analyze'](s, schema)
    with s.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        if _is_sqlite(s):
            c.exec_driver_sql("ANALYZE")
            return Result("ANALYZE", message="Statistics refreshed: the query planner now has up-to-date row counts.")
        rows = []
        for t in inspect(s.engine).get_table_names(schema=schema):
            rows += [tuple(r) for r in c.exec_driver_sql(f"ANALYZE TABLE {_qual(s, t, schema)}").fetchall()]
        return Result("ANALYZE TABLE", ["table", "op", "msg_type", "msg_text"], rows, True, "Key distribution statistics refreshed.")


def optimize(s, schema: str | None = None) -> Result:
    if s.cfg.driver in _OTHER:
        return _OTHER[s.cfg.driver]['optimize'](s, schema)
    with s.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        if _is_sqlite(s):
            path = Path(s.cfg.database).expanduser()
            before = path.stat().st_size if path.exists() else 0
            c.exec_driver_sql("VACUUM")
            after = path.stat().st_size if path.exists() else 0
            return Result("VACUUM", message=f"Rebuilt and compacted the file: {before:,} → {after:,} bytes.")
        rows = []
        for t in inspect(s.engine).get_table_names(schema=schema):
            rows += [tuple(r) for r in c.exec_driver_sql(f"OPTIMIZE TABLE {_qual(s, t, schema)}").fetchall()]
        return Result("OPTIMIZE TABLE", ["table", "op", "msg_type", "msg_text"], rows, True, "Tables defragmented / rebuilt.")


def backup(s, dest: str | Path, schema: str | None = None) -> Result:
    """SQLite: an exact copy via the online backup API. MySQL: a SQL dump (CREATE + INSERT)."""
    dest = Path(dest).expanduser()
    if _is_sqlite(s):
        src = sqlite3.connect(Path(s.cfg.database).expanduser())
        out = sqlite3.connect(dest)
        try:
            src.backup(out)
        finally:
            src.close()
            out.close()
        return Result("Backup", message=f"Copied the database to {dest} ({dest.stat().st_size:,} bytes).")
    if s.cfg.driver in _OTHER:
        return _generic_dump(s, dest, schema)
    insp = inspect(s.engine)
    n_rows = 0
    with s.engine.connect() as c, open(dest, "w", encoding="utf-8") as f:
        db = schema or s.cfg.database
        f.write(f"-- DB IDE dump of {db or 'database'} on {s.cfg.host}, {dt.datetime.now():%Y-%m-%d %H:%M}\n")
        f.write("SET FOREIGN_KEY_CHECKS = 0;\n\n")
        for t in insp.get_table_names(schema=schema):
            create = c.exec_driver_sql(f"SHOW CREATE TABLE {_qual(s, t, schema)}").fetchone()[1]
            f.write(f"DROP TABLE IF EXISTS {s.driver.quote(t)};\n{create};\n\n")
            res = c.exec_driver_sql(f"SELECT * FROM {_qual(s, t, schema)}")
            cols = ", ".join(s.driver.quote(k) for k in res.keys())
            while True:
                chunk = res.fetchmany(500)
                if not chunk:
                    break
                vals = ",\n".join("(" + ", ".join(sql_literal(v, s.driver) for v in r) + ")" for r in chunk)
                f.write(f"INSERT INTO {s.driver.quote(t)} ({cols}) VALUES\n{vals};\n")
                n_rows += len(chunk)
            f.write("\n")
        f.write("SET FOREIGN_KEY_CHECKS = 1;\n")
    return Result("Backup", message=f"Wrote a SQL dump ({n_rows:,} rows) to {dest}. Restore it by running the file.")


def processes(s) -> Result:
    if s.cfg.driver in _OTHER:
        return _OTHER[s.cfg.driver]['processes'](s)
    if _is_sqlite(s):
        return Result("Activity", ok=True, message="SQLite has no server: only this app's own connections use the file.")
    with s.engine.connect() as c:
        res = c.exec_driver_sql("SHOW FULL PROCESSLIST")
        cols = list(res.keys())
        return Result("Running queries", cols, [tuple(r) for r in res.fetchall()])


def kill(s, process_id: int, query_only: bool = True) -> None:
    if _is_sqlite(s):
        raise ValueError("SQLite has no server processes to stop.")
    if s.cfg.driver == "postgresql":
        fn = "pg_cancel_backend" if query_only else "pg_terminate_backend"
        with s.engine.connect() as c:
            c.exec_driver_sql(f"SELECT {fn}({int(process_id)})")
        return
    if s.cfg.driver == "mssql":
        with s.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
            c.exec_driver_sql(f"KILL {int(process_id)}")  # ends the session (SQL Server can't cancel just the statement)
        return
    with s.engine.connect() as c:
        c.exec_driver_sql(f"KILL {'QUERY ' if query_only else ''}{int(process_id)}")


def users(s) -> Result:
    if s.cfg.driver in _OTHER:
        return _OTHER[s.cfg.driver]['users'](s)
    if _is_sqlite(s):
        return Result("Users", ok=True, message="SQLite has no user accounts: access is controlled by who can read the file.")
    with s.engine.connect() as c:
        try:
            rows = c.exec_driver_sql("SELECT User, Host FROM mysql.user ORDER BY User, Host").fetchall()
        except Exception as exc:  # usually missing privilege on mysql.user
            orig = getattr(exc, "orig", exc)
            me = c.exec_driver_sql("SELECT CURRENT_USER()").scalar()
            return Result("Users", ["user@host"], [(me,)], False,
                          f"Can't list all users ({orig}). Showing only the current account.")
        return Result("Users", ["user", "host"], [tuple(r) for r in rows])


def grants(s, user: str, host: str = "%") -> list[str]:
    if _is_sqlite(s):
        return []
    if s.cfg.driver == "postgresql":
        with s.engine.connect() as c:
            rows = c.exec_driver_sql(
                "SELECT privilege_type, table_schema, table_name FROM information_schema.role_table_grants "
                "WHERE grantee = %s ORDER BY table_schema, table_name, privilege_type", (user,)).fetchall()
        return [f"GRANT {p} ON {sch}.{t} TO {user}" for p, sch, t in rows]
    if s.cfg.driver == "mssql":
        with s.engine.connect() as c:
            rows = c.exec_driver_sql(
                "SELECT p.state_desc, p.permission_name, ISNULL(OBJECT_SCHEMA_NAME(p.major_id) + '.' + OBJECT_NAME(p.major_id), p.class_desc) "
                "FROM sys.database_permissions p JOIN sys.database_principals u ON u.principal_id = p.grantee_principal_id "
                "WHERE u.name = %s", (user,)).fetchall()
        return [f"{state} {perm} ON {obj} TO [{user}]" for state, perm, obj in rows]
    with s.engine.connect() as c:
        return [r[0] for r in c.exec_driver_sql(f"SHOW GRANTS FOR {s.driver.literal(user)}@{s.driver.literal(host)}").fetchall()]


SQLITE_PRAGMAS = ["journal_mode", "synchronous", "foreign_keys", "page_size", "page_count", "freelist_count",
                  "cache_size", "auto_vacuum", "user_version", "encoding", "busy_timeout"]
MYSQL_VARIABLES = ["version", "version_comment", "innodb_buffer_pool_size", "max_connections", "max_allowed_packet",
                   "character_set_server", "collation_server", "time_zone", "sql_mode", "autocommit",
                   "transaction_isolation", "wait_timeout", "slow_query_log", "long_query_time"]


def settings_list(s) -> Result:
    if s.cfg.driver in _OTHER:
        return _OTHER[s.cfg.driver]['settings'](s)
    with s.engine.connect() as c:
        if _is_sqlite(s):
            rows = []
            for name in SQLITE_PRAGMAS:
                try:
                    rows.append((name, c.exec_driver_sql(f"PRAGMA {name}").scalar()))
                except Exception:
                    pass
            return Result("PRAGMA settings", ["setting", "value"], rows)
        placeholders = ", ".join(["%s"] * len(MYSQL_VARIABLES))
        rows = c.exec_driver_sql(f"SHOW VARIABLES WHERE Variable_name IN ({placeholders})", tuple(MYSQL_VARIABLES)).fetchall()
        status = c.exec_driver_sql("SHOW GLOBAL STATUS WHERE Variable_name IN ('Uptime', 'Threads_connected', 'Questions', 'Slow_queries')").fetchall()
        return Result("Server variables", ["setting", "value"], [tuple(r) for r in rows] + [tuple(r) for r in status])


# ---- PostgreSQL -------------------------------------------------------------------------


def _pg_overview(s, schema):
    insp = inspect(s.engine)
    schema = schema or "public"
    ov = Overview(engine=s.driver.label, version=s.server_version or "", location="", size_bytes=None)
    with s.engine.connect() as c:
        ov.version = ov.version or c.exec_driver_sql("SHOW server_version").scalar()
        db = c.exec_driver_sql("SELECT current_database()").scalar()
        ov.location = f"{s.cfg.host or 'localhost'}:{s.cfg.port or 5432} / {db} / {schema}"
        ov.size_bytes = c.exec_driver_sql("SELECT pg_database_size(current_database())").scalar()
        rows = c.exec_driver_sql(
            "SELECT c.relname, GREATEST(c.reltuples, 0)::bigint, pg_relation_size(c.oid), pg_indexes_size(c.oid), "
            "(SELECT COUNT(*) FROM pg_index i WHERE i.indrelid = c.oid) "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relkind IN ('r', 'p') ORDER BY c.relname", (schema,)).fetchall()
        for name, est, data, index, nidx in rows:
            exact = c.exec_driver_sql(f"SELECT COUNT(*) FROM {s.driver.qualify(name, schema)}").scalar() if est < 100000 else est
            ov.tables.append(TableStat(name, exact, int(nidx), data, index))
        ov.views = len(insp.get_view_names(schema=schema))
        ov.notes.append("Size is the whole database. Large tables show estimated row counts (from the planner's statistics).")
    return ov


def _pg_integrity(s, schema):
    """Postgres has no single integrity command: check for invalid indexes and constraints that were never validated."""
    with s.engine.connect() as c:
        bad_ix = c.exec_driver_sql(
            "SELECT 'invalid index', i.indexrelid::regclass::text FROM pg_index i WHERE NOT i.indisvalid").fetchall()
        bad_con = c.exec_driver_sql(
            "SELECT 'not validated', conname || ' on ' || conrelid::regclass::text FROM pg_constraint WHERE NOT convalidated").fetchall()
    rows = [tuple(r) for r in bad_ix + bad_con]
    ok = not rows
    return Result("Integrity check", ["check", "result"], rows or [("indexes and constraints", "ok")], ok,
                  "No invalid indexes or unvalidated constraints." if ok else f"{len(rows)} problem(s) found.")


def _pg_analyze(s, schema):
    with s.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        c.exec_driver_sql("ANALYZE")
    return Result("ANALYZE", message="Planner statistics refreshed for the whole database.")


def _pg_optimize(s, schema):
    with s.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        c.exec_driver_sql("VACUUM (ANALYZE)")
    return Result("VACUUM", message="Dead rows reclaimed and statistics refreshed (VACUUM ANALYZE).")


def _pg_processes(s):
    with s.engine.connect() as c:
        res = c.exec_driver_sql(
            "SELECT pid, usename AS user, datname AS database, state, now() - query_start AS running_for, "
            "LEFT(query, 200) AS query FROM pg_stat_activity WHERE backend_type = 'client backend' ORDER BY query_start")
        return Result("Running queries", list(res.keys()), [tuple(str(v) if hasattr(v, "total_seconds") else v for v in r) for r in res.fetchall()])


def _pg_users(s):
    with s.engine.connect() as c:
        res = c.exec_driver_sql(
            "SELECT rolname AS user, '' AS host, rolsuper AS superuser, rolcanlogin AS can_login, rolcreatedb AS create_db "
            "FROM pg_roles WHERE rolname NOT LIKE 'pg\\_%' ORDER BY rolname")
        return Result("Users", list(res.keys()), [tuple(r) for r in res.fetchall()])


PG_SETTINGS = ["server_version", "max_connections", "shared_buffers", "work_mem", "effective_cache_size", "maintenance_work_mem",
               "wal_level", "timezone", "default_transaction_isolation", "statement_timeout", "log_min_duration_statement",
               "autovacuum", "search_path", "server_encoding"]


def _pg_settings(s):
    with s.engine.connect() as c:
        rows = c.exec_driver_sql("SELECT name, setting || COALESCE(' ' || unit, '') FROM pg_settings WHERE name = ANY(%s) ORDER BY name",
                                 (PG_SETTINGS,)).fetchall()
        up = c.exec_driver_sql("SELECT now() - pg_postmaster_start_time()").scalar()
    return Result("Server settings", ["setting", "value"], [tuple(r) for r in rows] + [("uptime", str(up))])


# ---- SQL Server ----------------------------------------------------------------------------


def _ms_overview(s, schema):
    insp = inspect(s.engine)
    schema = schema or "dbo"
    ov = Overview(engine=s.driver.label, version=s.server_version or "", location="", size_bytes=None)
    with s.engine.connect() as c:
        ov.version = ov.version or str(c.exec_driver_sql("SELECT @@VERSION").scalar()).splitlines()[0]
        db = c.exec_driver_sql("SELECT DB_NAME()").scalar()
        ov.location = f"{s.cfg.host}:{s.cfg.port or 1433} / {db} / {schema}"
        ov.size_bytes = (c.exec_driver_sql("SELECT SUM(CAST(size AS BIGINT)) * 8192 FROM sys.database_files").scalar() or 0)
        rows = c.exec_driver_sql(
            "SELECT t.name, SUM(CASE WHEN p.index_id IN (0, 1) THEN p.rows ELSE 0 END), "
            "(SELECT COUNT(*) FROM sys.indexes i WHERE i.object_id = t.object_id AND i.index_id > 0), "
            "SUM(CASE WHEN a.type IN (1, 3) AND p.index_id IN (0, 1) THEN a.used_pages ELSE 0 END) * 8192, "
            "SUM(CASE WHEN p.index_id > 1 THEN a.used_pages ELSE 0 END) * 8192 "
            "FROM sys.tables t JOIN sys.schemas sc ON sc.schema_id = t.schema_id "
            "JOIN sys.partitions p ON p.object_id = t.object_id JOIN sys.allocation_units a ON a.container_id = p.partition_id "
            "WHERE sc.name = %s GROUP BY t.name, t.object_id ORDER BY t.name", (schema,)).fetchall()
        for name, nrows, nidx, data, index in rows:
            ov.tables.append(TableStat(name, nrows, int(nidx or 0), data, index))
        ov.views = len(insp.get_view_names(schema=schema))
        ov.notes.append("Size is the whole database (data and log files).")
    return ov


def _ms_integrity(s, schema):
    with s.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        c.exec_driver_sql("DBCC CHECKDB WITH NO_INFOMSGS")  # raises if corruption is found
    return Result("DBCC CHECKDB", ["check", "result"], [("DBCC CHECKDB", "ok")], True, "No consistency errors found.")


def _ms_analyze(s, schema):
    with s.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        c.exec_driver_sql("EXEC sp_updatestats")
    return Result("UPDATE STATISTICS", message="Statistics updated on every table (sp_updatestats).")


def _ms_optimize(s, schema):
    insp = inspect(s.engine)
    done = []
    with s.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        for t in insp.get_table_names(schema=schema):
            c.exec_driver_sql(f"ALTER INDEX ALL ON {s.driver.qualify(t, schema)} REBUILD")
            done.append((t, "indexes rebuilt"))
    return Result("Rebuild indexes", ["table", "result"], done, True, f"Rebuilt the indexes of {len(done)} table(s).")


def _ms_processes(s):
    with s.engine.connect() as c:
        res = c.exec_driver_sql(
            "SELECT s.session_id, s.login_name AS [user], DB_NAME(r.database_id) AS [database], r.status, "
            "r.total_elapsed_time / 1000.0 AS seconds, LEFT(t.text, 200) AS query "
            "FROM sys.dm_exec_requests r JOIN sys.dm_exec_sessions s ON s.session_id = r.session_id "
            "CROSS APPLY sys.dm_exec_sql_text(r.sql_handle) t WHERE s.is_user_process = 1")
        return Result("Running queries", list(res.keys()), [tuple(r) for r in res.fetchall()])


def _ms_users(s):
    with s.engine.connect() as c:
        res = c.exec_driver_sql(
            "SELECT name AS [user], type_desc AS host, create_date FROM sys.database_principals "
            "WHERE type IN ('S', 'U', 'G', 'E', 'X') AND name NOT IN ('guest', 'INFORMATION_SCHEMA', 'sys') ORDER BY name")
        return Result("Users", list(res.keys()), [tuple(r) for r in res.fetchall()])


def _ms_settings(s):
    with s.engine.connect() as c:
        props = [("edition", "Edition"), ("product_version", "ProductVersion"), ("collation", "Collation"), ("engine_edition", "EngineEdition")]
        rows = [(label, c.exec_driver_sql(f"SELECT CAST(SERVERPROPERTY('{p}') AS NVARCHAR(200))").scalar()) for label, p in props]
        rows += [tuple(r) for r in c.exec_driver_sql(
            "SELECT name, CAST(value_in_use AS NVARCHAR(50)) FROM sys.configurations WHERE name IN "
            "('max server memory (MB)', 'max degree of parallelism', 'cost threshold for parallelism', 'user connections', "
            "'backup compression default', 'optimize for ad hoc workloads')").fetchall()]
    return Result("Server settings", ["setting", "value"], rows)


def _generic_dump(s, dest: Path, schema):
    """A portable SQL dump: CREATE TABLE (parents first) then INSERTs, using the app's own DDL generator."""
    insp = inspect(s.engine)
    names = insp.get_table_names(schema=schema)
    with s.engine.connect() as c:
        defs = {t: s.driver.load_table_def(inspect(c), c, t, schema) for t in names}
    order, seen = [], set()

    def visit(t, stack=()):
        if t in seen or t in stack:
            return
        for fk in defs[t].foreign_keys:
            if fk.ref_table in defs and fk.ref_table != t:
                visit(fk.ref_table, stack + (t,))
        seen.add(t)
        order.append(t)

    for t in names:
        visit(t)
    n_rows = 0
    with s.engine.connect() as c, open(dest, "w", encoding="utf-8") as f:
        f.write(f"-- DB IDE dump ({s.driver.label}) {dt.datetime.now():%Y-%m-%d %H:%M}\n\n")
        for t in order:
            for stmt in s.driver.create_table_sql(defs[t]):
                f.write(stmt + ";\n")
            f.write("\n")
            res = c.exec_driver_sql(f"SELECT * FROM {s.driver.qualify(t, schema)}")
            cols = ", ".join(s.driver.quote(k) for k in res.keys())
            identity = [col.name for col in defs[t].columns if col.auto_increment]
            qualified = s.driver.qualify(t, schema)
            if identity:
                for stmt in s.driver.before_explicit_ids(qualified):
                    f.write(stmt + ";\n")
            for r in res.fetchall():
                vals = ", ".join(sql_literal(v, s.driver) for v in r)
                f.write(f"INSERT INTO {qualified} ({cols}) VALUES ({vals});\n")
                n_rows += 1
            for col in identity:
                for stmt in s.driver.after_explicit_ids(t, schema, col):
                    f.write(stmt + ";\n")
            f.write("\n")
    return Result("Backup", message=f"Wrote a SQL dump ({n_rows:,} rows, {len(order)} tables) to {dest}. Restore it by running the file.")


_OTHER = {
    "postgresql": {"overview": _pg_overview, "integrity": _pg_integrity, "analyze": _pg_analyze, "optimize": _pg_optimize,
                   "processes": _pg_processes, "users": _pg_users, "settings": _pg_settings},
    "mssql": {"overview": _ms_overview, "integrity": _ms_integrity, "analyze": _ms_analyze, "optimize": _ms_optimize,
              "processes": _ms_processes, "users": _ms_users, "settings": _ms_settings},
}
