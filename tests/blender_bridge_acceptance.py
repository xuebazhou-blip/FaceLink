import hashlib
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
os.environ["FACELINK_INSTANCE_DIR"] = str(instance_root)

import facelink as blender_addon  # noqa: E402
from facelink import bridge  # noqa: E402


def request_json(method, url, token, payload=None, raw_data=None):
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
        return json.loads(response.read().decode("utf-8"))


def expect_http_error(status, function):
    try:
        function()
    except urllib.error.HTTPError as exc:
        assert exc.code == status, f"Expected HTTP {status}, got {exc.code}"
    else:
        raise AssertionError(f"Expected HTTP {status}")


def wait_job(base_url, token, job_id):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        job = request_json("GET", f"{base_url}/v1/jobs/{job_id}", token)
        if job["status"] in {"succeeded", "failed"}:
            return job
        time.sleep(0.01)
    raise TimeoutError(job_id)


def submit(base_url, token, job_type, payload=None):
    return request_json(
        "POST",
        f"{base_url}/v1/jobs",
        token,
        {"type": job_type, "payload": payload or {}},
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

        expect_http_error(
            401,
            lambda: request_json("GET", f"{base_url}/v1/health", "wrong-token"),
        )
        expect_http_error(
            404,
            lambda: request_json("GET", f"{base_url}/v1/jobs/not-found", token),
        )
        expect_http_error(
            400,
            lambda: request_json(
                "POST",
                f"{base_url}/v1/jobs",
                token,
                {"type": "run_python", "payload": {}},
            ),
        )
        expect_http_error(
            400,
            lambda: request_json("POST", f"{base_url}/v1/jobs", token, raw_data=b"not-json"),
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
        assert health["protocol_version"] == "1.9"
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
first_info = bridge.start_bridge()
second_info = bridge.start_bridge()
assert first_info == second_info
assert bridge.Runtime.server.server_address[0] == "127.0.0.1"
instance_file = Path(bridge.Runtime.instance_file)
assert instance_file.exists()
assert len(bridge.Runtime.token) >= 32

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
result.update(
    suite="blender_bridge_acceptance",
    blender_version=bpy.app.version_string,
    main_thread_dispatch=True,
    undo_restored_location=True,
    discovery_record_cleaned=True,
)
report_path = os.environ.get("FACELINK_TEST_REPORT")
if report_path:
    Path(report_path).parent.mkdir(parents=True, exist_ok=True)
    Path(report_path).write_text(json.dumps(result, indent=2), encoding="utf-8")
print("FACELINK_BRIDGE_ACCEPTANCE=" + json.dumps(result, sort_keys=True))
blender_addon.unregister()
