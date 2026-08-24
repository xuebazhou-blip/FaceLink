import bpy
from bpy.props import BoolProperty, PointerProperty, StringProperty
from bpy.types import PropertyGroup


class FACELINK_PG_state(PropertyGroup):
    last_status: StringProperty(name="Status", default="Bridge stopped", options={"SKIP_SAVE"})
    last_error_code: StringProperty(name="Error code", default="", options={"SKIP_SAVE"})
    startup_error_version: StringProperty(
        name="Unsupported data version", default="", options={"SKIP_SAVE"}
    )
    transition_code: StringProperty(
        name="File transition", default="", options={"SKIP_SAVE"}
    )
    last_result: StringProperty(name="Last result", default="", options={"SKIP_SAVE"})
    show_tasks: BoolProperty(name="Show tasks", default=True, options={"SKIP_SAVE"})
    show_details: BoolProperty(name="Show scene tools", default=False, options={"SKIP_SAVE"})
    brief: StringProperty(
        name="Shot brief",
        default="Cube moves to Marker in 2 seconds, camera follows Cube",
    )


CLASSES = (FACELINK_PG_state,)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.facelink = PointerProperty(type=FACELINK_PG_state)


def unregister():
    if hasattr(bpy.types.WindowManager, "facelink"):
        del bpy.types.WindowManager.facelink
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
