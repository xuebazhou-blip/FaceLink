import importlib
import json
import os
from pathlib import Path

import bpy

module_name = os.environ.get("FACELINK_EXTENSION_MODULE", "bl_ext.user_default.facelink")
module = importlib.import_module(module_name)
panels = importlib.import_module(f"{module_name}.panels")
assert hasattr(bpy.types, "FACELINK_PT_main")
for operator_name in (
    "start_bridge",
    "stop_bridge",
    "refresh_status",
    "cancel_job",
    "acknowledge_recovery",
):
    assert hasattr(bpy.ops.facelink, operator_name), operator_name
assert hasattr(bpy.types.WindowManager, "facelink")
product_state = bpy.context.window_manager.facelink
for property_name in (
    "last_status",
    "last_error_code",
    "startup_error_version",
    "transition_code",
    "last_result",
    "show_tasks",
    "show_details",
):
    assert hasattr(product_state, property_name), property_name
assert product_state.bl_rna.properties["last_status"].is_skip_save is True
assert product_state.bl_rna.properties["transition_code"].is_skip_save is True
assert product_state.bl_rna.properties["show_tasks"].is_skip_save is True
assert bpy.app.timers.is_registered(panels._refresh_task_center)
result = {
    "module": module_name,
    "blender_version": bpy.app.version_string,
    "registered": True,
    "product_controls_registered": True,
    "product_refresh_timer_registered": True,
    "transition_code_registered_skip_save": True,
}
report_path = os.environ.get("FACELINK_TEST_REPORT")
if report_path:
    Path(report_path).parent.mkdir(parents=True, exist_ok=True)
    Path(report_path).write_text(json.dumps(result, indent=2), encoding="utf-8")
print("FACELINK_INSTALL_OK=" + json.dumps(result, sort_keys=True))
