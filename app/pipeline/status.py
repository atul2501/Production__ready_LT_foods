from dataclasses import dataclass

from app.config import settings
from app.core.constants import CRITICAL_HEADER_STRING_FIELDS
from app.pipeline.business_rules import RuleViolation
from app.pipeline.grounding import FieldGroundingResult
from app.pipeline.normalize import NormalizedDocument

_CRITICAL_PATHS = {f"invoice_header.{name}" for name in CRITICAL_HEADER_STRING_FIELDS}


@dataclass
class StatusResult:
    status: str  # "success" | "needs_review" (failed is assigned earlier, before this stage)
    flags: list[dict]


def assign_status(
    grounding_results: list[FieldGroundingResult],
    rule_violations: list[RuleViolation],
    normalized_doc: NormalizedDocument,
) -> StatusResult:
    flags: list[dict] = []
    has_critical = False
    has_warning = False

    for violation in rule_violations:
        flags.append(
            {
                "field": violation.field,
                "reason": violation.reason,
                "severity": violation.severity,
                "detail": violation.detail or None,
            }
        )
        if violation.severity == "critical":
            has_critical = True
        else:
            has_warning = True

    for result in grounding_results:
        if result.match_type == "unexpected_sap_field":
            flags.append({"field": result.field_path, "reason": "sap_managed_field_populated", "severity": "warning"})
            has_warning = True
        elif result.match_type == "inferred":
            flags.append({"field": result.field_path, "reason": "inferred", "severity": "warning"})
            has_warning = True
        elif not result.grounded:
            severity = "critical" if result.field_path in _CRITICAL_PATHS else "warning"
            flags.append(
                {
                    "field": result.field_path,
                    "reason": "ungrounded",
                    "severity": severity,
                    "detail": f"score={result.score:.1f}",
                }
            )
            if severity == "critical":
                has_critical = True
            else:
                has_warning = True

    if normalized_doc.extraction_source in ("ocr", "mixed") and normalized_doc.avg_ocr_confidence is not None:
        if normalized_doc.avg_ocr_confidence < settings.ocr_min_confidence:
            flags.append(
                {
                    "field": "_document",
                    "reason": "low_ocr_confidence",
                    "severity": "warning",
                    "detail": f"avg_confidence={normalized_doc.avg_ocr_confidence:.2f}",
                }
            )
            has_warning = True

    # A critical/ungrounded field means "needs a human to check", not "reject outright" -
    # true hard failures (unparseable LLM output, no usable text at all) are raised as
    # exceptions earlier in the pipeline and never reach this function.
    status = "needs_review" if (has_critical or has_warning) else "success"
    return StatusResult(status=status, flags=flags)
