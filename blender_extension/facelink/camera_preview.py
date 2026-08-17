import bpy
from mathutils import Matrix, Vector


def object_by_id(entity_id):
    for obj in bpy.context.scene.objects:
        if str(obj.get("facelink_id", "")) == str(entity_id):
            return obj
    return None


def vector(value):
    if isinstance(value, dict):
        return Vector((float(value["x"]), float(value["y"]), float(value["z"])))
    return Vector(tuple(float(component) for component in value))


def world_point(obj, value, space):
    point = vector(value)
    if space == "WORLD" or obj is None or obj.parent is None:
        return point
    parent_space = obj.parent.matrix_world @ obj.matrix_parent_inverse
    return parent_space @ point


def uses_vertical_sensor(state):
    return state["sensor_fit"] == "VERTICAL" or (
        state["sensor_fit"] == "AUTO" and state["aspect"] < 1.0
    )


def predict_camera_state(operation):
    payload = operation.get("payload", {})
    name = payload.get("name", "FaceLink Camera")
    camera = bpy.data.objects.get(name)
    if camera is not None and camera.type != "CAMERA":
        camera = None
    target = object_by_id(payload.get("target")) if payload.get("target") else None
    space = payload.get("space", "LOCAL")

    if payload.get("location") is not None:
        location = world_point(camera, payload["location"], space)
    elif target is not None:
        target_location = (
            target.matrix_world.translation.copy()
            if space == "WORLD"
            else target.location.copy()
        )
        planned = target_location + Vector(
            (0.0, -float(payload.get("distance", 6.0)), float(payload.get("height", 2.0)))
        )
        location = world_point(camera, planned, space)
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

    scene = bpy.context.scene
    render = scene.render
    aspect = (render.resolution_x * render.pixel_aspect_x) / max(
        1.0, render.resolution_y * render.pixel_aspect_y
    )
    return {
        "name": name,
        "camera": camera,
        "camera_type": camera.data.type if camera else "PERSP",
        "target": target,
        "location": location,
        "rotation": rotation,
        "lens": float(payload.get("lens_mm", camera.data.lens if camera else 50.0)),
        "sensor_width": float(camera.data.sensor_width if camera else 36.0),
        "sensor_height": float(camera.data.sensor_height if camera else 32.0),
        "sensor_fit": camera.data.sensor_fit if camera else "AUTO",
        "shift_x": float(camera.data.shift_x if camera else 0.0),
        "shift_y": float(camera.data.shift_y if camera else 0.0),
        "clip_start": float(camera.data.clip_start if camera else 0.1),
        "clip_end": float(camera.data.clip_end if camera else 1000.0),
        "aspect": aspect,
    }


def _camera_preview_at_current_frame(operation):
    payload = operation.get("payload", {})
    state = predict_camera_state(operation)
    location = state["location"]
    rotation = state["rotation"]
    depth = max(1.0, min(10.0, float(payload.get("distance", 6.0)) * 0.5))
    if uses_vertical_sensor(state):
        half_height = depth * state["sensor_height"] / (2.0 * state["lens"])
        half_width = half_height * state["aspect"]
    else:
        half_width = depth * state["sensor_width"] / (2.0 * state["lens"])
        half_height = half_width / max(state["aspect"], 1e-8)
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
        "name": state["name"],
        "origin": list(location),
        "segments": [[list(start), list(end)] for start, end in segments],
    }


def camera_preview(operation):
    payload = operation.get("payload", {})
    frame = payload.get("frame_start")
    if frame is None:
        return _camera_preview_at_current_frame(operation)
    scene = bpy.context.scene
    original_frame = scene.frame_current
    original_subframe = scene.frame_subframe
    try:
        scene.frame_set(int(frame))
        bpy.context.view_layer.update()
        return _camera_preview_at_current_frame(operation)
    finally:
        scene.frame_set(original_frame, subframe=original_subframe)
        bpy.context.view_layer.update()
