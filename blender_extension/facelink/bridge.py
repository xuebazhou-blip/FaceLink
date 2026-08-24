import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
import traceback
import uuid
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import bpy

from . import overlay
from .diagnostic_events import DiagnosticEventBuffer
from .executor import (
    SUPPORTED_PATCH_SCHEMA_VERSIONS,
    UnsupportedSchemaError,
    apply_patch,
    list_revision_history,
    rollback_to_revision,
    summarize_patch,
    undo_last_patch,
)
from .job_journal import (
    MUTATING_JOB_TYPES,
    JobJournal,
    JournalError,
    sanitize_job_records,
    workspace_identity,
)
from .job_runtime import (
    JobAdmissionError,
    JobControlError,
    JobInterruption,
    JobRuntime,
    error_payload,
)
from .product_status import JOB_ID_PATTERN, build_product_status
from .snapshot import scan_scene

MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_JOB_TIMEOUT_MS = 60 * 60 * 1000
JOBS_PER_TICK = 8
PROTOCOL_VERSION = "1.13"
SUPPORTED_PROTOCOL_VERSIONS = ("1.12", "1.13")
PROTOCOL_VERSION_PATTERN = re.compile(r"^(0|[1-9]\d{0,8})\.(0|[1-9]\d{0,8})$")
CORRELATION_ID_PATTERN = re.compile(r"^corr-[0-9a-f]{32}$")
MAX_DIAGNOSTIC_JOBS = 64
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


class CancellationPersistenceError(JournalError):
    def __init__(self, job_status):
        self.job_status = job_status if job_status in {"cancelled", "running"} else "unknown"
        super().__init__("The cancellation state could not be persisted.")


class Runtime:
    server = None
    thread = None
    token = None
    instance_id = None
    instance_file = None
    session_id = None
    job_runtime = JobRuntime()
    job_journal = None
    persisted_job_version = None
    journal_lock = threading.Lock()
    lifecycle_lock = threading.Lock()
    stopping = False
    diagnostic_events = DiagnosticEventBuffer()
    static_health = {}
    staged_patch = None
    staged_summary = None


def _instance_directory():
    override = os.environ.get("FACELINK_INSTANCE_DIR")
    root = Path(override) if override else Path(tempfile.gettempdir()) / "facelink" / "instances"
    root.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(root, 0o700)
    return root


def _write_instance_record(path, record):
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, indent=2))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _state_directory():
    override = os.environ.get("FACELINK_STATE_DIR")
    if override:
        root = Path(override)
    else:
        root = Path(bpy.utils.user_resource("CONFIG")) / "facelink" / "state"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _persist_job_journal(*, clean_shutdown=False, force=False):
    with Runtime.journal_lock:
        journal = Runtime.job_journal
        if journal is None:
            return
        version, records = Runtime.job_runtime.records_with_version()
        runtime_version = (id(Runtime.job_runtime), version)
        if not force and not clean_shutdown and runtime_version == Runtime.persisted_job_version:
            return
        journal.persist(records, clean_shutdown=clean_shutdown)
        Runtime.persisted_job_version = runtime_version


def _recovery_status():
    if Runtime.job_journal is None:
        return None
    return Runtime.job_journal.status()


def _record_job_checkpoint(job_id, checkpoint, metadata=None):
    Runtime.job_runtime.set_checkpoint(job_id, checkpoint, metadata)
    _persist_job_journal()


def _safe_job(job_id):
    job = Runtime.job_runtime.get(job_id)
    _persist_job_journal()
    return job


def _health_payload():
    job_runtime = Runtime.job_runtime.snapshot()
    _persist_job_journal()
    recovery = _recovery_status() or {}
    return {
        **Runtime.static_health,
        "job_runtime": job_runtime,
        "recovery": {
            "required": recovery.get("required", False),
            "reason": recovery.get("reason", "none"),
            "interrupted_job_count": len(recovery.get("interrupted_jobs", [])),
            "persistent_workspace": recovery.get("persistent_workspace", False),
        },
    }


def _diagnostic_payload():
    job_runtime = Runtime.job_runtime.snapshot()
    _persist_job_journal()
    _version, records = Runtime.job_runtime.records_with_version()
    recovery = _recovery_status() or {}
    return {
        "schema_version": "1.0",
        "facelink_version": Runtime.static_health.get("facelink_version"),
        "protocol_version": PROTOCOL_VERSION,
        "blender_version": bpy.app.version_string,
        "capabilities": sorted(Runtime.static_health.get("capabilities", [])),
        "job_runtime": job_runtime,
        "recovery": {
            "required": recovery.get("required", False),
            "reason": recovery.get("reason", "none"),
            "unclean_shutdown": recovery.get("unclean_shutdown", False),
            "interrupted_job_count": len(recovery.get("interrupted_jobs", [])),
            "persistent_workspace": recovery.get("persistent_workspace", False),
        },
        "recent_jobs": sanitize_job_records(records)[-MAX_DIAGNOSTIC_JOBS:],
        "events": Runtime.diagnostic_events.snapshot(),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "FaceLink/0.3.9"

    def log_message(self, format, *args):
        return

    def _authorized(self):
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {Runtime.token}"
        return hmac.compare_digest(supplied, expected)

    def _prepare_request(self):
        self.request_id = "req-" + uuid.uuid4().hex[:16]
        supplied_correlation = self.headers.get("X-FaceLink-Correlation-ID")
        self.correlation_id = "corr-" + uuid.uuid4().hex
        self._correlation_valid = supplied_correlation is None or (
            CORRELATION_ID_PATTERN.fullmatch(supplied_correlation) is not None
        )
        if supplied_correlation is not None and self._correlation_valid:
            digest = hmac.new(
                Runtime.token.encode("utf-8"),
                supplied_correlation.encode("ascii"),
                hashlib.sha256,
            ).hexdigest()[:32]
            self.correlation_id = "corr-" + digest

        supplied_protocol = self.headers.get("FaceLink-Protocol-Version")
        self.negotiated_protocol = supplied_protocol or SUPPORTED_PROTOCOL_VERSIONS[0]
        self._protocol_error = None
        if supplied_protocol is None:
            return
        match = PROTOCOL_VERSION_PATTERN.fullmatch(supplied_protocol)
        if match is None:
            self._protocol_error = (
                400,
                "invalid_protocol_version",
                "The client protocol version must use major.minor integer notation.",
            )
            return
        if supplied_protocol not in SUPPORTED_PROTOCOL_VERSIONS:
            self._protocol_error = (
                426,
                "unsupported_protocol_version",
                "The requested client protocol version is not supported by this bridge.",
            )

    def _request_contract_valid(self, *, check_protocol=True):
        if not self._correlation_valid:
            self._send_error(
                400,
                "invalid_correlation_id",
                "The correlation ID must be a FaceLink-formatted random identifier.",
            )
            return False
        if check_protocol and self._protocol_error is not None:
            status, code, message = self._protocol_error
            details = {
                "supported_versions": list(SUPPORTED_PROTOCOL_VERSIONS),
                "min_supported": SUPPORTED_PROTOCOL_VERSIONS[0],
                "max_supported": SUPPORTED_PROTOCOL_VERSIONS[-1],
            }
            if status == 426:
                details["requested"] = self.negotiated_protocol
            self._send_error(
                status,
                code,
                message,
                details=details,
            )
            return False
        return True

    def _send(self, status, payload, *, headers=None):
        response_payload = dict(payload)
        response_payload.setdefault("request_id", self.request_id)
        response_payload.setdefault("correlation_id", self.correlation_id)
        data = json.dumps(response_payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-FaceLink-Request-ID", self.request_id)
        self.send_header("X-FaceLink-Correlation-ID", self.correlation_id)
        response_protocol = getattr(self, "negotiated_protocol", PROTOCOL_VERSION)
        if self.path == "/v1/health" or response_protocol not in SUPPORTED_PROTOCOL_VERSIONS:
            response_protocol = PROTOCOL_VERSION
        self.send_header("FaceLink-Protocol-Version", response_protocol)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(data)
        finally:
            try:
                Runtime.diagnostic_events.record(
                    request_id=self.request_id,
                    correlation_id=self.correlation_id,
                    method=self.command,
                    path=self.path,
                    status=status,
                    error_code=(
                        response_payload.get("error_code")
                        if isinstance(response_payload.get("error_code"), str)
                        else None
                    ),
                    job_id=(
                        response_payload.get("job_id")
                        if isinstance(response_payload.get("job_id"), str)
                        else None
                    ),
                    job_type=(
                        response_payload.get("type")
                        if isinstance(response_payload.get("type"), str)
                        else None
                    ),
                    job_status=(
                        response_payload.get("status")
                        if isinstance(response_payload.get("status"), str)
                        else None
                    ),
                )
            except ValueError:
                pass

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
        timeout_ms = request.get("timeout_ms")
        if timeout_ms is not None and (
            isinstance(timeout_ms, bool)
            or not isinstance(timeout_ms, int)
            or not 1 <= timeout_ms <= MAX_JOB_TIMEOUT_MS
        ):
            raise RequestError(
                400,
                "invalid_job_timeout",
                "timeout_ms must be an integer from 1 through 3600000.",
                min_timeout_ms=1,
                max_timeout_ms=MAX_JOB_TIMEOUT_MS,
            )
        return job_type, payload, timeout_ms

    def _cancel_job(self, job_id):
        try:
            job = cancel_job(job_id)
        except JobControlError as exc:
            status = (
                404
                if exc.code == "job_not_found"
                else 503
                if exc.code == "bridge_stopping"
                else 409
            )
            self._send_error(
                status,
                exc.code,
                str(exc),
                details=exc.details,
                retryable=exc.code == "bridge_stopping",
            )
            return
        except CancellationPersistenceError as exc:
            message = (
                "Cancellation was requested, but its record could not be persisted; "
                "check the final job status."
                if exc.job_status == "running"
                else "The queued job was removed from this session, but its cancellation "
                "record could not be persisted."
            )
            self._send_error(
                503,
                "journal_unavailable",
                message,
                details={"cancellation_status": exc.job_status},
                retryable=True,
            )
            return
        except JournalError:
            self._send_error(
                503,
                "journal_unavailable",
                "The task journal could not be persisted.",
                retryable=True,
            )
            return
        self._send(202 if job["status"] == "running" else 200, job)

    def do_GET(self):
        self._prepare_request()
        if not self._authorized():
            self._send_error(
                401,
                "authentication_required",
                "A valid FaceLink bridge bearer token is required.",
            )
            return
        if not self._request_contract_valid(check_protocol=self.path != "/v1/health"):
            return
        if self.path == "/v1/health":
            try:
                self._send(200, _health_payload())
            except JournalError:
                self._send_error(
                    503,
                    "journal_unavailable",
                    "The task journal could not be persisted.",
                    retryable=True,
                )
            return
        if self.path == "/v1/recovery":
            self._send(200, _recovery_status())
            return
        if self.path == "/v1/diagnostics":
            try:
                self._send(200, _diagnostic_payload())
            except JournalError:
                self._send_error(
                    503,
                    "journal_unavailable",
                    "The task journal could not be persisted.",
                    retryable=True,
                )
            return
        if self.path.startswith("/v1/jobs/"):
            job_id = self.path.rsplit("/", 1)[-1]
            try:
                job = _safe_job(job_id)
            except JournalError:
                self._send_error(
                    503,
                    "journal_unavailable",
                    "The task journal could not be persisted.",
                    retryable=True,
                )
                return
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
        self._prepare_request()
        if not self._authorized():
            self._send_error(
                401,
                "authentication_required",
                "A valid FaceLink bridge bearer token is required.",
            )
            return
        if not self._request_contract_valid():
            return
        if self.path == "/v1/recovery/acknowledge":
            try:
                self._send(200, acknowledge_recovery())
            except JobControlError as exc:
                self._send_error(
                    503,
                    exc.code,
                    str(exc),
                    details=exc.details,
                    retryable=True,
                )
            except JournalError:
                self._send_error(
                    503,
                    "journal_unavailable",
                    "The recovery acknowledgement could not be persisted.",
                    retryable=True,
                )
            return
        parts = self.path.split("/")
        if len(parts) == 6 and parts[1:3] == ["v1", "jobs"] and parts[4:] == ["cancel", ""]:
            self._cancel_job(parts[3])
            return
        if len(parts) == 5 and parts[1:3] == ["v1", "jobs"] and parts[4] == "cancel":
            self._cancel_job(parts[3])
            return
        if self.path != "/v1/jobs":
            self._send_error(404, "route_not_found", "The requested bridge route does not exist.")
            return
        try:
            job_type, payload, timeout_ms = self._read_job_request()
            with Runtime.lifecycle_lock:
                if Runtime.stopping:
                    self._send_error(
                        503,
                        "bridge_stopping",
                        "The Blender bridge is stopping and cannot accept new jobs.",
                        retryable=True,
                    )
                    return
                recovery = _recovery_status() or {}
                if (
                    job_type in MUTATING_JOB_TYPES
                    and recovery.get("required") is True
                ):
                    self._send_error(
                        409,
                        "recovery_review_required",
                        "Review and acknowledge the previous session before changing the scene.",
                    )
                    return
                job_id = "job-" + uuid.uuid4().hex[:16]
                accepted_job = Runtime.job_runtime.submit(
                    job_id,
                    job_type,
                    payload,
                    timeout_ms=timeout_ms,
                    request_id=self.request_id,
                    correlation_id=self.correlation_id,
                )
                try:
                    _persist_job_journal()
                except JournalError:
                    Runtime.job_runtime.cancel(job_id)
                    self._send_error(
                        503,
                        "journal_unavailable",
                        "The task journal could not be persisted; the job was not accepted.",
                        retryable=True,
                    )
                    return
            self._send(202, accepted_job)
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
        pending = Runtime.job_runtime.start_next()
        if pending is None:
            break
        job_id, job_type, payload = pending
        checkpoint = partial(Runtime.job_runtime.raise_if_interrupted, job_id)
        record_checkpoint = partial(_record_job_checkpoint, job_id)
        try:
            _persist_job_journal()
            checkpoint()
            if job_type == "scan_scene":
                result = scan_scene()
                checkpoint()
            elif job_type == "stage_patch":
                result = stage_patch(payload["patch"], check_interrupted=checkpoint)
            elif job_type == "get_staged_patch":
                result = get_staged_patch()
                checkpoint()
            elif job_type == "apply_staged_patch":
                result = apply_staged_patch(
                    check_interrupted=checkpoint,
                    record_checkpoint=record_checkpoint,
                )
            elif job_type == "discard_staged_patch":
                result = discard_staged_patch()
            elif job_type == "apply_patch":
                result = apply_patch(
                    payload["patch"],
                    check_interrupted=checkpoint,
                    record_checkpoint=record_checkpoint,
                )
            elif job_type == "undo":
                result = undo_last_patch()
            elif job_type == "list_revisions":
                result = list_revision_history()
                checkpoint()
            elif job_type == "rollback_revision":
                result = rollback_to_revision(payload["revision_id"])
            Runtime.job_runtime.mark_succeeded(job_id, result)
        except JobInterruption as exc:
            Runtime.job_runtime.mark_interrupted(job_id, exc)
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
        except UnsupportedSchemaError as exc:
            Runtime.job_runtime.mark_failed(
                job_id,
                code="unsupported_schema_version",
                message="The requested data schema is not supported by this Blender bridge.",
                details={
                    "schema_kind": exc.schema_kind,
                    "requested": exc.requested,
                    "supported": exc.supported,
                },
            )
        except ValueError as exc:
            Runtime.job_runtime.mark_failed(
                job_id,
                code="job_rejected",
                message=str(exc),
            )
        except JournalError:
            job = Runtime.job_runtime.get(job_id)
            if job and job.get("transaction_committed") is True:
                metadata = job.get("checkpoint_metadata", {})
                Runtime.job_runtime.set_checkpoint(
                    job_id,
                    "transaction_rolled_back",
                    {
                        "revision_id": metadata.get("revision_id"),
                        "rolled_back_count": metadata.get("operation_count", 0),
                    },
                )
            Runtime.job_runtime.mark_failed(
                job_id,
                code="journal_unavailable",
                message="The task journal became unavailable; the job stopped safely.",
                retryable=True,
            )
        except Exception:
            traceback.print_exc()
            Runtime.job_runtime.mark_failed(
                job_id,
                code="internal_error",
                message="The Blender job failed unexpectedly.",
            )
        try:
            _persist_job_journal()
        except JournalError:
            traceback.print_exc()
    return 0.05 if Runtime.server else None


def is_running():
    return Runtime.server is not None


def workspace_matches_current_file():
    with Runtime.lifecycle_lock:
        if Runtime.server is None or Runtime.job_journal is None:
            return True
        workspace_id, _persistent = workspace_identity(bpy.data.filepath, os.getpid())
        return Runtime.job_journal.workspace_id == workspace_id


def product_status(
    *, startup_error_code=None, startup_error_version=None, transition_code=None
):
    """Return a bounded, content-free model for the Blender product panel."""
    with Runtime.lifecycle_lock:
        running = Runtime.server is not None and not Runtime.stopping
        view = {"runtime": {}, "jobs": []}
        recovery = {}
        instance_id = None
        protocol_version = None
        if running:
            view = Runtime.job_runtime.task_center_view()
            recovery = _recovery_status() or {}
            instance_id = Runtime.instance_id
            protocol_version = Runtime.static_health.get("protocol_version")
        return build_product_status(
            running=running,
            instance_id=instance_id,
            protocol_version=protocol_version,
            runtime=view["runtime"],
            records=view["jobs"],
            recovery=recovery,
            staged=Runtime.staged_patch is not None,
            startup_error_code=startup_error_code,
            startup_error_version=startup_error_version,
            transition_code=transition_code,
        )


def cancel_job(job_id):
    if not isinstance(job_id, str) or JOB_ID_PATTERN.fullmatch(job_id) is None:
        raise JobControlError(
            "job_not_found",
            "The Blender job does not exist or its completed record has expired.",
            details={},
        )
    with Runtime.lifecycle_lock:
        if Runtime.stopping or Runtime.server is None:
            raise JobControlError(
                "bridge_stopping",
                "The Blender bridge is not available for job control.",
                details={},
            )
        job = Runtime.job_runtime.cancel(job_id)
        if job is None:
            raise JobControlError(
                "job_not_found",
                "The Blender job does not exist or its completed record has expired.",
                details={},
            )
        try:
            _persist_job_journal()
        except JournalError as exc:
            raise CancellationPersistenceError(job["status"]) from exc
        return job


def acknowledge_recovery():
    with Runtime.lifecycle_lock:
        if Runtime.stopping or Runtime.server is None or Runtime.job_journal is None:
            raise JobControlError(
                "bridge_stopping",
                "The Blender bridge is not available for recovery control.",
                details={},
            )
        return Runtime.job_journal.acknowledge()


def stage_patch(patch, *, check_interrupted=None):
    def checkpoint():
        if check_interrupted is not None:
            check_interrupted()

    checkpoint()
    summary = summarize_patch(patch)
    checkpoint()
    previous = Runtime.staged_summary
    previous_patch = Runtime.staged_patch
    summary["preview"] = overlay.set_preview(patch)
    try:
        checkpoint()
    except Exception:
        if previous_patch is None:
            overlay.clear_preview()
        else:
            overlay.set_preview(previous_patch)
        raise
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


def apply_staged_patch(*, check_interrupted=None, record_checkpoint=None):
    if Runtime.staged_patch is None:
        raise ValueError("No FaceLink patch is staged for review")
    patch = Runtime.staged_patch
    summary = Runtime.staged_summary
    receipt = apply_patch(
        patch,
        check_interrupted=check_interrupted,
        record_checkpoint=record_checkpoint,
    )
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


def _cleanup_failed_bridge_start():
    server = Runtime.server
    thread = Runtime.thread
    Runtime.stopping = True
    if server is not None and thread is not None and thread.is_alive():
        server.shutdown()
        thread.join(timeout=1.0)
    if server is not None:
        server.server_close()
    if bpy.app.timers.is_registered(_process_jobs):
        bpy.app.timers.unregister(_process_jobs)
    if Runtime.instance_file is not None:
        try:
            Runtime.instance_file.unlink(missing_ok=True)
        except OSError:
            pass
    if Runtime.job_journal is not None:
        try:
            _persist_job_journal(clean_shutdown=True, force=True)
        except JournalError:
            pass
    Runtime.server = None
    Runtime.thread = None
    Runtime.instance_file = None
    Runtime.token = None
    Runtime.instance_id = None
    Runtime.session_id = None
    Runtime.job_journal = None
    Runtime.persisted_job_version = None
    Runtime.static_health = {}
    Runtime.job_runtime.clear()
    Runtime.diagnostic_events.clear()
    Runtime.stopping = False


def _start_bridge_once():
    Runtime.token = secrets.token_urlsafe(32)
    Runtime.stopping = False
    Runtime.diagnostic_events.clear()
    Runtime.instance_id = f"blender-{os.getpid()}"
    Runtime.session_id = "session-" + uuid.uuid4().hex
    Runtime.job_runtime.clear()
    workspace_id, persistent_workspace = workspace_identity(bpy.data.filepath, os.getpid())
    Runtime.job_journal = JobJournal(
        _state_directory() / f"{workspace_id}.json",
        workspace_id=workspace_id,
        persistent_workspace=persistent_workspace,
        session_id=Runtime.session_id,
    )
    Runtime.persisted_job_version = None
    Runtime.job_journal.start()
    _persist_job_journal(force=True)
    Runtime.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Runtime.server.daemon_threads = True
    Runtime.static_health = {
        "ok": True,
        "facelink_version": "0.3.9",
        "protocol_version": PROTOCOL_VERSION,
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
            "job_deadlines",
            "safe_job_cancellation",
            "persistent_job_journal",
            "startup_recovery",
            "recovery_mutation_gate",
            "correlation_ids",
            "sanitized_diagnostics",
            "protocol_negotiation",
        ],
        "job_control": {
            "cancel_endpoint": "/v1/jobs/{id}/cancel",
            "max_timeout_ms": MAX_JOB_TIMEOUT_MS,
            "running_cancellation": "cooperative_safe_checkpoints",
        },
        "recovery_control": {
            "status_endpoint": "/v1/recovery",
            "acknowledge_endpoint": "/v1/recovery/acknowledge",
            "automatic_replay": False,
        },
        "protocol_compatibility": {
            "min_supported": SUPPORTED_PROTOCOL_VERSIONS[0],
            "max_supported": SUPPORTED_PROTOCOL_VERSIONS[-1],
            "supported_versions": list(SUPPORTED_PROTOCOL_VERSIONS),
            "legacy_default": SUPPORTED_PROTOCOL_VERSIONS[0],
            "request_header": "FaceLink-Protocol-Version",
            "minor_versions_are_additive": True,
        },
        "schema_versions": {
            "scene_snapshot": {
                "write": "1.4",
            },
            "scene_patch": {
                "accept": list(SUPPORTED_PATCH_SCHEMA_VERSIONS),
                "preferred": "1.4",
            },
            "job_journal": {
                "read": ["1.0"],
                "write": "1.0",
            },
            "revision_audit_entry": {
                "read": ["legacy", "1.0"],
                "write": "1.0",
            },
            "sanitized_diagnostics": {
                "write": "1.0",
            },
        },
        "diagnostic_control": {
            "endpoint": "/v1/diagnostics",
            "max_events": Runtime.diagnostic_events.capacity,
            "max_recent_jobs": MAX_DIAGNOSTIC_JOBS,
            "contains_request_content": False,
        },
    }
    record = {
        "instance_id": Runtime.instance_id,
        "pid": os.getpid(),
        "port": Runtime.server.server_port,
        "token": Runtime.token,
        "scene_name": bpy.context.scene.name,
        "blender_version": bpy.app.version_string,
    }
    Runtime.instance_file = _instance_directory() / f"{Runtime.instance_id}.json"
    _write_instance_record(Runtime.instance_file, record)
    Runtime.thread = threading.Thread(
        target=Runtime.server.serve_forever, name="FaceLinkBridge", daemon=True
    )
    Runtime.thread.start()
    if not bpy.app.timers.is_registered(_process_jobs):
        bpy.app.timers.register(_process_jobs, first_interval=0.05, persistent=True)
    return connection_info()


def start_bridge():
    if Runtime.server:
        return connection_info()
    try:
        return _start_bridge_once()
    except Exception:
        _cleanup_failed_bridge_start()
        raise


def stop_bridge():
    server = Runtime.server
    with Runtime.lifecycle_lock:
        Runtime.stopping = True
        Runtime.server = None
        Runtime.job_runtime.interrupt_active()
        try:
            _persist_job_journal(clean_shutdown=True, force=True)
        except JournalError:
            pass
    if server:
        try:
            server.shutdown()
        except OSError:
            pass
        try:
            server.server_close()
        except OSError:
            pass
    if Runtime.thread:
        Runtime.thread.join(timeout=1.0)
    Runtime.thread = None
    if Runtime.instance_file:
        try:
            Runtime.instance_file.unlink(missing_ok=True)
        except OSError:
            pass
    Runtime.instance_file = None
    Runtime.token = None
    Runtime.instance_id = None
    Runtime.job_runtime.clear()
    Runtime.session_id = None
    Runtime.job_journal = None
    Runtime.persisted_job_version = None
    Runtime.static_health = {}
