
import bpy
from mathutils import Vector

from .snapshot import ensure_entity_id, object_by_id

ALLOWED_OPERATIONS = {
    "keyframe_transform",
    "look_at",
    "play_clip",
    "ensure_camera",
    "set_frame_range",
}


def _set_interpolation(obj, frames, interpolation):
    animation = obj.animation_data
    action = animation.action if animation else None
    if not action:
        return
    frame_numbers = {float(item["frame"]) for item in frames}
    for curve in action.fcurves:
        for point in curve.keyframe_points:
            if float(point.co.x) in frame_numbers:
                point.interpolation = interpolation


def _keyframe_transform(entity_id, payload):
    obj = object_by_id(entity_id)
    frames = payload.get("frames", [])
    for item in frames:
        frame = int(item["frame"])
        if "location" in item:
            obj.location = item["location"]
            obj.keyframe_insert(data_path="location", frame=frame, group="FaceLink")
        if "rotation_euler" in item:
            obj.rotation_mode = "XYZ"
            obj.rotation_euler = item["rotation_euler"]
            obj.keyframe_insert(data_path="rotation_euler", frame=frame, group="FaceLink")
        if "scale" in item:
            obj.scale = item["scale"]
            obj.keyframe_insert(data_path="scale", frame=frame, group="FaceLink")
    _set_interpolation(obj, frames, payload.get("interpolation", "BEZIER"))
    return obj


def _look_at(entity_id, payload):
    obj = object_by_id(entity_id)
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
    ensure_entity_id(camera)
    camera.data.lens = float(payload.get("lens_mm", 50.0))
    target = object_by_id(payload["target"]) if payload.get("target") else None
    if payload.get("location"):
        location = payload["location"]
        camera.location = [location[axis] for axis in ("x", "y", "z")]
    elif target:
        camera.location = [
            target.location.x,
            target.location.y - float(payload.get("distance", 6.0)),
            target.location.z + float(payload.get("height", 2.0)),
        ]
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
        direction = Vector(target.location) - camera.location
        if direction.length > 0.001:
            camera.location += direction.normalized() * min(
                direction.length * 0.5, float(payload.get("distance", 6.0)) * 0.5
            )
        camera.keyframe_insert(data_path="location", frame=end, group="FaceLink")
    bpy.context.scene.camera = camera
    return camera


def apply_patch(patch):
    if patch.get("schema_version") != "1.0":
        raise ValueError("Unsupported patch schema_version")
    operations = patch.get("operations")
    if not isinstance(operations, list):
        raise ValueError("Patch operations must be a list")
    unknown = {item.get("op") for item in operations} - ALLOWED_OPERATIONS
    if unknown:
        raise ValueError(f"Patch contains unsupported operations: {sorted(unknown)}")

    bpy.ops.ed.undo_push(message=f"FaceLink: {patch.get('source_title', 'Apply patch')}")
    changed = set()
    warnings = list(patch.get("warnings", []))
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
    return {
        "patch_id": patch.get("patch_id", "unknown"),
        "applied_operations": len(operations),
        "changed_entities": sorted(changed),
        "warnings": warnings,
    }

