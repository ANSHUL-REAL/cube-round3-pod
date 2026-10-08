"""Private photo storage (§6.5).

- Buckets are private (`public: false`); a bucket found public is an error, never silently accepted.
- Object keys: `org/{org_id}/returns/{return_id}/{uuid4}.{ext}`. The uuid4 makes keys unguessable; the org
  prefix is also enforced by a CHECK constraint on rm.return_photos.
- Nobody gets an object except through the backend (see signed_urls.py). Signed URLs are bearer tokens:
  they are never logged.
"""

from __future__ import annotations

from dataclasses import dataclass

from storage3.exceptions import StorageApiError

from returns_manager.config import Settings
from returns_manager.db.tenant import validate_org_id
from returns_manager.ids import new_storage_uuid
from returns_manager.storage.service_client import ServiceCredentials

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
ALLOWED_MIME = ["image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"]
_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/heic": "heic",
    "image/heif": "heif",
}


class BucketNotPrivate(RuntimeError):
    pass


@dataclass(frozen=True)
class StoredObject:
    bucket: str
    key: str


def photo_object_key(org_id: str, return_id: str, mime: str) -> str:
    validate_org_id(org_id)
    if not return_id or "/" in return_id:
        raise ValueError("invalid return_id")
    return f"org/{org_id}/returns/{return_id}/{new_storage_uuid()}.{_EXT[mime]}"


class PhotoStorage:
    def __init__(self, settings: Settings) -> None:
        self._creds = ServiceCredentials(settings)
        self.photos_bucket = settings.rm_storage_bucket_photos
        self.reference_bucket = settings.rm_storage_bucket_reference
        self.base_url = self._creds.base_url

    async def ensure_buckets(self) -> list[str]:
        """Create the private buckets if missing. Returns the ids created. Refuses a public bucket."""
        created: list[str] = []
        client = self._creds.storage_client()
        existing = {b.id: b for b in await client.list_buckets()}
        for bucket_id in (self.photos_bucket, self.reference_bucket):
            bucket = existing.get(bucket_id)
            if bucket is None:
                await client.create_bucket(
                    bucket_id,
                    options={
                        "public": False,
                        "file_size_limit": MAX_UPLOAD_BYTES,
                        "allowed_mime_types": ALLOWED_MIME,
                    },
                )
                created.append(bucket_id)
            elif bucket.public:
                raise BucketNotPrivate(f"storage bucket '{bucket_id}' is public; it must be private")
        return created

    async def bucket_is_private(self, bucket_id: str) -> bool:
        bucket = await self._creds.storage_client().get_bucket(bucket_id)
        return not bucket.public

    async def put(self, bucket: str, key: str, data: bytes, mime: str) -> StoredObject:
        if mime not in ALLOWED_MIME:
            raise ValueError(f"unsupported mime {mime}")
        await (
            self._creds.storage_client()
            .from_(bucket)
            .upload(key, data, file_options={"content-type": mime, "upsert": "false"})
        )
        return StoredObject(bucket, key)

    async def get(self, bucket: str, key: str) -> bytes:
        """Download an object (backend-internal use only; callers resolve `key` from an RLS-scoped row)."""
        data = await self._creds.storage_client().from_(bucket).download(key)
        return bytes(data)

    async def signed_url(self, bucket: str, key: str, ttl_s: int) -> str:
        """A short-TTL signed download URL. Callers must have checked ownership first (signed_urls.py)."""
        res = await self._creds.storage_client().from_(bucket).create_signed_url(key, ttl_s)
        url = res.get("signedURL") or res.get("signedUrl")
        if not url:
            raise StorageApiError("no signed URL returned", "no_signed_url", 500)
        return url

    async def ping(self) -> bool:
        try:
            await self._creds.storage_client().list_buckets()
            return True
        except Exception:
            return False
