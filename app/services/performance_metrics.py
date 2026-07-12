from __future__ import annotations

import math
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass(frozen=True)
class MetricSample:
    duration_ms: float
    status_code: int
    recorded_at: float


class PerformanceMetrics:
    def __init__(self, max_samples_per_operation: int = 500) -> None:
        self._max_samples = max_samples_per_operation
        self._samples: dict[str, deque[MetricSample]] = defaultdict(
            lambda: deque(maxlen=self._max_samples)
        )
        self._lock = threading.Lock()

    def record(self, operation: str, duration_ms: float, status_code: int) -> None:
        sample = MetricSample(
            duration_ms=max(0.0, float(duration_ms)),
            status_code=int(status_code),
            recorded_at=time.time(),
        )
        with self._lock:
            self._samples[operation].append(sample)

    def snapshot(self) -> dict:
        with self._lock:
            samples = {name: tuple(values) for name, values in self._samples.items()}
        return {
            "operations": {
                name: self._summarize(values)
                for name, values in sorted(samples.items())
            }
        }

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()

    @staticmethod
    def _summarize(samples: tuple[MetricSample, ...]) -> dict:
        durations = sorted(sample.duration_ms for sample in samples)
        count = len(durations)
        failures = sum(sample.status_code >= 400 for sample in samples)

        def percentile(value: float) -> float:
            if not durations:
                return 0.0
            index = max(0, math.ceil(value * count) - 1)
            return round(durations[index], 2)

        return {
            "count": count,
            "failures": failures,
            "failure_rate": round(failures / count, 4) if count else 0.0,
            "average_ms": round(sum(durations) / count, 2) if count else 0.0,
            "p50_ms": percentile(0.50),
            "p95_ms": percentile(0.95),
            "max_ms": round(durations[-1], 2) if durations else 0.0,
            "last_recorded_at": max((sample.recorded_at for sample in samples), default=None),
        }


performance_metrics = PerformanceMetrics()
