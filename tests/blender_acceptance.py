import json
import os
import sys
import traceback
from pathlib import Path

import bpy

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "blender_extension"))

from facelink.executor import apply_patch, undo_last_patch  # noqa: E402
from facelink.snapshot import ensure_entity_id, scan_scene  # noqa: E402

import facelink as blender_addon  # noqa: E402

RESULTS = []


def reset_scene():
    if bpy.context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for collection in (bpy.data.actions, bpy.data.cameras, bpy.data.meshes):
        for item in list(collection):
            if item.users == 0:
                collection.remove(item)
    scene = bpy.context.scene
    scene.frame_start = 1
    scene.frame_end = 250
    scene.render.fps = 24
    scene.render.fps_base = 1.0


def cube(name, location=(0, 0, 0)):
    bpy.ops.mesh.primitive_cube_add(location=location)
    obj = bpy.context.active_object
    obj.name = name
    return obj


def empty(name, location=(0, 0, 0)):
    obj = bpy.data.objects.new(name, None)
    obj.location = location
    bpy.context.scene.collection.objects.link(obj)
    return obj


def operation_patch(*operations, patch_id="acceptance"):
    return {
        "schema_version": "1.0",
        "patch_id": patch_id,
        "source_title": patch_id,
        "operations": list(operations),
    }


def action_fcurves(obj):
    animation = obj.animation_data
    action = animation.action if animation else None
    if not action:
        return []
    legacy_curves = getattr(action, "fcurves", None)
    if legacy_curves is not None:
        return list(legacy_curves)
    slot = animation.action_slot
    curves = []
    for layer in action.layers:
        for strip in layer.strips:
            channelbag = getattr(strip, "channelbag", None)
            if channelbag:
                bag = channelbag(slot)
                if bag:
                    curves.extend(bag.fcurves)
    return curves


def run_case(name, function):
    try:
        reset_scene()
        function()
    except Exception as exc:
        RESULTS.append(
            {
                "name": name,
                "status": "failed",
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
        )
    else:
        RESULTS.append({"name": name, "status": "passed"})


def test_registration_surface():
    assert hasattr(bpy.types, "FACELINK_PT_main")
    assert hasattr(bpy.types.WindowManager, "facelink")
    assert hasattr(bpy.ops.facelink, "start_bridge")


def test_snapshot_identity_bounds_parent_and_lock():
    parent = empty("Parent", (10, 0, 0))
    actor = cube("Actor", (1, 2, 3))
    actor.parent = parent
    actor["facelink_locked"] = True
    first = scan_scene()
    second = scan_scene()
    actor_first = next(item for item in first["entities"] if item["name"] == "Actor")
    actor_second = next(item for item in second["entities"] if item["name"] == "Actor")
    assert actor_first["id"] == actor_second["id"]
    assert actor_first["locked"] is True
    assert actor_first["metadata"]["parent"] == ensure_entity_id(parent)
    assert actor_first["bounds"]["minimum"]["x"] == 10.0
    assert actor_first["bounds"]["maximum"]["x"] == 12.0


def test_transform_keyframes_interpolation_and_frame_range():
    actor = cube("Actor")
    actor_id = ensure_entity_id(actor)
    receipt = apply_patch(
        operation_patch(
            {
                "op": "set_frame_range",
                "payload": {"fps": 23.976, "frame_start": 1, "frame_end": 49},
            },
            {
                "op": "keyframe_transform",
                "entity_id": actor_id,
                "payload": {
                    "frames": [
                        {"frame": 1, "location": [0, 0, 0]},
                        {"frame": 25, "location": [2, 3, 4]},
                    ],
                    "interpolation": "LINEAR",
                },
            },
        )
    )
    assert receipt["applied_operations"] == 2
    assert tuple(actor.location) == (2.0, 3.0, 4.0)
    assert bpy.context.scene.frame_end == 49
    effective_fps = bpy.context.scene.render.fps / bpy.context.scene.render.fps_base
    assert abs(effective_fps - 23.976) < 0.001
    points = [point for curve in action_fcurves(actor) for point in curve.keyframe_points]
    assert {point.co.x for point in points} == {1.0, 25.0}
    assert all(point.interpolation == "LINEAR" for point in points)


def test_look_at_and_camera_are_idempotent():
    actor = cube("Actor")
    target = empty("Target", (2, 3, 0))
    actor_id = ensure_entity_id(actor)
    target_id = ensure_entity_id(target)
    patch = operation_patch(
        {"op": "look_at", "entity_id": actor_id, "payload": {"target_id": target_id}},
        {
            "op": "ensure_camera",
            "payload": {
                "name": "FaceLink Camera",
                "mode": "follow",
                "target": actor_id,
                "lens_mm": 35,
                "distance": 6,
                "height": 2,
                "frame_start": 1,
                "frame_end": 48,
            },
        },
    )
    apply_patch(patch)
    apply_patch(patch)
    camera_objects = [obj for obj in bpy.data.objects if obj.name == "FaceLink Camera"]
    assert len(camera_objects) == 1
    camera = camera_objects[0]
    assert len([item for item in actor.constraints if item.name == "FaceLink Look At"]) == 1
    assert len([item for item in camera.constraints if item.name == "FaceLink Follow"]) == 1
    assert len([item for item in camera.constraints if item.name == "FaceLink Camera Target"]) == 1
    assert bpy.context.scene.camera == camera


def test_dolly_camera_creates_editable_keyframes():
    target = cube("Target")
    target_id = ensure_entity_id(target)
    apply_patch(
        operation_patch(
            {
                "op": "ensure_camera",
                "payload": {
                    "name": "Dolly Camera",
                    "mode": "dolly_in",
                    "target": target_id,
                    "lens_mm": 50,
                    "distance": 8,
                    "height": 2,
                    "frame_start": 1,
                    "frame_end": 48,
                },
            }
        )
    )
    camera = bpy.data.objects["Dolly Camera"]
    frames = {point.co.x for curve in action_fcurves(camera) for point in curve.keyframe_points}
    assert frames == {1.0, 48.0}


def test_play_clip_creates_one_reusable_nla_strip():
    source = cube("Motion Source")
    source.animation_data_create()
    action = bpy.data.actions.new("Walk")
    source.animation_data.action = action
    source.location.x = 0
    source.keyframe_insert(data_path="location", frame=1)
    source.location.x = 1
    source.keyframe_insert(data_path="location", frame=11)
    source.animation_data.action = None

    actor = cube("Actor")
    actor_id = ensure_entity_id(actor)
    patch = operation_patch(
        {
            "op": "play_clip",
            "entity_id": actor_id,
            "payload": {
                "clip": "Walk",
                "frame_start": 20,
                "frame_end": 40,
                "loop": False,
            },
        }
    )
    apply_patch(patch)
    apply_patch(patch)
    track = actor.animation_data.nla_tracks["FaceLink"]
    assert len(track.strips) == 1
    assert track.strips[0].action == action
    assert track.strips[0].frame_start == 20


def test_locked_objects_and_unknown_operations_fail_closed():
    actor = cube("Locked Actor")
    actor_id = ensure_entity_id(actor)
    actor["facelink_locked"] = True
    locked_patch = operation_patch(
        {
            "op": "keyframe_transform",
            "entity_id": actor_id,
            "payload": {"frames": [{"frame": 1, "location": [99, 0, 0]}]},
        }
    )
    try:
        apply_patch(locked_patch)
    except ValueError as exc:
        assert "locked" in str(exc).lower()
    else:
        raise AssertionError("A locked object accepted a mutation")
    assert tuple(actor.location) == (0.0, 0.0, 0.0)

    unsafe = operation_patch({"op": "run_python", "payload": {"code": "raise"}})
    try:
        apply_patch(unsafe)
    except ValueError as exc:
        assert "unsupported" in str(exc).lower()
    else:
        raise AssertionError("An unsupported operation was accepted")


def test_missing_entity_fails_with_clear_error():
    patch = operation_patch(
        {
            "op": "keyframe_transform",
            "entity_id": "missing",
            "payload": {"frames": [{"frame": 1, "location": [1, 0, 0]}]},
        }
    )
    try:
        apply_patch(patch)
    except ValueError as exc:
        assert "no longer exists" in str(exc)
    else:
        raise AssertionError("A missing entity mutation unexpectedly succeeded")


def test_failed_patch_rolls_back_earlier_operations():
    actor = cube("Transactional Actor")
    actor_id = ensure_entity_id(actor)
    patch = operation_patch(
        {
            "op": "keyframe_transform",
            "entity_id": actor_id,
            "payload": {"frames": [{"frame": 1, "location": [7, 0, 0]}]},
        },
        {
            "op": "keyframe_transform",
            "entity_id": "missing",
            "payload": {"frames": [{"frame": 1, "location": [1, 0, 0]}]},
        },
    )
    try:
        apply_patch(patch)
    except ValueError:
        pass
    else:
        raise AssertionError("A partially invalid patch unexpectedly succeeded")
    restored = bpy.data.objects.get("Transactional Actor")
    assert restored is not None
    assert tuple(round(value, 4) for value in restored.location) == (0.0, 0.0, 0.0)


def test_revision_undo_restores_animation_constraints_camera_and_nla():
    actor = cube("Revision Actor")
    target = empty("Revision Target", (3, 0, 0))
    actor_id = ensure_entity_id(actor)
    target_id = ensure_entity_id(target)
    actor.keyframe_insert(data_path="location", frame=1)
    original_action = actor.animation_data.action

    source = cube("Clip Source")
    source.animation_data_create()
    clip = bpy.data.actions.new("Revision Walk")
    source.animation_data.action = clip
    source.keyframe_insert(data_path="location", frame=1)
    source.location.x = 1
    source.keyframe_insert(data_path="location", frame=11)
    source.animation_data.action = None

    apply_patch(
        operation_patch(
            {
                "op": "keyframe_transform",
                "entity_id": actor_id,
                "payload": {"frames": [{"frame": 12, "location": [4, 0, 0]}]},
            },
            {
                "op": "look_at",
                "entity_id": actor_id,
                "payload": {"target_id": target_id},
            },
            {
                "op": "play_clip",
                "entity_id": actor_id,
                "payload": {
                    "clip": "Revision Walk",
                    "frame_start": 12,
                    "frame_end": 24,
                    "loop": False,
                },
            },
            {
                "op": "ensure_camera",
                "payload": {
                    "name": "Revision Camera",
                    "mode": "follow",
                    "target": actor_id,
                    "frame_start": 1,
                    "frame_end": 24,
                },
            },
            patch_id="revision-complete",
        )
    )
    assert actor.animation_data.action != original_action
    assert actor.constraints.get("FaceLink Look At") is not None
    assert actor.animation_data.nla_tracks.get("FaceLink") is not None
    assert bpy.data.objects.get("Revision Camera") is not None

    receipt = undo_last_patch()
    assert receipt == {"undone": True, "patch_id": "revision-complete"}
    restored = bpy.data.objects["Revision Actor"]
    assert restored.animation_data.action == original_action
    assert restored.constraints.get("FaceLink Look At") is None
    assert restored.animation_data.nla_tracks.get("FaceLink") is None
    assert bpy.data.objects.get("Revision Camera") is None
    assert bpy.context.scene.camera is None
    assert tuple(round(value, 4) for value in restored.location) == (0.0, 0.0, 0.0)


blender_addon.register()
CASES = [
    ("registration_surface", test_registration_surface),
    ("snapshot_identity_bounds_parent_lock", test_snapshot_identity_bounds_parent_and_lock),
    (
        "transform_keyframes_interpolation_frame_range",
        test_transform_keyframes_interpolation_and_frame_range,
    ),
    ("look_at_camera_idempotency", test_look_at_and_camera_are_idempotent),
    ("dolly_camera_keyframes", test_dolly_camera_creates_editable_keyframes),
    ("play_clip_nla_idempotency", test_play_clip_creates_one_reusable_nla_strip),
    ("locked_and_unknown_fail_closed", test_locked_objects_and_unknown_operations_fail_closed),
    ("missing_entity_error", test_missing_entity_fails_with_clear_error),
    ("failed_patch_transaction_rollback", test_failed_patch_rolls_back_earlier_operations),
    (
        "revision_undo_full_surface",
        test_revision_undo_restores_animation_constraints_camera_and_nla,
    ),
]
for case_name, case_function in CASES:
    run_case(case_name, case_function)

report = {
    "suite": "blender_acceptance",
    "blender_version": bpy.app.version_string,
    "python_version": sys.version,
    "passed": sum(item["status"] == "passed" for item in RESULTS),
    "failed": sum(item["status"] == "failed" for item in RESULTS),
    "cases": RESULTS,
}
report_path = os.environ.get("FACELINK_TEST_REPORT")
if report_path:
    Path(report_path).parent.mkdir(parents=True, exist_ok=True)
    Path(report_path).write_text(json.dumps(report, indent=2), encoding="utf-8")
print("FACELINK_ACCEPTANCE=" + json.dumps(report, sort_keys=True))
blender_addon.unregister()
if report["failed"]:
    raise RuntimeError(f"{report['failed']} Blender acceptance case(s) failed")
