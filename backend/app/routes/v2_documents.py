from fastapi import APIRouter, UploadFile, File, HTTPException
import shutil
import os
import uuid
import json
from pydantic import BaseModel
from typing import List, Optional

from app.services.document_processor import DocumentProcessor
from app.services.document_understanding import DocumentUnderstanding

router = APIRouter(prefix="/v2/documents", tags=["V2 Documents API"])

# In-memory storage for demonstration purposes (as requested: "do not store permanently unless required")
db_documents = {}

class ExtractedField(BaseModel):
    field_name: str
    value: Optional[str]
    confidence: float
    status: str
    page: int

class DocumentResponse(BaseModel):
    document_type: str
    pages: int
    fields: List[ExtractedField]

# Initialize engines
processor = None
understanding = None

def get_engines():
    global processor, understanding
    if not processor:
        processor = DocumentProcessor()
    if not understanding:
        understanding = DocumentUnderstanding()
    return processor, understanding

@router.post("/upload")
async def upload_document(file: UploadFile = File(...)):
    doc_id = str(uuid.uuid4())
    os.makedirs("uploads/v2", exist_ok=True)
    file_path = f"uploads/v2/{doc_id}.pdf"
    
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
        
    db_documents[doc_id] = {
        "id": doc_id,
        "filename": file.filename,
        "path": file_path,
        "status": "uploaded",
        "data": None
    }
    return {"id": doc_id, "filename": file.filename, "status": "uploaded"}

@router.post("/extract/{doc_id}")
async def extract_document(doc_id: str):
    if doc_id not in db_documents:
        raise HTTPException(status_code=404, detail="Document not found")
        
    doc = db_documents[doc_id]
    proc, understand = get_engines()
    
    # 1. Image Preprocessing & OCR
    ocr_results = proc.process_pdf(doc["path"])
    
    # 2. Document Understanding & Validation
    structured_json = understand.analyze_layout(ocr_results)
    
    doc["data"] = structured_json
    doc["status"] = "processed"
    
    return structured_json

@router.get("/{doc_id}")
async def get_document(doc_id: str):
    if doc_id not in db_documents:
        raise HTTPException(status_code=404, detail="Document not found")
    return db_documents[doc_id]

@router.get("/{doc_id}/fields")
async def get_document_fields(doc_id: str):
    if doc_id not in db_documents:
        raise HTTPException(status_code=404, detail="Document not found")
    if not db_documents[doc_id]["data"]:
        return {"fields": []}
    return db_documents[doc_id]["data"]
