import bpy
from bpy.types import Panel

from . import overlay
from .bridge import get_staged_patch, product_status
from .executor import list_revision_history

CONNECTION_LABELS = {
    "stopped": ("Offline", "UNLINKED"),
    "ready": ("Local bridge ready", "LINKED"),
    "busy": ("FaceLink is working", "TIME"),
    "recovery_required": ("Recovery review required", "ERROR"),
    "error": ("FaceLink needs attention", "ERROR"),
    "incompatible": ("Task data needs a newer FaceLink", "ERROR"),
}
STARTUP_MESSAGES = {
    "unsupported_journal_version": "Update FaceLink, then restart Blender",
    "journal_unavailable": "Check the FaceLink state folder and retry",
    "bridge_initialization_failed": "Check folder and local network permissions",
    "bridge_start_failed": "Restart Blender and retry",
}
TRANSITION_MESSAGES = {
    "file_loaded": "A different Blender file was loaded; start its local bridge",
    "workspace_changed": "The file location changed; restart the bridge for this file",
}
JOB_LABELS = {
    "scan_scene": "Scan scene",
    "stage_patch": "Prepare preview",
    "get_staged_patch": "Read preview",
    "apply_staged_patch": "Apply approved changes",
    "discard_staged_patch": "Discard preview",
    "apply_patch": "Apply changes",
    "undo": "Undo change",
    "list_revisions": "Read history",
    "rollback_revision": "Roll back history",
}
RECOVERY_GUIDANCE = {
    "journal_corrupt": "Previous task data was damaged; rescan and inspect History",
    "interrupted_jobs": "A previous task was interrupted; inspect the scene and rescan",
    "unclean_shutdown": "The previous session did not close cleanly; rescan the scene",
    "unknown": "Review the scene before sending any new work",
}
REFRESH_INTERVAL_SECONDS = 0.5
IDLE_REFRESH_INTERVAL_SECONDS = 2.0
_last_task_center_model = None


def _refresh_task_center():
    global _last_task_center_model

    model = product_status()
    if model == _last_task_center_model:
        return (
            REFRESH_INTERVAL_SECONDS
            if model["connection"]["running"] or model["recovery"]["required"]
            else IDLE_REFRESH_INTERVAL_SECONDS
        )
    _last_task_center_model = model
    window_manager = getattr(bpy.context, "window_manager", None)
    if window_manager is not None:
        for window in window_manager.windows:
            screen = window.screen
            if screen is None:
                continue
            for area in screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
    return (
        REFRESH_INTERVAL_SECONDS
        if model["connection"]["running"] or model["recovery"]["required"]
        else IDLE_REFRESH_INTERVAL_SECONDS
    )


def _draw_connection(layout, model):
    connection = model["connection"]
    label, icon = CONNECTION_LABELS[connection["state"]]
    box = layout.box()
    box.alert = connection["state"] in {"error", "recovery_required", "incompatible"}
    box.label(text="1. Connection", icon=icon)
    box.label(text=label)
    if connection["running"]:
        box.label(
            text=(
                f"{connection.get('instance_id', 'Local Blender')} · "
                f"Protocol {connection.get('protocol_version', 'unknown')}"
            )
        )
    error_code = connection.get("error_code")
    if error_code:
        box.label(text=STARTUP_MESSAGES[error_code])
        if connection.get("requested_version"):
            box.label(text=f"Task data schema: {connection['requested_version']}")
        if connection["state"] == "incompatible":
            box.label(text="The original task data was left unchanged", icon="LOCKED")
        box.label(text=f"Code: {error_code}")
    elif not connection["running"]:
        notice_code = connection.get("notice_code")
        box.label(
            text=TRANSITION_MESSAGES.get(
                notice_code, "Start the bridge to connect this Blender file"
            )
        )
    row = box.row(align=True)
    if connection["running"]:
        row.operator("facelink.stop_bridge", icon="PAUSE")
    else:
        start = row.row(align=True)
        start.enabled = connection["state"] != "incompatible"
        start.operator("facelink.start_bridge", icon="PLAY")
    row.operator("facelink.refresh_status", text="", icon="FILE_REFRESH")


def _draw_recovery(layout, model):
    recovery = model["recovery"]
    if not recovery["required"]:
        return
    box = layout.box()
    box.alert = True
    box.label(text="Previous session needs review", icon="ERROR")
    if recovery["interrupted_job_count"]:
        box.label(text=f"Interrupted tasks: {recovery['interrupted_job_count']}")
    box.label(
        text=RECOVERY_GUIDANCE.get(recovery["reason"], RECOVERY_GUIDANCE["unknown"])
    )
    box.label(text="FaceLink will never replay these tasks automatically", icon="LOCKED")
    row = box.row(align=True)
    row.operator("facelink.scan_scene", text="Rescan Scene", icon="VIEWZOOM")
    row.operator("facelink.acknowledge_recovery", text="I Reviewed", icon="CHECKMARK")


def _draw_tasks(layout, state, model):
    box = layout.box()
    header = box.row(align=True)
    header.prop(
        state,
        "show_tasks",
        text="Tasks",
        icon="TRIA_DOWN" if state.show_tasks else "TRIA_RIGHT",
        emboss=False,
    )
    queue = model["queue"]
    header.label(text=f"{model['active_job_count']} active")
    if not state.show_tasks:
        return
    box.label(text=f"Queue {queue['pending']} / {queue['pending_capacity']}")
    if queue["cancel_requested"]:
        box.label(text=f"Waiting for safe stop: {queue['cancel_requested']}", icon="TIME")
    for job in model["active_jobs"]:
        row = box.row(align=True)
        job_label = JOB_LABELS.get(job["type"], "FaceLink task")
        status = "waiting for safe stop" if job.get("cancel_requested") else job["status"]
        if job.get("queue_position"):
            status = f"queued #{job['queue_position']}"
        checkpoint = job.get("checkpoint")
        if checkpoint and checkpoint not in {"accepted", "terminal"}:
            status = f"{status} · {checkpoint.replace('_', ' ')}"
        row.label(text=f"{job_label} · {status}", icon="TIME")
        if not job.get("cancel_requested"):
            operator = row.operator("facelink.cancel_job", text="", icon="CANCEL")
            operator.job_id = job["job_id"]
    if model["hidden_active_job_count"]:
        box.label(text=f"{model['hidden_active_job_count']} more active task(s)")
    if not model["active_jobs"]:
        box.label(text="No active tasks")
    if queue["running"]:
        box.label(text="Running tasks stop only at safe checkpoints")
    for job in model["recent_jobs"]:
        label = JOB_LABELS.get(job["type"], "FaceLink task")
        if job.get("completed_after_cancel_request"):
            outcome = "completed before cancellation could take effect"
        elif job["status"] == "succeeded":
            outcome = "completed"
        else:
            outcome = f"{job['status']} ({job['error_code']})"
        box.label(
            text=f"{label}: {outcome}"[:96],
            icon="CHECKMARK" if job["status"] == "succeeded" else "ERROR",
        )


class FACELINK_PT_main(Panel):
    bl_label = "FaceLink"
    bl_idname = "FACELINK_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "FaceLink"

    def draw(self, context):
        layout = self.layout
        state = context.window_manager.facelink
        model = product_status(
            startup_error_code=state.last_error_code,
            startup_error_version=state.startup_error_version,
            transition_code=state.transition_code,
        )
        _draw_connection(layout, model)
        _draw_recovery(layout, model)
        _draw_tasks(layout, state, model)

        brief = layout.box()
        brief.label(text="2. Describe the shot", icon="TEXT")
        brief.prop(state, "brief", text="")
        brief.operator("facelink.copy_brief", icon="COPYDOWN")

        scene = layout.box()
        scene.label(text="3. Prepare the scene", icon="SCENE_DATA")
        scene.operator("facelink.scan_scene", icon="VIEWZOOM")
        scene.operator("facelink.demo_patch", icon="KEY_HLT")
        if state.last_result:
            scene.label(text=state.last_result[:80])

        navigation = layout.box()
        navigation.prop(
            state,
            "show_details",
            text="Scene details",
            icon="TRIA_DOWN" if state.show_details else "TRIA_RIGHT",
            emboss=False,
        )
        if state.show_details:
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
        review.label(text="4. Preview and apply", icon="PREVIEW_RANGE")
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
            if summary.get("rig_guarded"):
                review.label(text="Rig consistency check enabled", icon="ARMATURE_DATA")
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
                adapter_labels = {
                    "rename_only": "rename-only",
                    "bake_pose": "pose bake",
                    "bake_evaluated_pose": "evaluated pose bake",
                }
                adapters = ", ".join(
                    sorted(
                        {
                            adapter_labels.get(
                                item.get("adapter"), item.get("adapter", "unknown")
                            )
                            for item in summary.get("retargets", [])
                        }
                    )
                )
                review.label(
                    text=(
                        f"Retarget: {summary['retargeted_action_count']} action(s), "
                        f"{adapters}"
                    ),
                    icon="ARMATURE_DATA",
                )
                object_modes = sorted(
                    {
                        item["object_motion"]
                        for item in summary.get("retargets", [])
                        if item.get("object_motion") is not None
                    }
                )
                if object_modes:
                    review.label(
                        text="Object motion: " + ", ".join(object_modes),
                        icon="ORIENTATION_GLOBAL",
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
        history.label(text="5. History", icon="RECOVER_LAST")
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
        help_box.label(text="Previewed animation changes apply only with Apply above")


CLASSES = (FACELINK_PT_main,)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    if not bpy.app.timers.is_registered(_refresh_task_center):
        bpy.app.timers.register(
            _refresh_task_center,
            first_interval=REFRESH_INTERVAL_SECONDS,
            persistent=True,
        )


def unregister():
    global _last_task_center_model

    if bpy.app.timers.is_registered(_refresh_task_center):
        bpy.app.timers.unregister(_refresh_task_center)
    _last_task_center_model = None
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
