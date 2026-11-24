from __future__ import annotations

from sqlalchemy import Connection
from sqlalchemy.engine import URL

from dbide.core.config import ConnectionConfig
from dbide.core.schema_model import ColumnDef, TableDef, column_changed

from .base import Driver


class MySQLDriver(Driver):
    """MySQL and MariaDB via PyMySQL (pure Python, no client library needed)."""

    key = "mysql"
    label = "MySQL / MariaDB"
    default_port = 3306
    hash_comments = True
    supports_databases = True
    quote_char = "`"
    system_schemas = {"information_schema", "performance_schema", "mysql", "sys"}
    common_types = [
        "INT", "BIGINT", "TINYINT(1)", "SMALLINT", "DECIMAL(10,2)", "FLOAT", "DOUBLE",
        "VARCHAR(255)", "CHAR(36)", "TEXT", "MEDIUMTEXT", "LONGTEXT", "JSON",
        "DATE", "DATETIME", "TIMESTAMP", "TIME", "BLOB", "LONGBLOB", "ENUM('a','b')",
    ]
    extra_keywords = [
        "AFTER", "CHANGE", "CHARSET", "COLLATE", "DATABASES", "DESCRIBE", "ENGINE", "FIRST",
        "MODIFY", "SCHEMA", "SHOW", "TABLES", "UNSIGNED", "USE", "ZEROFILL", "KILL", "STATUS",
        "PROCESSLIST", "VARIABLES", "GRANT", "REVOKE",
    ]

    def url(self, cfg: ConnectionConfig, password: str | None) -> URL:
        return URL.create(
            "mysql+pymysql",
            username=cfg.user or None,
            password=password or None,
            host=cfg.host or "localhost",
            port=cfg.port or self.default_port,
            database=cfg.database or None,
            query={"charset": cfg.options.get("charset") or "utf8mb4"},
        )

    def engine_kwargs(self, cfg: ConnectionConfig) -> dict:
        connect_args = {"connect_timeout": 10}
        if cfg.options.get("ssl"):
            connect_args["ssl"] = {"check_hostname": False}
        return {"pool_pre_ping": True, "pool_recycle": 3600, "connect_args": connect_args}

    def connection_id(self, conn: Connection):
        return conn.exec_driver_sql("SELECT CONNECTION_ID()").scalar()

    def cancel(self, engine, conn: Connection, conn_id) -> None:
        with engine.connect() as other:
            other.exec_driver_sql(f"KILL QUERY {int(conn_id)}")

    def list_databases(self, insp) -> list[str]:
        names = insp.get_schema_names()
        user = sorted(n for n in names if n.lower() not in self.system_schemas)
        system = sorted(n for n in names if n.lower() in self.system_schemas)
        return user + system

    def show_create(self, conn: Connection, table: str, schema: str | None) -> str:
        row = conn.exec_driver_sql(f"SHOW CREATE TABLE {self.qualify(table, schema)}").fetchone()
        return (row[1] if row else "") + ";"

    def table_options(self, conn, table, schema) -> dict:
        row = conn.exec_driver_sql(
            "SELECT ENGINE, TABLE_COLLATION, TABLE_COMMENT FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = COALESCE(%s, DATABASE()) AND TABLE_NAME = %s",
            (schema, table),
        ).fetchone()
        if not row:
            return {}
        return {"engine": row[0] or "", "collation": row[1] or "", "comment": row[2] or ""}

    # ---- DDL -------------------------------------------------------------
    def column_sql(self, col: ColumnDef, td: TableDef) -> str:
        parts = [self.quote(col.name), col.type.strip()]
        parts.append("NOT NULL" if (not col.nullable or col.primary_key) else "NULL")
        if col.default not in (None, ""):
            parts.append(f"DEFAULT {col.default.strip()}")
        if col.auto_increment:
            parts.append("AUTO_INCREMENT")
        if col.comment:
            parts.append(f"COMMENT {self.literal(col.comment)}")
        return " ".join(parts)

    def create_table_body(self, td: TableDef) -> list[str]:
        lines = [self.column_sql(c, td) for c in td.columns]
        if td.primary_key:
            lines.append(f"PRIMARY KEY ({', '.join(self.quote(c) for c in td.primary_key)})")
        # indexes are emitted inline so AUTO_INCREMENT non-PK columns stay valid
        for ix in td.indexes:
            cols = ", ".join(self.quote(c) for c in ix.columns)
            lines.append(f"{'UNIQUE ' if ix.unique else ''}KEY {self.quote(ix.name)} ({cols})")
        lines += [self.fk_sql(fk, td.schema) for fk in td.foreign_keys]
        return lines

    def create_table_suffix(self, td: TableDef) -> str:
        opts = td.options
        parts = []
        if opts.get("engine"):
            parts.append(f"ENGINE={opts['engine']}")
        if opts.get("collation"):
            charset = opts["collation"].split("_")[0]
            parts.append(f"DEFAULT CHARSET={charset} COLLATE={opts['collation']}")
        if opts.get("comment"):
            parts.append(f"COMMENT={self.literal(opts['comment'])}")
        return (" " + " ".join(parts)) if parts else ""

    def create_table_sql(self, td: TableDef) -> list[str]:
        body = ",\n  ".join(self.create_table_body(td))
        return [f"CREATE TABLE {self.qualify(td.name, td.schema)} (\n  {body}\n){self.create_table_suffix(td)}"]

    def alter_table_sql(self, old: TableDef, new: TableDef) -> list[str]:
        t = self.qualify(old.name, old.schema)
        stmts: list[str] = []
        clause = lambda s: stmts.append(f"ALTER TABLE {t} {s}")  # noqa: E731

        old_cols = {c.name: c for c in old.columns}
        new_by_orig = {c.original_name: c for c in new.columns if c.original_name in old_cols}

        def orig(name: str) -> str:
            for c in new.columns:
                if c.name == name:
                    return c.original_name or name
            return name

        # 1. drop changed/removed foreign keys and indexes first
        old_fk = {f.original_name: f for f in old.foreign_keys if f.original_name}
        keep_fk = set()
        for fk in new.foreign_keys:
            o = old_fk.get(fk.original_name) if fk.original_name else None
            if o and fk.name == o.name and [orig(c) for c in fk.columns] == o.columns and (
                fk.ref_table, fk.ref_columns, fk.on_delete, fk.on_update
            ) == (o.ref_table, o.ref_columns, o.on_delete, o.on_update):
                keep_fk.add(o.original_name)
        for name in old_fk:
            if name not in keep_fk:
                clause(f"DROP FOREIGN KEY {self.quote(name)}")

        old_ix = {i.original_name: i for i in old.indexes if i.original_name}
        keep_ix = set()
        for ix in new.indexes:
            o = old_ix.get(ix.original_name) if ix.original_name else None
            if o and ix.name == o.name and [orig(c) for c in ix.columns] == o.columns and ix.unique == o.unique:
                keep_ix.add(o.original_name)
        for name in old_ix:
            if name not in keep_ix:
                clause(f"DROP INDEX {self.quote(name)}")

        new_pk_orig = [orig(c) for c in new.primary_key]
        pk_changed = new_pk_orig != old.primary_key
        if pk_changed and old.primary_key:
            # AUTO_INCREMENT columns must stay keyed; strip it before dropping the PK
            for name in old.primary_key:
                oc = old_cols[name]
                if oc.auto_increment:
                    tmp = ColumnDef(**{**oc.__dict__, "auto_increment": False})
                    clause(f"MODIFY COLUMN {self.column_sql(tmp, old)}")
            clause("DROP PRIMARY KEY")

        # 2. columns
        for name in old_cols:
            if name not in new_by_orig:
                clause(f"DROP COLUMN {self.quote(name)}")
        prev = None
        old_order = [c.name for c in old.columns if c.name in new_by_orig]
        new_order = [c.original_name for c in new.columns if c.original_name in old_cols]
        reordered = old_order != new_order
        for c in new.columns:
            position = f" AFTER {self.quote(prev)}" if prev else " FIRST"
            col = c
            if pk_changed and c.auto_increment:
                col = ColumnDef(**{**c.__dict__, "auto_increment": False})  # restored after ADD PRIMARY KEY
            if c.original_name in old_cols:
                o = old_cols[c.original_name]
                if c.name != o.name or column_changed(o, col) or reordered or (c.primary_key and o.nullable):
                    clause(
                        f"CHANGE COLUMN {self.quote(o.name)} {self.column_sql(col, new)}"
                        + (position if reordered else "")
                    )
            else:
                clause(f"ADD COLUMN {self.column_sql(col, new)}{position}")
            prev = c.name

        # 3. add primary key, indexes, foreign keys
        if pk_changed and new.primary_key:
            clause(f"ADD PRIMARY KEY ({', '.join(self.quote(c) for c in new.primary_key)})")
            for c in new.columns:
                if c.auto_increment:
                    clause(f"MODIFY COLUMN {self.column_sql(c, new)}")
        for ix in new.indexes:
            if ix.original_name not in keep_ix:
                cols = ", ".join(self.quote(c) for c in ix.columns)
                clause(f"ADD {'UNIQUE ' if ix.unique else ''}INDEX {self.quote(ix.name)} ({cols})")
        for fk in new.foreign_keys:
            if fk.original_name not in keep_fk:
                clause(f"ADD {self.fk_sql(fk, new.schema)}")

        # 4. table options and rename
        if new.options.get("engine") and new.options.get("engine") != old.options.get("engine"):
            clause(f"ENGINE={new.options['engine']}")
        if (new.options.get("comment") or "") != (old.options.get("comment") or ""):
            clause(f"COMMENT={self.literal(new.options.get('comment') or '')}")
        if new.name != old.name:
            stmts.append(f"RENAME TABLE {t} TO {self.qualify(new.name, new.schema)}")
        return stmts

    def truncate_table_sql(self, name: str, schema: str | None) -> str:
        return f"TRUNCATE TABLE {self.qualify(name, schema)}"

    def create_database_sql(self, name: str) -> str:
        return f"CREATE DATABASE {self.quote(name)} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"

    def drop_database_sql(self, name: str) -> str:
        return f"DROP DATABASE {self.quote(name)}"

    def literal(self, value: str) -> str:
        return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"
