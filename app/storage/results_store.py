"""File-based store for extracted invoice JSON - replaces the Postgres tables.

    STORAGE_DIR/pending/    extracted, not yet handed to the team
    STORAGE_DIR/delivered/  already returned by GET /api/v1/invoices/new (kept as a backup)
    STORAGE_DIR/failed/     attempt counters for PDFs that keep failing

A result's key is "{received_ts}_{hash(message_id)}_{n}", so it is the same on every poll
for the same email attachment: the poller skips any key that already exists in pending/ or
delivered/, and file names sort oldest-first.
"""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings
from app.logging_conf import get_logger

logger = get_logger(__name__)

_root = Path(settings.storage_dir).resolve()
PENDING_DIR = _root / "pending"
DELIVERED_DIR = _root / "delivered"
FAILED_DIR = _root / "failed"

for _dir in (PENDING_DIR, DELIVERED_DIR, FAILED_DIR):
    _dir.mkdir(parents=True, exist_ok=True)


def result_key(message_id: str, received_at: datetime | None, index: int) -> str:
    received_at = received_at or datetime(1970, 1, 1, tzinfo=timezone.utc)
    if received_at.tzinfo is not None:
        received_at = received_at.astimezone(timezone.utc)
    stamp = received_at.strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha1(message_id.encode("utf-8")).hexdigest()[:12]
    return f"{stamp}_{digest}_{index}"


def exists(key: str) -> bool:
    name = f"{key}.json"
    return (PENDING_DIR / name).exists() or (DELIVERED_DIR / name).exists()


def write_pending(key: str, data: dict) -> None:
    """Writes to a temp file first and renames it, so the API never reads a half-written
    file (claim_pending only picks up *.json)."""
    final_path = PENDING_DIR / f"{key}.json"
    tmp_path = PENDING_DIR / f"{key}.json.tmp"
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, final_path)
    logger.info("result_written", key=key)


def claim_pending() -> list[dict]:
    """Moves every pending result to delivered/ and returns their contents, oldest first.
    os.replace is atomic, so if two requests race, each file goes to exactly one of them."""
    results = []
    for path in sorted(PENDING_DIR.glob("*.json")):
        target = DELIVERED_DIR / path.name
        try:
            os.replace(path, target)
        except FileNotFoundError:
            continue  # another request claimed it first
        try:
            results.append(json.loads(target.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            logger.exception("result_unreadable", file=str(target))
    logger.info("results_claimed", count=len(results))
    return results


def pending_count() -> int:
    return sum(1 for _ in PENDING_DIR.glob("*.json"))


def bump_fail(key: str) -> int:
    """Records one more failed attempt for this key and returns the total so far."""
    path = FAILED_DIR / f"{key}.count"
    try:
        count = int(path.read_text(encoding="utf-8").strip() or 0)
    except (FileNotFoundError, ValueError):
        count = 0
    count += 1
    path.write_text(str(count), encoding="utf-8")
    return count


def clear_fail(key: str) -> None:
    try:
        (FAILED_DIR / f"{key}.count").unlink()
    except FileNotFoundError:
        pass
