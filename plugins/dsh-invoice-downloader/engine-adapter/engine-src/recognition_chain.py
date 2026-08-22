"""
Recognition chain arbitration.

Precedence: deterministic parser > local OCR + DeepSeek > optional GLM.
"""

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


class RecognitionChain:
    """
    Orchestrates the recognition chain for invoice extraction.

    Chain order:
    1. Deterministic parser (existing probe_local_only)
    2. Local OCR + DeepSeek text extraction (new default)
    3. GLM vision fallback (optional, user-keyed)
    """

    def __init__(self, extractor, local_ocr_module=None, glm_runtime=None):
        """
        Args:
            extractor: DeepSeekExtractor instance
            local_ocr_module: local_ocr module (for probe_local_ocr)
            glm_runtime: GlmRuntime instance (optional, for GLM fallback)
        """
        self._extractor = extractor
        self._local_ocr = local_ocr_module
        self._glm = glm_runtime

    def extract(self, pdf_path: str, document_context: dict = None) -> dict:
        """
        Run the recognition chain on a PDF invoice.

        Returns the extraction result dict matching the legacy _adapt_result shape.
        Raises RuntimeError if all chains fail.
        """
        context = document_context or {}

        # Chain 1: Deterministic parser (existing, not modified)
        # This is handled by the existing engine code before calling this chain.
        # If we reach here, deterministic parsing already failed.

        # Chain 2: Local OCR + DeepSeek
        try:
            logger.info("Trying local OCR + DeepSeek extraction")
            ocr_result = self._local_ocr.probe_local_ocr(pdf_path)
            if ocr_result["status"] == "ok" and ocr_result["text_length"] > 50:
                result = self._extractor.extract_and_adapt(
                    ocr_result["text"],
                    pdf_path=pdf_path,
                    document_context=context,
                )
                logger.info("Local OCR + DeepSeek extraction succeeded")
                return result
            else:
                logger.warning(f"Local OCR probe insufficient: {ocr_result}")
        except Exception as e:
            logger.warning(f"Local OCR + DeepSeek extraction failed: {e}")

        # Chain 3: GLM vision fallback (optional)
        if self._glm:
            try:
                logger.info("Falling back to GLM vision extraction")
                # Use existing GLM dual-track logic
                # This is a simplified call — the full GLM path is preserved in the existing engine
                result = self._glm.extract(pdf_path, document_context=context)
                logger.info("GLM vision extraction succeeded")
                return result
            except Exception as e:
                logger.warning(f"GLM vision extraction failed: {e}")

        raise RuntimeError("all recognition chains failed")
