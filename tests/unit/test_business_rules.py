from app.pipeline.business_rules import check_arithmetic, check_line_items, check_mandatory_fields
from app.schemas.invoice_schema import InvoiceExtraction, InvoiceHeader, LineItem


def _make_extraction(**header_overrides) -> InvoiceExtraction:
    header = InvoiceHeader(
        invoice_number="INV-1",
        invoice_date="2026-01-01",
        company_code="1000",
        vendor_name="Vendor A",
        customer_name="LT Foods",
        currency="GBP",
        subtotal=100.0,
        tax_amount=20.0,
        total_amount=120.0,
    ).model_copy(update=header_overrides)
    return InvoiceExtraction(
        invoice_header=header,
        line_items=[LineItem(description="Freight", amount=100.0)],
        additional_fields=[],
    )


def test_mandatory_fields_pass_when_all_present():
    assert check_mandatory_fields(_make_extraction()) == []


def test_mandatory_field_missing_flagged_critical():
    violations = check_mandatory_fields(_make_extraction(invoice_number=""))
    assert any(v.field == "invoice_header.invoice_number" and v.severity == "critical" for v in violations)


def test_company_code_missing_flagged_warning_not_critical():
    violations = check_mandatory_fields(_make_extraction(company_code=""))
    assert any(v.field == "invoice_header.company_code" and v.severity == "warning" for v in violations)
    assert not any(v.field == "invoice_header.company_code" and v.severity == "critical" for v in violations)


def test_arithmetic_within_tolerance_passes():
    extraction = _make_extraction(subtotal=100.0, tax_amount=20.0, total_amount=120.001)
    assert check_arithmetic(extraction) == []


def test_arithmetic_mismatch_flagged():
    extraction = _make_extraction(subtotal=100.0, tax_amount=20.0, total_amount=200.0)
    violations = check_arithmetic(extraction)
    assert len(violations) == 1
    assert violations[0].reason == "arithmetic_mismatch"
    assert violations[0].severity == "warning"


def test_empty_line_items_flagged_critical():
    extraction = _make_extraction()
    extraction.line_items = []
    violations = check_line_items(extraction)
    assert any(v.reason == "empty_line_items" and v.severity == "critical" for v in violations)


def test_line_item_missing_description_flagged():
    extraction = _make_extraction()
    extraction.line_items = [LineItem(description="", amount=50.0)]
    violations = check_line_items(extraction)
    assert any(v.field == "line_items[0].description" for v in violations)
