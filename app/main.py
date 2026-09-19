from fastapi import FastAPI

from app.api.routers import health, invoices, metrics
from app.logging_conf import configure_logging

configure_logging()

app = FastAPI(
    title="LT Foods Invoice Extraction API",
    version="1.0.0",
    description=(
        "Upload a scanned/digital invoice PDF and receive structured JSON for SAP "
        "MIRO/FB60 posting. SAP decides routing based on po_number; this API only "
        "populates fields."
    ),
)

app.include_router(health.router)
app.include_router(invoices.router)
app.include_router(metrics.router)
