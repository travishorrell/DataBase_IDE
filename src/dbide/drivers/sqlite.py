from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from sqlalchemy import Connection, event
from sqlalchemy.engine import URL

from dbide.core.config import ConnectionConfig
from dbide.core.schema_model import ColumnDef, TableDef, column_changed, normalize_type

from .base import Driver


class SQLiteDriver(Driver):
    key = "sqlite"
    label = "SQLite"
    file_based = True
    ddl_transactional = True
    explain_prefix = "EXPLAIN QUERY PLAN "
    ddl_cleanup = ["PRAGMA foreign_keys = ON", "PRAGMA legacy_alter_table = OFF"]
    common_types = [
        "INTEGER", "TEXT", "REAL", "NUMERIC", "BLOB", "BOOLEAN", "DATE", "DATETIME",
        "VARCHAR(255)", "DECIMAL(10,2)",
    ]
    extra_keywords = ["AUTOINCREMENT", "PRAGMA", "VACUUM", "ATTACH", "DETACH", "GLOB", "WITHOUT", "ROWID"]

    def url(self, cfg: ConnectionConfig, password: str | None) -> URL:
        path = str(Path(cfg.database).expanduser())
        return URL.create("sqlite+pysqlite", database=path)

    def engine_kwargs(self, cfg: ConnectionConfig) -> dict:
        return {"connect_args": {"check_same_thread": False, "timeout": 10}}

    def install_hooks(self, engine, cfg=None) -> None:
        sandbox = bool(cfg is not None and cfg.options.get("sandbox"))

        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys = ON")
            cur.close()
            if sandbox:
                # the playground may only touch its own file: no ATTACH/DETACH of other databases
                deny = {getattr(sqlite3, "SQLITE_ATTACH", 24), getattr(sqlite3, "SQLITE_DETACH", 25)}

                def authorizer(action, *_args):
                    return sqlite3.SQLITE_DENY if action in deny else sqlite3.SQLITE_OK

                dbapi_conn.set_authorizer(authorizer)

    def connection_id(self, conn: Connection):
        return conn.connection.dbapi_connection

    def cancel(self, engine, conn: Connection, conn_id) -> None:
        conn_id.interrupt()  # sqlite3.Connection.interrupt is thread-safe

    def show_create(self, conn: Connection, table: str, schema: str | None) -> str:
        master = f"{self.quote(schema)}.sqlite_master" if schema else "sqlite_master"
        rows = conn.exec_driver_sql(
            f"SELECT sql FROM {master} WHERE tbl_name = ? AND sql IS NOT NULL "
            "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'view' THEN 0 ELSE 1 END",
            (table,),
        ).fetchall()
        return ";\n\n".join(r[0] for r in rows) + ";"

    def is_auto_increment(self, conn, table, schema, col) -> bool:
        sql = self._table_sql(conn, table, schema)
        if not sql or "AUTOINCREMENT" not in sql.upper():
            return False
        pattern = rf'["`\[]?{re.escape(col["name"])}["`\]]?\s+INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT'
        return re.search(pattern, sql, re.IGNORECASE) is not None

    def table_options(self, conn, table, schema) -> dict:
        master = f"{self.quote(schema)}.sqlite_master" if schema else "sqlite_master"
        rows = conn.exec_driver_sql(
            f"SELECT sql FROM {master} WHERE type='trigger' AND tbl_name=? AND sql IS NOT NULL", (table,)
        ).fetchall()
        return {"triggers": [r[0] for r in rows]}

    def _table_sql(self, conn, table, schema) -> str | None:
        master = f"{self.quote(schema)}.sqlite_master" if schema else "sqlite_master"
        row = conn.exec_driver_sql(
            f"SELECT sql FROM {master} WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return row[0] if row else None

    # ---- DDL -------------------------------------------------------------
    def _inline_pk(self, td: TableDef) -> str | None:
        pk = [c for c in td.columns if c.primary_key]
        if len(pk) == 1 and normalize_type(pk[0].type) in ("INTEGER", "INT") or (
            len(pk) == 1 and pk[0].auto_increment
        ):
            return pk[0].name
        return None

    def column_sql(self, col: ColumnDef, td: TableDef) -> str:
        parts = [self.quote(col.name), col.type.strip()]
        if self._inline_pk(td) == col.name:
            if normalize_type(col.type) != "INTEGER" and col.auto_increment:
                parts[1] = "INTEGER"  # AUTOINCREMENT only works on INTEGER PRIMARY KEY
            parts.append("PRIMARY KEY")
            if col.auto_increment:
                parts.append("AUTOINCREMENT")
        elif not col.nullable:
            parts.append("NOT NULL")
        if col.default not in (None, ""):
            parts.append(f"DEFAULT {self._default(col.default)}")
        return " ".join(parts)

    def _default(self, expr: str) -> str:
        e = expr.strip()
        # SQLite needs parentheses around expressions other than literals/keywords
        if re.fullmatch(r"-?\d+(\.\d+)?|'.*'|NULL|CURRENT_(DATE|TIME|TIMESTAMP)|TRUE|FALSE|\(.*\)", e, re.I | re.S):
            return e
        return f"({e})"

    def create_table_body(self, td: TableDef) -> list[str]:
        lines = [self.column_sql(c, td) for c in td.columns]
        pk = td.primary_key
        if pk and not self._inline_pk(td):
            lines.append(f"PRIMARY KEY ({', '.join(self.quote(c) for c in pk)})")
        lines += [self.fk_sql(fk, None) for fk in td.foreign_keys]
        return lines

    def create_index_sql(self, ix, td) -> str:
        # In SQLite the schema prefix belongs on the index name, not the table
        cols = ", ".join(self.quote(c) for c in ix.columns)
        unique = "UNIQUE " if ix.unique else ""
        return f"CREATE {unique}INDEX {self.qualify(ix.name, td.schema)} ON {self.quote(td.name)} ({cols})"

    def alter_table_sql(self, old: TableDef, new: TableDef) -> list[str]:
        simple = self._simple_alter(old, new)
        return simple if simple is not None else self._rebuild(old, new)

    def _simple_alter(self, old: TableDef, new: TableDef) -> list[str] | None:
        """ALTER TABLE statements, or None when SQLite needs a table rebuild."""
        old_cols = {c.name: c for c in old.columns}
        kept = [c for c in new.columns if c.original_name in old_cols]
        added = [c for c in new.columns if c.original_name not in old_cols]
        dropped = set(old_cols) - {c.original_name for c in kept}

        if [c.original_name for c in kept] != [n for n in (c.name for c in old.columns) if n not in dropped]:
            return None  # column order changed
        if added and new.columns[-len(added):] != added:
            return None  # new columns must be appended
        for c in kept:
            o = old_cols[c.original_name]
            if column_changed(o, c) or o.primary_key != c.primary_key:
                return None
        for c in added:
            if c.primary_key or (not c.nullable and c.default in (None, "")):
                return None
            if c.default and c.default.strip().upper().startswith("CURRENT_"):
                return None
        if self._fk_key(old) != self._fk_key(new, renames={c.name: c.original_name for c in kept}):
            return None
        new_constraints = {
            (ix.original_name, tuple(self._orig(new, c) for c in ix.columns)) for ix in new.indexes if ix.constraint
        }
        for ix in old.indexes:
            if ix.constraint and (ix.original_name, tuple(ix.columns)) not in new_constraints:
                return None  # inline UNIQUE constraints can only change via a rebuild
        index_cols = {col for ix in old.indexes for col in ix.columns}
        fk_cols = {col for fk in old.foreign_keys for col in fk.columns}
        if dropped & (index_cols | fk_cols | set(old.primary_key)):
            return None

        t = self.qualify(old.name, old.schema)
        stmts = []
        old_ix = {ix.original_name: ix for ix in old.indexes if ix.original_name}
        new_ix_by_orig = {ix.original_name: ix for ix in new.indexes if ix.original_name}
        for name, ix in old_ix.items():
            nx = new_ix_by_orig.get(name)
            renamed_cols = [self._orig(new, c) for c in nx.columns] if nx else None
            if not nx or nx.name != ix.name or renamed_cols != ix.columns or nx.unique != ix.unique:
                stmts.append(f"DROP INDEX {self.qualify(name, old.schema)}")
        for c in kept:
            if c.name != c.original_name:
                stmts.append(f"ALTER TABLE {t} RENAME COLUMN {self.quote(c.original_name)} TO {self.quote(c.name)}")
        for name in [c.name for c in old.columns if c.name in dropped]:
            stmts.append(f"ALTER TABLE {t} DROP COLUMN {self.quote(name)}")
        for c in added:
            stmts.append(f"ALTER TABLE {t} ADD COLUMN {self.column_sql(c, new)}")
        if new.name != old.name:
            stmts.append(f"ALTER TABLE {t} RENAME TO {self.quote(new.name)}")
        for ix in new.indexes:
            o = old_ix.get(ix.original_name) if ix.original_name else None
            if o and ix.name == o.name and [self._orig(new, c) for c in ix.columns] == o.columns and ix.unique == o.unique:
                continue
            stmts.append(self.create_index_sql(ix, new))
        return stmts

    @staticmethod
    def _orig(td: TableDef, col_name: str) -> str:
        for c in td.columns:
            if c.name == col_name:
                return c.original_name or c.name
        return col_name

    @staticmethod
    def _fk_key(td: TableDef, renames: dict | None = None):
        renames = renames or {}
        return sorted(
            (
                tuple(renames.get(c, c) for c in fk.columns),
                fk.ref_table,
                tuple(fk.ref_columns),
                fk.on_delete or "",
                fk.on_update or "",
            )
            for fk in td.foreign_keys
        )

    def _rebuild(self, old: TableDef, new: TableDef) -> list[str]:
        """The documented SQLite 'make a new table and copy' procedure."""
        tmp = new.clone()
        tmp.name = f"_dbide_new_{new.name}"
        tmp.indexes = []
        old_names = {c.name for c in old.columns}
        copy_pairs = [(c.name, c.original_name) for c in new.columns if c.original_name in old_names]
        # legacy_alter_table stops the final RENAME from failing on views that
        # reference the table while it is briefly missing
        stmts = ["PRAGMA foreign_keys = OFF", "PRAGMA legacy_alter_table = ON", "BEGIN"]
        stmts += self.create_table_sql(tmp)
        if copy_pairs:
            dst = ", ".join(self.quote(n) for n, _ in copy_pairs)
            src = ", ".join(self.quote(o) for _, o in copy_pairs)
            stmts.append(
                f"INSERT INTO {self.qualify(tmp.name, new.schema)} ({dst}) "
                f"SELECT {src} FROM {self.qualify(old.name, old.schema)}"
            )
        stmts.append(f"DROP TABLE {self.qualify(old.name, old.schema)}")
        stmts.append(f"ALTER TABLE {self.qualify(tmp.name, new.schema)} RENAME TO {self.quote(new.name)}")
        stmts += [self.create_index_sql(ix, new) for ix in new.indexes]
        if new.name == old.name:
            stmts += list(old.options.get("triggers", []))
        stmts += ["COMMIT", "PRAGMA legacy_alter_table = OFF", "PRAGMA foreign_keys = ON"]
        return stmts
