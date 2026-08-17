import copy

import bpy
import gpu
from gpu_extras.batch import batch_for_shader

from .camera_preview import camera_preview, object_by_id, world_point

_HANDLER = None
_VISIBLE = False
_GEOMETRY = {"paths": [], "frustums": []}
_DRAW_CALL_COUNT = 0
_LAST_DRAW_ERROR = None


def build_preview_geometry(patch):
    geometry = {"paths": [], "frustums": []}
    for operation in patch.get("operations", []):
        op = operation.get("op")
        payload = operation.get("payload", {})
        if op == "keyframe_transform":
            obj = object_by_id(operation.get("entity_id"))
            points = []
            frames = []
            for frame in sorted(payload.get("frames", []), key=lambda item: int(item["frame"])):
                if "location" not in frame:
                    continue
                point = world_point(obj, frame["location"], payload.get("space", "LOCAL"))
                points.append(list(point))
                frames.append(int(frame["frame"]))
            if len(points) >= 2:
                geometry["paths"].append(
                    {
                        "entity_id": operation.get("entity_id"),
                        "name": obj.name if obj else str(operation.get("entity_id")),
                        "frames": frames,
                        "points": points,
                    }
                )
        elif op == "ensure_camera":
            geometry["frustums"].append(camera_preview(operation))
    return geometry


def _line_coordinates():
    path_lines = []
    for path in _GEOMETRY["paths"]:
        points = path["points"]
        for index in range(len(points) - 1):
            path_lines.extend((points[index], points[index + 1]))
    frustum_lines = []
    for frustum in _GEOMETRY["frustums"]:
        for start, end in frustum["segments"]:
            frustum_lines.extend((start, end))
    return path_lines, frustum_lines


def _draw_lines(coordinates, color):
    if not coordinates:
        return
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    batch = batch_for_shader(shader, "LINES", {"pos": coordinates})
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)


def _draw_preview():
    global _DRAW_CALL_COUNT, _LAST_DRAW_ERROR
    if not _VISIBLE:
        return
    path_lines, frustum_lines = _line_coordinates()
    try:
        gpu.state.blend_set("ALPHA")
        gpu.state.depth_test_set("LESS_EQUAL")
        gpu.state.line_width_set(2.0)
        _draw_lines(path_lines, (0.1, 0.8, 1.0, 0.9))
        _draw_lines(frustum_lines, (1.0, 0.55, 0.1, 0.9))
        _DRAW_CALL_COUNT += 1
        _LAST_DRAW_ERROR = None
    except Exception as exc:
        _LAST_DRAW_ERROR = str(exc)
    finally:
        try:
            gpu.state.line_width_set(1.0)
            gpu.state.depth_test_set("NONE")
            gpu.state.blend_set("NONE")
        except Exception:
            pass


def _request_redraw():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _remove_handler():
    global _HANDLER
    if _HANDLER is None:
        return
    try:
        bpy.types.SpaceView3D.draw_handler_remove(_HANDLER, "WINDOW")
    except (ReferenceError, RuntimeError, ValueError):
        pass
    _HANDLER = None


def _install_handler():
    global _HANDLER
    if _HANDLER is not None or not _VISIBLE:
        return
    try:
        _HANDLER = bpy.types.SpaceView3D.draw_handler_add(
            _draw_preview, (), "WINDOW", "POST_VIEW"
        )
    except (ReferenceError, RuntimeError, TypeError):
        _HANDLER = None


def preview_status():
    path_segments = sum(max(0, len(path["points"]) - 1) for path in _GEOMETRY["paths"])
    frustum_segments = sum(len(item["segments"]) for item in _GEOMETRY["frustums"])
    return {
        "visible": _VISIBLE,
        "draw_handler_active": _HANDLER is not None,
        "path_count": len(_GEOMETRY["paths"]),
        "frustum_count": len(_GEOMETRY["frustums"]),
        "segment_count": path_segments + frustum_segments,
    }


def preview_geometry():
    return copy.deepcopy(_GEOMETRY)


def draw_diagnostics():
    return {"draw_call_count": _DRAW_CALL_COUNT, "last_draw_error": _LAST_DRAW_ERROR}


def set_preview(patch):
    global _DRAW_CALL_COUNT, _GEOMETRY, _LAST_DRAW_ERROR, _VISIBLE
    _remove_handler()
    _GEOMETRY = build_preview_geometry(patch)
    _DRAW_CALL_COUNT = 0
    _LAST_DRAW_ERROR = None
    _VISIBLE = bool(_GEOMETRY["paths"] or _GEOMETRY["frustums"])
    _install_handler()
    _request_redraw()
    return preview_status()


def set_visible(visible):
    global _VISIBLE
    has_geometry = bool(_GEOMETRY["paths"] or _GEOMETRY["frustums"])
    _VISIBLE = bool(visible and has_geometry)
    if _VISIBLE:
        _install_handler()
    else:
        _remove_handler()
    _request_redraw()
    return preview_status()


def clear_preview():
    global _DRAW_CALL_COUNT, _GEOMETRY, _LAST_DRAW_ERROR, _VISIBLE
    _remove_handler()
    _GEOMETRY = {"paths": [], "frustums": []}
    _VISIBLE = False
    _DRAW_CALL_COUNT = 0
    _LAST_DRAW_ERROR = None
    _request_redraw()
    return preview_status()
