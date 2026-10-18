"""Pure, bounded PNG/JPEG previews, separate from original file downloads."""

from __future__ import annotations

from collections.abc import Buffer
from dataclasses import dataclass, field
from io import BytesIO
from typing import Literal

from PIL import Image, ImageOps

MAX_RASTER_INPUT_BYTES = 8 * 1024 * 1024
MAX_RASTER_PIXELS = 16_000_000
MAX_RASTER_OUTPUT_BYTES = 16 * 1024 * 1024


class RasterPreviewUnavailableError(ValueError):
    """The original file remains downloadable even when preview is rejected."""


@dataclass(frozen=True, slots=True)
class RasterPreview:
    content: bytes = field(repr=False)
    media_type: Literal["image/png", "image/jpeg"]
    width: int
    height: int


class _BoundedOutput(BytesIO):
    def write(self, data: Buffer, /) -> int:
        if self.tell() + memoryview(data).nbytes > MAX_RASTER_OUTPUT_BYTES:
            raise RasterPreviewUnavailableError("Raster preview exceeds its output budget.")
        return super().write(data)


def _check_image(image: Image.Image) -> None:
    if image.width <= 0 or image.height <= 0 or image.width * image.height > MAX_RASTER_PIXELS:
        raise RasterPreviewUnavailableError("Raster preview exceeds its pixel budget.")
    if image.format not in {"PNG", "JPEG"} or getattr(image, "n_frames", 1) != 1:
        raise RasterPreviewUnavailableError("Raster preview requires a single PNG/JPEG frame.")


def build_raster_preview(content: bytes) -> RasterPreview:
    """Verify, fully decode, then re-encode one raster without source metadata.

    No extension or caller-supplied MIME is trusted. Pillow's global safety
    limits and truncated-image policy are never changed. Budget rejection is
    a preview-only result and must not prevent upload or original download.
    """

    if type(content) is not bytes or len(content) > MAX_RASTER_INPUT_BYTES:
        raise RasterPreviewUnavailableError("Raster preview exceeds its input budget.")
    try:
        with Image.open(BytesIO(content), formats=("PNG", "JPEG")) as image:
            _check_image(image)
            image.verify()
        # verify() consumes the decoder. Reopen the exact same immutable bytes
        # and load every pixel; successful header identification is not enough.
        with Image.open(BytesIO(content), formats=("PNG", "JPEG")) as image:
            _check_image(image)
            image.load()
            image_format = image.format
            mode = "RGB"
            if image_format == "PNG" and ("A" in image.getbands() or "transparency" in image.info):
                mode = "RGBA"
            # Apply the source's EXIF orientation before discarding metadata.
            # Transposition preserves pixel count; input/pixel/output budgets
            # still apply. A fresh pixel-only image has no source metadata.
            with (
                ImageOps.exif_transpose(image) as oriented,
                oriented.convert(mode) as converted,
                Image.frombytes(mode, converted.size, converted.tobytes()) as clean,
                _BoundedOutput() as output,
            ):
                if image_format == "PNG":
                    clean.save(output, format="PNG", optimize=False)
                    media_type: Literal["image/png", "image/jpeg"] = "image/png"
                else:
                    clean.save(output, format="JPEG", quality=90, optimize=False)
                    media_type = "image/jpeg"
                encoded = output.getvalue()
                return RasterPreview(encoded, media_type, clean.width, clean.height)
    except RasterPreviewUnavailableError:
        raise
    except (Image.DecompressionBombError, OSError, ValueError, SyntaxError, OverflowError):
        raise RasterPreviewUnavailableError(
            "Raster preview requires a valid PNG/JPEG image."
        ) from None
