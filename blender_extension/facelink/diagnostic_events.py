from __future__ import annotations

import re
import threading
import time
from collections import deque
from typing import Any

MAX_DIAGNOSTIC_EVENTS = 256
IDENTIFIER_PATTERN = re.compile(r"^[a-z]+-[a-z0-9]{8,64}$")
ERROR_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
KNOWN_METHODS = frozenset({"GET", "POST"})
KNOWN_JOB_STATUSES = frozenset(
    {"queued", "running", "succeeded", "failed", "cancelled", "timed_out", "interrupted"}
)
KNOWN_ROUTES = frozenset(
    {
        "/v1/health",
        "/v1/jobs",
        "/v1/jobs/{id}",
        "/v1/jobs/{id}/cancel",
        "/v1/recovery",
        "/v1/recovery/acknowledge",
        "/v1/diagnostics",
        "/unmatched",
    }
)


def route_template(path: str) -> str:
    clean_path = path.split("?", 1)[0]
    if clean_path in KNOWN_ROUTES:
        return clean_path
    parts = clean_path.strip("/").split("/")
    if len(parts) == 3 and parts[:2] == ["v1", "jobs"]:
        return "/v1/jobs/{id}"
    if len(parts) == 4 and parts[:2] == ["v1", "jobs"] and parts[3] == "cancel":
        return "/v1/jobs/{id}/cancel"
    return "/unmatched"


class DiagnosticEventBuffer:
    """Thread-safe bounded metadata events; request content is never accepted."""

    def __init__(self, capacity: int = MAX_DIAGNOSTIC_EVENTS) -> None:
        if capacity < 1 or capacity > MAX_DIAGNOSTIC_EVENTS:
            raise ValueError(f"capacity must be from 1 through {MAX_DIAGNOSTIC_EVENTS}")
        self.capacity = capacity
        self._events: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._evicted_total = 0

    def record(
        self,
        *,
        request_id: str,
        correlation_id: str,
        method: str,
        path: str,
        status: int,
        error_code: str | None = None,
        job_id: str | None = None,
        job_type: str | None = None,
        job_status: str | None = None,
    ) -> None:
        if IDENTIFIER_PATTERN.fullmatch(request_id) is None:
            raise ValueError("request_id is not a safe FaceLink identifier")
        if IDENTIFIER_PATTERN.fullmatch(correlation_id) is None:
            raise ValueError("correlation_id is not a safe FaceLink identifier")
        if method not in KNOWN_METHODS:
            raise ValueError("method is not supported")
        if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
            raise ValueError("status must be an HTTP status code")
        if error_code is not None and ERROR_CODE_PATTERN.fullmatch(error_code) is None:
            raise ValueError("error_code is not safe")
        if job_id is not None and IDENTIFIER_PATTERN.fullmatch(job_id) is None:
            raise ValueError("job_id is not safe")
        if job_type is not None and ERROR_CODE_PATTERN.fullmatch(job_type) is None:
            raise ValueError("job_type is not safe")
        if job_status is not None and job_status not in KNOWN_JOB_STATUSES:
            raise ValueError("job_status is not safe")
        event: dict[str, Any] = {
            "timestamp": time.time(),
            "request_id": request_id,
            "correlation_id": correlation_id,
            "method": method,
            "route": route_template(path),
            "http_status": status,
        }
        for name, value in (
            ("error_code", error_code),
            ("job_id", job_id),
            ("job_type", job_type),
            ("job_status", job_status),
        ):
            if value is not None:
                event[name] = value
        with self._lock:
            if len(self._events) == self.capacity:
                self._evicted_total += 1
            self._events.append(event)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "capacity": self.capacity,
                "retained": len(self._events),
                "evicted_total": self._evicted_total,
                "events": [dict(event) for event in self._events],
            }

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
            self._evicted_total = 0
