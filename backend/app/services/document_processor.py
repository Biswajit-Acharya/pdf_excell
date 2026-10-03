import os
import cv2
import numpy as np
import fitz  # PyMuPDF
from paddleocr import PaddleOCR
import logging

logger = logging.getLogger(__name__)

class DocumentProcessor:
    def __init__(self):
        # Initialize PaddleOCR
        # Use English language for now. Adjust if needed.
        self.ocr = PaddleOCR(use_angle_cls=True, lang='en', enable_mkldnn=False)

    def preprocess_image(self, image_np):
        """
        Enhance image using OpenCV.
        Includes deskewing, grayscale, contrast, and noise reduction.
        """
        # Convert to grayscale
        gray = cv2.cvtColor(image_np, cv2.COLOR_BGR2GRAY)
        
        # Deskew (basic rotation correction)
        coords = np.column_stack(np.where(gray > 0))
        if len(coords) > 0:
            angle = cv2.minAreaRect(coords)[-1]
            if angle < -45:
                angle = -(90 + angle)
            else:
                angle = -angle
            (h, w) = gray.shape[:2]
            center = (w // 2, h // 2)
            M = cv2.getRotationMatrix2D(center, angle, 1.0)
            gray = cv2.warpAffine(gray, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
        
        # Noise reduction and Contrast
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        
        # Adaptive thresholding (binarization)
        thresh = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2)
        
        # Convert back to BGR for PaddleOCR (it expects 3 channels)
        result_bgr = cv2.cvtColor(thresh, cv2.COLOR_GRAY2BGR)
        return result_bgr

    def process_pdf(self, pdf_path: str):
        """
        Convert PDF to high-res images, preprocess, and run OCR.
        """
        doc = fitz.open(pdf_path)
        ocr_results = []
        
        for page_num in range(len(doc)):
            page = doc.load_page(page_num)
            
            # Render PDF page to high-res image (300 DPI approx)
            zoom = 2.0  # Increase resolution
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            
            # Convert to numpy array for OpenCV
            img_np = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)
            
            # Convert RGB (fitz default) to BGR (OpenCV default)
            if pix.n == 3:
                img_np = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)
            
            # Preprocess
            processed_img = self.preprocess_image(img_np)
            
            # Run PaddleOCR
            result = self.ocr.ocr(processed_img)
            
            # Print raw result for debugging
            print("RAW RESULT TYPE:", type(result))
            if isinstance(result, list) and len(result) > 0:
                print("FIRST ITEM TYPE:", type(result[0]))
                print("FIRST ITEM:", result[0])
            
            # Temporary stub while debugging format
            page_data = {
                "page": page_num + 1,
                "text_elements": []
            }
            ocr_results.append(page_data)
            
        doc.close()
        return ocr_results
