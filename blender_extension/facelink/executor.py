import json
import math
import time
import uuid

import bpy
from mathutils import Euler, Matrix, Vector

from .action_inventory import (
    action_fingerprint,
    action_pose_bones,
    iter_action_fcurves,
    retarget_action_name,
    rewrite_bone_data_path,
)
from .composition import analyze_patch_composition
from .rig_inventory import rig_fingerprint
from .snapshot import (
    ensure_entity_id,
    navigation_environment_fingerprint,
    object_by_id,
    scene_fingerprint,
)

ALLOWED_OPERATIONS = {
    "keyframe_transform",
    "look_at",
    "play_clip",
    "ensure_camera",
    "set_frame_range",
}
INTERPOLATIONS = {"LINEAR", "BEZIER", "CONSTANT"}
REVISION_STACK = []
MAX_REVISIONS = 50
MAX_AUDIT_ENTRIES = 100
AUDIT_LOG_KEY = "facelink_revision_log"


def _assert_editable(obj):
    if obj.hide_select or obj.get("facelink_locked", False):
        raise ValueError(f"FaceLink entity '{ensure_entity_id(obj)}' is locked")


def _action_fcurves(obj):
    animation = obj.animation_data
    action = animation.action if animation else None
    if not action:
        return []
    legacy_curves = getattr(action, "fcurves", None)
    if legacy_curves is not None:
        return list(legacy_curves)
    slot = getattr(animation, "action_slot", None)
    if slot is None:
        return []
    curves = []
    for layer in action.layers:
        for strip in layer.strips:
            channelbag = getattr(strip, "channelbag", None)
            if channelbag is None:
                continue
            bag = channelbag(slot)
            if bag is not None:
                curves.extend(bag.fcurves)
    return curves


def _validate_vector(value, field):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{field} must contain exactly three numbers")
    for component in value:
        if not math.isfinite(float(component)):
            raise ValueError(f"{field} values must be finite numbers")


def _validate_xyz(value, field):
    if not isinstance(value, dict) or not all(axis in value for axis in "xyz"):
        raise ValueError(f"{field} must contain x, y and z numbers")
    if not all(math.isfinite(float(value[axis])) for axis in "xyz"):
        raise ValueError(f"{field} values must be finite numbers")


def _validate_composition(value):
    if not isinstance(value, dict):
        raise ValueError("Camera composition must be an object")
    allowed = {
        "enabled",
        "safe_margin",
        "min_subject_height",
        "max_subject_height",
        "max_center_offset",
        "check_occlusion",
    }
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"Camera composition contains unsupported fields: {sorted(unknown)}")
    for field in ("enabled", "check_occlusion"):
        if field in value and not isinstance(value[field], bool):
            raise ValueError(f"Camera composition {field} must be a boolean")
    ranges = {
        "safe_margin": (0.0, 0.45),
        "min_subject_height": (0.0, 1.0),
        "max_subject_height": (0.0, 1.0),
        "max_center_offset": (0.0, 0.75),
    }
    for field, (minimum, maximum) in ranges.items():
        if field not in value:
            continue
        if isinstance(value[field], bool) or not isinstance(value[field], (int, float)):
            raise ValueError(f"Camera composition {field} must be a number")
        number = float(value[field])
        if not math.isfinite(number) or number < minimum or number > maximum:
            raise ValueError(
                f"Camera composition {field} must be between {minimum:g} and {maximum:g}"
            )
    minimum_height = float(value.get("min_subject_height", 0.15))
    maximum_height = float(value.get("max_subject_height", 0.9))
    if minimum_height >= maximum_height:
        raise ValueError(
            "Camera composition min_subject_height must be less than max_subject_height"
        )


def _resolved_retarget_map(obj, action, value):
    if not isinstance(value, dict):
        raise ValueError("play_clip retarget must be an object")
    allowed = {"adapter", "bone_map", "strict", "source_rig"}
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"play_clip retarget contains unsupported fields: {sorted(unknown)}")
    if value.get("adapter", "rename_only") != "rename_only":
        raise ValueError("Only the rename_only retarget adapter is supported")
    strict = value.get("strict", True)
    if not isinstance(strict, bool):
        raise ValueError("play_clip retarget strict must be a boolean")
    bone_map = value.get("bone_map")
    if not isinstance(bone_map, dict) or not 1 <= len(bone_map) <= 512:
        raise ValueError("play_clip retarget bone_map must contain 1 to 512 mappings")
    if not all(
        isinstance(source, str)
        and isinstance(target, str)
        and source.strip()
        and target.strip()
        for source, target in bone_map.items()
    ):
        raise ValueError("play_clip retarget bone names must be non-empty strings")
    if len(set(bone_map.values())) != len(bone_map):
        raise ValueError("play_clip retarget target bones must be unique")
    if obj.type != "ARMATURE":
        raise ValueError("play_clip retarget requires an armature target")
    action_bones = action_pose_bones(action)
    source_rig_id = value.get("source_rig")
    if source_rig_id is not None:
        if not isinstance(source_rig_id, str) or not source_rig_id:
            raise ValueError("play_clip retarget source_rig must be a non-empty entity ID")
        source_rig = object_by_id(source_rig_id)
        if source_rig.type != "ARMATURE":
            raise ValueError("play_clip retarget source_rig must identify an armature")
        source_bones = {bone.name for bone in source_rig.data.bones}
        missing_source_channels = sorted(action_bones - source_bones)
        if missing_source_channels:
            raise ValueError(
                f"Source armature '{source_rig.name}' is missing action bone(s): "
                + ", ".join(missing_source_channels)
            )
    missing_sources = sorted(action_bones - set(bone_map)) if strict else []
    if missing_sources:
        raise ValueError(
            "Strict retarget mapping does not cover action bone(s): "
            + ", ".join(missing_sources)
        )
    resolved = {
        source: bone_map.get(source, source) for source in sorted(action_bones)
    }
    target_bones = {bone.name for bone in obj.data.bones}
    missing_targets = sorted(set(resolved.values()) - target_bones)
    if missing_targets:
        raise ValueError(
            f"Target armature '{obj.name}' is missing bone(s): "
            + ", ".join(missing_targets)
        )
    owners = {}
    for source, target in resolved.items():
        owners.setdefault(target, []).append(source)
    collisions = sorted(target for target, sources in owners.items() if len(sources) > 1)
    if collisions:
        raise ValueError(
            "Multiple source bones resolve to target bone(s): " + ", ".join(collisions)
        )
    return resolved


def _preflight_operations(operations):
    for operation in operations:
        if not isinstance(operation, dict):
            raise ValueError("Every patch operation must be an object")
        op = operation["op"]
        payload = operation.get("payload", {})
        if not isinstance(payload, dict):
            raise ValueError(f"{op} payload must be an object")
        if op == "set_frame_range":
            fps = float(payload["fps"])
            frame_start = int(payload["frame_start"])
            frame_end = int(payload["frame_end"])
            if not math.isfinite(fps) or fps <= 0 or frame_end < frame_start:
                raise ValueError("Invalid frame range or FPS")
        elif op == "keyframe_transform":
            obj = object_by_id(operation.get("entity_id"))
            _assert_editable(obj)
            frames = payload.get("frames")
            if not isinstance(frames, list) or not frames:
                raise ValueError("keyframe_transform requires at least one frame")
            if payload.get("space", "LOCAL") not in {"LOCAL", "WORLD"}:
                raise ValueError("Transform space must be LOCAL or WORLD")
            if payload.get("interpolation", "BEZIER") not in INTERPOLATIONS:
                raise ValueError("Unsupported keyframe interpolation")
            seen_values = {}
            for item in frames:
                frame = int(item["frame"])
                fields = [
                    field for field in ("location", "rotation_euler", "scale") if field in item
                ]
                if not fields:
                    raise ValueError("A transform keyframe must change a transform field")
                for field in fields:
                    _validate_vector(item[field], field)
                    value = tuple(float(component) for component in item[field])
                    key = (frame, field)
                    if key in seen_values and seen_values[key] != value:
                        raise ValueError(
                            f"keyframe_transform has conflicting {field} values at frame {frame}"
                        )
                    seen_values[key] = value
        elif op == "look_at":
            obj = object_by_id(operation.get("entity_id"))
            _assert_editable(obj)
            object_by_id(payload["target_id"])
        elif op == "play_clip":
            obj = object_by_id(operation.get("entity_id"))
            _assert_editable(obj)
            action = bpy.data.actions.get(payload["clip"])
            if action is None:
                raise ValueError(f"Animation action '{payload['clip']}' was not found")
            if int(payload["frame_end"]) < int(payload["frame_start"]):
                raise ValueError("play_clip frame_end must not precede frame_start")
            if payload.get("retarget") is not None:
                _resolved_retarget_map(obj, action, payload["retarget"])
        elif op == "ensure_camera":
            name = payload.get("name", "FaceLink Camera")
            mode = payload.get("mode", "static")
            if mode not in {"static", "look_at", "follow", "dolly_in"}:
                raise ValueError(f"Unsupported camera mode '{mode}'")
            if mode != "static" and not payload.get("target"):
                raise ValueError(f"Camera mode '{mode}' requires a target")
            existing = bpy.data.objects.get(name)
            if existing is not None:
                if existing.type != "CAMERA":
                    raise ValueError(f"Object '{name}' exists but is not a camera")
                _assert_editable(existing)
            if payload.get("target"):
                object_by_id(payload["target"])
            lens = float(payload.get("lens_mm", 50.0))
            if not math.isfinite(lens) or lens < 1.0 or lens > 300.0:
                raise ValueError("Camera lens must be between 1 and 300 mm")
            distance = float(payload.get("distance", 6.0))
            height = float(payload.get("height", 2.0))
            if not math.isfinite(distance) or distance <= 0:
                raise ValueError("Camera distance must be a positive finite number")
            if not math.isfinite(height):
                raise ValueError("Camera height must be a finite number")
            if payload.get("location") is not None:
                _validate_xyz(payload["location"], "Camera location")
            if payload.get("space", "LOCAL") not in {"LOCAL", "WORLD"}:
                raise ValueError("Camera space must be LOCAL or WORLD")
            if "composition" in payload:
                _validate_composition(payload["composition"])


def _timeline_records(operations):
    records = []
    channel_names = {
        "location": "location",
        "rotation_euler": "rotation",
        "scale": "scale",
    }
    for operation_index, operation in enumerate(operations):
        op = operation["op"]
        payload = operation.get("payload", {})
        if op == "keyframe_transform":
            for field, channel in channel_names.items():
                samples = {
                    int(item["frame"]): tuple(float(value) for value in item[field])
                    for item in payload["frames"]
                    if field in item
                }
                if samples:
                    records.append(
                        {
                            "key": (str(operation.get("entity_id")), channel),
                            "label": f"entity '{operation.get('entity_id')}' {channel}",
                            "start": min(samples),
                            "end": max(samples),
                            "samples": samples,
                            "operation_index": operation_index,
                        }
                    )
        elif op == "play_clip":
            records.append(
                {
                    "key": (str(operation.get("entity_id")), "action"),
                    "label": f"entity '{operation.get('entity_id')}' action",
                    "start": int(payload["frame_start"]),
                    "end": int(payload["frame_end"]),
                    "samples": {},
                    "operation_index": operation_index,
                }
            )
        elif op == "ensure_camera" and payload.get("mode") == "dolly_in":
            name = payload.get("name", "FaceLink Camera")
            records.append(
                {
                    "key": (f"camera:{name}", "location"),
                    "label": f"camera '{name}' location",
                    "start": int(payload["frame_start"]),
                    "end": int(payload["frame_end"]),
                    "samples": {},
                    "operation_index": operation_index,
                }
            )
    return records


def _internal_timeline_conflicts(operations):
    conflicts = []
    occupied = {}
    for record in _timeline_records(operations):
        for previous in occupied.get(record["key"], []):
            overlap_start = max(record["start"], previous["start"])
            overlap_end = min(record["end"], previous["end"])
            overlaps = overlap_start < overlap_end
            if overlap_start == overlap_end:
                current_value = record["samples"].get(overlap_start)
                previous_value = previous["samples"].get(overlap_start)
                point_interval = (
                    record["start"] == record["end"]
                    or previous["start"] == previous["end"]
                )
                overlaps = (
                    current_value is not None
                    and previous_value is not None
                    and current_value != previous_value
                ) or (point_interval and (current_value is None or previous_value is None))
            if overlaps:
                conflicts.append(
                    f"operations {previous['operation_index']} and "
                    f"{record['operation_index']} overlap on {record['label']} "
                    f"during frames {overlap_start}-{overlap_end}"
                )
        occupied.setdefault(record["key"], []).append(record)
    return conflicts


def _preflight_nla_overlaps(operations):
    for operation in operations:
        if operation["op"] != "play_clip":
            continue
        payload = operation["payload"]
        obj = object_by_id(operation.get("entity_id"))
        animation = obj.animation_data
        track = animation.nla_tracks.get("FaceLink") if animation else None
        if track is None:
            continue
        start = int(payload["frame_start"])
        end = int(payload["frame_end"])
        action_name = payload["clip"]
        if payload.get("retarget") is not None:
            source = bpy.data.actions[payload["clip"]]
            resolved = _resolved_retarget_map(obj, source, payload["retarget"])
            action_name = retarget_action_name(
                source.name,
                obj.name,
                resolved,
                action_fingerprint(source),
            )
        replacement_name = f"FaceLink {action_name} {payload['frame_start']}"
        for strip in track.strips:
            if strip.name == replacement_name:
                continue
            if start < float(strip.frame_end) and end > float(strip.frame_start):
                raise ValueError(
                    f"play_clip frames {start}-{end} overlap existing FaceLink NLA strip "
                    f"'{strip.name}' on '{obj.name}'"
                )


def _existing_animation_warnings(operations):
    warnings = []
    data_paths = {
        "location": "location",
        "rotation": "rotation_euler",
        "scale": "scale",
    }
    for record in _timeline_records(operations):
        entity_key, channel = record["key"]
        if entity_key.startswith("camera:"):
            obj = bpy.data.objects.get(entity_key.split(":", 1)[1])
        elif channel != "action":
            try:
                obj = object_by_id(entity_key)
            except ValueError:
                obj = None
        else:
            obj = None
        if obj is None or channel not in data_paths:
            continue
        curves = [
            curve for curve in _action_fcurves(obj) if curve.data_path == data_paths[channel]
        ]
        if any(
            record["start"] <= float(point.co.x) <= record["end"]
            for curve in curves
            for point in curve.keyframe_points
        ):
            warnings.append(
                f"Existing keyframes overlap '{obj.name}' {channel} during frames "
                f"{record['start']}-{record['end']}."
            )
    return list(dict.fromkeys(warnings))


def validate_patch(patch):
    """Validate a patch against the current scene without changing scene content."""
    if not isinstance(patch, dict):
        raise ValueError("Patch must be an object")
    if patch.get("schema_version") not in {"1.0", "1.1", "1.2", "1.3", "1.4"}:
        raise ValueError("Unsupported patch schema_version")
    operations = patch.get("operations")
    if not isinstance(operations, list):
        raise ValueError("Patch operations must be a list")
    if not all(isinstance(item, dict) for item in operations):
        raise ValueError("Every patch operation must be an object")
    if not all(isinstance(item.get("op"), str) for item in operations):
        raise ValueError("Every patch operation must have a string op")
    for field in ("patch_id", "source_title"):
        if field in patch and not isinstance(patch[field], str):
            raise ValueError(f"Patch {field} must be a string")
    warnings = patch.get("warnings", [])
    if not isinstance(warnings, list) or not all(isinstance(item, str) for item in warnings):
        raise ValueError("Patch warnings must be a list of strings")
    expected_fingerprint = patch.get("scene_fingerprint")
    fingerprint_entities = patch.get("fingerprint_entities", [])
    fingerprint_frame = patch.get("fingerprint_frame")
    if expected_fingerprint is not None:
        if not isinstance(expected_fingerprint, str) or not expected_fingerprint.startswith(
            "scene-"
        ):
            raise ValueError("Patch scene_fingerprint is invalid")
        if not isinstance(fingerprint_entities, list) or not all(
            isinstance(item, str) for item in fingerprint_entities
        ):
            raise ValueError("Patch fingerprint_entities must be a list of strings")
        if fingerprint_frame is None:
            raise ValueError("A guarded patch requires fingerprint_frame")
        actual_fingerprint = scene_fingerprint(fingerprint_entities, fingerprint_frame)
        if actual_fingerprint != expected_fingerprint:
            raise ValueError(
                "The Blender scene changed after this patch was planned; scan and plan again"
            )
    elif fingerprint_entities or fingerprint_frame is not None:
        raise ValueError("Patch fingerprint metadata is incomplete")
    expected_navigation = patch.get("navigation_environment_fingerprint")
    if expected_navigation is not None:
        if not isinstance(expected_navigation, str) or not expected_navigation.startswith(
            "nav-"
        ):
            raise ValueError("Patch navigation_environment_fingerprint is invalid")
        if navigation_environment_fingerprint() != expected_navigation:
            raise ValueError(
                "The Blender navigation environment changed after this patch was planned; "
                "scan and plan again"
            )
    expected_actions = patch.get("action_fingerprints", {})
    if not isinstance(expected_actions, dict) or not all(
        isinstance(name, str)
        and name
        and isinstance(fingerprint, str)
        and fingerprint.startswith("action-")
        and len(fingerprint) == 31
        and all(character in "0123456789abcdef" for character in fingerprint[7:])
        for name, fingerprint in expected_actions.items()
    ):
        raise ValueError("Patch action_fingerprints must map names to valid fingerprints")
    clip_names = {
        item.get("payload", {}).get("clip")
        for item in operations
        if item.get("op") == "play_clip"
    }
    if patch.get("schema_version") in {"1.3", "1.4"} and set(expected_actions) != clip_names:
        raise ValueError("Scene Patch 1.3+ requires one action fingerprint per play_clip")
    for name, expected in expected_actions.items():
        action = bpy.data.actions.get(name)
        if action is None:
            raise ValueError(f"Animation action '{name}' no longer exists")
        if action_fingerprint(action) != expected:
            raise ValueError(
                f"Animation action '{name}' changed after this patch was planned; scan and "
                "plan again"
            )
    expected_rigs = patch.get("rig_fingerprints", {})
    if not isinstance(expected_rigs, dict) or not all(
        isinstance(entity_id, str)
        and entity_id
        and isinstance(fingerprint, str)
        and fingerprint.startswith("rig-")
        and len(fingerprint) == 28
        and all(character in "0123456789abcdef" for character in fingerprint[4:])
        for entity_id, fingerprint in expected_rigs.items()
    ):
        raise ValueError("Patch rig_fingerprints must map entity IDs to valid fingerprints")
    required_rigs = set()
    for item in operations:
        if item.get("op") != "play_clip":
            continue
        payload = item.get("payload", {})
        action = bpy.data.actions.get(payload.get("clip"))
        target = object_by_id(item.get("entity_id"))
        if action is not None and action_pose_bones(action) and target.type == "ARMATURE":
            required_rigs.add(str(item.get("entity_id")))
        source_rig = (payload.get("retarget") or {}).get("source_rig")
        if source_rig is not None:
            required_rigs.add(str(source_rig))
    if patch.get("schema_version") == "1.4" and set(expected_rigs) != required_rigs:
        raise ValueError("Scene Patch 1.4 requires one rig fingerprint per referenced armature")
    for entity_id, expected in expected_rigs.items():
        obj = object_by_id(entity_id)
        if obj.type != "ARMATURE":
            raise ValueError(f"Rig fingerprint entity '{entity_id}' is not an armature")
        if rig_fingerprint(obj) != expected:
            raise ValueError(
                f"Armature '{obj.name}' changed after this patch was planned; scan and plan again"
            )
    unknown = {item.get("op") for item in operations} - ALLOWED_OPERATIONS
    if unknown:
        raise ValueError(f"Patch contains unsupported operations: {sorted(unknown)}")
    _preflight_operations(operations)
    conflicts = _internal_timeline_conflicts(operations)
    if conflicts:
        raise ValueError("Patch timeline conflicts: " + "; ".join(conflicts))
    _preflight_nla_overlaps(operations)
    return operations


def summarize_patch(patch):
    """Return a compact artist-facing description after a read-only preflight."""
    operations = validate_patch(patch)
    affected = {}
    operation_types = {}
    frames = []
    retargets = []

    for operation in operations:
        op = operation["op"]
        payload = operation.get("payload", {})
        operation_types[op] = operation_types.get(op, 0) + 1

        entity_id = operation.get("entity_id")
        if entity_id:
            obj = object_by_id(entity_id)
            affected[obj.name] = {
                "id": entity_id,
                "name": obj.name,
                "type": obj.type,
                "will_create": False,
            }

        if op == "set_frame_range":
            frames.extend((int(payload["frame_start"]), int(payload["frame_end"])))
        elif op == "keyframe_transform":
            frames.extend(int(item["frame"]) for item in payload["frames"])
        elif op == "play_clip":
            frames.extend((int(payload["frame_start"]), int(payload["frame_end"])))
            if payload.get("retarget") is not None:
                obj = object_by_id(operation.get("entity_id"))
                action = bpy.data.actions[payload["clip"]]
                resolved = _resolved_retarget_map(obj, action, payload["retarget"])
                retargets.append(
                    {
                        "source_action": action.name,
                        "target_rig_id": operation.get("entity_id"),
                        "target_rig_name": obj.name,
                        "adapter": "rename_only",
                        "strict": payload["retarget"].get("strict", True),
                        "mapped_bone_count": len(resolved),
                        "output_action": retarget_action_name(
                            action.name,
                            obj.name,
                            resolved,
                            action_fingerprint(action),
                        ),
                    }
                )
        elif op == "ensure_camera":
            name = payload.get("name", "FaceLink Camera")
            camera = bpy.data.objects.get(name)
            affected[name] = {
                "id": camera.get("facelink_id") if camera else None,
                "name": name,
                "type": "CAMERA",
                "will_create": camera is None,
            }
            if payload.get("mode") == "dolly_in":
                frames.extend((int(payload["frame_start"]), int(payload["frame_end"])))

    timeline_warnings = _existing_animation_warnings(operations)
    composition = analyze_patch_composition(patch)
    composition_warnings = [item["message"] for item in composition["warnings"]]
    warnings = list(
        dict.fromkeys(
            [*patch.get("warnings", []), *timeline_warnings, *composition_warnings]
        )
    )
    return {
        "patch_id": patch.get("patch_id", "unknown"),
        "source_title": patch.get("source_title", "Untitled patch"),
        "operation_count": len(operations),
        "operation_types": dict(sorted(operation_types.items())),
        "affected_entities": [affected[name] for name in sorted(affected)],
        "frame_start": min(frames) if frames else None,
        "frame_end": max(frames) if frames else None,
        "warnings": [str(item) for item in warnings],
        "timeline_warning_count": len(timeline_warnings),
        "composition": composition,
        "composition_warning_count": len(composition_warnings),
        "retargets": retargets,
        "retargeted_action_count": len(retargets),
        "scene_guarded": patch.get("scene_fingerprint") is not None,
        "navigation_guarded": patch.get("navigation_environment_fingerprint") is not None,
        "action_guarded": bool(patch.get("action_fingerprints")),
        "rig_guarded": bool(patch.get("rig_fingerprints")),
    }


def _capture_constraints(obj):
    states = []
    for constraint in obj.constraints:
        if not constraint.name.startswith("FaceLink"):
            continue
        state = {
            "name": constraint.name,
            "type": constraint.type,
            "target": constraint.target.name if getattr(constraint, "target", None) else None,
            "influence": float(constraint.influence),
        }
        for field in ("track_axis", "up_axis", "use_offset"):
            if hasattr(constraint, field):
                state[field] = getattr(constraint, field)
        states.append(state)
    return states


def _capture_nla(obj):
    animation = obj.animation_data
    track = animation.nla_tracks.get("FaceLink") if animation else None
    if track is None:
        return None
    return [
        {
            "name": strip.name,
            "frame_start": float(strip.frame_start),
            "action": strip.action,
            "scale": float(strip.scale),
            "repeat": float(strip.repeat),
        }
        for strip in track.strips
    ]


def _capture_object(obj):
    animation = obj.animation_data
    return {
        "name": obj.name,
        "location": list(obj.location),
        "rotation_euler": list(obj.rotation_euler),
        "rotation_mode": obj.rotation_mode,
        "scale": list(obj.scale),
        "camera_lens": float(obj.data.lens) if obj.type == "CAMERA" else None,
        "had_animation_data": animation is not None,
        "action": animation.action if animation else None,
        "nla": _capture_nla(obj),
        "constraints": _capture_constraints(obj),
    }


def _capture_revision(patch, operations):
    scene = bpy.context.scene
    affected = {}
    new_camera_names = []
    new_action_names = []
    for operation in operations:
        entity_id = operation.get("entity_id")
        if entity_id:
            obj = object_by_id(entity_id)
            affected.setdefault(obj.name, _capture_object(obj))
        if operation["op"] == "ensure_camera":
            name = operation.get("payload", {}).get("name", "FaceLink Camera")
            camera = bpy.data.objects.get(name)
            if camera is None:
                new_camera_names.append(name)
            else:
                affected.setdefault(camera.name, _capture_object(camera))
        if operation["op"] == "play_clip" and operation.get("payload", {}).get(
            "retarget"
        ) is not None:
            obj = object_by_id(operation.get("entity_id"))
            payload = operation["payload"]
            source = bpy.data.actions[payload["clip"]]
            resolved = _resolved_retarget_map(obj, source, payload["retarget"])
            action_name = retarget_action_name(
                source.name,
                obj.name,
                resolved,
                action_fingerprint(source),
            )
            if bpy.data.actions.get(action_name) is None:
                new_action_names.append(action_name)
    return {
        "revision_id": "rev-" + uuid.uuid4().hex[:16],
        "patch_id": patch.get("patch_id", "unknown"),
        "source_title": patch.get("source_title", "Untitled patch"),
        "applied_at": time.time(),
        "scene_pointer": scene.as_pointer(),
        "had_audit_log": AUDIT_LOG_KEY in scene,
        "audit_log_raw": scene.get(AUDIT_LOG_KEY),
        "scene": {
            "fps": scene.render.fps,
            "fps_base": scene.render.fps_base,
            "frame_start": scene.frame_start,
            "frame_end": scene.frame_end,
            "camera": scene.camera.name if scene.camera else None,
        },
        "objects": affected,
        "new_camera_names": new_camera_names,
        "new_action_names": new_action_names,
    }


def _read_audit_log(scene=None):
    scene = scene or bpy.context.scene
    raw = scene.get(AUDIT_LOG_KEY, "[]")
    if not isinstance(raw, str):
        return []
    try:
        entries = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def _write_audit_log(entries, scene=None):
    scene = scene or bpy.context.scene
    scene[AUDIT_LOG_KEY] = json.dumps(
        entries[-MAX_AUDIT_ENTRIES:], ensure_ascii=False, separators=(",", ":")
    )


def clear_revision_history():
    scene = bpy.context.scene
    if AUDIT_LOG_KEY in scene:
        del scene[AUDIT_LOG_KEY]


def _record_revision(revision, operations, warnings):
    operation_types = {}
    for operation in operations:
        op = operation["op"]
        operation_types[op] = operation_types.get(op, 0) + 1
    affected_objects = sorted(set(revision["objects"]) | set(revision["new_camera_names"]))
    entry = {
        "revision_id": revision["revision_id"],
        "patch_id": revision["patch_id"],
        "source_title": revision["source_title"],
        "applied_at": revision["applied_at"],
        "operation_count": len(operations),
        "operation_types": dict(sorted(operation_types.items())),
        "affected_objects": affected_objects,
        "created_actions": sorted(revision["new_action_names"]),
        "warnings": list(warnings),
        "status": "applied",
    }
    entries = _read_audit_log()
    entries.append(entry)
    _write_audit_log(entries)


def _mark_revision_reverted(revision):
    entries = _read_audit_log()
    for entry in reversed(entries):
        if entry.get("revision_id") == revision["revision_id"]:
            entry["status"] = "reverted"
            entry["reverted_at"] = time.time()
            break
    _write_audit_log(entries)


def list_revision_history():
    scene = bpy.context.scene
    available = {
        revision["revision_id"]
        for revision in REVISION_STACK
        if revision["scene_pointer"] == scene.as_pointer()
    }
    entries = []
    for entry in _read_audit_log(scene):
        item = dict(entry)
        item["rollback_available"] = (
            item.get("status") == "applied" and item.get("revision_id") in available
        )
        entries.append(item)
    return {
        "scene_name": scene.name,
        "entries": entries,
        "available_count": len(available),
    }


def _isolate_action(obj):
    animation = obj.animation_data
    if not animation or not animation.action:
        return
    isolated = animation.action.copy()
    isolated.name = f"{animation.action.name} [FaceLink]"
    animation.action = isolated


def _prepare_animation_edits(operations):
    prepared = set()
    for operation in operations:
        obj = None
        if operation["op"] == "keyframe_transform":
            obj = object_by_id(operation.get("entity_id"))
        elif operation["op"] == "ensure_camera":
            payload = operation.get("payload", {})
            if payload.get("mode") == "dolly_in":
                candidate = bpy.data.objects.get(payload.get("name", "FaceLink Camera"))
                if candidate and candidate.type == "CAMERA":
                    obj = candidate
        if obj and obj.name not in prepared:
            _isolate_action(obj)
            prepared.add(obj.name)


def _restore_constraints(obj, states):
    for constraint in list(obj.constraints):
        if constraint.name.startswith("FaceLink"):
            obj.constraints.remove(constraint)
    for state in states:
        constraint = obj.constraints.new(type=state["type"])
        constraint.name = state["name"]
        constraint.influence = state["influence"]
        if state.get("target"):
            constraint.target = bpy.data.objects.get(state["target"])
        for field in ("track_axis", "up_axis", "use_offset"):
            if field in state and hasattr(constraint, field):
                setattr(constraint, field, state[field])


def _restore_nla(obj, states):
    animation = obj.animation_data
    if animation:
        current = animation.nla_tracks.get("FaceLink")
        if current:
            animation.nla_tracks.remove(current)
    if states is None:
        return
    animation = obj.animation_data_create()
    track = animation.nla_tracks.new()
    track.name = "FaceLink"
    for state in states:
        strip = track.strips.new(state["name"], int(state["frame_start"]), state["action"])
        strip.scale = state["scale"]
        strip.repeat = state["repeat"]


def _restore_object(state):
    obj = bpy.data.objects.get(state["name"])
    if obj is None:
        return
    obj.location = state["location"]
    obj.rotation_mode = state["rotation_mode"]
    obj.rotation_euler = state["rotation_euler"]
    obj.scale = state["scale"]
    if state["camera_lens"] is not None:
        obj.data.lens = state["camera_lens"]
    current_action = obj.animation_data.action if obj.animation_data else None
    if state["had_animation_data"]:
        obj.animation_data_create().action = state["action"]
    elif obj.animation_data:
        obj.animation_data.action = None
    _restore_nla(obj, state["nla"])
    _restore_constraints(obj, state["constraints"])
    if not state["had_animation_data"] and obj.animation_data:
        if not obj.animation_data.action and not obj.animation_data.nla_tracks:
            obj.animation_data_clear()
    if (
        current_action
        and current_action != state["action"]
        and current_action.users == 0
        and (state["action"] is None or "[FaceLink]" in current_action.name)
    ):
        bpy.data.actions.remove(current_action)


def _restore_revision(revision, *, restore_audit=False):
    scene = bpy.context.scene
    scene_state = revision["scene"]
    scene.render.fps = scene_state["fps"]
    scene.render.fps_base = scene_state["fps_base"]
    scene.frame_start = scene_state["frame_start"]
    scene.frame_end = scene_state["frame_end"]
    scene.camera = bpy.data.objects.get(scene_state["camera"]) if scene_state["camera"] else None
    for name in revision["new_camera_names"]:
        camera = bpy.data.objects.get(name)
        if camera and camera.type == "CAMERA":
            data = camera.data
            bpy.data.objects.remove(camera, do_unlink=True)
            if data.users == 0:
                bpy.data.cameras.remove(data)
    for state in revision["objects"].values():
        _restore_object(state)
    for name in revision["new_action_names"]:
        action = bpy.data.actions.get(name)
        if action is not None and action.users == 0:
            bpy.data.actions.remove(action)
    if restore_audit:
        if revision["had_audit_log"]:
            scene[AUDIT_LOG_KEY] = revision["audit_log_raw"]
        elif AUDIT_LOG_KEY in scene:
            del scene[AUDIT_LOG_KEY]
    bpy.context.view_layer.update()


def undo_last_patch():
    if not REVISION_STACK:
        raise ValueError("No FaceLink revision is available to undo")
    if REVISION_STACK[-1]["scene_pointer"] != bpy.context.scene.as_pointer():
        REVISION_STACK.clear()
        raise ValueError("FaceLink revisions were cleared because the active scene changed")
    revision = REVISION_STACK.pop()
    _restore_revision(revision)
    _mark_revision_reverted(revision)
    return {"undone": True, "patch_id": revision["patch_id"]}


def rollback_to_revision(revision_id):
    if not isinstance(revision_id, str) or not revision_id:
        raise ValueError("revision_id is required")
    if not REVISION_STACK:
        raise ValueError("No FaceLink revision is available to roll back")
    scene_pointer = bpy.context.scene.as_pointer()
    if REVISION_STACK[-1]["scene_pointer"] != scene_pointer:
        REVISION_STACK.clear()
        raise ValueError("FaceLink revisions were cleared because the active scene changed")
    target_index = next(
        (
            index
            for index, revision in enumerate(REVISION_STACK)
            if revision["scene_pointer"] == scene_pointer
            and revision["revision_id"] == revision_id
        ),
        None,
    )
    if target_index is None:
        raise ValueError(f"Revision '{revision_id}' is not available in this Blender session")
    rolled_back = []
    while len(REVISION_STACK) > target_index:
        revision = REVISION_STACK.pop()
        _restore_revision(revision)
        _mark_revision_reverted(revision)
        rolled_back.append(
            {
                "revision_id": revision["revision_id"],
                "patch_id": revision["patch_id"],
            }
        )
    return {
        "rolled_back": True,
        "target_revision_id": revision_id,
        "rolled_back_count": len(rolled_back),
        "revisions": rolled_back,
    }


def clear_revisions():
    REVISION_STACK.clear()


def _set_interpolation(obj, frames, interpolation):
    frame_numbers = {float(item["frame"]) for item in frames}
    for curve in _action_fcurves(obj):
        for point in curve.keyframe_points:
            if float(point.co.x) in frame_numbers:
                point.interpolation = interpolation


def _parent_space_matrix(obj):
    if obj.parent:
        return obj.parent.matrix_world @ obj.matrix_parent_inverse
    return Matrix.Identity(4)


def _set_world_location(obj, location):
    parent_space = _parent_space_matrix(obj)
    if abs(parent_space.determinant()) < 1e-12:
        raise ValueError("Cannot convert world transform through a zero-scale parent")
    obj.location = parent_space.inverted() @ Vector(location)
    bpy.context.view_layer.update()


def _keyframe_transform(entity_id, payload):
    obj = object_by_id(entity_id)
    _assert_editable(obj)
    frames = payload.get("frames", [])
    space = payload.get("space", "LOCAL")
    if space not in {"LOCAL", "WORLD"}:
        raise ValueError("Transform space must be LOCAL or WORLD")
    scene = bpy.context.scene
    original_frame = scene.frame_current
    original_subframe = scene.frame_subframe
    try:
        for item in frames:
            frame = int(item["frame"])
            if space == "WORLD":
                scene.frame_set(frame)
                parent_space = _parent_space_matrix(obj)
                if abs(parent_space.determinant()) < 1e-12:
                    raise ValueError("Cannot convert world transform through a zero-scale parent")
                if "location" in item:
                    _set_world_location(obj, item["location"])
                if "rotation_euler" in item:
                    parent_rotation = parent_space.to_quaternion()
                    desired_rotation = Euler(item["rotation_euler"], "XYZ").to_quaternion()
                    obj.rotation_mode = "XYZ"
                    obj.rotation_euler = (parent_rotation.inverted() @ desired_rotation).to_euler(
                        "XYZ"
                    )
                if "scale" in item:
                    parent_scale = parent_space.to_scale()
                    if any(abs(value) < 1e-8 for value in parent_scale):
                        raise ValueError("Cannot convert world scale through a zero-scale parent")
                    obj.scale = [
                        float(item["scale"][index]) / float(parent_scale[index])
                        for index in range(3)
                    ]
            else:
                if "location" in item:
                    obj.location = item["location"]
                if "rotation_euler" in item:
                    obj.rotation_mode = "XYZ"
                    obj.rotation_euler = item["rotation_euler"]
                if "scale" in item:
                    obj.scale = item["scale"]
            if "location" in item:
                obj.keyframe_insert(data_path="location", frame=frame, group="FaceLink")
            if "rotation_euler" in item:
                obj.keyframe_insert(data_path="rotation_euler", frame=frame, group="FaceLink")
            if "scale" in item:
                obj.keyframe_insert(data_path="scale", frame=frame, group="FaceLink")
    finally:
        if space == "WORLD":
            scene.frame_set(original_frame, subframe=original_subframe)
            bpy.context.view_layer.update()
    _set_interpolation(obj, frames, payload.get("interpolation", "BEZIER"))
    return obj


def _look_at(entity_id, payload):
    obj = object_by_id(entity_id)
    _assert_editable(obj)
    target = object_by_id(payload["target_id"])
    constraint = obj.constraints.get("FaceLink Look At")
    if constraint is None:
        constraint = obj.constraints.new(type="TRACK_TO")
        constraint.name = "FaceLink Look At"
    constraint.target = target
    constraint.track_axis = "TRACK_NEGATIVE_Z" if obj.type == "CAMERA" else "TRACK_Y"
    constraint.up_axis = "UP_Y" if obj.type == "CAMERA" else "UP_Z"
    return obj


def _retarget_action(obj, source, value):
    resolved = _resolved_retarget_map(obj, source, value)
    source_fingerprint = action_fingerprint(source)
    name = retarget_action_name(
        source.name,
        obj.name,
        resolved,
        source_fingerprint,
    )
    canonical_map = json.dumps(resolved, sort_keys=True, separators=(",", ":"))
    existing = bpy.data.actions.get(name)
    if existing is not None:
        if (
            existing.get("facelink_retarget_source") != source.name
            or existing.get("facelink_retarget_map") != canonical_map
            or existing.get("facelink_source_fingerprint") != source_fingerprint
        ):
            raise ValueError(f"Retarget action name collision for '{name}'")
        return existing
    action = source.copy()
    action.name = name
    action.use_fake_user = False
    action["facelink_retarget_source"] = source.name
    action["facelink_retarget_map"] = canonical_map
    action["facelink_source_fingerprint"] = source_fingerprint
    renamed_groups = set()
    for _, curve in iter_action_fcurves(action):
        curve.data_path = rewrite_bone_data_path(curve.data_path, resolved)
        group = curve.group
        if group is not None and group.as_pointer() not in renamed_groups:
            group.name = resolved.get(group.name, group.name)
            renamed_groups.add(group.as_pointer())
    return action


def _play_clip(entity_id, payload):
    obj = object_by_id(entity_id)
    _assert_editable(obj)
    source = bpy.data.actions.get(payload["clip"])
    if source is None:
        raise ValueError(f"Animation action '{payload['clip']}' was not found")
    action = (
        _retarget_action(obj, source, payload["retarget"])
        if payload.get("retarget") is not None
        else source
    )
    animation = obj.animation_data_create()
    track = animation.nla_tracks.get("FaceLink") or animation.nla_tracks.new()
    track.name = "FaceLink"
    strip_name = f"FaceLink {action.name} {payload['frame_start']}"
    existing = track.strips.get(strip_name)
    if existing:
        track.strips.remove(existing)
    strip = track.strips.new(strip_name, int(payload["frame_start"]), action)
    source_length = max(1.0, float(action.frame_range[1] - action.frame_range[0]))
    requested_length = max(1.0, float(payload["frame_end"] - payload["frame_start"]))
    strip.scale = requested_length / source_length
    if payload.get("loop"):
        strip.repeat = max(1.0, requested_length / source_length)
    return obj


def _camera_target(camera, target):
    constraint = camera.constraints.get("FaceLink Camera Target")
    if constraint is None:
        constraint = camera.constraints.new(type="TRACK_TO")
        constraint.name = "FaceLink Camera Target"
    constraint.target = target
    constraint.track_axis = "TRACK_NEGATIVE_Z"
    constraint.up_axis = "UP_Y"


def _ensure_camera(payload):
    name = payload.get("name", "FaceLink Camera")
    camera = bpy.data.objects.get(name)
    if camera is None or camera.type != "CAMERA":
        data = bpy.data.cameras.new(name)
        camera = bpy.data.objects.new(name, data)
        bpy.context.scene.collection.objects.link(camera)
    else:
        _assert_editable(camera)
    ensure_entity_id(camera)
    camera.data.lens = float(payload.get("lens_mm", 50.0))
    target = object_by_id(payload["target"]) if payload.get("target") else None
    space = payload.get("space", "LOCAL")
    scene = bpy.context.scene
    original_frame = scene.frame_current
    original_subframe = scene.frame_subframe
    try:
        if target and payload.get("frame_start") is not None:
            scene.frame_set(int(payload["frame_start"]))
            bpy.context.view_layer.update()
        if payload.get("location"):
            location = payload["location"]
            values = [location[axis] for axis in ("x", "y", "z")]
            if space == "WORLD":
                _set_world_location(camera, values)
            else:
                camera.location = values
        elif target:
            target_location = (
                target.matrix_world.translation if space == "WORLD" else target.location
            )
            location = [
                target_location.x,
                target_location.y - float(payload.get("distance", 6.0)),
                target_location.z + float(payload.get("height", 2.0)),
            ]
            if space == "WORLD":
                _set_world_location(camera, location)
            else:
                camera.location = location
        if target and payload.get("mode") in {"look_at", "follow", "dolly_in"}:
            _camera_target(camera, target)
        if target and payload.get("mode") == "follow":
            constraint = camera.constraints.get("FaceLink Follow")
            if constraint is None:
                constraint = camera.constraints.new(type="COPY_LOCATION")
                constraint.name = "FaceLink Follow"
            constraint.target = target
            constraint.use_offset = True
        if target and payload.get("mode") == "dolly_in":
            start = int(payload["frame_start"])
            end = int(payload["frame_end"])
            camera.keyframe_insert(data_path="location", frame=start, group="FaceLink")
            camera_location = (
                camera.matrix_world.translation.copy()
                if space == "WORLD"
                else camera.location.copy()
            )
            scene.frame_set(end)
            bpy.context.view_layer.update()
            target_location = (
                target.matrix_world.translation if space == "WORLD" else Vector(target.location)
            )
            direction = target_location - camera_location
            if direction.length > 0.001:
                destination = camera_location + direction.normalized() * min(
                    direction.length * 0.5,
                    float(payload.get("distance", 6.0)) * 0.5,
                )
                if space == "WORLD":
                    _set_world_location(camera, destination)
                else:
                    camera.location = destination
            camera.keyframe_insert(data_path="location", frame=end, group="FaceLink")
    finally:
        scene.frame_set(original_frame, subframe=original_subframe)
        bpy.context.view_layer.update()
    bpy.context.scene.camera = camera
    return camera


def apply_patch(patch):
    operations = validate_patch(patch)

    revision = _capture_revision(patch, operations)
    _prepare_animation_edits(operations)
    changed = set()
    composition = analyze_patch_composition(patch)
    composition_warnings = [item["message"] for item in composition["warnings"]]
    warnings = list(
        dict.fromkeys(
            [
                *patch.get("warnings", []),
                *_existing_animation_warnings(operations),
                *composition_warnings,
            ]
        )
    )
    try:
        for operation in operations:
            op = operation["op"]
            entity_id = operation.get("entity_id")
            payload = operation.get("payload", {})
            if op == "set_frame_range":
                scene = bpy.context.scene
                scene.render.fps = round(float(payload["fps"]))
                scene.render.fps_base = scene.render.fps / float(payload["fps"])
                scene.frame_start = int(payload["frame_start"])
                scene.frame_end = int(payload["frame_end"])
            elif op == "keyframe_transform":
                changed.add(ensure_entity_id(_keyframe_transform(entity_id, payload)))
            elif op == "look_at":
                changed.add(ensure_entity_id(_look_at(entity_id, payload)))
            elif op == "play_clip":
                changed.add(ensure_entity_id(_play_clip(entity_id, payload)))
            elif op == "ensure_camera":
                changed.add(ensure_entity_id(_ensure_camera(payload)))
        _record_revision(revision, operations, warnings)
    except Exception:
        _restore_revision(revision, restore_audit=True)
        raise
    REVISION_STACK.append(revision)
    if len(REVISION_STACK) > MAX_REVISIONS:
        del REVISION_STACK[0]
    return {
        "patch_id": patch.get("patch_id", "unknown"),
        "revision_id": revision["revision_id"],
        "applied_operations": len(operations),
        "changed_entities": sorted(changed),
        "warnings": warnings,
    }
