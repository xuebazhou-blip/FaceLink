import json
import math
import os
import sys
import traceback
from pathlib import Path

import bpy

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "blender_extension"))

from facelink.executor import (  # noqa: E402
    AUDIT_LOG_KEY,
    MAX_AUDIT_ENTRIES,
    MAX_REVISIONS,
    apply_patch,
    clear_revision_history,
    clear_revisions,
    list_revision_history,
    rollback_to_revision,
    undo_last_patch,
)
from facelink.snapshot import ensure_entity_id, scan_scene, scene_fingerprint  # noqa: E402

import facelink as blender_addon  # noqa: E402
from facelink import bridge, overlay  # noqa: E402

RESULTS = []


def reset_scene():
    bridge.clear_staged_patch()
    clear_revisions()
    clear_revision_history()
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
    scene.frame_set(1)
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


def mesh_object(name, vertices, faces):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
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
    assert hasattr(bpy.ops.facelink, "apply_staged_patch")
    assert hasattr(bpy.ops.facelink, "discard_staged_patch")
    assert hasattr(bpy.ops.facelink, "toggle_preview")
    assert hasattr(bpy.ops.facelink, "set_navigation_role")
    assert hasattr(bpy.ops.facelink, "rollback_revision")


def test_panel_stages_before_apply_and_can_discard():
    actor = cube("Review Actor")
    bpy.context.view_layer.objects.active = actor
    actor.select_set(True)

    assert bpy.ops.facelink.demo_patch() == {"FINISHED"}
    staged = bridge.get_staged_patch()
    assert staged["staged"] is True
    assert staged["summary"]["operation_count"] == 1
    assert staged["summary"]["affected_entities"][0]["name"] == "Review Actor"
    assert staged["summary"]["preview"]["path_count"] == 1
    assert overlay.preview_status()["visible"] is True
    assert tuple(actor.location) == (0.0, 0.0, 0.0)
    assert actor.animation_data is None
    assert list_revision_history()["entries"] == []

    assert bpy.ops.facelink.apply_staged_patch() == {"FINISHED"}
    assert bridge.get_staged_patch()["staged"] is False
    assert overlay.preview_status()["visible"] is False
    assert len(list_revision_history()["entries"]) == 1
    bpy.context.scene.frame_set(staged["summary"]["frame_end"])
    assert tuple(actor.location) == (2.0, 0.0, 0.0)
    assert actor.animation_data is not None

    assert bpy.ops.facelink.undo_patch() == {"FINISHED"}
    assert tuple(actor.location) == (0.0, 0.0, 0.0)
    history_before_discard = list_revision_history()
    assert history_before_discard["entries"][0]["status"] == "reverted"
    assert bpy.ops.facelink.demo_patch() == {"FINISHED"}
    assert bpy.ops.facelink.discard_staged_patch() == {"FINISHED"}
    assert bridge.get_staged_patch()["staged"] is False
    assert overlay.preview_status()["path_count"] == 0
    assert tuple(actor.location) == (0.0, 0.0, 0.0)
    assert list_revision_history() == history_before_discard


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
    assert first["schema_version"] == "1.2"
    assert first["navigation_environment_fingerprint"].startswith("nav-")
    assert first["transform_space"] == "WORLD"
    assert actor_first["transform"]["location"] == {"x": 11.0, "y": 2.0, "z": 3.0}
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


def test_world_space_keyframes_convert_through_parent_transform():
    parent = empty("Moving Parent", (10, 0, 0))
    parent.rotation_euler.z = 1.5707963267948966
    parent.scale = (2, 2, 2)
    actor = cube("World Child", (1, 0, 0))
    actor.parent = parent
    actor_id = ensure_entity_id(actor)
    ensure_entity_id(parent)
    bpy.context.view_layer.update()
    original_world = tuple(round(value, 4) for value in actor.matrix_world.translation)
    assert original_world == (10.0, 2.0, 0.0)

    apply_patch(
        {
            "schema_version": "1.1",
            "patch_id": "world-parent",
            "source_title": "World parent conversion",
            "operations": [
                {
                    "op": "keyframe_transform",
                    "entity_id": actor_id,
                    "payload": {
                        "space": "WORLD",
                        "frames": [
                            {"frame": 1, "location": [10, 2, 0]},
                            {"frame": 25, "location": [14, 4, 0]},
                        ],
                        "interpolation": "LINEAR",
                    },
                }
            ],
        }
    )
    bpy.context.scene.frame_set(25)
    assert tuple(round(value, 4) for value in actor.matrix_world.translation) == (14.0, 4.0, 0.0)
    assert tuple(round(value, 4) for value in actor.location) == (2.0, -2.0, 0.0)
    undo_last_patch()
    assert tuple(round(value, 4) for value in actor.matrix_world.translation) == original_world


def test_world_space_camera_location_converts_through_parent():
    parent = empty("Camera Parent", (10, 0, 0))
    parent.rotation_euler.z = 1.5707963267948966
    parent.scale = (2, 2, 2)
    data = bpy.data.cameras.new("Parented Camera")
    camera = bpy.data.objects.new("Parented Camera", data)
    bpy.context.scene.collection.objects.link(camera)
    camera.parent = parent
    ensure_entity_id(parent)
    ensure_entity_id(camera)
    bpy.context.view_layer.update()
    original_world = tuple(round(value, 4) for value in camera.matrix_world.translation)
    original_lens = camera.data.lens

    apply_patch(
        operation_patch(
            {
                "op": "ensure_camera",
                "payload": {
                    "name": "Parented Camera",
                    "mode": "static",
                    "space": "WORLD",
                    "location": {"x": 14, "y": 4, "z": 3},
                    "lens_mm": 40,
                },
            },
            patch_id="world-camera",
        )
    )
    assert tuple(round(value, 4) for value in camera.matrix_world.translation) == (14.0, 4.0, 3.0)
    assert camera.data.lens == 40
    undo_last_patch()
    assert tuple(round(value, 4) for value in camera.matrix_world.translation) == original_world
    assert camera.data.lens == original_lens


def test_world_space_zero_scale_parent_fails_closed():
    parent = empty("Zero Parent")
    parent.scale = (0, 1, 1)
    actor = cube("Zero Child", (1, 0, 0))
    actor.parent = parent
    actor_id = ensure_entity_id(actor)
    bpy.context.view_layer.update()
    try:
        apply_patch(
            {
                "schema_version": "1.1",
                "patch_id": "zero-parent",
                "source_title": "Impossible conversion",
                "operations": [
                    {
                        "op": "keyframe_transform",
                        "entity_id": actor_id,
                        "payload": {
                            "space": "WORLD",
                            "frames": [{"frame": 1, "location": [2, 0, 0]}],
                        },
                    }
                ],
            }
        )
    except ValueError as exc:
        assert "zero-scale parent" in str(exc)
    else:
        raise AssertionError("A non-invertible parent transform was accepted")
    assert actor.animation_data is None
    assert list_revision_history()["entries"] == []


def test_scene_fingerprint_rejects_changes_and_keeps_staging():
    actor = cube("Guarded Actor")
    actor_id = ensure_entity_id(actor)
    frame = float(bpy.context.scene.frame_current)
    fingerprint = scene_fingerprint([actor_id], frame)
    patch = {
        "schema_version": "1.1",
        "patch_id": "guarded",
        "source_title": "Guarded patch",
        "scene_fingerprint": fingerprint,
        "fingerprint_entities": [actor_id],
        "fingerprint_frame": frame,
        "operations": [
            {
                "op": "keyframe_transform",
                "entity_id": actor_id,
                "payload": {
                    "space": "WORLD",
                    "frames": [
                        {"frame": 1, "location": [0, 0, 0]},
                        {"frame": 25, "location": [3, 0, 0]},
                    ],
                },
            }
        ],
    }
    staged = bridge.stage_patch(patch)
    assert staged["summary"]["scene_guarded"] is True

    actor.location.x = 1
    bpy.context.view_layer.update()
    try:
        bridge.apply_staged_patch()
    except ValueError as exc:
        assert "scene changed" in str(exc).lower()
    else:
        raise AssertionError("A stale staged patch was applied")
    assert bridge.get_staged_patch()["staged"] is True
    assert actor.animation_data is None

    actor.location.x = 0
    bpy.context.view_layer.update()
    bpy.context.scene.frame_set(12)
    receipt = bridge.apply_staged_patch()
    assert receipt["receipt"]["patch_id"] == "guarded"
    assert bridge.get_staged_patch()["staged"] is False
    assert bpy.context.scene.frame_current == 12


def test_navigation_snapshot_guard_overlay_execution_and_stale_environment():
    actor = cube("Navigation Actor", (1, 1, 0))
    actor_id = ensure_entity_id(actor)
    target = empty("Navigation Goal", (5, 5, 0))
    ensure_entity_id(target)
    obstacle = cube("Navigation Wall", (3, 3, 1))
    obstacle.scale = (0.5, 0.5, 1)
    obstacle["facelink_obstacle"] = True
    obstacle["facelink_id"] = "wall"
    navmesh = mesh_object(
        "Navigation L Corridor",
        [
            (0, 0, 0),
            (2, 0, 0),
            (2, 4, 0),
            (0, 4, 0),
            (6, 4, 0),
            (6, 6, 0),
            (0, 6, 0),
            (2, 6, 0),
        ],
        [
            (0, 1, 2),
            (0, 2, 3),
            (3, 2, 7),
            (3, 7, 6),
            (2, 4, 5),
            (2, 5, 7),
        ],
    )
    navmesh["facelink_navmesh"] = True
    navmesh["facelink_id"] = "nav-l"
    bpy.context.view_layer.update()
    snapshot = scan_scene()
    navigation = snapshot["navigation_meshes"]
    obstacle_snapshot = next(
        item for item in snapshot["entities"] if item["name"] == "Navigation Wall"
    )
    navmesh_snapshot = next(
        item for item in snapshot["entities"] if item["name"] == "Navigation L Corridor"
    )

    assert snapshot["schema_version"] == "1.2"
    assert snapshot["navigation_environment_fingerprint"] == (
        "nav-63fb67061dd69bff488050e7"
    )
    assert len(navigation) == 1
    assert navigation[0]["entity_id"] == navmesh_snapshot["id"]
    assert len(navigation[0]["vertices"]) == 8
    assert len(navigation[0]["polygons"]) == 6
    assert obstacle_snapshot["metadata"]["navigation_role"] == "obstacle"
    assert navmesh_snapshot["metadata"]["navigation_role"] == "navmesh"

    patch = operation_patch(
        {
            "op": "keyframe_transform",
            "entity_id": actor_id,
            "payload": {
                "space": "WORLD",
                "path_mode": "navmesh",
                "navigation_mesh": navmesh_snapshot["id"],
                "interpolation": "LINEAR",
                "frames": [
                    {"frame": 1, "location": [1, 1, 0]},
                    {"frame": 25, "location": [1, 4, 0]},
                    {"frame": 49, "location": [5, 5, 0]},
                ],
            },
        },
        patch_id="navigation-path",
    )
    patch["schema_version"] = "1.2"
    patch["navigation_environment_fingerprint"] = snapshot[
        "navigation_environment_fingerprint"
    ]
    staged = bridge.stage_patch(patch)
    assert staged["summary"]["navigation_guarded"] is True
    assert staged["summary"]["preview"]["path_count"] == 1
    assert staged["summary"]["preview"]["segment_count"] == 2
    assert actor.animation_data is None
    result = bridge.apply_staged_patch()
    assert result["receipt"]["patch_id"] == "navigation-path"
    frames = {
        point.co.x for curve in action_fcurves(actor) for point in curve.keyframe_points
    }
    assert frames == {1.0, 25.0, 49.0}
    bpy.context.scene.frame_set(49)
    assert tuple(round(value, 4) for value in actor.matrix_world.translation) == (5.0, 5.0, 0.0)
    undo_last_patch()

    bridge.stage_patch(patch)
    new_obstacle = cube("New Untracked Obstacle", (1, 4, 0))
    new_obstacle["facelink_obstacle"] = True
    try:
        bridge.apply_staged_patch()
    except ValueError as exc:
        assert "navigation environment changed" in str(exc).lower()
    else:
        raise AssertionError("A stale navigation patch was applied after adding an obstacle")
    assert bridge.get_staged_patch()["staged"] is True
    bridge.discard_staged_patch()
    assert overlay.preview_status()["path_count"] == 0


def test_navigation_markers_reject_ambiguous_and_non_mesh_objects():
    invalid = empty("Invalid Navigation")
    invalid["facelink_navmesh"] = True
    try:
        scan_scene()
    except ValueError as exc:
        assert "must be a mesh" in str(exc).lower()
    else:
        raise AssertionError("A non-mesh navigation object was scanned")

    del invalid["facelink_navmesh"]
    ambiguous = cube("Ambiguous Navigation")
    ambiguous["facelink_navmesh"] = True
    ambiguous["facelink_obstacle"] = True
    try:
        scan_scene()
    except ValueError as exc:
        assert "both navmesh and obstacle" in str(exc).lower()
    else:
        raise AssertionError("An ambiguous navigation role was scanned")

    del ambiguous["facelink_navmesh"]
    del ambiguous["facelink_obstacle"]
    degenerate = mesh_object(
        "Degenerate Navigation",
        [(0, 0, 0), (1, 0, 0), (2, 0, 0)],
        [(0, 1, 2)],
    )
    degenerate["facelink_navmesh"] = True
    try:
        scan_scene()
    except ValueError as exc:
        assert "degenerate xy triangle" in str(exc).lower()
    else:
        raise AssertionError("A degenerate navigation triangle was scanned")


def test_navigation_role_operators_are_exclusive_and_validate_object_type():
    actor = cube("Navigation Role Actor")
    bpy.context.view_layer.objects.active = actor
    actor.select_set(True)
    assert bpy.ops.facelink.set_navigation_role(role="OBSTACLE") == {"FINISHED"}
    assert actor.get("facelink_obstacle") is True
    assert actor.get("facelink_navmesh") is None
    assert bpy.ops.facelink.set_navigation_role(role="NAVMESH") == {"FINISHED"}
    assert actor.get("facelink_navmesh") is True
    assert actor.get("facelink_obstacle") is None
    assert bpy.ops.facelink.set_navigation_role(role="NONE") == {"FINISHED"}
    assert actor.get("facelink_navmesh") is None
    assert actor.get("facelink_obstacle") is None

    invalid = empty("Navigation Role Empty")
    bpy.context.view_layer.objects.active = invalid
    actor.select_set(False)
    invalid.select_set(True)
    try:
        bpy.ops.facelink.set_navigation_role(role="NAVMESH")
    except RuntimeError as exc:
        assert "must be a mesh object" in str(exc).lower()
    else:
        raise AssertionError("A non-mesh object was marked as a navigation mesh")
    assert invalid.get("facelink_navmesh") is None


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

    invalid_metadata = operation_patch(
        {
            "op": "keyframe_transform",
            "entity_id": actor_id,
            "payload": {"frames": [{"frame": 1, "location": [99, 0, 0]}]},
        }
    )
    invalid_metadata["warnings"] = None
    try:
        bridge.stage_patch(invalid_metadata)
    except ValueError as exc:
        assert "warnings" in str(exc).lower()
    else:
        raise AssertionError("Invalid patch metadata was staged")
    assert bridge.get_staged_patch()["staged"] is False

    invalid_navigation_guard = operation_patch(patch_id="invalid-navigation-guard")
    invalid_navigation_guard["schema_version"] = "1.2"
    invalid_navigation_guard["navigation_environment_fingerprint"] = "invalid"
    try:
        bridge.stage_patch(invalid_navigation_guard)
    except ValueError as exc:
        assert "navigation_environment_fingerprint is invalid" in str(exc)
    else:
        raise AssertionError("An invalid navigation fingerprint was staged")
    assert bridge.get_staged_patch()["staged"] is False

    actor["facelink_locked"] = False
    non_finite = operation_patch(
        {
            "op": "keyframe_transform",
            "entity_id": actor_id,
            "payload": {"frames": [{"frame": 1, "location": [math.nan, 0, 0]}]},
        },
        patch_id="non-finite-transform",
    )
    try:
        bridge.stage_patch(non_finite)
    except ValueError as exc:
        assert "finite" in str(exc).lower()
    else:
        raise AssertionError("A non-finite transform was staged")

    invalid_camera_mode = operation_patch(
        {
            "op": "ensure_camera",
            "payload": {"name": "Unsafe Camera", "mode": "teleport"},
        },
        patch_id="invalid-camera-mode",
    )
    try:
        bridge.stage_patch(invalid_camera_mode)
    except ValueError as exc:
        assert "unsupported camera mode" in str(exc).lower()
    else:
        raise AssertionError("An unknown camera mode was staged")
    assert bpy.data.objects.get("Unsafe Camera") is None
    assert bridge.get_staged_patch()["staged"] is False


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


def test_internal_timeline_overlap_is_rejected_before_staging():
    actor = cube("Timeline Conflict Actor")
    actor_id = ensure_entity_id(actor)
    patch = operation_patch(
        {
            "op": "keyframe_transform",
            "entity_id": actor_id,
            "payload": {
                "frames": [
                    {"frame": 1, "location": [0, 0, 0]},
                    {"frame": 25, "location": [2, 0, 0]},
                ]
            },
        },
        {
            "op": "keyframe_transform",
            "entity_id": actor_id,
            "payload": {
                "frames": [
                    {"frame": 13, "location": [1, 0, 0]},
                    {"frame": 36, "location": [3, 0, 0]},
                ]
            },
        },
        patch_id="timeline-conflict",
    )

    try:
        bridge.stage_patch(patch)
    except ValueError as exc:
        assert "timeline conflicts" in str(exc).lower()
        assert "location" in str(exc).lower()
    else:
        raise AssertionError("An internally overlapping patch was staged")
    assert bridge.get_staged_patch()["staged"] is False
    assert actor.animation_data is None
    assert list_revision_history()["entries"] == []

    conflicting_frame = operation_patch(
        {
            "op": "keyframe_transform",
            "entity_id": actor_id,
            "payload": {
                "frames": [
                    {"frame": 10, "location": [1, 0, 0]},
                    {"frame": 10, "location": [2, 0, 0]},
                ]
            },
        },
        patch_id="same-frame-conflict",
    )
    try:
        bridge.stage_patch(conflicting_frame)
    except ValueError as exc:
        assert "conflicting location values" in str(exc).lower()
    else:
        raise AssertionError("Conflicting values at the same frame were staged")
    assert bridge.get_staged_patch()["staged"] is False


def test_existing_keyframes_are_reported_as_review_warnings():
    actor = cube("Existing Animation Actor")
    actor_id = ensure_entity_id(actor)
    actor.location.x = 1
    actor.keyframe_insert(data_path="location", frame=10)
    actor.location.x = 2
    actor.keyframe_insert(data_path="location", frame=20)
    original_action = actor.animation_data.action

    result = bridge.stage_patch(
        operation_patch(
            {
                "op": "keyframe_transform",
                "entity_id": actor_id,
                "payload": {
                    "frames": [
                        {"frame": 15, "location": [3, 0, 0]},
                        {"frame": 30, "location": [4, 0, 0]},
                    ]
                },
            },
            patch_id="existing-animation-warning",
        )
    )

    summary = result["summary"]
    assert summary["timeline_warning_count"] == 1
    assert "Existing keyframes overlap" in summary["warnings"][0]
    assert actor.animation_data.action == original_action
    assert len(action_fcurves(actor)[0].keyframe_points) == 2
    bridge.discard_staged_patch()


def test_staged_preview_builds_world_paths_and_camera_frustum_without_mutation():
    parent = empty("Preview Parent", (10, 0, 0))
    actor = cube("Preview Actor")
    actor.parent = parent
    actor_id = ensure_entity_id(actor)
    target = empty("Preview Target", (4, 4, 1))
    target_id = ensure_entity_id(target)
    camera_count = len(bpy.data.cameras)

    result = bridge.stage_patch(
        operation_patch(
            {
                "op": "keyframe_transform",
                "entity_id": actor_id,
                "payload": {
                    "space": "LOCAL",
                    "frames": [
                        {"frame": 25, "location": [2, 0, 0]},
                        {"frame": 1, "location": [0, 0, 0]},
                    ],
                },
            },
            {
                "op": "ensure_camera",
                "payload": {
                    "name": "Preview Camera",
                    "mode": "look_at",
                    "target": target_id,
                    "space": "WORLD",
                    "location": {"x": 4, "y": -4, "z": 3},
                    "lens_mm": 50,
                },
            },
            patch_id="spatial-preview",
        )
    )

    status = result["summary"]["preview"]
    assert status["visible"] is True
    assert status["path_count"] == 1
    assert status["frustum_count"] == 1
    assert status["segment_count"] == 9
    assert status["draw_handler_active"] is True
    geometry = overlay.preview_geometry()
    assert geometry["paths"][0]["points"] == [
        [10.0, 0.0, 0.0],
        [12.0, 0.0, 0.0],
    ]
    assert geometry["frustums"][0]["origin"] == [4.0, -4.0, 3.0]
    assert all(
        math.isfinite(component)
        for segment in geometry["frustums"][0]["segments"]
        for point in segment
        for component in point
    )
    assert len(bpy.data.cameras) == camera_count
    assert bpy.data.objects.get("Preview Camera") is None
    assert actor.animation_data is None

    assert bpy.ops.facelink.toggle_preview() == {"FINISHED"}
    assert overlay.preview_status()["visible"] is False
    assert bpy.ops.facelink.toggle_preview() == {"FINISHED"}
    assert overlay.preview_status()["visible"] is True
    bridge.discard_staged_patch()
    assert overlay.preview_status() == {
        "visible": False,
        "draw_handler_active": False,
        "path_count": 0,
        "frustum_count": 0,
        "segment_count": 0,
    }


def test_overlapping_existing_nla_strip_is_rejected_before_staging():
    source = cube("NLA Source")
    source.animation_data_create()
    first_action = bpy.data.actions.new("First Clip")
    source.animation_data.action = first_action
    source.keyframe_insert(data_path="location", frame=1)
    source.location.x = 1
    source.keyframe_insert(data_path="location", frame=11)
    second_action = bpy.data.actions.new("Second Clip")
    source.animation_data.action = second_action
    source.keyframe_insert(data_path="location", frame=1)
    source.location.x = 2
    source.keyframe_insert(data_path="location", frame=11)
    source.animation_data.action = None

    actor = cube("NLA Conflict Actor")
    actor_id = ensure_entity_id(actor)
    apply_patch(
        operation_patch(
            {
                "op": "play_clip",
                "entity_id": actor_id,
                "payload": {
                    "clip": "First Clip",
                    "frame_start": 10,
                    "frame_end": 30,
                },
            },
            patch_id="first-nla",
        )
    )

    try:
        bridge.stage_patch(
            operation_patch(
                {
                    "op": "play_clip",
                    "entity_id": actor_id,
                    "payload": {
                        "clip": "Second Clip",
                        "frame_start": 20,
                        "frame_end": 40,
                    },
                },
                patch_id="overlapping-nla",
            )
        )
    except ValueError as exc:
        assert "overlap existing facelink nla strip" in str(exc).lower()
    else:
        raise AssertionError("A patch overlapping an existing NLA strip was staged")

    track = actor.animation_data.nla_tracks["FaceLink"]
    assert len(track.strips) == 1
    assert track.strips[0].action == first_action
    assert len(list_revision_history()["entries"]) == 1


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


def test_revision_history_and_safe_rollback_to_target():
    actor = cube("History Actor")
    actor_id = ensure_entity_id(actor)
    revision_ids = []
    for index in range(1, 4):
        receipt = apply_patch(
            operation_patch(
                {
                    "op": "keyframe_transform",
                    "entity_id": actor_id,
                    "payload": {
                        "frames": [{"frame": 1, "location": [index, 0, 0]}]
                    },
                },
                patch_id=f"history-{index}",
            )
        )
        revision_ids.append(receipt["revision_id"])

    history = list_revision_history()
    assert history["available_count"] == 3
    assert [entry["patch_id"] for entry in history["entries"]] == [
        "history-1",
        "history-2",
        "history-3",
    ]
    assert all(entry["rollback_available"] for entry in history["entries"])
    assert tuple(actor.location) == (3.0, 0.0, 0.0)

    result = rollback_to_revision(revision_ids[1])
    assert result["rolled_back_count"] == 2
    assert [item["patch_id"] for item in result["revisions"]] == [
        "history-3",
        "history-2",
    ]
    assert tuple(actor.location) == (1.0, 0.0, 0.0)
    history = list_revision_history()
    assert [entry["status"] for entry in history["entries"]] == [
        "applied",
        "reverted",
        "reverted",
    ]
    assert [entry["rollback_available"] for entry in history["entries"]] == [
        True,
        False,
        False,
    ]
    undo_last_patch()
    assert tuple(actor.location) == (0.0, 0.0, 0.0)


def test_duplicate_patch_ids_still_get_unique_revision_ids():
    actor = cube("Duplicate Patch Actor")
    actor_id = ensure_entity_id(actor)
    receipts = []
    for location in (1, 2):
        receipts.append(
            apply_patch(
                operation_patch(
                    {
                        "op": "keyframe_transform",
                        "entity_id": actor_id,
                        "payload": {
                            "frames": [{"frame": 1, "location": [location, 0, 0]}]
                        },
                    },
                    patch_id="duplicate-patch-id",
                )
            )
        )

    assert receipts[0]["patch_id"] == receipts[1]["patch_id"]
    assert receipts[0]["revision_id"] != receipts[1]["revision_id"]
    history = list_revision_history()
    assert [entry["patch_id"] for entry in history["entries"]] == [
        "duplicate-patch-id",
        "duplicate-patch-id",
    ]
    assert len({entry["revision_id"] for entry in history["entries"]}) == 2

    rollback_to_revision(receipts[0]["revision_id"])
    assert tuple(actor.location) == (0.0, 0.0, 0.0)


def test_unknown_revision_rollback_does_not_change_scene_or_history():
    actor = cube("Unknown Revision Actor")
    actor_id = ensure_entity_id(actor)
    apply_patch(
        operation_patch(
            {
                "op": "keyframe_transform",
                "entity_id": actor_id,
                "payload": {"frames": [{"frame": 1, "location": [4, 0, 0]}]},
            },
            patch_id="known-revision",
        )
    )
    history_before = list_revision_history()

    try:
        rollback_to_revision("revision-does-not-exist")
    except ValueError as exc:
        assert "not available" in str(exc).lower()
    else:
        raise AssertionError("An unknown revision unexpectedly rolled back the scene")

    assert tuple(actor.location) == (4.0, 0.0, 0.0)
    assert list_revision_history() == history_before
    undo_last_patch()


def test_corrupt_audit_log_recovers_on_next_successful_patch():
    bpy.context.scene[AUDIT_LOG_KEY] = "{malformed-json"
    assert list_revision_history()["entries"] == []

    actor = cube("Audit Recovery Actor")
    actor_id = ensure_entity_id(actor)
    receipt = apply_patch(
        operation_patch(
            {
                "op": "keyframe_transform",
                "entity_id": actor_id,
                "payload": {"frames": [{"frame": 1, "location": [3, 0, 0]}]},
            },
            patch_id="audit-recovery",
        )
    )

    history = list_revision_history()
    assert history["available_count"] == 1
    assert len(history["entries"]) == 1
    assert history["entries"][0]["revision_id"] == receipt["revision_id"]
    assert history["entries"][0]["rollback_available"] is True
    assert isinstance(json.loads(bpy.context.scene[AUDIT_LOG_KEY]), list)
    undo_last_patch()


def test_revision_snapshot_and_audit_capacity_limits():
    total_revisions = MAX_AUDIT_ENTRIES + 5
    for index in range(total_revisions):
        apply_patch(
            operation_patch(
                {
                    "op": "set_frame_range",
                    "payload": {
                        "fps": 24,
                        "frame_start": 1,
                        "frame_end": 250 + index,
                    },
                },
                patch_id=f"capacity-{index}",
            )
        )

    history = list_revision_history()
    assert len(history["entries"]) == MAX_AUDIT_ENTRIES
    assert history["entries"][0]["patch_id"] == "capacity-5"
    assert history["entries"][-1]["patch_id"] == "capacity-104"
    assert history["available_count"] == MAX_REVISIONS
    available_entries = [
        entry for entry in history["entries"] if entry["rollback_available"]
    ]
    assert len(available_entries) == MAX_REVISIONS
    assert available_entries[0]["patch_id"] == "capacity-55"
    assert available_entries[-1]["patch_id"] == "capacity-104"
    assert history["entries"][49]["rollback_available"] is False
    assert history["entries"][50]["rollback_available"] is True


def test_revision_audit_survives_blend_reload_without_unsafe_rollback():
    actor = cube("Persistent History Actor")
    actor_id = ensure_entity_id(actor)
    receipt = apply_patch(
        operation_patch(
            {
                "op": "keyframe_transform",
                "entity_id": actor_id,
                "payload": {"frames": [{"frame": 1, "location": [2, 0, 0]}]},
            },
            patch_id="persistent-history",
        )
    )
    path = PROJECT / ".cache" / f"revision-persistence-{os.getpid()}.blend"
    path.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(path))
    bpy.ops.wm.open_mainfile(filepath=str(path))

    history = list_revision_history()
    assert len(history["entries"]) == 1
    assert history["entries"][0]["revision_id"] == receipt["revision_id"]
    assert history["entries"][0]["status"] == "applied"
    assert history["entries"][0]["rollback_available"] is False
    assert history["available_count"] == 0
    try:
        rollback_to_revision(receipt["revision_id"])
    except ValueError as exc:
        assert "no facelink revision" in str(exc).lower()
    else:
        raise AssertionError("A persisted audit entry was treated as an executable snapshot")
    path.unlink()


blender_addon.register()
CASES = [
    ("registration_surface", test_registration_surface),
    ("panel_stage_review_apply_discard", test_panel_stages_before_apply_and_can_discard),
    ("snapshot_identity_bounds_parent_lock", test_snapshot_identity_bounds_parent_and_lock),
    (
        "transform_keyframes_interpolation_frame_range",
        test_transform_keyframes_interpolation_and_frame_range,
    ),
    ("world_space_parent_conversion", test_world_space_keyframes_convert_through_parent_transform),
    ("world_space_parented_camera", test_world_space_camera_location_converts_through_parent),
    ("world_space_zero_parent_fail_closed", test_world_space_zero_scale_parent_fails_closed),
    ("scene_fingerprint_stale_guard", test_scene_fingerprint_rejects_changes_and_keeps_staging),
    (
        "navigation_snapshot_guard_execution",
        test_navigation_snapshot_guard_overlay_execution_and_stale_environment,
    ),
    (
        "navigation_marker_validation",
        test_navigation_markers_reject_ambiguous_and_non_mesh_objects,
    ),
    (
        "navigation_role_operators",
        test_navigation_role_operators_are_exclusive_and_validate_object_type,
    ),
    ("look_at_camera_idempotency", test_look_at_and_camera_are_idempotent),
    ("dolly_camera_keyframes", test_dolly_camera_creates_editable_keyframes),
    ("play_clip_nla_idempotency", test_play_clip_creates_one_reusable_nla_strip),
    ("locked_and_unknown_fail_closed", test_locked_objects_and_unknown_operations_fail_closed),
    ("missing_entity_error", test_missing_entity_fails_with_clear_error),
    ("failed_patch_transaction_rollback", test_failed_patch_rolls_back_earlier_operations),
    ("internal_timeline_overlap", test_internal_timeline_overlap_is_rejected_before_staging),
    ("existing_keyframe_warning", test_existing_keyframes_are_reported_as_review_warnings),
    (
        "spatial_preview_overlay",
        test_staged_preview_builds_world_paths_and_camera_frustum_without_mutation,
    ),
    ("existing_nla_overlap", test_overlapping_existing_nla_strip_is_rejected_before_staging),
    (
        "revision_undo_full_surface",
        test_revision_undo_restores_animation_constraints_camera_and_nla,
    ),
    ("revision_history_safe_rollback", test_revision_history_and_safe_rollback_to_target),
    ("duplicate_patch_revision_identity", test_duplicate_patch_ids_still_get_unique_revision_ids),
    (
        "unknown_revision_rollback_fail_closed",
        test_unknown_revision_rollback_does_not_change_scene_or_history,
    ),
    ("corrupt_audit_log_recovery", test_corrupt_audit_log_recovers_on_next_successful_patch),
    ("revision_capacity_limits", test_revision_snapshot_and_audit_capacity_limits),
    (
        "revision_audit_blend_persistence",
        test_revision_audit_survives_blend_reload_without_unsafe_rollback,
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
