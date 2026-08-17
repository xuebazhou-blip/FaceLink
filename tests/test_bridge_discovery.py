import json

from facelink.bridge_client import BlenderInstance


def test_instance_record_round_trip(tmp_path):
    path = tmp_path / "blender-42.json"
    path.write_text(
        json.dumps(
            {
                "instance_id": "blender-42",
                "pid": 42,
                "port": 17321,
                "token": "secret",
                "scene_name": "Scene",
                "blender_version": "4.5.12 LTS",
            }
        ),
        encoding="utf-8",
    )
    instance = BlenderInstance.from_file(path)
    assert instance.base_url == "http://127.0.0.1:17321"
    assert instance.scene_name == "Scene"

