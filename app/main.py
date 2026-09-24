from fastapi import FastAPI

from app.api.routers import health, invoices, metrics
from app.logging_conf import configure_logging

configure_logging()

app = FastAPI(
    title="LT Foods Invoice Extraction API",
    version="1.0.0",
    description=(
        "Invoice PDFs arriving by email are extracted to structured JSON for SAP "
        "MIRO/FB60 posting; GET /api/v1/invoices/new returns each new result once. "
        "SAP decides routing based on po_number; this API only populates fields."
    ),
)

app.include_router(health.router)
app.include_router(invoices.router)
app.include_router(metrics.router)
