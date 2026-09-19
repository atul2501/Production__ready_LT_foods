from dataclasses import dataclass

from app.config import settings
from app.core.constants import CRITICAL_HEADER_STRING_FIELDS, WARNING_HEADER_FIELDS
from app.schemas.invoice_schema import InvoiceExtraction


@dataclass
class RuleViolation:
    field: str
    reason: str
    severity: str  # "warning" | "critical"
    detail: str = ""


def check_mandatory_fields(extraction: InvoiceExtraction) -> list[RuleViolation]:
    violations: list[RuleViolation] = []
    header = extraction.invoice_header.model_dump()

    for field_name in CRITICAL_HEADER_STRING_FIELDS:
        if not header.get(field_name):
            violations.append(RuleViolation(f"invoice_header.{field_name}", "missing_mandatory", "critical"))

    for field_name in WARNING_HEADER_FIELDS:
        if not header.get(field_name):
            violations.append(RuleViolation(f"invoice_header.{field_name}", "missing_mandatory", "warning"))

    return violations


def check_arithmetic(extraction: InvoiceExtraction) -> list[RuleViolation]:
    header = extraction.invoice_header
    expected_total = header.subtotal + header.tax_amount
    tolerance = max(
        settings.arithmetic_tolerance_abs,
        abs(header.total_amount) * settings.arithmetic_tolerance_rel,
    )
    if abs(expected_total - header.total_amount) > tolerance:
        return [
            RuleViolation(
                "invoice_header.total_amount",
                "arithmetic_mismatch",
                "warning",
                detail=(
                    f"subtotal({header.subtotal}) + tax_amount({header.tax_amount}) "
                    f"!= total_amount({header.total_amount})"
                ),
            )
        ]
    return []


def check_line_items(extraction: InvoiceExtraction) -> list[RuleViolation]:
    violations: list[RuleViolation] = []
    if not extraction.line_items:
        return [RuleViolation("line_items", "empty_line_items", "critical")]

    for idx, item in enumerate(extraction.line_items):
        if not item.description:
            violations.append(RuleViolation(f"line_items[{idx}].description", "missing_mandatory", "critical"))
        if item.amount is None:
            violations.append(RuleViolation(f"line_items[{idx}].amount", "missing_mandatory", "critical"))
    return violations


def run_business_rules(extraction: InvoiceExtraction) -> list[RuleViolation]:
    return [
        *check_mandatory_fields(extraction),
        *check_arithmetic(extraction),
        *check_line_items(extraction),
    ]
