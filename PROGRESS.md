# Processing progress

**All 34 of 34 source PDFs processed.**

## Status by report type

| Type | Count | Status |
| --- | --- | --- |
| Monthly | 24 | all processed |
| Quarterly | 8 | all processed |
| Annual | 2 | all processed |

## A bug found and fixed after the first full pass

The first pass through all 34 reports surfaced a real bug affecting the 12
OCR-derived monthly reports (September 2024 - August 2025): section-divider
detection matched section titles as an exact phrase ("Instant Payment System
for"), which works for native text but not OCR -- OCR reads a divider's
decorative title as a scattered bag of words, not in reading order, so the
phrase never appears intact even though every word is genuinely on the page.
The result was most of a report's data being misfiled under `interoperability`
or `summary` instead of the correct payment system.

Fixed by matching on the *set* of significant words rather than word order,
with a specificity tie-break for pages that satisfy more than one rule (the
combined CliQ+JoMoPay "Instant Payment Systems in Jordan" divider was losing
a tie to the plain CliQ rule before this). All 12 affected reports have been
reprocessed with the fix and the combined dataset rebuilt.

## Known remaining limitations

- **A handful of values on the 12 OCR-era reports carry a generic fallback
  metric name** (e.g. "CliQ Transactions") instead of the specific one
  ("Number of Transactions") used elsewhere, on reports where OCR read the
  KPI caption with enough noise that the metric-name detector didn't
  recognise it. The data is present and correctly attributed to the right
  system -- only the metric label is less specific on those particular rows.
  Filtering on `system` alone still finds the right rows; filtering on an
  exact `metric` string may miss a few months' worth for these reports.
- **A small number of individual values carry a low confidence score**
  (visible in each workbook's `Measurements` sheet) where OCR read a figure
  with enough uncertainty that it may be wrong -- e.g. one November 2024
  transaction count reads anomalously high relative to its neighbours, with
  `confidence` around 64 and `ocr_repaired=True` flagged on that row. These
  are visible, not hidden: filter the dataset on confidence if you want to
  exclude or double-check them.
- Annual reports' 12-month trend lines carry no data labels and are omitted
  rather than estimated from pixel positions -- same figures exist at full
  precision in the monthly reports.
- On monthly reports' "Systems Summary" section, five sub-pages present
  per-system KPIs (CliQ, JoMoPay, eFAWATEERcom, ACH, ECCU, in that fixed
  order) but the page text only says "Summary" -- the system name is a logo
  image. These rows carry `system=summary` instead of the specific system;
  the values themselves are correct.

## Rebuilding

```bash
cd etl
# put all 34 source PDFs under data/raw/pdfs/
python -m jopacc_etl --config config/config.yaml run --workers 1 --force
python -m jopacc_etl --config config/config.yaml dataset
```
