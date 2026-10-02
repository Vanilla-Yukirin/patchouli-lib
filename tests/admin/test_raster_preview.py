import struct
import zlib
from io import BytesIO

import pytest
from PIL import Image, ImageFile, PngImagePlugin

from patchouli_lib.admin import raster_preview
from patchouli_lib.admin.raster_preview import (
    MAX_RASTER_INPUT_BYTES,
    MAX_RASTER_PIXELS,
    RasterPreviewUnavailableError,
    build_raster_preview,
)


def _image_bytes(image_format: str = "PNG", mode: str = "RGB") -> bytes:
    with Image.new(mode, (3, 2)) as image, BytesIO() as output:
        image.save(output, format=image_format)
        return output.getvalue()


@pytest.mark.parametrize(
    "image_format,mode,mime",
    [("PNG", "RGB", "image/png"), ("PNG", "RGBA", "image/png"), ("JPEG", "RGB", "image/jpeg")],
)
def test_fully_decodes_and_reencodes_supported_images(
    image_format: str, mode: str, mime: str
) -> None:
    original = _image_bytes(image_format, mode)
    preview = build_raster_preview(original)
    assert preview.media_type == mime
    assert (preview.width, preview.height) == (3, 2)
    assert "content=" not in repr(preview)
    with Image.open(BytesIO(preview.content)) as decoded:
        decoded.load()
        assert decoded.format == image_format
        assert decoded.mode == mode
        assert decoded.size == (3, 2)
    assert original == _image_bytes(image_format, mode)


def test_png_metadata_and_appended_active_bytes_are_not_forwarded() -> None:
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "private synthetic metadata")
    with Image.new("RGB", (2, 2)) as image, BytesIO() as output:
        image.save(output, format="PNG", pnginfo=info, icc_profile=b"synthetic ICC bytes")
        original = output.getvalue() + b"<script>synthetic tail</script>"
    preview = build_raster_preview(original)
    assert b"private synthetic metadata" not in preview.content
    assert b"synthetic tail" not in preview.content
    with Image.open(BytesIO(preview.content)) as decoded:
        decoded.load()
        assert decoded.info == {}


def test_jpeg_exif_and_comments_are_removed() -> None:
    exif = Image.Exif()
    exif[270] = "synthetic description"
    with Image.new("RGB", (2, 2)) as image, BytesIO() as output:
        image.save(output, format="JPEG", exif=exif, comment=b"synthetic comment")
        original = output.getvalue()
    preview = build_raster_preview(original)
    assert b"synthetic description" not in preview.content
    assert b"synthetic comment" not in preview.content
    with Image.open(BytesIO(preview.content)) as decoded:
        decoded.load()
        assert not decoded.getexif()
        assert "comment" not in decoded.info
        assert "icc_profile" not in decoded.info


def test_palette_transparency_is_preserved_without_metadata() -> None:
    with Image.new("P", (2, 2)) as image, BytesIO() as output:
        image.putpalette([0, 0, 0] * 256)
        image.save(output, format="PNG", transparency=0)
        original = output.getvalue()
    preview = build_raster_preview(original)
    with Image.open(BytesIO(preview.content)) as decoded:
        decoded.load()
        assert decoded.mode == "RGBA"
        assert decoded.getpixel((0, 0)) == (0, 0, 0, 0)


@pytest.mark.parametrize("orientation", [6, 2])
def test_exif_orientation_is_applied_before_metadata_removal(orientation: int) -> None:
    exif = Image.Exif()
    exif[274] = orientation
    exif[270] = "synthetic orientation description"
    with Image.new("RGB", (24, 16), "red") as image, BytesIO() as output:
        if orientation == 6:
            image.paste("blue", (0, 8, 24, 16))  # Clockwise: blue left, red right.
        else:
            image.paste("blue", (12, 0, 24, 16))  # Mirror: blue left, red right.
        image.save(output, format="JPEG", quality=100, subsampling=0, exif=exif)
        original = output.getvalue()
    preview = build_raster_preview(original)
    expected_size = (16, 24) if orientation == 6 else (24, 16)
    assert (preview.width, preview.height) == expected_size
    assert preview.media_type == "image/jpeg"
    with Image.open(BytesIO(preview.content)) as decoded:
        decoded.load()
        assert decoded.size == expected_size
        assert not decoded.getexif()
        left = decoded.getpixel((3, expected_size[1] // 2))
        right = decoded.getpixel((expected_size[0] - 4, expected_size[1] // 2))
        # JPEG re-encoding is lossy; compare clear color regions, not exact bytes.
        assert isinstance(left, tuple) and isinstance(right, tuple)
        assert left[2] > 200 and left[0] < 40
        assert right[0] > 200 and right[2] < 40
    assert b"synthetic orientation description" not in preview.content
    # The immutable original remains untouched, including its original pose tag.
    with Image.open(BytesIO(original)) as source:
        assert source.size == (24, 16)
        assert source.getexif()[274] == orientation


@pytest.mark.parametrize(
    "content",
    [b"", b"not an image", b"\x89PNG\r\n\x1a\n", b"<svg xmlns='http://www.w3.org/2000/svg'/>"],
)
def test_malformed_and_svg_bytes_are_rejected(content: bytes) -> None:
    with pytest.raises(RasterPreviewUnavailableError):
        build_raster_preview(content)


def test_other_actual_format_and_truncated_decode_are_rejected() -> None:
    with pytest.raises(RasterPreviewUnavailableError):
        build_raster_preview(_image_bytes("GIF"))
    with pytest.raises(RasterPreviewUnavailableError):
        build_raster_preview(_image_bytes("JPEG")[:-20])
    with pytest.raises(RasterPreviewUnavailableError):
        build_raster_preview(_image_bytes("PNG")[:-20])


def test_multiple_png_frames_are_rejected() -> None:
    with (
        Image.new("RGB", (2, 2), "red") as first,
        Image.new("RGB", (2, 2), "blue") as second,
        BytesIO() as output,
    ):
        first.save(output, format="PNG", save_all=True, append_images=[second])
        animated = output.getvalue()
    with pytest.raises(RasterPreviewUnavailableError, match="single PNG/JPEG frame"):
        build_raster_preview(animated)


def test_input_budget_is_preview_only_and_exact_boundary_is_allowed() -> None:
    original = _image_bytes()
    padded = original + b"\x00" * (MAX_RASTER_INPUT_BYTES - len(original))
    assert build_raster_preview(padded).width == 3
    with pytest.raises(RasterPreviewUnavailableError, match="input budget"):
        build_raster_preview(padded + b"\x00")
    assert len(padded) == MAX_RASTER_INPUT_BYTES


def test_pixel_budget_rejects_large_header_before_pixel_decode() -> None:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))
        )

    width, height = 4001, 4000
    assert width * height > MAX_RASTER_PIXELS
    # Deliberately no actual large raster allocation. Header parsing succeeds;
    # the helper must reject the dimensions before verify/load sees invalid rows.
    header = struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0)
    content = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
    content += chunk(b"IDAT", zlib.compress(b"invalid rows")) + chunk(b"IEND", b"")
    with pytest.raises(RasterPreviewUnavailableError, match="pixel budget"):
        build_raster_preview(content)


def test_output_budget_is_enforced_during_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(raster_preview, "MAX_RASTER_OUTPUT_BYTES", 32)
    with pytest.raises(RasterPreviewUnavailableError, match="output budget"):
        build_raster_preview(_image_bytes())


def test_pillow_global_safety_settings_are_never_mutated() -> None:
    pixel_limit = Image.MAX_IMAGE_PIXELS
    allow_truncated = ImageFile.LOAD_TRUNCATED_IMAGES
    build_raster_preview(_image_bytes())
    with pytest.raises(RasterPreviewUnavailableError):
        build_raster_preview(b"bad bytes")
    assert pixel_limit == Image.MAX_IMAGE_PIXELS
    assert allow_truncated == ImageFile.LOAD_TRUNCATED_IMAGES
