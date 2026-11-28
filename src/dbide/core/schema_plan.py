"""The schema designer's model: tables and relationships edited on a canvas,
turned into an ordered, explained list of DDL steps.

Relationships are created the way people think about them:
  one-to-many   "each order belongs to one customer"  → orders.customer_id + FK
  one-to-one    "each user has one profile"           → FK column that is also UNIQUE
  many-to-many  "students take many courses"         → a junction table with two FKs
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import copy

from dbide.core.nl2sql import singular
from dbide.core.roles import ColumnRole, suggested_type
from dbide.core.schema_model import ColumnDef, ForeignKeyDef, IndexDef, TableDef

REL_KINDS = {
    "one_to_many": "One-to-many",
    "one_to_one": "One-to-one",
    "many_to_many": "Many-to-many",
}


def safe_name(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_]+", "_", s.strip()).strip("_").lower()
    return s or "table"


@dataclass
class PlanStep:
    title: str  # plain English
    statements: list[str]
    table: str  # the table it concerns


class DesignerModel:
    def __init__(self, driver_key: str, originals: dict[str, TableDef] | None = None,
                 roles: dict[str, dict[str, ColumnRole]] | None = None):
        self.driver_key = driver_key
        # column roles (password / encrypted / media) live beside the SQL definition
        self.original_roles = {t: dict(r) for t, r in (roles or {}).items() if r}
        self.roles: dict[str, dict[str, ColumnRole]] = copy.deepcopy(self.original_roles)
        self.originals: dict[str, TableDef] = {k: v.clone() for k, v in (originals or {}).items()}
        self.tables: dict[str, TableDef] = {k: v.clone() for k, v in (originals or {}).items()}
        self.dropped: set[str] = set()  # original names
        self.log: list[str] = []  # human-readable change history

    # ---- helpers ----------------------------------------------------------------
    @property
    def id_type(self) -> str:
        return {"mysql": "INT", "mssql": "INT"}.get(self.driver_key, "INTEGER")

    @property
    def text_type(self) -> str:
        return {"mysql": "VARCHAR(255)", "mssql": "NVARCHAR(255)", "postgresql": "VARCHAR(255)"}.get(self.driver_key, "TEXT")

    def pk_of(self, table: str) -> list[ColumnDef]:
        return [c for c in self.tables[table].columns if c.primary_key]

    def unique_name(self, base: str) -> str:
        name, i = base, 2
        while name in self.tables:
            name, i = f"{base}_{i}", i + 1
        return name

    @property
    def roles_dirty(self) -> bool:
        clean = {t: r for t, r in self.roles.items() if r}
        return clean != self.original_roles

    @property
    def dirty(self) -> bool:
        return bool(self.plan_steps_raw()) or self.roles_dirty

    def set_role(self, table: str, column: str, role: ColumnRole | None) -> None:
        cols = self.roles.setdefault(table, {})
        if role is None:
            cols.pop(column, None)
        else:
            cols[column] = role

    # ---- edits ----------------------------------------------------------------
    def add_table(self, name: str) -> TableDef:
        name = self.unique_name(safe_name(name))
        td = TableDef(name=name, columns=[ColumnDef("id", self.id_type, nullable=False, primary_key=True, auto_increment=True)])
        if self.driver_key == "mysql":
            td.options = {"engine": "InnoDB"}
        self.tables[name] = td
        self.log.append(f"Create table {name}")
        return td

    def rename_table(self, old: str, new: str) -> str:
        new = safe_name(new)
        if new == old:
            return old
        if new in self.tables:
            raise ValueError(f"There's already a table called {new}.")
        td = self.tables.pop(old)
        td.name = new
        self.tables[new] = td
        if old in self.roles:
            self.roles[new] = self.roles.pop(old)
        for t in self.tables.values():
            for fk in t.foreign_keys:
                if fk.ref_table == old:
                    fk.ref_table = new
        self.log.append(f"Rename table {old} → {new}")
        return new

    def add_column(self, table: str, name: str, type_: str, nullable: bool = True, unique: bool = False,
                   default: str | None = None, role: ColumnRole | None = None) -> ColumnDef:
        td = self.tables[table]
        name = safe_name(name)
        if role is not None:
            type_ = suggested_type(role, self.driver_key)
        if any(c.name == name for c in td.columns):
            raise ValueError(f"{table} already has a column called {name}.")
        col = ColumnDef(name, type_, nullable=nullable, default=default or None)
        td.columns.append(col)
        if unique:
            td.indexes.append(IndexDef(f"ux_{table}_{name}", [name], True))
        if role is not None:
            self.set_role(table, name, role)
        self.log.append(f"Add column {table}.{name}" + (f" ({role.label()})" if role else ""))
        return col

    def drop_table(self, table: str) -> None:
        td = self.tables.pop(table)
        self.roles.pop(table, None)
        if td.original_name:
            self.dropped.add(td.original_name)
        for t in self.tables.values():  # links pointing at it go too
            t.foreign_keys = [fk for fk in t.foreign_keys if fk.ref_table != table]
        self.log.append(f"Delete table {table}")

    def remove_relationship(self, child: str, fk_name: str) -> None:
        td = self.tables[child]
        td.foreign_keys = [fk for fk in td.foreign_keys if fk.name != fk_name]
        self.log.append(f"Remove link {fk_name}")

    def default_fk_column(self, parent: str) -> str:
        pk = self.pk_of(parent)
        return f"{singular(parent)}_{pk[0].name if pk else 'id'}"

    def add_relationship(self, child: str, parent: str, kind: str, column: str | None = None,
                         required: bool = False, on_delete: str = "") -> str:
        """Create the columns/keys/tables for a relationship; returns a plain-English summary."""
        if kind == "many_to_many":
            return self._many_to_many(child, parent, on_delete or "CASCADE")
        ppk = self.pk_of(parent)
        if len(ppk) != 1:
            raise ValueError(f"{parent} needs a single-column primary key to be linked to.")
        ppk = ppk[0]
        ctd = self.tables[child]
        col_name = safe_name(column or self.default_fk_column(parent))
        col = next((c for c in ctd.columns if c.name == col_name), None)
        created = col is None
        if created:
            col = ColumnDef(col_name, ppk.type if "INT" in ppk.type.upper() else ppk.type, nullable=not required)
            ctd.columns.append(col)
        elif required:
            col.nullable = False
        fk_name = f"fk_{child}_{col_name}"
        if any(fk.columns == [col_name] for fk in ctd.foreign_keys):
            raise ValueError(f"{child}.{col_name} is already linked.")
        ctd.foreign_keys.append(ForeignKeyDef(fk_name, [col_name], parent, [ppk.name], on_delete=on_delete))
        if kind == "one_to_one":
            ctd.indexes.append(IndexDef(f"ux_{child}_{col_name}", [col_name], True))
        else:
            ctd.indexes.append(IndexDef(f"ix_{child}_{col_name}", [col_name], False))
        what = "has at most one" if kind == "one_to_one" else "can have many"
        summary = (f"Each {singular(child)} belongs to one {singular(parent)}; a {singular(parent)} {what} {child}. "
                   f"{'Added' if created else 'Using'} column {child}.{col_name} → {parent}.{ppk.name}.")
        self.log.append(summary)
        return summary

    def _many_to_many(self, a: str, b: str, on_delete: str) -> str:
        apk, bpk = self.pk_of(a), self.pk_of(b)
        if len(apk) != 1 or len(bpk) != 1:
            raise ValueError("Both tables need a single-column primary key.")
        name = self.unique_name(f"{a}_{b}")
        ca, cb = f"{singular(a)}_{apk[0].name}", f"{singular(b)}_{bpk[0].name}"
        if ca == cb:
            cb = f"related_{cb}"
        td = TableDef(
            name=name,
            columns=[ColumnDef(ca, apk[0].type, nullable=False, primary_key=True),
                     ColumnDef(cb, bpk[0].type, nullable=False, primary_key=True)],
            foreign_keys=[ForeignKeyDef(f"fk_{name}_{ca}", [ca], a, [apk[0].name], on_delete=on_delete),
                          ForeignKeyDef(f"fk_{name}_{cb}", [cb], b, [bpk[0].name], on_delete=on_delete)],
            indexes=[IndexDef(f"ix_{name}_{cb}", [cb], False)],
        )
        if self.driver_key == "mysql":
            td.options = {"engine": "InnoDB"}
        self.tables[name] = td
        summary = (f"Many {a} relate to many {b}. A database can't store that directly, so a junction table {name} "
                   f"holds one row per pair ({ca}, {cb}).")
        self.log.append(summary)
        return summary

    # ---- planning ---------------------------------------------------------------
    def _order_new(self, names: list[str]) -> list[str]:
        """Parents before children so foreign keys can be created."""
        out, seen = [], set()

        def visit(n, stack=()):
            if n in seen or n in stack:
                return
            for fk in self.tables[n].foreign_keys:
                if fk.ref_table in names and fk.ref_table != n:
                    visit(fk.ref_table, stack + (n,))
            seen.add(n)
            out.append(n)

        for n in names:
            visit(n)
        return out

    def plan_steps_raw(self) -> list[tuple[str, str, TableDef | None, TableDef | None]]:
        """(action, table, old, new) without SQL — cheap, used for 'dirty'."""
        steps = []
        new_names = [n for n, td in self.tables.items() if not td.original_name or td.original_name not in self.originals]
        for n in self._order_new(new_names):
            steps.append(("create", n, None, self.tables[n]))
        for n, td in self.tables.items():
            if td.original_name in self.originals and _changed(self.originals[td.original_name], td):
                steps.append(("alter", n, self.originals[td.original_name], td))
        for n in self.dropped:
            steps.append(("drop", n, self.originals.get(n), None))
        return steps

    def plan(self, driver) -> list[PlanStep]:
        out = []
        for action, name, old, new in self.plan_steps_raw():
            if action == "create":
                links = ", ".join(f"{fk.columns[0]} → {fk.ref_table}" for fk in new.foreign_keys)
                title = f"Create table {name} ({len(new.columns)} columns" + (f"; links {links}" if links else "") + ")"
                out.append(PlanStep(title, driver.create_table_sql(new), name))
            elif action == "alter":
                stmts = driver.alter_table_sql(old, new)
                if stmts:
                    out.append(PlanStep(_describe_alter(old, new), stmts, name))
            elif action == "drop":
                out.append(PlanStep(f"Delete table {name} and all its rows", [driver.drop_table_sql(name, None)], name))
        return out


def _changed(a: TableDef, b: TableDef) -> bool:
    def sig(t: TableDef):
        return (t.name, [(c.name, c.type, c.nullable, c.primary_key, c.auto_increment, c.default) for c in t.columns],
                sorted((i.name, tuple(i.columns), i.unique) for i in t.indexes),
                sorted((f.name, tuple(f.columns), f.ref_table, tuple(f.ref_columns), f.on_delete) for f in t.foreign_keys))
    return sig(a) != sig(b)


def _describe_alter(old: TableDef, new: TableDef) -> str:
    parts = []
    if old.name != new.name:
        parts.append(f"rename to {new.name}")
    oc = {c.name for c in old.columns}
    added = [c.name for c in new.columns if c.original_name is None and c.name not in oc]
    if added:
        parts.append("add " + ", ".join(added))
    ofk = {f.name for f in old.foreign_keys}
    links = [f"{f.columns[0]} → {f.ref_table}" for f in new.foreign_keys if f.name not in ofk]
    if links:
        parts.append("link " + ", ".join(links))
    gone = [f.name for f in old.foreign_keys if f.name not in {x.name for x in new.foreign_keys}]
    if gone:
        parts.append("remove link " + ", ".join(gone))
    return f"Change table {old.name}: " + ("; ".join(parts) or "update definition")
