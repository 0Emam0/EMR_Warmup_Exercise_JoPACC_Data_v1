"""Parse the numeric literals JoPACC uses in its reports.

The reports write values in a small number of recurring shapes:

    ``1.81 M``  ``162.18M``  ``2.43B JOD``  ``98.6K``  ``5.3%``
    ``2,601``   ``743``      ``118.9``      ``JOD 19.97 B``

Every parse keeps the raw literal alongside the normalised number so that a
reviewer can always see what was on the page. Nothing here guesses: a literal
that does not match a known shape returns ``None`` and is reported as
unparsed rather than silently dropped.
"""

from __future__ import annotations

import re
from decimal import Decimal
from dataclasses import dataclass, asdict
from typing import Optional

SCALE = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000, "BN": 1_000_000_000}

# 1.81 M / 162.18M / 98.6K / 2,601 / 118.9 / 5.3%
_NUM = r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
_PATTERNS = [
    # 2.43B JOD, 19.97 B JOD, 5.71 B JOD
    re.compile(rf"^{_NUM}\s*(?P<scale>[KMB])\s*(?P<cur>JOD)$", re.I),
    # JOD 19.97B
    re.compile(rf"^(?P<cur>JOD)\s*{_NUM}\s*(?P<scale>[KMB])?$", re.I),
    # 130 JOD -- a currency amount written without a scale suffix
    re.compile(rf"^{_NUM}\s*(?P<cur>JOD)$", re.I),
    # 5.3%
    re.compile(rf"^{_NUM}\s*(?P<pct>%)$"),
    # 1.81 M, 162.18M, 98.6K
    re.compile(rf"^{_NUM}\s*(?P<scale>[KMB])$", re.I),
    # 2,601 / 118.9 / 743
    re.compile(rf"^{_NUM}$"),
]

# Characters Tesseract commonly substitutes inside otherwise-numeric literals.
_OCR_FIXES = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "S": "5", "—": "-", "–": "-"})


@dataclass
class ParsedValue:
    raw: str
    value: Optional[float]
    unit: str                # 'count' | 'JOD' | 'percent' | 'unknown'
    scale_suffix: str        # '', 'K', 'M', 'B'
    ocr_repaired: bool
    parse_ok: bool
    note: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def _clean(text: str) -> str:
    t = text.strip()
    t = t.replace("\u00a0", " ").replace("\u2009", " ")
    t = re.sub(r"\s+", " ", t)
    # JOD is often set as a subscript and arrives glued on: '16.64BJOD'
    t = re.sub(r"(?i)([KMB])(JOD)$", r"\1 \2", t)
    t = re.sub(r"(?i)^(JOD)(\d)", r"\1 \2", t)
    return t


def _attempt(text: str) -> Optional[dict]:
    for pat in _PATTERNS:
        m = pat.match(text)
        if not m:
            continue
        g = m.groupdict()
        dec = Decimal(g["num"].replace(",", ""))
        scale = (g.get("scale") or "").upper()
        if scale:
            dec *= Decimal(SCALE[scale])
        num = float(dec.normalize())
        if g.get("pct"):
            unit = "percent"
        elif g.get("cur"):
            unit = "JOD"
        else:
            unit = "count"
        return {"value": num, "unit": unit, "scale_suffix": scale}
    return None


def parse_value(text: str, allow_ocr_repair: bool = True) -> ParsedValue:
    """Parse one on-page literal into a normalised value.

    ``allow_ocr_repair`` permits a second attempt with common OCR character
    substitutions undone. The repair is always reported so that a reviewer can
    filter on it.
    """
    raw = text
    t = _clean(text)
    if not t:
        return ParsedValue(raw, None, "unknown", "", False, False, "empty")

    hit = _attempt(t)
    if hit:
        return ParsedValue(raw, hit["value"], hit["unit"], hit["scale_suffix"], False, True)

    if allow_ocr_repair and re.search(r"\d", t):
        repaired = t.translate(_OCR_FIXES)
        if repaired != t:
            hit = _attempt(repaired)
            if hit:
                return ParsedValue(
                    raw, hit["value"], hit["unit"], hit["scale_suffix"], True, True,
                    f"repaired to {repaired!r}",
                )

    return ParsedValue(raw, None, "unknown", "", False, False, "no pattern matched")


_NUMERIC_HINT = re.compile(r"\d")


def looks_numeric(text: str) -> bool:
    """Cheap pre-filter: does this token plausibly carry a value?"""
    t = _clean(text)
    if not _NUMERIC_HINT.search(t):
        return False
    # Page numbers, footer stamps like '08.2026' and year labels are handled
    # by the caller with positional context, not excluded here.
    return True


RANGE_LABEL = re.compile(r"^(?:<\s*\d+|>\s*\d+|\d+\s*-\s*\d+|\d+\+)$")


def looks_like_range_label(text: str) -> bool:
    """Age bands and similar bucket labels ('<18', '18-30', '90+').

    These carry digits but are dimensions, not measurements, and treating them
    as unparsed values would bury the genuine misses.
    """
    return bool(RANGE_LABEL.match(_clean(text)))


_PERIOD_LABEL = re.compile(
    r"^(?:(?P<mon>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[- ](?P<yy>\d{2})"
    r"|Q(?P<q>[1-4])\s*(?P<qy>20\d{2})"
    r"|(?P<yyyy>20\d{2}))$",
    re.I,
)
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def parse_period_label(text: str) -> Optional[str]:
    """Normalise an axis label ('Apr-25', 'Q1 2026', '2024') to an ISO-ish key."""
    m = _PERIOD_LABEL.match(_clean(text))
    if not m:
        return None
    g = m.groupdict()
    if g.get("mon"):
        return f"20{g['yy']}-{_MONTHS.index(g['mon'].lower()) + 1:02d}"
    if g.get("q"):
        return f"{g['qy']}-Q{g['q']}"
    return g["yyyy"]
