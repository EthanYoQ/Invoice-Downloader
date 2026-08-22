"""
GLM vision fallback integration.

Wires the existing glm_runtime.py into the RecognitionChain as an optional
vision fallback when the user supplies a GLM API key.
"""

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


class GlmFallbackExtractor:
    """
    GLM vision fallback extractor.

    Uses the existing GlmRuntime and extract_info_via_llm from the vendored engine.
    Only active when the user supplies a GLM API key.
    """

    def __init__(self, api_key: str, settings: dict = None):
        """
        Args:
            api_key: GLM API key (from credential store via T7).
            settings: Optional GLM settings dict.
        """
        self._api_key = api_key
        self._settings = settings or {}
        self._runtime = None
        self._extractor = None

    def _get_runtime(self):
        """Lazy-initialize GLM runtime."""
        if self._runtime is None:
            from glm_runtime import GlmRuntime
            self._runtime = GlmRuntime(self._api_key, settings=self._settings)
        return self._runtime

    def _get_extractor(self):
        """Lazy-initialize the GLM extractor (InvoiceExtractor with GLM runtime)."""
        if self._extractor is None:
            from invoice_extractor import InvoiceExtractor
            runtime = self._get_runtime()
            self._extractor = InvoiceExtractor(
                api_key=self._api_key,
                glm_runtime=runtime,
                close_glm_runtime=False,
            )
        return self._extractor

    def extract(self, pdf_path: str, document_context: dict = None) -> dict:
        """
        Extract invoice fields using GLM vision (Track B) or GLM OCR + LLM (Track A).

        Uses the existing dual-track logic from the vendored engine.
        Returns the extraction result dict matching the legacy _adapt_result shape.
        """
        extractor = self._get_extractor()

        # Convert PDF to base64 images (existing engine method)
        base64_images = extractor.pdf_to_base64_image(pdf_path)
        if not base64_images:
            raise RuntimeError(f"failed to convert PDF to images: {pdf_path}")

        # Call the existing dual-track extraction
        result = extractor.extract_info_via_llm(
            base64_images,
            pdf_path=pdf_path,
            document_context=document_context,
        )
        return result

    def close(self):
        """Close the GLM runtime."""
        if self._runtime:
            try:
                self._runtime.close()
            except Exception:
                pass
            self._runtime = None
            self._extractor = None


def create_glm_fallback(api_key: str = None, credential_store = None) -> Optional[GlmFallbackExtractor]:
    """
    Create a GLM fallback extractor if a GLM API key is available.

    Args:
        api_key: Direct GLM API key (takes precedence).
        credential_store: CredentialStore from T7 to retrieve glmApiKeyRef.

    Returns:
        GlmFallbackExtractor if key available, None otherwise.
    """
    key = api_key
    if not key and credential_store:
        # Try to retrieve from credential store
        import asyncio
        try:
            # Check for glm-api-key reference
            key = asyncio.get_event_loop().run_until_complete(
                credential_store.retrieve('glm-api-key')
            )
        except Exception:
            pass

    if not key:
        logger.info("No GLM API key available — GLM fallback disabled")
        return None

    logger.info("GLM fallback enabled")
    return GlmFallbackExtractor(api_key=key)
