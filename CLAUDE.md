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
- **`status.py`** — 서버(라즈베리파이) 상태 점검. 추가 의존성 없이 `/proc`·`/sys` 직접 읽기.
  온도·CPU·부하·메모리·디스크·전원(저전압/스로틀)·가동시간·Ollama 상태 수집.
  `report(client)` → 텔레그램용 텍스트. 전원은 파이 펌웨어 throttle 비트마스크
  (`/sys/.../get_throttled`, vcgencmd 와 동일 값)로 저전압·스로틀 이력까지 잡는다.
  **컨테이너 안에서도 `/proc`·`/sys`는 호스트 커널 값을 반영**하므로 온도·메모리·부하·CPU는
  파이 호스트 기준으로 정확하다. **디스크만 컨테이너 오버레이FS 기준** — 정확히 보려면
  `docker-compose.yml`에서 호스트 루트를 ro 마운트하고 `DISK_PATH`로 가리킨다.
- **`search.py`** — 웹 검색. `TAVILY_API_KEY` 있으면 Tavily(본문·요약 제공,
  날씨·스코어 등 구체 사실에 강함), 없으면 DuckDuckGo(`ddgs`, 스니펫만)로 폴백.
  `search(client, query)` → `{"answer", "results":[{title,url,content}]}` 반환.
  Tavily 호출 실패 시에도 ddgs로 폴백한다. `MAX_RESULTS`(기본 5)개.
- **`aircon.py`** — 에어컨 IR 제어 서버(`ir_server`, LAN 전용 JSON API) 연동.
  `list_configs(client)` → 송신 가능한 설정 목록, `send(client, mode/temp/power/label)`
  → IR 송신. 켤 땐 mode·temp·power 전체 지정 권장(온도 생략 시 임의 온도), 끌 땐
  `power="off"`만으로 충분. `IR_HTTP_TOKEN` 있으면 Bearer 인증. 서버가 준 에러는
  `ValueError(메시지)`로 올려 사용자에게 그대로 보여준다.
- **`scheduler.py`** — 지연 예약("30분 뒤에 에어컨 꺼줘"). 추가 의존성 없이
  **인메모리 asyncio 태스크**(`asyncio.create_task`+`sleep`)로 구현. `parse(text)` →
  자연어에서 `(지연_초, 실행할_내용)` 추출(상대 "N분 뒤"·절대 "3시에" 지원, 못 뽑으면
  `None`), `schedule(chat_id, delay, action, callback)` → 예약 등록, `list_jobs`/
  `cancel`/`cancel_all` → 조회·취소. 예약 시각이 되면 저장한 발화를 **`route_text` 로
  다시 라우팅**해 실행한다(예약된 "에어컨 꺼줘"도 fast-path 직행).
  **한계: 인메모리라 봇 재시작 시 예약이 사라진다**(영구화하려면 SQLite 등 필요).

## 핵심 설계 결정 (수정 시 주의)

- **웹훅은 즉시 200을 응답하고 실제 작업은 `BackgroundTasks`로 처리한다.**
  Ollama 추론이 느려서 응답을 끌면 텔레그램이 같은 업데이트를 **재전송**해
  중복/폭주가 난다. 핸들러 안에서 LLM을 직접 await 하지 말 것.
- **`httpx.AsyncClient`의 read 타임아웃은 `None`**(connect만 10초). 저사양에서
  LLM 생성이 수십 초 걸려도 끊기지 않게 하기 위함.
- **일반 대화는 에이전트(tool calling) 루프(`run_agent`)로 처리한다.** 모델이
  필요하다고 판단하면 `web_search`/`get_news`/`get_status`/`list_aircon_configs`/
  `control_aircon`/`schedule_action` 도구를 스스로 호출하고, 봇이 실행해 결과를 돌려준 뒤 최종 답을
  낸다(ReAct). 무한 루프 방지로 `MAX_TOOL_ROUNDS`(3)회 제한.
  도구 결정 품질은 모델 크기에 의존적 — qwen3:4b 권장, 1.7b는 판단이 들쭉날쭉.
  `/news`·`/search` 수동 명령은 모델이 오판할 때의 강제 경로로 그대로 유지한다.
- **명백한 에어컨 켜기/끄기 자연어는 LLM을 건너뛴다(`match_aircon`).** 파이에서
  추론이 수십 초~수 분 걸려 "에어컨 꺼" 한마디도 느리기 때문. 웹훅에서 에이전트로
  넘기기 직전, `에어컨`+`꺼/끄/off`면 바로 `power=off`로 ir_server 직행(즉시 응답),
  켜기는 `냉방/난방`+온도가 분명할 때만 직행한다. 애매하면 `None` → 에이전트 경로.
- **모든 발화 라우팅은 `route_text(text, chat_id)` 한 곳으로 모았다.** 웹훅은
  검증(비밀 토큰·allowlist) 후 `route_text` 를 백그라운드 작업으로 넘길 뿐이다.
  예약 실행(`run_scheduled`)도 같은 `route_text` 를 재사용하므로 명령·자연어·에어컨
  fast-path 가 예약된 작업에도 동일하게 적용된다.
- **지연 예약도 자연어 fast-path 로 먼저 가로챈다(`scheduler.parse`).** "30분 뒤에
  에어컨 꺼줘"에서 `match_aircon` 이 먼저 걸리면 지연을 무시하고 **즉시** 꺼버리므로,
  `route_text` 는 반드시 `scheduler.parse` → (예약) 를 `match_aircon` **앞에서** 검사한다.
  파싱은 상대("N분/시간 뒤")·절대("3시에")를 지원하고 못 알아들으면 일반 대화로 흘려보낸다.
  에이전트에도 `schedule_action` 도구가 있어 fast-path 가 놓친 표현은 LLM 이 예약할 수 있다.
- **일반 대화는 무상태(stateless)다.** 이전 대화를 저장하지도, 컨텍스트로 함께
  보내지도 않는다. 매 요청은 `[sys_msg(), 이번 발화]`만으로 독립 처리된다(봇은
  앞 대화를 기억하지 않음). 저사양에서 프롬프트(prefill) 부담을 줄이려는 선택.
  과거엔 `deque(maxlen=20)` 슬라이딩 윈도우로 최근 10턴을 기억했으나 제거했다.
- **`ollama_chat`에서 `think: False`로 추론 모드를 끈다.** qwen3 같은
  하이브리드 모델의 thinking 토큰 생성이 저사양에서 큰 지연 원인이라 끈다.
  thinking이 없는 모델(gemma3, exaone3.5 등)에선 이 옵션이 무시되므로 안전.
  `keep_alive: "30m"`로 모델을 메모리에 유지해 재로딩 지연도 줄인다.
- **파이 추론 속도 튜닝.** CPU 추론은 답변 생성보다 **프롬프트 처리(prefill)**가
  병목이고, 에이전트는 매 라운드 `시스템 프롬프트+대화기록+도구 6개 스키마`를 다시
  처리한다. 그래서 ① 시스템 프롬프트·도구 description 을 짧게 유지(매 라운드 재처리분
  절감) ② `options`로 `num_ctx`(KV 캐시 과대 할당 방지)·`num_predict`(생성 길이 캡)·
  `num_thread`를 건다(`OLLAMA_NUM_*` 환경변수로 조정). 도구 description 을 더 줄일 땐
  트리거 문구를 남겨야 모델의 도구 선택 품질이 유지된다.
  **호스트 Ollama 쪽 추가 가속(코드 밖):** `ollama serve` 를 띄울 때
  `OLLAMA_FLASH_ATTENTION=1`(어텐션 가속), `OLLAMA_KV_CACHE_TYPE=q8_0`(KV 캐시 양자화,
  flash attention 필요)을 주면 더 빨라진다. 발열 스로틀이 걸리면 클럭이 떨어져
  급격히 느려지므로 `/status`의 throttle 이력부터 확인할 것.
- **Ollama는 컨테이너가 아니라 호스트에서 직접 구동.** 봇 컨테이너는
  `host.docker.internal`(또는 `network_mode: host` 시 `127.0.0.1`)로 접속한다.
  호스트 Ollama는 `OLLAMA_HOST=0.0.0.0 ollama serve`로 띄워야 컨테이너에서 보인다.

## 명령어

| 입력 | 동작 | 처리 함수 |
|------|------|-----------|
| (일반 텍스트) | 에이전트 대화(필요시 도구 자동 호출), 무상태(대화 기억 없음) | `process_message`→`run_agent` |
| `/news [키워드]` | 뉴스 수집→요약 (수동) | `process_news` |
| `/search <질문>`, `/ask <질문>` | 웹 검색→답변 | `process_search` |
| `/status` | 서버 상태(온도·전원·CPU·메모리·디스크) | `process_status` |
| `/ac on <모드> <온도>`, `/ac off`, `/ac list`, `/ac <라벨>` | 에어컨 IR 제어 (수동) | `process_ac` |
| `/remind <시간> <할일>`, `/remind list`, `/remind cancel [번호]` | 지연 예약 (수동). 자연어 "30분 뒤 ~"도 동일 | `process_remind`→`scheduler` |
| `/help`, `/start` | 도움말 | (인라인, `HELP_TEXT`) |

## 환경변수

| 이름 | 필수 | 기본값 | 설명 |
|------|------|--------|------|
| `TELEGRAM_TOKEN` | ✅ | — | @BotFather 봇 토큰. `.env`로 주입 |
| `OLLAMA_URL` | | `http://127.0.0.1:11434/api/chat` | Ollama chat 엔드포인트 |
| `OLLAMA_MODEL` | | `qwen3:4b` | 사용할 모델 |
| `OLLAMA_NUM_CTX` | | `4096` | 컨텍스트 길이. 모델 기본이 과하면 KV 캐시 연산·메모리가 커져 느려짐 → 명시 고정 |
| `OLLAMA_NUM_PREDICT` | | `1024` | 생성 토큰 상한. 답이 장황하게 늘어지는 걸 막아 생성 시간 캡 |
| `OLLAMA_NUM_THREAD` | | `0` | 추론 스레드 수. 0=Ollama 자동(보통 물리 코어 수) |
| `ALLOWED_CHAT_IDS` | | (빈 값) | 허용할 chat_id 콤마 목록. 비면 전체 허용 |
| `WEBHOOK_SECRET` | | (빈 값) | 웹훅 비밀 토큰. `setWebhook`의 `secret_token`과 동일 값. 설정 시 `X-Telegram-Bot-Api-Secret-Token` 헤더 불일치 요청을 403 거절. 비면 검증 안 함 |
| `TAVILY_API_KEY` | | (빈 값) | Tavily 검색 키. 있으면 Tavily, 없으면 DuckDuckGo |
| `DISK_PATH` | | `/` | `/status` 디스크 측정 경로. 호스트 루트 마운트 시 그 경로로 지정 |
| `IR_SERVER_URL` | | `http://127.0.0.1:8000` | 에어컨 IR 제어 서버(ir_server) 주소. 봇이 같은 파이면 기본값 |
| `IR_HTTP_TOKEN` | | (빈 값) | ir_server 인증 켰을 때 Bearer 토큰(서버의 동일 값). 비면 무인증 |

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
- **새 환경변수는 `.env`에만 적으면 된다.** `docker-compose.yml`이 `env_file: .env`로
  통째로 주입하므로 compose 를 손댈 필요 없다. (과거엔 `environment:`에 변수를 하나씩
  나열해서, `WEBHOOK_SECRET`을 거기 빠뜨려 `.env`에 적고도 검증이 안 먹힌 적 있음 →
  env_file 방식으로 바꿔 해결.) 단 `OLLAMA_URL`·`DISK_PATH`처럼 `.env`에 없을 때의
  기본값은 compose 의 `environment:`에 남겨뒀고, 이건 env_file 보다 우선한다.
  새 변수 추가 시 챙길 곳: ① `main.py`의 `os.environ` 읽기 ② `.env.example`
  ③ 이 문서의 환경변수 표.
- **`/news`, `/search`가 비는 경우** — RSS/DuckDuckGo가 일시 차단되거나 결과가
  없을 수 있다. 코드는 빈 결과 시 안내 메시지를 보내고 끝낸다(예외로 죽지 않음).
- **메시지를 보내도 봇이 무반응** — 봇 컨테이너 실행 여부와 텔레그램 `setWebhook`
  등록 여부부터 확인. LLM이 아니라 전달 경로 문제인 경우가 많다.
- **봇이 이전 대화를 기억 못 함** — 정상(의도된 동작). 일반 대화는 무상태라
  앞 메시지를 저장·전달하지 않는다. "방금 그거 다시"처럼 맥락에 의존하는 요청은
  한 메시지에 필요한 정보를 다 담아야 한다.

## 향후 개선 후보 (아직 미구현)

- (필요 시) 대화 맥락 복원 — 무상태로 전환했으므로, 다시 도입하려면 영구
  저장(SQLite)·오래된 `chat_id` 정리·프롬프트 부담을 함께 고려해야 함
- `/news` 정기 자동 발송(cron)
- 날씨 전용 도구(wttr.in 등) — 실시간 날씨는 검색 스니펫으로 약함
