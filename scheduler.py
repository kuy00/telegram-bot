"""지연 예약(reservation) 스케줄러.

"30분 뒤에 에어컨 꺼줘"처럼 나중에 실행할 작업을 예약한다. 구현은 이 봇의
가벼운/무상태 설계에 맞춰 **인메모리 asyncio 태스크**로 한다. uvicorn 이벤트
루프 위에서 `asyncio.create_task` 로 sleep 후 콜백을 실행할 뿐이라 추가 의존성이
없다.

한계(의도된 것): 예약은 프로세스 메모리에만 있어 **봇 재시작 시 사라진다.**
영구 예약(재시작 후에도 유지)이 필요하면 SQLite 등으로 저장해야 한다 —
CLAUDE.md 의 "향후 개선 후보"와 같은 맥락.

- `parse(text)`  : 자연어에서 (지연 초, 실행할 내용)을 뽑는다. 못 뽑으면 None.
- `schedule(...)`: 예약을 걸고 Job 을 돌려준다.
- `list_jobs`/`cancel`/`cancel_all` : 조회·취소.
"""
import asyncio
import re
import logging
from datetime import datetime, timezone, timedelta
from typing import Awaitable, Callable

logger = logging.getLogger("telegram-bot.scheduler")

# 한국 표준시 (DST 없음 → UTC+9 고정). main.py 와 동일.
KST = timezone(timedelta(hours=9))

# 시간 단위 → 초
_UNIT_SECONDS = {"초": 1, "분": 60, "시간": 3600}

# 예약 최대 지연(7일). 실수/오버플로 방지 및 무한정 sleep 차단.
MAX_DELAY = 7 * 24 * 3600

# 상대 시각: "1시간 30분 뒤에 <행동>" — 숫자+단위(하나 이상) + 뒤/후/이따 등 마커.
_REL_RE = re.compile(
    r"((?:\d+\s*(?:시간|분|초)\s*)+)"          # 1) 시간량 (여러 단위 연속 허용)
    r"(?:뒤|후|이따가?|있다가?|지나서?)"       # 2) 지연 마커
    r"에?\s*"                                   # 3) 마커에 붙은 조사 '에'(선택)
)

# 절대 시각: "오후 3시 30분에 <행동>" — 끝에 '에/쯤/정각' 을 요구해 오탐을 줄인다.
# '시간'의 '시' 오인 방지로 시(?!간).
_ABS_RE = re.compile(
    r"(오전|오후|아침|저녁|밤|낮)?\s*"          # 1) 오전/오후 등(선택)
    r"(\d{1,2})\s*시(?!간)\s*"                  # 2) 시
    r"(?:(\d{1,2})\s*분)?\s*"                   # 3) 분(선택)
    r"(?:에|쯤|정각에?)"                        # 4) 마커(필수) — 오탐 억제
)


class Job:
    """예약 1건. asyncio 태스크를 들고 있다가 취소 시 태스크를 취소한다."""

    def __init__(self, job_id: int, chat_id: int, action_text: str,
                 fire_at: datetime, task: "asyncio.Task"):
        self.id = job_id
        self.chat_id = chat_id
        self.action_text = action_text
        self.fire_at = fire_at
        self.task = task


_jobs: dict[int, Job] = {}
_counter = 0


def _next_id() -> int:
    global _counter
    _counter += 1
    return _counter


def _sum_relative(time_part: str) -> int:
    seconds = 0
    for num, unit in re.findall(r"(\d+)\s*(시간|분|초)", time_part):
        seconds += int(num) * _UNIT_SECONDS[unit]
    return seconds


def _split_action(text: str, m: "re.Match") -> str:
    """시간 표현(m) 앞/뒤에 흩어진 행동 문구를 이어붙인다.

    '30분 뒤에 에어컨 꺼줘' → '에어컨 꺼줘'
    '에어컨 30분 뒤에 꺼줘' → '에어컨 꺼줘'
    '에어컨 꺼줘 30분 뒤'   → '에어컨 꺼줘'
    """
    before = text[:m.start()].strip()
    after = text[m.end():].strip()
    action = (before + " " + after).strip() if before and after else (before or after)
    return action.strip(" ,.!?~")


def parse(text: str):
    """자연어에서 (지연_초, 실행할_내용)을 뽑는다. 못 뽑으면 None.

    상대 시각('N분 뒤')을 먼저, 없으면 절대 시각('3시에')을 시도한다.
    지연이 0 이하이거나 실행할 내용이 비면 None.
    """
    t = text.strip()

    # 1) 상대 시각
    m = _REL_RE.search(t)
    if m:
        seconds = _sum_relative(m.group(1))
        action = _split_action(t, m)
        if seconds > 0 and action:
            return min(seconds, MAX_DELAY), action

    # 2) 절대 시각
    m = _ABS_RE.search(t)
    if m:
        seconds = _absolute_delay(m.group(1), int(m.group(2)),
                                  int(m.group(3)) if m.group(3) else 0)
        action = _split_action(t, m)
        if seconds is not None and seconds > 0 and action:
            return min(seconds, MAX_DELAY), action

    return None


def _absolute_delay(ampm: str | None, hour: int, minute: int):
    """오늘/내일의 지정 시각까지 남은 초. 값이 이상하면 None."""
    if ampm in ("오후", "저녁", "밤") and hour < 12:
        hour += 12
    elif ampm in ("오전", "아침") and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    now = datetime.now(KST)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:                      # 이미 지난 시각이면 내일로
        target += timedelta(days=1)
    return (target - now).total_seconds()


def humanize_delay(seconds: float) -> str:
    """초 → '1시간 30분' 같은 사람이 읽는 문구."""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}시간")
    if m:
        parts.append(f"{m}분")
    if s and not h:            # 시간 단위가 있으면 초는 생략(장황함 방지)
        parts.append(f"{s}초")
    return " ".join(parts) or "잠시"


def schedule(
    chat_id: int,
    delay: float,
    action_text: str,
    callback: Callable[[int, str], Awaitable[None]],
) -> Job:
    """delay 초 뒤 callback(chat_id, action_text) 를 실행하는 예약을 건다."""
    job_id = _next_id()

    async def _runner():
        try:
            await asyncio.sleep(delay)
            await callback(chat_id, action_text)
        except asyncio.CancelledError:      # 취소는 조용히 종료
            raise
        except Exception:  # noqa: BLE001
            logger.exception("예약 작업 실행 실패 (#%s)", job_id)
        finally:
            _jobs.pop(job_id, None)

    task = asyncio.create_task(_runner())
    fire_at = datetime.now(KST) + timedelta(seconds=delay)
    job = Job(job_id, chat_id, action_text, fire_at, task)
    _jobs[job_id] = job
    logger.info("예약 등록 #%s chat=%s +%ss '%s'", job_id, chat_id, int(delay), action_text)
    return job


def list_jobs(chat_id: int) -> list[Job]:
    """해당 chat 의 예약을 실행 예정 시각 순으로 반환."""
    jobs = [j for j in _jobs.values() if j.chat_id == chat_id]
    return sorted(jobs, key=lambda j: j.fire_at)


def cancel(chat_id: int, job_id: int) -> bool:
    """예약 1건 취소. 성공 True. (본인 chat 의 예약만 취소 가능)"""
    job = _jobs.get(job_id)
    if job is None or job.chat_id != chat_id:
        return False
    job.task.cancel()
    _jobs.pop(job_id, None)
    return True


def cancel_all(chat_id: int) -> int:
    """해당 chat 의 예약 전부 취소. 취소한 건수 반환."""
    ids = [j.id for j in _jobs.values() if j.chat_id == chat_id]
    for jid in ids:
        cancel(chat_id, jid)
    return len(ids)
