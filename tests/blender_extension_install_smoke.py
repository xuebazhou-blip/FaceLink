import importlib
import json
import os
from pathlib import Path

import bpy

module_name = os.environ.get("FACELINK_EXTENSION_MODULE", "bl_ext.user_default.facelink")
module = importlib.import_module(module_name)
assert hasattr(bpy.types, "FACELINK_PT_main")
assert hasattr(bpy.ops.facelink, "start_bridge")
result = {
    "module": module_name,
    "blender_version": bpy.app.version_string,
    "registered": True,
}
report_path = os.environ.get("FACELINK_TEST_REPORT")
if report_path:
    Path(report_path).parent.mkdir(parents=True, exist_ok=True)
    Path(report_path).write_text(json.dumps(result, indent=2), encoding="utf-8")
print("FACELINK_INSTALL_OK=" + json.dumps(result, sort_keys=True))
