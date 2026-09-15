# Processing progress

17 of 34 source PDFs processed as of this commit. The split is chronological:
everything from September 2025 onward has a native text layer; everything
through August 2025 was exported with all text converted to vector outlines
and needs OCR, which is why it's slower and not yet done.

## Done (17)

| Report | Status |
| --- | --- |
| september_monthly_report_2025_en.pdf | ok |
| october_monthly_report_2025_en.pdf | ok |
| november_monthly_report_2025_en.pdf | ok |
| december_monthly_report_2025_en.pdf | ok |
| january_monthly_report_2026_en.pdf | ok |
| february_monthly_report_2026_en.pdf | ok |
| march_monthly_report_2026_en.pdf | ok |
| april_monthly_report_2026_en.pdf | ok |
| may_monthly_report_2026_en.pdf | ok |
| june_monthly_report_2026_en.pdf | ok |
| july_monthly_report_2026_en_0.pdf | ok |
| august_monthly_report_2026_en.pdf | ok |
| quarterly_report_q3_2025_en_1.pdf | ok |
| quarterly_report_q4_2025_en.pdf | ok |
| quarterly_report_q1_2026_en.pdf | ok |
| quarterly_report_q2_2026_en.pdf | ok |
| end_of_year_systems_report_2025_en.pdf | ok |

## Remaining (17) — need OCR

| Report |
| --- |
| september_monthly_report_2024_en_0.pdf |
| october_monthly_report_2024_en_0.pdf |
| november_monthly_report_2024_en.pdf |
| december_monthly_report_2024_en_0.pdf |
| january_monthly_report_2025_en_0.pdf |
| february_monthly_report_2025_en_1.pdf |
| march_monthly_report_2025_en.pdf |
| april_monthly_report_2025_en.pdf |
| may_monthly_report_2025_en.pdf |
| june_monthly_report_2025_en.pdf |
| july_monthly_report_2025_en.pdf |
| august_monthly_report_2025_en.pdf |
| quarterly_report_q3_2024_en.pdf |
| quarterly_report_q4_2024_en.pdf |
| quarterly_report_q1_2025_en.pdf |
| quarterly_report_q2_2025_en.pdf |
| end_of_year_systems_report_2024_en.pdf |

## Resuming

```bash
cd etl
# put all 34 source PDFs under data/raw/pdfs/
python -m jopacc_etl --config config/config.yaml run --workers 1
```

State is keyed by filename + content hash, so this picks up exactly where
it left off — the 17 already `ok` are skipped, only the remaining 17 (or
anything JoPACC has since replaced under an unchanged filename) get
processed. Rebuild the combined dataset afterward with:

```bash
python -m jopacc_etl --config config/config.yaml dataset
```

## Known limitations (see `etl/README.md` for detail)

- Annual reports' 12-month trend lines carry no data labels and are omitted
  rather than estimated from pixel positions — same figures exist at full
  precision in the monthly reports.
- On monthly reports' "Systems Summary" section, five sub-pages present
  per-system KPIs (CliQ, JoMoPay, eFAWATEERcom, ACH, ECCU, in that fixed
  order) but the page text only says "Summary" — the system name is a logo
  image. These ~75 rows per report currently carry `system=summary` instead
  of the specific system; the values themselves are correct.
