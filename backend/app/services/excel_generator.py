"""
excel_generator.py
==================
Generates Excel files from the normalised extraction records produced by
pdf_parser.py.

Key design principles
---------------------
- Uses the SAME normalised records array that is shown in Preview.
- No secondary extraction; no re-parsing.
- No hardcoded FIELD_ORDER list.
- Identifiers (PIN, phone, account, IFSC, Aadhaar, roll numbers, etc.) are
  written as Excel TEXT to prevent scientific-notation corruption.
- build_primary_field_rows() now returns ALL records unchanged (no dedup by
  name, no field-order filtering).
"""

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from app.schemas.conversion import ValidationSummary
import datetime
import re

# ---------------------------------------------------------------------------
# XML safety
# ---------------------------------------------------------------------------
XML_ILLEGAL_RE = re.compile(
    r"[^\x09\x0A\x0D\x20-\uD7FF\uE000-\uFFFD\U00010000-\U0010FFFF]"
)

# ---------------------------------------------------------------------------
# Identifier detection – values that must stay as Excel text strings
# ---------------------------------------------------------------------------
IDENTIFIER_RE = re.compile(
    r"^\s*("
    r"\d{10,}"              "|"   # long numbers (phone, account, Aadhaar)
    r"[A-Z]{4}\d{7}"        "|"   # IFSC
    r"\d{4}[\s-]?\d{4}[\s-]?\d{4}"  "|"  # 12-digit grouped
    r"\d{6}"                "|"   # PIN / 6-digit code
    r"0\d{4,}"              "|"   # leading-zero numbers
    r"[A-Z0-9]{6,20}"             # alphanumeric identifiers
    r")\s*$",
    re.IGNORECASE,
)


def _clean(val) -> str:
    if val is None:
        return ""
    if isinstance(val, (int, float)):
        return str(val)
    val = str(val)
    return XML_ILLEGAL_RE.sub("", val)


def _write_cell(ws, row: int, col: int, value, font=None, fill=None, bold=False):
    cleaned = _clean(value)
    cell = ws.cell(row=row, column=col, value=cleaned)
    # Force text format for identifier-like values to prevent Excel mutation
    if isinstance(value, str) and IDENTIFIER_RE.match(cleaned):
        cell.number_format = "@"
    cell.data_type = "s"
    if bold or font:
        cell.font = font or Font(bold=True)
    if fill:
        cell.fill = fill
    return cell


def _autosize(ws, max_width: int = 70):
    for col_cells in ws.columns:
        length = max(len(str(c.value or "")) for c in col_cells)
        col_cells[0].column_letter  # initialise
        ws.column_dimensions[col_cells[0].column_letter].width = min(max(length + 2, 12), max_width)


# ---------------------------------------------------------------------------
# Public: build_primary_field_rows
# ---------------------------------------------------------------------------

def build_primary_field_rows(fields: list) -> list:
    """
    Returns the list of field records ready for display / export.

    Unlike the previous implementation, this function:
      1. Does NOT filter by a hardcoded FIELD_ORDER list.
      2. Does NOT collapse repeated field names.
      3. Does NOT limit the output count.
      4. Preserves every record in page order.

    Each returned dict has keys: field, value, page, method, confidence,
    section, type, id  (all optional in older callers that only use field/value).
    """
    rows = []
    for rec in fields:
        field = _clean(rec.get("field", ""))
        value = _clean(rec.get("value", ""))
        if not field or not value:
            continue
        rows.append({
            "id":         rec.get("id", ""),
            "field":      field,
            "value":      value,
            "page":       rec.get("page", ""),
            "section":    rec.get("section", ""),
            "type":       rec.get("type", ""),
            "method":     rec.get("method", ""),
            "confidence": rec.get("confidence", ""),
        })
    return rows


# ---------------------------------------------------------------------------
# Excel generation
# ---------------------------------------------------------------------------

HEADER_FILL = PatternFill("solid", fgColor="1E3A5F")
HEADER_FONT = Font(bold=True, color="FFFFFF")
ALT_FILL    = PatternFill("solid", fgColor="F0F4FA")


def generate_excel(
    parsed_data: dict,
    validation_summary: ValidationSummary,
    output_path: str,
    original_filename: str,
    pdf_type: str,
    include_debug_sheets: bool = False,
    include_tables: bool = False,
):
    wb = Workbook()

    # ------------------------------------------------------------------ #
    #  Sheet 1: Extracted Data (selected records)                         #
    # ------------------------------------------------------------------ #
    ws_data = wb.active
    ws_data.title = "Extracted Data"

    data_headers = ["Page", "Section", "Field", "Detected Data"]
    for col, hdr in enumerate(data_headers, 1):
        cell = ws_data.cell(row=1, column=col, value=hdr)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(vertical="center")

    fields = parsed_data.get("fields", [])
    rows   = build_primary_field_rows(fields)

    for row_idx, rec in enumerate(rows, 2):
        fill = ALT_FILL if row_idx % 2 == 0 else None
        _write_cell(ws_data, row_idx, 1, rec.get("page", ""),    fill=fill)
        _write_cell(ws_data, row_idx, 2, rec.get("section", ""), fill=fill)
        _write_cell(ws_data, row_idx, 3, rec.get("field", ""),   fill=fill)
        _write_cell(ws_data, row_idx, 4, rec.get("value", ""),   fill=fill)

    ws_data.freeze_panes = "A2"
    _autosize(ws_data)

    # ------------------------------------------------------------------ #
    #  Sheet 2: Summary                                                   #
    # ------------------------------------------------------------------ #
    ws_summary = wb.create_sheet(title="Summary")
    key_font = Font(bold=True)

    missing_warning = "No"
    if validation_summary.status in ("COMPLETED WITH WARNINGS", "PARTIAL"):
        missing_warning = "Yes – See Warnings"

    summary_data = [
        ("Original PDF",              original_filename),
        ("Generated Date",            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("PDF Classification",        pdf_type),
        ("Total Pages",               validation_summary.total_pages),
        ("Direct-Text Pages",         parsed_data.get("direct_text_pages", 0)),
        ("OCR Attempted Pages",       parsed_data.get("ocr_attempted_pages", 0)),
        ("OCR Successful Pages",      parsed_data.get("ocr_successful_pages", 0)),
        ("OCR Failed Pages",          parsed_data.get("ocr_failed_pages", 0)),
        ("Total Extracted Records",   len(rows)),
        ("  Key/Value Records",       sum(1 for r in fields if r.get("type") == "key_value")),
        ("  Table Cell Records",      sum(1 for r in fields if r.get("type") == "table_cell")),
        ("  Paragraph/Declaration",   sum(1 for r in fields if r.get("type") in ("paragraph", "declaration"))),
        ("  Unstructured Text",       sum(1 for r in fields if r.get("type") == "unstructured_text")),
        ("Detected Tables (native)",  validation_summary.total_tables),
        ("Unmapped Content Blocks",   len(parsed_data.get("unmapped_content", []))),
        ("Warnings Present",          missing_warning),
        ("Validation Status",         validation_summary.status),
    ]

    for r, (k, v) in enumerate(summary_data, 1):
        ws_summary.cell(row=r, column=1, value=k).font = key_font
        ws_summary.cell(row=r, column=2, value=str(v) if not isinstance(v, (int, float)) else v)

    _autosize(ws_summary)

    # ------------------------------------------------------------------ #
    #  Sheet 3: Tables (if requested or available native tables)          #
    # ------------------------------------------------------------------ #
    if (include_debug_sheets or include_tables) and parsed_data.get("tables"):
        for t_idx, table in enumerate(parsed_data["tables"], 1):
            ws_tbl = wb.create_sheet(title=f"Table {t_idx}")
            ws_tbl.cell(row=1, column=1, value=f"Page: {table.get('page', '?')}").font = Font(bold=True)
            for r_idx, row in enumerate(table.get("data", []), 2):
                for c_idx, cell_val in enumerate(row, 1):
                    c = _write_cell(ws_tbl, r_idx, c_idx, cell_val)
                    if r_idx == 2:
                        c.font = Font(bold=True)
            ws_tbl.freeze_panes = "A3"
            _autosize(ws_tbl)

    # ------------------------------------------------------------------ #
    #  Optional debug sheets                                              #
    # ------------------------------------------------------------------ #
    if include_debug_sheets:
        # Raw OCR Text
        if parsed_data.get("raw_ocr_text"):
            ws_raw = wb.create_sheet(title="Raw OCR Text")
            raw_headers = ["Page", "Line No.", "Text", "Confidence", "X", "Y"]
            for c, h in enumerate(raw_headers, 1):
                ws_raw.cell(row=1, column=c, value=h).font = HEADER_FONT
            for r, item in enumerate(parsed_data["raw_ocr_text"], 2):
                _write_cell(ws_raw, r, 1, item.get("page", ""))
                _write_cell(ws_raw, r, 2, item.get("line_no", ""))
                _write_cell(ws_raw, r, 3, item.get("text", ""))
                _write_cell(ws_raw, r, 4, item.get("confidence", ""))
                _write_cell(ws_raw, r, 5, item.get("x", ""))
                _write_cell(ws_raw, r, 6, item.get("y", ""))
            ws_raw.freeze_panes = "A2"
            _autosize(ws_raw)

    # ------------------------------------------------------------------ #
    #  Warnings sheet                                                     #
    # ------------------------------------------------------------------ #
    all_warnings = list(set(
        list(validation_summary.warnings) + list(parsed_data.get("warnings", []))
    ))
    if all_warnings:
        ws_warn = wb.create_sheet(title="Warnings")
        ws_warn.cell(row=1, column=1, value="Warnings").font = Font(bold=True)
        for r, w in enumerate(all_warnings, 2):
            ws_warn.cell(row=r, column=1, value=w)
        _autosize(ws_warn)

    wb.save(output_path)
