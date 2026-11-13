from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

PDF_DOCUMENT_MAX_BYTES = 50_000_000
PDF_DOCUMENT_MAX_PAGES = 1_000
ARXIV_PDF_ASSET_PATH = "arxiv-fallback.pdf"
PDF_ONLY_READER_VERSION = "arxiv-pdf-reader-v1"


class PDFDocumentError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PDFDocumentMetadata:
    page_count: int
    page_width_points: float | None = None
    page_height_points: float | None = None


def _pdfinfo_value(output: str, label: str) -> str | None:
    prefix = f"{label.lower()}:"
    for line in output.splitlines():
        if line.lower().startswith(prefix):
            return line.split(":", 1)[1].strip()
    return None


def inspect_pdf_document(
    stream: BinaryIO,
    *,
    pdfinfo_command: str = "pdfinfo",
    timeout_seconds: int = 15,
) -> PDFDocumentMetadata:
    """Inspect a manifest-backed PDF without trusting its filename or metadata."""

    with tempfile.TemporaryDirectory(prefix="academatepaper-pdf-") as temp_dir:
        pdf_path = Path(temp_dir) / "document.pdf"
        with pdf_path.open("wb") as target:
            copied = 0
            while chunk := stream.read(min(1024 * 1024, PDF_DOCUMENT_MAX_BYTES + 1 - copied)):
                copied += len(chunk)
                if copied > PDF_DOCUMENT_MAX_BYTES:
                    raise PDFDocumentError(
                        f"Embedded PDF exceeds the {PDF_DOCUMENT_MAX_BYTES}-byte limit."
                    )
                target.write(chunk)
        byte_size = pdf_path.stat().st_size
        if byte_size <= 0 or byte_size > PDF_DOCUMENT_MAX_BYTES:
            raise PDFDocumentError(
                f"Embedded PDF size must be between 1 and {PDF_DOCUMENT_MAX_BYTES} bytes."
            )
        with pdf_path.open("rb") as source:
            if source.read(5) != b"%PDF-":
                raise PDFDocumentError("The embedded asset is not a valid PDF document.")

        try:
            result = subprocess.run(
                [pdfinfo_command, str(pdf_path)],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PDFDocumentError(f"PDF inspection failed: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise PDFDocumentError(detail or "PDF inspection failed.")

        encrypted = (_pdfinfo_value(result.stdout, "Encrypted") or "").lower()
        if encrypted.startswith("yes"):
            raise PDFDocumentError("Encrypted PDF documents cannot be displayed inline.")
        pages_value = _pdfinfo_value(result.stdout, "Pages")
        try:
            page_count = int(pages_value or "")
        except ValueError as exc:
            raise PDFDocumentError("PDF inspection returned an invalid page count.") from exc
        if page_count < 1 or page_count > PDF_DOCUMENT_MAX_PAGES:
            raise PDFDocumentError(
                f"Embedded PDF page count must be between 1 and {PDF_DOCUMENT_MAX_PAGES}."
            )

        try:
            javascript = subprocess.run(
                [pdfinfo_command, "-js", str(pdf_path)],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired):
            javascript = None
        if javascript is not None and javascript.returncode == 0 and javascript.stdout.strip():
            raise PDFDocumentError(
                "PDF documents containing JavaScript cannot be displayed inline."
            )

        page_width: float | None = None
        page_height: float | None = None
        page_size = _pdfinfo_value(result.stdout, "Page size")
        if page_size:
            dimensions = page_size.split("pts", 1)[0].strip().split(" x ", 1)
            if len(dimensions) == 2:
                try:
                    page_width = float(dimensions[0])
                    page_height = float(dimensions[1])
                except ValueError:
                    pass
        return PDFDocumentMetadata(
            page_count=page_count,
            page_width_points=page_width,
            page_height_points=page_height,
        )
