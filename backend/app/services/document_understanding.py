import os
import json
import logging
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

class DocumentUnderstanding:
    def __init__(self):
        self.api_key = os.environ.get("GEMINI_API_KEY")
        if self.api_key:
            self.client = genai.Client(api_key=self.api_key)
        else:
            self.client = None

    def analyze_layout(self, ocr_results):
        """
        Passes OCR bounding boxes and text to Gemini for layout understanding.
        Follows strict anti-hallucination rules.
        """
        if not self.client:
            logger.warning("No Gemini API key, falling back to basic extraction.")
            return self._fallback_extraction()

        # Reconstruct the document as a structured layout map
        layout_text = "Document OCR Results:\n"
        for page in ocr_results:
            layout_text += f"--- Page {page['page']} ---\n"
            for el in page['text_elements']:
                # Provide text with coordinates for Gemini to understand relationships
                layout_text += f"Text: '{el['text']}' | Box: {el['box']} | Conf: {el['confidence']:.2f}\n"

        prompt = f"""
        You are a generic Document Data Extractor.
        Below is the raw OCR output (text and bounding boxes) of a document.
        
        Identify meaningful field-value pairs dynamically.
        Do not force the document into a predefined schema. 
        Extract any obvious keys (like Name, DOB, Account Number, Address, PIN, Total Amount, etc.) and their corresponding values based on geometric layout.
        
        STRICT ANTI-HALLUCINATION RULE:
        You MUST NEVER invent, complete, guess, or assume a value.
        If a text is unclear, missing, or ambiguous, you must output:
        "value": null, "status": "needs_review"
        Otherwise, if confident, output: "status": "verified"
        
        Return exactly this JSON format:
        {{
            "document_type": "Identified Document Type",
            "pages": 1,
            "fields": [
                {{
                    "field_name": "Field Name",
                    "value": "Extracted Value or null",
                    "confidence": 0.95,
                    "status": "verified or needs_review",
                    "page": 1
                }}
            ]
        }}
        
        OCR Output:
        {layout_text}
        """

        import time
        max_retries = 2
        for attempt in range(max_retries):
            try:
                response = self.client.models.generate_content(
                    model='gemini-flash-latest',
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.1,
                    )
                )
                return json.loads(response.text)
            except Exception as e:
                logger.warning(f"Gemini API attempt {attempt+1} failed: {e}")
                time.sleep(2)
                
        # If API fails after retries (e.g. 503 Unavailable)
        logger.error("Gemini API is down or quota exceeded. Falling back.")
        return self._fallback_extraction()
        
    def _fallback_extraction(self):
        """
        Fallback when Gemini is down.
        Returns the structure requested by the user, but marked as needs_review since AI couldn't parse it.
        """
        return {
            "document_type": "Unknown Document (AI Down)",
            "pages": 1,
            "fields": [
                {
                    "field_name": "System Status",
                    "value": "AI Processing Failed",
                    "confidence": 0.0,
                    "status": "needs_review",
                    "page": 1
                }
            ]
        }
