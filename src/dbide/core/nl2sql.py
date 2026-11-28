"""Offline plain-English → SQL.

Nothing leaves the machine. The translator reads the request against the real
schema (tables, columns, foreign keys) and a local index of the values stored
in short text columns, so "customers in UK" knows that 'UK' lives in
customers.country. It only ever writes SELECT statements, and it reports how
it understood each phrase so the user can check (and learn from) the SQL.

Handled: filters (=, !=, >, <, between, contains, starts/ends with, empty),
dates ("in 2020", "after 2019"), counts/sums/averages/min/max, grouping
("per author", "by status"), top-N and ordering ("10 newest", "sorted by"),
joins along foreign-key paths, "X without Y" (anti-join), "X with more than N Y"
(correlated count) and DISTINCT.
"""
from __future__ import annotations

import difflib
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

# --------------------------------------------------------------------------------
# schema description


@dataclass
class Col:
    table: str
    name: str
    type: str
    pk: bool = False

    @property
    def kind(self) -> str:
        t = self.type.upper()
        n = self.name.lower()
        if any(k in t for k in ("DATE", "TIME")) or n.endswith(("_at", "_on", "_date")) or n in ("date", "created", "updated"):
            return "date"
        if n == "year" or n.endswith("_year"):
            return "year"
        if any(k in t for k in ("INT", "REAL", "NUM", "DEC", "FLOAT", "DOUBLE")):
            return "number"
        if any(k in t for k in ("CHAR", "TEXT", "CLOB", "ENUM", "STRING")) or not t:
            return "text"
        return "other"


@dataclass
class FK:
    child: str
    child_cols: list[str]
    parent: str
    parent_cols: list[str]


@dataclass
class SchemaInfo:
    tables: dict[str, list[Col]]
    fks: list[FK] = field(default_factory=list)
    # lower-cased stored value -> [(table, column, original value)]
    values: dict[str, list[tuple[str, str, str]]] = field(default_factory=dict)

    def cols(self, table: str) -> list[Col]:
        return self.tables.get(table, [])

    def col(self, table: str, name: str) -> Col | None:
        return next((c for c in self.cols(table) if c.name == name), None)

    def pk(self, table: str) -> list[str]:
        return [c.name for c in self.cols(table) if c.pk] or [self.cols(table)[0].name] if self.cols(table) else []

    def display_col(self, table: str) -> str:
        names = [c.name for c in self.cols(table)]
        for pref in ("name", "title", "full_name", "username", "label", "email", "first_name", "caption", "description"):
            if pref in names:
                return pref
        for c in self.cols(table):
            if c.kind == "text" and not c.pk:
                return c.name
        return self.pk(table)[0] if self.pk(table) else names[0]

    def date_col(self, table: str) -> Col | None:
        for kind in ("date", "year"):
            for c in self.cols(table):
                if c.kind == kind:
                    return c
        return None


def load_schema(session, schema: str | None = None, protected: dict[str, set[str]] | None = None,
                max_values: int = 400) -> SchemaInfo:
    """Read tables, keys and a local value index (never leaves the machine)."""
    from sqlalchemy import inspect

    insp = inspect(session.engine)
    protected = protected or {}
    tables: dict[str, list[Col]] = {}
    fks: list[FK] = []
    for t in insp.get_table_names(schema=schema):
        pk = set((insp.get_pk_constraint(t, schema=schema) or {}).get("constrained_columns") or [])
        cols = []
        for c in insp.get_columns(t, schema=schema):
            try:
                typ = c["type"].compile(dialect=session.engine.dialect)
            except Exception:
                typ = str(c["type"])
            cols.append(Col(t, c["name"], typ, c["name"] in pk))
        tables[t] = cols
        for fk in insp.get_foreign_keys(t, schema=schema):
            fks.append(FK(t, list(fk["constrained_columns"]), fk["referred_table"], list(fk["referred_columns"])))
    info = SchemaInfo(tables, fks)
    q, qual = session.driver.quote, session.driver.qualify
    with session.engine.connect() as conn:
        for t, cols in tables.items():
            for c in cols:
                if c.kind != "text" or c.pk or c.name in protected.get(t, set()):
                    continue
                try:
                    rows = conn.exec_driver_sql(
                        session.driver.select_limit(f"SELECT {q(c.name)} FROM {qual(t, schema)} WHERE {q(c.name)} IS NOT NULL "
                                                    f"GROUP BY {q(c.name)}", int(max_values))).fetchall()
                except Exception:
                    continue
                for (v,) in rows:
                    if isinstance(v, str) and 0 < len(v) <= 60 and not v.startswith(("$2", "$argon", "enc:", "pbkdf2", "scrypt$")):
                        info.values.setdefault(v.lower(), []).append((t, c.name, v))
    return info


# --------------------------------------------------------------------------------
# results


@dataclass
class Step:
    phrase: str  # the words from the request
    meaning: str  # what they became


@dataclass
class Generated:
    sql: str
    steps: list[Step]
    ignored: list[str]
    warnings: list[str]
    confident: bool


class NotUnderstood(Exception):
    pass


# --------------------------------------------------------------------------------
# vocabulary

STOP = set("""
show list find get display give me all the a an of which what who whose are is was were that those these
please can you i want to see do does did there their them and my our for with any every some
records rows entries data info information details be have has where whose
""".split())

AGG_WORDS = [
    (("how", "many"), "count"), (("number", "of"), "count"), (("count", "of"), "count"), (("count",), "count"),
    (("total", "number", "of"), "count"), (("total",), "sum"), (("sum", "of"), "sum"), (("sum",), "sum"),
    (("average",), "avg"), (("avg",), "avg"), (("mean",), "avg"),
    (("maximum",), "max"), (("max",), "max"), (("highest",), "max"), (("largest",), "max"), (("biggest",), "max"),
    (("most", "expensive"), "max"), (("most",), "max"),
    (("minimum",), "min"), (("min",), "min"), (("lowest",), "min"), (("smallest",), "min"), (("cheapest",), "min"),
    (("least",), "min"),
]
GROUP_WORDS = [("for", "each"), ("grouped", "by"), ("broken", "down", "by"), ("breakdown", "by"), ("per",), ("by",), ("each",)]
ORDER_WORDS = [("sorted", "by"), ("ordered", "by"), ("order", "by"), ("sort", "by"), ("sorted",), ("ordered",)]
RECENT = {"newest": "DESC", "latest": "DESC", "recent": "DESC", "last": "DESC", "oldest": "ASC", "earliest": "ASC", "first": "ASC"}
CMP_WORDS: list = [
    (("greater", "than", "or", "equal", "to"), ">="), (("more", "than"), ">"), (("greater", "than"), ">"),
    (("higher", "than"), ">"), (("over",), ">"), (("above",), ">"), (("at", "least"), ">="), (("min",), ">="),
    (("less", "than"), "<"), (("fewer", "than"), "<"), (("lower", "than"), "<"), (("under",), "<"), (("below",), "<"),
    (("at", "most"), "<="), (("up", "to"), "<="),
    (("is", "not"), "!="), (("isn't",), "!="), (("not", "equal", "to"), "!="), (("not",), "!="), (("except",), "!="),
    (("equal", "to"), "="), (("equals",), "="), (("is",), "="), (("=",), "="), (("==",), "="),
    ((">=",), ">="), (("<=",), "<="), ((">",), ">"), (("<",), "<"), (("!=",), "!="), (("<>",), "!="),
    (("between",), "between"), (("after",), ">"), (("since",), ">="), (("from",), "from"), (("before",), "<"),
    (("until",), "<="), (("in",), "in"), (("during",), "in"), (("on",), "in"),
    (("containing",), "contains"), (("contains",), "contains"), (("including",), "contains"), (("like",), "contains"),
    (("starts", "with"), "starts"), (("starting", "with"), "starts"), (("beginning", "with"), "starts"), (("begins", "with"), "starts"),
    (("ends", "with"), "ends"), (("ending", "with"), "ends"),
]
# "is at least", "is more than", "are over", ...
CMP_WORDS += [((lead,) + seq, op) for lead in ("is", "are", "was") for seq, op in list(CMP_WORDS)
              if seq[0] not in ("is", "isn't", "not", "equal", "equals", "=", "==") and op not in ("in",)]
EMPTY_WORDS = [("is", "empty"), ("is", "missing"), ("is", "null"), ("with", "no", "value"), ("missing",), ("empty",), ("blank",)]
NOT_EMPTY_WORDS = [("is", "not", "empty"), ("is", "not", "null"), ("is", "set"), ("not", "empty"), ("not", "null")]
NEG_REL = [("that", "have", "no"), ("who", "have", "no"), ("which", "have", "no"), ("having", "no"), ("with", "no"),
           ("without", "any"), ("without",), ("that", "never", "had"), ("never", "had"), ("with", "zero"), ("never",), ("that", "never"), ("which", "never")]
HAS_REL = [("that", "have"), ("who", "have"), ("which", "have"), ("having",), ("with",)]
DISTINCT = {"unique", "distinct", "different"}
DESC = {"desc", "descending", "decreasing", "highest", "largest", "biggest", "z-a"}
ASC = {"asc", "ascending", "increasing", "lowest", "smallest", "a-z", "alphabetical", "alphabetically"}
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
                "twenty": 20, "fifty": 50, "hundred": 100}


def singular(w: str) -> str:
    w = w.lower()
    irregular = {"people": "person", "children": "child", "men": "man", "women": "woman", "data": "datum", "media": "media"}
    if w in irregular:
        return irregular[w]
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith(("sses", "xes", "ches", "shes")):
        return w[:-2]
    if w.endswith(("us", "ss", "is", "ics")):  # status, address, analysis, statistics
        return w
    if w.endswith("s") and len(w) > 3:
        return w[:-1]
    return w


def _forms(name: str) -> set[str]:
    base = name.lower()
    spaced = base.replace("_", " ")
    out = {base, spaced, singular(base), " ".join(singular(p) for p in spaced.split())}
    if spaced.endswith(" id"):
        out.add(spaced[:-3])
    for suffix in (" at", " on"):
        if spaced.endswith(suffix):
            out.add(spaced[: -len(suffix)])
    out |= {plural(s) for s in list(out)}
    return {o for o in out if o}


def plural(w: str) -> str:
    if w.endswith("s"):
        return w
    if w.endswith("y") and len(w) > 2 and w[-2] not in "aeiou":
        return w[:-1] + "ies"
    if w.endswith(("x", "ch", "sh")):
        return w + "es"
    return w + "s"


# --------------------------------------------------------------------------------
# tokenizing & mentions

TOKEN = re.compile(r"'[^']*'|\"[^\"]*\"|\$?-?\d+(?:[.,]\d+)*%?|[A-Za-z_][\w'-]*|[<>=!]+")


@dataclass
class M:
    kind: str  # table, col, value, num, agg, group, order, recent, top, cmp, empty, notempty, neg, has, distinct, dir
    start: int
    end: int
    text: str
    data: object = None


class Translator:
    def __init__(self, info: SchemaInfo, quote: Callable[[str], str], qualify: Callable[[str, str | None], str],
                 schema: str | None = None, limit_fn=None):
        self.info, self.quote, self.qualify, self.schema = info, quote, qualify, schema
        self.limit_fn = limit_fn or _limit_clause
        self.table_forms: dict[str, str] = {}
        for t in info.tables:
            for f in _forms(t):
                self.table_forms.setdefault(f, t)
        self.col_forms: dict[str, list[Col]] = {}
        for cols in info.tables.values():
            for c in cols:
                for f in _forms(c.name):
                    self.col_forms.setdefault(f, []).append(c)

    # ---- lexing --------------------------------------------------------------
    def _mentions(self, text: str) -> tuple[list[str], list[M]]:
        raw = TOKEN.findall(text)
        toks = [r.lower() for r in raw]
        out: list[M] = []
        i = 0

        def match_seq(seqs) -> tuple[int, object] | None:
            best = None
            for item in seqs:
                seq = item[0] if isinstance(item[0], tuple) else item
                val = item[1] if isinstance(item[0], tuple) else None
                n = len(seq)
                if tuple(toks[i:i + n]) == seq and (best is None or n > best[0]):
                    best = (n, val)
            return best

        while i < len(toks):
            t, r = toks[i], raw[i]
            # quoted strings are always values
            if r[0] in "'\"" and len(r) >= 2:
                out.append(M("value", i, i + 1, r, [(None, None, r[1:-1])]))
                i += 1
                continue
            # numbers
            num = _number(r)
            if num is None and t in NUMBER_WORDS:
                num = NUMBER_WORDS[t]
            if num is not None:
                out.append(M("num", i, i + 1, r, num))
                i += 1
                continue
            # a single-word aggregate that is also a column ("total", "count") is the column
            # unless it is followed by something to aggregate: "total of orders", "highest price"
            if t in self.col_forms and any(seq == (t,) for seq, _ in AGG_WORDS):
                j = i + 1
                while j < len(toks) and toks[j] in STOP:
                    j += 1
                if j >= len(toks) or self._match_schema(toks, raw, j) is None:
                    out.append(M("col", i, i + 1, r, self.col_forms[t]))
                    i += 1
                    continue
            # multi-word keyword phrases, longest first
            for kind, seqs in (("notempty", [(s, None) for s in NOT_EMPTY_WORDS]), ("empty", [(s, None) for s in EMPTY_WORDS]),
                               ("neg", [(s, None) for s in NEG_REL]), ("agg", AGG_WORDS), ("group", [(s, None) for s in GROUP_WORDS]),
                               ("order", [(s, None) for s in ORDER_WORDS]), ("cmp", CMP_WORDS), ("has", [(s, None) for s in HAS_REL])):
                m = match_seq(seqs)
                if m:
                    n, val = m
                    # "has": only when a table/count follows, decided later; keep as marker
                    out.append(M(kind, i, i + n, " ".join(raw[i:i + n]), val))
                    i += n
                    break
            else:
                if t in ("top", "first", "limit") and i + 1 < len(toks) and (_number(raw[i + 1]) is not None or toks[i + 1] in NUMBER_WORDS):
                    n = _number(raw[i + 1]) if _number(raw[i + 1]) is not None else NUMBER_WORDS[toks[i + 1]]
                    out.append(M("top", i, i + 2, " ".join(raw[i:i + 2]), int(n)))
                    i += 2
                    continue
                if t in RECENT and t != "first":
                    out.append(M("recent", i, i + 1, r, RECENT[t]))
                    i += 1
                    if i < len(toks) and toks[i] in ("first",):
                        i += 1
                    continue
                if t in DISTINCT:
                    out.append(M("distinct", i, i + 1, r))
                    i += 1
                    continue
                if t in DESC or t in ASC:
                    out.append(M("dir", i, i + 1, r, "DESC" if t in DESC else "ASC"))
                    i += 1
                    continue
                m = self._match_schema(toks, raw, i)
                if m:
                    out.append(m)
                    i = m.end
                    continue
                if t not in STOP:
                    out.append(M("word", i, i + 1, r))
                i += 1
        return raw, out

    def _match_schema(self, toks, raw, i) -> M | None:
        # longest n-gram first: table+column ("author name"), stored values, columns, tables
        for n in (4, 3, 2, 1):
            if i + n > len(toks):
                continue
            phrase = " ".join(toks[i:i + n])
            text = " ".join(raw[i:i + n])
            if n >= 2:
                for split in range(1, n):
                    tp, cp = " ".join(toks[i:i + split]), " ".join(toks[i + split:i + n])
                    t = self.table_forms.get(tp)
                    if t:
                        c = next((c for c in self.col_forms.get(cp, []) if c.table == t), None)
                        if c:
                            return M("col", i, i + n, text, [c])
            if phrase in self.info.values and (n > 1 or len(phrase) > 1):
                # a table or column name wins over an identical stored value only for single words
                if not (n == 1 and (phrase in self.table_forms or phrase in self.col_forms)):
                    return M("value", i, i + n, text, self.info.values[phrase])
            if phrase in self.col_forms and phrase not in self.table_forms:
                return M("col", i, i + n, text, self.col_forms[phrase])
            if phrase in self.table_forms:
                if phrase in self.col_forms and n == 1:
                    # e.g. "year": both a table form and a column; prefer table only if nothing else
                    return M("table", i, i + n, text, self.table_forms[phrase])
                return M("table", i, i + n, text, self.table_forms[phrase])
        # typos: close match to a table or column name
        word = toks[i]
        if len(word) >= 4 and word not in STOP:
            close = difflib.get_close_matches(word, list(self.table_forms), n=1, cutoff=0.84)
            if close:
                return M("table", i, i + 1, raw[i], self.table_forms[close[0]])
            close = difflib.get_close_matches(word, list(self.col_forms), n=1, cutoff=0.86)
            if close:
                return M("col", i, i + 1, raw[i], self.col_forms[close[0]])
        return None

    # ---- graph -------------------------------------------------------------
    def _path(self, a: str, b: str) -> list[tuple[str, str, FK]] | None:
        """FK path a → b as [(from, to, fk)], following keys in either direction."""
        if a == b:
            return []
        adj: dict[str, list[tuple[str, FK]]] = {}
        for fk in self.info.fks:
            if fk.child in self.info.tables and fk.parent in self.info.tables:
                adj.setdefault(fk.child, []).append((fk.parent, fk))
                adj.setdefault(fk.parent, []).append((fk.child, fk))
        prev: dict[str, tuple[str, FK]] = {}
        seen, q = {a}, deque([a])
        while q:
            cur = q.popleft()
            for nxt, fk in adj.get(cur, []):
                if nxt not in seen:
                    seen.add(nxt)
                    prev[nxt] = (cur, fk)
                    if nxt == b:
                        path = []
                        node = b
                        while node != a:
                            p, f = prev[node]
                            path.append((p, node, f))
                            node = p
                        return list(reversed(path))
                    q.append(nxt)
        return None

    # ---- translate ----------------------------------------------------------
    def translate(self, text: str) -> Generated:
        if not self.info.tables:
            raise NotUnderstood("This database has no tables yet.")
        raw, ms = self._mentions(text)
        steps: list[Step] = []
        warnings: list[str] = []
        used: set[int] = set()

        def use(*idx):
            used.update(idx)

        # ---- the subject table
        subject = None
        for k, m in enumerate(ms):
            if m.kind == "table":
                prev = ms[k - 1] if k else None
                if prev and prev.kind in ("group", "neg", "has", "num", "cmp"):
                    continue  # "per author", "without books", "more than 2 books"
                subject = m.data
                use(k)
                steps.append(Step(m.text, f"rows of table <b>{subject}</b>"))
                break
        if subject is None:
            for k, m in enumerate(ms):
                if m.kind == "col" and m.data:
                    subject = m.data[0].table
                    break
                if m.kind == "value" and m.data and m.data[0][0]:
                    subject = m.data[0][0]
                    break
        if subject is None:
            tm = next((m for m in ms if m.kind == "table"), None)
            if tm:
                subject = tm.data
        if subject is None:
            raise NotUnderstood(
                "I couldn't tell which table you mean. Mention one of: " + ", ".join(sorted(self.info.tables)))

        q = Query(self, subject)

        def pick_col(cands: list[Col], prefer_kind: str | None = None) -> Col:
            order = [subject] + [t for t in q.tables if t != subject]
            for t in order:
                for c in cands:
                    if c.table == t and (prefer_kind is None or c.kind == prefer_kind):
                        return c
            for c in cands:
                if self._path(subject, c.table) is not None:
                    return c
            return cands[0]

        def value_target(m: M) -> tuple[Col | None, str]:
            hits = [(t, c, v) for t, c, v in m.data if t]
            if not hits:
                return None, m.data[0][2]
            for pref in [subject] + list(q.tables):
                for t, c, v in hits:
                    if t == pref:
                        return self.info.col(t, c), v
            t, c, v = hits[0]
            return self.info.col(t, c), v

        last_col: Col | None = None
        k = 0
        n = len(ms)
        while k < n:
            m = ms[k]
            nxt = ms[k + 1] if k + 1 < n else None
            nxt2 = ms[k + 2] if k + 2 < n else None

            if k in used:
                k += 1
                continue

            # --- relationships: "without books", "with more than 2 books", "with books"
            if m.kind in ("neg", "has"):
                j = k + 1
                op, count = None, None
                if j < n and ms[j].kind == "cmp" and ms[j].data in (">", "<", ">=", "<=", "=", "!=") and j + 1 < n and ms[j + 1].kind == "num":
                    op, count = ms[j].data, ms[j + 1].data
                    j += 2
                elif j < n and ms[j].kind == "num":
                    op, count = ">=", ms[j].data
                    j += 1
                if j < n and ms[j].kind == "table" and ms[j].data != subject:
                    other = ms[j].data
                    if self._path(subject, other) is not None:
                        if m.kind == "neg":
                            q.relations.append(("none", other, None, None))
                            steps.append(Step(" ".join(x.text for x in ms[k:j + 1]), f"{subject} that have <b>no</b> {other}"))
                        elif count is not None:
                            q.relations.append(("count", other, op, count))
                            steps.append(Step(" ".join(x.text for x in ms[k:j + 1]),
                                              f"{subject} whose number of {other} is {op} {count}"))
                        else:
                            q.relations.append(("some", other, None, None))
                            steps.append(Step(" ".join(x.text for x in ms[k:j + 1]), f"{subject} that have at least one {other}"))
                        use(*range(k, j + 1))
                        k = j + 1
                        continue
                # "never ordered", "without purchases": a verb/noun naming a related table
                if m.kind == "neg" and j < n and ms[j].kind == "col":
                    # "books never loaned": 'loaned' matched loans.loaned_on → books with no loans
                    other = next((c.table for c in ms[j].data if c.table != subject and self._path(subject, c.table) is not None), None)
                    if other:
                        q.relations.append(("none", other, None, None))
                        steps.append(Step(f"{m.text} {ms[j].text}", f"{subject} with <b>no</b> matching {other}"))
                        use(k, j)
                        k = j + 1
                        continue
                if m.kind == "neg" and j < n and ms[j].kind in ("word", "order"):
                    other = self._related_by_word(subject, ms[j].text)
                    if other:
                        q.relations.append(("none", other, None, None))
                        steps.append(Step(f"{m.text} {ms[j].text}", f"{subject} with <b>no</b> matching {other}"))
                        use(k, j)
                        k = j + 1
                        continue
                if m.kind == "has":  # plain "with": just a connector
                    use(k)
                    k += 1
                    continue

            # --- aggregates: "how many", "total price", "average age", "most expensive"
            if m.kind == "agg":
                func = m.data
                target = None
                if nxt and nxt.kind == "col":
                    target = pick_col(nxt.data, "number" if func in ("sum", "avg") else None)
                    q.need(target.table)
                    use(k + 1)
                elif nxt and nxt.kind == "table" and func in ("sum", "avg"):
                    # "total of orders": add up that table's main number column
                    target = next((c for c in self.info.cols(nxt.data) if c.kind == "number" and not c.pk and not c.name.endswith("_id")), None)
                    if target:
                        q.need(target.table)
                    use(k + 1)
                elif nxt and nxt.kind == "table":
                    if func == "count":
                        if nxt.data != subject and not q.aggs:
                            q.subject = subject = nxt.data
                            q.tables = [subject]
                        use(k + 1)
                    elif func in ("max", "min"):
                        # "most expensive book" with no measure column → order by first number column
                        tgt = next((c for c in self.info.cols(nxt.data) if c.kind == "number" and not c.pk and not c.name.endswith("_id")), None)
                        if tgt:
                            q.order.append((tgt, "DESC" if func == "max" else "ASC"))
                            q.limit = q.limit or 1
                            steps.append(Step(f"{m.text} {nxt.text}", f"sort by <code>{tgt.table}.{tgt.name}</code> and take the top row"))
                            use(k, k + 1)
                            k += 2
                            continue
                # superlative after a subject ("customer with the highest balance") → order + limit 1
                if func in ("max", "min") and target and (q.relations or any(s.meaning.startswith("rows of") for s in steps)) \
                        and m.text.lower() not in ("maximum", "max", "minimum", "min") and not any(x.kind == "group" for x in ms):
                    q.need(target.table)
                    q.order.append((target, "DESC" if func == "max" else "ASC"))
                    q.limit = q.limit or 1
                    steps.append(Step(m.text + (" " + nxt.text if nxt else ""),
                                      f"the row with the {'highest' if func == 'max' else 'lowest'} <code>{target.table}.{target.name}</code>"))
                    use(k)
                    k += 2
                    continue
                q.aggs.append((func, target))
                label = {"count": "count", "sum": "add up", "avg": "average", "max": "maximum", "min": "minimum"}[func]
                steps.append(Step(m.text + (" " + nxt.text if target and nxt else ""),
                                  f"{label} of " + (f"<code>{target.table}.{target.name}</code>" if target else "rows")))
                use(k)
                k += 1
                continue

            # --- "books by Jane Austen": 'by' before a stored value is just a connector, not grouping
            if m.kind == "group" and nxt and nxt.kind == "value" and m.text.lower() == "by":
                use(k)
                k += 1
                continue
            # --- grouping: "per author", "by status"
            if m.kind == "group" and nxt and nxt.kind in ("table", "col"):
                if nxt.kind == "table":
                    q.group_table = nxt.data
                    steps.append(Step(f"{m.text} {nxt.text}", f"one row per <b>{nxt.data}</b>"))
                else:
                    c = pick_col(nxt.data)
                    q.need(c.table)
                    q.group_cols.append(c)
                    steps.append(Step(f"{m.text} {nxt.text}", f"one row per value of <code>{c.table}.{c.name}</code>"))
                use(k, k + 1)
                k += 2
                continue

            # --- ordering
            if m.kind == "order":
                j = k + 1
                if j < n and ms[j].kind == "col":
                    c = pick_col(ms[j].data)
                    d = "ASC"
                    if j + 1 < n and ms[j + 1].kind == "dir":
                        d = ms[j + 1].data
                        use(j + 1)
                    q.need(c.table)
                    q.order.append((c, d))
                    steps.append(Step(f"{m.text} {ms[j].text}", f"sort by <code>{c.table}.{c.name}</code> {'descending' if d == 'DESC' else 'ascending'}"))
                    use(k, j)
                    k = j + 1
                    continue
                if j < n and ms[j].kind == "agg":
                    q.order_by_agg = "DESC" if ms[j].data in ("max", "count", "sum") else "ASC"
                    use(k, j)
                    k = j + 1
                    continue
                if j < n and ms[j].kind in ("value", "table"):
                    # "products ordered by Ada": the verb "ordered", not sorting; the value/table that follows does the work
                    use(k)
                    k += 1
                    continue
            if m.kind == "dir" and m.text.lower().startswith("alphabetical"):
                c = self.info.col(subject, self.info.display_col(subject))
                q.order.append((c, "ASC"))
                steps.append(Step(m.text, f"sort by <code>{c.table}.{c.name}</code> A→Z"))
                use(k)
                k += 1
                continue
            if m.kind == "dir" and q.order:
                q.order[-1] = (q.order[-1][0], m.data)
                use(k)
                k += 1
                continue

            # --- "top 5", "10 newest"
            if m.kind == "top":
                q.limit = m.data
                steps.append(Step(m.text, f"at most {m.data} rows"))
                use(k)
                k += 1
                continue
            if m.kind == "num" and nxt and nxt.kind == "recent":
                q.limit = int(m.data)
                use(k)
                k += 1
                continue
            if m.kind == "recent":
                dc = self.info.date_col(subject) or self.info.col(subject, self.info.pk(subject)[0])
                q.order.append((dc, m.data))
                steps.append(Step(m.text, f"sort by <code>{dc.table}.{dc.name}</code> {'newest' if m.data == 'DESC' else 'oldest'} first"))
                use(k)
                k += 1
                continue

            if m.kind == "distinct":
                q.distinct = True
                steps.append(Step(m.text, "each different value once (DISTINCT)"))
                use(k)
                k += 1
                continue

            # --- empty / not empty
            if m.kind in ("empty", "notempty") and last_col is not None:
                q.where.append((last_col, "null" if m.kind == "empty" else "notnull", None))
                steps.append(Step(m.text, f"<code>{last_col.table}.{last_col.name}</code> {'has no value' if m.kind == 'empty' else 'has a value'}"))
                use(k)
                k += 1
                continue

            # --- filters with an explicit column: "price over 20", "status is paid", "name contains 'ann'"
            if m.kind == "col":
                c = pick_col(m.data)
                last_col = c
                if nxt and nxt.kind in ("empty", "notempty"):
                    q.where.append((c, "null" if nxt.kind == "empty" else "notnull", None))
                    steps.append(Step(f"{m.text} {nxt.text}", f"<code>{c.table}.{c.name}</code> {'has no value' if nxt.kind == 'empty' else 'has a value'}"))
                    use(k, k + 1)
                    k += 2
                    continue
                if nxt and nxt.kind == "cmp" and nxt2 and nxt2.kind in ("num", "value"):
                    consumed = self._filter(q, c, nxt.data, nxt2, ms, k + 2, steps, f"{m.text} {nxt.text} {nxt2.text}")
                    use(*range(k, k + 3 + consumed))
                    k += 3 + consumed
                    continue
                if nxt and nxt.kind in ("num", "value") and not (nxt.kind == "num" and nxt2 and nxt2.kind in ("recent", "table")):
                    consumed = self._filter(q, c, "=", nxt, ms, k + 1, steps, f"{m.text} {nxt.text}")
                    use(*range(k, k + 2 + consumed))
                    k += 2 + consumed
                    continue
                # a bare column: something to show
                q.show.append(c)
                steps.append(Step(m.text, f"show <code>{c.table}.{c.name}</code>"))
                use(k)
                k += 1
                continue

            # --- comparison without a column: "in UK", "after 2019", "over 20"
            if m.kind == "cmp" and nxt and nxt.kind in ("num", "value"):
                col = None
                if nxt.kind == "value":
                    col, _ = value_target(nxt)
                elif _is_year(nxt.data) and m.data in (">", "<", ">=", "<=", "in", "=", "between", "from"):
                    col = self.info.date_col(subject)
                    if col is None:
                        for t in q.tables:
                            col = self.info.date_col(t) or col
                if col is None and nxt.kind == "num":
                    col = last_col if last_col and last_col.kind in ("number", "year", "date") else next(
                        (c for c in self.info.cols(subject) if c.kind == "number" and not c.pk and not c.name.endswith("_id")), None)
                if col is not None:
                    consumed = self._filter(q, col, m.data, nxt, ms, k + 1, steps, f"{m.text} {nxt.text}")
                    use(*range(k, k + 2 + consumed))
                    k += 2 + consumed
                    continue

            # --- a stored value on its own: "UK customers", "paid orders"
            if m.kind == "value" and m.data and m.data[0][0]:
                col, v = value_target(m)
                self._filter(q, col, "=", m, ms, k, steps, m.text)
                use(k)
                k += 1
                continue

            # --- secondary table mention: "books by Jane Austen" handled via the value; "orders and customers" → join
            if m.kind == "table" and m.data != subject:
                if self._path(subject, m.data) is not None and m.data not in q.tables:
                    q.need(m.data)
                    q.show_tables.append(m.data)
                    steps.append(Step(m.text, f"include matching <b>{m.data}</b> (joined by foreign key)"))
                use(k)
                k += 1
                continue

            k += 1

        ignored = [m.text for i, m in enumerate(ms) if i not in used
                   and m.kind in ("word", "num", "value", "cmp", "col", "table", "neg", "agg", "group", "order", "top", "empty", "notempty")
                   and not (m.kind == "cmp" and m.text.lower() in ("in", "on", "from", "is", "during"))]
        sql = q.build(steps, warnings)
        confident = not ignored and not warnings
        return Generated(sql, steps, ignored, warnings, confident)

    def _related_by_word(self, subject: str, word: str) -> str | None:
        """'ordered' → order_items for products; 'bought' etc. fall back to the only child table."""
        stem = word.lower()
        for suf in ("ed", "d", "s", "ing"):
            if stem.endswith(suf) and len(stem) - len(suf) >= 3:
                stem = stem[: -len(suf)]
                break
        children = [fk.child for fk in self.info.fks if fk.parent == subject and fk.child != subject]
        neighbours = children + [fk.parent for fk in self.info.fks if fk.child == subject]
        for t in neighbours:
            if stem in t.lower() or singular(t).startswith(stem):
                return t
        return children[0] if len(set(children)) == 1 else None

    def _filter(self, q: "Query", col: Col | None, op: str, vm: M, ms: list[M], vi: int, steps: list[Step], phrase: str) -> int:
        """Add a WHERE condition; returns how many extra mentions after `vm` were consumed (for BETWEEN)."""
        if col is None:
            return 0
        if vm.kind == "value":
            hits = [x for x in vm.data if x[0] == col.table and x[1] == col.name] or vm.data
            value = hits[0][2]
        else:
            value = vm.data
        extra = 0
        q.need(col.table)
        if op == "between":
            if vi + 1 < len(ms) and ms[vi + 1].kind in ("num", "value"):
                hi_m = ms[vi + 1]
                hi = hi_m.data if hi_m.kind == "num" else hi_m.data[0][2]
                q.where.append((col, "between", (value, hi)))
                steps.append(Step(f"{phrase} and {hi_m.text}", f"<code>{col.table}.{col.name}</code> between {value} and {hi}"))
                return 1
            op = ">="
        if op == "from":
            op = ">=" if not isinstance(value, str) else "="
        if col.kind == "date" and _is_year(value) and op in ("in", "=", ">", "<", ">=", "<="):
            y = int(value)
            if op in ("in", "="):
                q.where.append((col, ">=", f"{y}-01-01"))
                q.where.append((col, "<", f"{y + 1}-01-01"))
                steps.append(Step(phrase, f"<code>{col.table}.{col.name}</code> during {y}"))
            else:
                bound = f"{y + 1}-01-01" if op == ">" else f"{y}-01-01"
                real = ">=" if op == ">" else op
                q.where.append((col, real, bound))
                steps.append(Step(phrase, f"<code>{col.table}.{col.name}</code> {op} {y}"))
            return extra
        if op in ("in", "from", "on") or (op in (">=", ">", "<", "<=") and isinstance(value, str) and vm.kind == "value"
                                         and vm.text.lower() in [x[2].lower() for x in vm.data] and phrase.split()[0].lower() in ("from", "since", "in", "on")):
            op = "="
        if op in ("contains", "starts", "ends"):
            pat = {"contains": f"%{value}%", "starts": f"{value}%", "ends": f"%{value}"}[op]
            q.where.append((col, "like", pat))
            steps.append(Step(phrase, f"<code>{col.table}.{col.name}</code> {'contains' if op == 'contains' else op[:-1] + 's with'} '{value}'"))
            return extra
        q.where.append((col, op, value))
        words = {"=": "is", "!=": "is not", ">": "is more than", "<": "is less than", ">=": "is at least", "<=": "is at most"}
        shown = f"'{value}'" if isinstance(value, str) else value
        steps.append(Step(phrase, f"<code>{col.table}.{col.name}</code> {words.get(op, op)} {shown}"))
        return extra


def _number(s: str):
    s2 = s.replace("$", "").replace(",", "")
    pct = s2.endswith("%")
    s2 = s2.rstrip("%")
    try:
        v = float(s2)
    except ValueError:
        return None
    if pct:
        return v
    return int(v) if v.is_integer() else v


def _is_year(v) -> bool:
    return isinstance(v, int) and 1800 <= v <= 2200


class Query:
    def __init__(self, tr: Translator, subject: str):
        self.tr = tr
        self.subject = subject
        self.tables: list[str] = [subject]
        self.where: list[tuple[Col, str, object]] = []
        self.aggs: list[tuple[str, Col | None]] = []
        self.group_cols: list[Col] = []
        self.group_table: str | None = None
        self.order: list[tuple[Col, str]] = []
        self.order_by_agg: str | None = None
        self.limit: int | None = None
        self.distinct = False
        self.show: list[Col] = []
        self.show_tables: list[str] = []
        self.relations: list[tuple[str, str, str | None, int | None]] = []

    def need(self, table: str) -> None:
        if table not in self.tables:
            self.tables.append(table)

    def build(self, steps: list[Step], warnings: list[str]) -> str:
        tr, info, q, qual = self.tr, self.tr.info, self.tr.quote, self.tr.qualify
        aliases: dict[str, str] = {}

        def alias(t: str) -> str:
            if t not in aliases:
                base = re.sub(r"[^a-z]", "", t.lower())[:1] or "t"
                a, i = base, 2
                while a in aliases.values():
                    a, i = f"{base}{i}", i + 1
                aliases[t] = a
            return aliases[t]

        def ref(c: Col) -> str:
            return f"{alias(c.table)}.{q(c.name)}"

        def lit(v) -> str:
            if isinstance(v, (int, float)):
                return str(v)
            return "'" + str(v).replace("'", "''") + "'"

        # grouping by a related table: "count books per author" → FROM authors LEFT JOIN books
        root = self.subject
        group_by_parent = self.group_table and self.group_table != self.subject
        if self.group_table and not self.aggs:
            self.aggs.append(("count", None))
            steps.append(Step("(grouping)", "count the rows in each group"))
        if self.group_cols and not self.aggs:
            self.aggs.append(("count", None))
            steps.append(Step("(grouping)", "count the rows in each group"))
        if group_by_parent:
            root = self.group_table

        # FK joins from the root to every other table we need
        joins: list[str] = []
        joined = {root}
        need = [t for t in self.tables if t != root]
        if group_by_parent and self.subject not in need:
            need.insert(0, self.subject)
        for t in need:
            path = tr._path(root, t)
            if path is None:
                warnings.append(f"There's no foreign-key path between {root} and {t}, so I left {t} out.")
                continue
            for a, b, fk in path:
                if b in joined:
                    continue
                if fk.child == b:
                    cond = " AND ".join(f"{alias(b)}.{q(cc)} = {alias(a)}.{q(pc)}" for cc, pc in zip(fk.child_cols, fk.parent_cols))
                else:
                    cond = " AND ".join(f"{alias(b)}.{q(pc)} = {alias(a)}.{q(cc)}" for cc, pc in zip(fk.child_cols, fk.parent_cols))
                kind = "LEFT JOIN" if group_by_parent else "JOIN"
                joins.append(f"{kind} {qual(b, tr.schema)} AS {alias(b)} ON {cond}")
                joined.add(b)
        alias(root)

        # WHERE
        conds = []
        for c, op, v in self.where:
            if c.table not in joined:
                continue
            r = ref(c)
            if op == "null":
                conds.append(f"{r} IS NULL")
            elif op == "notnull":
                conds.append(f"{r} IS NOT NULL")
            elif op == "between":
                conds.append(f"{r} BETWEEN {lit(v[0])} AND {lit(v[1])}")
            elif op == "like":
                conds.append(f"{r} LIKE {lit(v)}")
            else:
                conds.append(f"{r} {'<>' if op == '!=' else op} {lit(v)}")
        for kind, other, op, count in self.relations:
            path = tr._path(self.subject, other)
            sub_alias = alias(other) + "x"
            inner_conds = []
            # direct link only (most common); longer paths use a join chain inside the subquery
            prev_alias, prev_table = alias(self.subject), self.subject
            sub_from = f"{qual(other, tr.schema)} AS {sub_alias}"
            chain = []
            for i, (a, b, fk) in enumerate(path or []):
                b_alias = sub_alias if b == other else alias(b) + "x"
                if fk.child == b:
                    cond = " AND ".join(f"{b_alias}.{q(cc)} = {prev_alias}.{q(pc)}" for cc, pc in zip(fk.child_cols, fk.parent_cols))
                else:
                    cond = " AND ".join(f"{b_alias}.{q(pc)} = {prev_alias}.{q(cc)}" for cc, pc in zip(fk.child_cols, fk.parent_cols))
                if i == 0:
                    inner_conds.append(cond)
                    if b != other:
                        sub_from = f"{qual(b, tr.schema)} AS {b_alias}"
                else:
                    chain.append(f"JOIN {qual(b, tr.schema)} AS {b_alias} ON {cond}")
                prev_alias, prev_table = b_alias, b
            subq = f"SELECT {'COUNT(*)' if kind == 'count' else '1'} FROM {sub_from} {' '.join(chain)} WHERE {' AND '.join(inner_conds)}".replace("  ", " ")
            if kind == "none":
                conds.append(f"NOT EXISTS ({subq})")
            elif kind == "some":
                conds.append(f"EXISTS ({subq})")
            else:
                conds.append(f"({subq}) {'<>' if op == '!=' else op} {count}")

        # SELECT list
        group_exprs: list[str] = []
        select: list[str] = []
        if self.aggs:
            if group_by_parent:
                g = self.group_table
                pk = info.pk(g)[0]
                disp = info.display_col(g)
                group_exprs = [f"{alias(g)}.{q(pk)}"] + ([f"{alias(g)}.{q(disp)}"] if disp != pk else [])
                select.append(f"{alias(g)}.{q(disp)}")
            for c in self.group_cols:
                group_exprs.append(ref(c))
                select.append(ref(c))
            agg_names = []
            for func, c in self.aggs:
                if func == "count":
                    expr = f"COUNT({alias(self.subject)}.{q(info.pk(self.subject)[0])})" if group_by_parent else "COUNT(*)"
                    name = f"{self.subject}_count" if group_by_parent or self.group_cols else "count"
                else:
                    if c is None:
                        c = next((x for x in info.cols(self.subject) if x.kind == "number" and not x.pk and not x.name.endswith("_id")), None)
                        if c is None:
                            warnings.append(f"I couldn't find a number column to {func}.")
                            continue
                        tr_need = c.table
                        if tr_need not in joined:
                            continue
                    expr = f"{func.upper()}({ref(c)})"
                    name = f"{func}_{c.name}"
                agg_names.append(name)
                select.append(f"{expr} AS {q(name)}")
            if (group_exprs) and not self.order:
                d = self.order_by_agg or "DESC"
                if agg_names:
                    self.order_sql = [f"{q(agg_names[0])} {d}"]
        else:
            if self.show:
                select = [ref(c) for c in self.show if c.table in joined]
            else:
                select = [f"{alias(self.subject)}.*"]
                for t in self.tables:
                    if t != self.subject and t in joined and (t in self.show_tables or any(c.table == t for c, *_ in self.where)):
                        disp = info.display_col(t)
                        select.append(f"{alias(t)}.{q(disp)} AS {q(singular(t) + '_' + disp)}")
        if not select:
            select = ["*"]

        sql = "SELECT " + ("DISTINCT " if self.distinct else "") + ", ".join(select)
        sql += f"\nFROM {qual(root, tr.schema)} AS {alias(root)}"
        for j in joins:
            sql += "\n" + j
        if conds:
            sql += "\nWHERE " + "\n  AND ".join(conds)
        if group_exprs:
            sql += "\nGROUP BY " + ", ".join(group_exprs)
        order_sql = getattr(self, "order_sql", None) or [f"{ref(c)} {d}" for c, d in self.order if c.table in joined]
        order_clause = ("ORDER BY " + ", ".join(order_sql)) if order_sql else ""
        limit = self.limit or (1000 if (not self.aggs or group_exprs) else None)
        if limit:
            sql = tr.limit_fn(sql, int(limit), 0, order_clause)  # LIMIT, TOP or OFFSET/FETCH depending on the database
        elif order_clause:
            sql += "\n" + order_clause
        return sql + ";"


def _limit_clause(select_sql: str, limit: int, offset: int = 0, order_sql: str = "") -> str:
    return select_sql + (f"\n{order_sql}" if order_sql else "") + f"\nLIMIT {int(limit)}" + (f" OFFSET {int(offset)}" if offset else "")


def translate(info: SchemaInfo, text: str, quote, qualify, schema: str | None = None, limit_fn=None) -> Generated:
    """limit_fn(select_sql, limit, offset, order_sql) writes the row limit in the database's dialect (driver.select_limit)."""
    return Translator(info, quote, qualify, schema, limit_fn).translate(text)
