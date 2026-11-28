"""Pick a chart for a query result, and prepare its data (no Qt here).

The form follows the data's job:
  one number                      → stat tile (the number is the chart)
  category + one measure          → ranked horizontal bars, one hue
  category + 2-4 measures         → grouped horizontal bars, categorical hues + legend
  date/ordered x + measure(s)     → line chart
  two measures, no category       → scatter
  anything else                   → no chart; the table is the right view
"""
from __future__ import annotations

import datetime as dt
import decimal
import math
import re
from dataclasses import dataclass, field

MAX_BARS = 24
MAX_SERIES = 8
MAX_SCATTER_SERIES = 1

ISO_DATE = re.compile(r"^\d{4}-\d{2}(-\d{2})?([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?")


def is_number(v) -> bool:
    return isinstance(v, (int, float, decimal.Decimal)) and not isinstance(v, bool) and not (
        isinstance(v, float) and math.isnan(v))


def to_float(v) -> float | None:
    if is_number(v):
        return float(v)
    return None


def to_time(v) -> float | None:
    """Seconds since epoch for date-ish values, else None."""
    if isinstance(v, dt.datetime):
        return v.timestamp() if v.tzinfo else v.replace(tzinfo=dt.timezone.utc).timestamp()
    if isinstance(v, dt.date):
        return dt.datetime(v.year, v.month, v.day, tzinfo=dt.timezone.utc).timestamp()
    if isinstance(v, str) and ISO_DATE.match(v):
        s = v.strip()
        if len(s) == 7:  # "2024-03"
            s += "-01"
        try:
            d = dt.datetime.fromisoformat(s)
        except ValueError:
            return None
        return (d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)).timestamp()
    return None


def classify(name: str, values: list) -> str:
    """'measure', 'time', 'category' or 'empty' for one result column."""
    vals = [v for v in values if v is not None]
    if not vals:
        return "empty"
    lname = name.lower()
    if all(is_number(v) for v in vals):
        if lname == "id" or lname.endswith("_id") or lname in ("year", "month", "day"):
            return "time" if lname in ("year",) else "category"
        return "measure"
    if sum(1 for v in vals if to_time(v) is not None) >= 0.9 * len(vals):
        return "time"
    return "category"


@dataclass
class ChartSpec:
    kind: str  # stat | bar | line | scatter | none
    x: str | None = None
    ys: list[str] = field(default_factory=list)
    reason: str = ""
    note: str = ""  # e.g. "showing top 24 of 80"


@dataclass
class Series:
    name: str
    points: list[tuple[object, float]]  # (x, y) — x is a label, a timestamp or a number


@dataclass
class ChartData:
    spec: ChartSpec
    series: list[Series]
    x_kind: str = "category"  # category | time | number
    stat_value: float | None = None
    stat_label: str = ""


def humanize(col: str) -> str:
    s = col.replace("_", " ").strip()
    return s[:1].upper() + s[1:]


def auto_spec(columns: list[str], rows: list[tuple]) -> ChartSpec:
    if not rows or not columns:
        return ChartSpec("none", reason="There are no rows to chart.")
    kinds = {c: classify(c, [r[i] for r in rows]) for i, c in enumerate(columns)}
    measures = [c for c in columns if kinds[c] == "measure"]
    times = [c for c in columns if kinds[c] == "time"]
    cats = [c for c in columns if kinds[c] == "category"]

    if len(rows) == 1 and len(measures) == 1 and len(columns) <= 2:
        return ChartSpec("stat", x=cats[0] if cats else None, ys=measures, reason="A single number: shown as a stat tile.")
    if not measures:
        return ChartSpec("none", reason="No numeric column to plot. The table is the best view of this result.")
    if times and len(rows) >= 2:
        ys = measures[:MAX_SERIES]
        return ChartSpec("line", x=times[0], ys=ys, reason=f"{humanize(ys[0])} over {humanize(times[0]).lower()}: a line shows the trend.",
                         note="" if len(measures) <= MAX_SERIES else f"showing the first {MAX_SERIES} measures")
    if cats and len(cats) == 1 and 1 <= len(measures) <= 4:
        note = f"showing the top {MAX_BARS} of {len(rows)}; the rest are in the table" if len(rows) > MAX_BARS else ""
        if len(measures) == 1:
            return ChartSpec("bar", x=cats[0], ys=measures,
                             reason=f"Comparing {humanize(measures[0]).lower()} across {humanize(cats[0]).lower()}: ranked bars, largest first.", note=note)
        return ChartSpec("bar", x=cats[0], ys=measures, reason=f"{len(measures)} measures per {humanize(cats[0]).lower()}: grouped bars with a legend.", note=note)
    if not cats and len(measures) >= 2 and len(rows) >= 3:
        return ChartSpec("scatter", x=measures[0], ys=[measures[1]],
                         reason=f"Two measures: a scatter shows how {humanize(measures[1]).lower()} relates to {humanize(measures[0]).lower()}.")
    if len(cats) > 1 and len(measures) == 1:
        return ChartSpec("none", reason="Several category columns: group the query by one of them to get a clear chart, or pick X yourself under Customize.")
    return ChartSpec("none", reason="No chart fits this shape well. The table is the best view.")


def build(spec: ChartSpec, columns: list[str], rows: list[tuple]) -> ChartData:
    idx = {c: i for i, c in enumerate(columns)}
    if spec.kind == "stat":
        y = idx[spec.ys[0]]
        label = humanize(spec.ys[0])
        if spec.x:
            label = f"{label} · {rows[0][idx[spec.x]]}"
        return ChartData(spec, [], stat_value=to_float(rows[0][y]), stat_label=label)
    if spec.kind == "none" or spec.x not in idx:
        return ChartData(spec, [])
    xi = idx[spec.x]
    if spec.kind == "bar":
        series = []
        data_rows = [r for r in rows if any(to_float(r[idx[y]]) is not None for y in spec.ys)]
        if len(spec.ys) == 1:
            yi = idx[spec.ys[0]]
            data_rows.sort(key=lambda r: -(to_float(r[yi]) or 0))  # ranked, largest first
        data_rows = data_rows[:MAX_BARS]
        for y in spec.ys:
            yi = idx[y]
            series.append(Series(humanize(y), [(_label(r[xi]), to_float(r[yi]) or 0.0) for r in data_rows]))
        return ChartData(spec, series, "category")
    if spec.kind == "line":
        pts = []
        for r in rows:
            t = to_time(r[xi]) if not is_number(r[xi]) else float(r[xi])
            if t is not None:
                pts.append((t, r))
        pts.sort(key=lambda p: p[0])
        x_kind = "time" if rows and not is_number(next((r[xi] for r in rows if r[xi] is not None), 0)) else "number"
        series = [Series(humanize(y), [(t, to_float(r[idx[y]])) for t, r in pts if to_float(r[idx[y]]) is not None]) for y in spec.ys]
        return ChartData(spec, series, x_kind)
    if spec.kind == "scatter":
        yi = idx[spec.ys[0]]
        pts = [(to_float(r[xi]), to_float(r[yi])) for r in rows if to_float(r[xi]) is not None and to_float(r[yi]) is not None]
        return ChartData(spec, [Series(humanize(spec.ys[0]), pts[:5000])], "number")
    return ChartData(spec, [])


def _label(v) -> str:
    if v is None:
        return "(none)"
    s = str(v)
    return s if len(s) <= 40 else s[:39] + "…"


def nice_ticks(lo: float, hi: float, target: int = 5) -> list[float]:
    """Round tick values covering [lo, hi] (0 included for bars by the caller)."""
    if hi == lo:
        hi = lo + (abs(lo) or 1)
    span = hi - lo
    raw = span / max(1, target)
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.floor(lo / step) * step
    ticks = []
    v = start
    while v <= hi + step * 0.5 and len(ticks) < 50:
        ticks.append(round(v, 10))
        v += step
    return ticks


def compact(v: float, axis_max: float | None = None) -> str:
    """1,284 / 12.9K / 4.2M — full under 10,000, compact above (one style per axis)."""
    ref = abs(axis_max if axis_max is not None else v)
    a = abs(v)
    if ref >= 10_000:
        for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
            if ref >= div:
                s = f"{v / div:.1f}".rstrip("0").rstrip(".")
                return f"{s}{suf}"
    if a == int(a):
        return f"{int(v):,}"
    return f"{v:,.2f}".rstrip("0").rstrip(".")
