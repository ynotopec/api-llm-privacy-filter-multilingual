import asyncio
import gc
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx
import torch
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from starlette.background import BackgroundTask

load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "info").upper()
logging.basicConfig(level=LOG_LEVEL)
log = logging.getLogger("llm-privacy-filter-multilingual")


@dataclass
class Settings:
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8088"))
    inbound_api_keys: List[str] = field(
        default_factory=lambda: [x.strip() for x in os.getenv("INBOUND_API_KEYS", "").split(",") if x.strip()]
    )
    upstream_base_url: str = os.getenv("UPSTREAM_BASE_URL", "").rstrip("/")
    upstream_api_key: str = os.getenv("UPSTREAM_API_KEY", "")
    privacy_model_id: str = os.getenv("PRIVACY_MODEL_ID", "OpenMed/privacy-filter-multilingual")
    device: str = os.getenv("DEVICE", "auto")
    torch_dtype: str = os.getenv("TORCH_DTYPE", "auto")
    trust_remote_code: bool = os.getenv("TRUST_REMOTE_CODE", "true").lower() in ("1", "true", "yes", "on")
    filter_output: bool = os.getenv("FILTER_OUTPUT", "true").lower() in ("1", "true", "yes", "on")
    min_entity_score: float = float(os.getenv("MIN_ENTITY_SCORE", "0.50"))
    max_string_chars: int = int(os.getenv("MAX_STRING_CHARS", "200000"))
    model_idle_unload_seconds: int = int(os.getenv("MODEL_IDLE_UNLOAD_SECONDS", "300"))
    model_suffix: str = os.getenv("MODEL_SUFFIX", "-anonym")
    skip_json_keys: set = field(
        default_factory=lambda: {
            x.strip()
            for x in os.getenv(
                "SKIP_JSON_KEYS",
                "model,role,type,stream,temperature,max_tokens,top_p,tools,tool_choice,name,thinking,reasoning,reasoning_effort",
            ).split(",")
            if x.strip()
        }
    )
    metrics_require_auth: bool = os.getenv("METRICS_REQUIRE_AUTH", "true").lower() in ("1", "true", "yes", "on")

    @property
    def llm_enabled(self) -> bool:
        return bool(self.upstream_base_url)


settings = Settings()


def suffix_model_id(model_id: str) -> str:
    if not model_id or not settings.model_suffix or model_id.endswith(settings.model_suffix):
        return model_id
    return f"{model_id}{settings.model_suffix}"


def unsuffix_model_id(model_id: str) -> str:
    suffix = settings.model_suffix
    if suffix and model_id.endswith(suffix) and len(model_id) > len(suffix):
        return model_id[: -len(suffix)]
    return model_id


def unsuffix_model_path(full_path: str) -> str:
    parts = full_path.split("/")
    if len(parts) >= 2 and parts[0] == "models":
        parts[1] = unsuffix_model_id(parts[1])
    return "/".join(parts)


def rewrite_request_model_ids(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    out = dict(value)
    model_id = out.get("model")
    if isinstance(model_id, str):
        model_id = model_id.strip()
        if not model_id or model_id == settings.model_suffix:
            raise HTTPException(status_code=400, detail="invalid_model_id")
        out["model"] = unsuffix_model_id(model_id)
    return out


def rewrite_response_model_ids(value: Any, *, models_endpoint: bool = False) -> Any:
    if isinstance(value, list):
        return [rewrite_response_model_ids(item, models_endpoint=models_endpoint) for item in value]
    if not isinstance(value, dict):
        return value
    out = dict(value)
    model_id = out.get("model")
    if isinstance(model_id, str):
        out["model"] = suffix_model_id(model_id)
    if models_endpoint:
        object_id = out.get("id")
        if isinstance(object_id, str) and out.get("object") == "model":
            out["id"] = suffix_model_id(object_id)
        data = out.get("data")
        if isinstance(data, list):
            out["data"] = [rewrite_response_model_ids(item, models_endpoint=True) for item in data]
    return out


class GlobalMetrics:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.requests_total = 0
        self.filtered_requests_total = 0
        self.filtered_tokens_total = 0
        self.filtered_spans_total = 0
        self.filtered_by_label: Dict[str, int] = {}

    async def add(self, tokens: int, spans: int, labels: Dict[str, int], *, count_request: bool = True) -> None:
        async with self.lock:
            if count_request:
                self.requests_total += 1
            if tokens > 0 or spans > 0:
                self.filtered_requests_total += 1
            self.filtered_tokens_total += tokens
            self.filtered_spans_total += spans
            for key, value in labels.items():
                self.filtered_by_label[key] = self.filtered_by_label.get(key, 0) + value

    async def prometheus(self) -> str:
        async with self.lock:
            lines = [
                "# HELP privacy_proxy_requests_total Total proxied requests.",
                "# TYPE privacy_proxy_requests_total counter",
                f"privacy_proxy_requests_total {self.requests_total}",
                "# HELP privacy_proxy_filtered_requests_total Requests where at least one span was filtered.",
                "# TYPE privacy_proxy_filtered_requests_total counter",
                f"privacy_proxy_filtered_requests_total {self.filtered_requests_total}",
                "# HELP privacy_proxy_filtered_tokens_total Estimated number of model-tokenized tokens filtered.",
                "# TYPE privacy_proxy_filtered_tokens_total counter",
                f"privacy_proxy_filtered_tokens_total {self.filtered_tokens_total}",
                "# HELP privacy_proxy_filtered_spans_total Number of PII spans filtered.",
                "# TYPE privacy_proxy_filtered_spans_total counter",
                f"privacy_proxy_filtered_spans_total {self.filtered_spans_total}",
            ]
            for label, count in sorted(self.filtered_by_label.items()):
                lines.append(f'privacy_proxy_filtered_spans_by_label_total{{label="{label.replace(chr(34), "")}"}} {count}')
            return "\n".join(lines) + "\n"


metrics = GlobalMetrics()


@dataclass
class RedactionStats:
    tokens: int = 0
    spans: int = 0
    labels: Dict[str, int] = field(default_factory=dict)

    def add(self, label: str, token_count: int) -> None:
        self.tokens += token_count
        self.spans += 1
        self.labels[label] = self.labels.get(label, 0) + 1


class RedactionContext:
    def __init__(self) -> None:
        self.by_value: Dict[Tuple[str, str], str] = {}
        self.next_index: Dict[str, int] = {}

    def placeholder(self, label: str, value: str) -> str:
        label = normalize_label(label)
        key = (label, value)
        if key in self.by_value:
            return self.by_value[key]
        self.next_index[label] = self.next_index.get(label, 0) + 1
        placeholder = f"[{label.upper()}_{self.next_index[label]}]"
        self.by_value[key] = placeholder
        return placeholder


def normalize_label(label: str) -> str:
    return (label or "private").replace("B-", "").replace("I-", "").replace("E-", "").replace("S-", "").lower()


class PrivacySanitizer:
    def __init__(self) -> None:
        self.tokenizer = None
        self.classifier = None
        self._last_used_at = 0.0
        self._load_lock = asyncio.Lock()

    def _touch(self) -> None:
        self._last_used_at = time.monotonic()

    def unload_if_idle(self) -> None:
        if self.classifier is None or settings.model_idle_unload_seconds <= 0:
            return
        if time.monotonic() - self._last_used_at < settings.model_idle_unload_seconds:
            return
        self.classifier = None
        self.tokenizer = None
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    async def ensure_loaded(self) -> None:
        self.unload_if_idle()
        if self.classifier is not None:
            return
        async with self._load_lock:
            if self.classifier is not None:
                return
            log.info("Loading OpenMed privacy model: %s", settings.privacy_model_id)
            from transformers import AutoModelForTokenClassification, AutoTokenizer, pipeline

            model_kwargs: Dict[str, Any] = {"trust_remote_code": settings.trust_remote_code}
            if settings.torch_dtype == "bf16":
                model_kwargs["torch_dtype"] = torch.bfloat16
            elif settings.torch_dtype == "fp16":
                model_kwargs["torch_dtype"] = torch.float16
            elif settings.torch_dtype == "fp32":
                model_kwargs["torch_dtype"] = torch.float32
            if settings.device in ("cuda", "cpu"):
                model_kwargs["device_map"] = settings.device
            else:
                model_kwargs["device_map"] = "auto"
            self.tokenizer = AutoTokenizer.from_pretrained(settings.privacy_model_id, trust_remote_code=settings.trust_remote_code)
            model = AutoModelForTokenClassification.from_pretrained(settings.privacy_model_id, **model_kwargs)
            model.eval()
            self.classifier = pipeline("token-classification", model=model, tokenizer=self.tokenizer, aggregation_strategy="simple")
            self._touch()

    def count_tokens(self, text: str) -> int:
        try:
            return len(self.tokenizer.encode(text, add_special_tokens=False)) if self.tokenizer else max(1, len(text.split()))
        except Exception:
            return max(1, len(text.split()))

    def _extract_entities(self, text: str) -> List[Dict[str, Any]]:
        if self.classifier is None:
            return []
        return self.classifier(text)

    @staticmethod
    def _entity_score(entity: Dict[str, Any]) -> float:
        return float(entity.get("score", entity.get("confidence", entity.get("probability", 1.0))))

    @staticmethod
    def _entity_label(entity: Dict[str, Any]) -> str:
        return normalize_label(str(entity.get("entity_group") or entity.get("entity") or entity.get("label") or "private"))

    @staticmethod
    def _entity_span(text: str, entity: Dict[str, Any]) -> Optional[Tuple[int, int]]:
        start = entity.get("start")
        end = entity.get("end")
        if start is None or end is None:
            value = entity.get("word") or entity.get("text") or entity.get("span")
            idx = text.find(value) if isinstance(value, str) and value else -1
            if idx >= 0:
                start, end = idx, idx + len(value)
        if start is None or end is None:
            return None
        start, end = int(start), int(end)
        if 0 <= start < end <= len(text):
            return start, end
        return None

    async def sanitize_text(self, text: str, ctx: RedactionContext, stats: RedactionStats) -> str:
        if not text or len(text) > settings.max_string_chars:
            return text
        await self.ensure_loaded()
        self._touch()
        try:
            entities = self._extract_entities(text)
        except Exception as exc:
            log.exception("Privacy model inference failed")
            raise HTTPException(status_code=500, detail=f"privacy_filter_failed: {exc}") from exc
        spans = []
        for entity in entities:
            score = self._entity_score(entity)
            if score < settings.min_entity_score:
                continue
            span = self._entity_span(text, entity)
            if span is None:
                continue
            spans.append((span[0], span[1], self._entity_label(entity), score))
        return self._replace_spans(text, spans, ctx, stats)

    def _replace_spans(self, text: str, spans: List[Tuple[int, int, str, float]], ctx: RedactionContext, stats: RedactionStats) -> str:
        if not spans:
            return text
        spans.sort(key=lambda item: (item[0], -(item[1] - item[0])))
        merged = []
        for span in spans:
            if not merged or span[0] >= merged[-1][1]:
                merged.append(span)
            elif span[1] > merged[-1][1]:
                previous = merged[-1]
                merged[-1] = (previous[0], span[1], previous[2], max(previous[3], span[3]))
        output = []
        last = 0
        for start, end, label, _score in merged:
            original = text[start:end]
            output.extend([text[last:start], ctx.placeholder(label, original)])
            last = end
            stats.add(label, self.count_tokens(original))
        output.append(text[last:])
        return "".join(output)

    async def sanitize_payload(self, payload: Any) -> Tuple[Any, RedactionStats]:
        ctx = RedactionContext()
        stats = RedactionStats()
        return await self._sanitize_any(payload, ctx, stats, None), stats

    async def _sanitize_any(self, value: Any, ctx: RedactionContext, stats: RedactionStats, parent_key: Optional[str]) -> Any:
        if parent_key in settings.skip_json_keys:
            return value
        if isinstance(value, str):
            return await self.sanitize_text(value, ctx, stats)
        if isinstance(value, list):
            return [await self._sanitize_any(item, ctx, stats, None) for item in value]
        if isinstance(value, dict):
            return {key: await self._sanitize_any(item, ctx, stats, str(key)) for key, item in value.items()}
        return value


sanitizer = PrivacySanitizer()
app = FastAPI(title="OpenMed Multilingual Privacy Filter Proxy", version="1.0.0")


def extract_bearer(req: Request) -> str:
    auth = req.headers.get("authorization", "")
    return auth.split(" ", 1)[1].strip() if auth.lower().startswith("bearer ") else ""


def require_auth(req: Request, *, metrics_auth: bool = False) -> None:
    if metrics_auth and not settings.metrics_require_auth:
        return
    if settings.inbound_api_keys and extract_bearer(req) not in settings.inbound_api_keys:
        raise HTTPException(status_code=401, detail="invalid_or_missing_api_token")


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "model": settings.privacy_model_id,
        "llm_enabled": settings.llm_enabled,
        "upstream": settings.upstream_base_url or None,
        "filter_output": settings.filter_output,
    }


@app.get("/metrics")
async def get_metrics(req: Request) -> PlainTextResponse:
    require_auth(req, metrics_auth=True)
    return PlainTextResponse(await metrics.prometheus(), media_type="text/plain")


def build_upstream_headers(req: Request) -> Dict[str, str]:
    excluded = {"host", "content-length", "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade"}
    headers = {key: value for key, value in req.headers.items() if key.lower() not in excluded}
    if settings.upstream_api_key:
        headers["authorization"] = f"Bearer {settings.upstream_api_key}"
    headers["content-type"] = "application/json"
    return headers


def headers_for_modified_body(resp: Response) -> Dict[str, str]:
    return {key: value for key, value in resp.headers.items() if key.lower() not in {"content-length", "content-encoding", "transfer-encoding", "connection"}}


def response_models_endpoint(full_path: str) -> bool:
    return full_path == "models" or full_path.startswith("models/")


def add_response_model_suffixes(upstream_resp: Response, full_path: str) -> Response:
    if "application/json" not in upstream_resp.headers.get("content-type", ""):
        return upstream_resp
    try:
        payload = json.loads(upstream_resp.body)
    except Exception:
        return upstream_resp
    payload = rewrite_response_model_ids(payload, models_endpoint=response_models_endpoint(full_path))
    return JSONResponse(content=payload, status_code=upstream_resp.status_code, headers=headers_for_modified_body(upstream_resp))


async def forward_request(req: Request, full_path: str, sanitized_payload: Any, stream: bool = False) -> Response:
    if not settings.llm_enabled:
        raise HTTPException(status_code=503, detail="llm_upstream_disabled")
    url = f"{settings.upstream_base_url}/{unsuffix_model_path(full_path)}"
    timeout = httpx.Timeout(600.0, connect=30.0)
    if stream:
        client = httpx.AsyncClient(timeout=timeout)
        upstream_req = client.build_request(req.method, url, headers=build_upstream_headers(req), params=dict(req.query_params), json=sanitized_payload)
        upstream_stream = await client.send(upstream_req, stream=True)

        async def close_upstream() -> None:
            await upstream_stream.aclose()
            await client.aclose()

        headers = {key: value for key, value in upstream_stream.headers.items() if key.lower() not in {"content-length", "connection"}}
        return StreamingResponse(upstream_stream.aiter_bytes(), status_code=upstream_stream.status_code, headers=headers, media_type=upstream_stream.headers.get("content-type", "application/json"), background=BackgroundTask(close_upstream))
    async with httpx.AsyncClient(timeout=timeout) as client:
        upstream = await client.request(req.method, url, headers=build_upstream_headers(req), params=dict(req.query_params), json=sanitized_payload)
    headers = {key: value for key, value in upstream.headers.items() if key.lower() not in {"content-length", "content-encoding", "transfer-encoding", "connection"}}
    return Response(content=upstream.content, status_code=upstream.status_code, headers=headers, media_type=upstream.headers.get("content-type", "application/json"))


def attach_privacy_headers(response: JSONResponse, stats: RedactionStats, latency_ms: float) -> JSONResponse:
    response.headers["x-privacy-filtered-tokens"] = str(stats.tokens)
    response.headers["x-privacy-filtered-spans"] = str(stats.spans)
    response.headers["x-privacy-filter-latency-ms"] = str(round(latency_ms, 2))
    return response


def privacy_metadata(stats: RedactionStats) -> Dict[str, Any]:
    return {
        "filtered_tokens": stats.tokens,
        "filtered_spans": stats.spans,
        "filtered_by_label": stats.labels,
    }


def extract_sanitized_text(payload: Any) -> str:
    if isinstance(payload, dict):
        messages = payload.get("messages")
        if isinstance(messages, list):
            for message in reversed(messages):
                if isinstance(message, dict) and isinstance(message.get("content"), str):
                    return message["content"]
        for key in ("input", "prompt", "content", "text"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False)


def privacy_only_response(sanitized_payload: Any, stats: RedactionStats, latency_ms: float) -> JSONResponse:
    response = JSONResponse(
        content={
            "object": "privacy.redaction",
            "llm_enabled": False,
            "data": sanitized_payload,
            "privacy": privacy_metadata(stats),
        },
        status_code=200,
    )
    return attach_privacy_headers(response, stats, latency_ms)


def openai_privacy_only_response(full_path: str, sanitized_payload: Any, stats: RedactionStats, latency_ms: float) -> JSONResponse:
    created = int(time.time())
    model = "privacy-redaction"
    if isinstance(sanitized_payload, dict) and isinstance(sanitized_payload.get("model"), str):
        model = suffix_model_id(sanitized_payload["model"])
    content = extract_sanitized_text(sanitized_payload)
    metadata = {"llm_enabled": False, "privacy": privacy_metadata(stats)}
    if full_path == "chat/completions":
        payload = {
            "id": f"chatcmpl-privacy-{created}",
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            **metadata,
        }
    elif full_path == "completions":
        payload = {
            "id": f"cmpl-privacy-{created}",
            "object": "text_completion",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "text": content, "finish_reason": "stop"}],
            **metadata,
        }
    elif full_path == "responses":
        payload = {
            "id": f"resp-privacy-{created}",
            "object": "response",
            "created_at": created,
            "model": model,
            "output_text": content,
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": content}],
                }
            ],
            **metadata,
        }
    else:
        return privacy_only_response(sanitized_payload, stats, latency_ms)
    return attach_privacy_headers(JSONResponse(content=payload, status_code=200), stats, latency_ms)


@app.post("/redact")
@app.post("/sanitize")
async def redact_payload(req: Request) -> JSONResponse:
    require_auth(req)
    try:
        payload = await req.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="expected_json_body") from exc
    started_at = time.perf_counter()
    sanitized_payload, stats = await sanitizer.sanitize_payload(payload)
    await metrics.add(stats.tokens, stats.spans, stats.labels)
    return privacy_only_response(sanitized_payload, stats, (time.perf_counter() - started_at) * 1000)


@app.api_route("/v1/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def proxy_openai(req: Request, full_path: str) -> Response:
    if req.method == "OPTIONS":
        return Response(status_code=204)
    require_auth(req)
    if req.method in ("GET", "DELETE"):
        if not settings.llm_enabled:
            raise HTTPException(status_code=503, detail="llm_upstream_disabled")
        return add_response_model_suffixes(await forward_request(req, full_path, None), full_path)
    try:
        payload = await req.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="expected_json_body") from exc
    started_at = time.perf_counter()
    sanitized_payload, in_stats = await sanitizer.sanitize_payload(payload)
    sanitized_payload = rewrite_request_model_ids(sanitized_payload)
    await metrics.add(in_stats.tokens, in_stats.spans, in_stats.labels)
    if not settings.llm_enabled:
        return openai_privacy_only_response(full_path, sanitized_payload, in_stats, (time.perf_counter() - started_at) * 1000)
    if isinstance(payload, dict) and payload.get("stream") is True:
        return await forward_request(req, full_path, sanitized_payload, stream=True)
    upstream_resp = await forward_request(req, full_path, sanitized_payload)
    upstream_resp.headers["x-privacy-filtered-tokens"] = str(in_stats.tokens)
    upstream_resp.headers["x-privacy-filtered-spans"] = str(in_stats.spans)
    upstream_resp.headers["x-privacy-filter-latency-ms"] = str(round((time.perf_counter() - started_at) * 1000, 2))
    rewritten_resp = add_response_model_suffixes(upstream_resp, full_path)
    if "application/json" not in rewritten_resp.headers.get("content-type", ""):
        return rewritten_resp
    try:
        response_payload = json.loads(rewritten_resp.body)
    except Exception:
        return rewritten_resp
    out_stats = RedactionStats()
    if settings.filter_output:
        response_payload, out_stats = await sanitizer.sanitize_payload(response_payload)
        await metrics.add(out_stats.tokens, out_stats.spans, out_stats.labels, count_request=False)
    final = JSONResponse(content=response_payload, status_code=rewritten_resp.status_code, headers=headers_for_modified_body(rewritten_resp))
    if settings.filter_output:
        final.headers["x-privacy-filtered-output-tokens"] = str(out_stats.tokens)
        final.headers["x-privacy-filtered-output-spans"] = str(out_stats.spans)
    return final


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host=settings.host, port=settings.port, log_level=os.getenv("LOG_LEVEL", "info"))
