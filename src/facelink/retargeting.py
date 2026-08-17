from __future__ import annotations

from dataclasses import dataclass

from .models import ActionInventory, ActionRetargetSpec, RigInventory


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
