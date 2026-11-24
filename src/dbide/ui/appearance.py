"""Appearance settings: mode, themes, accent, fonts, density and zoom, with live preview."""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFontComboBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from dbide.core.settings import settings
from dbide.ui.theme import ACCENTS, DEFAULTS, THEMES, Theme, ThemeManager, default_mono_family, default_ui_family, pref


class ThemeCard(QWidget):
    """A miniature window painted in the theme's colours."""

    clicked = Signal(str)

    def __init__(self, t: Theme):
        super().__init__()
        self.t = t
        self.selected = False
        self.setFixedSize(168, 118)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(t.name)

    def mouseReleaseEvent(self, ev):
        self.clicked.emit(self.t.key)

    def paintEvent(self, ev):
        t = self.t.with_accent(pref("accent") or None)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(3, 3, self.width() - 6, 88)
        cur = ThemeManager.get().theme
        p.setPen(QPen(QColor(cur.accent if self.selected else cur.border), 2.5 if self.selected else 1))
        p.setBrush(QColor(t.bg))
        p.drawRoundedRect(r, 9, 9)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(t.surface_alt))
        p.drawRoundedRect(QRectF(r.x() + 6, r.y() + 6, 40, r.height() - 12), 5, 5)
        p.setBrush(QColor(t.surface))
        p.drawRoundedRect(QRectF(r.x() + 52, r.y() + 6, r.width() - 58, r.height() - 12), 5, 5)
        for i, (w, c) in enumerate([(50, t.syntax.get("kw", t.accent)), (80, t.text), (36, t.syntax.get("str", t.success)), (64, t.muted)]):
            p.setBrush(QColor(c))
            p.drawRoundedRect(QRectF(r.x() + 60, r.y() + 14 + i * 13, w, 6), 3, 3)
        for i in range(3):
            p.setBrush(QColor(t.muted))
            p.drawRoundedRect(QRectF(r.x() + 12, r.y() + 16 + i * 12, 26, 5), 2, 2)
        p.setBrush(QColor(t.accent))
        p.drawRoundedRect(QRectF(r.right() - 38, r.bottom() - 20, 26, 9), 4, 4)
        p.setPen(QColor(cur.text))
        f = QFont(self.font())
        f.setBold(self.selected)
        p.setFont(f)
        p.drawText(QRectF(0, 94, self.width(), 22), Qt.AlignmentFlag.AlignCenter, t.name)
        p.end()


class ThemePicker(QWidget):
    chosen = Signal(str)

    def __init__(self, dark: bool):
        super().__init__()
        self.cards = [ThemeCard(t) for t in THEMES.values() if t.dark == dark]
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(10)
        for i, c in enumerate(self.cards):
            grid.addWidget(c, i // 4, i % 4)
            c.clicked.connect(self._pick)

    def set_selected(self, key: str) -> None:
        for c in self.cards:
            c.selected = c.t.key == key
            c.update()

    def _pick(self, key: str) -> None:
        self.set_selected(key)
        self.chosen.emit(key)


class AppearanceDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Appearance")
        self.resize(780, 720)
        self.tm = ThemeManager.get()
        self._snapshot = {k: settings().get(k) for k in DEFAULTS}

        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setSpacing(14)

        # mode
        mode_box = QGroupBox("Mode")
        ml = QHBoxLayout(mode_box)
        self.mode_group = QButtonGroup(self)
        for key, label in (("system", "Match system"), ("light", "Light"), ("dark", "Dark")):
            rb = QRadioButton(label)
            rb.setProperty("mode", key)
            rb.setChecked(pref("appearance_mode") == key)
            self.mode_group.addButton(rb)
            ml.addWidget(rb)
            rb.toggled.connect(lambda on, k=key: on and self._set("appearance_mode", k))
        ml.addStretch()
        lay.addWidget(mode_box)

        # themes
        light_box = QGroupBox("Light theme")
        QVBoxLayout(light_box).addWidget(light := ThemePicker(False))
        light.set_selected(pref("light_theme"))
        light.chosen.connect(lambda k: self._set("light_theme", k, mode="light"))
        dark_box = QGroupBox("Dark theme")
        QVBoxLayout(dark_box).addWidget(dark := ThemePicker(True))
        dark.set_selected(pref("dark_theme"))
        dark.chosen.connect(lambda k: self._set("dark_theme", k, mode="dark"))
        self.pickers = (light, dark)
        lay.addWidget(light_box)
        lay.addWidget(dark_box)

        # accent, fonts, density, zoom
        look = QGroupBox("Accent, fonts && size")
        form = QFormLayout(look)
        self.accent = QComboBox()
        for color, name in ACCENTS.items():
            self.accent.addItem(name, color)
            if color:
                pm_icon = _swatch(color)
                self.accent.setItemIcon(self.accent.count() - 1, pm_icon)
        self.accent.setCurrentIndex(max(0, self.accent.findData(pref("accent"))))
        self.accent.currentIndexChanged.connect(lambda _: self._set("accent", self.accent.currentData()))
        form.addRow("Accent colour", self.accent)

        self.ui_font = QFontComboBox()
        self.ui_font.setCurrentFont(QFont(pref("ui_font") or default_ui_family()))
        self.ui_font.currentFontChanged.connect(lambda f: self._set("ui_font", f.family()))
        self.ui_size = QDoubleSpinBox()
        self.ui_size.setRange(0, 24)
        self.ui_size.setSingleStep(0.5)
        self.ui_size.setSpecialValueText("Auto (fits your screen)")
        self.ui_size.setValue(pref("ui_font_size") or 0)
        self.ui_size.valueChanged.connect(lambda v: self._set("ui_font_size", v))
        form.addRow("Interface font", _row(self.ui_font, self.ui_size))

        self.ed_font = QFontComboBox()
        self.ed_font.setFontFilters(QFontComboBox.FontFilter.MonospacedFonts)
        self.ed_font.setCurrentFont(QFont(pref("editor_font") or default_mono_family()))
        self.ed_font.currentFontChanged.connect(lambda f: self._set("editor_font", f.family()))
        self.ed_size = QDoubleSpinBox()
        self.ed_size.setRange(0, 32)
        self.ed_size.setSingleStep(0.5)
        self.ed_size.setSpecialValueText("Auto")
        self.ed_size.setValue(pref("editor_font_size") or 0)
        self.ed_size.valueChanged.connect(lambda v: self._set("editor_font_size", v))
        form.addRow("Editor font", _row(self.ed_font, self.ed_size))

        self.density = QComboBox()
        for k, label in (("compact", "Compact"), ("comfortable", "Comfortable"), ("spacious", "Spacious")):
            self.density.addItem(label, k)
        self.density.setCurrentIndex(max(0, self.density.findData(pref("density"))))
        self.density.currentIndexChanged.connect(lambda _: self._set("density", self.density.currentData()))
        form.addRow("Density", self.density)

        self.zoom = QSlider(Qt.Orientation.Horizontal)
        self.zoom.setRange(70, 200)
        self.zoom.setSingleStep(5)
        self.zoom.setValue(round(pref("zoom") * 100))
        self.zoom_label = QLabel(f"{self.zoom.value()}%")
        self.zoom.valueChanged.connect(lambda v: (self.zoom_label.setText(f"{v}%"), self._set("zoom", v / 100)))
        form.addRow("Zoom (⌘+ / ⌘−)", _row(self.zoom, self.zoom_label))
        lay.addWidget(look)

        reset_note = QLabel("Changes apply instantly. Cancel restores what you had.")
        reset_note.setObjectName("Muted")
        lay.addWidget(reset_note)
        lay.addStretch()

        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QScrollArea.Shape.NoFrame)
        sa.setWidget(body)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
                              | QDialogButtonBox.StandardButton.RestoreDefaults)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        bb.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(self._defaults)
        outer = QVBoxLayout(self)
        outer.addWidget(sa, 1)
        outer.addWidget(bb)
        self.tm.on_change(self, self._repaint_cards)

    def _set(self, key, value, mode: str | None = None) -> None:
        settings().set(key, value)
        if mode and pref("appearance_mode") != "system" and pref("appearance_mode") != mode:
            # picking a dark theme while in light mode switches to dark so you see it
            settings().set("appearance_mode", mode)
            for b in self.mode_group.buttons():
                b.blockSignals(True)
                b.setChecked(b.property("mode") == mode)
                b.blockSignals(False)
        self.tm.apply()

    def _repaint_cards(self, _t) -> None:
        for p in self.pickers:
            for c in p.cards:
                c.update()

    def _defaults(self) -> None:
        for k, v in DEFAULTS.items():
            settings().set(k, v)
        self.tm.apply()
        self.accept()

    def reject(self) -> None:
        for k, v in self._snapshot.items():
            settings().set(k, v if v is not None else DEFAULTS[k])
        self.tm.apply()
        super().reject()


def _row(*widgets) -> QWidget:
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    for i, x in enumerate(widgets):
        h.addWidget(x, 1 if i == 0 else 0)
    w.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    return w


def _swatch(color: str):
    from PySide6.QtGui import QIcon, QPixmap

    pm = QPixmap(14, 14)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(1, 1, 12, 12)
    p.end()
    return QIcon(pm)

