"""Parser exceptions — raised by format-specific parsers."""


class ParserDependencyError(Exception):
    """Optional dependency is missing for this parser type.

    Example: pymupdf not installed for PDF parsing.
    The message includes the pip install command.
    """


class ParserNotFoundError(Exception):
    """No parser can handle the given source path."""


class IngestionSkipError(Exception):
    """Source should be skipped — already ingested or contains no extractable content."""
