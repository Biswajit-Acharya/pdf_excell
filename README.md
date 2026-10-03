# PDF to Excel Converter

A production-ready web application that converts uploaded PDF files into properly structured Excel (.xlsx) files with maximum data completeness and strong validation.

## Tech Stack
- **Frontend:** React, Vite, Tailwind CSS, Lucide React
- **Backend:** Python, FastAPI, pdfplumber, pandas, openpyxl, python-multipart

## Getting Started

### Backend Setup

1. Open a terminal in the `backend` directory.
2. Create and activate a virtual environment:
   ```bash
   python -m venv venv
   # On Windows:
   venv\Scripts\activate
   ```
3. Install requirements:
   ```bash
   pip install -r requirements.txt
   ```
4. Run the server:
   ```bash
   python -m uvicorn app.main:app --reload
   ```

### Frontend Setup

1. Open a terminal in the `frontend` directory.
2. Install dependencies:
   ```bash
   npm install
   ```
3. Run the development server:
   ```bash
   npm run dev
   ```

## Usage
1. Open your browser to the Vite frontend URL (typically http://localhost:5173).
2. Upload a PDF file.
3. Configure expected field counts or field schemas for robust validation.
4. Convert and download the resulting structured Excel file.
