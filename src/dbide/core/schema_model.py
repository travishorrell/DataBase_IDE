"""Plain data structures describing a table, used by the table builder and DDL generators."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

FK_ACTIONS = ["", "NO ACTION", "RESTRICT", "CASCADE", "SET NULL", "SET DEFAULT"]


@dataclass
class ColumnDef:
    name: str
    type: str = "TEXT"
    nullable: bool = True
    primary_key: bool = False
    auto_increment: bool = False
    default: str | None = None  # raw SQL expression, e.g. 0, 'abc', CURRENT_TIMESTAMP
    comment: str = ""
    original_name: str | None = None  # set when loaded from an existing table


@dataclass
class IndexDef:
    name: str
    columns: list[str]
    unique: bool = False
    original_name: str | None = None
    constraint: bool = False  # an unnamed UNIQUE constraint declared inside CREATE TABLE


@dataclass
class ForeignKeyDef:
    name: str
    columns: list[str]
    ref_table: str
    ref_columns: list[str]
    on_delete: str = ""
    on_update: str = ""
    original_name: str | None = None


@dataclass
class TableDef:
    name: str
    schema: str | None = None
    columns: list[ColumnDef] = field(default_factory=list)
    indexes: list[IndexDef] = field(default_factory=list)
    foreign_keys: list[ForeignKeyDef] = field(default_factory=list)
    options: dict = field(default_factory=dict)  # e.g. MySQL engine / charset / comment
    original_name: str | None = None

    @property
    def primary_key(self) -> list[str]:
        return [c.name for c in self.columns if c.primary_key]

    def clone(self) -> "TableDef":
        return copy.deepcopy(self)

    def validate(self) -> list[str]:
        problems = []
        if not self.name.strip():
            problems.append("Table name is required.")
        if not self.columns:
            problems.append("A table needs at least one column.")
        names = [c.name for c in self.columns]
        for c in self.columns:
            if not c.name.strip():
                problems.append("Every column needs a name.")
            if not c.type.strip():
                problems.append(f"Column {c.name!r} needs a type.")
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            problems.append(f"Duplicate column names: {', '.join(sorted(dupes))}")
        known = set(names)
        for idx in self.indexes:
            if not idx.name.strip() or not idx.columns:
                problems.append("Every index needs a name and at least one column.")
            for col in idx.columns:
                if col not in known:
                    problems.append(f"Index {idx.name!r} uses unknown column {col!r}.")
        for fk in self.foreign_keys:
            if not fk.columns or not fk.ref_table or len(fk.columns) != len(fk.ref_columns):
                problems.append(
                    f"Foreign key {fk.name!r} needs columns, a referenced table, "
                    "and the same number of referenced columns."
                )
            for col in fk.columns:
                if col not in known:
                    problems.append(f"Foreign key {fk.name!r} uses unknown column {col!r}.")
        return list(dict.fromkeys(problems))


def normalize_type(t: str) -> str:
    return " ".join(t.upper().replace(" (", "(").split())


def column_changed(a: ColumnDef, b: ColumnDef) -> bool:
    return (
        normalize_type(a.type) != normalize_type(b.type)
        or a.nullable != b.nullable
        or a.auto_increment != b.auto_increment
        or (a.default or None) != (b.default or None)
        or (a.comment or "") != (b.comment or "")
    )
