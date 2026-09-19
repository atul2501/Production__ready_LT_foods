import re
from dataclasses import dataclass
from datetime import datetime

from rapidfuzz import fuzz

from app.config import settings
from app.core.constants import INFERRED_FIELDS, SAP_MANAGED_LINE_FIELDS
from app.schemas.invoice_schema import InvoiceExtraction

NUMERIC_FIELDS = {"subtotal", "tax_amount", "tax_percent", "total_amount", "quantity", "unit_price", "amount"}
DATE_FIELDS = {"invoice_date", "due_date"}

_DATE_FORMATS = [
    "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%m/%d/%Y",
    "%d %b %Y", "%d %B %Y", "%b %d, %Y",
]
_NUMBER_TOKEN_RE = re.compile(r"[-+]?\d[\d,\.]*\d|\d")
_NUMBER_CLEAN_RE = re.compile(r"[^0-9.\-]")


@dataclass
class FieldGroundingResult:
    field_path: str
    value: object
    grounded: bool
    match_type: str  # "exact" | "fuzzy" | "numeric" | "date" | "inferred" | "skipped" | "ungrounded" | "unexpected_sap_field"
    score: float


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _normalize_number(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _extract_number_candidates(source_text: str) -> set[str]:
    candidates: set[str] = set()
    for match in _NUMBER_TOKEN_RE.findall(source_text):
        cleaned = _NUMBER_CLEAN_RE.sub("", match.replace(",", ""))
        if not cleaned or cleaned == "-":
            continue
        try:
            candidates.add(_normalize_number(float(cleaned)))
        except ValueError:
            continue
    return candidates


def _check_numeric_field(value: float, number_candidates: set[str]) -> bool:
    return _normalize_number(value) in number_candidates


def _parse_date(value: str) -> datetime | None:
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


def _check_date_field(value: str, source_text_normalized: str) -> tuple[bool, str, float]:
    parsed = _parse_date(value)
    if parsed is None:
        return _check_text_field(value, source_text_normalized)
    candidates = {
        parsed.strftime("%Y-%m-%d"), parsed.strftime("%d/%m/%Y"), parsed.strftime("%d-%m-%Y"),
        parsed.strftime("%d.%m.%Y"), parsed.strftime("%m/%d/%Y"),
        parsed.strftime("%d %b %Y"), parsed.strftime("%d %B %Y"),
    }
    for candidate in candidates:
        if candidate.lower() in source_text_normalized:
            return True, "date", 100.0
    return False, "ungrounded", 0.0


def _check_text_field(value: str, source_text_normalized: str) -> tuple[bool, str, float]:
    normalized_value = _normalize_text(value)
    if not normalized_value:
        return True, "skipped", 1.0
    if normalized_value in source_text_normalized:
        return True, "exact", 100.0
    score = fuzz.partial_ratio(normalized_value, source_text_normalized)
    if score >= settings.grounding_fuzzy_threshold:
        return True, "fuzzy", score
    return False, "ungrounded", score


def _ground_field(
    field_name: str, value: object, source_text_normalized: str, number_candidates: set[str]
) -> tuple[bool, str, float] | None:
    if value in (None, ""):
        return None
    if field_name in INFERRED_FIELDS:
        return True, "inferred", 0.0
    if field_name in NUMERIC_FIELDS:
        grounded = _check_numeric_field(float(value), number_candidates)  # type: ignore[arg-type]
        return grounded, "numeric", 100.0 if grounded else 0.0
    if field_name in DATE_FIELDS:
        return _check_date_field(str(value), source_text_normalized)
    return _check_text_field(str(value), source_text_normalized)


def ground_extraction(extraction: InvoiceExtraction, source_text: str) -> list[FieldGroundingResult]:
    """Core anti-hallucination check: every field the LLM populated is re-verified against
    the actual OCR'd/digital source text. Nothing the LLM asserts is trusted just because
    it parsed as valid JSON."""
    results: list[FieldGroundingResult] = []
    source_text_normalized = _normalize_text(source_text)
    number_candidates = _extract_number_candidates(source_text)

    for field_name, value in extraction.invoice_header.model_dump().items():
        outcome = _ground_field(field_name, value, source_text_normalized, number_candidates)
        if outcome is None:
            continue
        grounded, match_type, score = outcome
        results.append(FieldGroundingResult(f"invoice_header.{field_name}", value, grounded, match_type, score))

    for idx, item in enumerate(extraction.line_items):
        for field_name, value in item.model_dump().items():
            path = f"line_items[{idx}].{field_name}"
            if field_name in SAP_MANAGED_LINE_FIELDS:
                if value is not None:
                    # The LLM was explicitly told these must always be null - a non-null
                    # value here is itself a strong hallucination/instruction-following signal.
                    results.append(FieldGroundingResult(path, value, False, "unexpected_sap_field", 0.0))
                continue
            outcome = _ground_field(field_name, value, source_text_normalized, number_candidates)
            if outcome is None:
                continue
            grounded, match_type, score = outcome
            results.append(FieldGroundingResult(path, value, grounded, match_type, score))

    return results
