from imap_tools import AND, MailMessageFlags
from imap_tools.message import MailMessage

from app.config import settings
from app.email_ingest.client import open_mailbox
from app.logging_conf import get_logger
from app.metrics import JOBS_COMPLETED, PIPELINE_DURATION_SECONDS
from app.pipeline.run import run_pipeline
from app.pipeline.to_response import build_failure, build_result
from app.schemas.envelope import EmailInfo
from app.storage import results_store

logger = get_logger(__name__)

# (uidvalidity, uid) of unread messages already found to have no PDF attachment. They stay
# unread in the mailbox, so without this every poll would download them again.
_no_pdf_uids: set[tuple[int, int]] = set()


def _message_id(msg: MailMessage) -> str:
    raw = msg.headers.get("message-id")
    if raw:
        return raw[0]
    # Malformed/legacy senders occasionally omit Message-ID entirely - fall back to a key
    # that's still stable across polls for the same message (uid is stable within a folder
    # as long as the mailbox's UIDVALIDITY doesn't change), rather than skipping dedup.
    logger.warning("email_missing_message_id", uid=msg.uid, subject=msg.subject)
    return f"no-message-id:{settings.imap_folder}:{msg.uid}"


def _is_pdf_attachment(att) -> bool:
    # Checks the bytes, not the declared content type (senders often label PDFs
    # application/octet-stream), and tolerates a few junk bytes before the %PDF- header
    # the way PDF readers do - the old startswith() check silently skipped those.
    return b"%PDF-" in (att.payload or b"")[:1024]


def _strip_to_pdf_header(payload: bytes) -> bytes:
    return payload[payload.find(b"%PDF-"):]


def _extract_attachment(key: str, attachment, email: EmailInfo, log) -> bool:
    """Runs the pipeline on one PDF and writes its result to pending/. Returns True once
    this attachment has a result file (success, needs_review, or a final failure), False if
    it failed and should be retried on the next poll."""
    try:
        result = run_pipeline(_strip_to_pdf_header(attachment.payload), key)
    except Exception as exc:  # noqa: BLE001 - one bad PDF must not stop the rest
        attempts = results_store.bump_fail(key)
        log.exception("pdf_extraction_failed", key=key, filename=attachment.filename, attempt=attempts)
        if attempts < settings.email_max_attempts:
            return False
        # Out of attempts - hand the team a "failed" result instead of retrying forever.
        failure = build_failure(key, f"{type(exc).__name__}: {exc}", attachment.filename, email)
        results_store.write_pending(key, failure.model_dump(mode="json"))
        results_store.clear_fail(key)
        JOBS_COMPLETED.labels(status="failed").inc()
        return True

    item = build_result(key, result, attachment.filename, email)
    results_store.write_pending(key, item.model_dump(mode="json"))
    results_store.clear_fail(key)
    JOBS_COMPLETED.labels(status=result["status"]).inc()
    PIPELINE_DURATION_SECONDS.observe(result["processing_time_ms"] / 1000)
    log.info("pdf_extracted", key=key, filename=attachment.filename, status=result["status"])
    return True


def _handle_message(msg: MailMessage) -> bool | None:
    """Extracts every PDF attachment of one email. Returns None if it has no PDF, True if
    every PDF now has a result (so the email can be marked read), False otherwise."""
    log = logger.bind(uid=msg.uid, subject=msg.subject, from_=msg.from_)

    pdf_attachments = [att for att in msg.attachments if _is_pdf_attachment(att)]
    if not pdf_attachments:
        log.info(
            "email_skipped_no_pdf_attachment",
            attachments=[(att.filename, att.content_type) for att in msg.attachments],
        )
        return None

    message_id = _message_id(msg)
    received_at = msg.date if msg.date.year > 1900 else None  # imap_tools uses 1900-01-01 when missing
    email = EmailInfo(message_id=message_id, sender=msg.from_, subject=msg.subject, received_at=received_at)

    all_done = True
    for index, attachment in enumerate(pdf_attachments, start=1):
        key = results_store.result_key(message_id, received_at, index)
        if results_store.exists(key):
            # Extracted on an earlier poll that didn't get as far as marking the email read.
            log.info("pdf_already_extracted", key=key)
            continue
        if not _extract_attachment(key, attachment, email, log):
            all_done = False
    return all_done


def _mark_read(uid: str) -> None:
    with open_mailbox() as mailbox:
        mailbox.flag(uid, MailMessageFlags.SEEN, True)
        if settings.imap_processed_folder:
            mailbox.move(uid, settings.imap_processed_folder)


def run_ingest_cycle() -> None:
    """One poll: every unread email in imap_folder that has PDF attachment(s) is extracted
    to JSON files in pending/ (see app/storage/results_store.py). An email is marked read
    only once all of its PDFs have a result; otherwise it stays unread and is retried next
    poll. Emails without a PDF are left unread and untouched.

    The IMAP connection is not held open while PDFs are extracted (that can take minutes
    and servers drop idle connections) - each message is fetched, closed, processed, and
    then a fresh connection marks it read."""
    extracted = skipped_no_pdf = incomplete = failed = 0

    with open_mailbox() as mailbox:
        uid_validity = mailbox.folder.status(settings.imap_folder, ("UIDVALIDITY",))["UIDVALIDITY"]
        unread_uids = sorted(int(uid) for uid in mailbox.uids(AND(seen=False)))
    todo = [uid for uid in unread_uids if (uid_validity, uid) not in _no_pdf_uids]

    for uid in todo:
        try:
            with open_mailbox() as mailbox:
                msg = next(iter(mailbox.fetch(AND(uid=str(uid)), mark_seen=False)), None)
            if msg is None:
                continue  # deleted/moved since the uid list was taken

            outcome = _handle_message(msg)
            if outcome is None:
                _no_pdf_uids.add((uid_validity, uid))
                skipped_no_pdf += 1
            elif outcome:
                _mark_read(msg.uid)
                extracted += 1
            else:
                incomplete += 1
        except Exception:  # noqa: BLE001 - one bad message must not abort the whole cycle
            failed += 1
            logger.exception("email_ingest_message_failed", uid=uid)

    # Logged every cycle (not just when something is found) so a quiet log unambiguously
    # means "polled, nothing new" rather than "ingester is stuck or not running".
    logger.info(
        "email_ingest_cycle_complete",
        unread=len(unread_uids),
        extracted=extracted,
        skipped_no_pdf=skipped_no_pdf,
        retry_next_poll=incomplete,
        failed=failed,
        pending_results=results_store.pending_count(),
    )
