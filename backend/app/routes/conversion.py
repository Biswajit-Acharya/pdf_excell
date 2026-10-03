"""
conversion.py  –  API routes for PDF → Excel pipeline
"""

from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
import os
import uuid
import json
from app.services.pdf_parser import parse_pdf
from app.services.validator import validate_extraction
from app.services.excel_generator import build_primary_field_rows, generate_excel
from app.schemas.conversion import ConversionResponse
from typing import Optional

router = APIRouter()

UPLOAD_DIR   = "uploads"
OUTPUT_DIR   = "outputs"
ANALYSIS_DIR = os.path.join(OUTPUT_DIR, "analysis")

os.makedirs(UPLOAD_DIR,   exist_ok=True)
os.makedirs(OUTPUT_DIR,   exist_ok=True)
os.makedirs(ANALYSIS_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def _analysis_path(conversion_id: str) -> str:
    return os.path.join(ANALYSIS_DIR, f"{conversion_id}.json")


def _save_analysis(conversion_id: str, payload: dict):
    with open(_analysis_path(conversion_id), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def _load_analysis(conversion_id: str) -> dict:
    path = _analysis_path(conversion_id)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Analysis not found")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Build selectable fields list for the frontend Preview
# ---------------------------------------------------------------------------

def _build_selectable_fields(parsed_data: dict) -> list:
    """
    Convert every normalised record into a selectable-field entry.
    No field is dropped, no field is deduplicated by name.
    Every record gets a stable unique `id` for checkbox tracking.
    """
    rows = []

    # Primary: normalised records from pdf_parser
    source_rows = build_primary_field_rows(parsed_data.get("fields", []))
    for idx, rec in enumerate(source_rows):
        rows.append({
            "id":         rec.get("id") or f"field-{idx}",
            "field":      rec.get("field", f"Field {idx + 1}"),
            "value":      rec.get("value", ""),
            "page":       rec.get("page", ""),
            "section":    rec.get("section", ""),
            "type":       rec.get("type", ""),
            "confidence": rec.get("confidence", ""),
            "method":     rec.get("method", ""),
            "source":     "field",
        })

    return rows


# ---------------------------------------------------------------------------
# Build ConversionResponse (used by /convert and /export)
# ---------------------------------------------------------------------------

def _make_export_response(
    conversion_id: str,
    parsed_data: dict,
    validation_summary,
    pdf_type: str,
    warnings: list,
    original_filename: str,
) -> ConversionResponse:
    return ConversionResponse(
        conversion_id=conversion_id,
        original_filename=original_filename,
        page_count=parsed_data["total_pages"],
        pdf_type=pdf_type,
        ocr_used=parsed_data.get("ocr_used", False),
        ocr_attempted_pages=parsed_data.get("ocr_attempted_pages", 0),
        ocr_successful_pages=parsed_data.get("ocr_successful_pages", 0),
        ocr_failed_pages=parsed_data.get("ocr_failed_pages", 0),
        extraction_metrics={
            "tables":        len(parsed_data.get("tables", [])),
            "fields":        len(parsed_data.get("fields", [])),
            "text_blocks":   len(parsed_data.get("text_blocks", [])),
            "raw_ocr_lines": len(parsed_data.get("raw_ocr_text", [])),
        },
        validation_summary=validation_summary,
        preview_data=build_primary_field_rows(parsed_data.get("fields", []))[:20],
        unmapped_items=len(parsed_data.get("unmapped_content", [])),
        generated_excel_filename=f"{conversion_id}.xlsx",
        download_url=f"/api/download/{conversion_id}",
        warnings=warnings,
        status=validation_summary.status,
    )


# ---------------------------------------------------------------------------
# POST /api/analyze   – analyse and return selectable field list
# ---------------------------------------------------------------------------

@router.post("/analyze")
async def analyze_pdf(
    file: UploadFile = File(...),
    conversion_mode: Optional[str] = Form("Automatic"),
    password: Optional[str] = Form(None),
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    conversion_id  = str(uuid.uuid4())
    temp_pdf_path  = os.path.join(UPLOAD_DIR, f"{conversion_id}.pdf")

    with open(temp_pdf_path, "wb") as f:
        f.write(await file.read())

    parsed_data = parse_pdf(temp_pdf_path, conversion_mode, password)

    if parsed_data.get("password_required"):
        return {
            "conversion_id":   "",
            "status":          "PASSWORD_REQUIRED",
            "original_filename": file.filename,
            "fields":          [],
            "warnings":        parsed_data.get("warnings", []),
        }

    validation_summary  = validate_extraction(parsed_data)
    selectable_fields   = _build_selectable_fields(parsed_data)
    all_warnings        = list(set(
        list(validation_summary.warnings) + list(parsed_data.get("warnings", []))
    ))

    _save_analysis(conversion_id, {
        "original_filename": file.filename,
        "pdf_type":          parsed_data.get("pdf_type", "MIXED_PDF"),
        "parsed_data":       parsed_data,
        "fields":            selectable_fields,
    })

    return {
        "conversion_id":       conversion_id,
        "parser_debug_version": "GENERIC_LAYOUT_PARSER_FIX_V1",
        "status":              validation_summary.status,
        "original_filename":   file.filename,
        "page_count":          parsed_data.get("total_pages", 0),
        "pdf_type":            parsed_data.get("pdf_type", "MIXED_PDF"),
        "ocr_used":            parsed_data.get("ocr_used", False),
        "ocr_attempted_pages": parsed_data.get("ocr_attempted_pages", 0),
        "ocr_successful_pages":parsed_data.get("ocr_successful_pages", 0),
        "ocr_failed_pages":    parsed_data.get("ocr_failed_pages", 0),
        "fields":              selectable_fields,
        "tables":              len(parsed_data.get("tables", [])),
        "warnings":            all_warnings,
    }


# ---------------------------------------------------------------------------
# POST /api/export/{conversion_id}   – generate Excel from selected rows
# ---------------------------------------------------------------------------

@router.post("/export/{conversion_id}", response_model=ConversionResponse)
async def export_excel(
    conversion_id: str,
    selected_field_ids_json: Optional[str] = Form("[]"),
    export_all: Optional[bool] = Form(False),
):
    analysis        = _load_analysis(conversion_id)
    parsed_data     = analysis["parsed_data"]
    available_fields = analysis.get("fields", [])
    original_filename = analysis.get("original_filename", "document.pdf")
    pdf_type        = analysis.get("pdf_type", parsed_data.get("pdf_type", "MIXED_PDF"))

    try:
        selected_ids = set(json.loads(selected_field_ids_json or "[]"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid selected fields JSON")

    if export_all:
        selected_rows  = available_fields
        include_tables = True
    else:
        selected_rows  = [row for row in available_fields if row["id"] in selected_ids]
        include_tables = False

    if not selected_rows and not include_tables:
        raise HTTPException(status_code=400, detail="Please select at least one field")

    # Build export_data using ONLY the selected rows (single source of truth)
    export_data = dict(parsed_data)
    export_data["fields"] = [
        {
            "id":         row.get("id", ""),
            "field":      row.get("field", ""),
            "value":      row.get("value", ""),
            "page":       row.get("page", ""),
            "section":    row.get("section", ""),
            "type":       row.get("type", ""),
            "method":     row.get("method", ""),
            "confidence": row.get("confidence", ""),
        }
        for row in selected_rows
    ]

    if not include_tables:
        export_data["tables"] = []

    validation_summary = validate_extraction(export_data)
    warnings = list(set(
        list(validation_summary.warnings) + list(export_data.get("warnings", []))
    ))

    excel_path = os.path.join(OUTPUT_DIR, f"{conversion_id}.xlsx")
    generate_excel(
        export_data, validation_summary,
        excel_path, original_filename, pdf_type,
        include_tables=include_tables,
    )

    return _make_export_response(
        conversion_id, export_data, validation_summary,
        pdf_type, warnings, original_filename,
    )


# ---------------------------------------------------------------------------
# POST /api/convert   – legacy one-shot route (kept for backward compat)
# ---------------------------------------------------------------------------

@router.post("/convert", response_model=ConversionResponse)
async def convert_pdf(
    file: UploadFile = File(...),
    conversion_mode: Optional[str] = Form("Automatic"),
    expected_fields_count: Optional[int] = Form(None),
    expected_fields_json: Optional[str] = Form(None),
    password: Optional[str] = Form(None),
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    conversion_id = str(uuid.uuid4())
    temp_pdf_path = os.path.join(UPLOAD_DIR, f"{conversion_id}.pdf")

    with open(temp_pdf_path, "wb") as f:
        f.write(await file.read())

    try:
        expected_fields: list = []
        if expected_fields_json:
            try:
                expected_fields = json.loads(expected_fields_json)
            except json.JSONDecodeError:
                raise HTTPException(status_code=400, detail="Invalid JSON for expected fields")

        parsed_data = parse_pdf(temp_pdf_path, conversion_mode, password)

        if parsed_data.get("password_required"):
            return ConversionResponse(
                conversion_id="",
                original_filename=file.filename,
                page_count=0,
                pdf_type="ENCRYPTED",
                ocr_used=False,
                ocr_attempted_pages=0,
                ocr_successful_pages=0,
                ocr_failed_pages=0,
                extraction_metrics={"tables": 0, "fields": 0, "text_blocks": 0, "raw_ocr_lines": 0},
                validation_summary={
                    "total_pages": 0, "total_text_blocks": 0, "total_tables": 0,
                    "total_rows": 0, "total_columns": 0, "total_fields": 0,
                    "warnings": parsed_data.get("warnings", []), "status": "PASSWORD_REQUIRED"
                },
                preview_data=[],
                unmapped_items=0,
                generated_excel_filename="",
                download_url="",
                warnings=parsed_data.get("warnings", []),
                status="PASSWORD_REQUIRED",
            )

        pdf_type = parsed_data.get("pdf_type", "MIXED_PDF")
        validation_summary = validate_extraction(parsed_data, expected_fields_count, expected_fields)

        # Fail-safe: OCR produced text but pipeline lost it
        if (parsed_data.get("ocr_successful_pages", 0) > 0
                and len(parsed_data.get("raw_ocr_text", [])) > 0
                and not parsed_data.get("fields")):
            validation_summary.status = "FAILED"
            parsed_data["warnings"].append(
                "OCR_PIPELINE_DATA_LOSS: OCR produced text but no records were extracted."
            )

        if validation_summary.status != "FAILED" and (
            validation_summary.warnings or parsed_data.get("warnings")
        ):
            validation_summary.status = "COMPLETED WITH WARNINGS"

        excel_filename = f"{conversion_id}.xlsx"
        excel_path = os.path.join(OUTPUT_DIR, excel_filename)
        generate_excel(parsed_data, validation_summary, excel_path, file.filename, pdf_type)

        all_warnings = list(set(
            list(validation_summary.warnings) + list(parsed_data.get("warnings", []))
        ))

        return ConversionResponse(
            conversion_id=conversion_id,
            original_filename=file.filename,
            page_count=parsed_data["total_pages"],
            pdf_type=pdf_type,
            ocr_used=parsed_data.get("ocr_used", False),
            ocr_attempted_pages=parsed_data.get("ocr_attempted_pages", 0),
            ocr_successful_pages=parsed_data.get("ocr_successful_pages", 0),
            ocr_failed_pages=parsed_data.get("ocr_failed_pages", 0),
            extraction_metrics={
                "tables":        len(parsed_data.get("tables", [])),
                "fields":        len(parsed_data.get("fields", [])),
                "text_blocks":   len(parsed_data.get("text_blocks", [])),
                "raw_ocr_lines": len(parsed_data.get("raw_ocr_text", [])),
            },
            validation_summary=validation_summary,
            preview_data=build_primary_field_rows(parsed_data.get("fields", []))[:20],
            unmapped_items=len(parsed_data.get("unmapped_content", [])),
            generated_excel_filename=excel_filename,
            download_url=f"/api/download/{conversion_id}",
            warnings=all_warnings,
            status=validation_summary.status,
        )

    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# GET /api/download/{conversion_id}
# ---------------------------------------------------------------------------

@router.get("/download/{conversion_id}")
async def download_excel(conversion_id: str):
    excel_path = os.path.join(OUTPUT_DIR, f"{conversion_id}.xlsx")
    if not os.path.exists(excel_path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        path=excel_path,
        filename="converted_data.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
