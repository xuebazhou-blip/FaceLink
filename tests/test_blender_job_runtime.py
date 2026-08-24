import importlib.util
import json
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
        "checkpoint": "accepted",
        "checkpoint_at": 10.0,
    }
    assert runtime.start_next() == ("job-1", "scan_scene", {"frame": 1})
    runtime.mark_succeeded("job-1", {"entities": []})

    assert runtime.get("job-1") == {
        **queued,
        "status": "succeeded",
        "started_at": 20.0,
        "finished_at": 30.0,
        "checkpoint": "terminal",
        "checkpoint_at": 30.0,
        "result": {"entities": []},
    }
    assert runtime.start_next() is None


def test_submission_correlation_ids_survive_the_complete_job_lifecycle(monkeypatch):
    runtime = job_runtime.JobRuntime()
    request_id = "req-0123456789abcdef"
    correlation_id = "corr-0123456789abcdef0123456789abcdef"
    monkeypatch.setattr(job_runtime.time, "time", lambda: 20.0)

    queued = runtime.submit(
        "job-correlated",
        "apply_patch",
        {"private": "work-item-only"},
        created_at=10.0,
        request_id=request_id,
        correlation_id=correlation_id,
    )
    assert queued["submission_request_id"] == request_id
    assert queued["job_correlation_id"] == correlation_id

    assert runtime.start_next() == (
        "job-correlated",
        "apply_patch",
        {"private": "work-item-only"},
    )
    runtime.set_checkpoint(
        "job-correlated",
        "transaction_committed",
        {"revision_id": "rev-1"},
    )
    runtime.mark_succeeded("job-correlated", {"ok": True})

    completed = runtime.get("job-correlated")
    assert completed["submission_request_id"] == request_id
    assert completed["job_correlation_id"] == correlation_id
    _, records = runtime.records_with_version()
    assert records[0]["submission_request_id"] == request_id
    assert records[0]["job_correlation_id"] == correlation_id


def test_omitted_submission_correlation_ids_do_not_create_empty_metadata():
    runtime = job_runtime.JobRuntime()

    queued = runtime.submit("job-1", "scan_scene", {})

    assert "submission_request_id" not in queued
    assert "job_correlation_id" not in queued


def test_failed_transition_uses_stable_error_shape(monkeypatch):
    runtime = job_runtime.JobRuntime()
    monkeypatch.setattr(job_runtime.time, "time", lambda: 42.0)
    runtime.submit("job-1", "stage_patch", {})
    runtime.start_next()
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
    runtime.start_next()

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
        assert runtime.start_next()[0] == f"job-{index}"
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
        "status_counts": {
            "queued": 1,
            "running": 0,
            "succeeded": 2,
            "failed": 0,
            "cancelled": 0,
            "timed_out": 0,
            "interrupted": 0,
        },
        "cancel_requested": 0,
        "evicted_total": 1,
    }


def test_duplicate_job_id_is_rejected_without_mutating_the_queue():
    runtime = job_runtime.JobRuntime(pending_capacity=2, retention_capacity=2)
    runtime.submit("job-1", "scan_scene", {"first": True})
    with pytest.raises(ValueError, match="already exists"):
        runtime.submit("job-1", "undo", {"second": True})
    assert runtime.start_next() == ("job-1", "scan_scene", {"first": True})
    assert runtime.start_next() is None


def test_invalid_state_transitions_and_unknown_jobs_fail_closed():
    runtime = job_runtime.JobRuntime()
    runtime.submit("job-1", "scan_scene", {})
    with pytest.raises(RuntimeError, match="cannot finish from queued"):
        runtime.mark_succeeded("job-1", {})
    with pytest.raises(KeyError, match="missing"):
        runtime.raise_if_interrupted("missing")


def test_clear_removes_records_pending_work_and_eviction_metrics():
    runtime = job_runtime.JobRuntime(pending_capacity=1, retention_capacity=1)
    runtime.submit("job-1", "scan_scene", {})
    runtime.clear()
    assert runtime.get("job-1") is None
    assert runtime.start_next() is None
    assert runtime.snapshot() == {
        "pending": 0,
        "pending_capacity": 1,
        "retained": 0,
        "retention_capacity": 1,
        "status_counts": {
            "queued": 0,
            "running": 0,
            "succeeded": 0,
            "failed": 0,
            "cancelled": 0,
            "timed_out": 0,
            "interrupted": 0,
        },
        "cancel_requested": 0,
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
    while (item := runtime.start_next()) is not None:
        accepted_ids.append(item[0])
    assert len(accepted_ids) == len(set(accepted_ids)) == 32


def test_queued_cancel_is_immediate_idempotent_and_frees_capacity():
    runtime = job_runtime.JobRuntime(pending_capacity=1, retention_capacity=4)
    runtime.submit("job-1", "scan_scene", {})

    cancelled = runtime.cancel("job-1")

    assert cancelled["status"] == "cancelled"
    assert cancelled["error_code"] == "job_cancelled"
    assert cancelled["retryable"] is False
    assert cancelled["cancelled_at"] == cancelled["finished_at"]
    assert runtime.start_next() is None
    assert runtime.cancel("job-1") == cancelled
    runtime.submit("job-2", "scan_scene", {})
    assert runtime.snapshot()["pending"] == 1


def test_running_cancel_requests_interruption_at_a_safe_checkpoint():
    runtime = job_runtime.JobRuntime()
    runtime.submit("job-1", "apply_patch", {})
    runtime.start_next()

    requested = runtime.cancel("job-1")

    assert requested["status"] == "running"
    assert requested["cancel_requested"] is True
    with pytest.raises(job_runtime.JobInterruption) as captured:
        runtime.raise_if_interrupted("job-1")
    assert captured.value.status == "cancelled"
    assert captured.value.code == "job_cancelled"
    runtime.mark_interrupted("job-1", captured.value)
    assert runtime.get("job-1")["status"] == "cancelled"
    assert runtime.snapshot()["cancel_requested"] == 0


def test_completion_wins_if_cancel_arrives_after_the_last_safe_checkpoint():
    runtime = job_runtime.JobRuntime()
    runtime.submit("job-1", "undo", {})
    runtime.start_next()
    runtime.cancel("job-1")

    runtime.mark_succeeded("job-1", {"undone": True})

    completed = runtime.get("job-1")
    assert completed["status"] == "succeeded"
    assert completed["completed_after_cancel_request"] is True
    with pytest.raises(job_runtime.JobControlError) as captured:
        runtime.cancel("job-1")
    assert captured.value.code == "job_not_cancellable"
    assert captured.value.details == {"job_id": "job-1", "status": "succeeded"}


def test_unknown_cancel_returns_none_and_failed_job_is_not_cancellable():
    runtime = job_runtime.JobRuntime()
    assert runtime.cancel("missing") is None
    runtime.submit("job-1", "scan_scene", {})
    runtime.start_next()
    runtime.mark_failed("job-1", code="job_rejected", message="Rejected.")
    with pytest.raises(job_runtime.JobControlError, match="already finished"):
        runtime.cancel("job-1")


def test_queued_deadline_expires_without_dispatch_and_frees_capacity(monkeypatch):
    clock = {"monotonic": 10.0, "wall": 100.0}
    monkeypatch.setattr(job_runtime.time, "monotonic", lambda: clock["monotonic"])
    monkeypatch.setattr(job_runtime.time, "time", lambda: clock["wall"])
    runtime = job_runtime.JobRuntime(pending_capacity=1, retention_capacity=4)
    queued = runtime.submit("job-1", "scan_scene", {}, timeout_ms=500)
    assert queued["timeout_ms"] == 500
    assert queued["deadline_at"] == 100.5

    clock.update(monotonic=10.5, wall=100.5)
    expired = runtime.get("job-1")

    assert expired["status"] == "timed_out"
    assert expired["error_code"] == "job_timed_out"
    assert expired["retryable"] is True
    assert expired["timed_out_at"] == 100.5
    assert runtime.start_next() is None
    runtime.submit("job-2", "scan_scene", {})


def test_running_deadline_interrupts_at_checkpoint(monkeypatch):
    clock = {"monotonic": 10.0}
    monkeypatch.setattr(job_runtime.time, "monotonic", lambda: clock["monotonic"])
    runtime = job_runtime.JobRuntime()
    runtime.submit("job-1", "apply_patch", {}, timeout_ms=100)
    runtime.start_next()
    clock["monotonic"] = 10.1

    with pytest.raises(job_runtime.JobInterruption) as captured:
        runtime.raise_if_interrupted("job-1")

    assert captured.value.status == "timed_out"
    assert captured.value.retryable is True
    runtime.mark_interrupted("job-1", captured.value)
    assert runtime.get("job-1")["status"] == "timed_out"


def test_completion_after_deadline_is_reported_as_success_with_metadata(monkeypatch):
    clock = {"monotonic": 10.0}
    monkeypatch.setattr(job_runtime.time, "monotonic", lambda: clock["monotonic"])
    runtime = job_runtime.JobRuntime()
    runtime.submit("job-1", "undo", {}, timeout_ms=100)
    runtime.start_next()
    clock["monotonic"] = 10.2

    runtime.mark_succeeded("job-1", {"undone": True})

    completed = runtime.get("job-1")
    assert completed["status"] == "succeeded"
    assert completed["completed_after_deadline"] is True


def test_cancelled_and_timed_out_records_are_terminal_and_prunable(monkeypatch):
    clock = {"monotonic": 1.0}
    monkeypatch.setattr(job_runtime.time, "monotonic", lambda: clock["monotonic"])
    runtime = job_runtime.JobRuntime(pending_capacity=2, retention_capacity=2)
    runtime.submit("cancelled", "scan_scene", {})
    runtime.cancel("cancelled")
    runtime.submit("timed-out", "scan_scene", {}, timeout_ms=1)
    clock["monotonic"] = 1.001
    assert runtime.get("timed-out")["status"] == "timed_out"

    runtime.submit("new", "scan_scene", {})

    assert runtime.get("cancelled") is None
    assert runtime.get("timed-out")["status"] == "timed_out"
    assert runtime.snapshot()["evicted_total"] == 1


def test_cancel_start_race_has_one_safe_terminal_outcome():
    for index in range(50):
        runtime = job_runtime.JobRuntime()
        job_id = f"job-{index}"
        runtime.submit(job_id, "apply_patch", {})
        with ThreadPoolExecutor(max_workers=2) as pool:
            start_future = pool.submit(runtime.start_next)
            cancel_future = pool.submit(runtime.cancel, job_id)
            started = start_future.result()
            cancelled = cancel_future.result()
        if started is not None:
            assert cancelled["cancel_requested"] is True
            with pytest.raises(job_runtime.JobInterruption) as captured:
                runtime.raise_if_interrupted(job_id)
            runtime.mark_interrupted(job_id, captured.value)
        assert runtime.get(job_id)["status"] == "cancelled"


def test_lifecycle_checkpoints_and_versioned_export_are_atomic(monkeypatch):
    clock = iter([20.0, 30.0, 40.0])
    monkeypatch.setattr(job_runtime.time, "time", lambda: next(clock))
    runtime = job_runtime.JobRuntime()
    initial_version, initial_records = runtime.records_with_version()
    assert (initial_version, initial_records) == (0, [])

    runtime.submit("job-1", "apply_patch", {}, created_at=10.0)
    queued_version, queued_records = runtime.records_with_version()
    assert queued_version > initial_version
    assert queued_records[0]["checkpoint"] == "accepted"

    runtime.start_next()
    runtime.set_checkpoint(
        "job-1",
        "transaction_committed",
        {"revision_id": "rev-1", "operation_count": 2},
    )
    committed_version, committed_records = runtime.records_with_version()
    committed = committed_records[0]
    assert committed_version > queued_version
    assert committed["checkpoint"] == "transaction_committed"
    assert committed["transaction_committed"] is True
    assert committed["checkpoint_metadata"] == {
        "revision_id": "rev-1",
        "operation_count": 2,
    }

    runtime.mark_succeeded("job-1", {"ok": True})
    terminal_version, terminal_records = runtime.records_with_version()
    assert terminal_version > committed_version
    assert terminal_records[0]["checkpoint"] == "terminal"
    assert terminal_records[0]["transaction_committed"] is True


def test_transaction_rollback_checkpoint_clears_commit_signal():
    runtime = job_runtime.JobRuntime()
    runtime.submit("job-1", "apply_patch", {})
    runtime.start_next()
    runtime.set_checkpoint("job-1", "transaction_committed", {"revision_id": "rev-1"})
    runtime.set_checkpoint(
        "job-1",
        "transaction_rolled_back",
        {"revision_id": "rev-1", "rolled_back_count": 1},
    )
    job = runtime.get("job-1")
    assert job["transaction_committed"] is False
    assert job["checkpoint"] == "transaction_rolled_back"
    assert job["checkpoint_metadata"] == {
        "revision_id": "rev-1",
        "rolled_back_count": 1,
    }


def test_checkpoint_requires_running_job_and_copies_metadata():
    runtime = job_runtime.JobRuntime()
    runtime.submit("job-1", "apply_patch", {})
    metadata = {"revision_id": "rev-1"}
    with pytest.raises(RuntimeError, match="expected running"):
        runtime.set_checkpoint("job-1", "transaction_committed", metadata)
    runtime.start_next()
    runtime.set_checkpoint("job-1", "transaction_committed", metadata)
    metadata["revision_id"] = "changed"
    assert runtime.get("job-1")["checkpoint_metadata"] == {"revision_id": "rev-1"}


def test_stop_interrupts_all_active_jobs_and_preserves_commit_signal():
    runtime = job_runtime.JobRuntime(pending_capacity=3, retention_capacity=4)
    runtime.submit("running", "apply_patch", {})
    runtime.submit("queued", "scan_scene", {})
    runtime.start_next()
    runtime.set_checkpoint("running", "transaction_committed", {"revision_id": "rev-1"})

    interrupted = runtime.interrupt_active()

    assert {job["job_id"] for job in interrupted} == {"running", "queued"}
    running = runtime.get("running")
    assert running["status"] == "interrupted"
    assert running["error_code"] == "bridge_stopped"
    assert running["retryable"] is True
    assert running["interrupted_from"] == "running"
    assert running["transaction_committed"] is True
    assert running["checkpoint"] == "terminal"
    assert runtime.get("queued")["status"] == "interrupted"
    assert runtime.get("queued")["interrupted_from"] == "queued"
    assert runtime.start_next() is None
    assert runtime.snapshot()["status_counts"]["interrupted"] == 2


def test_interrupted_records_are_terminal_and_prunable():
    runtime = job_runtime.JobRuntime(pending_capacity=2, retention_capacity=1)
    runtime.submit("old", "scan_scene", {})
    runtime.interrupt_active()
    runtime.submit("new", "scan_scene", {})
    assert runtime.get("old") is None
    assert runtime.get("new")["status"] == "queued"


def test_task_center_view_is_a_repeatable_read_without_deadline_or_version_side_effects(
    monkeypatch,
):
    clock = {"monotonic": 10.0}
    monkeypatch.setattr(job_runtime.time, "monotonic", lambda: clock["monotonic"])
    runtime = job_runtime.JobRuntime(pending_capacity=2, retention_capacity=2)
    runtime.submit(
        "job-read-only",
        "scan_scene",
        {"private": "must-not-leak"},
        timeout_ms=1,
        created_at=100.0,
    )
    initial = runtime.task_center_view()
    initial_version = initial["version"]
    clock["monotonic"] = 999.0

    first = runtime.task_center_view()
    second = runtime.task_center_view()

    assert first == second == initial
    assert first["version"] == initial_version
    assert first["runtime"]["pending"] == 1
    assert first["runtime"]["status_counts"]["queued"] == 1
    assert first["jobs"][0]["status"] == "queued"
    assert first["jobs"][0]["queue_position"] == 1
    clock["monotonic"] = 10.0
    assert runtime.start_next()[0] == "job-read-only"


def test_task_center_view_does_not_consult_the_clock(monkeypatch):
    runtime = job_runtime.JobRuntime()
    runtime.submit("job-read-only", "scan_scene", {}, timeout_ms=1)

    def forbidden_clock_read():
        raise AssertionError("task_center_view must not advance deadline state")

    monkeypatch.setattr(job_runtime.time, "monotonic", forbidden_clock_read)

    assert runtime.task_center_view()["jobs"][0]["status"] == "queued"


def test_task_center_view_projects_only_content_free_fields_and_returns_deep_copies():
    secret = "PRIVATE-RUNTIME-CONTENT-19dd"
    runtime = job_runtime.JobRuntime(pending_capacity=3, retention_capacity=6)
    runtime.submit(
        "completed",
        "apply_patch",
        {"payload": secret},
        request_id=f"req-{secret}",
        correlation_id=f"corr-{secret}",
    )
    runtime.start_next()
    runtime.set_checkpoint(
        "completed",
        "transaction_committed",
        {"private_revision": secret},
    )
    runtime.cancel("completed")
    runtime.mark_succeeded("completed", {"result": secret})

    runtime.submit("failed", "scan_scene", {"payload": secret})
    runtime.start_next()
    runtime.mark_failed(
        "failed",
        code="job_failed",
        message=secret,
        details={"private_detail": secret},
    )
    runtime.submit("queued", "undo", {"payload": secret})

    baseline = runtime.task_center_view()
    serialized = json.dumps(baseline, sort_keys=True)

    assert secret not in serialized
    assert baseline["jobs"] == [
        {
            "job_id": "completed",
            "type": "apply_patch",
            "status": "succeeded",
            "checkpoint": "terminal",
            "cancel_requested": True,
            "completed_after_cancel_request": True,
        },
        {
            "job_id": "failed",
            "type": "scan_scene",
            "status": "failed",
            "checkpoint": "terminal",
            "error_code": "job_failed",
        },
        {
            "job_id": "queued",
            "type": "undo",
            "status": "queued",
            "checkpoint": "accepted",
            "queue_position": 1,
        },
    ]
    forbidden_fields = {
        "payload",
        "result",
        "submission_request_id",
        "job_correlation_id",
        "request_id",
        "correlation_id",
        "token",
        "path",
        "error",
        "details",
        "checkpoint_metadata",
        "created_at",
        "started_at",
        "finished_at",
        "deadline_at",
    }
    assert all(forbidden_fields.isdisjoint(job) for job in baseline["jobs"])

    baseline["runtime"]["status_counts"]["queued"] = 999
    baseline["runtime"]["pending"] = 999
    baseline["jobs"][0]["status"] = "poisoned"
    baseline["jobs"].append({"job_id": secret})

    reread = runtime.task_center_view()
    assert reread["runtime"]["status_counts"]["queued"] == 1
    assert reread["runtime"]["pending"] == 1
    assert reread["jobs"][0]["status"] == "succeeded"
    assert len(reread["jobs"]) == 3
    assert secret not in json.dumps(reread, sort_keys=True)


def test_task_center_view_capacity_boundary_and_queue_positions_are_exact():
    runtime = job_runtime.JobRuntime(pending_capacity=6, retention_capacity=8)
    for index in range(1, 7):
        runtime.submit(f"job-{index}", "scan_scene", {"index": index})

    full = runtime.task_center_view()

    assert full["runtime"] == {
        "pending": 6,
        "pending_capacity": 6,
        "retained": 6,
        "retention_capacity": 8,
        "status_counts": {
            "queued": 6,
            "running": 0,
            "succeeded": 0,
            "failed": 0,
            "cancelled": 0,
            "timed_out": 0,
            "interrupted": 0,
        },
        "cancel_requested": 0,
        "evicted_total": 0,
    }
    assert [job["queue_position"] for job in full["jobs"]] == list(range(1, 7))
    full_version = full["version"]

    with pytest.raises(job_runtime.JobAdmissionError) as captured:
        runtime.submit("job-7", "scan_scene", {})
    assert captured.value.code == "queue_full"
    assert runtime.task_center_view() == full
    assert runtime.task_center_view()["version"] == full_version

    assert runtime.start_next()[0] == "job-1"
    shifted = runtime.task_center_view()
    assert "queue_position" not in shifted["jobs"][0]
    assert [job["queue_position"] for job in shifted["jobs"][1:]] == list(range(1, 6))

    runtime.submit("job-7", "scan_scene", {})
    refilled = runtime.task_center_view()
    queued = [job for job in refilled["jobs"] if job["status"] == "queued"]
    assert [job["queue_position"] for job in queued] == list(range(1, 7))
    assert refilled["runtime"]["pending"] == refilled["runtime"]["pending_capacity"] == 6
