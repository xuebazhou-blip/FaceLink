import json

import bpy
from bpy.props import StringProperty
from bpy.types import Operator

from . import overlay
from .bridge import (
    apply_staged_patch,
    discard_staged_patch,
    get_staged_patch,
    stage_patch,
    start_bridge,
    stop_bridge,
)
from .executor import list_revision_history, rollback_to_revision, undo_last_patch
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
    bl_label = "Stage Demo for Selected"
    bl_description = "Prepare a two-key movement without changing the scene"

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
        result = stage_patch(patch)
        context.window_manager.facelink.last_result = json.dumps(
            result["summary"], ensure_ascii=False
        )
        self.report({"INFO"}, "Demo patch staged; review it before applying")
        return {"FINISHED"}


class FACELINK_OT_copy_brief(Operator):
    bl_idname = "facelink.copy_brief"
    bl_label = "Copy Brief"
    bl_description = "Copy the shot brief for use in an MCP client"

    @classmethod
    def poll(cls, context):
        return bool(context.window_manager.facelink.brief.strip())

    def execute(self, context):
        context.window_manager.clipboard = context.window_manager.facelink.brief
        self.report({"INFO"}, "Shot brief copied")
        return {"FINISHED"}


class FACELINK_OT_apply_staged_patch(Operator):
    bl_idname = "facelink.apply_staged_patch"
    bl_label = "Apply Staged Patch"
    bl_description = "Apply the patch currently shown in the review panel"

    @classmethod
    def poll(cls, _context):
        return get_staged_patch()["staged"]

    def execute(self, context):
        result = apply_staged_patch()
        patch_id = result["receipt"]["patch_id"]
        context.window_manager.facelink.last_result = f"Applied {patch_id}"
        self.report({"INFO"}, context.window_manager.facelink.last_result)
        return {"FINISHED"}


class FACELINK_OT_discard_staged_patch(Operator):
    bl_idname = "facelink.discard_staged_patch"
    bl_label = "Discard Staged Patch"
    bl_description = "Discard the reviewed patch without changing the scene"

    @classmethod
    def poll(cls, _context):
        return get_staged_patch()["staged"]

    def execute(self, context):
        result = discard_staged_patch()
        context.window_manager.facelink.last_result = f"Discarded {result['patch_id']}"
        self.report({"INFO"}, context.window_manager.facelink.last_result)
        return {"FINISHED"}


class FACELINK_OT_toggle_preview(Operator):
    bl_idname = "facelink.toggle_preview"
    bl_label = "Toggle Preview Overlay"
    bl_description = "Show or hide staged movement paths and camera frustums"

    @classmethod
    def poll(cls, _context):
        status = overlay.preview_status()
        return get_staged_patch()["staged"] and (
            status["path_count"] > 0 or status["frustum_count"] > 0
        )

    def execute(self, context):
        current = overlay.preview_status()
        status = overlay.set_visible(not current["visible"])
        state = "shown" if status["visible"] else "hidden"
        context.window_manager.facelink.last_result = f"Preview overlay {state}"
        self.report({"INFO"}, context.window_manager.facelink.last_result)
        return {"FINISHED"}


class FACELINK_OT_undo_patch(Operator):
    bl_idname = "facelink.undo_patch"
    bl_label = "Undo Last FaceLink Patch"
    bl_description = "Restore the scene state captured before the latest FaceLink patch"

    @classmethod
    def poll(cls, _context):
        return list_revision_history()["available_count"] > 0

    def execute(self, context):
        result = undo_last_patch()
        context.window_manager.facelink.last_result = f"Undid {result['patch_id']}"
        self.report({"INFO"}, context.window_manager.facelink.last_result)
        return {"FINISHED"}


class FACELINK_OT_rollback_revision(Operator):
    bl_idname = "facelink.rollback_revision"
    bl_label = "Roll Back to Revision"
    bl_description = "Undo this FaceLink revision and every newer FaceLink revision"

    revision_id: StringProperty(name="Revision ID")

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        result = rollback_to_revision(self.revision_id)
        context.window_manager.facelink.last_result = (
            f"Rolled back {result['rolled_back_count']} FaceLink revision(s)"
        )
        self.report({"INFO"}, context.window_manager.facelink.last_result)
        return {"FINISHED"}


CLASSES = (
    FACELINK_OT_start_bridge,
    FACELINK_OT_stop_bridge,
    FACELINK_OT_scan_scene,
    FACELINK_OT_demo_patch,
    FACELINK_OT_copy_brief,
    FACELINK_OT_apply_staged_patch,
    FACELINK_OT_discard_staged_patch,
    FACELINK_OT_toggle_preview,
    FACELINK_OT_undo_patch,
    FACELINK_OT_rollback_revision,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
