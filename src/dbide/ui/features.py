"""Progressive disclosure in the UI.

- Toolbar buttons for locked features are hidden (less clutter for beginners).
- Menu items stay visible with a 🔒 and, when chosen, explain how to unlock them,
  with "Unlock now" so experienced developers are never blocked.
- Panels/widgets bound to a feature appear when it unlocks.
- A small notice announces each new level and what it revealed.
"""
from __future__ import annotations

import html

import shiboken6
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget

from dbide.core.progress import FEATURES, LEVELS, SKILLS, progress


class Gate(QObject):
    changed = Signal(int)  # level
    level_up = Signal(int)
    skill_learned = Signal(str)

    _instance: "Gate | None" = None

    @classmethod
    def get(cls) -> "Gate":
        if cls._instance is None or not shiboken6.isValid(cls._instance):
            cls._instance = Gate()
        inst = cls._instance
        pr = progress()
        if inst._on_progress not in pr.listeners:  # the tracker may have been recreated
            pr.listeners.append(inst._on_progress)
        return inst

    def __init__(self):
        super().__init__()
        self._bound: list = []

    def _on_progress(self, event: str, payload) -> None:
        if event == "skill":
            self.skill_learned.emit(payload)
        elif event == "level":
            self.refresh()
            self.level_up.emit(payload)

    def on(self, signal, owner, fn) -> None:
        """Connect fn to one of the gate's signals for as long as `owner` (a QObject) lives."""

        def slot(*args):
            if shiboken6.isValid(owner):
                fn(*args)

        signal.connect(slot)

        def drop(*_):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

        owner.destroyed.connect(drop)

    @staticmethod
    def allowed(feature: str) -> bool:
        return progress().allowed(feature)

    # ---- binding ------------------------------------------------------------
    def bind_widget(self, widget: QWidget, feature: str) -> None:
        """Show `widget` only once `feature` is unlocked."""
        self._bound.append(("widget", widget, feature, None))
        widget.setVisible(self.allowed(feature))

    def bind_action(self, action, feature: str) -> None:
        """Show a QAction (e.g. the one QToolBar.addWidget returns) only once `feature` is unlocked."""
        self._bound.append(("action", action, feature, None))
        action.setVisible(self.allowed(feature))

    def bind_tool(self, toolbar, action, feature: str) -> None:
        """Keep `action` off the toolbar while `feature` is locked (the menu item stays, with a lock)."""
        order = toolbar.property("dbide_order")
        if order is None:
            order = list(toolbar.actions())  # remember the full order so unlocked items return to their place
            toolbar.setProperty("dbide_order", order)
        self._bound.append(("tool", toolbar, feature, action))
        self._apply_tool(toolbar, action, feature)

    def bind_menu(self, action, feature: str) -> None:
        """Mark a menu action with a lock while `feature` is locked."""
        action.setProperty("dbide_plain_text", action.text())
        self._bound.append(("menu", action, feature, None))
        self._apply_menu(action, feature)

    def bind_tab(self, tabs, page: QWidget, feature: str) -> None:
        """Show a tab page of a QTabWidget only once `feature` is unlocked."""
        self._bound.append(("tab", tabs, feature, page))
        self._apply_tab(tabs, page, feature)

    def _apply_tool(self, toolbar, action, feature):
        present = action in toolbar.actions()
        if self.allowed(feature) and not present:
            order = toolbar.property("dbide_order") or []
            after = order[order.index(action) + 1:] if action in order else []
            before = next((a for a in after if a in toolbar.actions()), None)
            if before is not None:
                toolbar.insertAction(before, action)
            else:
                toolbar.addAction(action)
        elif not self.allowed(feature) and present:
            toolbar.removeAction(action)

    def _apply_menu(self, action, feature):
        base = action.property("dbide_plain_text") or action.text()
        action.setText(base if self.allowed(feature) else f"{base}  🔒")

    def _apply_tab(self, tabs, page, feature):
        i = tabs.indexOf(page)
        if i >= 0:
            tabs.setTabVisible(i, self.allowed(feature))

    def refresh(self) -> None:
        alive = []
        for kind, obj, feature, extra in self._bound:
            if not shiboken6.isValid(obj) or (extra is not None and not shiboken6.isValid(extra)):
                continue
            if kind == "widget":
                obj.setVisible(self.allowed(feature))
            elif kind == "action":
                obj.setVisible(self.allowed(feature))
            elif kind == "tool":
                self._apply_tool(obj, extra, feature)
            elif kind == "menu":
                self._apply_menu(obj, feature)
            elif kind == "tab":
                self._apply_tab(obj, extra, feature)
            alive.append((kind, obj, feature, extra))
        self._bound = alive
        self.changed.emit(progress().level)

    # ---- guarding -------------------------------------------------------------
    def guarded(self, feature: str, slot, parent: QWidget | None = None):
        """A slot that runs only when the feature is unlocked, otherwise explains how to unlock it."""

        def run(*args):
            if self.allowed(feature) or self.explain_locked(feature, parent):
                return slot()
            return None

        return run

    def explain_locked(self, feature: str, parent: QWidget | None = None) -> bool:
        """Explain a locked feature. Returns True if the user chose to unlock it now."""
        f = FEATURES[feature]
        pr = progress()
        lvl_name = LEVELS[f.level][1]
        need = max(0, LEVELS[f.level][0] - pr.points)
        missing = [s for s in SKILLS if s.key not in pr.skills][:3]
        box = QMessageBox(parent)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle(f.title)
        box.setText(f"<b>{html.escape(f.title)}</b> unlocks at the <b>{lvl_name}</b> level.")
        box.setInformativeText(
            f"{f.why}\n\nYou're {need} skill(s) away. Next ideas:\n" + "\n".join(f"• {s.title}: {s.how}" for s in missing)
            + "\n\nAlready comfortable? Unlock it now.")
        unlock = box.addButton("Unlock Now", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is unlock:
            pr.set_override(max(f.level, pr.level))
            return True
        return False


class LevelToast(QFrame):
    """A brief notice in the window corner: 'You're now an Explorer — new: Diagram, History…'."""

    def __init__(self, parent: QWidget, level: int, on_open):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        name = LEVELS[level][1]
        feats = [f.title for f in FEATURES.values() if f.level == level]
        self.setFixedWidth(380)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        title = QLabel(f"🎉 You're now at the <b>{html.escape(name)}</b> level")
        title.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(title)
        if feats:
            body = QLabel("New tools: " + ", ".join(feats))
            body.setObjectName("Muted")
            body.setWordWrap(True)
            lay.addWidget(body)
        row = QHBoxLayout()
        row.addStretch()
        see = QPushButton("See What's New")
        see.setObjectName("Primary")
        see.clicked.connect(lambda: (on_open(), self.close()))
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        row.addWidget(close)
        row.addWidget(see)
        lay.addLayout(row)
        lay.activate()
        self.setFixedHeight(max(lay.totalHeightForWidth(380), lay.sizeHint().height()))
        self._place()
        self.show()
        self.raise_()
        QTimer.singleShot(9000, lambda: shiboken6.isValid(self) and self.close())

    def _place(self) -> None:
        p = self.parentWidget()
        self.move(p.width() - self.width() - 24, 70)
