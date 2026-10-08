"""Downloads a photo from an http(s) URL and re-encodes it the same way the real intake
pipeline prepares a photo for the model (`RM_ANALYSIS_LONG_EDGE`, JPEG). Never trusts the
URL's declared content-type; only a photo that Pillow can actually decode is accepted.
"""

from __future__ import annotations

import io

import httpx
from PIL import Image

from returns_manager.errors import ReturnsManagerError

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # matches llm.context.MAX_INLINE_BYTES headroom


class ImageFetchError(ReturnsManagerError):
    """A photo URL could not be fetched or decoded as an image. Never raised past the row
    it belongs to - the caller marks that return `uncertain`/`pending_review` and moves on
    (fail open, never a guessed verdict from a missing photo)."""


async def fetch_image(
    url: str,
    client: httpx.AsyncClient,
    *,
    long_edge: int,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
) -> bytes:
    url = url.strip()
    if not url:
        raise ImageFetchError("empty image URL")
    fetch_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    }
    try:
        resp = await client.get(url, headers=fetch_headers, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise ImageFetchError(f"could not fetch {url!r}: {exc}") from exc
    if resp.status_code != 200:
        raise ImageFetchError(f"{url!r} returned HTTP {resp.status_code}")
    data = resp.content
    if not data:
        raise ImageFetchError(f"{url!r} returned an empty body")
    if len(data) > max_bytes:
        raise ImageFetchError(f"{url!r} is {len(data)} bytes, over the {max_bytes} byte cap")
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            rgb = img.convert("RGB")
    except Exception as exc:  # Pillow raises several exception types for bad image data
        raise ImageFetchError(f"{url!r} did not decode as an image: {type(exc).__name__}: {exc}") from exc
    w, h = rgb.size
    scale = long_edge / max(w, h)
    if scale < 1:
        rgb = rgb.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    rgb.save(out, format="JPEG", quality=90)
    return out.getvalue()
