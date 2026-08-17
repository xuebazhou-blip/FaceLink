import pytest
from pydantic import ValidationError

from facelink.compiler import compile_shot, validate_shot
from facelink.models import (
    ActionInventory,
    ActionRetargetSpec,
    RetargetProfile,
    RigInventory,
    ScenePatch,
    SceneSnapshot,
    ShotSpec,
)
from facelink.retargeting import analyze_retarget


def rig_action_snapshot():
    return SceneSnapshot.model_validate(
        {
            "schema_version": "1.3",
            "scene_name": "Retarget",
            "entities": [
                {
                    "id": "source-rig",
                    "name": "Source Rig",
                    "type": "ARMATURE",
                    "transform": {},
                },
                {
                    "id": "target-rig",
                    "name": "Target Rig",
                    "type": "ARMATURE",
                    "transform": {},
                },
                {
                    "id": "mesh",
                    "name": "Not A Rig",
                    "type": "MESH",
                    "transform": {},
                },
            ],
            "rigs": [
                {
                    "entity_id": "source-rig",
                    "name": "Source Rig",
                    "bones": [
                        {
                            "name": "mixamorig:Hips",
                            "head": {},
                            "tail": {"z": 1},
                        },
                        {
                            "name": "mixamorig:Spine",
                            "parent": "mixamorig:Hips",
                            "head": {"z": 1},
                            "tail": {"z": 2},
                        },
                    ],
                },
                {
                    "entity_id": "target-rig",
                    "name": "Target Rig",
                    "bones": [
                        {"name": "pelvis", "head": {}, "tail": {"z": 1}},
                        {
                            "name": "spine",
                            "parent": "pelvis",
                            "head": {"z": 1},
                            "tail": {"z": 2},
                        },
                    ],
                },
            ],
            "actions": [
                {
                    "name": "Source Walk",
                    "frame_start": 1,
                    "frame_end": 24,
                    "fcurve_count": 3,
                    "keyframe_count": 6,
                    "pose_bones": ["mixamorig:Hips", "mixamorig:Spine"],
                    "data_paths": [
                        "location",
                        'pose.bones["mixamorig:Hips"].rotation_euler',
                        'pose.bones["mixamorig:Spine"].rotation_euler',
                    ],
                    "fingerprint": "action-111111111111111111111111",
                }
            ],
        }
    )


def retarget_spec(**updates):
    payload = {
        "adapter": "rename_only",
        "bone_map": {
            "mixamorig:Hips": "pelvis",
            "mixamorig:Spine": "spine",
        },
        "strict": True,
    }
    payload.update(updates)
    return ActionRetargetSpec.model_validate(payload)


def test_retarget_analysis_resolves_explicit_and_identity_mappings():
    snapshot = rig_action_snapshot()
    action = snapshot.actions[0]
    target = snapshot.rigs[1]
    explicit = analyze_retarget(action, target, retarget_spec())
    assert explicit.compatible is True
    assert explicit.resolved_map == {
        "mixamorig:Hips": "pelvis",
        "mixamorig:Spine": "spine",
    }

    source = analyze_retarget(action, snapshot.rigs[0], None)
    assert source.compatible is True
    assert source.resolved_map == {
        "mixamorig:Hips": "mixamorig:Hips",
        "mixamorig:Spine": "mixamorig:Spine",
    }


def test_retarget_analysis_reports_incomplete_missing_unused_and_colliding_targets():
    snapshot = rig_action_snapshot()
    action = snapshot.actions[0]
    target = snapshot.rigs[1]
    incomplete = analyze_retarget(
        action,
        target,
        ActionRetargetSpec(bone_map={"mixamorig:Hips": "pelvis"}),
    )
    assert incomplete.missing_sources == ("mixamorig:Spine",)

    missing = analyze_retarget(
        action,
        target,
        ActionRetargetSpec(
            bone_map={
                "mixamorig:Hips": "missing",
                "mixamorig:Spine": "spine",
            }
        ),
    )
    assert missing.missing_targets == ("missing",)

    fallback_collision = analyze_retarget(
        action,
        target,
        ActionRetargetSpec(
            bone_map={"mixamorig:Hips": "mixamorig:Spine"},
            strict=False,
        ),
    )
    assert fallback_collision.duplicate_targets == ("mixamorig:Spine",)

    unused = analyze_retarget(
        action,
        target,
        ActionRetargetSpec(
            bone_map={
                "mixamorig:Hips": "pelvis",
                "mixamorig:Spine": "spine",
                "unused": "head",
            }
        ),
    )
    assert unused.unused_sources == ("unused",)


def test_retarget_models_reject_invalid_hierarchies_maps_and_fingerprints():
    with pytest.raises(ValidationError, match="missing parent"):
        RigInventory.model_validate(
            {
                "entity_id": "rig",
                "name": "Rig",
                "bones": [
                    {
                        "name": "child",
                        "parent": "missing",
                        "head": {},
                        "tail": {"z": 1},
                    }
                ],
            }
        )
    with pytest.raises(ValidationError, match="contains a cycle"):
        RigInventory.model_validate(
            {
                "entity_id": "rig",
                "name": "Cyclic Rig",
                "bones": [
                    {"name": "a", "parent": "b", "head": {}, "tail": {"z": 1}},
                    {"name": "b", "parent": "a", "head": {}, "tail": {"z": 1}},
                ],
            }
        )
    with pytest.raises(ValidationError, match="target bones must be unique"):
        ActionRetargetSpec(bone_map={"a": "same", "b": "same"})
    with pytest.raises(ValidationError):
        ActionInventory.model_validate(
            {
                "name": "Bad",
                "frame_start": 1,
                "frame_end": 2,
                "fcurve_count": 0,
                "keyframe_count": 0,
                "fingerprint": "not-a-fingerprint",
            }
        )
    profile = RetargetProfile(
        name="Mixamo to compact",
        bone_map={"mixamorig:Hips": "pelvis"},
    )
    assert profile.schema_version == "1.0"
    assert profile.adapter == "rename_only"


def test_scene_patch_rejects_non_hex_action_fingerprint():
    with pytest.raises(ValidationError, match="invalid name or fingerprint"):
        ScenePatch(
            patch_id="bad-fingerprint",
            source_title="Bad fingerprint",
            operations=[],
            action_fingerprints={"Walk": "action-zzzzzzzzzzzzzzzzzzzzzzzz"},
        )


def test_compiler_emits_guarded_retarget_patch_and_explainable_warnings():
    snapshot = rig_action_snapshot()
    shot = ShotSpec.model_validate(
        {
            "title": "Retarget walk",
            "duration": 2,
            "beats": [
                {
                    "type": "play_clip",
                    "actor": "target-rig",
                    "clip": "Source Walk",
                    "duration": 2,
                    "retarget": retarget_spec().model_dump(mode="json"),
                }
            ],
        }
    )
    report = validate_shot(shot, snapshot)
    assert report.valid is True
    assert [issue.code for issue in report.issues] == [
        "retarget_preserves_non_pose_channels"
    ]
    patch = compile_shot(shot, snapshot)
    assert patch.schema_version == "1.3"
    assert patch.action_fingerprints == {
        "Source Walk": "action-111111111111111111111111"
    }
    operation = patch.operations[1]
    assert operation.payload["retarget"]["adapter"] == "rename_only"
    assert operation.payload["retarget"]["bone_map"]["mixamorig:Hips"] == "pelvis"
    assert "non-pose channels" in patch.warnings[0]


def test_compiler_accepts_identity_rig_and_rejects_unmapped_incompatible_rig():
    snapshot = rig_action_snapshot()
    shot = ShotSpec.model_validate(
        {
            "duration": 1,
            "beats": [
                {
                    "type": "play_clip",
                    "actor": "source-rig",
                    "clip": "Source Walk",
                    "duration": 1,
                }
            ],
        }
    )
    assert validate_shot(shot, snapshot).valid is True
    patch = compile_shot(shot, snapshot)
    assert patch.action_fingerprints == {
        "Source Walk": "action-111111111111111111111111"
    }
    assert "retarget" not in patch.operations[1].payload

    shot.beats[0].actor = "target-rig"
    report = validate_shot(shot, snapshot)
    assert report.valid is False
    assert "action_incompatible_with_rig" in {issue.code for issue in report.issues}


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda shot, snapshot: setattr(shot.beats[0], "clip", "Missing"), "unknown_action"),
        (
            lambda shot, snapshot: setattr(shot.beats[0], "actor", "mesh"),
            "action_target_not_armature",
        ),
        (
            lambda shot, snapshot: setattr(
                shot.beats[0],
                "retarget",
                ActionRetargetSpec(bone_map={"mixamorig:Hips": "pelvis"}),
            ),
            "retarget_mapping_incomplete",
        ),
    ],
)
def test_compiler_retarget_failures_are_typed(mutate, code):
    snapshot = rig_action_snapshot()
    shot = ShotSpec.model_validate(
        {
            "duration": 2,
            "beats": [
                {
                    "type": "play_clip",
                    "actor": "target-rig",
                    "clip": "Source Walk",
                    "duration": 2,
                    "retarget": retarget_spec().model_dump(mode="json"),
                }
            ],
        }
    )
    mutate(shot, snapshot)
    report = validate_shot(shot, snapshot)
    assert report.valid is False
    assert code in {issue.code for issue in report.issues}
    with pytest.raises(ValueError, match="Shot validation failed"):
        compile_shot(shot, snapshot)


def test_legacy_snapshot_allows_direct_clip_but_rejects_retargeting():
    snapshot = rig_action_snapshot()
    snapshot.schema_version = "1.2"
    shot = ShotSpec.model_validate(
        {
            "duration": 1,
            "beats": [
                {
                    "type": "play_clip",
                    "actor": "target-rig",
                    "clip": "unverified legacy clip",
                    "duration": 1,
                }
            ],
        }
    )
    assert validate_shot(shot, snapshot).valid is True
    shot.beats[0].retarget = retarget_spec()
    report = validate_shot(shot, snapshot)
    assert report.valid is False
    assert report.issues[-1].code == "retarget_inventory_unavailable"
