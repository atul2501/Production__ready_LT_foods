import pytest
from pydantic import ValidationError

from app.schemas.invoice_schema import AdditionalField, InvoiceExtraction, InvoiceHeader, LineItem


def _valid_header() -> InvoiceHeader:
    return InvoiceHeader(
        invoice_number="INV-1",
        invoice_date="2026-01-01",
        company_code="1000",
        vendor_name="Vendor A",
        customer_name="LT Foods",
        currency="GBP",
        subtotal=100.0,
        tax_amount=20.0,
        total_amount=120.0,
    )


def test_po_number_defaults_to_none_but_key_is_present():
    header = _valid_header()
    dumped = header.model_dump()
    assert "po_number" in dumped
    assert dumped["po_number"] is None


def test_missing_mandatory_field_raises():
    with pytest.raises(ValidationError):
        InvoiceHeader(
            invoice_date="2026-01-01",
            company_code="1000",
            vendor_name="Vendor A",
            customer_name="LT Foods",
            currency="GBP",
            subtotal=100.0,
            tax_amount=20.0,
            total_amount=120.0,
        )


def test_empty_string_is_a_valid_value_for_mandatory_field():
    header = _valid_header().model_copy(update={"invoice_number": ""})
    assert header.invoice_number == ""


def test_full_extraction_round_trips_and_sap_managed_fields_default_null():
    extraction = InvoiceExtraction(
        invoice_header=_valid_header(),
        line_items=[LineItem(description="Item", amount=10.0)],
        additional_fields=[AdditionalField(field_name="Your Reference", field_value="ABC123")],
    )
    dumped = extraction.model_dump()
    assert dumped["line_items"][0]["gl_account"] is None
    assert dumped["line_items"][0]["cost_center"] is None
    assert dumped["line_items"][0]["profit_center"] is None
    assert dumped["additional_fields"][0]["field_name"] == "Your Reference"


def test_json_schema_marks_mandatory_fields_required():
    schema = InvoiceHeader.model_json_schema()
    required = set(schema["required"])
    for field in ["invoice_number", "invoice_date", "company_code", "vendor_name", "customer_name", "currency", "subtotal", "tax_amount", "total_amount"]:
        assert field in required
    assert "due_date" not in required
