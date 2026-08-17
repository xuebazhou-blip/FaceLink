import json
import sys
from pathlib import Path

import bpy

project = Path(__file__).resolve().parents[1]
extension_root = project / "blender_extension"
sys.path.insert(0, str(extension_root))

from facelink.executor import apply_patch  # noqa: E402
from facelink.snapshot import ensure_entity_id, scan_scene  # noqa: E402

import facelink as blender_addon  # noqa: E402

blender_addon.register()
bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
cube = bpy.context.active_object
cube.name = "FaceLink Smoke Cube"
entity_id = ensure_entity_id(cube)
snapshot = scan_scene()
assert any(item["id"] == entity_id for item in snapshot["entities"])

receipt = apply_patch(
    {
        "schema_version": "1.0",
        "patch_id": "smoke",
        "source_title": "Smoke test",
        "operations": [
            {
                "op": "keyframe_transform",
                "entity_id": entity_id,
                "payload": {
                    "frames": [
                        {"frame": 1, "location": [0, 0, 0]},
                        {"frame": 25, "location": [2, 0, 0]},
                    ],
                    "interpolation": "LINEAR",
                },
            },
            {
                "op": "ensure_camera",
                "payload": {
                    "name": "FaceLink Smoke Camera",
                    "mode": "look_at",
                    "target": entity_id,
                    "lens_mm": 50,
                    "distance": 6,
                    "height": 2,
                    "frame_start": 1,
                    "frame_end": 25,
                },
            },
        ],
    }
)
assert receipt["applied_operations"] == 2
assert cube.animation_data and cube.animation_data.action
assert bpy.context.scene.camera.name == "FaceLink Smoke Camera"
print("FACELINK_SMOKE_OK=" + json.dumps(receipt, sort_keys=True))
blender_addon.unregister()
