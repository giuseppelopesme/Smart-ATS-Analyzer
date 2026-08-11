"""Reconstruct reading order from placed text, the way an ATS parser would.

The central idea: extract the page *twice*.

* ``naive_text`` replays the content stream in the order the operators appear.
  Plenty of production resume parsers do exactly this.
* ``layout_text`` groups spans into visual lines, detects column gutters, and
  reads each column top-to-bottom.

When those two disagree, the resume's meaning depends on the extractor. That
is the single highest-value signal here -- it is what turns "Senior Backend
Engineer / Python" into "Senior Backend Engineer Python" in one system and
keeps them apart in another.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from pdfmini import Page, Span

__all__ = [
    "Line",
    "Column",
    "PageLayout",
    "analyse_page",
    "naive_text",
    "layout_text",
]


@dataclass
class Line:
    y: float
    spans: List[Span] = field(default_factory=list)

    @property
    def x0(self) -> float:
        return min(s.x for s in self.spans)

    @property
    def x1(self) -> float:
        return max(s.x1 for s in self.spans)

    @property
    def size(self) -> float:
        return max((s.size for s in self.spans), default=0.0)

    @property
    def text(self) -> str:
        return _join_spans(sorted(self.spans, key=lambda s: s.x))

    @property
    def stream_text(self) -> str:
        return _join_spans(sorted(self.spans, key=lambda s: s.order))


@dataclass
class Column:
    x0: float
    x1: float
    lines: List[Line] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


@dataclass
class PageLayout:
    page: Page
    lines: List[Line]
    columns: List[Column]
    gutters: List[Tuple[float, float, float]]  # (x_start, x_end, vertical_coverage)
    body_size: float

    @property
    def multi_column(self) -> bool:
        return len(self.columns) > 1


def _join_spans(spans: Sequence[Span]) -> str:
    """Concatenate spans on a line, inserting a space where there is a gap."""
    out: List[str] = []
    prev: Optional[Span] = None
    for s in spans:
        if not s.text:
            continue
        if prev is not None:
            gap = s.x - prev.x1
            # A gap wider than a quarter em is a word break; anything smaller
            # is kerning or a same-word split.
            threshold = max(0.25 * max(s.size, prev.size), 0.8)
            if gap > threshold and not out[-1].endswith(" ") and not s.text.startswith(" "):
                out.append(" ")
        out.append(s.text)
        prev = s
    return "".join(out).strip()


def build_lines(page: Page, tolerance_ratio: float = 0.5) -> List[Line]:
    """Cluster spans into visual lines by their baseline y."""
    visible = [s for s in page.spans if s.text.strip() and not s.invisible]
    if not visible:
        return []
    sizes = [s.size for s in visible if s.size > 0]
    body = statistics.median(sizes) if sizes else 10.0
    tol = max(body * tolerance_ratio, 1.5)

    lines: List[Line] = []
    for span in sorted(visible, key=lambda s: (round(s.y, 1), s.x)):
        placed = False
        for line in reversed(lines[-4:]):  # only recent lines can match
            if abs(line.y - span.y) <= tol:
                line.spans.append(span)
                line.y = (line.y * (len(line.spans) - 1) + span.y) / len(line.spans)
                placed = True
                break
        if not placed:
            lines.append(Line(y=span.y, spans=[span]))
    lines.sort(key=lambda ln: ln.y)
    return lines


def detect_gutters(
    page: Page,
    lines: Sequence[Line],
    min_width_pt: float = 22.0,
    min_rows: int = 3,
) -> List[Tuple[float, float, float]]:
    """Find vertical whitespace channels that split the page into columns.

    A gutter must be wide, must sit away from the margins, and -- critically --
    must have text on *both* sides on several separate lines. That last test
    is what separates a real two-column layout from a wide indent or a
    right-aligned date column.
    """
    spans = [s for ln in lines for s in ln.spans]
    if len(spans) < 8:
        return []

    text_x0 = min(s.x for s in spans)
    text_x1 = max(s.x1 for s in spans)
    span_width = text_x1 - text_x0
    if span_width < 100:
        return []

    bin_w = 2.0
    nbins = max(1, int(span_width / bin_w) + 1)
    occupied = [False] * nbins
    for s in spans:
        start = max(0, int((s.x - text_x0) / bin_w))
        end = min(nbins - 1, int((s.x1 - text_x0) / bin_w))
        for b in range(start, end + 1):
            occupied[b] = True

    gutters: List[Tuple[float, float, float]] = []
    b = 0
    while b < nbins:
        if occupied[b]:
            b += 1
            continue
        start = b
        while b < nbins and not occupied[b]:
            b += 1
        gx0 = text_x0 + start * bin_w
        gx1 = text_x0 + b * bin_w
        if gx1 - gx0 < min_width_pt:
            continue
        # Reject channels hugging the text block's own edges.
        if gx0 <= text_x0 + 0.10 * span_width or gx1 >= text_x1 - 0.10 * span_width:
            continue
        mid = (gx0 + gx1) / 2
        both, left_only, right_only = 0, 0, 0
        ys: List[float] = []
        for ln in lines:
            has_l = any(s.x1 <= mid + 1 for s in ln.spans)
            has_r = any(s.x >= mid - 1 for s in ln.spans)
            if has_l and has_r:
                both += 1
                ys.append(ln.y)
            elif has_l:
                left_only += 1
            elif has_r:
                right_only += 1
        if both < max(min_rows, 0.2 * len(lines)):
            continue
        # Require the right-hand side to be substantial, not a stray date.
        right_chars = sum(len(s.text) for s in spans if s.x >= mid)
        total_chars = sum(len(s.text) for s in spans) or 1
        if right_chars / total_chars < 0.12:
            continue
        # Measure how much of the *text block* the gutter runs through, not
        # how much of the page: a resume that ends halfway down the sheet
        # still has a full-height gutter as far as the reader is concerned.
        block_y0 = min(ln.y for ln in lines)
        block_y1 = max(ln.y for ln in lines)
        block_h = max(block_y1 - block_y0, 1.0)
        coverage = (max(ys) - min(ys)) / block_h if ys else 0.0
        if coverage < 0.25:
            continue
        page_coverage = (max(ys) - min(ys)) / page.height if (ys and page.height) else 0.0
        gutters.append((gx0, gx1, round(max(coverage, page_coverage), 3)))

    # Keep the widest gutters; more than two columns on a resume is rare.
    gutters.sort(key=lambda g: (g[1] - g[0]), reverse=True)
    return sorted(gutters[:2], key=lambda g: g[0])


def split_columns(lines: Sequence[Line], gutters: Sequence[Tuple[float, float, float]]) -> List[Column]:
    if not gutters:
        col = Column(x0=0.0, x1=1e9, lines=list(lines))
        return [col]

    bounds = [0.0] + [(g[0] + g[1]) / 2 for g in gutters] + [1e9]
    columns = [Column(x0=bounds[i], x1=bounds[i + 1]) for i in range(len(bounds) - 1)]

    for ln in lines:
        # A line that straddles a gutter (a full-width heading or rule) is
        # split so each fragment lands in its own column.
        buckets: List[List[Span]] = [[] for _ in columns]
        for s in ln.spans:
            centre = (s.x + s.x1) / 2
            for i, col in enumerate(columns):
                if col.x0 <= centre < col.x1:
                    buckets[i].append(s)
                    break
        for i, group in enumerate(buckets):
            if group:
                columns[i].lines.append(Line(y=ln.y, spans=group))

    for col in columns:
        col.lines.sort(key=lambda ln: ln.y)
    return [c for c in columns if c.lines]


def analyse_page(page: Page) -> PageLayout:
    lines = build_lines(page)
    sizes = [s.size for ln in lines for s in ln.spans if s.size > 0]
    body_size = statistics.median(sizes) if sizes else 10.0
    gutters = detect_gutters(page, lines)
    columns = split_columns(lines, gutters)
    return PageLayout(page=page, lines=lines, columns=columns, gutters=gutters, body_size=body_size)


def naive_text(page: Page) -> str:
    """Text in raw content-stream order -- the lowest-common-denominator read."""
    out: List[str] = []
    prev: Optional[Span] = None
    for s in sorted((s for s in page.spans if not s.invisible), key=lambda s: s.order):
        if not s.text:
            continue
        if prev is not None:
            # New line whenever the baseline moves.
            if abs(s.y - prev.y) > max(0.5 * max(s.size, prev.size), 1.5):
                out.append("\n")
            elif s.x - prev.x1 > max(0.25 * max(s.size, prev.size), 0.8):
                out.append(" ")
        out.append(s.text)
        prev = s
    return _tidy("".join(out))


def layout_text(layout: PageLayout) -> str:
    """Column-aware text: each column read fully before moving right."""
    if not layout.multi_column:
        return _tidy("\n".join(ln.text for ln in layout.lines))
    return _tidy("\n\n".join(col.text for col in layout.columns))


def _tidy(text: str) -> str:
    lines = [ln.rstrip() for ln in text.split("\n")]
    out: List[str] = []
    blanks = 0
    for ln in lines:
        if not ln.strip():
            blanks += 1
            if blanks > 1:
                continue
        else:
            blanks = 0
        out.append(ln)
    return "\n".join(out).strip()
