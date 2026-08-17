import json
import sys
from types import SimpleNamespace

from facelink import cli
from facelink.models import ShotSpec


def test_cli_preview_writes_compiled_patch(monkeypatch, tmp_path, scene_snapshot):
    shot_path = tmp_path / "shot.json"
    snapshot_path = tmp_path / "snapshot.json"
    output_path = tmp_path / "patch.json"
    shot_path.write_text(
        json.dumps(
            {
                "duration": 1,
                "beats": [
                    {
                        "type": "move_to",
                        "actor": "actor",
                        "target_entity": "marker",
                        "duration": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    snapshot_path.write_text(scene_snapshot.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "facelink",
            "preview",
            "--shot",
            str(shot_path),
            "--snapshot",
            str(snapshot_path),
            "--out",
            str(output_path),
        ],
    )
    cli.main()
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["operations"][1]["op"] == "keyframe_transform"


def test_cli_instances_prints_discovered_blender(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(
        cli,
        "discover_instances",
        lambda: [
            SimpleNamespace(
                instance_id="blender-1",
                scene_name="Scene",
                blender_version="4.5.12 LTS",
            )
        ],
    )
    monkeypatch.setattr(sys, "argv", ["facelink", "instances"])
    cli.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["instance_id"] == "blender-1"


def test_cli_plan_uses_provider_and_writes_shot(monkeypatch, tmp_path, scene_snapshot):
    snapshot_path = tmp_path / "snapshot.json"
    output_path = tmp_path / "shot.json"
    snapshot_path.write_text(scene_snapshot.model_dump_json(), encoding="utf-8")
    planned = ShotSpec(title="Planned", duration=1)

    def planner(*args, **kwargs):
        return planned

    monkeypatch.setattr(cli, "plan_with_openai", planner)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "facelink",
            "plan",
            "--brief",
            "hold",
            "--snapshot",
            str(snapshot_path),
            "--out",
            str(output_path),
        ],
    )
    cli.main()
    assert json.loads(output_path.read_text(encoding="utf-8"))["title"] == "Planned"
