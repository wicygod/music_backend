from app.middleware.performance import operation_for_request
from app.services.performance_metrics import PerformanceMetrics


def test_request_classifier_covers_critical_user_flows() -> None:
    assert operation_for_request("POST", "/api/auth/register") == "registration"
    assert operation_for_request("GET", "/api/search") == "search"
    assert operation_for_request("POST", "/api/stream/track/42/ticket") == "stream_ticket"
    assert operation_for_request("GET", "/api/stream/track/42") == "stream_setup"
    assert operation_for_request("POST", "/api/history/progress") == "listening_progress"
    assert operation_for_request("GET", "/api/feed/home") is None


def test_metrics_report_latency_percentiles_and_failures() -> None:
    metrics = PerformanceMetrics(max_samples_per_operation=3)
    metrics.record("search", 10, 200)
    metrics.record("search", 20, 200)
    metrics.record("search", 90, 502)

    summary = metrics.snapshot()["operations"]["search"]

    assert summary["count"] == 3
    assert summary["failures"] == 1
    assert summary["failure_rate"] == 0.3333
    assert summary["average_ms"] == 40.0
    assert summary["p50_ms"] == 20.0
    assert summary["p95_ms"] == 90.0
