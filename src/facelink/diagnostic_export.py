from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .bridge_client import BridgeClient, BridgeError, discover_instances
from .diagnostics import diagnose_environment

DIAGNOSTIC_SCHEMA_VERSION = "1.0"
MAX_DIAGNOSTIC_BYTES = 1024 * 1024
MAX_DIAGNOSTIC_INSTANCES = 32
MAX_DIAGNOSTIC_CHECKS = 64
MAX_DIAGNOSTIC_JOBS = 64
MAX_DIAGNOSTIC_EVENTS = 256
BRIDGE_DIAGNOSTIC_TIMEOUT = 0.75
SAFE_CODE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.+-]{0,63}$")
SAFE_IDENTIFIER_PATTERN = re.compile(
    r"^(?:req-[0-9a-f]{16}(?:[0-9a-f]{16})?|corr-[0-9a-f]{32}|"
    r"job-[0-9a-f]{16}(?:[0-9a-f]{16})?|diag-[0-9a-f]{32}|"
    r"instance-[0-9a-f]{16})$"
)
SAFE_VERSION_PATTERN = re.compile(
    r"^\d{1,4}(?:\.\d{1,4}){1,3}"
    r"(?:(?:[-+][0-9A-Za-z][0-9A-Za-z.-]{0,31})|(?: (?:LTS|Alpha|Beta|RC)))?$"
)
SAFE_CHECK_IDS = frozenset(
    {
        "python",
        "facelink_package",
        "mcp_launcher",
        "blender",
        "instance_directory",
        "blender_bridge",
        "openai_api_key",
    }
)
SAFE_CAPABILITIES = frozenset(
    {
        "scan_scene",
        "stage_patch",
        "review_staged_patch",
        "apply_staged_patch",
        "discard_staged_patch",
        "world_space_transforms",
        "scene_fingerprint",
        "timeline_diagnostics",
        "viewport_preview",
        "navigation_mesh_paths",
        "collision_warnings",
        "navigation_fingerprint",
        "camera_composition_preflight",
        "rig_action_inventory",
        "action_fingerprint",
        "rename_only_retarget",
        "sampled_pose_bake",
        "evaluated_pose_bake",
        "object_motion_bake",
        "rig_rest_geometry",
        "rig_fingerprint",
        "revision_history",
        "rollback_revision",
        "keyframe_transform",
        "look_at",
        "play_clip",
        "ensure_camera",
        "undo",
        "bounded_job_queue",
        "stable_error_codes",
        "job_deadlines",
        "safe_job_cancellation",
        "persistent_job_journal",
        "startup_recovery",
        "recovery_mutation_gate",
        "correlation_ids",
        "sanitized_diagnostics",
        "protocol_negotiation",
    }
)
SAFE_JOB_TYPES = frozenset(
    {
        "scan_scene",
        "stage_patch",
        "get_staged_patch",
        "apply_staged_patch",
        "discard_staged_patch",
        "apply_patch",
        "undo",
        "list_revisions",
        "rollback_revision",
    }
)
SAFE_JOB_STATUSES = frozenset(
    {"queued", "running", "succeeded", "failed", "cancelled", "timed_out", "interrupted"}
)
SAFE_ERROR_CODES = frozenset(
    {
        "authentication_required",
        "route_not_found",
        "job_not_found",
        "invalid_content_length",
        "invalid_request_size",
        "request_too_large",
        "invalid_json",
        "invalid_request",
        "unsupported_job_type",
        "invalid_job_payload",
        "invalid_job_timeout",
        "queue_full",
        "job_registry_full",
        "journal_unavailable",
        "recovery_review_required",
        "bridge_stopping",
        "invalid_correlation_id",
        "invalid_protocol_version",
        "unsupported_protocol_version",
        "job_cancelled",
        "job_timed_out",
        "job_interrupted",
        "bridge_stopped",
        "bridge_restarted",
        "job_not_cancellable",
        "unsupported_schema_version",
        "job_rejected",
        "job_failed",
        "internal_error",
        "recovery_journal_corrupt",
        "diagnostic_unavailable",
    }
)
SAFE_CHECKPOINTS = frozenset(
    {
        "accepted",
        "main_thread_started",
        "transaction_committed",
        "transaction_rolled_back",
        "terminal",
    }
)
SAFE_RECOVERY_REASONS = frozenset(
    {"none", "journal_corrupt", "interrupted_jobs", "unclean_shutdown", "unknown"}
)
SENSITIVE_PATTERNS = (
    re.compile(rb"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"(?i)authorization\s*[:=]\s*bearer\s+\S+"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(rb"(?i)[A-Z]:[\\/]Users[\\/][^\\/\s]+"),
    re.compile(rb"/(?:home|Users)/[^/\s]+"),
    re.compile(rb"\\\\[^\\\s]+\\[^\\\s]+"),
)


class DiagnosticExportError(RuntimeError):
    pass


def _looks_sensitive(value: str) -> bool:
    encoded = value.encode("utf-8", errors="ignore")
    return any(pattern.search(encoded) for pattern in SENSITIVE_PATTERNS)


def _safe_code(value: Any) -> str | None:
    return (
        value
        if isinstance(value, str)
        and SAFE_CODE_PATTERN.fullmatch(value)
        and not _looks_sensitive(value)
        else None
    )


def _safe_known_code(value: Any, allowed: frozenset[str]) -> str | None:
    code = _safe_code(value)
    return code if code in allowed else None


def _safe_identifier(value: Any) -> str | None:
    return (
        value
        if isinstance(value, str)
        and SAFE_IDENTIFIER_PATTERN.fullmatch(value)
        and not _looks_sensitive(value)
        else None
    )


def _safe_version(value: Any) -> str | None:
    return (
        value
        if isinstance(value, str)
        and SAFE_VERSION_PATTERN.fullmatch(value)
        and not _looks_sensitive(value)
        else None
    )


def _safe_count(value: Any, maximum: int = 1_000_000_000) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        return None
    return value


def _safe_timestamp(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not 0 <= value <= 4_102_444_800:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def sanitize_doctor_report(report: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(report, dict):
        report = {}
    check_items = report.get("checks")
    if not isinstance(check_items, list):
        check_items = []
    checks = []
    for item in check_items[:MAX_DIAGNOSTIC_CHECKS]:
        if not isinstance(item, dict):
            continue
        check_id = _safe_known_code(item.get("check_id"), SAFE_CHECK_IDS)
        status = item.get("status")
        if check_id is not None and status in {"pass", "warning", "fail"}:
            checks.append({"check_id": check_id, "status": status})

    installations = []
    blender = report.get("blender")
    if isinstance(blender, dict):
        installation_items = blender.get("installations")
        if not isinstance(installation_items, list):
            installation_items = []
        for item in installation_items[:MAX_DIAGNOSTIC_INSTANCES]:
            if not isinstance(item, dict):
                continue
            installation = {
                "available": item.get("available") is True,
                "supported": item.get("supported") is True,
            }
            if (version := _safe_version(item.get("version"))) is not None:
                installation["version"] = version
            installations.append(installation)

    bridge = report.get("bridge")
    stale_count = 0
    unprobed_count = 0
    probe_limit = MAX_DIAGNOSTIC_INSTANCES
    if isinstance(bridge, dict):
        stale_count = _safe_count(bridge.get("stale_record_count")) or 0
        unprobed_count = _safe_count(bridge.get("unprobed_record_count")) or 0
        probe_limit = (
            _safe_count(bridge.get("probe_limit"), MAX_DIAGNOSTIC_INSTANCES)
            or MAX_DIAGNOSTIC_INSTANCES
        )
    provider = report.get("provider")
    api_key_configured = (
        isinstance(provider, dict) and provider.get("openai_api_key_configured") is True
    )
    readiness = report.get("readiness") if isinstance(report.get("readiness"), dict) else {}
    operating_system = platform.system()
    if operating_system not in {"Windows", "Linux", "Darwin", "FreeBSD"}:
        operating_system = "Other"
    return {
        "facelink_version": _safe_version(report.get("facelink_version")) or __version__,
        "python_version": _safe_version(platform.python_version()) or "unknown",
        "operating_system": operating_system,
        "environment_ok": report.get("ok") is True,
        "readiness": {
            "mcp": readiness.get("mcp") is True,
            "byok": readiness.get("byok") is True,
        },
        "checks": checks,
        "blender_installations": installations,
        "stale_bridge_records": stale_count,
        "unprobed_bridge_records": unprobed_count,
        "bridge_probe_limit": probe_limit,
        "openai_api_key_configured": api_key_configured,
    }


def _sanitize_job(item: dict[str, Any]) -> dict[str, Any] | None:
    job_id = _safe_identifier(item.get("job_id"))
    job_type = _safe_known_code(item.get("type"), SAFE_JOB_TYPES)
    status = _safe_known_code(item.get("status"), SAFE_JOB_STATUSES)
    if job_id is None or job_type is None or status is None:
        return None
    result: dict[str, Any] = {"job_id": job_id, "type": job_type, "status": status}
    for field in ("submission_request_id", "job_correlation_id"):
        if (value := _safe_identifier(item.get(field))) is not None:
            result[field] = value
    for field, allowed in (
        ("error_code", SAFE_ERROR_CODES),
        ("checkpoint", SAFE_CHECKPOINTS),
        ("interrupted_from", SAFE_JOB_STATUSES),
    ):
        if (value := _safe_known_code(item.get(field), allowed)) is not None:
            result[field] = value
    for field in ("created_at", "started_at", "finished_at", "checkpoint_at"):
        if (value := _safe_timestamp(item.get(field))) is not None:
            result[field] = value
    for field in ("retryable", "transaction_committed"):
        if isinstance(item.get(field), bool):
            result[field] = item[field]
    return result


def _sanitize_event(item: dict[str, Any]) -> dict[str, Any] | None:
    request_id = _safe_identifier(item.get("request_id"))
    correlation_id = _safe_identifier(item.get("correlation_id"))
    method = item.get("method")
    route = item.get("route")
    status = _safe_count(item.get("http_status"), 599)
    if (
        request_id is None
        or correlation_id is None
        or method not in {"GET", "POST"}
        or route
        not in {
            "/v1/health",
            "/v1/jobs",
            "/v1/jobs/{id}",
            "/v1/jobs/{id}/cancel",
            "/v1/recovery",
            "/v1/recovery/acknowledge",
            "/v1/diagnostics",
            "/unmatched",
        }
        or status is None
        or status < 100
    ):
        return None
    result: dict[str, Any] = {
        "request_id": request_id,
        "correlation_id": correlation_id,
        "method": method,
        "route": route,
        "http_status": status,
    }
    if (timestamp := _safe_timestamp(item.get("timestamp"))) is not None:
        result["timestamp"] = timestamp
    for field, allowed in (
        ("error_code", SAFE_ERROR_CODES),
        ("job_type", SAFE_JOB_TYPES),
        ("job_status", SAFE_JOB_STATUSES),
    ):
        if (value := _safe_known_code(item.get(field), allowed)) is not None:
            result[field] = value
    if (job_id := _safe_identifier(item.get("job_id"))) is not None:
        result["job_id"] = job_id
    return result


def sanitize_bridge_diagnostics(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        payload = {}
    runtime = payload.get("job_runtime") if isinstance(payload.get("job_runtime"), dict) else {}
    recovery = payload.get("recovery") if isinstance(payload.get("recovery"), dict) else {}
    events_payload = payload.get("events") if isinstance(payload.get("events"), dict) else {}
    counts = runtime.get("status_counts") if isinstance(runtime.get("status_counts"), dict) else {}
    status_counts = {
        key: value
        for key in (
            "queued",
            "running",
            "succeeded",
            "failed",
            "cancelled",
            "timed_out",
            "interrupted",
        )
        if (value := _safe_count(counts.get(key))) is not None
    }
    job_items = payload.get("recent_jobs")
    if not isinstance(job_items, list):
        job_items = []
    jobs = []
    for item in job_items[-MAX_DIAGNOSTIC_JOBS:]:
        if isinstance(item, dict) and (job := _sanitize_job(item)) is not None:
            jobs.append(job)
    event_items = events_payload.get("events")
    if not isinstance(event_items, list):
        event_items = []
    events = []
    for item in event_items[-MAX_DIAGNOSTIC_EVENTS:]:
        if isinstance(item, dict) and (event := _sanitize_event(item)) is not None:
            events.append(event)
    capability_items = payload.get("capabilities")
    if not isinstance(capability_items, list):
        capability_items = []
    capabilities = sorted(
        {
            value
            for value in capability_items
            if _safe_known_code(value, SAFE_CAPABILITIES) is not None
        }
    )
    return {
        "protocol_version": _safe_version(payload.get("protocol_version")),
        "facelink_version": _safe_version(payload.get("facelink_version")),
        "blender_version": _safe_version(payload.get("blender_version")),
        "capabilities": capabilities,
        "job_runtime": {
            key: value
            for key in (
                "pending",
                "pending_capacity",
                "retained",
                "retention_capacity",
                "cancel_requested",
                "evicted_total",
            )
            if (value := _safe_count(runtime.get(key))) is not None
        }
        | {"status_counts": status_counts},
        "recovery": {
            "required": recovery.get("required") is True,
            "reason": (
                _safe_known_code(recovery.get("reason"), SAFE_RECOVERY_REASONS)
                or "unknown"
            ),
            "unclean_shutdown": recovery.get("unclean_shutdown") is True,
            "interrupted_job_count": (
                _safe_count(recovery.get("interrupted_job_count")) or 0
            ),
            "persistent_workspace": recovery.get("persistent_workspace") is True,
        },
        "recent_jobs": jobs,
        "events": {
            "capacity": _safe_count(events_payload.get("capacity")) or 0,
            "retained": len(events),
            "evicted_total": _safe_count(events_payload.get("evicted_total")) or 0,
            "events": events,
        },
    }


def collect_bridge_diagnostics() -> list[dict[str, Any]]:
    output = []
    for instance in discover_instances(verify=False)[:MAX_DIAGNOSTIC_INSTANCES]:
        instance_ref = "instance-" + uuid.uuid4().hex[:16]
        client = BridgeClient(instance)
        try:
            payload = client.diagnostics(timeout=BRIDGE_DIAGNOSTIC_TIMEOUT)
        except BridgeError as exc:
            output.append(
                {
                    "instance_ref": instance_ref,
                    "status": "unavailable",
                    "error_code": (
                        _safe_known_code(exc.code, SAFE_ERROR_CODES)
                        or "diagnostic_unavailable"
                    ),
                }
            )
        except Exception:
            output.append(
                {
                    "instance_ref": instance_ref,
                    "status": "unavailable",
                    "error_code": "diagnostic_unavailable",
                }
            )
        else:
            output.append(
                {
                    "instance_ref": instance_ref,
                    "status": "collected",
                    "diagnostics": sanitize_bridge_diagnostics(payload),
                }
            )
    return output


def _sanitize_bridge_report(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    instance_ref = _safe_identifier(item.get("instance_ref"))
    status = item.get("status")
    if instance_ref is None or status not in {"collected", "unavailable"}:
        return None
    result: dict[str, Any] = {"instance_ref": instance_ref, "status": status}
    if status == "collected" and isinstance(item.get("diagnostics"), dict):
        result["diagnostics"] = sanitize_bridge_diagnostics(item["diagnostics"])
    elif status == "unavailable":
        result["error_code"] = (
            _safe_known_code(item.get("error_code"), SAFE_ERROR_CODES)
            or "diagnostic_unavailable"
        )
    return result


def _decoded_json_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _decoded_json_strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _decoded_json_strings(key)
            yield from _decoded_json_strings(item)


def _assert_share_safe(encoded: bytes) -> None:
    searchable = [encoded, encoded.replace(b"\0", b"")]
    try:
        decoded = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        decoded = None
    if decoded is not None:
        for value in _decoded_json_strings(decoded):
            candidate = value.encode("utf-8", errors="ignore")
            searchable.extend((candidate, candidate.replace(b"\0", b"")))
    if any(pattern.search(candidate) for pattern in SENSITIVE_PATTERNS for candidate in searchable):
        raise DiagnosticExportError("The diagnostic export failed its privacy safety scan.")


def build_diagnostic_payload(
    *,
    doctor_report: dict[str, Any],
    bridge_reports: list[dict[str, Any]],
    diagnostic_id: str | None = None,
    created_at: float | None = None,
) -> dict[str, Any]:
    identifier = "diag-" + uuid.uuid4().hex if diagnostic_id is None else diagnostic_id
    if _safe_identifier(identifier) is None:
        raise DiagnosticExportError("The diagnostic identifier is invalid.")
    timestamp = time.time() if created_at is None else created_at
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, (int, float))
        or not 0 <= timestamp <= 4_102_444_800
        or (isinstance(timestamp, float) and not math.isfinite(timestamp))
    ):
        raise DiagnosticExportError("The diagnostic timestamp is invalid.")
    bridge_items = bridge_reports if isinstance(bridge_reports, list) else []
    sanitized_bridges = []
    for item in bridge_items[:MAX_DIAGNOSTIC_INSTANCES]:
        if (sanitized := _sanitize_bridge_report(item)) is not None:
            sanitized_bridges.append(sanitized)
    return {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "diagnostic_id": identifier,
        "created_at": datetime.fromtimestamp(timestamp, tz=UTC).isoformat(),
        "privacy": {
            "policy": "strict_allowlist_v1",
            "contains_credentials": False,
            "contains_scene_content": False,
            "contains_absolute_paths": False,
            "automatic_upload": False,
        },
        "doctor": sanitize_doctor_report(doctor_report),
        "bridges": sanitized_bridges,
    }


def _publish_diagnostic_file(temporary: Path, destination: Path, *, overwrite: bool) -> None:
    if overwrite:
        os.replace(temporary, destination)
    elif os.name == "nt":
        os.rename(temporary, destination)
    else:
        os.link(temporary, destination)


def export_diagnostics(
    destination: str | Path,
    *,
    blender_exe: str | Path | None = None,
    overwrite: bool = False,
    doctor_report: dict[str, Any] | None = None,
    bridge_reports: list[dict[str, Any]] | None = None,
    diagnostic_id: str | None = None,
    created_at: float | None = None,
) -> dict[str, Any]:
    path = Path(destination)
    if path.suffix.lower() != ".json":
        raise DiagnosticExportError("Diagnostic exports must use a .json filename.")
    if path.exists() and not overwrite:
        raise DiagnosticExportError(
            "The diagnostic export already exists; use overwrite explicitly."
        )
    report = doctor_report if doctor_report is not None else diagnose_environment(blender_exe)
    bridges = bridge_reports if bridge_reports is not None else collect_bridge_diagnostics()
    payload = build_diagnostic_payload(
        doctor_report=report,
        bridge_reports=bridges,
        diagnostic_id=diagnostic_id,
        created_at=created_at,
    )
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    if len(encoded) > MAX_DIAGNOSTIC_BYTES:
        raise DiagnosticExportError("The diagnostic export exceeds the 1 MiB safety limit.")
    _assert_share_safe(encoded)

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        _publish_diagnostic_file(temporary, path, overwrite=overwrite)
    except FileExistsError as exc:
        raise DiagnosticExportError(
            "The diagnostic export already exists; use overwrite explicitly."
        ) from exc
    except OSError as exc:
        raise DiagnosticExportError("Could not write the diagnostic export atomically.") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return {
        "diagnostic_id": payload["diagnostic_id"],
        "path": str(path.resolve(strict=False)),
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "uploaded": False,
    }
