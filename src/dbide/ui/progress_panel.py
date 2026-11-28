"""The Progress panel: your level, skills learned, what to try next, and what unlocks when."""
from __future__ import annotations

import html

from PySide6.QtWidgets import QCheckBox, QLabel, QMessageBox, QProgressBar, QPushButton, QTextBrowser, QVBoxLayout, QWidget

from dbide.core.progress import FEATURES, LEVELS, SKILLS, progress
from dbide.ui.features import Gate
from dbide.ui.theme import ThemeManager


class ProgressPanel(QWidget):
    def __init__(self):
        super().__init__()
        self.level = QLabel()
        self.level.setObjectName("Hero")
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        self.next = QLabel()
        self.next.setObjectName("Muted")
        self.next.setWordWrap(True)
        self.body = QTextBrowser()
        self.body.setOpenLinks(False)
        self.body.setFrameShape(QTextBrowser.Shape.NoFrame)
        self.show_all = QCheckBox("Show all features now")
        self.show_all.setToolTip("For experienced developers: reveal every tool immediately")
        self.show_all.toggled.connect(self._toggle_all)
        reset = QPushButton("Reset Progress")
        reset.clicked.connect(self._reset)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 4, 12, 12)
        lay.setSpacing(8)
        lay.addWidget(self.level)
        lay.addWidget(self.bar)
        lay.addWidget(self.next)
        lay.addWidget(self.body, 1)
        lay.addWidget(self.show_all)
        lay.addWidget(reset)
        g = Gate.get()
        g.on(g.changed, self, lambda _l: self.refresh())
        g.on(g.skill_learned, self, lambda _k: self.refresh())
        ThemeManager.get().on_change(self, lambda _t: self.refresh())
        self.refresh()

    def refresh(self) -> None:
        pr = progress()
        t = ThemeManager.get().theme
        self.level.setText(f"{pr.level_name}")
        nxt = pr.next_level()
        lo = LEVELS[pr.earned_level][0]
        hi = LEVELS[min(pr.earned_level + 1, len(LEVELS) - 1)][0]
        self.bar.setRange(0, max(1, hi - lo))
        self.bar.setValue(min(hi - lo, pr.points - lo) if nxt else hi - lo)
        if nxt:
            name, need = nxt
            self.next.setText(f"{pr.points} skill(s) learned · {need} more to reach {name}")
        else:
            self.next.setText(f"{pr.points} skill(s) learned · every feature is available")
        self.show_all.blockSignals(True)
        self.show_all.setChecked(pr.level >= len(LEVELS) - 1)
        self.show_all.blockSignals(False)
        rows = []
        for s in SKILLS:
            done = s.key in pr.skills
            mark = f"<span style='color:{t.success}'>✔</span>" if done else f"<span style='color:{t.muted}'>○</span>"
            how = "" if done else f"<br><span style='color:{t.muted}'>{html.escape(s.how)}</span>"
            rows.append(f"<p style='margin:4px 0'>{mark} <b>{html.escape(s.title)}</b>{how}</p>")
        feats = []
        for i, (_need, name) in enumerate(LEVELS[1:], 1):
            items = [f for f in FEATURES.values() if f.level == i]
            open_ = pr.level >= i
            head = f"<p style='margin-top:10px'><b>{'🔓' if open_ else '🔒'} {html.escape(name)}</b></p><ul style='margin-top:0'>"
            feats.append(head + "".join(f"<li>{html.escape(f.title)}: <span style='color:{t.muted}'>{html.escape(f.why)}</span></li>" for f in items) + "</ul>")
        self.body.setHtml("<p><b>Skills</b></p>" + "".join(rows) + "<p style='margin-top:12px'><b>Tools by level</b></p>" + "".join(feats))

    def _toggle_all(self, on: bool) -> None:
        progress().set_override(len(LEVELS) - 1 if on else None)
        self.refresh()

    def _reset(self) -> None:
        if QMessageBox.question(self, "Reset progress", "Forget learned skills and start again as a Beginner?") == QMessageBox.StandardButton.Yes:
            progress().reset()
            Gate.get().refresh()
            self.refresh()

