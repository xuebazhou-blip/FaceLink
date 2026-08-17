import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

import bpy

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / "blender_extension"))

import facelink as blender_addon  # noqa: E402
from facelink import bridge  # noqa: E402


def request_json(method, url, token, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
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


def wait_job(base_url, token, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = request_json("GET", f"{base_url}/v1/jobs/{job_id}", token)
        if job["status"] == "succeeded":
            return job["result"]
        if job["status"] == "failed":
            raise RuntimeError(job["error"])
        time.sleep(0.02)
    raise TimeoutError(job_id)


def client_flow(result_box):
    record = json.loads(Path(bridge.Runtime.instance_file).read_text(encoding="utf-8"))
    base_url = f"http://127.0.0.1:{record['port']}"
    token = record["token"]
    health = request_json("GET", f"{base_url}/v1/health", token)
    scan = request_json("POST", f"{base_url}/v1/jobs", token, {"type": "scan_scene", "payload": {}})
    snapshot = wait_job(base_url, token, scan["job_id"])
    cube = next(item for item in snapshot["entities"] if item["name"] == "Bridge Cube")
    patch = {
        "schema_version": "1.0",
        "patch_id": "bridge-smoke",
        "source_title": "Bridge smoke test",
        "operations": [
            {
                "op": "keyframe_transform",
                "entity_id": cube["id"],
                "payload": {
                    "frames": [
                        {"frame": 1, "location": [0, 0, 0]},
                        {"frame": 13, "location": [1, 2, 0]},
                    ],
                    "interpolation": "LINEAR",
                },
            }
        ],
    }
    apply = request_json(
        "POST",
        f"{base_url}/v1/jobs",
        token,
        {"type": "apply_patch", "payload": {"patch": patch}},
    )
    receipt = wait_job(base_url, token, apply["job_id"])
    result_box.update(health=health, snapshot=snapshot, receipt=receipt)


instance_dir = project / ".test-instances"
os.environ["FACELINK_INSTANCE_DIR"] = str(instance_dir)
blender_addon.register()
bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
bpy.context.active_object.name = "Bridge Cube"
bridge.start_bridge()

result = {}
thread = threading.Thread(target=client_flow, args=(result,), daemon=True)
thread.start()
deadline = time.monotonic() + 8
while thread.is_alive() and time.monotonic() < deadline:
    bridge._process_jobs()
    time.sleep(0.01)
thread.join(timeout=0.5)

assert not thread.is_alive(), "Bridge client flow timed out"
assert result["health"]["ok"] is True
assert result["receipt"]["applied_operations"] == 1
final_location = tuple(round(value, 4) for value in bpy.data.objects["Bridge Cube"].location)
assert final_location == (1.0, 2.0, 0.0)
print("FACELINK_BRIDGE_OK=" + json.dumps(result["receipt"], sort_keys=True))
blender_addon.unregister()
