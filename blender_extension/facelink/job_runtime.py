from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

DEFAULT_PENDING_CAPACITY = 128
DEFAULT_RETENTION_CAPACITY = 256
TERMINAL_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}
)


def error_payload(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
    retry_after_ms: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "error": message,
        "error_code": code,
        "retryable": retryable,
    }
    if details:
        payload["details"] = dict(details)
    if retry_after_ms is not None:
        payload["retry_after_ms"] = retry_after_ms
    return payload


class JobAdmissionError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any],
        retry_after_ms: int = 100,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details)
        self.retry_after_ms = retry_after_ms

    def as_payload(self) -> dict[str, Any]:
        return error_payload(
            self.code,
            str(self),
            retryable=True,
            details=self.details,
            retry_after_ms=self.retry_after_ms,
        )


class JobControlError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any]) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details)


class JobInterruption(RuntimeError):
    def __init__(self, status: str, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable

    @classmethod
    def cancelled(cls) -> JobInterruption:
        return cls(
            "cancelled",
            "job_cancelled",
            "The Blender job was cancelled at a safe checkpoint.",
            retryable=False,
        )

    @classmethod
    def timed_out(cls) -> JobInterruption:
        return cls(
            "timed_out",
            "job_timed_out",
            "The Blender job exceeded its deadline before a safe checkpoint.",
            retryable=True,
        )

    @classmethod
    def bridge_stopped(cls) -> JobInterruption:
        return cls(
            "interrupted",
            "bridge_stopped",
            "The Blender bridge stopped before the job reached a terminal response.",
            retryable=True,
        )


class JobRuntime:
    """Thread-safe, bounded lifecycle and admission control for Blender bridge jobs."""

    def __init__(
        self,
        *,
        pending_capacity: int = DEFAULT_PENDING_CAPACITY,
        retention_capacity: int = DEFAULT_RETENTION_CAPACITY,
    ) -> None:
        if pending_capacity < 1:
            raise ValueError("pending_capacity must be at least 1")
        if retention_capacity < 1:
            raise ValueError("retention_capacity must be at least 1")
        self.pending_capacity = pending_capacity
        self.retention_capacity = retention_capacity
        self._pending: deque[str] = deque()
        self._work_items: dict[str, tuple[str, dict[str, Any]]] = {}
        self._deadlines: dict[str, float] = {}
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._evicted_total = 0
        self._version = 0

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._expire_queued_locked(time.monotonic())
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def submit(
        self,
        job_id: str,
        job_type: str,
        payload: dict[str, Any],
        *,
        timeout_ms: int | None = None,
        created_at: float | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            monotonic_now = time.monotonic()
            self._expire_queued_locked(monotonic_now)
            if job_id in self._jobs:
                raise ValueError(f"Job {job_id} already exists.")
            if len(self._pending) >= self.pending_capacity:
                raise JobAdmissionError(
                    "queue_full",
                    "The Blender job queue is full; retry after pending work completes.",
                    details=self._capacity_details_locked(),
                )

            self._prune_terminal_jobs_locked(required_slots=1)
            if len(self._jobs) >= self.retention_capacity:
                raise JobAdmissionError(
                    "job_registry_full",
                    "The Blender job registry is full of active work; retry later.",
                    details=self._capacity_details_locked(),
                )

            wall_now = time.time() if created_at is None else created_at
            record = {
                "job_id": job_id,
                "type": job_type,
                "status": "queued",
                "created_at": wall_now,
                "checkpoint": "accepted",
                "checkpoint_at": wall_now,
            }
            if timeout_ms is not None:
                record.update(
                    timeout_ms=timeout_ms,
                    deadline_at=wall_now + timeout_ms / 1000,
                )
                self._deadlines[job_id] = monotonic_now + timeout_ms / 1000
            if request_id is not None:
                record["submission_request_id"] = request_id
            if correlation_id is not None:
                record["job_correlation_id"] = correlation_id
            self._jobs[job_id] = record
            self._work_items[job_id] = (job_type, payload)
            self._pending.append(job_id)
            self._touch_locked()
            return dict(record)

    def start_next(self) -> tuple[str, str, dict[str, Any]] | None:
        with self._lock:
            self._expire_queued_locked(time.monotonic())
            if not self._pending:
                return None
            job_id = self._pending.popleft()
            job_type, payload = self._work_items.pop(job_id)
            job = self._jobs[job_id]
            started_at = time.time()
            job.update(
                status="running",
                started_at=started_at,
                checkpoint="main_thread_started",
                checkpoint_at=started_at,
            )
            self._touch_locked()
            return job_id, job_type, payload

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._expire_queued_locked(time.monotonic())
            job = self._jobs.get(job_id)
            if job is None:
                return None
            status = job["status"]
            if status == "queued":
                self._pending.remove(job_id)
                self._finish_interrupted_locked(job_id, JobInterruption.cancelled())
            elif status == "running":
                if not job.get("cancel_requested"):
                    job.update(cancel_requested=True, cancel_requested_at=time.time())
                    self._touch_locked()
            elif status in {"cancelled", "timed_out"}:
                pass
            else:
                raise JobControlError(
                    "job_not_cancellable",
                    "The Blender job has already finished and cannot be cancelled.",
                    details={"job_id": job_id, "status": status},
                )
            return dict(job)

    def raise_if_interrupted(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job["status"] != "running":
                raise RuntimeError(f"Job {job_id} cannot check interruption from {job['status']}.")
            if job.get("cancel_requested"):
                raise JobInterruption.cancelled()
            deadline = self._deadlines.get(job_id)
            if deadline is not None and time.monotonic() >= deadline:
                raise JobInterruption.timed_out()

    def mark_succeeded(self, job_id: str, result: Any) -> None:
        with self._lock:
            job = self._require_running_locked(job_id)
            if job.get("cancel_requested"):
                job["completed_after_cancel_request"] = True
            deadline = self._deadlines.get(job_id)
            if deadline is not None and time.monotonic() >= deadline:
                job["completed_after_deadline"] = True
            job.update(
                status="succeeded",
                result=result,
                finished_at=time.time(),
            )
            self._mark_terminal_checkpoint_locked(job)
            self._deadlines.pop(job_id, None)
            self._touch_locked()

    def mark_failed(
        self,
        job_id: str,
        *,
        code: str,
        message: str,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            job = self._require_running_locked(job_id)
            changes = error_payload(code, message, retryable=retryable, details=details)
            changes.update(status="failed", finished_at=time.time())
            job.update(changes)
            self._mark_terminal_checkpoint_locked(job)
            self._deadlines.pop(job_id, None)
            self._touch_locked()

    def mark_interrupted(self, job_id: str, interruption: JobInterruption) -> None:
        with self._lock:
            self._require_running_locked(job_id)
            self._finish_interrupted_locked(job_id, interruption)

    def set_checkpoint(
        self,
        job_id: str,
        checkpoint: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            job = self._require_running_locked(job_id)
            job.update(checkpoint=checkpoint, checkpoint_at=time.time())
            if checkpoint == "transaction_committed":
                job["transaction_committed"] = True
            elif checkpoint == "transaction_rolled_back":
                job["transaction_committed"] = False
            if metadata:
                job["checkpoint_metadata"] = dict(metadata)
            else:
                job.pop("checkpoint_metadata", None)
            self._touch_locked()

    def interrupt_active(self) -> list[dict[str, Any]]:
        with self._lock:
            interrupted = []
            for job_id, job in self._jobs.items():
                if job["status"] not in {"queued", "running"}:
                    continue
                interrupted_from = job["status"]
                self._finish_interrupted_locked(job_id, JobInterruption.bridge_stopped())
                job["interrupted_from"] = interrupted_from
                interrupted.append(dict(job))
            self._pending.clear()
            return interrupted

    def records_with_version(self) -> tuple[int, list[dict[str, Any]]]:
        with self._lock:
            self._expire_queued_locked(time.monotonic())
            return self._version, [dict(job) for job in self._jobs.values()]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._expire_queued_locked(time.monotonic())
            status_counts = {
                status: sum(job["status"] == status for job in self._jobs.values())
                for status in (
                    "queued",
                    "running",
                    "succeeded",
                    "failed",
                    "cancelled",
                    "timed_out",
                    "interrupted",
                )
            }
            return {
                "pending": len(self._pending),
                "pending_capacity": self.pending_capacity,
                "retained": len(self._jobs),
                "retention_capacity": self.retention_capacity,
                "status_counts": status_counts,
                "cancel_requested": sum(
                    job["status"] == "running" and job.get("cancel_requested") is True
                    for job in self._jobs.values()
                ),
                "evicted_total": self._evicted_total,
            }

    def clear(self) -> None:
        with self._lock:
            self._jobs.clear()
            self._pending.clear()
            self._work_items.clear()
            self._deadlines.clear()
            self._evicted_total = 0
            self._touch_locked()

    def _require_running_locked(self, job_id: str) -> dict[str, Any]:
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job["status"] != "running":
            raise RuntimeError(
                f"Job {job_id} cannot finish from {job['status']}; expected running."
            )
        return job

    def _expire_queued_locked(self, monotonic_now: float) -> None:
        if not self._pending:
            return
        retained = deque()
        for job_id in self._pending:
            deadline = self._deadlines.get(job_id)
            if deadline is not None and monotonic_now >= deadline:
                self._finish_interrupted_locked(job_id, JobInterruption.timed_out())
            else:
                retained.append(job_id)
        self._pending = retained

    def _finish_interrupted_locked(
        self,
        job_id: str,
        interruption: JobInterruption,
    ) -> None:
        job = self._jobs[job_id]
        finished_at = time.time()
        job.update(
            error_payload(
                interruption.code,
                str(interruption),
                retryable=interruption.retryable,
            )
        )
        job.update(status=interruption.status, finished_at=finished_at)
        if interruption.status == "cancelled":
            job["cancelled_at"] = finished_at
        elif interruption.status == "timed_out":
            job["timed_out_at"] = finished_at
        else:
            job["interrupted_at"] = finished_at
        self._mark_terminal_checkpoint_locked(job)
        self._work_items.pop(job_id, None)
        self._deadlines.pop(job_id, None)
        self._touch_locked()

    def _prune_terminal_jobs_locked(self, *, required_slots: int) -> None:
        target_size = self.retention_capacity - required_slots
        if len(self._jobs) <= target_size:
            return
        for job_id, job in list(self._jobs.items()):
            if job["status"] in TERMINAL_STATUSES:
                del self._jobs[job_id]
                self._work_items.pop(job_id, None)
                self._deadlines.pop(job_id, None)
                self._evicted_total += 1
                self._touch_locked()
                if len(self._jobs) <= target_size:
                    return

    @staticmethod
    def _mark_terminal_checkpoint_locked(job: dict[str, Any]) -> None:
        job.update(checkpoint="terminal", checkpoint_at=job["finished_at"])

    def _touch_locked(self) -> None:
        self._version += 1

    def _capacity_details_locked(self) -> dict[str, int]:
        return {
            "pending": len(self._pending),
            "pending_capacity": self.pending_capacity,
            "retained": len(self._jobs),
            "retention_capacity": self.retention_capacity,
        }
