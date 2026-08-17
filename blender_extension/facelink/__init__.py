bl_info = {
    "name": "FaceLink",
    "author": "FaceLink Contributors",
    "version": (0, 1, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > FaceLink",
    "description": "Apply safe, editable previs animation patches",
    "category": "Animation",
}

from . import operators, panels, state  # noqa: E402


def register():
    state.register()
    operators.register()
    panels.register()


def unregister():
    from .bridge import stop_bridge

    stop_bridge()
    panels.unregister()
    operators.unregister()
    state.unregister()
