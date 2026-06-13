# CLAUDE.md

이 파일은 이 저장소에서 작업하는 Claude Code(및 다른 기여자)를 위한 안내서다.

## 프로젝트 개요

텔레그램 메시지를 웹훅으로 받아 **로컬 Ollama LLM**(기본 `qwen3:4b`)으로
답하는 봇. 일반 대화 외에 뉴스 요약(`/news`)과 웹 검색 기반 답변(`/search`)을 지원한다.
라즈베리파이 같은 저사양 환경에서 도는 것을 전제로 가볍게 설계됐다.

핵심 원칙: **LLM은 인터넷에 접근하지 못한다.** 최신 정보가 필요한 기능
(`/news`, `/search`)은 코드가 외부에서 데이터를 가져와 프롬프트에 넣어주고,
모델은 그걸 근거로 요약/답변만 한다 (RAG 패턴).

## 아키텍처

```
텔레그램 ──웹훅(POST /telegram)──▶ FastAPI(main.py)
                                      │
                  ┌───────────────────┼───────────────────┐
                  ▼                   ▼                   ▼
          (일반 대화)           news.py             search.py
       대화기록+Ollama       Google뉴스 RSS      DuckDuckGo 검색
                  └───────────────────┼───────────────────┘
                                      ▼
                              Ollama /api/chat ──▶ 답변 ──▶ 텔레그램 sendMessage
```

- **`main.py`** — FastAPI 앱. 웹훅 수신, 명령 라우팅, Ollama 호출(`ollama_chat`),
  텔레그램 전송(`send_message`), 대화 기록 관리.
- **`news.py`** — Google 뉴스 RSS 수집(`fetch_headlines`)과 요약 프롬프트 생성(`build_prompt`).
  API 키 불필요. 국내(ko)+해외(en) 피드, 피드당 `PER_FEED`(기본 5)개.
- **`search.py`** — DuckDuckGo 웹 검색(`ddgs`). API 키 불필요. `MAX_RESULTS`(기본 5)개.

## 핵심 설계 결정 (수정 시 주의)

- **웹훅은 즉시 200을 응답하고 실제 작업은 `BackgroundTasks`로 처리한다.**
  Ollama 추론이 느려서 응답을 끌면 텔레그램이 같은 업데이트를 **재전송**해
  중복/폭주가 난다. 핸들러 안에서 LLM을 직접 await 하지 말 것.
- **`httpx.AsyncClient`의 read 타임아웃은 `None`**(connect만 10초). 저사양에서
  LLM 생성이 수십 초 걸려도 끊기지 않게 하기 위함.
- **대화 기록은 `chat_id`별 메모리(`deque(maxlen=20)`)**. 최근 10턴 슬라이딩 윈도우.
  재시작하면 사라짐(영구 저장 아님). `/news`·`/search`는 1회성이라 기록에 넣지 않는다.
- **`ollama_chat`에서 `think: False`로 추론 모드를 끈다.** qwen3 같은
  하이브리드 모델의 thinking 토큰 생성이 저사양에서 큰 지연 원인이라 끈다.
  thinking이 없는 모델(gemma3, exaone3.5 등)에선 이 옵션이 무시되므로 안전.
  `keep_alive: "30m"`로 모델을 메모리에 유지해 재로딩 지연도 줄인다.
- **Ollama는 컨테이너가 아니라 호스트에서 직접 구동.** 봇 컨테이너는
  `host.docker.internal`(또는 `network_mode: host` 시 `127.0.0.1`)로 접속한다.
  호스트 Ollama는 `OLLAMA_HOST=0.0.0.0 ollama serve`로 띄워야 컨테이너에서 보인다.

## 명령어

| 입력 | 동작 | 처리 함수 |
|------|------|-----------|
| (일반 텍스트) | 대화, 최근 10턴 기억 | `process_message` |
| `/news [키워드]` | 뉴스 수집→요약 | `process_news` |
| `/search <질문>`, `/ask <질문>` | 웹 검색→답변 | `process_search` |
| `/reset` | 대화 기록 초기화 | (인라인) |
| `/help`, `/start` | 도움말 | (인라인, `HELP_TEXT`) |

## 환경변수

| 이름 | 필수 | 기본값 | 설명 |
|------|------|--------|------|
| `TELEGRAM_TOKEN` | ✅ | — | @BotFather 봇 토큰. `.env`로 주입 |
| `OLLAMA_URL` | | `http://127.0.0.1:11434/api/chat` | Ollama chat 엔드포인트 |
| `OLLAMA_MODEL` | | `qwen3:4b` | 사용할 모델 |

## 개발 / 실행

```bash
# 로컬 문법 체크
python3 -m py_compile main.py news.py search.py

# 빌드 & 실행 (호스트에 Ollama가 떠 있어야 함)
cp .env.example .env          # TELEGRAM_TOKEN 입력
docker compose up -d --build
docker logs -f telegram-bot   # 'Uvicorn running on ...' 뜨면 정상

# 헬스체크
curl http://localhost:8000/health   # {"status":"ok"}
```

웹훅 등록·모델 pull 등 운영 절차는 `README.md` 참고.

## 자주 겪는 함정

- **새 `.py` 모듈을 추가하면 `Dockerfile`의 `COPY *.py .`로 들어가는지 확인.**
  과거 `main.py`만 복사해서 `ModuleNotFoundError`가 났던 이력 있음.
- **`/news`, `/search`가 비는 경우** — RSS/DuckDuckGo가 일시 차단되거나 결과가
  없을 수 있다. 코드는 빈 결과 시 안내 메시지를 보내고 끝낸다(예외로 죽지 않음).
- **메시지를 보내도 봇이 무반응** — 봇 컨테이너 실행 여부와 텔레그램 `setWebhook`
  등록 여부부터 확인. LLM이 아니라 전달 경로 문제인 경우가 많다.
- **봇 재시작 후 이전 대화를 기억 못 함** — 정상. 기록은 메모리에만 있다.

## 향후 개선 후보 (아직 미구현)

- 대화 기록 영구 저장(SQLite)과 오래된 `chat_id` 정리(메모리 누수 방지)
- 검색을 Tavily 등 LLM 친화 API로 교체(ddgs 스크래핑은 가끔 막힘)
- `/news` 정기 자동 발송(cron)
