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
        self.engine = create_engine(self.driver.url(cfg, password), **self.driver.engine_kwargs(cfg))
        self.driver.install_hooks(self.engine)
        self.server_version = ""

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
                f"SELECT * FROM {src}{where_sql}{order_sql} LIMIT {int(limit)} OFFSET {int(offset)}"
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
                    conn.exec_driver_sql(f"INSERT INTO {t} DEFAULT VALUES" if self.cfg.driver == "sqlite" else f"INSERT INTO {t} () VALUES ()")
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
        sql = text(f"INSERT INTO {self.driver.qualify(table, schema)} ({cols}) VALUES ({ph})")
        with self.engine.begin() as conn:
            for start in range(0, len(rows), 500):
                chunk = rows[start : start + 500]
                conn.execute(sql, [{f"p{i}": v for i, v in enumerate(r)} for r in chunk])
        return len(rows)

    # ---- DDL --------------------------------------------------------------
    def execute_ddl(self, statements: list[str]) -> None:
        """Run builder-generated statements; roll back on failure where the DB allows."""
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
        with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.exec_driver_sql(sql)


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

    def _ensure(self):
        if self._conn is None or self._conn.closed or self._conn.invalidated:
            self._conn = self.session.engine.connect().execution_options(isolation_level="AUTOCOMMIT")
            self._conn_id = self.session.driver.connection_id(self._conn)
        return self._conn

    def run(self, script: str, max_rows: int = 5000, stop_on_error: bool = True) -> list[StatementResult]:
        stmts = split_sql(script, self.session.driver.hash_comments)
        results: list[StatementResult] = []
        with self._lock:
            self._cancelled = False
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
        return results

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
