"""SQL code editor: line numbers, syntax highlighting, autocomplete."""
from __future__ import annotations

import re

from PySide6.QtCore import QRect, QSize, QStringListModel, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QKeyEvent,
    QPainter,
    QPalette,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import QCompleter, QPlainTextEdit, QTextEdit, QWidget


def monospace_font(size: int | None = None) -> QFont:
    """The editor font from Appearance settings (optionally scaled relative to its default size)."""
    from dbide.ui.theme import ThemeManager

    font = ThemeManager.get().editor_font()
    if size is not None and size < 13:
        font.setPointSizeF(font.pointSizeF() * size / 13)
    return font


def _fmt(color: str, bold: bool = False, italic: bool = False) -> QTextCharFormat:
    f = QTextCharFormat()
    f.setForeground(QColor(color))
    if bold:
        f.setFontWeight(QFont.Weight.Bold)
    f.setFontItalic(italic)
    return f


class SqlHighlighter(QSyntaxHighlighter):
    def __init__(self, document, keywords: list[str], functions: list[str], hash_comments: bool = False):
        super().__init__(document)
        self.hash_comments = hash_comments
        self.keywords: set[str] = set()
        self.functions: set[str] = set()
        self.set_colors()
        self.set_words(keywords, functions)

    def set_colors(self) -> None:
        from dbide.ui.theme import theme

        pal = theme().syntax
        self.kw_fmt = _fmt(pal["kw"], bold=True)
        self.fn_fmt = _fmt(pal["fn"])
        self.str_fmt = _fmt(pal["str"])
        self.num_fmt = _fmt(pal["num"])
        self.cmt_fmt = _fmt(pal["cmt"], italic=True)
        self.ident_fmt = _fmt(pal["ident"])
        self.rehighlight()

    def set_words(self, keywords: list[str], functions: list[str]) -> None:
        self.keywords = {k.upper() for k in keywords}
        self.functions = {f.upper() for f in functions}
        self.rehighlight()

    _token = re.compile(
        r"(?P<word>[A-Za-z_][A-Za-z0-9_$]*)|(?P<num>\b\d+(\.\d+)?\b)|(?P<str>'(?:[^'\\]|\\.|'')*'?)"
        r"|(?P<ident>\"(?:[^\"]|\"\")*\"?|`[^`]*`?)|(?P<line>--[^\n]*)|(?P<hash>#[^\n]*)|(?P<block>/\*)"
    )

    def highlightBlock(self, text: str) -> None:
        start = 0
        if self.previousBlockState() == 1:
            end = text.find("*/")
            if end == -1:
                self.setFormat(0, len(text), self.cmt_fmt)
                self.setCurrentBlockState(1)
                return
            self.setFormat(0, end + 2, self.cmt_fmt)
            start = end + 2
        self.setCurrentBlockState(0)
        pos = start
        while True:
            m = self._token.search(text, pos)
            if not m:
                break
            kind = m.lastgroup
            s, e = m.start(), m.end()
            if kind == "word":
                w = m.group().upper()
                following = text[e : e + 1]
                if w in self.keywords:
                    self.setFormat(s, e - s, self.kw_fmt)
                elif w in self.functions and following == "(":
                    self.setFormat(s, e - s, self.fn_fmt)
            elif kind == "num":
                self.setFormat(s, e - s, self.num_fmt)
            elif kind == "str":
                self.setFormat(s, e - s, self.str_fmt)
            elif kind == "ident":
                self.setFormat(s, e - s, self.ident_fmt)
            elif kind == "line" or (kind == "hash" and self.hash_comments):
                self.setFormat(s, len(text) - s, self.cmt_fmt)
                break
            elif kind == "hash":
                pass
            elif kind == "block":
                close = text.find("*/", e)
                if close == -1:
                    self.setFormat(s, len(text) - s, self.cmt_fmt)
                    self.setCurrentBlockState(1)
                    break
                self.setFormat(s, close + 2 - s, self.cmt_fmt)
                e = close + 2
            pos = max(e, s + 1)


class _LineNumbers(QWidget):
    def __init__(self, editor: "SqlEditor"):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self) -> QSize:
        return QSize(self.editor.gutter_width(), 0)

    def paintEvent(self, event) -> None:
        self.editor.paint_gutter(event)


class SqlEditor(QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFont(monospace_font())
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setFrameShape(QPlainTextEdit.Shape.NoFrame)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self._gutter = _LineNumbers(self)
        self.blockCountChanged.connect(self._update_gutter_width)
        self.updateRequest.connect(self._update_gutter)
        self.cursorPositionChanged.connect(self._highlight_line)
        self._update_gutter_width()
        self._highlight_line()

        self.catalog: dict[str, list[str]] = {}
        from dbide.drivers.base import SQL_FUNCTIONS, SQL_KEYWORDS

        # generic SQL until a connection supplies its dialect's words
        self.keywords, self.functions = list(SQL_KEYWORDS), list(SQL_FUNCTIONS)
        self.highlighter = SqlHighlighter(self.document(), self.keywords, self.functions)
        from dbide.ui.theme import ThemeManager

        ThemeManager.get().on_change(self, self._theme_changed)

        self.completer = QCompleter(self)
        self.completer.setWidget(self)
        self.completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.completer.setFilterMode(Qt.MatchFlag.MatchStartsWith)
        self.completer.setModel(QStringListModel([], self.completer))
        self.completer.activated.connect(self._insert_completion)

    def event(self, e):
        from PySide6.QtCore import QEvent

        from dbide.ui.help_panel import HelpCenter

        if e.type() == QEvent.Type.ToolTip and HelpCenter.get().beginner:
            from PySide6.QtWidgets import QToolTip

            from dbide.core import knowledge

            cur = self.cursorForPosition(e.pos())
            cur.select(QTextCursor.SelectionType.WordUnderCursor)
            word = cur.selectedText()
            key = knowledge.keyword_topic(word) if word else None
            tip = knowledge.short_tip(key) if key else None
            if tip:
                QToolTip.showText(e.globalPos(), tip, self)
            else:
                QToolTip.hideText()
            return True
        return super().event(e)

    def _theme_changed(self, _theme) -> None:
        self.setFont(monospace_font())
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.highlighter.set_colors()
        self._update_gutter_width()
        self._highlight_line()

    # ---- vocab ------------------------------------------------------------
    def set_dialect(self, keywords: list[str], functions: list[str], hash_comments: bool) -> None:
        self.keywords, self.functions = keywords, functions
        self.highlighter.hash_comments = hash_comments
        self.highlighter.set_words(keywords, functions)

    def set_catalog(self, catalog: dict[str, list[str]]) -> None:
        self.catalog = catalog

    # ---- completion -------------------------------------------------------
    def _word_before_cursor(self) -> str:
        cur = self.textCursor()
        block = cur.block().text()[: cur.positionInBlock()]
        m = re.search(r"[A-Za-z0-9_$.]*$", block)
        return m.group() if m else ""

    def _candidates(self, word: str) -> list[str]:
        if "." in word:
            table = word.rsplit(".", 1)[0].split(".")[-1].strip("`\"")
            alias_target = self._resolve_alias(table)
            cols = self.catalog.get(alias_target) or self.catalog.get(table) or []
            return sorted(set(cols), key=str.lower)
        words = set(self.keywords) | {f for f in self.functions} | set(self.catalog)
        for cols in self.catalog.values():
            words.update(cols)
        return sorted(words, key=str.lower)

    def _resolve_alias(self, alias: str) -> str:
        """`FROM users u` / `JOIN users AS u` -> 'users' for alias 'u'."""
        pattern = rf"(?:FROM|JOIN)\s+[`\"]?([A-Za-z0-9_$]+)[`\"]?(?:\s+AS)?\s+{re.escape(alias)}\b"
        m = re.search(pattern, self.toPlainText(), re.IGNORECASE)
        return m.group(1) if m else alias

    def _show_completions(self, force: bool = False) -> None:
        word = self._word_before_cursor()
        prefix = word.rsplit(".", 1)[-1]
        if not force and len(prefix) < 2 and not word.endswith("."):
            self.completer.popup().hide()
            return
        cands = self._candidates(word)
        self.completer.model().setStringList(cands)
        self.completer.setCompletionPrefix(prefix)
        if self.completer.completionCount() == 0 or (
            self.completer.completionCount() == 1 and self.completer.currentCompletion() == prefix
        ):
            self.completer.popup().hide()
            return
        rect = self.cursorRect()
        rect.setWidth(
            self.completer.popup().sizeHintForColumn(0) + self.completer.popup().verticalScrollBar().sizeHint().width()
        )
        self.completer.complete(rect)
        self.completer.popup().setCurrentIndex(self.completer.completionModel().index(0, 0))

    def _insert_completion(self, completion: str) -> None:
        prefix = self._word_before_cursor().rsplit(".", 1)[-1]
        cur = self.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.Left, QTextCursor.MoveMode.KeepAnchor, len(prefix))
        is_kw = completion.upper() in {k.upper() for k in self.keywords} and completion not in self.catalog
        cur.insertText(completion.upper() if is_kw and prefix.isupper() else completion)
        self.setTextCursor(cur)

    def keyPressEvent(self, e: QKeyEvent) -> None:
        popup = self.completer.popup()
        if popup.isVisible() and e.key() in (
            Qt.Key.Key_Enter, Qt.Key.Key_Return, Qt.Key.Key_Tab, Qt.Key.Key_Escape, Qt.Key.Key_Backtab,
        ):
            if e.key() == Qt.Key.Key_Escape:
                popup.hide()
            else:
                e.ignore()  # QCompleter handles the choice
            return
        # Ctrl+Space (on macOS the physical Ctrl key is MetaModifier; Cmd+Space is Spotlight)
        if e.key() == Qt.Key.Key_Space and e.modifiers() & (
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier
        ):
            self._show_completions(force=True)
            return
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not e.modifiers():
            # keep indentation
            line = self.textCursor().block().text()
            indent = line[: len(line) - len(line.lstrip())]
            super().keyPressEvent(e)
            self.insertPlainText(indent)
            return
        if e.key() == Qt.Key.Key_Tab and not e.modifiers():
            self.insertPlainText("    ")
            return
        if e.key() == Qt.Key.Key_Slash and e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.toggle_comment()
            return
        super().keyPressEvent(e)
        text = e.text()
        if text and (text.isalnum() or text in "_."):
            self._show_completions()
        elif popup.isVisible():
            popup.hide()

    def toggle_comment(self) -> None:
        cur = self.textCursor()
        start, end = sorted((cur.selectionStart(), cur.selectionEnd()))
        doc = self.document()
        first = doc.findBlock(start)
        last = doc.findBlock(end)
        blocks = []
        b = first
        while b.isValid():
            blocks.append(b)
            if b == last:
                break
            b = b.next()
        uncomment = all(bl.text().lstrip().startswith("--") or not bl.text().strip() for bl in blocks)
        cur.beginEditBlock()
        for bl in blocks:
            t = bl.text()
            c = QTextCursor(bl)
            if uncomment:
                i = t.find("--")
                if i >= 0:
                    c.setPosition(bl.position() + i)
                    n = 3 if t[i : i + 3] == "-- " else 2
                    c.setPosition(bl.position() + i + n, QTextCursor.MoveMode.KeepAnchor)
                    c.removeSelectedText()
            elif t.strip():
                c.insertText("-- ")
        cur.endEditBlock()

    # ---- gutter -----------------------------------------------------------
    def gutter_width(self) -> int:
        digits = max(3, len(str(self.blockCount())))
        return 22 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_gutter_width(self, *_):
        self.setViewportMargins(self.gutter_width(), 0, 0, 0)

    def _update_gutter(self, rect, dy):
        if dy:
            self._gutter.scroll(0, dy)
        else:
            self._gutter.update(0, rect.y(), self._gutter.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_gutter_width()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        cr = self.contentsRect()
        self._gutter.setGeometry(QRect(cr.left(), cr.top(), self.gutter_width(), cr.height()))

    def paint_gutter(self, event) -> None:
        painter = QPainter(self._gutter)
        pal = self.palette()
        painter.fillRect(event.rect(), pal.color(QPalette.ColorRole.Base))
        block = self.firstVisibleBlock()
        num = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        current = self.textCursor().blockNumber()
        dim = pal.color(QPalette.ColorRole.PlaceholderText)
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.setPen(pal.color(QPalette.ColorRole.Text) if num == current else dim)
                painter.drawText(
                    0, top, self._gutter.width() - 12, self.fontMetrics().height(),
                    Qt.AlignmentFlag.AlignRight, str(num + 1),
                )
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            num += 1

    def _highlight_line(self) -> None:
        sel = QTextEdit.ExtraSelection()
        color = self.palette().color(QPalette.ColorRole.Highlight)
        color.setAlpha(22)
        sel.format.setBackground(color)
        sel.format.setProperty(QTextCharFormat.Property.FullWidthSelection, True)
        sel.cursor = self.textCursor()
        sel.cursor.clearSelection()
        self.setExtraSelections([sel])
        self._gutter.update()
