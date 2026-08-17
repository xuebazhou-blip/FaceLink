import pytest
from pydantic import ValidationError

from facelink.models import (
    CameraCompositionSpec,
    CameraSpec,
    ExecutionReceipt,
    ScenePatch,
    ShotSpec,
    Vec3,
)


@pytest.mark.parametrize("beat_type", ["move_to", "turn_to"])
def test_transform_beats_require_exactly_one_target(beat_type):
    base = {"type": beat_type, "actor": "actor", "duration": 1}
    with pytest.raises(ValidationError, match="exactly one"):
        ShotSpec.model_validate({"duration": 2, "beats": [base]})
    with pytest.raises(ValidationError, match="exactly one"):
        ShotSpec.model_validate(
            {
                "duration": 2,
                "beats": [
                    base
                    | {
                        "target_entity": "marker",
                        "target_position": {"x": 1},
                    }
                ],
            }
        )


def test_models_reject_unknown_fields_and_invalid_ranges():
    with pytest.raises(ValidationError, match="extra_forbidden"):
        Vec3.model_validate({"x": 1, "unexpected": "value"})
    with pytest.raises(ValidationError):
        ShotSpec(duration=0)
    with pytest.raises(ValidationError):
        CameraSpec(lens_mm=0)


def test_scene_patch_rejects_arbitrary_operation_name():
    with pytest.raises(ValidationError):
        ScenePatch.model_validate(
            {
                "patch_id": "unsafe",
                "source_title": "unsafe",
                "operations": [{"op": "run_python", "payload": {"code": "pass"}}],
            }
        )


def test_discriminated_union_rejects_unknown_beat():
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        ShotSpec.model_validate({"duration": 1, "beats": [{"type": "teleport", "actor": "actor"}]})


def test_execution_receipt_preserves_revision_identity_and_legacy_compatibility():
    receipt = ExecutionReceipt(
        patch_id="patch-1",
        revision_id="revision-1",
        applied_operations=2,
        changed_entities=["actor"],
    )
    assert receipt.model_dump()["revision_id"] == "revision-1"

    legacy_receipt = ExecutionReceipt(patch_id="legacy", applied_operations=0)
    assert legacy_receipt.revision_id is None


@pytest.mark.parametrize("beat_type", ["move_to", "turn_to", "wait", "play_clip"])
def test_timed_beats_reject_zero_duration(beat_type):
    beat = {"type": beat_type, "duration": 0}
    if beat_type != "wait":
        beat["actor"] = "actor"
    if beat_type in {"move_to", "turn_to"}:
        beat["target_position"] = {"x": 1}
    if beat_type == "play_clip":
        beat["clip"] = "Walk"
    with pytest.raises(ValidationError):
        ShotSpec.model_validate({"duration": 1, "beats": [beat]})


@pytest.mark.parametrize("mode", ["look_at", "follow", "dolly_in"])
def test_camera_motion_modes_require_target(mode):
    with pytest.raises(ValidationError, match="requires a target"):
        CameraSpec(mode=mode)


def test_camera_composition_thresholds_are_strict_and_ordered():
    settings = CameraCompositionSpec()
    assert settings.enabled is True
    assert settings.safe_margin == 0.05
    assert settings.min_subject_height == 0.15
    assert settings.max_subject_height == 0.9
    assert settings.max_center_offset == 0.2
    assert settings.check_occlusion is True

    with pytest.raises(ValidationError, match="less than"):
        CameraCompositionSpec(min_subject_height=0.5, max_subject_height=0.5)
    with pytest.raises(ValidationError):
        CameraCompositionSpec(safe_margin=0.5)
    with pytest.raises(ValidationError):
        CameraCompositionSpec.model_validate({"enabled": "yes"}, strict=True)


def test_actor_and_clip_identifiers_cannot_be_empty():
    with pytest.raises(ValidationError):
        ShotSpec.model_validate(
            {
                "duration": 1,
                "beats": [
                    {
                        "type": "play_clip",
                        "actor": "",
                        "clip": "",
                        "duration": 1,
                    }
                ],
            }
        )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_models_reject_non_finite_numbers(value):
    with pytest.raises(ValidationError):
        Vec3(x=value)
    with pytest.raises(ValidationError):
        ShotSpec(fps=value, duration=1)
