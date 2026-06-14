import os
import json
import logging
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta

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

# 허용된 chat_id 목록 (콤마 구분). 비어 있으면 전체 허용.
ALLOWED_CHAT_IDS = {
    int(x) for x in os.environ.get("ALLOWED_CHAT_IDS", "").replace(" ", "").split(",") if x
}

HELP_TEXT = (
    "🤖 사용 방법\n\n"
    "• 그냥 질문하세요 — 필요하면 봇이 알아서 웹 검색·뉴스를 찾아 답해요.\n"
    "  (최근 10턴까지 대화 맥락을 기억합니다.)\n\n"
    "강제로 쓰고 싶을 때 쓰는 명령어:\n"
    "• /news, /news <키워드> — 뉴스 요약 (예: /news AI)\n"
    "• /search <질문> — 웹 검색 후 답변 (예: /search 오늘 환율)\n"
    "• /reset — 대화 기록 초기화\n"
    "• /help — 이 도움말 보기"
)

# 대화 기록: chat_id -> 최근 메시지들 (user/assistant 합쳐서 최대 MAX_MESSAGES 개)
# 10턴 = user 10 + assistant 10 = 20개
MAX_TURNS = 10
MAX_MESSAGES = MAX_TURNS * 2
histories: dict[int, deque] = defaultdict(lambda: deque(maxlen=MAX_MESSAGES))

# LLM 추론은 오래 걸릴 수 있으므로 읽기 타임아웃 없음(연결만 10초 제한)
client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0))


# 한국 표준시 (DST 없음 → UTC+9 고정)
KST = timezone(timedelta(hours=9))


def current_kst() -> str:
    now = datetime.now(KST)
    weekday = "월화수목금토일"[now.weekday()]
    return now.strftime(f"%Y년 %m월 %d일 ({weekday}) %H:%M")


def sys_msg() -> dict:
    """매 요청마다 현재 한국 시각을 모델에 주입해 '오늘/어제/지금'을 정확히 해석시킨다."""
    return {
        "role": "system",
        "content": (
            f"너는 한국어로 답하는 텔레그램 봇 비서야. 현재 한국 시각은 {current_kst()}야. "
            "'오늘'·'어제'·'지금' 같은 표현은 반드시 이 시각을 기준으로 해석해라."
        ),
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


async def ollama_call(messages: list[dict], tools: list | None = None) -> dict:
    """Ollama /api/chat 호출 후 message 객체(dict) 반환. tools 주면 도구 사용 허용."""
    payload = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        # qwen3 등 하이브리드 모델의 추론(thinking) 모드를 꺼 응답 속도 향상.
        # thinking 없는 모델(gemma3, exaone3.5 등)에선 무시되므로 안전.
        "think": False,
        # 모델을 메모리에 유지해 매 요청마다 재로딩하지 않도록 함
        "keep_alive": "30m",
    }
    if tools:
        payload["tools"] = tools
    resp = await client.post(OLLAMA_URL, json=payload)
    resp.raise_for_status()
    return resp.json()["message"]


async def ollama_chat(messages: list[dict]) -> str:
    """도구 없이 단순 답변 텍스트만 필요할 때(/news, /search) 사용."""
    msg = await ollama_call(messages)
    return msg.get("content", "")


# ── 에이전트: 모델이 필요할 때 스스로 호출하는 도구들 ──────────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "최신 정보나 사실 확인이 필요할 때 웹을 검색한다. "
                "시세·환율·날씨·최근 사건 등 학습 시점 이후의 정보에 사용."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "검색어"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_news",
            "description": "최신 뉴스 헤드라인을 가져온다. 키워드를 주면 관련 뉴스, 없으면 주요 뉴스.",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "뉴스 키워드 (선택)"},
                },
            },
        },
    },
]

MAX_TOOL_ROUNDS = 3  # 무한 루프 방지: 도구 호출 왕복 횟수 제한


async def exec_tool(name: str, args: dict, chat_id: int) -> str:
    """도구 이름에 맞춰 실제 함수 실행 후 결과 텍스트 반환."""
    if name == "web_search":
        query = (args.get("query") or "").strip()
        await send_message(chat_id, f"🔎 '{query}' 검색 중...")
        data = await search.search(client, query)
        context = search.to_context(data)
        return context or "검색 결과가 없습니다."
    if name == "get_news":
        keyword = (args.get("keyword") or "").strip() or None
        await send_message(chat_id, "📰 뉴스 수집 중...")
        headlines = await news.fetch_headlines(client, keyword)
        if not headlines:
            return "뉴스를 가져오지 못했습니다."
        return "\n".join(f"[{h['label']}] {h['title']}" for h in headlines)
    return f"알 수 없는 도구: {name}"


async def run_agent(messages: list[dict], chat_id: int) -> str:
    """모델이 도구를 호출하면 실행해 결과를 돌려주는 ReAct 루프. 최종 답변 텍스트 반환."""
    msgs = list(messages)
    for _ in range(MAX_TOOL_ROUNDS):
        msg = await ollama_call(msgs, tools=TOOLS)
        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            return msg.get("content", "")

        msgs.append(msg)  # 도구 호출을 요청한 assistant 메시지
        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            args = fn.get("arguments") or {}
            if isinstance(args, str):  # 일부 모델은 arguments 를 문자열로 반환
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            result = await exec_tool(name, args, chat_id)
            msgs.append({"role": "tool", "content": result})

    # 도구 왕복 한도 소진 — 도구 없이 마지막으로 답을 강제
    final = await ollama_call(msgs)
    return final.get("content", "") or "답변을 생성하지 못했어요."


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
        # 에이전트 루프: 모델이 필요하다고 판단하면 web_search/get_news 를 스스로 호출
        # 시스템 메시지(현재 시각)는 매 턴 새로 앞에 붙이고 기록엔 저장하지 않음
        answer = await run_agent([sys_msg()] + list(history), chat_id)
        # 최종 답변만 기록에 남김(도구 왕복 메시지는 저장하지 않아 윈도우를 아낌)
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
        summary = await ollama_chat([sys_msg(), {"role": "user", "content": prompt}])
    except Exception:  # noqa: BLE001
        logger.exception("뉴스 요약 실패")
        summary = "⚠️ 뉴스 요약 중 오류가 발생했어요."

    await send_message(chat_id, summary)


async def process_search(chat_id: int, query: str):
    """/search: 웹 검색 → 결과를 근거로 Ollama 답변 → 전송. 대화 기록과는 분리."""
    await send_message(chat_id, f"🔎 '{query}' 검색 중이에요... 잠시만요.")

    data = await search.search(client, query)
    if not data["results"] and not data["answer"]:
        await send_message(chat_id, "검색 결과를 가져오지 못했어요. 잠시 후 다시 시도해 주세요.")
        return

    prompt = search.build_prompt(query, data)
    try:
        answer = await ollama_chat([sys_msg(), {"role": "user", "content": prompt}])
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

    # allowlist 가 설정돼 있으면 허용된 chat_id 외에는 무시
    if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
        logger.info("허용되지 않은 chat_id 무시: %s", chat_id)
        return {"ok": True}

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
