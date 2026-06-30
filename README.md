# OpenMed Multilingual Privacy Filter Proxy

Minimal OpenAI-compatible `/v1/*` privacy proxy. It redacts multilingual PII with `OpenMed/privacy-filter-multilingual` and requires a bearer token from callers. An OpenAI-compatible LLM upstream is optional: configure one to proxy completions, or leave it empty to run the service as a privacy-only redaction API.

## Quick start

```bash
./install.sh
nano .env
source ./run.sh 0.0.0.0 8088
```

`install.sh` is idempotent and upgrade-safe: it uses `uv`, creates or reuses `~/venv/api-llm-privacy-filter-multilingual` by default, upgrades dependencies from `requirements.txt`, and creates `.env` from `.env.example` only when `.env` does not exist.

`run.sh [IP] [PORT]` can be executed directly or sourced. Direct execution is suitable for systemd because it `exec`s uvicorn; sourced execution keeps compatibility with shell workflows.

## Required configuration

```bash
cp .env.example .env
nano .env
```

Important variables:

```bash
INBOUND_API_KEYS=change-me
UPSTREAM_BASE_URL=
UPSTREAM_API_KEY=
PRIVACY_MODEL_ID=OpenMed/privacy-filter-multilingual
```

`UPSTREAM_BASE_URL` and `UPSTREAM_API_KEY` are optional. Leave `UPSTREAM_BASE_URL` empty for privacy-only mode, or set it to an OpenAI-compatible `/v1` endpoint to forward sanitized requests to an LLM. Optional/default variables are commented in `.env.example`.

## API

When `UPSTREAM_BASE_URL` is configured, the proxy is OpenAI-compatible and forwards popular `/v1/*` APIs, including:

- `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /v1/responses`
- `POST /v1/completions`
- `POST /v1/embeddings`
- any other OpenAI-compatible `/v1/{path}` endpoint supported by your upstream

Authentication uses bearer tokens from `INBOUND_API_KEYS`:

```bash
curl -s http://127.0.0.1:8088/v1/chat/completions \
  -H 'Authorization: Bearer change-me' \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "gpt-4o-anonym",
    "messages": [
      {"role": "user", "content": "Bonjour, je suis Alice Martin, email alice@example.com"}
    ]
  }' | jq .
```

In privacy-only mode, POST requests to `/sanitize` or `/redact` return the sanitized payload plus redaction counts without calling an LLM. OpenAI-compatible endpoints such as `/v1/chat/completions`, `/v1/completions`, and `/v1/responses` keep an OpenAI-compatible response shape and put the sanitized text in the normal output field, for example `choices[0].message.content` for chat completions.

```bash
curl -s http://127.0.0.1:8088/sanitize \
  -H 'Authorization: Bearer change-me' \
  -H 'Content-Type: application/json' \
  -d '{"text":"Bonjour, je suis Alice Martin, email alice@example.com"}' | jq .
```

`MODEL_SUFFIX` is exposed to clients only when LLM proxying is enabled. By default the client asks for `gpt-4o-anonym`; the proxy sends `gpt-4o` upstream and adds `-anonym` back in JSON responses and `/v1/models`.

## GPU notes: H100 and DGX Spark

Defaults are hardware-compatible:

- `DEVICE=auto` lets Transformers/Accelerate choose CUDA devices when available.
- `TORCH_DTYPE=auto` keeps dtype selection conservative across CPU, H100, and DGX Spark style CUDA environments.
- For H100, you may set `TORCH_DTYPE=bf16` after validating your local PyTorch/CUDA stack.
- For constrained GPU memory, keep `MODEL_IDLE_UNLOAD_SECONDS=300` or lower it so idle model weights are released.

Install the PyTorch build recommended for your NVIDIA driver/CUDA image before or after `./install.sh` if your base image does not already provide a suitable GPU-enabled `torch` wheel.

## Health, metrics, and tests

```bash
curl -s http://127.0.0.1:8088/health | jq .
curl -s -H 'Authorization: Bearer change-me' http://127.0.0.1:8088/metrics
python -m py_compile app.py fake_upstream.py
pytest -q
```

For local upstream testing:

```bash
uvicorn fake_upstream:app --host 127.0.0.1 --port 8000
source ./run.sh 127.0.0.1 8088
```

## systemd example

```ini
[Unit]
Description=OpenMed Privacy Filter Proxy
After=network-online.target

[Service]
WorkingDirectory=/workspace/api-llm-privacy-filter-multilingual
Environment=VENV_DIR=/root/venv/api-llm-privacy-filter-multilingual
ExecStart=/workspace/api-llm-privacy-filter-multilingual/run.sh 0.0.0.0 8088
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```
