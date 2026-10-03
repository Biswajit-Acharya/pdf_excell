from pydantic import BaseModel
from typing import List, Optional, Dict, Any

class ValidationSummary(BaseModel):
    total_pages: int
    total_text_blocks: int
    total_tables: int
    total_rows: int
    total_columns: int
    total_fields: int
    expected_field_count: Optional[int] = None
    missing_fields: List[str] = []
    duplicate_fields: List[str] = []
    blank_values: int = 0
    warnings: List[str] = []
    status: str  # VERIFIED, COMPLETED WITH WARNINGS, FAILED

class ConversionResponse(BaseModel):
    conversion_id: str
    original_filename: str
    page_count: int
    pdf_type: str
    ocr_used: bool
    ocr_attempted_pages: int
    ocr_successful_pages: int
    ocr_failed_pages: int
    extraction_metrics: Dict[str, Any]
    validation_summary: ValidationSummary
    preview_data: List[Dict[str, Any]]
    unmapped_items: int
    generated_excel_filename: str
    download_url: str
    warnings: List[str]
    status: str
