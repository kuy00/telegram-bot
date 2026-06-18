import os
import json
import logging
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta

import httpx
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException

import news
import search
import status
import aircon

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

# 웹훅 비밀 토큰. 설정하면 setWebhook 의 secret_token 과 같은 값을 넣어야 하며,
# 텔레그램이 매 요청에 붙이는 X-Telegram-Bot-Api-Secret-Token 헤더와 비교해
# 일치하지 않는 요청(=텔레그램이 보낸 게 아닌 직접 호출)은 거절한다. 비면 검증 안 함.
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

HELP_TEXT = (
    "🤖 사용 방법\n\n"
    "• 그냥 질문하세요 — 필요하면 봇이 알아서 웹 검색·뉴스를 찾아 답해요.\n"
    "  (최근 10턴까지 대화 맥락을 기억합니다.)\n\n"
    "강제로 쓰고 싶을 때 쓰는 명령어:\n"
    "• /news, /news <키워드> — 뉴스 요약 (예: /news AI)\n"
    "• /search <질문> — 웹 검색 후 답변 (예: /search 오늘 환율)\n"
    "• /status — 서버(라즈베리파이) 상태 확인 (온도·전원·CPU·메모리·디스크)\n"
    "• /ac on <모드> <온도> | /ac off | /ac list — 에어컨 제어 (예: /ac on 냉방 25)\n"
    "• /reset — 대화 기록 초기화\n"
    "• /help — 이 도움말 보기"
)

AC_HELP = (
    "❄️ 에어컨 제어\n\n"
    "• /ac on <모드> <온도> — 켜기 (예: /ac on 냉방 25)\n"
    "• /ac off — 끄기\n"
    "• /ac list — 사용 가능한 설정 목록\n"
    "• /ac <라벨> — 라벨로 직접 송신 (예: /ac 냉방_25_on)\n\n"
    "그냥 '에어컨 켜줘'처럼 말해도 봇이 알아서 제어해요."
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
    """매 요청마다 현재 시각 + 도구 사용 지침을 주입한다.
    핵심: 모델이 낡은 학습 지식으로 최신 사실을 단정하지 않고 검색에 의존하게 만든다."""
    return {
        "role": "system",
        "content": (
            f"너는 한국어로 답하는 텔레그램 봇 비서야. 현재 한국 시각은 {current_kst()}야. "
            "'오늘'·'어제'·'지금' 같은 표현은 반드시 이 시각을 기준으로 해석해라.\n"
            "너의 학습 지식은 과거 시점에 멈춰 있어 최신 사건을 모른다. "
            "날짜·뉴스·스포츠 경기 결과·시세·날씨처럼 시간에 따라 변하는 정보는, "
            "네 기억으로 추측하거나 '존재하지 않는다/아직 시작되지 않았다'고 단정하지 마라. "
            "그런 질문은 반드시 web_search 도구로 먼저 확인하고, "
            "제공된 검색 결과에 있는 사실에만 근거해서 답해라."
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
    {
        "type": "function",
        "function": {
            "name": "get_status",
            "description": (
                "이 봇이 돌아가는 서버(라즈베리파이)의 현재 상태를 확인한다. "
                "CPU 온도·사용률·부하·메모리·디스크·전원(저전압/스로틀)·가동시간·Ollama 상태를 반환. "
                "'서버 괜찮아?', '온도 몇 도야?', '전원 괜찮아?', '메모리 얼마나 써?' 같은 질문에 사용."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_aircon_configs",
            "description": (
                "에어컨에서 사용 가능한 설정(모드·온도·전원 조합) 목록을 가져온다. "
                "어떤 모드나 온도를 쓸 수 있는지 모를 때, 에어컨을 켜기 전에 확인용으로 사용."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "control_aircon",
            "description": (
                "에어컨을 켜거나 끄거나 모드·온도를 바꾼다. '에어컨 켜줘', '26도로 해줘', "
                "'에어컨 꺼줘' 같은 요청에 사용. "
                "켤 때는 mode·temp·power='on' 을 모두 지정한다(온도를 빼면 임의 온도가 잡힘). "
                "끌 때는 power='off' 만 주면 된다(온도 불필요). "
                "사용 가능한 mode·온도 값을 모르면 먼저 list_aircon_configs 로 확인한다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "description": "운전 모드 (예: 냉방, 난방)"},
                    "temp": {"type": "integer", "description": "설정 온도(℃). 켤 때 필요"},
                    "power": {
                        "type": "string",
                        "enum": ["on", "off"],
                        "description": "전원 on/off",
                    },
                    "label": {
                        "type": "string",
                        "description": "'모드_온도_전원' 형식 라벨로 직접 지정할 때 (예: 냉방_25_on)",
                    },
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
    if name == "get_status":
        await send_message(chat_id, "🖥 서버 상태 확인 중...")
        return await status.report(client)
    if name == "list_aircon_configs":
        await send_message(chat_id, "❄️ 에어컨 설정 목록 확인 중...")
        try:
            configs = await aircon.list_configs(client)
        except Exception:  # noqa: BLE001
            logger.exception("에어컨 목록 조회 실패")
            return "에어컨 서버에 연결하지 못했습니다."
        if not configs:
            return "사용 가능한 에어컨 설정이 없습니다."
        return "사용 가능한 설정: " + ", ".join(c["label"] for c in configs)
    if name == "control_aircon":
        mode = (args.get("mode") or "").strip() or None
        power = (args.get("power") or "").strip().lower() or None
        label = (args.get("label") or "").strip() or None
        temp = args.get("temp")
        if isinstance(temp, str):  # 일부 모델은 숫자를 문자열로 반환
            temp = int(temp) if temp.strip().lstrip("-").isdigit() else None
        await send_message(chat_id, "❄️ 에어컨 제어 중...")
        try:
            result = await aircon.send(
                client, mode=mode, temp=temp, power=power, label=label
            )
        except ValueError as e:  # 서버가 준 사용자용 에러 메시지
            return f"에어컨 제어 실패: {e}"
        except Exception:  # noqa: BLE001
            logger.exception("에어컨 제어 오류")
            return "에어컨 제어 중 오류가 발생했습니다."
        return f"에어컨 송신 완료: {result.get('label')}"
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


async def process_status(chat_id: int):
    """/status: 서버 상태를 그대로 전송. LLM을 거치지 않아 빠르고 정확하다."""
    try:
        text = await status.report(client)
    except Exception:  # noqa: BLE001
        logger.exception("서버 상태 수집 실패")
        text = "⚠️ 서버 상태를 읽는 중 오류가 발생했어요."
    await send_message(chat_id, text)


async def _ac_send(chat_id: int, **kwargs):
    """에어컨 송신 공통부: 진행 안내 → 송신 → 결과/오류 전송."""
    await send_message(chat_id, "❄️ 에어컨 제어 중...")
    try:
        result = await aircon.send(client, **kwargs)
    except ValueError as e:  # 서버가 준 사용자용 에러 메시지
        await send_message(chat_id, f"⚠️ 에어컨 제어 실패: {e}")
        return
    except Exception:  # noqa: BLE001
        logger.exception("에어컨 제어 오류")
        await send_message(chat_id, "⚠️ 에어컨 제어 중 오류가 발생했어요.")
        return
    await send_message(chat_id, f"✅ 송신 완료: {result.get('label')}")


async def process_ac(chat_id: int, arg: str):
    """/ac: 에어컨 IR 제어(수동 강제 경로). LLM 을 거치지 않아 빠르고 확실하다."""
    arg = arg.strip()
    parts = arg.split()
    sub = parts[0].lower() if parts else ""

    if not arg or sub in ("help", "도움말"):
        await send_message(chat_id, AC_HELP)
        return
    if sub in ("list", "목록"):
        try:
            configs = await aircon.list_configs(client)
        except Exception:  # noqa: BLE001
            logger.exception("에어컨 목록 조회 실패")
            await send_message(chat_id, "⚠️ 에어컨 서버에 연결하지 못했어요.")
            return
        if not configs:
            await send_message(chat_id, "사용 가능한 에어컨 설정이 없어요.")
            return
        lines = "\n".join(f"• {c['label']}" for c in configs)
        await send_message(chat_id, f"❄️ 사용 가능한 에어컨 설정:\n{lines}")
        return
    if sub in ("off", "끄기", "꺼"):
        await _ac_send(chat_id, power="off")
        return
    if sub in ("on", "켜기", "켜"):
        # /ac on <모드> <온도>
        if len(parts) < 3 or not parts[2].isdigit():
            await send_message(chat_id, "켤 땐 모드와 온도를 함께 주세요. 예) /ac on 냉방 25")
            return
        await _ac_send(chat_id, mode=parts[1], temp=int(parts[2]), power="on")
        return
    # 그 외 한 단어는 라벨 직접 지정으로 간주 (예: /ac 냉방_25_on)
    await _ac_send(chat_id, label=arg)


@app.post("/telegram")
async def telegram(request: Request, background_tasks: BackgroundTasks):
    # 비밀 토큰 검증: 설정돼 있으면 텔레그램이 붙인 헤더와 일치해야만 처리.
    # (텔레그램을 거치지 않은 임의의 직접 호출 차단)
    if WEBHOOK_SECRET:
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
            logger.warning("웹훅 비밀 토큰 불일치 — 요청 거절")
            raise HTTPException(status_code=403, detail="forbidden")

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

    # /status — 서버(라즈베리파이) 상태 확인. LLM 없이 즉시 응답.
    if text == "/status":
        background_tasks.add_task(process_status, chat_id)
        return {"ok": True}

    # /ac — 에어컨 제어(수동). LLM 없이 ir_server 직접 호출.
    if text == "/ac" or text.startswith("/ac "):
        arg = text[len("/ac "):] if text.startswith("/ac ") else ""
        background_tasks.add_task(process_ac, chat_id, arg)
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
