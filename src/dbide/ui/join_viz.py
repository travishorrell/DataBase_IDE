"""Visual JOIN explainer: tables, linking columns, a Venn diagram, and real match counts."""
from __future__ import annotations

import html

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from dbide.core.sql_explain import JOIN_WORDS, JoinStep, extract_joins, join_stat_queries, rows_for_kind
from dbide.ui.grid import GridView, ResultModel
from dbide.ui.worker import run_async


class JoinDiagram(QWidget):
    """Two tables with their linking columns, and a Venn diagram of what the join keeps."""

    def __init__(self, step: JoinStep, left_cols: list[str], right_cols: list[str]):
        super().__init__()
        self.step, self.left_cols, self.right_cols = step, left_cols, right_cols
        self.stats: dict | None = None
        self.setMinimumHeight(250)
        self.setMinimumWidth(800)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_stats(self, stats: dict) -> None:
        self.stats = stats
        self.update()

    def _cols_to_show(self, cols: list[str], keys: list[str]) -> list[str]:
        shown = [c for c in cols if c in keys]
        rest = [c for c in cols if c not in keys][: max(0, 5 - len(shown))]
        out = shown + rest
        if len(cols) > len(out):
            out.append("…")
        return out

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        from dbide.ui.theme import theme

        th = theme()
        text, sub = QColor(th.text), QColor(th.muted)
        a_col, b_col, hi = QColor(th.accent), QColor(th.warning), QColor(th.success)
        step = self.step
        lkeys = [x[1] for x in step.pairs]
        rkeys = [x[3] for x in step.pairs]
        lshow = self._cols_to_show(self.left_cols, lkeys)
        rshow = self._cols_to_show(self.right_cols, rkeys)

        def box(x, y, title, cols, keys, color) -> dict[str, QPointF]:
            w, rh = 190, 22
            h = 28 + rh * len(cols)
            p.setPen(QPen(color, 1.6))
            p.setBrush(QColor(color.red(), color.green(), color.blue(), 28))
            p.drawRoundedRect(QRectF(x, y, w, h), 6, 6)
            f = QFont()
            f.setBold(True)
            p.setFont(f)
            p.setPen(text)
            p.drawText(QRectF(x + 8, y, w - 16, 28), Qt.AlignmentFlag.AlignVCenter, title)
            anchors = {}
            for i, c in enumerate(cols):
                cy = y + 28 + rh * i
                is_key = c in keys
                f.setBold(is_key)
                p.setFont(f)
                if is_key:
                    p.fillRect(QRectF(x + 2, cy, w - 4, rh), QColor(hi.red(), hi.green(), hi.blue(), 50))
                p.setPen(text if c != "…" else sub)
                p.drawText(QRectF(x + 10, cy, w - 20, rh), Qt.AlignmentFlag.AlignVCenter, ("🔗 " if is_key else "") + c)
                anchors[c] = QPointF(x, cy + rh / 2)
            return anchors

        lt = f"{step.left.name}" + (f" ({step.left.alias})" if step.left.alias != step.left.name else "")
        rt = f"{step.right.name}" + (f" ({step.right.alias})" if step.right.alias != step.right.name else "")
        la = box(10, 20, lt, lshow, lkeys, a_col)
        ra = box(330, 20, rt, rshow, rkeys, b_col)
        p.setPen(QPen(hi, 2.2))
        for _la, lc, _ra, rc in step.pairs:
            if lc in la and rc in ra:
                a = la[lc] + QPointF(190, 0)
                b = ra[rc]
                path = QPainterPath(a)
                path.cubicTo(a + QPointF(60, 0), b - QPointF(60, 0), b)
                p.drawPath(path)
                p.setFont(QFont())
                p.drawText(QRectF((a.x() + b.x()) / 2 - 15, (a.y() + b.y()) / 2 - 20, 30, 16), Qt.AlignmentFlag.AlignCenter, "=")

        # Venn diagram
        cx, cy, r = 650, 110, 62
        left_c = QRectF(cx - r - 30, cy - r, 2 * r, 2 * r)
        right_c = QRectF(cx - r + 30, cy - r, 2 * r, 2 * r)
        lp, rp = QPainterPath(), QPainterPath()
        lp.addEllipse(left_c)
        rp.addEllipse(right_c)
        inter = lp.intersected(rp)
        kind = step.kind
        fill = QColor(hi.red(), hi.green(), hi.blue(), 150)
        if kind == "CROSS":
            p.setPen(QPen(sub, 1, Qt.PenStyle.DashLine))
            for i in range(5):
                p.drawLine(QPointF(cx - 80, cy - 50 + i * 25), QPointF(cx + 80, cy - 50 + i * 25))
                p.drawLine(QPointF(cx - 80 + i * 40, cy - 60), QPointF(cx - 80 + i * 40, cy + 60))
        else:
            region = inter
            if kind == "LEFT":
                region = lp
            elif kind == "RIGHT":
                region = rp
            elif kind == "FULL":
                region = lp.united(rp)
            p.fillPath(region, fill)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(a_col, 2))
            p.drawEllipse(left_c)
            p.setPen(QPen(b_col, 2))
            p.drawEllipse(right_c)
        p.setPen(text)
        f = QFont()
        f.setBold(True)
        p.setFont(f)
        p.drawText(QRectF(cx - 120, cy + r + 8, 240, 20), Qt.AlignmentFlag.AlignCenter, f"{kind} JOIN")
        f.setBold(False)
        p.setFont(f)
        p.setPen(sub)
        p.drawText(QRectF(cx - 140, cy - r - 22, 120, 18), Qt.AlignmentFlag.AlignCenter, step.left.alias)
        p.drawText(QRectF(cx + 20, cy - r - 22, 120, 18), Qt.AlignmentFlag.AlignCenter, step.right.alias)
        if self.stats:
            s = self.stats
            p.setPen(text)
            p.drawText(QRectF(cx - r - 30 - 10, cy - 8, 50, 16), Qt.AlignmentFlag.AlignCenter, str(s["left_unmatched"]))
            p.drawText(QRectF(cx - 25, cy - 8, 50, 16), Qt.AlignmentFlag.AlignCenter, str(s["inner_rows"]))
            p.drawText(QRectF(cx + r + 30 - 40, cy - 8, 50, 16), Qt.AlignmentFlag.AlignCenter, str(s["right_unmatched"]))
        p.end()


class JoinStepView(QWidget):
    def __init__(self, session, step: JoinStep, index: int):
        super().__init__()
        self.session, self.step = session, step
        lay = QVBoxLayout(self)
        title = QLabel(
            f"<b>Step {index}:</b> {html.escape(step.left.name if step.left else '?')} "
            f"<b>{step.kind} JOIN</b> {html.escape(step.right.name)}"
            + (f" <b>ON</b> <code>{html.escape(step.on_sql)}</code>" if step.on_sql else "")
        )
        title.setTextFormat(Qt.TextFormat.RichText)
        title.setWordWrap(True)
        lay.addWidget(title)
        meaning = QLabel(f"<i>{step.kind} JOIN: {JOIN_WORDS[step.kind]}.</i>")
        meaning.setWordWrap(True)
        lay.addWidget(meaning)
        self.diagram = None
        self.stats_label = QLabel("Counting rows…")
        self.stats_label.setTextFormat(Qt.TextFormat.RichText)
        self.stats_label.setWordWrap(True)
        self.samples = QTabWidget()
        self.samples.setMinimumHeight(170)
        self._lay = lay
        if not step.left or not step.pairs:
            self.stats_label.setText(
                "I couldn't tell which columns link the tables. Qualify the ON columns with table names or aliases, "
                "e.g. <code>ON o.customer_id = c.id</code>, so they can be visualized."
                if step.kind != "CROSS" else
                "A CROSS JOIN has no condition: every row of one table is paired with every row of the other."
            )
            lay.addWidget(self.stats_label)
            lay.addStretch()
            return

        def cols():
            return ([c["name"] for c in session.columns(step.left.name, step.left.schema)],
                    [c["name"] for c in session.columns(step.right.name, step.right.schema)])

        run_async(cols, self._got_cols, lambda m: self.stats_label.setText(f"Could not read columns: {html.escape(m)}"))

    def _got_cols(self, cols) -> None:
        self.diagram = JoinDiagram(self.step, *cols)
        self._lay.addWidget(self.diagram)
        self._lay.addWidget(self.stats_label)
        self._lay.addWidget(self.samples, 1)
        queries = join_stat_queries(self.step, self.session.driver.quote, self.session.driver.qualify)
        s = self.session

        def work():
            out, samples = {}, {}
            with s.engine.connect() as conn:
                for k, q in queries.items():
                    res = conn.exec_driver_sql(q)
                    if k.endswith("_sample"):
                        samples[k] = (list(res.keys()), [tuple(r) for r in res.fetchall()])
                    else:
                        out[k] = int(res.scalar() or 0)
            return out, samples

        run_async(work, self._got_stats, lambda m: self.stats_label.setText(f"Could not count rows: {html.escape(m)}"))

    def _got_stats(self, result) -> None:
        stats, samples = result
        self.diagram.set_stats(stats)
        st = self.step
        L, R = html.escape(st.left.name), html.escape(st.right.name)
        lines = [
            f"<b>{L}</b>: {stats['left_total']} rows, of which <b>{stats['left_total'] - stats['left_unmatched']}</b> have a match "
            f"and <b>{stats['left_unmatched']}</b> have none.",
            f"<b>{R}</b>: {stats['right_total']} rows, of which <b>{stats['right_total'] - stats['right_unmatched']}</b> have a match "
            f"and <b>{stats['right_unmatched']}</b> have none.",
            f"Matching pairs: <b>{stats['inner_rows']}</b>"
            + (" (more than the rows on one side, because one row matches several on the other side: one-to-many)"
               if stats["inner_rows"] > min(stats["left_total"], stats["right_total"]) else "") + ".",
        ]
        n = rows_for_kind(st.kind, stats)
        keep = {
            "INNER": "only the matching pairs",
            "LEFT": f"the matching pairs <b>plus</b> the {stats['left_unmatched']} unmatched {L} rows (with NULLs for {R})",
            "RIGHT": f"the matching pairs <b>plus</b> the {stats['right_unmatched']} unmatched {R} rows (with NULLs for {L})",
            "FULL": "the matching pairs plus unmatched rows from both sides",
            "CROSS": "every combination",
        }[st.kind]
        lines.append(f"➡️ This <b>{st.kind} JOIN</b> returns <b>{n}</b> rows: {keep}.")
        if st.kind == "INNER" and stats["left_unmatched"]:
            lines.append(f"💡 Use <b>LEFT JOIN</b> to also see the {stats['left_unmatched']} {L} rows without a match "
                         f"(add <code>WHERE {html.escape(st.right.alias)}.{html.escape(st.pairs[0][3])} IS NULL</code> to see <i>only</i> them).")
        self.stats_label.setText("<br>".join(lines))
        for key, label in (("matched_sample", "Matched pairs"), ("left_sample", f"{st.left.name} without match"),
                           ("right_sample", f"{st.right.name} without match")):
            cols, rows = samples.get(key, ([], []))
            g = GridView()
            g.setModel(ResultModel(cols, rows, g))
            g.fit_columns()
            self.samples.addTab(g, f"{label} ({len(rows)}{'+' if len(rows) == 5 else ''})")


class JoinVisualizer(QDialog):
    def __init__(self, session, sql: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("JOIN Explainer")
        self.resize(900, 720)
        lay = QVBoxLayout(self)
        analysis = extract_joins(sql, session.cfg.driver)
        if not analysis or not analysis.steps:
            msg = QLabel("No JOIN found in this statement. Put the cursor inside a SELECT … JOIN … query and try again.")
            msg.setWordWrap(True)
            lay.addWidget(msg)
            return
        intro = QLabel(
            "A JOIN goes through the rows of the first table and attaches the rows of the next table whose linking columns match. "
            "Green lines show the columns being compared. The Venn diagram shows what this join type keeps, "
            "and the numbers are counted from your real data."
        )
        intro.setWordWrap(True)
        lay.addWidget(intro)
        tabs = QTabWidget()
        for i, step in enumerate(analysis.steps, 1):
            sa = QScrollArea()
            sa.setWidgetResizable(True)
            sa.setWidget(JoinStepView(session, step, i))
            tabs.addTab(sa, f"{step.kind} JOIN {step.right.name}")
        lay.addWidget(tabs, 1)
