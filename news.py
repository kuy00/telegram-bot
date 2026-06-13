"""Google 뉴스 RSS 에서 헤드라인을 수집한다 (API 키 불필요)."""
import logging
from urllib.parse import quote

import feedparser
import httpx

logger = logging.getLogger("telegram-bot.news")

# 피드당 가져올 기사 수 (라즈베리파이 + 작은 모델 부담을 고려해 작게)
PER_FEED = 5

# 전체 주요 뉴스 (키워드 없을 때): 국내 + 해외 톱 헤드라인
TOP_FEEDS = [
    ("국내", "https://news.google.com/rss?hl=ko&gl=KR&ceid=KR:ko"),
    ("해외", "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"),
]


def _search_feeds(keyword: str):
    q = quote(keyword)
    return [
        ("국내", f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"),
        ("해외", f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"),
    ]


async def fetch_headlines(client: httpx.AsyncClient, keyword: str | None = None) -> list[dict]:
    """제목 + 출처 라벨 리스트를 반환. 실패한 피드는 건너뛴다."""
    feeds = _search_feeds(keyword) if keyword else TOP_FEEDS
    headlines: list[dict] = []

    for label, url in feeds:
        try:
            resp = await client.get(url, timeout=15.0, follow_redirects=True)
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
        except Exception:  # noqa: BLE001
            logger.exception("RSS 수집 실패: %s", url)
            continue

        for entry in parsed.entries[:PER_FEED]:
            title = getattr(entry, "title", "").strip()
            if title:
                headlines.append({"label": label, "title": title})

    return headlines


def build_prompt(headlines: list[dict], keyword: str | None) -> str:
    """수집한 헤드라인을 Ollama 에 넘길 요약/분석 지시문으로 만든다."""
    lines = [f"[{h['label']}] {h['title']}" for h in headlines]
    topic = f"'{keyword}' 관련 " if keyword else ""
    return (
        f"다음은 방금 수집한 {topic}최신 뉴스 헤드라인이야.\n"
        "한국어로, 아래 형식에 맞춰 정리해줘:\n"
        "1) 핵심 내용을 3~5개의 불릿으로 요약\n"
        "2) 전체적으로 어떤 흐름·이슈인지 2~3문장 분석\n"
        "추측은 피하고 주어진 헤드라인 범위 안에서만 작성해.\n\n"
        + "\n".join(lines)
    )
