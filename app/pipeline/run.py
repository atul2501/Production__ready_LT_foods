import concurrent.futures
import hashlib
import threading
import time
from datetime import datetime, timezone

from app.config import settings
from app.core.exceptions import NoUsableTextError, OcrTimeoutError
from app.logging_conf import get_logger
from app.pipeline.business_rules import run_business_rules
from app.pipeline.grounding import ground_extraction
from app.pipeline.llm_structurer import PROMPT_VERSION, structure_invoice
from app.pipeline.normalize import normalize_document
from app.pipeline.ocr import get_ocr_engine
from app.pipeline.status import assign_status
from app.pipeline.text_extract import ExtractedLine, extract_digital_page_text, render_page_image
from app.pipeline.triage import triage_pdf

logger = get_logger(__name__)

# One OCR engine per worker thread rather than a single instance shared across the whole
# thread pool - a shared PaddleOCR instance was suspected to be behind a transient crash
# under concurrent OCR calls (see README "Key risks"). Thread-local trades a bit of extra
# memory (one loaded model per worker thread instead of one total) for genuine isolation,
# which is what actually lets WORKER_CONCURRENCY be raised safely.
_thread_local = threading.local()


def _get_ocr_engine():
    engine = getattr(_thread_local, "ocr_engine", None)
    if engine is None:
        engine = get_ocr_engine("paddle")
        _thread_local.ocr_engine = engine
    return engine


def _ocr_page_with_timeout(image_bytes: bytes, page_number: int) -> list[ExtractedLine]:
    """Runs one page's OCR call with a hard wall-clock ceiling. A pathological page
    (huge/corrupt scan) would otherwise tie up a worker thread with no bound other than the
    20-minute stale-job reaper. The OCR call itself isn't cancellable mid-flight, so a
    timeout here abandons waiting on it (the underlying thread runs to completion in the
    background) rather than actually interrupting it - still converts a hang into a clean,
    retryable failure instead of a stuck worker."""
    engine = _get_ocr_engine()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(engine.ocr_page_image, image_bytes, page_number)
        try:
            return future.result(timeout=settings.ocr_timeout_seconds)
        except concurrent.futures.TimeoutError as exc:
            raise OcrTimeoutError(
                f"OCR timed out after {settings.ocr_timeout_seconds}s on page {page_number}"
            ) from exc


def _ms_since(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def run_pipeline(pdf_bytes: bytes, job_id: str) -> dict:
    """Runs triage -> extract/OCR -> LLM structuring -> grounding -> business rules ->
    status assignment for a single PDF. Raises ExtractionError subclasses on hard failures
    (no usable text, LLM never produced valid JSON) - the caller is responsible for marking
    the job `failed` in that case. Anything that parses successfully always returns here
    with a status of "success" or "needs_review", never silently drops a flagged field.

    Every stage logs its own start/complete + duration, correlated by job_id, so a slow or
    wrong extraction at 200k/month volume can be traced back to the exact stage that caused it.
    """
    started = time.monotonic()
    log = logger.bind(job_id=job_id)
    log.info("pipeline_started", pdf_bytes=len(pdf_bytes))

    # --- Stage 1: triage ---
    stage_started = time.monotonic()
    pages = triage_pdf(pdf_bytes)
    if not pages:
        log.error("triage_failed", reason="no_pages")
        raise NoUsableTextError("PDF has no pages")
    digital_pages = sum(1 for p in pages if p.is_digital)
    log.info(
        "triage_complete",
        pages=len(pages),
        digital_pages=digital_pages,
        scanned_pages=len(pages) - digital_pages,
        duration_ms=_ms_since(stage_started),
    )

    # --- Stage 2: extract / OCR ---
    stage_started = time.monotonic()
    all_lines = []
    page_sources: dict[int, str] = {}
    for page in pages:
        if page.is_digital:
            lines = extract_digital_page_text(pdf_bytes, page.page_number)
            page_sources[page.page_number] = "digital"
        else:
            image_bytes = render_page_image(pdf_bytes, page.page_number)
            lines = _ocr_page_with_timeout(image_bytes, page.page_number)
            page_sources[page.page_number] = "ocr"
        all_lines.extend(lines)
        log.debug(
            "page_extracted",
            page=page.page_number,
            source=page_sources[page.page_number],
            line_count=len(lines),
        )

    if not all_lines:
        log.error("extraction_failed", reason="no_text_on_any_page")
        raise NoUsableTextError("no text extracted from any page (digital or OCR)")
    log.info("extraction_complete", total_lines=len(all_lines), duration_ms=_ms_since(stage_started))

    # --- Stage 3: normalize into a single source_text ---
    stage_started = time.monotonic()
    normalized = normalize_document(all_lines, page_sources)
    log.info(
        "normalization_complete",
        extraction_source=normalized.extraction_source,
        source_text_chars=len(normalized.source_text),
        avg_ocr_confidence=normalized.avg_ocr_confidence,
        duration_ms=_ms_since(stage_started),
    )

    # --- Stage 4: LLM structuring (Ollama) ---
    stage_started = time.monotonic()
    log.info(
        "llm_structuring_started",
        model=settings.ollama_model,
        prompt_chars=len(normalized.source_text),
    )
    extraction = structure_invoice(normalized.source_text, log=log)
    log.info(
        "llm_structuring_complete",
        model=settings.ollama_model,
        duration_ms=_ms_since(stage_started),
    )

    # --- Stage 5: grounding (anti-hallucination check) ---
    stage_started = time.monotonic()
    grounding_results = ground_extraction(extraction, normalized.source_text)
    grounded_count = sum(1 for r in grounding_results if r.grounded)
    log.info(
        "grounding_complete",
        fields_checked=len(grounding_results),
        grounded=grounded_count,
        ungrounded=len(grounding_results) - grounded_count,
        duration_ms=_ms_since(stage_started),
    )

    # --- Stage 6: business rules ---
    stage_started = time.monotonic()
    rule_violations = run_business_rules(extraction)
    log.info(
        "business_rules_complete",
        violations=len(rule_violations),
        critical=sum(1 for v in rule_violations if v.severity == "critical"),
        warning=sum(1 for v in rule_violations if v.severity == "warning"),
        duration_ms=_ms_since(stage_started),
    )

    # --- Stage 7: status assignment ---
    status_result = assign_status(grounding_results, rule_violations, normalized)
    log.info("status_assigned", status=status_result.status, flag_count=len(status_result.flags))

    source_text_hash = hashlib.sha256(normalized.source_text.encode("utf-8")).hexdigest()
    processing_time_ms = int((time.monotonic() - started) * 1000)

    log.info(
        "pipeline_complete",
        status=status_result.status,
        flag_count=len(status_result.flags),
        processing_time_ms=processing_time_ms,
    )

    return {
        "extraction": extraction,
        "status": status_result.status,
        "flags": status_result.flags,
        "model_name": settings.ollama_model,
        "prompt_version": PROMPT_VERSION,
        "extraction_source": normalized.extraction_source,
        "source_text_hash": source_text_hash,
        "processing_time_ms": processing_time_ms,
        "completed_at": datetime.now(timezone.utc),
    }
