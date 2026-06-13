import os
import logging
from collections import defaultdict, deque

import httpx
from fastapi import FastAPI, Request, BackgroundTasks

import news
import search

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("telegram-bot")

app = FastAPI()

# 환경변수 (docker-compose / .env 에서 주입)
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:4b")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

HELP_TEXT = (
    "🤖 사용 가능한 명령어\n\n"
    "• (그냥 메시지) — 대화. 최근 10턴까지 맥락을 기억해요.\n"
    "• /news — 국내+해외 주요 뉴스 요약·분석\n"
    "• /news <키워드> — 키워드 관련 뉴스 (예: /news AI)\n"
    "• /search <질문> — 웹 검색 후 답변 (예: /search 오늘 환율)\n"
    "• /reset — 대화 기록 초기화\n"
    "• /help — 이 도움말 보기\n\n"
    "ℹ️ 최신 정보나 사실 확인이 필요하면 /search 를 쓰는 걸 권장해요."
)

# 대화 기록: chat_id -> 최근 메시지들 (user/assistant 합쳐서 최대 MAX_MESSAGES 개)
# 10턴 = user 10 + assistant 10 = 20개
MAX_TURNS = 10
MAX_MESSAGES = MAX_TURNS * 2
histories: dict[int, deque] = defaultdict(lambda: deque(maxlen=MAX_MESSAGES))

# LLM 추론은 오래 걸릴 수 있으므로 읽기 타임아웃 없음(연결만 10초 제한)
client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0))


@app.get("/health")
async def health():
    return {"status": "ok"}


async def ollama_chat(messages: list[dict]) -> str:
    """Ollama /api/chat 호출 후 답변 텍스트 반환."""
    resp = await client.post(
        OLLAMA_URL,
        json={
            "model": OLLAMA_MODEL,
            "messages": messages,
            "stream": False,
            # qwen3 등 하이브리드 모델의 추론(thinking) 모드를 꺼 응답 속도 향상.
            # thinking 없는 모델(gemma3, exaone3.5 등)에선 무시되므로 안전.
            "think": False,
            # 모델을 메모리에 유지해 매 요청마다 재로딩하지 않도록 함
            "keep_alive": "30m",
        },
    )
    resp.raise_for_status()
    return resp.json()["message"]["content"]


async def send_message(chat_id: int, text: str):
    try:
        await client.post(TELEGRAM_API, json={"chat_id": chat_id, "text": text})
    except Exception:  # noqa: BLE001
        logger.exception("Telegram 전송 실패")


async def process_message(text: str, chat_id: int):
    """일반 대화: Ollama 대화 생성 + 텔레그램 전송. 백그라운드 실행."""
    history = histories[chat_id]

    # 이번 사용자 발화를 기록에 추가하고, 윈도우(최근 N턴) 전체를 모델에 전달
    history.append({"role": "user", "content": text})

    try:
        answer = await ollama_chat(list(history))
        # 모델 응답도 기록에 남겨 다음 턴의 맥락으로 사용
        history.append({"role": "assistant", "content": answer})
    except Exception:  # noqa: BLE001
        logger.exception("Ollama 호출 실패")
        # 실패한 사용자 발화는 기록에서 되돌림(반쪽짜리 맥락 방지)
        if history and history[-1]["role"] == "user":
            history.pop()
        answer = "⚠️ 답변 생성 중 오류가 발생했어요. 잠시 후 다시 시도해 주세요."

    await send_message(chat_id, answer)


async def process_news(chat_id: int, keyword: str | None):
    """/news: 뉴스 수집 → Ollama 요약/분석 → 전송. 대화 기록과는 분리."""
    await send_message(chat_id, "📰 뉴스를 수집하는 중이에요... 잠시만요.")

    headlines = await news.fetch_headlines(client, keyword)
    if not headlines:
        await send_message(chat_id, "뉴스를 가져오지 못했어요. 잠시 후 다시 시도해 주세요.")
        return

    prompt = news.build_prompt(headlines, keyword)
    try:
        # 1회성 요약 — 대화 기록에 넣지 않음
        summary = await ollama_chat([{"role": "user", "content": prompt}])
    except Exception:  # noqa: BLE001
        logger.exception("뉴스 요약 실패")
        summary = "⚠️ 뉴스 요약 중 오류가 발생했어요."

    await send_message(chat_id, summary)


async def process_search(chat_id: int, query: str):
    """/search: 웹 검색 → 결과를 근거로 Ollama 답변 → 전송. 대화 기록과는 분리."""
    await send_message(chat_id, f"🔎 '{query}' 검색 중이에요... 잠시만요.")

    results = await search.search(query)
    if not results:
        await send_message(chat_id, "검색 결과를 가져오지 못했어요. 잠시 후 다시 시도해 주세요.")
        return

    prompt = search.build_prompt(query, results)
    try:
        answer = await ollama_chat([{"role": "user", "content": prompt}])
    except Exception:  # noqa: BLE001
        logger.exception("검색 답변 생성 실패")
        answer = "⚠️ 답변 생성 중 오류가 발생했어요."

    await send_message(chat_id, answer)


@app.post("/telegram")
async def telegram(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()

    # 텍스트 메시지가 아닌 업데이트(사진, 멤버 변경 등)는 무시
    message = data.get("message")
    if not message or "text" not in message:
        return {"ok": True}

    text = message["text"].strip()
    chat_id = message["chat"]["id"]

    # /help, /start — 사용 가능한 명령어 안내
    if text in ("/help", "/start"):
        background_tasks.add_task(send_message, chat_id, HELP_TEXT)
        return {"ok": True}

    # /reset 으로 대화 기록 초기화
    if text == "/reset":
        histories.pop(chat_id, None)
        background_tasks.add_task(send_message, chat_id, "🧹 대화 기록을 초기화했어요.")
        return {"ok": True}

    # /news [키워드] — 키워드 없으면 전체 주요 뉴스
    if text == "/news" or text.startswith("/news "):
        keyword = text[len("/news "):].strip() if text.startswith("/news ") else None
        background_tasks.add_task(process_news, chat_id, keyword or None)
        return {"ok": True}

    # /search <질문> — 웹 검색 후 답변
    if text.startswith("/search ") or text.startswith("/ask "):
        query = text.split(" ", 1)[1].strip()
        if query:
            background_tasks.add_task(process_search, chat_id, query)
        else:
            background_tasks.add_task(send_message, chat_id, "검색어를 함께 보내주세요. 예) /search 오늘 환율")
        return {"ok": True}
    if text in ("/search", "/ask"):
        background_tasks.add_task(send_message, chat_id, "검색어를 함께 보내주세요. 예) /search 오늘 환율")
        return {"ok": True}

    # 일반 대화: 생성은 백그라운드로, 텔레그램에는 즉시 200 응답
    # (오래 끌면 텔레그램이 같은 업데이트를 재전송 → 중복/폭주 발생)
    background_tasks.add_task(process_message, text, chat_id)
    return {"ok": True}


@app.on_event("shutdown")
async def shutdown():
    await client.aclose()
