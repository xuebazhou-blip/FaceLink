from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    report_path = Path(sys.argv[1])
    report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    assert report["blender_bundled"] is False
    assert report["checksums_verified"] is True
    assert report["mcp_configuration"] is False
    assert report["extension_install"] is False
    assert report["blender_version"].startswith("4.5.")
    assert Path(report["python_executable"]).is_file()
    assert not Path(report["install_root"]).exists()
    print("FACELINK_INSTALLER_REPORT_OK")


if __name__ == "__main__":
    main()
