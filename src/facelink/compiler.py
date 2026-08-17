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
    ScenePatch,
    SceneSnapshot,
    ShotSpec,
    TurnToBeat,
    ValidationIssue,
    ValidationReport,
    Vec3,
)


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
            operations.append(
                PatchOperation(
                    op="keyframe_transform",
                    entity_id=beat.actor,
                    payload={
                        "frames": [
                            {"frame": start, "location": origin.as_list()},
                            {"frame": end, "location": target.as_list()},
                        ],
                        "interpolation": beat.easing,
                        "space": "WORLD",
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
            operations.append(
                PatchOperation(
                    op="play_clip",
                    entity_id=beat.actor,
                    payload={
                        "clip": beat.clip,
                        "frame_start": start,
                        "frame_end": end,
                        "loop": beat.loop,
                    },
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
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    patch_id = "patch-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    warnings = [issue.message for issue in report.issues if issue.severity == "warning"]
    return ScenePatch(
        patch_id=patch_id,
        source_title=shot.title,
        operations=operations,
        warnings=warnings,
        scene_fingerprint=scene_fingerprint,
        fingerprint_entities=fingerprint_ids,
        fingerprint_frame=snapshot.frame_current,
    )
