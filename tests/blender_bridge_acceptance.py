import hashlib
import http.client
import json
import os
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

import bpy

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "blender_extension"))
instance_root = PROJECT / ".cache" / "bridge-instances" / str(os.getpid())
state_root = PROJECT / ".cache" / "bridge-state" / str(os.getpid())
os.environ["FACELINK_INSTANCE_DIR"] = str(instance_root)
os.environ["FACELINK_STATE_DIR"] = str(state_root)

import facelink as blender_addon  # noqa: E402
from facelink import bridge  # noqa: E402
from facelink import executor as blender_executor  # noqa: E402


def request_json_response(method, url, token, payload=None, raw_data=None):
    data = raw_data
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def request_json(method, url, token, payload=None, raw_data=None):
    return request_json_response(method, url, token, payload, raw_data)[1]


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


def request_with_declared_size(port, token, declared_size):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.putrequest("POST", "/v1/jobs")
        connection.putheader("Authorization", f"Bearer {token}")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(declared_size))
        connection.endheaders()
        response = connection.getresponse()
        return response.status, json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()


def wait_job(base_url, token, job_id):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        job = request_json("GET", f"{base_url}/v1/jobs/{job_id}", token)
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


def submit(base_url, token, job_type, payload=None, timeout_ms=None):
    request = {"type": job_type, "payload": payload or {}}
    if timeout_ms is not None:
        request["timeout_ms"] = timeout_ms
    return request_json("POST", f"{base_url}/v1/jobs", token, request)["job_id"]


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

        expect_http_error(
            401,
            lambda: request_json("GET", f"{base_url}/v1/health", "wrong-token"),
            error_code="authentication_required",
        )
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

        health = request_json("GET", f"{base_url}/v1/health", token)
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
        assert health["protocol_version"] == "1.12"
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
        )
    except Exception as exc:
        result_box.update(status="failed", error=str(exc), traceback=traceback.format_exc())


blender_addon.register()
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
)
report_path = os.environ.get("FACELINK_TEST_REPORT")
if report_path:
    Path(report_path).parent.mkdir(parents=True, exist_ok=True)
    Path(report_path).write_text(json.dumps(result, indent=2), encoding="utf-8")
print("FACELINK_BRIDGE_ACCEPTANCE=" + json.dumps(result, sort_keys=True))
blender_addon.unregister()
