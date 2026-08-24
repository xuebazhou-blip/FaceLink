import json
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

import facelink.bridge_client as bridge_module
from facelink.bridge_client import (
    BlenderInstance,
    BridgeClient,
    BridgeError,
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


def test_submit_surfaces_structured_overload_error(monkeypatch, instance):
    request = httpx.Request("POST", f"{instance.base_url}/v1/jobs")
    response = httpx.Response(
        429,
        request=request,
        json={
            "error": "The queue is full.",
            "error_code": "queue_full",
            "retryable": True,
            "retry_after_ms": 100,
            "details": {"pending": 128, "pending_capacity": 128},
        },
    )
    monkeypatch.setattr(bridge_module.httpx, "post", Mock(return_value=response))

    with pytest.raises(BridgeError, match="queue is full") as captured:
        BridgeClient(instance).submit_job("scan_scene")

    error = captured.value
    assert error.code == "queue_full"
    assert error.retryable is True
    assert error.status_code == 429
    assert error.retry_after_ms == 100
    assert error.details == {"pending": 128, "pending_capacity": 128}


def test_http_error_falls_back_for_legacy_or_non_json_bridge(monkeypatch, instance):
    request = httpx.Request("GET", f"{instance.base_url}/v1/health")
    legacy = httpx.Response(401, request=request, json={"error": "unauthorized"})
    monkeypatch.setattr(bridge_module.httpx, "get", Mock(return_value=legacy))
    with pytest.raises(BridgeError, match="unauthorized") as captured:
        BridgeClient(instance).health()
    assert captured.value.code == "http_error"
    assert captured.value.status_code == 401
    assert captured.value.retryable is False

    non_json = httpx.Response(502, request=request, content=b"bad gateway")
    monkeypatch.setattr(bridge_module.httpx, "get", Mock(return_value=non_json))
    with pytest.raises(BridgeError, match="HTTP 502") as captured:
        BridgeClient(instance).health()
    assert captured.value.code == "http_error"


def test_wait_for_job_surfaces_structured_execution_error(monkeypatch, instance):
    client = BridgeClient(instance)
    monkeypatch.setattr(
        client,
        "get_job",
        lambda _job_id: {
            "status": "failed",
            "error": "Patch no longer matches the scene.",
            "error_code": "job_rejected",
            "retryable": False,
            "details": {"entity_id": "missing"},
        },
    )

    with pytest.raises(BridgeError, match="no longer matches") as captured:
        client.wait_for_job("job-1")
    assert captured.value.code == "job_rejected"
    assert captured.value.retryable is False
    assert captured.value.details == {"entity_id": "missing"}


@pytest.mark.parametrize(
    "job",
    [
        {"status": "succeeded"},
        {"status": "mystery", "result": {}},
        {},
    ],
)
def test_wait_for_job_rejects_invalid_success_or_status(monkeypatch, instance, job):
    client = BridgeClient(instance)
    monkeypatch.setattr(client, "get_job", lambda _job_id: job)
    with pytest.raises(BridgeError) as captured:
        client.wait_for_job("job-1")
    assert captured.value.code == "invalid_response"


def test_submit_rejects_success_response_without_job_id(monkeypatch, instance):
    response = Mock()
    response.json.return_value = {"status": "queued"}
    monkeypatch.setattr(bridge_module.httpx, "post", Mock(return_value=response))
    with pytest.raises(BridgeError, match="without returning a job ID") as captured:
        BridgeClient(instance).submit_job("scan_scene")
    assert captured.value.code == "invalid_response"


def test_submit_sends_optional_server_deadline(monkeypatch, instance):
    response = Mock()
    response.json.return_value = {"job_id": "job-1", "status": "queued"}
    post = Mock(return_value=response)
    monkeypatch.setattr(bridge_module.httpx, "post", post)

    assert BridgeClient(instance).submit_job("scan_scene", timeout_ms=2500) == "job-1"

    assert post.call_args.kwargs["json"] == {
        "type": "scan_scene",
        "payload": {},
        "timeout_ms": 2500,
    }


def test_cancel_job_uses_authenticated_control_endpoint(monkeypatch, instance):
    response = Mock()
    response.json.return_value = {
        "job_id": "job-1",
        "status": "cancelled",
        "error_code": "job_cancelled",
    }
    post = Mock(return_value=response)
    monkeypatch.setattr(bridge_module.httpx, "post", post)

    cancelled = BridgeClient(instance).cancel_job("job-1")

    assert cancelled["status"] == "cancelled"
    assert post.call_args.args[0].endswith("/v1/jobs/job-1/cancel")
    assert post.call_args.kwargs["headers"] == {"Authorization": "Bearer secret"}
    assert "json" not in post.call_args.kwargs


@pytest.mark.parametrize(
    ("status", "expected_code", "retryable"),
    [
        ("cancelled", "job_cancelled", False),
        ("timed_out", "job_timed_out", True),
    ],
)
def test_wait_for_job_surfaces_cancel_and_deadline_terminal_states(
    monkeypatch, instance, status, expected_code, retryable
):
    client = BridgeClient(instance)
    monkeypatch.setattr(
        client,
        "get_job",
        lambda _job_id: {
            "status": status,
            "error": f"Job {status}.",
            "error_code": expected_code,
            "retryable": retryable,
        },
    )

    with pytest.raises(BridgeError, match=status) as captured:
        client.wait_for_job("job-1")

    assert captured.value.code == expected_code
    assert captured.value.retryable is retryable


def test_run_job_keeps_poll_timeout_separate_from_server_deadline(monkeypatch, instance):
    client = BridgeClient(instance)
    submit = Mock(return_value="job-1")
    wait = Mock(return_value={"ok": True})
    monkeypatch.setattr(client, "submit_job", submit)
    monkeypatch.setattr(client, "wait_for_job", wait)

    assert client.run_job(
        "scan_scene", timeout=7.5, job_timeout_ms=5000
    ) == {"ok": True}
    submit.assert_called_once_with("scan_scene", None, timeout_ms=5000)
    wait.assert_called_once_with("job-1", timeout=7.5)
