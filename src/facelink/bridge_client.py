from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


class BridgeError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool = False,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
        retry_after_ms: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status_code = status_code
        self.details = dict(details or {})
        self.retry_after_ms = retry_after_ms


def _response_json(response: httpx.Response) -> dict[str, Any]:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        status_code = response.status_code
        message = payload.get("error") or f"FaceLink bridge returned HTTP {status_code}."
        raise BridgeError(
            str(message),
            code=str(payload.get("error_code") or "http_error"),
            retryable=payload.get("retryable") is True,
            status_code=status_code,
            details=payload.get("details") if isinstance(payload.get("details"), dict) else None,
            retry_after_ms=(
                payload.get("retry_after_ms")
                if isinstance(payload.get("retry_after_ms"), int)
                else None
            ),
        ) from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise BridgeError(
            "FaceLink bridge returned a non-JSON response.",
            code="invalid_response",
            status_code=getattr(response, "status_code", None),
        ) from exc
    if not isinstance(payload, dict):
        raise BridgeError(
            "FaceLink bridge returned a JSON value that is not an object.",
            code="invalid_response",
            status_code=getattr(response, "status_code", None),
        )
    return payload


def instance_directory() -> Path:
    override = os.environ.get("FACELINK_INSTANCE_DIR")
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / "facelink" / "instances"


@dataclass(frozen=True)
class BlenderInstance:
    instance_id: str
    pid: int
    port: int
    token: str
    scene_name: str
    blender_version: str
    file_path: Path

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @classmethod
    def from_file(cls, path: Path) -> BlenderInstance:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(file_path=path, **payload)


def discover_instances(*, verify: bool = True) -> list[BlenderInstance]:
    directory = instance_directory()
    if not directory.exists():
        return []
    instances: list[BlenderInstance] = []
    for path in directory.glob("*.json"):
        try:
            instance = BlenderInstance.from_file(path)
            if verify:
                BridgeClient(instance).health(timeout=0.6)
            instances.append(instance)
        except (
            OSError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
            httpx.HTTPError,
            BridgeError,
        ):
            continue
    return sorted(instances, key=lambda item: item.instance_id)


class BridgeClient:
    def __init__(self, instance: BlenderInstance):
        self.instance = instance

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.instance.token}"}

    def health(self, timeout: float = 2.0) -> dict[str, Any]:
        response = httpx.get(
            f"{self.instance.base_url}/v1/health", headers=self._headers, timeout=timeout
        )
        return _response_json(response)

    def recovery_status(self, timeout: float = 2.0) -> dict[str, Any]:
        response = httpx.get(
            f"{self.instance.base_url}/v1/recovery",
            headers=self._headers,
            timeout=timeout,
        )
        return _response_json(response)

    def acknowledge_recovery(self, timeout: float = 3.0) -> dict[str, Any]:
        response = httpx.post(
            f"{self.instance.base_url}/v1/recovery/acknowledge",
            headers=self._headers,
            timeout=timeout,
        )
        return _response_json(response)

    def submit_job(
        self,
        job_type: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout_ms: int | None = None,
    ) -> str:
        request = {"type": job_type, "payload": payload or {}}
        if timeout_ms is not None:
            request["timeout_ms"] = timeout_ms
        response = httpx.post(
            f"{self.instance.base_url}/v1/jobs",
            headers=self._headers,
            json=request,
            timeout=3.0,
        )
        result = _response_json(response)
        job_id = result.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise BridgeError(
                "FaceLink bridge accepted a job without returning a job ID.",
                code="invalid_response",
            )
        return job_id

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        response = httpx.post(
            f"{self.instance.base_url}/v1/jobs/{job_id}/cancel",
            headers=self._headers,
            timeout=3.0,
        )
        return _response_json(response)

    def get_job(self, job_id: str) -> dict[str, Any]:
        response = httpx.get(
            f"{self.instance.base_url}/v1/jobs/{job_id}",
            headers=self._headers,
            timeout=3.0,
        )
        return _response_json(response)

    def wait_for_job(self, job_id: str, timeout: float = 30.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.get_job(job_id)
            status = job.get("status")
            if status == "succeeded":
                if "result" not in job:
                    raise BridgeError(
                        "A succeeded Blender job did not include a result.",
                        code="invalid_response",
                    )
                return job["result"]
            if status in {"failed", "cancelled", "timed_out", "interrupted"}:
                fallback_codes = {
                    "failed": "job_failed",
                    "cancelled": "job_cancelled",
                    "timed_out": "job_timed_out",
                    "interrupted": "job_interrupted",
                }
                raise BridgeError(
                    str(job.get("error", "Blender job failed")),
                    code=str(job.get("error_code") or fallback_codes[status]),
                    retryable=job.get("retryable") is True,
                    details=job.get("details") if isinstance(job.get("details"), dict) else None,
                    retry_after_ms=(
                        job.get("retry_after_ms")
                        if isinstance(job.get("retry_after_ms"), int)
                        else None
                    ),
                )
            if status not in {"queued", "running"}:
                raise BridgeError(
                    "FaceLink bridge returned an unknown job status.",
                    code="invalid_response",
                    details={"job_id": job_id, "status": status},
                )
            time.sleep(0.08)
        raise TimeoutError(f"Blender job {job_id} did not finish within {timeout:g}s")

    def run_job(
        self,
        job_type: str,
        payload: dict[str, Any] | None = None,
        timeout: float = 30.0,
        *,
        job_timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        return self.wait_for_job(
            self.submit_job(job_type, payload, timeout_ms=job_timeout_ms),
            timeout=timeout,
        )


def select_instance(instance_id: str | None = None) -> BlenderInstance:
    instances = discover_instances()
    if not instances:
        raise RuntimeError("No running FaceLink Blender instance was discovered.")
    if instance_id is None:
        if len(instances) > 1:
            names = ", ".join(instance.instance_id for instance in instances)
            raise RuntimeError(f"Multiple Blender instances found; choose one of: {names}")
        return instances[0]
    for instance in instances:
        if instance.instance_id == instance_id:
            return instance
    raise RuntimeError(f"Blender instance '{instance_id}' was not found.")
