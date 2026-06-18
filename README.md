# Telegram + Ollama 봇

FastAPI 웹훅으로 텔레그램 메시지를 받아 **호스트에 설치된 Ollama** LLM 으로 답변하는 봇.

## 구성
- `main.py` — FastAPI 웹훅 서버 + 명령 라우팅 + 에이전트 루프
- `news.py` — Google 뉴스 RSS 수집
- `search.py` — 웹 검색 (`TAVILY_API_KEY` 있으면 Tavily, 없으면 DuckDuckGo 폴백)
- `status.py` — 서버(라즈베리파이) 상태 점검 (온도·CPU·메모리·디스크·전원)
- `aircon.py` — 에어컨 IR 제어 서버(ir_server) 연동
- `bot` 컨테이너 — 웹훅 서버. Ollama 는 호스트에서 직접 구동.

## 명령어
- 일반 메시지 — 대화. 필요하면 봇이 **알아서 웹 검색·뉴스·서버상태·에어컨 도구를
  호출**해 답한다(에이전트). 최근 10턴 맥락을 기억함
- `/news`, `/news <키워드>` — 뉴스 요약·분석 (예: `/news AI`)
- `/search <질문>`, `/ask <질문>` — 웹 검색 후 그 결과를 근거로 답변 (예: `/search 오늘 환율`)
- `/status` — 서버 상태 확인 (온도·전원·CPU·메모리·디스크)
- `/ac on <모드> <온도>` · `/ac off` · `/ac list` — 에어컨 제어 (예: `/ac on 냉방 25`)
- `/reset` — 대화 기록 초기화
- `/help`, `/start` — 도움말

> 일반 대화에서도 모델이 판단해 도구를 부르지만, 오판할 때를 대비한 **강제 경로**로
> `/news`·`/search`·`/status`·`/ac` 수동 명령을 그대로 둔다.
> 환경변수·설계 상세는 `CLAUDE.md` 참고.

## 1. 준비

```bash
cp .env.example .env
# .env 를 열어 TELEGRAM_TOKEN 을 @BotFather 에서 받은 값으로 채우기
```

## 2. 호스트 Ollama 가 컨테이너에서 보이게 하기

기본 Ollama 는 `127.0.0.1` 만 바인딩해서 컨테이너에서 접근이 안 된다.
`0.0.0.0` 으로 바인딩해서 띄운다:

```bash
OLLAMA_HOST=0.0.0.0 ollama serve
```

> 앱(메뉴바) 형태로 켜둔 Ollama 라면 종료하고 위 명령으로 다시 실행.
> 모델은 한 번만 받아두면 된다: `ollama pull qwen3:4b` (기본 모델)

## 3. 봇 실행

```bash
docker compose up -d --build
```

상태 확인:

```bash
curl http://localhost:8000/health   # {"status":"ok"}
```

## 4. 텔레그램 웹훅 등록

서버가 인터넷에서 접근 가능한 HTTPS 주소를 가져야 한다.
로컬 테스트는 [ngrok](https://ngrok.com/) 등으로 터널링:

```bash
ngrok http 8000
```

받은 https 주소로 웹훅 등록:

```bash
curl "https://api.telegram.org/bot<TELEGRAM_TOKEN>/setWebhook?url=https://<your-domain>/telegram"
```

> `.env` 에 `WEBHOOK_SECRET` 을 설정했다면, 등록 시 같은 값을 `secret_token` 으로
> 함께 넣어야 한다(불일치 직접 호출을 봇이 403 거절):
> ```bash
> curl "https://api.telegram.org/bot<TELEGRAM_TOKEN>/setWebhook" \
>   -d "url=https://<your-domain>/telegram" -d "secret_token=<WEBHOOK_SECRET 와 동일 값>"
> ```

이제 봇에게 메시지를 보내면 Ollama 가 생성한 답변이 돌아온다.

## 로그 / 종료

```bash
docker compose logs -f bot
docker compose down
```
