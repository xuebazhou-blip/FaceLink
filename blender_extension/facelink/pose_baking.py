import math

import bpy

from .action_inventory import (
    MAX_ACTION_FCURVES,
    MAX_ACTION_KEYFRAMES,
    iter_action_fcurves,
)

MAX_POSE_BAKE_SAMPLES = MAX_ACTION_KEYFRAMES // 10


def sample_frames(action, step):
    """Return bounded, deterministic samples including both Action endpoints."""
    start, end = (float(value) for value in action.frame_range)
    if not math.isfinite(start) or not math.isfinite(end) or end < start:
        raise ValueError(f"Action '{action.name}' has an invalid frame range")
    span = end - start
    count = 1 if span <= 1e-8 else int(math.ceil(span / float(step) - 1e-12)) + 1
    if count > MAX_POSE_BAKE_SAMPLES:
        raise ValueError(
            f"bake_pose would exceed the {MAX_POSE_BAKE_SAMPLES} sample safety limit; "
            "increase sample_step or shorten the source Action"
        )
    frames = [start]
    cursor = start + float(step)
    while cursor < end - 1e-8:
        frames.append(cursor)
        cursor += float(step)
    if end > start + 1e-8:
        frames.append(end)
    return frames


def _set_scene_frame(scene, frame):
    integral = math.floor(frame)
    scene.frame_set(integral, subframe=frame - integral)


def _neutralize_pose(obj):
    for pose_bone in obj.pose.bones:
        pose_bone.location = (0.0, 0.0, 0.0)
        pose_bone.scale = (1.0, 1.0, 1.0)
        pose_bone.rotation_euler = (0.0, 0.0, 0.0)
        pose_bone.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
        pose_bone.rotation_axis_angle = (0.0, 0.0, 1.0, 0.0)


def _temporary_source(source_rig, source_action):
    source_copy = source_rig.copy()
    data_copy = source_rig.data.copy()
    try:
        source_copy.data = data_copy
        source_copy.name = "FaceLink Pose Bake Source"
        source_copy.hide_render = True
        source_copy.hide_viewport = False
        source_copy.hide_set(False)
        source_copy.select_set(False)
        if "facelink_id" in source_copy:
            del source_copy["facelink_id"]
        bpy.context.scene.collection.objects.link(source_copy)
        source_copy.animation_data_clear()
        _neutralize_pose(source_copy)
        animation = source_copy.animation_data_create()
        animation.action = source_action
        slots = list(getattr(source_action, "slots", []))
        if slots and hasattr(animation, "action_slot"):
            animation.action_slot = slots[0]
        return source_copy, data_copy
    except Exception:
        _remove_temporary_source(source_copy, data_copy)
        raise


def _remove_temporary_source(source_copy, data_copy):
    if source_copy is not None and source_copy.name in bpy.data.objects:
        bpy.data.objects.remove(source_copy, do_unlink=True)
    if data_copy is not None and data_copy.users == 0:
        bpy.data.armatures.remove(data_copy)


def _capture_pose(obj, names):
    return {name: obj.pose.bones[name].matrix_basis.copy() for name in names}


def _restore_pose(obj, matrices):
    for name, matrix in matrices.items():
        pose_bone = obj.pose.bones.get(name)
        if pose_bone is not None:
            pose_bone.matrix_basis = matrix


def _mute_tracks(animation):
    states = []
    if animation is None:
        return states
    for track in animation.nla_tracks:
        states.append((track, bool(track.mute)))
        track.mute = True
    return states


def _restore_tracks(states):
    for track, mute in states:
        try:
            track.mute = mute
        except ReferenceError:
            continue


def _keyframe_pose_bone(pose_bone, frame, previous_rotation):
    pose_bone.keyframe_insert(data_path="location", frame=frame, group=pose_bone.name)
    pose_bone.keyframe_insert(data_path="scale", frame=frame, group=pose_bone.name)
    mode = pose_bone.rotation_mode
    if mode == "QUATERNION":
        rotation = pose_bone.rotation_quaternion.copy()
        rotation.normalize()
        if previous_rotation is not None and rotation.dot(previous_rotation) < 0.0:
            rotation.negate()
        pose_bone.rotation_quaternion = rotation
        pose_bone.keyframe_insert(
            data_path="rotation_quaternion", frame=frame, group=pose_bone.name
        )
    elif mode == "AXIS_ANGLE":
        rotation = pose_bone.matrix_basis.to_quaternion()
        rotation.normalize()
        if previous_rotation is not None and rotation.dot(previous_rotation) < 0.0:
            rotation.negate()
        angle, axis = rotation.to_axis_angle()
        pose_bone.rotation_axis_angle = (angle, axis.x, axis.y, axis.z)
        pose_bone.keyframe_insert(
            data_path="rotation_axis_angle", frame=frame, group=pose_bone.name
        )
    else:
        rotation = pose_bone.rotation_euler.copy()
        if previous_rotation is not None:
            rotation.make_compatible(previous_rotation)
        pose_bone.rotation_euler = rotation
        pose_bone.keyframe_insert(
            data_path="rotation_euler", frame=frame, group=pose_bone.name
        )
    return rotation


def bake_pose_action(
    target_rig,
    source_rig,
    source_action,
    *,
    action_name,
    bone_map,
    scale_map,
    sample_step=1,
    root_motion="scale",
):
    """Sample an Action's local pose basis into a normal editable target Action.

    The rigs must have an equivalent mapped parent hierarchy and no pose constraints on
    mapped bones. Local basis transfer corrects different rest axes automatically because
    Blender evaluates the copied transform relative to the target bone's own rest matrix.
    """
    frames = sample_frames(source_action, sample_step)
    curve_budget = len(bone_map) * 10
    keyframe_budget = curve_budget * len(frames)
    if curve_budget > MAX_ACTION_FCURVES:
        raise ValueError(
            f"bake_pose would exceed the {MAX_ACTION_FCURVES} F-curve safety limit"
        )
    if keyframe_budget > MAX_ACTION_KEYFRAMES:
        raise ValueError(
            f"bake_pose would exceed the {MAX_ACTION_KEYFRAMES} keyframe safety limit; "
            "increase sample_step or reduce the mapping"
        )

    target_names = list(bone_map.values())
    source_copy = None
    data_copy = None
    action = None
    succeeded = False
    scene = bpy.context.scene
    original_frame = float(scene.frame_current) + float(scene.frame_subframe)
    target_animation = target_rig.animation_data_create()
    original_target_action = target_animation.action
    original_target_slot = getattr(target_animation, "action_slot", None)
    target_track_states = _mute_tracks(target_animation)
    original_target_pose = _capture_pose(target_rig, target_names)
    try:
        source_copy, data_copy = _temporary_source(source_rig, source_action)
        action = bpy.data.actions.new(action_name)
        action.use_fake_user = False
        target_animation.action = action
        bpy.context.view_layer.update()

        length_ratios = {}
        mapped_ratios = []
        for source_name, target_name in scale_map.items():
            source_length = float(source_rig.data.bones[source_name].length)
            target_length = float(target_rig.data.bones[target_name].length)
            if source_length <= 1e-8 or target_length <= 1e-8:
                raise ValueError("bake_pose does not support zero-length mapped bones")
            ratio = target_length / source_length
            length_ratios[source_name] = ratio
            mapped_ratios.append(ratio)
        ordered_ratios = sorted(mapped_ratios)
        middle = len(ordered_ratios) // 2
        uniform_ratio = (
            ordered_ratios[middle]
            if len(ordered_ratios) % 2
            else (ordered_ratios[middle - 1] + ordered_ratios[middle]) * 0.5
        )

        previous_rotations = {}
        for frame in frames:
            _set_scene_frame(scene, frame)
            bpy.context.view_layer.update()
            for source_name, target_name in bone_map.items():
                source_pose = source_copy.pose.bones[source_name]
                target_pose = target_rig.pose.bones[target_name]
                basis = source_pose.matrix_basis.copy()
                source_bone = source_rig.data.bones[source_name]
                if source_bone.parent is None:
                    if root_motion == "drop":
                        basis.translation = (0.0, 0.0, 0.0)
                    elif root_motion == "scale":
                        basis.translation *= uniform_ratio
                else:
                    basis.translation *= length_ratios[source_name]
                target_pose.matrix_basis = basis
                previous_rotations[target_name] = _keyframe_pose_bone(
                    target_pose,
                    frame,
                    previous_rotations.get(target_name),
                )

        for _, curve in iter_action_fcurves(action):
            for point in curve.keyframe_points:
                point.interpolation = "LINEAR"
        succeeded = True
        return action
    finally:
        target_animation.action = original_target_action
        if original_target_slot is not None and hasattr(target_animation, "action_slot"):
            target_animation.action_slot = original_target_slot
        _restore_tracks(target_track_states)
        _restore_pose(target_rig, original_target_pose)
        _set_scene_frame(scene, original_frame)
        _remove_temporary_source(source_copy, data_copy)
        if not succeeded and action is not None and action.users == 0:
            bpy.data.actions.remove(action)
        bpy.context.view_layer.update()
