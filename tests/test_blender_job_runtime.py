import importlib.util
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "blender_extension" / "facelink" / "job_runtime.py"
)
SPEC = importlib.util.spec_from_file_location("facelink_blender_job_runtime", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
job_runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(job_runtime)


def test_error_payload_has_stable_required_fields_and_copies_details():
    details = {"capacity": 2}
    payload = job_runtime.error_payload(
        "queue_full",
        "Try later.",
        retryable=True,
        details=details,
        retry_after_ms=100,
    )
    details["capacity"] = 99
    assert payload == {
        "error": "Try later.",
        "error_code": "queue_full",
        "retryable": True,
        "details": {"capacity": 2},
        "retry_after_ms": 100,
    }


def test_error_payload_omits_optional_fields_when_absent():
    assert job_runtime.error_payload("invalid_json", "Invalid.") == {
        "error": "Invalid.",
        "error_code": "invalid_json",
        "retryable": False,
    }


@pytest.mark.parametrize(
    ("keyword", "value", "message"),
    [
        ("pending_capacity", 0, "pending_capacity"),
        ("pending_capacity", -1, "pending_capacity"),
        ("retention_capacity", 0, "retention_capacity"),
        ("retention_capacity", -1, "retention_capacity"),
    ],
)
def test_runtime_rejects_non_positive_capacity(keyword, value, message):
    with pytest.raises(ValueError, match=message):
        job_runtime.JobRuntime(**{keyword: value})


def test_submit_take_and_success_transition_preserve_contract(monkeypatch):
    runtime = job_runtime.JobRuntime(pending_capacity=2, retention_capacity=3)
    clock = iter([20.0, 30.0])
    monkeypatch.setattr(job_runtime.time, "time", lambda: next(clock))

    queued = runtime.submit("job-1", "scan_scene", {"frame": 1}, created_at=10.0)
    assert queued == {
        "job_id": "job-1",
        "type": "scan_scene",
        "status": "queued",
        "created_at": 10.0,
    }
    assert runtime.take_nowait() == ("job-1", "scan_scene", {"frame": 1})
    runtime.mark_running("job-1")
    runtime.mark_succeeded("job-1", {"entities": []})

    assert runtime.get("job-1") == {
        **queued,
        "status": "succeeded",
        "started_at": 20.0,
        "finished_at": 30.0,
        "result": {"entities": []},
    }
    assert runtime.take_nowait() is None


def test_failed_transition_uses_stable_error_shape(monkeypatch):
    runtime = job_runtime.JobRuntime()
    monkeypatch.setattr(job_runtime.time, "time", lambda: 42.0)
    runtime.submit("job-1", "stage_patch", {})
    runtime.take_nowait()
    runtime.mark_running("job-1")
    runtime.mark_failed(
        "job-1",
        code="invalid_job_payload",
        message="Missing patch.",
        details={"field": "patch"},
    )

    failed = runtime.get("job-1")
    assert failed is not None
    assert failed["status"] == "failed"
    assert failed["error"] == "Missing patch."
    assert failed["error_code"] == "invalid_job_payload"
    assert failed["retryable"] is False
    assert failed["details"] == {"field": "patch"}
    assert failed["finished_at"] == 42.0


def test_queue_full_rejects_without_creating_a_ghost_job():
    runtime = job_runtime.JobRuntime(pending_capacity=2, retention_capacity=5)
    runtime.submit("job-1", "scan_scene", {})
    runtime.submit("job-2", "scan_scene", {})

    with pytest.raises(job_runtime.JobAdmissionError) as captured:
        runtime.submit("job-3", "scan_scene", {})

    error = captured.value
    assert error.code == "queue_full"
    assert error.retry_after_ms == 100
    assert error.as_payload()["retryable"] is True
    assert error.details == {
        "pending": 2,
        "pending_capacity": 2,
        "retained": 2,
        "retention_capacity": 5,
    }
    assert runtime.get("job-3") is None
    assert runtime.snapshot()["retained"] == 2


def test_registry_full_never_evicts_queued_or_running_jobs():
    runtime = job_runtime.JobRuntime(pending_capacity=3, retention_capacity=2)
    runtime.submit("job-1", "scan_scene", {})
    runtime.submit("job-2", "scan_scene", {})
    runtime.take_nowait()
    runtime.mark_running("job-1")

    with pytest.raises(job_runtime.JobAdmissionError) as captured:
        runtime.submit("job-3", "scan_scene", {})

    assert captured.value.code == "job_registry_full"
    assert runtime.get("job-1")["status"] == "running"
    assert runtime.get("job-2")["status"] == "queued"
    assert runtime.get("job-3") is None


def test_new_admission_prunes_only_the_oldest_terminal_record():
    runtime = job_runtime.JobRuntime(pending_capacity=3, retention_capacity=3)
    for index in range(1, 4):
        runtime.submit(f"job-{index}", "scan_scene", {})
    for index in range(1, 4):
        assert runtime.take_nowait()[0] == f"job-{index}"
        runtime.mark_running(f"job-{index}")
        runtime.mark_succeeded(f"job-{index}", {"index": index})

    runtime.submit("job-4", "scan_scene", {})

    assert runtime.get("job-1") is None
    assert runtime.get("job-2")["status"] == "succeeded"
    assert runtime.get("job-3")["status"] == "succeeded"
    assert runtime.get("job-4")["status"] == "queued"
    assert runtime.snapshot() == {
        "pending": 1,
        "pending_capacity": 3,
        "retained": 3,
        "retention_capacity": 3,
        "status_counts": {"queued": 1, "running": 0, "succeeded": 2, "failed": 0},
        "evicted_total": 1,
    }


def test_duplicate_job_id_is_rejected_without_mutating_the_queue():
    runtime = job_runtime.JobRuntime(pending_capacity=2, retention_capacity=2)
    runtime.submit("job-1", "scan_scene", {"first": True})
    with pytest.raises(ValueError, match="already exists"):
        runtime.submit("job-1", "undo", {"second": True})
    assert runtime.take_nowait() == ("job-1", "scan_scene", {"first": True})
    assert runtime.take_nowait() is None


def test_invalid_state_transitions_and_unknown_jobs_fail_closed():
    runtime = job_runtime.JobRuntime()
    runtime.submit("job-1", "scan_scene", {})
    with pytest.raises(RuntimeError, match="cannot transition from queued"):
        runtime.mark_succeeded("job-1", {})
    with pytest.raises(KeyError, match="missing"):
        runtime.mark_running("missing")


def test_clear_removes_records_pending_work_and_eviction_metrics():
    runtime = job_runtime.JobRuntime(pending_capacity=1, retention_capacity=1)
    runtime.submit("job-1", "scan_scene", {})
    runtime.clear()
    assert runtime.get("job-1") is None
    assert runtime.take_nowait() is None
    assert runtime.snapshot() == {
        "pending": 0,
        "pending_capacity": 1,
        "retained": 0,
        "retention_capacity": 1,
        "status_counts": {"queued": 0, "running": 0, "succeeded": 0, "failed": 0},
        "evicted_total": 0,
    }


def test_concurrent_admission_is_bounded_atomic_and_non_duplicating():
    runtime = job_runtime.JobRuntime(pending_capacity=32, retention_capacity=32)

    def attempt(index):
        try:
            runtime.submit(f"job-{index}", "scan_scene", {"index": index})
            return "accepted"
        except job_runtime.JobAdmissionError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = list(pool.map(attempt, range(100)))

    assert outcomes.count("accepted") == 32
    assert outcomes.count("queue_full") == 68
    snapshot = runtime.snapshot()
    assert snapshot["pending"] == 32
    assert snapshot["retained"] == 32
    accepted_ids = []
    while (item := runtime.take_nowait()) is not None:
        accepted_ids.append(item[0])
    assert len(accepted_ids) == len(set(accepted_ids)) == 32
