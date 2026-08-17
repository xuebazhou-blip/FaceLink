import hmac
import json
import os
import queue
import secrets
import tempfile
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import bpy

from .executor import apply_patch, undo_last_patch
from .snapshot import scan_scene


class Runtime:
    server = None
    thread = None
    token = None
    instance_id = None
    instance_file = None
    jobs = {}
    lock = threading.Lock()
    pending = queue.Queue()
    static_health = {}


def _instance_directory():
    override = os.environ.get("FACELINK_INSTANCE_DIR")
    root = Path(override) if override else Path(tempfile.gettempdir()) / "facelink" / "instances"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_job(job_id):
    with Runtime.lock:
        job = Runtime.jobs.get(job_id)
        return dict(job) if job else None


class Handler(BaseHTTPRequestHandler):
    server_version = "FaceLink/0.1"

    def log_message(self, format, *args):
        return

    def _authorized(self):
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {Runtime.token}"
        return hmac.compare_digest(supplied, expected)

    def _send(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return
        if self.path == "/v1/health":
            self._send(200, Runtime.static_health)
            return
        if self.path.startswith("/v1/jobs/"):
            job_id = self.path.rsplit("/", 1)[-1]
            job = _safe_job(job_id)
            self._send(200 if job else 404, job or {"error": "job_not_found"})
            return
        self._send(404, {"error": "not_found"})

    def do_POST(self):
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return
        if self.path != "/v1/jobs":
            self._send(404, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 4 * 1024 * 1024:
                raise ValueError("invalid request size")
            request = json.loads(self.rfile.read(length).decode("utf-8"))
            job_type = request.get("type")
            if job_type not in {"scan_scene", "apply_patch", "undo"}:
                raise ValueError("unsupported job type")
            job_id = "job-" + uuid.uuid4().hex[:16]
            with Runtime.lock:
                Runtime.jobs[job_id] = {
                    "job_id": job_id,
                    "type": job_type,
                    "status": "queued",
                    "created_at": time.time(),
                }
            Runtime.pending.put((job_id, job_type, request.get("payload", {})))
            self._send(202, {"job_id": job_id, "status": "queued"})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._send(400, {"error": str(exc)})


def _process_jobs():
    for _ in range(8):
        try:
            job_id, job_type, payload = Runtime.pending.get_nowait()
        except queue.Empty:
            break
        with Runtime.lock:
            Runtime.jobs[job_id]["status"] = "running"
        try:
            if job_type == "scan_scene":
                result = scan_scene()
            elif job_type == "apply_patch":
                result = apply_patch(payload["patch"])
            elif job_type == "undo":
                result = undo_last_patch()
            with Runtime.lock:
                Runtime.jobs[job_id].update(status="succeeded", result=result)
        except (KeyError, TypeError, ValueError) as exc:
            with Runtime.lock:
                Runtime.jobs[job_id].update(status="failed", error=str(exc))
        except Exception as exc:
            traceback.print_exc()
            with Runtime.lock:
                Runtime.jobs[job_id].update(status="failed", error=str(exc))
    return 0.05 if Runtime.server else None


def is_running():
    return Runtime.server is not None


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
    Runtime.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Runtime.server.daemon_threads = True
    Runtime.static_health = {
        "ok": True,
        "protocol_version": "1.0",
        "instance_id": Runtime.instance_id,
        "blender_version": bpy.app.version_string,
        "capabilities": [
            "scan_scene",
            "keyframe_transform",
            "look_at",
            "play_clip",
            "ensure_camera",
            "undo",
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
    with Runtime.lock:
        Runtime.jobs.clear()
