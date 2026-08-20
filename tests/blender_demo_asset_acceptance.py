from __future__ import annotations

import json
import os
from pathlib import Path

import bpy


def main() -> None:
    scene = bpy.context.scene
    actor = bpy.data.objects.get("FaceLink Actor Root")
    target = bpy.data.objects.get("Director Target")
    assert actor is not None, "The editable demo actor is missing"
    assert target is not None, "The demo target is missing"
    assert actor.get("facelink_id"), "The demo actor has no stable FaceLink ID"
    assert scene.frame_start == 1 and scene.frame_end == 96
    assert scene.render.fps == 24

    action = actor.animation_data.action if actor.animation_data else None
    assert action is not None, "The demo actor has no editable Blender Action"
    curves = list(action.fcurves)
    assert {(curve.data_path, curve.array_index) for curve in curves} == {
        ("location", 0),
        ("location", 1),
        ("location", 2),
        ("rotation_euler", 0),
        ("rotation_euler", 1),
        ("rotation_euler", 2),
    }
    keyframe_values = sum(len(curve.keyframe_points) for curve in curves)
    assert keyframe_values == 24
    assert all(
        point.interpolation == "LINEAR"
        for curve in curves
        for point in curve.keyframe_points
    )

    report = {
        "status": "passed",
        "suite": "blender_demo_asset_acceptance",
        "blender_version": bpy.app.version_string,
        "frames": scene.frame_end - scene.frame_start + 1,
        "action": action.name,
        "fcurves": len(curves),
        "keyframe_values": keyframe_values,
        "editable": True,
    }
    report_path = os.environ.get("FACELINK_TEST_REPORT")
    if report_path:
        Path(report_path).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("FACELINK_DEMO_ASSET_ACCEPTANCE_OK")


if __name__ == "__main__":
    main()
