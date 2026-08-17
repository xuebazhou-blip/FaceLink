import pytest
from pydantic import ValidationError

from facelink.models import CameraSpec, ExecutionReceipt, ScenePatch, ShotSpec, Vec3


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
