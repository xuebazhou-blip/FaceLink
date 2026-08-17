bl_info = {
    "name": "FaceLink",
    "author": "FaceLink Contributors",
    "version": (0, 1, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > FaceLink",
    "description": "Apply safe, editable previs animation patches",
    "category": "Animation",
}

import bpy  # noqa: E402
from bpy.app.handlers import persistent  # noqa: E402

from . import operators, panels, state  # noqa: E402
from .executor import clear_revisions  # noqa: E402


@persistent
def _clear_revisions_after_load(_unused):
    clear_revisions()


def register():
    state.register()
    operators.register()
    panels.register()
    if _clear_revisions_after_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_clear_revisions_after_load)


def unregister():
    from .bridge import stop_bridge

    stop_bridge()
    if _clear_revisions_after_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_clear_revisions_after_load)
    clear_revisions()
    panels.unregister()
    operators.unregister()
    state.unregister()
