"""Register every PDF in the input directory before anything is extracted.

The inventory is the pipeline's memory. It records what each file is, whether
it opens, what period it covers and what its content hashes to, so that a
later run can tell a new report from one it has already processed -- and,
importantly, from one JoPACC has quietly replaced under the same name.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import List, Optional

import pymupdf

from ..extraction.tokens import extract_tokens

log = logging.getLogger(__name__)

MONTHS = ["january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december"]

# OCR reads the digit 1 in 'Q1' as a capital I often enough that matching only
# on digits loses a quarter of the quarterly reports.
QUARTER_RE = re.compile(r"\bQ\s*([1-4iIlL])\b")
YEAR_RE = re.compile(r"\b(20\d{2})\b")
_Q_FIX = {"i": "1", "l": "1", "1": "1", "2": "2", "3": "3", "4": "4"}


@dataclass
class PdfRecord:
    path: str
    filename: str
    size_bytes: int
    sha256: str
    modified_utc: str
    opens: bool = True
    page_count: int = 0
    pdf_created: str = ""
    pdf_modified: str = ""
    producer: str = ""
    canvas_w_pt: float = 0.0
    canvas_h_pt: float = 0.0
    report_type: Optional[str] = None
    report_type_source: str = ""
    period_key: Optional[str] = None
    period_source: str = ""
    filename_type: Optional[str] = None
    filename_period: Optional[str] = None
    in_scope: Optional[bool] = None
    text_pages: int = 0
    outlined_pages: int = 0
    cover_text: str = ""
    issues: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def _pdf_date(value: str) -> str:
    """Convert a PDF ``D:YYYYMMDDHHmmSS`` timestamp to ISO form."""
    if not value or not value.startswith("D:"):
        return ""
    raw = value[2:16]
    try:
        return dt.datetime.strptime(raw, "%Y%m%d%H%M%S").isoformat()
    except ValueError:
        return ""


def _classify_by_geometry(width: float, height: float, cfg: dict) -> Optional[str]:
    for name, spec in cfg["classification"]["page_geometry"].items():
        tol = spec["tolerance_pt"]
        if abs(width - spec["width_pt"]) <= tol and abs(height - spec["height_pt"]) <= tol:
            return name
    return None


def _period_from_text(text: str) -> tuple[Optional[str], Optional[str]]:
    """Read report type and period from cover text. Returns ``(type, period)``."""
    low = text.lower()
    year = YEAR_RE.search(text)
    quarter = QUARTER_RE.search(text)
    month = next((i + 1 for i, name in enumerate(MONTHS) if name in low), None)

    if month and year:
        return "monthly", f"{year.group(1)}-{month:02d}"
    if quarter and year:
        digit = _Q_FIX.get(quarter.group(1).lower())
        if digit:
            return "quarterly", f"{year.group(1)}-Q{digit}"
    if year:
        return "yearly", year.group(1)
    return None, None


def _period_from_filename(name: str) -> tuple[Optional[str], Optional[str]]:
    low = name.lower()
    year = YEAR_RE.search(low)
    if not year:
        return None, None
    if "quarterly" in low:
        q = re.search(r"_q([1-4])_", low)
        return "quarterly", f"{year.group(1)}-Q{q.group(1)}" if q else None
    if "monthly" in low:
        month = next((i + 1 for i, m in enumerate(MONTHS) if m in low), None)
        return "monthly", f"{year.group(1)}-{month:02d}" if month else None
    if "end_of_year" in low or "annual" in low:
        return "yearly", year.group(1)
    return None, None


def _period_in_scope(report_type: str, period: str, cfg: dict) -> Optional[bool]:
    """Does the period this report covers overlap the configured window?

    Scope is judged on the period the data describes, not on when the file was
    published, because publication dates change when JoPACC re-issues a file.
    """
    start = cfg["scope"]["window_start"]
    end = cfg["scope"]["window_end"]
    try:
        if report_type == "monthly":
            year, month = (int(p) for p in period.split("-"))
            first = dt.date(year, month, 1)
            last = dt.date(year + month // 12, month % 12 + 1, 1) - dt.timedelta(days=1)
        elif report_type == "quarterly":
            year, q = period.split("-Q")
            year, q = int(year), int(q)
            first = dt.date(year, 3 * (q - 1) + 1, 1)
            last = dt.date(year + (3 * q) // 12, (3 * q) % 12 + 1, 1) - dt.timedelta(days=1)
        else:
            year = int(period)
            first, last = dt.date(year, 1, 1), dt.date(year, 12, 31)
    except (ValueError, AttributeError):
        return None
    return first <= end and last >= start


def register(path: Path, cfg: dict) -> PdfRecord:
    """Inspect one PDF and return its inventory record."""
    stat = path.stat()
    record = PdfRecord(
        path=str(path), filename=path.name, size_bytes=stat.st_size,
        sha256=sha256_of(path),
        modified_utc=dt.datetime.utcfromtimestamp(stat.st_mtime).isoformat(),
    )

    try:
        doc = pymupdf.open(path)
    except Exception as exc:                       # corrupt or not a PDF
        record.opens = False
        record.issues.append(f"cannot open: {exc}")
        log.error("%s could not be opened: %s", path.name, exc)
        return record

    meta = doc.metadata or {}
    record.page_count = doc.page_count
    record.pdf_created = _pdf_date(meta.get("creationDate", ""))
    record.pdf_modified = _pdf_date(meta.get("modDate", ""))
    record.producer = (meta.get("producer") or "")[:80]

    if doc.page_count == 0:
        record.issues.append("PDF contains no pages")
        doc.close()
        return record

    first = doc[0]
    record.canvas_w_pt = round(first.rect.width, 1)
    record.canvas_h_pt = round(first.rect.height, 1)

    min_chars = cfg["text_layer"]["min_chars_per_page"]
    for page in doc:
        if len(page.get_text().strip()) >= min_chars:
            record.text_pages += 1
        else:
            record.outlined_pages += 1

    tokens, _ = extract_tokens(first, cfg)
    record.cover_text = " ".join(t.text for t in tokens)[:300]
    type_text, period_text = _period_from_text(record.cover_text)
    type_geom = _classify_by_geometry(record.canvas_w_pt, record.canvas_h_pt, cfg)
    type_name, period_name = _period_from_filename(path.name)
    record.filename_type, record.filename_period = type_name, period_name

    # Geometry is the more reliable type signal; cover text supplies the period.
    record.report_type = type_geom or type_text
    record.report_type_source = "canvas geometry" if type_geom else "cover text"
    record.period_key = period_text or period_name
    record.period_source = "cover text" if period_text else "filename"

    if not cfg["classification"]["trust_filename"]:
        if type_name and record.report_type and type_name != record.report_type:
            record.issues.append(
                f"type disagreement: filename says {type_name}, PDF says {record.report_type}")
        if period_name and period_text and period_name != period_text:
            record.issues.append(
                f"period disagreement: filename says {period_name}, cover says {period_text}")

    if record.period_key is None:
        record.issues.append("reporting period could not be determined")
    elif record.report_type:
        record.in_scope = _period_in_scope(record.report_type, record.period_key, cfg)

    doc.close()
    return record


def discover(cfg: dict, root: Optional[Path] = None) -> List[PdfRecord]:
    """Walk the input directory and register every PDF found."""
    base = Path(root or cfg["paths"]["input_dir"])
    files = sorted(p for p in base.rglob("*.pdf") if p.is_file())
    log.info("discovered %d PDF(s) under %s", len(files), base)
    return [register(p, cfg) for p in files]


def detect_duplicates(records: List[PdfRecord]) -> dict:
    """Group records by content hash and by reporting period."""
    by_hash: dict = {}
    by_period: dict = {}
    for rec in records:
        by_hash.setdefault(rec.sha256, []).append(rec.filename)
        if rec.report_type and rec.period_key:
            by_period.setdefault((rec.report_type, rec.period_key), []).append(rec.filename)
    return {
        "identical_files": {k: v for k, v in by_hash.items() if len(v) > 1},
        "period_collisions": {f"{k[0]} {k[1]}": v for k, v in by_period.items() if len(v) > 1},
    }


def missing_periods(records: List[PdfRecord], cfg: dict) -> dict:
    """Which periods inside the window have no report?

    Periods that could not have been published yet are not reported as gaps:
    a month still running, or a quarter that has not closed, is simply absent.
    """
    today = dt.date.today()
    have = {(r.report_type, r.period_key) for r in records if r.in_scope}
    start, end = cfg["scope"]["window_start"], cfg["scope"]["window_end"]
    gaps: dict = {"monthly": [], "quarterly": [], "yearly": []}

    cursor = dt.date(start.year, start.month, 1)
    while cursor <= end:
        key = f"{cursor.year}-{cursor.month:02d}"
        closed = dt.date(cursor.year + cursor.month // 12, cursor.month % 12 + 1, 1)
        if closed <= today and ("monthly", key) not in have:
            gaps["monthly"].append(key)
        cursor = closed

    for year in range(start.year, end.year + 1):
        for q in range(1, 5):
            key = f"{year}-Q{q}"
            closed = dt.date(year + (3 * q) // 12, (3 * q) % 12 + 1, 1)
            if start <= closed <= end and closed <= today and ("quarterly", key) not in have:
                gaps["quarterly"].append(key)
        if dt.date(year, 12, 31) <= end and ("yearly", str(year)) not in have:
            if dt.date(year + 1, 4, 1) <= today:      # annual reports land in Q1
                gaps["yearly"].append(str(year))
    return gaps


def save(records: List[PdfRecord], destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps([r.as_dict() for r in records], indent=1))


def load(destination: Path) -> List[PdfRecord]:
    if not destination.exists():
        return []
    return [PdfRecord(**row) for row in json.loads(destination.read_text())]
