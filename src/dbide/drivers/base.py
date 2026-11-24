"""Driver abstraction. Each database engine supplies one Driver subclass.

The rest of the app talks to databases through SQLAlchemy Core plus the hooks
here (URL building, identifier quoting, DDL generation, query cancellation), so
adding an engine such as PostgreSQL means adding one module in this package.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, Inspector
from sqlalchemy.engine import URL

from dbide.core.config import ConnectionConfig
from dbide.core.schema_model import ColumnDef, ForeignKeyDef, IndexDef, TableDef

SQL_KEYWORDS = """
ADD ALL ALTER AND ANY AS ASC AUTO_INCREMENT AUTOINCREMENT BEGIN BETWEEN BY CASCADE CASE
CHECK COLUMN COMMIT CONSTRAINT CREATE CROSS CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP
DATABASE DEFAULT DELETE DESC DISTINCT DROP ELSE END ESCAPE EXCEPT EXISTS EXPLAIN FOREIGN
FROM FULL GROUP HAVING IF IN INDEX INNER INSERT INTERSECT INTO IS JOIN KEY LEFT LIKE LIMIT
NOT NULL OFFSET ON OR ORDER OUTER PRIMARY REFERENCES RENAME REPLACE RETURNING RIGHT
ROLLBACK SELECT SET TABLE THEN TO TRANSACTION TRIGGER TRUNCATE UNION UNIQUE UPDATE USING
VALUES VIEW WHEN WHERE WITH
""".split()

SQL_FUNCTIONS = """
ABS AVG CAST COALESCE CONCAT COUNT DATE DATETIME IFNULL LENGTH LOWER MAX MIN NOW NULLIF
ROUND SUBSTR SUBSTRING SUM TRIM UPPER
""".split()


class Driver:
    key = ""
    label = ""
    default_port: int | None = None
    file_based = False
    hash_comments = False  # '#' starts a line comment
    supports_databases = False  # server holds multiple databases shown in the tree
    ddl_transactional = False  # DDL can be rolled back as a unit
    ddl_cleanup: list[str] = []
    explain_prefix = "EXPLAIN "  # statements that restore session settings after a failed DDL batch
    system_schemas: set[str] = set()
    common_types: list[str] = []
    extra_keywords: list[str] = []

    # ---- connecting -------------------------------------------------------
    def url(self, cfg: ConnectionConfig, password: str | None) -> URL:
        raise NotImplementedError

    def engine_kwargs(self, cfg: ConnectionConfig) -> dict[str, Any]:
        return {}

    def install_hooks(self, engine) -> None:
        """Attach SQLAlchemy event listeners (pragmas etc.)."""

    def connection_id(self, conn: Connection) -> Any:
        return None

    def cancel(self, engine, conn: Connection, conn_id: Any) -> None:
        raise NotImplementedError("Cancelling queries is not supported for this database.")

    # ---- naming -----------------------------------------------------------
    quote_char = '"'

    def quote(self, name: str) -> str:
        q = self.quote_char
        return f"{q}{name.replace(q, q + q)}{q}"

    def qualify(self, name: str, schema: str | None = None) -> str:
        return f"{self.quote(schema)}.{self.quote(name)}" if schema else self.quote(name)

    def literal(self, value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    # ---- introspection ----------------------------------------------------
    def list_databases(self, insp: Inspector) -> list[str]:
        return []

    def show_create(self, conn: Connection, table: str, schema: str | None) -> str:
        raise NotImplementedError

    def is_auto_increment(self, conn: Connection, table: str, schema: str | None, col: dict) -> bool:
        return col.get("autoincrement") is True

    def table_options(self, conn: Connection, table: str, schema: str | None) -> dict:
        return {}

    def load_table_def(self, insp: Inspector, conn: Connection, table: str, schema: str | None) -> TableDef:
        dialect = conn.dialect
        pk = insp.get_pk_constraint(table, schema=schema) or {}
        pk_cols = set(pk.get("constrained_columns") or [])
        cols = []
        for c in insp.get_columns(table, schema=schema):
            try:
                type_str = c["type"].compile(dialect=dialect)
            except Exception:
                type_str = str(c["type"])
            default = c.get("default")
            cols.append(
                ColumnDef(
                    name=c["name"],
                    type=type_str,
                    nullable=bool(c.get("nullable", True)) and c["name"] not in pk_cols,
                    primary_key=c["name"] in pk_cols,
                    auto_increment=self.is_auto_increment(conn, table, schema, c),
                    default=None if default is None else str(default),
                    comment=c.get("comment") or "",
                    original_name=c["name"],
                )
            )
        indexes = []
        seen = set()
        for ix in insp.get_indexes(table, schema=schema):
            if not ix.get("name") or any(col is None for col in ix["column_names"]):
                continue  # expression indexes are not editable here
            seen.add(ix["name"])
            indexes.append(
                IndexDef(ix["name"], list(ix["column_names"]), bool(ix.get("unique")), ix["name"])
            )
        for uc in insp.get_unique_constraints(table, schema=schema):
            name = uc.get("name")
            uc_cols = list(uc["column_names"])
            if name and name not in seen:
                indexes.append(IndexDef(name, uc_cols, True, name))
            elif not name:
                # e.g. SQLite `email TEXT UNIQUE`: show it so a rebuild keeps it
                gen = f"uq_{table}_{'_'.join(uc_cols)}"
                indexes.append(IndexDef(gen, uc_cols, True, gen, constraint=True))
        fks = []
        for i, fk in enumerate(insp.get_foreign_keys(table, schema=schema)):
            opts = fk.get("options") or {}
            name = fk.get("name") or ""
            fks.append(
                ForeignKeyDef(
                    name=name or f"fk_{table}_{i + 1}",
                    columns=list(fk["constrained_columns"]),
                    ref_table=fk["referred_table"],
                    ref_columns=list(fk["referred_columns"]),
                    on_delete=(opts.get("ondelete") or "").upper(),
                    on_update=(opts.get("onupdate") or "").upper(),
                    original_name=name or None,
                )
            )
        return TableDef(
            name=table,
            schema=schema,
            columns=cols,
            indexes=indexes,
            foreign_keys=fks,
            options=self.table_options(conn, table, schema),
            original_name=table,
        )

    # ---- DDL generation ---------------------------------------------------
    def column_sql(self, col: ColumnDef, td: TableDef) -> str:
        raise NotImplementedError

    def fk_sql(self, fk: ForeignKeyDef, schema: str | None) -> str:
        cols = ", ".join(self.quote(c) for c in fk.columns)
        refs = ", ".join(self.quote(c) for c in fk.ref_columns)
        sql = ""
        if fk.name:
            sql += f"CONSTRAINT {self.quote(fk.name)} "
        sql += f"FOREIGN KEY ({cols}) REFERENCES {self.qualify(fk.ref_table, schema)} ({refs})"
        if fk.on_delete:
            sql += f" ON DELETE {fk.on_delete}"
        if fk.on_update:
            sql += f" ON UPDATE {fk.on_update}"
        return sql

    def create_index_sql(self, ix: IndexDef, td: TableDef) -> str:
        cols = ", ".join(self.quote(c) for c in ix.columns)
        unique = "UNIQUE " if ix.unique else ""
        return f"CREATE {unique}INDEX {self.quote(ix.name)} ON {self.qualify(td.name, td.schema)} ({cols})"

    def create_table_body(self, td: TableDef) -> list[str]:
        raise NotImplementedError

    def create_table_suffix(self, td: TableDef) -> str:
        return ""

    def create_table_sql(self, td: TableDef) -> list[str]:
        body = ",\n  ".join(self.create_table_body(td))
        stmt = f"CREATE TABLE {self.qualify(td.name, td.schema)} (\n  {body}\n){self.create_table_suffix(td)}"
        return [stmt] + [self.create_index_sql(ix, td) for ix in td.indexes]

    def alter_table_sql(self, old: TableDef, new: TableDef) -> list[str]:
        raise NotImplementedError

    def drop_table_sql(self, name: str, schema: str | None, view: bool = False) -> str:
        return f"DROP {'VIEW' if view else 'TABLE'} {self.qualify(name, schema)}"

    def truncate_table_sql(self, name: str, schema: str | None) -> str:
        return f"DELETE FROM {self.qualify(name, schema)}"

    def create_database_sql(self, name: str) -> str:
        raise NotImplementedError("This database type does not support creating databases.")

    def drop_database_sql(self, name: str) -> str:
        raise NotImplementedError

    def keywords(self) -> list[str]:
        return SQL_KEYWORDS + self.extra_keywords

    def functions(self) -> list[str]:
        return SQL_FUNCTIONS
