# Mandatory ("Yes") header fields per the SAP invoice API spec that are plain strings.
# invoice_number/invoice_date/vendor_name/customer_name/currency must never be empty
# without triggering review. subtotal/tax_amount/total_amount are also mandatory but are
# enforced structurally by the pydantic schema (required, no default) rather than here.
CRITICAL_HEADER_STRING_FIELDS = [
    "invoice_number",
    "invoice_date",
    "vendor_name",
    "customer_name",
    "currency",
]

# company_code is mandatory per spec but the spec itself notes it "often must be inferred" -
# treated as warning-severity rather than critical, since a blank/inferred value is expected
# for some vendor formats and shouldn't hard-fail a job the way a missing invoice_number should.
WARNING_HEADER_FIELDS = ["company_code"]

# Fields the LLM is allowed to infer from context (currency symbols, vendor country, etc.)
# rather than copy verbatim - exempted from strict grounding but always flagged for review.
INFERRED_FIELDS = {"company_code", "currency"}

# Always resolved inside SAP from a vendor-to-GL mapping table, never present on the PDF itself.
SAP_MANAGED_LINE_FIELDS = ["gl_account", "cost_center", "profit_center"]
