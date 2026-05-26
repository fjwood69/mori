"""PDF parser — extracts text from PDF files for distillation.

Uses pymupdf (MuPDF) if available, falls back to pypdf2.
If neither is installed, raises ParserDependencyError.
"""

from __future__ import annotations

import logging
from io import BytesIO
from pathlib import Path

from mori_advisor.parsers import BaseParser, Chunk, register_parser
from mori_advisor.parsers.exceptions import ParserDependencyError

logger = logging.getLogger(__name__)

# ~50K token limit, roughly 200K characters
CHUNK_CHAR_LIMIT = 200_000

_pymupdf = None
_pypdf2 = None
_HAS_PDF_SUPPORT = False

try:
    import fitz as _pymupdf

    _HAS_PDF_SUPPORT = True
except ImportError:
    pass

if not _HAS_PDF_SUPPORT:
    try:
        import pypdf as _pypdf2

        _HAS_PDF_SUPPORT = True
    except ImportError:
        pass


@register_parser("pdf")
class PdfParser(BaseParser):
    """Extract text from PDF files, chunked by page groups for token limits."""

    @classmethod
    def can_handle(cls, source: Path) -> bool:
        if not source.exists():
            return False
        if source.is_dir():
            return False
        return source.suffix.lower() == ".pdf"

    def parse(self, source: Path, **kwargs) -> list[Chunk]:
        if not _HAS_PDF_SUPPORT:
            raise ParserDependencyError(
                "PDF parsing requires pymupdf or pypdf2. "
                "Install: pip install pymupdf  (recommended) "
                "or: pip install pypdf2"
            )

        pages_text, total_pages = self._extract_text(source)
        if not pages_text:
            logger.warning("No extractable text in %s (scanned PDF?)", source)
            return []

        return self._chunk_pages(pages_text, str(source), total_pages)

    def parse_content(self, name: str, content_bytes: bytes, mime_type: str) -> list[Chunk]:
        """Parse PDF content from raw bytes.

        Args:
            name: Source name/identifier.
            content_bytes: Raw PDF file bytes.
            mime_type: MIME type (should be application/pdf).

        Returns:
            List of Chunks.
        """
        if not _HAS_PDF_SUPPORT:
            raise ParserDependencyError(
                "PDF parsing requires pymupdf or pypdf2. "
                "Install: pip install pymupdf  (recommended) "
                "or: pip install pypdf2"
            )

        pages_text, total_pages = self._extract_from_stream(BytesIO(content_bytes), name)
        if not pages_text:
            logger.warning("No extractable text in %s (scanned PDF?)", name)
            return []

        return self._chunk_pages(pages_text, name, total_pages)

    def _extract_text(self, source: Path) -> tuple[list[tuple[int, str]], int]:
        """Extract text per page. Returns (pages, total_count) where
        pages is a list of (page_number, text) tuples.
        """
        if _pymupdf is not None:
            with _pymupdf.open(str(source)) as doc:
                return self._extract_from_stream_inner(doc, str(source))
        elif _pypdf2 is not None:
            reader = _pypdf2.PdfReader(str(source))
            pages: list[tuple[int, str]] = []
            for i, page in enumerate(reader.pages):
                text = page.extract_text()
                if text and text.strip():
                    pages.append((i + 1, text))
            return pages, len(pages)

        return [], 0

    def _extract_from_stream(
        self, stream: BytesIO, source_name: str
    ) -> tuple[list[tuple[int, str]], int]:
        """Extract text from a PDF byte stream.

        Args:
            stream: BytesIO containing PDF bytes.
            source_name: Name for logging/metadata.

        Returns:
            (pages, total_count) where pages is a list of (page_number, text) tuples.
        """
        if _pymupdf is not None:
            with _pymupdf.open(stream=stream, filetype="pdf") as doc:
                return self._extract_from_stream_inner(doc, source_name)
        elif _pypdf2 is not None:
            reader = _pypdf2.PdfReader(stream)
            pages: list[tuple[int, str]] = []
            for i, page in enumerate(reader.pages):
                text = page.extract_text()
                if text and text.strip():
                    pages.append((i + 1, text))
            return pages, len(pages)

        return [], 0

    def _extract_from_stream_inner(
        self, doc, source_name: str
    ) -> tuple[list[tuple[int, str]], int]:
        """Extract text from an already-open MuPDF document."""
        pages: list[tuple[int, str]] = []
        for i, page in enumerate(doc):
            text = page.get_text()
            if text.strip():
                pages.append((i + 1, text))
        return pages, len(pages) if pages else 0

    def _chunk_pages(
        self, pages: list[tuple[int, str]], source_name: str, total_pages: int
    ) -> list[Chunk]:
        """Group pages into chunks that fit within the token limit."""
        chunks: list[Chunk] = []
        current_pages: list[int] = []
        current_text = ""
        part = 1

        for pn, text in pages:
            if len(current_text) + len(text) > CHUNK_CHAR_LIMIT and current_text:
                chunks.append(self._make_chunk(current_text, current_pages, source_name, total_pages, part))
                part += 1
                current_pages = []
                current_text = ""

            current_pages.append(pn)
            if current_text:
                current_text += f"\n\n--- Page {pn} ---\n{text}"
            else:
                current_text = f"--- Page {pn} ---\n{text}"

        if current_text.strip():
            chunks.append(self._make_chunk(current_text, current_pages, source_name, total_pages, part))

        return chunks

    def _make_chunk(
        self, text: str, page_nums: list[int], source_name: str, total_pages: int, part: int
    ) -> Chunk:
        page_range = f"{page_nums[0]}-{page_nums[-1]}" if len(page_nums) > 1 else str(page_nums[0])
        return Chunk(
            content=text,
            metadata={
                "source_path": source_name,
                "type": "pdf",
                "pages": page_range,
                "total_pages": total_pages,
                "part": part,
            },
        )
