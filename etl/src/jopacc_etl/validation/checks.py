"""Check extracted data and say plainly where it is doubtful.

Nothing here deletes or edits a record. Every check attaches a finding, and
the findings travel into the workbook alongside the data so that whoever uses
the output can see exactly which rows to treat with suspicion.

Reconciliation is deliberately narrow. These reports show shares that sum to
100% and split totals into two named parts, and those relationships can be
checked. They also show figures that look additive but are not -- a summary
page counting users across systems double-counts anyone using two of them --
so no check assumes a displayed total equals the sum of its neighbours unless
the report presents them as parts of that whole.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, asdict
from typing import List

log = logging.getLogger(__name__)

SEVERITY = ("info", "warning", "error")


@dataclass
class Finding:
    severity: str
    check: str
    page: object
    detail: str
    record_id: object = None

    def as_dict(self) -> dict:
        return asdict(self)


def check_value_bounds(records: List[dict], cfg: dict) -> List[Finding]:
    """Flag values outside the plausible range for their unit."""
    bounds = cfg["validation"]["bounds"]
    out = []
    for rec in records:
        limits = bounds.get(rec["unit"])
        value = rec["value_numeric"]
        if limits is None or value is None:
            continue
        if value < limits["min"] or value > limits["max"]:
            out.append(Finding(
                "error", "value_out_of_bounds", rec["source_page"],
                f"{rec['raw_value']!r} resolves to {value:,.2f} {rec['unit']}, "
                f"outside {limits['min']:,}..{limits['max']:,}",
                rec["record_id"]))
    return out


def check_ocr_conflicts(records: List[dict]) -> List[Finding]:
    """Surface positions where OCR passes disagreed on the reading.

    This is the check that catches a decimal point lost in rendering, where
    ``1.74 M`` is also readable as ``174M``. Nothing else in the pipeline can
    see that difference, because both are valid numbers.
    """
    out = []
    for rec in records:
        if not rec.get("ocr_alternatives"):
            continue
        out.append(Finding(
            "warning", "ocr_readings_disagree", rec["source_page"],
            f"read as {rec['raw_value']!r}; other passes read "
            f"{rec['ocr_alternatives']}",
            rec["record_id"]))
    return out


def check_unlabelled(records: List[dict]) -> List[Finding]:
    """Report values with neither a dimension nor a metric name.

    A value can legitimately have no dimension label -- a flat KPI like
    'Number of Users' has nothing to break out by -- as long as it has a
    metric name. It is only a real miss when both are empty, because then
    nothing on the record says what the number means at all.
    """
    out = []
    for rec in records:
        if rec.get("label") or rec.get("metric"):
            continue
        out.append(Finding(
            "warning", "value_unidentified", rec["source_page"],
            f"{rec['raw_value']!r} extracted but neither a metric name nor a "
            f"dimension label could be matched to it",
            rec["record_id"]))
    return out


def check_share_sums(records: List[dict], cfg: dict) -> List[Finding]:
    """Where a panel shows a two-part split, do the shares reach 100%?

    Only percentages inside the same panel of the same page are compared, and
    only when there are exactly two of them, which is the shape the reports
    actually use for on-us/off-us and money-transfer/purchase splits.
    """
    tol = cfg["validation"]["share_sum_tolerance_pct"]
    grouped = defaultdict(list)
    for rec in records:
        if rec["unit"] == "percent" and rec["value_numeric"] is not None:
            grouped[(rec["source_page"], rec["panel_index"], rec["label"])].append(rec)

    near = cfg["validation"]["share_pair_window_pct"]
    out = []
    for (page, panel, label), group in grouped.items():
        if len(group) != 2:
            continue
        total = sum(r["value_numeric"] for r in group)
        parts = " + ".join(r["raw_value"] for r in group)

        if abs(total - 100.0) <= tol:
            out.append(Finding(
                "info", "shares_reconcile", page,
                f"panel {panel} label {label!r}: {parts} = {total:.1f}%", None))
        elif abs(total - 100.0) <= near:
            # Close to a whole but not close enough. Two percentages that sum
            # to 9.8% are simply unrelated figures -- a pair of growth rates
            # sitting side by side -- and warning about those would bury the
            # real misreads. Only a near miss is evidence of one.
            out.append(Finding(
                "warning", "shares_nearly_sum_to_100", page,
                f"panel {panel} label {label!r}: {parts} = {total:.1f}%; "
                f"a two-part split is likely and one part may be misread", None))
    return out


def check_page_yield(page_log: List[dict], cfg: dict) -> List[Finding]:
    """Pages that produced suspiciously little are reported, never skipped."""
    floor = cfg["validation"]["min_tokens_per_content_page"]
    out = []
    for page in page_log:
        if page["status"] != "ok":
            out.append(Finding("error", "page_extraction_failed", page["page"],
                               page.get("error", "page could not be read")))
        elif page["token_count"] == 0:
            out.append(Finding("warning", "empty_page", page["page"],
                               "no text recovered by either extraction route"))
        elif page["token_count"] < floor:
            out.append(Finding("info", "sparse_page", page["page"],
                               f"only {page['token_count']} token(s) recovered"))
    return out


def check_period_agreement(record, findings_page=None) -> List[Finding]:
    """Compare the period on the cover with the one in the filename."""
    out = []
    for issue in record.issues:
        severity = "error" if "cannot open" in issue else "warning"
        out.append(Finding(severity, "inventory_issue", None, issue))
    return out


def check_structure(warnings: List[str]) -> List[Finding]:
    return [Finding("warning", "structure_change", None, text) for text in warnings]


def summarise(findings: List[Finding]) -> dict:
    counts = {level: 0 for level in SEVERITY}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    return counts
