from __future__ import annotations

import hashlib
import json
import math

from .fingerprint import fingerprint_snapshot
from .models import (
    LookAtBeat,
    MoveToBeat,
    PatchOperation,
    PlayClipBeat,
    RigInventory,
    ScenePatch,
    SceneSnapshot,
    ShotSpec,
    TurnToBeat,
    ValidationIssue,
    ValidationReport,
    Vec3,
)
from .navigation import (
    NavigationError,
    allocate_path_frames,
    navigation_environment_fingerprint,
    plan_move_path,
)
from .retargeting import analyze_retarget, analyze_rig_compatibility


def _frame(seconds: float, fps: float, start: int) -> int:
    return start + round(seconds * fps)


def _target_position(
    target_entity: str | None,
    target_position: Vec3 | None,
    locations: dict[str, Vec3],
) -> Vec3 | None:
    if target_position is not None:
        return target_position
    if target_entity is not None and target_entity in locations:
        return locations[target_entity]
    return None


def _navigation_issues(shot: ShotSpec, snapshot: SceneSnapshot) -> list[ValidationIssue]:
    issues = []
    entities = snapshot.by_id()
    current_locations = {
        entity_id: entity.transform.location.model_copy(deep=True)
        for entity_id, entity in entities.items()
    }
    declared_fingerprint = snapshot.navigation_environment_fingerprint
    actual_fingerprint = navigation_environment_fingerprint(snapshot)
    if declared_fingerprint is not None and declared_fingerprint != actual_fingerprint:
        issues.append(
            ValidationIssue(
                severity="error",
                code="navigation_fingerprint_mismatch",
                message="Scene navigation data does not match its declared fingerprint.",
            )
        )
    for index, beat in sorted(enumerate(shot.beats), key=lambda item: (item[1].at, item[0])):
        if not isinstance(beat, MoveToBeat):
            continue
        if beat.actor not in entities:
            continue
        target = _target_position(
            beat.target_entity,
            beat.target_position,
            current_locations,
        )
        if target is None:
            continue
        try:
            plan = plan_move_path(
                snapshot,
                entities[beat.actor],
                current_locations[beat.actor],
                target,
                path_mode=beat.path_mode,
                navigation_mesh=beat.navigation_mesh,
                clearance=beat.clearance,
                target_entity_id=beat.target_entity,
            )
            allocate_path_frames(
                plan.points,
                _frame(beat.at, shot.fps, snapshot.frame_start),
                _frame(beat.at + beat.duration, shot.fps, snapshot.frame_start),
            )
        except NavigationError as exc:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code=exc.code,
                    message=str(exc),
                    beat_index=index,
                )
            )
        else:
            for warning in plan.warnings:
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        code=("path_collision" if plan.collision_ids else "navigation_warning"),
                        message=warning,
                        beat_index=index,
                    )
                )
            if beat.path_mode == "navmesh" and beat.easing != "LINEAR":
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        code="navmesh_requires_linear_interpolation",
                        message=(
                            f"Beat {index} uses LINEAR interpolation so the curve stays on "
                            "its navigation path."
                        ),
                        beat_index=index,
                    )
                )
        current_locations[beat.actor] = target.model_copy(deep=True)
    return issues


def _resolve_source_rig(
    action,
    retarget,
    rigs: dict[str, RigInventory],
) -> tuple[RigInventory | None, str | None]:
    if retarget is None or not action.pose_bones:
        return None, None
    if retarget.source_rig is not None:
        source = rigs.get(retarget.source_rig)
        return (source, "missing") if source is None else (source, None)
    action_bones = set(action.pose_bones)
    candidates = [
        rig for rig in rigs.values() if action_bones <= {bone.name for bone in rig.bones}
    ]
    if len(candidates) == 1:
        return candidates[0], None
    return None, "ambiguous" if candidates else "missing"


def _rig_action_issues(shot: ShotSpec, snapshot: SceneSnapshot) -> list[ValidationIssue]:
    issues = []
    entities = snapshot.by_id()
    actions = {action.name: action for action in snapshot.actions}
    rigs = {rig.entity_id: rig for rig in snapshot.rigs}
    has_inventory = snapshot.schema_version in {"1.3", "1.4"}
    for index, beat in enumerate(shot.beats):
        if not isinstance(beat, PlayClipBeat):
            continue
        if not has_inventory:
            if beat.retarget is not None:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="retarget_inventory_unavailable",
                        message="Retargeting requires a Scene Snapshot 1.3+ rig/action inventory.",
                        beat_index=index,
                    )
                )
            continue
        action = actions.get(beat.clip)
        if action is None:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="unknown_action",
                    message=f"Action '{beat.clip}' does not exist in the scene inventory.",
                    beat_index=index,
                )
            )
            continue
        if (
            beat.retarget is not None
            and beat.retarget.adapter == "bake_pose"
            and not action.pose_bones
        ):
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="bake_pose_requires_pose_channels",
                    message="bake_pose requires at least one pose-bone Action channel.",
                    beat_index=index,
                )
            )
            continue
        actor = entities.get(beat.actor)
        needs_rig = bool(action.pose_bones or beat.retarget is not None)
        if actor is None or not needs_rig:
            continue
        if actor.type != "ARMATURE":
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="action_target_not_armature",
                    message=(
                        f"Action '{beat.clip}' has pose-bone channels but actor "
                        f"'{actor.name}' is not an armature."
                    ),
                    beat_index=index,
                )
            )
            continue
        rig = rigs.get(actor.id)
        if rig is None:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="rig_inventory_missing",
                    message=f"Armature '{actor.name}' has no rig inventory.",
                    beat_index=index,
                )
            )
            continue
        analysis = analyze_retarget(action, rig, beat.retarget)
        if analysis.missing_sources:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="retarget_mapping_incomplete",
                    message=(
                        "Strict retarget mapping does not cover action bone(s): "
                        + ", ".join(analysis.missing_sources)
                    ),
                    beat_index=index,
                )
            )
        if analysis.missing_targets:
            code = (
                "retarget_target_bone_missing"
                if beat.retarget is not None
                else "action_incompatible_with_rig"
            )
            issues.append(
                ValidationIssue(
                    severity="error",
                    code=code,
                    message=(
                        f"Target rig '{rig.name}' is missing bone(s): "
                        + ", ".join(analysis.missing_targets)
                    ),
                    beat_index=index,
                )
            )
        if analysis.duplicate_targets:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="retarget_target_collision",
                    message=(
                        "Multiple source bones resolve to target bone(s): "
                        + ", ".join(analysis.duplicate_targets)
                    ),
                    beat_index=index,
                )
            )
        if analysis.unused_sources:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    code="unused_retarget_mapping",
                    message=(
                        "Retarget mapping contains source bone(s) unused by the action: "
                        + ", ".join(analysis.unused_sources)
                        + ". They remain available for hierarchy compatibility checks."
                    ),
                    beat_index=index,
                )
            )
        has_non_pose_channels = any(
            not path.startswith('pose.bones["') for path in action.data_paths
        )
        if beat.retarget is not None and has_non_pose_channels:
            rename_only = beat.retarget.adapter == "rename_only"
            issues.append(
                ValidationIssue(
                    severity="warning",
                    code=(
                        "retarget_preserves_non_pose_channels"
                        if rename_only
                        else "bake_pose_ignores_non_pose_channels"
                    ),
                    message=(
                        f"Action '{beat.clip}' contains non-pose channels; "
                        + (
                            "rename_only preserves them unchanged."
                            if rename_only
                            else "bake_pose omits them. Root motion must be stored on a "
                            "mapped root pose bone to be transferred."
                        )
                    ),
                    beat_index=index,
                )
            )
        if (
            beat.retarget is not None
            and beat.retarget.adapter == "bake_pose"
            and snapshot.schema_version != "1.4"
        ):
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="bake_pose_requires_rig_geometry",
                    message="bake_pose requires a Scene Snapshot 1.4 rig geometry inventory.",
                    beat_index=index,
                )
            )
            continue
        if beat.retarget is not None and snapshot.schema_version == "1.4":
            source_rig, source_error = _resolve_source_rig(action, beat.retarget, rigs)
            if source_rig is None:
                code = (
                    "retarget_source_rig_ambiguous"
                    if source_error == "ambiguous"
                    else "retarget_source_rig_missing"
                )
                message = (
                    "Multiple armatures can own the source Action channels; set "
                    "retarget.source_rig explicitly."
                    if source_error == "ambiguous"
                    else "No armature matches the source Action channels."
                )
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code=code,
                        message=message,
                        beat_index=index,
                    )
                )
                continue
            compatibility = analyze_rig_compatibility(source_rig, rig, beat.retarget)
            has_pose_translation = any(
                path.startswith('pose.bones["') and path.endswith("].location")
                for path in action.data_paths
            )
            translation_scale_mismatch = (
                has_pose_translation
                and compatibility.median_length_ratio is not None
                and abs(compatibility.median_length_ratio - 1.0) > 0.02
            )
            if beat.retarget.adapter == "bake_pose":
                hierarchy_issues = {
                    item.code
                    for item in compatibility.issues
                    if item.code in {"hierarchy_mismatch", "source_parent_unmapped"}
                }
                if compatibility.status == "incompatible":
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            code="bake_pose_geometry_incompatible",
                            message=(
                                f"bake_pose cannot map '{source_rig.name}' to '{rig.name}' "
                                "because the rig mapping is incomplete or invalid."
                            ),
                            beat_index=index,
                        )
                    )
                elif hierarchy_issues:
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            code="bake_pose_hierarchy_unsupported",
                            message=(
                                "bake_pose v1 requires mapped source and target parent "
                                "hierarchies to match; remap or bake through an intermediate "
                                "deform skeleton."
                            ),
                            beat_index=index,
                        )
                    )
                elif compatibility.status in {"review", "bake_required"}:
                    issues.append(
                        ValidationIssue(
                            severity="warning",
                            code="bake_pose_output_review",
                            message=(
                                f"bake_pose will correct local rest orientation and scale "
                                f"from '{source_rig.name}' to '{rig.name}', but the generated "
                                "Action should be reviewed in Blender."
                            ),
                            beat_index=index,
                        )
                    )
            elif compatibility.status in {"incompatible", "bake_required"}:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="rename_only_geometry_incompatible",
                        message=(
                            f"rename_only is not safe for '{source_rig.name}' to '{rig.name}' "
                            f"(status: {compatibility.status}, max rest-axis difference: "
                            f"{compatibility.max_axis_angle_degrees:.2f} degrees)."
                        ),
                        beat_index=index,
                    )
                )
            elif translation_scale_mismatch:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="rename_only_translation_scale_mismatch",
                        message=(
                            f"Action '{action.name}' contains pose-bone location channels, "
                            f"but target/source uniform scale is "
                            f"{compatibility.median_length_ratio:.4f}; translation scaling "
                            "requires pose baking."
                        ),
                        beat_index=index,
                    )
                )
            elif compatibility.status == "review":
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        code="rename_only_geometry_review",
                        message=(
                            f"Rest geometry for '{source_rig.name}' to '{rig.name}' has small "
                            "differences and should be reviewed after apply."
                        ),
                        beat_index=index,
                    )
                )
    return issues


def validate_shot(shot: ShotSpec, snapshot: SceneSnapshot) -> ValidationReport:
    issues: list[ValidationIssue] = []
    entities = snapshot.by_id()
    occupied_channels: dict[tuple[str, str], list[tuple[float, float, int]]] = {}
    for index, beat in enumerate(shot.beats):
        actor = getattr(beat, "actor", None)
        if actor and actor not in entities:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="unknown_actor",
                    message=f"Actor '{actor}' does not exist in the current scene snapshot.",
                    beat_index=index,
                )
            )
        if actor and actor in entities and entities[actor].locked:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="locked_actor",
                    message=f"Actor '{actor}' is locked and cannot be changed.",
                    beat_index=index,
                )
            )
        for field in ("target", "target_entity"):
            target = getattr(beat, field, None)
            if target and target not in entities:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="unknown_target",
                        message=f"Target '{target}' does not exist in the current scene snapshot.",
                        beat_index=index,
                    )
                )
        if beat.at + beat.duration > shot.duration + 1e-6:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="beat_outside_shot",
                    message=f"Beat {index} ends after the shot duration.",
                    beat_index=index,
                )
            )
        if isinstance(beat, (MoveToBeat, TurnToBeat, PlayClipBeat)) and _frame(
            beat.at, shot.fps, snapshot.frame_start
        ) == _frame(beat.at + beat.duration, shot.fps, snapshot.frame_start):
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="beat_shorter_than_frame",
                    message=(
                        f"Beat {index} is too short to occupy a frame at {shot.fps:g} FPS."
                    ),
                    beat_index=index,
                )
            )
        channel = None
        if isinstance(beat, MoveToBeat):
            channel = "location"
        elif isinstance(beat, TurnToBeat):
            channel = "rotation"
        elif isinstance(beat, PlayClipBeat):
            channel = "action"
        if actor and channel and beat.duration > 0:
            key = (actor, channel)
            start, end = beat.at, beat.at + beat.duration
            for previous_start, previous_end, previous_index in occupied_channels.get(key, []):
                if start < previous_end and end > previous_start:
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            code="overlapping_channel",
                            message=(
                                f"Beat {index} overlaps beat {previous_index} on "
                                f"'{actor}' {channel}."
                            ),
                            beat_index=index,
                        )
                    )
            occupied_channels.setdefault(key, []).append((start, end, index))
    if shot.camera and shot.camera.target and shot.camera.target not in entities:
        issues.append(
            ValidationIssue(
                severity="error",
                code="unknown_camera_target",
                message=f"Camera target '{shot.camera.target}' does not exist.",
            )
        )
    if shot.fps != snapshot.fps:
        issues.append(
            ValidationIssue(
                severity="warning",
                code="fps_change",
                message=f"Shot changes scene FPS from {snapshot.fps:g} to {shot.fps:g}.",
            )
        )
    issues.extend(_navigation_issues(shot, snapshot))
    issues.extend(_rig_action_issues(shot, snapshot))
    return ValidationReport(
        valid=not any(issue.severity == "error" for issue in issues), issues=issues
    )


def compile_shot(shot: ShotSpec, snapshot: SceneSnapshot) -> ScenePatch:
    report = validate_shot(shot, snapshot)
    errors = [issue.message for issue in report.issues if issue.severity == "error"]
    if errors:
        raise ValueError("Shot validation failed: " + "; ".join(errors))

    entities = snapshot.by_id()
    current_locations = {
        entity_id: entity.transform.location.model_copy(deep=True)
        for entity_id, entity in entities.items()
    }
    current_rotations = {
        entity_id: entity.transform.rotation_euler.model_copy(deep=True)
        for entity_id, entity in entities.items()
    }
    navigation_fingerprint = navigation_environment_fingerprint(snapshot)
    patch_schema = (
        "1.4"
        if snapshot.schema_version == "1.4"
        else ("1.3" if snapshot.schema_version == "1.3" else "1.2")
    )
    actions_by_name = (
        {action.name: action for action in snapshot.actions}
        if snapshot.schema_version in {"1.3", "1.4"}
        else {}
    )
    rigs_by_id = {rig.entity_id: rig for rig in snapshot.rigs}
    action_fingerprints: dict[str, str] = {}
    rig_fingerprints: dict[str, str] = {}
    fingerprint_entities: set[str] = set()
    operations: list[PatchOperation] = [
        PatchOperation(
            op="set_frame_range",
            payload={
                "fps": shot.fps,
                "frame_start": snapshot.frame_start,
                "frame_end": _frame(shot.duration, shot.fps, snapshot.frame_start),
            },
        )
    ]

    indexed_beats = sorted(enumerate(shot.beats), key=lambda item: (item[1].at, item[0]))
    for _, beat in indexed_beats:
        start = _frame(beat.at, shot.fps, snapshot.frame_start)
        end = _frame(beat.at + beat.duration, shot.fps, snapshot.frame_start)
        if isinstance(beat, MoveToBeat):
            fingerprint_entities.add(beat.actor)
            if beat.target_entity:
                fingerprint_entities.add(beat.target_entity)
            origin = current_locations[beat.actor]
            target = _target_position(
                beat.target_entity,
                beat.target_position,
                current_locations,
            )
            assert target is not None
            plan = plan_move_path(
                snapshot,
                entities[beat.actor],
                origin,
                target,
                path_mode=beat.path_mode,
                navigation_mesh=beat.navigation_mesh,
                clearance=beat.clearance,
                target_entity_id=beat.target_entity,
            )
            path_frames = allocate_path_frames(plan.points, start, end)
            fingerprint_entities.update(plan.considered_obstacle_ids)
            if plan.navigation_mesh_id:
                fingerprint_entities.add(plan.navigation_mesh_id)
            operations.append(
                PatchOperation(
                    op="keyframe_transform",
                    entity_id=beat.actor,
                    payload={
                        "frames": [
                            {"frame": frame, "location": point.as_list()}
                            for frame, point in zip(path_frames, plan.points, strict=True)
                        ],
                        "interpolation": (
                            "LINEAR" if beat.path_mode == "navmesh" else beat.easing
                        ),
                        "space": "WORLD",
                        "path_mode": beat.path_mode,
                        "navigation_mesh": plan.navigation_mesh_id,
                    },
                )
            )
            current_locations[beat.actor] = target.model_copy(deep=True)
        elif isinstance(beat, TurnToBeat):
            fingerprint_entities.add(beat.actor)
            if beat.target_entity:
                fingerprint_entities.add(beat.target_entity)
            target = _target_position(
                beat.target_entity,
                beat.target_position,
                current_locations,
            )
            assert target is not None
            origin = current_locations[beat.actor]
            yaw = math.atan2(-(target.x - origin.x), target.y - origin.y)
            start_rotation = current_rotations[beat.actor].as_list()
            end_rotation = [start_rotation[0], start_rotation[1], yaw]
            operations.append(
                PatchOperation(
                    op="keyframe_transform",
                    entity_id=beat.actor,
                    payload={
                        "frames": [
                            {"frame": start, "rotation_euler": start_rotation},
                            {"frame": end, "rotation_euler": end_rotation},
                        ],
                        "interpolation": beat.easing,
                        "space": "WORLD",
                    },
                )
            )
            current_rotations[beat.actor] = Vec3(
                x=end_rotation[0], y=end_rotation[1], z=end_rotation[2]
            )
        elif isinstance(beat, LookAtBeat):
            fingerprint_entities.update((beat.actor, beat.target))
            operations.append(
                PatchOperation(
                    op="look_at",
                    entity_id=beat.actor,
                    payload={"target_id": beat.target},
                )
            )
        elif isinstance(beat, PlayClipBeat):
            fingerprint_entities.add(beat.actor)
            action = actions_by_name.get(beat.clip)
            if action is not None:
                action_fingerprints[beat.clip] = action.fingerprint
                target_rig = rigs_by_id.get(beat.actor)
                if target_rig is not None and target_rig.fingerprint is not None:
                    rig_fingerprints[target_rig.entity_id] = target_rig.fingerprint
            payload = {
                "clip": beat.clip,
                "frame_start": start,
                "frame_end": end,
                "loop": beat.loop,
            }
            if beat.retarget is not None:
                retarget_payload = beat.retarget.model_dump(mode="json", exclude_none=True)
                if action is not None and snapshot.schema_version == "1.4":
                    source_rig, _ = _resolve_source_rig(action, beat.retarget, rigs_by_id)
                    if source_rig is not None:
                        retarget_payload["source_rig"] = source_rig.entity_id
                        fingerprint_entities.add(source_rig.entity_id)
                        if source_rig.fingerprint is not None:
                            rig_fingerprints[source_rig.entity_id] = source_rig.fingerprint
                payload["retarget"] = retarget_payload
            operations.append(
                PatchOperation(
                    op="play_clip",
                    entity_id=beat.actor,
                    payload=payload,
                )
            )

    if shot.camera:
        if shot.camera.target:
            fingerprint_entities.add(shot.camera.target)
        existing_camera = next(
            (entity for entity in entities.values() if entity.name == shot.camera.name), None
        )
        if existing_camera:
            fingerprint_entities.add(existing_camera.id)
        payload = shot.camera.model_dump(exclude_none=True)
        payload["space"] = "WORLD"
        payload["frame_start"] = snapshot.frame_start
        payload["frame_end"] = _frame(shot.duration, shot.fps, snapshot.frame_start)
        operations.append(PatchOperation(op="ensure_camera", payload=payload))

    fingerprint_ids = sorted(fingerprint_entities)
    scene_fingerprint = fingerprint_snapshot(snapshot, fingerprint_ids)
    canonical = json.dumps(
        {
            "shot": shot.model_dump(mode="json", exclude_none=True),
            "operations": [operation.model_dump(mode="json") for operation in operations],
            "scene_fingerprint": scene_fingerprint,
            "fingerprint_entities": fingerprint_ids,
            "fingerprint_frame": snapshot.frame_current,
            "navigation_environment_fingerprint": navigation_fingerprint,
            "action_fingerprints": action_fingerprints,
            "rig_fingerprints": rig_fingerprints,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    patch_id = "patch-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    warnings = [issue.message for issue in report.issues if issue.severity == "warning"]
    return ScenePatch(
        schema_version=patch_schema,
        patch_id=patch_id,
        source_title=shot.title,
        operations=operations,
        warnings=warnings,
        scene_fingerprint=scene_fingerprint,
        fingerprint_entities=fingerprint_ids,
        fingerprint_frame=snapshot.frame_current,
        navigation_environment_fingerprint=navigation_fingerprint,
        action_fingerprints=dict(sorted(action_fingerprints.items())),
        rig_fingerprints=dict(sorted(rig_fingerprints.items())),
    )
