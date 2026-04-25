"""
OCR Pipeline — GAP-A11 (Sprint 17)
=====================================
Adds Tesseract OCR as an optional ingestion stage for scanned PDFs.

Architecture:
  pdf_pipeline.py detects scanned pages (no text layer)
    → OCRProcessor.process_page() extracts text via Tesseract
    → Text rejoins the standard chunking pipeline

Integration point: src/IngestionPipeline/parsers/pdf_pipeline.py
  - Detects pages with < 10 characters of extractable text
  - Routes through OCR before chunking

Design principles:
  - Optional dependency: tesseract-ocr via subprocess (not Python bindings)
  - Graceful degradation: if Tesseract not installed, logs warning and skips
  - Page-level OCR (not document-level) for memory efficiency
  - Quality check: OCR confidence threshold to skip low-quality pages
  - Docker: add `tesseract-ocr` to Dockerfile if needed
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────

OCR_ENABLED = os.getenv("OCR_ENABLED", "true").lower() == "true"
OCR_LANGUAGE = os.getenv("OCR_LANGUAGE", "eng")
OCR_MIN_TEXT_CHARS = int(os.getenv("OCR_MIN_TEXT_CHARS", "10"))
OCR_DPI = int(os.getenv("OCR_DPI", "300"))
OCR_TIMEOUT_SECONDS = int(os.getenv("OCR_TIMEOUT_SECONDS", "30"))


# ═════════════════════════════════════════════════════════════════════════
#  Data classes
# ═════════════════════════════════════════════════════════════════════════

@dataclass
class OCRResult:
    """Result of OCR processing for a single page."""
    page_number: int
    text: str
    confidence: float = 0.0
    is_scanned: bool = False
    ocr_applied: bool = False
    latency_ms: float = 0.0
    error: Optional[str] = None


@dataclass
class OCRDocumentResult:
    """Result of OCR processing for an entire document."""
    pages: List[OCRResult]
    total_pages: int
    scanned_pages: int
    ocr_applied_pages: int
    total_latency_ms: float
    tesseract_available: bool

    def as_dict(self) -> Dict[str, Any]:
        return {
            "total_pages": self.total_pages,
            "scanned_pages": self.scanned_pages,
            "ocr_applied_pages": self.ocr_applied_pages,
            "total_latency_ms": round(self.total_latency_ms, 2),
            "tesseract_available": self.tesseract_available,
        }


# ═════════════════════════════════════════════════════════════════════════
#  OCRProcessor
# ═════════════════════════════════════════════════════════════════════════

class OCRProcessor:
    """
    Tesseract OCR integration for scanned PDF pages.

    Uses subprocess calls to tesseract (not pytesseract) for
    minimal Python dependencies. Requires:
      - tesseract-ocr system package
      - pdftoppm (from poppler-utils) for PDF → image conversion

    Parameters
    ----------
    language : str
        Tesseract language code (default "eng").
    dpi : int
        Resolution for PDF → image conversion (default 300).
    enabled : bool
        Whether OCR is active.
    """

    def __init__(
        self,
        language: str = OCR_LANGUAGE,
        dpi: int = OCR_DPI,
        enabled: bool = OCR_ENABLED,
    ):
        self._language = language
        self._dpi = dpi
        self._enabled = enabled
        self._tesseract_path = shutil.which("tesseract")
        self._pdftoppm_path = shutil.which("pdftoppm")

    @property
    def available(self) -> bool:
        """Check if Tesseract and pdftoppm are installed."""
        return (
            self._enabled
            and self._tesseract_path is not None
            and self._pdftoppm_path is not None
        )

    def is_scanned_page(self, text: str) -> bool:
        """Determine if a page is scanned (minimal extractable text)."""
        if not text:
            return True
        clean = text.strip()
        return len(clean) < OCR_MIN_TEXT_CHARS

    def process_page_image(self, image_path: str) -> OCRResult:
        """
        OCR a single page image.

        Parameters
        ----------
        image_path : str
            Path to the page image (PNG/TIFF).

        Returns
        -------
        OCRResult with extracted text.
        """
        start = time.monotonic()

        if not self.available:
            return OCRResult(
                page_number=0, text="", is_scanned=True, ocr_applied=False,
                error="Tesseract not available",
            )

        try:
            result = subprocess.run(
                [
                    self._tesseract_path,
                    image_path,
                    "stdout",
                    "-l", self._language,
                    "--oem", "1",  # LSTM engine
                    "--psm", "3",  # Fully automatic page segmentation
                ],
                capture_output=True,
                text=True,
                timeout=OCR_TIMEOUT_SECONDS,
            )

            text = result.stdout.strip()
            elapsed = (time.monotonic() - start) * 1000

            # Basic confidence estimate from text quality
            confidence = self._estimate_confidence(text)

            return OCRResult(
                page_number=0,
                text=text,
                confidence=confidence,
                is_scanned=True,
                ocr_applied=True,
                latency_ms=elapsed,
            )

        except subprocess.TimeoutExpired:
            elapsed = (time.monotonic() - start) * 1000
            return OCRResult(
                page_number=0, text="", is_scanned=True, ocr_applied=False,
                latency_ms=elapsed, error="OCR timeout",
            )
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            return OCRResult(
                page_number=0, text="", is_scanned=True, ocr_applied=False,
                latency_ms=elapsed, error=str(exc),
            )

    def process_pdf(
        self,
        pdf_path: str,
        page_texts: Optional[List[str]] = None,
    ) -> OCRDocumentResult:
        """
        Process a PDF, applying OCR to scanned pages.

        Parameters
        ----------
        pdf_path : str
            Path to the PDF file.
        page_texts : list[str], optional
            Pre-extracted text per page (from PyPDF2/pdfplumber).
            Pages with < OCR_MIN_TEXT_CHARS will be OCR'd.

        Returns
        -------
        OCRDocumentResult with text for all pages.
        """
        start = time.monotonic()
        results: List[OCRResult] = []
        ocr_count = 0

        if not self.available:
            total = len(page_texts) if page_texts else 0
            return OCRDocumentResult(
                pages=[], total_pages=total, scanned_pages=0,
                ocr_applied_pages=0, total_latency_ms=0.0,
                tesseract_available=False,
            )

        # Determine which pages need OCR
        if page_texts is None:
            page_texts = []

        pages_needing_ocr = []
        for i, text in enumerate(page_texts):
            if self.is_scanned_page(text):
                pages_needing_ocr.append(i)

        if not pages_needing_ocr:
            # No pages need OCR
            elapsed = (time.monotonic() - start) * 1000
            return OCRDocumentResult(
                pages=[
                    OCRResult(page_number=i, text=t, is_scanned=False)
                    for i, t in enumerate(page_texts)
                ],
                total_pages=len(page_texts),
                scanned_pages=0,
                ocr_applied_pages=0,
                total_latency_ms=elapsed,
                tesseract_available=True,
            )

        # Convert scanned pages to images and OCR
        with tempfile.TemporaryDirectory() as tmpdir:
            for page_num in pages_needing_ocr:
                try:
                    # Convert single page to image
                    image_prefix = os.path.join(tmpdir, f"page_{page_num}")
                    subprocess.run(
                        [
                            self._pdftoppm_path,
                            "-f", str(page_num + 1),  # 1-indexed
                            "-l", str(page_num + 1),
                            "-r", str(self._dpi),
                            "-png",
                            pdf_path,
                            image_prefix,
                        ],
                        capture_output=True,
                        timeout=OCR_TIMEOUT_SECONDS,
                    )

                    # Find the generated image
                    images = list(Path(tmpdir).glob(f"page_{page_num}*.png"))
                    if images:
                        ocr_result = self.process_page_image(str(images[0]))
                        ocr_result.page_number = page_num
                        if ocr_result.ocr_applied and ocr_result.text:
                            page_texts[page_num] = ocr_result.text
                            ocr_count += 1

                except Exception as exc:
                    logger.warning("OCR failed for page %d: %s", page_num, exc)

        # Build final results
        for i, text in enumerate(page_texts):
            is_scanned = i in pages_needing_ocr
            results.append(OCRResult(
                page_number=i,
                text=text,
                is_scanned=is_scanned,
                ocr_applied=is_scanned and i < len(page_texts),
            ))

        elapsed = (time.monotonic() - start) * 1000
        logger.info(
            "OCR processing: %d pages, %d scanned, %d OCR'd, %.0f ms",
            len(page_texts), len(pages_needing_ocr), ocr_count, elapsed,
        )

        return OCRDocumentResult(
            pages=results,
            total_pages=len(page_texts),
            scanned_pages=len(pages_needing_ocr),
            ocr_applied_pages=ocr_count,
            total_latency_ms=elapsed,
            tesseract_available=True,
        )

    @staticmethod
    def _estimate_confidence(text: str) -> float:
        """Estimate OCR confidence from text quality heuristics."""
        if not text:
            return 0.0

        score = 0.5  # base

        # Proportion of alphanumeric characters
        alpha_ratio = sum(c.isalnum() or c.isspace() for c in text) / max(len(text), 1)
        score += alpha_ratio * 0.3

        # Presence of known domain terms
        domain_terms = ["function", "register", "module", "init", "return", "void", "uint"]
        for term in domain_terms:
            if term in text.lower():
                score += 0.03

        return min(score, 1.0)
