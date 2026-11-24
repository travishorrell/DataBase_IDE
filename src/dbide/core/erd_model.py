"""Everything the ER diagram draws: tables, keys, relationships and media storage."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import inspect

from dbide.core import media
from dbide.core.roles import ColumnRole, guess_media


@dataclass
class ErdColumn:
    name: str
    type: str
    pk: bool = False
    fk: bool = False
    nullable: bool = True
    unique: bool = False
    role: ColumnRole | None = None
    media_guess: str | None = None  # 'blob'/'path' when there's no role but the column looks like media


@dataclass
class ErdRelation:
    child: str
    child_cols: list[str]
    parent: str
    parent_cols: list[str]
    name: str = ""
    one_to_one: bool = False  # FK columns are also unique / the PK
    on_delete: str = ""


@dataclass
class MediaStorage:
    table: str
    column: str
    storage: str  # blob | path
    guessed: bool
    rows: int = 0
    total_bytes: int = 0
    location: str = ""  # where BLOBs physically live (db file / server)
    folders: list[media.FolderReport] = field(default_factory=list)


@dataclass
class ErdModel:
    tables: dict[str, list[ErdColumn]]
    views: list[str]
    relations: list[ErdRelation]
    media: list[MediaStorage]
    location: str


def load_erd(session, schema: str | None, roles: dict[str, dict[str, ColumnRole]], with_media: bool = True) -> ErdModel:
    insp = inspect(session.engine)
    tables = sorted(insp.get_table_names(schema=schema), key=str.lower)
    views = sorted(insp.get_view_names(schema=schema), key=str.lower)
    dialect = session.engine.dialect
    out: dict[str, list[ErdColumn]] = {}
    rels: list[ErdRelation] = []
    for t in tables:
        pk = set((insp.get_pk_constraint(t, schema=schema) or {}).get("constrained_columns") or [])
        fks = insp.get_foreign_keys(t, schema=schema)
        fk_cols = {c for fk in fks for c in fk["constrained_columns"]}
        uniques = {tuple(u["column_names"]) for u in insp.get_unique_constraints(t, schema=schema)}
        uniques |= {tuple(i["column_names"]) for i in insp.get_indexes(t, schema=schema) if i.get("unique")}
        troles = roles.get(t, {})
        cols = []
        for c in insp.get_columns(t, schema=schema):
            try:
                typ = c["type"].compile(dialect=dialect)
            except Exception:
                typ = str(c["type"])
            role = troles.get(c["name"])
            cols.append(
                ErdColumn(
                    name=c["name"], type=typ, pk=c["name"] in pk, fk=c["name"] in fk_cols,
                    nullable=bool(c.get("nullable", True)), unique=(c["name"],) in uniques, role=role,
                    media_guess=None if role else guess_media(c["name"], typ),
                )
            )
        out[t] = cols
        for fk in fks:
            cc = list(fk["constrained_columns"])
            rels.append(
                ErdRelation(
                    child=t, child_cols=cc, parent=fk["referred_table"], parent_cols=list(fk["referred_columns"]),
                    name=fk.get("name") or "", one_to_one=tuple(cc) in uniques or set(cc) == pk,
                    on_delete=((fk.get("options") or {}).get("ondelete") or "").upper(),
                )
            )
    location = _location(session, schema)
    storages = []
    if with_media:
        for t, cols in out.items():
            for c in cols:
                storage = c.role.storage if c.role and c.role.kind == "media" else c.media_guess
                if storage:
                    storages.append(_media_info(session, schema, t, c, storage, location))
    return ErdModel(out, views, rels, storages, location)


def _location(session, schema) -> str:
    cfg = session.cfg
    if cfg.driver == "sqlite":
        return str(Path(cfg.database).expanduser().resolve())
    return f"{cfg.host}:{cfg.port or 3306} / {schema or cfg.database or '(default database)'}"


def _media_info(session, schema, table, col: ErdColumn, storage: str, location: str) -> MediaStorage:
    q = session.driver.quote
    t = session.driver.qualify(table, schema)
    info = MediaStorage(table, col.name, storage, guessed=col.role is None, location=location)
    try:
        with session.engine.connect() as conn:
            if storage == "blob":
                row = conn.exec_driver_sql(f"SELECT COUNT({q(col.name)}), SUM(LENGTH({q(col.name)})) FROM {t}").fetchone()
                info.rows, info.total_bytes = int(row[0] or 0), int(row[1] or 0)
            else:
                values = [r[0] for r in conn.exec_driver_sql(
                    f"SELECT {q(col.name)} FROM {t} WHERE {q(col.name)} IS NOT NULL LIMIT 5000").fetchall()]
                info.rows = len(values)
                root = None
                if col.role is not None:
                    root = Path(col.role.folder).expanduser() if col.role.folder else media.default_folder(session.cfg)
                info.folders = media.folder_reports([str(v) for v in values], session.cfg, root)
                info.total_bytes = sum(f.total_bytes for f in info.folders)
    except Exception:
        pass
    return info


def layout(model: ErdModel, col_w: int = 330, row_h: int = 22, head_h: int = 30) -> dict[str, tuple[int, int]]:
    """Place parents left of children: column = longest FK chain above the table."""
    parents: dict[str, set[str]] = {t: set() for t in model.tables}
    for r in model.relations:
        if r.child in parents and r.parent in model.tables and r.parent != r.child:
            parents[r.child].add(r.parent)
    depth: dict[str, int] = {}

    def d(t, seen=()):
        if t in depth:
            return depth[t]
        if t in seen:
            return 0
        depth[t] = 0 if not parents[t] else 1 + max(d(p, seen + (t,)) for p in parents[t])
        return depth[t]

    for t in model.tables:
        d(t)
    cols: dict[int, list[str]] = {}
    for t in sorted(model.tables, key=str.lower):
        cols.setdefault(depth[t], []).append(t)
    pos = {}
    for c, names in cols.items():
        y = 0
        for n in names:
            pos[n] = (c * col_w, y)
            y += head_h + row_h * len(model.tables[n]) + 50
    return pos
