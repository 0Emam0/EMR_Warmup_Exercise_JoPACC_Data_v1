# JoPACC Payment System Reports — ETL & Dataset

Extracts structured, analysis-ready data from JoPACC's payment system reports
(monthly, quarterly, annual) and produces a combined dataset covering
September 2024 – September 2026.

## Status

**17 of 34 source PDFs processed** (all the text-bearing reports, Sep 2025
onward). The remaining 17 need OCR and are still in progress — see
[`PROGRESS.md`](PROGRESS.md) for exactly which ones and why.

## What's in this repo

| Path | What it is |
| --- | --- |
| `dataset/jopacc_dataset.xlsx` | **Start here.** One combined, long-format table across all processed reports — `report_type, reporting_period, system, metric, dimension, period, value, unit`. Ready to pivot or load into pandas. |
| `workbooks/` | One workbook per source PDF, each with a `Curated` sheet (same clean shape, one report) and a `Measurements` sheet (full audit trail — page, position, confidence, raw literal). |
| `etl/` | The Python ETL pipeline that produced all of the above. See `etl/README.md` for architecture, setup, and how to run it. |
| `docs/` | Dataset scope inventory and data-dictionary/model writeups produced during analysis. |

## Reproducing or extending this

```bash
cd etl
pip install -r requirements.txt      # plus: apt-get install tesseract-ocr
# put PDFs under etl/data/raw/pdfs/
python -m jopacc_etl --config config/config.yaml inventory
python -m jopacc_etl --config config/config.yaml run --workers 1
python -m jopacc_etl --config config/config.yaml dataset   # rebuilds dataset/
```

State is incremental — rerunning `run` skips reports already processed
successfully and only reprocesses what's new, failed, or changed.
