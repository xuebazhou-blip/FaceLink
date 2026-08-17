import bpy
from bpy.props import PointerProperty, StringProperty
from bpy.types import PropertyGroup


class FACELINK_PG_state(PropertyGroup):
    last_status: StringProperty(name="Status", default="Bridge stopped")
    last_result: StringProperty(name="Last result", default="")
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

