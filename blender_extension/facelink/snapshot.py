import hashlib
import json
import math
import uuid

import bpy
from mathutils import Vector

from .action_inventory import (
    MAX_RIG_BONES,
    MAX_RIGS,
    action_inventories,
)

MAX_NAVIGATION_VERTICES = 20_000
MAX_NAVIGATION_POLYGONS = 20_000
MAX_NAVIGATION_MESHES = 32
MAX_NAVIGATION_OBSTACLES = 2_000


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


def _navigation_role(obj):
    is_navmesh = bool(obj.get("facelink_navmesh", False))
    is_obstacle = bool(obj.get("facelink_obstacle", False))
    if is_navmesh and is_obstacle:
        raise ValueError(f"Object '{obj.name}' cannot be both navmesh and obstacle")
    if is_navmesh:
        if obj.type != "MESH":
            raise ValueError(f"Navigation object '{obj.name}' must be a mesh")
        return "navmesh"
    return "obstacle" if is_obstacle else None


def _navigation_mesh(obj, entity_id):
    mesh = obj.data
    mesh.calc_loop_triangles()
    if len(mesh.vertices) > MAX_NAVIGATION_VERTICES:
        raise ValueError(
            f"Navigation mesh '{obj.name}' exceeds {MAX_NAVIGATION_VERTICES} vertices"
        )
    if len(mesh.loop_triangles) > MAX_NAVIGATION_POLYGONS:
        raise ValueError(
            f"Navigation mesh '{obj.name}' exceeds {MAX_NAVIGATION_POLYGONS} triangles"
        )
    vertices = [_vec3(obj.matrix_world @ vertex.co) for vertex in mesh.vertices]
    polygons = [list(triangle.vertices) for triangle in mesh.loop_triangles]
    edge_owners = {}
    for index, polygon in enumerate(polygons):
        area = 0.0
        for position, vertex_index in enumerate(polygon):
            first = vertices[vertex_index]
            second_index = polygon[(position + 1) % len(polygon)]
            second = vertices[second_index]
            area += first["x"] * second["y"] - second["x"] * first["y"]
            edge = tuple(sorted((vertex_index, second_index)))
            edge_owners[edge] = edge_owners.get(edge, 0) + 1
        if abs(area) <= 1e-9:
            raise ValueError(
                f"Navigation mesh '{obj.name}' has a degenerate XY triangle at index {index}"
            )
    if any(count > 2 for count in edge_owners.values()):
        raise ValueError(f"Navigation mesh '{obj.name}' has a non-manifold edge")
    return {
        "entity_id": entity_id,
        "name": obj.name,
        "vertices": vertices,
        "polygons": polygons,
    }


def _navigation_payload(*, assign_ids):
    navigation_meshes = []
    obstacles = []
    for obj in sorted(bpy.context.scene.objects, key=lambda item: item.name):
        role = _navigation_role(obj)
        if role is None:
            continue
        entity_id = obj.get("facelink_id")
        if not entity_id and assign_ids:
            entity_id = ensure_entity_id(obj)
        entity_id = str(entity_id) if entity_id else f"untracked:{obj.name}"
        if role == "navmesh":
            navigation_meshes.append(_navigation_mesh(obj, entity_id))
        else:
            obstacles.append({"entity_id": entity_id, "bounds": _bounds(obj)})
    if len(navigation_meshes) > MAX_NAVIGATION_MESHES:
        raise ValueError(f"A scene may contain at most {MAX_NAVIGATION_MESHES} navigation meshes")
    if len(obstacles) > MAX_NAVIGATION_OBSTACLES:
        raise ValueError(f"A scene may contain at most {MAX_NAVIGATION_OBSTACLES} obstacles")
    return navigation_meshes, obstacles


def _navigation_fingerprint(navigation_meshes, obstacles):
    canonical = {
        "navigation_meshes": [
            {
                "entity_id": item["entity_id"],
                "vertices": [
                    [_number(vertex[axis]) for axis in "xyz"] for vertex in item["vertices"]
                ],
                "polygons": item["polygons"],
            }
            for item in sorted(navigation_meshes, key=lambda value: value["entity_id"])
        ],
        "obstacles": [
            {
                "entity_id": item["entity_id"],
                "bounds": (
                    {
                        "minimum": [
                            _number(item["bounds"]["minimum"][axis]) for axis in "xyz"
                        ],
                        "maximum": [
                            _number(item["bounds"]["maximum"][axis]) for axis in "xyz"
                        ],
                    }
                    if item["bounds"]
                    else None
                ),
            }
            for item in sorted(obstacles, key=lambda value: value["entity_id"])
        ],
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "nav-" + hashlib.sha256(encoded).hexdigest()[:24]


def navigation_environment_fingerprint():
    navigation_meshes, obstacles = _navigation_payload(assign_ids=False)
    return _navigation_fingerprint(navigation_meshes, obstacles)


def _world_transform(obj):
    location, rotation, scale = obj.matrix_world.decompose()
    rotation_euler = rotation.to_euler("XYZ")
    return {
        "location": _vec3(location),
        "rotation_euler": _vec3(rotation_euler),
        "scale": _vec3(scale),
    }


def _rig_inventories():
    armatures = sorted(
        (obj for obj in bpy.context.scene.objects if obj.type == "ARMATURE"),
        key=lambda item: item.name,
    )
    if len(armatures) > MAX_RIGS:
        raise ValueError(f"A scene may contain at most {MAX_RIGS} armature rigs")
    inventories = []
    for obj in armatures:
        bones = sorted(obj.data.bones, key=lambda item: item.name)
        if len(bones) > MAX_RIG_BONES:
            raise ValueError(f"Rig '{obj.name}' exceeds {MAX_RIG_BONES} bones")
        inventories.append(
            {
                "entity_id": ensure_entity_id(obj),
                "name": obj.name,
                "bones": [
                    {
                        "name": bone.name,
                        "parent": bone.parent.name if bone.parent else None,
                        "use_deform": bool(bone.use_deform),
                        "head": _vec3(bone.head_local),
                        "tail": _vec3(bone.tail_local),
                    }
                    for bone in bones
                ],
            }
        )
    return inventories


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
        navigation_role = _navigation_role(obj)
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
                    "navigation_role": navigation_role,
                },
            }
        )
    navigation_meshes, obstacles = _navigation_payload(assign_ids=True)
    rigs = _rig_inventories()
    actions = action_inventories()
    return {
        "schema_version": "1.3",
        "transform_space": "WORLD",
        "scene_name": scene.name,
        "fps": float(scene.render.fps) / float(scene.render.fps_base),
        "frame_start": int(scene.frame_start),
        "frame_end": int(scene.frame_end),
        "frame_current": float(scene.frame_current) + float(scene.frame_subframe),
        "entities": entities,
        "navigation_meshes": navigation_meshes,
        "navigation_environment_fingerprint": _navigation_fingerprint(
            navigation_meshes, obstacles
        ),
        "rigs": rigs,
        "actions": actions,
    }


def object_by_id(entity_id):
    for obj in bpy.context.scene.objects:
        if str(obj.get("facelink_id", "")) == str(entity_id):
            return obj
    raise ValueError(f"FaceLink entity '{entity_id}' no longer exists in this scene")
