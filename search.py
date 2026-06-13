"""DuckDuckGo 웹 검색 (API 키 불필요)."""
import asyncio
import logging

from ddgs import DDGS

logger = logging.getLogger("telegram-bot.search")

MAX_RESULTS = 5


def _search_sync(query: str) -> list[dict]:
    with DDGS() as ddgs:
        return list(ddgs.text(query, max_results=MAX_RESULTS))


async def search(query: str) -> list[dict]:
    """검색은 블로킹이므로 별도 스레드에서 실행해 이벤트 루프를 막지 않는다."""
    try:
        return await asyncio.to_thread(_search_sync, query)
    except Exception:  # noqa: BLE001
        logger.exception("웹 검색 실패")
        return []


def build_prompt(query: str, results: list[dict]) -> str:
    """검색 결과를 근거로 답하도록 지시문을 만든다."""
    blocks = []
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "").strip()
        body = (r.get("body") or "").strip()
        href = (r.get("href") or "").strip()
        blocks.append(f"[{i}] {title}\n{body}\n출처: {href}")
    context = "\n\n".join(blocks)

    return (
        "아래는 웹 검색 결과야. 이걸 근거로 사용자 질문에 한국어로 답해줘.\n"
        "- 검색 결과에 있는 내용만 사용하고, 없는 내용은 지어내지 마.\n"
        "- 결과가 질문에 충분하지 않으면 '검색 결과로는 확실하지 않다'고 솔직히 말해.\n"
        "- 답변 끝에 참고한 출처 번호([1], [2] 등)를 표시해.\n\n"
        f"=== 검색 결과 ===\n{context}\n\n"
        f"=== 질문 ===\n{query}"
    )
