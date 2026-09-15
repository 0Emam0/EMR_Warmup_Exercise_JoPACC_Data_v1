"""Process one PDF end to end.

The unit of work is a single report, because the required output is one
workbook per report and because a failure should cost one file rather than the
run. Everything a reviewer needs to audit a figure -- the page it came from,
the section it sat in, how it was read and how confident the reader was --
is carried on the record itself.
"""

from __future__ import annotations

import copy
import datetime as dt
import logging
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

import pymupdf

from .discovery.inventory import PdfRecord
from .extraction import layout as L
from .extraction.sections import build_sections, section_for_page
from .extraction.tokens import extract_tokens
from .transform.numbers import looks_like_range_label, parse_period_label
from .validation import checks

log = logging.getLogger(__name__)


@dataclass
class ReportResult:
    record: PdfRecord
    measurements: List[dict] = field(default_factory=list)
    unparsed: List[dict] = field(default_factory=list)
    page_log: List[dict] = field(default_factory=list)
    sections: List = field(default_factory=list)
    findings: List[checks.Finding] = field(default_factory=list)
    status: str = "pending"
    error: str = ""

    @property
    def counts(self) -> dict:
        ok = [p for p in self.page_log if p["status"] == "ok"]
        return {
            "pages_total": len(self.page_log),
            "pages_ok": len(ok),
            "pages_failed": len(self.page_log) - len(ok),
            "pages_native": len([p for p in ok if p["method"] == "native"]),
            "pages_ocr": len([p for p in ok if p["method"] == "ocr"]),
            "measurements": len(self.measurements),
            "unparsed_tokens": len(self.unparsed),
        }


def _config_for(report_type: str, cfg: dict) -> dict:
    """Apply the per-report-type layout overrides."""
    tuned = copy.deepcopy(cfg)
    overrides = cfg["layout"].get("panel_gap_ratio_by_type", {})
    if report_type in overrides:
        tuned["layout"]["panel_gap_ratio"] = overrides[report_type]
    return tuned


def process(record: PdfRecord, cfg: dict) -> ReportResult:
    result = ReportResult(record=record)
    if not record.opens:
        result.status = "failed"
        result.error = "; ".join(record.issues)
        result.findings += checks.check_period_agreement(record)
        return result

    tuned = _config_for(record.report_type or "monthly", cfg)
    stamp = dt.datetime.utcnow().isoformat(timespec="seconds")

    try:
        doc = pymupdf.open(record.path)
    except Exception as exc:
        result.status = "failed"
        result.error = f"open failed: {exc}"
        return result

    page_texts: Dict[int, str] = {}
    per_page_tokens: Dict[int, tuple] = {}

    # Pass one: read every page. A page that raises is logged and the run
    # continues; it must not take the report down with it.
    for index in range(doc.page_count):
        page_no = index + 1
        try:
            page = doc[index]
            tokens, method = extract_tokens(page, tuned)
            kept, ornament = L.drop_decorative(tokens)
            upright, rotated = L.split_orientation(kept)
            per_page_tokens[page_no] = (page, upright, rotated, method)
            page_texts[page_no] = " ".join(t.text for t in upright)
            result.page_log.append({
                "page": page_no, "status": "ok", "method": method,
                "token_count": len(kept), "ornament_dropped": len(ornament),
                "rotated_tokens": len(rotated),
                "width_pt": round(page.rect.width, 1),
                "height_pt": round(page.rect.height, 1),
                "error": "",
            })
        except Exception as exc:
            log.error("%s page %d failed: %s", record.filename, page_no, exc)
            result.page_log.append({
                "page": page_no, "status": "failed", "method": "",
                "token_count": 0, "ornament_dropped": 0, "rotated_tokens": 0,
                "width_pt": 0, "height_pt": 0,
                "error": f"{type(exc).__name__}: {exc}",
            })

    sections, structure_warnings = build_sections(page_texts, doc.page_count, tuned)
    result.sections = sections
    result.findings += checks.check_structure(structure_warnings)

    # Pass two: turn tokens into measurements, now that sections are known.
    counter = 0
    for page_no, (page, upright, rotated, method) in sorted(per_page_tokens.items()):
        section = section_for_page(sections, page_no)
        rotated_labels = [t for t in rotated if parse_period_label(t.text)]
        try:
            lines = L.build_lines(upright, tuned)
            content, furniture = L.strip_furniture(
                lines, page.rect.height, page.rect.width, tuned)
            panels = L.split_panels(content, page.rect.height, tuned)
            axis = L.page_axis_labels(panels)
            running_titles = L.running_panel_titles(panels)

            page_measurements = []
            for panel in panels:
                page_measurements += L.measure_panel(
                    panel, page.rect.width, tuned, axis, rotated_labels,
                    running_titles.get(panel.index, ""))

            for measurement in page_measurements:
                counter += 1
                result.measurements.append({
                    "record_id": f"{record.period_key}-p{page_no:03d}-{counter:04d}",
                    "source_pdf": record.filename,
                    "report_type": record.report_type,
                    "reporting_period": record.period_key,
                    "source_page": page_no,
                    "section_key": section.key if section else "",
                    "section_title": section.title if section else "",
                    "panel_index": measurement.panel_index,
                    "panel_title": measurement.panel_title,
                    "metric": measurement.metric,
                    "metric_source": measurement.metric_source,
                    "label": measurement.label,
                    "label_period": measurement.label_period,
                    "raw_value": measurement.raw_text,
                    "value_numeric": measurement.value,
                    "unit": measurement.unit,
                    "scale_suffix": measurement.scale_suffix,
                    "extraction_method": measurement.method,
                    "ocr_layer": measurement.layer,
                    "confidence": round(measurement.conf, 1),
                    "ocr_repaired": measurement.ocr_repaired,
                    "ocr_alternatives": measurement.ocr_alternatives,
                    "x_pt": measurement.x,
                    "y_pt": measurement.y,
                    "note": measurement.note,
                    "extracted_at_utc": stamp,
                })

            # Anything on the page that carried a digit but did not parse is
            # recorded, so that a missed figure is visible rather than absent.
            taken = {m.raw_text for m in page_measurements}
            for line in content:
                for token in line.tokens:
                    if not any(ch.isdigit() for ch in token.text) or token.text in taken:
                        continue
                    reason = _why_not_a_value(token, line)
                    result.unparsed.append({
                        "source_pdf": record.filename,
                        "source_page": page_no,
                        "section_key": section.key if section else "",
                        "raw_text": token.text,
                        "extraction_method": token.method,
                        "confidence": round(token.conf, 1),
                        "x_pt": round(token.cx, 1),
                        "y_pt": round(token.cy, 1),
                        "reason": reason,
                    })
        except Exception as exc:
            log.error("%s page %d transform failed: %s", record.filename, page_no, exc)
            for entry in result.page_log:
                if entry["page"] == page_no:
                    entry["status"] = "failed"
                    entry["error"] = f"transform: {type(exc).__name__}: {exc}"

    doc.close()

    result.findings += checks.check_page_yield(result.page_log, tuned)
    result.findings += checks.check_value_bounds(result.measurements, tuned)
    result.findings += checks.check_ocr_conflicts(result.measurements)
    result.findings += checks.check_unlabelled(result.measurements)
    result.findings += checks.check_share_sums(result.measurements, tuned)
    result.findings += checks.check_period_agreement(record)

    failed = [p for p in result.page_log if p["status"] == "failed"]
    result.status = "ok" if not failed else "partial"
    if result.status == "partial":
        result.error = f"{len(failed)} page(s) failed"
    return result


def _why_not_a_value(token, line) -> str:
    """Say why a digit-bearing token was not loaded as a measurement.

    Most of these are correct exclusions -- axis labels, age bands, numerals
    inside headings. Separating them by reason keeps the genuine misses
    visible instead of burying them in expected noise.
    """
    if parse_period_label(token.text):
        return "period label, used to date other values rather than loaded as one"
    if looks_like_range_label(token.text):
        return "category or bucket label, not a measurement"
    if L._is_prose_number(token, line):
        return "numeral inside a heading or sentence"
    return "contains digits but matched no known value pattern -- review"


def safe_process(record: PdfRecord, cfg: dict) -> ReportResult:
    """Never let one bad report abort the run."""
    try:
        return process(record, cfg)
    except Exception as exc:
        log.exception("unhandled failure on %s", record.filename)
        result = ReportResult(record=record)
        result.status = "failed"
        result.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}"
        return result
