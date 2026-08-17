import pytest

from facelink.models import SceneSnapshot


@pytest.fixture
def scene_snapshot():
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
