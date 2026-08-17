from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass

from .models import (
    ActionInventory,
    ActionRetargetSpec,
    BoneMapMatch,
    Quaternion,
    RetargetBoneMetric,
    RetargetCompatibilityIssue,
    RetargetCompatibilityReport,
    RetargetProfile,
    RetargetProfileSuggestion,
    RigBone,
    RigInventory,
    Vec3,
)

SAFE_REST_ANGLE_DEGREES = 1.0
BAKE_REST_ANGLE_DEGREES = 5.0
SAFE_PROPORTION_DEVIATION_PERCENT = 2.0
BAKE_PROPORTION_DEVIATION_PERCENT = 10.0


@dataclass(frozen=True)
class RetargetAnalysis:
    resolved_map: dict[str, str]
    missing_sources: tuple[str, ...]
    missing_targets: tuple[str, ...]
    unused_sources: tuple[str, ...]
    duplicate_targets: tuple[str, ...]

    @property
    def compatible(self) -> bool:
        return not (self.missing_sources or self.missing_targets or self.duplicate_targets)


def analyze_retarget(
    action: ActionInventory,
    rig: RigInventory,
    spec: ActionRetargetSpec | None,
) -> RetargetAnalysis:
    action_bones = set(action.pose_bones)
    target_bones = {bone.name for bone in rig.bones}
    declared = spec.bone_map if spec is not None else {}
    strict = spec.strict if spec is not None else False
    resolved = {}
    missing_sources = []
    for source in sorted(action_bones):
        if source in declared:
            resolved[source] = declared[source]
        elif strict:
            missing_sources.append(source)
        else:
            resolved[source] = source
    missing_targets = sorted(
        {target for target in resolved.values() if target not in target_bones}
    )
    unused_sources = sorted(set(declared) - action_bones)
    owners: dict[str, list[str]] = {}
    for source, target in resolved.items():
        owners.setdefault(target, []).append(source)
    duplicate_targets = sorted(
        target for target, sources in owners.items() if len(sources) > 1
    )
    return RetargetAnalysis(
        resolved_map=dict(sorted(resolved.items())),
        missing_sources=tuple(missing_sources),
        missing_targets=tuple(missing_targets),
        unused_sources=tuple(unused_sources),
        duplicate_targets=tuple(duplicate_targets),
    )


_ALIASES = {
    "hips": "pelvis",
    "hip": "pelvis",
    "pelvis": "pelvis",
    "upperarm": "upperarm",
    "arm": "upperarm",
    "lowerarm": "forearm",
    "forearm": "forearm",
    "upleg": "thigh",
    "upperleg": "thigh",
    "thigh": "thigh",
    "lowerleg": "calf",
    "leg": "calf",
    "calf": "calf",
}


def _name_parts(name: str) -> tuple[str, str]:
    local = name.rsplit(":", 1)[-1]
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", local)
    tokens = re.findall(r"[A-Za-z]+|[0-9]+", separated.lower())
    side = ""
    if tokens and tokens[0] in {"left", "right", "l", "r"}:
        side = "l" if tokens.pop(0) in {"left", "l"} else "r"
    elif tokens and tokens[-1] in {"left", "right", "l", "r"}:
        side = "l" if tokens.pop() in {"left", "l"} else "r"
    base = "".join(tokens)
    return side, base


def _plain_name(name: str) -> str:
    local = name.rsplit(":", 1)[-1]
    return "".join(character for character in local.lower() if character.isalnum())


def _semantic_name(name: str) -> str:
    side, base = _name_parts(name)
    return f"{side}:{_ALIASES.get(base, base)}"


def suggest_retarget_profile(
    source_rig: RigInventory,
    target_rig: RigInventory,
    *,
    name: str,
    source_bones: set[str] | None = None,
) -> RetargetProfileSuggestion:
    if source_bones is not None:
        source_by_name = {bone.name: bone for bone in source_rig.bones}
        expanded = set(source_bones)
        for bone_name in list(source_bones):
            bone = source_by_name.get(bone_name)
            while bone is not None and bone.parent is not None:
                expanded.add(bone.parent)
                bone = source_by_name.get(bone.parent)
        source_bones = expanded
    source_names = sorted(
        bone.name
        for bone in source_rig.bones
        if source_bones is None or bone.name in source_bones
    )
    target_names = sorted(bone.name for bone in target_rig.bones)
    available = set(target_names)
    unmatched = set(source_names)
    mapping: dict[str, str] = {}
    matches: list[BoneMapMatch] = []
    conflicts: dict[str, list[str]] = {}

    tiers = (
        ("exact", lambda value: value, "high"),
        ("normalized", _plain_name, "high"),
        ("alias", _semantic_name, "medium"),
    )
    for method, key_function, confidence in tiers:
        proposals = {}
        for source in sorted(unmatched):
            candidates = sorted(
                target for target in available if key_function(target) == key_function(source)
            )
            if len(candidates) == 1:
                proposals[source] = candidates[0]
            elif len(candidates) > 1:
                conflicts[source] = candidates
        target_owners: dict[str, list[str]] = {}
        for source, target in proposals.items():
            target_owners.setdefault(target, []).append(source)
        for source, target in sorted(proposals.items()):
            owners = target_owners[target]
            if len(owners) > 1:
                conflicts[source] = sorted(owners)
                continue
            mapping[source] = target
            conflicts.pop(source, None)
            matches.append(
                BoneMapMatch(
                    source_bone=source,
                    target_bone=target,
                    method=method,
                    confidence=confidence,
                )
            )
            unmatched.remove(source)
            available.remove(target)

    profile = (
        RetargetProfile(
            name=name,
            bone_map=dict(sorted(mapping.items())),
            strict=True,
            source_rig=source_rig.entity_id,
        )
        if mapping
        else None
    )
    return RetargetProfileSuggestion(
        source_rig_id=source_rig.entity_id,
        target_rig_id=target_rig.entity_id,
        profile=profile,
        matches=matches,
        unmapped_sources=sorted(unmatched),
        unused_targets=sorted(available),
        conflicts=dict(sorted(conflicts.items())),
    )


def _subtract(first: Vec3, second: Vec3) -> tuple[float, float, float]:
    return first.x - second.x, first.y - second.y, first.z - second.z


def _length(vector: tuple[float, float, float]) -> float:
    return math.sqrt(sum(component * component for component in vector))


def _normalize(vector: tuple[float, float, float]) -> tuple[float, float, float]:
    magnitude = _length(vector)
    if magnitude <= 1e-9:
        return 0.0, 0.0, 0.0
    return tuple(component / magnitude for component in vector)


def _quat(value: Quaternion) -> tuple[float, float, float, float]:
    return value.w, value.x, value.y, value.z


def _quat_inverse(value):
    return value[0], -value[1], -value[2], -value[3]


def _quat_multiply(first, second):
    w1, x1, y1, z1 = first
    w2, x2, y2, z2 = second
    return (
        w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
        w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
        w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
        w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
    )


def _rotate_inverse(rotation, vector):
    pure = (0.0, *vector)
    inverse = _quat_inverse(rotation)
    rotated = _quat_multiply(_quat_multiply(inverse, pure), rotation)
    return rotated[1], rotated[2], rotated[3]


def _angle(first, second) -> float:
    first_normalized = _normalize(first)
    second_normalized = _normalize(second)
    if _length(first_normalized) <= 1e-9 or _length(second_normalized) <= 1e-9:
        return 180.0
    cosine = max(
        -1.0,
        min(
            1.0,
            sum(
                a * b
                for a, b in zip(first_normalized, second_normalized, strict=True)
            ),
        ),
    )
    return math.degrees(math.acos(cosine))


def _quaternion_angle(first, second) -> float:
    dot = abs(sum(a * b for a, b in zip(first, second, strict=True)))
    return math.degrees(2.0 * math.acos(max(-1.0, min(1.0, dot))))


def _relative_rotation(bone: RigBone, bones: dict[str, RigBone]):
    rotation = _quat(bone.rest_rotation)
    if bone.parent is None:
        return rotation
    return _quat_multiply(_quat_inverse(_quat(bones[bone.parent].rest_rotation)), rotation)


def _relative_axis(bone: RigBone, bones: dict[str, RigBone]):
    axis = _subtract(bone.tail, bone.head)
    if bone.parent is None:
        return axis
    return _rotate_inverse(_quat(bones[bone.parent].rest_rotation), axis)


def analyze_rig_compatibility(
    source_rig: RigInventory,
    target_rig: RigInventory,
    spec: ActionRetargetSpec,
) -> RetargetCompatibilityReport:
    source_bones = {bone.name: bone for bone in source_rig.bones}
    target_bones = {bone.name: bone for bone in target_rig.bones}
    issues: list[RetargetCompatibilityIssue] = []
    valid_pairs = []
    target_owners: dict[str, list[str]] = {}
    for source_name, target_name in sorted(spec.bone_map.items()):
        target_owners.setdefault(target_name, []).append(source_name)
        source = source_bones.get(source_name)
        target = target_bones.get(target_name)
        if source is None:
            issues.append(
                RetargetCompatibilityIssue(
                    severity="error",
                    code="source_bone_missing",
                    message=f"Source rig is missing mapped bone '{source_name}'.",
                    source_bone=source_name,
                    target_bone=target_name,
                )
            )
        if target is None:
            issues.append(
                RetargetCompatibilityIssue(
                    severity="error",
                    code="target_bone_missing",
                    message=f"Target rig is missing mapped bone '{target_name}'.",
                    source_bone=source_name,
                    target_bone=target_name,
                )
            )
        if source is not None and target is not None:
            valid_pairs.append((source, target))
    for target_name, owners in sorted(target_owners.items()):
        if len(owners) > 1:
            issues.append(
                RetargetCompatibilityIssue(
                    severity="error",
                    code="target_bone_collision",
                    message=(
                        f"Target bone '{target_name}' is mapped from multiple source bones: "
                        + ", ".join(sorted(owners))
                    ),
                    target_bone=target_name,
                )
            )

    ratios = []
    lengths = {}
    for source, target in valid_pairs:
        source_length = _length(_subtract(source.tail, source.head))
        target_length = _length(_subtract(target.tail, target.head))
        ratio = (
            target_length / source_length
            if source_length > 1e-9 and target_length > 1e-9
            else None
        )
        lengths[(source.name, target.name)] = (source_length, target_length, ratio)
        if ratio is not None:
            ratios.append(ratio)
        else:
            issues.append(
                RetargetCompatibilityIssue(
                    severity="error",
                    code="zero_length_bone",
                    message=f"Mapped bone '{source.name}' or '{target.name}' has zero length.",
                    source_bone=source.name,
                    target_bone=target.name,
                )
            )
    median_ratio = statistics.median(ratios) if ratios else None
    metrics = []
    hierarchy_mismatch = False
    geometry_requires_bake = False
    geometry_needs_review = False
    for source, target in valid_pairs:
        source_length, target_length, ratio = lengths[(source.name, target.name)]
        deviation = (
            abs(ratio / median_ratio - 1.0) * 100.0
            if ratio is not None and median_ratio is not None
            else None
        )
        expected_parent = spec.bone_map.get(source.parent) if source.parent else None
        hierarchy_preserved = expected_parent == target.parent
        if source.parent is not None and expected_parent is None:
            hierarchy_preserved = False
            issues.append(
                RetargetCompatibilityIssue(
                    severity="warning",
                    code="source_parent_unmapped",
                    message=f"Parent '{source.parent}' of mapped bone '{source.name}' is unmapped.",
                    source_bone=source.name,
                    target_bone=target.name,
                )
            )
        elif not hierarchy_preserved:
            issues.append(
                RetargetCompatibilityIssue(
                    severity="warning",
                    code="hierarchy_mismatch",
                    message=(
                        f"Mapped parent of '{source.name}' is '{expected_parent}', but target "
                        f"bone '{target.name}' is parented to '{target.parent}'."
                    ),
                    source_bone=source.name,
                    target_bone=target.name,
                )
            )
        hierarchy_mismatch = hierarchy_mismatch or not hierarchy_preserved
        axis_angle = _angle(
            _relative_axis(source, source_bones),
            _relative_axis(target, target_bones),
        )
        rotation_angle = _quaternion_angle(
            _relative_rotation(source, source_bones),
            _relative_rotation(target, target_bones),
        )
        if axis_angle > SAFE_REST_ANGLE_DEGREES:
            issues.append(
                RetargetCompatibilityIssue(
                    severity="warning",
                    code="rest_axis_difference",
                    message=(
                        f"Mapped bone '{source.name}' to '{target.name}' differs by "
                        f"{axis_angle:.2f} degrees in rest axis."
                    ),
                    source_bone=source.name,
                    target_bone=target.name,
                )
            )
        if rotation_angle > SAFE_REST_ANGLE_DEGREES:
            issues.append(
                RetargetCompatibilityIssue(
                    severity="warning",
                    code="rest_rotation_difference",
                    message=(
                        f"Mapped bone '{source.name}' to '{target.name}' differs by "
                        f"{rotation_angle:.2f} degrees in local rest orientation."
                    ),
                    source_bone=source.name,
                    target_bone=target.name,
                )
            )
        if (
            deviation is not None
            and deviation > SAFE_PROPORTION_DEVIATION_PERCENT
        ):
            issues.append(
                RetargetCompatibilityIssue(
                    severity="warning",
                    code="proportion_difference",
                    message=(
                        f"Mapped bone '{source.name}' to '{target.name}' differs from the "
                        f"median rig scale by {deviation:.2f}%."
                    ),
                    source_bone=source.name,
                    target_bone=target.name,
                )
            )
        geometry_requires_bake = geometry_requires_bake or (
            axis_angle > BAKE_REST_ANGLE_DEGREES
            or rotation_angle > BAKE_REST_ANGLE_DEGREES
            or (
                deviation is not None
                and deviation > BAKE_PROPORTION_DEVIATION_PERCENT
            )
        )
        geometry_needs_review = geometry_needs_review or (
            axis_angle > SAFE_REST_ANGLE_DEGREES
            or rotation_angle > SAFE_REST_ANGLE_DEGREES
            or (
                deviation is not None
                and deviation > SAFE_PROPORTION_DEVIATION_PERCENT
            )
        )
        metrics.append(
            RetargetBoneMetric(
                source_bone=source.name,
                target_bone=target.name,
                source_length=source_length,
                target_length=target_length,
                length_ratio=ratio,
                length_deviation_percent=deviation,
                axis_angle_degrees=axis_angle,
                rest_rotation_angle_degrees=rotation_angle,
                hierarchy_preserved=hierarchy_preserved,
            )
        )

    has_errors = any(issue.severity == "error" for issue in issues)
    if has_errors or not valid_pairs:
        status = "incompatible"
    elif hierarchy_mismatch or geometry_requires_bake:
        status = "bake_required"
    elif geometry_needs_review:
        status = "review"
    else:
        status = "safe"
    return RetargetCompatibilityReport(
        source_rig_id=source_rig.entity_id,
        source_rig_name=source_rig.name,
        target_rig_id=target_rig.entity_id,
        target_rig_name=target_rig.name,
        status=status,
        rename_only_safe=status == "safe",
        mapped_bone_count=len(valid_pairs),
        median_length_ratio=median_ratio,
        max_axis_angle_degrees=max(
            (metric.axis_angle_degrees for metric in metrics), default=0.0
        ),
        max_rest_rotation_angle_degrees=max(
            (metric.rest_rotation_angle_degrees for metric in metrics), default=0.0
        ),
        metrics=metrics,
        issues=issues,
    )
