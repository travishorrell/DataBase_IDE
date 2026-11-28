"""Themes, light/dark mode, fonts, UI zoom and themed icons.

Everything visual reads its colors from the current Theme, so switching theme
or mode restyles the whole app live: Qt palette, the generated stylesheet,
icons, the SQL highlighter, the ER diagram and the JOIN explainer.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

os.environ.setdefault("QT_API", "pyside6")  # for qtawesome / QtPy

import shiboken6
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QIcon, QPalette
from PySide6.QtWidgets import QApplication

from dbide.core.settings import settings


@dataclass(frozen=True)
class Theme:
    key: str
    name: str
    dark: bool
    bg: str  # window / chrome background
    surface: str  # editors, grids, main content
    surface_alt: str  # sidebars, alternating rows, headers
    raised: str  # inputs, cards, menus
    border: str
    text: str
    muted: str
    accent: str
    accent_text: str
    danger: str
    warning: str
    success: str
    syntax: dict = field(default_factory=dict)  # kw, fn, str, num, cmt, ident

    def with_accent(self, accent: str | None) -> "Theme":
        if not accent:
            return self
        txt = "#ffffff" if QColor(accent).lightness() < 165 else "#111111"
        return replace(self, accent=accent, accent_text=txt)

    def mix(self, a: str, b: str, t: float) -> str:
        ca, cb = QColor(a), QColor(b)
        return QColor(
            round(ca.red() + (cb.red() - ca.red()) * t),
            round(ca.green() + (cb.green() - ca.green()) * t),
            round(ca.blue() + (cb.blue() - ca.blue()) * t),
        ).name()

    @property
    def hover(self) -> str:
        return self.mix(self.surface_alt, self.text, 0.07)

    @property
    def selection(self) -> str:
        return self.mix(self.surface, self.accent, 0.28 if self.dark else 0.18)

    @property
    def accent_soft(self) -> str:
        return self.mix(self.surface, self.accent, 0.14)


def _syn(kw, fn, s, num, cmt, ident):
    return dict(kw=kw, fn=fn, str=s, num=num, cmt=cmt, ident=ident)


THEMES: dict[str, Theme] = {t.key: t for t in [
    # ---- light ----
    Theme("daylight", "Daylight", False, bg="#f3f4f6", surface="#ffffff", surface_alt="#f8f9fb", raised="#ffffff",
          border="#dfe2e7", text="#1d2330", muted="#6b7280", accent="#2563eb", accent_text="#ffffff",
          danger="#dc2626", warning="#b7791f", success="#15803d",
          syntax=_syn("#1d4ed8", "#0e7490", "#15803d", "#c2410c", "#9ca3af", "#7c3aed")),
    Theme("paper", "Paper", False, bg="#efe9df", surface="#fbf8f2", surface_alt="#f4eee4", raised="#fffdf8",
          border="#ddd3c4", text="#3b3227", muted="#857868", accent="#b45309", accent_text="#ffffff",
          danger="#b91c1c", warning="#a16207", success="#4d7c0f",
          syntax=_syn("#9a3412", "#0f766e", "#4d7c0f", "#a21caf", "#a8a29e", "#1d4ed8")),
    Theme("arctic", "Arctic", False, bg="#eef3f8", surface="#ffffff", surface_alt="#f5f9fc", raised="#ffffff",
          border="#d5e0ea", text="#1b2a3a", muted="#64788c", accent="#0891b2", accent_text="#ffffff",
          danger="#e11d48", warning="#ca8a04", success="#059669",
          syntax=_syn("#0369a1", "#7c3aed", "#059669", "#ea580c", "#94a3b8", "#db2777")),
    # ---- dark ----
    Theme("graphite", "Graphite", True, bg="#1b1d21", surface="#202328", surface_alt="#25282e", raised="#2b2f36",
          border="#363a42", text="#e3e6ea", muted="#9097a1", accent="#4f8cff", accent_text="#ffffff",
          danger="#f87171", warning="#fbbf24", success="#4ade80",
          syntax=_syn("#c792ea", "#82aaff", "#a5d6a7", "#f78c6c", "#6b7280", "#ffcb6b")),
    Theme("midnight", "Midnight", True, bg="#0d1220", surface="#111829", surface_alt="#161e31", raised="#1c253b",
          border="#26314a", text="#dbe4f3", muted="#8090ad", accent="#38bdf8", accent_text="#06121f",
          danger="#fb7185", warning="#fcd34d", success="#34d399",
          syntax=_syn("#7dd3fc", "#c4b5fd", "#86efac", "#fda4af", "#5b6b8a", "#fde68a")),
    Theme("forest", "Forest", True, bg="#141a16", surface="#18201b", surface_alt="#1d2620", raised="#232e27",
          border="#2e3b33", text="#dfe9e1", muted="#8aa092", accent="#34d399", accent_text="#052e1c",
          danger="#f87171", warning="#facc15", success="#86efac",
          syntax=_syn("#6ee7b7", "#93c5fd", "#bef264", "#fdba74", "#5f7567", "#f0abfc")),
    Theme("ember", "Ember", True, bg="#1c1714", surface="#211b17", surface_alt="#28211c", raised="#302822",
          border="#3d332b", text="#f1e7de", muted="#a8968a", accent="#fb923c", accent_text="#1c0d02",
          danger="#f87171", warning="#fcd34d", success="#a3e635",
          syntax=_syn("#fdba74", "#93c5fd", "#bef264", "#f9a8d4", "#7c6a5d", "#fde68a")),
    Theme("contrast", "High Contrast", True, bg="#000000", surface="#000000", surface_alt="#0a0a0a", raised="#141414",
          border="#6b6b6b", text="#ffffff", muted="#c8c8c8", accent="#ffd60a", accent_text="#000000",
          danger="#ff5c5c", warning="#ffd60a", success="#5cff8d",
          syntax=_syn("#ffd60a", "#6ecbff", "#7dff9b", "#ff9e5e", "#a0a0a0", "#ff8ef0")),
]}

ACCENTS = {"": "Theme default", "#2563eb": "Blue", "#7c3aed": "Violet", "#db2777": "Pink", "#0891b2": "Teal",
           "#16a34a": "Green", "#ea580c": "Orange", "#e11d48": "Red"}
DENSITY = {"compact": 0.75, "comfortable": 1.0, "spacious": 1.3}

DEFAULTS = {
    "appearance_mode": "system",  # system | light | dark
    "light_theme": "daylight",
    "dark_theme": "graphite",
    "accent": "",
    "ui_font": "",
    "ui_font_size": 0,  # 0 = automatic from screen
    "editor_font": "",
    "editor_font_size": 0,
    "density": "comfortable",
    "zoom": 1.0,
}


def pref(key: str):
    v = settings().get(key)
    return DEFAULTS[key] if v is None else v


_SF_MONO: list[str] = []


def _register_bundled_fonts() -> None:
    """macOS ships SF Mono as a file that isn't registered as a family; load it if present.

    It registers under a hidden name (".SF NS Mono"), which we remember and prefer.
    """
    if sys.platform == "darwin" and "SF Mono" not in QFontDatabase.families():
        for f in ("SFNSMono.ttf", "SFNSMonoItalic.ttf"):
            p = Path("/System/Library/Fonts") / f
            if p.exists():
                fid = QFontDatabase.addApplicationFont(str(p))
                if fid >= 0:
                    _SF_MONO.extend(QFontDatabase.applicationFontFamilies(fid)[:1])


def default_ui_family() -> str:
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont).family()


def default_mono_family() -> str:
    fams = set(QFontDatabase.families())
    for f in ("SF Mono", *_SF_MONO, "JetBrains Mono", "Cascadia Code", "Fira Code", "Menlo", "Consolas", "DejaVu Sans Mono"):
        if f in fams:
            return f
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()


def auto_font_size() -> float:
    """A base UI size that scales with the screen: bigger on large monitors, smaller on small laptops."""
    screen = QGuiApplication.primaryScreen()
    base = 13.0 if sys.platform == "darwin" else 10.0
    if screen is None:
        return base
    w = screen.availableGeometry().width()
    if w >= 2400:
        return base + 1.5
    if w >= 1900:
        return base + 0.5
    if w < 1300:
        return base - 1
    return base


class ThemeManager(QObject):
    changed = Signal(object)  # Theme

    _instance: "ThemeManager | None" = None

    @classmethod
    def get(cls) -> "ThemeManager":
        if cls._instance is None:
            cls._instance = ThemeManager()
        return cls._instance

    def __init__(self):
        super().__init__()
        self.theme: Theme = THEMES["daylight"]
        self._icons: list[tuple[object, str, str]] = []
        self._applied = False
        hints = QGuiApplication.styleHints()
        if hasattr(hints, "colorSchemeChanged"):
            hints.colorSchemeChanged.connect(lambda *_: self.apply() if pref("appearance_mode") == "system" else None)

    def on_change(self, owner, fn) -> None:
        """Call fn(theme) on every theme change while `owner` (a QObject) is alive.

        Plain `changed.connect(lambda ...)` would keep calling into widgets after
        they are destroyed; this disconnects automatically.
        """

        def slot(t):
            if shiboken6.isValid(owner):
                fn(t)

        self.changed.connect(slot)

        def drop(*_):
            try:
                self.changed.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

        owner.destroyed.connect(drop)

    # ---- choosing -------------------------------------------------------------
    def system_is_dark(self) -> bool:
        hints = QGuiApplication.styleHints()
        if hasattr(hints, "colorScheme"):
            return hints.colorScheme() == Qt.ColorScheme.Dark
        return False

    def resolve(self) -> Theme:
        mode = pref("appearance_mode")
        dark = self.system_is_dark() if mode == "system" else mode == "dark"
        key = pref("dark_theme") if dark else pref("light_theme")
        theme = THEMES.get(key) or THEMES["graphite" if dark else "daylight"]
        return theme.with_accent(pref("accent") or None)

    def set_pref(self, key: str, value) -> None:
        settings().set(key, value)
        self.apply()

    def toggle_mode(self) -> None:
        self.set_pref("appearance_mode", "light" if self.theme.dark else "dark")

    def zoom(self, delta: float | None) -> None:
        z = 1.0 if delta is None else min(2.0, max(0.7, round(pref("zoom") + delta, 2)))
        self.set_pref("zoom", z)

    # ---- fonts ----------------------------------------------------------------
    def ui_font(self) -> QFont:
        f = QFont(pref("ui_font") or default_ui_family())
        f.setPointSizeF((pref("ui_font_size") or auto_font_size()) * pref("zoom"))
        return f

    def editor_font(self) -> QFont:
        f = QFont(pref("editor_font") or default_mono_family())
        f.setStyleHint(QFont.StyleHint.Monospace)
        size = pref("editor_font_size") or (self.ui_font().pointSizeF() / pref("zoom") + 0.5)
        f.setPointSizeF(size * pref("zoom"))
        return f

    def pad(self, px: float) -> int:
        return max(1, round(px * DENSITY.get(pref("density"), 1.0) * pref("zoom")))

    # ---- applying ---------------------------------------------------------------
    def apply(self, app: QApplication | None = None) -> None:
        app = app or QApplication.instance()
        if app is None:
            return
        if not self._applied:
            _register_bundled_fonts()
            app.setStyle("Fusion")
            self._applied = True
        t = self.resolve()
        self.theme = t
        if hasattr(app.styleHints(), "setColorScheme"):
            app.styleHints().setColorScheme(Qt.ColorScheme.Dark if t.dark else Qt.ColorScheme.Light)
        app.setPalette(build_palette(t))
        app.setFont(self.ui_font())
        app.setStyleSheet(build_stylesheet(t, self))
        self._refresh_icons()
        self.changed.emit(t)

    # ---- icons --------------------------------------------------------------------
    def icon(self, name: str, role: str = "text") -> QIcon:
        color = {"text": self.theme.text, "muted": self.theme.muted, "accent": self.theme.accent,
                 "on_accent": self.theme.accent_text, "danger": self.theme.danger,
                 "success": self.theme.success, "warning": self.theme.warning}.get(role, role)
        return make_icon(name, color, self.theme.accent)

    def bind_icon(self, obj, name: str, role: str = "text") -> None:
        """Give a QAction/QAbstractButton an icon that follows theme changes."""
        obj.setIcon(self.icon(name, role))
        self._icons.append((obj, name, role))

    def _refresh_icons(self) -> None:
        alive = []
        for obj, name, role in self._icons:
            if shiboken6.isValid(obj):
                obj.setIcon(self.icon(name, role))
                alive.append((obj, name, role))
        self._icons = alive


ICON_ALIASES = {
    # our name -> candidates (first that exists wins)
    "connect": ["mdi6.database-plus-outline", "ph.database"],
    "database": ["ph.database", "mdi6.database-outline"],
    "query": ["mdi6.file-code-outline", "ph.code"],
    "table": ["ph.table"],
    "erd": ["ph.share-network", "mdi6.sitemap-outline"],
    "history": ["ph.clock-counter-clockwise"],
    "lessons": ["ph.graduation-cap"],
    "help": ["ph.question"],
    "beginner": ["ph.lightbulb"],
    "appearance": ["ph.palette"],
    "sun": ["ph.sun"],
    "moon": ["ph.moon"],
    "run": ["ph.play"],
    "run_all": ["ph.lightning"],
    "stop": ["ph.stop"],
    "explain": ["ph.magnifying-glass"],
    "english": ["ph.chats-circle"],
    "join": ["ph.git-merge"],
    "refresh": ["ph.arrows-clockwise"],
    "plus": ["ph.plus"],
    "minus": ["ph.minus"],
    "null": ["ph.selection-plus", "ph.x"],
    "apply": ["ph.check"],
    "discard": ["ph.x"],
    "export": ["ph.export"],
    "prev": ["ph.arrow-left"],
    "next": ["ph.arrow-right"],
    "filter": ["ph.funnel"],
    "sort": ["ph.sort-ascending"],
    "save": ["ph.floppy-disk"],
    "trash": ["ph.trash"],
    "security": ["ph.shield-check"],
    "key": ["ph.key"],
    "lock": ["ph.lock-simple"],
    "image": ["ph.image"],
    "folder": ["ph.folder-simple"],
    "sidebar": ["mdi6.dock-left"],
    "panel_right": ["mdi6.dock-right"],
    "connections": ["mdi6.connection", "ph.database"],
    "layout": ["ph.layout"],
    "fit": ["ph.squares-four"],
    "zoom_in": ["ph.magnifying-glass-plus", "ph.plus"],
    "zoom_out": ["ph.magnifying-glass-minus", "ph.minus"],
    "book": ["ph.book-open"],
    "info": ["ph.info"],
    "more": ["ph.dots-three-outline"],
    "designer": ["ph.pencil-simple-line", "ph.pencil-simple", "ph.pencil", "mdi6.pencil-outline"],
    "chart": ["ph.chart-bar"],
    "sliders": ["ph.sliders-horizontal"],
    "caret_down": ["ph.caret-down", "mdi6.chevron-down"],
    "caret_up": ["ph.caret-up", "mdi6.chevron-up"],
}

_icon_cache: dict[tuple[str, str], QIcon] = {}


def make_icon(name: str, color: str, active: str | None = None) -> QIcon:
    key = (name, color)
    if key in _icon_cache:
        return _icon_cache[key]
    icon = QIcon()
    try:
        import qtawesome as qta

        for cand in ICON_ALIASES.get(name, [name]):
            try:
                icon = qta.icon(cand, color=color, color_active=active or color, color_disabled=QColor(color).darker(160) if QColor(color).lightness() > 128 else QColor(color).lighter(220))
                break
            except Exception:
                continue
    except Exception:
        pass
    _icon_cache[key] = icon
    return icon


def theme() -> Theme:
    return ThemeManager.get().theme


def build_palette(t: Theme) -> QPalette:
    p = QPalette()
    C = QColor
    R = QPalette.ColorRole
    p.setColor(R.Window, C(t.bg))
    p.setColor(R.WindowText, C(t.text))
    p.setColor(R.Base, C(t.surface))
    p.setColor(R.AlternateBase, C(t.surface_alt))
    p.setColor(R.Text, C(t.text))
    p.setColor(R.Button, C(t.raised))
    p.setColor(R.ButtonText, C(t.text))
    p.setColor(R.BrightText, C(t.danger))
    p.setColor(R.Highlight, C(t.accent))
    p.setColor(R.HighlightedText, C(t.accent_text))
    p.setColor(R.ToolTipBase, C(t.raised))
    p.setColor(R.ToolTipText, C(t.text))
    p.setColor(R.PlaceholderText, C(t.muted))
    p.setColor(R.Link, C(t.accent))
    p.setColor(R.Mid, C(t.border))
    p.setColor(R.Midlight, C(t.surface_alt))
    p.setColor(R.Dark, C(t.border))
    p.setColor(R.Light, C(t.raised))
    for role in (R.WindowText, R.Text, R.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, C(t.muted))
    return p


def _icon_file(name: str, color: str, size: int = 32) -> str:
    """Render an icon to a PNG in the app dir so stylesheets (which need a url) can use it."""
    from dbide.core.config import app_dir

    d = app_dir() / "theme_cache"
    d.mkdir(exist_ok=True)
    path = d / f"{name}_{color.lstrip('#')}.png"
    if not path.exists():
        make_icon(name, color).pixmap(size, size).save(str(path))
    return path.as_posix()


def build_stylesheet(t: Theme, tm: ThemeManager) -> str:
    pad = tm.pad
    r = 7  # corner radius
    sel = t.selection
    arrow = _icon_file("caret_down", t.muted)
    arrow_hover = _icon_file("caret_down", t.text)
    arrow_up = _icon_file("caret_up", t.muted)
    close_icon = _icon_file("discard", t.muted)
    close_hover = _icon_file("discard", t.text)
    return f"""
QMainWindow, QDialog {{ background: {t.bg}; }}
QWidget {{ color: {t.text}; }}
QToolTip {{ background: {t.raised}; color: {t.text}; border: 1px solid {t.border}; border-radius: 6px; padding: 5px 8px; }}

/* ---- side panels & headers ---- */
#ActivityBar {{ background: {t.bg}; border-right: 1px solid {t.border}; }}
#ActivityBar QToolButton {{ border: none; border-radius: 9px; padding: {pad(8)}px; margin: 3px 5px; background: transparent; }}
#ActivityBar QToolButton:hover {{ background: {t.hover}; }}
#ActivityBar QToolButton:checked {{ background: {t.accent_soft}; }}
#SidePanel, #RightPanel {{ background: {t.surface_alt}; }}
#SidePanel {{ border-right: 1px solid {t.border}; }}
#RightPanel {{ border-left: 1px solid {t.border}; }}
#PanelHeader {{ background: transparent; }}
#PanelTitle {{ font-weight: 600; color: {t.muted}; letter-spacing: 0.4px; }}
#PanelHeader QToolButton {{ border: none; border-radius: 6px; padding: 3px; background: transparent; }}
#PanelHeader QToolButton:hover {{ background: {t.hover}; }}

/* ---- toolbars ---- */
QToolBar {{ background: {t.bg}; border: none; border-bottom: 1px solid {t.border}; padding: {pad(4)}px {pad(6)}px; spacing: {pad(4)}px; }}
QToolBar#TabToolbar {{ background: {t.surface}; }}
QToolBar::separator {{ background: {t.border}; width: 1px; margin: 5px 6px; }}
QToolBar QToolButton {{ border: 1px solid transparent; border-radius: {r}px; padding: {pad(4)}px {pad(8)}px; background: transparent; }}
QToolBar QToolButton:hover {{ background: {t.hover}; }}
QToolBar QToolButton:pressed, QToolBar QToolButton:checked {{ background: {t.accent_soft}; border-color: {t.mix(t.accent_soft, t.accent, 0.35)}; }}
QToolBar QToolButton:disabled {{ color: {t.muted}; }}
QToolButton#Primary {{ background: {t.accent}; color: {t.accent_text}; font-weight: 600; }}
QToolButton#Primary:hover {{ background: {t.mix(t.accent, t.text, 0.12)}; }}
QToolButton#Primary:disabled {{ background: {t.mix(t.accent, t.bg, 0.6)}; color: {t.accent_text}; }}

/* ---- tabs ---- */
QTabWidget::pane {{ border: none; background: {t.surface}; }}
QTabBar {{ background: {t.bg}; qproperty-drawBase: 0; }}
QTabBar::tab {{ background: transparent; color: {t.muted}; padding: {pad(7)}px {pad(14)}px; margin: 4px 2px 0 2px;
               border: 1px solid transparent; border-top-left-radius: {r}px; border-top-right-radius: {r}px; }}
QTabBar::tab:hover {{ color: {t.text}; background: {t.hover}; }}
QTabBar::tab:selected {{ color: {t.text}; background: {t.surface}; border-color: {t.border}; border-bottom-color: {t.surface};
                        border-top: 2px solid {t.accent}; }}
QTabBar::close-button {{ subcontrol-position: right; image: url({close_icon}); width: 14px; height: 14px; padding: 2px; border-radius: 4px; }}
QTabBar::close-button:hover {{ image: url({close_hover}); background: {t.hover}; }}
QTabWidget#Results QTabBar::tab, QTabWidget#SubTabs QTabBar::tab {{ padding: {pad(5)}px {pad(12)}px; }}

/* ---- inputs ---- */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QFontComboBox {{
    background: {t.raised}; border: 1px solid {t.border}; border-radius: {r}px; padding: {pad(4)}px {pad(8)}px;
    selection-background-color: {t.accent}; selection-color: {t.accent_text}; }}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border: 1px solid {t.accent}; }}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover {{ border-color: {t.mix(t.border, t.text, 0.25)}; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: top right; border: none; width: 24px; }}
QComboBox::down-arrow {{ image: url({arrow}); width: 12px; height: 12px; }}
QComboBox::down-arrow:hover, QComboBox::down-arrow:on {{ image: url({arrow_hover}); }}
QComboBox {{ padding-right: 26px; }}
QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right; width: 18px; border: none; }}
QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right; width: 18px; border: none; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url({arrow_up}); width: 10px; height: 10px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url({arrow}); width: 10px; height: 10px; }}
QSpinBox, QDoubleSpinBox {{ padding-right: 20px; }}
QTableView QComboBox, QTableWidget QComboBox {{ border-radius: 0; padding: 0 22px 0 6px; border: 1px solid {t.accent}; }}
QComboBox QAbstractItemView {{ background: {t.raised}; border: 1px solid {t.border}; selection-background-color: {sel};
    selection-color: {t.text}; outline: none; padding: 4px; }}
QPushButton {{ background: {t.raised}; border: 1px solid {t.border}; border-radius: {r}px; padding: {pad(6)}px {pad(14)}px; }}
QPushButton:hover {{ background: {t.hover}; }}
QPushButton:pressed {{ background: {t.accent_soft}; }}
QPushButton:default, QPushButton#Primary {{ background: {t.accent}; color: {t.accent_text}; border-color: {t.accent}; font-weight: 600; }}
QPushButton:default:hover, QPushButton#Primary:hover {{ background: {t.mix(t.accent, t.text, 0.12)}; }}
QPushButton:disabled {{ color: {t.muted}; background: {t.surface_alt}; border-color: {t.border}; }}
QCheckBox, QRadioButton {{ spacing: 8px; }}

/* ---- text areas ---- */
QPlainTextEdit, QTextEdit, QTextBrowser {{ background: {t.surface}; border: none; selection-background-color: {sel}; selection-color: {t.text}; }}
QTextBrowser {{ padding: {pad(8)}px; }}

/* ---- trees, lists, grids ---- */
QTreeView, QListView, QTableView, QTableWidget {{ background: {t.surface}; alternate-background-color: {t.surface_alt};
    border: none; outline: none; selection-background-color: {sel}; selection-color: {t.text}; gridline-color: {t.border}; }}
#SidePanel QTreeView {{ background: {t.surface_alt}; }}
QTreeView::item {{ padding: {pad(3)}px 2px; border-radius: 5px; }}
QTreeView::item:hover, QListView::item:hover {{ background: {t.hover}; }}
QTreeView::item:selected, QListView::item:selected {{ background: {sel}; color: {t.text}; }}
QHeaderView::section {{ background: {t.surface_alt}; color: {t.muted}; border: none; border-right: 1px solid {t.border};
    border-bottom: 1px solid {t.border}; padding: {pad(4)}px {pad(8)}px; font-weight: 600; }}
QTableCornerButton::section {{ background: {t.surface_alt}; border: none; border-bottom: 1px solid {t.border}; }}

/* ---- scrollbars ---- */
QScrollBar:vertical {{ background: transparent; width: 11px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 2px; }}
QScrollBar::handle {{ background: {t.mix(t.surface, t.text, 0.22)}; border-radius: 4px; min-height: 28px; min-width: 28px; }}
QScrollBar::handle:hover {{ background: {t.mix(t.surface, t.text, 0.38)}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---- menus, splitters, status bar ---- */
QMenuBar {{ background: {t.bg}; }}
QMenuBar::item {{ padding: 4px 10px; border-radius: 5px; }}
QMenuBar::item:selected {{ background: {t.hover}; }}
QMenu {{ background: {t.raised}; border: 1px solid {t.border}; border-radius: 9px; padding: 6px; }}
QMenu::item {{ padding: {pad(6)}px {pad(22)}px {pad(6)}px {pad(12)}px; border-radius: 6px; }}
QMenu::item:selected {{ background: {sel}; color: {t.text}; }}
QMenu::separator {{ height: 1px; background: {t.border}; margin: 5px 8px; }}
QSplitter::handle {{ background: {t.border}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}
QStatusBar {{ background: {t.bg}; border-top: 1px solid {t.border}; color: {t.muted}; }}
QStatusBar::item {{ border: none; }}
#StatusChip {{ background: {t.surface_alt}; border: 1px solid {t.border}; border-radius: 10px; padding: 1px 10px; }}
#Badge {{ background: {t.accent_soft}; color: {t.accent}; border-radius: 9px; padding: 1px 9px; font-weight: 600; }}

/* ---- cards (welcome, appearance) ---- */
#Card {{ background: {t.raised}; border: 1px solid {t.border}; border-radius: 12px; }}
#Card:hover {{ border-color: {t.accent}; }}
#CardTitle {{ font-weight: 600; }}
#CardText, #Muted {{ color: {t.muted}; }}
#Hero {{ font-weight: 700; }}
#Welcome {{ background: {t.surface}; }}
QGroupBox {{ border: 1px solid {t.border}; border-radius: 10px; margin-top: 14px; padding: 12px 10px 10px 10px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; color: {t.muted}; font-weight: 600; }}
QSlider::groove:horizontal {{ height: 4px; background: {t.border}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {t.accent}; width: 16px; height: 16px; margin: -6px 0; border-radius: 8px; }}
QSlider::sub-page:horizontal {{ background: {t.accent}; border-radius: 2px; }}
"""


class _ToolbarResponder(QObject):
    def __init__(self, toolbar, breakpoint: int):
        super().__init__(toolbar)
        self.toolbar, self.breakpoint = toolbar, breakpoint

    def eventFilter(self, obj, ev):
        from PySide6.QtCore import QEvent

        if ev.type() == QEvent.Type.Resize and shiboken6.isValid(self.toolbar):
            wide = obj.width() >= self.breakpoint
            style = Qt.ToolButtonStyle.ToolButtonTextBesideIcon if wide else Qt.ToolButtonStyle.ToolButtonIconOnly
            if self.toolbar.toolButtonStyle() != style:
                self.toolbar.setToolButtonStyle(style)
        return False


def responsive_toolbar(toolbar, watched, breakpoint: int) -> None:
    """Show text beside icons when `watched` is at least `breakpoint` px wide, icons only when narrower."""
    watched.installEventFilter(_ToolbarResponder(toolbar, breakpoint))
