import json
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

import facelink.bridge_client as bridge_module
from facelink.bridge_client import (
    BlenderInstance,
    BridgeClient,
    discover_instances,
    instance_directory,
    select_instance,
)


@pytest.fixture
def instance(tmp_path):
    return BlenderInstance(
        instance_id="blender-42",
        pid=42,
        port=17321,
        token="secret",
        scene_name="Scene",
        blender_version="4.5.12 LTS",
        file_path=tmp_path / "blender-42.json",
    )


def _write_instance(path: Path, instance_id: str, port: int = 17321):
    path.write_text(
        json.dumps(
            {
                "instance_id": instance_id,
                "pid": 42,
                "port": port,
                "token": "secret",
                "scene_name": "Scene",
                "blender_version": "4.5.12 LTS",
            }
        ),
        encoding="utf-8",
    )


def test_instance_record_round_trip(tmp_path):
    path = tmp_path / "blender-42.json"
    _write_instance(path, "blender-42")
    instance = BlenderInstance.from_file(path)
    assert instance.base_url == "http://127.0.0.1:17321"
    assert instance.scene_name == "Scene"


def test_instance_directory_honors_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("FACELINK_INSTANCE_DIR", str(tmp_path))
    assert instance_directory() == tmp_path


def test_discovery_ignores_malformed_and_unreachable_records(monkeypatch, tmp_path):
    monkeypatch.setenv("FACELINK_INSTANCE_DIR", str(tmp_path))
    _write_instance(tmp_path / "good.json", "good")
    _write_instance(tmp_path / "stale.json", "stale")
    (tmp_path / "broken.json").write_text("{not-json", encoding="utf-8")

    def fake_health(self, timeout=2.0):
        if self.instance.instance_id == "stale":
            raise httpx.ConnectError("offline")
        return {"ok": True}

    monkeypatch.setattr(BridgeClient, "health", fake_health)
    assert [item.instance_id for item in discover_instances()] == ["good"]
    assert [item.instance_id for item in discover_instances(verify=False)] == ["good", "stale"]


def test_select_instance_requires_disambiguation(monkeypatch, instance):
    second = BlenderInstance(
        instance_id="blender-43",
        pid=43,
        port=17322,
        token="other",
        scene_name="Scene 2",
        blender_version="5.2.0",
        file_path=instance.file_path.parent / "blender-43.json",
    )
    monkeypatch.setattr(bridge_module, "discover_instances", lambda: [instance, second])
    with pytest.raises(RuntimeError, match="Multiple Blender"):
        select_instance()
    assert select_instance("blender-43") == second
    with pytest.raises(RuntimeError, match="was not found"):
        select_instance("missing")


def test_select_instance_reports_no_running_blender(monkeypatch):
    monkeypatch.setattr(bridge_module, "discover_instances", lambda: [])
    with pytest.raises(RuntimeError, match="No running"):
        select_instance()


def test_bridge_client_sends_bearer_auth(monkeypatch, instance):
    response = Mock()
    response.json.return_value = {"ok": True}
    get = Mock(return_value=response)
    monkeypatch.setattr(bridge_module.httpx, "get", get)
    result = BridgeClient(instance).health()
    assert result == {"ok": True}
    assert get.call_args.kwargs["headers"] == {"Authorization": "Bearer secret"}
    response.raise_for_status.assert_called_once()


def test_submit_and_get_job_contract(monkeypatch, instance):
    submitted = Mock()
    submitted.json.return_value = {"job_id": "job-1"}
    monkeypatch.setattr(bridge_module.httpx, "post", Mock(return_value=submitted))
    client = BridgeClient(instance)
    assert client.submit_job("scan_scene") == "job-1"
    submitted.raise_for_status.assert_called_once()

    fetched = Mock()
    fetched.json.return_value = {"job_id": "job-1", "status": "queued"}
    get = Mock(return_value=fetched)
    monkeypatch.setattr(bridge_module.httpx, "get", get)
    assert client.get_job("job-1")["status"] == "queued"
    assert get.call_args.args[0].endswith("/v1/jobs/job-1")


def test_wait_for_job_handles_success_failure_and_timeout(monkeypatch, instance):
    client = BridgeClient(instance)
    states = iter(
        [
            {"status": "queued"},
            {"status": "succeeded", "result": {"entities": []}},
        ]
    )
    monkeypatch.setattr(client, "get_job", lambda _job_id: next(states))
    monkeypatch.setattr(bridge_module.time, "sleep", lambda _seconds: None)
    assert client.wait_for_job("job-1", timeout=1) == {"entities": []}

    monkeypatch.setattr(
        client,
        "get_job",
        lambda _job_id: {"status": "failed", "error": "bad patch"},
    )
    with pytest.raises(RuntimeError, match="bad patch"):
        client.wait_for_job("job-2", timeout=1)
    with pytest.raises(TimeoutError, match="job-3"):
        client.wait_for_job("job-3", timeout=0)
