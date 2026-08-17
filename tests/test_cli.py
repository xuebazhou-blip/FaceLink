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


def test_cli_plan_loads_explicit_retarget_profile(monkeypatch, tmp_path, scene_snapshot):
    snapshot_path = tmp_path / "snapshot.json"
    profile_path = tmp_path / "profile.json"
    snapshot_path.write_text(scene_snapshot.model_dump_json(), encoding="utf-8")
    profile_path.write_text(
        json.dumps({"name": "Exact", "bone_map": {"source": "target"}}),
        encoding="utf-8",
    )
    captured = {}

    def planner(*args, **kwargs):
        captured.update(kwargs)
        return ShotSpec(title="Profile plan", duration=1)

    monkeypatch.setattr(cli, "plan_with_openai", planner)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "facelink",
            "plan",
            "--brief",
            "retarget",
            "--snapshot",
            str(snapshot_path),
            "--retarget-profile",
            str(profile_path),
        ],
    )
    cli.main()
    assert captured["retarget_profiles"][0].bone_map == {"source": "target"}


def test_cli_workflow_scans_plans_and_stages_without_applying(
    monkeypatch, tmp_path, scene_snapshot
):
    output_path = tmp_path / "workflow.json"
    planned = ShotSpec(
        title="Review me",
        duration=1,
        beats=[
            {
                "type": "move_to",
                "actor": "actor",
                "target_entity": "marker",
                "duration": 1,
            }
        ],
    )
    calls = []

    class FakeClient:
        def __init__(self, instance):
            assert instance.instance_id == "blender-7"

        def run_job(self, job_type, payload=None):
            calls.append((job_type, payload))
            if job_type == "scan_scene":
                return scene_snapshot.model_dump(mode="json")
            assert job_type == "stage_patch"
            assert payload["patch"]["schema_version"] == "1.3"
            assert payload["patch"]["scene_fingerprint"].startswith("scene-")
            return {
                "staged": True,
                "summary": {"patch_id": payload["patch"]["patch_id"]},
            }

    monkeypatch.setattr(
        cli, "select_instance", lambda _instance_id: SimpleNamespace(instance_id="blender-7")
    )
    monkeypatch.setattr(cli, "BridgeClient", FakeClient)
    monkeypatch.setattr(cli, "plan_with_openai", lambda *args, **kwargs: planned)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "facelink",
            "workflow",
            "--brief",
            "Actor moves to Marker",
            "--out",
            str(output_path),
        ],
    )

    cli.main()

    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert [call[0] for call in calls] == ["scan_scene", "stage_patch"]
    assert result["review"]["staged"] is True
    assert result["patch"]["operations"][1]["op"] == "keyframe_transform"
    assert "Apply or Discard" in result["next_step"]


def test_cli_history_and_rollback_use_revision_jobs(monkeypatch, capsys):
    calls = []

    class FakeClient:
        def __init__(self, instance):
            assert instance.instance_id == "blender-history"

        def run_job(self, job_type, payload=None):
            calls.append((job_type, payload))
            if job_type == "list_revisions":
                return {"entries": [{"revision_id": "rev-1"}]}
            return {"rolled_back": True, "target_revision_id": payload["revision_id"]}

    monkeypatch.setattr(
        cli,
        "select_instance",
        lambda _instance_id: SimpleNamespace(instance_id="blender-history"),
    )
    monkeypatch.setattr(cli, "BridgeClient", FakeClient)

    monkeypatch.setattr(sys, "argv", ["facelink", "history"])
    cli.main()
    assert json.loads(capsys.readouterr().out)["entries"][0]["revision_id"] == "rev-1"

    monkeypatch.setattr(
        sys, "argv", ["facelink", "rollback", "--revision", "rev-1"]
    )
    cli.main()
    assert json.loads(capsys.readouterr().out)["rolled_back"] is True
    assert calls == [
        ("list_revisions", None),
        ("rollback_revision", {"revision_id": "rev-1"}),
    ]


def test_cli_validates_and_normalizes_retarget_profile(monkeypatch, tmp_path):
    profile_path = tmp_path / "profile.json"
    output_path = tmp_path / "normalized.json"
    profile_path.write_text(
        json.dumps(
            {
                "name": "Mixamo compact",
                "bone_map": {"mixamorig:Hips": "pelvis"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "facelink",
            "validate-profile",
            "--profile",
            str(profile_path),
            "--out",
            str(output_path),
        ],
    )
    cli.main()
    normalized = json.loads(output_path.read_text(encoding="utf-8"))
    assert normalized == {
        "adapter": "rename_only",
        "bone_map": {"mixamorig:Hips": "pelvis"},
        "strict": True,
        "schema_version": "1.0",
        "name": "Mixamo compact",
    }
