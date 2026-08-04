# PDF Table Extractor — get tables out of PDFs without losing the columns

Extract **tables** from PDF files into clean JSON, CSV, Markdown, and Excel-ready rows.
Column structure and empty cells are preserved, so every number stays in the column it
came from.

Built for financial reports, scientific papers, government statistics, invoices,
tariff schedules, and any other PDF where the value you need lives inside a table.

---

## The problem this solves

Every generic "PDF to text" tool flattens a table into one stream of words.
Take this table from a research paper:

| Model | BLEU EN-DE | BLEU EN-FR | Cost EN-DE | Cost EN-FR |
|---|---|---|---|---|
| ByteNet | 23.75 | | | |
| Deep-Att + PosUnk | | 39.2 | | 1.0e20 |
| GNMT + RL | 24.6 | 39.92 | 2.3e19 | 1.4e20 |

A text extractor gives you this:

```
Model BLEU Training Cost EN-DE EN-FR EN-DE EN-FR ByteNet 23.75
Deep-Att + PosUnk 39.2 1.0e20 GNMT + RL 24.6 39.92 2.3e19 1.4e20
```

Is `23.75` an EN-DE score or an EN-FR score? **There is no way to tell.**
The empty cells vanished, and with them the alignment between values and columns.
Every downstream use — a spreadsheet, a database, an LLM prompt — inherits that error.

This Actor reads the table geometrically instead, so the output is:

```json
{
  "header": ["Model", "BLEU EN-DE", "BLEU EN-FR", "Cost EN-DE", "Cost EN-FR"],
  "rows": [
    ["ByteNet",           "23.75", null,    null,     null],
    ["Deep-Att + PosUnk", null,    "39.2",  null,     "1.0e20"],
    ["GNMT + RL",         "24.6",  "39.92", "2.3e19", "1.4e20"]
  ]
}
```

`null` means the cell was genuinely empty. Nothing is silently dropped.

---

## What you get

- **Empty cells preserved as `null`** — the single most common failure of text extractors
- **Spanning headers flattened** — a two-row header like `BLEU / EN-DE EN-FR` becomes `BLEU EN-DE`, `BLEU EN-FR`
- **Three detectors, best result wins** — ruled borders, whitespace alignment, and a hybrid; overlapping detections are de-duplicated
- **Table captions detected** — `Table 3: Variations on the architecture` is attached to the right table
- **Split decimals repaired** — `1 . 0` becomes `1.0`, `· 10 20` becomes `·10^20`
- **Quality score per table** so you can filter out noisy detections
- **JSON + CSV + Markdown** in every result — export to Excel in one click, or paste straight into an LLM prompt
- **No proxy, no browser, no API key** — fast and cheap to run

---

## Input

```json
{
  "pdfUrls": [
    "https://example.com/annual-report-2025.pdf",
    "https://arxiv.org/pdf/1706.03762"
  ],
  "strategy": "auto",
  "minRows": 2,
  "minColumns": 2,
  "minQualityScore": 25,
  "includeMarkdown": true,
  "includeCsv": true
}
```

| Field | Default | Notes |
|---|---|---|
| `pdfUrls` | — | Required. Direct links to PDF files. |
| `strategy` | `auto` | `auto` runs all detectors. Use `lines` for bordered tables, `text` for whitespace-aligned ones. |
| `minRows` / `minColumns` | `2` / `2` | Filters out fragments. Keep `minColumns` at 2+ to avoid catching paragraphs. |
| `minQualityScore` | `25` | 0–100. Lower it if tables are missed, raise it if you get noise. |
| `maxPagesPerPdf` | `0` | `0` processes every page. |
| `maxFileSizeMb` | `50` | Larger PDFs are skipped. |

---

## Output

One dataset item per table:

| Field | Description |
|---|---|
| `sourceUrl`, `fileName` | Where the table came from |
| `pageNumber`, `pageCount` | Location in the document |
| `tableIndex` | 1-based index within the PDF |
| `caption` | e.g. `Table 2: BLEU scores on newstest2014` |
| `header` | Header row, or `null` if none detected |
| `rows` | Data rows. Empty cells are `null`. |
| `rowCount`, `columnCount`, `emptyCellCount` | Shape of the table |
| `qualityScore` | 0–1 confidence in the detection |
| `extractionStrategy` | Which detector produced it |
| `markdown` | Ready-to-paste Markdown table |
| `csv` | CSV string |

Export the dataset as **Excel, CSV, JSON, or XML** from the Apify Console, or pull it
through the API.

---

## Typical uses

- **Financial analysis** — pull balance sheets and income statements out of annual reports
- **Research** — collect result tables across dozens of papers into one spreadsheet
- **Government and regulatory data** — statistics releases, tariff schedules, procurement notices
- **RAG and LLM pipelines** — feed models a real table instead of a scrambled paragraph
- **Competitive intelligence** — spec sheets and pricing tables from vendor PDFs

---

## Limits

- **Digital text only.** Scanned PDFs are images; run OCR first, then use this Actor.
  When no tables are found you get a result explaining why rather than an empty run.
- **Rotated and vertically-written tables** are not supported yet.
- **Cells merged across rows** are reported in the first row they occupy.
- Password-protected PDFs are skipped with an error field.

---

## Pricing

Pay per event. You are charged for each table successfully extracted, plus a small
start fee per run. PDFs that yield no tables cost only the start fee.

---

## Support

Found a PDF that extracts badly? Open an issue on the Actor's **Issues** tab with the
URL and the page number. Detector tuning is the main way this Actor improves.
