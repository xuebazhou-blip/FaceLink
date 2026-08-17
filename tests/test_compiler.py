import math

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from facelink.compiler import compile_shot, validate_shot
from facelink.models import ShotSpec


def test_compile_move_is_deterministic(scene_snapshot):
    shot = ShotSpec.model_validate(
        {
            "title": "Walk",
            "duration": 3,
            "beats": [
                {
                    "type": "move_to",
                    "actor": "actor",
                    "target_entity": "marker",
                    "at": 0,
                    "duration": 2,
                }
            ],
        }
    )
    first = compile_shot(shot, scene_snapshot)
    second = compile_shot(shot, scene_snapshot)
    assert first.patch_id == second.patch_id
    assert first.operations[1].payload["frames"][1] == {
        "frame": 49,
        "location": [4.0, 2.0, 0.0],
    }


def test_unknown_actor_is_rejected(scene_snapshot):
    shot = ShotSpec.model_validate(
        {
            "duration": 2,
            "beats": [
                {
                    "type": "move_to",
                    "actor": "ghost",
                    "target_entity": "marker",
                    "duration": 1,
                }
            ],
        }
    )
    report = validate_shot(shot, scene_snapshot)
    assert not report.valid
    assert report.issues[0].code == "unknown_actor"
    with pytest.raises(ValueError, match="ghost"):
        compile_shot(shot, scene_snapshot)


def test_all_supported_beats_and_camera_compile(scene_snapshot):
    shot = ShotSpec.model_validate(
        {
            "title": "Full operation surface",
            "duration": 5,
            "beats": [
                {
                    "type": "move_to",
                    "actor": "actor",
                    "target_position": {"x": 1, "y": 2, "z": 0},
                    "at": 0,
                    "duration": 1,
                    "easing": "LINEAR",
                },
                {
                    "type": "turn_to",
                    "actor": "actor",
                    "target_entity": "marker",
                    "at": 1,
                    "duration": 1,
                },
                {"type": "look_at", "actor": "actor", "target": "marker", "at": 2},
                {"type": "wait", "actor": "actor", "at": 2, "duration": 1},
                {
                    "type": "play_clip",
                    "actor": "actor",
                    "clip": "Walk",
                    "at": 3,
                    "duration": 1,
                },
            ],
            "camera": {
                "mode": "follow",
                "target": "actor",
                "lens_mm": 35,
                "distance": 8,
                "height": 3,
            },
        }
    )
    patch = compile_shot(shot, scene_snapshot)
    assert [operation.op for operation in patch.operations] == [
        "set_frame_range",
        "keyframe_transform",
        "keyframe_transform",
        "look_at",
        "play_clip",
        "ensure_camera",
    ]
    turn_end = patch.operations[2].payload["frames"][1]
    assert turn_end["frame"] == 49
    assert turn_end["rotation_euler"][2] == pytest.approx(math.atan2(-3, 0))
    assert patch.operations[-1].payload["mode"] == "follow"


def test_sequential_moves_start_from_previous_destination(scene_snapshot):
    shot = ShotSpec.model_validate(
        {
            "duration": 4,
            "beats": [
                {
                    "type": "move_to",
                    "actor": "actor",
                    "target_position": {"x": 1, "y": 0, "z": 0},
                    "at": 0,
                    "duration": 1,
                },
                {
                    "type": "move_to",
                    "actor": "actor",
                    "target_position": {"x": 2, "y": 0, "z": 0},
                    "at": 2,
                    "duration": 1,
                },
            ],
        }
    )
    patch = compile_shot(shot, scene_snapshot)
    second_move_start = patch.operations[2].payload["frames"][0]["location"]
    assert second_move_start == [1.0, 0.0, 0.0]


def test_operations_are_compiled_in_timeline_order(scene_snapshot):
    shot = ShotSpec.model_validate(
        {
            "duration": 4,
            "beats": [
                {
                    "type": "move_to",
                    "actor": "actor",
                    "target_position": {"x": 2, "y": 0, "z": 0},
                    "at": 2,
                    "duration": 1,
                },
                {
                    "type": "move_to",
                    "actor": "actor",
                    "target_position": {"x": 1, "y": 0, "z": 0},
                    "at": 0,
                    "duration": 1,
                },
            ],
        }
    )
    patch = compile_shot(shot, scene_snapshot)
    assert patch.operations[1].payload["frames"][0]["frame"] == 1
    assert patch.operations[2].payload["frames"][0]["frame"] == 49


def test_patch_id_changes_when_compiled_scene_state_changes(scene_snapshot):
    shot = ShotSpec.model_validate(
        {
            "duration": 2,
            "beats": [
                {
                    "type": "move_to",
                    "actor": "actor",
                    "target_entity": "marker",
                    "duration": 1,
                }
            ],
        }
    )
    moved_snapshot = scene_snapshot.model_copy(deep=True)
    moved_snapshot.entities[1].transform.location.x = 99
    assert (
        compile_shot(shot, scene_snapshot).patch_id != compile_shot(shot, moved_snapshot).patch_id
    )


def test_validation_reports_all_high_value_failures(scene_snapshot):
    locked = scene_snapshot.model_copy(deep=True)
    locked.entities[0].locked = True
    shot = ShotSpec.model_validate(
        {
            "fps": 30,
            "duration": 1,
            "beats": [
                {
                    "type": "move_to",
                    "actor": "actor",
                    "target_entity": "missing",
                    "at": 0.5,
                    "duration": 1,
                }
            ],
            "camera": {"mode": "look_at", "target": "missing-camera-target"},
        }
    )
    report = validate_shot(shot, locked)
    codes = {issue.code for issue in report.issues}
    assert {
        "locked_actor",
        "unknown_target",
        "beat_outside_shot",
        "unknown_camera_target",
        "fps_change",
    } <= codes
    assert not report.valid


def test_overlapping_moves_for_one_actor_are_rejected(scene_snapshot):
    shot = ShotSpec.model_validate(
        {
            "duration": 3,
            "beats": [
                {
                    "type": "move_to",
                    "actor": "actor",
                    "target_position": {"x": 1},
                    "at": 0,
                    "duration": 2,
                },
                {
                    "type": "move_to",
                    "actor": "actor",
                    "target_position": {"x": 2},
                    "at": 1,
                    "duration": 1,
                },
            ],
        }
    )
    report = validate_shot(shot, scene_snapshot)
    assert any(issue.code == "overlapping_channel" for issue in report.issues)


@given(
    x=st.floats(min_value=-1000, max_value=1000, allow_nan=False, allow_infinity=False),
    y=st.floats(min_value=-1000, max_value=1000, allow_nan=False, allow_infinity=False),
    z=st.floats(min_value=-1000, max_value=1000, allow_nan=False, allow_infinity=False),
)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_direct_move_coordinates_round_trip(scene_snapshot, x, y, z):
    shot = ShotSpec.model_validate(
        {
            "duration": 1,
            "beats": [
                {
                    "type": "move_to",
                    "actor": "actor",
                    "target_position": {"x": x, "y": y, "z": z},
                    "duration": 1,
                }
            ],
        }
    )
    destination = compile_shot(shot, scene_snapshot).operations[1].payload["frames"][1]
    assert destination["location"] == [x, y, z]
