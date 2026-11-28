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
    explain_prefix: str | None = "EXPLAIN "  # None = no query-plan support from the editor
    container_label = "Database"  # what the tree's second level is called (database or schema)
    named_unique_constraints = False  # UNIQUE constraints are dropped with DROP CONSTRAINT (Postgres / SQL Server)
    length_fn = "LENGTH"  # byte length of a BLOB value
    system_schemas: set[str] = set()
    common_types: list[str] = []
    extra_keywords: list[str] = []

    # ---- connecting -------------------------------------------------------
    def url(self, cfg: ConnectionConfig, password: str | None) -> URL:
        raise NotImplementedError

    def engine_kwargs(self, cfg: ConnectionConfig) -> dict[str, Any]:
        return {}

    def install_hooks(self, engine, cfg=None) -> None:
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

    # ---- dialect differences used across the app -------------------------------
    def select_limit(self, select_sql: str, limit: int, offset: int = 0, order_sql: str = "") -> str:
        """`select_sql` + ORDER BY + row limit, in this dialect (LIMIT/OFFSET by default)."""
        sql = select_sql.rstrip().rstrip(";") + (f"\n{order_sql}" if order_sql else "") + f"\nLIMIT {int(limit)}"
        return sql + (f" OFFSET {int(offset)}" if offset else "")

    schema_required = False  # MySQL: tools need a database chosen; Postgres/SQL Server fall back to public/dbo
    tree_default_schema: str | None = None  # schema to expand automatically in the sidebar

    def default_schema(self, cfg) -> str | None:
        """Schema to use when none is selected (None = the server's default, e.g. public / dbo)."""
        return None

    def use_schema_sql(self, schema: str) -> str | None:
        """Make `schema` the default for unqualified names in this session (None if unsupported)."""
        return None

    def insert_default_row_sql(self, qualified_table: str) -> str:
        return f"INSERT INTO {qualified_table} DEFAULT VALUES"

    def before_explicit_ids(self, table_sql: str) -> list[str]:
        """Statements needed before inserting explicit values into an auto-numbered column."""
        return []

    def after_explicit_ids(self, table: str, schema: str | None, column: str) -> list[str]:
        """Statements to run after inserting explicit ids (e.g. move a sequence past them)."""
        return []
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
            if ix.get("duplicates_constraint"):
                continue  # the index behind a UNIQUE constraint; listed below as the constraint
            seen.add(ix["name"])
            indexes.append(
                IndexDef(ix["name"], list(ix["column_names"]), bool(ix.get("unique")), ix["name"])
            )
        for uc in insp.get_unique_constraints(table, schema=schema):
            name = uc.get("name")
            uc_cols = list(uc["column_names"])
            if name and name not in seen:
                indexes.append(IndexDef(name, uc_cols, True, name, constraint=self.named_unique_constraints))
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


class GenericAlterMixin:
    """ALTER TABLE planning shared by drivers with one-change-per-statement DDL (Postgres, SQL Server).

    Subclasses provide the small per-dialect statement builders (_rename_table, _alter_type …).
    Order: drop links and indexes, drop the old PK, drop/rename/add/change columns,
    add the new PK, indexes and links, then rename the table.
    """

    def alter_table_sql(self, old: TableDef, new: TableDef) -> list[str]:
        from dbide.core.schema_model import normalize_type

        t = self.qualify(old.name, old.schema)
        out: list[str] = []
        old_cols = {c.name: c for c in old.columns}
        new_by_orig = {c.original_name: c for c in new.columns if c.original_name in old_cols}

        def orig(name: str) -> str:
            for c in new.columns:
                if c.name == name:
                    return c.original_name or name
            return name

        # 1. foreign keys and indexes that change or go away
        old_fk = {f.original_name: f for f in old.foreign_keys if f.original_name}
        keep_fk = set()
        for fk in new.foreign_keys:
            o = old_fk.get(fk.original_name) if fk.original_name else None
            if o and fk.name == o.name and [orig(c) for c in fk.columns] == o.columns and (
                    fk.ref_table, fk.ref_columns, fk.on_delete, fk.on_update) == (o.ref_table, o.ref_columns, o.on_delete, o.on_update):
                keep_fk.add(o.original_name)
        for name in old_fk:
            if name not in keep_fk:
                out.append(f"ALTER TABLE {t} DROP CONSTRAINT {self.quote(name)}")
        old_ix = {i.original_name: i for i in old.indexes if i.original_name}
        keep_ix = set()
        for ix in new.indexes:
            o = old_ix.get(ix.original_name) if ix.original_name else None
            if o and ix.name == o.name and [orig(c) for c in ix.columns] == o.columns and ix.unique == o.unique:
                keep_ix.add(o.original_name)
        for name, ix in old_ix.items():
            if name not in keep_ix:
                out.append(f"ALTER TABLE {t} DROP CONSTRAINT {self.quote(name)}" if ix.constraint else self._drop_index(name, old))
        new_pk = [orig(c) for c in new.primary_key]
        pk_changed = new_pk != old.primary_key
        if pk_changed and old.primary_key:
            out.append(self._drop_pk(old))

        # 2. columns
        for name in old_cols:
            if name not in new_by_orig:
                out.append(f"ALTER TABLE {t} DROP COLUMN {self.quote(name)}")
        for c in new.columns:
            o = old_cols.get(c.original_name) if c.original_name else None
            if o is None:
                out.append(self._add_column(t, c, new))
                continue
            if c.name != o.name:
                out += self._rename_column(old, o.name, c.name)
            ref = self.quote(c.name)
            if normalize_type(c.type) != normalize_type(o.type):
                out += self._alter_type(t, ref, c)
            if c.auto_increment != o.auto_increment:
                out += self._alter_identity(t, ref, c)
            nullable = c.nullable and not c.primary_key
            if nullable != (o.nullable and not o.primary_key):
                out += self._alter_null(t, ref, c, nullable)
            if (c.default or None) != (o.default or None):
                out += self._alter_default(t, old, c, o)

        # 3. new primary key, indexes, links
        if pk_changed and new.primary_key:
            cols = ", ".join(self.quote(c) for c in new.primary_key)
            out.append(f"ALTER TABLE {t} ADD CONSTRAINT {self.quote('pk_' + new.name)} PRIMARY KEY ({cols})")
        for ix in new.indexes:
            if ix.original_name in keep_ix:
                continue
            cols = ", ".join(self.quote(c) for c in ix.columns)
            if ix.constraint:  # a UNIQUE constraint (as opposed to a unique index)
                out.append(f"ALTER TABLE {t} ADD CONSTRAINT {self.quote(ix.name)} UNIQUE ({cols})")
            else:
                out.append(self.create_index_sql(ix, old))
        for fk in new.foreign_keys:
            if fk.original_name not in keep_fk:
                out.append(f"ALTER TABLE {t} ADD {self.fk_sql(fk, new.schema)}")
        if new.name != old.name:
            out += self._rename_table(old, new.name)
        return out

    # per-dialect pieces
    def _add_column(self, t, c, td):
        raise NotImplementedError

    def _rename_column(self, td, old, new):
        raise NotImplementedError

    def _rename_table(self, td, new):
        raise NotImplementedError

    def _alter_type(self, t, ref, c):
        raise NotImplementedError

    def _alter_null(self, t, ref, c, nullable):
        raise NotImplementedError

    def _alter_default(self, t, td, c, o):
        raise NotImplementedError

    def _alter_identity(self, t, ref, c):
        raise NotImplementedError

    def _drop_pk(self, td):
        raise NotImplementedError

    def _drop_index(self, name, td):
        raise NotImplementedError
