import json

import bpy
from bpy.types import Operator

from .bridge import start_bridge, stop_bridge
from .executor import REVISION_STACK, apply_patch, undo_last_patch
from .snapshot import ensure_entity_id, scan_scene


class FACELINK_OT_start_bridge(Operator):
    bl_idname = "facelink.start_bridge"
    bl_label = "Start Bridge"
    bl_description = "Start the authenticated localhost FaceLink bridge"

    def execute(self, context):
        info = start_bridge()
        context.window_manager.facelink.last_status = (
            f"Running on 127.0.0.1:{info['port']} ({info['instance_id']})"
        )
        self.report({"INFO"}, "FaceLink bridge started")
        return {"FINISHED"}


class FACELINK_OT_stop_bridge(Operator):
    bl_idname = "facelink.stop_bridge"
    bl_label = "Stop Bridge"

    def execute(self, context):
        stop_bridge()
        context.window_manager.facelink.last_status = "Bridge stopped"
        self.report({"INFO"}, "FaceLink bridge stopped")
        return {"FINISHED"}


class FACELINK_OT_scan_scene(Operator):
    bl_idname = "facelink.scan_scene"
    bl_label = "Scan Scene"

    def execute(self, context):
        snapshot = scan_scene()
        context.window_manager.facelink.last_result = (
            f"Found {len(snapshot['entities'])} editable scene entities"
        )
        self.report({"INFO"}, context.window_manager.facelink.last_result)
        return {"FINISHED"}


class FACELINK_OT_demo_patch(Operator):
    bl_idname = "facelink.demo_patch"
    bl_label = "Apply Demo to Selected"
    bl_description = "Add an editable two-key movement to the selected object"

    @classmethod
    def poll(cls, context):
        return context.active_object is not None

    def execute(self, context):
        obj = context.active_object
        entity_id = ensure_entity_id(obj)
        start = context.scene.frame_current
        end = start + context.scene.render.fps * 2
        destination = list(obj.location)
        destination[0] += 2.0
        patch = {
            "schema_version": "1.0",
            "patch_id": "blender-demo",
            "source_title": "Blender panel demo",
            "operations": [
                {
                    "op": "keyframe_transform",
                    "entity_id": entity_id,
                    "payload": {
                        "frames": [
                            {"frame": start, "location": list(obj.location)},
                            {"frame": end, "location": destination},
                        ],
                        "interpolation": "BEZIER",
                    },
                }
            ],
        }
        receipt = apply_patch(patch)
        context.window_manager.facelink.last_result = json.dumps(receipt, ensure_ascii=False)
        self.report({"INFO"}, "Demo keyframes applied; use Ctrl+Z to undo")
        return {"FINISHED"}


class FACELINK_OT_undo_patch(Operator):
    bl_idname = "facelink.undo_patch"
    bl_label = "Undo Last FaceLink Patch"
    bl_description = "Restore the scene state captured before the latest FaceLink patch"

    @classmethod
    def poll(cls, context):
        return bool(REVISION_STACK)

    def execute(self, context):
        result = undo_last_patch()
        context.window_manager.facelink.last_result = f"Undid {result['patch_id']}"
        self.report({"INFO"}, context.window_manager.facelink.last_result)
        return {"FINISHED"}


CLASSES = (
    FACELINK_OT_start_bridge,
    FACELINK_OT_stop_bridge,
    FACELINK_OT_scan_scene,
    FACELINK_OT_demo_patch,
    FACELINK_OT_undo_patch,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
