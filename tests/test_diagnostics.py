from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import facelink.diagnostics as diagnostics
from facelink.bridge_client import BlenderInstance
from facelink.diagnostics import BlenderCandidate


def _instance(tmp_path: Path) -> BlenderInstance:
    return BlenderInstance(
        instance_id="blender-42",
        pid=42,
        port=17321,
        token="must-never-leak",
        scene_name="Previs",
        blender_version="4.5.12 LTS",
        file_path=tmp_path / "blender-42.json",
    )


def _checks(report):
    return {item["check_id"]: item for item in report["checks"]}


def test_parse_blender_version_is_strict_and_handles_release_output():
    assert diagnostics._parse_blender_version("Blender 4.5.12 LTS\n") == (4, 5, 12)
    assert diagnostics._parse_blender_version("Blender 5.2.0\n build date") == (5, 2, 0)
    assert diagnostics._parse_blender_version("Blender 4.5") is None
    assert diagnostics._parse_blender_version("not Blender") is None


def test_candidate_discovery_prioritizes_explicit_and_deduplicates(monkeypatch, tmp_path):
    blender = tmp_path / "blender.exe"
    blender.write_bytes(b"")
    monkeypatch.setenv("FACELINK_BLENDER_EXE", str(blender))
    monkeypatch.setattr(diagnostics.shutil, "which", lambda _name: str(blender))
    monkeypatch.setattr(diagnostics.sys, "platform", "linux")

    candidates = diagnostics.find_blender_candidates(blender)

    assert candidates == [BlenderCandidate(blender, "command line")]


def test_candidate_discovery_finds_bounded_windows_installations(monkeypatch, tmp_path):
    installed = tmp_path / "Blender Foundation" / "Blender 4.5" / "blender.exe"
    installed.parent.mkdir(parents=True)
    installed.write_bytes(b"")
    monkeypatch.delenv("FACELINK_BLENDER_EXE", raising=False)
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(diagnostics.shutil, "which", lambda _name: None)
    monkeypatch.setattr(diagnostics.sys, "platform", "win32")

    candidates = diagnostics.find_blender_candidates()

    assert candidates == [BlenderCandidate(installed, "Windows installation")]


def test_candidate_helper_ignores_empty_and_missing_required_paths(tmp_path):
    output = []
    seen = set()
    diagnostics._add_candidate(output, seen, None, "empty", require_file=False)
    diagnostics._add_candidate(
        output,
        seen,
        tmp_path / "missing",
        "missing",
        require_file=True,
    )
    assert output == []


def test_probe_blender_reports_supported_unsupported_and_invalid(monkeypatch, tmp_path):
    blender = tmp_path / "blender.exe"
    blender.write_bytes(b"")
    candidate = BlenderCandidate(blender, "test")

    monkeypatch.setattr(
        diagnostics.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="Blender 4.5.12 LTS\n", stderr=""
        ),
    )
    supported = diagnostics.probe_blender(candidate)
    assert supported["available"] is True
    assert supported["supported"] is True
    assert supported["version"] == "4.5.12"

    monkeypatch.setattr(
        diagnostics.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="Blender 4.0.2\n", stderr=""
        ),
    )
    assert diagnostics.probe_blender(candidate)["supported"] is False

    monkeypatch.setattr(
        diagnostics.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="unknown", stderr=""),
    )
    invalid = diagnostics.probe_blender(candidate)
    assert invalid["available"] is False
    assert "valid Blender version" in invalid["error"]


def test_probe_blender_handles_missing_and_process_failure(monkeypatch, tmp_path):
    missing = diagnostics.probe_blender(BlenderCandidate(tmp_path / "missing.exe", "test"))
    assert missing["available"] is False
    assert "does not exist" in missing["error"]

    blender = tmp_path / "blender.exe"
    blender.write_bytes(b"")

    def fail(*args, **kwargs):
        raise TimeoutError

    monkeypatch.setattr(diagnostics.subprocess, "run", fail)
    failed = diagnostics.probe_blender(BlenderCandidate(blender, "test"))
    assert failed["available"] is False
    assert failed["error"] == "Version probe failed: TimeoutError"


def test_discovery_directory_failure_is_actionable(monkeypatch, tmp_path):
    monkeypatch.setattr(diagnostics.os, "access", lambda path, mode: False)
    result = diagnostics._directory_check(tmp_path / "instances")
    assert result.status == "fail"
    assert "FACELINK_INSTANCE_DIR" in result.remediation


def test_doctor_full_report_is_ready_and_never_exposes_secrets(monkeypatch, tmp_path):
    blender = tmp_path / "blender.exe"
    blender.write_bytes(b"")
    launcher = tmp_path / "facelink-mcp"
    launcher.write_bytes(b"")
    instance = _instance(tmp_path)
    monkeypatch.setenv("FACELINK_INSTANCE_DIR", str(tmp_path / "instances"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-never-print-this")
    monkeypatch.setattr(
        diagnostics,
        "find_blender_candidates",
        lambda _explicit=None: [BlenderCandidate(blender, "test")],
    )
    monkeypatch.setattr(
        diagnostics,
        "probe_blender",
        lambda candidate: {
            "path": str(candidate.path),
            "source": candidate.source,
            "available": True,
            "supported": True,
            "version": "4.5.12",
        },
    )
    monkeypatch.setattr(
        diagnostics.shutil,
        "which",
        lambda name: str(launcher) if name == "facelink-mcp" else None,
    )
    monkeypatch.setattr(diagnostics, "discover_instances", lambda verify=False: [instance])
    monkeypatch.setattr(
        diagnostics.BridgeClient,
        "health",
        lambda self, timeout=0.8: {
            "ok": True,
            "facelink_version": "0.3.8",
            "protocol_version": "1.9",
            "capabilities": ["stage_patch", "scan_scene"],
        },
    )

    report = diagnostics.diagnose_environment(blender)

    assert report["ok"] is True
    assert report["readiness"] == {"mcp": True, "byok": True}
    assert _checks(report)["blender_bridge"]["status"] == "pass"
    assert report["bridge"]["live_instances"][0]["facelink_version"] == "0.3.8"
    assert report["bridge"]["live_instances"][0]["capabilities"] == [
        "scan_scene",
        "stage_patch",
    ]
    encoded = json.dumps(report)
    assert "must-never-leak" not in encoded
    assert "sk-never-print-this" not in encoded


def test_doctor_distinguishes_required_failures_from_optional_warnings(monkeypatch, tmp_path):
    monkeypatch.setenv("FACELINK_INSTANCE_DIR", str(tmp_path / "instances"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(diagnostics, "find_blender_candidates", lambda _explicit=None: [])
    monkeypatch.setattr(diagnostics.shutil, "which", lambda _name: None)
    monkeypatch.setattr(diagnostics.sys, "executable", str(tmp_path / "missing-python.exe"))
    monkeypatch.setattr(diagnostics, "discover_instances", lambda verify=False: [])

    report = diagnostics.diagnose_environment()
    checks = _checks(report)

    assert report["ok"] is False
    assert report["readiness"] == {"mcp": False, "byok": False}
    assert checks["blender"]["status"] == "fail"
    assert checks["mcp_launcher"]["status"] == "warning"
    assert checks["blender_bridge"]["status"] == "warning"
    assert checks["openai_api_key"]["status"] == "warning"
    assert report["provider"] == {"openai_api_key_configured": False}


def test_doctor_counts_stale_records_without_failing(monkeypatch, tmp_path):
    live = _instance(tmp_path)
    stale = BlenderInstance(
        instance_id="stale",
        pid=99,
        port=1,
        token="stale-secret",
        scene_name="Old",
        blender_version="4.2.0",
        file_path=tmp_path / "stale.json",
    )
    monkeypatch.setattr(diagnostics, "discover_instances", lambda verify=False: [live, stale])

    def health(self, timeout=0.8):
        if self.instance.instance_id == "stale":
            raise OSError("offline")
        return {"protocol_version": "1.9", "capabilities": []}

    monkeypatch.setattr(diagnostics.BridgeClient, "health", health)
    instances, stale_count = diagnostics._probe_instances()
    assert [item["instance_id"] for item in instances] == ["blender-42"]
    assert stale_count == 1


def test_render_doctor_report_contains_actions_but_not_machine_details():
    report = {
        "facelink_version": "0.3.8",
        "checks": [
            {"status": "pass", "summary": "Ready.", "detail": "C:/FaceLink"},
            {"status": "warning", "summary": "Optional.", "remediation": "Configure it."},
            {"status": "fail", "summary": "Missing.", "remediation": "Install it."},
        ],
        "readiness": {"mcp": False, "byok": False},
    }

    text = diagnostics.render_doctor_report(report)

    assert "FaceLink Doctor 0.3.8" in text
    assert "[PASS] Ready." in text
    assert "[WARN] Optional." in text
    assert "Fix: Install it." in text
    assert "MCP ready: no" in text
