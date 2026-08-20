import math

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
from facelink.retargeting import (
    analyze_retarget,
    analyze_rig_compatibility,
    suggest_retarget_profile,
)


def rig_action_snapshot():
    return SceneSnapshot.model_validate(
        {
            "schema_version": "1.4",
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
                    "fingerprint": "rig-111111111111111111111111",
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
                    "fingerprint": "rig-222222222222222222222222",
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


def bake_spec(**updates):
    payload = {
        "adapter": "bake_pose",
        "bone_map": {
            "mixamorig:Hips": "pelvis",
            "mixamorig:Spine": "spine",
        },
        "strict": True,
        "source_rig": "source-rig",
    }
    payload.update(updates)
    return ActionRetargetSpec.model_validate(payload)


def evaluated_bake_spec(**updates):
    payload = {
        "adapter": "bake_evaluated_pose",
        "bone_map": {
            "mixamorig:Hips": "pelvis",
            "mixamorig:Spine": "spine",
        },
        "strict": True,
        "source_rig": "source-rig",
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
    with pytest.raises(ValidationError, match="explicit source_rig"):
        ActionRetargetSpec(adapter="bake_pose", bone_map={"a": "b"})
    with pytest.raises(ValidationError, match="only supported by bake_pose"):
        ActionRetargetSpec(
            adapter="rename_only",
            bone_map={"a": "b"},
            sample_step=2,
        )
    with pytest.raises(ValidationError):
        ActionRetargetSpec(
            adapter="bake_pose",
            source_rig="source",
            bone_map={"a": "b"},
            sample_step=0,
        )
    with pytest.raises(ValidationError, match="strict=true"):
        ActionRetargetSpec(
            adapter="bake_pose",
            source_rig="source",
            bone_map={"a": "b"},
            strict=False,
        )
    baked = ActionRetargetSpec(
        adapter="bake_pose",
        source_rig="source",
        bone_map={"a": "b"},
        sample_step=2,
        root_motion="drop",
    )
    assert baked.adapter == "bake_pose"
    assert baked.sample_step == 2
    assert baked.root_motion == "drop"
    evaluated = evaluated_bake_spec(sample_step=3, root_motion="preserve")
    assert evaluated.adapter == "bake_evaluated_pose"
    assert evaluated.source_rig == "source-rig"
    object_bake = evaluated_bake_spec(object_motion="scale")
    assert object_bake.object_motion == "scale"
    with pytest.raises(ValidationError):
        evaluated_bake_spec(object_motion="world")
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
    with pytest.raises(ValidationError, match="quaternion must be normalized"):
        RigInventory.model_validate(
            {
                "entity_id": "rig",
                "name": "Bad rotation",
                "bones": [
                    {
                        "name": "root",
                        "head": {},
                        "tail": {"z": 1},
                        "rest_rotation": {"w": 2, "x": 0, "y": 0, "z": 0},
                    }
                ],
            }
        )
    with pytest.raises(ValidationError, match="requires a fingerprint for every rig"):
        SceneSnapshot.model_validate(
            {
                "schema_version": "1.4",
                "scene_name": "Missing rig guard",
                "entities": [
                    {"id": "rig", "name": "Rig", "type": "ARMATURE", "transform": {}}
                ],
                "rigs": [{"entity_id": "rig", "name": "Rig", "bones": []}],
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
    with pytest.raises(ValidationError, match="invalid entity ID or fingerprint"):
        ScenePatch(
            patch_id="bad-rig-fingerprint",
            source_title="Bad rig fingerprint",
            operations=[],
            rig_fingerprints={"rig": "rig-ZZZZZZZZZZZZZZZZZZZZZZZZ"},
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
    assert patch.schema_version == "1.4"
    assert patch.action_fingerprints == {
        "Source Walk": "action-111111111111111111111111"
    }
    operation = patch.operations[1]
    assert operation.payload["retarget"]["adapter"] == "rename_only"
    assert operation.payload["retarget"]["source_rig"] == "source-rig"
    assert operation.payload["retarget"]["bone_map"]["mixamorig:Hips"] == "pelvis"
    assert patch.rig_fingerprints == {
        "source-rig": "rig-111111111111111111111111",
        "target-rig": "rig-222222222222222222222222",
    }
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
    assert patch.rig_fingerprints == {
        "source-rig": "rig-111111111111111111111111"
    }
    assert "retarget" not in patch.operations[1].payload

    shot.beats[0].actor = "target-rig"
    report = validate_shot(shot, snapshot)
    assert report.valid is False
    assert "action_incompatible_with_rig" in {issue.code for issue in report.issues}


def test_rig_geometry_report_distinguishes_safe_review_bake_and_incompatible():
    snapshot = rig_action_snapshot()
    source = snapshot.rigs[0]
    target = snapshot.rigs[1]
    spec = retarget_spec(source_rig="source-rig")
    safe = analyze_rig_compatibility(source, target, spec)
    assert safe.status == "safe"
    assert safe.rename_only_safe is True
    assert safe.median_length_ratio == 1.0

    target.bones[1].rest_rotation.w = math.cos(math.radians(1.5))
    target.bones[1].rest_rotation.x = math.sin(math.radians(1.5))
    review = analyze_rig_compatibility(source, target, spec)
    assert review.status == "review"
    assert review.max_rest_rotation_angle_degrees == pytest.approx(3.0)

    target.bones[1].rest_rotation.w = math.cos(math.radians(10.0))
    target.bones[1].rest_rotation.x = math.sin(math.radians(10.0))
    bake = analyze_rig_compatibility(source, target, spec)
    assert bake.status == "bake_required"
    assert "rest_rotation_difference" in {issue.code for issue in bake.issues}

    missing = analyze_rig_compatibility(
        source,
        target,
        ActionRetargetSpec(
            bone_map={
                "mixamorig:Hips": "missing",
                "mixamorig:Spine": "spine",
            }
        ),
    )
    assert missing.status == "incompatible"
    assert "target_bone_missing" in {issue.code for issue in missing.issues}


def test_profile_suggestion_uses_bounded_deterministic_matches_and_exposes_conflicts():
    source = RigInventory.model_validate(
        {
            "entity_id": "source",
            "name": "Source",
            "bones": [
                {"name": "mixamorig:Hips", "head": {}, "tail": {"z": 1}},
                {
                    "name": "mixamorig:Spine",
                    "parent": "mixamorig:Hips",
                    "head": {},
                    "tail": {"z": 1},
                },
                {
                    "name": "mixamorig:LeftArm",
                    "parent": "mixamorig:Spine",
                    "head": {},
                    "tail": {"z": 1},
                },
            ],
        }
    )
    target = RigInventory.model_validate(
        {
            "entity_id": "target",
            "name": "Target",
            "bones": [
                {"name": "pelvis", "head": {}, "tail": {"z": 1}},
                {
                    "name": "Spine",
                    "parent": "pelvis",
                    "head": {},
                    "tail": {"z": 1},
                },
                {
                    "name": "upper_arm.L",
                    "parent": "Spine",
                    "head": {},
                    "tail": {"z": 1},
                },
            ],
        }
    )
    suggestion = suggest_retarget_profile(source, target, name="Suggested")
    assert suggestion.review_required is True
    assert suggestion.profile is not None
    assert suggestion.profile.source_rig == "source"
    assert suggestion.profile.bone_map == {
        "mixamorig:Hips": "pelvis",
        "mixamorig:LeftArm": "upper_arm.L",
        "mixamorig:Spine": "Spine",
    }
    assert {match.method for match in suggestion.matches} == {"normalized", "alias"}
    assert suggestion.unmapped_sources == []

    action_limited = suggest_retarget_profile(
        source,
        target,
        name="Action limited",
        source_bones={"mixamorig:LeftArm"},
    )
    assert action_limited.profile is not None
    assert set(action_limited.profile.bone_map) == {
        "mixamorig:Hips",
        "mixamorig:Spine",
        "mixamorig:LeftArm",
    }

    ambiguous_target = target.model_copy(deep=True)
    ambiguous_target.bones.append(
        ambiguous_target.bones[0].model_copy(update={"name": "hip"})
    )
    ambiguous = suggest_retarget_profile(
        source,
        ambiguous_target,
        name="Ambiguous",
        source_bones={"mixamorig:Hips"},
    )
    assert ambiguous.profile is None
    assert ambiguous.conflicts == {"mixamorig:Hips": ["hip", "pelvis"]}
    assert ambiguous.unmapped_sources == ["mixamorig:Hips"]


def test_compiler_blocks_rename_only_when_rest_geometry_requires_baking():
    snapshot = rig_action_snapshot()
    snapshot.rigs[1].bones[1].rest_rotation.w = math.cos(math.radians(10.0))
    snapshot.rigs[1].bones[1].rest_rotation.x = math.sin(math.radians(10.0))
    shot = ShotSpec.model_validate(
        {
            "duration": 1,
            "beats": [
                {
                    "type": "play_clip",
                    "actor": "target-rig",
                    "clip": "Source Walk",
                    "duration": 1,
                    "retarget": retarget_spec().model_dump(mode="json"),
                }
            ],
        }
    )
    report = validate_shot(shot, snapshot)
    assert report.valid is False
    assert "rename_only_geometry_incompatible" in {
        issue.code for issue in report.issues
    }


def test_compiler_blocks_unscaled_pose_translation_on_uniformly_scaled_rig():
    snapshot = rig_action_snapshot()
    snapshot.actions[0].data_paths.append(
        'pose.bones["mixamorig:Hips"].location'
    )
    snapshot.rigs[1].bones[0].tail.z = 2
    snapshot.rigs[1].bones[1].head.z = 2
    snapshot.rigs[1].bones[1].tail.z = 4
    shot = ShotSpec.model_validate(
        {
            "duration": 1,
            "beats": [
                {
                    "type": "play_clip",
                    "actor": "target-rig",
                    "clip": "Source Walk",
                    "duration": 1,
                    "retarget": retarget_spec().model_dump(mode="json"),
                }
            ],
        }
    )
    report = validate_shot(shot, snapshot)
    assert report.valid is False
    assert "rename_only_translation_scale_mismatch" in {
        issue.code for issue in report.issues
    }


def test_compiler_accepts_bake_pose_for_rest_axis_and_scale_differences():
    snapshot = rig_action_snapshot()
    snapshot.actions[0].data_paths.append(
        'pose.bones["mixamorig:Hips"].location'
    )
    snapshot.rigs[1].bones[0].tail.z = 2
    snapshot.rigs[1].bones[1].head.z = 2
    snapshot.rigs[1].bones[1].tail.z = 4
    snapshot.rigs[1].bones[1].rest_rotation.w = math.cos(math.radians(10.0))
    snapshot.rigs[1].bones[1].rest_rotation.x = math.sin(math.radians(10.0))
    shot = ShotSpec.model_validate(
        {
            "duration": 1,
            "beats": [
                {
                    "type": "play_clip",
                    "actor": "target-rig",
                    "clip": "Source Walk",
                    "duration": 1,
                    "retarget": bake_spec(
                        sample_step=2,
                        root_motion="scale",
                    ).model_dump(mode="json", exclude_none=True),
                }
            ],
        }
    )
    report = validate_shot(shot, snapshot)
    assert report.valid is True
    assert {issue.code for issue in report.issues} == {
        "bake_pose_ignores_non_pose_channels",
        "bake_pose_output_review",
    }
    patch = compile_shot(shot, snapshot)
    retarget = patch.operations[1].payload["retarget"]
    assert retarget == {
        "adapter": "bake_pose",
        "bone_map": {
            "mixamorig:Hips": "pelvis",
            "mixamorig:Spine": "spine",
        },
        "strict": True,
        "source_rig": "source-rig",
        "sample_step": 2,
        "root_motion": "scale",
    }
    assert patch.rig_fingerprints == {
        "source-rig": "rig-111111111111111111111111",
        "target-rig": "rig-222222222222222222222222",
    }


def test_compiler_evaluated_bake_maps_deform_bones_not_controller_channels():
    snapshot = rig_action_snapshot()
    snapshot.rigs[0].bones.append(
        snapshot.rigs[0].bones[0].model_copy(
            deep=True,
            update={"name": "ctrl", "parent": None, "use_deform": False},
        )
    )
    action = snapshot.actions[0]
    action.pose_bones = ["ctrl"]
    action.data_paths = ['pose.bones["ctrl"]["drive"]', "location"]
    action.fcurve_count = 2
    action.keyframe_count = 4
    shot = ShotSpec.model_validate(
        {
            "duration": 1,
            "beats": [
                {
                    "type": "play_clip",
                    "actor": "target-rig",
                    "clip": "Source Walk",
                    "duration": 1,
                    "retarget": evaluated_bake_spec(
                        sample_step=2,
                        root_motion="drop",
                        object_motion="preserve",
                    ).model_dump(mode="json", exclude_none=True),
                }
            ],
        }
    )
    report = validate_shot(shot, snapshot)
    assert report.valid is True
    assert "retarget_mapping_incomplete" not in {
        issue.code for issue in report.issues
    }
    assert "unused_retarget_mapping" not in {issue.code for issue in report.issues}
    patch = compile_shot(shot, snapshot)
    assert patch.operations[1].payload["retarget"] == {
        "adapter": "bake_evaluated_pose",
        "bone_map": {
            "mixamorig:Hips": "pelvis",
            "mixamorig:Spine": "spine",
        },
        "strict": True,
        "source_rig": "source-rig",
        "sample_step": 2,
        "root_motion": "drop",
        "object_motion": "preserve",
    }
    assert patch.rig_fingerprints == {
        "source-rig": "rig-111111111111111111111111",
        "target-rig": "rig-222222222222222222222222",
    }


def test_compiler_accepts_object_only_bake_and_rejects_missing_object_channels():
    snapshot = rig_action_snapshot()
    action = snapshot.actions[0]
    action.pose_bones = []
    action.data_paths = ["location", "rotation_euler"]
    action.fcurve_count = 6
    action.keyframe_count = 12
    shot = ShotSpec.model_validate(
        {
            "duration": 1,
            "beats": [
                {
                    "type": "play_clip",
                    "actor": "target-rig",
                    "clip": action.name,
                    "duration": 1,
                    "retarget": bake_spec(
                        object_motion="scale"
                    ).model_dump(mode="json", exclude_none=True),
                }
            ],
        }
    )
    report = validate_shot(shot, snapshot)
    assert report.valid is True
    assert "bake_pose_requires_pose_channels" not in {
        issue.code for issue in report.issues
    }
    assert compile_shot(shot, snapshot).operations[1].payload["retarget"][
        "object_motion"
    ] == "scale"

    action.data_paths = ['pose.bones["mixamorig:Hips"]["control"]']
    action.pose_bones = ["mixamorig:Hips"]
    missing = validate_shot(shot, snapshot)
    assert missing.valid is False
    assert "object_motion_requires_object_transform_channels" in {
        issue.code for issue in missing.issues
    }


def test_compiler_bake_pose_rejects_hierarchy_mismatch_and_old_inventory():
    snapshot = rig_action_snapshot()
    snapshot.rigs[1].bones[1].parent = None
    shot = ShotSpec.model_validate(
        {
            "duration": 1,
            "beats": [
                {
                    "type": "play_clip",
                    "actor": "target-rig",
                    "clip": "Source Walk",
                    "duration": 1,
                    "retarget": bake_spec().model_dump(mode="json", exclude_none=True),
                }
            ],
        }
    )
    mismatch = validate_shot(shot, snapshot)
    assert mismatch.valid is False
    assert "bake_pose_hierarchy_unsupported" in {
        issue.code for issue in mismatch.issues
    }

    old_payload = rig_action_snapshot().model_dump(mode="json")
    old_payload["schema_version"] = "1.3"
    for rig in old_payload["rigs"]:
        rig.pop("fingerprint", None)
    old_snapshot = SceneSnapshot.model_validate(old_payload)
    old_report = validate_shot(shot, old_snapshot)
    assert old_report.valid is False
    assert "bake_pose_requires_rig_geometry" in {
        issue.code for issue in old_report.issues
    }


def test_compiler_requires_explicit_source_rig_when_action_owner_is_ambiguous():
    snapshot = rig_action_snapshot()
    duplicate = snapshot.rigs[0].model_copy(
        deep=True,
        update={
            "entity_id": "source-rig-copy",
            "name": "Source Rig Copy",
            "fingerprint": "rig-333333333333333333333333",
        },
    )
    snapshot.rigs.append(duplicate)
    snapshot.entities.append(
        snapshot.entities[0].model_copy(
            deep=True,
            update={"id": "source-rig-copy", "name": "Source Rig Copy"},
        )
    )
    shot = ShotSpec.model_validate(
        {
            "duration": 1,
            "beats": [
                {
                    "type": "play_clip",
                    "actor": "target-rig",
                    "clip": "Source Walk",
                    "duration": 1,
                    "retarget": retarget_spec().model_dump(mode="json"),
                }
            ],
        }
    )
    ambiguous = validate_shot(shot, snapshot)
    assert ambiguous.valid is False
    assert "retarget_source_rig_ambiguous" in {
        issue.code for issue in ambiguous.issues
    }

    shot.beats[0].retarget.source_rig = "source-rig"
    assert validate_shot(shot, snapshot).valid is True
    shot.beats[0].retarget.source_rig = "missing"
    missing = validate_shot(shot, snapshot)
    assert missing.valid is False
    assert "retarget_source_rig_missing" in {issue.code for issue in missing.issues}


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
