"""Text and code file parser — reads plain text, code, markdown, config files."""

from __future__ import annotations

import logging
from pathlib import Path

from mori_advisor.parsers import BaseParser, Chunk, register_parser

logger = logging.getLogger(__name__)

# Approximate: 4 chars ≈ 1 token. 50K tokens ≈ 200K chars.
CHUNK_CHAR_LIMIT = 200_000

# Extensions we handle as text/code
_TEXT_EXTENSIONS = {
    ".py", ".ts", ".js", ".jsx", ".tsx", ".go", ".rs", ".rb",
    ".java", ".kt", ".scala", ".c", ".h", ".cpp", ".hpp", ".cs", ".swift",
    ".sh", ".bash", ".zsh", ".ps1", ".psm1",
    ".sql", ".r",
    ".md", ".markdown", ".rst", ".txt",
    ".json", ".yaml", ".yml", ".toml", ".xml", ".html", ".css", ".scss",
    ".dockerfile", ".Dockerfile",
    ".tf", ".hcl", ".cfg", ".ini", ".conf",
    ".env", ".envrc",
    ".csv", ".tsv", ".log",
}


@register_parser("text")
class TextParser(BaseParser):
    """Parser for text/code files — reads content and splits into token-sized chunks."""

    @classmethod
    def can_handle(cls, source: Path) -> bool:
        if not source.exists():
            return False
        if source.is_dir():
            return False
        return source.suffix.lower() in _TEXT_EXTENSIONS

    def parse(self, source: Path, **kwargs) -> list[Chunk]:
        """Read a text file and produce chunks.

        For a single file, produces one chunk unless the file exceeds
        CHUNK_CHAR_LIMIT, in which case it splits at paragraph boundaries.
        """
        try:
            text = source.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            logger.warning("Failed to read %s: %s", source, e)
            return []

        if not text.strip():
            return []

        chunks: list[Chunk] = []
        suffix = source.suffix.lower()
        lang = suffix.lstrip(".")

        if len(text) <= CHUNK_CHAR_LIMIT:
            chunks.append(
                Chunk(
                    content=text,
                    metadata={
                        "source_path": str(source),
                        "language": lang,
                        "file_size": len(text),
                    },
                )
            )
        else:
            # Split at paragraph boundaries
            paragraphs = text.split("\n\n")
            current = ""
            part = 1
            for para in paragraphs:
                if len(current) + len(para) > CHUNK_CHAR_LIMIT and current:
                    chunks.append(
                        Chunk(
                            content=current,
                            metadata={
                                "source_path": str(source),
                                "language": lang,
                                "part": part,
                                "file_size": len(text),
                            },
                        )
                    )
                    part += 1
                    current = para
                else:
                    current = f"{current}\n\n{para}" if current else para
            if current.strip():
                chunks.append(
                    Chunk(
                        content=current,
                        metadata={
                            "source_path": str(source),
                            "language": lang,
                            "part": part,
                            "file_size": len(text),
                        },
                    )
                )

        return chunks


@register_parser("code")
class CodeParser(TextParser):
    """Alias for text parser — same behaviour, explicit type name."""

    source_type = "code"


def parse_directory(directory: Path, **kwargs) -> list[Chunk]:
    """Parse all text/code files in a directory recursively.

    Each file becomes its own chunk(s). Skips hidden directories and
    binary files.
    """
    chunks: list[Chunk] = []
    parser = TextParser()

    for file_path in sorted(directory.rglob("*")):
        if not file_path.is_file():
            continue
        # Skip hidden dirs
        if any(part.startswith(".") for part in file_path.parts if part != "."):
            continue
        if file_path.suffix.lower() not in _TEXT_EXTENSIONS:
            continue
        try:
            chunks.extend(parser.parse(file_path, **kwargs))
        except Exception as e:
            logger.warning("Skipping %s: %s", file_path, e)

    return chunks