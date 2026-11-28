"""Microsoft SQL Server (and Azure SQL) via pymssql: no ODBC driver needed."""
from __future__ import annotations

import re

from sqlalchemy import Connection
from sqlalchemy.engine import URL

from dbide.core.config import ConnectionConfig
from dbide.core.schema_model import ColumnDef, TableDef

from .base import Driver, GenericAlterMixin


class MSSQLDriver(GenericAlterMixin, Driver):
    key = "mssql"
    label = "Microsoft SQL Server"
    default_port = 1433
    supports_databases = True  # second level lists schemas (dbo, …) of the connection's database
    container_label = "Schema"
    tree_default_schema = "dbo"
    ddl_transactional = True
    named_unique_constraints = True
    explain_prefix = None  # SQL Server plans need SET SHOWPLAN in its own batch
    length_fn = "DATALENGTH"
    quote_char = "]"
    system_schemas = {"sys", "INFORMATION_SCHEMA", "guest", "db_owner", "db_accessadmin", "db_securityadmin", "db_ddladmin",
                      "db_backupoperator", "db_datareader", "db_datawriter", "db_denydatareader", "db_denydatawriter"}
    common_types = [
        "INT", "BIGINT", "SMALLINT", "BIT", "DECIMAL(10,2)", "FLOAT", "NVARCHAR(255)", "NVARCHAR(MAX)", "VARCHAR(255)",
        "DATE", "DATETIME2", "DATETIMEOFFSET", "TIME", "UNIQUEIDENTIFIER", "VARBINARY(MAX)",
    ]
    extra_keywords = ["TOP", "IDENTITY", "NVARCHAR", "GO", "EXEC", "DECLARE", "BEGIN", "TRAN", "OUTPUT", "MERGE", "PIVOT",
                      "APPLY", "OFFSET", "FETCH", "NEXT", "ROWS", "ONLY", "SCHEMA", "PROCEDURE"]

    def quote(self, name: str) -> str:
        return "[" + name.replace("]", "]]") + "]"

    def literal(self, value: str) -> str:
        return "N'" + value.replace("'", "''") + "'"

    def url(self, cfg: ConnectionConfig, password: str | None) -> URL:
        return URL.create("mssql+pymssql", username=cfg.user or None, password=password or None, host=cfg.host or "localhost",
                          port=cfg.port or self.default_port, database=cfg.database or "master", query={"charset": "utf8"})

    def engine_kwargs(self, cfg: ConnectionConfig) -> dict:
        return {"pool_pre_ping": True, "connect_args": {"login_timeout": 10, "timeout": 0}}

    def connection_id(self, conn: Connection):
        return conn.exec_driver_sql("SELECT @@SPID").scalar()

    def cancel(self, engine, conn: Connection, conn_id) -> None:
        # SQL Server can't cancel another session's statement from outside; KILL ends that session
        # (the editor reconnects automatically on the next run).
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as other:
            other.exec_driver_sql(f"KILL {int(conn_id)}")

    def list_databases(self, insp) -> list[str]:
        names = insp.get_schema_names()
        user = sorted(n for n in names if n not in self.system_schemas)
        return user + sorted(n for n in names if n in self.system_schemas)

    def select_limit(self, select_sql: str, limit: int, offset: int = 0, order_sql: str = "") -> str:
        body = select_sql.rstrip().rstrip(";")
        if offset or order_sql:
            return f"{body}\n{order_sql or 'ORDER BY (SELECT NULL)'}\nOFFSET {int(offset)} ROWS FETCH NEXT {int(limit)} ROWS ONLY"
        # no ordering: SELECT TOP n (keeps DISTINCT working)
        return re.sub(r"^\s*SELECT(\s+DISTINCT)?\s", lambda m: f"SELECT{m.group(1) or ''} TOP {int(limit)} ", body, count=1, flags=re.I)

    def insert_default_row_sql(self, qualified_table: str) -> str:
        return f"INSERT INTO {qualified_table} DEFAULT VALUES"

    def before_explicit_ids(self, table_sql: str) -> list[str]:
        return [f"SET IDENTITY_INSERT {table_sql} ON"]

    def after_explicit_ids(self, table: str, schema: str | None, column: str) -> list[str]:
        return [f"SET IDENTITY_INSERT {self.qualify(table, schema)} OFF"]

    def show_create(self, conn: Connection, table: str, schema: str | None) -> str:
        from sqlalchemy import inspect

        td = self.load_table_def(inspect(conn), conn, table, schema)
        return ";\n\n".join(self.create_table_sql(td)) + ";"

    def is_auto_increment(self, conn, table, schema, col) -> bool:
        return bool(col.get("identity")) or col.get("autoincrement") is True

    def load_table_def(self, insp, conn, table, schema) -> TableDef:
        td = super().load_table_def(insp, conn, table, schema)
        for c in td.columns:
            if c.default:
                c.default = _strip_parens(c.default)  # SQL Server stores ((0)) / ('paid')
        return td

    # ---- DDL -------------------------------------------------------------
    def column_sql(self, col: ColumnDef, td: TableDef) -> str:
        parts = [self.quote(col.name), col.type.strip()]
        if col.auto_increment:
            parts.append("IDENTITY(1,1)")
        parts.append("NOT NULL" if (not col.nullable or col.primary_key) else "NULL")
        if col.default not in (None, "") and not col.auto_increment:
            parts.append(f"CONSTRAINT {self.quote(f'df_{td.name}_{col.name}')} DEFAULT {col.default.strip()}")
        return " ".join(parts)

    def create_table_body(self, td: TableDef) -> list[str]:
        lines = [self.column_sql(c, td) for c in td.columns]
        if td.primary_key:
            lines.append(f"CONSTRAINT {self.quote('pk_' + td.name)} PRIMARY KEY ({', '.join(self.quote(c) for c in td.primary_key)})")
        lines += [self.fk_sql(fk, td.schema) for fk in td.foreign_keys]
        return lines

    def create_index_sql(self, ix, td) -> str:
        cols = ", ".join(self.quote(c) for c in ix.columns)
        return f"CREATE {'UNIQUE ' if ix.unique else ''}INDEX {self.quote(ix.name)} ON {self.qualify(td.name, td.schema)} ({cols})"

    def _obj(self, td: TableDef) -> str:
        return self.literal(f"{td.schema + '.' if td.schema else ''}{td.name}")

    def _add_column(self, t, c, td):
        return f"ALTER TABLE {t} ADD {self.column_sql(c, td)}"

    def _rename_column(self, td, old, new):
        obj = f"{td.schema + '.' if td.schema else ''}{td.name}.{old}"
        return [f"EXEC sp_rename {self.literal(obj)}, {self.literal(new)}, 'COLUMN'"]

    def _rename_table(self, td, new):
        return [f"EXEC sp_rename {self._obj(td)}, {self.literal(new)}"]

    def _alter_type(self, t, ref, c):
        return [f"ALTER TABLE {t} ALTER COLUMN {ref} {c.type} {'NULL' if c.nullable and not c.primary_key else 'NOT NULL'}"]

    def _alter_null(self, t, ref, c, nullable):
        return [f"ALTER TABLE {t} ALTER COLUMN {ref} {c.type} {'NULL' if nullable else 'NOT NULL'}"]

    def _alter_default(self, t, td, c, o):
        # defaults are named constraints: drop the current one (whatever its name), then add the new one
        out = [
            "DECLARE @df sysname = (SELECT dc.name FROM sys.default_constraints dc "
            f"WHERE dc.parent_object_id = OBJECT_ID({self._obj(td)}) "
            f"AND dc.parent_column_id = COLUMNPROPERTY(OBJECT_ID({self._obj(td)}), {self.literal(o.name)}, 'ColumnId'));\n"
            f"IF @df IS NOT NULL EXEC('ALTER TABLE {t.replace(chr(39), chr(39) * 2)} DROP CONSTRAINT [' + @df + ']')"
        ]
        if c.default not in (None, ""):
            out.append(f"ALTER TABLE {t} ADD CONSTRAINT {self.quote(f'df_{td.name}_{c.name}')} DEFAULT {c.default} FOR {self.quote(c.name)}")
        return out

    def _alter_identity(self, t, ref, c):
        raise ValueError("SQL Server can't add or remove IDENTITY on an existing column. Create a new column instead.")

    def _drop_pk(self, td):
        t = self.qualify(td.name, td.schema)
        return ("DECLARE @pk sysname = (SELECT name FROM sys.key_constraints "
                f"WHERE type = 'PK' AND parent_object_id = OBJECT_ID({self._obj(td)}));\n"
                f"IF @pk IS NOT NULL EXEC('ALTER TABLE {t.replace(chr(39), chr(39) * 2)} DROP CONSTRAINT [' + @pk + ']')")

    def _drop_index(self, name, td):
        return f"DROP INDEX {self.quote(name)} ON {self.qualify(td.name, td.schema)}"

    def truncate_table_sql(self, name: str, schema: str | None) -> str:
        return f"TRUNCATE TABLE {self.qualify(name, schema)}"

    def create_database_sql(self, name: str) -> str:
        return f"CREATE SCHEMA {self.quote(name)}"

    def drop_database_sql(self, name: str) -> str:
        return f"DROP SCHEMA {self.quote(name)}"


def _strip_parens(expr: str) -> str:
    e = expr.strip()
    while e.startswith("(") and e.endswith(")") and _balanced(e[1:-1]):
        e = e[1:-1].strip()
    return e


def _balanced(s: str) -> bool:
    depth = 0
    for ch in s:
        depth += ch == "("
        depth -= ch == ")"
        if depth < 0:
            return False
    return depth == 0
