import httpx
import pytest

from app.services.ollama_client import OllamaClient, settings


class _FakeResponse:
    def __init__(self, json_body: dict, status_code: int = 200):
        self._json_body = json_body
        self.status_code = status_code
        self.text = str(json_body)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "http://fake/api/generate")
            response = httpx.Response(self.status_code, request=request, text=self.text)
            raise httpx.HTTPStatusError("error", request=request, response=response)

    def json(self) -> dict:
        return self._json_body


def test_no_auth_header_when_api_key_unset(monkeypatch):
    # Explicit api_key=None falls back to settings.ollama_api_key (so a shared client can
    # pick up .env without passing it everywhere) - isolate from whatever the real .env
    # on disk happens to contain, since a real deployment will have a key configured there.
    monkeypatch.setattr(settings, "ollama_api_key", None)
    captured = {}

    def fake_post(self, url, json=None, headers=None):
        captured["headers"] = headers
        return _FakeResponse({"response": "{}"})

    monkeypatch.setattr(httpx.Client, "post", fake_post)

    client = OllamaClient(hosts=["http://localhost:11434"], model="qwen2.5:7b", api_key=None)
    client.generate_structured("prompt", {"type": "object"})

    assert captured["headers"] == {}


def test_auth_header_sent_when_api_key_set(monkeypatch):
    captured = {}

    def fake_post(self, url, json=None, headers=None):
        captured["url"] = url
        captured["headers"] = headers
        return _FakeResponse({"response": "{}"})

    monkeypatch.setattr(httpx.Client, "post", fake_post)

    client = OllamaClient(hosts=["https://ollama.com"], model="gemma4:31b", api_key="test-key-123")
    client.generate_structured("prompt", {"type": "object"})

    assert captured["headers"] == {"Authorization": "Bearer test-key-123"}
    assert captured["url"] == "https://ollama.com/api/generate"


def test_401_error_surfaces_response_body(monkeypatch):
    def fake_post(self, url, json=None, headers=None):
        return _FakeResponse({"error": "invalid api key"}, status_code=401)

    monkeypatch.setattr(httpx.Client, "post", fake_post)

    client = OllamaClient(hosts=["https://ollama.com"], model="gemma4:31b", api_key="bad-key")
    with pytest.raises(Exception, match="invalid api key"):
        client.generate_structured("prompt", {"type": "object"})
