from __future__ import annotations

import json
import sys
from pathlib import Path


def verify_report(report: dict, expected_blender_version: str) -> None:
    assert report["blender_bundled"] is False
    assert report["checksums_verified"] is True
    assert report["mcp_configuration"] is False
    assert report["extension_install"] is False
    assert report["blender_version"] == expected_blender_version
    assert Path(report["python_executable"]).is_file()
    assert not Path(report["install_root"]).exists()


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(
            "usage: verify_installer_report.py REPORT EXPECTED_BLENDER_VERSION"
        )
    report_path = Path(sys.argv[1])
    report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    verify_report(report, sys.argv[2])
    print("FACELINK_INSTALLER_REPORT_OK")


if __name__ == "__main__":
    main()
