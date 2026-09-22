from imap_tools import AND, MailMessageFlags
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


def _handle_message(mailbox, msg: MailMessage) -> None:
    log = logger.bind(uid=msg.uid, subject=msg.subject, from_=msg.from_)

    pdf_attachments = [att for att in msg.attachments if att.payload.startswith(b"%PDF-")]
    if not pdf_attachments:
        log.debug("email_skipped_no_pdf_attachment")
        return

    message_id = _message_id(msg)
    session = SessionLocal()
    try:
        if _already_processed(session, message_id):
            log.info("email_already_processed", message_id=message_id)
        else:
            for attachment in pdf_attachments:
                job = _persist_upload(session, attachment.payload, attachment.filename, uploaded_by=msg.from_)
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


def run_ingest_cycle() -> None:
    """One poll: fetch every unread message in imap_folder, and for each one containing at
    least one PDF attachment, queue a Job per attachment via the same _persist_upload the
    HTTP upload endpoint uses. Unread messages with no PDF attachment are left untouched."""
    with open_mailbox() as mailbox:
        for msg in mailbox.fetch(AND(seen=False), mark_seen=False):
            try:
                _handle_message(mailbox, msg)
            except Exception:  # noqa: BLE001 - one bad message must not abort the whole cycle
                logger.exception("email_ingest_message_failed", uid=msg.uid, subject=msg.subject)
