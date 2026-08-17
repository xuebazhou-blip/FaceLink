import json
import time
import uuid

import bpy
from mathutils import Euler, Matrix, Vector

from .snapshot import ensure_entity_id, object_by_id, scene_fingerprint

ALLOWED_OPERATIONS = {
    "keyframe_transform",
    "look_at",
    "play_clip",
    "ensure_camera",
    "set_frame_range",
}
INTERPOLATIONS = {"LINEAR", "BEZIER", "CONSTANT"}
REVISION_STACK = []
MAX_REVISIONS = 50
MAX_AUDIT_ENTRIES = 100
AUDIT_LOG_KEY = "facelink_revision_log"


def _assert_editable(obj):
    if obj.hide_select or obj.get("facelink_locked", False):
        raise ValueError(f"FaceLink entity '{ensure_entity_id(obj)}' is locked")


def _action_fcurves(obj):
    animation = obj.animation_data
    action = animation.action if animation else None
    if not action:
        return []
    legacy_curves = getattr(action, "fcurves", None)
    if legacy_curves is not None:
        return list(legacy_curves)
    slot = getattr(animation, "action_slot", None)
    if slot is None:
        return []
    curves = []
    for layer in action.layers:
        for strip in layer.strips:
            channelbag = getattr(strip, "channelbag", None)
            if channelbag is None:
                continue
            bag = channelbag(slot)
            if bag is not None:
                curves.extend(bag.fcurves)
    return curves


def _validate_vector(value, field):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{field} must contain exactly three numbers")
    for component in value:
        float(component)


def _preflight_operations(operations):
    for operation in operations:
        if not isinstance(operation, dict):
            raise ValueError("Every patch operation must be an object")
        op = operation["op"]
        payload = operation.get("payload", {})
        if not isinstance(payload, dict):
            raise ValueError(f"{op} payload must be an object")
        if op == "set_frame_range":
            fps = float(payload["fps"])
            frame_start = int(payload["frame_start"])
            frame_end = int(payload["frame_end"])
            if fps <= 0 or frame_end < frame_start:
                raise ValueError("Invalid frame range or FPS")
        elif op == "keyframe_transform":
            obj = object_by_id(operation.get("entity_id"))
            _assert_editable(obj)
            frames = payload.get("frames")
            if not isinstance(frames, list) or not frames:
                raise ValueError("keyframe_transform requires at least one frame")
            if payload.get("space", "LOCAL") not in {"LOCAL", "WORLD"}:
                raise ValueError("Transform space must be LOCAL or WORLD")
            if payload.get("interpolation", "BEZIER") not in INTERPOLATIONS:
                raise ValueError("Unsupported keyframe interpolation")
            for item in frames:
                int(item["frame"])
                fields = [
                    field for field in ("location", "rotation_euler", "scale") if field in item
                ]
                if not fields:
                    raise ValueError("A transform keyframe must change a transform field")
                for field in fields:
                    _validate_vector(item[field], field)
        elif op == "look_at":
            obj = object_by_id(operation.get("entity_id"))
            _assert_editable(obj)
            object_by_id(payload["target_id"])
        elif op == "play_clip":
            obj = object_by_id(operation.get("entity_id"))
            _assert_editable(obj)
            if bpy.data.actions.get(payload["clip"]) is None:
                raise ValueError(f"Animation action '{payload['clip']}' was not found")
            if int(payload["frame_end"]) < int(payload["frame_start"]):
                raise ValueError("play_clip frame_end must not precede frame_start")
        elif op == "ensure_camera":
            name = payload.get("name", "FaceLink Camera")
            existing = bpy.data.objects.get(name)
            if existing is not None:
                if existing.type != "CAMERA":
                    raise ValueError(f"Object '{name}' exists but is not a camera")
                _assert_editable(existing)
            if payload.get("target"):
                object_by_id(payload["target"])
            lens = float(payload.get("lens_mm", 50.0))
            if lens < 1.0 or lens > 300.0:
                raise ValueError("Camera lens must be between 1 and 300 mm")
            if payload.get("space", "LOCAL") not in {"LOCAL", "WORLD"}:
                raise ValueError("Camera space must be LOCAL or WORLD")


def validate_patch(patch):
    """Validate a patch against the current scene without changing scene content."""
    if not isinstance(patch, dict):
        raise ValueError("Patch must be an object")
    if patch.get("schema_version") not in {"1.0", "1.1"}:
        raise ValueError("Unsupported patch schema_version")
    operations = patch.get("operations")
    if not isinstance(operations, list):
        raise ValueError("Patch operations must be a list")
    if not all(isinstance(item, dict) for item in operations):
        raise ValueError("Every patch operation must be an object")
    if not all(isinstance(item.get("op"), str) for item in operations):
        raise ValueError("Every patch operation must have a string op")
    for field in ("patch_id", "source_title"):
        if field in patch and not isinstance(patch[field], str):
            raise ValueError(f"Patch {field} must be a string")
    warnings = patch.get("warnings", [])
    if not isinstance(warnings, list) or not all(isinstance(item, str) for item in warnings):
        raise ValueError("Patch warnings must be a list of strings")
    expected_fingerprint = patch.get("scene_fingerprint")
    fingerprint_entities = patch.get("fingerprint_entities", [])
    fingerprint_frame = patch.get("fingerprint_frame")
    if expected_fingerprint is not None:
        if not isinstance(expected_fingerprint, str) or not expected_fingerprint.startswith(
            "scene-"
        ):
            raise ValueError("Patch scene_fingerprint is invalid")
        if not isinstance(fingerprint_entities, list) or not all(
            isinstance(item, str) for item in fingerprint_entities
        ):
            raise ValueError("Patch fingerprint_entities must be a list of strings")
        if fingerprint_frame is None:
            raise ValueError("A guarded patch requires fingerprint_frame")
        actual_fingerprint = scene_fingerprint(fingerprint_entities, fingerprint_frame)
        if actual_fingerprint != expected_fingerprint:
            raise ValueError(
                "The Blender scene changed after this patch was planned; scan and plan again"
            )
    elif fingerprint_entities or fingerprint_frame is not None:
        raise ValueError("Patch fingerprint metadata is incomplete")
    unknown = {item.get("op") for item in operations} - ALLOWED_OPERATIONS
    if unknown:
        raise ValueError(f"Patch contains unsupported operations: {sorted(unknown)}")
    _preflight_operations(operations)
    return operations


def summarize_patch(patch):
    """Return a compact artist-facing description after a read-only preflight."""
    operations = validate_patch(patch)
    affected = {}
    operation_types = {}
    frames = []

    for operation in operations:
        op = operation["op"]
        payload = operation.get("payload", {})
        operation_types[op] = operation_types.get(op, 0) + 1

        entity_id = operation.get("entity_id")
        if entity_id:
            obj = object_by_id(entity_id)
            affected[obj.name] = {
                "id": entity_id,
                "name": obj.name,
                "type": obj.type,
                "will_create": False,
            }

        if op == "set_frame_range":
            frames.extend((int(payload["frame_start"]), int(payload["frame_end"])))
        elif op == "keyframe_transform":
            frames.extend(int(item["frame"]) for item in payload["frames"])
        elif op == "play_clip":
            frames.extend((int(payload["frame_start"]), int(payload["frame_end"])))
        elif op == "ensure_camera":
            name = payload.get("name", "FaceLink Camera")
            camera = bpy.data.objects.get(name)
            affected[name] = {
                "id": camera.get("facelink_id") if camera else None,
                "name": name,
                "type": "CAMERA",
                "will_create": camera is None,
            }
            if payload.get("mode") == "dolly_in":
                frames.extend((int(payload["frame_start"]), int(payload["frame_end"])))

    return {
        "patch_id": patch.get("patch_id", "unknown"),
        "source_title": patch.get("source_title", "Untitled patch"),
        "operation_count": len(operations),
        "operation_types": dict(sorted(operation_types.items())),
        "affected_entities": [affected[name] for name in sorted(affected)],
        "frame_start": min(frames) if frames else None,
        "frame_end": max(frames) if frames else None,
        "warnings": [str(item) for item in patch.get("warnings", [])],
        "scene_guarded": patch.get("scene_fingerprint") is not None,
    }


def _capture_constraints(obj):
    states = []
    for constraint in obj.constraints:
        if not constraint.name.startswith("FaceLink"):
            continue
        state = {
            "name": constraint.name,
            "type": constraint.type,
            "target": constraint.target.name if getattr(constraint, "target", None) else None,
            "influence": float(constraint.influence),
        }
        for field in ("track_axis", "up_axis", "use_offset"):
            if hasattr(constraint, field):
                state[field] = getattr(constraint, field)
        states.append(state)
    return states


def _capture_nla(obj):
    animation = obj.animation_data
    track = animation.nla_tracks.get("FaceLink") if animation else None
    if track is None:
        return None
    return [
        {
            "name": strip.name,
            "frame_start": float(strip.frame_start),
            "action": strip.action,
            "scale": float(strip.scale),
            "repeat": float(strip.repeat),
        }
        for strip in track.strips
    ]


def _capture_object(obj):
    animation = obj.animation_data
    return {
        "name": obj.name,
        "location": list(obj.location),
        "rotation_euler": list(obj.rotation_euler),
        "rotation_mode": obj.rotation_mode,
        "scale": list(obj.scale),
        "camera_lens": float(obj.data.lens) if obj.type == "CAMERA" else None,
        "had_animation_data": animation is not None,
        "action": animation.action if animation else None,
        "nla": _capture_nla(obj),
        "constraints": _capture_constraints(obj),
    }


def _capture_revision(patch, operations):
    scene = bpy.context.scene
    affected = {}
    new_camera_names = []
    for operation in operations:
        entity_id = operation.get("entity_id")
        if entity_id:
            obj = object_by_id(entity_id)
            affected.setdefault(obj.name, _capture_object(obj))
        if operation["op"] == "ensure_camera":
            name = operation.get("payload", {}).get("name", "FaceLink Camera")
            camera = bpy.data.objects.get(name)
            if camera is None:
                new_camera_names.append(name)
            else:
                affected.setdefault(camera.name, _capture_object(camera))
    return {
        "revision_id": "rev-" + uuid.uuid4().hex[:16],
        "patch_id": patch.get("patch_id", "unknown"),
        "source_title": patch.get("source_title", "Untitled patch"),
        "applied_at": time.time(),
        "scene_pointer": scene.as_pointer(),
        "had_audit_log": AUDIT_LOG_KEY in scene,
        "audit_log_raw": scene.get(AUDIT_LOG_KEY),
        "scene": {
            "fps": scene.render.fps,
            "fps_base": scene.render.fps_base,
            "frame_start": scene.frame_start,
            "frame_end": scene.frame_end,
            "camera": scene.camera.name if scene.camera else None,
        },
        "objects": affected,
        "new_camera_names": new_camera_names,
    }


def _read_audit_log(scene=None):
    scene = scene or bpy.context.scene
    raw = scene.get(AUDIT_LOG_KEY, "[]")
    if not isinstance(raw, str):
        return []
    try:
        entries = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def _write_audit_log(entries, scene=None):
    scene = scene or bpy.context.scene
    scene[AUDIT_LOG_KEY] = json.dumps(
        entries[-MAX_AUDIT_ENTRIES:], ensure_ascii=False, separators=(",", ":")
    )


def clear_revision_history():
    scene = bpy.context.scene
    if AUDIT_LOG_KEY in scene:
        del scene[AUDIT_LOG_KEY]


def _record_revision(revision, operations, warnings):
    operation_types = {}
    for operation in operations:
        op = operation["op"]
        operation_types[op] = operation_types.get(op, 0) + 1
    affected_objects = sorted(
        set(revision["objects"]) | set(revision["new_camera_names"])
    )
    entry = {
        "revision_id": revision["revision_id"],
        "patch_id": revision["patch_id"],
        "source_title": revision["source_title"],
        "applied_at": revision["applied_at"],
        "operation_count": len(operations),
        "operation_types": dict(sorted(operation_types.items())),
        "affected_objects": affected_objects,
        "warnings": list(warnings),
        "status": "applied",
    }
    entries = _read_audit_log()
    entries.append(entry)
    _write_audit_log(entries)


def _mark_revision_reverted(revision):
    entries = _read_audit_log()
    for entry in reversed(entries):
        if entry.get("revision_id") == revision["revision_id"]:
            entry["status"] = "reverted"
            entry["reverted_at"] = time.time()
            break
    _write_audit_log(entries)


def list_revision_history():
    scene = bpy.context.scene
    available = {
        revision["revision_id"]
        for revision in REVISION_STACK
        if revision["scene_pointer"] == scene.as_pointer()
    }
    entries = []
    for entry in _read_audit_log(scene):
        item = dict(entry)
        item["rollback_available"] = (
            item.get("status") == "applied" and item.get("revision_id") in available
        )
        entries.append(item)
    return {
        "scene_name": scene.name,
        "entries": entries,
        "available_count": len(available),
    }


def _isolate_action(obj):
    animation = obj.animation_data
    if not animation or not animation.action:
        return
    isolated = animation.action.copy()
    isolated.name = f"{animation.action.name} [FaceLink]"
    animation.action = isolated


def _prepare_animation_edits(operations):
    prepared = set()
    for operation in operations:
        obj = None
        if operation["op"] == "keyframe_transform":
            obj = object_by_id(operation.get("entity_id"))
        elif operation["op"] == "ensure_camera":
            payload = operation.get("payload", {})
            if payload.get("mode") == "dolly_in":
                candidate = bpy.data.objects.get(payload.get("name", "FaceLink Camera"))
                if candidate and candidate.type == "CAMERA":
                    obj = candidate
        if obj and obj.name not in prepared:
            _isolate_action(obj)
            prepared.add(obj.name)


def _restore_constraints(obj, states):
    for constraint in list(obj.constraints):
        if constraint.name.startswith("FaceLink"):
            obj.constraints.remove(constraint)
    for state in states:
        constraint = obj.constraints.new(type=state["type"])
        constraint.name = state["name"]
        constraint.influence = state["influence"]
        if state.get("target"):
            constraint.target = bpy.data.objects.get(state["target"])
        for field in ("track_axis", "up_axis", "use_offset"):
            if field in state and hasattr(constraint, field):
                setattr(constraint, field, state[field])


def _restore_nla(obj, states):
    animation = obj.animation_data
    if animation:
        current = animation.nla_tracks.get("FaceLink")
        if current:
            animation.nla_tracks.remove(current)
    if states is None:
        return
    animation = obj.animation_data_create()
    track = animation.nla_tracks.new()
    track.name = "FaceLink"
    for state in states:
        strip = track.strips.new(state["name"], int(state["frame_start"]), state["action"])
        strip.scale = state["scale"]
        strip.repeat = state["repeat"]


def _restore_object(state):
    obj = bpy.data.objects.get(state["name"])
    if obj is None:
        return
    obj.location = state["location"]
    obj.rotation_mode = state["rotation_mode"]
    obj.rotation_euler = state["rotation_euler"]
    obj.scale = state["scale"]
    if state["camera_lens"] is not None:
        obj.data.lens = state["camera_lens"]
    current_action = obj.animation_data.action if obj.animation_data else None
    if state["had_animation_data"]:
        obj.animation_data_create().action = state["action"]
    elif obj.animation_data:
        obj.animation_data.action = None
    _restore_nla(obj, state["nla"])
    _restore_constraints(obj, state["constraints"])
    if not state["had_animation_data"] and obj.animation_data:
        if not obj.animation_data.action and not obj.animation_data.nla_tracks:
            obj.animation_data_clear()
    if (
        current_action
        and current_action != state["action"]
        and current_action.users == 0
        and (state["action"] is None or "[FaceLink]" in current_action.name)
    ):
        bpy.data.actions.remove(current_action)


def _restore_revision(revision, *, restore_audit=False):
    scene = bpy.context.scene
    scene_state = revision["scene"]
    scene.render.fps = scene_state["fps"]
    scene.render.fps_base = scene_state["fps_base"]
    scene.frame_start = scene_state["frame_start"]
    scene.frame_end = scene_state["frame_end"]
    scene.camera = bpy.data.objects.get(scene_state["camera"]) if scene_state["camera"] else None
    for name in revision["new_camera_names"]:
        camera = bpy.data.objects.get(name)
        if camera and camera.type == "CAMERA":
            data = camera.data
            bpy.data.objects.remove(camera, do_unlink=True)
            if data.users == 0:
                bpy.data.cameras.remove(data)
    for state in revision["objects"].values():
        _restore_object(state)
    if restore_audit:
        if revision["had_audit_log"]:
            scene[AUDIT_LOG_KEY] = revision["audit_log_raw"]
        elif AUDIT_LOG_KEY in scene:
            del scene[AUDIT_LOG_KEY]
    bpy.context.view_layer.update()


def undo_last_patch():
    if not REVISION_STACK:
        raise ValueError("No FaceLink revision is available to undo")
    if REVISION_STACK[-1]["scene_pointer"] != bpy.context.scene.as_pointer():
        REVISION_STACK.clear()
        raise ValueError("FaceLink revisions were cleared because the active scene changed")
    revision = REVISION_STACK.pop()
    _restore_revision(revision)
    _mark_revision_reverted(revision)
    return {"undone": True, "patch_id": revision["patch_id"]}


def rollback_to_revision(revision_id):
    if not isinstance(revision_id, str) or not revision_id:
        raise ValueError("revision_id is required")
    if not REVISION_STACK:
        raise ValueError("No FaceLink revision is available to roll back")
    scene_pointer = bpy.context.scene.as_pointer()
    if REVISION_STACK[-1]["scene_pointer"] != scene_pointer:
        REVISION_STACK.clear()
        raise ValueError("FaceLink revisions were cleared because the active scene changed")
    target_index = next(
        (
            index
            for index, revision in enumerate(REVISION_STACK)
            if revision["scene_pointer"] == scene_pointer
            and revision["revision_id"] == revision_id
        ),
        None,
    )
    if target_index is None:
        raise ValueError(f"Revision '{revision_id}' is not available in this Blender session")
    rolled_back = []
    while len(REVISION_STACK) > target_index:
        revision = REVISION_STACK.pop()
        _restore_revision(revision)
        _mark_revision_reverted(revision)
        rolled_back.append(
            {
                "revision_id": revision["revision_id"],
                "patch_id": revision["patch_id"],
            }
        )
    return {
        "rolled_back": True,
        "target_revision_id": revision_id,
        "rolled_back_count": len(rolled_back),
        "revisions": rolled_back,
    }


def clear_revisions():
    REVISION_STACK.clear()


def _set_interpolation(obj, frames, interpolation):
    frame_numbers = {float(item["frame"]) for item in frames}
    for curve in _action_fcurves(obj):
        for point in curve.keyframe_points:
            if float(point.co.x) in frame_numbers:
                point.interpolation = interpolation


def _parent_space_matrix(obj):
    if obj.parent:
        return obj.parent.matrix_world @ obj.matrix_parent_inverse
    return Matrix.Identity(4)


def _set_world_location(obj, location):
    parent_space = _parent_space_matrix(obj)
    if abs(parent_space.determinant()) < 1e-12:
        raise ValueError("Cannot convert world transform through a zero-scale parent")
    obj.location = parent_space.inverted() @ Vector(location)
    bpy.context.view_layer.update()


def _keyframe_transform(entity_id, payload):
    obj = object_by_id(entity_id)
    _assert_editable(obj)
    frames = payload.get("frames", [])
    space = payload.get("space", "LOCAL")
    if space not in {"LOCAL", "WORLD"}:
        raise ValueError("Transform space must be LOCAL or WORLD")
    scene = bpy.context.scene
    original_frame = scene.frame_current
    original_subframe = scene.frame_subframe
    try:
        for item in frames:
            frame = int(item["frame"])
            if space == "WORLD":
                scene.frame_set(frame)
                parent_space = _parent_space_matrix(obj)
                if abs(parent_space.determinant()) < 1e-12:
                    raise ValueError("Cannot convert world transform through a zero-scale parent")
                if "location" in item:
                    _set_world_location(obj, item["location"])
                if "rotation_euler" in item:
                    parent_rotation = parent_space.to_quaternion()
                    desired_rotation = Euler(item["rotation_euler"], "XYZ").to_quaternion()
                    obj.rotation_mode = "XYZ"
                    obj.rotation_euler = (parent_rotation.inverted() @ desired_rotation).to_euler(
                        "XYZ"
                    )
                if "scale" in item:
                    parent_scale = parent_space.to_scale()
                    if any(abs(value) < 1e-8 for value in parent_scale):
                        raise ValueError("Cannot convert world scale through a zero-scale parent")
                    obj.scale = [
                        float(item["scale"][index]) / float(parent_scale[index])
                        for index in range(3)
                    ]
            else:
                if "location" in item:
                    obj.location = item["location"]
                if "rotation_euler" in item:
                    obj.rotation_mode = "XYZ"
                    obj.rotation_euler = item["rotation_euler"]
                if "scale" in item:
                    obj.scale = item["scale"]
            if "location" in item:
                obj.keyframe_insert(data_path="location", frame=frame, group="FaceLink")
            if "rotation_euler" in item:
                obj.keyframe_insert(data_path="rotation_euler", frame=frame, group="FaceLink")
            if "scale" in item:
                obj.keyframe_insert(data_path="scale", frame=frame, group="FaceLink")
    finally:
        if space == "WORLD":
            scene.frame_set(original_frame, subframe=original_subframe)
            bpy.context.view_layer.update()
    _set_interpolation(obj, frames, payload.get("interpolation", "BEZIER"))
    return obj


def _look_at(entity_id, payload):
    obj = object_by_id(entity_id)
    _assert_editable(obj)
    target = object_by_id(payload["target_id"])
    constraint = obj.constraints.get("FaceLink Look At")
    if constraint is None:
        constraint = obj.constraints.new(type="TRACK_TO")
        constraint.name = "FaceLink Look At"
    constraint.target = target
    constraint.track_axis = "TRACK_NEGATIVE_Z" if obj.type == "CAMERA" else "TRACK_Y"
    constraint.up_axis = "UP_Y" if obj.type == "CAMERA" else "UP_Z"
    return obj


def _play_clip(entity_id, payload):
    obj = object_by_id(entity_id)
    _assert_editable(obj)
    action = bpy.data.actions.get(payload["clip"])
    if action is None:
        raise ValueError(f"Animation action '{payload['clip']}' was not found")
    animation = obj.animation_data_create()
    track = animation.nla_tracks.get("FaceLink") or animation.nla_tracks.new()
    track.name = "FaceLink"
    strip_name = f"FaceLink {action.name} {payload['frame_start']}"
    existing = track.strips.get(strip_name)
    if existing:
        track.strips.remove(existing)
    strip = track.strips.new(strip_name, int(payload["frame_start"]), action)
    source_length = max(1.0, float(action.frame_range[1] - action.frame_range[0]))
    requested_length = max(1.0, float(payload["frame_end"] - payload["frame_start"]))
    strip.scale = requested_length / source_length
    if payload.get("loop"):
        strip.repeat = max(1.0, requested_length / source_length)
    return obj


def _camera_target(camera, target):
    constraint = camera.constraints.get("FaceLink Camera Target")
    if constraint is None:
        constraint = camera.constraints.new(type="TRACK_TO")
        constraint.name = "FaceLink Camera Target"
    constraint.target = target
    constraint.track_axis = "TRACK_NEGATIVE_Z"
    constraint.up_axis = "UP_Y"


def _ensure_camera(payload):
    name = payload.get("name", "FaceLink Camera")
    camera = bpy.data.objects.get(name)
    if camera is None or camera.type != "CAMERA":
        data = bpy.data.cameras.new(name)
        camera = bpy.data.objects.new(name, data)
        bpy.context.scene.collection.objects.link(camera)
    else:
        _assert_editable(camera)
    ensure_entity_id(camera)
    camera.data.lens = float(payload.get("lens_mm", 50.0))
    target = object_by_id(payload["target"]) if payload.get("target") else None
    space = payload.get("space", "LOCAL")
    if payload.get("location"):
        location = payload["location"]
        values = [location[axis] for axis in ("x", "y", "z")]
        if space == "WORLD":
            _set_world_location(camera, values)
        else:
            camera.location = values
    elif target:
        target_location = target.matrix_world.translation if space == "WORLD" else target.location
        location = [
            target_location.x,
            target_location.y - float(payload.get("distance", 6.0)),
            target_location.z + float(payload.get("height", 2.0)),
        ]
        if space == "WORLD":
            _set_world_location(camera, location)
        else:
            camera.location = location
    if target and payload.get("mode") in {"look_at", "follow", "dolly_in"}:
        _camera_target(camera, target)
    if target and payload.get("mode") == "follow":
        constraint = camera.constraints.get("FaceLink Follow")
        if constraint is None:
            constraint = camera.constraints.new(type="COPY_LOCATION")
            constraint.name = "FaceLink Follow"
        constraint.target = target
        constraint.use_offset = True
    if target and payload.get("mode") == "dolly_in":
        start = int(payload["frame_start"])
        end = int(payload["frame_end"])
        camera.keyframe_insert(data_path="location", frame=start, group="FaceLink")
        if space == "WORLD":
            camera_location = camera.matrix_world.translation.copy()
            direction = target.matrix_world.translation - camera_location
        else:
            camera_location = camera.location.copy()
            direction = Vector(target.location) - camera_location
        if direction.length > 0.001:
            destination = camera_location + direction.normalized() * min(
                direction.length * 0.5, float(payload.get("distance", 6.0)) * 0.5
            )
            if space == "WORLD":
                _set_world_location(camera, destination)
            else:
                camera.location = destination
        camera.keyframe_insert(data_path="location", frame=end, group="FaceLink")
    bpy.context.scene.camera = camera
    return camera


def apply_patch(patch):
    operations = validate_patch(patch)

    revision = _capture_revision(patch, operations)
    _prepare_animation_edits(operations)
    changed = set()
    warnings = list(patch.get("warnings", []))
    try:
        for operation in operations:
            op = operation["op"]
            entity_id = operation.get("entity_id")
            payload = operation.get("payload", {})
            if op == "set_frame_range":
                scene = bpy.context.scene
                scene.render.fps = round(float(payload["fps"]))
                scene.render.fps_base = scene.render.fps / float(payload["fps"])
                scene.frame_start = int(payload["frame_start"])
                scene.frame_end = int(payload["frame_end"])
            elif op == "keyframe_transform":
                changed.add(ensure_entity_id(_keyframe_transform(entity_id, payload)))
            elif op == "look_at":
                changed.add(ensure_entity_id(_look_at(entity_id, payload)))
            elif op == "play_clip":
                changed.add(ensure_entity_id(_play_clip(entity_id, payload)))
            elif op == "ensure_camera":
                changed.add(ensure_entity_id(_ensure_camera(payload)))
        _record_revision(revision, operations, warnings)
    except Exception:
        _restore_revision(revision, restore_audit=True)
        raise
    REVISION_STACK.append(revision)
    if len(REVISION_STACK) > MAX_REVISIONS:
        del REVISION_STACK[0]
    return {
        "patch_id": patch.get("patch_id", "unknown"),
        "revision_id": revision["revision_id"],
        "applied_operations": len(operations),
        "changed_entities": sorted(changed),
        "warnings": warnings,
    }
