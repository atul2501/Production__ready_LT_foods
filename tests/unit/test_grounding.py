from app.pipeline.grounding import ground_extraction
from app.schemas.invoice_schema import InvoiceExtraction, InvoiceHeader, LineItem

SOURCE_TEXT = """
Invoice Number: 6376-2026-16
Invoice Date: 20/04/2026
Vendor: WS Digital Freight Ltd
Customer: LT FOODS U.K. Limited
Currency: GBP
Subtotal: 1362.70
Tax: 272.54
Total: 1635.24
General Haulage (Harlow - Normanton)  500.00
"""


def _extraction(**header_overrides) -> InvoiceExtraction:
    header = InvoiceHeader(
        invoice_number="6376-2026-16",
        invoice_date="2026-04-20",
        company_code="",
        vendor_name="WS Digital Freight Ltd",
        customer_name="LT FOODS U.K. Limited",
        currency="GBP",
        subtotal=1362.70,
        tax_amount=272.54,
        total_amount=1635.24,
    ).model_copy(update=header_overrides)
    return InvoiceExtraction(
        invoice_header=header,
        line_items=[LineItem(description="General Haulage (Harlow - Normanton)", amount=500.0)],
        additional_fields=[],
    )


def test_exact_text_field_grounds():
    results = ground_extraction(_extraction(), SOURCE_TEXT)
    vendor_result = next(r for r in results if r.field_path == "invoice_header.vendor_name")
    assert vendor_result.grounded is True
    assert vendor_result.match_type == "exact"


def test_numeric_field_grounds_despite_different_decimal_formatting():
    results = ground_extraction(_extraction(), SOURCE_TEXT)
    subtotal_result = next(r for r in results if r.field_path == "invoice_header.subtotal")
    assert subtotal_result.grounded is True
    assert subtotal_result.match_type == "numeric"


def test_date_field_grounds_across_different_formats():
    results = ground_extraction(_extraction(), SOURCE_TEXT)
    date_result = next(r for r in results if r.field_path == "invoice_header.invoice_date")
    assert date_result.grounded is True
    assert date_result.match_type == "date"


def test_fabricated_value_not_grounded():
    results = ground_extraction(_extraction(vendor_name="Zzyxquilt Nonexistent Traders Ltd"), SOURCE_TEXT)
    vendor_result = next(r for r in results if r.field_path == "invoice_header.vendor_name")
    assert vendor_result.grounded is False


def test_inferred_field_exempted_from_strict_grounding():
    results = ground_extraction(_extraction(company_code="1000"), SOURCE_TEXT)
    company_result = next(r for r in results if r.field_path == "invoice_header.company_code")
    assert company_result.match_type == "inferred"
    assert company_result.grounded is True


def test_sap_managed_field_populated_is_flagged_not_trusted():
    extraction = _extraction()
    extraction.line_items[0].gl_account = "400100"
    results = ground_extraction(extraction, SOURCE_TEXT)
    flagged = [r for r in results if r.match_type == "unexpected_sap_field"]
    assert len(flagged) == 1
    assert flagged[0].grounded is False
