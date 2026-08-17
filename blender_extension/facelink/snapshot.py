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
                "transform": {
                    "location": _vec3(obj.location),
                    "rotation_euler": _vec3(obj.rotation_euler),
                    "scale": _vec3(obj.scale),
                },
                "bounds": _bounds(obj),
                "locked": bool(obj.hide_select or obj.get("facelink_locked", False)),
                "metadata": {
                    "parent": ensure_entity_id(obj.parent) if obj.parent else None,
                    "collection": obj.users_collection[0].name if obj.users_collection else None,
                },
            }
        )
    return {
        "schema_version": "1.0",
        "scene_name": scene.name,
        "fps": float(scene.render.fps) / float(scene.render.fps_base),
        "frame_start": int(scene.frame_start),
        "frame_end": int(scene.frame_end),
        "entities": entities,
    }


def object_by_id(entity_id):
    for obj in bpy.context.scene.objects:
        if str(obj.get("facelink_id", "")) == str(entity_id):
            return obj
    raise ValueError(f"FaceLink entity '{entity_id}' no longer exists in this scene")
