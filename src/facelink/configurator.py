from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MANAGED_BEGIN = "# >>> FaceLink managed MCP server >>>"
MANAGED_END = "# <<< FaceLink managed MCP server <<<"
SERVER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def default_codex_config_path() -> Path:
    return Path.home() / ".codex" / "config.toml"


def _toml_string(value: str | Path) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def render_mcp_block(
    mcp_launcher: str | Path,
    instance_dir: str | Path,
    *,
    server_name: str = "facelink",
) -> str:
    if SERVER_NAME_PATTERN.fullmatch(server_name) is None:
        raise ValueError("MCP server name may contain only letters, numbers, '_' and '-'")
    launcher = Path(mcp_launcher).expanduser().resolve(strict=False)
    discovery = Path(instance_dir).expanduser().resolve(strict=False)
    return "\n".join(
        [
            MANAGED_BEGIN,
            f"[mcp_servers.{server_name}]",
            f"command = {_toml_string(launcher)}",
            "enabled = true",
            "startup_timeout_sec = 15",
            "tool_timeout_sec = 120",
            "default_tools_approval_mode = \"writes\"",
            "",
            f"[mcp_servers.{server_name}.env]",
            f"FACELINK_INSTANCE_DIR = {_toml_string(discovery)}",
            MANAGED_END,
        ]
    )


def _replace_or_append_managed_block(existing: str, block: str, server_name: str) -> str:
    begin_count = existing.count(MANAGED_BEGIN)
    end_count = existing.count(MANAGED_END)
    if begin_count != end_count or begin_count > 1:
        raise ValueError("The FaceLink-managed MCP block markers are malformed")
    if begin_count == 1:
        start = existing.index(MANAGED_BEGIN)
        end = existing.index(MANAGED_END, start) + len(MANAGED_END)
        return existing[:start] + block + existing[end:]
    table = re.compile(rf"(?m)^\s*\[mcp_servers\.{re.escape(server_name)}(?:\.|\])")
    if table.search(existing):
        raise ValueError(
            f"An unmanaged mcp_servers.{server_name} configuration already exists; "
            "rename or remove it before FaceLink configures this server"
        )
    separator = "" if not existing or existing.endswith(("\n", "\r")) else "\n"
    prefix = existing + separator
    if prefix and not prefix.endswith("\n\n"):
        prefix += "\n"
    return prefix + block + "\n"


def configure_codex_mcp(
    mcp_launcher: str | Path,
    instance_dir: str | Path,
    *,
    config_path: str | Path | None = None,
    server_name: str = "facelink",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Safely add or update FaceLink's managed Codex/ChatGPT Desktop MCP block."""
    launcher = Path(mcp_launcher).expanduser().resolve(strict=False)
    if not launcher.is_file():
        raise FileNotFoundError(f"The facelink-mcp launcher does not exist: {launcher}")
    discovery = Path(instance_dir).expanduser().resolve(strict=False)
    target = (
        Path(config_path).expanduser().resolve(strict=False)
        if config_path is not None
        else default_codex_config_path().resolve(strict=False)
    )
    original_bytes = target.read_bytes() if target.is_file() else b""
    try:
        existing = original_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Codex configuration is not valid UTF-8: {target}") from exc
    newline = "\r\n" if b"\r\n" in original_bytes else "\n"
    block = render_mcp_block(launcher, discovery, server_name=server_name).replace(
        "\n", newline
    )
    candidate = _replace_or_append_managed_block(existing, block, server_name)
    try:
        tomllib.loads(candidate)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"The merged Codex configuration would be invalid TOML: {exc}") from exc
    changed = candidate.encode("utf-8") != original_bytes
    action = "created" if not original_bytes else "updated"
    if not changed:
        action = "unchanged"
    result: dict[str, Any] = {
        "schema_version": "1.0",
        "status": "planned" if dry_run else action,
        "changed": changed,
        "config_path": str(target),
        "server_name": server_name,
        "mcp_launcher": str(launcher),
        "instance_directory": str(discovery),
        "backup_path": None,
        "restart_required": changed,
        "clients": ["ChatGPT Desktop", "Codex CLI", "Codex IDE extension"],
    }
    if dry_run or not changed:
        return result

    target.parent.mkdir(parents=True, exist_ok=True)
    if original_bytes:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = target.with_name(f"{target.name}.facelink-{stamp}.bak")
        counter = 1
        while backup.exists():
            backup = target.with_name(f"{target.name}.facelink-{stamp}-{counter}.bak")
            counter += 1
        shutil.copy2(target, backup)
        result["backup_path"] = str(backup)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.facelink-", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(candidate.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return result
