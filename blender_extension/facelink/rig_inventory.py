import hashlib
import json

from .action_inventory import MAX_RIG_BONES


def _number(value):
    rounded = round(float(value), 6)
    return 0.0 if rounded == 0.0 else rounded


def _vec3(value):
    return {"x": float(value[0]), "y": float(value[1]), "z": float(value[2])}


def _quaternion(value):
    components = [float(value.w), float(value.x), float(value.y), float(value.z)]
    first_nonzero = next((item for item in components if abs(item) > 1e-12), 1.0)
    if first_nonzero < 0.0:
        components = [-item for item in components]
    return {
        "w": components[0],
        "x": components[1],
        "y": components[2],
        "z": components[3],
    }


def _bone_payload(bone):
    return {
        "name": bone.name,
        "parent": bone.parent.name if bone.parent else None,
        "use_deform": bool(bone.use_deform),
        "head": _vec3(bone.head_local),
        "tail": _vec3(bone.tail_local),
        "rest_rotation": _quaternion(bone.matrix_local.to_quaternion()),
    }


def _fingerprint_payload(bones):
    return [
        {
            "name": item["name"],
            "parent": item["parent"],
            "use_deform": item["use_deform"],
            "head": [_number(item["head"][axis]) for axis in "xyz"],
            "tail": [_number(item["tail"][axis]) for axis in "xyz"],
            "rest_rotation": [
                _number(item["rest_rotation"][axis]) for axis in ("w", "x", "y", "z")
            ],
        }
        for item in bones
    ]


def _id_reference(value):
    if value is None:
        return None
    library = getattr(value, "library", None)
    return {
        "type": getattr(getattr(value, "bl_rna", None), "identifier", type(value).__name__),
        "name": getattr(value, "name_full", getattr(value, "name", None)),
        "library": getattr(library, "filepath", None),
        "facelink_id": value.get("facelink_id") if hasattr(value, "get") else None,
    }


def _rna_scalar(value, prop_type):
    if prop_type == "BOOLEAN":
        return bool(value)
    if prop_type == "INT":
        return int(value)
    if prop_type == "FLOAT":
        return _number(value)
    if prop_type in {"STRING", "ENUM"}:
        return str(value)
    return None


def _rna_item_payload(item):
    payload = {}
    for prop in item.bl_rna.properties:
        if prop.identifier == "rna_type":
            continue
        try:
            value = getattr(item, prop.identifier)
        except (AttributeError, ReferenceError, TypeError):
            continue
        if prop.type in {"BOOLEAN", "INT", "FLOAT", "STRING", "ENUM"}:
            payload[prop.identifier] = (
                [_rna_scalar(component, prop.type) for component in value]
                if getattr(prop, "is_array", False)
                else _rna_scalar(value, prop.type)
            )
        elif prop.type == "POINTER":
            payload[prop.identifier] = _id_reference(value)
    return payload


def _constraint_payload(constraint):
    payload = {
        "name": constraint.name,
        "type": constraint.type,
        "settings": {},
    }
    for prop in constraint.bl_rna.properties:
        if prop.identifier in {"rna_type", "name", "type"}:
            continue
        try:
            value = getattr(constraint, prop.identifier)
        except (AttributeError, ReferenceError, TypeError):
            continue
        if prop.type in {"BOOLEAN", "INT", "FLOAT", "STRING", "ENUM"}:
            payload["settings"][prop.identifier] = (
                [_rna_scalar(component, prop.type) for component in value]
                if getattr(prop, "is_array", False)
                else _rna_scalar(value, prop.type)
            )
        elif prop.type == "POINTER":
            payload["settings"][prop.identifier] = _id_reference(value)
        elif prop.type == "COLLECTION":
            payload["settings"][prop.identifier] = [
                _rna_item_payload(item) for item in value
            ]
    return payload


def _driver_payload(curve):
    driver = curve.driver
    return {
        "data_path": curve.data_path,
        "array_index": int(curve.array_index),
        "type": driver.type,
        "expression": driver.expression,
        "use_self": bool(driver.use_self),
        "variables": [
            {
                "name": variable.name,
                "type": variable.type,
                "targets": [
                    {
                        "id": _id_reference(target.id),
                        "data_path": target.data_path,
                        "bone_target": target.bone_target,
                        "transform_space": target.transform_space,
                        "transform_type": target.transform_type,
                        "rotation_mode": target.rotation_mode,
                    }
                    for target in variable.targets
                ],
            }
            for variable in driver.variables
        ],
    }


def _custom_value(value, depth=0):
    if depth > 4:
        return "<depth-limit>"
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return _number(value)
    if hasattr(value, "bl_rna") and hasattr(value, "name_full"):
        return _id_reference(value)
    if hasattr(value, "keys"):
        return {
            str(key): _custom_value(value[key], depth + 1)
            for key in sorted(value.keys())
            if key != "_RNA_UI"
        }
    if hasattr(value, "__iter__"):
        return [_custom_value(item, depth + 1) for item in value]
    return repr(value)


def _custom_properties(owner):
    if not hasattr(owner, "keys"):
        return {}
    return {
        str(key): _custom_value(owner[key])
        for key in sorted(owner.keys())
        if key != "_RNA_UI"
    }


def _owner_drivers(owner):
    animation = getattr(owner, "animation_data", None)
    if animation is None:
        return []
    return sorted(
        (_driver_payload(curve) for curve in animation.drivers),
        key=lambda item: (item["data_path"], item["array_index"]),
    )


def _rig_fingerprint_payload(obj, bones):
    return {
        "bones": _fingerprint_payload(bones),
        "pose_rotation_modes": [
            [bone.name, obj.pose.bones[bone.name].rotation_mode]
            for bone in sorted(obj.data.bones, key=lambda item: item.name)
        ],
        "pose_constraints": [
            [
                pose_bone.name,
                [_constraint_payload(item) for item in pose_bone.constraints],
            ]
            for pose_bone in sorted(obj.pose.bones, key=lambda item: item.name)
        ],
        "drivers": {
            "object": _owner_drivers(obj),
            "armature_data": _owner_drivers(obj.data),
        },
        "custom_properties": {
            "object": _custom_properties(obj),
            "armature_data": _custom_properties(obj.data),
            "pose_bones": [
                [pose_bone.name, _custom_properties(pose_bone)]
                for pose_bone in sorted(obj.pose.bones, key=lambda item: item.name)
            ],
        },
    }


def rig_fingerprint(obj):
    bones = [_bone_payload(bone) for bone in sorted(obj.data.bones, key=lambda item: item.name)]
    if len(bones) > MAX_RIG_BONES:
        raise ValueError(f"Rig '{obj.name}' exceeds {MAX_RIG_BONES} bones")
    encoded = json.dumps(
        _rig_fingerprint_payload(obj, bones), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "rig-" + hashlib.sha256(encoded).hexdigest()[:24]


def rig_inventory(obj, entity_id):
    bones = [_bone_payload(bone) for bone in sorted(obj.data.bones, key=lambda item: item.name)]
    if len(bones) > MAX_RIG_BONES:
        raise ValueError(f"Rig '{obj.name}' exceeds {MAX_RIG_BONES} bones")
    encoded = json.dumps(
        _rig_fingerprint_payload(obj, bones), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "entity_id": entity_id,
        "name": obj.name,
        "bones": bones,
        "fingerprint": "rig-" + hashlib.sha256(encoded).hexdigest()[:24],
    }
