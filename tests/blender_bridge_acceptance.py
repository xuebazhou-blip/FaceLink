import hashlib
import http.client
import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import bpy

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "blender_extension"))
instance_root = PROJECT / ".cache" / "bridge-instances" / str(os.getpid())
state_root = PROJECT / ".cache" / "bridge-state" / str(os.getpid())
os.environ["FACELINK_INSTANCE_DIR"] = str(instance_root)
os.environ["FACELINK_STATE_DIR"] = str(state_root)

from facelink.job_journal import JournalError  # noqa: E402
from facelink.snapshot import ensure_entity_id  # noqa: E402

import facelink as blender_addon  # noqa: E402
from facelink import bridge  # noqa: E402
from facelink import executor as blender_executor  # noqa: E402
from facelink import operators as blender_operators  # noqa: E402
from facelink import panels as blender_panels  # noqa: E402

REQUEST_ID_PATTERN = re.compile(r"^req-[0-9a-f]{16}$")
CORRELATION_ID_PATTERN = re.compile(r"^corr-[0-9a-f]{32}$")


class OperatorReportSink:
    def __init__(self):
        self.reports = []

    def report(self, levels, message):
        self.reports.append((set(levels), message))


def request_json_response_with_headers(
    method,
    url,
    token,
    payload=None,
    raw_data=None,
    extra_headers=None,
):
    data = raw_data
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    headers.update(extra_headers or {})
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        return (
            response.status,
            json.loads(response.read().decode("utf-8")),
            response.headers,
        )


def request_json_response(method, url, token, payload=None, raw_data=None, extra_headers=None):
    status, response, _headers = request_json_response_with_headers(
        method,
        url,
        token,
        payload,
        raw_data,
        extra_headers,
    )
    return status, response


def request_json(method, url, token, payload=None, raw_data=None, extra_headers=None):
    return request_json_response(
        method,
        url,
        token,
        payload,
        raw_data,
        extra_headers,
    )[1]


def expect_http_error(status, function, *, error_code=None):
    try:
        function()
    except urllib.error.HTTPError as exc:
        assert exc.code == status, f"Expected HTTP {status}, got {exc.code}"
        payload = json.loads(exc.read().decode("utf-8"))
        if error_code is not None:
            assert payload["error_code"] == error_code, payload
        return payload, exc.headers
    else:
        raise AssertionError(f"Expected HTTP {status}")


def assert_response_envelope(payload, headers):
    assert REQUEST_ID_PATTERN.fullmatch(payload["request_id"]), payload
    assert CORRELATION_ID_PATTERN.fullmatch(payload["correlation_id"]), payload
    assert headers["X-FaceLink-Request-ID"] == payload["request_id"]
    assert headers["X-FaceLink-Correlation-ID"] == payload["correlation_id"]


def request_with_declared_size(
    port,
    token,
    declared_size,
    *,
    protocol_version=None,
    correlation_id=None,
):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.putrequest("POST", "/v1/jobs")
        connection.putheader("Authorization", f"Bearer {token}")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(declared_size))
        if protocol_version is not None:
            connection.putheader("FaceLink-Protocol-Version", protocol_version)
        if correlation_id is not None:
            connection.putheader("X-FaceLink-Correlation-ID", correlation_id)
        connection.endheaders()
        response = connection.getresponse()
        return response.status, json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()


def wait_job(base_url, token, job_id, *, extra_headers=None):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        job = request_json(
            "GET",
            f"{base_url}/v1/jobs/{job_id}",
            token,
            extra_headers=extra_headers,
        )
        if job["status"] in {
            "succeeded",
            "failed",
            "cancelled",
            "timed_out",
            "interrupted",
        }:
            return job
        time.sleep(0.01)
    raise TimeoutError(job_id)


def submit(
    base_url,
    token,
    job_type,
    payload=None,
    timeout_ms=None,
    *,
    extra_headers=None,
):
    request = {"type": job_type, "payload": payload or {}}
    if timeout_ms is not None:
        request["timeout_ms"] = timeout_ms
    return request_json(
        "POST",
        f"{base_url}/v1/jobs",
        token,
        request,
        extra_headers=extra_headers,
    )["job_id"]


def fingerprint_snapshot(snapshot, entity_ids):
    def number(value):
        rounded = round(float(value), 6)
        return 0.0 if rounded == 0.0 else rounded

    entities = {item["id"]: item for item in snapshot["entities"]}
    selected = []
    for entity_id in sorted(set(entity_ids)):
        entity = entities[entity_id]
        transform = entity["transform"]
        selected.append(
            {
                "id": entity_id,
                "type": entity["type"],
                "location": [number(transform["location"][axis]) for axis in "xyz"],
                "rotation_euler": [
                    number(transform["rotation_euler"][axis]) for axis in "xyz"
                ],
                "scale": [number(transform["scale"][axis]) for axis in "xyz"],
                "locked": entity["locked"],
                "parent": entity["metadata"]["parent"],
            }
        )
    canonical = {
        "scene_name": snapshot["scene_name"],
        "fps": number(snapshot["fps"]),
        "frame_start": snapshot["frame_start"],
        "frame_end": snapshot["frame_end"],
        "frame_current": number(snapshot["frame_current"]),
        "entities": selected,
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "scene-" + hashlib.sha256(encoded).hexdigest()[:24]


def client_flow(result_box):
    try:
        record_path = Path(bridge.Runtime.instance_file)
        record = json.loads(record_path.read_text(encoding="utf-8"))
        base_url = f"http://127.0.0.1:{record['port']}"
        token = record["token"]
        assert "protocol_version" not in record

        client_correlation = "corr-0123456789abcdef0123456789abcdef"
        status, bootstrap_health, bootstrap_headers = request_json_response_with_headers(
            "GET",
            f"{base_url}/v1/health",
            token,
            extra_headers={
                "FaceLink-Protocol-Version": "99.invalid",
                "X-FaceLink-Correlation-ID": client_correlation,
            },
        )
        assert status == 200
        assert bootstrap_health["protocol_version"] == "1.13"
        assert bootstrap_headers["FaceLink-Protocol-Version"] == "1.13"
        assert bootstrap_health["correlation_id"] != client_correlation
        assert_response_envelope(bootstrap_health, bootstrap_headers)

        for accepted_version in (None, "1.12", "1.13"):
            headers = {"X-FaceLink-Correlation-ID": client_correlation}
            if accepted_version is not None:
                headers["FaceLink-Protocol-Version"] = accepted_version
            response_status, response_payload, response_headers = (
                request_json_response_with_headers(
                    "GET",
                    f"{base_url}/v1/recovery",
                    token,
                    extra_headers=headers,
                )
            )
            assert response_status == 200
            assert_response_envelope(response_payload, response_headers)
            assert response_payload["correlation_id"] == bootstrap_health["correlation_id"]
            assert response_headers["FaceLink-Protocol-Version"] == (
                accepted_version or "1.12"
            )

        for rejected_version in ("1.11", "1.14", "2.0"):
            rejected_payload, rejected_headers = expect_http_error(
                426,
                lambda version=rejected_version: request_json(
                    "GET",
                    f"{base_url}/v1/recovery",
                    token,
                    extra_headers={"FaceLink-Protocol-Version": version},
                ),
                error_code="unsupported_protocol_version",
            )
            assert_response_envelope(rejected_payload, rejected_headers)
            assert rejected_headers["FaceLink-Protocol-Version"] == "1.13"
            assert rejected_payload["details"] == {
                "requested": rejected_version,
                "supported_versions": ["1.12", "1.13"],
                "min_supported": "1.12",
                "max_supported": "1.13",
            }
            assert rejected_payload["retryable"] is False

        for malformed_version in (
            "1",
            "01.13",
            "1.013",
            "v1.13",
            "1234567890.13",
        ):
            malformed_payload, malformed_headers = expect_http_error(
                400,
                lambda version=malformed_version: request_json(
                    "GET",
                    f"{base_url}/v1/recovery",
                    token,
                    extra_headers={"FaceLink-Protocol-Version": version},
                ),
                error_code="invalid_protocol_version",
            )
            assert_response_envelope(malformed_payload, malformed_headers)
            assert "requested" not in malformed_payload["details"]

        invalid_correlation_payload, invalid_correlation_headers = expect_http_error(
            400,
            lambda: request_json(
                "GET",
                f"{base_url}/v1/health",
                token,
                extra_headers={"X-FaceLink-Correlation-ID": "private-log-injection"},
            ),
            error_code="invalid_correlation_id",
        )
        assert_response_envelope(invalid_correlation_payload, invalid_correlation_headers)
        assert "private-log-injection" not in json.dumps(invalid_correlation_payload)

        runtime_before_protocol_rejection = bridge.Runtime.job_runtime.snapshot()
        status, early_rejection = request_with_declared_size(
            record["port"],
            token,
            bridge.MAX_REQUEST_BYTES + 1,
            protocol_version="2.0",
        )
        assert status == 426
        assert early_rejection["error_code"] == "unsupported_protocol_version"
        status, early_malformed = request_with_declared_size(
            record["port"],
            token,
            0,
            protocol_version="1",
        )
        assert status == 400
        assert early_malformed["error_code"] == "invalid_protocol_version"
        assert bridge.Runtime.job_runtime.snapshot() == runtime_before_protocol_rejection

        auth_payload, auth_headers = expect_http_error(
            401,
            lambda: request_json("GET", f"{base_url}/v1/health", "wrong-token"),
            error_code="authentication_required",
        )
        assert_response_envelope(auth_payload, auth_headers)
        expect_http_error(
            404,
            lambda: request_json("GET", f"{base_url}/v1/jobs/not-found", token),
            error_code="job_not_found",
        )
        expect_http_error(
            400,
            lambda: request_json(
                "POST",
                f"{base_url}/v1/jobs",
                token,
                {"type": "run_python", "payload": {}},
            ),
            error_code="unsupported_job_type",
        )
        for invalid_timeout in (0, True, 3_600_001):
            expect_http_error(
                400,
                lambda value=invalid_timeout: request_json(
                    "POST",
                    f"{base_url}/v1/jobs",
                    token,
                    {"type": "scan_scene", "payload": {}, "timeout_ms": value},
                ),
                error_code="invalid_job_timeout",
            )
        expect_http_error(
            400,
            lambda: request_json("POST", f"{base_url}/v1/jobs", token, raw_data=b"not-json"),
            error_code="invalid_json",
        )
        expect_http_error(
            400,
            lambda: request_json("POST", f"{base_url}/v1/jobs", token, payload=[]),
            error_code="invalid_request",
        )
        expect_http_error(
            400,
            lambda: request_json(
                "POST",
                f"{base_url}/v1/jobs",
                token,
                {"type": "scan_scene", "payload": []},
            ),
            error_code="invalid_job_payload",
        )
        expect_http_error(
            400,
            lambda: request_json(
                "POST",
                f"{base_url}/v1/jobs",
                token,
                {"type": [], "payload": {}},
            ),
            error_code="unsupported_job_type",
        )

        _, health, health_headers = request_json_response_with_headers(
            "GET", f"{base_url}/v1/health", token
        )
        assert_response_envelope(health, health_headers)
        assert {"timeline_diagnostics", "viewport_preview"} <= set(health["capabilities"])
        assert {
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
        } <= set(health["capabilities"])
        assert health["facelink_version"] == "0.3.9"
        assert {
            "bounded_job_queue",
            "stable_error_codes",
            "job_deadlines",
            "safe_job_cancellation",
            "persistent_job_journal",
            "startup_recovery",
        } <= set(health["capabilities"])
        assert health["protocol_version"] == "1.13"
        assert {
            "correlation_ids",
            "sanitized_diagnostics",
            "protocol_negotiation",
        } <= set(health["capabilities"])
        assert health["protocol_compatibility"] == {
            "min_supported": "1.12",
            "max_supported": "1.13",
            "supported_versions": ["1.12", "1.13"],
            "legacy_default": "1.12",
            "request_header": "FaceLink-Protocol-Version",
            "minor_versions_are_additive": True,
        }
        assert health["schema_versions"]["scene_patch"]["accept"] == [
            "1.0",
            "1.1",
            "1.2",
            "1.3",
            "1.4",
        ]
        assert health_headers["FaceLink-Protocol-Version"] == "1.13"
        assert health["job_control"] == {
            "cancel_endpoint": "/v1/jobs/{id}/cancel",
            "max_timeout_ms": 3_600_000,
            "running_cancellation": "cooperative_safe_checkpoints",
        }
        assert health["recovery_control"] == {
            "status_endpoint": "/v1/recovery",
            "acknowledge_endpoint": "/v1/recovery/acknowledge",
            "automatic_replay": False,
        }
        assert health["recovery"]["required"] is False
        assert health["job_runtime"]["pending_capacity"] == 128
        assert health["job_runtime"]["retention_capacity"] == 256
        assert health["job_runtime"]["pending"] == 0
        assert health["job_runtime"]["cancel_requested"] == 0

        private_prompt = "FACELINK_PRIVATE_DIAGNOSTIC_PROMPT"
        private_key = "sk-" + "D" * 40
        query_correlation = "corr-11111111111111111111111111111111"
        submit_status, submitted_job, submitted_headers = request_json_response_with_headers(
            "POST",
            f"{base_url}/v1/jobs",
            token,
            {
                "type": "scan_scene",
                "payload": {"prompt": private_prompt, "api_key": private_key},
            },
            extra_headers={
                "FaceLink-Protocol-Version": "1.13",
                "X-FaceLink-Correlation-ID": client_correlation,
            },
        )
        assert submit_status == 202
        assert_response_envelope(submitted_job, submitted_headers)
        diagnostic_job_id = submitted_job["job_id"]
        submission_request_id = submitted_job["request_id"]
        submission_correlation_id = submitted_job["correlation_id"]
        assert submitted_job["submission_request_id"] == submission_request_id
        assert submitted_job["job_correlation_id"] == submission_correlation_id
        assert submission_correlation_id == bootstrap_health["correlation_id"]
        diagnostic_job = wait_job(
            base_url,
            token,
            diagnostic_job_id,
            extra_headers={
                "FaceLink-Protocol-Version": "1.13",
                "X-FaceLink-Correlation-ID": query_correlation,
            },
        )
        assert diagnostic_job["status"] == "succeeded"
        assert diagnostic_job["job_correlation_id"] == submission_correlation_id
        assert diagnostic_job["correlation_id"] != submission_correlation_id
        assert diagnostic_job["submission_request_id"] == submission_request_id
        assert diagnostic_job["request_id"] != submission_request_id
        _, queried_job, queried_headers = request_json_response_with_headers(
            "GET",
            f"{base_url}/v1/jobs/{diagnostic_job_id}",
            token,
            extra_headers={
                "FaceLink-Protocol-Version": "1.13",
                "X-FaceLink-Correlation-ID": query_correlation,
            },
        )
        assert_response_envelope(queried_job, queried_headers)
        query_correlation_id = queried_job["correlation_id"]
        assert queried_job["job_correlation_id"] == submission_correlation_id
        assert queried_job["submission_request_id"] == submission_request_id
        assert query_correlation_id == diagnostic_job["correlation_id"]
        assert query_correlation_id != submission_correlation_id

        expect_http_error(
            401,
            lambda: request_json("GET", f"{base_url}/v1/diagnostics", "wrong-token"),
            error_code="authentication_required",
        )
        _, diagnostics, diagnostic_headers = request_json_response_with_headers(
            "GET",
            f"{base_url}/v1/diagnostics",
            token,
            extra_headers={
                "FaceLink-Protocol-Version": "1.13",
                "X-FaceLink-Correlation-ID": client_correlation,
            },
        )
        assert_response_envelope(diagnostics, diagnostic_headers)
        assert diagnostics["schema_version"] == "1.0"
        assert diagnostics["events"]["capacity"] == 256
        assert diagnostics["events"]["events"]
        assert diagnostics["events"]["retained"] == len(
            diagnostics["events"]["events"]
        )
        assert diagnostics["events"]["retained"] <= 256
        assert diagnostics["recent_jobs"]
        assert len(diagnostics["recent_jobs"]) <= 64
        diagnostic_record = next(
            job for job in diagnostics["recent_jobs"] if job["job_id"] == diagnostic_job_id
        )
        assert diagnostic_record["status"] == "succeeded"
        assert diagnostic_record["submission_request_id"] == submission_request_id
        assert diagnostic_record["job_correlation_id"] == submission_correlation_id
        submitted_event = next(
            event
            for event in diagnostics["events"]["events"]
            if event.get("job_id") == diagnostic_job_id
            and event["method"] == "POST"
            and event["route"] == "/v1/jobs"
        )
        assert submitted_event["request_id"] == submission_request_id
        assert submitted_event["correlation_id"] == submission_correlation_id
        queried_event = next(
            event
            for event in diagnostics["events"]["events"]
            if event.get("job_id") == diagnostic_job_id
            and event["method"] == "GET"
            and event["request_id"] == queried_job["request_id"]
        )
        assert queried_event["correlation_id"] == query_correlation_id
        assert all(
            event["route"]
            in {
                "/v1/health",
                "/v1/jobs",
                "/v1/jobs/{id}",
                "/v1/jobs/{id}/cancel",
                "/v1/recovery",
                "/v1/recovery/acknowledge",
                "/v1/diagnostics",
                "/unmatched",
            }
            for event in diagnostics["events"]["events"]
        )
        diagnostic_text = json.dumps(diagnostics, ensure_ascii=False)
        for secret in (
            token,
            client_correlation,
            query_correlation,
            private_prompt,
            private_key,
            str(instance_root),
            str(state_root),
            "Bridge Actor",
            "Bridge Obstacle",
        ):
            assert secret not in diagnostic_text
        journal_text = bridge.Runtime.job_journal.path.read_text(encoding="utf-8")
        assert private_prompt not in journal_text
        assert private_key not in journal_text
        assert client_correlation not in journal_text
        assert query_correlation not in journal_text
        journal_payload = json.loads(journal_text)
        journal_record = next(
            job for job in journal_payload["jobs"] if job["job_id"] == diagnostic_job_id
        )
        assert journal_record["submission_request_id"] == submission_request_id
        assert journal_record["job_correlation_id"] == submission_correlation_id
        assert query_correlation_id not in journal_text

        scan_ids = [submit(base_url, token, "scan_scene") for _ in range(3)]
        assert len(set(scan_ids)) == 3
        scans = [wait_job(base_url, token, job_id) for job_id in scan_ids]
        assert all(job["status"] == "succeeded" for job in scans)
        snapshot = scans[0]["result"]
        assert snapshot["schema_version"] == "1.4"
        assert isinstance(snapshot["rigs"], list)
        assert isinstance(snapshot["actions"], list)
        assert snapshot["navigation_environment_fingerprint"].startswith("nav-")
        assert len(snapshot["navigation_meshes"]) == 1
        actor = next(item for item in snapshot["entities"] if item["name"] == "Bridge Actor")

        audit_before_schema_rejections = bpy.context.scene.get(
            blender_executor.AUDIT_LOG_KEY
        )
        revisions_before_schema_rejections = list(blender_executor.REVISION_STACK)
        actor_object = bpy.data.objects["Bridge Actor"]
        actor_location_before_schema_rejections = tuple(actor_object.location)
        actions_before_schema_rejections = set(bpy.data.actions.keys())
        scene_timing_before_schema_rejections = (
            bpy.context.scene.frame_start,
            bpy.context.scene.frame_end,
            bpy.context.scene.frame_current,
            bpy.context.scene.render.fps,
            bpy.context.scene.render.fps_base,
        )

        def actor_animation_state():
            animation = actor_object.animation_data
            if animation is None or animation.action is None:
                return None
            action = animation.action
            return (
                action.name,
                tuple(
                    (
                        curve.data_path,
                        curve.array_index,
                        tuple(
                            (float(point.co[0]), float(point.co[1]))
                            for point in curve.keyframe_points
                        ),
                    )
                    for curve in action.fcurves
                ),
            )

        animation_before_schema_rejections = actor_animation_state()
        private_schema_value = "PRIVATE_SCHEMA_SECRET"
        invalid_schema_values = (
            "0.9",
            "1.5",
            "2.0",
            None,
            14,
            private_schema_value,
        )
        for index, invalid_schema in enumerate(invalid_schema_values):
            invalid_patch = {
                "patch_id": f"unsupported-schema-{index}",
                "source_title": "must not mutate",
                "operations": [
                    {
                        "op": "keyframe_transform",
                        "entity_id": actor["id"],
                        "payload": {
                            "frames": [
                                {"frame": 1, "location": [0, 0, 0]},
                                {"frame": 5, "location": [9, 8, 7]},
                            ],
                            "space": "WORLD",
                        },
                    }
                ],
            }
            if invalid_schema is not None:
                invalid_patch["schema_version"] = invalid_schema
            rejected_schema_job = wait_job(
                base_url,
                token,
                submit(
                    base_url,
                    token,
                    "apply_patch",
                    {"patch": invalid_patch},
                ),
            )
            assert rejected_schema_job["status"] == "failed"
            assert rejected_schema_job["error_code"] == "unsupported_schema_version"
            assert rejected_schema_job["details"]["schema_kind"] == "scene_patch"
            assert rejected_schema_job["details"]["supported"] == [
                "1.0",
                "1.1",
                "1.2",
                "1.3",
                "1.4",
            ]
            expected_requested = (
                invalid_schema
                if isinstance(invalid_schema, str)
                and all(character.isdigit() or character == "." for character in invalid_schema)
                else None
            )
            assert rejected_schema_job["details"]["requested"] == expected_requested
            assert private_schema_value not in json.dumps(rejected_schema_job)
        assert bridge.Runtime.staged_patch is None
        assert bridge.Runtime.staged_summary is None
        assert bpy.context.scene.get(blender_executor.AUDIT_LOG_KEY) == (
            audit_before_schema_rejections
        )
        assert blender_executor.REVISION_STACK == revisions_before_schema_rejections
        assert tuple(bpy.data.objects["Bridge Actor"].location) == (
            actor_location_before_schema_rejections
        )
        assert set(bpy.data.actions.keys()) == actions_before_schema_rejections
        assert actor_animation_state() == animation_before_schema_rejections
        assert (
            bpy.context.scene.frame_start,
            bpy.context.scene.frame_end,
            bpy.context.scene.frame_current,
            bpy.context.scene.render.fps,
            bpy.context.scene.render.fps_base,
        ) == scene_timing_before_schema_rejections

        legacy_entry = {
            "revision_id": "rev-legacy-entry",
            "patch_id": "legacy-patch",
            "status": "reverted",
        }
        bpy.context.scene[blender_executor.AUDIT_LOG_KEY] = json.dumps([legacy_entry])
        legacy_history = bridge.list_revision_history()
        assert legacy_history["entries"][0]["revision_id"] == "rev-legacy-entry"
        assert "schema_version" not in legacy_history["entries"][0]
        if audit_before_schema_rejections is None:
            del bpy.context.scene[blender_executor.AUDIT_LOG_KEY]
        else:
            bpy.context.scene[blender_executor.AUDIT_LOG_KEY] = audit_before_schema_rejections

        composition_patch = {
            "schema_version": "1.2",
            "patch_id": "bridge-composition",
            "source_title": "Bridge composition",
            "operations": [
                {
                    "op": "ensure_camera",
                    "payload": {
                        "name": "Bridge Preview Camera",
                        "mode": "look_at",
                        "target": actor["id"],
                        "space": "WORLD",
                        "location": {"x": 0, "y": -8, "z": 0},
                        "lens_mm": 50,
                        "composition": {
                            "safe_margin": 0.05,
                            "min_subject_height": 0.15,
                            "max_subject_height": 0.9,
                            "max_center_offset": 0.2,
                            "check_occlusion": False,
                        },
                    },
                }
            ],
        }
        composition_job = wait_job(
            base_url,
            token,
            submit(base_url, token, "stage_patch", {"patch": composition_patch}),
        )
        assert composition_job["status"] == "succeeded"
        composition = composition_job["result"]["summary"]["composition"]
        assert composition["evaluated_count"] == 1
        assert composition["warning_count"] == 0
        assert composition["shots"][0]["metrics"]["fully_visible"] is True
        composition_scan = wait_job(
            base_url, token, submit(base_url, token, "scan_scene")
        )["result"]
        assert not any(
            item["name"] == "Bridge Preview Camera"
            for item in composition_scan["entities"]
        )
        discard_composition = wait_job(
            base_url, token, submit(base_url, token, "discard_staged_patch")
        )
        assert discard_composition["status"] == "succeeded"

        fingerprint_entities = [actor["id"]]
        patch = {
            "schema_version": "1.2",
            "patch_id": "bridge-acceptance",
            "source_title": "Bridge acceptance",
            "scene_fingerprint": fingerprint_snapshot(snapshot, fingerprint_entities),
            "fingerprint_entities": fingerprint_entities,
            "fingerprint_frame": snapshot["frame_current"],
            "navigation_environment_fingerprint": snapshot[
                "navigation_environment_fingerprint"
            ],
            "operations": [
                {
                    "op": "keyframe_transform",
                    "entity_id": actor["id"],
                    "payload": {
                        "frames": [
                            {"frame": 1, "location": [0, 0, 0]},
                            {"frame": 13, "location": [1, 2, 0]},
                        ],
                        "interpolation": "LINEAR",
                        "space": "WORLD",
                    },
                }
            ],
        }
        stage_job = wait_job(
            base_url,
            token,
            submit(base_url, token, "stage_patch", {"patch": patch}),
        )
        assert stage_job["status"] == "succeeded"
        assert stage_job["result"]["summary"]["operation_count"] == 1
        assert stage_job["result"]["summary"]["preview"]["path_count"] == 1
        staged_job = wait_job(base_url, token, submit(base_url, token, "get_staged_patch"))
        assert staged_job["result"]["staged"] is True
        assert staged_job["result"]["patch"]["patch_id"] == "bridge-acceptance"

        unchanged_job = wait_job(base_url, token, submit(base_url, token, "scan_scene"))
        unchanged_actor = next(
            item for item in unchanged_job["result"]["entities"] if item["name"] == "Bridge Actor"
        )
        assert unchanged_actor["transform"]["location"] == {"x": 0.0, "y": 0.0, "z": 0.0}

        apply_job = wait_job(base_url, token, submit(base_url, token, "apply_staged_patch"))
        assert apply_job["status"] == "succeeded"
        assert apply_job["result"]["receipt"]["applied_operations"] == 1
        assert (
            wait_job(base_url, token, submit(base_url, token, "get_staged_patch"))["result"][
                "staged"
            ]
            is False
        )

        restage_job = wait_job(
            base_url,
            token,
            submit(base_url, token, "stage_patch", {"patch": patch | {"patch_id": "discard-me"}}),
        )
        assert restage_job["status"] == "succeeded"
        assert restage_job["result"]["summary"]["timeline_warning_count"] == 1
        discard_job = wait_job(base_url, token, submit(base_url, token, "discard_staged_patch"))
        assert discard_job["result"]["patch_id"] == "discard-me"
        assert discard_job["result"]["discarded"] is True

        bad_patch = patch | {
            "patch_id": "bad-entity",
            "operations": [
                {
                    "op": "keyframe_transform",
                    "entity_id": "missing",
                    "payload": {"frames": [{"frame": 1, "location": [5, 0, 0]}]},
                }
            ],
        }
        failed_job = wait_job(
            base_url,
            token,
            submit(base_url, token, "stage_patch", {"patch": bad_patch}),
        )
        assert failed_job["status"] == "failed"
        assert "no longer exists" in failed_job["error"]
        assert failed_job["error_code"] == "job_rejected"
        assert failed_job["retryable"] is False

        second_patch = patch | {
            "patch_id": "bridge-second",
            "source_title": "Bridge second revision",
            "operations": [
                {
                    "op": "keyframe_transform",
                    "entity_id": actor["id"],
                    "payload": {
                        "frames": [
                            {"frame": 1, "location": [0, 0, 0]},
                            {"frame": 13, "location": [2, 0, 0]},
                        ],
                        "space": "WORLD",
                    },
                }
            ],
        }
        second_job = wait_job(
            base_url,
            token,
            submit(base_url, token, "apply_patch", {"patch": second_patch}),
        )
        assert second_job["status"] == "succeeded"
        undo_job = wait_job(base_url, token, submit(base_url, token, "undo"))
        assert undo_job["status"] == "succeeded"
        assert undo_job["result"]["patch_id"] == "bridge-second"

        history_job = wait_job(base_url, token, submit(base_url, token, "list_revisions"))
        history = history_job["result"]
        assert history["available_count"] == 1
        assert [entry["status"] for entry in history["entries"]] == ["applied", "reverted"]
        assert all(entry["schema_version"] == "1.0" for entry in history["entries"])
        first_revision_id = apply_job["result"]["receipt"]["revision_id"]
        rollback_job = wait_job(
            base_url,
            token,
            submit(
                base_url,
                token,
                "rollback_revision",
                {"revision_id": first_revision_id},
            ),
        )
        assert rollback_job["status"] == "succeeded"
        assert rollback_job["result"]["rolled_back_count"] == 1
        final_history = wait_job(
            base_url, token, submit(base_url, token, "list_revisions")
        )["result"]
        assert final_history["available_count"] == 0
        assert all(entry["status"] == "reverted" for entry in final_history["entries"])
        result_box.update(
            status="passed",
            health=health,
            scan_jobs=len(scans),
            staged_without_mutation=True,
            preview_via_bridge=True,
            timeline_warning_via_bridge=True,
            navigation_snapshot_via_bridge=True,
            composition_preflight_via_bridge=True,
            receipt=apply_job["result"]["receipt"],
            discarded_without_apply=True,
            persistent_history_entries=len(final_history["entries"]),
            rollback_via_bridge=True,
            failed_job_error=failed_job["error"],
            failed_job_error_code=failed_job["error_code"],
            protocol_negotiation=True,
            previous_protocol_compatibility=True,
            unsupported_protocol_rejected_before_body=True,
            correlation_chain=True,
            sanitized_diagnostics=True,
            unsupported_schema_zero_mutation=True,
            revision_audit_schema=True,
        )
    except Exception as exc:
        result_box.update(status="failed", error=str(exc), traceback=traceback.format_exc())


blender_addon.register()
for operator_name in (
    "start_bridge",
    "stop_bridge",
    "refresh_status",
    "cancel_job",
    "acknowledge_recovery",
):
    assert hasattr(bpy.ops.facelink, operator_name), operator_name
assert hasattr(bpy.context.window_manager, "facelink")
for property_name in (
    "last_status",
    "last_error_code",
    "startup_error_version",
    "last_result",
    "show_tasks",
    "show_details",
):
    assert hasattr(bpy.context.window_manager.facelink, property_name), property_name
assert bpy.app.timers.is_registered(blender_panels._refresh_task_center)


class RedrawCountingArea:
    type = "VIEW_3D"

    def __init__(self):
        self.redraw_count = 0

    def tag_redraw(self):
        self.redraw_count += 1


# Exercise the real timer callback with a deterministic Blender context facade. Identical
# models must not redraw twice, and a disconnected panel must use the slower refresh cadence.
redraw_area = RedrawCountingArea()
fake_window_manager = SimpleNamespace(
    windows=[SimpleNamespace(screen=SimpleNamespace(areas=[redraw_area]))]
)
offline_timer_model = {
    "connection": {"running": False},
    "recovery": {"required": False},
}
running_timer_model = {
    "connection": {"running": True},
    "recovery": {"required": False},
}
original_panels_bpy = blender_panels.bpy
original_panels_product_status = blender_panels.product_status
try:
    blender_panels.bpy = SimpleNamespace(
        context=SimpleNamespace(window_manager=fake_window_manager)
    )
    blender_panels._last_task_center_model = None
    blender_panels.product_status = lambda: offline_timer_model
    assert (
        blender_panels._refresh_task_center()
        == blender_panels.IDLE_REFRESH_INTERVAL_SECONDS
    )
    assert redraw_area.redraw_count == 1
    assert (
        blender_panels._refresh_task_center()
        == blender_panels.IDLE_REFRESH_INTERVAL_SECONDS
    )
    assert redraw_area.redraw_count == 1
    blender_panels.product_status = lambda: running_timer_model
    assert (
        blender_panels._refresh_task_center()
        == blender_panels.REFRESH_INTERVAL_SECONDS
    )
    assert redraw_area.redraw_count == 2
    assert (
        blender_panels._refresh_task_center()
        == blender_panels.REFRESH_INTERVAL_SECONDS
    )
    assert redraw_area.redraw_count == 2
finally:
    blender_panels.bpy = original_panels_bpy
    blender_panels.product_status = original_panels_product_status
    blender_panels._last_task_center_model = None
bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
bpy.context.active_object.name = "Bridge Actor"
bpy.ops.mesh.primitive_cube_add(location=(10, 10, 0))
bpy.context.active_object.name = "Bridge Obstacle"
bpy.context.active_object["facelink_obstacle"] = True
navigation_mesh = bpy.data.meshes.new("Bridge Navigation")
navigation_mesh.from_pydata(
    [(-2, -2, 0), (4, -2, 0), (4, 4, 0), (-2, 4, 0)],
    [],
    [(0, 1, 2), (0, 2, 3)],
)
navigation_object = bpy.data.objects.new("Bridge Navigation", navigation_mesh)
bpy.context.scene.collection.objects.link(navigation_object)
navigation_object["facelink_navmesh"] = True
original_scan = bridge.scan_scene
main_thread_checks = []


def checked_scan():
    main_thread_checks.append(threading.current_thread() is threading.main_thread())
    return original_scan()


bridge.scan_scene = checked_scan

# Seed the on-disk state as if Blender exited after an apply transaction committed but before
# the terminal HTTP response. Startup must report this ambiguity without replaying the payload.
seed_workspace_id, seed_persistent = bridge.workspace_identity(bpy.data.filepath, os.getpid())
seed_journal = bridge.JobJournal(
    state_root / f"{seed_workspace_id}.json",
    workspace_id=seed_workspace_id,
    persistent_workspace=seed_persistent,
    session_id="acceptance-previous-session",
)
seed_journal.start()
seed_journal.persist(
    [
        {
            "job_id": "job-interrupted-before-response",
            "type": "apply_patch",
            "status": "running",
            "created_at": 1.0,
            "started_at": 2.0,
            "checkpoint": "transaction_committed",
            "checkpoint_at": 3.0,
            "transaction_committed": True,
            "checkpoint_metadata": {
                "revision_id": "rev-interrupted",
                "operation_count": 1,
            },
            "payload": {
                "prompt": "FACELINK_PRIVATE_RECOVERY_PROMPT",
                "api_key": "FACELINK_PRIVATE_RECOVERY_KEY",
            },
        }
    ]
)
first_info = bridge.start_bridge()
second_info = bridge.start_bridge()
assert first_info == second_info
assert bridge.Runtime.server.server_address[0] == "127.0.0.1"
instance_file = Path(bridge.Runtime.instance_file)
assert instance_file.exists()
assert len(bridge.Runtime.token) >= 32

# Exercise real HTTP admission and eviction deterministically before the main client flow.
# Blender timers are restored after this bounded-capacity probe; subsequent jobs are manually
# dispatched by the main-thread loop below as in the rest of this acceptance suite.
if bpy.app.timers.is_registered(bridge._process_jobs):
    bpy.app.timers.unregister(bridge._process_jobs)
record = json.loads(instance_file.read_text(encoding="utf-8"))
probe_base_url = f"http://127.0.0.1:{record['port']}"
probe_token = record["token"]
recovery = request_json("GET", f"{probe_base_url}/v1/recovery", probe_token)
assert recovery["required"] is True
assert recovery["reason"] == "interrupted_jobs"
assert recovery["previous_session_id"] == "acceptance-previous-session"
assert recovery["persistent_workspace"] is False
assert len(recovery["interrupted_jobs"]) == 1
assert recovery["interrupted_jobs"][0]["may_have_committed_changes"] is True
assert recovery["interrupted_jobs"][0]["may_have_partial_changes"] is False
assert recovery["interrupted_jobs"][0]["guidance_code"] == "inspect_revision_history"
assert recovery["session_id"] != recovery["previous_session_id"]
expect_http_error(
    401,
    lambda: request_json("GET", f"{probe_base_url}/v1/recovery", "wrong-token"),
    error_code="authentication_required",
)


def scene_state_for_recovery_assertion():
    return {
        "objects": sorted(
            (
                item.name,
                item.type,
                tuple(round(float(value), 6) for value in item.location),
                item.animation_data.action.name
                if item.animation_data is not None
                and item.animation_data.action is not None
                else None,
            )
            for item in bpy.context.scene.objects
        ),
        "actions": sorted(item.name for item in bpy.data.actions),
        "revisions": bridge.list_revision_history(),
        "staged": bridge.get_staged_patch(),
    }


# Recovery review gates every scene-changing HTTP job before admission while preserving
# bounded read-only inspection. UI mutation controls must expose the same gate.
recovery_gate_private_marker = "FACELINK_PRIVATE_RECOVERY_GATE_PAYLOAD"
recovery_gate_patch = {
    "schema_version": "1.0",
    "patch_id": "recovery-gate-acceptance",
    "source_title": recovery_gate_private_marker,
    "operations": [
        {
            "op": "keyframe_transform",
            "entity_id": ensure_entity_id(bpy.data.objects["Bridge Actor"]),
            "payload": {"frames": [{"frame": 1, "location": [91, 0, 0]}]},
        }
    ],
}
bridge.stage_patch(recovery_gate_patch)
assert blender_operators.FACELINK_OT_apply_staged_patch.poll(None) is False
assert bpy.ops.facelink.apply_staged_patch.poll() is False
original_operator_revision_history = blender_operators.list_revision_history
blender_operators.list_revision_history = lambda: {"available_count": 1}
try:
    assert blender_operators.FACELINK_OT_undo_patch.poll(None) is False
    assert bpy.ops.facelink.undo_patch.poll() is False
finally:
    blender_operators.list_revision_history = original_operator_revision_history
assert blender_operators.FACELINK_OT_rollback_revision.poll(None) is False
assert bpy.ops.facelink.rollback_revision.poll() is False
bridge.clear_staged_patch()

recovery_gate_scene_before = scene_state_for_recovery_assertion()
recovery_gate_runtime_before = bridge.Runtime.job_runtime.task_center_view()
recovery_gate_journal_before = bridge.Runtime.job_journal.path.read_bytes()
gated_mutations = (
    ("apply_patch", {"patch": recovery_gate_patch}),
    ("apply_staged_patch", {}),
    ("undo", {}),
    ("rollback_revision", {"revision_id": "rev-recovery-gate"}),
)
for gated_job_type, gated_payload in gated_mutations:
    rejected_mutation, rejected_headers = expect_http_error(
        409,
        lambda job_type=gated_job_type, payload=gated_payload: submit(
            probe_base_url,
            probe_token,
            job_type,
            payload,
        ),
        error_code="recovery_review_required",
    )
    assert_response_envelope(rejected_mutation, rejected_headers)
    assert rejected_mutation["retryable"] is False
    assert rejected_mutation.get("details", {}) == {}
    assert recovery_gate_private_marker not in json.dumps(rejected_mutation)
    assert bridge.Runtime.job_runtime.task_center_view() == recovery_gate_runtime_before
    assert bridge.Runtime.job_journal.path.read_bytes() == recovery_gate_journal_before
    assert scene_state_for_recovery_assertion() == recovery_gate_scene_before

readonly_scan_id = submit(probe_base_url, probe_token, "scan_scene")
readonly_history_id = submit(probe_base_url, probe_token, "list_revisions")
assert bridge.Runtime.job_runtime.task_center_view()["runtime"]["pending"] == 2
bridge._process_jobs()
bridge._process_jobs()
for readonly_job_id in (readonly_scan_id, readonly_history_id):
    readonly_job = request_json(
        "GET",
        f"{probe_base_url}/v1/jobs/{readonly_job_id}",
        probe_token,
    )
    assert readonly_job["status"] == "succeeded"
assert bridge.Runtime.job_journal.status()["required"] is True

# Leave the following recovery acknowledgement assertions with an empty runtime, while the
# successful read-only records above have already proved the admission exception end to end.
bridge.Runtime.job_runtime = bridge.JobRuntime()
bridge._persist_job_journal()

# A failed acknowledgement must preserve the recovery requirement and the exact journal.
# A successful UI acknowledgement must only update recovery metadata: it never replays work
# or mutates the Blender scene.
recovery_scene_before = scene_state_for_recovery_assertion()
recovery_journal_before = bridge.Runtime.job_journal.path.read_bytes()
original_journal_writer = bridge.Runtime.job_journal._write_payload


def fail_recovery_acknowledgement(_payload):
    raise JournalError("injected recovery acknowledgement failure")


bridge.Runtime.job_journal._write_payload = fail_recovery_acknowledgement
try:
    failed_acknowledgement_reports = OperatorReportSink()
    failed_acknowledgement = blender_operators.FACELINK_OT_acknowledge_recovery.execute(
        failed_acknowledgement_reports,
        bpy.context,
    )
finally:
    bridge.Runtime.job_journal._write_payload = original_journal_writer
assert failed_acknowledgement == {"CANCELLED"}
assert failed_acknowledgement_reports.reports[-1][0] == {"ERROR"}
assert bridge.Runtime.job_journal.status()["required"] is True
assert bridge.product_status()["recovery"]["required"] is True
assert bridge.Runtime.job_journal.path.read_bytes() == recovery_journal_before
assert bpy.context.window_manager.facelink.last_error_code == "journal_unavailable"
assert scene_state_for_recovery_assertion() == recovery_scene_before

successful_acknowledgement = bpy.ops.facelink.acknowledge_recovery()
assert successful_acknowledgement == {"FINISHED"}
assert bridge.Runtime.job_journal.status()["required"] is False
assert bridge.product_status()["recovery"]["required"] is False
assert bridge.Runtime.job_runtime.task_center_view()["jobs"] == []
assert bpy.context.window_manager.facelink.last_error_code == ""
assert scene_state_for_recovery_assertion() == recovery_scene_before

bridge.stage_patch(recovery_gate_patch)
assert blender_operators.FACELINK_OT_apply_staged_patch.poll(None) is True
assert bpy.ops.facelink.apply_staged_patch.poll() is True
blender_operators.list_revision_history = lambda: {"available_count": 1}
try:
    assert blender_operators.FACELINK_OT_undo_patch.poll(None) is True
    assert bpy.ops.facelink.undo_patch.poll() is True
finally:
    blender_operators.list_revision_history = original_operator_revision_history
assert blender_operators.FACELINK_OT_rollback_revision.poll(None) is True
assert bpy.ops.facelink.rollback_revision.poll() is True
bridge.clear_staged_patch()

# The UI acknowledgement immediately reopens admission. Keep the jobs queued, verify 202,
# then cancel them so this gate test itself does not change the Blender scene.
post_ack_scene_before = scene_state_for_recovery_assertion()
post_ack_job_ids = []
for gated_job_type, gated_payload in gated_mutations:
    admitted_status, admitted_job = request_json_response(
        "POST",
        f"{probe_base_url}/v1/jobs",
        probe_token,
        {"type": gated_job_type, "payload": gated_payload},
    )
    assert admitted_status == 202
    assert admitted_job["status"] == "queued"
    post_ack_job_ids.append(admitted_job["job_id"])
assert bridge.Runtime.job_runtime.task_center_view()["runtime"]["pending"] == 4
assert scene_state_for_recovery_assertion() == post_ack_scene_before
for admitted_job_id in post_ack_job_ids:
    cancelled_after_ack = request_json(
        "POST",
        f"{probe_base_url}/v1/jobs/{admitted_job_id}/cancel",
        probe_token,
    )
    assert cancelled_after_ack["status"] == "cancelled"
assert scene_state_for_recovery_assertion() == post_ack_scene_before
bridge.Runtime.job_runtime = bridge.JobRuntime()
bridge._persist_job_journal()

acknowledged = request_json(
    "POST", f"{probe_base_url}/v1/recovery/acknowledge", probe_token
)
assert acknowledged["required"] is False
assert acknowledged["acknowledged"] is True
assert request_json("GET", f"{probe_base_url}/v1/recovery", probe_token)[
    "required"
] is False
status, invalid_size_payload = request_with_declared_size(record["port"], probe_token, 0)
assert status == 400
assert invalid_size_payload["error_code"] == "invalid_request_size"
status, oversized_payload = request_with_declared_size(
    record["port"], probe_token, bridge.MAX_REQUEST_BYTES + 1
)
assert status == 413
assert oversized_payload["error_code"] == "request_too_large"
assert oversized_payload["details"]["max_bytes"] == bridge.MAX_REQUEST_BYTES

# The task centre is a read-only projection. Reading it must not advance a queued job,
# expire it, persist anything, or expose its payload. The UI cancellation operator must
# remove queued work before dispatch and persist the terminal cancellation record.
bridge.Runtime.job_runtime = bridge.JobRuntime()
ui_cancel_id = "job-cccccccccccccccc"
ui_cancel_private_marker = "FACELINK_PRIVATE_UI_CANCEL_PAYLOAD"
ui_cancel_patch = {
    "schema_version": "1.0",
    "patch_id": "ui-queued-cancel",
    "source_title": ui_cancel_private_marker,
    "operations": [
        {
            "op": "keyframe_transform",
            "entity_id": ensure_entity_id(bpy.data.objects["Bridge Actor"]),
            "payload": {"frames": [{"frame": 1, "location": [77, 0, 0]}]},
        }
    ],
}
bridge.Runtime.job_runtime.submit(
    ui_cancel_id,
    "apply_patch",
    {"patch": ui_cancel_patch, "private": ui_cancel_private_marker},
)
bridge._persist_job_journal()
ui_actor = bpy.data.objects["Bridge Actor"]
ui_actor_before = tuple(ui_actor.location)
ui_action_before = ui_actor.animation_data.action if ui_actor.animation_data else None
task_view_before = bridge.Runtime.job_runtime.task_center_view()
task_journal_before = bridge.Runtime.job_journal.path.read_bytes()
first_product_status = bridge.product_status()
second_product_status = bridge.product_status()
assert first_product_status == second_product_status
assert bridge.Runtime.job_runtime.task_center_view() == task_view_before
assert bridge.Runtime.job_journal.path.read_bytes() == task_journal_before
assert first_product_status["connection"]["state"] == "busy"
assert first_product_status["active_jobs"] == [
    {
        "job_id": ui_cancel_id,
        "type": "apply_patch",
        "status": "queued",
        "checkpoint": "accepted",
        "queue_position": 1,
    }
]
assert ui_cancel_private_marker not in repr(first_product_status)

queued_ui_cancel_result = bpy.ops.facelink.cancel_job(job_id=ui_cancel_id)
assert queued_ui_cancel_result == {"FINISHED"}
assert bpy.context.window_manager.facelink.last_error_code == ""
assert bpy.context.window_manager.facelink.last_result == "Queued task cancelled"
cancelled_ui_job = bridge.Runtime.job_runtime.get(ui_cancel_id)
assert cancelled_ui_job["status"] == "cancelled"
assert cancelled_ui_job["error_code"] == "job_cancelled"
assert bridge.Runtime.job_runtime.task_center_view()["runtime"]["pending"] == 0
assert bridge._process_jobs() == 0.05
assert tuple(ui_actor.location) == ui_actor_before
assert (ui_actor.animation_data.action if ui_actor.animation_data else None) is ui_action_before
cancelled_journal = json.loads(bridge.Runtime.job_journal.path.read_text(encoding="utf-8"))
cancelled_journal_job = next(
    item for item in cancelled_journal["jobs"] if item["job_id"] == ui_cancel_id
)
assert cancelled_journal_job["status"] == "cancelled"
assert cancelled_journal_job["error_code"] == "job_cancelled"
assert ui_cancel_private_marker not in json.dumps(cancelled_journal)

# Construct the running/completion race directly so this assertion remains deterministic:
# the UI first records a cooperative cancel request, then completion wins before the next
# checkpoint. Both the intermediate and terminal outcomes must remain visible and persisted.
bridge.Runtime.job_runtime = bridge.JobRuntime()
running_race_id = "job-dddddddddddddddd"
running_private_marker = "FACELINK_PRIVATE_RUNNING_RACE_RESULT"
bridge.Runtime.job_runtime.submit(running_race_id, "scan_scene", {})
started_race = bridge.Runtime.job_runtime.start_next()
assert started_race[:2] == (running_race_id, "scan_scene")
bridge._persist_job_journal()
running_ui_cancel_result = bpy.ops.facelink.cancel_job(job_id=running_race_id)
assert running_ui_cancel_result == {"FINISHED"}
running_race_status = bridge.product_status()
assert running_race_status["queue"]["cancel_requested"] == 1
assert running_race_status["active_jobs"] == [
    {
        "job_id": running_race_id,
        "type": "scan_scene",
        "status": "running",
        "checkpoint": "main_thread_started",
        "cancel_requested": True,
    }
]
bridge.Runtime.job_runtime.mark_succeeded(
    running_race_id,
    {"private": running_private_marker},
)
bridge._persist_job_journal()
completed_race_status = bridge.product_status()
assert completed_race_status["active_job_count"] == 0
assert completed_race_status["queue"]["cancel_requested"] == 0
assert completed_race_status["recent_jobs"] == [
    {
        "job_id": running_race_id,
        "type": "scan_scene",
        "status": "succeeded",
        "checkpoint": "terminal",
        "completed_after_cancel_request": True,
    }
]
assert running_private_marker not in repr(completed_race_status)
race_journal = json.loads(bridge.Runtime.job_journal.path.read_text(encoding="utf-8"))
assert race_journal["jobs"][0]["completed_after_cancel_request"] is True
assert running_private_marker not in json.dumps(race_journal)

# Cancellation changes memory before its journal write. Inject the persistence fault at that
# exact boundary and prove the 503 response describes the in-session truth without claiming
# more than happened. A queued mutation stays removed and can never reach Blender dispatch.
cancellation_fault_marker = (
    "FACELINK_PRIVATE_CANCEL_PERSISTENCE E:\\Private\\cancel-journal.json"
)
original_persist_job_journal = bridge._persist_job_journal
bridge.Runtime.job_runtime = bridge.JobRuntime()
queued_fault_scene_before = scene_state_for_recovery_assertion()
queued_fault_id = submit(
    probe_base_url,
    probe_token,
    "apply_patch",
    {"patch": ui_cancel_patch},
)
queued_fault_journal_before = bridge.Runtime.job_journal.path.read_bytes()


def fail_cancel_persistence(*, clean_shutdown=False, force=False):
    raise JournalError(cancellation_fault_marker)


bridge._persist_job_journal = fail_cancel_persistence
try:
    queued_fault_payload, queued_fault_headers = expect_http_error(
        503,
        lambda: request_json(
            "POST",
            f"{probe_base_url}/v1/jobs/{queued_fault_id}/cancel",
            probe_token,
        ),
        error_code="journal_unavailable",
    )
finally:
    bridge._persist_job_journal = original_persist_job_journal
assert_response_envelope(queued_fault_payload, queued_fault_headers)
assert queued_fault_payload["retryable"] is True
assert queued_fault_payload["details"] == {"cancellation_status": "cancelled"}
assert cancellation_fault_marker not in json.dumps(queued_fault_payload)
assert "E:\\Private" not in json.dumps(queued_fault_payload)
queued_fault_job = bridge.Runtime.job_runtime.get(queued_fault_id)
assert queued_fault_job["status"] == "cancelled"
assert queued_fault_job["error_code"] == "job_cancelled"
assert bridge.Runtime.job_runtime.task_center_view()["runtime"]["pending"] == 0
assert bridge.Runtime.job_journal.path.read_bytes() == queued_fault_journal_before
assert bridge._process_jobs() == 0.05
assert scene_state_for_recovery_assertion() == queued_fault_scene_before
bridge._persist_job_journal()

# The UI uses a fixed queued-failure message. It reports the record failure, not the injected
# exception or path, while retaining the same in-session cancelled/no-dispatch guarantee.
bridge.Runtime.job_runtime = bridge.JobRuntime()
queued_ui_fault_id = "job-eeeeeeeeeeeeeeee"
bridge.Runtime.job_runtime.submit(
    queued_ui_fault_id,
    "apply_patch",
    {"patch": ui_cancel_patch},
)
bridge._persist_job_journal()
queued_ui_fault_scene_before = scene_state_for_recovery_assertion()
bridge._persist_job_journal = fail_cancel_persistence
try:
    queued_ui_fault_reports = OperatorReportSink()
    queued_ui_fault_reports.job_id = queued_ui_fault_id
    queued_ui_fault_result = blender_operators.FACELINK_OT_cancel_job.execute(
        queued_ui_fault_reports,
        bpy.context,
    )
finally:
    bridge._persist_job_journal = original_persist_job_journal
queued_ui_fault_message = (
    "Queued task was removed, but its cancellation record was not saved"
)
assert queued_ui_fault_result == {"CANCELLED"}
assert queued_ui_fault_reports.reports[-1] == ({"ERROR"}, queued_ui_fault_message)
assert bpy.context.window_manager.facelink.last_status == queued_ui_fault_message
assert bpy.context.window_manager.facelink.last_error_code == "journal_unavailable"
assert cancellation_fault_marker not in queued_ui_fault_message
assert "E:\\Private" not in queued_ui_fault_message
assert bridge.Runtime.job_runtime.get(queued_ui_fault_id)["status"] == "cancelled"
assert bridge._process_jobs() == 0.05
assert scene_state_for_recovery_assertion() == queued_ui_fault_scene_before
bridge._persist_job_journal()

# For a genuinely running job, fail only the cancel-request persistence. The Blender operation
# is allowed to return normally, so mark_succeeded records completion-wins rather than falsely
# claiming cancellation. This is synchronized with events and has no timing race.
bridge.Runtime.job_runtime = bridge.JobRuntime()
bridge.stage_patch(recovery_gate_patch)
running_fault_id = submit(probe_base_url, probe_token, "discard_staged_patch")
running_fault_operation_started = threading.Event()
running_fault_response_finished = threading.Event()
running_fault_result = {}
running_fault_count = {"value": 0}
original_discard_staged_patch = bridge.discard_staged_patch


def controlled_discard_for_persistence_fault():
    running_fault_operation_started.set()
    if not running_fault_response_finished.wait(timeout=3):
        raise TimeoutError("Cancellation persistence response did not complete")
    return original_discard_staged_patch()


def fail_only_running_cancel_persistence(*, clean_shutdown=False, force=False):
    current = bridge.Runtime.job_runtime.get(running_fault_id)
    if (
        running_fault_count["value"] == 0
        and current is not None
        and current["status"] == "running"
        and current.get("cancel_requested") is True
    ):
        running_fault_count["value"] += 1
        raise JournalError(cancellation_fault_marker)
    return original_persist_job_journal(
        clean_shutdown=clean_shutdown,
        force=force,
    )


def request_running_cancel_with_persistence_fault():
    try:
        if not running_fault_operation_started.wait(timeout=3):
            raise TimeoutError("Running job did not reach the controlled operation")
        payload, headers = expect_http_error(
            503,
            lambda: request_json(
                "POST",
                f"{probe_base_url}/v1/jobs/{running_fault_id}/cancel",
                probe_token,
            ),
            error_code="journal_unavailable",
        )
        running_fault_result.update(
            status="passed",
            payload=payload,
            headers=headers,
            intermediate=bridge.Runtime.job_runtime.get(running_fault_id),
        )
    except Exception as exc:
        running_fault_result.update(
            status="failed",
            error=str(exc),
            traceback=traceback.format_exc(),
        )
    finally:
        running_fault_response_finished.set()


bridge.discard_staged_patch = controlled_discard_for_persistence_fault
bridge._persist_job_journal = fail_only_running_cancel_persistence
running_fault_thread = threading.Thread(
    target=request_running_cancel_with_persistence_fault,
    daemon=True,
)
running_fault_thread.start()
try:
    bridge._process_jobs()
finally:
    bridge.discard_staged_patch = original_discard_staged_patch
    bridge._persist_job_journal = original_persist_job_journal
running_fault_thread.join(timeout=1)
assert not running_fault_thread.is_alive()
assert running_fault_result.get("status") == "passed", running_fault_result.get(
    "traceback", running_fault_result
)
assert running_fault_count["value"] == 1
running_fault_payload = running_fault_result["payload"]
assert_response_envelope(running_fault_payload, running_fault_result["headers"])
assert running_fault_payload["retryable"] is True
assert running_fault_payload["details"] == {"cancellation_status": "running"}
assert cancellation_fault_marker not in json.dumps(running_fault_payload)
assert "E:\\Private" not in json.dumps(running_fault_payload)
assert running_fault_result["intermediate"]["status"] == "running"
assert running_fault_result["intermediate"]["cancel_requested"] is True
completed_running_fault = bridge.Runtime.job_runtime.get(running_fault_id)
assert completed_running_fault["status"] == "succeeded"
assert completed_running_fault["completed_after_cancel_request"] is True
completed_running_fault_http = request_json(
    "GET",
    f"{probe_base_url}/v1/jobs/{running_fault_id}",
    probe_token,
)
assert completed_running_fault_http["status"] == "succeeded"
assert completed_running_fault_http["completed_after_cancel_request"] is True
running_fault_product_status = bridge.product_status()
assert running_fault_product_status["recent_jobs"] == [
    {
        "job_id": running_fault_id,
        "type": "discard_staged_patch",
        "status": "succeeded",
        "checkpoint": "terminal",
        "completed_after_cancel_request": True,
    }
]
running_fault_journal = json.loads(
    bridge.Runtime.job_journal.path.read_text(encoding="utf-8")
)
assert running_fault_journal["jobs"][0]["status"] == "succeeded"
assert running_fault_journal["jobs"][0]["completed_after_cancel_request"] is True

# Exercise the running branch of the UI operator independently, checking its exact copy and
# proving it says only that cancellation was requested, never that the task already stopped.
bridge.Runtime.job_runtime = bridge.JobRuntime()
running_ui_fault_id = "job-ffffffffffffffff"
bridge.Runtime.job_runtime.submit(running_ui_fault_id, "scan_scene", {})
assert bridge.Runtime.job_runtime.start_next()[:2] == (
    running_ui_fault_id,
    "scan_scene",
)
bridge._persist_job_journal()
bridge._persist_job_journal = fail_cancel_persistence
try:
    running_ui_fault_reports = OperatorReportSink()
    running_ui_fault_reports.job_id = running_ui_fault_id
    running_ui_fault_result = blender_operators.FACELINK_OT_cancel_job.execute(
        running_ui_fault_reports,
        bpy.context,
    )
finally:
    bridge._persist_job_journal = original_persist_job_journal
running_ui_fault_message = (
    "Cancellation was requested but not saved; check the final task result"
)
assert running_ui_fault_result == {"CANCELLED"}
assert running_ui_fault_reports.reports[-1] == ({"ERROR"}, running_ui_fault_message)
assert bpy.context.window_manager.facelink.last_status == running_ui_fault_message
assert bpy.context.window_manager.facelink.last_error_code == "journal_unavailable"
assert "stopped" not in running_ui_fault_message.lower()
assert cancellation_fault_marker not in running_ui_fault_message
assert "E:\\Private" not in running_ui_fault_message
running_ui_fault_job = bridge.Runtime.job_runtime.get(running_ui_fault_id)
assert running_ui_fault_job["status"] == "running"
assert running_ui_fault_job["cancel_requested"] is True
bridge.Runtime.job_runtime.mark_succeeded(running_ui_fault_id, {"completed": True})
bridge._persist_job_journal()

bridge.Runtime.job_runtime = bridge.JobRuntime(pending_capacity=2, retention_capacity=2)
cancel_probe = submit(
    probe_base_url,
    probe_token,
    "scan_scene",
    {
        "prompt": "FACELINK_PRIVATE_LIVE_PROMPT",
        "api_key": "FACELINK_PRIVATE_LIVE_KEY",
    },
)
live_journal_text = bridge.Runtime.job_journal.path.read_text(encoding="utf-8")
assert "FACELINK_PRIVATE_LIVE_PROMPT" not in live_journal_text
assert "FACELINK_PRIVATE_LIVE_KEY" not in live_journal_text
assert probe_token not in live_journal_text
cancelled_status, cancelled_probe = request_json_response(
    "POST",
    f"{probe_base_url}/v1/jobs/{cancel_probe}/cancel",
    probe_token,
)
assert cancelled_status == 200
assert cancelled_probe["status"] == "cancelled"
assert cancelled_probe["error_code"] == "job_cancelled"
assert request_json(
    "POST", f"{probe_base_url}/v1/jobs/{cancel_probe}/cancel", probe_token
)["status"] == "cancelled"
assert request_json("GET", f"{probe_base_url}/v1/health", probe_token)["job_runtime"][
    "pending"
] == 0
timeout_probe = submit(probe_base_url, probe_token, "scan_scene", timeout_ms=20)
timed_out_probe = wait_job(probe_base_url, probe_token, timeout_probe)
assert timed_out_probe["status"] == "timed_out"
assert timed_out_probe["error_code"] == "job_timed_out"
assert timed_out_probe["retryable"] is True
assert bridge._process_jobs() == 0.05

bridge.Runtime.job_runtime = bridge.JobRuntime(pending_capacity=2, retention_capacity=2)
probe_ids = [submit(probe_base_url, probe_token, "scan_scene") for _ in range(2)]
overload_payload, overload_headers = expect_http_error(
    429,
    lambda: submit(probe_base_url, probe_token, "scan_scene"),
    error_code="queue_full",
)
assert overload_payload["retryable"] is True
assert overload_payload["retry_after_ms"] == 100
assert overload_payload["details"]["pending"] == 2
assert overload_payload["details"]["pending_capacity"] == 2
assert overload_headers["Retry-After"] == "1"
probe_health = request_json("GET", f"{probe_base_url}/v1/health", probe_token)
assert probe_health["job_runtime"]["pending"] == 2
assert probe_health["job_runtime"]["retained"] == 2
bridge._process_jobs()
assert all(
    request_json("GET", f"{probe_base_url}/v1/jobs/{job_id}", probe_token)["status"]
    == "succeeded"
    for job_id in probe_ids
)
expect_http_error(
    409,
    lambda: request_json(
        "POST", f"{probe_base_url}/v1/jobs/{probe_ids[1]}/cancel", probe_token
    ),
    error_code="job_not_cancellable",
)
submit(probe_base_url, probe_token, "scan_scene")
expired_payload, _expired_headers = expect_http_error(
    404,
    lambda: request_json("GET", f"{probe_base_url}/v1/jobs/{probe_ids[0]}", probe_token),
    error_code="job_not_found",
)
assert "expired" in expired_payload["error"]
probe_health = request_json("GET", f"{probe_base_url}/v1/health", probe_token)
assert probe_health["job_runtime"]["retained"] == 2
assert probe_health["job_runtime"]["evicted_total"] == 1

# Prove that a running HTTP cancellation is observed after a real Blender mutation and that
# apply_patch restores the captured revision before the job becomes cancelled.
bridge.Runtime.job_runtime = bridge.JobRuntime()
actor = bpy.data.objects["Bridge Actor"]
actor_id = actor["facelink_id"]
cancel_patch = {
    "schema_version": "1.0",
    "patch_id": "bridge-running-cancel",
    "source_title": "Bridge running cancellation",
    "operations": [
        {
            "op": "keyframe_transform",
            "entity_id": actor_id,
            "payload": {
                "frames": [
                    {"frame": 1, "location": [0, 0, 0]},
                    {"frame": 5, "location": [9, 0, 0]},
                ]
            },
        }
    ],
}
operation_applied = threading.Event()
cancel_completed = threading.Event()
cancel_result = {}
original_keyframe_transform = blender_executor._keyframe_transform


def controlled_keyframe_transform(entity_id, payload):
    result = original_keyframe_transform(entity_id, payload)
    operation_applied.set()
    if not cancel_completed.wait(timeout=3):
        raise TimeoutError("Running cancellation request did not complete")
    return result


blender_executor._keyframe_transform = controlled_keyframe_transform
running_cancel_id = submit(
    probe_base_url,
    probe_token,
    "apply_patch",
    {"patch": cancel_patch},
)


def cancel_running_job():
    try:
        if not operation_applied.wait(timeout=3):
            raise TimeoutError("Blender operation did not reach the cancellation checkpoint")
        status, payload = request_json_response(
            "POST",
            f"{probe_base_url}/v1/jobs/{running_cancel_id}/cancel",
            probe_token,
        )
        cancel_result.update(payload, http_status=status)
    finally:
        cancel_completed.set()


cancel_thread = threading.Thread(target=cancel_running_job, daemon=True)
cancel_thread.start()
try:
    bridge._process_jobs()
finally:
    blender_executor._keyframe_transform = original_keyframe_transform
cancel_thread.join(timeout=1)
assert not cancel_thread.is_alive()
assert cancel_result["status"] == "running"
assert cancel_result["cancel_requested"] is True
assert cancel_result["http_status"] == 202
running_cancelled = request_json(
    "GET", f"{probe_base_url}/v1/jobs/{running_cancel_id}", probe_token
)
assert running_cancelled["status"] == "cancelled"
assert running_cancelled["error_code"] == "job_cancelled"
assert actor.animation_data is None
assert tuple(round(value, 4) for value in actor.location) == (0.0, 0.0, 0.0)
assert bridge.list_revision_history()["entries"] == []

# An interrupted replacement stage must restore both prior metadata and overlay geometry.
first_stage = bridge.stage_patch(cancel_patch)
first_geometry = bridge.overlay.preview_geometry()
assert first_stage["staged"] is True
replacement_patch = cancel_patch | {
    "patch_id": "bridge-interrupted-replacement",
    "operations": [
        cancel_patch["operations"][0]
        | {
            "payload": {
                "frames": [
                    {"frame": 1, "location": [0, 0, 0]},
                    {"frame": 5, "location": [3, 4, 0]},
                ]
            }
        }
    ],
}
assert bridge.overlay.build_preview_geometry(replacement_patch) != first_geometry
stage_checks = {"count": 0}


def interrupt_replacement_stage():
    stage_checks["count"] += 1
    if stage_checks["count"] == 3:
        raise bridge.JobInterruption.cancelled()


try:
    bridge.stage_patch(
        replacement_patch,
        check_interrupted=interrupt_replacement_stage,
    )
except bridge.JobInterruption as exc:
    assert exc.code == "job_cancelled"
else:
    raise AssertionError("Interrupted replacement staging unexpectedly succeeded")
assert bridge.get_staged_patch()["patch"]["patch_id"] == "bridge-running-cancel"
assert bridge.overlay.preview_geometry() == first_geometry
bridge.clear_staged_patch()
bridge.Runtime.job_runtime = bridge.JobRuntime()
if not bpy.app.timers.is_registered(bridge._process_jobs):
    bpy.app.timers.register(bridge._process_jobs, first_interval=0.05, persistent=True)

result = {}
client_thread = threading.Thread(target=client_flow, args=(result,), daemon=True)
client_thread.start()
deadline = time.monotonic() + 15
while client_thread.is_alive() and time.monotonic() < deadline:
    bridge._process_jobs()
    time.sleep(0.005)
client_thread.join(timeout=0.5)

assert not client_thread.is_alive(), "Bridge acceptance client timed out"
assert result.get("status") == "passed", result.get("traceback", result)
assert main_thread_checks and all(main_thread_checks)
actor_after_undo = bpy.data.objects.get("Bridge Actor")
assert actor_after_undo is not None
assert tuple(round(value, 4) for value in actor_after_undo.location) == (0.0, 0.0, 0.0)

bridge.stop_bridge()
assert not instance_file.exists()
assert bridge.is_running() is False
journal_path = state_root / f"{seed_workspace_id}.json"
assert journal_path.exists()
stopped_journal = json.loads(journal_path.read_text(encoding="utf-8"))
assert stopped_journal["clean_shutdown"] is True
assert stopped_journal["recovery"]["required"] is False
assert "FACELINK_PRIVATE_RECOVERY_PROMPT" not in journal_path.read_text(encoding="utf-8")
assert "FACELINK_PRIVATE_RECOVERY_KEY" not in journal_path.read_text(encoding="utf-8")

original_instance_writer = bridge._write_instance_record


def fail_instance_discovery_write(_path, _record):
    raise OSError("injected discovery write failure")


bridge._write_instance_record = fail_instance_discovery_write
try:
    bridge.start_bridge()
except OSError as exc:
    assert "injected discovery write failure" in str(exc)
else:
    raise AssertionError("Bridge unexpectedly survived a failed discovery-record write")
finally:
    bridge._write_instance_record = original_instance_writer
assert bridge.Runtime.server is None
assert bridge.Runtime.thread is None
assert bridge.Runtime.instance_file is None
assert bridge.Runtime.token is None
assert bridge.Runtime.job_journal is None
assert not list(instance_root.glob("*.json"))

restart_after_failure = bridge.start_bridge()
assert restart_after_failure["port"] > 0
restart_instance_file = Path(bridge.Runtime.instance_file)
assert restart_instance_file.exists()
bridge.stop_bridge()
assert not restart_instance_file.exists()
assert bridge.is_running() is False

last_good_journal = journal_path.read_bytes()
future_journal = (
    json.dumps(
        {
            "schema_version": "2.0",
            "workspace_id": seed_workspace_id,
            "jobs": [],
            "future_private_field": "preserve-exactly",
        },
        separators=(",", ":"),
    )
    + "\r\n"
).encode("utf-8")
journal_path.write_bytes(future_journal)
future_start_reports = OperatorReportSink()
future_start_result = blender_operators.FACELINK_OT_start_bridge.execute(
    future_start_reports,
    bpy.context,
)
assert future_start_result == {"CANCELLED"}
assert future_start_reports.reports[-1][0] == {"ERROR"}
product_state = bpy.context.window_manager.facelink
assert product_state.last_error_code == "unsupported_journal_version"
assert product_state.startup_error_version == "2.0"
assert "2.0" in product_state.last_status
assert "preserve-exactly" not in product_state.last_status
future_product_status = bridge.product_status(
    startup_error_code=product_state.last_error_code,
    startup_error_version=product_state.startup_error_version,
)
assert future_product_status["connection"] == {
    "running": False,
    "state": "incompatible",
    "error_code": "unsupported_journal_version",
    "requested_version": "2.0",
}
assert journal_path.read_bytes() == future_journal
assert bridge.Runtime.server is None
assert bridge.Runtime.thread is None
assert bridge.Runtime.instance_file is None
assert bridge.Runtime.token is None
assert bridge.Runtime.job_journal is None
assert not list(instance_root.glob("*.json"))
journal_path.write_bytes(last_good_journal)

# Saving an unsaved workspace under a persistent path changes its journal identity, so the
# save handler must stop the old bridge. Re-saving the same workspace may keep it running;
# loading any file must stop it before Blender replaces scene data and clear transient state.
workspace_file = state_root / "workspace-transition.blend"
bridge.start_bridge()
save_transition_instance = Path(bridge.Runtime.instance_file)
assert save_transition_instance.exists()
assert bridge.workspace_matches_current_file() is True
bpy.ops.wm.save_as_mainfile(filepath=str(workspace_file))
assert Path(bpy.data.filepath) == workspace_file
assert bridge.is_running() is False
assert not save_transition_instance.exists()
assert (
    bpy.context.window_manager.facelink.last_status
    == "File location changed; restart FaceLink for this file"
)
assert bpy.context.window_manager.facelink.transition_code == "workspace_changed"
workspace_changed_model = bridge.product_status(
    transition_code=bpy.context.window_manager.facelink.transition_code
)
assert workspace_changed_model["connection"]["notice_code"] == "workspace_changed"
poisoned_transition = "FACELINK_PRIVATE_TRANSITION_C_DO_NOT_PROJECT"
bpy.context.window_manager.facelink.transition_code = poisoned_transition
poisoned_transition_model = bridge.product_status(
    transition_code=bpy.context.window_manager.facelink.transition_code
)
assert "notice_code" not in poisoned_transition_model["connection"]
assert poisoned_transition not in repr(poisoned_transition_model)
bpy.context.window_manager.facelink.transition_code = "workspace_changed"

bridge.start_bridge()
same_workspace_instance = Path(bridge.Runtime.instance_file)
assert bridge.workspace_matches_current_file() is True
bpy.ops.wm.save_as_mainfile(filepath=str(workspace_file))
assert bridge.is_running() is True
assert same_workspace_instance.exists()
bpy.ops.wm.open_mainfile(filepath=str(workspace_file))
assert bridge.is_running() is False
assert not same_workspace_instance.exists()
assert bpy.context.window_manager.facelink.last_status == (
    "File loaded; start FaceLink for this Blender file"
)
assert bpy.context.window_manager.facelink.transition_code == "file_loaded"
file_loaded_model = bridge.product_status(
    transition_code=bpy.context.window_manager.facelink.transition_code
)
assert file_loaded_model["connection"]["notice_code"] == "file_loaded"
assert bridge.get_staged_patch()["staged"] is False
assert bridge.list_revision_history()["available_count"] == 0

result.update(
    suite="blender_bridge_acceptance",
    blender_version=bpy.app.version_string,
    main_thread_dispatch=True,
    undo_restored_location=True,
    discovery_record_cleaned=True,
    bounded_queue_http=True,
    stable_error_codes_http=True,
    completed_job_eviction_http=True,
    queued_cancellation_http=True,
    queued_deadline_http=True,
    running_cancellation_rollback=True,
    interrupted_stage_restore=True,
    startup_recovery_http=True,
    recovery_acknowledgement_http=True,
    persistent_journal_privacy=True,
    clean_shutdown_journal=True,
    failed_start_cleanup=True,
    restart_after_failed_start=True,
    future_journal_bridge_fail_closed=True,
    product_controls_registered=True,
    product_status_read_only=True,
    queued_ui_cancellation_persisted=True,
    running_ui_cancellation_race_visible=True,
    recovery_ui_acknowledgement_safe=True,
    recovery_ui_write_failure_preserved=True,
    future_journal_operator_fail_closed=True,
    workspace_save_load_bridge_safety=True,
    recovery_http_mutation_gate=True,
    recovery_http_readonly_allowed=True,
    recovery_ui_mutation_gate=True,
    recovery_acknowledgement_reopens_admission=True,
    task_center_timer_idempotent=True,
    task_center_offline_backoff=True,
    safe_file_transition_notices=True,
    queued_cancel_persistence_failure_http=True,
    running_cancel_persistence_failure_completion_wins=True,
    cancellation_persistence_failure_ui_copy=True,
)
report_path = os.environ.get("FACELINK_TEST_REPORT")
if report_path:
    Path(report_path).parent.mkdir(parents=True, exist_ok=True)
    Path(report_path).write_text(json.dumps(result, indent=2), encoding="utf-8")
print("FACELINK_BRIDGE_ACCEPTANCE=" + json.dumps(result, sort_keys=True))
blender_addon.unregister()
