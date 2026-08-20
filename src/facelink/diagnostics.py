from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from . import __version__
from .bridge_client import BlenderInstance, BridgeClient, discover_instances, instance_directory

MINIMUM_BLENDER_VERSION = (4, 2, 0)
BLENDER_DOWNLOAD_URL = "https://www.blender.org/download/lts/"
Status = Literal["pass", "warning", "fail"]


@dataclass(frozen=True)
class DiagnosticCheck:
    check_id: str
    status: Status
    summary: str
    detail: str | None = None
    remediation: str | None = None

    def payload(self) -> dict[str, str]:
        return {
            key: value
            for key, value in asdict(self).items()
            if value is not None
        }


@dataclass(frozen=True)
class BlenderCandidate:
    path: Path
    source: str


def _path_key(path: Path) -> str:
    value = str(path.resolve(strict=False))
    return value.casefold() if os.name == "nt" else value


def _add_candidate(
    output: list[BlenderCandidate],
    seen: set[str],
    path: str | Path | None,
    source: str,
    *,
    require_file: bool,
) -> None:
    if not path:
        return
    candidate = Path(path).expanduser()
    if require_file and not candidate.is_file():
        return
    key = _path_key(candidate)
    if key in seen:
        return
    seen.add(key)
    output.append(BlenderCandidate(candidate, source))


def find_blender_candidates(explicit: str | Path | None = None) -> list[BlenderCandidate]:
    """Find bounded, conventional Blender installations without scanning whole drives."""
    output: list[BlenderCandidate] = []
    seen: set[str] = set()
    _add_candidate(output, seen, explicit, "command line", require_file=False)
    _add_candidate(
        output,
        seen,
        os.environ.get("FACELINK_BLENDER_EXE"),
        "FACELINK_BLENDER_EXE",
        require_file=False,
    )
    _add_candidate(output, seen, shutil.which("blender"), "PATH", require_file=True)

    if sys.platform == "win32":
        roots = [
            Path(value) / "Blender Foundation"
            for name in ("ProgramFiles", "ProgramFiles(x86)")
            if (value := os.environ.get(name))
        ]
        if local_app_data := os.environ.get("LOCALAPPDATA"):
            roots.append(Path(local_app_data) / "Programs" / "Blender Foundation")
        for root in roots:
            if not root.is_dir():
                continue
            for path in sorted(root.glob("Blender */blender.exe"), reverse=True):
                _add_candidate(
                    output, seen, path, "Windows installation", require_file=True
                )
    elif sys.platform == "darwin":
        _add_candidate(
            output,
            seen,
            "/Applications/Blender.app/Contents/MacOS/Blender",
            "Applications",
            require_file=True,
        )
    else:
        for path in ("/usr/bin/blender", "/usr/local/bin/blender", "/snap/bin/blender"):
            _add_candidate(output, seen, path, "system installation", require_file=True)
    return output


def _parse_blender_version(output: str) -> tuple[int, int, int] | None:
    match = re.search(r"\bBlender\s+(\d+)\.(\d+)\.(\d+)\b", output)
    if match is None:
        return None
    return tuple(int(value) for value in match.groups())


def probe_blender(candidate: BlenderCandidate) -> dict[str, Any]:
    path = candidate.path.resolve(strict=False)
    payload: dict[str, Any] = {
        "path": str(path),
        "source": candidate.source,
        "available": False,
        "supported": False,
    }
    if not path.is_file():
        payload["error"] = "The executable does not exist."
        return payload
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        result = subprocess.run(
            [str(path), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=flags,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        payload["error"] = f"Version probe failed: {type(exc).__name__}"
        return payload
    combined = "\n".join(part for part in (result.stdout, result.stderr) if part)
    version = _parse_blender_version(combined)
    if result.returncode != 0 or version is None:
        payload["error"] = "The executable did not report a valid Blender version."
        return payload
    payload.update(
        {
            "available": True,
            "version": ".".join(str(value) for value in version),
            "supported": version >= MINIMUM_BLENDER_VERSION,
        }
    )
    return payload


def _instance_payload(instance: BlenderInstance, health: dict[str, Any]) -> dict[str, Any]:
    return {
        "instance_id": instance.instance_id,
        "scene_name": instance.scene_name,
        "blender_version": instance.blender_version,
        "protocol_version": health.get("protocol_version"),
        "facelink_version": health.get("facelink_version"),
        "capabilities": sorted(str(value) for value in health.get("capabilities", [])),
    }


def _probe_instances() -> tuple[list[dict[str, Any]], int]:
    records = discover_instances(verify=False)
    live: list[dict[str, Any]] = []
    for instance in records:
        try:
            health = BridgeClient(instance).health(timeout=0.8)
        except Exception:  # A diagnostic must survive malformed/stale local records.
            continue
        live.append(_instance_payload(instance, health))
    return live, len(records) - len(live)


def _directory_check(path: Path) -> DiagnosticCheck:
    cursor = path
    while not cursor.exists() and cursor != cursor.parent:
        cursor = cursor.parent
    if cursor.exists() and os.access(cursor, os.R_OK | os.W_OK):
        return DiagnosticCheck(
            "instance_directory",
            "pass",
            "The Blender discovery directory is accessible.",
            detail=str(path),
        )
    return DiagnosticCheck(
        "instance_directory",
        "fail",
        "The Blender discovery directory is not accessible.",
        detail=str(path),
        remediation="Choose a writable FACELINK_INSTANCE_DIR for Blender and facelink-mcp.",
    )


def diagnose_environment(blender_exe: str | Path | None = None) -> dict[str, Any]:
    """Return a secret-safe product readiness report for the local FaceLink installation."""
    checks: list[DiagnosticCheck] = []
    python_supported = sys.version_info >= (3, 11)
    checks.append(
        DiagnosticCheck(
            "python",
            "pass" if python_supported else "fail",
            (
                f"Python {platform.python_version()} is "
                f"{'supported' if python_supported else 'too old'}."
            ),
            remediation=None if python_supported else "Install Python 3.11 or newer.",
        )
    )
    checks.append(
        DiagnosticCheck(
            "facelink_package",
            "pass",
            f"FaceLink {__version__} is importable.",
            detail=str(Path(__file__).resolve()),
        )
    )

    launcher_name = "facelink-mcp.exe" if sys.platform == "win32" else "facelink-mcp"
    launcher = shutil.which("facelink-mcp")
    adjacent_launcher = Path(sys.executable).resolve().parent / launcher_name
    if launcher is None and adjacent_launcher.is_file():
        launcher = str(adjacent_launcher)
    checks.append(
        DiagnosticCheck(
            "mcp_launcher",
            "pass" if launcher else "warning",
            "The facelink-mcp launcher is available."
            if launcher
            else "The facelink-mcp launcher is not on PATH.",
            detail=launcher,
            remediation=(
                None
                if launcher
                else "Use the absolute facelink-mcp path from the installed virtual environment."
            ),
        )
    )

    installations = [probe_blender(item) for item in find_blender_candidates(blender_exe)]
    supported_installations = [item for item in installations if item["supported"]]
    if supported_installations:
        versions = ", ".join(item["version"] for item in supported_installations)
        checks.append(
            DiagnosticCheck(
                "blender",
                "pass",
                f"Supported Blender installation found: {versions}.",
            )
        )
    else:
        checks.append(
            DiagnosticCheck(
                "blender",
                "fail",
                "No supported Blender 4.2-or-newer executable was found.",
                remediation=(
                    "Install an official Blender LTS release or pass --blender-exe. "
                    + BLENDER_DOWNLOAD_URL
                ),
            )
        )

    discovery_dir = instance_directory().resolve(strict=False)
    checks.append(_directory_check(discovery_dir))
    live_instances, stale_records = _probe_instances()
    if live_instances:
        checks.append(
            DiagnosticCheck(
                "blender_bridge",
                "pass",
                f"Connected to {len(live_instances)} live FaceLink Blender instance(s).",
            )
        )
    else:
        checks.append(
            DiagnosticCheck(
                "blender_bridge",
                "warning",
                "No running FaceLink Blender bridge was discovered.",
                remediation=(
                    "Open Blender, enable FaceLink, and press Start Bridge. Ensure Blender and "
                    "facelink-mcp use the same FACELINK_INSTANCE_DIR."
                ),
            )
        )

    api_key_configured = bool(os.environ.get("OPENAI_API_KEY"))
    checks.append(
        DiagnosticCheck(
            "openai_api_key",
            "pass" if api_key_configured else "warning",
            "OPENAI_API_KEY is configured."
            if api_key_configured
            else "OPENAI_API_KEY is not configured; BYOK planning is unavailable.",
            remediation=(
                None
                if api_key_configured
                else "This is optional for MCP-client planning. Set an API key only for BYOK mode."
            ),
        )
    )

    payload_checks = [item.payload() for item in checks]
    hard_failure = any(item.status == "fail" for item in checks)
    bridge_ready = bool(live_instances)
    mcp_ready = not hard_failure and bool(launcher) and bridge_ready
    return {
        "schema_version": "1.0",
        "ok": not hard_failure,
        "facelink_version": __version__,
        "system": {
            "platform": platform.platform(),
            "python_version": platform.python_version(),
            "python_executable": str(Path(sys.executable).resolve()),
        },
        "readiness": {
            "mcp": mcp_ready,
            "byok": mcp_ready and api_key_configured,
        },
        "checks": payload_checks,
        "blender": {
            "minimum_version": "4.2.0",
            "download_url": BLENDER_DOWNLOAD_URL,
            "installations": installations,
        },
        "bridge": {
            "instance_directory": str(discovery_dir),
            "live_instances": live_instances,
            "stale_record_count": stale_records,
        },
        "provider": {"openai_api_key_configured": api_key_configured},
    }


def render_doctor_report(report: dict[str, Any]) -> str:
    labels = {"pass": "PASS", "warning": "WARN", "fail": "FAIL"}
    lines = [f"FaceLink Doctor {report['facelink_version']}", ""]
    for check in report["checks"]:
        lines.append(f"[{labels[check['status']]}] {check['summary']}")
        if detail := check.get("detail"):
            lines.append(f"       {detail}")
        if remediation := check.get("remediation"):
            lines.append(f"       Fix: {remediation}")
    readiness = report["readiness"]
    lines.extend(
        [
            "",
            f"MCP ready: {'yes' if readiness['mcp'] else 'no'}",
            f"BYOK ready: {'yes' if readiness['byok'] else 'no'}",
        ]
    )
    return "\n".join(lines)
