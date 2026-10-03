from app.schemas.conversion import ValidationSummary
from typing import Dict, Any, List, Optional

def validate_extraction(
    parsed_data: Dict[str, Any], 
    expected_fields_count: Optional[int] = None,
    expected_fields: List[str] = None
) -> ValidationSummary:
    
    fields = parsed_data.get("fields", [])
    tables = parsed_data.get("tables", [])
    text_blocks = parsed_data.get("text_blocks", [])
    
    detected_fields = [f["field"] for f in fields]
    
    missing_fields = []
    if expected_fields:
        for ef in expected_fields:
            if ef not in detected_fields:
                missing_fields.append(ef)
                
    blank_values = sum(1 for f in fields if not f["value"])
    
    status = "VERIFIED"
    warnings = []
    
    if expected_fields_count and len(fields) < expected_fields_count:
        status = "FAILED_VALIDATION"
        warnings.append(f"Expected {expected_fields_count} fields, found {len(fields)}")
    elif missing_fields:
        status = "PARTIAL"
        warnings.append(f"Missing fields: {', '.join(missing_fields)}")
        
    return ValidationSummary(
        total_pages=parsed_data["total_pages"],
        total_text_blocks=len(text_blocks),
        total_tables=len(tables),
        total_rows=sum(len(t["data"]) for t in tables),
        total_columns=max((len(t["data"][0]) for t in tables if t["data"]), default=0),
        total_fields=len(fields),
        expected_field_count=expected_fields_count,
        missing_fields=missing_fields,
        duplicate_fields=[],
        blank_values=blank_values,
        warnings=warnings,
        status=status
    )
