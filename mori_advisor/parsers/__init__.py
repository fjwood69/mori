"""Parser registry — format-aware source material parsers.

Each parser handles a specific source type (text, pdf, image, transcript, git)
and produces Chunks that the IngestionPipeline feeds to the distillation model.

Usage:
    from mori_advisor.parsers import get_parser, Chunk

    parser = get_parser(Path("~/docs/review.pdf"))
    chunks = parser.parse(Path("~/docs/review.pdf"))
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from pathlib import Path

from mori_advisor.parsers.exceptions import ParserNotFoundError

logger = logging.getLogger(__name__)

# Maximum raw byte size for content-mode ingestion (10MB)
CONTENT_SIZE_CEILING = 10 * 1024 * 1024


def normalize_encoding(text: str) -> str:
    """Strip BOM and normalize newlines."""
    if text.startswith("﻿"):
        text = text[1:]
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def detect_charset(raw: bytes) -> str:
    """Detect charset from raw bytes. Falls back to utf-8 with replacement."""
    try:
        import chardet

        result = chardet.detect(raw)
        if result.get("encoding") and result.get("confidence", 0) > 0.5:
            return result["encoding"]
    except ImportError:
        pass
    return "utf-8"


def decode_bytes(raw: bytes) -> str:
    """Decode bytes to string with charset detection and normalization.

    Tries chardet if available, falls back to utf-8 then latin-1.
    Strips BOM and normalizes newlines.
    """
    charset = detect_charset(raw)
    try:
        text = raw.decode(charset)
    except (LookupError, UnicodeDecodeError):
        text = raw.decode("utf-8", errors="replace")
    return normalize_encoding(text)


def is_binary(raw: bytes) -> bool:
    """Quick binary detection — check for null bytes in first 8KB."""
    try:
        import magic as _magic

        mime = _magic.from_buffer(raw[:8192], mime=True)
        if mime and not mime.startswith("text/"):
            return True
    except ImportError:
        pass
    # Fallback: check for null bytes
    return b"\x00" in raw[:8192]


@dataclass
class Chunk:
    """A chunk of source material ready for LLM distillation.

    Attributes:
        content: The text content (or base64 data URI for images).
        metadata: Source provenance — path, page range, session ID, etc.
        is_image: If True, the IngestionPipeline routes this to consult_vision().
    """

    content: str
    metadata: dict = field(default_factory=dict)
    is_image: bool = False

    @classmethod
    def from_content(
        cls,
        content_bytes: bytes,
        name: str,
        mime_type: str,
        is_image: bool = False,
        **extra_meta,
    ) -> "Chunk":
        """Create a Chunk from raw bytes rather than a filesystem path.

        For images: base64-encode into a data URI.
        For text: decode with charset detection and normalization.

        Args:
            content_bytes: Raw bytes of the source material.
            name: Source name (filename or identifier).
            mime_type: MIME type of the content.
            is_image: If True, route through consult_vision().
            **extra_meta: Additional metadata fields to include.

        Returns:
            A Chunk ready for distillation.
        """
        meta = {"source_path": name, "mime_type": mime_type}
        meta.update(extra_meta)

        if is_image:
            encoded = base64.b64encode(content_bytes).decode("ascii")
            data_uri = f"data:{mime_type};base64,{encoded}"
            return cls(
                content=data_uri,
                metadata=meta,
                is_image=True,
            )

        text = decode_bytes(content_bytes)
        meta["file_size"] = len(text)
        return cls(content=text, metadata=meta)


class BaseParser:
    """Abstract base for format-specific parsers.

    Subclasses set `source_type` and implement `can_handle()` + `parse()`.
    Use the `@register_parser` decorator to auto-register.
    """

    source_type: str = ""

    @classmethod
    def can_handle(cls, source: Path) -> bool:
        """Return True if this parser can handle the given source path."""
        raise NotImplementedError

    def parse(self, source: Path, **kwargs) -> list[Chunk]:
        """Parse source into a list of Chunks for distillation."""
        raise NotImplementedError


# ── Registry ───────────────────────────────────────────────────────────────

_registry: dict[str, type[BaseParser]] = {}


def register_parser(source_type: str):
    """Decorator to register a parser class for a source type.

    Usage:
        @register_parser("pdf")
        class PdfParser(BaseParser):
            ...
    """

    def decorator(cls: type[BaseParser]):
        cls.source_type = source_type
        _registry[source_type] = cls
        logger.debug("Registered parser %s for type %s", cls.__name__, source_type)
        return cls

    return decorator


def get_parser(source: Path, explicit_type: str | None = None) -> BaseParser | None:
    """Resolve a parser for the given source path.

    If `explicit_type` is provided, returns that parser directly.
    Otherwise iterates the registry looking for a parser whose `can_handle()`
    returns True.

    Returns None if no parser matches.
    """
    if explicit_type and explicit_type != "auto":
        cls = _registry.get(explicit_type)
        if cls is None:
            raise ParserNotFoundError(
                f"No parser registered for type '{explicit_type}'. "
                f"Available: {', '.join(sorted(_registry.keys()))}"
            )
        return cls()

    for source_type, cls in _registry.items():
        try:
            if cls.can_handle(source):
                return cls()
        except Exception:
            continue

    return None


def list_parsers() -> list[str]:
    """Return list of registered parser type names."""
    return sorted(_registry.keys())


# ── Import parser modules to trigger @register_parser decorators ────────────

from mori_advisor.parsers import (  # noqa: E402
    git_parser,  # noqa: F401
    image_parser,  # noqa: F401
    pdf_parser,  # noqa: F401
    text_parser,  # noqa: F401
    transcript_parser,  # noqa: F401
)
