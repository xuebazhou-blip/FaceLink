from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
from bisect import insort
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx

CURRENT_PROTOCOL_VERSION = "1.13"
MINIMUM_PROTOCOL_VERSION = "1.12"
SUPPORTED_PROTOCOL_VERSIONS = ("1.12", "1.13")
PROTOCOL_VERSION_PATTERN = re.compile(r"^(0|[1-9]\d{0,8})\.(0|[1-9]\d{0,8})$")
CORRELATION_ID_PATTERN = re.compile(r"^corr-[0-9a-f]{32}$")
MAX_DISCOVERY_CANDIDATES = 32
MAX_INSTANCE_RECORD_BYTES = 16 * 1024
INSTANCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
INSTANCE_RECORD_FIELDS = frozenset(
    {
        "instance_id",
        "pid",
        "port",
        "token",
        "scene_name",
        "blender_version",
        "protocol_version",
    }
)
REQUIRED_INSTANCE_RECORD_FIELDS = INSTANCE_RECORD_FIELDS - {"protocol_version"}


def new_correlation_id() -> str:
    return "corr-" + uuid.uuid4().hex


def protocol_compatibility(
    version: Any,
    advertised_versions: Any = None,
) -> dict[str, Any]:
    if not isinstance(version, str) or PROTOCOL_VERSION_PATTERN.fullmatch(version) is None:
        return {
            "compatible": False,
            "status": "invalid",
            "error_code": "invalid_protocol_version",
            "actual_version": version if isinstance(version, str) and len(version) <= 32 else None,
            "minimum_version": MINIMUM_PROTOCOL_VERSION,
            "current_version": CURRENT_PROTOCOL_VERSION,
        }
    if advertised_versions is None:
        server_versions = (version,)
    elif not isinstance(advertised_versions, list) or not advertised_versions:
        return {
            "compatible": False,
            "status": "invalid",
            "error_code": "invalid_protocol_version",
            "actual_version": version,
            "minimum_version": MINIMUM_PROTOCOL_VERSION,
            "current_version": CURRENT_PROTOCOL_VERSION,
        }
    elif any(
        not isinstance(item, str) or PROTOCOL_VERSION_PATTERN.fullmatch(item) is None
        for item in advertised_versions
    ):
        return {
            "compatible": False,
            "status": "invalid",
            "error_code": "invalid_protocol_version",
            "actual_version": version,
            "minimum_version": MINIMUM_PROTOCOL_VERSION,
            "current_version": CURRENT_PROTOCOL_VERSION,
        }
    else:
        server_versions = tuple(dict.fromkeys(advertised_versions))
    overlap = sorted(
        set(server_versions) & set(SUPPORTED_PROTOCOL_VERSIONS),
        key=lambda item: tuple(int(value) for value in item.split(".")),
    )
    compatible = bool(overlap)
    negotiated_version = overlap[-1] if overlap else None
    status = (
        "current"
        if negotiated_version == CURRENT_PROTOCOL_VERSION
        else "previous_minor"
        if negotiated_version is not None
        else "unsupported"
    )
    result = {
        "compatible": compatible,
        "status": status,
        "actual_version": version,
        "minimum_version": MINIMUM_PROTOCOL_VERSION,
        "current_version": CURRENT_PROTOCOL_VERSION,
        "client_supported_versions": list(SUPPORTED_PROTOCOL_VERSIONS),
        "server_supported_versions": list(server_versions),
    }
    if negotiated_version is not None:
        result["negotiated_version"] = negotiated_version
    else:
        result["error_code"] = "unsupported_protocol_version"
    return result


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
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status_code = status_code
        self.details = dict(details or {})
        self.retry_after_ms = retry_after_ms
        self.request_id = request_id
        self.correlation_id = correlation_id


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
            request_id=(
                payload.get("request_id")
                if isinstance(payload.get("request_id"), str)
                else response.headers.get("X-FaceLink-Request-ID")
            ),
            correlation_id=(
                payload.get("correlation_id")
                if isinstance(payload.get("correlation_id"), str)
                else response.headers.get("X-FaceLink-Correlation-ID")
            ),
        ) from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise BridgeError(
            "FaceLink bridge returned a non-JSON response.",
            code="invalid_response",
            status_code=getattr(response, "status_code", None),
            request_id=response.headers.get("X-FaceLink-Request-ID"),
            correlation_id=response.headers.get("X-FaceLink-Correlation-ID"),
        ) from exc
    if not isinstance(payload, dict):
        raise BridgeError(
            "FaceLink bridge returned a JSON value that is not an object.",
            code="invalid_response",
            status_code=getattr(response, "status_code", None),
            request_id=response.headers.get("X-FaceLink-Request-ID"),
            correlation_id=response.headers.get("X-FaceLink-Correlation-ID"),
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
    protocol_version: str | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @classmethod
    def from_file(cls, path: Path) -> BlenderInstance:
        with path.open("rb") as handle:
            raw = handle.read(MAX_INSTANCE_RECORD_BYTES + 1)
        if len(raw) > MAX_INSTANCE_RECORD_BYTES:
            raise ValueError("FaceLink instance record exceeds the safe size limit.")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("FaceLink instance record is not valid UTF-8 JSON.") from None
        if not isinstance(payload, dict) or not _valid_instance_payload(payload):
            raise ValueError("FaceLink instance record has an invalid schema.")
        try:
            return cls(file_path=path, **payload)
        except (TypeError, ValueError):
            raise ValueError("FaceLink instance record has an invalid schema.") from None


def _bounded_record_text(value: Any, *, maximum: int, allow_empty: bool = False) -> bool:
    return (
        isinstance(value, str)
        and (allow_empty or bool(value))
        and len(value) <= maximum
        and all(character >= " " and character != "\x7f" for character in value)
    )


def _valid_instance_payload(payload: dict[str, Any]) -> bool:
    fields = set(payload)
    if not REQUIRED_INSTANCE_RECORD_FIELDS <= fields <= INSTANCE_RECORD_FIELDS:
        return False
    pid = payload["pid"]
    port = payload["port"]
    protocol_version = payload.get("protocol_version")
    return (
        isinstance(payload["instance_id"], str)
        and INSTANCE_ID_PATTERN.fullmatch(payload["instance_id"]) is not None
        and isinstance(pid, int)
        and not isinstance(pid, bool)
        and pid > 0
        and isinstance(port, int)
        and not isinstance(port, bool)
        and 1 <= port <= 65535
        and _bounded_record_text(payload["token"], maximum=512)
        and _bounded_record_text(payload["scene_name"], maximum=1024)
        and _bounded_record_text(payload["blender_version"], maximum=128)
        and (
            protocol_version is None
            or _bounded_record_text(protocol_version, maximum=32)
        )
    )


@dataclass(frozen=True)
class InstanceDiscovery:
    """Bounded discovery result; the legacy list API remains available below."""

    instances: tuple[BlenderInstance, ...]
    candidate_count: int
    skipped_candidate_count: int
    candidate_limit: int


def _bounded_instance_paths(directory: Path) -> tuple[list[Path], int]:
    """Select deterministic candidates while retaining only a bounded path set."""
    selected: list[tuple[str, str, str, Path]] = []
    candidate_count = 0
    with os.scandir(directory) as entries:
        for entry in entries:
            if not entry.name.casefold().endswith(".json"):
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            candidate_count += 1
            path = Path(entry.path)
            insort(
                selected,
                (entry.name.casefold(), entry.name, str(path), path),
            )
            if len(selected) > MAX_DISCOVERY_CANDIDATES:
                selected.pop()
    return [item[3] for item in selected], candidate_count


def discover_instances_with_metadata(*, verify: bool = True) -> InstanceDiscovery:
    """Discover a bounded set and report how many candidate records were not opened."""
    directory = instance_directory()
    if not directory.exists():
        return InstanceDiscovery((), 0, 0, MAX_DISCOVERY_CANDIDATES)
    try:
        paths, candidate_count = _bounded_instance_paths(directory)
    except OSError:
        return InstanceDiscovery((), 0, 0, MAX_DISCOVERY_CANDIDATES)
    instances: list[BlenderInstance] = []
    for path in paths:
        try:
            instance = BlenderInstance.from_file(path)
            if verify:
                health = BridgeClient(instance).health(timeout=0.6)
                version = health.get("protocol_version")
                if isinstance(version, str):
                    instance = replace(instance, protocol_version=version)
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
    instances.sort(
        key=lambda item: (
            item.instance_id,
            item.file_path.name.casefold(),
            item.file_path.name,
        )
    )
    return InstanceDiscovery(
        tuple(instances),
        candidate_count,
        max(0, candidate_count - len(paths)),
        MAX_DISCOVERY_CANDIDATES,
    )


def discover_instances(*, verify: bool = True) -> list[BlenderInstance]:
    """Backward-compatible list view over bounded instance discovery."""
    return list(discover_instances_with_metadata(verify=verify).instances)


class BridgeClient:
    def __init__(
        self,
        instance: BlenderInstance,
        *,
        correlation_id: str | None = None,
    ):
        self.instance = instance
        self.correlation_id = new_correlation_id() if correlation_id is None else correlation_id
        if CORRELATION_ID_PATTERN.fullmatch(self.correlation_id) is None:
            raise ValueError(
                "correlation_id must use corr- followed by 32 lowercase hexadecimal characters"
            )
        self._compatibility: dict[str, Any] | None = None

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.instance.token}",
            "FaceLink-Protocol-Version": (
                self._compatibility["negotiated_version"]
                if self._compatibility is not None
                else CURRENT_PROTOCOL_VERSION
            ),
            "X-FaceLink-Correlation-ID": self.correlation_id,
        }

    def ensure_compatible(self, *, timeout: float | None = None) -> dict[str, Any]:
        if self._compatibility is not None:
            return dict(self._compatibility)
        version = self.instance.protocol_version
        advertised = None
        health: dict[str, Any] = {}
        if version not in SUPPORTED_PROTOCOL_VERSIONS:
            health = self.health() if timeout is None else self.health(timeout=timeout)
            version = health.get("protocol_version")
            contract = health.get("protocol_compatibility")
            advertised = (
                contract.get("supported_versions") if isinstance(contract, dict) else None
            )
        compatibility = protocol_compatibility(version, advertised)
        if not compatibility["compatible"]:
            raise BridgeError(
                "The Blender bridge protocol is not compatible with this FaceLink client.",
                code=str(compatibility["error_code"]),
                details=compatibility,
                request_id=(
                    health.get("request_id")
                    if isinstance(health.get("request_id"), str)
                    else None
                ),
                correlation_id=(
                    health.get("correlation_id")
                    if isinstance(health.get("correlation_id"), str)
                    else None
                ),
            )
        self._compatibility = compatibility
        return dict(compatibility)

    def health(self, timeout: float = 2.0) -> dict[str, Any]:
        response = httpx.get(
            f"{self.instance.base_url}/v1/health", headers=self._headers, timeout=timeout
        )
        return _response_json(response)

    def recovery_status(self, timeout: float = 2.0) -> dict[str, Any]:
        self.ensure_compatible()
        response = httpx.get(
            f"{self.instance.base_url}/v1/recovery",
            headers=self._headers,
            timeout=timeout,
        )
        return _response_json(response)

    def acknowledge_recovery(self, timeout: float = 3.0) -> dict[str, Any]:
        self.ensure_compatible()
        response = httpx.post(
            f"{self.instance.base_url}/v1/recovery/acknowledge",
            headers=self._headers,
            timeout=timeout,
        )
        return _response_json(response)

    def diagnostics(self, timeout: float = 3.0) -> dict[str, Any]:
        self.ensure_compatible(timeout=timeout)
        response = httpx.get(
            f"{self.instance.base_url}/v1/diagnostics",
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
        self.ensure_compatible()
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
                request_id=(
                    result.get("request_id")
                    if isinstance(result.get("request_id"), str)
                    else None
                ),
                correlation_id=(
                    result.get("correlation_id")
                    if isinstance(result.get("correlation_id"), str)
                    else None
                ),
            )
        return job_id

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        self.ensure_compatible()
        response = httpx.post(
            f"{self.instance.base_url}/v1/jobs/{job_id}/cancel",
            headers=self._headers,
            timeout=3.0,
        )
        return _response_json(response)

    def get_job(self, job_id: str) -> dict[str, Any]:
        self.ensure_compatible()
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
                        request_id=(
                            job.get("request_id")
                            if isinstance(job.get("request_id"), str)
                            else None
                        ),
                        correlation_id=(
                            job.get("correlation_id")
                            if isinstance(job.get("correlation_id"), str)
                            else None
                        ),
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
                    request_id=(
                        job.get("request_id")
                        if isinstance(job.get("request_id"), str)
                        else None
                    ),
                    correlation_id=(
                        job.get("correlation_id")
                        if isinstance(job.get("correlation_id"), str)
                        else None
                    ),
                )
            if status not in {"queued", "running"}:
                raise BridgeError(
                    "FaceLink bridge returned an unknown job status.",
                    code="invalid_response",
                    details={"job_id": job_id, "status": status},
                    request_id=(
                        job.get("request_id")
                        if isinstance(job.get("request_id"), str)
                        else None
                    ),
                    correlation_id=(
                        job.get("correlation_id")
                        if isinstance(job.get("correlation_id"), str)
                        else None
                    ),
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
