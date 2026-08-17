import bpy
from bpy.types import Panel

from .bridge import is_running


class FACELINK_PT_main(Panel):
    bl_label = "FaceLink"
    bl_idname = "FACELINK_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "FaceLink"

    def draw(self, context):
        layout = self.layout
        state = context.window_manager.facelink
        status = layout.box()
        status.label(text="Bridge", icon="LINKED" if is_running() else "UNLINKED")
        status.label(text=state.last_status)
        row = status.row(align=True)
        if is_running():
            row.operator("facelink.stop_bridge", icon="PAUSE")
        else:
            row.operator("facelink.start_bridge", icon="PLAY")

        scene = layout.box()
        scene.label(text="Scene", icon="SCENE_DATA")
        scene.operator("facelink.scan_scene", icon="VIEWZOOM")
        scene.operator("facelink.demo_patch", icon="KEY_HLT")
        if state.last_result:
            scene.label(text=state.last_result[:80])

        help_box = layout.box()
        help_box.label(text="Connect an MCP client, then:")
        help_box.label(text="1. scan_scene")
        help_box.label(text="2. preview_shot")
        help_box.label(text="3. apply_scene_patch")


CLASSES = (FACELINK_PT_main,)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)

