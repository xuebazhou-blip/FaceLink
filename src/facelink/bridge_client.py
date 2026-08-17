from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


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
        except (OSError, ValueError, TypeError, json.JSONDecodeError, httpx.HTTPError):
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
        response.raise_for_status()
        return response.json()

    def submit_job(self, job_type: str, payload: dict[str, Any] | None = None) -> str:
        response = httpx.post(
            f"{self.instance.base_url}/v1/jobs",
            headers=self._headers,
            json={"type": job_type, "payload": payload or {}},
            timeout=3.0,
        )
        response.raise_for_status()
        return str(response.json()["job_id"])

    def get_job(self, job_id: str) -> dict[str, Any]:
        response = httpx.get(
            f"{self.instance.base_url}/v1/jobs/{job_id}",
            headers=self._headers,
            timeout=3.0,
        )
        response.raise_for_status()
        return response.json()

    def wait_for_job(self, job_id: str, timeout: float = 30.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.get_job(job_id)
            if job["status"] == "succeeded":
                return job["result"]
            if job["status"] == "failed":
                raise RuntimeError(job.get("error", "Blender job failed"))
            time.sleep(0.08)
        raise TimeoutError(f"Blender job {job_id} did not finish within {timeout:g}s")

    def run_job(
        self, job_type: str, payload: dict[str, Any] | None = None, timeout: float = 30.0
    ) -> dict[str, Any]:
        return self.wait_for_job(self.submit_job(job_type, payload), timeout=timeout)


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

