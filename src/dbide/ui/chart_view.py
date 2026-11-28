"""Charts for query results: auto-picked form, theme-aware, with hover readouts.

Visual rules (from the data-viz method this app follows):
  - colour comes from a validated categorical palette, assigned in fixed order;
    single-series charts use slot 1 only
  - bars <= 24px thick, 4px rounded at the data end, square at the baseline
  - lines 2px with round joins; end dots r=4 with a 2px surface ring
  - gridlines are solid 1px hairlines one step off the surface
  - text uses text colours, never the series colour
  - a legend whenever there are >= 2 series; hover tooltips lead with the value
  - the Result tab next to the chart is always the full table view
"""
from __future__ import annotations

import datetime as dt
import math

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QFontMetrics, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from dbide.core.chart_model import ChartData, ChartSpec, auto_spec, build, classify, compact, humanize, nice_ticks
from dbide.ui.theme import ThemeManager, theme

# validated categorical palette (see the dataviz reference): light and dark steps of the same eight hues
CAT_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
CAT_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#0ca30c", "#9085e9", "#e66767"]


def series_color(i: int) -> QColor:
    pal = CAT_DARK if theme().dark else CAT_LIGHT
    return QColor(pal[i % len(pal)])


class ChartWidget(QWidget):
    fit_note = Signal(str)  # e.g. "showing the top 6 of 24 (make the panel taller to see more)"

    def __init__(self):
        super().__init__()
        self.data: ChartData | None = None
        self.setMouseTracking(True)
        self.setMinimumHeight(220)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._hits: list[tuple[QRectF, list[tuple[str, str, QColor | None]]]] = []  # hover rects → tooltip lines
        self._hover: tuple[QPointF, list] | None = None
        self._line_geom = None  # for crosshair snapping
        ThemeManager.get().on_change(self, lambda _t: self.update())

    def set_data(self, data: ChartData | None) -> None:
        self.data = data
        self._hover = None
        self.update()

    # ---- colours ------------------------------------------------------------
    def _ink(self):
        t = theme()
        return dict(surface=QColor(t.surface), text=QColor(t.text), secondary=QColor(t.muted),
                    muted=QColor(t.mix(t.muted, t.surface, 0.25)), grid=QColor(t.mix(t.surface, t.text, 0.08)),
                    base=QColor(t.mix(t.surface, t.text, 0.22)), raised=QColor(t.raised), border=QColor(t.border))

    # ---- painting -------------------------------------------------------------
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        ink = self._ink()
        p.fillRect(self.rect(), ink["surface"])
        self._hits = []
        self._line_geom = None
        d = self.data
        if d is None or (d.spec.kind != "stat" and not any(s.points for s in d.series)):
            p.setPen(ink["secondary"])
            msg = d.spec.reason if d else "Run a query to see a chart."
            p.drawText(self.rect().adjusted(24, 24, -24, -24), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, msg)
            p.end()
            return
        area = QRectF(self.rect()).adjusted(16, 12, -20, -12)
        if len(d.series) >= 2:
            area.setTop(area.top() + self._legend(p, area, ink) + 8)
        {"stat": self._stat, "bar": self._bars, "line": self._lines, "scatter": self._scatter}[d.spec.kind](p, area, ink)
        if self._hover:
            self._tooltip(p, *self._hover, ink)
        p.end()

    def _legend(self, p: QPainter, area: QRectF, ink) -> float:
        fm = QFontMetrics(self.font())
        x, y = area.left(), area.top()
        h = fm.height()
        is_line = self.data.spec.kind == "line"
        for i, s in enumerate(self.data.series):
            w = 18 + fm.horizontalAdvance(s.name) + 16
            if x + w > area.right():
                x, y = area.left(), y + h + 4
            c = series_color(i)
            if is_line:  # legends mirror the mark: a line key for lines, a rect for bars
                p.setPen(QPen(c, 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawLine(QPointF(x, y + h / 2), QPointF(x + 12, y + h / 2))
            else:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(c)
                p.drawRoundedRect(QRectF(x, y + h / 2 - 5, 10, 10), 2, 2)
            p.setPen(ink["secondary"])
            p.drawText(QPointF(x + 18, y + fm.ascent()), s.name)
            x += w
        return (y - area.top()) + h

    def _stat(self, p: QPainter, area: QRectF, ink) -> None:
        d = self.data
        f = QFont(self.font())
        p.setFont(f)
        p.setPen(ink["secondary"])
        p.drawText(QRectF(area.left() + 8, area.top() + 8, area.width(), 24), Qt.AlignmentFlag.AlignLeft, d.stat_label)
        big = QFont(self.font())
        big.setPixelSize(max(48, min(96, int(area.height() * 0.35))))
        big.setWeight(QFont.Weight.DemiBold)
        p.setFont(big)
        p.setPen(ink["text"])
        v = d.stat_value
        txt = "—" if v is None else compact(v, v)
        p.drawText(QRectF(area.left() + 8, area.top() + 36, area.width(), big.pixelSize() * 1.4), Qt.AlignmentFlag.AlignLeft, txt)
        p.setFont(self.font())

    def _axis_x_values(self, p, plot: QRectF, ticks, lo, hi, ink, fmt=None):
        fm = QFontMetrics(self.font())
        for t in ticks:
            x = plot.left() + (t - lo) / (hi - lo) * plot.width()
            p.setPen(QPen(ink["grid"], 1))
            p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            label = fmt(t) if fmt else compact(t, max(abs(lo), abs(hi)))
            p.setPen(ink["secondary"])
            w = fm.horizontalAdvance(label)
            p.drawText(QPointF(min(max(x - w / 2, plot.left() - 4), plot.right() - w + 4), plot.bottom() + fm.ascent() + 6), label)

    def _bars(self, p: QPainter, area: QRectF, ink) -> None:
        d = self.data
        fm = QFontMetrics(self.font())
        k = len(d.series)
        # only as many bars as can be labelled legibly; the data is ranked, so these are the top ones
        fit = max(1, int((area.height() - fm.height() - 18) // max(fm.height() + 4, 6 * k)))
        total = len(d.series[0].points)
        if total > fit:
            from dbide.core.chart_model import Series

            d = type(d)(d.spec, [Series(s.name, s.points[:fit]) for s in d.series], d.x_kind)
        cats = [x for x, _ in d.series[0].points]
        vals = [v for s in d.series for _, v in s.points]
        lo, hi = min(0.0, min(vals)), max(0.0, max(vals))
        ticks = nice_ticks(lo, hi)
        lo, hi = min(ticks[0], lo), max(ticks[-1], hi)
        label_w = min(area.width() * 0.35, max(fm.horizontalAdvance(c) for c in cats) + 12)
        value_w = fm.horizontalAdvance(compact(hi, hi)) + 10
        plot = QRectF(area.left() + label_w, area.top() + 4, area.width() - label_w - value_w, area.height() - fm.height() - 14)
        if plot.width() < 40 or plot.height() < 20:
            return
        self._axis_x_values(p, plot, ticks, lo, hi, ink)
        band = plot.height() / len(cats)
        thick = min(24.0, max(3.0, (band * 0.72 - 2 * (k - 1)) / k))
        group_h = thick * k + 2 * (k - 1)
        x0 = plot.left() + (0 - lo) / (hi - lo) * plot.width()
        label_every = len(cats) <= 12
        for ci, cat in enumerate(cats):
            top = plot.top() + ci * band + (band - group_h) / 2
            p.setPen(ink["secondary"])
            p.drawText(QRectF(area.left(), plot.top() + ci * band, label_w - 10, band),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, fm.elidedText(cat, Qt.TextElideMode.ElideRight, int(label_w - 10)))
            lines = []
            for si, s in enumerate(d.series):
                v = s.points[ci][1]
                x1 = plot.left() + (v - lo) / (hi - lo) * plot.width()
                r = QRectF(min(x0, x1), top + si * (thick + 2), abs(x1 - x0), thick)
                hovered = self._hover is not None and QRectF(plot.left(), plot.top() + ci * band, plot.width() + value_w, band).contains(self._hover[0])
                c = QColor(series_color(si))
                if hovered:
                    c = c.lighter(115)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(c)
                p.drawPath(_bar_path(r, v >= 0))
                if label_every or ci < 3:
                    txt = compact(v, hi)
                    p.setPen(ink["secondary"])
                    tx = r.right() + 6 if v >= 0 else r.left() - 6 - fm.horizontalAdvance(txt)
                    if k == 1 or thick >= fm.height() * 0.8:
                        p.drawText(QPointF(tx, r.center().y() + fm.ascent() / 2 - 1), txt)
                lines.append((compact(v, hi), s.name, series_color(si)))
            self._hits.append((QRectF(area.left(), plot.top() + ci * band, area.width(), band), [(cat, "", None)] + lines))
        p.setPen(QPen(ink["base"], 1))
        p.drawLine(QPointF(x0, plot.top()), QPointF(x0, plot.bottom()))
        note = f"Showing the top {len(cats)} of {total}: make the panel taller, or see the Result tab." if total > len(cats) else ""
        if note != getattr(self, "_last_note", None):
            self._last_note = note
            QTimer.singleShot(0, lambda n=note: self.fit_note.emit(n))

    def _lines(self, p: QPainter, area: QRectF, ink) -> None:
        d = self.data
        fm = QFontMetrics(self.font())
        xs = [x for s in d.series for x, _ in s.points]
        ys = [y for s in d.series for _, y in s.points]
        if not xs:
            return
        xlo, xhi = min(xs), max(xs)
        if xlo == xhi:
            xlo, xhi = xlo - 1, xhi + 1
        yt = nice_ticks(min(0.0, min(ys)) if min(ys) >= 0 else min(ys), max(ys))
        ylo, yhi = yt[0], yt[-1]
        axis_w = max(fm.horizontalAdvance(compact(t, yhi)) for t in yt) + 10
        end_label_w = 0
        if len(d.series) <= 4:
            end_label_w = max(fm.horizontalAdvance(compact(s.points[-1][1], yhi)) for s in d.series if s.points) + 14
        plot = QRectF(area.left() + axis_w, area.top() + 6, area.width() - axis_w - end_label_w, area.height() - fm.height() - 16)
        if plot.width() < 40 or plot.height() < 20:
            return
        for t in yt:
            y = plot.bottom() - (t - ylo) / (yhi - ylo) * plot.height()
            p.setPen(QPen(ink["grid"] if t != 0 else ink["base"], 1))
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            p.setPen(ink["secondary"])
            lab = compact(t, yhi)
            p.drawText(QPointF(plot.left() - 8 - fm.horizontalAdvance(lab), y + fm.ascent() / 2 - 1), lab)
        # x ticks
        if d.x_kind == "time":
            span = xhi - xlo
            fmt = "%Y" if span > 3 * 365 * 86400 else "%b %Y" if span > 60 * 86400 else "%d %b"
            n = max(2, min(7, int(plot.width() // 110)))
            ticks = [xlo + i * (xhi - xlo) / (n - 1) for i in range(n)]
            label = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime(fmt)  # noqa: E731
        else:
            ticks = [t for t in nice_ticks(xlo, xhi) if xlo <= t <= xhi]
            label = lambda t: compact(t, xhi)  # noqa: E731
        for t in ticks:
            x = plot.left() + (t - xlo) / (xhi - xlo) * plot.width()
            lab = label(t)
            w = fm.horizontalAdvance(lab)
            p.setPen(ink["secondary"])
            p.drawText(QPointF(min(max(x - w / 2, plot.left()), plot.right() - w), plot.bottom() + fm.ascent() + 6), lab)

        def pt(x, y):
            return QPointF(plot.left() + (x - xlo) / (xhi - xlo) * plot.width(), plot.bottom() - (y - ylo) / (yhi - ylo) * plot.height())

        for si, s in enumerate(d.series):
            if not s.points:
                continue
            c = series_color(si)
            path = QPainterPath(pt(*s.points[0]))
            for x, y in s.points[1:]:
                path.lineTo(pt(x, y))
            if len(d.series) == 1 and len(s.points) > 1:  # area wash for a single series
                fill = QPainterPath(path)
                fill.lineTo(pt(s.points[-1][0], max(ylo, 0)))
                fill.lineTo(pt(s.points[0][0], max(ylo, 0)))
                fill.closeSubpath()
                wash = QColor(c)
                wash.setAlphaF(0.10)
                p.fillPath(fill, wash)
            p.setPen(QPen(c, 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
            end = pt(*s.points[-1])
            _dot(p, end, c, ink["surface"])
            if end_label_w:
                p.setPen(ink["secondary"])
                p.drawText(QPointF(end.x() + 8, end.y() + fm.ascent() / 2 - 1), compact(s.points[-1][1], yhi))
        self._line_geom = (plot, xlo, xhi, pt, label)
        if self._hover:  # crosshair snaps to the nearest x
            hx = self._hover[0].x()
            if plot.left() - 10 <= hx <= plot.right() + 10:
                allx = sorted({x for s in d.series for x, _ in s.points})
                nx = min(allx, key=lambda x: abs(pt(x, ylo).x() - hx))
                cx = pt(nx, ylo).x()
                p.setPen(QPen(ink["base"], 1))
                p.drawLine(QPointF(cx, plot.top()), QPointF(cx, plot.bottom()))
                lines = [(label(nx) if d.x_kind == "time" else compact(nx, xhi), "", None)]
                for si, s in enumerate(d.series):
                    for x, y in s.points:
                        if x == nx:
                            _dot(p, pt(x, y), series_color(si), ink["surface"])
                            lines.append((compact(y, yhi), s.name, series_color(si)))
                self._hover = (QPointF(cx, self._hover[0].y()), lines)

    def _scatter(self, p: QPainter, area: QRectF, ink) -> None:
        d = self.data
        fm = QFontMetrics(self.font())
        pts = d.series[0].points
        xs, ys = [x for x, _ in pts], [y for _, y in pts]
        xt, yt = nice_ticks(min(xs), max(xs)), nice_ticks(min(ys), max(ys))
        xlo, xhi, ylo, yhi = xt[0], xt[-1], yt[0], yt[-1]
        axis_w = max(fm.horizontalAdvance(compact(t, yhi)) for t in yt) + 10
        plot = QRectF(area.left() + axis_w, area.top() + 6, area.width() - axis_w - 8, area.height() - fm.height() - 16)
        if plot.width() < 40 or plot.height() < 20:
            return
        self._axis_x_values(p, plot, xt, xlo, xhi, ink)
        for t in yt:
            y = plot.bottom() - (t - ylo) / (yhi - ylo) * plot.height()
            p.setPen(QPen(ink["grid"], 1))
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            p.setPen(ink["secondary"])
            lab = compact(t, yhi)
            p.drawText(QPointF(plot.left() - 8 - fm.horizontalAdvance(lab), y + fm.ascent() / 2 - 1), lab)
        c = series_color(0)
        best, best_d = None, 24.0  # nearest point within 24px
        for x, y in pts:
            q = QPointF(plot.left() + (x - xlo) / (xhi - xlo) * plot.width(), plot.bottom() - (y - ylo) / (yhi - ylo) * plot.height())
            _dot(p, q, c, ink["surface"])
            if self._hover:
                dd = math.hypot(q.x() - self._hover[0].x(), q.y() - self._hover[0].y())
                if dd < best_d:
                    best, best_d = (q, x, y), dd
        p.setPen(ink["muted"])
        p.drawText(QRectF(plot.left(), plot.bottom() + fm.height() + 2, plot.width(), fm.height()), Qt.AlignmentFlag.AlignRight, humanize(d.spec.x))
        if self._hover:
            if best:
                q, x, y = best
                _dot(p, q, c.lighter(120), ink["surface"], r=6)
                self._hover = (q, [(compact(y, yhi), d.series[0].name, c), (compact(x, xhi), humanize(d.spec.x), None)])
            else:
                self._hover = None

    def _tooltip(self, p: QPainter, pos: QPointF, lines, ink) -> None:
        if not lines:
            return
        fm = QFontMetrics(self.font())
        bold = QFont(self.font())
        bold.setWeight(QFont.Weight.DemiBold)
        bfm = QFontMetrics(bold)
        rows_h = fm.height() + 4
        w = max(bfm.horizontalAdvance(v) + (fm.horizontalAdvance("  " + n) if n else 0) for v, n, _ in lines) + 34
        h = rows_h * len(lines) + 12
        x = pos.x() + 14
        y = pos.y() + 14
        if x + w > self.width() - 4:
            x = pos.x() - w - 14
        if y + h > self.height() - 4:
            y = self.height() - h - 4
        box = QRectF(x, max(4, y), w, h)
        p.setPen(QPen(ink["border"], 1))
        p.setBrush(ink["raised"])
        p.drawRoundedRect(box, 8, 8)
        ty = box.top() + 6
        for v, n, c in lines:
            tx = box.left() + 10
            if c is not None:  # a short line key, not a filled box
                p.setPen(QPen(c, 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawLine(QPointF(tx, ty + rows_h / 2), QPointF(tx + 10, ty + rows_h / 2))
                tx += 16
            p.setFont(bold)
            p.setPen(ink["text"])
            p.drawText(QPointF(tx, ty + bfm.ascent() + 2), v)  # values lead
            if n:
                p.setFont(self.font())
                p.setPen(ink["secondary"])
                p.drawText(QPointF(tx + bfm.horizontalAdvance(v) + 6, ty + fm.ascent() + 2), n)
            ty += rows_h
        p.setFont(self.font())

    # ---- hover ----------------------------------------------------------------
    def mouseMoveEvent(self, ev):
        pos = ev.position()
        d = self.data
        if d is None:
            return
        if d.spec.kind == "bar":
            hit = next((lines for r, lines in self._hits if r.contains(pos)), None)
            self._hover = (pos, hit) if hit else None
        elif d.spec.kind in ("line", "scatter"):
            self._hover = (pos, [])
        self.update()

    def leaveEvent(self, ev):
        self._hover = None
        self.update()

    def to_image(self, scale: float = 2.0) -> QImage:
        img = QImage(int(self.width() * scale), int(self.height() * scale), QImage.Format.Format_ARGB32)
        img.setDevicePixelRatio(scale)
        img.fill(QColor(theme().surface))
        self.render(img)
        return img


def _bar_path(r: QRectF, positive: bool, radius: float = 4.0) -> QPainterPath:
    """Square at the baseline, rounded at the data end."""
    rad = min(radius, r.width() / 2, r.height() / 2)
    path = QPainterPath()
    if r.width() <= 0:
        return path
    if positive:
        path.moveTo(r.left(), r.top())
        path.lineTo(r.right() - rad, r.top())
        path.quadTo(r.right(), r.top(), r.right(), r.top() + rad)
        path.lineTo(r.right(), r.bottom() - rad)
        path.quadTo(r.right(), r.bottom(), r.right() - rad, r.bottom())
        path.lineTo(r.left(), r.bottom())
    else:
        path.moveTo(r.right(), r.top())
        path.lineTo(r.left() + rad, r.top())
        path.quadTo(r.left(), r.top(), r.left(), r.top() + rad)
        path.lineTo(r.left(), r.bottom() - rad)
        path.quadTo(r.left(), r.bottom(), r.left() + rad, r.bottom())
        path.lineTo(r.right(), r.bottom())
    path.closeSubpath()
    return path


def _dot(p: QPainter, c: QPointF, color: QColor, surface: QColor, r: float = 4.0) -> None:
    p.setPen(QPen(surface, 2))  # 2px surface ring keeps dots legible over lines
    p.setBrush(color)
    p.drawEllipse(c, r + 1, r + 1)


class ChartPanel(QWidget):
    """The Chart tab: an automatic chart, plus a one-click Customize row for experts."""

    spec_changed = Signal(object)

    def __init__(self, columns: list[str], rows: list[tuple], advanced: bool = False):
        super().__init__()
        self.columns, self.rows = columns, rows
        self.auto = auto_spec(columns, rows)
        self.spec = self.auto
        self.chart = ChartWidget()
        tm = ThemeManager.get()

        self.summary = QLabel()
        self.summary.setObjectName("Muted")
        self.summary.setWordWrap(True)
        self._summary_text = ""
        self.chart.fit_note.connect(self._show_fit_note)
        self.custom_btn = QToolButton()
        self.custom_btn.setText("Customize")
        self.custom_btn.setCheckable(True)
        self.custom_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tm.bind_icon(self.custom_btn, "sliders", "muted")
        self.custom_btn.toggled.connect(lambda on: self.controls.setVisible(on))
        from dbide.ui.features import Gate

        Gate.get().bind_widget(self.custom_btn, "chart_custom")
        save = QToolButton()
        save.setText("Save PNG")
        save.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tm.bind_icon(save, "export", "muted")
        save.clicked.connect(self._save)
        top = QHBoxLayout()
        top.setContentsMargins(12, 8, 12, 0)
        top.addWidget(self.summary, 1)
        top.addWidget(self.custom_btn)
        top.addWidget(save)

        # advanced controls: type, X, Y
        self.controls = QWidget()
        cl = QHBoxLayout(self.controls)
        cl.setContentsMargins(12, 4, 12, 0)
        self.kind = QComboBox()
        for k, label in (("auto", "Automatic"), ("bar", "Bars"), ("line", "Line"), ("scatter", "Scatter"), ("stat", "Single number")):
            self.kind.addItem(label, k)
        self.xcol = QComboBox()
        for c in columns:
            self.xcol.addItem(c, c)
        self.ybtn = QToolButton()
        self.ybtn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.ymenu = QMenu(self.ybtn)
        self.yacts: dict[str, QAction] = {}
        for c in columns:
            if classify(c, [r[columns.index(c)] for r in rows]) == "measure":
                a = self.ymenu.addAction(c)
                a.setCheckable(True)
                a.toggled.connect(lambda _on: self._custom())
                self.yacts[c] = a
        self.ybtn.setMenu(self.ymenu)
        cl.addWidget(QLabel("Chart"))
        cl.addWidget(self.kind)
        cl.addWidget(QLabel("X"))
        cl.addWidget(self.xcol)
        cl.addWidget(QLabel("Y"))
        cl.addWidget(self.ybtn)
        cl.addStretch()
        self.kind.currentIndexChanged.connect(lambda _: self._custom())
        self.xcol.currentIndexChanged.connect(lambda _: self._custom())
        from dbide.ui.features import Gate as _G

        advanced = advanced and _G.get().allowed("chart_custom")
        self.controls.setVisible(advanced)
        self.custom_btn.setChecked(advanced)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lay.addLayout(top)
        lay.addWidget(self.controls)
        lay.addWidget(self.chart, 1)
        self._sync_controls(self.auto)
        self._render(self.auto)

    @property
    def has_chart(self) -> bool:
        return self.auto.kind != "none"

    def _sync_controls(self, spec: ChartSpec) -> None:
        for w in (self.kind, self.xcol):
            w.blockSignals(True)
        if spec.x:
            self.xcol.setCurrentIndex(max(0, self.xcol.findData(spec.x)))
        for c, a in self.yacts.items():
            a.blockSignals(True)
            a.setChecked(c in spec.ys)
            a.blockSignals(False)
        self.ybtn.setText(", ".join(spec.ys) or "choose…")
        for w in (self.kind, self.xcol):
            w.blockSignals(False)

    def _custom(self) -> None:
        kind = self.kind.currentData()
        if kind == "auto":
            self._sync_controls(self.auto)
            self._render(self.auto)
            return
        ys = [c for c, a in self.yacts.items() if a.isChecked()] or list(self.auto.ys) or list(self.yacts)[:1]
        x = self.xcol.currentData()
        spec = ChartSpec(kind, x=x, ys=ys[: 1 if kind in ("scatter", "stat") else 8], reason="Custom chart.")
        self.ybtn.setText(", ".join(spec.ys) or "choose…")
        self._render(spec)

    def _render(self, spec: ChartSpec) -> None:
        self.spec = spec
        try:
            data = build(spec, self.columns, self.rows)
        except Exception as exc:  # noqa: BLE001 - a custom combination that can't be drawn
            data = ChartData(ChartSpec("none", reason=f"Can't draw that combination: {exc}"), [])
        self.chart.set_data(data)
        prefix = "Automatic" if spec is self.auto else "Custom"
        self._summary_text = f"{prefix}: {data.spec.reason}"
        self.summary.setText(self._summary_text + (f"  ({spec.note})" if spec.note else ""))
        self.spec_changed.emit(spec)

    def _show_fit_note(self, note: str) -> None:
        self.summary.setText(self._summary_text + (f"  {note}" if note else (f"  ({self.spec.note})" if self.spec.note else "")))

    def _save(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save chart", "chart.png", "PNG image (*.png)")
        if path:
            self.chart.to_image().save(path)

