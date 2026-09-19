from typing import Optional

from pydantic import BaseModel, Field


class InvoiceHeader(BaseModel):
    # Required fields have no default: this makes them "required" in the JSON schema Ollama
    # is constrained to, so the model must always emit the key (value may still be "").
    invoice_number: str
    invoice_date: str
    due_date: Optional[str] = None
    payment_terms: Optional[str] = None
    company_code: str
    vendor_name: str
    vendor_address: Optional[str] = None
    vendor_tax_id: Optional[str] = None
    vendor_bank_name: Optional[str] = None
    vendor_account_no: Optional[str] = None
    vendor_sort_code: Optional[str] = None
    vendor_iban: Optional[str] = None
    customer_name: str
    customer_address: Optional[str] = None
    po_number: Optional[str] = None  # SAP uses null vs present to decide MIRO vs FB60
    reference_number: Optional[str] = None
    currency: str
    subtotal: float
    tax_amount: float
    tax_percent: Optional[float] = None
    total_amount: float


class LineItem(BaseModel):
    line_no: Optional[str] = None
    description: str
    quantity: Optional[float] = None
    unit_price: Optional[float] = None
    amount: float
    tax_percent: Optional[float] = None
    gl_account: Optional[str] = None  # always null from extraction - filled by SAP logic
    cost_center: Optional[str] = None  # always null from extraction - filled by SAP logic
    profit_center: Optional[str] = None  # always null from extraction - filled by SAP logic
    reference_code: Optional[str] = None


class AdditionalField(BaseModel):
    field_name: str
    field_value: str


class InvoiceExtraction(BaseModel):
    invoice_header: InvoiceHeader
    line_items: list[LineItem] = Field(default_factory=list)
    additional_fields: list[AdditionalField] = Field(default_factory=list)
