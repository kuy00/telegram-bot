"""서버(라즈베리파이) 상태 점검. 추가 의존성 없이 /proc·/sys 를 직접 읽는다.

컨테이너 안에서 돌더라도 /proc(meminfo·loadavg·stat)와 /sys/class/thermal 은
호스트 커널 값을 그대로 반영한다 → 온도·메모리·부하·CPU는 추가 마운트 없이
'파이 호스트' 기준으로 정확하다. 디스크만 컨테이너 오버레이FS 기준이라,
SD카드 실제 사용량을 보려면 호스트 루트를 마운트하고 DISK_PATH 로 가리키면 된다."""
import os
import asyncio
import shutil
import logging
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("telegram-bot.status")

# 디스크 사용량을 측정할 경로. 컨테이너 기본 '/'는 오버레이FS라 SD카드와 다르다.
# docker-compose 에서 호스트 루트를 ro 로 마운트하고 이 값을 그 경로로 주면 정확.
DISK_PATH = os.environ.get("DISK_PATH", "/")

# Ollama 살아있는지 확인할 베이스 URL (OLLAMA_URL 에서 scheme+host:port 만 추출)
_ollama_url = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
_p = urlparse(_ollama_url)
OLLAMA_BASE = f"{_p.scheme}://{_p.netloc}"
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:4b")

# 라즈베리파이는 80°C 부근에서 스로틀링 → 경고 임계값
TEMP_WARN_C = 75.0

# 라즈베리파이 펌웨어가 노출하는 throttle 비트마스크(vcgencmd get_throttled 와 동일 값).
# 컨테이너 안에서도 /sys 가 마운트돼 읽힌다.
THROTTLE_PATH = "/sys/devices/platform/soc/soc:firmware/get_throttled"
# 비트 의미 (현재 상태는 하위 비트, 부팅 후 발생 이력은 +16 비트)
THROTTLE_BITS = {
    "under_voltage_now": 0,   # 현재 저전압
    "freq_capped_now": 1,     # 현재 주파수 제한
    "throttled_now": 2,       # 현재 스로틀링
    "under_voltage_past": 16,  # 부팅 후 저전압 발생 이력
    "freq_capped_past": 17,
    "throttled_past": 18,
}


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def cpu_temp() -> float | None:
    """CPU 온도(℃). thermal_zone0 가 보통 SoC 온도다."""
    raw = _read("/sys/class/thermal/thermal_zone0/temp").strip()
    if raw.lstrip("-").isdigit():
        return int(raw) / 1000.0
    return None


def load_avg() -> tuple[float, float, float] | None:
    """1/5/15분 평균 부하."""
    parts = _read("/proc/loadavg").split()
    if len(parts) >= 3:
        try:
            return float(parts[0]), float(parts[1]), float(parts[2])
        except ValueError:
            return None
    return None


def mem_info() -> dict | None:
    """메모리 사용량(MB)과 사용률(%). MemAvailable 기준(캐시 제외 실사용)."""
    info: dict[str, int] = {}
    for line in _read("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        val = rest.strip().split(" ")[0]
        if val.isdigit():
            info[key.strip()] = int(val)  # kB
    total = info.get("MemTotal")
    avail = info.get("MemAvailable")
    if total and avail is not None:
        used = total - avail
        return {
            "total_mb": total / 1024,
            "used_mb": used / 1024,
            "percent": used / total * 100,
        }
    return None


def uptime_seconds() -> float | None:
    parts = _read("/proc/uptime").split()
    if parts:
        try:
            return float(parts[0])
        except ValueError:
            return None
    return None


def _cpu_times() -> tuple[int, int] | None:
    """/proc/stat 첫 줄에서 (idle, total) 누적 jiffies."""
    first = _read("/proc/stat").splitlines()
    if not first or not first[0].startswith("cpu "):
        return None
    try:
        fields = [int(x) for x in first[0].split()[1:]]
    except ValueError:
        return None
    if len(fields) < 4:
        return None
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)  # idle + iowait
    return idle, sum(fields)


async def cpu_percent(interval: float = 0.5) -> float | None:
    """짧은 간격으로 두 번 샘플링해 CPU 사용률(%) 계산."""
    a = _cpu_times()
    await asyncio.sleep(interval)
    b = _cpu_times()
    if not a or not b:
        return None
    idle_d = b[0] - a[0]
    total_d = b[1] - a[1]
    if total_d <= 0:
        return None
    return (1 - idle_d / total_d) * 100


def disk_usage() -> dict | None:
    try:
        u = shutil.disk_usage(DISK_PATH)
    except OSError:
        return None
    return {
        "total_gb": u.total / 2**30,
        "used_gb": u.used / 2**30,
        "percent": u.used / u.total * 100 if u.total else 0.0,
    }


def _undervolt_alarm_path() -> str | None:
    """rpi_volt hwmon 의 저전압 알람 파일 경로(현재 저전압 여부 1/0)."""
    base = "/sys/class/hwmon"
    try:
        for d in os.listdir(base):
            if _read(os.path.join(base, d, "name")).strip() == "rpi_volt":
                return os.path.join(base, d, "in0_lcrit_alarm")
    except OSError:
        pass
    return None


def power_status() -> dict | None:
    """전원/스로틀 상태. 저전압(전원 부족)이 핵심 신호다. 못 읽으면 None."""
    raw = _read(THROTTLE_PATH).strip()
    if raw:
        try:
            value = int(raw, 16)  # 보통 '0x0' / '0x50000' 형식
        except ValueError:
            value = None
        if value is not None:
            return {k: bool(value & (1 << b)) for k, b in THROTTLE_BITS.items()}

    # 폴백: hwmon 저전압 알람(현재 저전압만 알 수 있음)
    alarm = _undervolt_alarm_path()
    if alarm:
        flag = _read(alarm).strip() == "1"
        return {"under_voltage_now": flag, "under_voltage_past": flag,
                "freq_capped_now": False, "throttled_now": False,
                "freq_capped_past": False, "throttled_past": False}
    return None


async def ollama_status(client: httpx.AsyncClient) -> dict:
    """Ollama 데몬 가용성과 메모리에 올라온 모델 확인."""
    try:
        resp = await client.get(f"{OLLAMA_BASE}/api/ps", timeout=5.0)
        resp.raise_for_status()
        models = [m.get("name", "") for m in resp.json().get("models", [])]
        return {"alive": True, "loaded": models}
    except Exception:  # noqa: BLE001
        return {"alive": False, "loaded": []}


def _fmt_uptime(secs: float) -> str:
    total = int(secs)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    out = []
    if days:
        out.append(f"{days}일")
    if hours:
        out.append(f"{hours}시간")
    out.append(f"{mins}분")
    return " ".join(out)


async def report(client: httpx.AsyncClient) -> str:
    """모든 지표를 모아 텔레그램용 한국어 텍스트로 만든다."""
    # CPU 사용률은 샘플링에 약간 시간이 걸리므로 다른 지표와 함께 처리
    cpu, ollama = await asyncio.gather(cpu_percent(), ollama_status(client))

    lines = ["🖥 서버 상태"]

    temp = cpu_temp()
    if temp is not None:
        flag = " ⚠️ 과열 주의" if temp >= TEMP_WARN_C else ""
        lines.append(f"🌡 CPU 온도: {temp:.1f}°C{flag}")

    power = power_status()
    if power is not None:
        if power["under_voltage_now"]:
            lines.append("🔌 전원: ⚠️ 저전압 감지됨 — 전원 어댑터/케이블 점검 필요")
        elif power["under_voltage_past"]:
            lines.append("🔌 전원: ⚠️ 부팅 후 저전압 발생 이력 있음 (현재는 정상)")
        elif power["throttled_now"] or power["freq_capped_now"]:
            lines.append("🔌 전원: ⚠️ 스로틀링 중 (전원/온도 확인)")
        else:
            lines.append("🔌 전원: 정상")

    if cpu is not None:
        lines.append(f"⚙️ CPU 사용률: {cpu:.0f}%")

    la = load_avg()
    if la:
        lines.append(f"📊 부하(1/5/15분): {la[0]:.2f} / {la[1]:.2f} / {la[2]:.2f}")

    mem = mem_info()
    if mem:
        lines.append(
            f"🧠 메모리: {mem['used_mb']/1024:.1f}/{mem['total_mb']/1024:.1f} GB "
            f"({mem['percent']:.0f}%)"
        )

    disk = disk_usage()
    if disk:
        lines.append(
            f"💾 디스크: {disk['used_gb']:.1f}/{disk['total_gb']:.1f} GB "
            f"({disk['percent']:.0f}%)"
        )

    up = uptime_seconds()
    if up is not None:
        lines.append(f"⏱ 가동시간: {_fmt_uptime(up)}")

    if ollama["alive"]:
        loaded = ", ".join(ollama["loaded"]) if ollama["loaded"] else "로드된 모델 없음"
        lines.append(f"🤖 Ollama: 정상 ({loaded})")
    else:
        lines.append("🤖 Ollama: ⚠️ 응답 없음")

    if len(lines) == 1:
        return "상태 정보를 읽지 못했어요. (/proc·/sys 접근 불가일 수 있어요)"
    return "\n".join(lines)
