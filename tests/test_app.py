import json

import pytest
from fastapi import Request, Response

import app


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    monkeypatch.setattr(app.settings, "inbound_api_keys", ["test-key"])
    monkeypatch.setattr(app.settings, "upstream_api_key", "")
    monkeypatch.setattr(app.settings, "model_suffix", "-anonym")
    monkeypatch.setattr(app.settings, "filter_output", True)
    app.metrics.requests_total = 0
    app.metrics.filtered_requests_total = 0
    app.metrics.filtered_tokens_total = 0
    app.metrics.filtered_spans_total = 0
    app.metrics.filtered_by_label = {}


def test_model_suffix_helpers():
    assert app.suffix_model_id("gpt-4o") == "gpt-4o-anonym"
    assert app.suffix_model_id("gpt-4o-anonym") == "gpt-4o-anonym"
    assert app.unsuffix_model_id("gpt-4o-anonym") == "gpt-4o"
    assert app.unsuffix_model_path("models/gpt-4o-anonym") == "models/gpt-4o"


@pytest.mark.asyncio
async def test_sanitize_payload_redacts_stable_placeholders(monkeypatch):
    sanitizer = app.PrivacySanitizer()

    async def fake_ensure_loaded():
        sanitizer.tokenizer = None

    def fake_classifier(text):
        entities = []
        start = text.find("Alice")
        while start >= 0:
            entities.append({"start": start, "end": start + 5, "entity_group": "FIRSTNAME", "score": 0.99})
            start = text.find("Alice", start + 1)
        email = "alice@example.com"
        start = text.find(email)
        if start >= 0:
            entities.append({"start": start, "end": start + len(email), "entity_group": "EMAIL", "score": 0.99})
        return entities

    monkeypatch.setattr(sanitizer, "ensure_loaded", fake_ensure_loaded)
    sanitizer.classifier = fake_classifier
    payload, stats = await sanitizer.sanitize_payload({"model": "demo-anonym", "messages": [{"role": "user", "content": "Alice: alice@example.com. Alice again."}]})
    assert payload["model"] == "demo-anonym"
    assert payload["messages"][0]["content"] == "[FIRSTNAME_1]: [EMAIL_1]. [FIRSTNAME_1] again."
    assert stats.spans == 3
    assert stats.labels == {"firstname": 2, "email": 1}


@pytest.mark.asyncio
async def test_proxy_sanitizes_and_rewrites_models(monkeypatch):
    async def fake_sanitize_payload(payload):
        if "messages" in payload:
            payload = json.loads(json.dumps(payload))
            payload["messages"][0]["content"] = "Bonjour [FIRSTNAME_1]"
            return payload, app.RedactionStats(tokens=1, spans=1, labels={"firstname": 1})
        return payload, app.RedactionStats()

    async def fake_forward_request(req: Request, full_path: str, sanitized_payload, stream: bool = False):
        assert sanitized_payload["model"] == "demo-model"
        assert sanitized_payload["messages"][0]["content"] == "Bonjour [FIRSTNAME_1]"
        return Response(
            content=json.dumps({"model": "demo-model", "choices": [{"message": {"content": "OK"}}]}),
            status_code=200,
            media_type="application/json",
        )

    monkeypatch.setattr(app.sanitizer, "sanitize_payload", fake_sanitize_payload)
    monkeypatch.setattr(app, "forward_request", fake_forward_request)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "headers": [(b"authorization", b"Bearer test-key"), (b"content-type", b"application/json")],
        "query_string": b"",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "scheme": "http",
    }
    body = json.dumps({"model": "demo-model-anonym", "messages": [{"role": "user", "content": "Bonjour Alice"}]}).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    response = await app.proxy_openai(Request(scope, receive), "chat/completions")
    assert response.status_code == 200
    assert json.loads(response.body)["model"] == "demo-model-anonym"
    assert response.headers["x-privacy-filtered-spans"] == "1"
