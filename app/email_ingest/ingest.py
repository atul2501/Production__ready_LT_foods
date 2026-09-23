from imap_tools import AND, U, MailMessageFlags
from imap_tools.message import MailMessage
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.routers.invoices import _persist_upload
from app.config import settings
from app.db import models
from app.db.base import SessionLocal
from app.email_ingest.client import open_mailbox
from app.logging_conf import get_logger

logger = get_logger(__name__)


def _message_id(msg: MailMessage) -> str:
    raw = msg.headers.get("message-id")
    if raw:
        return raw[0]
    # Malformed/legacy senders occasionally omit Message-ID entirely - fall back to a key
    # that's still stable across polls for the same message (uid is stable within a folder
    # as long as the mailbox's UIDVALIDITY doesn't change), rather than skipping dedup.
    logger.warning("email_missing_message_id", uid=msg.uid, subject=msg.subject)
    return f"no-message-id:{settings.imap_folder}:{msg.uid}"


def _already_processed(db: Session, message_id: str) -> bool:
    return db.scalar(select(models.ProcessedEmail.id).where(models.ProcessedEmail.message_id == message_id)) is not None


def _is_pdf_attachment(att) -> bool:
    # Checks the bytes, not the declared content type (senders often label PDFs
    # application/octet-stream), and tolerates a few junk bytes before the %PDF- header
    # the way PDF readers do - the old startswith() check silently skipped those.
    return b"%PDF-" in (att.payload or b"")[:1024]


def _strip_to_pdf_header(payload: bytes) -> bytes:
    return payload[payload.find(b"%PDF-"):]


def _handle_message(mailbox, msg: MailMessage) -> bool:
    """Returns True if the message had PDF attachment(s) and was ingested (or had already
    been ingested on a prior cycle), False if it was skipped for having no PDF."""
    log = logger.bind(uid=msg.uid, subject=msg.subject, from_=msg.from_)

    pdf_attachments = [att for att in msg.attachments if _is_pdf_attachment(att)]
    if not pdf_attachments:
        log.info(
            "email_skipped_no_pdf_attachment",
            attachments=[(att.filename, att.content_type) for att in msg.attachments],
        )
        return False

    message_id = _message_id(msg)
    session = SessionLocal()
    try:
        if _already_processed(session, message_id):
            log.info("email_already_processed", message_id=message_id)
        else:
            for attachment in pdf_attachments:
                job = _persist_upload(
                    session, _strip_to_pdf_header(attachment.payload), attachment.filename, uploaded_by=msg.from_
                )
                log.info("job_created_from_email", job_id=job.id, filename=attachment.filename)
            session.add(models.ProcessedEmail(message_id=message_id))
            session.commit()
    finally:
        session.close()

    # Only reached once every PDF attachment is safely queued (or the message was already
    # done on a prior cycle) - a message that fails partway through stays unread so the next
    # poll retries it instead of silently losing an invoice.
    mailbox.flag(msg.uid, MailMessageFlags.SEEN, True)
    if settings.imap_processed_folder:
        mailbox.move(msg.uid, settings.imap_processed_folder)
    return True


def _load_watermark(mailbox) -> int:
    """Returns the highest UID already dealt with in imap_folder. The first time a folder is
    seen (or after its UIDVALIDITY changes, which invalidates every stored UID) the watermark
    starts at the folder's current highest UID - so only mail arriving from then on is
    ingested, and the pre-existing unread backlog is ignored without being downloaded."""
    status = mailbox.folder.status(settings.imap_folder, ("UIDNEXT", "UIDVALIDITY"))
    uid_validity, current_max_uid = status["UIDVALIDITY"], status["UIDNEXT"] - 1

    session = SessionLocal()
    try:
        state = session.get(models.EmailIngestState, settings.imap_folder)
        if state is not None and state.uid_validity == uid_validity:
            return state.last_uid

        if state is None:
            logger.info("email_watermark_initialized", folder=settings.imap_folder, last_uid=current_max_uid)
            state = models.EmailIngestState(folder=settings.imap_folder)
            session.add(state)
        else:
            logger.warning(
                "email_uidvalidity_changed",
                folder=settings.imap_folder,
                old_uid_validity=state.uid_validity,
                new_uid_validity=uid_validity,
                last_uid=current_max_uid,
            )
        state.uid_validity = uid_validity
        state.last_uid = current_max_uid
        session.commit()
        return current_max_uid
    finally:
        session.close()


def _save_watermark(last_uid: int) -> None:
    session = SessionLocal()
    try:
        state = session.get(models.EmailIngestState, settings.imap_folder)
        state.last_uid = last_uid
        session.commit()
    finally:
        session.close()


def run_ingest_cycle() -> None:
    """One poll: fetch the unread messages in imap_folder that arrived after the watermark
    (see _load_watermark), and for each one containing at least one PDF attachment, queue a
    Job per attachment via the same _persist_upload the HTTP upload endpoint uses. Unread
    messages with no PDF attachment are left untouched in the mailbox."""
    ingested = skipped_no_pdf = failed = 0
    with open_mailbox() as mailbox:
        last_uid = _load_watermark(mailbox)
        # "N:*" always matches the folder's highest UID even when it's below N (IMAP quirk),
        # hence the explicit > filter.
        new_uids = sorted(
            int(uid)
            for uid in mailbox.uids(AND(seen=False, uid=U(str(last_uid + 1), "*")))
            if int(uid) > last_uid
        )

        new_watermark = last_uid
        blocked = False
        if new_uids:
            messages = mailbox.fetch(AND(uid=[str(uid) for uid in new_uids]), mark_seen=False)
            for msg in sorted(messages, key=lambda m: int(m.uid)):
                try:
                    if _handle_message(mailbox, msg):
                        ingested += 1
                    else:
                        skipped_no_pdf += 1
                except Exception:  # noqa: BLE001 - one bad message must not abort the whole cycle
                    failed += 1
                    # The watermark stops before the first failure so that message is retried
                    # next poll; later messages that did succeed were marked Seen, so they
                    # aren't picked up again (and processed_emails dedups them regardless).
                    blocked = True
                    logger.exception("email_ingest_message_failed", uid=msg.uid, subject=msg.subject)
                    continue
                if not blocked:
                    new_watermark = int(msg.uid)

        if new_watermark > last_uid:
            _save_watermark(new_watermark)

    # Logged every cycle (not just when something is found) so a quiet log unambiguously
    # means "polled, nothing new" rather than "ingester is stuck or not running".
    logger.info(
        "email_ingest_cycle_complete",
        new_unread=len(new_uids),
        ingested=ingested,
        skipped_no_pdf=skipped_no_pdf,
        failed=failed,
        last_uid=new_watermark,
    )
