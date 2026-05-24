"""Parser registry — format-aware source material parsers.

Each parser handles a specific source type (text, pdf, image, transcript, git)
and produces Chunks that the IngestionPipeline feeds to the distillation model.

Usage:
    from mori_advisor.parsers import get_parser, Chunk

    parser = get_parser(Path("~/docs/review.pdf"))
    chunks = parser.parse(Path("~/docs/review.pdf"))
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from mori_advisor.parsers.exceptions import ParserNotFoundError

logger = logging.getLogger(__name__)


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


def get_parser(
    source: Path, explicit_type: str | None = None
) -> BaseParser | None:
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

from mori_advisor.parsers import text_parser  # noqa: E402, F401
from mori_advisor.parsers import pdf_parser  # noqa: E402, F401
from mori_advisor.parsers import image_parser  # noqa: E402, F401
from mori_advisor.parsers import transcript_parser  # noqa: E402, F401
from mori_advisor.parsers import git_parser  # noqa: E402, F401