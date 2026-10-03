import os
import json
import uuid
import fitz
import logging
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

def parse_with_gemini(pdf_path: str, password: str = None) -> dict:
    # Use environment variable for GEMINI_API_KEY.
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY environment variable is not set.")

    client = genai.Client(api_key=api_key)
    
    # Get total pages
    try:
        doc = fitz.open(pdf_path)
        if doc.needs_pass and password:
            doc.authenticate(password)
        total_pages = len(doc)
        doc.close()
    except Exception as e:
        logger.error(f"Error opening PDF: {e}")
        total_pages = 0

    # Upload file to Gemini
    uploaded_file = client.files.upload(file=pdf_path)
    
    # Prompt for structured data extraction
    prompt = """
    You are an expert document-understanding engine for HR/back-office PDF to Excel conversion.
    Analyze this uploaded PDF visually and textually. If pages are scanned images, read the image first
    with OCR-style visual understanding, then extract structured records from the visible text.

    Extract all real fields, key-value pairs, check boxes, table rows, and certificate/marksheet data.
    For school/matric/marksheet certificates, include every visible field such as:
    student name, father name, mother name, date of birth, roll number, registration number,
    serial/sl number, school name, school/centre code, board name, examination name/year,
    subject names, subject codes, maximum marks, marks secured, grades/result/division,
    total marks, percentage, issue date, and any other visible labelled detail.

    Organize records logically into sections.
    Return ONLY a JSON array of objects with this exact structure for each extracted item:
    [
      {
        "section": "Name of the section or 'General' if no clear section",
        "field": "Clean, descriptive, proper name for the key/field without trailing colons",
        "value": "The extracted value",
        "type": "key_value" (use for standard fields) or "table_cell" (if it's part of a table),
        "page": 1 (the page number this item was found on)
      }
    ]
    IMPORTANT RULES:
    1. NEVER combine the value into the field name. Example: if a document says "Institution: Demo National Bank", the field MUST be "Institution", and the value MUST be "Demo National Bank". NEVER output "Institution Demo National Bank" as a field name.
    2. NEVER use generic field names like "Item", "Value", "Field", "Row X", "Column X", "Extracted Text". If extracting from a table, combine the column name and a unique identifier (e.g. "Transaction (28 Sep 2026) - Amount"). DO NOT use suffixes like "(row 1)" or "(row 2)".
    3. Remove trailing colons, periods, or asterisks from field names.
    4. For subject marks in marksheets, use descriptive fields like "Mathematics - Maximum Marks" and "Mathematics - Marks Secured". 
    5. Do not invent, guess, complete, or correct values that are not visible in the document.
    6. Keep identifiers exactly as printed, including leading zeros and separators.
    7. If a visible text cannot be mapped to a useful field, skip it instead of returning garbage.
    Do not wrap the JSON output in markdown blocks, output raw JSON only.
    """
    
    import time
    max_retries = 10
    response = None
    last_error = None
    
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model='gemini-3.5-flash',
                contents=[uploaded_file, prompt],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                )
            )
            break  # Success
        except Exception as e:
            last_error = e
            if "503" in str(e) or "429" in str(e):
                logger.warning(f"Gemini API busy (Attempt {attempt+1}/{max_retries}): {e}. Retrying in 5 seconds...")
                time.sleep(5)
            else:
                logger.error(f"Gemini API Error: {e}")
                break
    
    extracted_data = []
    if response:
        try:
            extracted_data = json.loads(response.text)
        except json.JSONDecodeError:
            # Sometime it adds markdown anyway, try to clean it
            text = response.text.strip()
            if text.startswith("```json"):
                text = text[7:]
            if text.endswith("```"):
                text = text[:-3]
            try:
                extracted_data = json.loads(text)
            except Exception:
                logger.error("Failed to parse Gemini JSON response.")
    else:
        raise Exception(f"Gemini generation failed after {max_retries} retries. Last error: {last_error}")

    # Cleanup the file from Gemini
    try:
        client.files.delete(name=uploaded_file.name)
    except Exception:
        pass
        
    records = []
    for item in extracted_data:
        records.append({
            "id": str(uuid.uuid4()),
            "page": item.get("page", 1),
            "section": item.get("section", "General"),
            "field": item.get("field", ""),
            "value": str(item.get("value", "")),
            "type": item.get("type", "key_value"),
            "confidence": 99.0,
            "source": "gemini",
            "method": "Gemini AI",
            "bbox": {"x0":0, "y0":0, "x1":0, "y1":0},
            "selected": True,
            "x": 0,
            "y": 0,
        })
        
    results = {
        "total_pages": total_pages, 
        "direct_text_pages": total_pages, 
        "ocr_attempted_pages": 0,
        "ocr_successful_pages": 0, 
        "ocr_failed_pages": 0, 
        "text_blocks": [],
        "tables": [], 
        "fields": records, 
        "raw_ocr_text": [], 
        "unmapped_content": [],
        "pdf_type": "AI_PARSED", 
        "ocr_used": False, 
        "warnings": [],
        "is_encrypted": False, 
        "password_required": False, 
        "page_audit": []
    }
    return results
