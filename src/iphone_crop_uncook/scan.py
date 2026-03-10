"""Core pipeline: query Photos, process screenshots, export lossless PNGs."""

import json
import logging
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from osxphotos.utils import uuid_to_shortuuid
from tqdm import tqdm

from . import config, db
from .uncook import find_crop_region, lossless_crop

log = logging.getLogger(__name__)

# osxphotos' internal SQLite isn't thread-safe when generating sidecar JSON,
# and AppleScript (use_photos_export) isn't thread-safe either.
_export_lock = threading.Lock()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    with db.get_conn() as conn:
        db.create_tables(conn)
        done = db.processed_uuids(conn)

    to_process = _query_photos(done)
    log.info("%d screenshots to process (%d already done)", len(to_process), len(done))

    if not to_process:
        with db.get_conn() as conn:
            db.set_last_sync(conn, datetime.now(timezone.utc))
        log.info("Nothing to do.")
        return

    processed, failed = _process_batch(to_process)
    log.info("Done: %d processed, %d failed", processed, failed)


def _query_photos(done: set[str]) -> list:
    """Load Photos library and return unprocessed screenshots."""
    import osxphotos

    with db.get_conn() as conn:
        last_sync = db.get_last_sync(conn)

    log.info("Loading Photos library...")
    photosdb = osxphotos.PhotosDB()

    # from_date is a coarse filter by creation date to speed up incremental runs.
    # UUID dedup handles edge cases (photos created before but added after last sync).
    kwargs = {"images": True, "movies": False}
    if last_sync:
        kwargs["from_date"] = last_sync
        log.info("Incremental sync: from_date=%s", last_sync.isoformat())

    all_photos = photosdb.photos(**kwargs)
    return [p for p in all_photos if p.screenshot and p.uuid not in done]


def _process_batch(photos: list) -> tuple[int, int]:
    """Process photos in parallel, persist to DB. Returns (processed, failed)."""
    processed = 0
    failed = 0
    failed_dates = []

    with ThreadPoolExecutor(max_workers=config.WORKERS) as pool:
        futures = {pool.submit(_process_one, p): p for p in photos}

        with db.get_conn() as conn:
            for future in tqdm(
                as_completed(futures), total=len(futures), desc="Processing", unit="photo"
            ):
                photo = futures[future]
                try:
                    result = future.result()
                    if result:
                        db.mark_processed(conn, **result)
                        processed += 1
                        if processed % config.COMMIT_BATCH_SIZE == 0:
                            conn.commit()
                    else:
                        failed += 1
                        failed_dates.append(photo.date)
                except Exception:
                    log.exception("Error processing %s (%s)", photo.uuid, photo.original_filename)
                    failed += 1
                    failed_dates.append(photo.date)

            # Set last_sync to just before the earliest failure so it gets retried.
            # If no failures, use current time.
            if failed_dates:
                sync_point = min(failed_dates) - timedelta(seconds=1)
            else:
                sync_point = datetime.now(timezone.utc)
            db.set_last_sync(conn, sync_point)

    return processed, failed


def _process_one(photo) -> dict | None:
    """Process a single photo. Returns metadata dict for DB insertion."""
    output_path = _build_output_path(photo)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if photo.hasadjustments:
        if not _export_uncooked(photo, output_path):
            return None
    else:
        if not _export_original(photo, output_path):
            return None

    return {
        "uuid": photo.uuid,
        "original_filename": photo.original_filename,
        "exported_path": str(output_path),
        "is_edited": photo.hasadjustments,
    }


def _export_original(photo, output_path: Path) -> bool:
    """Export an unedited photo directly to the output path. Returns False on failure."""
    filename = output_path.stem + output_path.suffix
    use_photos = photo.path is None
    with _export_lock:
        exported = photo.export(
            str(output_path.parent), filename=filename,
            sidecar_json=True, use_photos_export=use_photos,
        )
    if not exported:
        log.warning(
            "Export failed for %s (%s), skipping",
            photo.uuid, photo.original_filename,
        )
        return False
    log.debug("Exported original %s -> %s", photo.original_filename, output_path.name)
    return True


def _export_uncooked(photo, output_path: Path) -> bool:
    """Export a lossless crop from an edited screenshot. Returns False if match fails."""
    filename = output_path.stem + output_path.suffix

    with tempfile.TemporaryDirectory() as tmpdir:
        use_photos = photo.path is None
        if use_photos:
            # AppleScript path — must be serialized
            with _export_lock:
                orig_exports = photo.export(tmpdir, sidecar_json=True, use_photos_export=True)
                edit_exports = photo.export(tmpdir, edited=True, use_photos_export=True)
        else:
            with _export_lock:
                orig_exports = photo.export(tmpdir, sidecar_json=True)
            edit_exports = photo.export(tmpdir, edited=True)

        if not orig_exports or not edit_exports:
            raise RuntimeError(
                f"Failed to export {photo.uuid} "
                f"(original={len(orig_exports or [])}, edited={len(edit_exports or [])})"
            )

        # Use cv2 template matching to locate the JPEG crop within the original PNG
        region = find_crop_region(orig_exports[0], edit_exports[0])
        if region is None:
            log.warning(
                "Template match failed for %s (%s), skipping",
                photo.uuid, photo.original_filename,
            )
            return False

        # Extract that region from the original PNG (lossless, no JPEG artifacts)
        x, y, w, h = region
        cropped = lossless_crop(orig_exports[0], region)
        cropped.save(str(output_path), "PNG")

        # Copy sidecar from temp and fix the filename references
        tmp_sidecar = Path(orig_exports[0] + ".json")
        final_sidecar = output_path.parent / f"{filename}.json"
        final_sidecar.write_text(tmp_sidecar.read_text())
        _fix_sidecar_filename(final_sidecar, filename)

    log.debug(
        "Uncook'd %s: crop (%d,%d %dx%d) -> %s",
        photo.original_filename, x, y, w, h, output_path.name,
    )
    return True


def _build_output_path(photo) -> Path:
    dt = photo.date
    stem = Path(photo.original_filename).stem
    short = uuid_to_shortuuid(photo.uuid)
    filename = f"{dt.strftime('%Y%m%d_%H%M%S')}--{stem}--{short}.png"
    return config.OUTPUT_DIR / dt.strftime("%Y") / dt.strftime("%m") / filename


def _fix_sidecar_filename(sidecar_path: Path, new_filename: str):
    """Update SourceFile and File:FileName in a JSON sidecar."""
    data = json.loads(sidecar_path.read_text())
    data[0]["SourceFile"] = new_filename
    data[0]["File:FileName"] = new_filename
    sidecar_path.write_text(json.dumps(data))
