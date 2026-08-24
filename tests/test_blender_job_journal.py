import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "blender_extension" / "facelink" / "job_journal.py"
)
SPEC = importlib.util.spec_from_file_location("facelink_blender_job_journal", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
job_journal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(job_journal)


def _journal(path, session_id, *, workspace_id="blend-workspace", persistent=True):
    return job_journal.JobJournal(
        path,
        workspace_id=workspace_id,
        persistent_workspace=persistent,
        session_id=session_id,
    )


def _running_job(**changes):
    record = {
        "job_id": "job-1",
        "type": "apply_patch",
        "status": "running",
        "created_at": 10.0,
        "started_at": 11.0,
        "checkpoint": "main_thread_started",
        "checkpoint_at": 11.0,
    }
    record.update(changes)
    return record


def test_workspace_identity_is_stable_and_does_not_disclose_saved_path(tmp_path):
    blend_path = tmp_path / "Private Client" / "secret-project.blend"
    first = job_journal.workspace_identity(str(blend_path), 101)
    second = job_journal.workspace_identity(str(blend_path), 999)
    assert first == second
    assert first[0].startswith("blend-")
    assert len(first[0]) == len("blend-") + 24
    assert "private" not in first[0].lower()
    assert first[1] is True


def test_unsaved_workspace_is_process_scoped_and_reports_limitation():
    workspace_id, persistent = job_journal.workspace_identity("", 321)
    assert (workspace_id, persistent) == ("unsaved-321", False)


def test_start_creates_atomic_bounded_journal_without_sensitive_job_fields(tmp_path):
    path = tmp_path / "journal.json"
    journal = _journal(path, "session-1")
    status = journal.start()
    assert status["required"] is False

    jobs = []
    for index in range(300):
        jobs.append(
            _running_job(
                job_id=f"job-{index}",
                payload={"prompt": "private prompt", "api_key": "sk-secret"},
                result={"scene_snapshot": "private scene"},
                error="private raw error",
                details={"token": "bearer-secret"},
                checkpoint_metadata={
                    "revision_id": f"rev-{index}",
                    "operation_count": index,
                    "scene_path": "E:/private/project.blend",
                },
            )
        )
    journal.persist(jobs)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert len(payload["jobs"]) == job_journal.MAX_JOURNAL_JOBS
    assert payload["jobs"][0]["job_id"] == "job-44"
    encoded = path.read_text(encoding="utf-8")
    for secret in (
        "private prompt",
        "sk-secret",
        "private scene",
        "private raw error",
        "bearer-secret",
        "E:/private/project.blend",
    ):
        assert secret not in encoded
    assert payload["jobs"][-1]["checkpoint_metadata"] == {
        "operation_count": 299,
        "revision_id": "rev-299",
    }
    assert list(tmp_path.glob("*.tmp")) == []


def test_sanitizer_rejects_non_finite_values_and_incomplete_records():
    records = [
        _running_job(created_at=float("nan"), deadline_at=float("inf")),
        {"job_id": "missing-type", "status": "queued"},
    ]
    sanitized = job_journal.sanitize_job_records(records)
    assert len(sanitized) == 1
    assert "created_at" not in sanitized[0]
    assert "deadline_at" not in sanitized[0]


def test_sanitizer_preserves_only_safe_submission_correlation_metadata():
    request_id = "req-0123456789abcdef"
    correlation_id = "corr-0123456789abcdef0123456789abcdef"
    records = [
        _running_job(
            submission_request_id=request_id,
            job_correlation_id=correlation_id,
            payload={"prompt": "PRIVATE_PROMPT"},
            result={"api_key": "PRIVATE_KEY"},
            details={"authorization": "PRIVATE_BEARER"},
        )
    ]

    sanitized = job_journal.sanitize_job_records(records)

    assert sanitized[0]["submission_request_id"] == request_id
    assert sanitized[0]["job_correlation_id"] == correlation_id
    encoded = json.dumps(sanitized)
    assert "PRIVATE_PROMPT" not in encoded
    assert "PRIVATE_KEY" not in encoded
    assert "PRIVATE_BEARER" not in encoded


def test_unclean_restart_reports_jobs_without_replaying_them(tmp_path):
    path = tmp_path / "journal.json"
    previous = _journal(path, "previous")
    previous.start()
    previous.persist(
        [
            _running_job(),
            _running_job(
                job_id="job-committed",
                checkpoint="transaction_committed",
                transaction_committed=True,
                checkpoint_metadata={"revision_id": "rev-42", "operation_count": 3},
            ),
        ]
    )

    current = _journal(path, "current")
    recovery = current.start()

    assert recovery["required"] is True
    assert recovery["reason"] == "interrupted_jobs"
    assert recovery["previous_session_id"] == "previous"
    assert recovery["unclean_shutdown"] is True
    assert len(recovery["interrupted_jobs"]) == 2
    recovered = {item["job_id"]: item for item in recovery["interrupted_jobs"]}
    assert recovered["job-1"]["guidance_code"] == "rescan_and_inspect_scene"
    assert recovered["job-1"]["may_have_committed_changes"] is False
    assert recovered["job-1"]["may_have_partial_changes"] is True
    assert recovered["job-committed"]["guidance_code"] == "inspect_revision_history"
    assert recovered["job-committed"]["may_have_committed_changes"] is True
    assert recovered["job-committed"]["may_have_partial_changes"] is False
    assert json.loads(path.read_text(encoding="utf-8"))["jobs"] == []


def test_submission_correlation_ids_survive_persistence_and_recovery(tmp_path):
    path = tmp_path / "journal.json"
    request_id = "req-fedcba9876543210"
    correlation_id = "corr-fedcba9876543210fedcba9876543210"
    previous = _journal(path, "previous")
    previous.start()
    previous.persist(
        [
            _running_job(
                submission_request_id=request_id,
                job_correlation_id=correlation_id,
                payload={"secret": "must-not-persist"},
            )
        ]
    )

    persisted = json.loads(path.read_text(encoding="utf-8"))["jobs"][0]
    assert persisted["submission_request_id"] == request_id
    assert persisted["job_correlation_id"] == correlation_id
    assert "must-not-persist" not in path.read_text(encoding="utf-8")

    recovery = _journal(path, "current").start()

    recovered = recovery["interrupted_jobs"][0]
    assert recovered["submission_request_id"] == request_id
    assert recovered["job_correlation_id"] == correlation_id


def test_clean_shutdown_without_active_work_requires_no_recovery(tmp_path):
    path = tmp_path / "journal.json"
    previous = _journal(path, "previous")
    previous.start()
    previous.persist(
        [_running_job(status="succeeded", finished_at=12.0, checkpoint="terminal")],
        clean_shutdown=True,
    )
    recovery = _journal(path, "current").start()
    assert recovery["required"] is False
    assert recovery["reason"] == "none"
    assert recovery["unclean_shutdown"] is False


def test_queued_mutating_job_has_not_reached_partial_scene_changes(tmp_path):
    path = tmp_path / "journal.json"
    previous = _journal(path, "previous")
    previous.start()
    previous.persist(
        [
            _running_job(
                status="queued",
                checkpoint="accepted",
                checkpoint_at=10.0,
            )
        ]
    )
    recovery = _journal(path, "current").start()["interrupted_jobs"][0]
    assert recovery["previous_status"] == "queued"
    assert recovery["may_have_partial_changes"] is False
    assert recovery["guidance_code"] == "rescan_and_replan"


def test_clean_stop_with_interrupted_work_still_requires_explicit_recovery(tmp_path):
    path = tmp_path / "journal.json"
    previous = _journal(path, "previous")
    previous.start()
    previous.persist(
        [
            _running_job(
                status="interrupted",
                error_code="bridge_stopped",
                finished_at=12.0,
                checkpoint="terminal",
                transaction_committed=True,
            )
        ],
        clean_shutdown=True,
    )
    recovery = _journal(path, "current").start()
    assert recovery["required"] is True
    assert recovery["unclean_shutdown"] is False
    assert recovery["interrupted_jobs"][0]["may_have_committed_changes"] is True


def test_acknowledgement_is_persisted_and_not_carried_to_later_sessions(tmp_path):
    path = tmp_path / "journal.json"
    previous = _journal(path, "previous")
    previous.start()
    previous.persist([_running_job()])
    current = _journal(path, "current")
    assert current.start()["required"] is True

    acknowledged = current.acknowledge()
    assert acknowledged["required"] is False
    assert acknowledged["acknowledged"] is True
    current.persist([], clean_shutdown=True)
    later = _journal(path, "later").start()
    assert later["required"] is False


def test_corrupt_journal_is_quarantined_and_replaced(tmp_path):
    path = tmp_path / "journal.json"
    path.write_text('{"broken":', encoding="utf-8")
    recovery = _journal(path, "current").start()
    assert recovery["required"] is True
    assert recovery["reason"] == "journal_corrupt"
    assert recovery["error_code"] == "recovery_journal_corrupt"
    quarantined = list(tmp_path.glob("journal.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == '{"broken":'
    assert json.loads(path.read_text(encoding="utf-8"))["session_id"] == "current"


def test_future_journal_schema_is_not_quarantined_or_overwritten(tmp_path):
    path = tmp_path / "journal.json"
    original = (
        b'{"schema_version":"2.0","workspace_id":"blend-workspace",'
        b'"jobs":[],"future_private_field":"preserve-exactly"}\r\n'
    )
    path.write_bytes(original)
    journal = _journal(path, "current")

    with pytest.raises(
        job_journal.UnsupportedJournalVersion, match="Unsupported"
    ) as captured:
        journal.start()

    assert captured.value.requested == "2.0"
    assert path.read_bytes() == original
    assert list(tmp_path.glob("journal.json.corrupt-*")) == []
    assert list(tmp_path.glob("*.tmp")) == []
    with pytest.raises(job_journal.JournalError, match="has not been started"):
        journal.persist([])


def test_valid_older_unknown_journal_schema_is_preserved_for_explicit_migration(tmp_path):
    path = tmp_path / "journal.json"
    original = (
        b'{"schema_version":"0.9","workspace_id":"blend-workspace","jobs":[]}\n'
    )
    path.write_bytes(original)

    with pytest.raises(job_journal.UnsupportedJournalVersion) as captured:
        _journal(path, "current").start()

    assert captured.value.requested == "0.9"
    assert path.read_bytes() == original
    assert list(tmp_path.glob("journal.json.corrupt-*")) == []


@pytest.mark.parametrize(
    "schema_value",
    ["__missing__", None, [], {}, "garbage", "1", "01.0", "1.00", "1234567890.0"],
)
def test_missing_or_malformed_journal_schema_is_quarantined_as_corrupt(
    tmp_path, schema_value
):
    path = tmp_path / "journal.json"
    payload = {
        "workspace_id": "blend-workspace",
        "jobs": [],
    }
    if schema_value != "__missing__":
        payload["schema_version"] = schema_value
    original = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    path.write_bytes(original)

    recovery = _journal(path, "current").start()

    assert recovery["reason"] == "journal_corrupt"
    quarantined = list(tmp_path.glob("journal.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == original
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == "1.0"


def test_carried_recovery_is_resanitized_before_a_new_session(tmp_path):
    path = tmp_path / "journal.json"
    previous = _journal(path, "previous")
    previous.start()
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["recovery"] = {
        "required": True,
        "interrupted_jobs": [
            _running_job(
                status="interrupted",
                payload={"prompt": "CARRIED_PRIVATE_PROMPT"},
                result={"token": "CARRIED_PRIVATE_TOKEN"},
            )
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    recovery = _journal(path, "current").start()

    assert recovery["required"] is True
    assert recovery["interrupted_jobs"][0]["job_id"] == "job-1"
    current_text = path.read_text(encoding="utf-8")
    assert "CARRIED_PRIVATE_PROMPT" not in current_text
    assert "CARRIED_PRIVATE_TOKEN" not in current_text


def test_failed_atomic_replace_retains_last_good_file_and_memory(monkeypatch, tmp_path):
    path = tmp_path / "journal.json"
    journal = _journal(path, "session-1")
    journal.start()
    original = path.read_bytes()

    def fail_replace(_source, _target):
        raise OSError("disk unavailable")

    monkeypatch.setattr(job_journal.os, "replace", fail_replace)
    with pytest.raises(job_journal.JournalError, match="Could not persist"):
        journal.persist([_running_job()])

    assert path.read_bytes() == original
    assert json.loads(path.read_text(encoding="utf-8"))["jobs"] == []
    assert list(tmp_path.glob("*.tmp")) == []


def test_concurrent_persistence_always_leaves_valid_complete_json(tmp_path):
    path = tmp_path / "journal.json"
    journal = _journal(path, "session-1")
    journal.start()

    def persist(index):
        journal.persist([_running_job(job_id=f"job-{index}")])

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(persist, range(50)))

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert len(payload["jobs"]) == 1
    assert payload["jobs"][0]["job_id"].startswith("job-")
    assert list(tmp_path.glob("*.tmp")) == []
