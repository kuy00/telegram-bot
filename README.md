# Telegram + Ollama 봇

FastAPI 웹훅으로 텔레그램 메시지를 받아 로컬 Ollama LLM 으로 답변하는 봇.

## 구성
- `main.py` — FastAPI 웹훅 서버
- `ollama` 컨테이너 — LLM 추론 엔진
- `bot` 컨테이너 — 웹훅 서버 (위 main.py)

## 1. 준비

```bash
cp .env.example .env
# .env 를 열어 TELEGRAM_TOKEN 을 @BotFather 에서 받은 값으로 채우기
```

## 2. 실행

```bash
docker compose up -d --build
```

처음 한 번은 모델을 받아야 한다 (Ollama 는 자동으로 받지 않음):

```bash
docker compose exec ollama ollama pull qwen3:1.7b
```

상태 확인:

```bash
curl http://localhost:8000/health   # {"status":"ok"}
```

## 3. 텔레그램 웹훅 등록

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
