import json

import bpy
from bpy.props import EnumProperty, StringProperty
from bpy.types import Operator

from . import overlay
from .bridge import (
    CancellationPersistenceError,
    acknowledge_recovery,
    apply_staged_patch,
    cancel_job,
    discard_staged_patch,
    get_staged_patch,
    product_status,
    stage_patch,
    start_bridge,
    stop_bridge,
)
from .executor import list_revision_history, rollback_to_revision, undo_last_patch
from .job_journal import JournalError, UnsupportedJournalVersion
from .job_runtime import JobControlError
from .snapshot import ensure_entity_id, scan_scene


def _set_error(context, code, message):
    state = context.window_manager.facelink
    state.last_error_code = code
    state.startup_error_version = ""
    state.transition_code = ""
    state.last_status = message


class FACELINK_OT_start_bridge(Operator):
    bl_idname = "facelink.start_bridge"
    bl_label = "Start Bridge"
    bl_description = "Start the authenticated localhost FaceLink bridge"

    def execute(self, context):
        try:
            info = start_bridge()
        except UnsupportedJournalVersion as exc:
            version = exc.requested or "a newer version"
            message = f"Update FaceLink: existing task data uses schema {version}"
            _set_error(context, "unsupported_journal_version", message)
            context.window_manager.facelink.startup_error_version = exc.requested or ""
            self.report({"ERROR"}, message)
            return {"CANCELLED"}
        except JournalError:
            message = "Task recovery storage is unavailable; check the FaceLink state folder"
            _set_error(context, "journal_unavailable", message)
            self.report({"ERROR"}, message)
            return {"CANCELLED"}
        except OSError:
            message = "Could not start the local bridge; check folder and network permissions"
            _set_error(context, "bridge_initialization_failed", message)
            self.report({"ERROR"}, message)
            return {"CANCELLED"}
        except Exception:
            message = "FaceLink could not start safely; restart Blender and retry"
            _set_error(context, "bridge_start_failed", message)
            self.report({"ERROR"}, message)
            return {"CANCELLED"}
        state = context.window_manager.facelink
        state.last_error_code = ""
        state.startup_error_version = ""
        state.transition_code = ""
        state.last_status = (
            f"Running on 127.0.0.1:{info['port']} ({info['instance_id']})"
        )
        self.report({"INFO"}, "FaceLink bridge started")
        return {"FINISHED"}


class FACELINK_OT_stop_bridge(Operator):
    bl_idname = "facelink.stop_bridge"
    bl_label = "Stop Bridge"

    def invoke(self, context, event):
        if product_status()["active_job_count"]:
            return context.window_manager.invoke_confirm(self, event)
        return self.execute(context)

    def execute(self, context):
        stop_bridge()
        state = context.window_manager.facelink
        state.last_error_code = ""
        state.startup_error_version = ""
        state.transition_code = ""
        state.last_status = "Bridge stopped"
        self.report({"INFO"}, "FaceLink bridge stopped")
        return {"FINISHED"}


class FACELINK_OT_refresh_status(Operator):
    bl_idname = "facelink.refresh_status"
    bl_label = "Refresh FaceLink Status"
    bl_description = "Refresh the local connection, task and recovery status"

    def execute(self, context):
        if context.area is not None:
            context.area.tag_redraw()
        self.report({"INFO"}, "FaceLink status refreshed")
        return {"FINISHED"}


class FACELINK_OT_cancel_job(Operator):
    bl_idname = "facelink.cancel_job"
    bl_label = "Cancel FaceLink Task"
    bl_description = "Request cancellation at the next safe checkpoint"

    job_id: StringProperty(name="Job ID", options={"HIDDEN", "SKIP_SAVE"})

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        try:
            job = cancel_job(self.job_id)
        except JobControlError as exc:
            messages = {
                "job_not_found": "The task is no longer available",
                "job_not_cancellable": "The task has already finished",
                "bridge_stopping": "The FaceLink bridge is not running",
            }
            message = messages.get(exc.code, "The task could not be cancelled safely")
            _set_error(context, exc.code, message)
            self.report({"ERROR"}, message)
            return {"CANCELLED"}
        except CancellationPersistenceError as exc:
            message = (
                "Cancellation was requested but not saved; check the final task result"
                if exc.job_status == "running"
                else "Queued task was removed, but its cancellation record was not saved"
            )
            _set_error(context, "journal_unavailable", message)
            self.report({"ERROR"}, message)
            return {"CANCELLED"}
        except JournalError:
            message = "The cancellation record could not be saved; check the task list"
            _set_error(context, "journal_unavailable", message)
            self.report({"ERROR"}, message)
            return {"CANCELLED"}
        state = context.window_manager.facelink
        state.last_error_code = ""
        if job["status"] == "running":
            state.last_result = "Cancellation recorded; final outcome appears under Tasks"
        else:
            state.last_result = "Queued task cancelled"
        self.report({"INFO"}, state.last_result)
        return {"FINISHED"}


class FACELINK_OT_acknowledge_recovery(Operator):
    bl_idname = "facelink.acknowledge_recovery"
    bl_label = "Confirm Recovery Review"
    bl_description = "Confirm that recovery guidance was reviewed; this never replays a task"

    @classmethod
    def poll(cls, _context):
        return product_status()["recovery"]["required"]

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        try:
            acknowledge_recovery()
        except (JobControlError, JournalError):
            message = "Recovery review could not be saved; keep the scene unchanged and retry"
            _set_error(context, "journal_unavailable", message)
            self.report({"ERROR"}, message)
            return {"CANCELLED"}
        state = context.window_manager.facelink
        state.last_error_code = ""
        state.last_result = "Recovery reviewed; rescan before sending new work"
        self.report({"INFO"}, state.last_result)
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


class FACELINK_OT_set_navigation_role(Operator):
    bl_idname = "facelink.set_navigation_role"
    bl_label = "Set FaceLink Navigation Role"
    bl_description = "Mark the active object as a navigation mesh, obstacle or neither"
    bl_options = {"REGISTER", "UNDO"}

    role: EnumProperty(
        name="Role",
        items=(
            ("NAVMESH", "Navmesh", "Use this mesh as a walkable navigation surface"),
            ("OBSTACLE", "Obstacle", "Include this object in movement collision checks"),
            ("NONE", "None", "Remove FaceLink navigation markers"),
        ),
    )

    @classmethod
    def poll(cls, context):
        return context.active_object is not None

    def execute(self, context):
        obj = context.active_object
        if self.role == "NAVMESH" and obj.type != "MESH":
            self.report({"ERROR"}, "A FaceLink navigation mesh must be a mesh object")
            return {"CANCELLED"}
        for key in ("facelink_navmesh", "facelink_obstacle"):
            if key in obj:
                del obj[key]
        if self.role == "NAVMESH":
            obj["facelink_navmesh"] = True
        elif self.role == "OBSTACLE":
            obj["facelink_obstacle"] = True
        label = self.role.lower() if self.role != "NONE" else "none"
        context.window_manager.facelink.last_result = (
            f"Navigation role for {obj.name}: {label}"
        )
        self.report({"INFO"}, context.window_manager.facelink.last_result)
        return {"FINISHED"}


class FACELINK_OT_apply_staged_patch(Operator):
    bl_idname = "facelink.apply_staged_patch"
    bl_label = "Apply Staged Patch"
    bl_description = "Apply the patch currently shown in the review panel"

    @classmethod
    def poll(cls, _context):
        return (
            get_staged_patch()["staged"]
            and not product_status()["recovery"]["required"]
        )

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
        return (
            list_revision_history()["available_count"] > 0
            and not product_status()["recovery"]["required"]
        )

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

    @classmethod
    def poll(cls, _context):
        return not product_status()["recovery"]["required"]

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
    FACELINK_OT_refresh_status,
    FACELINK_OT_cancel_job,
    FACELINK_OT_acknowledge_recovery,
    FACELINK_OT_scan_scene,
    FACELINK_OT_demo_patch,
    FACELINK_OT_copy_brief,
    FACELINK_OT_set_navigation_role,
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
