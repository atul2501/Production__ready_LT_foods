# Optional lookup table for vendors where company_code/currency inference is known and
# validated, rather than left entirely to the LLM's best guess. Empty until populated from
# real production data - the pipeline works fine without entries here, they just reduce the
# needs_review rate for known vendors over time.
VENDOR_COMPANY_CODE: dict[str, str] = {}
VENDOR_DEFAULT_CURRENCY: dict[str, str] = {}


def lookup_company_code(vendor_name: str) -> str | None:
    return VENDOR_COMPANY_CODE.get(vendor_name.strip().lower())


def lookup_default_currency(vendor_name: str) -> str | None:
    return VENDOR_DEFAULT_CURRENCY.get(vendor_name.strip().lower())
