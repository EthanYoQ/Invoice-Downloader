"""
Local OCR adapter using RapidOCR (ONNX Runtime with PP-OCRv4 models).

This adapter produces raw text from invoice images/PDFs for consumption by the DSH LLM extractor.
No field understanding — text output only.
"""

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Lazy import to avoid loading ONNX runtime at module import time
_ocr_instance = None


def _get_ocr():
    """Lazy-initialize RapidOCR instance."""
    global _ocr_instance
    if _ocr_instance is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
            _ocr_instance = RapidOCR()
            logger.info("RapidOCR initialized successfully")
        except ImportError:
            logger.error("rapidocr-onnxruntime not installed")
            raise RuntimeError("rapidocr-onnxruntime is required for local OCR but not installed")
    return _ocr_instance


# Config fields for OCR quality knobs (can be overridden via cordis.yml config)
DEFAULT_MAX_PAGES = 3
DEFAULT_DPI = 300


def ocr_pdf(pdf_path: str, max_pages: int = None, dpi: int = None) -> str:
    """Extract text from a PDF invoice using local OCR."""
    max_pages = max_pages or DEFAULT_MAX_PAGES
    dpi = dpi or DEFAULT_DPI

    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    import fitz  # PyMuPDF

    ocr = _get_ocr()
    texts = []

    doc = fitz.open(pdf_path)
    try:
        num_pages = min(len(doc), max_pages)
        for page_num in range(num_pages):
            page = doc[page_num]
            pix = page.get_pixmap(dpi=dpi)
            img_bytes = pix.tobytes("png")
            result, elapse = ocr(img_bytes)
            if result:
                page_text = "\n".join([line[1] for line in result])
                texts.append(page_text)
                logger.debug(f"OCR page {page_num + 1}: {len(result)} lines, elapse={elapse}")
    finally:
        doc.close()

    return "\n\n".join(texts)


def ocr_image(image_path: str) -> str:
    """Extract text from an image file using local OCR."""
    if not os.path.exists(image_path):
        raise FileNotFoundError(f"Image not found: {image_path}")

    ocr = _get_ocr()
    result, elapse = ocr(image_path)
    if not result:
        return ""
    return "\n".join([line[1] for line in result])


def probe_local_ocr(pdf_path: str) -> dict:
    """Probe local OCR on a PDF and return metadata about the extraction."""
    try:
        text = ocr_pdf(pdf_path, max_pages=2)
        return {
            "status": "ok",
            "text_length": len(text),
            "line_count": text.count("\n") + 1 if text else 0,
            "text": text,
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "text": "",
        }
