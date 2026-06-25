# OpenMed Multilingual Privacy Filter Proxy

Proxy OpenAI-compatible `/v1/*` qui anonymise les PII avec [`OpenMed/privacy-filter-multilingual`](https://huggingface.co/OpenMed/privacy-filter-multilingual) avant transmission à un backend LLM OpenAI-compatible.

## Fonctionnement

```text
Client OpenAI-compatible
→ api-llm-privacy-filter-multilingual
→ redaction PII multilingue : [EMAIL_1], [FIRSTNAME_1], [DATEOFBIRTH_1], etc.
→ upstream OpenAI-compatible
```

Le projet reprend le principe de [`ynotopec/api-llm-privacy-proxy`](https://github.com/ynotopec/api-llm-privacy-proxy), mais configure par défaut le modèle Hugging Face `OpenMed/privacy-filter-multilingual` pour une détection PII plus fine et multilingue.

## Installation

```bash
./install.sh
cp .env.example .env
nano .env
./run.sh 0.0.0.0 8088
```

## Variables importantes

```bash
INBOUND_API_KEYS='change-me'
UPSTREAM_BASE_URL='http://127.0.0.1:8000/v1'
UPSTREAM_API_KEY=''
PRIVACY_MODEL_ID='OpenMed/privacy-filter-multilingual'
DEVICE=auto
TORCH_DTYPE=auto
TRUST_REMOTE_CODE=true
FILTER_OUTPUT=true
MODEL_SUFFIX='-anonym'
MIN_ENTITY_SCORE=0.50
```

## Appel OpenAI-compatible

```bash
curl -s http://127.0.0.1:8088/v1/chat/completions \
  -H 'Authorization: Bearer change-me' \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "gpt-4o-anonym",
    "messages": [
      {
        "role": "user",
        "content": "Bonjour, je suis Alice Martin, née le 03/15/1985, email alice@example.com"
      }
    ]
  }' | jq .
```

Le suffixe `MODEL_SUFFIX` est uniquement exposé au client. Le proxy retire `-anonym` avant d'appeler l'upstream, puis le rajoute dans les réponses JSON et `/v1/models`.

## Endpoints

- `GET /health` : état du proxy et configuration principale.
- `GET /metrics` : métriques Prometheus, protégées par `INBOUND_API_KEYS` si `METRICS_REQUIRE_AUTH=true`.
- `/v1/{path}` : proxy générique OpenAI-compatible.

## Développement et tests

```bash
python -m py_compile app.py fake_upstream.py
pytest -q
```

Pour tester sans vrai serveur LLM :

```bash
uvicorn fake_upstream:app --host 127.0.0.1 --port 8000
./run.sh 127.0.0.1 8088
```

## Notes production

- Par défaut, le proxy filtre les entrées envoyées au LLM et les réponses du LLM (`FILTER_OUTPUT=true`).
- `OpenMed/privacy-filter-multilingual` expose 54 catégories PII et couvre 16 langues, mais un seuil et des règles métier doivent être validés sur vos données.
- `TRUST_REMOTE_CODE=true` est activé par défaut pour charger correctement le modèle OpenMed via Transformers.
- Le streaming est transmis tel quel : l'entrée est filtrée avant l'appel upstream, mais les chunks de sortie streamés ne sont pas réécrits.
- Pour contexte gouvernemental, médical, RH ou financier, valider sur corpus interne et ajouter des règles déterministes si nécessaire.
