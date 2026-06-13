import os
import logging

import httpx
from fastapi import FastAPI, Request, BackgroundTasks

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("telegram-bot")

app = FastAPI()

# 환경변수 (docker-compose / .env 에서 주입)
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:1.7b")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

# LLM 추론은 오래 걸릴 수 있으므로 읽기 타임아웃 없음(연결만 10초 제한)
client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0))


@app.get("/health")
async def health():
    return {"status": "ok"}


async def process_message(text: str, chat_id: int):
    """Ollama 생성 + 텔레그램 전송. 웹훅 응답과 분리해 백그라운드에서 실행."""
    try:
        ollama_response = await client.post(
            OLLAMA_URL,
            json={"model": OLLAMA_MODEL, "prompt": text, "stream": False},
        )
        ollama_response.raise_for_status()
        answer = ollama_response.json()["response"]
    except Exception:  # noqa: BLE001
        logger.exception("Ollama 호출 실패")
        answer = "⚠️ 답변 생성 중 오류가 발생했어요. 잠시 후 다시 시도해 주세요."

    try:
        await client.post(
            TELEGRAM_API,
            json={"chat_id": chat_id, "text": answer},
        )
    except Exception:  # noqa: BLE001
        logger.exception("Telegram 전송 실패")


@app.post("/telegram")
async def telegram(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()

    # 텍스트 메시지가 아닌 업데이트(사진, 멤버 변경 등)는 무시
    message = data.get("message")
    if not message or "text" not in message:
        return {"ok": True}

    # 생성은 백그라운드로 넘기고 텔레그램에는 즉시 200 응답
    # (오래 끌면 텔레그램이 같은 업데이트를 재전송 → 중복/폭주 발생)
    background_tasks.add_task(process_message, message["text"], message["chat"]["id"])
    return {"ok": True}


@app.on_event("shutdown")
async def shutdown():
    await client.aclose()
