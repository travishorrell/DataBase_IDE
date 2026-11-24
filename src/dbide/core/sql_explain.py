"""Turn SQL into plain English, and pull JOINs apart for the visualizer."""
from __future__ import annotations

import html
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

DIALECTS = {"sqlite": "sqlite", "mysql": "mysql"}


@dataclass
class Explanation:
    summary: str  # one-line HTML
    details: list[str] = field(default_factory=list)  # bullet points (HTML)
    warnings: list[str] = field(default_factory=list)  # dangerous things (HTML)
    destructive: bool = False  # changes or deletes many rows / objects


def _code(node, dialect) -> str:
    return f"<code>{html.escape(node.sql(dialect=dialect))}</code>"


def _from(sel: exp.Select):
    return sel.args.get("from_") or sel.args.get("from")


def _table_label(t: exp.Expression, dialect: str) -> str:
    if isinstance(t, exp.Table):
        alias = t.alias
        return f"<b>{html.escape(t.name)}</b>" + (f" (as <i>{html.escape(alias)}</i>)" if alias else "")
    if isinstance(t, exp.Subquery):
        return "a sub-query" + (f" named <i>{html.escape(t.alias)}</i>" if t.alias else "")
    return _code(t, dialect)


JOIN_WORDS = {
    "INNER": "only rows that have a match in both tables are kept",
    "LEFT": "every row of the left side is kept; columns from the right are NULL when there's no match",
    "RIGHT": "every row of the right table is kept; columns from the left are NULL when there's no match",
    "FULL": "every row from both sides is kept, matched where possible",
    "CROSS": "every row is paired with every other row (a Cartesian product)",
}


def join_type(j: exp.Join) -> str:
    side = (j.side or "").upper()
    kind = (j.kind or "").upper()
    if kind == "CROSS" or (not j.args.get("on") and not j.args.get("using") and not side):
        return "CROSS"
    return side or "INNER"


def explain(sql: str, dialect: str = "sqlite") -> Explanation | None:
    d = DIALECTS.get(dialect, dialect)
    try:
        tree = sqlglot.parse_one(sql, read=d)
    except Exception:
        return None
    if tree is None:
        return None
    try:
        if isinstance(tree, (exp.Select, exp.Union)):
            return _explain_select(tree, d)
        if isinstance(tree, exp.Insert):
            return _explain_insert(tree, d)
        if isinstance(tree, exp.Update):
            return _explain_update(tree, d)
        if isinstance(tree, exp.Delete):
            return _explain_delete(tree, d)
        if isinstance(tree, exp.Create):
            return _explain_create(tree, d)
        if isinstance(tree, exp.Drop):
            kind = (tree.args.get("kind") or "object").lower()
            targets = [tree.this] if tree.this is not None else list(tree.args.get("tables") or tree.expressions)
            names = ", ".join(_code(t, d) for t in targets) or "(unnamed)"
            return Explanation(
                f"Permanently deletes the {kind} {names}.",
                warnings=[f"⚠️ DROP removes the {kind} <b>and all of its data</b>. There is no undo."],
                destructive=True,
            )
        if isinstance(tree, exp.Alter):
            return Explanation(f"Changes the structure of table {_code(tree.this, d)}: "
                               + ", ".join(_code(a, d) for a in tree.args.get("actions") or []))
    except Exception:
        return None
    return Explanation(f"Runs a <b>{html.escape(tree.key.upper())}</b> statement.")


def _explain_select(tree, d) -> Explanation:
    if isinstance(tree, exp.Union):
        dup = "keeping duplicates" if not tree.args.get("distinct") else "removing duplicate rows"
        return Explanation("Combines the results of two queries into one list, " + dup + ".")
    ex = Explanation("")
    cols = tree.expressions
    if len(cols) == 1 and isinstance(cols[0], exp.Star):
        what = "<b>all columns</b>"
    else:
        what = ", ".join(_code(c, d) for c in cols[:8]) + (" …" if len(cols) > 8 else "")
    f = _from(tree)
    source = _table_label(f.this, d) if f else "no table"
    ex.summary = f"Reads {what} from {source}" + (" (each different row once)" if tree.args.get("distinct") else "") + "."
    for j in tree.args.get("joins") or []:
        jt = join_type(j)
        on = j.args.get("on")
        cond = f" where {_code(on, d)}" if on is not None else ""
        ex.details.append(f"<b>{jt} JOIN</b> {_table_label(j.this, d)}{cond}: {JOIN_WORDS[jt]}.")
        if jt == "CROSS" and len(tree.args.get("joins")) and on is None:
            ex.warnings.append("⚠️ A join with no ON condition pairs every row with every row. Did you forget the ON?")
    if tree.args.get("where"):
        ex.details.append(f"Keeps only rows where {_code(tree.args['where'].this, d)}.")
    if tree.args.get("group"):
        ex.details.append(f"Groups rows by {', '.join(_code(g, d) for g in tree.args['group'].expressions)} and returns one row per group.")
    if tree.args.get("having"):
        ex.details.append(f"Then keeps only groups where {_code(tree.args['having'].this, d)}.")
    if tree.args.get("order"):
        parts = []
        for o in tree.args["order"].expressions:
            parts.append(_code(o.this, d) + (" (largest/latest first)" if o.args.get("desc") else ""))
        ex.details.append("Sorts by " + ", ".join(parts) + ".")
    if tree.args.get("limit"):
        ex.details.append(f"Returns at most {_code(tree.args['limit'].expression, d)} rows.")
    ex.details.append("Reading data never changes anything; it's safe to run.")
    return ex


def _explain_insert(tree: exp.Insert, d) -> Explanation:
    target = tree.this
    table = target.this if isinstance(target, exp.Schema) else target
    cols = [c.name for c in target.expressions] if isinstance(target, exp.Schema) else []
    src = tree.expression
    if isinstance(src, exp.Values):
        n = len(src.expressions)
        ex = Explanation(f"Adds <b>{n}</b> new row{'s' if n != 1 else ''} to {_code(table, d)}.")
    else:
        ex = Explanation(f"Adds the rows returned by a query to {_code(table, d)}.")
    if cols:
        ex.details.append("Sets columns " + ", ".join(f"<code>{html.escape(c)}</code>" for c in cols) + "; other columns get their DEFAULT (or NULL).")
    else:
        ex.details.append("No column list given, so values must be in the exact order of the table's columns.")
    return ex


def _explain_update(tree: exp.Update, d) -> Explanation:
    sets = ", ".join(_code(e, d) for e in tree.expressions)
    ex = Explanation(f"Changes {sets} in table {_code(tree.this, d)}.")
    where = tree.args.get("where")
    if where:
        ex.details.append(f"Only rows where {_code(where.this, d)} are changed.")
    else:
        ex.warnings.append("⚠️ <b>No WHERE clause: every row in the table will be changed.</b>")
        ex.destructive = True
    return ex


def _explain_delete(tree: exp.Delete, d) -> Explanation:
    ex = Explanation(f"Deletes rows from {_code(tree.this, d)}.")
    where = tree.args.get("where")
    if where:
        ex.details.append(f"Only rows where {_code(where.this, d)} are deleted.")
    else:
        ex.warnings.append("⚠️ <b>No WHERE clause: every row in the table will be deleted.</b>")
        ex.destructive = True
    return ex


def _explain_create(tree: exp.Create, d) -> Explanation:
    kind = (tree.args.get("kind") or "").upper()
    if kind != "TABLE" or not isinstance(tree.this, exp.Schema):
        return Explanation(f"Creates a new {kind.lower() or 'object'} {_code(tree.this, d)}.")
    schema = tree.this
    ex = Explanation(f"Creates a new table <b>{html.escape(schema.this.name)}</b>.")
    pks: list[str] = []
    for item in schema.expressions:
        if isinstance(item, exp.ColumnDef):
            name = item.name
            typ = item.args.get("kind")
            notes = []
            for c in item.args.get("constraints") or []:
                k = c.args.get("kind")
                if isinstance(k, exp.PrimaryKeyColumnConstraint):
                    pks.append(name)
                    notes.append("primary key")
                elif isinstance(k, exp.NotNullColumnConstraint):
                    notes.append("required")
                elif isinstance(k, exp.UniqueColumnConstraint):
                    notes.append("unique")
                elif isinstance(k, exp.AutoIncrementColumnConstraint):
                    notes.append("auto-numbered")
                elif isinstance(k, exp.DefaultColumnConstraint):
                    notes.append(f"default {_code(k.this, d)}")
                elif isinstance(k, exp.Reference):
                    ref = k.this
                    notes.append(f"must match an existing <b>{html.escape(ref.this.name)}</b>."
                                 + ", ".join(html.escape(i.name) for i in ref.expressions))
            t = typ.sql(dialect=d) if typ is not None else "?"
            ex.details.append(f"Column <code>{html.escape(name)}</code> ({html.escape(t)})" + (": " + ", ".join(notes) if notes else ""))
        elif isinstance(item, exp.PrimaryKey):
            pks += [i.name for i in item.expressions]
            ex.details.append("Primary key: " + ", ".join(f"<code>{html.escape(i.name)}</code>" for i in item.expressions))
        elif isinstance(item, exp.ForeignKey):
            ref = item.args.get("reference").this
            cols = ", ".join(html.escape(i.name) for i in item.expressions)
            ex.details.append(f"Foreign key: <code>{cols}</code> must match an existing <b>{html.escape(ref.this.name)}</b>."
                              + ", ".join(html.escape(i.name) for i in ref.expressions)
                              + ". This links each row to one parent row (one-to-many).")
    if not pks:
        ex.warnings.append("This table has no primary key, so rows can't be uniquely identified (and the grid can't edit them).")
    return ex


# ---- JOIN extraction for the visualizer ------------------------------------------


@dataclass
class TableRef:
    name: str
    alias: str  # alias or the name itself
    schema: str | None = None


@dataclass
class JoinStep:
    kind: str  # INNER / LEFT / RIGHT / FULL / CROSS
    left: TableRef | None  # the earlier table the ON condition links to
    right: TableRef
    on_sql: str  # ON condition as SQL ("" for CROSS)
    pairs: list[tuple[str, str, str, str]] = field(default_factory=list)  # (left alias, left col, right alias, right col)


@dataclass
class JoinAnalysis:
    tables: list[TableRef]
    steps: list[JoinStep]


def extract_joins(sql: str, dialect: str = "sqlite") -> JoinAnalysis | None:
    d = DIALECTS.get(dialect, dialect)
    try:
        tree = sqlglot.parse_one(sql, read=d)
    except Exception:
        return None
    sel = tree if isinstance(tree, exp.Select) else tree.find(exp.Select) if tree else None
    if sel is None or not sel.args.get("joins"):
        return None
    f = _from(sel)
    if f is None or not isinstance(f.this, exp.Table):
        return None

    def ref(t: exp.Table) -> TableRef:
        return TableRef(t.name, t.alias or t.name, t.args.get("db").name if t.args.get("db") else None)

    tables = [ref(f.this)]
    steps = []
    for j in sel.args["joins"]:
        if not isinstance(j.this, exp.Table):
            continue
        right = ref(j.this)
        kind = join_type(j)
        on = j.args.get("on")
        using = j.args.get("using")
        pairs = []
        left = None
        if on is not None:
            for eq in on.find_all(exp.EQ):
                a, b = eq.this, eq.expression
                if isinstance(a, exp.Column) and isinstance(b, exp.Column) and a.table and b.table:
                    if b.table == right.alias:
                        a, b = a, b
                    elif a.table == right.alias:
                        a, b = b, a
                    else:
                        continue
                    pairs.append((a.table, a.name, b.table, b.name))
            on_sql = on.sql(dialect=d)
        elif using:
            prev = tables[-1]
            for ident in using:
                pairs.append((prev.alias, ident.name, right.alias, ident.name))
            on_sql = " AND ".join(f"{prev.alias}.{i.name} = {right.alias}.{i.name}" for i in using)
        else:
            on_sql = ""
        if pairs:
            left = next((t for t in tables if t.alias == pairs[0][0]), None)
        steps.append(JoinStep(kind, left or (tables[-1] if kind == "CROSS" else None), right, on_sql, pairs))
        tables.append(right)
    return JoinAnalysis(tables, steps)


def join_stat_queries(step: JoinStep, quote, qualify) -> dict[str, str] | None:
    """SQL that counts matched and unmatched rows for one join step (pairwise)."""
    if not step.left or not step.on_sql:
        return None
    L = f"{qualify(step.left.name, step.left.schema)} AS {quote(step.left.alias)}"
    R = f"{qualify(step.right.name, step.right.schema)} AS {quote(step.right.alias)}"
    on = step.on_sql
    return {
        "left_total": f"SELECT COUNT(*) FROM {L}",
        "right_total": f"SELECT COUNT(*) FROM {R}",
        "left_unmatched": f"SELECT COUNT(*) FROM {L} WHERE NOT EXISTS (SELECT 1 FROM {R} WHERE {on})",
        "right_unmatched": f"SELECT COUNT(*) FROM {R} WHERE NOT EXISTS (SELECT 1 FROM {L} WHERE {on})",
        "inner_rows": f"SELECT COUNT(*) FROM {L} JOIN {R} ON {on}",
        "left_sample": f"SELECT * FROM {L} WHERE NOT EXISTS (SELECT 1 FROM {R} WHERE {on}) LIMIT 5",
        "right_sample": f"SELECT * FROM {R} WHERE NOT EXISTS (SELECT 1 FROM {L} WHERE {on}) LIMIT 5",
        "matched_sample": f"SELECT * FROM {L} JOIN {R} ON {on} LIMIT 5",
    }


def rows_for_kind(kind: str, stats: dict[str, int]) -> int:
    inner = stats["inner_rows"]
    return {
        "INNER": inner,
        "LEFT": inner + stats["left_unmatched"],
        "RIGHT": inner + stats["right_unmatched"],
        "FULL": inner + stats["left_unmatched"] + stats["right_unmatched"],
        "CROSS": stats["left_total"] * stats["right_total"],
    }[kind]
