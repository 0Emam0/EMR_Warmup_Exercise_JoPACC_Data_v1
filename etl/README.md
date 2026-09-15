# JoPACC Payment System Reports — ETL

Extracts structured data from the payment system reports published by the
Jordan Payments and Clearing Company and writes one Excel workbook per report.

The reports are designed infographics rather than data documents, so this is
not a table-scraping job. What follows explains what the pipeline actually
does, what it deliberately does not do, and where its output should be treated
with suspicion.

---

## What the source material is really like

Three things about the corpus drove nearly every design decision.

**Half the files contain no text.** Seventeen of the thirty-four reports were
exported with all glyphs converted to vector outlines. An eighty-one page
monthly report returns around 275 characters — barely more than its cover.
Rendered to screen they look identical to the readable files. The split is
chronological: everything up to and including the August 2025 monthly is
outlined, everything from September 2025 onward carries real text.

**The numbers that matter are the hardest to read.** JoPACC sets its key
figures in white on grey bars and in amber inside ring charts. A plain OCR
pass reads the headings correctly and drops exactly those values.

**Nothing on the page says which system it describes.** A slide headed
`Summary` sits inside the CliQ chapter and means CliQ, but the only thing
saying so is a logo, which is an image.

## How it handles them

| Problem | Approach |
| --- | --- |
| No text layer | Render and OCR, chosen per page rather than per file |
| Low-contrast values | Ensemble of OCR passes over isolated colour layers |
| Amber text inside rings | Connected-component filtering removes the ring |
| Misread decimals | Competing readings kept and surfaced, never silently dropped |
| Rotated bar labels | Text read as spans, matched by row instead of column |
| Unnamed systems | Inherited from section dividers, discovered per file |

### The ensemble, and why it is the size it is

Each OCR page is read several times: once in greyscale, once with near-white
pixels isolated, once with amber isolated and ring shapes filtered out, each
under two page-segmentation modes. The readings are merged by box overlap and
the most confident one wins.

The composition is measured, not guessed:

- dropping the **light** layer loses every figure on navy slides
- dropping **psm 6** loses values that sparse-text mode alone misses
- dropping the **dark** layer changes nothing, because the greyscale pass
  already reads dark type on a light ground — so it is not run

The merge keeps the losing readings on the token. This is the only mechanism
that can catch a lost decimal point, where `1.74 M` is also readable as
`174M` — a hundredfold error in a number that parses perfectly either way.
Those disagreements appear in each workbook's `Validation` sheet.

---

## Installation

Python 3.10 or newer.

```bash
pip install -r requirements.txt
```

System dependency, needed for the outlined files:

```bash
sudo apt-get install tesseract-ocr        # Debian / Ubuntu
brew install tesseract                    # macOS
```

Check it is visible: `tesseract --version`.

---

## Running it

Put the PDFs anywhere under the input directory. Subfolders are fine and are
not used for classification — report type is read from the file itself.

```
data/raw/pdfs/
├── monthly/
├── quarterly/
└── yearly/
```

**1. Register the files and check coverage.**

```bash
python -m jopacc_etl --config config/config.yaml inventory
```

This reports how many files of each type were found, which are inside the
scope window, which need OCR, any duplicates, any periods with no report, and
any file whose cover disagrees with its filename. It writes nothing except
`data/state/inventory.json`.

**2. Extract.**

```bash
python -m jopacc_etl --config config/config.yaml run
```

Useful variants:

```bash
run --only quarterly              # one report type
run --period 2026-08              # one period
run --failed                      # retry only what did not succeed
run --force                       # ignore previous state, redo everything
run --workers 4                   # parallelism; see the note below
```

Workbooks land in `data/output/<report_type>/`, one per PDF, named
`jopacc_monthly_2026_08.xlsx` and so on. Logs go to `logs/`.

**A note on workers.** OCR is CPU-bound. Setting more workers than you have
cores does not speed the run up — it stops any single report from finishing,
which also defeats the incremental state. On a single core, leave it at 1.

Expect roughly forty seconds per text-bearing report and five to six minutes
per outlined one on a single core.

**3. Resume or re-run.** State is committed after each report, so an
interrupted run picks up where it stopped. A report is reprocessed
automatically if its content hash has changed since last time — JoPACC
replaces published files without renaming them, and five files in the
2024–2026 window have already been replaced this way.

---

## What a workbook contains

| Sheet | What is in it |
| --- | --- |
| `Measurements` | One row per extracted figure, with full provenance |
| `Sections` | The section map discovered in this report |
| `Unparsed` | Every digit-bearing token that did not become a measurement, with the reason |
| `Page_Log` | Per page: how it was read, how much came back, any error |
| `Metadata` | The report's identity, hashes, timestamps, processing counts |
| `Validation` | Findings, colour-coded by severity |

### Grain

> One row in `Measurements` = one numeric value as printed on one page.

This is the grain the source supports. A higher grain — one row per period per
system per metric — would require asserting that a given figure *is* the
transaction count for CliQ in August 2026, and on many pages that assertion
cannot be made from the page alone. Loading at the printed-value grain keeps
the output honest and leaves the semantic mapping to a downstream model, where
it can be reviewed.

Each row carries where it came from: source PDF, report type, reporting
period, page, section, panel, panel title, the label it was matched to, the
raw literal as printed, the normalised value, unit, how it was read, the
reader's confidence, and its coordinates on the page.

### Keys

- `record_id` is unique within a workbook: period, page, sequence.
- `source_pdf` + `source_page` + `x_pt` + `y_pt` identifies a value's position
  on the page and is stable across re-runs.
- `reporting_period` + `report_type` is the natural join key to any other
  JoPACC data.

---

## Limitations

These are known and deliberate. None of them is a bug to be fixed by loosening
a threshold.

**Annual trend lines cannot be read.** The annual reports plot twelve-month
series with an axis but no data labels. Recovering them would mean estimating
values from pixel positions, which would produce numbers that look precise and
are not. They are omitted and logged. The same figures are available at full
precision from the monthly reports.

**System attribution is inherited, not read.** A value's system comes from the
section it sits in. If a report reorders its sections without changing its
divider titles, attribution follows the dividers and would be wrong. The
`Sections` sheet shows the map that was used, so this is checkable.

**Labels on dense pages are positional.** On the quarterly posters, a value is
matched to the nearest plausible label. Where no match is found the row is
still loaded, with the note `value not associated with any label`, rather than
attached to a guess.

**Percentages are not reconciled against their totals.** Shares are checked to
sum to 100% within a panel. Displayed totals are *not* checked against the sum
of nearby categories, because the reports include totals that legitimately do
not equal such a sum — the cross-system user counts double-count anyone using
two systems.

---

## Layout

```
jopacc_etl/
├── config/config.yaml           every threshold and path; nothing is hard-coded
├── data/
│   ├── raw/pdfs/                input
│   ├── state/                   inventory and run state
│   └── output/                  workbooks, by report type
├── logs/
├── src/jopacc_etl/
│   ├── cli.py                   commands
│   ├── config.py                config loading and validation
│   ├── logging_setup.py
│   ├── pipeline.py              per-report orchestration
│   ├── discovery/inventory.py   registration, hashing, classification, scope
│   ├── extraction/
│   │   ├── tokens.py            native and OCR reading
│   │   ├── layout.py            lines, panels, label association
│   │   └── sections.py          divider detection
│   ├── transform/numbers.py     value parsing and normalisation
│   ├── validation/checks.py     bounds, conflicts, reconciliation
│   └── load/excel.py            workbook writing
├── tests/
└── requirements.txt
```

## Tests

```bash
pytest tests -v
```

The tests run against the real PDFs, not fixtures, and assert values verified
by eye against the rendered pages.
