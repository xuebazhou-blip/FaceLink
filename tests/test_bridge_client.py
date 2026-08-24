import json
from dataclasses import replace
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
    discover_instances_with_metadata,
    instance_directory,
    protocol_compatibility,
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
        protocol_version="1.13",
    )


def _write_instance(
    path: Path,
    instance_id: str,
    port: int = 17321,
    *,
    protocol_version: str | None = None,
):
    payload = {
        "instance_id": instance_id,
        "pid": 42,
        "port": port,
        "token": "secret",
        "scene_name": "Scene",
        "blender_version": "4.5.12 LTS",
    }
    if protocol_version is not None:
        payload["protocol_version"] = protocol_version
    path.write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def test_instance_record_round_trip(tmp_path):
    path = tmp_path / "blender-42.json"
    _write_instance(path, "blender-42")
    instance = BlenderInstance.from_file(path)
    assert instance.base_url == "http://127.0.0.1:17321"
    assert instance.scene_name == "Scene"
    assert instance.protocol_version is None


def test_instance_record_round_trip_preserves_advertised_protocol(tmp_path):
    path = tmp_path / "blender-42.json"
    _write_instance(path, "blender-42", protocol_version="1.12")

    instance = BlenderInstance.from_file(path)

    assert instance.protocol_version == "1.12"


def test_instance_record_accepts_exact_size_limit_and_rejects_one_byte_over(
    tmp_path,
):
    path = tmp_path / "bounded.json"
    payload = {
        "instance_id": "bounded",
        "pid": 42,
        "port": 17321,
        "token": "secret",
        "scene_name": "Scene",
        "blender_version": "4.5.12 LTS",
    }
    encoded = json.dumps(payload).encode("utf-8")
    path.write_bytes(
        encoded + b" " * (bridge_module.MAX_INSTANCE_RECORD_BYTES - len(encoded))
    )

    assert BlenderInstance.from_file(path).instance_id == "bounded"

    private_marker = b"private-record-content"
    path.write_bytes(
        encoded
        + b" " * (bridge_module.MAX_INSTANCE_RECORD_BYTES - len(encoded))
        + private_marker
    )
    with pytest.raises(ValueError) as captured:
        BlenderInstance.from_file(path)
    assert "size limit" in str(captured.value)
    assert private_marker.decode() not in str(captured.value)


@pytest.mark.parametrize(
    "content",
    [
        b"\xffprivate-record-content",
        b'{"private-record-content": "must-not-be-reported"}',
        b"[]",
    ],
)
def test_instance_record_damage_uses_fixed_non_sensitive_errors(tmp_path, content):
    path = tmp_path / "damaged.json"
    path.write_bytes(content)

    with pytest.raises(ValueError) as captured:
        BlenderInstance.from_file(path)

    assert "private-record-content" not in str(captured.value)


def test_discovery_limits_candidates_before_parse_and_selects_deterministically(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("FACELINK_INSTANCE_DIR", str(tmp_path))
    monkeypatch.setattr(bridge_module, "MAX_DISCOVERY_CANDIDATES", 2)
    _write_instance(tmp_path / "z-last.json", "first-by-id")
    _write_instance(tmp_path / "A-first.json", "second-by-id")
    _write_instance(tmp_path / "m-middle.json", "alpha-by-id")
    parsed_paths = []
    original = BlenderInstance.from_file.__func__

    def tracking_from_file(cls, path):
        parsed_paths.append(path.name)
        return original(cls, path)

    monkeypatch.setattr(
        BlenderInstance,
        "from_file",
        classmethod(tracking_from_file),
    )

    result = discover_instances_with_metadata(verify=False)

    assert parsed_paths == ["A-first.json", "m-middle.json"]
    assert [item.instance_id for item in result.instances] == [
        "alpha-by-id",
        "second-by-id",
    ]
    assert result.candidate_count == 3
    assert result.skipped_candidate_count == 1
    assert result.candidate_limit == 2


def test_discovery_ignores_oversized_record_without_opening_later_candidates(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("FACELINK_INSTANCE_DIR", str(tmp_path))
    monkeypatch.setattr(bridge_module, "MAX_DISCOVERY_CANDIDATES", 2)
    (tmp_path / "a-oversized.json").write_bytes(
        b"x" * (bridge_module.MAX_INSTANCE_RECORD_BYTES + 1)
    )
    _write_instance(tmp_path / "b-valid.json", "valid")
    _write_instance(tmp_path / "c-not-selected.json", "not-selected")

    result = discover_instances_with_metadata(verify=False)

    assert [item.instance_id for item in result.instances] == ["valid"]
    assert result.candidate_count == 3
    assert result.skipped_candidate_count == 1


def test_verified_discovery_only_probes_candidates_inside_the_read_limit(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("FACELINK_INSTANCE_DIR", str(tmp_path))
    monkeypatch.setattr(bridge_module, "MAX_DISCOVERY_CANDIDATES", 1)
    _write_instance(tmp_path / "a-selected.json", "selected")
    _write_instance(tmp_path / "b-skipped.json", "skipped")
    health_calls = []

    def fake_health(self, timeout=2.0):
        health_calls.append((self.instance.instance_id, timeout))
        return {"ok": True, "protocol_version": "1.12"}

    monkeypatch.setattr(BridgeClient, "health", fake_health)

    result = discover_instances_with_metadata()

    assert [item.instance_id for item in result.instances] == ["selected"]
    assert result.instances[0].protocol_version == "1.12"
    assert health_calls == [("selected", 0.6)]
    assert result.skipped_candidate_count == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("instance_id", 42),
        ("instance_id", "private\nterminal-injection"),
        ("pid", True),
        ("pid", 0),
        ("port", "17321"),
        ("port", 65536),
        ("token", "private\r\nheader-injection"),
        ("scene_name", ["PrivateScene"]),
        ("blender_version", None),
        ("protocol_version", {"private": "value"}),
    ],
)
def test_discovery_skips_invalid_field_types_and_control_characters(
    monkeypatch, tmp_path, field, value
):
    monkeypatch.setenv("FACELINK_INSTANCE_DIR", str(tmp_path))
    _write_instance(tmp_path / "good.json", "good")
    bad_payload = {
        "instance_id": "bad",
        "pid": 42,
        "port": 17321,
        "token": "secret",
        "scene_name": "Scene",
        "blender_version": "4.5.12 LTS",
    }
    bad_payload[field] = value
    (tmp_path / "bad.json").write_text(json.dumps(bad_payload), encoding="utf-8")

    assert [item.instance_id for item in discover_instances(verify=False)] == ["good"]


def test_discovery_directory_scan_failure_returns_bounded_empty_report(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("FACELINK_INSTANCE_DIR", str(tmp_path))

    def deny_scan(_directory):
        raise PermissionError("private-directory-name")

    monkeypatch.setattr(bridge_module.os, "scandir", deny_scan)

    result = discover_instances_with_metadata(verify=False)

    assert result.instances == ()
    assert result.candidate_count == 0
    assert result.skipped_candidate_count == 0
    assert discover_instances(verify=False) == []


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


def test_verified_discovery_refreshes_protocol_from_health(monkeypatch, tmp_path):
    monkeypatch.setenv("FACELINK_INSTANCE_DIR", str(tmp_path))
    _write_instance(tmp_path / "legacy.json", "legacy")
    monkeypatch.setattr(
        BridgeClient,
        "health",
        lambda self, timeout=2.0: {"ok": True, "protocol_version": "1.12"},
    )

    discovered = discover_instances()

    assert len(discovered) == 1
    assert discovered[0].protocol_version == "1.12"


@pytest.mark.parametrize(
    ("version", "expected_status"),
    [("1.12", "previous_minor"), ("1.13", "current")],
)
def test_protocol_compatibility_accepts_exact_supported_versions(
    version, expected_status
):
    result = protocol_compatibility(version)

    assert result["compatible"] is True
    assert result["status"] == expected_status
    assert result["negotiated_version"] == version
    assert result["client_supported_versions"] == ["1.12", "1.13"]


@pytest.mark.parametrize("version", ["1.11", "1.14", "2.0"])
def test_protocol_compatibility_rejects_versions_without_an_exact_overlap(version):
    result = protocol_compatibility(version)

    assert result["compatible"] is False
    assert result["status"] == "unsupported"
    assert result["error_code"] == "unsupported_protocol_version"
    assert "negotiated_version" not in result


@pytest.mark.parametrize(
    "version",
    [
        None,
        113,
        "",
        "1",
        "v1.13",
        "01.13",
        "1.013",
        "1.13.0",
        " 1.13",
        "1234567890.13",
        "1.1234567890",
    ],
)
def test_protocol_compatibility_rejects_malformed_versions(version):
    result = protocol_compatibility(version)

    assert result["compatible"] is False
    assert result["status"] == "invalid"
    assert result["error_code"] == "invalid_protocol_version"


def test_protocol_compatibility_negotiates_highest_overlap_with_future_server():
    result = protocol_compatibility("1.14", ["1.12", "1.13", "1.14"])

    assert result["compatible"] is True
    assert result["status"] == "current"
    assert result["negotiated_version"] == "1.13"
    assert result["server_supported_versions"] == ["1.12", "1.13", "1.14"]


@pytest.mark.parametrize("version", ["1.12", "1.13"])
def test_discovery_record_with_supported_protocol_posts_without_health_probe(
    monkeypatch, instance, version
):
    declared = replace(instance, protocol_version=version)
    response = Mock()
    response.json.return_value = {"job_id": f"job-{version}", "status": "queued"}
    get = Mock()
    post = Mock(return_value=response)
    monkeypatch.setattr(bridge_module.httpx, "get", get)
    monkeypatch.setattr(bridge_module.httpx, "post", post)

    assert BridgeClient(declared).submit_job("scan_scene") == f"job-{version}"

    get.assert_not_called()
    assert post.call_args.kwargs["headers"]["FaceLink-Protocol-Version"] == version


def test_legacy_health_without_compatibility_metadata_negotiates_1_12_before_post(
    monkeypatch, instance
):
    legacy_instance = replace(instance, protocol_version=None)
    health_request = httpx.Request("GET", f"{instance.base_url}/v1/health")
    health_response = httpx.Response(
        200,
        request=health_request,
        json={"ok": True, "protocol_version": "1.12"},
    )
    submit_request = httpx.Request("POST", f"{instance.base_url}/v1/jobs")
    submit_response = httpx.Response(
        202,
        request=submit_request,
        json={"job_id": "job-legacy", "status": "queued"},
    )
    get = Mock(return_value=health_response)
    post = Mock(return_value=submit_response)
    monkeypatch.setattr(bridge_module.httpx, "get", get)
    monkeypatch.setattr(bridge_module.httpx, "post", post)
    correlation_id = "corr-0123456789abcdef0123456789abcdef"

    client = BridgeClient(legacy_instance, correlation_id=correlation_id)

    assert client.submit_job("scan_scene") == "job-legacy"
    assert get.call_count == 1
    assert get.call_args.args[0].endswith("/v1/health")
    assert get.call_args.kwargs["headers"]["FaceLink-Protocol-Version"] == "1.13"
    submit_headers = post.call_args.kwargs["headers"]
    assert submit_headers["FaceLink-Protocol-Version"] == "1.12"
    assert submit_headers["X-FaceLink-Correlation-ID"] == correlation_id


def test_future_server_with_overlap_negotiates_1_13_before_post(monkeypatch, instance):
    future_instance = replace(instance, protocol_version="1.14")
    health_response = Mock()
    health_response.json.return_value = {
        "ok": True,
        "protocol_version": "1.14",
        "protocol_compatibility": {
            "supported_versions": ["1.13", "1.14"],
        },
    }
    submit_response = Mock()
    submit_response.json.return_value = {"job_id": "job-future", "status": "queued"}
    get = Mock(return_value=health_response)
    post = Mock(return_value=submit_response)
    monkeypatch.setattr(bridge_module.httpx, "get", get)
    monkeypatch.setattr(bridge_module.httpx, "post", post)

    assert BridgeClient(future_instance).submit_job("scan_scene") == "job-future"

    assert get.call_count == 1
    assert post.call_args.kwargs["headers"]["FaceLink-Protocol-Version"] == "1.13"


@pytest.mark.parametrize("server_version", ["1.11", "1.14", "2.0"])
def test_incompatible_server_is_rejected_before_any_job_post(
    monkeypatch, instance, server_version
):
    incompatible = replace(instance, protocol_version=server_version)
    health_response = Mock()
    health_response.json.return_value = {
        "ok": True,
        "protocol_version": server_version,
        "protocol_compatibility": {"supported_versions": [server_version]},
        "request_id": "req-1111111111111111",
        "correlation_id": "corr-11111111111111111111111111111111",
    }
    get = Mock(return_value=health_response)
    post = Mock()
    monkeypatch.setattr(bridge_module.httpx, "get", get)
    monkeypatch.setattr(bridge_module.httpx, "post", post)

    with pytest.raises(BridgeError) as captured:
        BridgeClient(incompatible).submit_job("scan_scene", {"must_not": "leave"})

    assert captured.value.code == "unsupported_protocol_version"
    assert captured.value.details["actual_version"] == server_version
    assert captured.value.request_id == "req-1111111111111111"
    assert captured.value.correlation_id == "corr-11111111111111111111111111111111"
    assert get.call_count == 1
    post.assert_not_called()


@pytest.mark.parametrize("server_version", [None, "1", "01.13", "1.13.0"])
def test_malformed_server_protocol_is_rejected_before_any_job_post(
    monkeypatch, instance, server_version
):
    malformed = replace(instance, protocol_version=server_version)
    health_response = Mock()
    health_response.json.return_value = {
        "ok": True,
        "protocol_version": server_version,
        "request_id": "req-2222222222222222",
        "correlation_id": "corr-22222222222222222222222222222222",
    }
    post = Mock()
    monkeypatch.setattr(bridge_module.httpx, "get", Mock(return_value=health_response))
    monkeypatch.setattr(bridge_module.httpx, "post", post)

    with pytest.raises(BridgeError) as captured:
        BridgeClient(malformed).submit_job("scan_scene")

    assert captured.value.code == "invalid_protocol_version"
    assert captured.value.request_id == "req-2222222222222222"
    assert captured.value.correlation_id == "corr-22222222222222222222222222222222"
    post.assert_not_called()


def test_protocol_negotiation_is_cached_across_control_requests(monkeypatch, instance):
    client = BridgeClient(replace(instance, protocol_version=None))
    health = Mock(
        return_value={
            "ok": True,
            "protocol_version": "1.12",
            "protocol_compatibility": {"supported_versions": ["1.12"]},
        }
    )
    response = Mock()
    response.json.return_value = {"schema_version": "1.0", "events": []}
    get = Mock(return_value=response)
    monkeypatch.setattr(client, "health", health)
    monkeypatch.setattr(bridge_module.httpx, "get", get)

    assert client.ensure_compatible()["negotiated_version"] == "1.12"
    assert client.ensure_compatible()["negotiated_version"] == "1.12"
    assert client.diagnostics()["schema_version"] == "1.0"

    health.assert_called_once_with()
    assert get.call_count == 1
    assert get.call_args.kwargs["headers"]["FaceLink-Protocol-Version"] == "1.12"


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
    headers = get.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer secret"
    assert headers["FaceLink-Protocol-Version"] == "1.13"
    assert bridge_module.CORRELATION_ID_PATTERN.fullmatch(
        headers["X-FaceLink-Correlation-ID"]
    )
    response.raise_for_status.assert_called_once()


@pytest.mark.parametrize(
    "correlation_id",
    [
        "",
        "corr-short",
        "corr-0123456789ABCDEF0123456789ABCDEF",
        "corr-0123456789abcdef0123456789abcdeg",
        "request-0123456789abcdef0123456789abcdef",
        "corr-0123456789abcdef0123456789abcdef-extra",
    ],
)
def test_client_rejects_noncanonical_correlation_ids(instance, correlation_id):
    with pytest.raises(ValueError, match="correlation_id"):
        BridgeClient(instance, correlation_id=correlation_id)


def test_client_reuses_one_correlation_id_across_all_request_types(
    monkeypatch, instance
):
    correlation_id = "corr-fedcba9876543210fedcba9876543210"
    get_response = Mock()
    get_response.json.side_effect = [
        {"ok": True},
        {"schema_version": "1.0", "events": []},
        {"job_id": "job-1", "status": "queued"},
    ]
    post_response = Mock()
    post_response.json.side_effect = [
        {"job_id": "job-1", "status": "queued"},
        {"job_id": "job-1", "status": "cancelled"},
    ]
    get = Mock(return_value=get_response)
    post = Mock(return_value=post_response)
    monkeypatch.setattr(bridge_module.httpx, "get", get)
    monkeypatch.setattr(bridge_module.httpx, "post", post)
    client = BridgeClient(instance, correlation_id=correlation_id)

    client.health()
    client.diagnostics()
    assert client.submit_job("scan_scene") == "job-1"
    assert client.get_job("job-1")["status"] == "queued"
    assert client.cancel_job("job-1")["status"] == "cancelled"

    calls = [*get.call_args_list, *post.call_args_list]
    assert len(calls) == 5
    for call in calls:
        headers = call.kwargs["headers"]
        assert headers["Authorization"] == "Bearer secret"
        assert headers["FaceLink-Protocol-Version"] == "1.13"
        assert headers["X-FaceLink-Correlation-ID"] == correlation_id


def test_recovery_status_uses_authenticated_endpoint(monkeypatch, instance):
    response = Mock()
    response.json.return_value = {"required": True, "interrupted_jobs": []}
    get = Mock(return_value=response)
    monkeypatch.setattr(bridge_module.httpx, "get", get)

    recovery = BridgeClient(instance).recovery_status(timeout=1.5)

    assert recovery["required"] is True
    assert get.call_args.args[0].endswith("/v1/recovery")
    assert get.call_args.kwargs["headers"]["Authorization"] == "Bearer secret"
    assert get.call_args.kwargs["headers"]["FaceLink-Protocol-Version"] == "1.13"
    assert get.call_args.kwargs["timeout"] == 1.5


def test_diagnostics_uses_authenticated_versioned_endpoint(monkeypatch, instance):
    response = Mock()
    response.json.return_value = {
        "schema_version": "1.0",
        "events": [],
        "jobs": [],
    }
    get = Mock(return_value=response)
    monkeypatch.setattr(bridge_module.httpx, "get", get)

    diagnostics = BridgeClient(instance).diagnostics(timeout=1.25)

    assert diagnostics["schema_version"] == "1.0"
    assert get.call_args.args[0].endswith("/v1/diagnostics")
    headers = get.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer secret"
    assert headers["FaceLink-Protocol-Version"] == "1.13"
    assert bridge_module.CORRELATION_ID_PATTERN.fullmatch(
        headers["X-FaceLink-Correlation-ID"]
    )
    assert get.call_args.kwargs["timeout"] == 1.25


def test_diagnostics_timeout_also_bounds_health_bootstrap(monkeypatch, instance):
    client = BridgeClient(replace(instance, protocol_version=None))
    health = Mock(
        return_value={
            "protocol_version": "1.12",
            "protocol_compatibility": {"supported_versions": ["1.12"]},
        }
    )
    response = Mock()
    response.json.return_value = {"schema_version": "1.0", "events": []}
    get = Mock(return_value=response)
    monkeypatch.setattr(client, "health", health)
    monkeypatch.setattr(bridge_module.httpx, "get", get)

    client.diagnostics(timeout=0.25)

    health.assert_called_once_with(timeout=0.25)
    assert get.call_args.kwargs["timeout"] == 0.25


def test_acknowledge_recovery_uses_authenticated_endpoint_without_body(
    monkeypatch, instance
):
    response = Mock()
    response.json.return_value = {"required": False, "acknowledged": True}
    post = Mock(return_value=response)
    monkeypatch.setattr(bridge_module.httpx, "post", post)

    acknowledged = BridgeClient(instance).acknowledge_recovery(timeout=2.5)

    assert acknowledged["acknowledged"] is True
    assert post.call_args.args[0].endswith("/v1/recovery/acknowledge")
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer secret"
    assert post.call_args.kwargs["headers"]["FaceLink-Protocol-Version"] == "1.13"
    assert post.call_args.kwargs["timeout"] == 2.5


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


def test_http_error_parses_body_correlation_metadata_before_headers(
    monkeypatch, instance
):
    request = httpx.Request("POST", f"{instance.base_url}/v1/jobs")
    response = httpx.Response(
        429,
        request=request,
        headers={
            "X-FaceLink-Request-ID": "req-aaaaaaaaaaaaaaaa",
            "X-FaceLink-Correlation-ID": "corr-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        },
        json={
            "error": "The queue is full.",
            "error_code": "queue_full",
            "request_id": "req-bbbbbbbbbbbbbbbb",
            "correlation_id": "corr-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        },
    )
    monkeypatch.setattr(bridge_module.httpx, "post", Mock(return_value=response))

    with pytest.raises(BridgeError) as captured:
        BridgeClient(instance).submit_job("scan_scene")

    assert captured.value.request_id == "req-bbbbbbbbbbbbbbbb"
    assert captured.value.correlation_id == "corr-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def test_http_error_falls_back_to_correlation_response_headers(monkeypatch, instance):
    request = httpx.Request("GET", f"{instance.base_url}/v1/health")
    response = httpx.Response(
        503,
        request=request,
        headers={
            "X-FaceLink-Request-ID": "req-0123456789abcdef",
            "X-FaceLink-Correlation-ID": "corr-0123456789abcdef0123456789abcdef",
        },
        json={"error": "Unavailable.", "error_code": "bridge_unavailable"},
    )
    monkeypatch.setattr(bridge_module.httpx, "get", Mock(return_value=response))

    with pytest.raises(BridgeError) as captured:
        BridgeClient(instance).health()

    assert captured.value.request_id == "req-0123456789abcdef"
    assert captured.value.correlation_id == "corr-0123456789abcdef0123456789abcdef"


@pytest.mark.parametrize("content", [b"not-json", b"[]"])
def test_invalid_success_response_keeps_header_correlation_metadata(
    monkeypatch, instance, content
):
    request = httpx.Request("GET", f"{instance.base_url}/v1/health")
    response = httpx.Response(
        200,
        request=request,
        headers={
            "X-FaceLink-Request-ID": "req-abcdef0123456789",
            "X-FaceLink-Correlation-ID": "corr-abcdef0123456789abcdef0123456789",
        },
        content=content,
    )
    monkeypatch.setattr(bridge_module.httpx, "get", Mock(return_value=response))

    with pytest.raises(BridgeError) as captured:
        BridgeClient(instance).health()

    assert captured.value.code == "invalid_response"
    assert captured.value.request_id == "req-abcdef0123456789"
    assert captured.value.correlation_id == "corr-abcdef0123456789abcdef0123456789"


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
            "request_id": "req-3333333333333333",
            "correlation_id": "corr-33333333333333333333333333333333",
        },
    )

    with pytest.raises(BridgeError, match="no longer matches") as captured:
        client.wait_for_job("job-1")
    assert captured.value.code == "job_rejected"
    assert captured.value.retryable is False
    assert captured.value.details == {"entity_id": "missing"}
    assert captured.value.request_id == "req-3333333333333333"
    assert captured.value.correlation_id == "corr-33333333333333333333333333333333"


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
    job = {
        **job,
        "request_id": "req-4444444444444444",
        "correlation_id": "corr-44444444444444444444444444444444",
    }
    monkeypatch.setattr(client, "get_job", lambda _job_id: job)
    with pytest.raises(BridgeError) as captured:
        client.wait_for_job("job-1")
    assert captured.value.code == "invalid_response"
    assert captured.value.request_id == "req-4444444444444444"
    assert captured.value.correlation_id == "corr-44444444444444444444444444444444"


def test_submit_rejects_success_response_without_job_id(monkeypatch, instance):
    response = Mock()
    response.json.return_value = {
        "status": "queued",
        "request_id": "req-5555555555555555",
        "correlation_id": "corr-55555555555555555555555555555555",
    }
    monkeypatch.setattr(bridge_module.httpx, "post", Mock(return_value=response))
    with pytest.raises(BridgeError, match="without returning a job ID") as captured:
        BridgeClient(instance).submit_job("scan_scene")
    assert captured.value.code == "invalid_response"
    assert captured.value.request_id == "req-5555555555555555"
    assert captured.value.correlation_id == "corr-55555555555555555555555555555555"


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
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer secret"
    assert post.call_args.kwargs["headers"]["FaceLink-Protocol-Version"] == "1.13"
    assert "json" not in post.call_args.kwargs


@pytest.mark.parametrize(
    ("status", "expected_code", "retryable"),
    [
        ("cancelled", "job_cancelled", False),
        ("timed_out", "job_timed_out", True),
        ("interrupted", "job_interrupted", True),
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
