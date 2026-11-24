"""The Help panel: explains whatever you hover, type or do; plus Beginner mode."""
from __future__ import annotations

import html

from PySide6.QtCore import QEvent, QObject, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QTextBrowser, QVBoxLayout, QWidget

from dbide.core import knowledge
from dbide.core.settings import settings

HELP_PROP = "dbide_help"


class HelpCenter(QObject):
    """App-wide hub. Anything can call show_topic()/show_html(); the panel listens."""

    changed = Signal(str, str)  # title, html
    beginner_changed = Signal(bool)

    _instance: "HelpCenter | None" = None

    @classmethod
    def get(cls) -> "HelpCenter":
        if cls._instance is None:
            cls._instance = HelpCenter()
        return cls._instance

    def __init__(self):
        super().__init__()
        self._beginner = bool(settings().get("beginner_mode"))
        self._last_key = None
        self._hold_until = 0.0

    @property
    def beginner(self) -> bool:
        return self._beginner

    def set_beginner(self, on: bool) -> None:
        if on != self._beginner:
            self._beginner = on
            settings().set("beginner_mode", on)
            self.beginner_changed.emit(on)

    def show_topic(self, key: str | None, passive: bool = False) -> None:
        """passive=True (cursor-following) doesn't replace an explanation the user just asked for."""
        import time

        if not key or key == self._last_key:
            return
        if passive and time.monotonic() < self._hold_until:
            return
        t = knowledge.topic(key)
        if t:
            self._last_key = key
            self.changed.emit(t[0], t[1])

    def show_html(self, title: str, body: str, hold: float = 4.0) -> None:
        import time

        self._last_key = None
        self._hold_until = time.monotonic() + hold
        self.changed.emit(title, body)


def tag(obj, key: str) -> None:
    """Attach a help topic to a widget or QAction."""
    if isinstance(obj, QAction):
        obj.hovered.connect(lambda: HelpCenter.get().show_topic(key))
        t = knowledge.topic(key)
        if t and not obj.toolTip().strip().endswith("…"):
            obj.setStatusTip(re_strip(t[1])[:160])
    else:
        obj.setProperty(HELP_PROP, key)


def re_strip(s: str) -> str:
    import re

    return html.unescape(re.sub(r"<[^>]+>", "", s))


class HoverFilter(QObject):
    """Shows a widget's help topic when the mouse enters it (or any child of it)."""

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.Enter and isinstance(obj, QWidget):
            w = obj
            for _ in range(4):
                if w is None:
                    break
                key = w.property(HELP_PROP)
                if key:
                    HelpCenter.get().show_topic(key)
                    break
                w = w.parentWidget()
        return False


def install_hover_help(app: QApplication) -> HoverFilter:
    f = HoverFilter(app)
    app.installEventFilter(f)
    return f


class HelpPanel(QWidget):
    def __init__(self):
        super().__init__()
        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(False)
        self.view.setOpenLinks(False)
        self.view.anchorClicked.connect(self._link)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.view)
        self._last = None
        hc = HelpCenter.get()
        hc.changed.connect(self._show)
        from dbide.ui.theme import ThemeManager

        self._style(ThemeManager.get().theme)
        ThemeManager.get().on_change(self, self._style)
        self._pending = None
        self._timer = QTimer(self, singleShot=True, interval=120)  # avoid flicker while moving the mouse
        self._timer.timeout.connect(self._flush)
        self._show("Welcome", self.intro())

    def _style(self, t) -> None:
        self.view.document().setDefaultStyleSheet(
            f"h3 {{ color: {t.text}; margin-top: 2px; }}"
            f"code {{ background-color: {t.surface_alt}; color: {t.syntax.get('kw', t.accent)}; }}"
            f"pre {{ background-color: {t.surface_alt}; color: {t.text}; padding: 8px; }}"
            f"a {{ color: {t.accent}; text-decoration: none; }}"
            f"li {{ margin-bottom: 3px; }}"
        )
        self.view.document().setDocumentMargin(12)
        if self._last:
            self._render(*self._last)

    @staticmethod
    def intro() -> str:
        concepts = "".join(
            f'<li><a href="topic:{k}">{html.escape(v[0])}</a></li>' for k, v in knowledge.CONCEPTS.items()
        )
        return (
            "<p>This panel explains whatever you point at or type: buttons, SQL keywords under the cursor, "
            "and fields in the table designer. Errors get a plain-English explanation here too.</p>"
            f"<p><b>Concepts</b></p><ul>{concepts}</ul>"
            "<p>New to databases? Open <b>Help → Lessons</b>.</p>"
        )

    def _show(self, title: str, body: str) -> None:
        self._pending = (title, body)
        self._timer.start()

    def _flush(self) -> None:
        if not self._pending:
            return
        self._render(*self._pending)

    def _render(self, title: str, body: str) -> None:
        self._last = (title, body)
        self.view.setHtml(
            f"<h3 style='margin-bottom:4px'>{html.escape(title)}</h3>{body}"
            "<p style='margin-top:14px'><a href='home'>All concepts →</a></p>"
        )

    def _link(self, url) -> None:
        s = url.toString()
        if s == "home":
            HelpCenter.get().show_html("Help", self.intro())
        elif s.startswith("topic:"):
            HelpCenter.get()._last_key = None
            HelpCenter.get().show_topic(s[6:])


def word_at_cursor(editor) -> str:
    cur = editor.textCursor()
    block = cur.block().text()
    pos = cur.positionInBlock()
    start = pos
    while start > 0 and (block[start - 1].isalnum() or block[start - 1] == "_"):
        start -= 1
    end = pos
    while end < len(block) and (block[end].isalnum() or block[end] == "_"):
        end += 1
    return block[start:end]


def follow_editor(editor) -> None:
    """Explain the SQL keyword under the cursor (debounced)."""
    timer = QTimer(editor, singleShot=True, interval=300)

    def fire():
        key = knowledge.keyword_topic(word_at_cursor(editor))
        if key:
            HelpCenter.get().show_topic(key, passive=True)

    timer.timeout.connect(fire)
    editor.cursorPositionChanged.connect(timer.start)
