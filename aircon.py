"""에어컨 IR 제어 서버(ir_server) 연동.

LAN 안 라즈베리파이에서 도는 ir_server HTTP API(JSON)를 호출해 에어컨을
켜고/끄고 모드·온도를 바꾼다. 봇이 같은 파이 위(host networking)면 localhost:8000.

핵심:
- 켤 때(on)는 mode·temp·power 를 모두 지정하는 게 안전하다. 온도를 빼면
  서버가 임의 온도 수집본을 잡을 수 있다.
- 끌 때(off)는 온도 무관 → power="off" 만으로 충분.
- 가능한 mode/temp 값은 수집된 데이터에 따름 → list_configs 로 확인.
- ir_server 에 IR_HTTP_TOKEN 이 설정돼 있으면 Bearer 인증 헤더가 필요하다.
"""
import os
import logging

import httpx

logger = logging.getLogger("telegram-bot.aircon")

# ir_server 베이스 URL. 봇이 같은 파이 위면 localhost:8000.
IR_SERVER_URL = os.environ.get("IR_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")
# ir_server 에 IR_HTTP_TOKEN 설정 시 Bearer 인증 필요. 비면 무인증.
IR_HTTP_TOKEN = os.environ.get("IR_HTTP_TOKEN", "")

# IR 송신은 LED 단일 자원이라 서버가 직렬 처리한다. 합성/대기까지 고려해
# read 는 넉넉히, 다만 무한 대기는 피한다.
_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=10.0)


def _headers() -> dict:
    return {"Authorization": f"Bearer {IR_HTTP_TOKEN}"} if IR_HTTP_TOKEN else {}


async def list_configs(client: httpx.AsyncClient) -> list[dict]:
    """수집/송신 가능한 설정 목록을 반환. 연결/HTTP 오류 시 예외를 올린다."""
    resp = await client.get(f"{IR_SERVER_URL}/list", headers=_headers(), timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json().get("configs", [])


async def send(
    client: httpx.AsyncClient,
    *,
    mode: str | None = None,
    temp: int | None = None,
    power: str | None = None,
    label: str | None = None,
) -> dict:
    """에어컨에 IR 송신. 성공 시 결과 dict, 실패 시 ValueError(사용자용 메시지)를 올린다.

    label 이 있으면 라벨 직접 지정, 없으면 mode/temp/power 부분 지정으로 매칭한다.
    """
    if label:
        body: dict = {"label": label}
    else:
        body = {}
        if mode:
            body["mode"] = mode
        if temp is not None:
            body["temp"] = temp
        if power:
            body["power"] = power
    if not body:
        raise ValueError("제어 파라미터가 없습니다 (mode/temp/power 또는 label 필요).")

    try:
        resp = await client.post(
            f"{IR_SERVER_URL}/send", json=body, headers=_headers(), timeout=_TIMEOUT
        )
    except httpx.RequestError as e:
        logger.exception("ir_server 연결 실패")
        raise ValueError(f"에어컨 서버에 연결하지 못했습니다 ({IR_SERVER_URL}).") from e

    try:
        data = resp.json()
    except ValueError:
        data = {}

    if resp.status_code == 200 and data.get("ok"):
        return data

    # 서버가 준 에러 메시지를 우선 노출, 없으면 상태코드로 대체
    raise ValueError(data.get("error") or f"서버 오류 (HTTP {resp.status_code}).")
