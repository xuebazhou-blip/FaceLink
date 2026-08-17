from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parents[1]
ARTIFACTS = PROJECT / "artifacts"
RUN_ID = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
RUN_DIR = ARTIFACTS / RUN_ID
LOG_DIR = RUN_DIR / "logs"
MANIFEST = tomllib.loads(
    (PROJECT / "blender_extension" / "facelink" / "blender_manifest.toml").read_text(
        encoding="utf-8"
    )
)
EXTENSION_VERSION = MANIFEST["version"]


@dataclass
class CommandResult:
    name: str
    status: str
    exit_code: int
    duration_seconds: float
    command: list[str]
    log: str
    blender_version: str | None = None


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-").lower()


def run_command(
    name: str,
    command: list[str | Path],
    *,
    environment: dict[str, str] | None = None,
    timeout: float = 180.0,
    blender_version: str | None = None,
    hidden_window: bool = False,
) -> CommandResult:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    normalized = [str(item) for item in command]
    log_path = LOG_DIR / f"{safe_name(name)}.log"
    merged_environment = os.environ.copy()
    if environment:
        merged_environment.update(environment)
    print(f"[RUN] {name}", flush=True)
    started = time.monotonic()
    startupinfo = None
    if hidden_window and os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
    try:
        process = subprocess.run(
            normalized,
            cwd=PROJECT,
            env=merged_environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            startupinfo=startupinfo,
        )
        output = process.stdout + process.stderr
        exit_code = process.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = (
            exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else exc.stdout
        )
        stderr = (
            exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else exc.stderr
        )
        output = (stdout or "") + (stderr or "") + f"\nTimed out after {timeout:g} seconds."
        exit_code = 124
    duration = round(time.monotonic() - started, 3)
    log_path.write_text(output, encoding="utf-8")
    status = "passed" if exit_code == 0 else "failed"
    print(f"[{status.upper()}] {name} ({duration:.3f}s)", flush=True)
    return CommandResult(
        name=name,
        status=status,
        exit_code=exit_code,
        duration_seconds=duration,
        command=normalized,
        log=str(log_path),
        blender_version=blender_version,
    )


def blender_version(executable: Path) -> str:
    process = subprocess.run(
        [str(executable), "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=True,
    )
    return process.stdout.splitlines()[0].removeprefix("Blender ").strip()


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def parse_python_metrics(junit_path: Path, coverage_path: Path) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    if junit_path.exists():
        root = ET.parse(junit_path).getroot()
        suite = root if root.tag == "testsuite" else root.find("testsuite")
        if suite is not None:
            metrics["tests"] = int(suite.attrib.get("tests", 0))
            metrics["failures"] = int(suite.attrib.get("failures", 0))
            metrics["errors"] = int(suite.attrib.get("errors", 0))
            metrics["skipped"] = int(suite.attrib.get("skipped", 0))
    if coverage_path.exists():
        root = ET.parse(coverage_path).getroot()
        metrics["line_coverage_percent"] = round(float(root.attrib["line-rate"]) * 100, 2)
        metrics["branch_coverage_percent"] = round(float(root.attrib["branch-rate"]) * 100, 2)
    return metrics


def write_markdown(report: dict[str, Any], target: Path) -> None:
    lines = [
        "# FaceLink acceptance report",
        "",
        f"- Run: `{report['run_id']}`",
        f"- Result: **{report['status'].upper()}**",
        f"- Commit: `{report.get('git_commit', 'unknown')}`",
        f"- Dirty working tree: `{report.get('git_dirty', 'unknown')}`",
        f"- Python tests: {report['python_metrics'].get('tests', 'unknown')}",
        f"- Line coverage: {report['python_metrics'].get('line_coverage_percent', 'unknown')}%",
        f"- Branch coverage: {report['python_metrics'].get('branch_coverage_percent', 'unknown')}%",
        "",
        "## Command matrix",
        "",
        "| Check | Blender | Result | Seconds | Log |",
        "|---|---:|---:|---:|---|",
    ]
    for item in report["commands"]:
        log = Path(item["log"])
        relative_log = log.relative_to(target.parent).as_posix()
        lines.append(
            f"| {item['name']} | {item.get('blender_version') or '-'} | "
            f"{item['status']} | {item['duration_seconds']:.3f} | [{log.name}]({relative_log}) |"
        )
    lines.extend(["", "## Explicitly unverified", ""])
    lines.extend(f"- {item}" for item in report["unverified"])
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


def git_commit() -> str | None:
    try:
        process = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    return process.stdout.strip() or None


def git_is_dirty() -> bool | None:
    try:
        process = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=PROJECT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    return bool(process.stdout.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the full FaceLink acceptance matrix")
    parser.add_argument("--blender", action="append", required=True, type=Path)
    args = parser.parse_args()
    RUN_DIR.mkdir(parents=True, exist_ok=True)

    matrix = []
    for path in args.blender:
        resolved = path.resolve()
        if not resolved.is_file():
            parser.error(f"Blender executable does not exist: {resolved}")
        matrix.append((resolved, blender_version(resolved)))
    unique = {str(path).lower(): (path, version) for path, version in matrix}
    matrix = sorted(unique.values(), key=lambda item: item[1])
    primary = next((item for item in matrix if item[1].startswith("4.5")), matrix[0])

    commands: list[CommandResult] = []
    junit_path = RUN_DIR / "pytest.xml"
    coverage_path = RUN_DIR / "coverage.xml"
    commands.append(run_command("ruff", [sys.executable, "-m", "ruff", "check", "."]))
    commands.append(
        run_command(
            "python-tests",
            [
                sys.executable,
                "-m",
                "pytest",
                "--cov=facelink",
                f"--cov-report=xml:{coverage_path}",
                "--cov-report=term-missing",
                f"--junitxml={junit_path}",
            ],
        )
    )

    dist = RUN_DIR / "dist"
    dist.mkdir(exist_ok=True)
    package = dist / f"facelink-{EXTENSION_VERSION}.zip"
    build_result = run_command(
        "extension-build",
        [
            primary[0],
            "--command",
            "extension",
            "build",
            "--source-dir",
            PROJECT / "blender_extension" / "facelink",
            "--output-dir",
            dist,
        ],
        blender_version=primary[1],
    )
    commands.append(build_result)
    if build_result.status == "passed" and package.exists():
        public_dist = PROJECT / "dist"
        public_dist.mkdir(exist_ok=True)
        shutil.copy2(package, public_dist / package.name)

    suite_reports: dict[str, dict[str, Any] | None] = {}
    for executable, version in matrix:
        version_key = safe_name(version)
        commands.append(
            run_command(
                f"extension-validate-{version_key}",
                [executable, "--command", "extension", "validate", package],
                blender_version=version,
            )
        )

        source_report = RUN_DIR / f"blender-source-{version_key}.json"
        commands.append(
            run_command(
                f"blender-source-{version_key}",
                [
                    executable,
                    "--background",
                    "--factory-startup",
                    "--python-exit-code",
                    "1",
                    "--python",
                    PROJECT / "tests" / "blender_acceptance.py",
                ],
                environment={"FACELINK_TEST_REPORT": str(source_report)},
                blender_version=version,
            )
        )
        suite_reports[f"source-{version}"] = read_json(source_report)

        overlay_report = RUN_DIR / f"blender-overlay-ui-{version_key}.json"
        commands.append(
            run_command(
                f"blender-overlay-ui-{version_key}",
                [
                    executable,
                    "--factory-startup",
                    "--python-exit-code",
                    "1",
                    "--python",
                    PROJECT / "tests" / "blender_overlay_ui_acceptance.py",
                ],
                environment={"FACELINK_TEST_REPORT": str(overlay_report)},
                timeout=20.0,
                blender_version=version,
                hidden_window=True,
            )
        )
        suite_reports[f"overlay-ui-{version}"] = read_json(overlay_report)

        bridge_report = RUN_DIR / f"blender-bridge-{version_key}.json"
        commands.append(
            run_command(
                f"blender-bridge-{version_key}",
                [
                    executable,
                    "--background",
                    "--factory-startup",
                    "--python-exit-code",
                    "1",
                    "--python",
                    PROJECT / "tests" / "blender_bridge_acceptance.py",
                ],
                environment={"FACELINK_TEST_REPORT": str(bridge_report)},
                blender_version=version,
            )
        )
        suite_reports[f"bridge-{version}"] = read_json(bridge_report)

        portable = executable.parent / "portable"
        portable.mkdir(exist_ok=True)
        installed_package = portable / "extensions" / "user_default" / "facelink"
        if installed_package.exists():
            commands.append(
                run_command(
                    f"extension-remove-old-{version_key}",
                    [executable, "--command", "extension", "remove", "facelink"],
                    blender_version=version,
                )
            )
        commands.append(
            run_command(
                f"extension-install-{version_key}",
                [
                    executable,
                    "--command",
                    "extension",
                    "install-file",
                    "-r",
                    "user_default",
                    "-e",
                    package,
                ],
                blender_version=version,
            )
        )
        install_report = RUN_DIR / f"blender-install-{version_key}.json"
        commands.append(
            run_command(
                f"extension-load-installed-{version_key}",
                [
                    executable,
                    "--background",
                    "--python-exit-code",
                    "1",
                    "--python",
                    PROJECT / "tests" / "blender_extension_install_smoke.py",
                ],
                environment={"FACELINK_TEST_REPORT": str(install_report)},
                blender_version=version,
            )
        )
        suite_reports[f"install-{version}"] = read_json(install_report)

    status = "passed" if all(item.status == "passed" for item in commands) else "failed"
    report = {
        "run_id": RUN_ID,
        "status": status,
        "git_commit": git_commit(),
        "git_dirty": git_is_dirty(),
        "started_at_utc": RUN_ID,
        "finished_at_utc": datetime.now(UTC).isoformat(),
        "python": sys.version,
        "python_metrics": parse_python_metrics(junit_path, coverage_path),
        "blender_matrix": [
            {"version": version, "executable": str(executable)} for executable, version in matrix
        ],
        "commands": [asdict(item) for item in commands],
        "suite_reports": suite_reports,
        "unverified": [
            (
                "A paid/live OpenAI API request; provider behavior is verified with a "
                "strict mock only."
            ),
            (
                "Interactive viewport clicking, lighting/aesthetic judgment and rendered "
                "partial-occlusion quality; geometric composition and the GPU overlay draw "
                "callback are tested."
            ),
            (
                "Large production rigs, rest-pose/axis/proportion-aware retargeting, "
                "multi-level or dynamic-obstacle navigation and multi-shot editing."
            ),
            "Linux and macOS Blender builds; this run covers Windows x64 only.",
        ],
    }
    report_path = RUN_DIR / "report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    markdown_path = RUN_DIR / "report.md"
    write_markdown(report, markdown_path)
    ARTIFACTS.mkdir(exist_ok=True)
    (ARTIFACTS / "latest-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    write_markdown(report, ARTIFACTS / "latest-report.md")
    print(f"\nAcceptance result: {status.upper()}")
    print(f"JSON report: {report_path}")
    print(f"Markdown report: {markdown_path}")
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
