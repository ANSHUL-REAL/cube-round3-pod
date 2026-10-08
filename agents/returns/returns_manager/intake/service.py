"""Intake service (§9): returns, photos, observations, and submit."""

from __future__ import annotations

import io
import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from PIL import Image

from returns_manager.db.pool import Database
from returns_manager.errors import BadRequest, Conflict, NotFound, QualityGateRefusal
from returns_manager.ids import new_id, new_storage_uuid
from returns_manager.intake.images import process_photo
from returns_manager.intake.quality import assess_photo_quality, load_thresholds
from returns_manager.jobs.queue import JobQueue, compute_job_priority
from returns_manager.storage.photos import PhotoStorage, photo_object_key
from returns_manager.vision.barcode import read_barcodes_from_image

_RECORD_ID_RE = re.compile(r"^RTN-[0-9]{4}(-[0-9]+)?$")
VALID_OBSERVED_STATES = {
    "factory_sealed",
    "opened_unused",
    "signs_of_use",
    "damaged",
    "empty_box",
    "uncertain",
}


@dataclass(frozen=True)
class ReturnRecord:
    return_id: str
    org_id: str
    record_id: str
    unit_id: str
    order_id: str
    ordered_sku: str | None
    ordered_asin: str | None
    return_seq: int
    status: str
    created_by: str
    created_at: str
    submitted_at: str | None = None
    finalized_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PhotoRecord:
    photo_id: str
    slot: int
    alias: str
    mime: str
    bytes: int
    width: int
    height: int
    sha256_original: str
    sha256_analysis: str | None
    phash: str | None
    quality_status: str
    quality: dict[str, Any]
    retake_of: str | None
    superseded: bool
    uploaded_at: str
    storage_key_original: str
    storage_key_analysis: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PhotoUploadResult:
    photo_id: str
    slot: int
    alias: str
    quality: dict[str, Any]
    quality_status: str
    retake_guidance: list[dict[str, str]]
    idempotent_replay: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ObservationRecord:
    obs_id: str
    return_id: str
    observed_state: str
    operator_id: str
    recorded_at: str
    note: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SubmitResult:
    job_id: str
    return_id: str
    status: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReturnDetails:
    return_data: ReturnRecord
    photos: list[PhotoRecord]
    observation: ObservationRecord | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "return": self.return_data.to_dict(),
            "photos": [p.to_dict() for p in self.photos],
            "observation": self.observation.to_dict() if self.observation else None,
        }


def format_record_id(unit_id: str, return_seq: int = 1, record_id: str | None = None) -> str:
    """Format and validate record_id matching `RTN-<4-digit unit number>(-<seq>)?`."""
    if record_id:
        if not _RECORD_ID_RE.match(record_id):
            raise BadRequest(f"Invalid record_id '{record_id}': must match RTN-<4-digit>(-<seq>)?")
        return record_id

    # The unit number is the unit_id's digits (UNIT-0014 -> 0014). Without digits there is no deterministic
    # record_id, so the caller must pass one explicitly.
    digits = "".join(c for c in unit_id if c.isdigit())
    if not digits or int(digits[-4:]) == 0:
        raise BadRequest(
            f"unit_id '{unit_id}' has no unit number; pass record_id explicitly (RTN-<4-digit>(-<seq>)?)"
        )
    unit_num = int(digits[-4:])

    if return_seq == 1:
        return f"RTN-{unit_num:04d}"
    return f"RTN-{unit_num:04d}-{return_seq}"


class IntakeService:
    def __init__(
        self,
        db: Database,
        storage: PhotoStorage | None = None,
        *,
        analysis_long_edge: int = 1568,
        job_max_attempts: int = 5,
        high_value_threshold_minor: int = 500000,
    ) -> None:
        self.db = db
        self.storage = storage
        self.analysis_long_edge = analysis_long_edge
        self.job_max_attempts = job_max_attempts
        self.high_value_threshold_minor = high_value_threshold_minor

    async def create_return(
        self,
        org_id: str,
        actor_id: str,
        order_id: str,
        unit_id: str,
        ordered_sku: str | None = None,
        ordered_asin: str | None = None,
        return_seq: int = 1,
        record_id: str | None = None,
    ) -> ReturnRecord:
        """Create a new return in status 'capturing' (§7.3)."""
        rec_id = format_record_id(unit_id, return_seq, record_id)

        async with self.db.transaction(org_id) as conn:
            # Look up catalogue order if SKU/ASIN omitted
            sku = ordered_sku
            asin = ordered_asin
            if not sku or not asin:
                res = await conn.execute(
                    """
                    SELECT ordered_sku, ordered_asin FROM rm.orders
                    WHERE org_id = %s AND order_id = %s AND unit_id = %s
                    """,
                    (org_id, order_id, unit_id),
                )
                row = await res.fetchone()
                if row:
                    sku = sku or row["ordered_sku"]
                    asin = asin or row["ordered_asin"]

            ret_id = new_id()
            await conn.execute(
                """
                INSERT INTO rm.returns (
                    return_id, org_id, client_id, record_id, unit_id, order_id,
                    ordered_sku, ordered_asin, return_seq, status, created_by
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'capturing', %s)
                """,
                (
                    ret_id,
                    org_id,
                    org_id,
                    rec_id,
                    unit_id,
                    order_id,
                    sku,
                    asin,
                    return_seq,
                    actor_id,
                ),
            )

            res = await conn.execute(
                "SELECT * FROM rm.returns WHERE org_id = %s AND return_id = %s",
                (org_id, ret_id),
            )
            r = await res.fetchone()
            assert r is not None

            return ReturnRecord(
                return_id=r["return_id"],
                org_id=r["org_id"],
                record_id=r["record_id"],
                unit_id=r["unit_id"],
                order_id=r["order_id"],
                ordered_sku=r["ordered_sku"],
                ordered_asin=r["ordered_asin"],
                return_seq=r["return_seq"],
                status=r["status"],
                created_by=r["created_by"],
                created_at=r["created_at"].isoformat(),
            )

    async def get_return(self, org_id: str, return_id: str) -> ReturnDetails:
        """Fetch return details, active and superseded photos, and operator observations."""
        async with self.db.transaction(org_id) as conn:
            res = await conn.execute(
                "SELECT * FROM rm.returns WHERE org_id = %s AND return_id = %s",
                (org_id, return_id),
            )
            r = await res.fetchone()
            if not r:
                raise NotFound(f"Return '{return_id}' not found")

            ret_rec = ReturnRecord(
                return_id=r["return_id"],
                org_id=r["org_id"],
                record_id=r["record_id"],
                unit_id=r["unit_id"],
                order_id=r["order_id"],
                ordered_sku=r["ordered_sku"],
                ordered_asin=r["ordered_asin"],
                return_seq=r["return_seq"],
                status=r["status"],
                created_by=r["created_by"],
                created_at=r["created_at"].isoformat(),
                submitted_at=r["submitted_at"].isoformat() if r["submitted_at"] else None,
                finalized_at=r["finalized_at"].isoformat() if r["finalized_at"] else None,
            )

            res_p = await conn.execute(
                """
                SELECT photo_id, slot, alias, mime, bytes, width, height, sha256_original,
                       sha256_analysis, phash::text, quality_status, quality, retake_of,
                       superseded, uploaded_at, storage_key_original, storage_key_analysis
                FROM rm.return_photos
                WHERE org_id = %s AND return_id = %s
                ORDER BY slot ASC, uploaded_at ASC
                """,
                (org_id, return_id),
            )
            photos: list[PhotoRecord] = []
            for p in await res_p.fetchall():
                # Signed URLs are issued only on request, per photo (GET /photos/{id}/url), never inline.
                photos.append(
                    PhotoRecord(
                        photo_id=p["photo_id"],
                        slot=p["slot"],
                        alias=p["alias"],
                        mime=p["mime"],
                        bytes=p["bytes"],
                        width=p["width"],
                        height=p["height"],
                        sha256_original=p["sha256_original"],
                        sha256_analysis=p["sha256_analysis"],
                        phash=p["phash"],
                        quality_status=p["quality_status"],
                        quality=p["quality"] or {},
                        retake_of=p["retake_of"],
                        superseded=p["superseded"],
                        uploaded_at=p["uploaded_at"].isoformat(),
                        storage_key_original=p["storage_key_original"],
                        storage_key_analysis=p["storage_key_analysis"],
                    )
                )

            res_obs = await conn.execute(
                """
                SELECT * FROM rm.operator_observations
                WHERE org_id = %s AND return_id = %s
                ORDER BY recorded_at DESC LIMIT 1
                """,
                (org_id, return_id),
            )
            obs_row = await res_obs.fetchone()
            obs_rec = None
            if obs_row:
                obs_rec = ObservationRecord(
                    obs_id=obs_row["obs_id"],
                    return_id=obs_row["return_id"],
                    observed_state=obs_row["observed_state"],
                    operator_id=obs_row["operator_id"],
                    recorded_at=obs_row["recorded_at"].isoformat(),
                    note=obs_row["note"],
                )

            return ReturnDetails(
                return_data=ret_rec,
                photos=photos,
                observation=obs_rec,
            )

    async def upload_photo(
        self,
        org_id: str,
        actor_id: str,
        return_id: str,
        photo_bytes: bytes,
        idempotency_key: str | None = None,
        client_transform: dict[str, Any] | None = None,
        role_hint: str | None = None,
        retake_of: str | None = None,
    ) -> PhotoUploadResult:
        """Handle resilient photo upload (§9.1): validation, storage, and quality assessment."""
        async with self.db.transaction(org_id) as conn:
            # 1. Auth & ownership
            res = await conn.execute(
                "SELECT status FROM rm.returns WHERE org_id = %s AND return_id = %s",
                (org_id, return_id),
            )
            ret_row = await res.fetchone()
            if not ret_row:
                raise NotFound(f"Return '{return_id}' not found")
            if ret_row["status"] != "capturing":
                raise Conflict(
                    f"Return is in status '{ret_row['status']}'; "
                    "photos can only be uploaded in 'capturing' status"
                )

            # 2. Idempotency replay check
            if idempotency_key:
                res_idem = await conn.execute(
                    """
                    SELECT photo_id, slot, alias, quality, quality_status
                    FROM rm.return_photos
                    WHERE org_id = %s AND return_id = %s AND idempotency_key = %s
                    """,
                    (org_id, return_id, idempotency_key),
                )
                existing_idem = await res_idem.fetchone()
                if existing_idem:
                    guidance = (
                        existing_idem["quality"].get("guidance", []) if existing_idem["quality"] else []
                    )
                    return PhotoUploadResult(
                        photo_id=existing_idem["photo_id"],
                        slot=existing_idem["slot"],
                        alias=existing_idem["alias"],
                        quality=existing_idem["quality"] or {},
                        quality_status=existing_idem["quality_status"],
                        retake_guidance=guidance,
                        idempotent_replay=True,
                    )

            # 3. Process photo safely
            processed = process_photo(photo_bytes, max_long_edge=self.analysis_long_edge)

            # 4. Dedupe by sha256_original within this return
            res_hash = await conn.execute(
                """
                SELECT photo_id, slot, alias, quality, quality_status
                FROM rm.return_photos
                WHERE org_id = %s AND return_id = %s AND sha256_original = %s
                """,
                (org_id, return_id, processed.sha256_original),
            )
            existing_hash = await res_hash.fetchone()
            if existing_hash:
                guidance = existing_hash["quality"].get("guidance", []) if existing_hash["quality"] else []
                return PhotoUploadResult(
                    photo_id=existing_hash["photo_id"],
                    slot=existing_hash["slot"],
                    alias=existing_hash["alias"],
                    quality=existing_hash["quality"] or {},
                    quality_status=existing_hash["quality_status"],
                    retake_guidance=guidance,
                    idempotent_replay=False,
                )

            # 5. Slots & aliases
            res_active = await conn.execute(
                """
                SELECT photo_id, slot, alias, phash::text
                FROM rm.return_photos
                WHERE org_id = %s AND return_id = %s AND superseded = false
                """,
                (org_id, return_id),
            )
            active_photos = await res_active.fetchall()

            if retake_of:
                target_old = next((p for p in active_photos if p["photo_id"] == retake_of), None)
                if not target_old:
                    raise NotFound(f"Photo '{retake_of}' to retake does not exist or is already superseded")
                # Supersede old photo
                await conn.execute(
                    "UPDATE rm.return_photos SET superseded = true WHERE org_id = %s AND photo_id = %s",
                    (org_id, retake_of),
                )
                slot = target_old["slot"]
                alias = target_old["alias"]
            else:
                used_slots = {p["slot"] for p in active_photos}
                available_slots = [s for s in (1, 2, 3) if s not in used_slots]
                if not available_slots:
                    raise Conflict(
                        "Return already has 3 active photos (slots 1-3). Use retake_of to replace a photo."
                    )
                slot = min(available_slots)
                alias = f"P{slot}"

            # 6. Storage keys and upload
            if self.storage:
                key_orig = photo_object_key(org_id, return_id, processed.mime)
                key_analysis = photo_object_key(org_id, return_id, "image/jpeg")
                await self.storage.put(
                    self.storage.photos_bucket, key_orig, processed.original_bytes, processed.mime
                )
                await self.storage.put(
                    self.storage.photos_bucket, key_analysis, processed.analysis_bytes, "image/jpeg"
                )
            else:
                ext_map = {
                    "image/jpeg": "jpg",
                    "image/png": "png",
                    "image/webp": "webp",
                    "image/heic": "heic",
                    "image/heif": "heif",
                }
                ext = ext_map.get(processed.mime, "jpg")
                key_orig = f"org/{org_id}/returns/{return_id}/{new_storage_uuid()}.{ext}"
                key_analysis = f"org/{org_id}/returns/{return_id}/{new_storage_uuid()}.jpg"

            # 7. Quality gate evaluation
            other_set_phashes = [
                p["phash"] for p in active_photos if p["photo_id"] != retake_of and p["phash"]
            ]
            res_org_phashes = await conn.execute(
                """
                SELECT phash::text FROM rm.return_photos
                WHERE org_id = %s AND return_id <> %s
                  AND uploaded_at >= now() - make_interval(days => %s)
                  AND phash IS NOT NULL
                ORDER BY uploaded_at DESC
                LIMIT 500
                """,
                (org_id, return_id, load_thresholds().reused_window_days),
            )
            org_30d_phashes = [r["phash"] for r in await res_org_phashes.fetchall() if r["phash"]]

            quality_report = assess_photo_quality(
                image_bytes=processed.original_bytes,
                phash=processed.phash,
                orig_width=processed.original_width,
                orig_height=processed.original_height,
                other_set_phashes=other_set_phashes,
                org_recent_phashes=org_30d_phashes,
            )

            # 8. Barcode scan
            with Image.open(io.BytesIO(processed.original_bytes)) as pil_scan:
                barcodes = read_barcodes_from_image(pil_scan)
            if barcodes:
                quality_report.metrics["barcodes"] = [b.to_dict() for b in barcodes]

            # 9. Insert into rm.return_photos
            new_photo_id = new_id()
            await conn.execute(
                """
                INSERT INTO rm.return_photos (
                    photo_id, org_id, return_id, slot, alias, role_hint,
                    storage_key_original, storage_key_analysis, mime, bytes,
                    width, height, sha256_original, sha256_analysis, phash,
                    transform_version, client_transform, exif_summary, quality,
                    quality_status, uploaded_by, idempotency_key, retake_of, superseded
                ) VALUES (
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, %s::bit(64),
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, false
                )
                """,
                (
                    new_photo_id,
                    org_id,
                    return_id,
                    slot,
                    alias,
                    role_hint,
                    key_orig,
                    key_analysis,
                    processed.mime,
                    len(processed.original_bytes),
                    processed.original_width,
                    processed.original_height,
                    processed.sha256_original,
                    processed.sha256_analysis,
                    processed.phash,
                    processed.transform_version,
                    json.dumps(client_transform) if client_transform else None,
                    json.dumps(processed.exif_summary),
                    json.dumps(quality_report.to_dict()),
                    quality_report.status,
                    actor_id,
                    idempotency_key,
                    retake_of,
                ),
            )

            return PhotoUploadResult(
                photo_id=new_photo_id,
                slot=slot,
                alias=alias,
                quality=quality_report.to_dict(),
                quality_status=quality_report.status,
                retake_guidance=quality_report.guidance,
                idempotent_replay=False,
            )

    async def record_observation(
        self,
        org_id: str,
        actor_id: str,
        return_id: str,
        observed_state: str,
        note: str | None = None,
    ) -> ObservationRecord:
        """Store operator's one-tap observation (§9.4)."""
        if observed_state not in VALID_OBSERVED_STATES:
            states_str = ", ".join(sorted(VALID_OBSERVED_STATES))
            raise BadRequest(f"Invalid observed_state '{observed_state}'. Must be one of: {states_str}")

        async with self.db.transaction(org_id) as conn:
            res = await conn.execute(
                "SELECT status FROM rm.returns WHERE org_id = %s AND return_id = %s",
                (org_id, return_id),
            )
            if not await res.fetchone():
                raise NotFound(f"Return '{return_id}' not found")

            obs_id = new_id()
            await conn.execute(
                """
                INSERT INTO rm.operator_observations (
                    obs_id, org_id, return_id, observed_state, operator_id, note
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (obs_id, org_id, return_id, observed_state, actor_id, note),
            )

            res_obs = await conn.execute(
                "SELECT * FROM rm.operator_observations WHERE org_id = %s AND obs_id = %s",
                (org_id, obs_id),
            )
            r = await res_obs.fetchone()
            assert r is not None

            return ObservationRecord(
                obs_id=r["obs_id"],
                return_id=r["return_id"],
                observed_state=r["observed_state"],
                operator_id=r["operator_id"],
                recorded_at=r["recorded_at"].isoformat(),
                note=r["note"],
            )

    async def _job_priority(self, conn: Any, org_id: str, return_id: str) -> int:
        """Enqueue priority (§10.3) from the item's list price and the operator's latest observation."""
        cur = await conn.execute(
            """
            SELECT (p.card -> 'value' -> 'list_price' ->> 'amount_minor')::bigint AS list_price_minor
            FROM rm.returns r
            JOIN rm.products p ON p.org_id = r.org_id AND p.sku = r.ordered_sku AND p.active
            WHERE r.org_id = %s AND r.return_id = %s
            ORDER BY p.created_at DESC LIMIT 1
            """,
            (org_id, return_id),
        )
        price_row = await cur.fetchone()
        cur = await conn.execute(
            """
            SELECT observed_state FROM rm.operator_observations
            WHERE org_id = %s AND return_id = %s ORDER BY recorded_at DESC LIMIT 1
            """,
            (org_id, return_id),
        )
        obs_row = await cur.fetchone()
        return compute_job_priority(
            kind="judgment",
            item_value_minor=price_row["list_price_minor"] if price_row else None,
            high_value_threshold_minor=self.high_value_threshold_minor,
            observed_state=obs_row["observed_state"] if obs_row else None,
        )

    async def submit_return(
        self,
        org_id: str,
        actor_id: str,
        return_id: str,
        acknowledge_quality_warnings: bool = False,
        note: str | None = None,
    ) -> SubmitResult:
        """Submit return for inspection (§9.4), enqueuing a judgment job."""
        async with self.db.transaction(org_id) as conn:
            res = await conn.execute(
                "SELECT status FROM rm.returns WHERE org_id = %s AND return_id = %s",
                (org_id, return_id),
            )
            ret_row = await res.fetchone()
            if not ret_row:
                raise NotFound(f"Return '{return_id}' not found")

            curr_status = ret_row["status"]
            if curr_status == "queued":
                # Idempotent replay: find existing job
                res_job = await conn.execute(
                    """
                    SELECT job_id FROM rm.inspection_jobs
                    WHERE org_id = %s AND return_id = %s AND kind = 'judgment'
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (org_id, return_id),
                )
                j = await res_job.fetchone()
                if j is None:
                    raise Conflict("Return is queued but has no judgment job; ask an admin to requeue it")
                job_id = j["job_id"]
                return SubmitResult(job_id=job_id, return_id=return_id, status="queued")

            if curr_status != "capturing":
                raise Conflict(f"Return is in status '{curr_status}'; cannot submit")

            # Check photos count and quality
            res_photos = await conn.execute(
                """
                SELECT count(*) as total,
                       count(*) FILTER (WHERE quality_status <> 'fail') as non_fail
                FROM rm.return_photos
                WHERE org_id = %s AND return_id = %s AND superseded = false
                """,
                (org_id, return_id),
            )
            photo_counts = await res_photos.fetchone()
            non_fail = photo_counts["non_fail"] if photo_counts else 0

            # Set-level rule (§9.2): at least 2 photos not in 'fail' are needed
            if non_fail < 2 and not acknowledge_quality_warnings:
                raise QualityGateRefusal(
                    f"Submission requires at least 2 non-failing photos (found {non_fail}). "
                    "Retake failing photos or submit with acknowledge_quality_warnings=true."
                )

            priority = await self._job_priority(conn, org_id, return_id)
            job = await JobQueue.enqueue_job(
                conn,
                org_id=org_id,
                return_id=return_id,
                kind="judgment",
                priority=priority,
                max_attempts=self.job_max_attempts,
            )
            job_id = job.job_id

            return SubmitResult(job_id=job_id, return_id=return_id, status="queued")
