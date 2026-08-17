import hashlib
import json
import math
import re

import bpy

MAX_ACTIONS = 512
MAX_ACTION_FCURVES = 10_000
MAX_ACTION_KEYFRAMES = 200_000
MAX_RIGS = 64
MAX_RIG_BONES = 1_024

_POSE_BONE_PATTERN = re.compile(r'^pose\.bones\["((?:\\.|[^"\\])*)"\]')


def _number(value):
    rounded = round(float(value), 6)
    return 0.0 if rounded == 0.0 else rounded


def bone_name_from_data_path(data_path):
    match = _POSE_BONE_PATTERN.match(data_path)
    if match is None:
        return None
    return bpy.utils.unescape_identifier(match.group(1))


def rewrite_bone_data_path(data_path, bone_map):
    match = _POSE_BONE_PATTERN.match(data_path)
    if match is None:
        return data_path
    source = bpy.utils.unescape_identifier(match.group(1))
    target = bone_map.get(source, source)
    escaped = bpy.utils.escape_identifier(target)
    return f'pose.bones["{escaped}"]{data_path[match.end() :]}'


def iter_action_fcurves(action):
    records = []
    seen = set()
    legacy = getattr(action, "fcurves", None)
    if legacy is not None:
        for curve in legacy:
            pointer = curve.as_pointer()
            if pointer not in seen:
                records.append(("legacy", curve))
                seen.add(pointer)
    slots = list(getattr(action, "slots", []))
    for layer in getattr(action, "layers", []):
        for strip in layer.strips:
            channelbag_method = getattr(strip, "channelbag", None)
            if channelbag_method is None:
                continue
            for slot in slots:
                try:
                    bag = channelbag_method(slot)
                except (RuntimeError, TypeError, ValueError):
                    continue
                if bag is None:
                    continue
                slot_id = str(
                    getattr(slot, "identifier", getattr(slot, "handle", "unknown"))
                )
                for curve in bag.fcurves:
                    pointer = curve.as_pointer()
                    if pointer not in seen:
                        records.append((slot_id, curve))
                        seen.add(pointer)
    return sorted(records, key=lambda item: (item[0], item[1].data_path, item[1].array_index))


def _modifier_payload(modifier):
    values = {"type": modifier.type}
    for prop in modifier.bl_rna.properties:
        identifier = prop.identifier
        if identifier == "rna_type" or prop.is_readonly or prop.type in {"POINTER", "COLLECTION"}:
            continue
        try:
            value = getattr(modifier, identifier)
        except (AttributeError, RuntimeError, TypeError):
            continue
        if isinstance(value, bool | int | str):
            values[identifier] = value
        elif isinstance(value, float):
            values[identifier] = _number(value) if math.isfinite(value) else str(value)
        elif hasattr(value, "__iter__"):
            try:
                values[identifier] = [
                    _number(item) if isinstance(item, float) else item for item in value
                ]
            except (RuntimeError, TypeError, ValueError):
                continue
    return values


def _curve_payload(slot_id, curve):
    return {
        "slot": slot_id,
        "data_path": curve.data_path,
        "array_index": int(curve.array_index),
        "extrapolation": curve.extrapolation,
        "keyframes": [
            {
                "co": [_number(value) for value in point.co],
                "handle_left": [_number(value) for value in point.handle_left],
                "handle_right": [_number(value) for value in point.handle_right],
                "handle_left_type": point.handle_left_type,
                "handle_right_type": point.handle_right_type,
                "interpolation": point.interpolation,
            }
            for point in curve.keyframe_points
        ],
        "modifiers": [_modifier_payload(modifier) for modifier in curve.modifiers],
    }


def action_fingerprint(action):
    payload = [_curve_payload(slot_id, curve) for slot_id, curve in iter_action_fcurves(action)]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "action-" + hashlib.sha256(encoded).hexdigest()[:24]


def action_inventory(action):
    records = iter_action_fcurves(action)
    if len(records) > MAX_ACTION_FCURVES:
        raise ValueError(f"Action '{action.name}' exceeds {MAX_ACTION_FCURVES} F-curves")
    keyframe_count = sum(len(curve.keyframe_points) for _, curve in records)
    if keyframe_count > MAX_ACTION_KEYFRAMES:
        raise ValueError(f"Action '{action.name}' exceeds {MAX_ACTION_KEYFRAMES} keyframes")
    data_paths = sorted({curve.data_path for _, curve in records})
    pose_bones = sorted(
        {
            bone_name
            for data_path in data_paths
            if (bone_name := bone_name_from_data_path(data_path)) is not None
        }
    )
    frame_start, frame_end = action.frame_range
    return {
        "name": action.name,
        "frame_start": float(frame_start),
        "frame_end": float(frame_end),
        "fcurve_count": len(records),
        "keyframe_count": keyframe_count,
        "pose_bones": pose_bones,
        "data_paths": data_paths,
        "fingerprint": action_fingerprint(action),
    }


def action_pose_bones(action):
    return {
        bone_name
        for _, curve in iter_action_fcurves(action)
        if (bone_name := bone_name_from_data_path(curve.data_path)) is not None
    }


def action_inventories():
    actions = sorted(bpy.data.actions, key=lambda item: item.name)
    if len(actions) > MAX_ACTIONS:
        raise ValueError(f"A file may contain at most {MAX_ACTIONS} actions")
    return [action_inventory(action) for action in actions]


def retarget_action_name(source_name, target_name, bone_map, source_fingerprint):
    encoded = json.dumps(
        {"bone_map": bone_map, "source_fingerprint": source_fingerprint},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    suffix = hashlib.sha256(encoded).hexdigest()[:10]
    return f"FaceLink {source_name[:24]} to {target_name[:24]} {suffix}"
