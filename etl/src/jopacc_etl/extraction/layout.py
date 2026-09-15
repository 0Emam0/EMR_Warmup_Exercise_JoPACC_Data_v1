"""Recover structure from a bag of positioned tokens.

JoPACC's reports are designed documents, not tables. A page carries a title,
sometimes a subtitle, and one or more chart panels; the numbers sit inside or
beside the bars they describe. None of that survives as markup, so structure
has to be inferred from geometry.

The three inferences made here, in order:

* **lines** -- tokens sharing a baseline are one phrase
* **panels** -- horizontal whitespace bands split the page into chart regions
* **series** -- within a panel, a value belongs to the axis label nearest it
  in x

Every inference is deliberately conservative. Where a value cannot be tied to
a label, it is still emitted, with an empty label and a note saying so, rather
than attached to a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from .tokens import Token
from ..transform.numbers import parse_period_label, parse_value

FOOTER_STAMP = re.compile(r"^(0[1-9]|1[0-2])\.20\d{2}$")
PAGE_NUMBER = re.compile(r"^\d{1,3}$")
# Bullet and tick glyphs the source decks leave behind as single characters.
# They carry no meaning but sit between a value and its unit, which breaks the
# value/unit fusion below if they are left in place.
DECORATIVE = re.compile(r"^(?:[A-Za-z]|[^\w\s])$")
# K, M and B are single characters too, but they are scale suffixes, not
# ornament, and dropping them turns 1.74 M into a count of one point seven four.
# The percent sign is often its own text run, sitting beside the figure it
# qualifies. Dropping it as ornament silently turns 81.3% into a count of 81.3.
SCALE_CHARS = {"K", "M", "B", "%"}
# A hyphen is often its own text run inside a compound word -- 'Non-Jordanian'
# sometimes arrives as three separate runs. Dropping the middle one as a stray
# character does not just lose a hyphen: it silently turns the category into
# a different, wrong one ('Jordanian') rather than an incomplete one. It is
# kept here and reglued to its neighbours in _make_line.
SCALE_CHARS |= {"-", "\u2013", "\u2014"}
_HYPHENS = {"-", "\u2013", "\u2014"}
BARE_INTEGER = re.compile(r"^\d{1,4}$")


def drop_decorative(tokens: List[Token]) -> tuple[List[Token], List[Token]]:
    keep, dropped = [], []
    for tok in tokens:
        ornament = DECORATIVE.match(tok.text) and tok.text not in SCALE_CHARS
        (dropped if ornament else keep).append(tok)
    return keep, dropped


def _is_prose_number(token: Token, line: "Line") -> bool:
    """Is this numeral part of a sentence rather than a reported figure?

    Titles carry numerals -- ``Over the Past 3 Months``, ``Since the Beginning
    of 2026`` -- that parse perfectly well and are not measurements. A bare
    integer with words on both sides of it is prose; a figure quoted at the end
    of a label, as in ``Number of Billers 743``, is not.
    """
    if not BARE_INTEGER.match(token.text.strip()):
        return False
    words = [t for t in line.tokens if t.text.isalpha()]
    if len(words) < 4:
        return False
    # A trailing year in a heading -- 'Since the Beginning of 2026' -- has no
    # word after it, so requiring words on both sides would let it through.
    return any(t.x1 <= token.x0 and t.text.isalpha() for t in line.tokens)


@dataclass
class Line:
    text: str
    tokens: List[Token]
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def height(self) -> float:
        return self.y1 - self.y0


@dataclass
class Panel:
    index: int
    y0: float
    y1: float
    lines: List[Line] = field(default_factory=list)
    title: str = ""

    @property
    def text(self) -> str:
        return " | ".join(line.text for line in self.lines)


def split_orientation(tokens: List[Token]) -> tuple[List[Token], List[Token]]:
    """Separate upright tokens from rotated ones.

    A sideways ``Q1 2026`` shares a baseline with nothing; folding it into a
    horizontal line would corrupt that line's text and its geometry.
    """
    upright = [t for t in tokens if t.orientation != "v"]
    rotated = [t for t in tokens if t.orientation == "v"]
    return upright, rotated


def build_lines(tokens: List[Token], cfg: dict) -> List[Line]:
    """Group tokens into lines by shared baseline, then order left to right."""
    if not tokens:
        return []
    ratio = cfg["layout"]["line_tolerance_ratio"]
    remaining = sorted(tokens, key=lambda t: (t.y0, t.x0))
    lines: List[Line] = []
    current: List[Token] = [remaining[0]]

    for tok in remaining[1:]:
        ref = current[-1]
        tol = max(ref.height, tok.height) * ratio
        if abs(tok.cy - ref.cy) <= tol:
            current.append(tok)
        else:
            lines.append(_make_line(current))
            current = [tok]
    lines.append(_make_line(current))
    return lines


_SUFFIX = re.compile(r"^(?:[KMB]|JOD|%|BJOD|MJOD|KJOD)$", re.I)


def _fuse_units(tokens: List[Token], gap_limit: float = 1.4) -> List[Token]:
    """Glue a value to the unit token that follows it.

    The reports set the scale and currency as separate runs -- ``1.81`` then
    ``M``, ``16.64B`` then ``JOD`` -- and both the text layer and OCR hand
    them back as separate tokens. Left apart, ``1.81`` would be loaded as a
    count of one point eight one.
    """
    ordered = sorted(tokens, key=lambda t: t.x0)
    fused: List[Token] = []
    for tok in ordered:
        if fused and _SUFFIX.match(tok.text) and re.search(r"\d", fused[-1].text):
            prev = fused[-1]
            gap = tok.x0 - prev.x1
            if gap <= prev.height * gap_limit:
                joiner = "" if tok.text in {"%"} else " "
                fused[-1] = Token(
                    f"{prev.text}{joiner}{tok.text}", prev.x0, min(prev.y0, tok.y0),
                    tok.x1, max(prev.y1, tok.y1), min(prev.conf, tok.conf),
                    prev.method, prev.layer, list(prev.alternatives),
                )
                continue
        fused.append(tok)
    return fused


def _reglue_hyphens(tokens: List[Token]) -> List[Token]:
    """Merge a standalone hyphen back into the words on either side of it.

    Where 'Non-Jordanian' arrives as three separate runs -- a font or kerning
    boundary the source deck introduces, not a real word break -- this
    reassembles them into one token before anything downstream (label
    matching, in particular) has to treat 'Non' and 'Jordanian' as two
    unrelated words.

    The neighbour is found by the closest actual gap to an alphabetic token,
    not by position in an x-sorted list: on a ring chart the category text and
    its value can share almost the same x-range, so a numeral can legitimately
    sit between a hyphen and the word it belongs to.
    """
    hyphens = [t for t in tokens if t.text in _HYPHENS]
    if not hyphens:
        return tokens
    words = [t for t in tokens if re.fullmatch(r"[A-Za-z][A-Za-z'.]*", t.text)]
    consumed = set()
    merges: dict = {}

    for hy in hyphens:
        left = min(
            (w for w in words if id(w) not in consumed and w.x1 <= hy.x0 + 0.5
             and abs(w.cy - hy.cy) < hy.height),
            key=lambda w: hy.x0 - w.x1, default=None)
        right = min(
            (w for w in words if id(w) not in consumed and w.x0 >= hy.x1 - 0.5
             and abs(w.cy - hy.cy) < hy.height),
            key=lambda w: w.x0 - hy.x1, default=None)
        gap_ok = lambda a, b: -1.0 <= b - a <= hy.height * 0.5
        if left and right and gap_ok(left.x1, hy.x0) and gap_ok(hy.x1, right.x0):
            merges[id(hy)] = (left, right)
            consumed.update({id(left), id(right)})

    if not merges:
        return tokens

    out: List[Token] = []
    skip = {id(w) for pair in merges.values() for w in pair}
    for tok in sorted(tokens, key=lambda t: t.x0):
        if id(tok) in merges:
            left, right = merges[id(tok)]
            out.append(Token(
                f"{left.text}-{right.text}", left.x0, min(left.y0, right.y0),
                right.x1, max(left.y1, right.y1),
                min(left.conf, tok.conf, right.conf),
                left.method, left.layer, left.orientation,
                list(left.alternatives)))
        elif id(tok) in skip:
            continue
        else:
            out.append(tok)
    return out


def _make_line(tokens: List[Token]) -> Line:
    ordered = _fuse_units(_reglue_hyphens(tokens))
    return Line(
        text=" ".join(t.text for t in ordered),
        tokens=ordered,
        x0=min(t.x0 for t in ordered),
        y0=min(t.y0 for t in ordered),
        x1=max(t.x1 for t in ordered),
        y1=max(t.y1 for t in ordered),
    )


def strip_furniture(lines: List[Line], page_height: float, page_width: float,
                    cfg: dict) -> tuple[List[Line], List[Line]]:
    """Separate page furniture (page number, date stamp) from content.

    Removing these matters: the footer stamp '08.2026' parses cleanly as a
    number and would otherwise enter the fact table as a measurement.
    """
    band = page_height * cfg["layout"]["footer_band_ratio"]
    content, furniture = [], []
    for line in lines:
        text = line.text.strip()
        is_footer = line.y0 >= band and (FOOTER_STAMP.match(text) or PAGE_NUMBER.match(text))
        if is_footer or FOOTER_STAMP.match(text):
            furniture.append(line)
        else:
            content.append(line)
    return content, furniture


def split_panels(lines: List[Line], page_height: float, cfg: dict) -> List[Panel]:
    """Cut the page at vertical whitespace gaps into chart panels.

    A monthly slide is usually one panel. A quarterly poster page carries six
    or more, which is exactly why this step exists: without it every value on
    the page would be matched against every label on the page.
    """
    if not lines:
        return []
    gap = page_height * cfg["layout"]["panel_gap_ratio"]
    ordered = sorted(lines, key=lambda line: line.y0)
    panels: List[Panel] = []
    bucket: List[Line] = [ordered[0]]

    for line in ordered[1:]:
        if line.y0 - bucket[-1].y1 > gap:
            panels.append(_make_panel(len(panels), bucket))
            bucket = [line]
        else:
            bucket.append(line)
    panels.append(_make_panel(len(panels), bucket))
    return panels


def _make_panel(index: int, lines: List[Line]) -> Panel:
    panel = Panel(index=index, y0=min(l.y0 for l in lines), y1=max(l.y1 for l in lines),
                  lines=lines)
    panel.title = _panel_title(lines)
    return panel


def _panel_title(lines: List[Line]) -> str:
    """The title is the topmost run of lines that carry no numeric value."""
    parts = []
    for line in lines:
        if any(parse_value(t.text).parse_ok for t in line.tokens):
            break
        parts.append(line.text)
    return " ".join(parts).strip()


@dataclass
class Measurement:
    raw_text: str
    value: Optional[float]
    unit: str
    scale_suffix: str
    label: str            # the axis/category label this value sits under
    label_period: Optional[str]
    metric: str            # what this value measures, e.g. 'Number of Users'
    metric_source: str     # 'inline' | 'axis' | 'column_caption' | 'page_title' | ''
    panel_index: int
    panel_title: str
    x: float
    y: float
    conf: float
    method: str
    layer: str
    ocr_repaired: bool
    ocr_alternatives: str
    note: str


def _axis_candidates(panel: Panel) -> List[Token]:
    """Tokens that read as a period label -- the x axis of a chart panel."""
    out = []
    for line in panel.lines:
        if parse_period_label(line.text):
            out.append(_as_token(line))
        else:
            for tok in line.tokens:
                if parse_period_label(tok.text):
                    out.append(tok)
    return out


def _as_token(line: Line) -> Token:
    first = line.tokens[0]
    return Token(line.text, line.x0, line.y0, line.x1, line.y1,
                 min(t.conf for t in line.tokens), first.method, first.layer)


def _label_phrases(panel: Panel, max_chars: int = 60) -> List[Token]:
    """Contiguous runs of non-numeric tokens, which is how categories appear.

    JoPACC sets a category and its value on the same baseline -- a line reads
    ``95.9% Jordanian 2.29M``. Treating whole lines as labels would therefore
    find nothing here, so runs are collected inside each line as well.
    """
    phrases: List[Token] = []
    for line in panel.lines:
        if parse_period_label(line.text):
            continue
        run: List[Token] = []
        for tok in line.tokens:
            breaks_run = parse_value(tok.text).parse_ok or parse_period_label(tok.text)
            # A wide horizontal gap means a different column, not a longer
            # phrase. Without this, five side-by-side KPI captions merge into
            # one nonsense label.
            if run and not breaks_run:
                gap = tok.x0 - run[-1].x1
                if gap > max(run[-1].height, tok.height) * 1.6:
                    phrases.append(_join_run(run))
                    run = []
            if breaks_run:
                if run:
                    phrases.append(_join_run(run))
                    run = []
            else:
                run.append(tok)
        if run:
            phrases.append(_join_run(run))
    return [p for p in phrases if 0 < len(p.text) <= max_chars]


def _join_run(tokens: List[Token]) -> Token:
    text = " ".join(t.text for t in tokens)
    text = re.sub(r"\s+-", "-", text).strip()
    first = tokens[0]
    return Token(text, min(t.x0 for t in tokens), min(t.y0 for t in tokens),
                 max(t.x1 for t in tokens), max(t.y1 for t in tokens),
                 min(t.conf for t in tokens), first.method, first.layer)


def running_panel_titles(panels: List[Panel]) -> dict:
    """The title in force for each panel, carried forward from the last one
    that had no values of its own.

    A monthly slide has one title above one chart, and a single page-wide
    title would be enough. The quarterly poster stacks several independent
    title-then-chart groups down one long page, so the title has to be
    tracked as it changes rather than taken once from the top of the page --
    otherwise every section below the first inherits the first section's
    name.
    """
    current = ""
    out = {}
    for panel in panels:
        has_values = any(
            parse_value(t.text).parse_ok and not _is_prose_number(t, line)
            for line in panel.lines for t in line.tokens)
        if not has_values:
            if panel.text:
                current = panel.text
            out[panel.index] = ""
        else:
            out[panel.index] = current
    return out


def page_title_from_panels(panels: List[Panel]) -> str:
    """The slide's headline, when the page opens with pure-text panels.

    JoPACC pages are usually one title (and often a subtitle) followed by the
    chart itself, with enough vertical whitespace between them that panel
    splitting treats the title as its own, valueless panel. That whitespace
    is real page layout, not a sign the title belongs to nothing -- so it is
    captured here and offered as a fallback name for every panel that follows,
    for pages where no panel of its own carries a caption.
    """
    parts = []
    for panel in panels:
        real_values = [
            tok for line in panel.lines for tok in line.tokens
            if parse_value(tok.text).parse_ok and not _is_prose_number(tok, line)
        ]
        if real_values:
            break
        parts.append(panel.text)
    return " | ".join(p for p in parts if p)


def _column_captions(panel: Panel, value_tokens: List[Token], page_width: float,
                     cfg: dict) -> dict:
    """Match each value to the caption stacked above it, by column.

    Some pages set several KPIs side by side with their captions merged across
    two shared lines -- ``Number of Number of ... Value of`` above
    ``Users Billers Services ... Transactions`` above the five values
    themselves. Whole-line grouping cannot separate these; individual token
    x-positions can; every value is stacked in its own vertical band, and the
    caption words in that band belong to it. Each caption token is assigned to
    the nearest value token below it in the same column, and the tokens
    sharing a value are joined in reading order.
    """
    tol = cfg["layout"].get("column_caption_ratio", 0.09) * page_width
    caption_tokens = [
        tok for line in panel.lines for tok in line.tokens
        if not parse_value(tok.text).parse_ok and not parse_period_label(tok.text)
    ]
    groups: dict = {id(v): [] for v in value_tokens}
    for cap in caption_tokens:
        below = [v for v in value_tokens if v.y0 >= cap.y1]
        if not below:
            continue
        nearest = min(below, key=lambda v: abs(v.cx - cap.cx))
        if abs(nearest.cx - cap.cx) <= tol:
            groups[id(nearest)].append(cap)

    out = {}
    for value in value_tokens:
        toks = sorted(groups[id(value)], key=lambda t: (t.y0, t.x0))
        if toks:
            out[id(value)] = " ".join(t.text for t in toks)
    return out


def page_axis_labels(panels: List[Panel]) -> List[Token]:
    """Collect period labels from the whole page.

    A chart's axis sits in the whitespace below its plot area, which the panel
    split treats as a separate region. Matching axes per panel would therefore
    never connect a bar to the month underneath it, so candidates are gathered
    page-wide and constrained by geometry instead.
    """
    out: List[Token] = []
    for panel in panels:
        out += _axis_candidates(panel)
    return out


_METRIC_WORD = re.compile(
    r"(?i)\b(number|value|total|average|percentage|count|amount|balance)\b")


def _is_metric_caption(text: str) -> bool:
    """Does this phrase name a metric ('Number of Transactions') rather than
    a dimension value ('Jordanian', 'Money Transfer', 'On Us')?

    The reports are formulaic enough that this holds up well: every metric
    name in the corpus is built from 'Number of', 'Value of', 'Total',
    'Average' or 'Percentage'; every category value the pipeline has to
    distinguish from one is a bare noun or noun phrase that uses none of
    them.
    """
    return bool(_METRIC_WORD.search(text))


def measure_panel(panel: Panel, page_width: float, cfg: dict,
                  axis: Optional[List[Token]] = None,
                  rotated_axis: Optional[List[Token]] = None,
                  running_title: str = "") -> List[Measurement]:
    """Emit one Measurement per numeric token in the panel.

    Two separate things are recovered for each value: ``label``, the
    dimension it breaks out by (a period, a category like 'Jordanian'), and
    ``metric``, what is actually being measured ('Number of Transactions').
    Collapsing them into one field is what made the raw output unreadable --
    a metric name and a category value look identical once extracted, and
    without separating them a reader cannot tell 'Money Transfer' the category
    from 'Number of Transactions' the metric.
    """
    axis = _axis_candidates(panel) if axis is None else axis
    rotated_axis = rotated_axis or []
    phrases = _label_phrases(panel)
    tol = page_width * cfg["layout"]["axis_match_ratio"]
    out: List[Measurement] = []

    value_tokens = [
        tok for line in panel.lines for tok in line.tokens
        if parse_value(tok.text).parse_ok and not _is_prose_number(tok, line)
    ]
    column_captions = _column_captions(panel, value_tokens, page_width, cfg)
    fallback_metric = panel.title if _is_metric_caption(panel.title) else ""
    if not fallback_metric:
        fallback_metric = panel.title or running_title

    for line in panel.lines:
        same_line = [p for p in phrases if abs(p.cy - line.cy) <= max(line.height, 1.0)]
        for tok in line.tokens:
            parsed = parse_value(tok.text)
            if not parsed.parse_ok or _is_prose_number(tok, line):
                continue
            label, period, note = "", None, ""
            metric, metric_source = "", ""

            near_row = _nearest_y(tok, rotated_axis, tok.height * 2.5)
            near_axis = _nearest_x(tok, [a for a in axis if a.y0 > tok.y0], tol)
            near_inline = _nearest(tok, same_line, page_width * 0.30)
            column_caption = column_captions.get(id(tok), "")

            if near_row is not None:
                label, period = near_row.text, parse_period_label(near_row.text)
            elif near_axis is not None:
                label, period = near_axis.text, parse_period_label(near_axis.text)
            elif near_inline is not None:
                if _is_metric_caption(near_inline.text):
                    metric, metric_source = near_inline.text, "inline"
                else:
                    label = near_inline.text

            if not metric and column_caption and _is_metric_caption(column_caption):
                metric, metric_source = column_caption, "column_caption"
            if not metric and fallback_metric:
                metric = fallback_metric
                metric_source = "running_title" if fallback_metric == running_title else "panel_title"

            if not label and not metric_source:
                near_any = _nearest(tok, phrases, page_width * 0.25)
                if near_any is not None:
                    label = near_any.text
                    note = "label matched by proximity only"
            if not label and not metric:
                note = note or "value not associated with any label"

            out.append(Measurement(
                raw_text=tok.text, value=parsed.value, unit=parsed.unit,
                scale_suffix=parsed.scale_suffix, label=label, label_period=period,
                metric=metric, metric_source=metric_source,
                panel_index=panel.index, panel_title=panel.title,
                x=round(tok.cx, 2), y=round(tok.cy, 2), conf=tok.conf,
                method=tok.method, layer=tok.layer,
                ocr_repaired=parsed.ocr_repaired,
                ocr_alternatives=", ".join(tok.alternatives), note=note,
            ))
    return out


def _nearest_x(tok: Token, candidates: List[Token], tol: float) -> Optional[Token]:
    """Axis labels sit directly below their value, so match on x alone."""
    best, best_dx = None, tol
    for cand in candidates:
        dx = abs(cand.cx - tok.cx)
        if dx < best_dx:
            best, best_dx = cand, dx
    return best


def _nearest_y(tok: Token, candidates: List[Token], tol: float) -> Optional[Token]:
    """Rotated labels sit level with their bar, so match on y alone."""
    best, best_dy = None, tol
    for cand in candidates:
        dy = abs(cand.cy - tok.cy)
        if dy < best_dy:
            best, best_dy = cand, dy
    return best


def _nearest(tok: Token, candidates: List[Token], tol: float) -> Optional[Token]:
    best, best_d = None, tol
    for cand in candidates:
        d = ((cand.cx - tok.cx) ** 2 + (cand.cy - tok.cy) ** 2) ** 0.5
        if d < best_d:
            best, best_d = cand, d
    return best
