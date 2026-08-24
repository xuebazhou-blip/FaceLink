from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from facelink import diagnostic_export
from facelink.bridge_client import BridgeError
from facelink.diagnostic_export import DiagnosticExportError

DIAGNOSTIC_ID = "diag-" + "d" * 32
JOB_ID = "job-" + "a" * 32
REQUEST_ID = "req-" + "b" * 32
CORRELATION_ID = "corr-" + "c" * 32
EVENT_REQUEST_ID = "req-" + "e" * 32


def _openai_key() -> str:
    return "sk-" + "sensitivevalue" * 2


def _github_token() -> str:
    return "ghp_" + "a" * 24


def _aws_key() -> str:
    return "AKIA" + "B" * 16


def _poisoned_doctor_report() -> dict:
    return {
        "ok": False,
        "facelink_version": "0.3.9",
        "checks": [
            {
                "check_id": "blender_bridge",
                "status": "warning",
                "summary": "C:\\Users\\PrivateUser\\bridge.json",
                "details": {"api_key": _openai_key(), "scene_name": "绝密场景"},
            },
            {
                "check_id": "invalid/id",
                "status": "pass",
                "summary": _github_token(),
            },
            {"check_id": "provider", "status": "unexpected", "email": "me@example.test"},
            "not-an-object",
        ],
        "blender": {
            "installations": [
                {
                    "available": True,
                    "supported": True,
                    "version": "4.5.12 LTS",
                    "executable": "C:\\Users\\PrivateUser\\Blender\\blender.exe",
                    "probe_output": _github_token(),
                },
                {
                    "available": False,
                    "supported": False,
                    "version": "bad/version",
                    "path": "/home/private/blender",
                },
            ]
        },
        "bridge": {
            "stale_record_count": 2,
            "instances": [
                {
                    "scene_name": "绝密场景",
                    "token": _openai_key(),
                    "file_path": "C:\\Users\\PrivateUser\\instance.json",
                }
            ],
        },
        "provider": {
            "openai_api_key_configured": True,
            "api_key": _openai_key(),
            "account_email": "me@example.test",
        },
        "readiness": {"mcp": True, "byok": False, "private_note": "绝密"},
        "environment": {"username": "PrivateUser", "home": "/home/private"},
    }


def _poisoned_bridge_payload() -> dict:
    return {
        "protocol_version": "1.13",
        "facelink_version": "0.3.9",
        "blender_version": "4.5.12 LTS",
        "capabilities": [
            "correlation_ids",
            "correlation_ids",
            "bad/capability",
            "PrivateProject",
            _openai_key(),
        ],
        "scene_name": "绝密场景",
        "file_path": "C:\\Users\\PrivateUser\\secret.blend",
        "token": _github_token(),
        "job_runtime": {
            "pending": 1,
            "pending_capacity": 8,
            "retained": 2,
            "retention_capacity": 64,
            "cancel_requested": 0,
            "evicted_total": 3,
            "status_counts": {"queued": 1, "succeeded": 1, "private": 99},
            "payload": {"prompt": "绝密场景", "api_key": _openai_key()},
        },
        "recovery": {
            "required": True,
            "reason": "unclean_shutdown",
            "unclean_shutdown": True,
            "interrupted_job_count": 1,
            "persistent_workspace": True,
            "journal_path": "/home/private/jobs.json",
        },
        "recent_jobs": [
            {
                "job_id": JOB_ID,
                "type": "apply_patch",
                "status": "failed",
                "submission_request_id": REQUEST_ID,
                "job_correlation_id": CORRELATION_ID,
                "error_code": "unsupported_schema_version",
                "checkpoint": "validated",
                "interrupted_from": "running",
                "created_at": 10.5,
                "started_at": 11,
                "finished_at": 12,
                "checkpoint_at": 11.5,
                "retryable": False,
                "transaction_committed": False,
                "payload": {"scene_name": "绝密场景", "api_key": _openai_key()},
                "result": {"objects": ["PrivateObject"]},
                "error": f"C:\\Users\\PrivateUser\\scene.blend {_github_token()}",
            },
            {
                "job_id": "private-scene-name-with-hyphens",
                "type": "apply_patch",
                "status": "failed",
                "payload": _openai_key(),
            },
        ],
        "events": {
            "capacity": 256,
            "retained": 999,
            "evicted_total": 7,
            "raw_log": "me@example.test used /home/private/file.blend",
            "events": [
                {
                    "request_id": EVENT_REQUEST_ID,
                    "correlation_id": CORRELATION_ID,
                    "method": "POST",
                    "route": "/v1/jobs",
                    "http_status": 202,
                    "timestamp": 13.25,
                    "error_code": "none",
                    "job_type": "apply_patch",
                    "job_status": "queued",
                    "job_id": JOB_ID,
                    "request_body": {"prompt": "绝密场景"},
                    "authorization": "Bearer " + _openai_key(),
                },
                {
                    "request_id": "invalid",
                    "correlation_id": CORRELATION_ID,
                    "method": "POST",
                    "route": "/v1/jobs",
                    "http_status": 202,
                    "secret": _aws_key(),
                },
            ],
        },
    }


def _safe_reports() -> tuple[dict, list[dict]]:
    return (
        {"ok": True, "facelink_version": "0.3.9", "checks": [], "readiness": {}},
        [],
    )


def _bounded_id(prefix: str, value: int, *, width: int = 32) -> str:
    return f"{prefix}-{value:0{width}x}"


def _safe_job_record(value: int) -> dict:
    return {
        "job_id": _bounded_id("job", value),
        "type": "apply_patch",
        "status": "failed",
        "submission_request_id": _bounded_id("req", value),
        "job_correlation_id": _bounded_id("corr", value),
        "error_code": "job_failed",
        "checkpoint": "terminal",
        "interrupted_from": "running",
        "created_at": value,
        "started_at": value,
        "finished_at": value,
        "checkpoint_at": value,
        "retryable": False,
        "transaction_committed": False,
        "payload": {"api_key": _openai_key(), "scene_name": "绝密场景"},
        "result": {"path": "C:\\Users\\PrivateUser\\secret.blend"},
    }


def _safe_event_record(value: int) -> dict:
    return {
        "request_id": _bounded_id("req", value),
        "correlation_id": _bounded_id("corr", value),
        "method": "POST",
        "route": "/v1/jobs/{id}",
        "http_status": 500,
        "timestamp": value,
        "error_code": "job_failed",
        "job_type": "apply_patch",
        "job_status": "failed",
        "job_id": _bounded_id("job", value),
        "authorization": "Bearer " + _openai_key(),
        "request_body": {"scene_name": "绝密场景"},
    }


def _dense_bridge_report(
    bridge_index: int,
    *,
    job_count: int = 65,
    event_count: int = 257,
) -> dict:
    base = bridge_index * 10_000
    return {
        "instance_ref": _bounded_id("instance", bridge_index, width=16),
        "status": "collected",
        "diagnostics": {
            "protocol_version": "1.13",
            "facelink_version": "0.3.9",
            "blender_version": "4.5.12 LTS",
            "capabilities": ["sanitized_diagnostics", "correlation_ids"],
            "recent_jobs": [
                _safe_job_record(base + offset) for offset in range(job_count)
            ],
            "events": {
                "capacity": 256,
                "evicted_total": 1,
                "events": [
                    _safe_event_record(base + offset)
                    for offset in range(event_count)
                ],
            },
            "token": _github_token(),
            "scene_name": "绝密场景",
            "file_path": "C:\\Users\\PrivateUser\\secret.blend",
        },
    }


def _encode_payload(payload: dict) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def test_sanitize_doctor_report_is_a_strict_allowlist(monkeypatch):
    monkeypatch.setattr(diagnostic_export.platform, "python_version", lambda: "3.11.9")
    monkeypatch.setattr(diagnostic_export.platform, "system", lambda: "Windows")

    sanitized = diagnostic_export.sanitize_doctor_report(_poisoned_doctor_report())

    assert sanitized == {
        "facelink_version": "0.3.9",
        "python_version": "3.11.9",
        "operating_system": "Windows",
        "environment_ok": False,
        "readiness": {"mcp": True, "byok": False},
        "checks": [{"check_id": "blender_bridge", "status": "warning"}],
        "blender_installations": [
            {"available": True, "supported": True, "version": "4.5.12 LTS"},
            {"available": False, "supported": False},
        ],
        "stale_bridge_records": 2,
        "unprobed_bridge_records": 0,
        "bridge_probe_limit": 32,
        "openai_api_key_configured": True,
    }
    encoded = json.dumps(sanitized, ensure_ascii=False)
    for forbidden in (
        "PrivateUser",
        "绝密场景",
        "me@example.test",
        _openai_key(),
        _github_token(),
        "/home/private",
    ):
        assert forbidden not in encoded


def test_sanitizers_fail_closed_for_malformed_container_types_and_platform_values(
    monkeypatch,
):
    monkeypatch.setattr(diagnostic_export.platform, "python_version", _openai_key)
    monkeypatch.setattr(
        diagnostic_export.platform,
        "system",
        lambda: "C:\\Users\\PrivateUser",
    )

    doctor = diagnostic_export.sanitize_doctor_report(
        {
            "checks": {"private": _openai_key()},
            "blender": {"installations": "C:\\Users\\PrivateUser"},
        }
    )
    bridge = diagnostic_export.sanitize_bridge_diagnostics(
        {
            "capabilities": _openai_key(),
            "recent_jobs": {"payload": _github_token()},
            "events": {"events": "me@example.test"},
        }
    )

    assert doctor["python_version"] == "unknown"
    assert doctor["operating_system"] == "Other"
    assert doctor["checks"] == []
    assert doctor["blender_installations"] == []
    assert bridge["capabilities"] == []
    assert bridge["recent_jobs"] == []
    assert bridge["events"]["events"] == []
    encoded = json.dumps({"doctor": doctor, "bridge": bridge})
    assert "PrivateUser" not in encoded
    assert _openai_key() not in encoded


@pytest.mark.parametrize("timestamp", [-1, float("nan"), float("inf"), 10**1000])
def test_safe_timestamp_projection_rejects_unbounded_or_non_finite_values(timestamp):
    assert diagnostic_export._safe_timestamp(timestamp) is None


@pytest.mark.parametrize("secret", [_openai_key(), _github_token(), _aws_key()])
def test_safe_code_projection_rejects_credential_shapes(secret):
    assert diagnostic_export._safe_code(secret) is None


def test_safe_identifier_projection_rejects_openai_key_shape():
    assert diagnostic_export._safe_identifier(_openai_key()) is None


def test_semantic_allowlists_reject_safe_shaped_business_text():
    assert (
        diagnostic_export._safe_known_code(
            "PrivateProject", diagnostic_export.SAFE_CAPABILITIES
        )
        is None
    )
    assert diagnostic_export._safe_version("4.5 PrivateClient") is None
    assert diagnostic_export._safe_identifier("project-confidential123") is None


def test_payload_caps_all_nested_collections_and_preserves_recent_order():
    reports = [_dense_bridge_report(index) for index in range(33)]
    reports[-1]["instance_ref"] = "../../invalid-private-tail"
    reports[-1]["tail_secret"] = _openai_key()
    doctor, _ = _safe_reports()

    payload = diagnostic_export.build_diagnostic_payload(
        doctor_report=doctor,
        bridge_reports=reports,
        diagnostic_id=DIAGNOSTIC_ID,
        created_at=0,
    )
    repeated = diagnostic_export.build_diagnostic_payload(
        doctor_report=doctor,
        bridge_reports=reports,
        diagnostic_id=DIAGNOSTIC_ID,
        created_at=0,
    )

    assert payload == repeated
    assert len(payload["bridges"]) == diagnostic_export.MAX_DIAGNOSTIC_INSTANCES
    assert [item["instance_ref"] for item in payload["bridges"]] == [
        _bounded_id("instance", index, width=16)
        for index in range(diagnostic_export.MAX_DIAGNOSTIC_INSTANCES)
    ]
    for bridge_index, item in enumerate(payload["bridges"]):
        diagnostics = item["diagnostics"]
        jobs = diagnostics["recent_jobs"]
        events = diagnostics["events"]["events"]
        base = bridge_index * 10_000
        assert len(jobs) == diagnostic_export.MAX_DIAGNOSTIC_JOBS
        assert [job["job_id"] for job in jobs] == [
            _bounded_id("job", base + offset) for offset in range(1, 65)
        ]
        assert len(events) == diagnostic_export.MAX_DIAGNOSTIC_EVENTS
        assert [event["request_id"] for event in events] == [
            _bounded_id("req", base + offset) for offset in range(1, 257)
        ]

    encoded = _encode_payload(payload)
    assert "invalid-private-tail" not in encoded.decode("utf-8")
    for forbidden in (_openai_key(), _github_token(), "PrivateUser", "绝密场景"):
        assert forbidden.encode("utf-8") not in encoded


def test_invalid_items_in_bounded_recent_tail_are_dropped_without_backfill():
    report = _dense_bridge_report(0)
    diagnostics = report["diagnostics"]
    diagnostics["recent_jobs"].append(
        {
            "job_id": "invalid-private-tail",
            "type": "apply_patch",
            "status": "failed",
            "payload": _openai_key(),
        }
    )
    diagnostics["events"]["events"].append(
        {
            "request_id": "invalid-private-tail",
            "correlation_id": CORRELATION_ID,
            "method": "POST",
            "route": "/v1/jobs",
            "http_status": 202,
            "authorization": "Bearer " + _openai_key(),
        }
    )

    sanitized = diagnostic_export.sanitize_bridge_diagnostics(diagnostics)

    jobs = sanitized["recent_jobs"]
    events = sanitized["events"]["events"]
    assert len(jobs) == diagnostic_export.MAX_DIAGNOSTIC_JOBS - 1
    assert jobs[0]["job_id"] == _bounded_id("job", 2)
    assert jobs[-1]["job_id"] == _bounded_id("job", 64)
    assert len(events) == diagnostic_export.MAX_DIAGNOSTIC_EVENTS - 1
    assert events[0]["request_id"] == _bounded_id("req", 2)
    assert events[-1]["request_id"] == _bounded_id("req", 256)
    encoded = _encode_payload(sanitized)
    assert b"invalid-private-tail" not in encoded
    assert _openai_key().encode("utf-8") not in encoded


def test_sanitize_bridge_diagnostics_drops_scene_content_secrets_and_raw_errors():
    sanitized = diagnostic_export.sanitize_bridge_diagnostics(_poisoned_bridge_payload())

    assert sanitized["capabilities"] == ["correlation_ids"]
    assert sanitized["job_runtime"] == {
        "pending": 1,
        "pending_capacity": 8,
        "retained": 2,
        "retention_capacity": 64,
        "cancel_requested": 0,
        "evicted_total": 3,
        "status_counts": {"queued": 1, "succeeded": 1},
    }
    assert sanitized["recent_jobs"] == [
        {
            "job_id": JOB_ID,
            "type": "apply_patch",
            "status": "failed",
            "submission_request_id": REQUEST_ID,
            "job_correlation_id": CORRELATION_ID,
            "error_code": "unsupported_schema_version",
            "interrupted_from": "running",
            "created_at": 10.5,
            "started_at": 11,
            "finished_at": 12,
            "checkpoint_at": 11.5,
            "retryable": False,
            "transaction_committed": False,
        }
    ]
    assert sanitized["events"]["retained"] == 1
    assert sanitized["events"]["events"][0] == {
        "request_id": EVENT_REQUEST_ID,
        "correlation_id": CORRELATION_ID,
        "method": "POST",
        "route": "/v1/jobs",
        "http_status": 202,
        "timestamp": 13.25,
        "job_type": "apply_patch",
        "job_status": "queued",
        "job_id": JOB_ID,
    }
    encoded = json.dumps(sanitized, ensure_ascii=False)
    for forbidden in (
        "PrivateUser",
        "PrivateObject",
        "绝密场景",
        "me@example.test",
        _openai_key(),
        _github_token(),
        _aws_key(),
        "/home/private",
        "payload",
        "result",
        "raw_log",
    ):
        assert forbidden not in encoded


def test_build_payload_resanitizes_injected_bridge_reports_and_declares_no_upload():
    payload = diagnostic_export.build_diagnostic_payload(
        doctor_report=_poisoned_doctor_report(),
        bridge_reports=[
            {
                "instance_ref": "instance-" + "1" * 16,
                "status": "collected",
                "diagnostics": _poisoned_bridge_payload(),
                "raw": _openai_key(),
            },
            {
                "instance_ref": "instance-" + "2" * 16,
                "status": "unavailable",
                "error_code": "unsupported_protocol_version",
                "exception": "me@example.test",
            },
            {"instance_ref": "../../private", "status": "collected"},
        ],
        diagnostic_id=DIAGNOSTIC_ID,
        created_at=0,
    )

    assert payload["diagnostic_id"] == DIAGNOSTIC_ID
    assert payload["created_at"] == "1970-01-01T00:00:00+00:00"
    assert payload["privacy"] == {
        "policy": "strict_allowlist_v1",
        "contains_credentials": False,
        "contains_scene_content": False,
        "contains_absolute_paths": False,
        "automatic_upload": False,
    }
    assert len(payload["bridges"]) == 2
    assert payload["bridges"][1] == {
        "instance_ref": "instance-" + "2" * 16,
        "status": "unavailable",
        "error_code": "unsupported_protocol_version",
    }
    encoded = json.dumps(payload, ensure_ascii=False)
    assert _openai_key() not in encoded
    assert "me@example.test" not in encoded
    assert "../../private" not in encoded


def test_unavailable_bridge_unknown_safe_shaped_error_is_mapped_to_stable_fallback():
    sanitized = diagnostic_export._sanitize_bridge_report(
        {
            "instance_ref": "instance-" + "3" * 16,
            "status": "unavailable",
            "error_code": "PrivateProjectSecret",
        }
    )

    assert sanitized == {
        "instance_ref": "instance-" + "3" * 16,
        "status": "unavailable",
        "error_code": "diagnostic_unavailable",
    }


@pytest.mark.parametrize(
    "secret",
    [
        pytest.param(_openai_key(), id="openai-key"),
        pytest.param(_github_token(), id="github-token"),
        pytest.param(_aws_key(), id="aws-access-key"),
        pytest.param("Authorization: Bearer opaque-token", id="bearer-header"),
        pytest.param(
            "-----BEGIN " + "PRIVATE KEY-----\nopaque",
            id="private-key",
        ),
        pytest.param("private.user@example.test", id="email"),
        pytest.param("C:\\Users\\PrivateUser\\file.blend", id="windows-user-path"),
        pytest.param("/home/private/file.blend", id="linux-home-path"),
        pytest.param("/Users/private/file.blend", id="macos-home-path"),
        pytest.param("\\\\private-server\\private-share\\file.blend", id="unc-path"),
    ],
)
def test_privacy_scan_rejects_known_credential_and_identity_shapes(secret):
    with pytest.raises(DiagnosticExportError, match="privacy safety scan"):
        diagnostic_export._assert_share_safe(secret.encode("utf-8"))


def test_privacy_scan_rejects_null_interleaved_secret_encoding():
    encoded = _openai_key().encode("utf-16-le")
    with pytest.raises(DiagnosticExportError, match="privacy safety scan"):
        diagnostic_export._assert_share_safe(encoded)


@pytest.mark.parametrize(
    "secret",
    [
        "C:\\Users\\PrivateUser\\file.blend",
        "\\\\private-server\\private-share\\file.blend",
    ],
)
def test_privacy_scan_checks_decoded_json_string_values(secret):
    encoded = json.dumps({"apparently_safe_field": secret}).encode("utf-8")
    with pytest.raises(DiagnosticExportError, match="privacy safety scan"):
        diagnostic_export._assert_share_safe(encoded)


@pytest.mark.parametrize(
    ("diagnostic_id", "created_at"),
    [
        ("../../private", 0),
        ("diag-short", 0),
        (DIAGNOSTIC_ID, True),
        (DIAGNOSTIC_ID, float("nan")),
        (DIAGNOSTIC_ID, -1),
        (DIAGNOSTIC_ID, 4_102_444_801),
    ],
)
def test_build_payload_rejects_invalid_identifier_and_timestamp(diagnostic_id, created_at):
    doctor, bridges = _safe_reports()
    with pytest.raises(DiagnosticExportError):
        diagnostic_export.build_diagnostic_payload(
            doctor_report=doctor,
            bridge_reports=bridges,
            diagnostic_id=diagnostic_id,
            created_at=created_at,
        )


def test_export_is_deterministic_atomic_json_and_never_uploads(tmp_path, monkeypatch):
    doctor, bridges = _safe_reports()
    monkeypatch.setattr(diagnostic_export.platform, "python_version", lambda: "3.11.9")
    monkeypatch.setattr(diagnostic_export.platform, "system", lambda: "Windows")
    first = tmp_path / "first.json"
    second = tmp_path / "second.JSON"

    summary = diagnostic_export.export_diagnostics(
        first,
        doctor_report=doctor,
        bridge_reports=bridges,
        diagnostic_id=DIAGNOSTIC_ID,
        created_at=1_700_000_000,
    )
    second_summary = diagnostic_export.export_diagnostics(
        second,
        doctor_report=doctor,
        bridge_reports=bridges,
        diagnostic_id=DIAGNOSTIC_ID,
        created_at=1_700_000_000,
    )

    assert first.read_bytes() == second.read_bytes()
    assert first.read_bytes().endswith(b"\n")
    assert summary["bytes"] == len(first.read_bytes())
    assert summary["sha256"] == second_summary["sha256"]
    assert summary["path"] == str(first.resolve())
    assert summary["uploaded"] is False
    assert json.loads(first.read_text(encoding="utf-8"))["privacy"]["automatic_upload"] is False
    assert not list(tmp_path.glob(".*.tmp"))


def test_export_of_poisoned_upstream_reports_succeeds_without_private_values(tmp_path):
    output = tmp_path / "diagnostic.json"

    diagnostic_export.export_diagnostics(
        output,
        doctor_report=_poisoned_doctor_report(),
        bridge_reports=[
            {
                "instance_ref": "instance-" + "1" * 16,
                "status": "collected",
                "diagnostics": _poisoned_bridge_payload(),
            }
        ],
        diagnostic_id=DIAGNOSTIC_ID,
        created_at=0,
    )

    encoded = output.read_text(encoding="utf-8")
    for forbidden in (
        "PrivateUser",
        "PrivateObject",
        "绝密场景",
        "me@example.test",
        _openai_key(),
        _github_token(),
        _aws_key(),
        "/home/private",
    ):
        assert forbidden not in encoded


def test_existing_destination_is_not_overwritten_or_probed_by_default(tmp_path, monkeypatch):
    output = tmp_path / "diagnostic.json"
    output.write_bytes(b"original")
    monkeypatch.setattr(
        diagnostic_export,
        "diagnose_environment",
        lambda *_args, **_kwargs: pytest.fail("environment must not be probed"),
    )
    monkeypatch.setattr(
        diagnostic_export,
        "collect_bridge_diagnostics",
        lambda: pytest.fail("bridges must not be contacted"),
    )

    with pytest.raises(DiagnosticExportError, match="already exists"):
        diagnostic_export.export_diagnostics(output)

    assert output.read_bytes() == b"original"
    assert not list(tmp_path.glob(".*.tmp"))


def test_concurrent_destination_creation_is_never_overwritten(tmp_path, monkeypatch):
    doctor, bridges = _safe_reports()
    output = tmp_path / "diagnostic.json"
    original_publish = diagnostic_export._publish_diagnostic_file

    def publish_after_competitor(temporary, destination, *, overwrite):
        destination.write_bytes(b"concurrent-writer")
        return original_publish(temporary, destination, overwrite=overwrite)

    monkeypatch.setattr(
        diagnostic_export,
        "_publish_diagnostic_file",
        publish_after_competitor,
    )

    with pytest.raises(DiagnosticExportError, match="already exists"):
        diagnostic_export.export_diagnostics(
            output,
            doctor_report=doctor,
            bridge_reports=bridges,
            diagnostic_id=DIAGNOSTIC_ID,
            created_at=0,
        )

    assert output.read_bytes() == b"concurrent-writer"
    assert not list(tmp_path.glob(".*.tmp"))


def test_explicit_overwrite_replaces_existing_export(tmp_path):
    doctor, bridges = _safe_reports()
    output = tmp_path / "diagnostic.json"
    output.write_bytes(b"original")

    diagnostic_export.export_diagnostics(
        output,
        overwrite=True,
        doctor_report=doctor,
        bridge_reports=bridges,
        diagnostic_id=DIAGNOSTIC_ID,
        created_at=0,
    )

    assert output.read_bytes() != b"original"
    assert json.loads(output.read_text(encoding="utf-8"))["diagnostic_id"] == DIAGNOSTIC_ID


def test_replace_failure_preserves_old_file_and_removes_temporary(tmp_path, monkeypatch):
    doctor, bridges = _safe_reports()
    output = tmp_path / "diagnostic.json"
    output.write_bytes(b"original")

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(diagnostic_export.os, "replace", fail_replace)

    with pytest.raises(DiagnosticExportError, match="atomically"):
        diagnostic_export.export_diagnostics(
            output,
            overwrite=True,
            doctor_report=doctor,
            bridge_reports=bridges,
            diagnostic_id=DIAGNOSTIC_ID,
            created_at=0,
        )

    assert output.read_bytes() == b"original"
    assert not list(tmp_path.glob(".*.tmp"))


def test_size_limit_fails_before_creating_output_directory(tmp_path, monkeypatch):
    doctor, bridges = _safe_reports()
    output = tmp_path / "not-created" / "diagnostic.json"
    monkeypatch.setattr(diagnostic_export, "MAX_DIAGNOSTIC_BYTES", 1)

    with pytest.raises(DiagnosticExportError, match="1 MiB safety limit"):
        diagnostic_export.export_diagnostics(
            output,
            doctor_report=doctor,
            bridge_reports=bridges,
            diagnostic_id=DIAGNOSTIC_ID,
            created_at=0,
        )

    assert not output.exists()
    assert not output.parent.exists()


def test_real_one_mib_limit_accepts_near_boundary_and_fails_closed_over_it(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(diagnostic_export.platform, "python_version", lambda: "3.11.9")
    monkeypatch.setattr(diagnostic_export.platform, "system", lambda: "Windows")
    doctor, _ = _safe_reports()
    common = [
        _dense_bridge_report(index, job_count=64, event_count=256)
        for index in range(6)
    ]
    under_reports = common + [
        _dense_bridge_report(6, job_count=62, event_count=0)
    ]
    over_reports = common + [
        _dense_bridge_report(6, job_count=63, event_count=0)
    ]
    under_encoded = _encode_payload(
        diagnostic_export.build_diagnostic_payload(
            doctor_report=doctor,
            bridge_reports=under_reports,
            diagnostic_id=DIAGNOSTIC_ID,
            created_at=0,
        )
    )
    over_encoded = _encode_payload(
        diagnostic_export.build_diagnostic_payload(
            doctor_report=doctor,
            bridge_reports=over_reports,
            diagnostic_id=DIAGNOSTIC_ID,
            created_at=0,
        )
    )

    assert diagnostic_export.MAX_DIAGNOSTIC_BYTES == 1024 * 1024
    assert 0 <= diagnostic_export.MAX_DIAGNOSTIC_BYTES - len(under_encoded) < 1024
    assert 0 < len(over_encoded) - diagnostic_export.MAX_DIAGNOSTIC_BYTES < 1024

    under_output = tmp_path / "under" / "diagnostic.json"
    summary = diagnostic_export.export_diagnostics(
        under_output,
        doctor_report=doctor,
        bridge_reports=under_reports,
        diagnostic_id=DIAGNOSTIC_ID,
        created_at=0,
    )
    assert summary["bytes"] == len(under_encoded)
    assert under_output.read_bytes() == under_encoded
    diagnostic_export._assert_share_safe(under_output.read_bytes())
    exported_text = under_output.read_text(encoding="utf-8")
    for forbidden in (_openai_key(), _github_token(), "PrivateUser", "绝密场景"):
        assert forbidden not in exported_text

    over_output = tmp_path / "over" / "diagnostic.json"
    with pytest.raises(DiagnosticExportError, match="1 MiB safety limit"):
        diagnostic_export.export_diagnostics(
            over_output,
            doctor_report=doctor,
            bridge_reports=over_reports,
            diagnostic_id=DIAGNOSTIC_ID,
            created_at=0,
        )
    assert not over_output.exists()
    assert not over_output.parent.exists()


@pytest.mark.parametrize("filename", ["diagnostic", "diagnostic.zip", "diagnostic.json.txt"])
def test_export_requires_json_extension(tmp_path, filename):
    doctor, bridges = _safe_reports()
    with pytest.raises(DiagnosticExportError, match=".json filename"):
        diagnostic_export.export_diagnostics(
            tmp_path / filename,
            doctor_report=doctor,
            bridge_reports=bridges,
        )


def test_injected_reports_skip_environment_auth_and_bridge_collection(tmp_path, monkeypatch):
    doctor, bridges = _safe_reports()
    auth_json = tmp_path / "auth.json"
    auth_json.write_text(json.dumps({"token": _openai_key()}), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(
        diagnostic_export,
        "diagnose_environment",
        lambda *_args, **_kwargs: pytest.fail("must not inspect the environment or auth.json"),
    )
    monkeypatch.setattr(
        diagnostic_export,
        "collect_bridge_diagnostics",
        lambda: pytest.fail("must not perform bridge or network collection"),
    )
    output = tmp_path / "diagnostic.json"

    diagnostic_export.export_diagnostics(
        output,
        doctor_report=doctor,
        bridge_reports=bridges,
        diagnostic_id=DIAGNOSTIC_ID,
        created_at=0,
    )

    assert _openai_key() in auth_json.read_text(encoding="utf-8")
    assert _openai_key() not in output.read_text(encoding="utf-8")


def test_collect_bridge_diagnostics_without_blender_constructs_no_client(monkeypatch):
    monkeypatch.setattr(diagnostic_export, "discover_instances", lambda verify: [])
    monkeypatch.setattr(
        diagnostic_export,
        "BridgeClient",
        lambda _instance: pytest.fail("client must not be constructed"),
    )

    assert diagnostic_export.collect_bridge_diagnostics() == []


def test_collect_bridge_diagnostics_reports_incompatible_instance_without_details(monkeypatch):
    instance = SimpleNamespace(
        instance_id="blender-private-machine-name",
        token=_github_token(),
    )

    class IncompatibleClient:
        def __init__(self, selected):
            assert selected is instance

        def diagnostics(self, timeout):
            assert timeout == diagnostic_export.BRIDGE_DIAGNOSTIC_TIMEOUT
            raise BridgeError(
                f"C:\\Users\\PrivateUser\\bridge {_openai_key()}",
                code="unsupported_protocol_version",
                details={"token": _github_token(), "email": "me@example.test"},
            )

    monkeypatch.setattr(diagnostic_export, "discover_instances", lambda verify: [instance])
    monkeypatch.setattr(diagnostic_export, "BridgeClient", IncompatibleClient)

    reports = diagnostic_export.collect_bridge_diagnostics()

    assert len(reports) == 1
    assert reports[0]["instance_ref"].startswith("instance-")
    assert len(reports[0]["instance_ref"]) == len("instance-") + 16
    assert reports[0]["status"] == "unavailable"
    assert reports[0]["error_code"] == "unsupported_protocol_version"
    encoded = json.dumps(reports)
    assert "PrivateUser" not in encoded
    assert _openai_key() not in encoded
    assert _github_token() not in encoded
    assert "me@example.test" not in encoded


def test_collect_bridge_diagnostics_sanitizes_successful_client_payload(monkeypatch):
    instance = SimpleNamespace(instance_id="blender-12345678", token=_github_token())

    class SuccessfulClient:
        def __init__(self, selected):
            assert selected is instance

        def diagnostics(self, timeout):
            assert timeout == diagnostic_export.BRIDGE_DIAGNOSTIC_TIMEOUT
            return _poisoned_bridge_payload()

    monkeypatch.setattr(diagnostic_export, "discover_instances", lambda verify: [instance])
    monkeypatch.setattr(diagnostic_export, "BridgeClient", SuccessfulClient)

    reports = diagnostic_export.collect_bridge_diagnostics()

    assert reports[0]["status"] == "collected"
    assert reports[0]["instance_ref"].startswith("instance-")
    assert reports[0]["diagnostics"]["recent_jobs"][0]["job_id"] == JOB_ID
    encoded = json.dumps(reports, ensure_ascii=False)
    for forbidden in (
        "blender-12345678",
        "PrivateUser",
        "绝密场景",
        _openai_key(),
        _github_token(),
    ):
        assert forbidden not in encoded


def test_collect_bridge_diagnostics_maps_unexpected_failure_to_stable_code(monkeypatch):
    instance = SimpleNamespace(instance_id="blender-12345678", token=_github_token())

    class FailingClient:
        def __init__(self, _selected):
            pass

        def diagnostics(self, timeout):
            raise RuntimeError(f"me@example.test {_openai_key()}")

    monkeypatch.setattr(diagnostic_export, "discover_instances", lambda verify: [instance])
    monkeypatch.setattr(diagnostic_export, "BridgeClient", FailingClient)

    reports = diagnostic_export.collect_bridge_diagnostics()

    assert reports[0]["status"] == "unavailable"
    assert reports[0]["error_code"] == "diagnostic_unavailable"
    encoded = json.dumps(reports)
    assert "example.test" not in encoded
    assert _openai_key() not in encoded
