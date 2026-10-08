"""Signed download URLs, issued only after an RLS-scoped ownership check (§6.5).

The photo row is looked up inside the caller's tenant transaction. Row-level security makes another org's
photo indistinguishable from a photo that does not exist, so both answer NotFound (HTTP 404, never 403).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from returns_manager.db.pool import Database
from returns_manager.errors import NotFound
from returns_manager.security.roles import Permission, Principal, require
from returns_manager.storage.photos import PhotoStorage


@dataclass(frozen=True)
class SignedPhotoUrl:
    photo_id: str
    variant: str
    url: str
    expires_at: datetime


async def signed_url_for_photo(
    db: Database,
    storage: PhotoStorage,
    principal: Principal,
    photo_id: str,
    variant: Literal["original", "analysis"],
    ttl_s: int,
) -> SignedPhotoUrl:
    require(principal, Permission.PHOTO_URL)
    async with db.transaction(principal.org_id) as conn:
        cur = await conn.execute(
            "SELECT storage_key_original, storage_key_analysis FROM rm.return_photos WHERE photo_id = %s",
            (photo_id,),
        )
        row = await cur.fetchone()
    if row is None:
        raise NotFound("photo not found")
    key = row["storage_key_original"] if variant == "original" else row["storage_key_analysis"]
    if key is None:
        raise NotFound("photo variant not available")
    url = await storage.signed_url(storage.photos_bucket, key, ttl_s)
    return SignedPhotoUrl(photo_id, variant, url, datetime.now(UTC) + timedelta(seconds=ttl_s))
