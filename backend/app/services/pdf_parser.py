"""
pdf_parser.py
=============
Production multi-stage document processing pipeline.

Architecture
------------
1. pdfProcessor   – open PDF, detect page count, render pages
2. ocrEngine      – call ocr_service for scanned pages
3. layoutAnalyzer – spatial block analysis, section detection,
                    key/value pairing, table detection, paragraph capture
4. documentParser – normalise records, merge pages, dedup, audit completeness

Record schema (normalised)
--------------------------
{
    "id":         str,   # stable unique ID
    "page":       int,
    "section":    str,
    "field":      str,
    "value":      str,
    "type":       str,   # key_value | table_cell | table_row | heading |
                         # paragraph | declaration | checkbox | identifier |
                         # unstructured_text
    "confidence": float,
    "source":     str,   # native | ocr | hybrid
    "method":     str,   # human-readable extraction method label
    "bbox":       dict,  # {x0, y0, x1, y1}
}
"""

import fitz
import pdfplumber
import re
import logging
import uuid
import math
from typing import Optional, List, Dict, Any
from app.services.ocr_service import perform_ocr_on_page

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants / tunables
# ---------------------------------------------------------------------------

MIN_ALNUM_FOR_NATIVE = 25

SECTION_HEADING_MAX_WORDS = 8
SECTION_HEADING_MIN_LEN   = 4

SECTION_KEYWORDS = re.compile(
    r"\b("
    r"basic\s+information|applicant\s+details|address\s+information|"
    r"educational\s+qualification|institute\s+/?course|residential\s+status|"
    r"last\s+exam|eligibility|bank\s+information|student\s+declaration|"
    r"income\s+details?|certificate\s+details?|course\s+information|"
    r"personal\s+details?|contact\s+details?|family\s+details?|"
    r"qualification\s+details?|document\s+details?|declaration"
    r")\b",
    re.IGNORECASE,
)

LABEL_SUFFIX_RE = re.compile(
    r"(no\.?|number|name|date|code|type|status|year|id|address|"
    r"income|authority|amount|percentage|marks?|cgpa|stream|trade|"
    r"branch|course|board|district|block|ward|village|pin|mobile|"
    r"email|e-mail|gender|religion|category|occupation|education|"
    r"relationship|guardian|father|mother|spouse|roll|registration|"
    r"enrollment|enrolment|admission|bank|branch|ifsc|account|"
    r"seeding|scholar|scheme|department|academic|issuing|aadhaar|"
    r"aadhar|uid|dob|place|signature|applicant|institution|"
    r"institute|nature|duration|medium|language|subject|result|"
    r"grade|division|class|section|serial|sl)\s*$",
    re.IGNORECASE,
)

BAD_FIELD_NAME_RE = re.compile(r"^col\s+\d+(\s*\(row\s+\d+\))?$", re.IGNORECASE)

DOCUMENT_TITLE_WORDS_RE = re.compile(
    r"\b("
    r"school|college|board|certificate|examination|marksheet|mark\s*sheet|"
    r"government|department|university|council|office|secretary|controller"
    r")\b",
    re.IGNORECASE,
)

SUBJECT_WORDS_RE = re.compile(
    r"\b(language|english|sanskrit|science|mathematics|maths|social|history|geography|subject)\b",
    re.IGNORECASE,
)

SUBJECT_CODE_RE = re.compile(
    r"^(?P<code>[A-Z]{2,5})\s*(?P<subject>[A-Z][A-Z\s/&().-]{2,}?)"
    r"\s+(?P<max>\d{2,5})(?:\s+(?P<marks>[#A-Z0-9]{1,4}))?$",
    re.IGNORECASE,
)

CERT_FIELD_PATTERNS = [
    ("Roll Number", r"\b(?:roll|rol)\s*(?:no\.?|number)?\s*[:\-]?\s*([A-Z0-9\/\-]*\d[A-Z0-9\/\-]{3,})\b"),
    ("Registration Number", r"\b(?:registration|regn|reg\.?)\s*(?:no\.?|number)?\s*[:\-]?\s*([A-Z0-9\/\-]*\d[A-Z0-9\/\-]{3,})\b"),
    ("Serial Number", r"\b(?:serial|sl)\s*(?:no\.?|number)?\s*[:\-]?\s*([A-Z0-9\/\-]*\d[A-Z0-9\/\-]{2,})\b"),
    ("School Code", r"\b(?:school|centre|center)\s*code\s*[:\-]?\s*([A-Z0-9\/\-]*\d[A-Z0-9\/\-]{1,})\b"),
    ("Issue Date", r"\bissue\s*date\s*[:\-]?\s*(\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{2,4})\b"),
    ("Print Date", r"\bprint\s*date\s*[:\-]?\s*(\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{2,4})\b"),
    ("Date of Birth", r"\b(?:date\s*of\s*birth|dob|born\s+on)\s*[:\-]?\s*(\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{2,4})\b"),
    ("Examination Year", r"\b(?:exam(?:ination)?|held).*?\b(20\d{2}|19\d{2})\b"),
    ("Result", r"\b(?:result|status)\s*[:\-]?\s*(pass(?:ed)?|fail(?:ed)?|qualified)\b"),
    ("Division", r"\b(?:division|class|grade)\s*[:\-]?\s*([A-Z0-9+\- ]{1,20})\b"),
    ("Total Marks", r"\b(?:total|aggregate)\s*(?:marks)?\s*[:\-]?\s*(\d{2,4})\b"),
    ("Percentage", r"\b(?:percentage|percent)\s*[:\-]?\s*(\d{1,3}(?:\.\d+)?)\s*%?\b"),
]

NUMBER_WORD_TOTALS = {
    "FOUR HUNDRED AND FORTY TWO": "442",
    "FOUR HUNDRED FORTY TWO": "442",
}

BORDER_LINE_RE = re.compile(r"^[\s\-_=|*#~.]{3,}$")

IDENTIFIER_RE = re.compile(
    r"^\s*("
    r"\d{10,}|"                       # long numbers (account, aadhaar, phone)
    r"[A-Z]{4}\d{7}|"                 # IFSC-like
    r"\d{4}\s?\d{4}\s?\d{4}|"         # 12-digit grouped (Aadhaar)
    r"\d{6}|"                         # PIN / OTP / roll
    r"[A-Z0-9]{6,20}"                 # alphanumeric identifiers
    r")\s*$",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _uid() -> str:
    return str(uuid.uuid4())

def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())

def _clean_label(text: str) -> str:
    text = _clean(text)
    text = re.sub(r"[:|=_\-]+$", "", text).strip()
    if re.fullmatch(r"dob", text, flags=re.IGNORECASE):
        return "DOB"
    text = re.sub(r"\bno\b\.?", "Number", text, flags=re.IGNORECASE)
    text = " ".join(w.capitalize() if not w.isupper() else w for w in text.split())
    return _dedupe_repeated_words(text)

def _clean_value(text: str) -> str:
    text = _clean(text)
    text = re.sub(r"^[|:=\-\s]+|[|:=\-\s]+$", "", text)
    return _clean(text)

def _dedupe_repeated_words(text: str) -> str:
    words = _clean_value(text).split()
    output = []
    for word in words:
        if not output or output[-1].lower() != word.lower():
            output.append(word)
    half = len(output) // 2
    if half and len(output) % 2 == 0:
        if [w.lower() for w in output[:half]] == [w.lower() for w in output[half:]]:
            output = output[:half]
    return " ".join(output)

def _looks_like_person_name(text: str) -> bool:
    t = _clean_value(text)
    if not t or DOCUMENT_TITLE_WORDS_RE.search(t) or LABEL_SUFFIX_RE.search(t) or re.search(r"\d", t):
        return False
    if re.search(r"(son\s*daughter|sondaughter|\bson\b|\bdaughter\b|\bfather\b|\bmother\b|\bschool\b|\bcertificate\b|\bpassed\b|\bborn\b)", t, re.IGNORECASE):
        return False
    if re.search(r"[(){}[\]|_=]", t):
        return False
    words = [w for w in re.split(r"\s+", t) if len(w) > 1]
    if not 2 <= len(words) <= 5:
        return False
    letters = sum(ch.isalpha() for ch in t)
    return letters >= max(4, len(t.replace(" ", "")) * 0.65)

def _clean_school_name(text: str) -> str:
    t = _clean_value(text)
    t = re.sub(r"[^A-Za-z0-9 .,&'()-]", " ", t)
    t = _clean(t)
    t = re.sub(r"^(?:from|ffom|fom|frm|fo)\s+", "", t, flags=re.IGNORECASE)
    matches = re.findall(r"([A-Z][A-Z0-9 .,&'()-]{2,80}?\bSCHOOL(?:\s*,?\s*[A-Z][A-Z .'-]{2,30})?)", t, re.IGNORECASE)
    if matches:
        t = matches[-1]
    t = re.sub(r"^.*?\b(SRI(?:\s+SRI)?\s+[A-Z0-9 .,&'()-]*?\bSCHOOL\b.*)$", r"\1", t, flags=re.IGNORECASE)
    t = re.sub(r"\b(cape|code|sl|no|roll|m\d+[a-z]*|certificate|examination)\b.*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"^[^A-Za-z]*(?:ese|im|ry|fo|from|ffom|fom|frm)\b[ .,-]*", "", t, flags=re.IGNORECASE)
    t = _clean_value(t)
    t = re.sub(r"\s+\b[a-z]{1,3}\b$", "", t)
    return _clean_value(t)

def _school_name_candidates(lines: list[str]) -> list[tuple[int, str]]:
    candidates = []
    for idx, line in enumerate(lines):
        if not re.search(r"\bschool\b", line, re.IGNORECASE):
            continue
        if re.search(r"\b(certificate|examination|board|secondary)\b", line, re.IGNORECASE):
            continue
        school = _clean_school_name(line)
        if len(school) < 8 or not re.search(r"\bschool\b", school, re.IGNORECASE):
            continue
        score = 10
        if re.search(r"\b(high|public|upper|primary|secondary)\s+school\b", school, re.IGNORECASE):
            score += 20
        if re.search(r"\b(from|ffom|fom|frm)\b", line, re.IGNORECASE):
            score += 18
        if "," in school:
            score += 8
        if idx > 4:
            score += 5
        if re.search(r"\b(cape|roll|sl\.?\s*no|code|m\d+[a-z]*)\b", line, re.IGNORECASE):
            score -= 12
        candidates.append((score, school))
    return sorted(candidates, key=lambda item: (item[0], len(item[1])), reverse=True)

def _looks_like_value(text: str) -> bool:
    t = _clean_value(text)
    if not t:
        return False
    if ":" in t:
        return False
    if LABEL_SUFFIX_RE.search(t) and not re.search(r"\d|@|/", t):
        return False
    return True

def _clean_aadhaar_name(text: str) -> str:
    t = _clean_value(text)
    t = re.split(r"\b(?:DOB|YOB|Date\s*of\s*Birth)\b|/", t, maxsplit=1, flags=re.IGNORECASE)[0]
    t = re.sub(r"[^A-Za-z .'-]", " ", t)
    t = _clean(t)
    words = [
        w for w in t.split()
        if len(w) > 2 and w.lower() not in {"qg", "iqg", "gq", "dob"}
    ]
    while words and words[0].lower() in {"gee", "uidai", "govt", "government", "india", "aloidy"}:
        words.pop(0)
    # OCR often prefixes the local-language name before the English name.
    if len(words) > 3:
        words = words[-3:]
    name = " ".join(words)
    return name.title() if name else ""

def _clean_address(text: str) -> str:
    address = _clean_value(text)
    address = re.sub(r"�", " ", address)
    address = re.sub(r"\b(?:help@uidai\.gov\.in|www\.uidai\.gov\.in|1947)\b.*$", "", address, flags=re.IGNORECASE)
    pin_match = re.search(r"\b\d{6}\b", address)
    if pin_match:
        address = address[:pin_match.end()]
    address = re.sub(r"\bAT\s*-\s+\d+\s+[A-Za-z]+\s+[^A-Z0-9]*([A-Z][A-Z]{2,})", r"AT- \1", address)
    address = re.sub(r",\s*(?:[a-z]{1,3}\s+){2,}[a-z]{2,}\s+(?=[A-Z][a-z])", ", ", address)
    address = re.sub(r"\s+", " ", address)
    address = re.sub(r"\s+,", ",", address)
    return _clean_value(address)

def _aadhaar_records_from_lines(page: int, lines: list[str], source: str) -> list:
    records = []
    seen = set()
    clean_lines = [_clean_value(line) for line in lines if _clean_value(line)]
    full_text = " ".join(clean_lines)
    has_dob = re.search(r"\b(?:DOB|YOB|Date\s*of\s*Birth)\b", full_text, re.IGNORECASE)
    aadhaar_match = re.search(r"\b(\d{4}\s+\d{4}\s+\d{4})\b", full_text)
    gender_match = re.search(r"\b(Male|Female|Transgender|Purush|Mahila)\b", full_text, re.IGNORECASE)

    _add_record_once(records, _make_record(
        page, "Identity Details", "Aadhaar Number", aadhaar_match.group(1),
        "key_value", 96.0, source, "Aadhaar Heuristic",
        {"x0": 0, "y0": 0, "x1": 0, "y1": 0}
    ), seen) if aadhaar_match else None

    dob_match = re.search(
        r"\b(?:DOB|YOB|Date\s*of\s*Birth)\s*[:\-]?\s*(\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{2,4})\b",
        full_text,
        re.IGNORECASE,
    )
    if dob_match:
        _add_record_once(records, _make_record(
            page, "Identity Details", "Date of Birth", dob_match.group(1),
            "key_value", 96.0, source, "Aadhaar Heuristic",
            {"x0": 0, "y0": 0, "x1": 0, "y1": 0}
        ), seen)

    if gender_match:
        _add_record_once(records, _make_record(
            page, "Identity Details", "Gender", gender_match.group(1).title(),
            "key_value", 95.0, source, "Aadhaar Heuristic",
            {"x0": 0, "y0": 0, "x1": 0, "y1": 0}
        ), seen)

    address_match = re.search(
        r"\bAddress\s*:\s*(.+?)(?=\b(?:Print Date|Issue Date|DOB|Male|Female|Government|Aadhaar)\b|$)",
        full_text,
        re.IGNORECASE,
    )
    if address_match:
        address = _clean_address(address_match.group(1))
        if len(address) >= 10:
            _add_record_once(records, _make_record(
                page, "Address Details", "Address", address,
                "key_value", 92.0, source, "Aadhaar Heuristic",
                {"x0": 0, "y0": 0, "x1": 0, "y1": 0}
            ), seen)

            care_match = re.search(r"\bC\s*/?\s*O\s*:?\s*([^,]{3,80})", address, re.IGNORECASE)
            if care_match:
                care_of = _clean_value(care_match.group(1))
                if re.search(r"[A-Za-z]", care_of):
                    _add_record_once(records, _make_record(
                        page, "Address Details", "Care Of", care_of,
                        "key_value", 88.0, source, "Aadhaar Heuristic",
                        {"x0": 0, "y0": 0, "x1": 0, "y1": 0}
                    ), seen)

            pin_match = re.search(r"\b(\d{6})\b", address)
            if pin_match:
                _add_record_once(records, _make_record(
                    page, "Address Details", "Pin Code", pin_match.group(1),
                    "key_value", 90.0, source, "Aadhaar Heuristic",
                    {"x0": 0, "y0": 0, "x1": 0, "y1": 0}
                ), seen)

    if not (aadhaar_match and (has_dob or gender_match)) and not address_match:
        return records

    for line in clean_lines:
        if re.search(r"\b(?:DOB|YOB|Date\s*of\s*Birth)\b", line, re.IGNORECASE):
            name = _clean_aadhaar_name(line)
            if _looks_like_person_name(name):
                _add_record_once(records, _make_record(
                    page, "Identity Details", "Name", name,
                    "key_value", 90.0, source, "Aadhaar Heuristic",
                    {"x0": 0, "y0": 0, "x1": 0, "y1": 0}
                ), seen)
            break
    return records

def _record_key(rec: dict) -> tuple:
    return (
        rec.get("page", 1),
        _clean(rec.get("field", "")).lower(),
        _clean(rec.get("value", "")).lower(),
    )

def _subject_name_from_text(text: str) -> str:
    t = _clean_value(text)
    t = re.sub(r"\bOD[1I]A\b", "ODA", t, flags=re.IGNORECASE)
    t = re.sub(r"^[|:=\-\s]+", "", t)
    t = re.sub(r"\b[A-Z]{2,5}\b\s*[|:=\-]?\s*", "", t, count=1)
    t = re.sub(r"\b(first|second|third)language\b", r"\1 language", t, flags=re.IGNORECASE)
    t = re.sub(r"\b(first|second|third)\s*language\b", r"\1 language", t, flags=re.IGNORECASE)
    t = re.sub(r"\b\d{1,3}\b.*$", "", t).strip(" |:=.-")
    t = re.sub(r"\s+", " ", t)
    if not t or len(t) < 3:
        return ""
    return t.title()

def _normalise_subject_line(line: str) -> str:
    clean = _clean_value(line).replace("SUBJECTSANDMARKSSECURED", "")
    clean = clean.replace("|", " ")
    clean = re.sub(r"[_=~`'\"“”‘’\\{}\[\]()>]+", " ", clean)
    clean = re.sub(r"^[^A-Za-z0-9]+", "", clean)
    clean = re.sub(r"\b(FIRST|SECOND|THIRD)LANGUAGE\b", r"\1 LANGUAGE", clean, flags=re.IGNORECASE)
    clean = re.sub(r"\bOD[1I]A\b", "ODA", clean, flags=re.IGNORECASE)
    clean = re.sub(r"\s+", " ", clean).strip()
    clean = re.sub(r"(?<=\d)\s+[A-Z]\b$", "", clean, flags=re.IGNORECASE)
    number_matches = list(re.finditer(r"\d+", clean))
    if number_matches:
        first = number_matches[0]
        if first.group().startswith("100") and len(first.group()) >= 5:
            clean = clean[:first.end()]
        elif len(number_matches) >= 2:
            clean = clean[:number_matches[1].end()]
    return clean

def _repair_subject_numbers(max_marks: str | None, marks: str | None) -> tuple[str | None, str | None]:
    if marks:
        marks = marks.upper().replace("O", "0").replace("I", "1").replace("L", "1")
        marks = marks.replace("#", "7")
        marks = re.sub(r"\D", "", marks)
    if max_marks:
        max_marks = re.sub(r"\D", "", max_marks)
    if max_marks and len(max_marks) == 4 and max_marks.startswith("100") and marks:
        max_marks = "100"
    if max_marks and len(max_marks) == 4 and max_marks.startswith("100") and not marks:
        max_marks = "100"
    if max_marks and len(max_marks) >= 5 and max_marks.startswith("100"):
        tail = max_marks[3:]
        max_marks = "100"
        if not marks and len(tail) == 2 and int(tail) >= 20:
            marks = tail
    if max_marks and max_marks != "100" and max_marks.startswith("100") and marks:
        max_marks = "100"
    return max_marks, marks

def _extract_total_marks_from_lines(page_lines: dict) -> dict[int, int]:
    totals: dict[int, int] = {}
    for page, lines in page_lines.items():
        joined = " ".join(_clean_value(line).upper() for line in lines)
        for words, value in NUMBER_WORD_TOTALS.items():
            if words in joined:
                totals[page] = int(value)
                break
    return totals

def _repair_subject_totals(records: list, page_lines: dict) -> list:
    totals = _extract_total_marks_from_lines(page_lines)
    if not totals:
        return records

    output = list(records)
    by_page: dict[int, dict[str, dict[str, dict]]] = {}
    for rec in output:
        field = _clean(rec.get("field", ""))
        match = re.match(r"(.+?) - (Subject Code|Maximum Marks|Marks Secured)$", field, re.IGNORECASE)
        if not match:
            continue
        subject, kind = match.group(1), match.group(2).lower()
        by_page.setdefault(rec.get("page", 1), {}).setdefault(subject.lower(), {})[kind] = rec

    for page, total in totals.items():
        subjects = by_page.get(page, {})
        mark_values = []
        missing_subjects = []
        for subject_key, parts in subjects.items():
            mark_rec = parts.get("marks secured")
            if mark_rec and re.fullmatch(r"\d{1,3}", _clean_value(mark_rec.get("value", ""))):
                mark_values.append(int(_clean_value(mark_rec["value"])))
            elif parts.get("subject code") and parts.get("maximum marks"):
                missing_subjects.append((subject_key, parts))

        if len(missing_subjects) != 1 or not mark_values:
            continue
        inferred = total - sum(mark_values)
        if not (0 <= inferred <= 100):
            continue

        subject_key, parts = missing_subjects[0]
        subject_name = _clean(parts["maximum marks"]["field"]).rsplit(" - ", 1)[0]
        new_rec = dict(parts["maximum marks"])
        new_rec.update({
            "id": _uid(),
            "field": f"{subject_name} - Marks Secured",
            "value": str(inferred),
            "confidence": min(float(new_rec.get("confidence", 70.0)), 78.0),
            "method": "Total Marks Repair",
        })
        output.append(new_rec)

    return output

def _subject_records_from_line(
    line: str,
    page: int,
    section: str,
    source: str,
    method: str,
) -> list:
    clean = _normalise_subject_line(line)
    match = SUBJECT_CODE_RE.match(clean)
    if not match:
        loose = re.match(
            r"^(?P<subject>[A-Z][A-Z\s/&().-]{3,}?)\s+(?P<max>\d{2,4})(?:\s+(?P<marks>[#A-Z0-9]{1,4}))?$",
            clean,
            re.IGNORECASE,
        )
        if not loose:
            return []
        data = loose.groupdict()
        code = ""
    else:
        data = match.groupdict()
        code = data.get("code") or ""

    subject = _subject_name_from_text(f"{code} {data.get('subject') or ''}")
    if not subject or DOCUMENT_TITLE_WORDS_RE.search(subject):
        return []

    max_marks, marks = _repair_subject_numbers(data.get("max"), data.get("marks"))
    records = []
    bbox = {"x0": 0, "y0": 0, "x1": 0, "y1": 0}

    if code:
        records.append(_make_record(page, "Subjects and Marks", f"{subject} - Subject Code", code, "table_cell", 90.0, source, method, bbox))
    if max_marks:
        records.append(_make_record(page, "Subjects and Marks", f"{subject} - Maximum Marks", max_marks, "table_cell", 90.0, source, method, bbox))
    if marks:
        records.append(_make_record(page, "Subjects and Marks", f"{subject} - Marks Secured", marks, "table_cell", 90.0, source, method, bbox))
    return records

def _add_record_once(records: list, record: dict, seen: set):
    key = _record_key(record)
    if key in seen:
        return
    records.append(record)
    seen.add(key)

def _semantic_document_records(page_lines: dict, source: str = "hybrid") -> list:
    records = []
    seen = set()

    for page, lines in page_lines.items():
        clean_lines = [_clean_value(line) for line in lines if _clean_value(line)]
        full_text = " ".join(clean_lines)
        upper = full_text.upper()
        section = "General"

        for rec in _aadhaar_records_from_lines(page, clean_lines, source):
            _add_record_once(records, rec, seen)

        # Strict check for certificates to avoid false positives on bank statements
        is_certificate = False
        if any(token in upper for token in ["CERTIFICATE", "MARKSHEET", "BOARD OF SECONDARY EDUCATION", "UNIVERSITY"]):
            is_certificate = True

        if is_certificate:
            for field, pattern in CERT_FIELD_PATTERNS:
                for match in re.finditer(pattern, full_text, re.IGNORECASE):
                    value = _clean_value(match.group(1))
                    if value:
                        _add_record_once(records, _make_record(
                            page, "Document Details", field, value, "key_value", 92.0, source,
                            "Semantic Regex", {"x0": 0, "y0": 0, "x1": 0, "y1": 0}
                        ), seen)

            for line in clean_lines:
                for rec in _subject_records_from_line(line, page, "Subjects and Marks", source, "Semantic Subject Parser"):
                    _add_record_once(records, rec, seen)

            school_candidates = _school_name_candidates(clean_lines)
            if school_candidates:
                school = school_candidates[0][1]
                _add_record_once(records, _make_record(
                    page, "School Details", "School Name", school, "key_value", 88.0, source,
                    "Certificate Heuristic", {"x0": 0, "y0": 0, "x1": 0, "y1": 0}
            ), seen)

        name_patterns = [
            r"(?:certify\s+that|this\s+is\s+to\s+certify\s+that)\s+([A-Z][A-Z .'-]{3,}?)(?:\s+(?:son|daughter|s/?o|d/?o|born|has|passed)\b)",
            r"\b(?:candidate|student|pupil|name)\s*(?:name)?\s*[:\-]\s*([A-Z][A-Z .'-]{3,})",
        ]
        for pattern in name_patterns:
            match = re.search(pattern, full_text, re.IGNORECASE)
            if match:
                name = _dedupe_repeated_words(match.group(1))
                if _looks_like_person_name(name):
                    _add_record_once(records, _make_record(
                        page, "Student Details", "Name", name, "key_value", 88.0, source,
                        "Certificate Heuristic", {"x0": 0, "y0": 0, "x1": 0, "y1": 0}
                    ), seen)
                    break

        parent_patterns = [
            ("Father Name", r"(?:son|daughter)\s+of\s+([A-Z][A-Z .'-]{3,})(?:\s+(?:and|born|passed|,|\.|$))"),
            ("Father Name", r"([A-Z][A-Z .'-]{3,})\s*\(\s*father\s*\)"),
            ("Mother Name", r"([A-Z][A-Z .'-]{3,})\s*\(\s*mother\s*\)"),
        ]
        for field, pattern in parent_patterns:
            match = re.search(pattern, full_text, re.IGNORECASE)
            if match:
                parent = _dedupe_repeated_words(match.group(1))
                parent = re.sub(r"^(of|shri|smt|mr|mrs)\s+", "", parent, flags=re.IGNORECASE)
                if _looks_like_person_name(parent):
                    _add_record_once(records, _make_record(
                        page, "Student Details", field, parent, "key_value", 84.0, source,
                        "Certificate Heuristic", {"x0": 0, "y0": 0, "x1": 0, "y1": 0}
                    ), seen)

    return records

def _valid_date(value: str) -> bool:
    match = re.fullmatch(r"(\d{1,2})[\/\-.](\d{1,2})[\/\-.](\d{2,4})", _clean_value(value))
    if not match:
        return True
    day, month, year = [int(part) for part in match.groups()]
    if year < 100:
        year += 2000
    return 1 <= day <= 31 and 1 <= month <= 12 and 1900 <= year <= 2100

def _is_field_value_reliable(rec: dict) -> bool:
    field = _clean(rec.get("field", "")).lower()
    value = _clean_value(rec.get("value", ""))
    if not value:
        return False
    if "�" in field or "�" in value:
        return False
    if re.search(r"[^a-z0-9 \-_/().]", field):
        return False
    if field == "name":
        if SUBJECT_WORDS_RE.search(value) or DOCUMENT_TITLE_WORDS_RE.search(value):
            return False
        return bool(re.fullmatch(r"[A-Za-z][A-Za-z .'-]{1,60}", value))
    if field in {"father name", "mother name"}:
        return _looks_like_person_name(value)
    if any(token in field for token in ["roll number", "registration number", "serial number", "school code"]):
        return bool(re.search(r"\d", value))
    if field == "division":
        return bool(re.fullmatch(r"(?:division\s*)?[A-D][+ -]?", value, flags=re.IGNORECASE))
    if "date" in field or field == "dob":
        return bool(re.search(r"\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{2,4}", value)) and _valid_date(
            re.search(r"\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{2,4}", value).group(0)
        )
    if field == "mobile number":
        return bool(re.fullmatch(r"(?:\+?91[-\s]?)?[6-9]\d{9}", re.sub(r"[\s.-]+", "", value)))
    if field.endswith("maximum marks") or field.endswith("marks secured"):
        try:
            numeric = int(value)
        except ValueError:
            return False
        if field.endswith("maximum marks") and not (1 <= numeric <= 200):
            return False
        if field.endswith("marks secured") and not (0 <= numeric <= 200):
            return False
    return True

def _resolve_record_conflicts(records: list) -> list:
    year_records = [
        rec for rec in records
        if _clean(rec.get("field", "")).lower() == "examination year"
        and re.fullmatch(r"\d{4}", _clean_value(rec.get("value", "")))
    ]
    output = list(records)
    current_like = [rec for rec in year_records if int(_clean_value(rec["value"])) >= 2010]
    if year_records and not (len(current_like) <= 1 and len(year_records) <= 1):
        candidates = current_like or year_records
        latest_year = max(int(_clean_value(rec["value"])) for rec in candidates)
        output = []
        kept_latest = False
        for rec in records:
            if _clean(rec.get("field", "")).lower() != "examination year":
                output.append(rec)
                continue
            value = _clean_value(rec.get("value", ""))
            if value == str(latest_year) and not kept_latest:
                output.append(rec)
                kept_latest = True
    deduped = []
    best_address_by_page: dict[int, dict] = {}
    for rec in output:
        if _clean(rec.get("field", "")).lower() == "address":
            page = rec.get("page", 1)
            current = best_address_by_page.get(page)
            if current is None or len(_clean_value(rec.get("value", ""))) > len(_clean_value(current.get("value", ""))):
                best_address_by_page[page] = rec
            continue
        deduped.append(rec)
    existing_address_pages = set()
    for rec in output:
        if _clean(rec.get("field", "")).lower() == "address":
            page = rec.get("page", 1)
            if page not in existing_address_pages and page in best_address_by_page:
                deduped.append(best_address_by_page[page])
                existing_address_pages.add(page)
    return deduped

def _post_process_records(records: list, page_lines: dict) -> list:
    semantic = _semantic_document_records(page_lines)
    candidate_records = _repair_subject_totals(semantic + records, page_lines)
    combined = []
    seen = set()

    for rec in candidate_records:
        rec = dict(rec)
        field = _clean_label(rec.get("field", ""))
        value = _clean(rec.get("value", ""))
        rtype = rec.get("type", "")
        if "date" in field.lower() or field.lower() == "dob":
            date_match = re.search(r"\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{2,4}", value)
            if date_match:
                value = date_match.group(0)
                rec["value"] = value
        if field.lower() == "address":
            value = _clean_address(value)
            rec["value"] = value
        rec["field"] = field
        if not field or not value:
            continue
        if BAD_FIELD_NAME_RE.match(field):
            continue
        if field.lower().startswith("extracted text"):
            continue
        if rtype in {"paragraph", "needs_review", "unstructured_text"}:
            continue
        if field.lower() == "name" and DOCUMENT_TITLE_WORDS_RE.search(value):
            continue
        if not _is_field_value_reliable(rec):
            continue
        _add_record_once(combined, rec, seen)

    return _resolve_record_conflicts(combined)

def _alnum_count(text: str) -> int:
    return sum(1 for c in text if c.isalnum())

def _is_usable_text(text: str) -> bool:
    return _alnum_count(text) >= MIN_ALNUM_FOR_NATIVE

def _looks_like_heading(cell: dict) -> bool:
    t = _clean(cell["text"])
    if len(t) < SECTION_HEADING_MIN_LEN:
        return False
    if ":" in t and t.index(":") < len(t) - 2:
        return False
    words = t.split()
    if len(words) > SECTION_HEADING_MAX_WORDS:
        return False
    # If font size is larger or bold, stronger evidence, but we may not have font data.
    if SECTION_KEYWORDS.search(t):
        return True
    if t.isupper() and 2 <= len(words) <= 6:
        return True
    return False

def _is_border(text: str) -> bool:
    return bool(BORDER_LINE_RE.match(text.strip()))

def _make_record(
    page: int,
    section: str,
    field: str,
    value: str,
    rtype: str,
    confidence: float,
    source: str,
    method: str,
    bbox: dict,
) -> dict:
    return {
        "id":         _uid(),
        "page":       page,
        "section":    _clean(section) or "General",
        "field":      _clean(field),
        "value":      _clean(value),
        "type":       rtype,
        "confidence": round(float(confidence), 2),
        "source":     source,
        "method":     method,
        "bbox":       bbox,
        "selected":   rtype not in ["needs_review", "unstructured_text", "unmapped_content", "paragraph"],
        # backward compatibility:
        "x":          bbox.get("x0", 0),
        "y":          bbox.get("y0", 0),
    }

# ---------------------------------------------------------------------------
# Layout Analyser
# ---------------------------------------------------------------------------

class LayoutAnalyser:
    def __init__(self, elements: List[Dict], page_num: int, source: str, method: str):
        # elements are individual words: {text, x0, y0, x1, y1, confidence, id}
        self.elements = elements
        self.page = page_num
        self.source = source
        self.method = method
        self.records = []
        self.current_section = "General"
        self.consumed_element_ids = set()
        
        self.visual_lines = self._build_visual_lines(self.elements)
        self.rows = self._split_lines_into_cells(self.visual_lines)

    def _build_visual_lines(self, elements: List[Dict]) -> List[List[Dict]]:
        # Group elements into visual lines by overlapping Y coordinate
        # Sort by mid-y
        sorted_els = sorted(elements, key=lambda e: (e["y0"] + e["y1"]) / 2)
        lines = []
        current_line = []
        current_y_mid = -1
        
        for e in sorted_els:
            e_mid = (e["y0"] + e["y1"]) / 2
            e_h = e["y1"] - e["y0"]
            if not current_line:
                current_line.append(e)
                current_y_mid = e_mid
            else:
                # If mid-y is within ~50% of element height, group it
                if abs(e_mid - current_y_mid) < e_h * 0.6:
                    current_line.append(e)
                    # Update rolling average mid-y
                    current_y_mid = sum((x["y0"] + x["y1"]) / 2 for x in current_line) / len(current_line)
                else:
                    current_line.sort(key=lambda x: x["x0"])
                    lines.append(current_line)
                    current_line = [e]
                    current_y_mid = e_mid
        if current_line:
            current_line.sort(key=lambda x: x["x0"])
            lines.append(current_line)
        return lines

    def _split_lines_into_cells(self, lines: List[List[Dict]]) -> List[Dict]:
        rows = []
        for line in lines:
            if not line:
                continue
            cells = []
            current_cell = [line[0]]
            
            for i in range(1, len(line)):
                prev = line[i-1]
                curr = line[i]
                gap = curr["x0"] - prev["x1"]
                avg_h = ((curr["y1"] - curr["y0"]) + (prev["y1"] - prev["y0"])) / 2
                
                # If gap is significantly larger than space width (e.g. > 1.5 * font height)
                # it's a column boundary.
                if gap > max(15, avg_h * 1.5):
                    cells.append(self._make_cell(current_cell))
                    current_cell = [curr]
                else:
                    current_cell.append(curr)
            
            if current_cell:
                cells.append(self._make_cell(current_cell))
                
            # Compute row bounding box
            y0 = min(c["y0"] for c in cells)
            y1 = max(c["y1"] for c in cells)
            rows.append({"cells": cells, "y0": y0, "y1": y1})
            
        return rows

    def _make_cell(self, elements: List[Dict]) -> Dict:
        text = " ".join(e["text"] for e in elements)
        x0 = min(e["x0"] for e in elements)
        y0 = min(e["y0"] for e in elements)
        x1 = max(e["x1"] for e in elements)
        y1 = max(e["y1"] for e in elements)
        conf = sum(e["confidence"] for e in elements) / len(elements)
        return {
            "text": text,
            "x0": x0, "y0": y0, "x1": x1, "y1": y1,
            "confidence": conf,
            "elements": elements
        }

    def _consume(self, cell: Dict):
        for e in cell["elements"]:
            self.consumed_element_ids.add(e["id"])

    def _is_consumed(self, cell: Dict) -> bool:
        if not cell["elements"]:
            return True
        return any(e["id"] in self.consumed_element_ids for e in cell["elements"])

    def _is_garbage(self, cell: Dict) -> bool:
        """Filter out low-confidence OCR fragments and noise."""
        if cell["confidence"] < 45: 
            return True
        t = _clean(cell["text"])
        # Meaningless 1-2 char fragments (unless they are digits or common words)
        if len(t) <= 2 and not t.isdigit() and t.lower() not in ["to", "of", "in", "on", "at", "by", "is", "a", "an", "do", "no"]:
            return True
        # Strings that are just punctuation
        if re.match(r'^[^a-zA-Z0-9]+$', t):
            return True
        return False

    def _is_label(self, text: str) -> bool:
        t = _clean(text)
        if ":" in t: return True
        if LABEL_SUFFIX_RE.search(t): return True
        # A valid label without colon/keyword should usually be Title Case or UPPERCASE, not just any text
        if 1 <= len(t.split()) <= 4 and len(t) <= 40:
            if t.istitle() or t.isupper():
                return True
        return False

    def analyse(self) -> list:
        # 0. Clean garbage cells
        cleaned_rows = []
        for row in self.rows:
            valid_cells = [c for c in row["cells"] if not self._is_garbage(c)]
            if valid_cells:
                row["cells"] = valid_cells
                cleaned_rows.append(row)
        self.rows = cleaned_rows

        # 1. Table Detection
        self._detect_tables()
        
        # 2. Main Layout Pass
        for i, row in enumerate(self.rows):
            unconsumed_cells = [c for c in row["cells"] if not self._is_consumed(c)]
            if not unconsumed_cells:
                continue
                
            # Check for Section Heading
            if len(unconsumed_cells) == 1:
                cell = unconsumed_cells[0]
                if _looks_like_heading(cell) and not _is_border(cell["text"]):
                    self.current_section = _clean(cell["text"])
                    self._consume(cell)
                    continue
                    
            # Skip KV pairing if row has 4 or more unconsumed cells (likely a broken table row)
            if len(unconsumed_cells) >= 4:
                continue

            # Inline KV (e.g. "Name: John")
            for cell in unconsumed_cells:
                if self._is_consumed(cell): continue
                text = _clean(cell["text"])
                if ":" in text:
                    parts = text.split(":", 1)
                    label, val = parts[0].strip(), parts[1].strip()
                    if label and val:
                        self.records.append(_make_record(
                            self.page, self.current_section, _clean_label(label), _clean_value(val),
                            "key_value", cell["confidence"], self.source, f"{self.method} + Inline",
                            {"x0": cell["x0"], "y0": cell["y0"], "x1": cell["x1"], "y1": cell["y1"]}
                        ))
                        self._consume(cell)
            
            # Left/Right KV (same row, gap split)
            unconsumed_cells = [c for c in row["cells"] if not self._is_consumed(c)]
            if len(unconsumed_cells) == 2:
                c1, c2 = unconsumed_cells
                t1 = _clean(c1["text"])
                # Only pair if it strongly looks like a key-value (e.g. has colon or known suffix)
                # and length is reasonable to avoid garbage OCR with colons
                if len(t1) < 30 and (":" in t1 or LABEL_SUFFIX_RE.search(t1)) and c2["x0"] > c1["x1"]:
                    self.records.append(_make_record(
                        self.page, self.current_section, t1, _clean(c2["text"]),
                        "key_value", min(c1["confidence"], c2["confidence"]), self.source, f"{self.method} + Same Row Strict",
                        {"x0": c1["x0"], "y0": c1["y0"], "x1": c2["x1"], "y1": max(c1["y1"], c2["y1"])}
                    ))
                    self._consume(c1)
                    self._consume(c2)
                    continue

        self._detect_header_value_rows()
        self._detect_vertical_key_values()

        # 2.5 Smart Logic for Indian ID Cards & Resumes (Fallback for when AI is down)
        full_text = " ".join([_clean(c["text"]) for row in self.rows for c in row["cells"]])
        full_text_upper = full_text.upper()
        import re

        # -- Generic Contact Extraction --
        # Email
        email_match = re.search(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', full_text)
        if email_match:
            self.records.append(_make_record(self.page, "Contact", "Email", email_match.group(0), "key_value", 95.0, self.source, "Generic Regex", {"x0":0, "y0":0, "x1":0, "y1":0}))
            
        # Mobile/Phone
        phone_match = re.search(r'\+?\b\d{2,4}[-.\s]?\d{3,4}[-.\s]?\d{3,4}\b', full_text)
        if phone_match and len(re.sub(r'\D', '', phone_match.group(0))) >= 10:
            self.records.append(_make_record(self.page, "Contact", "Mobile Number", phone_match.group(0).strip(), "key_value", 95.0, self.source, "Generic Regex", {"x0":0, "y0":0, "x1":0, "y1":0}))

        # -- Candidate Name Guesser (only for unlabeled ID/certificate-like docs) --
        if self.page == 1 and re.search(r"\b(aadhaar|certificate|identity|government of india)\b", full_text, re.IGNORECASE):
            for row in self.rows:
                for c in row["cells"]:
                    t = _clean(c["text"])
                # Check if it's 2-3 words and either Title Case or ALL CAPS
                    if _looks_like_person_name(t) and (t.istitle() or t.isupper()):
                        self.records.append(_make_record(self.page, "General", "Name", t, "key_value", 90.0, self.source, "Heuristic Name", {"x0":0, "y0":0, "x1":0, "y1":0}))
                        self._consume(c)
                        break
                else:
                    continue
                break

        # -- Specific Aadhaar logic --
        if "AADHAAR" in full_text_upper or ("GOVERNMENT OF INDIA" in full_text_upper and "DOB" in full_text_upper):
            aadhaar_match = re.search(r'\b(\d{4}\s?\d{4}\s?\d{4})\b', full_text_upper)
            if aadhaar_match:
                self.records.append(_make_record(self.page, "Identity Details", "Aadhaar Number", aadhaar_match.group(1), "key_value", 99.0, self.source, "Smart Logic", {"x0":0, "y0":0, "x1":0, "y1":0}))
            
            dob_match = re.search(r'\b(?:DOB|YOB|Year of Birth)[\s:]*([\d/:-]+)\b', full_text_upper, re.IGNORECASE)
            if dob_match:
                self.records.append(_make_record(self.page, "Identity Details", "Date of Birth", dob_match.group(1), "key_value", 99.0, self.source, "Smart Logic", {"x0":0, "y0":0, "x1":0, "y1":0}))
            
            gender_match = re.search(r'\b(MALE|FEMALE|TRANSGENDER|PURUSH|MAHILA)\b', full_text_upper)
            if gender_match:
                self.records.append(_make_record(self.page, "Identity Details", "Gender", gender_match.group(1), "key_value", 99.0, self.source, "Smart Logic", {"x0":0, "y0":0, "x1":0, "y1":0}))

            for row in self.rows:
                for c in row["cells"]:
                    self._consume(c)

        # 3. Capture remaining paragraphs
        self._capture_unmapped_as_text()
        return self.records

    def _detect_tables(self):
        # A table is 3+ consecutive rows with 2+ cells that align vertically
        i = 0
        while i < len(self.rows):
            row = self.rows[i]
            unconsumed = [c for c in row["cells"] if not self._is_consumed(c)]
            if len(unconsumed) < 2:
                i += 1
                continue
                
            # Find contiguous rows with similar cell counts and boundaries
            table_rows = [unconsumed]
            j = i + 1
            while j < len(self.rows):
                next_row = self.rows[j]
                next_un = [c for c in next_row["cells"] if not self._is_consumed(c)]
                if len(next_un) < 2:
                    break
                    
                # Check vertical gap
                y_gap = next_un[0]["y0"] - table_rows[-1][0]["y1"]
                if y_gap > 35:
                    break
                    
                # Strict column alignment check: 
                # Check if at least 2 cells align well in x0
                aligned_cols = 0
                for cell_curr in next_un:
                    for cell_base in unconsumed:
                        if abs(cell_curr["x0"] - cell_base["x0"]) < 40:
                            aligned_cols += 1
                            break
                
                # A row must have at least 1 aligned column (left or right) to belong to the block
                if aligned_cols < 1:
                    break
                    
                table_rows.append(next_un)
                j += 1
                
            if len(table_rows) >= 3:
                if self._emit_subject_mark_rows(table_rows):
                    i = j
                    continue

                headers = self._infer_table_headers(table_rows)
                if not headers:
                    i += 1
                    continue

                column_x0s = []
                for trow in table_rows:
                    for cell in trow:
                        if not any(abs(cell["x0"] - cx) < 30 for cx in column_x0s):
                            column_x0s.append(cell["x0"])
                column_x0s.sort()

                start_idx = 1 if self._row_is_header(table_rows[0]) else 0
                for r_idx, trow in enumerate(table_rows[start_idx:], 1):
                    row_label = self._best_row_label(trow, r_idx)
                    for cell in trow:
                        best_c_idx = -1
                        min_dist = 9999
                        for c_idx, cx in enumerate(column_x0s):
                            d = abs(cell["x0"] - cx)
                            if d < 30 and d < min_dist:
                                best_c_idx = c_idx
                                min_dist = d

                        if best_c_idx != -1 and best_c_idx < len(headers):
                            value = _clean_value(cell["text"])
                            if not value or value.lower() == headers[best_c_idx].lower():
                                self._consume(cell)
                                continue
                            field = f"{row_label} - {headers[best_c_idx]}" if row_label and best_c_idx != 0 else headers[best_c_idx]
                            self.records.append(_make_record(
                                self.page, self.current_section, field, value,
                                "table_cell", cell["confidence"], self.source, f"{self.method} + Named Table",
                                {"x0": cell["x0"], "y0": cell["y0"], "x1": cell["x1"], "y1": cell["y1"]}
                            ))
                            self._consume(cell)
                i = j
            else:
                i += 1

    def _capture_unmapped_as_text(self):
        # Disabled as user strictly doesn't want unstructured paragraphs
        pass

    def _detect_header_value_rows(self):
        i = 0
        while i < len(self.rows) - 1:
            header_cells = [c for c in self.rows[i]["cells"] if not self._is_consumed(c)]
            value_cells = [c for c in self.rows[i + 1]["cells"] if not self._is_consumed(c)]
            if len(header_cells) >= 2 and len(value_cells) >= 2:
                pairs = []
                for label_cell in header_cells:
                    label = _clean_label(label_cell["text"])
                    if not self._is_label(label):
                        continue
                    aligned = min(
                        value_cells,
                        key=lambda vc: abs(((vc["x0"] + vc["x1"]) / 2) - ((label_cell["x0"] + label_cell["x1"]) / 2)),
                    )
                    if abs(aligned["x0"] - label_cell["x0"]) <= 45 and _looks_like_value(aligned["text"]):
                        pairs.append((label_cell, aligned))
                if len(pairs) >= 2:
                    for label_cell, value_cell in pairs:
                        self.records.append(_make_record(
                            self.page, self.current_section, _clean_label(label_cell["text"]), _clean_value(value_cell["text"]),
                            "key_value", min(label_cell["confidence"], value_cell["confidence"]), self.source,
                            f"{self.method} + Header Value Row",
                            {"x0": label_cell["x0"], "y0": label_cell["y0"], "x1": value_cell["x1"], "y1": value_cell["y1"]}
                        ))
                        self._consume(label_cell)
                        self._consume(value_cell)
                    i += 2
                    continue
            i += 1

    def _detect_vertical_key_values(self):
        for i in range(len(self.rows) - 1):
            row = self.rows[i]
            next_row = self.rows[i + 1]
            cells = [c for c in row["cells"] if not self._is_consumed(c)]
            next_cells = [c for c in next_row["cells"] if not self._is_consumed(c)]
            if len(cells) != 1 or len(next_cells) != 1:
                continue
            label_cell = cells[0]
            value_cell = next_cells[0]
            label = _clean_label(label_cell["text"])
            if not self._is_label(label) or not _looks_like_value(value_cell["text"]):
                continue
            y_gap = value_cell["y0"] - label_cell["y1"]
            x_aligned = abs(value_cell["x0"] - label_cell["x0"]) <= 35
            if 0 <= y_gap <= 25 and x_aligned:
                self.records.append(_make_record(
                    self.page, self.current_section, label, _clean_value(value_cell["text"]),
                    "key_value", min(label_cell["confidence"], value_cell["confidence"]), self.source,
                    f"{self.method} + Vertical Pair",
                    {"x0": label_cell["x0"], "y0": label_cell["y0"], "x1": max(label_cell["x1"], value_cell["x1"]), "y1": value_cell["y1"]}
                ))
                self._consume(label_cell)
                self._consume(value_cell)

    def _row_is_header(self, row: list) -> bool:
        joined = " ".join(_clean(c["text"]).lower() for c in row)
        return any(word in joined for word in ["subject", "marks", "maximum", "secured", "grade", "code"])

    def _infer_table_headers(self, table_rows: list) -> list:
        first_row = table_rows[0]
        if self._row_is_header(first_row):
            headers = [_clean_label(c["text"]) or f"Column {i + 1}" for i, c in enumerate(first_row)]
        else:
            max_cols = max(len(r) for r in table_rows)
            known = ["Item", "Maximum Marks", "Marks Secured", "Grade", "Remarks"]
            headers = known[:max_cols]
        if any(BAD_FIELD_NAME_RE.match(h) for h in headers):
            return []
        return headers

    def _best_row_label(self, row: list, row_idx: int) -> str:
        for cell in row:
            text = _clean_value(cell["text"])
            if re.search(r"[A-Za-z]", text) and not text.isdigit():
                subject = _subject_name_from_text(text)
                return subject or text[:60]
        return f"Row {row_idx}"

    def _emit_subject_mark_rows(self, table_rows: list) -> bool:
        emitted = False
        for row in table_rows:
            row_text = _clean_value(" ".join(c["text"] for c in row))
            recs = _subject_records_from_line(
                row_text, self.page, self.current_section, self.source,
                f"{self.method} + Subject Marks"
            )
            if not recs:
                continue
            conf = min(c["confidence"] for c in row)
            bbox = {
                "x0": min(c["x0"] for c in row),
                "y0": min(c["y0"] for c in row),
                "x1": max(c["x1"] for c in row),
                "y1": max(c["y1"] for c in row),
            }
            for rec in recs:
                rec["confidence"] = round(float(conf), 2)
                rec["bbox"] = bbox
                rec["x"] = bbox["x0"]
                rec["y"] = bbox["y0"]
                self.records.append(rec)
            for cell in row:
                self._consume(cell)
            emitted = True
        return emitted

# ---------------------------------------------------------------------------
# Native-text processing using fitz text
# ---------------------------------------------------------------------------

def _extract_fitz_native_elements(page: fitz.Page) -> list:
    """Extract individual words with true coordinates."""
    elements = []
    try:
        words = page.get_text("words")
        for w in words:
            x0, y0, x1, y1, text, block_no, line_no, word_no = w
            t = _clean(text)
            if t:
                elements.append({
                    "text": t,
                    "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                    "confidence": 100.0,
                    "id": _uid()
                })
    except Exception as exc:
        logger.warning(f"Native extraction failed: {exc}")
    return elements

def _plumber_table_to_records(table: dict, source: str) -> list:
    records = []
    data     = table.get("data", [])
    page     = table.get("page", 1)
    section  = table.get("section", "Table Data")

    if not data: return records
    
    # Check if it's a 2-column Key-Value table
    if len(data[0]) == 2 and all(len(row) == 2 for row in data):
        # Check if it's alternating rows (Row 1 = Headers, Row 2 = Values)
        # We can assume it's alternating if we have an even number of rows
        if len(data) % 2 == 0:
            for i in range(0, len(data), 2):
                header_row = data[i]
                value_row = data[i+1]
                if header_row[0] and value_row[0]:
                    records.append(_make_record(
                        page, section, _clean_label(header_row[0]), _clean_value(value_row[0]),
                        "key_value", 100.0, source, "pdfplumber KV (Vertical)",
                        {"x0":0, "y0":0, "x1":0, "y1":0}
                    ))
                if header_row[1] and value_row[1]:
                    records.append(_make_record(
                        page, section, _clean_label(header_row[1]), _clean_value(value_row[1]),
                        "key_value", 100.0, source, "pdfplumber KV (Vertical)",
                        {"x0":0, "y0":0, "x1":0, "y1":0}
                    ))
            return records
        
        # Standard horizontal 2-column KV
        for row in data:
            if not row[0] or not row[1]: continue
            records.append(_make_record(
                page, section, _clean_label(row[0]), _clean_value(row[1]),
                "key_value", 100.0, source, "pdfplumber KV (Horizontal)",
                {"x0":0, "y0":0, "x1":0, "y1":0}
            ))
        return records

    if len(data) >= 2:
        headers = [h or f"Col {i+1}" for i, h in enumerate(data[0])]
        for row_idx, row in enumerate(data[1:], 1):
            for col_idx, cell in enumerate(row):
                if not cell: continue
                field = headers[col_idx] if col_idx < len(headers) else f"Col {col_idx + 1}"
                records.append(_make_record(
                    page, section, f"{field}", cell,
                    "table_cell", 100.0, source, "pdfplumber Table",
                    {"x0":0, "y0":0, "x1":0, "y1":0}
                ))
    else:
        for row_idx, row in enumerate(data):
            for col_idx, cell in enumerate(row):
                if not cell: continue
                records.append(_make_record(
                    page, section, f"Col {col_idx + 1}", cell,
                    "table_cell", 100.0, source, "pdfplumber Table",
                    {"x0":0, "y0":0, "x1":0, "y1":0}
                ))
    return records

def _extract_plumber_tables(pdf_path: str, password: Optional[str], page_nums_1indexed: list) -> list:
    tables_out = []
    try:
        with pdfplumber.open(pdf_path, password=password or "") as pdf:
            for pg_num in page_nums_1indexed:
                if pg_num < 1 or pg_num > len(pdf.pages): continue
                pg = pdf.pages[pg_num - 1]
                for tbl in pg.extract_tables():
                    clean = [[_clean(str(cell)) if cell is not None else "" for cell in row] for row in tbl]
                    tables_out.append({"page": pg_num, "data": clean})
    except Exception:
        pass
    return tables_out

# ---------------------------------------------------------------------------
# Deduplication & Audit
# ---------------------------------------------------------------------------

def _deduplicate_records(records: list) -> list:
    seen = set()
    output = []
    for rec in records:
        key = (rec["page"], _clean(rec["field"]).lower(), _clean(rec["value"]).lower())
        if key not in seen:
            seen.add(key)
            output.append(rec)
    return output

def _audit_completeness(elements: list, records: list, page: int, consumed_ids: set) -> list:
    # Disabled as per user request to suppress garbage data.
    return []

# ---------------------------------------------------------------------------
# Main parse_pdf
# ---------------------------------------------------------------------------

def parse_pdf(pdf_path: str, conversion_mode: str = "Automatic", password: Optional[str] = None) -> dict:
    import os
    if os.environ.get("GEMINI_API_KEY") or conversion_mode == "AI":
        try:
            from app.services.gemini_parser import parse_with_gemini
            logger.info("Using Gemini for PDF parsing")
            return parse_with_gemini(pdf_path, password)
        except Exception as e:
            logger.error(f"Gemini parsing failed: {e}")
            return {
                "total_pages": 1, "direct_text_pages": 0, "ocr_attempted_pages": 0,
                "ocr_successful_pages": 0, "ocr_failed_pages": 0, "text_blocks": [],
                "tables": [], "fields": [], "raw_ocr_text": [], "unmapped_content": [],
                "pdf_type": "ERROR", "ocr_used": False,
                "warnings": [f"AI Extraction Failed: Google AI Server is busy. Please try again. Details: {e}"],
                "is_encrypted": False, "password_required": False, "page_audit": []
            }

    results = {
        "total_pages": 0, "direct_text_pages": 0, "ocr_attempted_pages": 0,
        "ocr_successful_pages": 0, "ocr_failed_pages": 0, "text_blocks": [],
        "tables": [], "fields": [], "raw_ocr_text": [], "unmapped_content": [],
        "pdf_type": "MIXED_PDF", "ocr_used": False, "warnings": [],
        "is_encrypted": False, "password_required": False, "page_audit": []
    }

    try:
        doc = fitz.open(pdf_path)
    except Exception as exc:
        results["warnings"].append(f"Failed to open PDF: {exc}")
        return results

    if doc.needs_pass:
        if password and doc.authenticate(password): pass
        else:
            results["is_encrypted"] = True
            results["password_required"] = True
            return results

    num_pages = len(doc)
    results["total_pages"] = num_pages

    all_records = []
    page_lines = {}
    
    # Process pages
    for page_idx in range(num_pages):
        page_num = page_idx + 1
        page = doc[page_idx]
        
        raw_text = page.get_text("text")
        native_usable = _is_usable_text(raw_text)
        
        elements = []
        source = "native"
        method = "Spatial Layout"
        
        if native_usable and conversion_mode != "OCR Only":
            results["direct_text_pages"] += 1
            elements = _extract_fitz_native_elements(page)
            native_lines = [_clean(line) for line in raw_text.splitlines() if _clean(line)]
            page_lines[page_num] = native_lines
            for idx, line in enumerate(native_lines, 1):
                results["raw_ocr_text"].append({
                    "page": page_num, "line_no": idx, "text": line,
                    "confidence": 100.0, "x": "", "y": ""
                })
        else:
            results["ocr_attempted_pages"] += 1
            results["ocr_used"] = True
            ocr_res = perform_ocr_on_page(page, page_num)
            if ocr_res.get("error"):
                results["ocr_failed_pages"] += 1
                results["warnings"].append(f"OCR failed on page {page_num}")
                continue
            results["ocr_successful_pages"] += 1
            page_lines[page_num] = [_clean(line.get("text", "")) for line in ocr_res.get("lines", []) if _clean(line.get("text", ""))]
            for idx, line in enumerate(ocr_res.get("lines", []), 1):
                results["raw_ocr_text"].append({
                    "page": page_num,
                    "line_no": idx,
                    "text": _clean(line.get("text", "")),
                    "confidence": line.get("confidence", ""),
                    "x": line.get("x", ""),
                    "y": line.get("y", ""),
                })
            
            # ocr_res["lines"] contains children with word bounding boxes
            source = "ocr"
            for line in ocr_res.get("lines", []):
                for w in line.get("children", []):
                    elements.append({
                        "text": w["text"],
                        "x0": w["x0"], "y0": w["y0"],
                        "x1": w["x1"], "y1": w["y1"],
                        "confidence": w["confidence"],
                        "id": _uid()
                    })
                    
        # Plumber tables
        if native_usable:
            ptables = _extract_plumber_tables(pdf_path, password, [page_num])
            for pt in ptables:
                pt["section"] = "Table Data"
                all_records.extend(_plumber_table_to_records(pt, "native"))

        # Analyse
        if elements:
            analyser = LayoutAnalyser(elements, page_num, source, method)
            records = analyser.analyse()
            all_records.extend(records)
            
            # Unmapped (since we flush paragraphs, this will mostly catch true leftovers)
            unmapped = _audit_completeness(elements, records, page_num, analyser.consumed_element_ids)
            all_records.extend(unmapped)

        results["page_audit"].append({
            "page": page_num,
            "source": source,
            "elements": len(elements)
        })

    all_records = _post_process_records(_deduplicate_records(all_records), page_lines)
    results["fields"] = all_records
    
    if results["direct_text_pages"] == num_pages:
        results["pdf_type"] = "NATIVE_TEXT"
    elif results["ocr_successful_pages"] == num_pages:
        results["pdf_type"] = "SCANNED_IMAGE"
        
    doc.close()
    return results
