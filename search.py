"""웹 검색. TAVILY_API_KEY 가 있으면 Tavily(LLM 친화, 본문·요약 제공),
없으면 DuckDuckGo(스니펫만)로 폴백한다."""
import os
import asyncio
import logging

import httpx
from ddgs import DDGS

logger = logging.getLogger("telegram-bot.search")

MAX_RESULTS = 5
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "").strip()
TAVILY_URL = "https://api.tavily.com/search"


async def _tavily(client: httpx.AsyncClient, query: str) -> dict:
    resp = await client.post(
        TAVILY_URL,
        headers={"Authorization": f"Bearer {TAVILY_API_KEY}"},
        json={
            "query": query,
            "max_results": MAX_RESULTS,
            "search_depth": "basic",   # basic=1크레딧 (무료 한도 절약)
            "include_answer": True,    # 검색 결과를 종합한 요약 답변까지 받기
        },
        timeout=20.0,
    )
    resp.raise_for_status()
    d = resp.json()
    results = [
        {"title": r.get("title", ""), "url": r.get("url", ""), "content": r.get("content", "")}
        for r in d.get("results", [])
    ]
    return {"answer": d.get("answer") or "", "results": results}


def _ddgs_sync(query: str) -> list[dict]:
    with DDGS() as ddgs:
        return list(ddgs.text(query, max_results=MAX_RESULTS))


async def _ddgs(query: str) -> dict:
    raw = await asyncio.to_thread(_ddgs_sync, query)
    results = [
        {"title": r.get("title", ""), "url": r.get("href", ""), "content": r.get("body", "")}
        for r in raw
    ]
    return {"answer": "", "results": results}


async def search(client: httpx.AsyncClient, query: str) -> dict:
    """{"answer": str, "results": [{title, url, content}]} 반환. 실패 시 빈 결과."""
    try:
        if TAVILY_API_KEY:
            return await _tavily(client, query)
        return await _ddgs(query)
    except Exception:  # noqa: BLE001
        logger.exception("웹 검색 실패 — 폴백 시도")
        try:
            return await _ddgs(query)  # Tavily 실패 시 DuckDuckGo 폴백
        except Exception:  # noqa: BLE001
            logger.exception("폴백 검색도 실패")
            return {"answer": "", "results": []}


def to_context(data: dict) -> str:
    """검색 결과를 모델에 넣을 텍스트로 변환."""
    parts = []
    if data.get("answer"):
        parts.append(f"[검색 요약]\n{data['answer']}")
    for i, r in enumerate(data.get("results", []), 1):
        parts.append(f"[{i}] {r['title']}\n{r['content']}\n출처: {r['url']}")
    return "\n\n".join(parts)


def build_prompt(query: str, data: dict) -> str:
    """/search 수동 명령용: 검색 결과를 근거로 답하도록 지시문 생성."""
    return (
        "아래는 웹 검색 결과야. 이걸 근거로 사용자 질문에 한국어로 답해줘.\n"
        "- 검색 결과에 있는 내용만 사용하고, 없는 내용은 지어내지 마.\n"
        "- 결과가 질문에 충분하지 않으면 '검색 결과로는 확실하지 않다'고 솔직히 말해.\n"
        "- 답변 끝에 참고한 출처 번호([1], [2] 등)를 표시해.\n\n"
        f"=== 검색 결과 ===\n{to_context(data)}\n\n"
        f"=== 질문 ===\n{query}"
    )
