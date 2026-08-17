import copy

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Vector

_HANDLER = None
_VISIBLE = False
_GEOMETRY = {"paths": [], "frustums": []}
_DRAW_CALL_COUNT = 0
_LAST_DRAW_ERROR = None


def _object_by_id(entity_id):
    for obj in bpy.context.scene.objects:
        if str(obj.get("facelink_id", "")) == str(entity_id):
            return obj
    return None


def _vector(value):
    if isinstance(value, dict):
        return Vector((float(value["x"]), float(value["y"]), float(value["z"])))
    return Vector(tuple(float(component) for component in value))


def _world_point(obj, value, space):
    point = _vector(value)
    if space == "WORLD" or obj is None or obj.parent is None:
        return point
    parent_space = obj.parent.matrix_world @ obj.matrix_parent_inverse
    return parent_space @ point


def _camera_preview(operation):
    payload = operation.get("payload", {})
    name = payload.get("name", "FaceLink Camera")
    camera = bpy.data.objects.get(name)
    if camera is not None and camera.type != "CAMERA":
        camera = None
    target = _object_by_id(payload.get("target")) if payload.get("target") else None
    space = payload.get("space", "LOCAL")

    if payload.get("location") is not None:
        location = _world_point(camera, payload["location"], space)
    elif target is not None:
        target_location = (
            target.matrix_world.translation.copy()
            if space == "WORLD"
            else target.location.copy()
        )
        planned = target_location + Vector(
            (0.0, -float(payload.get("distance", 6.0)), float(payload.get("height", 2.0)))
        )
        location = _world_point(camera, planned, space)
    elif camera is not None:
        location = camera.matrix_world.translation.copy()
    else:
        location = Vector((0.0, 0.0, 0.0))

    if target is not None and payload.get("mode") in {"look_at", "follow", "dolly_in"}:
        direction = target.matrix_world.translation - location
        rotation = (
            direction.to_track_quat("-Z", "Y").to_matrix()
            if direction.length > 1e-8
            else Matrix.Identity(3)
        )
    elif camera is not None:
        rotation = camera.matrix_world.to_quaternion().to_matrix()
    else:
        rotation = Matrix.Identity(3)

    lens = float(payload.get("lens_mm", camera.data.lens if camera else 50.0))
    sensor_width = float(camera.data.sensor_width if camera else 36.0)
    sensor_height = float(camera.data.sensor_height if camera else 32.0)
    sensor_fit = camera.data.sensor_fit if camera else "AUTO"
    scene = bpy.context.scene
    render = scene.render
    aspect = (render.resolution_x * render.pixel_aspect_x) / max(
        1.0, render.resolution_y * render.pixel_aspect_y
    )
    depth = max(1.0, min(10.0, float(payload.get("distance", 6.0)) * 0.5))
    if sensor_fit == "VERTICAL":
        half_height = depth * sensor_height / (2.0 * lens)
        half_width = half_height * aspect
    else:
        half_width = depth * sensor_width / (2.0 * lens)
        half_height = half_width / max(aspect, 1e-8)
    local_corners = [
        Vector((-half_width, -half_height, -depth)),
        Vector((half_width, -half_height, -depth)),
        Vector((half_width, half_height, -depth)),
        Vector((-half_width, half_height, -depth)),
    ]
    corners = [location + rotation @ corner for corner in local_corners]
    segments = [(location, corner) for corner in corners]
    segments.extend((corners[index], corners[(index + 1) % 4]) for index in range(4))
    return {
        "name": name,
        "origin": list(location),
        "segments": [[list(start), list(end)] for start, end in segments],
    }


def build_preview_geometry(patch):
    geometry = {"paths": [], "frustums": []}
    for operation in patch.get("operations", []):
        op = operation.get("op")
        payload = operation.get("payload", {})
        if op == "keyframe_transform":
            obj = _object_by_id(operation.get("entity_id"))
            points = []
            frames = []
            for frame in sorted(payload.get("frames", []), key=lambda item: int(item["frame"])):
                if "location" not in frame:
                    continue
                point = _world_point(obj, frame["location"], payload.get("space", "LOCAL"))
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
            geometry["frustums"].append(_camera_preview(operation))
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
