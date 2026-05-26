"""Image parser — loads images and packages them for Kimi's vision API.

Encodes images as base64 data URIs. The IngestionPipeline routes these
chunks through consult_vision() rather than the standard text path.
"""

from __future__ import annotations

import base64
import io
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
            return self._process_image(str(source), str(source), f"image/{source.suffix.lower().lstrip('.')}")
        except Exception as e:
            logger.warning("Failed to parse image %s: %s", source, e)
            return []

    def parse_content(self, name: str, content_bytes: bytes, mime_type: str) -> list[Chunk]:
        """Parse image content from raw bytes.

        Args:
            name: Source name/identifier.
            content_bytes: Raw image bytes.
            mime_type: MIME type (e.g. image/png, image/jpeg).

        Returns:
            List of Chunks (single image chunk).
        """
        if not _HAS_IMAGE_SUPPORT:
            raise ParserDependencyError(
                "Image parsing requires Pillow. Install: pip install pillow"
            )

        try:
            return self._process_image(io.BytesIO(content_bytes), name, mime_type)
        except Exception as e:
            logger.warning("Failed to parse image %s: %s", name, e)
            return []

    def _process_image(self, img_source, source_name: str, mime_type: str) -> list[Chunk]:
        """Process an image — resize if needed, encode as base64 data URI.

        Args:
            img_source: Path string or BytesIO to open.
            source_name: Name for metadata.
            mime_type: MIME type string.

        Returns:
            List with a single image Chunk.
        """
        if isinstance(img_source, str):
            img = PILImage.open(img_source)
        else:
            img = PILImage.open(img_source)

        original_size = img.size

        # Resize large images to keep base64 payloads manageable
        if max(img.size) > MAX_IMAGE_DIMENSION:
            ratio = MAX_IMAGE_DIMENSION / max(img.size)
            new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
            img = img.resize(new_size, PILImage.LANCZOS)
            logger.debug("Resized %s from %s to %s", source_name, original_size, new_size)

        # Determine save format from MIME type
        if mime_type == "image/jpeg" or mime_type == "image/jpg":
            save_fmt = "JPEG"
        elif mime_type == "image/png":
            save_fmt = "PNG"
        elif mime_type == "image/webp":
            save_fmt = "WEBP"
        else:
            save_fmt = "PNG"

        # Convert to RGB/RGBA as needed
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            if save_fmt == "JPEG":
                # JPEG doesn't support alpha — composite on white
                background = PILImage.new("RGB", img.size, (255, 255, 255))
                background.paste(img, mask=img.split()[3])
                img = background
                save_fmt = "JPEG"
        else:
            img = img.convert("RGB")

        buffer = io.BytesIO()
        img.save(buffer, format=save_fmt)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")

        mime = f"image/{save_fmt.lower()}"
        data_uri = f"data:{mime};base64,{encoded}"

        return [
            Chunk(
                content=data_uri,
                metadata={
                    "source_path": source_name,
                    "type": "image",
                    "format": save_fmt,
                    "original_size": list(original_size),
                    "encoded_size": len(encoded),
                    "mime_type": mime,
                },
                is_image=True,
            )
        ]
