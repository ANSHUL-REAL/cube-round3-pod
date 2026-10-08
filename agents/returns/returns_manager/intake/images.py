"""Image handling, sniffing, safe decoding, and preprocessing (§9.1)."""

from __future__ import annotations

import contextlib
import io
from dataclasses import dataclass
from typing import Any

import imagehash
import pillow_heif
from PIL import ExifTags, Image, ImageOps

from returns_manager.canonical.hashing import sha256_hex
from returns_manager.errors import PayloadTooLarge, UnsupportedMediaType

# Register HEIF opener once at module load
pillow_heif.register_heif_opener()

# Safety cap against decompression bombs (§9.1)
Image.MAX_IMAGE_PIXELS = 60_000_000

MAX_PHOTO_BYTES = 20 * 1024 * 1024  # 20 MB cap


@dataclass(frozen=True)
class ProcessedImage:
    mime: str
    original_bytes: bytes
    original_width: int
    original_height: int
    sha256_original: str
    analysis_bytes: bytes
    analysis_width: int
    analysis_height: int
    sha256_analysis: str
    phash: str  # 64-bit binary string, e.g. '0101...'
    exif_summary: dict[str, Any]
    transform_version: str = "v1"


def sniff_mime(data: bytes) -> str:
    """Sniff image MIME type using magic bytes. Client Content-Type is ignored (§9.1)."""
    if len(data) >= 3 and data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 8 and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        major_brand = data[8:12]
        if major_brand in (b"heic", b"heix", b"hevc", b"heim", b"heis"):
            return "image/heic"
        if major_brand in (b"mif1", b"msf1"):
            return "image/heif"
    raise UnsupportedMediaType("Unsupported image format. Allowed formats: JPEG, PNG, WebP, HEIC, HEIF")


def validate_photo_bytes(data: bytes) -> str:
    """Validate photo file size and format."""
    if len(data) > MAX_PHOTO_BYTES:
        raise PayloadTooLarge(f"Photo size ({len(data)} bytes) exceeds the 20 MB cap")
    return sniff_mime(data)


def sanitize_exif(img: Image.Image) -> dict[str, Any]:
    """Extract a sanitized EXIF summary, strictly excluding GPS coordinates (§9.1)."""
    exif_data = img.getexif()
    if not exif_data:
        return {}

    summary: dict[str, Any] = {}
    for tag_id, value in exif_data.items():
        tag_name = ExifTags.TAGS.get(tag_id, str(tag_id))
        # Exclude GPSInfo tag (tag 34853 / 'GPSInfo')
        if tag_name.lower() == "gpsinfo" or tag_id == 34853:
            continue
        # Only keep common harmless metadata
        if tag_name in ("Make", "Model", "DateTime", "Software", "Orientation", "LensModel"):
            # Convert bytes to string if needed
            if isinstance(value, bytes):
                with contextlib.suppress(UnicodeDecodeError, AttributeError):
                    summary[tag_name] = value.decode("utf-8", errors="replace").strip("\x00")
            elif isinstance(value, (str, int, float)):
                summary[tag_name] = value

    return summary


def process_photo(
    data: bytes,
    max_long_edge: int = 1568,
    jpeg_quality: int = 88,
) -> ProcessedImage:
    """Safely decode, transpose EXIF, downscale analysis copy, and compute hashes."""
    mime = validate_photo_bytes(data)

    try:
        raw_img = Image.open(io.BytesIO(data))
    except Exception as exc:
        raise UnsupportedMediaType(f"Failed to decode image: {exc}") from exc

    # Reject animated / multi-frame images
    if getattr(raw_img, "is_animated", False) or getattr(raw_img, "n_frames", 1) > 1:
        raise UnsupportedMediaType("Multi-frame or animated images are not allowed")

    orig_width, orig_height = raw_img.size

    # Sanitize EXIF before transposition
    exif_summary = sanitize_exif(raw_img)

    # Respect EXIF orientation
    transposed = ImageOps.exif_transpose(raw_img)
    if transposed is None:
        transposed = raw_img

    # Convert to standard RGB
    rgb_img = transposed.convert("RGB")

    # Compute 64-bit perceptual hash
    ph = imagehash.phash(rgb_img)
    phash_bin = bin(int(str(ph), 16))[2:].zfill(64)

    # Downscale for analysis copy
    long_edge = max(rgb_img.width, rgb_img.height)
    if long_edge > max_long_edge:
        scale = max_long_edge / float(long_edge)
        new_w = max(1, round(rgb_img.width * scale))
        new_h = max(1, round(rgb_img.height * scale))
        analysis_img = rgb_img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    else:
        analysis_img = rgb_img

    out_buf = io.BytesIO()
    # Strip all metadata from analysis copy
    analysis_img.save(out_buf, format="JPEG", quality=jpeg_quality, optimize=True)
    analysis_bytes = out_buf.getvalue()

    return ProcessedImage(
        mime=mime,
        original_bytes=data,
        original_width=orig_width,
        original_height=orig_height,
        sha256_original=sha256_hex(data),
        analysis_bytes=analysis_bytes,
        analysis_width=analysis_img.width,
        analysis_height=analysis_img.height,
        sha256_analysis=sha256_hex(analysis_bytes),
        phash=phash_bin,
        exif_summary=exif_summary,
        transform_version="v1",
    )
