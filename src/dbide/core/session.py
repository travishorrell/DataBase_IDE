"""A live connection to one database: introspection, querying, data editing.

All methods are blocking and thread-safe; the UI calls them from worker threads.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

from sqlalchemy import create_engine, inspect, text

from dbide.core.config import ConnectionConfig
from dbide.core.schema_model import TableDef
from dbide.core.sqlsplit import split_sql
from dbide.drivers import get_driver


@dataclass
class StatementResult:
    sql: str
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    rowcount: int = -1
    returns_rows: bool = False
    truncated: bool = False
    elapsed: float = 0.0
    error: str | None = None


@dataclass
class TablePage:
    columns: list[str]
    rows: list[list[Any]]
    primary_key: list[str]
    total: int
    offset: int


class DatabaseSession:
    def __init__(self, cfg: ConnectionConfig, password: str | None = None):
        self.cfg = cfg
        self.driver = get_driver(cfg.driver)
        # no_parameters: SQL typed by the user (or generated without bind values) goes to the driver untouched.
        # Otherwise psycopg / PyMySQL / pymssql treat every % as a placeholder and `LIKE '%a%'` or `10 % 3` fail.
        self.engine = create_engine(self.driver.url(cfg, password), execution_options={"no_parameters": True},
                                    **self.driver.engine_kwargs(cfg))
        self.driver.install_hooks(self.engine, cfg)
        self.server_version = ""
        # called (from a worker thread) right before anything is written; the Playground snapshots here
        self.before_write = None
        self.time_limit: float | None = cfg.options.get("time_limit")

    # ---- lifecycle --------------------------------------------------------
    def connect(self) -> str:
        """Open a test connection; returns a server version string."""
        with self.engine.connect() as conn:
            if self.cfg.driver == "sqlite":
                v = conn.exec_driver_sql("SELECT sqlite_version()").scalar()
                self.server_version = f"SQLite {v}"
            else:
                self.server_version = str(conn.exec_driver_sql("SELECT VERSION()").scalar())
        return self.server_version

    def close(self) -> None:
        self.engine.dispose()

    def query_connection(self) -> "QueryConnection":
        return QueryConnection(self)

    # ---- introspection ----------------------------------------------------
    def databases(self) -> list[str]:
        return self.driver.list_databases(inspect(self.engine))

    def tables(self, schema: str | None = None) -> list[str]:
        return sorted(inspect(self.engine).get_table_names(schema=schema), key=str.lower)

    def views(self, schema: str | None = None) -> list[str]:
        return sorted(inspect(self.engine).get_view_names(schema=schema), key=str.lower)

    def columns(self, table: str, schema: str | None = None) -> list[dict]:
        insp = inspect(self.engine)
        pk = set((insp.get_pk_constraint(table, schema=schema) or {}).get("constrained_columns") or [])
        out = []
        with self.engine.connect() as conn:
            for c in insp.get_columns(table, schema=schema):
                try:
                    t = c["type"].compile(dialect=conn.dialect)
                except Exception:
                    t = str(c["type"])
                out.append({"name": c["name"], "type": t, "nullable": c.get("nullable", True), "pk": c["name"] in pk})
        return out

    def indexes(self, table: str, schema: str | None = None) -> list[dict]:
        return inspect(self.engine).get_indexes(table, schema=schema)

    def table_def(self, table: str, schema: str | None = None) -> TableDef:
        with self.engine.connect() as conn:
            return self.driver.load_table_def(inspect(conn), conn, table, schema)

    def ddl(self, table: str, schema: str | None = None) -> str:
        with self.engine.connect() as conn:
            return self.driver.show_create(conn, table, schema)

    def completion_catalog(self, schema: str | None = None) -> dict[str, list[str]]:
        """{table_or_view: [column, ...]} for the editor's autocomplete."""
        insp = inspect(self.engine)
        catalog: dict[str, list[str]] = {}
        try:
            multi = insp.get_multi_columns(schema=schema, kind=_all_kinds())
            for (_, table), cols in multi.items():
                catalog[table] = [c["name"] for c in cols]
        except Exception:
            for t in insp.get_table_names(schema=schema) + insp.get_view_names(schema=schema):
                try:
                    catalog[t] = [c["name"] for c in insp.get_columns(t, schema=schema)]
                except Exception:
                    catalog[t] = []
        return catalog

    def _about_to_write(self, what: str) -> None:
        if self.before_write is not None:
            self.before_write(what)

    # ---- table data -------------------------------------------------------
    def fetch_page(
        self,
        table: str,
        schema: str | None,
        limit: int,
        offset: int,
        where: str = "",
        order_by: str = "",
    ) -> TablePage:
        q = self.driver.quote
        pk = (inspect(self.engine).get_pk_constraint(table, schema=schema) or {}).get("constrained_columns") or []
        src = self.driver.qualify(table, schema)
        where_sql = f" WHERE {where}" if where.strip() else ""
        if order_by.strip():
            order_sql = f" ORDER BY {order_by}"
        elif pk:
            order_sql = " ORDER BY " + ", ".join(q(c) for c in pk)
        else:
            order_sql = ""
        with self.engine.connect() as conn:
            total = conn.exec_driver_sql(f"SELECT COUNT(*) FROM {src}{where_sql}").scalar() or 0
            res = conn.exec_driver_sql(
                self.driver.select_limit(f"SELECT * FROM {src}{where_sql}", int(limit), int(offset), order_sql.strip())
            )
            cols = list(res.keys())
            rows = [list(r) for r in res.fetchall()]
        return TablePage(cols, rows, list(pk), int(total), offset)

    def apply_changes(
        self,
        table: str,
        schema: str | None,
        updates: list[tuple[dict, dict]],
        inserts: list[dict],
        deletes: list[dict],
    ) -> int:
        """Apply grid edits in one transaction. Keys are column names. Returns rows affected."""
        q = self.driver.quote
        t = self.driver.qualify(table, schema)
        affected = 0
        self._about_to_write("edit rows in " + table)

        def where_clause(keys: dict, prefix: str) -> tuple[str, dict]:
            parts, params = [], {}
            for i, (k, v) in enumerate(keys.items()):
                if v is None:
                    parts.append(f"{q(k)} IS NULL")
                else:
                    parts.append(f"{q(k)} = :{prefix}{i}")
                    params[f"{prefix}{i}"] = v
            return " AND ".join(parts), params

        with self.engine.begin() as conn:
            for keys in deletes:
                w, p = where_clause(keys, "k")
                affected += conn.execute(text(f"DELETE FROM {t} WHERE {w}"), p).rowcount
            for keys, values in updates:
                sets = ", ".join(f"{q(c)} = :v{i}" for i, c in enumerate(values))
                params = {f"v{i}": v for i, v in enumerate(values.values())}
                w, p = where_clause(keys, "k")
                rc = conn.execute(text(f"UPDATE {t} SET {sets} WHERE {w}"), {**params, **p}).rowcount
                if rc != 1:
                    raise RuntimeError(f"Update matched {rc} rows instead of 1; nothing was saved.")
                affected += rc
            for values in inserts:
                if values:
                    cols = ", ".join(q(c) for c in values)
                    ph = ", ".join(f":v{i}" for i in range(len(values)))
                    params = {f"v{i}": v for i, v in enumerate(values.values())}
                    conn.execute(text(f"INSERT INTO {t} ({cols}) VALUES ({ph})"), params)
                else:
                    conn.exec_driver_sql(self.driver.insert_default_row_sql(t))
                affected += 1
        return affected

    def iter_rows(self, table: str, schema: str | None, batch: int = 1000) -> tuple[list[str], Iterator[tuple]]:
        """Stream every row of a table (for export)."""
        conn = self.engine.connect().execution_options(stream_results=True)
        res = conn.exec_driver_sql(f"SELECT * FROM {self.driver.qualify(table, schema)}")
        cols = list(res.keys())

        def gen():
            try:
                while True:
                    chunk = res.fetchmany(batch)
                    if not chunk:
                        break
                    yield from (tuple(r) for r in chunk)
            finally:
                conn.close()

        return cols, gen()

    def insert_rows(self, table: str, schema: str | None, columns: list[str], rows: list[list]) -> int:
        q = self.driver.quote
        cols = ", ".join(q(c) for c in columns)
        ph = ", ".join(f":p{i}" for i in range(len(columns)))
        qualified = self.driver.qualify(table, schema)
        sql = text(f"INSERT INTO {qualified} ({cols}) VALUES ({ph})")
        self._about_to_write(f"import into {table}")
        with self.engine.begin() as conn:
            # explicit values for an auto-numbered column need help on some servers
            identity = [c for c in self._identity_columns(conn, table, schema) if c in columns]
            if identity:
                for stmt in self.driver.before_explicit_ids(qualified):
                    conn.exec_driver_sql(stmt)
            for start in range(0, len(rows), 500):
                chunk = rows[start : start + 500]
                conn.execute(sql, [{f"p{i}": v for i, v in enumerate(r)} for r in chunk])
            for col in identity:
                for stmt in self.driver.after_explicit_ids(table, schema, col):
                    conn.exec_driver_sql(stmt)
        return len(rows)

    def _identity_columns(self, conn, table: str, schema: str | None) -> list[str]:
        if not (self.driver.before_explicit_ids("x") or self.driver.after_explicit_ids("x", None, "x")):
            return []  # SQLite / MySQL accept explicit ids as-is
        from sqlalchemy import inspect as _inspect

        return [c["name"] for c in _inspect(conn).get_columns(table, schema=schema)
                if self.driver.is_auto_increment(conn, table, schema, c)]

    # ---- DDL --------------------------------------------------------------
    def execute_ddl(self, statements: list[str]) -> None:
        """Run builder-generated statements; roll back on failure where the DB allows."""
        self._about_to_write("change the schema")
        with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            wrap = self.driver.ddl_transactional and not any(s.upper() == "BEGIN" for s in statements)
            if wrap:
                statements = ["BEGIN", *statements, "COMMIT"]
            try:
                for s in statements:
                    conn.exec_driver_sql(s)
            except Exception:
                if self.driver.ddl_transactional:
                    for cleanup in ("ROLLBACK", *self.driver.ddl_cleanup):
                        try:
                            conn.exec_driver_sql(cleanup)
                        except Exception:
                            pass
                raise

    def execute(self, sql: str) -> None:
        if not is_read_only(sql):
            self._about_to_write(sql)
        with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.exec_driver_sql(sql)


def is_read_only(sql: str) -> bool:
    """Statements that never change data (best effort, used to decide when to snapshot)."""
    words = sql.lstrip().split(None, 1)
    head = words[0].upper().strip("(") if words else ""
    if head in ("SELECT", "EXPLAIN", "SHOW", "DESCRIBE", "DESC", "VALUES"):
        return True
    if head == "WITH":
        upper = sql.upper()
        return not any(k in upper for k in ("INSERT ", "UPDATE ", "DELETE ", "REPLACE "))
    if head == "PRAGMA":
        return "=" not in sql
    return False


def _all_kinds():
    from sqlalchemy.engine.reflection import ObjectKind

    return ObjectKind.ANY


class QueryConnection:
    """A dedicated autocommit connection for one SQL editor tab.

    Keeping it open preserves session state such as `USE db`, temp tables and
    manual transactions between runs, and gives us something to cancel.
    """

    def __init__(self, session: DatabaseSession):
        self.session = session
        self._conn = None
        self._conn_id = None
        self._lock = threading.Lock()
        self._cancelled = False
        self._timed_out = False

    def _ensure(self):
        if self._conn is None or self._conn.closed or self._conn.invalidated:
            self._conn = self.session.engine.connect().execution_options(isolation_level="AUTOCOMMIT")
            self._conn_id = self.session.driver.connection_id(self._conn)
        return self._conn

    def run(self, script: str, max_rows: int = 5000, stop_on_error: bool = True) -> list[StatementResult]:
        stmts = split_sql(script, self.session.driver.hash_comments)
        results: list[StatementResult] = []
        writes = [st.text for st in stmts if not is_read_only(st.text)]
        with self._lock:
            self._cancelled = False
            if writes:
                self.session._about_to_write(writes[0] if len(writes) == 1 else f"{len(writes)} statements")
            timer = None
            if self.session.time_limit:
                import threading as _t

                timer = _t.Timer(self.session.time_limit, self._time_up)
                timer.daemon = True
                timer.start()
            for st in stmts:
                if self._cancelled:
                    break
                r = StatementResult(sql=st.text)
                start = time.perf_counter()
                try:
                    conn = self._ensure()
                    res = conn.exec_driver_sql(st.text)
                    if res.returns_rows:
                        r.returns_rows = True
                        r.columns = list(res.keys())
                        rows = res.fetchmany(max_rows + 1)
                        r.truncated = len(rows) > max_rows
                        r.rows = [tuple(x) for x in rows[:max_rows]]
                        res.close()
                        r.rowcount = len(r.rows)
                    else:
                        r.rowcount = res.rowcount
                except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
                    r.error = _error_text(exc)
                    if getattr(exc, "connection_invalidated", False):
                        self._conn = None
                r.elapsed = time.perf_counter() - start
                results.append(r)
                if r.error and stop_on_error:
                    break
            if timer is not None:
                timer.cancel()
            if self._timed_out:
                for r in results:
                    if r.error and "interrupt" in r.error.lower():
                        r.error = (f"TimeLimit: stopped after {self.session.time_limit:g} seconds. In the playground long queries are "
                                   "stopped automatically, which often means a JOIN is missing its ON condition.")
                self._timed_out = False
        return results

    def _time_up(self) -> None:
        self._timed_out = True
        try:
            self.cancel()
        except Exception:
            pass

    def cancel(self) -> None:
        self._cancelled = True
        if self._conn is not None and self._conn_id is not None:
            self.session.driver.cancel(self.session.engine, self._conn, self._conn_id)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None


def _error_text(exc: Exception) -> str:
    orig = getattr(exc, "orig", None)
    if orig is not None:
        return f"{type(orig).__name__}: {orig}"
    return f"{type(exc).__name__}: {exc}"
