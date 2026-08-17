import hashlib
import json
import math
import uuid

import bpy
from mathutils import Vector


def ensure_entity_id(obj):
    entity_id = obj.get("facelink_id")
    if not entity_id:
        entity_id = f"obj-{uuid.uuid4().hex[:12]}"
        obj["facelink_id"] = entity_id
    return str(entity_id)


def _vec3(value):
    return {"x": float(value[0]), "y": float(value[1]), "z": float(value[2])}


def _bounds(obj):
    if not getattr(obj, "bound_box", None):
        return None
    points = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
    if not points:
        return None
    return {
        "minimum": _vec3([min(point[i] for point in points) for i in range(3)]),
        "maximum": _vec3([max(point[i] for point in points) for i in range(3)]),
    }


def _number(value):
    rounded = round(float(value), 6)
    return 0.0 if rounded == 0.0 else rounded


def _world_transform(obj):
    location, rotation, scale = obj.matrix_world.decompose()
    rotation_euler = rotation.to_euler("XYZ")
    return {
        "location": _vec3(location),
        "rotation_euler": _vec3(rotation_euler),
        "scale": _vec3(scale),
    }


def _fingerprint_entity(obj, entity_id):
    transform = _world_transform(obj)
    return {
        "id": entity_id,
        "type": obj.type,
        "location": [_number(value) for value in transform["location"].values()],
        "rotation_euler": [
            _number(value) for value in transform["rotation_euler"].values()
        ],
        "scale": [_number(value) for value in transform["scale"].values()],
        "locked": bool(obj.hide_select or obj.get("facelink_locked", False)),
        "parent": (
            str(obj.parent.get("facelink_id"))
            if obj.parent and obj.parent.get("facelink_id")
            else (f"untracked:{obj.parent.name}" if obj.parent else None)
        ),
    }


def scene_fingerprint(entity_ids, frame=None):
    """Hash relevant scene state without assigning any new stable IDs."""
    scene = bpy.context.scene
    original_frame = scene.frame_current
    original_subframe = scene.frame_subframe
    target_frame = (
        float(original_frame) + float(original_subframe) if frame is None else float(frame)
    )
    whole_frame = math.floor(target_frame)
    try:
        scene.frame_set(whole_frame, subframe=target_frame - whole_frame)
        bpy.context.view_layer.update()
        selected = []
        for entity_id in sorted(set(entity_ids)):
            obj = object_by_id(entity_id)
            selected.append(_fingerprint_entity(obj, str(entity_id)))
        canonical = {
            "scene_name": scene.name,
            "fps": _number(float(scene.render.fps) / float(scene.render.fps_base)),
            "frame_start": int(scene.frame_start),
            "frame_end": int(scene.frame_end),
            "frame_current": _number(target_frame),
            "entities": selected,
        }
        encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return "scene-" + hashlib.sha256(encoded).hexdigest()[:24]
    finally:
        scene.frame_set(original_frame, subframe=original_subframe)
        bpy.context.view_layer.update()


def scan_scene():
    scene = bpy.context.scene
    bpy.context.view_layer.update()
    entities = []
    for obj in sorted(scene.objects, key=lambda item: item.name):
        entity_id = ensure_entity_id(obj)
        entities.append(
            {
                "id": entity_id,
                "name": obj.name,
                "type": obj.type,
                "transform": _world_transform(obj),
                "bounds": _bounds(obj),
                "locked": bool(obj.hide_select or obj.get("facelink_locked", False)),
                "metadata": {
                    "parent": ensure_entity_id(obj.parent) if obj.parent else None,
                    "collection": obj.users_collection[0].name if obj.users_collection else None,
                },
            }
        )
    return {
        "schema_version": "1.1",
        "transform_space": "WORLD",
        "scene_name": scene.name,
        "fps": float(scene.render.fps) / float(scene.render.fps_base),
        "frame_start": int(scene.frame_start),
        "frame_end": int(scene.frame_end),
        "frame_current": float(scene.frame_current) + float(scene.frame_subframe),
        "entities": entities,
    }


def object_by_id(entity_id):
    for obj in bpy.context.scene.objects:
        if str(obj.get("facelink_id", "")) == str(entity_id):
            return obj
    raise ValueError(f"FaceLink entity '{entity_id}' no longer exists in this scene")
