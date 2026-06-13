import os
import logging

import httpx
from fastapi import FastAPI, Request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("telegram-bot")

app = FastAPI()

# 환경변수 (docker-compose / .env 에서 주입)
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434/api/generate")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:1.7b")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

# 외부 호출용 비동기 클라이언트 (타임아웃 넉넉히 — LLM 추론이 오래 걸릴 수 있음)
client = httpx.AsyncClient(timeout=120.0)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/telegram")
async def telegram(request: Request):
    data = await request.json()

    # 텍스트 메시지가 아닌 업데이트(사진, 멤버 변경 등)는 무시
    message = data.get("message")
    if not message or "text" not in message:
        return {"ok": True}

    text = message["text"]
    chat_id = message["chat"]["id"]

    try:
        ollama_response = await client.post(
            OLLAMA_URL,
            json={
                "model": OLLAMA_MODEL,
                "prompt": text,
                "stream": False,
            },
        )
        ollama_response.raise_for_status()
        answer = ollama_response.json()["response"]
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ollama 호출 실패")
        answer = "⚠️ 답변 생성 중 오류가 발생했어요. 잠시 후 다시 시도해 주세요."

    try:
        await client.post(
            TELEGRAM_API,
            json={"chat_id": chat_id, "text": answer},
        )
    except Exception:  # noqa: BLE001
        logger.exception("Telegram 전송 실패")

    return {"ok": True}


@app.on_event("shutdown")
async def shutdown():
    await client.aclose()
