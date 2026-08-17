import pytest

from facelink.compiler import compile_shot, validate_shot
from facelink.models import SceneSnapshot, ShotSpec


@pytest.fixture
def snapshot():
    return SceneSnapshot.model_validate(
        {
            "scene_name": "Scene",
            "fps": 24,
            "frame_start": 1,
            "frame_end": 250,
            "entities": [
                {
                    "id": "actor",
                    "name": "Actor",
                    "type": "MESH",
                    "transform": {
                        "location": {"x": 0, "y": 0, "z": 0},
                        "rotation_euler": {"x": 0, "y": 0, "z": 0},
                        "scale": {"x": 1, "y": 1, "z": 1},
                    },
                },
                {
                    "id": "marker",
                    "name": "Marker",
                    "type": "EMPTY",
                    "transform": {
                        "location": {"x": 4, "y": 2, "z": 0},
                        "rotation_euler": {"x": 0, "y": 0, "z": 0},
                        "scale": {"x": 1, "y": 1, "z": 1},
                    },
                },
            ],
        }
    )


def test_compile_move_is_deterministic(snapshot):
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
    first = compile_shot(shot, snapshot)
    second = compile_shot(shot, snapshot)
    assert first.patch_id == second.patch_id
    assert first.operations[1].payload["frames"][1] == {
        "frame": 49,
        "location": [4.0, 2.0, 0.0],
    }


def test_unknown_actor_is_rejected(snapshot):
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
    report = validate_shot(shot, snapshot)
    assert not report.valid
    with pytest.raises(ValueError, match="ghost"):
        compile_shot(shot, snapshot)


def test_exactly_one_move_target():
    with pytest.raises(ValueError, match="exactly one"):
        ShotSpec.model_validate(
            {
                "duration": 2,
                "beats": [{"type": "move_to", "actor": "actor", "duration": 1}],
            }
        )

