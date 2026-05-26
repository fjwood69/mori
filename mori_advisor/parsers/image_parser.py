"""Image parser — loads images and packages them for Kimi's vision API.

Encodes images as base64 data URIs. The IngestionPipeline routes these
chunks through consult_vision() rather than the standard text path.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

from mori_advisor.parsers import BaseParser, Chunk, register_parser
from mori_advisor.parsers.exceptions import ParserDependencyError

logger = logging.getLogger(__name__)

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}

_HAS_IMAGE_SUPPORT = False
try:
    from PIL import Image as PILImage

    _HAS_IMAGE_SUPPORT = True
except ImportError:
    pass

# Resize images larger than this (in pixels, longest edge) before encoding.
# Keeps base64 payloads reasonable — large images inflate cost unpredictably.
MAX_IMAGE_DIMENSION = 2048


@register_parser("image")
class ImageParser(BaseParser):
    """Load images, optionally resize, and encode as base64 data URIs.

    Each image produces a single Chunk with is_image=True.
    """

    @classmethod
    def can_handle(cls, source: Path) -> bool:
        if not source.exists():
            return False
        if source.is_dir():
            return False
        return source.suffix.lower() in _IMAGE_EXTENSIONS

    def parse(self, source: Path, **kwargs) -> list[Chunk]:
        if not _HAS_IMAGE_SUPPORT:
            raise ParserDependencyError(
                "Image parsing requires Pillow. Install: pip install pillow"
            )

        try:
            img = PILImage.open(str(source))
            original_size = img.size

            # Resize large images to keep base64 payloads manageable
            if max(img.size) > MAX_IMAGE_DIMENSION:
                ratio = MAX_IMAGE_DIMENSION / max(img.size)
                new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
                img = img.resize(new_size, PILImage.LANCZOS)
                logger.debug("Resized %s from %s to %s", source.name, original_size, new_size)

            # Determine format for data URI
            fmt = img.format or source.suffix.lstrip(".").upper()
            if fmt == "JPG":
                fmt = "JPEG"

            # Encode to base64 data URI
            import io

            buffer = io.BytesIO()
            save_fmt = "JPEG" if fmt == "JPEG" else "PNG"
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGBA")
            else:
                img = img.convert("RGB")
            img.save(buffer, format=save_fmt)
            encoded = base64.b64encode(buffer.getvalue()).decode("ascii")

            mime = f"image/{save_fmt.lower()}"
            data_uri = f"data:{mime};base64,{encoded}"

            return [
                Chunk(
                    content=data_uri,
                    metadata={
                        "source_path": str(source),
                        "type": "image",
                        "format": fmt,
                        "original_size": list(original_size),
                        "encoded_size": len(encoded),
                        "mime_type": mime,
                    },
                    is_image=True,
                )
            ]

        except Exception as e:
            logger.warning("Failed to parse image %s: %s", source, e)
            return []
