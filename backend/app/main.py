from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routes import conversion

app = FastAPI(title="PDF to Excel API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
from app.routes import v2_documents

app.include_router(conversion.router, prefix="/api", tags=["conversion"])
app.include_router(v2_documents.router, prefix="/api", tags=["v2_documents"])
@app.get("/")
def read_root():
    return {"message": "PDF to Excel API is running"}
