import json
import os
import sys
import time
import traceback
from pathlib import Path

import bpy

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "blender_extension"))

from facelink.snapshot import ensure_entity_id  # noqa: E402

import facelink as blender_addon  # noqa: E402
from facelink import bridge, overlay  # noqa: E402


def write_report(payload):
    report_path = os.environ.get("FACELINK_TEST_REPORT")
    if report_path:
        path = Path(report_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print("FACELINK_OVERLAY_UI_ACCEPTANCE=" + json.dumps(payload, sort_keys=True), flush=True)


def fail(message):
    payload = {
        "suite": "blender_overlay_ui_acceptance",
        "blender_version": bpy.app.version_string,
        "status": "failed",
        "error": message,
        "traceback": traceback.format_exc(),
    }
    write_report(payload)
    try:
        blender_addon.unregister()
    finally:
        os._exit(1)


try:
    blender_addon.register()
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
    actor = bpy.context.active_object
    actor.name = "Overlay UI Actor"
    actor_id = ensure_entity_id(actor)
    bpy.ops.object.empty_add(location=(3, 2, 1))
    target = bpy.context.active_object
    target.name = "Overlay UI Target"
    target_id = ensure_entity_id(target)
    staged = bridge.stage_patch(
        {
            "schema_version": "1.0",
            "patch_id": "overlay-ui-acceptance",
            "source_title": "Overlay UI acceptance",
            "operations": [
                {
                    "op": "keyframe_transform",
                    "entity_id": actor_id,
                    "payload": {
                        "space": "WORLD",
                        "frames": [
                            {"frame": 1, "location": [0, 0, 0]},
                            {"frame": 13, "location": [0.5, 1, 0]},
                            {"frame": 25, "location": [2, 1, 0]},
                        ],
                    },
                },
                {
                    "op": "ensure_camera",
                    "payload": {
                        "name": "Overlay UI Camera",
                        "mode": "look_at",
                        "target": target_id,
                        "space": "WORLD",
                        "location": {"x": 3, "y": -5, "z": 3},
                        "lens_mm": 50,
                    },
                },
            ],
        }
    )
    assert staged["summary"]["preview"]["path_count"] == 1
    assert staged["summary"]["preview"]["frustum_count"] == 1
    assert staged["summary"]["preview"]["segment_count"] == 10
    assert staged["summary"]["composition"]["evaluated_count"] == 1
    assert staged["summary"]["composition"]["shots"][0]["status"] == "unavailable"
    assert staged["summary"]["composition_warning_count"] == 1
except Exception as exc:
    fail(str(exc))


deadline = time.monotonic() + 8.0


def finish_after_draw():
    diagnostics = overlay.draw_diagnostics()
    if diagnostics["last_draw_error"]:
        fail(diagnostics["last_draw_error"])
    if diagnostics["draw_call_count"] > 0:
        payload = {
            "suite": "blender_overlay_ui_acceptance",
            "blender_version": bpy.app.version_string,
            "status": "passed",
            "path_count": overlay.preview_status()["path_count"],
            "frustum_count": overlay.preview_status()["frustum_count"],
            "draw_call_count": diagnostics["draw_call_count"],
            "segment_count": overlay.preview_status()["segment_count"],
            "composition_warning_count": staged["summary"][
                "composition_warning_count"
            ],
            "scene_mutated_while_staged": (
                actor.animation_data is not None
                or bpy.data.objects.get("Overlay UI Camera") is not None
            ),
        }
        if payload["scene_mutated_while_staged"]:
            fail("Staged overlay mutated Blender scene data")
        write_report(payload)
        blender_addon.unregister()
        bpy.ops.wm.quit_blender()
        return None
    if time.monotonic() >= deadline:
        fail("Viewport draw callback did not run before the 8 second deadline")
    return 0.1


bpy.app.timers.register(finish_after_draw, first_interval=0.1)
