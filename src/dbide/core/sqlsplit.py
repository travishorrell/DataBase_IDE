"""Split a SQL script into statements without being fooled by quotes or comments."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Statement:
    text: str
    start: int  # offset in the original script (inclusive)
    end: int  # offset in the original script (exclusive)


def split_sql(script: str, hash_comments: bool = False) -> list[Statement]:
    """Split on top-level semicolons.

    Handles '...', "...", `...` (with doubled-quote and backslash escapes),
    -- line comments, /* block */ comments and, for MySQL, # line comments.
    SQLite trigger bodies (CREATE TRIGGER ... BEGIN ...; END) are kept whole.
    """
    out: list[Statement] = []
    i, n = 0, len(script)
    stmt_start = 0
    depth_begin = 0  # nesting for BEGIN ... END in CREATE TRIGGER
    word = []

    def flush(end: int) -> None:
        nonlocal stmt_start
        raw = script[stmt_start:end]
        text = raw.strip()
        if text and not _only_comments(text, hash_comments):
            lead = len(raw) - len(raw.lstrip())
            out.append(Statement(text, stmt_start + lead, stmt_start + lead + len(text)))
        stmt_start = end + 1

    def end_word() -> None:
        nonlocal depth_begin
        if not word:
            return
        w = "".join(word).upper()
        word.clear()
        head = script[stmt_start:i].lstrip()[:40].upper()
        if "TRIGGER" in head and head.startswith("CREATE"):
            if w == "BEGIN" or (w == "CASE" and depth_begin):
                depth_begin += 1
            elif w == "END" and depth_begin:
                depth_begin -= 1

    while i < n:
        ch = script[i]
        nxt = script[i + 1] if i + 1 < n else ""
        if ch in "'\"`":
            end_word()
            i = _skip_quoted(script, i, ch)
            continue
        if ch == "-" and nxt == "-" or (hash_comments and ch == "#"):
            end_word()
            j = script.find("\n", i)
            i = n if j == -1 else j + 1
            continue
        if ch == "/" and nxt == "*":
            end_word()
            j = script.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        if ch.isalnum() or ch == "_":
            word.append(ch)
            i += 1
            continue
        end_word()
        if ch == ";" and depth_begin == 0:
            flush(i)
        i += 1
    end_word()
    flush(n)
    return out


def _skip_quoted(s: str, i: int, q: str) -> int:
    n = len(s)
    i += 1
    while i < n:
        c = s[i]
        if c == "\\" and q != "`":
            i += 2
            continue
        if c == q:
            if i + 1 < n and s[i + 1] == q:
                i += 2
                continue
            return i + 1
        i += 1
    return n


def _only_comments(text: str, hash_comments: bool) -> bool:
    i, n = 0, len(text)
    while i < n:
        if text[i].isspace():
            i += 1
        elif text.startswith("--", i) or (hash_comments and text[i] == "#"):
            j = text.find("\n", i)
            i = n if j == -1 else j + 1
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
        else:
            return False
    return True


def statement_at(script: str, pos: int, hash_comments: bool = False) -> Statement | None:
    """Return the statement under the cursor (or the nearest one before it)."""
    stmts = split_sql(script, hash_comments)
    if not stmts:
        return None
    best = None
    for st in stmts:
        if st.start <= pos <= st.end + 1:
            return st
        if st.end < pos:
            best = st
    return best or stmts[0]
