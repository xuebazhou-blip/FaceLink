import importlib.util
import json
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "blender_extension"
    / "facelink"
    / "product_status.py"
)
SPEC = importlib.util.spec_from_file_location("facelink_blender_product_status", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
product_status = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(product_status)


def _job(index, status, *, job_type="scan_scene", **extra):
    return {
        "job_id": f"job-{index:016x}",
        "type": job_type,
        "status": status,
        **extra,
    }


def _keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


@pytest.mark.parametrize(
    ("overrides", "expected_state"),
    [
        (
            {
                "running": False,
                "storage_error": True,
                "recovery": {"required": True, "reason": "interrupted_jobs"},
                "runtime": {"pending": 2},
                "records": [_job(1, "running")],
            },
            "stopped",
        ),
        ({"running": False, "startup_error_code": "bridge_start_failed"}, "error"),
        (
            {
                "running": False,
                "startup_error_code": "unsupported_journal_version",
                "startup_error_version": "2.0",
            },
            "incompatible",
        ),
        (
            {
                "running": True,
                "storage_error": True,
                "recovery": {"required": True, "reason": "interrupted_jobs"},
                "runtime": {"pending": 2},
                "records": [_job(1, "running")],
            },
            "error",
        ),
        (
            {
                "running": True,
                "recovery": {"required": True, "reason": "interrupted_jobs"},
                "runtime": {"pending": 2},
                "records": [_job(1, "running")],
            },
            "recovery_required",
        ),
        ({"running": True, "records": [_job(1, "running")]}, "busy"),
        ({"running": True, "runtime": {"pending": 1}}, "busy"),
        (
            {"running": True, "runtime": {"status_counts": {"running": 1}}},
            "busy",
        ),
        (
            {"running": True, "records": [_job(1, "succeeded")], "staged": True},
            "ready",
        ),
    ],
)
def test_connection_state_priority_is_deterministic(overrides, expected_state):
    status = product_status.build_product_status(**overrides)

    assert status["connection"]["state"] == expected_state


def test_ready_connection_exposes_only_strict_valid_identity_and_protocol():
    status = product_status.build_product_status(
        running=True,
        instance_id="blender-12345678901234567890",
        protocol_version="999999999.999999999",
    )

    assert status["connection"] == {
        "running": True,
        "state": "ready",
        "instance_id": "blender-12345678901234567890",
        "protocol_version": "999999999.999999999",
    }


@pytest.mark.parametrize(
    "instance_id",
    [
        True,
        1,
        [],
        "blender-0",
        "blender-01",
        "blender-123456789012345678901",
        "blender-1/private-project",
        "BLENDER-1",
        " blender-1",
    ],
)
def test_connection_rejects_poisoned_or_malformed_instance_id(instance_id):
    marker = "private-project"
    status = product_status.build_product_status(
        running=True,
        instance_id=instance_id,
        protocol_version="1.13",
    )

    assert "instance_id" not in status["connection"]
    assert marker not in json.dumps(status, sort_keys=True)


@pytest.mark.parametrize(
    "protocol_version",
    [
        True,
        1.13,
        [],
        "1",
        "1.13.0",
        "01.13",
        "1.013",
        "1000000000.1",
        "1.1000000000",
        "1.13/private-project",
        " 1.13",
    ],
)
def test_connection_rejects_poisoned_or_malformed_protocol_version(protocol_version):
    status = product_status.build_product_status(
        running=True,
        instance_id="blender-1",
        protocol_version=protocol_version,
    )

    assert "protocol_version" not in status["connection"]
    assert "private-project" not in json.dumps(status, sort_keys=True)


@pytest.mark.parametrize(
    "startup_error_code",
    [True, 7, [], "", "unsupported-journal-version", "private-startup-error"],
)
def test_unknown_startup_error_is_not_exposed_or_promoted_to_error(startup_error_code):
    status = product_status.build_product_status(
        running=False,
        startup_error_code=startup_error_code,
        startup_error_version="2.0",
    )

    assert status["connection"] == {"running": False, "state": "stopped"}
    assert "private-startup-error" not in json.dumps(status, sort_keys=True)


@pytest.mark.parametrize("transition_code", ["file_loaded", "workspace_changed"])
def test_stopped_connection_projects_only_known_file_transition(transition_code):
    status = product_status.build_product_status(
        running=False,
        transition_code=transition_code,
    )

    assert status["connection"] == {
        "running": False,
        "state": "stopped",
        "notice_code": transition_code,
    }


@pytest.mark.parametrize("transition_code", [True, 7, [], "", "private-project"])
def test_unknown_file_transition_is_not_exposed(transition_code):
    status = product_status.build_product_status(
        running=False,
        transition_code=transition_code,
    )

    assert status["connection"] == {"running": False, "state": "stopped"}
    assert "private-project" not in json.dumps(status, sort_keys=True)


def test_file_transition_is_hidden_while_running_or_reporting_startup_error():
    running = product_status.build_product_status(
        running=True,
        transition_code="workspace_changed",
    )
    failed = product_status.build_product_status(
        running=False,
        transition_code="workspace_changed",
        startup_error_code="bridge_start_failed",
    )

    assert "notice_code" not in running["connection"]
    assert "notice_code" not in failed["connection"]


@pytest.mark.parametrize(
    "startup_error_version",
    [True, 2, [], "2", "2.0.0", "02.0", "2.00", "private-version", "2.0/private"],
)
def test_incompatible_state_never_exposes_unvalidated_requested_version(
    startup_error_version,
):
    status = product_status.build_product_status(
        running=False,
        startup_error_code="unsupported_journal_version",
        startup_error_version=startup_error_version,
    )

    assert status["connection"] == {
        "running": False,
        "state": "incompatible",
        "error_code": "unsupported_journal_version",
    }
    assert "private-version" not in json.dumps(status, sort_keys=True)


def test_active_and_recent_job_windows_are_bounded_and_ordered():
    records = [
        _job(1, "queued", checkpoint="accepted", queue_position=1),
        _job(2, "queued", checkpoint="accepted", queue_position=2),
        _job(3, "running", checkpoint="main_thread_started"),
        _job(4, "queued", checkpoint="accepted", queue_position=3),
        _job(5, "running", checkpoint="transaction_committed"),
        _job(6, "running", checkpoint="transaction_rolled_back"),
        _job(7, "queued", checkpoint="accepted", queue_position=4),
        _job(8, "running", checkpoint="main_thread_started"),
        _job(9, "succeeded", checkpoint="terminal"),
        _job(10, "failed", checkpoint="terminal", error_code="job_rejected"),
        _job(11, "cancelled", checkpoint="terminal", error_code="job_cancelled"),
        _job(12, "timed_out", checkpoint="terminal", error_code="job_timed_out"),
        _job(13, "interrupted", checkpoint="terminal", error_code="job_interrupted"),
    ]

    status = product_status.build_product_status(running=True, records=records)

    assert status["active_job_count"] == 8
    assert status["hidden_active_job_count"] == 2
    assert [job["job_id"] for job in status["active_jobs"]] == [
        _job(index, "running")["job_id"] for index in (3, 5, 6, 8, 1, 2)
    ]
    assert status["active_jobs"][4]["queue_position"] == 1
    assert status["active_jobs"][5]["queue_position"] == 2
    assert [job["job_id"] for job in status["recent_jobs"]] == [
        _job(index, "succeeded")["job_id"] for index in (13, 12, 11)
    ]
    assert [job["status"] for job in status["recent_jobs"]] == [
        "interrupted",
        "timed_out",
        "cancelled",
    ]


def test_checkpoint_and_queue_position_are_strictly_projected():
    records = [
        _job(1, "queued", checkpoint="accepted", queue_position=1),
        _job(2, "queued", checkpoint="private-checkpoint", queue_position=0),
        _job(3, "queued", checkpoint="terminal", queue_position=128),
        _job(4, "queued", checkpoint="accepted", queue_position=129),
        _job(5, "queued", checkpoint="accepted", queue_position=True),
    ]

    status = product_status.build_product_status(running=True, records=records)

    assert status["active_jobs"] == [
        {
            "job_id": _job(1, "queued")["job_id"],
            "type": "scan_scene",
            "status": "queued",
            "checkpoint": "accepted",
            "queue_position": 1,
        },
        {
            "job_id": _job(2, "queued")["job_id"],
            "type": "scan_scene",
            "status": "queued",
        },
        {
            "job_id": _job(3, "queued")["job_id"],
            "type": "scan_scene",
            "status": "queued",
            "checkpoint": "terminal",
            "queue_position": 128,
        },
        {
            "job_id": _job(4, "queued")["job_id"],
            "type": "scan_scene",
            "status": "queued",
            "checkpoint": "accepted",
        },
        {
            "job_id": _job(5, "queued")["job_id"],
            "type": "scan_scene",
            "status": "queued",
            "checkpoint": "accepted",
        },
    ]
    assert "private-checkpoint" not in json.dumps(status, sort_keys=True)


def test_successful_completion_wins_and_preserves_only_safe_race_flags():
    status = product_status.build_product_status(
        running=True,
        records=[
            _job(
                1,
                "succeeded",
                checkpoint="terminal",
                cancel_requested=True,
                completed_after_cancel_request=True,
                completed_after_deadline=True,
                error_code="job_cancelled",
                error="private cancellation detail",
            )
        ],
    )

    assert status["recent_jobs"] == [
        {
            "job_id": _job(1, "succeeded")["job_id"],
            "type": "scan_scene",
            "status": "succeeded",
            "checkpoint": "terminal",
            "completed_after_cancel_request": True,
            "completed_after_deadline": True,
        }
    ]
    assert "private cancellation detail" not in json.dumps(status, sort_keys=True)


def test_product_status_never_contains_private_job_or_transport_content():
    poison = "PRIVATE-MARKER-a7f31c"
    records = [
        _job(
            1,
            "running",
            checkpoint="main_thread_started",
            payload={"scene_name": poison},
            result={"object_name": poison},
            submission_request_id=f"req-{poison}",
            job_correlation_id=f"corr-{poison}",
            token=poison,
            path=f"C:/Users/private/{poison}.blend",
            error=poison,
            details={"raw": poison},
            checkpoint_metadata={"revision": poison},
        ),
        _job(
            2,
            "failed",
            checkpoint="terminal",
            error_code=poison,
            error=poison,
            result=poison,
        ),
    ]

    status = product_status.build_product_status(
        running=True,
        records=records,
        runtime={"pending": 1, "payload": poison, "token": poison},
        recovery={"required": False, "path": poison, "raw_error": poison},
    )
    serialized = json.dumps(status, sort_keys=True)

    assert poison not in serialized
    assert set(_keys(status)).isdisjoint(
        {
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
            "raw_error",
        }
    )
    assert status["recent_jobs"][0]["error_code"] == "job_failed"


def test_product_status_counts_and_recovery_metadata_are_bounded():
    status = product_status.build_product_status(
        running=True,
        runtime={
            "pending": -1,
            "pending_capacity": 1_000_001,
            "status_counts": {"running": True},
            "cancel_requested": 1_000_000,
        },
        recovery={
            "required": True,
            "reason": "private-reason",
            "interrupted_jobs": [object()] * 300,
            "persistent_workspace": True,
        },
    )

    assert status["queue"] == {
        "pending": 0,
        "pending_capacity": 0,
        "running": 0,
        "cancel_requested": 1_000_000,
    }
    assert status["recovery"] == {
        "required": True,
        "reason": "unknown",
        "interrupted_job_count": 256,
        "persistent_workspace": True,
    }
    assert "private-reason" not in json.dumps(status, sort_keys=True)
