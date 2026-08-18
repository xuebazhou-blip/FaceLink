import ast
import math

import bpy

from .action_inventory import (
    MAX_ACTION_FCURVES,
    MAX_ACTION_KEYFRAMES,
    iter_action_fcurves,
)

MAX_POSE_BAKE_SAMPLES = MAX_ACTION_KEYFRAMES // 10
SAFE_DRIVER_FUNCTIONS = {
    "abs",
    "acos",
    "asin",
    "atan",
    "atan2",
    "ceil",
    "cos",
    "degrees",
    "exp",
    "floor",
    "fmod",
    "log",
    "max",
    "min",
    "pow",
    "radians",
    "round",
    "sin",
    "sqrt",
    "tan",
    "trunc",
}
SAFE_DRIVER_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.IfExp,
    ast.Call,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.UAdd,
    ast.USub,
    ast.Not,
    ast.And,
    ast.Or,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
)


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


def _driver_curves(owner):
    animation = getattr(owner, "animation_data", None)
    return list(animation.drivers) if animation is not None else []


def _driver_expression_error(driver):
    if driver.type != "SCRIPTED":
        return None
    if driver.use_self:
        return "uses the implicit self object"
    try:
        tree = ast.parse(driver.expression, mode="eval")
    except SyntaxError:
        return "has an invalid scripted expression"
    allowed_names = {variable.name for variable in driver.variables}
    allowed_names.update(SAFE_DRIVER_FUNCTIONS)
    allowed_names.add("pi")
    for node in ast.walk(tree):
        if not isinstance(node, SAFE_DRIVER_NODES):
            return f"uses unsupported expression syntax '{type(node).__name__}'"
        if isinstance(node, ast.Name) and node.id not in allowed_names:
            return f"uses external expression name '{node.id}'"
        if isinstance(node, ast.Call) and (
            not isinstance(node.func, ast.Name)
            or node.func.id not in SAFE_DRIVER_FUNCTIONS
        ):
            return "calls a non-deterministic driver function"
    return None


def evaluated_pose_dependency_errors(source_rig):
    """Return deterministic reasons an evaluated source is not self-contained.

    v1 intentionally allows only constraints and driver variables that refer to the
    source armature object or its Armature datablock. This excludes scene state,
    helper objects, Actions and other hidden dependencies from staged execution.
    """
    allowed_ids = {source_rig.as_pointer(), source_rig.data.as_pointer()}
    errors = []

    def check_pointer(owner, prop, label):
        try:
            target = getattr(owner, prop.identifier)
        except (AttributeError, ReferenceError, TypeError):
            return
        if target is None:
            return
        pointer = getattr(target, "as_pointer", None)
        if pointer is None or pointer() not in allowed_ids:
            target_label = getattr(target, "name", target.__class__.__name__)
            errors.append(
                f"{label} property '{prop.identifier}' references external "
                f"'{target_label}'"
            )

    for pose_bone in sorted(source_rig.pose.bones, key=lambda item: item.name):
        for constraint in pose_bone.constraints:
            for prop in constraint.bl_rna.properties:
                if prop.identifier == "rna_type":
                    continue
                label = f"constraint '{pose_bone.name}/{constraint.name}'"
                if prop.type == "POINTER":
                    check_pointer(constraint, prop, label)
                elif prop.type == "COLLECTION":
                    try:
                        items = getattr(constraint, prop.identifier)
                    except (AttributeError, ReferenceError, TypeError):
                        continue
                    for item_index, item in enumerate(items):
                        for item_prop in item.bl_rna.properties:
                            if (
                                item_prop.identifier != "rna_type"
                                and item_prop.type == "POINTER"
                            ):
                                check_pointer(
                                    item,
                                    item_prop,
                                    f"{label} collection '{prop.identifier}[{item_index}]'",
                                )
    driver_owners = (("object", source_rig), ("armature data", source_rig.data))
    for owner_label, owner in driver_owners:
        for curve in _driver_curves(owner):
            expression_error = _driver_expression_error(curve.driver)
            if expression_error:
                errors.append(
                    f"{owner_label} driver '{curve.data_path}' {expression_error}"
                )
            for variable in curve.driver.variables:
                for target in variable.targets:
                    target_id = target.id
                    if target_id is None:
                        errors.append(
                            f"{owner_label} driver '{curve.data_path}' variable "
                            f"'{variable.name}' has no explicit source-rig target"
                        )
                    elif target_id.as_pointer() not in allowed_ids:
                        errors.append(
                            f"{owner_label} driver '{curve.data_path}' variable "
                            f"'{variable.name}' references external '{target_id.name}'"
                        )
    return sorted(set(errors))


def _capture_animation_state(obj):
    animation = obj.animation_data
    return {
        "existed": animation is not None,
        "action": animation.action if animation is not None else None,
        "slot": getattr(animation, "action_slot", None) if animation is not None else None,
        "tracks": (
            [(track, bool(track.mute)) for track in animation.nla_tracks]
            if animation is not None
            else []
        ),
    }


def _restore_animation_state(obj, state):
    animation = obj.animation_data
    if animation is not None:
        animation.action = state["action"]
        if state["slot"] is not None and hasattr(animation, "action_slot"):
            animation.action_slot = state["slot"]
    _restore_tracks(state["tracks"])
    if not state["existed"] and obj.animation_data is not None:
        obj.animation_data_clear()


def _assign_source_action(source_rig, source_action):
    animation = source_rig.animation_data_create()
    animation.action = source_action
    slots = list(getattr(source_action, "slots", []))
    if slots and hasattr(animation, "action_slot"):
        animation.action_slot = slots[0]


def _evaluated_local_basis(pose_bone):
    bone = pose_bone.bone
    kwargs = {"invert": True}
    if bone.parent is not None:
        kwargs["parent_matrix"] = pose_bone.parent.matrix
        kwargs["parent_matrix_local"] = bone.parent.matrix_local
    return bone.convert_local_to_pose(
        pose_bone.matrix,
        bone.matrix_local,
        **kwargs,
    )


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
    target_animation_existed = target_rig.animation_data is not None
    target_animation = None
    original_target_action = None
    original_target_slot = None
    target_track_states = []
    original_target_pose = _capture_pose(target_rig, target_names)
    try:
        target_animation = target_rig.animation_data_create()
        original_target_action = target_animation.action
        original_target_slot = getattr(target_animation, "action_slot", None)
        target_track_states = _mute_tracks(target_animation)
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
        if target_animation is not None:
            target_animation.action = original_target_action
        if (
            target_animation is not None
            and original_target_slot is not None
            and hasattr(target_animation, "action_slot")
        ):
            target_animation.action_slot = original_target_slot
        _restore_tracks(target_track_states)
        _restore_pose(target_rig, original_target_pose)
        _set_scene_frame(scene, original_frame)
        _remove_temporary_source(source_copy, data_copy)
        if not succeeded and action is not None and action.users == 0:
            bpy.data.actions.remove(action)
        if (
            not succeeded
            and not target_animation_existed
            and target_rig.animation_data is not None
        ):
            target_rig.animation_data_clear()
        bpy.context.view_layer.update()


def bake_evaluated_pose_action(
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
    """Bake the source rig's final constrained/driven pose into an editable Action."""
    if source_rig == target_rig:
        raise ValueError("bake_evaluated_pose requires distinct source and target rigs")
    dependency_errors = evaluated_pose_dependency_errors(source_rig)
    if dependency_errors:
        raise ValueError(
            "bake_evaluated_pose requires self-contained source dependencies: "
            + "; ".join(dependency_errors)
        )
    frames = sample_frames(source_action, sample_step)
    curve_budget = len(bone_map) * 10
    keyframe_budget = curve_budget * len(frames)
    if curve_budget > MAX_ACTION_FCURVES:
        raise ValueError(
            f"bake_evaluated_pose would exceed the {MAX_ACTION_FCURVES} F-curve safety limit"
        )
    if keyframe_budget > MAX_ACTION_KEYFRAMES:
        raise ValueError(
            f"bake_evaluated_pose would exceed the {MAX_ACTION_KEYFRAMES} keyframe "
            "safety limit; increase sample_step or reduce the mapping"
        )

    scene = bpy.context.scene
    original_frame = float(scene.frame_current) + float(scene.frame_subframe)
    source_names = [bone.name for bone in source_rig.pose.bones]
    target_names = list(bone_map.values())
    original_source_pose = _capture_pose(source_rig, source_names)
    original_target_pose = _capture_pose(target_rig, target_names)
    source_animation_state = _capture_animation_state(source_rig)
    target_animation_existed = target_rig.animation_data is not None
    target_animation = None
    original_target_action = None
    original_target_slot = None
    target_track_states = []
    action = None
    succeeded = False
    try:
        for track, _ in source_animation_state["tracks"]:
            track.mute = True
        target_animation = target_rig.animation_data_create()
        original_target_action = target_animation.action
        original_target_slot = getattr(target_animation, "action_slot", None)
        target_track_states = _mute_tracks(target_animation)
        source_animation = source_rig.animation_data_create()
        source_animation.action = None
        _neutralize_pose(source_rig)
        _assign_source_action(source_rig, source_action)
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
                raise ValueError(
                    "bake_evaluated_pose does not support zero-length mapped bones"
                )
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
            sampled = {
                source_name: _evaluated_local_basis(
                    source_rig.pose.bones[source_name]
                ).copy()
                for source_name in bone_map
            }
            for source_name, target_name in bone_map.items():
                basis = sampled[source_name]
                source_bone = source_rig.data.bones[source_name]
                if source_bone.parent is None:
                    if root_motion == "drop":
                        basis.translation = (0.0, 0.0, 0.0)
                    elif root_motion == "scale":
                        basis.translation *= uniform_ratio
                else:
                    basis.translation *= length_ratios[source_name]
                target_pose = target_rig.pose.bones[target_name]
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
        if target_animation is not None:
            target_animation.action = original_target_action
        if (
            target_animation is not None
            and original_target_slot is not None
            and hasattr(target_animation, "action_slot")
        ):
            target_animation.action_slot = original_target_slot
        _restore_tracks(target_track_states)
        _restore_pose(target_rig, original_target_pose)
        _restore_animation_state(source_rig, source_animation_state)
        _restore_pose(source_rig, original_source_pose)
        _set_scene_frame(scene, original_frame)
        if not succeeded and action is not None and action.users == 0:
            bpy.data.actions.remove(action)
        if (
            not succeeded
            and not target_animation_existed
            and target_rig.animation_data is not None
        ):
            target_rig.animation_data_clear()
        bpy.context.view_layer.update()
