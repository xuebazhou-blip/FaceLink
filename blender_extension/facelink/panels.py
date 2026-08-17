import bpy
from bpy.types import Panel

from . import overlay
from .bridge import get_staged_patch, is_running
from .executor import list_revision_history


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

        brief = layout.box()
        brief.label(text="1. Describe the shot", icon="TEXT")
        brief.prop(state, "brief", text="")
        brief.operator("facelink.copy_brief", icon="COPYDOWN")

        scene = layout.box()
        scene.label(text="2. Connect AI", icon="SCENE_DATA")
        scene.operator("facelink.scan_scene", icon="VIEWZOOM")
        scene.operator("facelink.demo_patch", icon="KEY_HLT")
        if state.last_result:
            scene.label(text=state.last_result[:80])

        navigation = layout.box()
        navigation.label(text="Navigation", icon="MOD_CURVE")
        active = context.active_object
        if active is None:
            navigation.label(text="Select an object to set its role")
        else:
            if active.get("facelink_navmesh", False):
                role = "Navmesh"
            elif active.get("facelink_obstacle", False):
                role = "Obstacle"
            else:
                role = "None"
            navigation.label(text=f"Selected: {active.name[:42]} ({role})")
            row = navigation.row(align=True)
            operator = row.operator("facelink.set_navigation_role", text="Navmesh")
            operator.role = "NAVMESH"
            operator = row.operator("facelink.set_navigation_role", text="Obstacle")
            operator.role = "OBSTACLE"
            operator = row.operator("facelink.set_navigation_role", text="Clear")
            operator.role = "NONE"

        review = layout.box()
        review.label(text="3. Review changes", icon="PREVIEW_RANGE")
        staged = get_staged_patch()
        if not staged["staged"]:
            review.label(text="No patch waiting for approval")
        else:
            summary = staged["summary"]
            review.label(text=summary["source_title"][:64])
            if summary.get("scene_guarded"):
                review.label(text="Scene consistency check enabled", icon="LOCKED")
            if summary.get("navigation_guarded"):
                review.label(text="Navigation environment guarded", icon="MOD_DYNAMICPAINT")
            if summary.get("action_guarded"):
                review.label(text="Action consistency check enabled", icon="ACTION")
            review.label(text=f"Operations: {summary['operation_count']}")
            names = ", ".join(item["name"] for item in summary["affected_entities"])
            if names:
                review.label(text=f"Objects: {names}"[:80])
            if summary["frame_start"] is not None:
                review.label(
                    text=f"Frames: {summary['frame_start']} - {summary['frame_end']}"
                )
            composition = summary.get("composition", {})
            if composition.get("evaluated_count"):
                warning_count = composition.get("warning_count", 0)
                review.label(
                    text=(
                        f"Composition: {composition['evaluated_count']} sample(s), "
                        f"{warning_count} warning(s)"
                    ),
                    icon="CAMERA_DATA" if warning_count == 0 else "ERROR",
                )
            if summary.get("retargeted_action_count"):
                review.label(
                    text=(
                        f"Retarget: {summary['retargeted_action_count']} action(s), "
                        "rename-only"
                    ),
                    icon="ARMATURE_DATA",
                )
            for warning in summary["warnings"][:2]:
                review.label(text=warning[:80], icon="ERROR")
            preview = overlay.preview_status()
            if preview["path_count"] or preview["frustum_count"]:
                review.label(
                    text=(
                        f"Preview: {preview['path_count']} path(s), "
                        f"{preview['frustum_count']} camera(s)"
                    ),
                    icon="HIDE_OFF" if preview["visible"] else "HIDE_ON",
                )
                review.operator(
                    "facelink.toggle_preview",
                    text="Hide Overlay" if preview["visible"] else "Show Overlay",
                    icon="HIDE_OFF" if preview["visible"] else "HIDE_ON",
                )
            row = review.row(align=True)
            row.operator("facelink.apply_staged_patch", icon="CHECKMARK")
            row.operator("facelink.discard_staged_patch", icon="TRASH")

        history = layout.box()
        history.label(text="History", icon="RECOVER_LAST")
        history.operator("facelink.undo_patch", icon="LOOP_BACK")
        revision_history = list_revision_history()
        entries = revision_history["entries"][-4:]
        if not entries:
            history.label(text="No FaceLink revisions in this scene")
        for entry in reversed(entries):
            row = history.row(align=True)
            status = entry.get("status", "unknown")
            icon = "CHECKMARK" if status == "applied" else "LOOP_BACK"
            row.label(text=f"{entry.get('source_title', 'Untitled')[:36]}", icon=icon)
            if entry.get("rollback_available"):
                operator = row.operator("facelink.rollback_revision", text="", icon="BACK")
                operator.revision_id = entry["revision_id"]
            elif status == "applied":
                row.label(text="saved log", icon="LOCKED")

        help_box = layout.box()
        help_box.label(text="MCP flow: scan -> preview -> stage")
        help_box.label(text="Nothing changes until Apply above")


CLASSES = (FACELINK_PT_main,)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
