"""PDF Table Extractor — structure-preserving table extraction for Apify.

Why this exists
---------------
Generic "PDF to text" tools flatten tables into a single stream of words.
When a table has empty cells, the column each value belonged to is lost
forever. This Actor extracts tables geometrically, so empty cells stay
empty and every value keeps its column.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any

import httpx
import pdfplumber
from apify import Actor

# --------------------------------------------------------------------------
# text cleaning
# --------------------------------------------------------------------------

_WS = re.compile(r"\s+")
# PDF renderers often emit "1 . 0" or "0 . 98" because of glyph spacing.
_SPLIT_DECIMAL = re.compile(r"(?<=\d)\s+\.\s+(?=\d)")
# "· 10 20" in scientific papers means 10^20.
_SCI_EXP = re.compile(r"(?<=\d)\s*·\s*10\s+(\d+)")
_LIGATURES = {"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl"}


def clean_cell(value: Any) -> str | None:
    """Normalise a raw cell string. Returns None for genuinely empty cells."""
    if value is None:
        return None
    text = str(value)
    for bad, good in _LIGATURES.items():
        text = text.replace(bad, good)
    text = text.replace("\n", " ")
    text = _SPLIT_DECIMAL.sub(".", text)
    text = _SCI_EXP.sub(r"·10^\1", text)
    text = _WS.sub(" ", text).strip()
    return text or None


# --------------------------------------------------------------------------
# table geometry
# --------------------------------------------------------------------------

LINE_SETTINGS = {
    "vertical_strategy": "lines",
    "horizontal_strategy": "lines",
    "snap_tolerance": 3,
    "join_tolerance": 3,
    "intersection_tolerance": 3,
}

TEXT_SETTINGS = {
    "vertical_strategy": "text",
    "horizontal_strategy": "text",
    "text_tolerance": 2,
    "text_x_tolerance": 2,
    "text_y_tolerance": 2,
    "intersection_tolerance": 5,
}

HYBRID_SETTINGS = {
    "vertical_strategy": "lines",
    "horizontal_strategy": "text",
    "snap_tolerance": 3,
    "text_tolerance": 2,
}

SETTINGS_MAP = {
    "lines": LINE_SETTINGS,
    "text": TEXT_SETTINGS,
    "hybrid": HYBRID_SETTINGS,
}


def _iou(a: tuple, b: tuple) -> float:
    """Intersection-over-union of two (x0, top, x1, bottom) boxes."""
    ax0, at, ax1, ab = a
    bx0, bt, bx1, bb = b
    ix0, it = max(ax0, bx0), max(at, bt)
    ix1, ib = min(ax1, bx1), min(ab, bb)
    if ix1 <= ix0 or ib <= it:
        return 0.0
    inter = (ix1 - ix0) * (ib - it)
    area_a = max((ax1 - ax0) * (ab - at), 1e-9)
    area_b = max((bx1 - bx0) * (bb - bt), 1e-9)
    return inter / (area_a + area_b - inter)


def normalise_rows(raw_rows: list) -> list:
    """Clean every cell and pad every row to the same width."""
    rows = [[clean_cell(c) for c in row] for row in raw_rows]
    if not rows:
        return []
    width = max(len(r) for r in rows)
    return [r + [None] * (width - len(r)) for r in rows]


def drop_empty_edges(rows: list) -> list:
    """Remove every fully-empty row and every fully-empty column.

    The "text" detection strategy frequently inserts blank spacer rows between
    real rows. Those carry no information and make the output unusable, so we
    drop them wherever they appear - not just at the edges.
    """
    rows = [r for r in rows if any(c is not None for c in r)]
    if not rows:
        return rows
    width = len(rows[0])
    keep = [i for i in range(width) if any(r[i] is not None for r in rows)]
    if len(keep) == width:
        return rows
    return [[r[i] for i in keep] for r in rows]


def strip_caption_row(rows: list) -> list:
    """Drop a leading row that is really the table's caption, not data.

    The caption is often split across several detected columns, so we test the
    joined text of the whole row rather than a single cell.
    """
    while len(rows) >= 2:
        joined = " ".join(c for c in rows[0] if c is not None)
        if _CAPTION.match(joined):
            rows = rows[1:]
            continue
        break
    return rows


_HAS_DIGIT = re.compile(r"\d")


def looks_like_prose(rows: list) -> bool:
    """Detect body text that a whitespace detector mistook for a table.

    Three independent signals, any of which rejects the block:

    1. Column-wise repetition - prose sliced into columns repeats the same word
       in the same column on every line. Real tables vary down a column.
    2. Long cells - table cells are short labels and values; prose fragments run
       long and contain several words.
    3. Sentence punctuation - prose fragments end in commas and periods.

    Every rule is gated on the block being largely non-numeric, so numeric
    tables are never rejected.
    """
    if len(rows) < 4 or not rows[0]:
        return False
    n_cols = len(rows[0])
    if n_cols < 2:
        return False

    filled = [c for r in rows for c in r if c is not None]
    if not filled:
        return False

    numeric_ratio = sum(1 for c in filled if _HAS_DIGIT.search(c)) / len(filled)
    if numeric_ratio >= 0.15:
        return False

    # 2. Wordy cells.
    multiword = sum(1 for c in filled if c.count(" ") >= 3) / len(filled)
    avg_len = sum(len(c) for c in filled) / len(filled)
    if multiword > 0.30 or (avg_len > 28 and numeric_ratio < 0.05):
        return True

    # 3. Sentence punctuation on non-numeric cells.
    sentence_end = sum(1 for c in filled if c.endswith((".", ",", ";", ":"))) / len(filled)
    if sentence_end > 0.45 and numeric_ratio < 0.10:
        return True

    # 1. Column repetition.
    ratios = []
    for i in range(n_cols):
        col = [r[i] for r in rows if r[i] is not None]
        if len(col) >= 3:
            ratios.append(len(set(col)) / len(col))
    if ratios and (sum(ratios) / len(ratios)) < 0.45 and numeric_ratio < 0.10:
        return True

    return False


def merge_multilevel_header(rows: list) -> list:
    """Flatten a two-row spanning header into one row.

    Papers and financial reports routinely use headers like::

        Model |  BLEU        |  Training Cost
              | EN-DE EN-FR  |  EN-DE EN-FR

    Read naively this loses which metric each column belongs to. We forward-fill
    the top row across its span and join it with the row beneath.
    """
    if len(rows) < 3:
        return rows
    top, second = rows[0], rows[1]
    top_gaps = sum(1 for c in top if c is None)
    # Top row must be sparse (it spans), second row must be complete.
    if top_gaps == 0 or top_gaps == len(top):
        return rows
    if any(c is None for c in second[1:]):
        return rows
    # The third row should look like data, otherwise this is not a header block.
    if all(c is not None for c in rows[2]) and top_gaps < 2:
        return rows

    filled, carry = [], None
    for cell in top:
        if cell is not None:
            carry = cell
        filled.append(carry)

    merged = []
    for upper, lower in zip(filled, second):
        parts = [p for p in (upper, lower) if p]
        merged.append(" ".join(dict.fromkeys(parts)) if parts else None)
    return [merged] + rows[2:]


def score_table(rows: list) -> float:
    """Heuristic 0..1 quality score. Rewards density and column consistency."""
    if not rows or not rows[0]:
        return 0.0
    total = sum(len(r) for r in rows)
    filled = sum(1 for r in rows for c in r if c is not None)
    density = filled / total if total else 0.0

    consistency = 1.0 if len({len(r) for r in rows}) == 1 else 0.6
    n_rows, n_cols = len(rows), len(rows[0])
    size_bonus = min(1.0, (n_rows * n_cols) / 20.0)
    # A single-column "table" is almost always a mis-detected paragraph.
    shape_penalty = 0.4 if n_cols < 2 else 1.0

    return round(density * consistency * (0.6 + 0.4 * size_bonus) * shape_penalty, 3)


_CAPTION = re.compile(
    r"((?:table|tab\.|tbl\.|exhibit|표)\s*[\dIVXA-Z]+[.:\)]?\s+[^\n]{0,220})",
    re.IGNORECASE,
)


def find_caption(page: Any, bbox: tuple) -> str | None:
    """Look for a 'Table N: ...' line just above (or just below) the table."""
    x0, top, x1, bottom = bbox
    page_w, page_h = page.width, page.height
    bands = (
        (0, max(0, top - 90), page_w, max(1, top)),
        (0, min(page_h - 1, bottom), page_w, min(page_h, bottom + 70)),
    )
    for band in bands:
        if band[3] - band[1] <= 1:
            continue
        try:
            text = page.crop(band).extract_text() or ""
        except Exception:
            continue
        hit = _CAPTION.search(_WS.sub(" ", text))
        if hit:
            return hit.group(1).strip()
    return None


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def to_markdown(rows: list, has_header: bool) -> str:
    if not rows:
        return ""
    header = rows[0] if has_header else [f"col{i + 1}" for i in range(len(rows[0]))]
    body = rows[1:] if has_header else rows
    out = ["| " + " | ".join((c or "") for c in header) + " |"]
    out.append("|" + "|".join(["---"] * len(header)) + "|")
    for r in body:
        out.append("| " + " | ".join((c or "") for c in r) + " |")
    return "\n".join(out)


def to_csv(rows: list) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    for r in rows:
        writer.writerow(["" if c is None else c for c in r])
    return buf.getvalue()


def looks_like_header(rows: list) -> bool:
    """First row is a header if it is complete and mostly non-numeric."""
    if len(rows) < 2:
        return False
    first = rows[0]
    if any(c is None for c in first):
        return False
    numericish = sum(
        1 for c in first if c and re.fullmatch(r"[-+]?[\d.,%()·^/\s]+", c)
    )
    return numericish <= len(first) // 2


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------


def extract_page_tables(
    page: Any, strategies: list, min_rows: int, min_cols: int, min_score: float
) -> list:
    candidates = []

    for name in strategies:
        settings = SETTINGS_MAP.get(name)
        if settings is None:
            continue
        try:
            found = page.find_tables(table_settings=settings)
        except Exception:
            continue
        for tbl in found:
            try:
                raw = tbl.extract()
            except Exception:
                continue
            rows = drop_empty_edges(normalise_rows(raw))
            rows = strip_caption_row(rows)
            if looks_like_prose(rows):
                continue
            rows = merge_multilevel_header(rows)
            if len(rows) < min_rows or not rows or len(rows[0]) < min_cols:
                continue
            score = score_table(rows)
            if score < min_score:
                continue
            candidates.append(
                {"bbox": tuple(tbl.bbox), "rows": rows, "strategy": name, "score": score}
            )

    # Two strategies often find the same table. Keep the higher-scoring one.
    candidates.sort(key=lambda c: (-c["score"], -len(c["rows"])))
    kept = []
    for cand in candidates:
        if any(_iou(cand["bbox"], k["bbox"]) > 0.5 for k in kept):
            continue
        kept.append(cand)

    kept.sort(key=lambda c: (c["bbox"][1], c["bbox"][0]))
    return kept


async def fetch_pdf(client: httpx.AsyncClient, url: str, max_bytes: int) -> bytes:
    async with client.stream("GET", url) as resp:
        resp.raise_for_status()
        chunks, total = [], 0
        async for chunk in resp.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"PDF exceeds the {max_bytes // (1024 * 1024)} MB limit")
            chunks.append(chunk)
    return b"".join(chunks)


async def charge(event: str) -> None:
    """Charge a PPE event. No-ops safely when monetization is not configured."""
    try:
        await Actor.charge(event_name=event)
    except Exception as exc:  # pragma: no cover
        Actor.log.debug(f"charge({event}) skipped: {exc}")


# --------------------------------------------------------------------------
# entrypoint
# --------------------------------------------------------------------------


async def main() -> None:
    async with Actor:
        cfg = await Actor.get_input() or {}

        urls = []
        for entry in cfg.get("pdfUrls") or []:
            if isinstance(entry, dict):
                entry = entry.get("url")
            if entry:
                urls.append(str(entry).strip())

        if not urls:
            raise ValueError("Input 'pdfUrls' is empty. Provide at least one PDF URL.")

        choice = cfg.get("strategy", "auto")
        strategies = ["lines", "text", "hybrid"] if choice == "auto" else [choice]

        min_rows = int(cfg.get("minRows", 2))
        min_cols = int(cfg.get("minColumns", 2))
        # The input schema exposes this as 0-100; internally scores are 0-1.
        raw_score = float(cfg.get("minQualityScore", 25))
        min_score = raw_score / 100.0 if raw_score > 1 else raw_score
        max_pages = int(cfg.get("maxPagesPerPdf", 0)) or None
        want_md = bool(cfg.get("includeMarkdown", True))
        want_csv = bool(cfg.get("includeCsv", True))
        require_caption = bool(cfg.get("requireCaption", False))
        max_bytes = int(cfg.get("maxFileSizeMb", 50)) * 1024 * 1024

        await charge("actor-start")
        totals = {"pdfs": 0, "tables": 0, "failed": 0}

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(90.0),
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; PDFTableExtractor/1.0)"},
        ) as client:
            for url in urls:
                Actor.log.info(f"Processing {url}")
                try:
                    blob = await fetch_pdf(client, url, max_bytes)
                except Exception as exc:
                    totals["failed"] += 1
                    Actor.log.warning(f"Download failed for {url}: {exc}")
                    await Actor.push_data({"sourceUrl": url, "error": f"download failed: {exc}"})
                    continue

                file_name = url.rstrip("/").split("/")[-1].split("?")[0] or "document.pdf"
                if not file_name.lower().endswith(".pdf"):
                    file_name += ".pdf"

                try:
                    with pdfplumber.open(io.BytesIO(blob)) as pdf:
                        page_total = len(pdf.pages)
                        limit = min(page_total, max_pages) if max_pages else page_total
                        table_index = 0

                        for page_no in range(limit):
                            page = pdf.pages[page_no]
                            try:
                                found = extract_page_tables(
                                    page, strategies, min_rows, min_cols, min_score
                                )
                            except Exception as exc:
                                Actor.log.warning(
                                    f"page {page_no + 1} of {file_name} failed: {exc}"
                                )
                                continue

                            for tbl in found:
                                caption = find_caption(page, tbl["bbox"])
                                # A labelled table ("Table 3: ...") is almost
                                # always a real table. In papers and reports this
                                # is the single most reliable precision filter.
                                if require_caption and not caption:
                                    continue

                                rows = tbl["rows"]
                                header_flag = looks_like_header(rows)
                                table_index += 1
                                totals["tables"] += 1

                                item = {
                                    "sourceUrl": url,
                                    "fileName": file_name,
                                    "pageNumber": page_no + 1,
                                    "pageCount": page_total,
                                    "tableIndex": table_index,
                                    "caption": caption,
                                    "rowCount": len(rows),
                                    "columnCount": len(rows[0]),
                                    "hasHeader": header_flag,
                                    "header": rows[0] if header_flag else None,
                                    "rows": rows[1:] if header_flag else rows,
                                    "emptyCellCount": sum(
                                        1 for r in rows for c in r if c is None
                                    ),
                                    "qualityScore": tbl["score"],
                                    "extractionStrategy": tbl["strategy"],
                                }
                                if want_md:
                                    item["markdown"] = to_markdown(rows, header_flag)
                                if want_csv:
                                    item["csv"] = to_csv(rows)

                                await Actor.push_data(item)
                                await charge("table-extracted")

                        totals["pdfs"] += 1
                        Actor.log.info(
                            f"{file_name}: {table_index} table(s) from {limit} page(s)"
                        )

                        if table_index == 0:
                            await Actor.push_data(
                                {
                                    "sourceUrl": url,
                                    "fileName": file_name,
                                    "pageCount": page_total,
                                    "tableIndex": 0,
                                    "note": (
                                        "No tables detected. If this PDF is a scan, run OCR "
                                        "first - this Actor reads digital text, not images."
                                    ),
                                }
                            )
                except Exception as exc:
                    totals["failed"] += 1
                    Actor.log.warning(f"Parsing failed for {url}: {exc}")
                    await Actor.push_data({"sourceUrl": url, "error": f"parse failed: {exc}"})

        await Actor.set_status_message(
            f"Done. {totals['tables']} tables from {totals['pdfs']} PDFs "
            f"({totals['failed']} failed)."
        )
