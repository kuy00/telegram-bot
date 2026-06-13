# Telegram + Ollama 봇

FastAPI 웹훅으로 텔레그램 메시지를 받아 **호스트에 설치된 Ollama** LLM 으로 답변하는 봇.

## 구성
- `main.py` — FastAPI 웹훅 서버 + 명령 라우팅
- `news.py` — Google 뉴스 RSS 수집
- `search.py` — DuckDuckGo 웹 검색
- `bot` 컨테이너 — 웹훅 서버. Ollama 는 호스트에서 직접 구동.

## 명령어
- 일반 메시지 — 대화. 최근 10턴 맥락을 기억함
- `/news` — 국내+해외 주요 뉴스 요약·분석
- `/news <키워드>` — 키워드 관련 뉴스 요약 (예: `/news AI`)
- `/search <질문>` — 웹 검색 후 그 결과를 근거로 답변 (예: `/search 오늘 환율`)
- `/reset` — 대화 기록 초기화

> 작은 모델(qwen3:1.7b)은 모르는 것도 지어낼 수 있어, 최신·사실 확인이
> 필요한 질문은 그냥 대화 대신 `/search` 를 쓰는 걸 권장.

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
> 모델은 한 번만 받아두면 된다: `ollama pull qwen3:1.7b`

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

이제 봇에게 메시지를 보내면 Ollama 가 생성한 답변이 돌아온다.

## 로그 / 종료

```bash
docker compose logs -f bot
docker compose down
```
