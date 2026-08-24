from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).with_name("verify_installer_report.py")
SPEC = importlib.util.spec_from_file_location("facelink_installer_report", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


def _report(tmp_path: Path, blender_version: str) -> dict:
    return {
        "blender_bundled": False,
        "checksums_verified": True,
        "mcp_configuration": False,
        "extension_install": False,
        "blender_version": blender_version,
        "python_executable": sys.executable,
        "install_root": str(tmp_path / "not-created"),
    }


@pytest.mark.parametrize("version", ["4.2.23", "4.5.12", "5.2.0"])
def test_installer_report_accepts_the_selected_blender_version(tmp_path, version):
    verifier.verify_report(_report(tmp_path, version), version)


def test_installer_report_rejects_a_different_blender_version(tmp_path):
    with pytest.raises(AssertionError):
        verifier.verify_report(_report(tmp_path, "4.2.23"), "4.5.12")
