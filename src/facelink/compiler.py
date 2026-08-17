from __future__ import annotations

import hashlib
import math

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
    target_entity: str | None, target_position: Vec3 | None, snapshot: SceneSnapshot
) -> Vec3 | None:
    if target_position is not None:
        return target_position
    if target_entity is not None and target_entity in snapshot.by_id():
        return snapshot.by_id()[target_entity].transform.location
    return None


def validate_shot(shot: ShotSpec, snapshot: SceneSnapshot) -> ValidationReport:
    issues: list[ValidationIssue] = []
    entities = snapshot.by_id()
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

    for beat in shot.beats:
        start = _frame(beat.at, shot.fps, snapshot.frame_start)
        end = _frame(beat.at + beat.duration, shot.fps, snapshot.frame_start)
        if isinstance(beat, MoveToBeat):
            actor = entities[beat.actor]
            target = _target_position(beat.target_entity, beat.target_position, snapshot)
            assert target is not None
            operations.append(
                PatchOperation(
                    op="keyframe_transform",
                    entity_id=beat.actor,
                    payload={
                        "frames": [
                            {"frame": start, "location": actor.transform.location.as_list()},
                            {"frame": end, "location": target.as_list()},
                        ],
                        "interpolation": beat.easing,
                    },
                )
            )
        elif isinstance(beat, TurnToBeat):
            actor = entities[beat.actor]
            target = _target_position(beat.target_entity, beat.target_position, snapshot)
            assert target is not None
            origin = actor.transform.location
            yaw = math.atan2(-(target.x - origin.x), target.y - origin.y)
            start_rotation = actor.transform.rotation_euler.as_list()
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
                    },
                )
            )
        elif isinstance(beat, LookAtBeat):
            operations.append(
                PatchOperation(
                    op="look_at",
                    entity_id=beat.actor,
                    payload={"target_id": beat.target},
                )
            )
        elif isinstance(beat, PlayClipBeat):
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
        payload = shot.camera.model_dump(exclude_none=True)
        payload["frame_start"] = snapshot.frame_start
        payload["frame_end"] = _frame(shot.duration, shot.fps, snapshot.frame_start)
        operations.append(PatchOperation(op="ensure_camera", payload=payload))

    canonical = shot.model_dump_json(exclude_none=True)
    patch_id = "patch-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    warnings = [issue.message for issue in report.issues if issue.severity == "warning"]
    return ScenePatch(
        patch_id=patch_id,
        source_title=shot.title,
        operations=operations,
        warnings=warnings,
    )

