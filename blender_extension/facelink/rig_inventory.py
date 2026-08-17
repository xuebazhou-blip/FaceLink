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


def _rig_fingerprint_payload(obj, bones):
    return {
        "bones": _fingerprint_payload(bones),
        "pose_rotation_modes": [
            [bone.name, obj.pose.bones[bone.name].rotation_mode]
            for bone in sorted(obj.data.bones, key=lambda item: item.name)
        ],
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
