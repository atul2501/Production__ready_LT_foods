import json

from app.core.exceptions import LLMFormatError
from app.logging_conf import get_logger
from app.schemas.invoice_schema import InvoiceExtraction
from app.schemas.ollama_json_schema import get_invoice_json_schema
from app.services.ollama_client import OllamaClient

logger = get_logger(__name__)

PROMPT_VERSION = "v2"

# A literal worked example (the canonical example from the SAP field spec) rather than just
# the abstract JSON schema. Confirmed necessary: Ollama's `format` JSON-schema constraint is
# NOT enforced by Ollama Cloud's serving backend (verified against deepseek-v4.1-flash,
# gpt-oss:120b, glm-5.3-flash - all three silently invented their own flat/renamed structure
# instead of the nested schema, e.g. "tax" instead of "tax_amount", vendor as a nested object).
# For self-hosted Ollama the `format` param IS still enforced and this example is redundant
# but harmless - so it stays in the prompt for both paths rather than branching on backend.
EXAMPLE_JSON = json.dumps(
    {
        "invoice_header": {
            "invoice_number": "6376-2026-16",
            "invoice_date": "2026-04-20",
            "due_date": "2026-05-20",
            "payment_terms": "Net 30",
            "company_code": "",
            "vendor_name": "WS Digital Freight Ltd",
            "vendor_address": "Vanguard House, Keckwick Lane, Warrington, WA4 4AB",
            "vendor_tax_id": "GB390726676",
            "vendor_bank_name": "HSBC",
            "vendor_account_no": "03009968",
            "vendor_sort_code": "406135",
            "vendor_iban": None,
            "customer_name": "LT FOODS U.K. Limited",
            "customer_address": "Unit 5, Midas, River Way, Harlow, CM20 2GJ",
            "po_number": "6600174563",
            "reference_number": "36150623",
            "currency": "GBP",
            "subtotal": 1362.70,
            "tax_amount": 272.54,
            "tax_percent": 20,
            "total_amount": 1635.24,
        },
        "line_items": [
            {
                "line_no": "416512",
                "description": "General Haulage (Harlow - Normanton)",
                "quantity": 1,
                "unit_price": 500.00,
                "amount": 500.00,
                "tax_percent": 20,
                "gl_account": None,
                "cost_center": None,
                "profit_center": None,
                "reference_code": "2604011349263537",
            }
        ],
        "additional_fields": [{"field_name": "Your Reference", "field_value": "LTST042606"}],
    },
    indent=2,
)

SYSTEM_INSTRUCTIONS = f"""You are an invoice data extraction assistant. You will be given the raw OCR/extracted text of a single invoice PDF. Extract the fields into the exact JSON structure requested.

Your output MUST have EXACTLY these three top-level keys: `invoice_header` (an object), `line_items` (an array), `additional_fields` (an array). Do NOT flatten invoice_header's fields to the top level. Do NOT rename any field (use `tax_amount` not `tax`, `total_amount` not `total`, `payment_terms` not `terms`, `vendor_name`/`vendor_address` as flat strings not a nested "vendor" object, `customer_name`/`customer_address` not a nested "buyer"/"ship_to" object). Do NOT add fields that are not in the structure below - anything else goes into `additional_fields`.

Here is a worked example showing the EXACT structure, field names, and nesting to use (the values are just an example, not real data to copy):
{EXAMPLE_JSON}

STRICT RULES - follow exactly, this data feeds financial accounting systems:
1. Only use values that literally appear in the provided text. NEVER invent, guess, calculate, or hallucinate a value that is not present in the text.
2. Exception: `currency` and `company_code` may be reasonably inferred from context (e.g. currency symbols, vendor country) if not explicit in the text.
3. If a mandatory field is not present in the text, use an empty string "" for header/line text fields, or 0 for missing required numeric fields. Never omit a key.
4. `po_number` must always be present as a key. Use null if no PO number is present anywhere in the text.
5. `gl_account`, `cost_center`, and `profit_center` on every line item MUST always be null. These are never present on vendor invoices.
6. Any field in the text that does not correspond to a fixed schema field (e.g. GSTIN, PAN No, Contract No, EAN No, Vessel Reference, Week Ending, "Your Reference") must go into `additional_fields` as {{"field_name": ..., "field_value": ...}}. Never invent a new top-level key.
7. `line_items` must contain at least one entry with `description` and `amount` populated from the text.
8. Return ONLY the JSON object. No commentary, no markdown fences.
"""


def build_prompt(source_text: str) -> str:
    return f"{SYSTEM_INSTRUCTIONS}\n\n--- INVOICE TEXT START ---\n{source_text}\n--- INVOICE TEXT END ---\n"


def _build_correction_prompt(original_prompt: str, previous_raw: dict, error: Exception) -> str:
    return (
        f"{original_prompt}\n\n"
        "--- YOUR PREVIOUS RESPONSE WAS INVALID ---\n"
        f"You returned:\n{json.dumps(previous_raw, indent=2)}\n\n"
        f"That failed validation with this error:\n{error}\n\n"
        "Fix it to conform EXACTLY to the three-top-level-key structure and field names shown "
        "in the worked example above (invoice_header / line_items / additional_fields - no "
        "flattening, no renamed fields, no extra top-level keys). Return ONLY the corrected "
        "JSON object, nothing else."
    )


def structure_invoice(
    source_text: str, *, client: OllamaClient | None = None, log=None
) -> InvoiceExtraction:
    log = log or logger
    client = client or OllamaClient()
    schema = get_invoice_json_schema()
    prompt = build_prompt(source_text)

    last_error: Exception | None = None
    last_raw: dict | None = None
    max_attempts = 3  # self-hosted rarely needs more than 1; cloud backends that ignore the
    # schema constraint benefit from the extra error-corrective attempts below
    for attempt in range(1, max_attempts + 1):
        attempt_prompt = (
            prompt if last_raw is None else _build_correction_prompt(prompt, last_raw, last_error)
        )
        raw: dict | None = None
        try:
            log.info("llm_call_attempt", attempt=attempt, max_attempts=max_attempts)
            raw = client.generate_structured(attempt_prompt, schema, temperature=0.0)
            extraction = InvoiceExtraction.model_validate(raw)
            log.info("llm_call_succeeded", attempt=attempt)
            return extraction
        except Exception as exc:  # noqa: BLE001 - deliberately broad, retried then re-raised
            last_error = exc
            last_raw = raw
            log.warning("llm_call_attempt_failed", attempt=attempt, error=str(exc))
            continue
    log.error("llm_structuring_failed", attempts=max_attempts, error=str(last_error))
    raise LLMFormatError(f"LLM structuring failed after retries: {last_error}") from last_error
