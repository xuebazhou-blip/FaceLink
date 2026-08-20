from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

import bpy
from mathutils import Vector

PROJECT = Path(__file__).resolve().parents[1]
OUTPUT = Path(os.environ.get("FACELINK_DEMO_OUTPUT", PROJECT / "artifacts" / "demo"))
FRAMES = OUTPUT / "frames"
ASSETS = PROJECT / "docs" / "assets"
sys.path.insert(0, str(PROJECT / "blender_extension"))

from facelink.action_inventory import iter_action_fcurves  # noqa: E402
from facelink.executor import apply_patch  # noqa: E402
from facelink.snapshot import ensure_entity_id  # noqa: E402

import facelink as blender_addon  # noqa: E402


def material(name: str, color: tuple[float, float, float, float], metallic=0.0):
    value = bpy.data.materials.new(name)
    value.diffuse_color = color
    value.use_nodes = True
    principled = value.node_tree.nodes.get("Principled BSDF")
    principled.inputs["Base Color"].default_value = color
    principled.inputs["Roughness"].default_value = 0.55
    principled.inputs["Metallic"].default_value = metallic
    return value


def rounded_cube(name, location, scale, surface, bevel=0.12):
    bpy.ops.mesh.primitive_cube_add(location=location)
    obj = bpy.context.object
    obj.name = name
    obj.scale = scale
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    modifier = obj.modifiers.new("Soft white-model edges", "BEVEL")
    modifier.width = bevel
    modifier.segments = 3
    obj.data.materials.append(surface)
    return obj


def parent_keep_world(child, parent):
    world = child.matrix_world.copy()
    child.parent = parent
    child.matrix_world = world


def look_at(obj, point):
    direction = Vector(point) - obj.location
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def reset_scene():
    if bpy.context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for data in (
        bpy.data.actions,
        bpy.data.cameras,
        bpy.data.curves,
        bpy.data.lights,
        bpy.data.materials,
        bpy.data.meshes,
    ):
        for item in list(data):
            if item.users == 0:
                data.remove(item)


def build_scene():
    reset_scene()
    scene = bpy.context.scene
    scene.frame_start = 1
    scene.frame_end = 96
    scene.render.fps = 24
    scene.render.engine = "BLENDER_EEVEE_NEXT"
    scene.render.resolution_x = 720
    scene.render.resolution_y = 405
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.film_transparent = False
    scene.world.color = (0.025, 0.03, 0.045)
    world_nodes = scene.world.node_tree if scene.world and scene.world.use_nodes else None
    if scene.world:
        scene.world.use_nodes = True
        world_nodes = scene.world.node_tree
    if world_nodes:
        background = world_nodes.nodes.get("Background")
        background.inputs["Color"].default_value = (0.025, 0.03, 0.055, 1.0)
        background.inputs["Strength"].default_value = 0.35

    white = material("White model", (0.72, 0.76, 0.84, 1.0))
    dark = material("Obstacle", (0.16, 0.19, 0.27, 1.0), metallic=0.1)
    blue = material("FaceLink blue", (0.03, 0.38, 1.0, 1.0), metallic=0.15)
    cyan = material("Editable path", (0.02, 0.85, 0.95, 1.0), metallic=0.1)
    orange = material("Target", (1.0, 0.36, 0.04, 1.0), metallic=0.05)

    floor = rounded_cube("Previs Floor", (0, 0, -0.18), (5.8, 4.2, 0.16), white, 0.08)
    floor.data.materials.clear()
    floor.data.materials.append(material("Floor", (0.08, 0.095, 0.14, 1.0)))
    rounded_cube("Obstacle A", (-0.1, -0.7, 0.65), (0.75, 1.2, 0.65), dark)
    rounded_cube("Obstacle B", (1.9, 0.2, 0.45), (0.55, 0.65, 0.45), dark)
    rounded_cube("Set Block", (-3.8, 2.6, 0.35), (0.8, 0.6, 0.35), white)

    actor = bpy.data.objects.new("FaceLink Actor Root", None)
    actor.empty_display_type = "PLAIN_AXES"
    actor.location = (-4.2, -2.4, 0)
    scene.collection.objects.link(actor)
    body = rounded_cube("Actor Body", (-4.2, -2.4, 0.95), (0.48, 0.38, 0.78), blue, 0.22)
    parent_keep_world(body, actor)
    bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=16, location=(-4.2, -2.4, 2.0))
    head = bpy.context.object
    head.name = "Actor Head"
    head.scale = (0.44, 0.44, 0.44)
    head.data.materials.append(white)
    parent_keep_world(head, actor)
    for offset in (-0.25, 0.25):
        leg = rounded_cube(
            "Actor Leg",
            (-4.2 + offset, -2.4, 0.22),
            (0.16, 0.18, 0.3),
            blue,
            0.1,
        )
        parent_keep_world(leg, actor)

    target_location = (3.8, 1.8, 0.06)
    bpy.ops.mesh.primitive_torus_add(
        major_radius=0.52,
        minor_radius=0.09,
        major_segments=48,
        minor_segments=12,
        location=target_location,
    )
    target = bpy.context.object
    target.name = "Director Target"
    target.data.materials.append(orange)

    path_points = [(-4.2, -2.4), (-1.5, -2.4), (-1.5, 1.8), (3.8, 1.8)]
    for start, end in zip(path_points, path_points[1:], strict=True):
        start_v = Vector((start[0], start[1], 0.035))
        end_v = Vector((end[0], end[1], 0.035))
        distance = (end_v - start_v).length
        count = max(2, int(distance / 0.35))
        for index in range(count + 1):
            point = start_v.lerp(end_v, index / count)
            bpy.ops.mesh.primitive_uv_sphere_add(
                segments=12, ring_count=6, radius=0.045, location=point
            )
            bpy.context.object.data.materials.append(cyan)
    for x, y in path_points:
        bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=2, radius=0.13, location=(x, y, 0.08))
        bpy.context.object.data.materials.append(orange)

    camera_data = bpy.data.cameras.new("FaceLink Demo Camera")
    camera = bpy.data.objects.new("FaceLink Demo Camera", camera_data)
    scene.collection.objects.link(camera)
    camera.location = (8.8, -11.2, 10.0)
    camera_data.type = "ORTHO"
    camera_data.ortho_scale = 12.1
    look_at(camera, (0, 0, 0.4))
    scene.camera = camera

    key = bpy.data.lights.new("Key", "AREA")
    key.energy = 1050
    key.shape = "DISK"
    key.size = 5.0
    key_obj = bpy.data.objects.new("Key", key)
    scene.collection.objects.link(key_obj)
    key_obj.location = (-4, -5, 9)
    look_at(key_obj, (0, 0, 0))
    fill = bpy.data.lights.new("Fill", "AREA")
    fill.energy = 750
    fill.color = (0.2, 0.45, 1.0)
    fill.size = 4.0
    fill_obj = bpy.data.objects.new("Fill", fill)
    scene.collection.objects.link(fill_obj)
    fill_obj.location = (5, 4, 6)
    look_at(fill_obj, (0, 0, 0))

    actor_id = ensure_entity_id(actor)
    receipt = apply_patch(
        {
            "schema_version": "1.0",
            "patch_id": "readme-demo",
            "source_title": "Move the actor around the blocks to the orange target",
            "operations": [
                {
                    "op": "set_frame_range",
                    "payload": {"fps": 24, "frame_start": 1, "frame_end": 96},
                },
                {
                    "op": "keyframe_transform",
                    "entity_id": actor_id,
                    "payload": {
                        "frames": [
                            {"frame": 1, "location": [-4.2, -2.4, 0]},
                            {"frame": 13, "location": [-4.2, -2.4, 0]},
                            {"frame": 36, "location": [-1.5, -2.4, 0]},
                            {
                                "frame": 60,
                                "location": [-1.5, 1.8, 0],
                                "rotation_euler": [0, 0, math.pi / 2],
                            },
                            {
                                "frame": 84,
                                "location": [3.8, 1.8, 0],
                                "rotation_euler": [0, 0, 0],
                            },
                            {"frame": 96, "location": [3.8, 1.8, 0]},
                        ],
                        "interpolation": "LINEAR",
                    },
                },
            ],
        }
    )
    scene.frame_set(1)
    action = actor.animation_data.action if actor.animation_data else None
    curves = list(iter_action_fcurves(action)) if action else []
    keyframe_count = sum(len(curve.keyframe_points) for _, curve in curves)
    if receipt["applied_operations"] != 2 or keyframe_count < 12:
        raise RuntimeError("FaceLink demo patch did not create the expected editable keyframes")
    return receipt, keyframe_count


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    FRAMES.mkdir(parents=True, exist_ok=True)
    ASSETS.mkdir(parents=True, exist_ok=True)
    blender_addon.register()
    try:
        receipt, keyframe_count = build_scene()
        scene = bpy.context.scene
        blend_path = ASSETS / "facelink-demo.blend"
        bpy.context.preferences.filepaths.save_version = 0
        bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))
        scene.render.filepath = str(FRAMES / "frame_")
        bpy.ops.render.render(animation=True)
        report = {
            "status": "passed",
            "blender_version": bpy.app.version_string,
            "blend_file": str(blend_path),
            "frames": scene.frame_end - scene.frame_start + 1,
            "keyframe_values": keyframe_count,
            "receipt": receipt,
        }
        (OUTPUT / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print("FACELINK_DEMO_OK=" + json.dumps(report, sort_keys=True))
    finally:
        blender_addon.unregister()


if __name__ == "__main__":
    main()
