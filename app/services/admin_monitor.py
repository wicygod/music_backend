from __future__ import annotations

import os
import platform
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any

try:
    import psutil
except ImportError:  # pragma: no cover - production dependency is installed on VPS.
    psutil = None


_started_at = time.time()
_events: deque[dict[str, Any]] = deque(maxlen=300)
_lock = threading.Lock()


def record_event(kind: str, message: str, *, ip: str | None = None, path: str | None = None) -> None:
    with _lock:
        _events.append(
            {
                "id": int(time.time() * 1000),
                "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "kind": kind,
                "ip": ip or "unknown",
                "path": path or "",
                "message": message,
            }
        )


def recent_events(limit: int = 80) -> list[dict[str, Any]]:
    with _lock:
        return list(_events)[-limit:]


def _memory_stats() -> dict[str, float | int]:
    if not psutil:
        return {"percent": 0, "used": 0, "total": 0}
    memory = psutil.virtual_memory()
    return {
        "percent": round(float(memory.percent), 1),
        "used": int(memory.used),
        "total": int(memory.total),
    }


def system_stats() -> dict[str, Any]:
    cpu_percent = psutil.cpu_percent(interval=0.1) if psutil else 0.0
    return {
        "host": platform.node() or "hiplet",
        "pid": os.getpid(),
        "uptime_seconds": int(time.time() - _started_at),
        "cpu_percent": round(float(cpu_percent), 1),
        "memory": _memory_stats(),
        "events_buffer": len(_events),
    }
