from __future__ import annotations

import queue
import threading
import time
from typing import Any

DEFAULT_PENDING_CAPACITY = 128
DEFAULT_RETENTION_CAPACITY = 256
TERMINAL_STATUSES = frozenset({"succeeded", "failed"})


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


class JobRuntime:
    """Thread-safe, bounded storage and admission control for Blender bridge jobs."""

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
        self._pending: queue.Queue[tuple[str, str, dict[str, Any]]] = queue.Queue(
            maxsize=pending_capacity
        )
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._evicted_total = 0

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def submit(
        self,
        job_id: str,
        job_type: str,
        payload: dict[str, Any],
        *,
        created_at: float | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if job_id in self._jobs:
                raise ValueError(f"Job {job_id} already exists.")
            if self._pending.full():
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

            record = {
                "job_id": job_id,
                "type": job_type,
                "status": "queued",
                "created_at": time.time() if created_at is None else created_at,
            }
            self._jobs[job_id] = record
            try:
                self._pending.put_nowait((job_id, job_type, payload))
            except queue.Full as exc:  # Defensive: all admissions share the same lock.
                del self._jobs[job_id]
                raise JobAdmissionError(
                    "queue_full",
                    "The Blender job queue is full; retry after pending work completes.",
                    details=self._capacity_details_locked(),
                ) from exc
            return dict(record)

    def take_nowait(self) -> tuple[str, str, dict[str, Any]] | None:
        try:
            return self._pending.get_nowait()
        except queue.Empty:
            return None

    def mark_running(self, job_id: str) -> None:
        self._transition(job_id, {"queued"}, status="running", started_at=time.time())

    def mark_succeeded(self, job_id: str, result: Any) -> None:
        self._transition(
            job_id,
            {"running"},
            status="succeeded",
            result=result,
            finished_at=time.time(),
        )

    def mark_failed(
        self,
        job_id: str,
        *,
        code: str,
        message: str,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        changes = error_payload(code, message, retryable=retryable, details=details)
        changes.update(status="failed", finished_at=time.time())
        self._transition(job_id, {"running"}, **changes)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            status_counts = {
                status: sum(job["status"] == status for job in self._jobs.values())
                for status in ("queued", "running", "succeeded", "failed")
            }
            return {
                "pending": self._pending.qsize(),
                "pending_capacity": self.pending_capacity,
                "retained": len(self._jobs),
                "retention_capacity": self.retention_capacity,
                "status_counts": status_counts,
                "evicted_total": self._evicted_total,
            }

    def clear(self) -> None:
        with self._lock:
            self._jobs.clear()
            self._pending = queue.Queue(maxsize=self.pending_capacity)
            self._evicted_total = 0

    def _transition(
        self,
        job_id: str,
        expected_statuses: set[str],
        **changes: Any,
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job["status"] not in expected_statuses:
                expected = ", ".join(sorted(expected_statuses))
                raise RuntimeError(
                    f"Job {job_id} cannot transition from {job['status']}; expected {expected}."
                )
            job.update(changes)

    def _prune_terminal_jobs_locked(self, *, required_slots: int) -> None:
        target_size = self.retention_capacity - required_slots
        if len(self._jobs) <= target_size:
            return
        for job_id, job in list(self._jobs.items()):
            if job["status"] in TERMINAL_STATUSES:
                del self._jobs[job_id]
                self._evicted_total += 1
                if len(self._jobs) <= target_size:
                    return

    def _capacity_details_locked(self) -> dict[str, int]:
        return {
            "pending": self._pending.qsize(),
            "pending_capacity": self.pending_capacity,
            "retained": len(self._jobs),
            "retention_capacity": self.retention_capacity,
        }
