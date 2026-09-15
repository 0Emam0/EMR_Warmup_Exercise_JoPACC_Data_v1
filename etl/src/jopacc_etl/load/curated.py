"""Turn the raw, page-level measurements into a dataset built for reanalysis.

The `Measurements` sheet each workbook already carries is an audit trail: one
row per printed number, with its page, its panel, and how confident the
reader was. That is the right shape for checking a figure against the PDF. It
is the wrong shape for pivoting, charting, or running a time series, because
nothing on a raw row says plainly what it measures versus what it is broken
out by, and because every report sits in its own file.

This module produces the other half: one row per figure, with a clean metric
name, a clean dimension, and a period -- filtered so that page furniture
(cover pages, table-of-contents cross-references, section-divider page
numbers) never enters it. It also merges curated rows across every processed
report into one long-format table, because a dataset spread across
seventeen files is not a dataset a person can pivot.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List

# Metric text that names page furniture, not a figure -- a divider slide's
# running header, the table of contents, the cover. These carry stray
# numbers (page cross-references, the report month and year, a page number)
# that parse cleanly as values and would otherwise enter the dataset as
# meaningless rows: 'Table of Content' -> 25, 'Monthly Report' -> 2026.
_BOILERPLATE_METRICS = {
    "monthly report", "table of content", "quarterly report",
    "end of year systems report", "systems summary",
}


def _is_boilerplate(metric: str) -> bool:
    flat = re.sub(r"\s+", " ", metric).strip().lower()
    if not flat:
        return False
    if flat in _BOILERPLATE_METRICS:
        return True
    # A metric string built entirely from page furniture words and nothing
    # else -- 'Monthly Report August 2026', '3 Systems Summary Monthly
    # Report' -- names the page, not a figure on it.
    stripped = re.sub(
        r"\b(monthly|quarterly|annual|report|summary|systems|end|of|year"
        r"|january|february|march|april|may|june|july|august|september"
        r"|october|november|december|\d+)\b", "", flat)
    return not stripped.strip()


def _clean_metric(metric: str) -> str:
    """Collapse the running-title artefacts ('Users | Total Number of...')
    and repeated whitespace left over from panel/page-title extraction."""
    flat = metric.replace("|", " ").strip()
    return re.sub(r"\s+", " ", flat)


def curate_measurements(measurements: List[dict], record) -> List[dict]:
    """Filter and reshape one report's raw measurements into analysis rows.

    Dropped, with the reason kept only in code comments rather than the data
    (the raw sheet is the audit trail for that): front-matter/cover pages,
    boilerplate-only metrics, and the handful of values tied to neither a
    metric nor a dimension label -- the ones the validation sheet already
    flags as `value_unidentified`.
    """
    out = []
    for row in measurements:
        if row["section_key"] == "front_matter":
            continue
        metric = _clean_metric(row.get("metric") or "")
        if _is_boilerplate(metric):
            continue
        label = (row.get("label") or "").strip()
        if not metric and not label:
            continue
        if row["value_numeric"] is None:
            continue

        out.append({
            "report_type": row["report_type"],
            "reporting_period": row["reporting_period"],
            "system": row["section_key"] or "",
            "system_title": row["section_title"] or "",
            "metric": metric,
            "dimension": label,
            "dimension_period": row.get("label_period") or "",
            # the period this data point actually describes: the axis/row
            # period if the chart gave one, otherwise the report's own period
            "period": row.get("label_period") or row["reporting_period"],
            "value": row["value_numeric"],
            "unit": row["unit"],
            "raw_value": row["raw_value"],
            "confidence": row["confidence"],
            "extraction_method": row["extraction_method"],
            "flagged": bool(row.get("ocr_alternatives")) or bool(row.get("note")),
            "source_pdf": row["source_pdf"],
            "source_page": row["source_page"],
        })
    return out


CURATED_COLUMNS = [
    ("report_type", 12), ("reporting_period", 16), ("system", 16),
    ("system_title", 34), ("metric", 34), ("dimension", 22),
    ("dimension_period", 14), ("period", 14), ("value", 16), ("unit", 9),
    ("raw_value", 14), ("confidence", 11), ("extraction_method", 16),
    ("flagged", 9), ("source_pdf", 32), ("source_page", 12),
]


def write_master_dataset(all_curated_rows: List[dict], destination: Path) -> Path:
    """Combine curated rows from every processed report into one file.

    This is the file built for reanalysis: every period, every system, one
    row per metric-dimension-period combination, ready to pivot or load into
    pandas without first stitching together one workbook per report.
    """
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.table import Table, TableStyleInfo

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Dataset"
    headers = [c[0] for c in CURATED_COLUMNS]
    sheet.append(headers)
    for cell in sheet[1]:
        cell.fill = PatternFill("solid", fgColor="13293D")
        cell.font = Font(color="FFFFFF", bold=True, size=10)

    rows = sorted(all_curated_rows,
                 key=lambda r: (r["report_type"] or "", r["system"] or "",
                                r["metric"] or "", r["period"] or "",
                                r["dimension"] or ""))
    for row in rows:
        sheet.append([row.get(h) for h in headers])

    for index, (_, width) in enumerate(CURATED_COLUMNS, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.freeze_panes = "A2"
    if rows:
        ref = f"A1:{get_column_letter(len(headers))}{len(rows) + 1}"
        table = Table(displayName="Dataset", ref=ref)
        table.tableStyleInfo = TableStyleInfo(
            name="TableStyleLight1", showRowStripes=True)
        sheet.add_table(table)

    _write_coverage_sheet(book, rows)
    destination.parent.mkdir(parents=True, exist_ok=True)
    book.save(destination)
    return destination


def _write_coverage_sheet(book, rows: List[dict]) -> None:
    """One row per report, so a gap in the combined dataset is visible."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    sheet = book.create_sheet("Coverage")
    headers = ["report_type", "reporting_period", "rows", "systems", "metrics"]
    sheet.append(headers)
    for cell in sheet[1]:
        cell.fill = PatternFill("solid", fgColor="13293D")
        cell.font = Font(color="FFFFFF", bold=True, size=10)

    by_report: dict = {}
    for row in rows:
        key = (row["report_type"], row["reporting_period"])
        entry = by_report.setdefault(key, {"rows": 0, "systems": set(), "metrics": set()})
        entry["rows"] += 1
        entry["systems"].add(row["system"])
        entry["metrics"].add(row["metric"])

    for (rtype, period), entry in sorted(by_report.items()):
        sheet.append([rtype, period, entry["rows"],
                     len(entry["systems"]), len(entry["metrics"])])
    for index, width in enumerate([12, 16, 8, 10, 10], start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.freeze_panes = "A2"
