from __future__ import annotations

import re
from typing import Any

STATUS_SCHEMA_VERSION = "1.0"
MAX_ACTIVE_JOBS = 6
MAX_RECENT_JOBS = 3
ACTIVE_STATUSES = frozenset({"queued", "running"})
TERMINAL_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}
)
JOB_TYPES = frozenset(
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
ERROR_CODES = frozenset(
    {
        "job_cancelled",
        "job_timed_out",
        "job_interrupted",
        "bridge_stopped",
        "bridge_restarted",
        "invalid_job_payload",
        "unsupported_schema_version",
        "job_rejected",
        "job_failed",
        "internal_error",
        "journal_unavailable",
        "recovery_review_required",
    }
)
RECOVERY_REASONS = frozenset(
    {"none", "journal_corrupt", "interrupted_jobs", "unclean_shutdown", "unknown"}
)
STARTUP_ERROR_CODES = frozenset(
    {
        "unsupported_journal_version",
        "journal_unavailable",
        "bridge_initialization_failed",
        "bridge_start_failed",
    }
)
TRANSITION_CODES = frozenset({"file_loaded", "workspace_changed"})
CHECKPOINTS = frozenset(
    {
        "accepted",
        "main_thread_started",
        "transaction_committed",
        "transaction_rolled_back",
        "terminal",
    }
)
JOB_ID_PATTERN = re.compile(r"^job-[0-9a-f]{16}(?:[0-9a-f]{16})?$")
INSTANCE_ID_PATTERN = re.compile(r"^blender-[1-9]\d{0,19}$")
PROTOCOL_PATTERN = re.compile(r"^(0|[1-9]\d{0,8})\.(0|[1-9]\d{0,8})$")


def _bounded_count(value: Any, maximum: int = 1_000_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        return 0
    return value


def _project_job(record: Any, *, terminal: bool = False) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None
    job_id = record.get("job_id")
    job_type = record.get("type")
    status = record.get("status")
    allowed_statuses = TERMINAL_STATUSES if terminal else ACTIVE_STATUSES
    if (
        not isinstance(job_id, str)
        or JOB_ID_PATTERN.fullmatch(job_id) is None
        or not isinstance(job_type, str)
        or job_type not in JOB_TYPES
        or not isinstance(status, str)
        or status not in allowed_statuses
    ):
        return None
    projected: dict[str, Any] = {
        "job_id": job_id,
        "type": job_type,
        "status": status,
    }
    checkpoint = record.get("checkpoint")
    if isinstance(checkpoint, str) and checkpoint in CHECKPOINTS:
        projected["checkpoint"] = checkpoint
    if not terminal and isinstance(record.get("cancel_requested"), bool):
        projected["cancel_requested"] = record["cancel_requested"]
    if not terminal:
        position = record.get("queue_position")
        if isinstance(position, int) and not isinstance(position, bool) and 1 <= position <= 128:
            projected["queue_position"] = position
    if terminal and status != "succeeded":
        code = record.get("error_code")
        projected["error_code"] = (
            code if isinstance(code, str) and code in ERROR_CODES else "job_failed"
        )
    if terminal:
        for field in ("completed_after_cancel_request", "completed_after_deadline"):
            if isinstance(record.get(field), bool):
                projected[field] = record[field]
    return projected


def _connection_payload(
    *,
    running: bool,
    instance_id: Any,
    protocol_version: Any,
    state: str,
    error_code: str | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"running": running, "state": state}
    if (
        running
        and isinstance(instance_id, str)
        and INSTANCE_ID_PATTERN.fullmatch(instance_id) is not None
    ):
        result["instance_id"] = instance_id
    if (
        running
        and isinstance(protocol_version, str)
        and PROTOCOL_PATTERN.fullmatch(protocol_version) is not None
    ):
        result["protocol_version"] = protocol_version
    if error_code is not None:
        result["error_code"] = error_code
    return result


def build_product_status(
    *,
    running: bool,
    instance_id: Any = None,
    protocol_version: Any = None,
    runtime: Any = None,
    records: Any = None,
    recovery: Any = None,
    staged: bool = False,
    storage_error: bool = False,
    startup_error_code: Any = None,
    startup_error_version: Any = None,
    transition_code: Any = None,
) -> dict[str, Any]:
    """Project internal runtime data into a bounded, content-free Blender UI model."""
    runtime = runtime if isinstance(runtime, dict) else {}
    counts = runtime.get("status_counts")
    counts = counts if isinstance(counts, dict) else {}
    record_items = records if isinstance(records, list) else []
    recovery = recovery if isinstance(recovery, dict) else {}

    active_jobs = [
        item
        for record in record_items
        if (item := _project_job(record)) is not None
    ]
    active_jobs.sort(key=lambda item: 0 if item["status"] == "running" else 1)
    recent_jobs = [
        item
        for record in reversed(record_items)
        if (item := _project_job(record, terminal=True)) is not None
    ][:MAX_RECENT_JOBS]

    pending = _bounded_count(runtime.get("pending"))
    pending_capacity = _bounded_count(runtime.get("pending_capacity"))
    running_count = _bounded_count(counts.get("running"))
    cancel_requested = _bounded_count(runtime.get("cancel_requested"))
    recovery_required = recovery.get("required") is True
    reason = recovery.get("reason")
    if not isinstance(reason, str) or reason not in RECOVERY_REASONS:
        reason = "unknown" if recovery_required else "none"
    interrupted_items = recovery.get("interrupted_jobs")
    interrupted_count = (
        len(interrupted_items)
        if isinstance(interrupted_items, list)
        else _bounded_count(recovery.get("interrupted_job_count"))
    )

    safe_startup_error = (
        startup_error_code
        if isinstance(startup_error_code, str)
        and startup_error_code in STARTUP_ERROR_CODES
        else None
    )
    safe_startup_version = (
        startup_error_version
        if isinstance(startup_error_version, str)
        and PROTOCOL_PATTERN.fullmatch(startup_error_version) is not None
        else None
    )
    safe_transition = (
        transition_code
        if isinstance(transition_code, str) and transition_code in TRANSITION_CODES
        else None
    )
    if not running and safe_startup_error == "unsupported_journal_version":
        connection_state = "incompatible"
    elif not running and safe_startup_error is not None:
        connection_state = "error"
    elif not running:
        connection_state = "stopped"
    elif storage_error or safe_startup_error == "journal_unavailable":
        connection_state = "error"
    elif recovery_required:
        connection_state = "recovery_required"
    elif active_jobs or pending or running_count:
        connection_state = "busy"
    else:
        connection_state = "ready"

    connection = _connection_payload(
        running=running,
        instance_id=instance_id,
        protocol_version=protocol_version,
        state=connection_state,
        error_code=("journal_unavailable" if storage_error else safe_startup_error),
    )
    if connection_state == "incompatible" and safe_startup_version is not None:
        connection["requested_version"] = safe_startup_version
    if connection_state == "stopped" and safe_transition is not None:
        connection["notice_code"] = safe_transition

    return {
        "schema_version": STATUS_SCHEMA_VERSION,
        "connection": connection,
        "queue": {
            "pending": pending,
            "pending_capacity": pending_capacity,
            "running": running_count,
            "cancel_requested": cancel_requested,
        },
        "active_job_count": len(active_jobs),
        "hidden_active_job_count": max(0, len(active_jobs) - MAX_ACTIVE_JOBS),
        "active_jobs": active_jobs[:MAX_ACTIVE_JOBS],
        "recent_jobs": recent_jobs,
        "recovery": {
            "required": recovery_required,
            "reason": reason,
            "interrupted_job_count": min(interrupted_count, 256),
            "persistent_workspace": recovery.get("persistent_workspace") is True,
        },
        "staged": staged is True,
        "storage_error": storage_error is True,
    }
