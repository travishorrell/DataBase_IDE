"""Skills, levels and which features are shown (progressive disclosure).

The IDE notices what you do (your first query, filtering, a JOIN, creating a
table…) and records each new skill. Skills add up to a level; each level reveals
more tools. Nothing is ever truly locked: "Show all features" (or Unlock now on
any locked item) jumps straight to the expert level.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

from dbide.core.settings import settings


@dataclass(frozen=True)
class Skill:
    key: str
    title: str
    how: str  # how to earn it (shown in the Progress panel)


SKILLS: list[Skill] = [
    Skill("first_query", "Run your first query", "Run any SELECT, or ask a question in plain English."),
    Skill("ask", "Ask in plain English", "Type a question in the Ask bar and press Enter."),
    Skill("filter", "Filter rows (WHERE)", "Run a query with WHERE, e.g. customers in UK."),
    Skill("sort", "Sort results (ORDER BY)", "Sort with ORDER BY, or ask for the top 5 of something."),
    Skill("edit_data", "Change data", "Edit a cell in a table grid and click Apply, or run an UPDATE."),
    Skill("undo", "Undo a change", "In the Playground, change something and click Undo."),
    Skill("join", "Combine tables (JOIN)", "Run a query that joins two tables."),
    Skill("group", "Summarise (GROUP BY / COUNT)", "Count or total something per group, e.g. orders by status."),
    Skill("create_table", "Create a table", "Create a table in the Schema Designer, Table Designer or with CREATE TABLE."),
    Skill("relationship", "Link tables (foreign key)", "Drag between two tables in the Schema Designer, or use REFERENCES."),
    Skill("lesson", "Finish a lesson step", "Open Lessons and pass a 'Check My Work'."),
    Skill("template", "Start from a template", "Create a project from a template, or open one in the Playground."),
]
SKILL_KEYS = {s.key for s in SKILLS}

LEVELS = [(0, "Beginner"), (4, "Explorer"), (8, "Builder"), (11, "Pro")]  # points needed → name


@dataclass(frozen=True)
class Feature:
    key: str
    title: str
    level: int  # index into LEVELS
    why: str  # what it is, in one line


FEATURES: dict[str, Feature] = {f.key: f for f in [
    Feature("history", "Query History", 1, "Every statement you've run, ready to reuse."),
    Feature("export", "Export & Import", 1, "Save results as CSV/JSON/SQL, or load a CSV into a table."),
    Feature("diagram", "Diagram (ERD)", 1, "See all tables and how their keys connect."),
    Feature("join_viz", "Visualize JOIN", 1, "Draw which rows a JOIN matches."),
    Feature("chart_custom", "Chart customization", 1, "Pick the chart type and columns yourself."),
    Feature("designer", "Schema Designer", 1, "Build tables and relationships by drag and drop."),
    Feature("table_advanced", "Indexes, foreign keys & column roles", 2, "Advanced tabs in the Table Designer."),
    Feature("run_all", "Run All & Max rows", 2, "Run whole scripts and control how many rows come back."),
    Feature("password_tool", "Password & Encryption Tool", 2, "Hash, verify and encrypt values by hand."),
    Feature("query_plan", "Query plan (EXPLAIN)", 3, "See how the database executes a query."),
    Feature("security", "Security Settings", 3, "Choose where saved logins and encryption keys are kept."),
    Feature("admin", "Admin Console", 3, "Health checks, maintenance, backups, running queries, users and settings."),
    Feature("sql_preview", "SQL previews open by default", 3, "Designers show the generated SQL without a click."),
]}


def analyze_sql(sql: str, ok: bool) -> set[str]:
    """Which skills a successfully-run statement demonstrates."""
    if not ok:
        return set()
    s = " " + re.sub(r"'[^']*'", "''", sql.upper()) + " "
    got = set()
    head = s.split(None, 1)[0] if s.strip() else ""
    if head in ("SELECT", "WITH"):
        got.add("first_query")
    if " WHERE " in s:
        got.add("filter")
    if " ORDER BY " in s or " LIMIT " in s:
        got.add("sort")
    if " JOIN " in s:
        got.add("join")
    if " GROUP BY " in s or re.search(r"\b(COUNT|SUM|AVG|MIN|MAX)\s*\(", s):
        got.add("group")
    if head in ("INSERT", "UPDATE", "DELETE"):
        got.add("edit_data")
    if head == "CREATE" and " TABLE " in s:
        got.add("create_table")
    if " REFERENCES " in s or " FOREIGN KEY " in s:
        got.add("relationship")
    return got


class Progress:
    def __init__(self):
        self.listeners: list = []  # fn(event: str, payload) — "skill", "level"

    # ---- persisted state
    def _state(self) -> dict:
        st = settings().get("progress") or {}
        st.setdefault("skills", {})
        st.setdefault("override", None)
        return st

    def _save(self, st: dict) -> None:
        settings().set("progress", st)

    @property
    def skills(self) -> set[str]:
        return set(self._state()["skills"]) & SKILL_KEYS

    @property
    def points(self) -> int:
        return len(self.skills)

    @property
    def earned_level(self) -> int:
        lvl = 0
        for i, (need, _name) in enumerate(LEVELS):
            if self.points >= need:
                lvl = i
        return lvl

    @property
    def level(self) -> int:
        from dbide.core.mode import is_power

        if is_power():
            return len(LEVELS) - 1  # power users get every tool; levels are a learning-mode idea
        override = self._state().get("override")
        return max(self.earned_level, override if isinstance(override, int) else 0)

    @property
    def level_name(self) -> str:
        return LEVELS[self.level][1]

    def next_level(self) -> tuple[str, int] | None:
        """(name, skills still needed) for the next level, or None at the top."""
        if self.level + 1 >= len(LEVELS):
            return None
        need, name = LEVELS[self.level + 1]
        return name, max(0, need - self.points)

    def allowed(self, feature: str) -> bool:
        f = FEATURES.get(feature)
        return f is None or self.level >= f.level

    def unlocked_at(self, level: int) -> list[Feature]:
        return [f for f in FEATURES.values() if f.level == level]

    # ---- changes
    def record(self, *skill_keys: str) -> list[str]:
        """Note new skills; returns the ones that were new. Fires level-up when a threshold is crossed."""
        st = self._state()
        before = self.level
        new = [k for k in skill_keys if k in SKILL_KEYS and k not in st["skills"]]
        if not new:
            return []
        for k in new:
            st["skills"][k] = time.time()
        self._save(st)
        for k in new:
            self._emit("skill", k)
        if self.level > before:
            self._emit("level", self.level)
        return new

    def record_sql(self, sql: str, ok: bool) -> list[str]:
        return self.record(*analyze_sql(sql, ok))

    def set_override(self, level: int | None) -> None:
        st = self._state()
        before = self.level
        st["override"] = level
        self._save(st)
        if self.level != before:
            self._emit("level", self.level)

    def reset(self) -> None:
        before = self.level
        self._save({"skills": {}, "override": None})
        if self.level != before:
            self._emit("level", self.level)

    def _emit(self, event: str, payload) -> None:
        for fn in list(self.listeners):
            try:
                fn(event, payload)
            except Exception:
                pass


_progress: Progress | None = None


def progress() -> Progress:
    global _progress
    if _progress is None:
        _progress = Progress()
    return _progress


def reset_for_tests() -> None:
    global _progress
    _progress = None
