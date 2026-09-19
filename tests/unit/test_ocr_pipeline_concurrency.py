import threading
import time

import pytest

from app.core.exceptions import OcrTimeoutError
from app.pipeline import run as run_module


class _SlowEngine:
    def ocr_page_image(self, image_bytes, page_number):
        time.sleep(0.3)
        return ["line"]


class _FastEngine:
    def ocr_page_image(self, image_bytes, page_number):
        return ["line"]


def test_ocr_timeout_raises_when_engine_exceeds_deadline(monkeypatch):
    monkeypatch.setattr(run_module.settings, "ocr_timeout_seconds", 0.05)
    monkeypatch.setattr(run_module, "_get_ocr_engine", lambda: _SlowEngine())

    with pytest.raises(OcrTimeoutError):
        run_module._ocr_page_with_timeout(b"fake-image-bytes", page_number=1)


def test_ocr_timeout_returns_result_within_deadline(monkeypatch):
    monkeypatch.setattr(run_module.settings, "ocr_timeout_seconds", 5)
    monkeypatch.setattr(run_module, "_get_ocr_engine", lambda: _FastEngine())

    result = run_module._ocr_page_with_timeout(b"fake-image-bytes", page_number=1)

    assert result == ["line"]


def test_ocr_engine_is_cached_per_thread_but_not_shared_across_threads(monkeypatch):
    created: list[object] = []

    def _fake_get_ocr_engine(name: str) -> object:
        engine = object()
        created.append(engine)
        return engine

    monkeypatch.setattr(run_module, "get_ocr_engine", _fake_get_ocr_engine)
    if hasattr(run_module._thread_local, "ocr_engine"):
        del run_module._thread_local.ocr_engine

    engine_a = run_module._get_ocr_engine()
    engine_again = run_module._get_ocr_engine()
    assert engine_a is engine_again  # cached within the same thread, no re-init per call

    other_thread_result: dict[str, object] = {}

    def _in_other_thread() -> None:
        other_thread_result["engine"] = run_module._get_ocr_engine()

    thread = threading.Thread(target=_in_other_thread)
    thread.start()
    thread.join()

    # a different thread gets its own instance rather than racing on the shared one
    assert other_thread_result["engine"] is not engine_a
    assert len(created) == 2
