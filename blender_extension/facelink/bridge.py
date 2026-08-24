import copy
import hmac
import json
import os
import secrets
import tempfile
import threading
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import bpy

from . import overlay
from .executor import (
    apply_patch,
    list_revision_history,
    rollback_to_revision,
    summarize_patch,
    undo_last_patch,
)
from .job_runtime import JobAdmissionError, JobRuntime, error_payload
from .snapshot import scan_scene

MAX_REQUEST_BYTES = 4 * 1024 * 1024
JOBS_PER_TICK = 8
SUPPORTED_JOB_TYPES = frozenset(
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


class RequestError(ValueError):
    def __init__(self, status: int, code: str, message: str, **details):
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = details


class Runtime:
    server = None
    thread = None
    token = None
    instance_id = None
    instance_file = None
    job_runtime = JobRuntime()
    static_health = {}
    staged_patch = None
    staged_summary = None


def _instance_directory():
    override = os.environ.get("FACELINK_INSTANCE_DIR")
    root = Path(override) if override else Path(tempfile.gettempdir()) / "facelink" / "instances"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_job(job_id):
    return Runtime.job_runtime.get(job_id)


def _health_payload():
    return {**Runtime.static_health, "job_runtime": Runtime.job_runtime.snapshot()}


class Handler(BaseHTTPRequestHandler):
    server_version = "FaceLink/0.3.9"

    def log_message(self, format, *args):
        return

    def _authorized(self):
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {Runtime.token}"
        return hmac.compare_digest(supplied, expected)

    def _send(self, status, payload, *, headers=None):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _send_error(
        self,
        status,
        code,
        message,
        *,
        retryable=False,
        details=None,
        retry_after_ms=None,
    ):
        headers = {"Retry-After": "1"} if retry_after_ms is not None else None
        self._send(
            status,
            error_payload(
                code,
                message,
                retryable=retryable,
                details=details,
                retry_after_ms=retry_after_ms,
            ),
            headers=headers,
        )

    def _read_job_request(self):
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else 0
        except ValueError as exc:
            raise RequestError(
                400,
                "invalid_content_length",
                "Content-Length must be a positive integer.",
            ) from exc
        if length <= 0:
            raise RequestError(
                400,
                "invalid_request_size",
                "The request body must not be empty.",
            )
        if length > MAX_REQUEST_BYTES:
            raise RequestError(
                413,
                "request_too_large",
                "The request body exceeds the 4 MiB limit.",
                max_bytes=MAX_REQUEST_BYTES,
            )
        try:
            request = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RequestError(400, "invalid_json", "The request body is not valid JSON.") from exc
        if not isinstance(request, dict):
            raise RequestError(400, "invalid_request", "The request body must be a JSON object.")
        job_type = request.get("type")
        if not isinstance(job_type, str) or job_type not in SUPPORTED_JOB_TYPES:
            raise RequestError(
                400,
                "unsupported_job_type",
                "The requested Blender job type is not supported.",
                supported_types=sorted(SUPPORTED_JOB_TYPES),
            )
        payload = request.get("payload", {})
        if not isinstance(payload, dict):
            raise RequestError(
                400,
                "invalid_job_payload",
                "The job payload must be a JSON object.",
            )
        return job_type, payload

    def do_GET(self):
        if not self._authorized():
            self._send_error(
                401,
                "authentication_required",
                "A valid FaceLink bridge bearer token is required.",
            )
            return
        if self.path == "/v1/health":
            self._send(200, _health_payload())
            return
        if self.path.startswith("/v1/jobs/"):
            job_id = self.path.rsplit("/", 1)[-1]
            job = _safe_job(job_id)
            if job:
                self._send(200, job)
            else:
                self._send_error(
                    404,
                    "job_not_found",
                    "The Blender job does not exist or its completed record has expired.",
                )
            return
        self._send_error(404, "route_not_found", "The requested bridge route does not exist.")

    def do_POST(self):
        if not self._authorized():
            self._send_error(
                401,
                "authentication_required",
                "A valid FaceLink bridge bearer token is required.",
            )
            return
        if self.path != "/v1/jobs":
            self._send_error(404, "route_not_found", "The requested bridge route does not exist.")
            return
        try:
            job_type, payload = self._read_job_request()
            job_id = "job-" + uuid.uuid4().hex[:16]
            Runtime.job_runtime.submit(job_id, job_type, payload)
            self._send(202, {"job_id": job_id, "status": "queued"})
        except RequestError as exc:
            self._send_error(exc.status, exc.code, str(exc), details=exc.details)
        except JobAdmissionError as exc:
            self._send(
                429,
                exc.as_payload(),
                headers={"Retry-After": "1"},
            )


def _process_jobs():
    for _ in range(JOBS_PER_TICK):
        pending = Runtime.job_runtime.take_nowait()
        if pending is None:
            break
        job_id, job_type, payload = pending
        Runtime.job_runtime.mark_running(job_id)
        try:
            if job_type == "scan_scene":
                result = scan_scene()
            elif job_type == "stage_patch":
                result = stage_patch(payload["patch"])
            elif job_type == "get_staged_patch":
                result = get_staged_patch()
            elif job_type == "apply_staged_patch":
                result = apply_staged_patch()
            elif job_type == "discard_staged_patch":
                result = discard_staged_patch()
            elif job_type == "apply_patch":
                result = apply_patch(payload["patch"])
            elif job_type == "undo":
                result = undo_last_patch()
            elif job_type == "list_revisions":
                result = list_revision_history()
            elif job_type == "rollback_revision":
                result = rollback_to_revision(payload["revision_id"])
            Runtime.job_runtime.mark_succeeded(job_id, result)
        except KeyError as exc:
            field = exc.args[0] if exc.args else "unknown"
            Runtime.job_runtime.mark_failed(
                job_id,
                code="invalid_job_payload",
                message=f"The job payload is missing the required field: {field}.",
            )
        except TypeError as exc:
            Runtime.job_runtime.mark_failed(
                job_id,
                code="invalid_job_payload",
                message=str(exc),
            )
        except ValueError as exc:
            Runtime.job_runtime.mark_failed(
                job_id,
                code="job_rejected",
                message=str(exc),
            )
        except Exception:
            traceback.print_exc()
            Runtime.job_runtime.mark_failed(
                job_id,
                code="internal_error",
                message="The Blender job failed unexpectedly.",
            )
    return 0.05 if Runtime.server else None


def is_running():
    return Runtime.server is not None


def stage_patch(patch):
    summary = summarize_patch(patch)
    summary["preview"] = overlay.set_preview(patch)
    previous = Runtime.staged_summary
    Runtime.staged_patch = copy.deepcopy(patch)
    Runtime.staged_summary = copy.deepcopy(summary)
    return {
        "staged": True,
        "replaced_patch_id": previous["patch_id"] if previous else None,
        "summary": copy.deepcopy(summary),
    }


def get_staged_patch():
    return {
        "staged": Runtime.staged_patch is not None,
        "patch": copy.deepcopy(Runtime.staged_patch),
        "summary": copy.deepcopy(Runtime.staged_summary),
    }


def apply_staged_patch():
    if Runtime.staged_patch is None:
        raise ValueError("No FaceLink patch is staged for review")
    patch = Runtime.staged_patch
    summary = Runtime.staged_summary
    receipt = apply_patch(patch)
    clear_staged_patch()
    return {"staged": False, "summary": summary, "receipt": receipt}


def discard_staged_patch():
    if Runtime.staged_patch is None:
        raise ValueError("No FaceLink patch is staged for review")
    patch_id = Runtime.staged_summary["patch_id"]
    clear_staged_patch()
    return {"staged": False, "discarded": True, "patch_id": patch_id}


def clear_staged_patch():
    Runtime.staged_patch = None
    Runtime.staged_summary = None
    overlay.clear_preview()


def connection_info():
    if not Runtime.server:
        return None
    return {
        "instance_id": Runtime.instance_id,
        "port": Runtime.server.server_port,
        "instance_file": str(Runtime.instance_file),
    }


def start_bridge():
    if Runtime.server:
        return connection_info()
    Runtime.token = secrets.token_urlsafe(32)
    Runtime.instance_id = f"blender-{os.getpid()}"
    Runtime.job_runtime.clear()
    Runtime.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Runtime.server.daemon_threads = True
    Runtime.static_health = {
        "ok": True,
        "facelink_version": "0.3.9",
        "protocol_version": "1.10",
        "instance_id": Runtime.instance_id,
        "blender_version": bpy.app.version_string,
        "capabilities": [
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
        ],
    }
    Runtime.thread = threading.Thread(
        target=Runtime.server.serve_forever, name="FaceLinkBridge", daemon=True
    )
    Runtime.thread.start()
    record = {
        "instance_id": Runtime.instance_id,
        "pid": os.getpid(),
        "port": Runtime.server.server_port,
        "token": Runtime.token,
        "scene_name": bpy.context.scene.name,
        "blender_version": bpy.app.version_string,
    }
    Runtime.instance_file = _instance_directory() / f"{Runtime.instance_id}.json"
    Runtime.instance_file.write_text(json.dumps(record, indent=2), encoding="utf-8")
    if not bpy.app.timers.is_registered(_process_jobs):
        bpy.app.timers.register(_process_jobs, first_interval=0.05, persistent=True)
    return connection_info()


def stop_bridge():
    server = Runtime.server
    Runtime.server = None
    if server:
        server.shutdown()
        server.server_close()
    if Runtime.thread:
        Runtime.thread.join(timeout=1.0)
    Runtime.thread = None
    if Runtime.instance_file and Runtime.instance_file.exists():
        Runtime.instance_file.unlink()
    Runtime.instance_file = None
    Runtime.token = None
    Runtime.instance_id = None
    Runtime.job_runtime.clear()
    Runtime.static_health = {}
