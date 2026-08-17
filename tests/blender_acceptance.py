import json
import math
import os
import sys
import traceback
from pathlib import Path

import bpy
from mathutils import Quaternion

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "blender_extension"))

from facelink.action_inventory import (  # noqa: E402
    action_fingerprint,
    action_inventory,
    iter_action_fcurves,
)
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
from facelink.rig_inventory import rig_fingerprint  # noqa: E402
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
    for collection in (
        bpy.data.actions,
        bpy.data.armatures,
        bpy.data.cameras,
        bpy.data.meshes,
    ):
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


def armature(name, bones):
    data = bpy.data.armatures.new(f"{name} Data")
    obj = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    created = {}
    for bone_name, parent_name in bones:
        bone = data.edit_bones.new(bone_name)
        bone.head = (0.0, 0.0, float(len(created)))
        bone.tail = (0.0, 0.0, float(len(created) + 1))
        if parent_name is not None:
            bone.parent = created[parent_name]
            bone.use_connect = True
        created[bone_name] = bone
    bpy.ops.object.mode_set(mode="OBJECT")
    return obj


def geometric_armature(name, bones):
    data = bpy.data.armatures.new(f"{name} Data")
    obj = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    created = {}
    for bone_name, parent_name, head, tail in bones:
        bone = data.edit_bones.new(bone_name)
        bone.head = head
        bone.tail = tail
        if parent_name is not None:
            bone.parent = created[parent_name]
            bone.use_connect = tuple(head) == tuple(created[parent_name].tail)
        created[bone_name] = bone
    bpy.ops.object.mode_set(mode="OBJECT")
    return obj


def pose_action(obj, name, bone_names, *, include_object_location=False):
    obj.animation_data_create()
    action = bpy.data.actions.new(name)
    obj.animation_data.action = action
    for index, bone_name in enumerate(bone_names, start=1):
        pose_bone = obj.pose.bones[bone_name]
        pose_bone.rotation_mode = "XYZ"
        pose_bone.rotation_euler = (0.0, 0.0, 0.0)
        pose_bone.keyframe_insert(
            data_path="rotation_euler", frame=1, group=bone_name
        )
        pose_bone.rotation_euler.y = 0.1 * index
        pose_bone.keyframe_insert(
            data_path="rotation_euler", frame=11, group=bone_name
        )
    if include_object_location:
        obj.location.x = 0.0
        obj.keyframe_insert(data_path="location", frame=1)
        obj.location.x = 1.0
        obj.keyframe_insert(data_path="location", frame=11)
    obj.animation_data.action = None
    return action


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


def find_action_curve(action, data_path, array_index):
    return next(
        curve
        for _, curve in iter_action_fcurves(action)
        if curve.data_path == data_path and curve.array_index == array_index
    )


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
    assert first["schema_version"] == "1.4"
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

    assert snapshot["schema_version"] == "1.4"
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


def test_rig_action_inventory_retarget_execution_and_rollback():
    source_rig = armature(
        "Mixamo Source",
        [("mixamorig:Hips", None), ("mixamorig:Spine", "mixamorig:Hips")],
    )
    target_rig = armature(
        "FaceLink Target", [("pelvis", None), ("spine", "pelvis")]
    )
    source_id = ensure_entity_id(source_rig)
    target_id = ensure_entity_id(target_rig)
    source_action = pose_action(
        source_rig,
        "Mixamo Walk",
        ["mixamorig:Hips", "mixamorig:Spine"],
        include_object_location=True,
    )
    source_fingerprint = action_fingerprint(source_action)
    source_paths = {
        curve.data_path for _, curve in iter_action_fcurves(source_action)
    }

    snapshot = scan_scene()
    assert snapshot["schema_version"] == "1.4"
    source_inventory = next(
        item for item in snapshot["rigs"] if item["entity_id"] == source_id
    )
    target_inventory = next(
        item for item in snapshot["rigs"] if item["entity_id"] == target_id
    )
    action_state = next(
        item for item in snapshot["actions"] if item["name"] == "Mixamo Walk"
    )
    assert [bone["name"] for bone in source_inventory["bones"]] == [
        "mixamorig:Hips",
        "mixamorig:Spine",
    ]
    assert [bone["name"] for bone in target_inventory["bones"]] == [
        "pelvis",
        "spine",
    ]
    assert action_state["pose_bones"] == ["mixamorig:Hips", "mixamorig:Spine"]
    assert action_state["fingerprint"] == source_fingerprint
    assert action_state["keyframe_count"] > 0
    assert source_inventory["fingerprint"] == rig_fingerprint(source_rig)
    assert target_inventory["fingerprint"] == rig_fingerprint(target_rig)
    assert set(source_inventory["bones"][0]["rest_rotation"]) == {"w", "x", "y", "z"}

    patch = {
        "schema_version": "1.4",
        "patch_id": "retarget-editable-action",
        "source_title": "Retarget editable action",
        "action_fingerprints": {"Mixamo Walk": source_fingerprint},
        "rig_fingerprints": {
            source_id: source_inventory["fingerprint"],
            target_id: target_inventory["fingerprint"],
        },
        "operations": [
            {
                "op": "play_clip",
                "entity_id": target_id,
                "payload": {
                    "clip": "Mixamo Walk",
                    "frame_start": 20,
                    "frame_end": 40,
                    "loop": False,
                    "retarget": {
                        "adapter": "rename_only",
                        "strict": True,
                        "source_rig": source_id,
                        "bone_map": {
                            "mixamorig:Hips": "pelvis",
                            "mixamorig:Spine": "spine",
                        },
                    },
                },
            }
        ],
    }
    missing_rig_guard = json.loads(json.dumps(patch))
    missing_rig_guard["rig_fingerprints"] = {}
    try:
        bridge.stage_patch(missing_rig_guard)
    except ValueError as exc:
        assert "one rig fingerprint per referenced armature" in str(exc).lower()
    else:
        raise AssertionError("Scene Patch 1.4 accepted unguarded armatures")
    malformed_rig_guard = json.loads(json.dumps(patch))
    malformed_rig_guard["rig_fingerprints"][target_id] = "rig-ZZZZZZZZZZZZZZZZZZZZZZZZ"
    try:
        bridge.stage_patch(malformed_rig_guard)
    except ValueError as exc:
        assert "valid fingerprints" in str(exc).lower()
    else:
        raise AssertionError("Scene Patch 1.4 accepted a malformed rig fingerprint")
    action_names_before = set(bpy.data.actions.keys())
    staged = bridge.stage_patch(patch)
    assert staged["summary"]["action_guarded"] is True
    assert staged["summary"]["rig_guarded"] is True
    assert staged["summary"]["retargeted_action_count"] == 1
    assert staged["summary"]["retargets"][0]["mapped_bone_count"] == 2
    expected_output = staged["summary"]["retargets"][0]["output_action"]
    assert set(bpy.data.actions.keys()) == action_names_before
    assert target_rig.animation_data is None

    receipt = bridge.apply_staged_patch()["receipt"]
    assert receipt["applied_operations"] == 1
    track = target_rig.animation_data.nla_tracks["FaceLink"]
    assert len(track.strips) == 1
    derived = track.strips[0].action
    derived_name = derived.name
    assert derived_name == expected_output
    assert derived != source_action
    assert derived.get("facelink_retarget_source") == "Mixamo Walk"
    derived_inventory = action_inventory(derived)
    assert derived_inventory["pose_bones"] == ["pelvis", "spine"]
    assert any(path == "location" for path in derived_inventory["data_paths"])
    assert {curve.group.name for _, curve in iter_action_fcurves(derived) if curve.group} >= {
        "pelvis",
        "spine",
    }
    assert {curve.data_path for _, curve in iter_action_fcurves(source_action)} == source_paths
    assert {
        curve.group.name
        for _, curve in iter_action_fcurves(source_action)
        if curve.group
    } >= {"mixamorig:Hips", "mixamorig:Spine"}
    assert action_fingerprint(source_action) == source_fingerprint
    assert list_revision_history()["entries"][-1]["created_actions"] == [derived_name]

    apply_patch(patch)
    assert len(track.strips) == 1
    assert track.strips[0].action == derived
    assert len(
        [
            action
            for action in bpy.data.actions
            if action.get("facelink_retarget_source") == "Mixamo Walk"
        ]
    ) == 1
    assert list_revision_history()["entries"][-1]["created_actions"] == []

    undo_last_patch()
    assert bpy.data.actions.get(derived_name) is not None
    assert len(target_rig.animation_data.nla_tracks["FaceLink"].strips) == 1
    undo_last_patch()
    assert bpy.data.actions.get(derived_name) is None
    assert target_rig.animation_data is None


def test_retarget_action_guard_rejects_stale_source_and_keeps_staging():
    source_rig = armature("Guard Source", [("root", None)])
    target_rig = armature("Guard Target", [("pelvis", None)])
    source_action = pose_action(source_rig, "Guarded Action", ["root"])
    target_id = ensure_entity_id(target_rig)
    source_fingerprint = action_fingerprint(source_action)
    patch = {
        "schema_version": "1.3",
        "patch_id": "stale-action-guard",
        "source_title": "Stale action guard",
        "action_fingerprints": {"Guarded Action": source_fingerprint},
        "operations": [
            {
                "op": "play_clip",
                "entity_id": target_id,
                "payload": {
                    "clip": "Guarded Action",
                    "frame_start": 1,
                    "frame_end": 11,
                    "retarget": {
                        "adapter": "rename_only",
                        "bone_map": {"root": "pelvis"},
                    },
                },
            }
        ],
    }
    bridge.stage_patch(patch)
    curve = next(iter(iter_action_fcurves(source_action)))[1]
    curve.keyframe_points[0].co.y += 0.25
    try:
        bridge.apply_staged_patch()
    except ValueError as exc:
        assert "changed after this patch was planned" in str(exc)
    else:
        raise AssertionError("A staged patch accepted a modified source Action")
    assert bridge.get_staged_patch()["staged"] is True
    assert target_rig.animation_data is None
    assert not any(
        action.get("facelink_retarget_source") == "Guarded Action"
        for action in bpy.data.actions
    )
    bridge.discard_staged_patch()


def test_bake_pose_creates_axis_corrected_scaled_editable_action():
    source_rig = geometric_armature(
        "Bake Source",
        [
            ("root", None, (0, 0, 0), (0, 1, 0)),
            ("spine", "root", (0, 1, 0), (0, 2, 0)),
        ],
    )
    target_rig = geometric_armature(
        "Bake Target",
        [
            ("pelvis", None, (0, 0, 0), (2, 0, 0)),
            ("chest", "pelvis", (2, 0, 0), (4, 0, 0)),
        ],
    )
    for rig, names in ((source_rig, ("root", "spine")), (target_rig, ("pelvis", "chest"))):
        for name in names:
            rig.pose.bones[name].rotation_mode = "XYZ"
    source_rig.animation_data_create()
    source_action = bpy.data.actions.new("Bake Walk")
    source_rig.animation_data.action = source_action
    for frame, root_y, root_z, spine_x in (
        (1, 0.0, 0.0, 0.0),
        (6, 0.5, 0.25, 0.15),
        (11, 1.0, 0.5, 0.3),
    ):
        root = source_rig.pose.bones["root"]
        root.location = (0.0, root_y, 0.0)
        root.rotation_euler = (0.0, 0.0, root_z)
        root.keyframe_insert(data_path="location", frame=frame, group="root")
        root.keyframe_insert(data_path="rotation_euler", frame=frame, group="root")
        spine = source_rig.pose.bones["spine"]
        spine.rotation_euler = (spine_x, 0.0, 0.0)
        spine.keyframe_insert(data_path="rotation_euler", frame=frame, group="spine")
        source_rig.location.x = float(frame - 1) / 10.0
        source_rig.keyframe_insert(data_path="location", frame=frame)
    source_rig.animation_data.action = None

    target_rig.animation_data_create()
    target_idle = bpy.data.actions.new("Bake Target Idle")
    target_rig.animation_data.action = target_idle
    target_rig.pose.bones["chest"].rotation_euler.z = 0.1
    target_rig.pose.bones["chest"].keyframe_insert(
        data_path="rotation_euler", frame=1, group="chest"
    )

    source_id = ensure_entity_id(source_rig)
    target_id = ensure_entity_id(target_rig)
    source_fingerprint = action_fingerprint(source_action)
    snapshot = scan_scene()
    rigs = {item["entity_id"]: item for item in snapshot["rigs"]}
    patch = {
        "schema_version": "1.4",
        "patch_id": "bake-pose-editable",
        "source_title": "Bake pose editable",
        "action_fingerprints": {source_action.name: source_fingerprint},
        "rig_fingerprints": {
            source_id: rigs[source_id]["fingerprint"],
            target_id: rigs[target_id]["fingerprint"],
        },
        "operations": [
            {
                "op": "play_clip",
                "entity_id": target_id,
                "payload": {
                    "clip": source_action.name,
                    "frame_start": 20,
                    "frame_end": 40,
                    "retarget": {
                        "adapter": "bake_pose",
                        "source_rig": source_id,
                        "strict": True,
                        "sample_step": 1,
                        "root_motion": "scale",
                        "bone_map": {"root": "pelvis", "spine": "chest"},
                    },
                },
            }
        ],
    }
    source_paths = {curve.data_path for _, curve in iter_action_fcurves(source_action)}
    source_rig.animation_data.action = source_action
    source_rig.hide_viewport = True
    bpy.context.scene.frame_set(5, subframe=0.25)
    summary = bridge.stage_patch(patch)["summary"]
    assert summary["retargets"][0]["adapter"] == "bake_pose"
    assert summary["retargets"][0]["sample_count"] == 11
    assert summary["retargets"][0]["root_motion"] == "scale"
    output_name = summary["retargets"][0]["output_action"]
    assert bpy.data.actions.get(output_name) is None

    receipt = bridge.apply_staged_patch()["receipt"]
    derived = bpy.data.actions[output_name]
    assert target_rig.animation_data.nla_tracks["FaceLink"].strips[0].action == derived
    assert target_rig.animation_data.action == target_idle
    assert derived.get("facelink_retarget_adapter") == "bake_pose"
    assert derived.get("facelink_retarget_source") == source_action.name
    inventory = action_inventory(derived)
    assert inventory["pose_bones"] == ["chest", "pelvis"]
    assert all(path.startswith('pose.bones["') for path in inventory["data_paths"])
    assert inventory["fcurve_count"] == 18
    assert inventory["keyframe_count"] == 198
    assert all(
        point.interpolation == "LINEAR"
        for _, curve in iter_action_fcurves(derived)
        for point in curve.keyframe_points
    )
    pelvis_path = 'pose.bones["pelvis"].location'
    pelvis_y = find_action_curve(derived, pelvis_path, 1).evaluate(11)
    assert math.isclose(pelvis_y, 2.0), pelvis_y
    assert math.isclose(
        find_action_curve(derived, 'pose.bones["pelvis"].rotation_euler', 2).evaluate(11),
        0.5,
        abs_tol=1e-5,
    )
    assert math.isclose(
        find_action_curve(derived, 'pose.bones["chest"].rotation_euler', 0).evaluate(11),
        0.3,
        abs_tol=1e-5,
    )
    source_axis = source_rig.data.bones["root"].vector.normalized()
    target_axis = target_rig.data.bones["pelvis"].vector.normalized()
    assert abs(source_axis.dot(target_axis)) < 1e-6
    assert source_rig.animation_data.action == source_action
    assert source_rig.hide_viewport is True
    assert math.isclose(bpy.context.scene.frame_current_final, 5.25)
    assert {curve.data_path for _, curve in iter_action_fcurves(source_action)} == source_paths
    assert action_fingerprint(source_action) == source_fingerprint
    before_edit = action_fingerprint(derived)
    next(iter(iter_action_fcurves(derived)))[1].keyframe_points[0].co.y += 0.125
    assert action_fingerprint(derived) != before_edit
    assert list_revision_history()["entries"][-1]["created_actions"] == [output_name]
    assert receipt["applied_operations"] == 1

    apply_patch(patch)
    assert bpy.data.actions[output_name] == derived
    assert len(target_rig.animation_data.nla_tracks["FaceLink"].strips) == 1
    assert list_revision_history()["entries"][-1]["created_actions"] == []
    undo_last_patch()
    assert bpy.data.actions.get(output_name) == derived
    assert len(target_rig.animation_data.nla_tracks["FaceLink"].strips) == 1

    undo_last_patch()
    assert bpy.data.actions.get(output_name) is None
    assert target_rig.animation_data.action == target_idle
    assert target_rig.animation_data.nla_tracks.get("FaceLink") is None
    assert source_rig.animation_data.action == source_action


def test_bake_pose_root_motion_policies_are_deterministic():
    source_rig = geometric_armature(
        "Root Policy Source", [("root", None, (0, 0, 0), (0, 1, 0))]
    )
    source_rig.pose.bones["root"].rotation_mode = "XYZ"
    source_rig.animation_data_create()
    source_action = bpy.data.actions.new("Root Policy Action")
    source_rig.animation_data.action = source_action
    root = source_rig.pose.bones["root"]
    for frame, value in ((1, 0.0), (11, 1.0)):
        root.location = (value, 0.0, 0.0)
        root.keyframe_insert(data_path="location", frame=frame, group="root")
    source_rig.animation_data.action = None
    source_id = ensure_entity_id(source_rig)
    source_fingerprint = action_fingerprint(source_action)

    expected_values = {"scale": 2.0, "preserve": 1.0, "drop": 0.0}
    for index, (policy, expected) in enumerate(expected_values.items()):
        target = geometric_armature(
            f"Root Policy {policy}", [("pelvis", None, (0, 0, 0), (0, 2, 0))]
        )
        target.pose.bones["pelvis"].rotation_mode = "XYZ"
        target_id = ensure_entity_id(target)
        snapshot = scan_scene()
        rigs = {item["entity_id"]: item for item in snapshot["rigs"]}
        patch = {
            "schema_version": "1.4",
            "patch_id": f"root-policy-{policy}",
            "source_title": f"Root policy {policy}",
            "action_fingerprints": {source_action.name: source_fingerprint},
            "rig_fingerprints": {
                source_id: rigs[source_id]["fingerprint"],
                target_id: rigs[target_id]["fingerprint"],
            },
            "operations": [
                {
                    "op": "play_clip",
                    "entity_id": target_id,
                    "payload": {
                        "clip": source_action.name,
                        "frame_start": 1 + index * 20,
                        "frame_end": 11 + index * 20,
                        "retarget": {
                            "adapter": "bake_pose",
                            "source_rig": source_id,
                            "root_motion": policy,
                            "bone_map": {"root": "pelvis"},
                        },
                    },
                }
            ],
        }
        summary = bridge.stage_patch(patch)["summary"]
        bridge.apply_staged_patch()
        derived = bpy.data.actions[summary["retargets"][0]["output_action"]]
        curve = find_action_curve(derived, 'pose.bones["pelvis"].location', 0)
        actual = curve.evaluate(11)
        assert math.isclose(actual, expected, abs_tol=1e-6), (policy, actual, expected)


def test_bake_pose_quaternion_continuity_and_fractional_endpoint():
    source = geometric_armature(
        "Quaternion Source", [("root", None, (0, 0, 0), (0, 1, 0))]
    )
    target = geometric_armature(
        "Quaternion Target", [("pelvis", None, (0, 0, 0), (0, 1, 0))]
    )
    source.pose.bones["root"].rotation_mode = "QUATERNION"
    target.pose.bones["pelvis"].rotation_mode = "QUATERNION"
    source.animation_data_create()
    action = bpy.data.actions.new("Quaternion Crossing")
    source.animation_data.action = action
    root = source.pose.bones["root"]
    for frame, degrees in ((1.0, 170.0), (10.5, 190.0)):
        root.rotation_quaternion = Quaternion(
            (0.0, 0.0, 1.0), math.radians(degrees)
        )
        root.keyframe_insert(
            data_path="rotation_quaternion", frame=frame, group="root"
        )
    source.animation_data.action = None
    source_id = ensure_entity_id(source)
    target_id = ensure_entity_id(target)
    snapshot = scan_scene()
    rigs = {item["entity_id"]: item for item in snapshot["rigs"]}
    patch = {
        "schema_version": "1.4",
        "patch_id": "quaternion-continuity",
        "source_title": "Quaternion continuity",
        "action_fingerprints": {action.name: action_fingerprint(action)},
        "rig_fingerprints": {
            source_id: rigs[source_id]["fingerprint"],
            target_id: rigs[target_id]["fingerprint"],
        },
        "operations": [
            {
                "op": "play_clip",
                "entity_id": target_id,
                "payload": {
                    "clip": action.name,
                    "frame_start": 1,
                    "frame_end": 20,
                    "retarget": {
                        "adapter": "bake_pose",
                        "source_rig": source_id,
                        "sample_step": 4,
                        "bone_map": {"root": "pelvis"},
                    },
                },
            }
        ],
    }
    summary = bridge.stage_patch(patch)["summary"]
    assert summary["retargets"][0]["sample_count"] == 4
    bridge.apply_staged_patch()
    derived = bpy.data.actions[summary["retargets"][0]["output_action"]]
    rotation_path = 'pose.bones["pelvis"].rotation_quaternion'
    curves = [find_action_curve(derived, rotation_path, index) for index in range(4)]
    assert [float(point.co.x) for point in curves[0].keyframe_points] == [
        1.0,
        5.0,
        9.0,
        10.5,
    ]
    previous = None
    for frame in (1.0, 5.0, 9.0, 10.5):
        quaternion = Quaternion(tuple(curve.evaluate(frame) for curve in curves))
        quaternion.normalize()
        if previous is not None:
            assert quaternion.dot(previous) >= 0.0
        previous = quaternion


def test_bake_pose_boundaries_fail_closed_without_generated_actions():
    source = armature("Boundary Source", [("root", None), ("child", "root")])
    target = armature("Boundary Target", [("pelvis", None), ("chest", "pelvis")])
    action = pose_action(source, "Boundary Action", ["root", "child"])
    source_id = ensure_entity_id(source)
    target_id = ensure_entity_id(target)
    fingerprint = action_fingerprint(action)
    snapshot = scan_scene()
    rigs = {item["entity_id"]: item for item in snapshot["rigs"]}
    base = {
        "schema_version": "1.4",
        "patch_id": "bake-boundaries",
        "source_title": "Bake boundaries",
        "action_fingerprints": {action.name: fingerprint},
        "rig_fingerprints": {
            source_id: rigs[source_id]["fingerprint"],
            target_id: rigs[target_id]["fingerprint"],
        },
        "operations": [
            {
                "op": "play_clip",
                "entity_id": target_id,
                "payload": {
                    "clip": action.name,
                    "frame_start": 1,
                    "frame_end": 11,
                    "retarget": {
                        "adapter": "bake_pose",
                        "source_rig": source_id,
                        "bone_map": {"root": "pelvis", "child": "chest"},
                    },
                },
            }
        ],
    }
    before = set(bpy.data.actions.keys())
    target.pose.bones["chest"].constraints.new(type="COPY_ROTATION")
    try:
        bridge.stage_patch(base)
    except ValueError as exc:
        assert "constrained target bones" in str(exc)
    else:
        raise AssertionError("bake_pose accepted a constrained target bone")
    assert set(bpy.data.actions.keys()) == before
    assert target.animation_data is None

    old_schema = json.loads(json.dumps(base))
    old_schema["schema_version"] = "1.3"
    old_schema["rig_fingerprints"] = {}
    try:
        bridge.stage_patch(old_schema)
    except ValueError as exc:
        assert "Scene Patch 1.4" in str(exc)
    else:
        raise AssertionError("bake_pose accepted an unguarded old patch schema")
    assert set(bpy.data.actions.keys()) == before
    for constraint in list(target.pose.bones["chest"].constraints):
        target.pose.bones["chest"].constraints.remove(constraint)

    bridge.stage_patch(base)
    original_mode = target.pose.bones["chest"].rotation_mode
    target.pose.bones["chest"].rotation_mode = (
        "XYZ" if original_mode != "XYZ" else "QUATERNION"
    )
    try:
        bridge.apply_staged_patch()
    except ValueError as exc:
        assert "changed after this patch was planned" in str(exc)
    else:
        raise AssertionError("bake_pose accepted a changed target rotation representation")
    assert bridge.get_staged_patch()["staged"] is True
    assert set(bpy.data.actions.keys()) == before
    assert target.animation_data is None
    target.pose.bones["chest"].rotation_mode = original_mode
    bridge.discard_staged_patch()

    target.pose.bones["chest"].driver_add("location", 0)
    try:
        bridge.stage_patch(base)
    except ValueError as exc:
        assert "driven target bones" in str(exc)
    else:
        raise AssertionError("bake_pose accepted a driven target bone")
    assert set(bpy.data.actions.keys()) == before
    target.pose.bones["chest"].driver_remove("location", 0)
    target.animation_data_clear()

    missing_source = json.loads(json.dumps(base))
    del missing_source["operations"][0]["payload"]["retarget"]["source_rig"]
    missing_source["rig_fingerprints"] = {target_id: rigs[target_id]["fingerprint"]}
    try:
        bridge.stage_patch(missing_source)
    except ValueError as exc:
        assert "explicit source_rig" in str(exc)
    else:
        raise AssertionError("bake_pose accepted no source rig")
    assert set(bpy.data.actions.keys()) == before

    invalid_step = json.loads(json.dumps(base))
    invalid_step["operations"][0]["payload"]["retarget"]["sample_step"] = 0
    try:
        bridge.stage_patch(invalid_step)
    except ValueError as exc:
        assert "sample_step" in str(exc)
    else:
        raise AssertionError("bake_pose accepted an invalid sample step")
    assert set(bpy.data.actions.keys()) == before

    malformed_root = json.loads(json.dumps(base))
    malformed_root["operations"][0]["payload"]["retarget"]["root_motion"] = []
    try:
        bridge.stage_patch(malformed_root)
    except ValueError as exc:
        assert "root_motion" in str(exc)
    else:
        raise AssertionError("bake_pose accepted a non-string root motion policy")
    assert set(bpy.data.actions.keys()) == before

    source.animation_data.action = action
    source.pose.bones["root"].rotation_euler.y = 0.5
    source.pose.bones["root"].keyframe_insert(
        data_path="rotation_euler", frame=20001, group="root"
    )
    source.animation_data.action = None
    oversized = json.loads(json.dumps(base))
    oversized["action_fingerprints"][action.name] = action_fingerprint(action)
    try:
        bridge.stage_patch(oversized)
    except ValueError as exc:
        assert "sample safety limit" in str(exc) or "keyframe safety limit" in str(exc)
    else:
        raise AssertionError("bake_pose accepted an oversized sampling workload")
    assert set(bpy.data.actions.keys()) == before
    assert target.animation_data is None


def test_retarget_rig_guard_rejects_rest_pose_edit_and_keeps_staging():
    source_rig = armature("Rig Guard Source", [("root", None)])
    target_rig = armature("Rig Guard Target", [("pelvis", None)])
    source_id = ensure_entity_id(source_rig)
    target_id = ensure_entity_id(target_rig)
    source_action = pose_action(source_rig, "Rig Guard Action", ["root"])
    snapshot = scan_scene()
    rig_states = {item["entity_id"]: item for item in snapshot["rigs"]}
    patch = {
        "schema_version": "1.4",
        "patch_id": "stale-rig-guard",
        "source_title": "Stale rig guard",
        "action_fingerprints": {
            "Rig Guard Action": action_fingerprint(source_action)
        },
        "rig_fingerprints": {
            source_id: rig_states[source_id]["fingerprint"],
            target_id: rig_states[target_id]["fingerprint"],
        },
        "operations": [
            {
                "op": "play_clip",
                "entity_id": target_id,
                "payload": {
                    "clip": "Rig Guard Action",
                    "frame_start": 1,
                    "frame_end": 11,
                    "retarget": {
                        "adapter": "rename_only",
                        "bone_map": {"root": "pelvis"},
                        "source_rig": source_id,
                    },
                },
            }
        ],
    }
    bridge.stage_patch(patch)
    bpy.ops.object.select_all(action="DESELECT")
    target_rig.select_set(True)
    bpy.context.view_layer.objects.active = target_rig
    bpy.ops.object.mode_set(mode="EDIT")
    target_rig.data.edit_bones["pelvis"].tail.z += 0.5
    bpy.ops.object.mode_set(mode="OBJECT")
    try:
        bridge.apply_staged_patch()
    except ValueError as exc:
        assert "changed after this patch was planned" in str(exc)
    else:
        raise AssertionError("A staged patch accepted a modified target rest pose")
    assert bridge.get_staged_patch()["staged"] is True
    assert target_rig.animation_data is None
    assert not any(
        action.get("facelink_retarget_source") == "Rig Guard Action"
        for action in bpy.data.actions
    )
    bridge.discard_staged_patch()


def test_retarget_invalid_maps_fail_closed_without_creating_actions():
    source_rig = armature("Invalid Source", [("root", None), ("spine", "root")])
    target_rig = armature("Invalid Target", [("pelvis", None), ("chest", "pelvis")])
    source_action = pose_action(source_rig, "Invalid Map Action", ["root", "spine"])
    target_id = ensure_entity_id(target_rig)
    fingerprint = action_fingerprint(source_action)
    base_patch = {
        "schema_version": "1.3",
        "patch_id": "invalid-retarget-map",
        "source_title": "Invalid retarget map",
        "action_fingerprints": {"Invalid Map Action": fingerprint},
        "operations": [
            {
                "op": "play_clip",
                "entity_id": target_id,
                "payload": {
                    "clip": "Invalid Map Action",
                    "frame_start": 1,
                    "frame_end": 11,
                    "retarget": {
                        "adapter": "rename_only",
                        "strict": True,
                        "bone_map": {"root": "pelvis", "spine": "chest"},
                    },
                },
            }
        ],
    }
    invalid_variants = [
        ({"root": "pelvis"}, "does not cover"),
        ({"root": "pelvis", "spine": "missing"}, "missing bone"),
        ({"root": "pelvis", "spine": "pelvis"}, "must be unique"),
    ]
    action_names_before = set(bpy.data.actions.keys())
    for bone_map, expected_message in invalid_variants:
        candidate = json.loads(json.dumps(base_patch))
        candidate["operations"][0]["payload"]["retarget"]["bone_map"] = bone_map
        try:
            bridge.stage_patch(candidate)
        except ValueError as exc:
            assert expected_message in str(exc).lower()
        else:
            raise AssertionError(f"Invalid retarget map was accepted: {bone_map}")
        assert set(bpy.data.actions.keys()) == action_names_before
        assert target_rig.animation_data is None

    unsupported = json.loads(json.dumps(base_patch))
    unsupported["operations"][0]["payload"]["retarget"]["adapter"] = "rest_pose"
    try:
        bridge.stage_patch(unsupported)
    except ValueError as exc:
        assert "rename_only" in str(exc)
    else:
        raise AssertionError("An unsupported retarget adapter was accepted")
    assert set(bpy.data.actions.keys()) == action_names_before

    missing_guard = json.loads(json.dumps(base_patch))
    missing_guard["action_fingerprints"] = {}
    try:
        bridge.stage_patch(missing_guard)
    except ValueError as exc:
        assert "one action fingerprint per play_clip" in str(exc).lower()
    else:
        raise AssertionError("Scene Patch 1.3 accepted an unguarded source Action")

    malformed_guard = json.loads(json.dumps(base_patch))
    malformed_guard["action_fingerprints"]["Invalid Map Action"] = (
        "action-ZZZZZZZZZZZZZZZZZZZZZZZZ"
    )
    try:
        bridge.stage_patch(malformed_guard)
    except ValueError as exc:
        assert "valid fingerprints" in str(exc).lower()
    else:
        raise AssertionError("A malformed source Action fingerprint was accepted")
    assert set(bpy.data.actions.keys()) == action_names_before
    assert target_rig.animation_data is None


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

    invalid_compositions = [
        ([], "must be an object"),
        ({"min_subject_height": 0.9, "max_subject_height": 0.2}, "less than"),
        ({"safe_margin": 0.5}, "between 0 and 0.45"),
        ({"enabled": "yes"}, "must be a boolean"),
        ({"safe_margin": True}, "must be a number"),
        ({"untrusted": True}, "unsupported fields"),
    ]
    for index, (composition, expected) in enumerate(invalid_compositions):
        invalid_composition = operation_patch(
            {
                "op": "ensure_camera",
                "payload": {
                    "name": f"Invalid Composition {index}",
                    "mode": "static",
                    "composition": composition,
                },
            },
            patch_id=f"invalid-composition-{index}",
        )
        try:
            bridge.stage_patch(invalid_composition)
        except ValueError as exc:
            assert expected in str(exc).lower()
        else:
            raise AssertionError(f"Invalid composition {index} was staged")
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
    unavailable = result["summary"]["composition"]
    assert unavailable["evaluated_count"] == 1
    assert unavailable["shots"][0]["status"] == "unavailable"
    assert unavailable["warnings"][0]["code"] == "composition_target_unavailable"
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


def test_camera_composition_preflight_reports_size_clipping_occlusion_and_dolly():
    target = cube("Composition Target")
    target_id = ensure_entity_id(target)
    original_transform = target.matrix_world.copy()
    camera_count = len(bpy.data.cameras)

    def camera_patch(name, location, *, mode="look_at", distance=8, composition=None):
        payload = {
            "name": name,
            "mode": mode,
            "target": target_id,
            "space": "WORLD",
            "location": {"x": location[0], "y": location[1], "z": location[2]},
            "distance": distance,
            "lens_mm": 50,
            "frame_start": 1,
            "frame_end": 49,
        }
        if composition is not None:
            payload["composition"] = composition
        return operation_patch(
            {"op": "ensure_camera", "payload": payload},
            patch_id=f"composition-{name}",
        )

    good = bridge.stage_patch(camera_patch("Good Camera", (0, -8, 0)))
    analysis = good["summary"]["composition"]
    assert analysis["evaluated_count"] == 1
    assert analysis["warning_count"] == 0
    metrics = analysis["shots"][0]["metrics"]
    assert metrics["fully_visible"] is True
    assert metrics["inside_safe_area"] is True
    assert 0.6 < metrics["subject_height"] < 0.8
    assert metrics["center_offset"] == 0
    assert metrics["center_occluded"] is False
    bridge.discard_staged_patch()

    render = bpy.context.scene.render
    original_resolution = (render.resolution_x, render.resolution_y)
    render.resolution_x = 1080
    render.resolution_y = 1920
    portrait = bridge.stage_patch(camera_patch("Portrait Camera", (0, -8, 0)))
    portrait_height = portrait["summary"]["composition"]["shots"][0]["metrics"][
        "subject_height"
    ]
    assert 0.4 < portrait_height < 0.5
    bridge.discard_staged_patch()
    render.resolution_x, render.resolution_y = original_resolution

    distant = bridge.stage_patch(camera_patch("Distant Camera", (0, -50, 0)))
    distant_codes = {
        item["code"] for item in distant["summary"]["composition"]["warnings"]
    }
    assert "subject_too_small" in distant_codes
    bridge.discard_staged_patch()

    offset = bridge.stage_patch(
        camera_patch("Offset Camera", (1.5, 0, 8), mode="static")
    )
    offset_codes = {item["code"] for item in offset["summary"]["composition"]["warnings"]}
    assert {"subject_outside_safe_area", "subject_off_center"} <= offset_codes
    bridge.discard_staged_patch()

    behind = bridge.stage_patch(
        camera_patch("Backwards Camera", (0, 0, -8), mode="static")
    )
    behind_shot = behind["summary"]["composition"]["shots"][0]
    assert behind_shot["metrics"]["visible_corner_count"] == 0
    assert behind_shot["warnings"][0]["code"] == "subject_behind_camera"
    bridge.discard_staged_patch()

    camera_data = bpy.data.cameras.new("Orthographic Existing")
    camera_data.type = "ORTHO"
    orthographic = bpy.data.objects.new("Orthographic Existing", camera_data)
    bpy.context.scene.collection.objects.link(orthographic)
    orthographic.location = (0, 0, 8)
    ensure_entity_id(orthographic)
    unsupported = bridge.stage_patch(
        camera_patch("Orthographic Existing", (0, 0, 8), mode="static")
    )
    unsupported_shot = unsupported["summary"]["composition"]["shots"][0]
    assert unsupported_shot["status"] == "unavailable"
    assert unsupported_shot["warnings"][0]["code"] == "composition_camera_unsupported"
    bridge.discard_staged_patch()

    camera_data.type = "PERSP"
    camera_data.shift_x = 0.1
    shifted = bridge.stage_patch(
        camera_patch("Orthographic Existing", (0, 0, 8), mode="static")
    )
    shifted_shot = shifted["summary"]["composition"]["shots"][0]
    assert shifted_shot["status"] == "unavailable"
    assert "lens shift" in shifted_shot["warnings"][0]["message"]
    bridge.discard_staged_patch()
    bpy.data.objects.remove(orthographic, do_unlink=True)
    bpy.data.cameras.remove(camera_data)

    occluder = cube("Foreground Occluder", (0, -4, 0))
    bpy.context.view_layer.update()
    occluded = bridge.stage_patch(camera_patch("Occluded Camera", (0, -8, 0)))
    occluded_shot = occluded["summary"]["composition"]["shots"][0]
    assert occluded_shot["metrics"]["center_occluded"] is True
    assert "subject_occluded" in {
        item["code"] for item in occluded_shot["warnings"]
    }
    applied_occluded = bridge.apply_staged_patch()
    assert any(
        "blocks the center" in warning
        for warning in applied_occluded["receipt"]["warnings"]
    )
    assert undo_last_patch()["patch_id"] == "composition-Occluded Camera"
    assert bpy.data.objects.get("Occluded Camera") is None
    bpy.data.objects.remove(occluder, do_unlink=True)
    bpy.context.view_layer.update()

    dolly = bridge.stage_patch(
        camera_patch("Dolly Composition", (0, -8, 0), mode="dolly_in", distance=8)
    )
    dolly_analysis = dolly["summary"]["composition"]
    assert dolly_analysis["evaluated_count"] == 2
    assert [item["sample"] for item in dolly_analysis["shots"]] == ["start", "end"]
    end_codes = {item["code"] for item in dolly_analysis["shots"][1]["warnings"]}
    assert {"subject_clipped", "subject_too_large"} <= end_codes
    bridge.discard_staged_patch()

    disabled = bridge.stage_patch(
        camera_patch(
            "Disabled Composition",
            (0, -8, 0),
            composition={"enabled": False},
        )
    )
    assert disabled["summary"]["composition"]["evaluated_count"] == 0
    assert disabled["summary"]["composition_warning_count"] == 0
    bridge.discard_staged_patch()

    assert len(bpy.data.cameras) == camera_count
    assert bpy.data.objects.get("Good Camera") is None
    assert target.matrix_world == original_transform


def test_camera_preflight_and_execution_use_declared_frame_and_restore_timeline():
    target = cube("Animated Camera Target")
    target_id = ensure_entity_id(target)
    target.location.x = 0
    target.keyframe_insert(data_path="location", frame=1)
    target.location.x = 4
    target.keyframe_insert(data_path="location", frame=49)
    scene = bpy.context.scene
    scene.frame_set(37)
    bpy.context.view_layer.update()
    current_target_x = target.matrix_world.translation.x
    assert 2 < current_target_x < 4

    patch = operation_patch(
        {
            "op": "ensure_camera",
            "payload": {
                "name": "Frame-stable Camera",
                "mode": "look_at",
                "target": target_id,
                "space": "WORLD",
                "distance": 8,
                "height": 0,
                "lens_mm": 50,
                "frame_start": 1,
                "frame_end": 49,
                "composition": {"check_occlusion": False},
            },
        },
        patch_id="frame-stable-camera",
    )
    staged = bridge.stage_patch(patch)
    shot = staged["summary"]["composition"]["shots"][0]
    assert shot["frame"] == 1
    assert shot["camera_location"] == [0.0, -8.0, 0.0]
    assert shot["metrics"]["center_offset"] == 0
    assert overlay.preview_geometry()["frustums"][0]["origin"] == [0.0, -8.0, 0.0]
    assert scene.frame_current == 37
    assert target.matrix_world.translation.x == current_target_x

    dolly_patch = operation_patch(
        {
            "op": "ensure_camera",
            "payload": {
                "name": "Animated Dolly Preview",
                "mode": "dolly_in",
                "target": target_id,
                "space": "WORLD",
                "distance": 8,
                "height": 0,
                "lens_mm": 50,
                "frame_start": 1,
                "frame_end": 49,
                "composition": {"check_occlusion": False},
            },
        },
        patch_id="animated-dolly-preview",
    )
    dolly = bridge.stage_patch(dolly_patch)["summary"]["composition"]
    assert [shot["frame"] for shot in dolly["shots"]] == [1, 49]
    assert dolly["shots"][0]["camera_location"] == [0.0, -8.0, 0.0]
    assert dolly["shots"][1]["camera_location"][0] > 1.5
    assert scene.frame_current == 37
    assert target.matrix_world.translation.x == current_target_x
    bridge.discard_staged_patch()

    restaged = bridge.stage_patch(patch)["summary"]["composition"]["shots"][0]
    assert restaged["camera_location"] == [0.0, -8.0, 0.0]
    receipt = bridge.apply_staged_patch()["receipt"]
    assert receipt["patch_id"] == "frame-stable-camera"
    camera = bpy.data.objects["Frame-stable Camera"]
    assert tuple(round(value, 6) for value in camera.location) == (0.0, -8.0, 0.0)
    assert scene.frame_current == 37
    assert target.matrix_world.translation.x == current_target_x
    assert undo_last_patch()["patch_id"] == "frame-stable-camera"
    assert bpy.data.objects.get("Frame-stable Camera") is None
    assert scene.frame_current == 37


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
    (
        "rig_action_inventory_retarget_rollback",
        test_rig_action_inventory_retarget_execution_and_rollback,
    ),
    (
        "bake_pose_axis_scale_editable_action",
        test_bake_pose_creates_axis_corrected_scaled_editable_action,
    ),
    (
        "bake_pose_root_motion_policies",
        test_bake_pose_root_motion_policies_are_deterministic,
    ),
    (
        "bake_pose_quaternion_continuity",
        test_bake_pose_quaternion_continuity_and_fractional_endpoint,
    ),
    (
        "bake_pose_boundaries_fail_closed",
        test_bake_pose_boundaries_fail_closed_without_generated_actions,
    ),
    (
        "retarget_stale_action_guard",
        test_retarget_action_guard_rejects_stale_source_and_keeps_staging,
    ),
    (
        "retarget_stale_rig_guard",
        test_retarget_rig_guard_rejects_rest_pose_edit_and_keeps_staging,
    ),
    (
        "retarget_invalid_maps_fail_closed",
        test_retarget_invalid_maps_fail_closed_without_creating_actions,
    ),
    ("locked_and_unknown_fail_closed", test_locked_objects_and_unknown_operations_fail_closed),
    ("missing_entity_error", test_missing_entity_fails_with_clear_error),
    ("failed_patch_transaction_rollback", test_failed_patch_rolls_back_earlier_operations),
    ("internal_timeline_overlap", test_internal_timeline_overlap_is_rejected_before_staging),
    ("existing_keyframe_warning", test_existing_keyframes_are_reported_as_review_warnings),
    (
        "spatial_preview_overlay",
        test_staged_preview_builds_world_paths_and_camera_frustum_without_mutation,
    ),
    (
        "camera_composition_preflight",
        test_camera_composition_preflight_reports_size_clipping_occlusion_and_dolly,
    ),
    (
        "camera_declared_frame_stability",
        test_camera_preflight_and_execution_use_declared_frame_and_restore_timeline,
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
