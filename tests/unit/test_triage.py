import fitz

from app.pipeline.triage import triage_pdf


def _make_pdf_with_text(text: str) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


def _make_blank_pdf() -> bytes:
    doc = fitz.open()
    doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


def test_digital_page_with_ample_text_detected_as_digital():
    pdf_bytes = _make_pdf_with_text("Invoice Number: 12345\n" * 20)
    pages = triage_pdf(pdf_bytes)
    assert len(pages) == 1
    assert pages[0].is_digital is True


def test_blank_page_detected_as_scanned():
    pdf_bytes = _make_blank_pdf()
    pages = triage_pdf(pdf_bytes)
    assert len(pages) == 1
    assert pages[0].is_digital is False
