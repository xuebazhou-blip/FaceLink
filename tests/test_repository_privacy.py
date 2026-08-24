from __future__ import annotations

import re
import subprocess
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


def _public_files() -> list[Path]:
    process = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=PROJECT,
        capture_output=True,
        check=True,
    )
    return [PROJECT / item.decode() for item in process.stdout.split(b"\0") if item]


def test_public_tree_contains_no_private_machine_or_credential_signatures():
    private_fragments = {
        "former author email": b"xuebazhou" + b"@gmail.com",
        "former Windows user directory": b"C:" + b"\\Users\\" + b"Administrator",
        "former Windows user name fragment": b"Users" + b"\\Administrator",
        "development data root": b"E:" + b"\\CodexData",
        "development repository root": b"E:" + b"\\FaceLink",
        "development FFmpeg root": b"D:" + b"\\tools\\ffmpeg",
    }
    credential_patterns = {
        "OpenAI-style key": re.compile(rb"sk-[A-Za-z0-9_-]{20,}"),
        "GitHub token": re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}"),
        "AWS access key": re.compile(rb"AKIA[0-9A-Z]{16}"),
        "private key": re.compile(
            rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
        ),
    }
    findings: list[str] = []
    for path in _public_files():
        if not path.is_file():
            continue
        data = path.read_bytes()
        searchable = (data, data.replace(b"\0", b""))
        for label, fragment in private_fragments.items():
            if any(fragment.lower() in candidate.lower() for candidate in searchable):
                findings.append(f"{path.relative_to(PROJECT)}: {label}")
        for label, pattern in credential_patterns.items():
            if any(pattern.search(candidate) for candidate in searchable):
                findings.append(f"{path.relative_to(PROJECT)}: {label}")
    assert findings == []
