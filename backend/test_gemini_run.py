import os
import json
from dotenv import load_dotenv
load_dotenv()

try:
    from app.services.gemini_parser import parse_with_gemini
    import glob
    pdfs = glob.glob("uploads/*.pdf")
    if pdfs:
        res = parse_with_gemini(pdfs[-1])
        for f in res.get("fields", []):
            print(f"FIELD: {f.get('field')} | VALUE: {f.get('value')}")
except Exception as e:
    import traceback
    print("ERROR:", traceback.format_exc())
