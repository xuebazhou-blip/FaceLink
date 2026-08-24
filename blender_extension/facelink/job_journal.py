from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

JOURNAL_SCHEMA_VERSION = "1.0"
MAX_JOURNAL_JOBS = 256
RECOVERY_STATUSES = frozenset({"queued", "running", "interrupted"})
MUTATING_JOB_TYPES = frozenset(
    {"apply_patch", "apply_staged_patch", "undo", "rollback_revision"}
)
SAFE_RECORD_FIELDS = (
    "job_id",
    "type",
    "status",
    "created_at",
    "started_at",
    "finished_at",
    "timeout_ms",
    "deadline_at",
    "error_code",
    "retryable",
    "cancel_requested",
    "cancel_requested_at",
    "cancelled_at",
    "timed_out_at",
    "completed_after_cancel_request",
    "completed_after_deadline",
    "interrupted_at",
    "interrupted_from",
    "transaction_committed",
    "checkpoint",
    "checkpoint_at",
)


class JournalError(RuntimeError):
    pass


def workspace_identity(blend_filepath: str, process_id: int) -> tuple[str, bool]:
    if blend_filepath:
        normalized = os.path.abspath(blend_filepath).replace("\\", "/").casefold()
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
        return f"blend-{digest}", True
    return f"unsaved-{process_id}", False


def sanitize_job_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def safe_scalar(value: Any) -> bool:
        return (
            isinstance(value, (str, int, bool))
            or isinstance(value, float)
            and math.isfinite(value)
        )

    sanitized = []
    for record in records[-MAX_JOURNAL_JOBS:]:
        item = {
            field: record[field]
            for field in SAFE_RECORD_FIELDS
            if field in record
            and safe_scalar(record[field])
        }
        metadata = record.get("checkpoint_metadata")
        if isinstance(metadata, dict):
            safe_metadata = {
                key: value
                for key, value in metadata.items()
                if key in {"revision_id", "operation_count", "rolled_back_count"}
                and isinstance(value, (str, int))
                and not isinstance(value, bool)
            }
            if safe_metadata:
                item["checkpoint_metadata"] = safe_metadata
        if {"job_id", "type", "status"} <= item.keys():
            sanitized.append(item)
    return sanitized


def _empty_recovery(*, persistent_workspace: bool) -> dict[str, Any]:
    return {
        "required": False,
        "reason": "none",
        "previous_session_id": None,
        "unclean_shutdown": False,
        "interrupted_jobs": [],
        "persistent_workspace": persistent_workspace,
        "guidance": (
            "No recovery action is required."
            if persistent_workspace
            else "Save the .blend file to enable recovery across Blender process restarts."
        ),
    }


class JobJournal:
    def __init__(
        self,
        path: Path,
        *,
        workspace_id: str,
        persistent_workspace: bool,
        session_id: str,
    ) -> None:
        self.path = path
        self.workspace_id = workspace_id
        self.persistent_workspace = persistent_workspace
        self.session_id = session_id
        self._payload: dict[str, Any] | None = None
        self._recovery = _empty_recovery(persistent_workspace=persistent_workspace)
        self._lock = threading.RLock()

    def start(self) -> dict[str, Any]:
        with self._lock:
            previous = None
            corrupt_reason = None
            if self.path.exists():
                try:
                    previous = self._read_payload()
                except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                    corrupt_reason = type(exc).__name__
                    self._quarantine_corrupt_file()
            if corrupt_reason:
                self._recovery = {
                    "required": True,
                    "reason": "journal_corrupt",
                    "error_code": "recovery_journal_corrupt",
                    "previous_session_id": None,
                    "unclean_shutdown": True,
                    "interrupted_jobs": [],
                    "persistent_workspace": self.persistent_workspace,
                    "guidance": (
                        "The previous task journal was preserved as corrupt. Rescan the scene, "
                        "inspect FaceLink revision history and create a new plan before applying."
                    ),
                }
            elif previous is not None:
                self._recovery = self._build_recovery(previous)
            payload = self._new_payload()
            self._write_payload(payload)
            self._payload = payload
            return self.status()

    def persist(self, records: list[dict[str, Any]], *, clean_shutdown: bool = False) -> None:
        with self._lock:
            if self._payload is None:
                raise JournalError("The job journal has not been started.")
            payload = copy.deepcopy(self._payload)
            payload.update(
                jobs=sanitize_job_records(records),
                clean_shutdown=clean_shutdown,
                updated_at=time.time(),
            )
            self._write_payload(payload)
            self._payload = payload

    def acknowledge(self) -> dict[str, Any]:
        with self._lock:
            if self._payload is None:
                raise JournalError("The job journal has not been started.")
            recovery = _empty_recovery(persistent_workspace=self.persistent_workspace)
            recovery["acknowledged"] = True
            payload = copy.deepcopy(self._payload)
            payload["recovery"] = copy.deepcopy(recovery)
            payload["updated_at"] = time.time()
            self._write_payload(payload)
            self._recovery = recovery
            self._payload = payload
            return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                **copy.deepcopy(self._recovery),
                "workspace_id": self.workspace_id,
                "session_id": self.session_id,
                "journal_schema_version": JOURNAL_SCHEMA_VERSION,
            }

    def _new_payload(self) -> dict[str, Any]:
        now = time.time()
        return {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "workspace_id": self.workspace_id,
            "persistent_workspace": self.persistent_workspace,
            "session_id": self.session_id,
            "started_at": now,
            "updated_at": now,
            "clean_shutdown": False,
            "jobs": [],
            "recovery": copy.deepcopy(self._recovery),
        }

    def _read_payload(self) -> dict[str, Any]:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("Journal root must be an object")
        if payload.get("schema_version") != JOURNAL_SCHEMA_VERSION:
            raise ValueError("Unsupported journal schema")
        if payload.get("workspace_id") != self.workspace_id:
            raise ValueError("Journal workspace mismatch")
        if not isinstance(payload.get("jobs"), list):
            raise TypeError("Journal jobs must be a list")
        return payload

    def _build_recovery(self, previous: dict[str, Any]) -> dict[str, Any]:
        interrupted = []
        records = []
        prior_recovery = previous.get("recovery")
        if isinstance(prior_recovery, dict) and prior_recovery.get("required") is True:
            candidates = prior_recovery.get("interrupted_jobs", [])
            if isinstance(candidates, list):
                records.extend(sanitize_job_records(candidates))
        records.extend(sanitize_job_records(previous.get("jobs", [])))
        for record in records:
            if record.get("status") not in RECOVERY_STATUSES:
                continue
            recovered = dict(record)
            recovered["previous_status"] = record.get("interrupted_from", record["status"])
            recovered["status"] = "interrupted"
            recovered["error_code"] = (
                record.get("error_code")
                if record["status"] == "interrupted"
                else "bridge_restarted"
            )
            committed = record.get("transaction_committed") is True or (
                record.get("checkpoint") == "transaction_committed"
            )
            may_have_partial_changes = (
                record.get("type") in MUTATING_JOB_TYPES
                and (
                    recovered["previous_status"] == "running"
                    or record.get("checkpoint")
                    in {"main_thread_started", "transaction_rolled_back"}
                )
                and not committed
            )
            recovered["may_have_committed_changes"] = committed
            recovered["may_have_partial_changes"] = may_have_partial_changes
            if committed:
                recovered["guidance_code"] = "inspect_revision_history"
            elif may_have_partial_changes:
                recovered["guidance_code"] = "rescan_and_inspect_scene"
            else:
                recovered["guidance_code"] = "rescan_and_replan"
            interrupted.append(recovered)
        unique = {}
        for item in interrupted:
            key = (item.get("job_id"), item.get("created_at"))
            unique[key] = item
        interrupted = list(unique.values())[-MAX_JOURNAL_JOBS:]
        unclean = previous.get("clean_shutdown") is not True
        required = bool(interrupted or unclean)
        if interrupted:
            reason = "interrupted_jobs"
            guidance = (
                "Do not replay interrupted jobs automatically. Rescan the Blender scene; "
                "inspect revision history for committed checkpoints, then create a new plan."
            )
        elif unclean:
            reason = "unclean_shutdown"
            guidance = (
                "The previous bridge did not close cleanly. Rescan the scene before applying "
                "new work."
            )
        else:
            reason = "none"
            guidance = "No recovery action is required."
        return {
            "required": required,
            "reason": reason,
            "previous_session_id": previous.get("session_id"),
            "unclean_shutdown": unclean,
            "interrupted_jobs": interrupted,
            "persistent_workspace": self.persistent_workspace,
            "guidance": guidance,
        }

    def _quarantine_corrupt_file(self) -> None:
        suffix = f".corrupt-{time.time_ns()}"
        target = self.path.with_name(self.path.name + suffix)
        try:
            os.replace(self.path, target)
        except OSError as exc:
            raise JournalError("Could not preserve the corrupt task journal.") from exc

    def _write_payload(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            if os.name != "nt":
                os.chmod(self.path, 0o600)
        except OSError as exc:
            raise JournalError("Could not persist the FaceLink task journal.") from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
