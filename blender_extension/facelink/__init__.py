bl_info = {
    "name": "FaceLink",
    "author": "FaceLink Contributors",
    "version": (0, 3, 3),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > FaceLink",
    "description": "Apply safe, editable previs animation patches",
    "category": "Animation",
}

import bpy  # noqa: E402
from bpy.app.handlers import persistent  # noqa: E402

from . import operators, panels, state  # noqa: E402
from .bridge import clear_staged_patch  # noqa: E402
from .executor import clear_revisions  # noqa: E402


@persistent
def _stop_bridge_before_load(_unused):
    from .bridge import is_running, stop_bridge

    if is_running():
        stop_bridge()


def _set_file_transition_status(code, message):
    window_manager = getattr(bpy.context, "window_manager", None)
    if window_manager is None or not hasattr(window_manager, "facelink"):
        return
    state = window_manager.facelink
    state.last_status = message
    state.last_error_code = ""
    state.startup_error_version = ""
    state.transition_code = code
    state.last_result = ""


@persistent
def _reset_after_load(_unused):
    clear_revisions()
    clear_staged_patch()
    _set_file_transition_status(
        "file_loaded", "File loaded; start FaceLink for this Blender file"
    )


@persistent
def _rebind_after_save(_unused):
    from .bridge import is_running, stop_bridge, workspace_matches_current_file

    if is_running() and not workspace_matches_current_file():
        stop_bridge()
        _set_file_transition_status(
            "workspace_changed",
            "File location changed; restart FaceLink for this file",
        )


def register():
    state.register()
    operators.register()
    panels.register()
    if _stop_bridge_before_load not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(_stop_bridge_before_load)
    if _reset_after_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_reset_after_load)
    if _rebind_after_save not in bpy.app.handlers.save_post:
        bpy.app.handlers.save_post.append(_rebind_after_save)


def unregister():
    from .bridge import stop_bridge

    stop_bridge()
    if _stop_bridge_before_load in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_stop_bridge_before_load)
    if _reset_after_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_reset_after_load)
    if _rebind_after_save in bpy.app.handlers.save_post:
        bpy.app.handlers.save_post.remove(_rebind_after_save)
    clear_revisions()
    clear_staged_patch()
    panels.unregister()
    operators.unregister()
    state.unregister()
