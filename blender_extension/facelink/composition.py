import math

import bpy
from mathutils import Vector

from .camera_preview import predict_camera_state, uses_vertical_sensor

DEFAULT_SETTINGS = {
    "enabled": True,
    "safe_margin": 0.05,
    "min_subject_height": 0.15,
    "max_subject_height": 0.9,
    "max_center_offset": 0.2,
    "check_occlusion": True,
}


def _settings(payload):
    return {**DEFAULT_SETTINGS, **payload.get("composition", {})}


def _bounds_points(obj):
    if obj is None or not getattr(obj, "bound_box", None):
        return []
    points = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
    if not points:
        return []
    minimum = Vector(tuple(min(point[index] for point in points) for index in range(3)))
    maximum = Vector(tuple(max(point[index] for point in points) for index in range(3)))
    if (maximum - minimum).length <= 1e-8:
        return []
    return points


def _project(point, state):
    local = state["rotation"].transposed() @ (point - state["location"])
    depth = -float(local.z)
    if depth <= state["clip_start"] or depth >= state["clip_end"]:
        return None, depth
    if uses_vertical_sensor(state):
        half_height = depth * state["sensor_height"] / (2.0 * state["lens"])
        half_width = half_height * state["aspect"]
    else:
        half_width = depth * state["sensor_width"] / (2.0 * state["lens"])
        half_height = half_width / max(state["aspect"], 1e-8)
    if half_width <= 1e-12 or half_height <= 1e-12:
        return None, depth
    return (
        (0.5 + float(local.x) / (2.0 * half_width), 0.5 + float(local.y) / (2.0 * half_height)),
        depth,
    )


def _belongs_to(hit, target):
    current = hit
    while current is not None:
        if current == target:
            return True
        current = current.parent
    return False


def _center_occluder(state, points):
    target = state["target"]
    center = sum(points, Vector((0.0, 0.0, 0.0))) / len(points)
    direction = center - state["location"]
    distance = direction.length
    if distance <= state["clip_start"]:
        return None
    direction.normalize()
    origin = state["location"] + direction * max(state["clip_start"] * 1.01, 1e-4)
    hit, _, _, _, hit_object, _ = bpy.context.scene.ray_cast(
        bpy.context.evaluated_depsgraph_get(),
        origin,
        direction,
        distance=max(0.0, distance - state["clip_start"]),
    )
    if hit and hit_object is not None and not _belongs_to(hit_object, target):
        return hit_object
    return None


def _warning(code, message):
    return {"code": code, "message": message}


def _set_frame(value):
    whole = math.floor(float(value))
    bpy.context.scene.frame_set(whole, subframe=float(value) - whole)
    bpy.context.view_layer.update()


def _analyze_state(state, settings, sample):
    target = state["target"]
    target_name = target.name if target is not None else "unknown target"
    result = {
        "camera_name": state["name"],
        "target_id": str(target.get("facelink_id", "")) if target is not None else None,
        "target_name": target_name,
        "sample": sample,
        "frame": state.get("sample_frame"),
        "camera_location": [round(float(value), 6) for value in state["location"]],
        "status": "evaluated",
        "metrics": None,
        "warnings": [],
    }
    if state["camera_type"] != "PERSP":
        result["status"] = "unavailable"
        result["warnings"].append(
            _warning(
                "composition_camera_unsupported",
                f"Camera '{state['name']}' uses unsupported "
                f"{state['camera_type']} projection.",
            )
        )
        return result
    if abs(state["shift_x"]) > 1e-8 or abs(state["shift_y"]) > 1e-8:
        result["status"] = "unavailable"
        result["warnings"].append(
            _warning(
                "composition_camera_unsupported",
                f"Camera '{state['name']}' uses unsupported lens shift.",
            )
        )
        return result
    points = _bounds_points(target)
    if not points:
        result["status"] = "unavailable"
        result["warnings"].append(
            _warning(
                "composition_target_unavailable",
                f"Camera '{state['name']}' target '{target_name}' has no usable bounds.",
            )
        )
        return result

    projected = [_project(point, state) for point in points]
    valid = [coordinates for coordinates, _ in projected if coordinates is not None]
    depths = [depth for _, depth in projected]
    if not valid:
        result["metrics"] = {
            "visible_corner_count": 0,
            "total_corner_count": len(points),
            "minimum_depth": round(min(depths), 6),
            "maximum_depth": round(max(depths), 6),
        }
        result["warnings"].append(
            _warning(
                "subject_behind_camera",
                f"Camera '{state['name']}' cannot see target '{target_name}' "
                "in front of its clip planes.",
            )
        )
        return result

    minimum_x = min(point[0] for point in valid)
    maximum_x = max(point[0] for point in valid)
    minimum_y = min(point[1] for point in valid)
    maximum_y = max(point[1] for point in valid)
    width = maximum_x - minimum_x
    height = maximum_y - minimum_y
    center_x = (minimum_x + maximum_x) / 2.0
    center_y = (minimum_y + maximum_y) / 2.0
    center_offset = math.hypot(center_x - 0.5, center_y - 0.5)
    margin = settings["safe_margin"]
    fully_visible = len(valid) == len(points) and (
        minimum_x >= 0.0 and maximum_x <= 1.0 and minimum_y >= 0.0 and maximum_y <= 1.0
    )
    inside_safe_area = fully_visible and (
        minimum_x >= margin
        and maximum_x <= 1.0 - margin
        and minimum_y >= margin
        and maximum_y <= 1.0 - margin
    )
    result["metrics"] = {
        "frame_bounds": [
            round(minimum_x, 6),
            round(minimum_y, 6),
            round(maximum_x, 6),
            round(maximum_y, 6),
        ],
        "center": [round(center_x, 6), round(center_y, 6)],
        "center_offset": round(center_offset, 6),
        "subject_width": round(width, 6),
        "subject_height": round(height, 6),
        "fully_visible": fully_visible,
        "inside_safe_area": inside_safe_area,
        "visible_corner_count": len(valid),
        "total_corner_count": len(points),
        "minimum_depth": round(min(depths), 6),
        "maximum_depth": round(max(depths), 6),
        "center_occluded": False,
    }
    if not fully_visible:
        result["warnings"].append(
            _warning(
                "subject_clipped",
                f"Camera '{state['name']}' clips target '{target_name}' "
                "at the frame edge or clip plane.",
            )
        )
    elif not inside_safe_area:
        result["warnings"].append(
            _warning(
                "subject_outside_safe_area",
                f"Camera '{state['name']}' places target '{target_name}' outside "
                f"the {margin:.0%} safe margin.",
            )
        )
    if height < settings["min_subject_height"]:
        result["warnings"].append(
            _warning(
                "subject_too_small",
                f"Target '{target_name}' occupies only {height:.1%} of frame height.",
            )
        )
    if height > settings["max_subject_height"]:
        result["warnings"].append(
            _warning(
                "subject_too_large",
                f"Target '{target_name}' occupies {height:.1%} of frame height.",
            )
        )
    if center_offset > settings["max_center_offset"]:
        result["warnings"].append(
            _warning(
                "subject_off_center",
                f"Target '{target_name}' is {center_offset:.1%} from the frame center.",
            )
        )
    if settings["check_occlusion"]:
        occluder = _center_occluder(state, points)
        if occluder is not None:
            result["metrics"]["center_occluded"] = True
            result["warnings"].append(
                _warning(
                    "subject_occluded",
                    f"'{occluder.name}' blocks the center of target '{target_name}' "
                    f"from camera '{state['name']}'.",
                )
            )
    return result


def analyze_patch_composition(patch):
    shots = []
    scene = bpy.context.scene
    original_frame = scene.frame_current
    original_subframe = scene.frame_subframe
    try:
        for operation in patch.get("operations", []):
            if operation.get("op") != "ensure_camera":
                continue
            payload = operation.get("payload", {})
            settings = _settings(payload)
            if not settings["enabled"] or not payload.get("target"):
                continue
            start_frame = payload.get(
                "frame_start", patch.get("fingerprint_frame", original_frame)
            )
            _set_frame(start_frame)
            state = predict_camera_state(operation)
            state["sample_frame"] = float(start_frame)
            shots.append(_analyze_state(state, settings, "start"))
            if payload.get("mode") == "dolly_in" and state["target"] is not None:
                end_frame = payload.get("frame_end", start_frame)
                _set_frame(end_frame)
                direction = state["target"].matrix_world.translation - state["location"]
                if direction.length > 0.001:
                    end_state = dict(state)
                    end_state["sample_frame"] = float(end_frame)
                    end_state["location"] = state["location"] + direction.normalized() * min(
                        direction.length * 0.5,
                        float(payload.get("distance", 6.0)) * 0.5,
                    )
                    target_direction = (
                        state["target"].matrix_world.translation - end_state["location"]
                    )
                    end_state["rotation"] = target_direction.to_track_quat(
                        "-Z", "Y"
                    ).to_matrix()
                    shots.append(_analyze_state(end_state, settings, "end"))
    finally:
        scene.frame_set(original_frame, subframe=original_subframe)
        bpy.context.view_layer.update()
    warnings = [warning for shot in shots for warning in shot["warnings"]]
    return {
        "evaluated_count": len(shots),
        "warning_count": len(warnings),
        "shots": shots,
        "warnings": warnings,
    }
