from fastapi import FastAPI, Request

app = FastAPI(title="Fake OpenAI-compatible upstream")


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": "demo-model", "object": "model"}]}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    payload = await request.json()
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "model": payload.get("model", "demo-model"),
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Réponse upstream: " + str(payload.get("messages", [])),
                },
                "finish_reason": "stop",
            }
        ],
    }
