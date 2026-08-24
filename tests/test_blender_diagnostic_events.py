import importlib.util
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "blender_extension"
    / "facelink"
    / "diagnostic_events.py"
)
SPEC = importlib.util.spec_from_file_location("facelink_blender_diagnostic_events", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
diagnostic_events = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diagnostic_events)

REQUEST_ID = "req-0123456789abcdef"
CORRELATION_ID = "corr-0123456789abcdef0123456789abcdef"
JOB_ID = "job-0123456789abcdef"


@pytest.mark.parametrize("capacity", [0, -1, 257])
def test_event_buffer_rejects_capacity_outside_public_bound(capacity):
    with pytest.raises(ValueError, match="capacity"):
        diagnostic_events.DiagnosticEventBuffer(capacity)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/v1/health", "/v1/health"),
        ("/v1/jobs/job-private", "/v1/jobs/{id}"),
        ("/v1/jobs/job-private/cancel", "/v1/jobs/{id}/cancel"),
        ("/v1/health?token=private", "/v1/health"),
        ("/private/project/name", "/unmatched"),
    ],
)
def test_route_template_never_retains_dynamic_path_content(path, expected):
    assert diagnostic_events.route_template(path) == expected


def test_record_retains_only_allowlisted_metadata(monkeypatch):
    monkeypatch.setattr(diagnostic_events.time, "time", lambda: 42.5)
    events = diagnostic_events.DiagnosticEventBuffer(capacity=2)
    events.record(
        request_id=REQUEST_ID,
        correlation_id=CORRELATION_ID,
        method="POST",
        path="/v1/jobs/private-job/cancel?prompt=private",
        status=409,
        error_code="job_not_cancellable",
        job_id=JOB_ID,
        job_type="apply_patch",
        job_status="failed",
    )
    assert events.snapshot() == {
        "capacity": 2,
        "retained": 1,
        "evicted_total": 0,
        "events": [
            {
                "timestamp": 42.5,
                "request_id": REQUEST_ID,
                "correlation_id": CORRELATION_ID,
                "method": "POST",
                "route": "/v1/jobs/{id}/cancel",
                "http_status": 409,
                "error_code": "job_not_cancellable",
                "job_id": JOB_ID,
                "job_type": "apply_patch",
                "job_status": "failed",
            }
        ],
    }


def test_event_buffer_is_bounded_and_evicts_only_oldest():
    events = diagnostic_events.DiagnosticEventBuffer(capacity=2)
    for index in range(3):
        events.record(
            request_id=f"req-{index:016x}",
            correlation_id=CORRELATION_ID,
            method="GET",
            path="/v1/health",
            status=200,
        )
    snapshot = events.snapshot()
    assert snapshot["retained"] == 2
    assert snapshot["evicted_total"] == 1
    assert [item["request_id"] for item in snapshot["events"]] == [
        "req-0000000000000001",
        "req-0000000000000002",
    ]


def test_concurrent_event_writes_remain_bounded_and_complete():
    events = diagnostic_events.DiagnosticEventBuffer()

    def record(index):
        events.record(
            request_id=f"req-{index:016x}",
            correlation_id=CORRELATION_ID,
            method="GET",
            path=f"/v1/jobs/job-{index}",
            status=200,
        )

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(record, range(1000)))

    snapshot = events.snapshot()
    assert snapshot["retained"] == diagnostic_events.MAX_DIAGNOSTIC_EVENTS
    assert snapshot["evicted_total"] == 1000 - diagnostic_events.MAX_DIAGNOSTIC_EVENTS
    assert len({item["request_id"] for item in snapshot["events"]}) == snapshot["retained"]
    assert {item["route"] for item in snapshot["events"]} == {"/v1/jobs/{id}"}


@pytest.mark.parametrize(
    "changes",
    [
        {"request_id": "private request"},
        {"correlation_id": "corr-secret"},
        {"method": "PUT"},
        {"status": 99},
        {"status": True},
        {"error_code": "bad error"},
        {"job_id": "private-job-name"},
        {"job_type": "private job"},
        {"job_status": "unknown"},
    ],
)
def test_event_buffer_rejects_unbounded_or_untrusted_metadata(changes):
    arguments = {
        "request_id": REQUEST_ID,
        "correlation_id": CORRELATION_ID,
        "method": "GET",
        "path": "/v1/health",
        "status": 200,
    }
    arguments.update(changes)
    with pytest.raises(ValueError):
        diagnostic_events.DiagnosticEventBuffer().record(**arguments)


def test_snapshot_is_a_copy_and_clear_resets_metrics():
    events = diagnostic_events.DiagnosticEventBuffer(capacity=1)
    events.record(
        request_id=REQUEST_ID,
        correlation_id=CORRELATION_ID,
        method="GET",
        path="/v1/health",
        status=200,
    )
    snapshot = events.snapshot()
    snapshot["events"][0]["route"] = "/changed"
    assert events.snapshot()["events"][0]["route"] == "/v1/health"
    events.clear()
    assert events.snapshot() == {
        "capacity": 1,
        "retained": 0,
        "evicted_total": 0,
        "events": [],
    }
