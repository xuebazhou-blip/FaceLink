from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from facelink.configurator import (
    MANAGED_BEGIN,
    MANAGED_END,
    configure_codex_mcp,
    render_mcp_block,
)


def _launcher(tmp_path):
    path = tmp_path / "host" / "facelink-mcp.exe"
    path.parent.mkdir()
    path.write_bytes(b"launcher")
    return path


def test_rendered_mcp_block_is_valid_toml_and_escapes_windows_paths(tmp_path):
    launcher = _launcher(tmp_path)
    instance_dir = tmp_path / "instances with space"
    block = render_mcp_block(launcher, instance_dir)
    parsed = tomllib.loads(block)
    server = parsed["mcp_servers"]["facelink"]
    assert server["command"] == str(launcher.resolve())
    assert server["env"]["FACELINK_INSTANCE_DIR"] == str(instance_dir.resolve())
    assert server["default_tools_approval_mode"] == "writes"


def test_configure_creates_managed_config_without_api_keys(tmp_path):
    launcher = _launcher(tmp_path)
    config = tmp_path / ".codex" / "config.toml"
    result = configure_codex_mcp(launcher, tmp_path / "instances", config_path=config)
    text = config.read_text(encoding="utf-8")
    assert result["status"] == "created"
    assert result["backup_path"] is None
    assert MANAGED_BEGIN in text and MANAGED_END in text
    assert "OPENAI_API_KEY" not in text
    assert tomllib.loads(text)["mcp_servers"]["facelink"]["enabled"] is True


def test_configure_preserves_unrelated_content_and_backs_up_updates(tmp_path):
    launcher = _launcher(tmp_path)
    config = tmp_path / "config.toml"
    config.write_text('model = "gpt-test"\n\n[mcp_servers.other]\ncommand = "other"\n')
    first = configure_codex_mcp(launcher, tmp_path / "one", config_path=config)
    original_with_block = config.read_bytes()
    second = configure_codex_mcp(launcher, tmp_path / "two", config_path=config)
    text = config.read_text(encoding="utf-8")
    assert first["status"] == "updated"
    assert second["status"] == "updated"
    assert second["backup_path"] is not None
    assert Path(second["backup_path"]).read_bytes() == original_with_block
    assert text.count(MANAGED_BEGIN) == 1
    assert 'model = "gpt-test"' in text
    assert tomllib.loads(text)["mcp_servers"]["other"]["command"] == "other"
    assert tomllib.loads(text)["mcp_servers"]["facelink"]["env"][
        "FACELINK_INSTANCE_DIR"
    ].endswith("two")


def test_configure_is_idempotent_and_dry_run_does_not_write(tmp_path):
    launcher = _launcher(tmp_path)
    config = tmp_path / "config.toml"
    created = configure_codex_mcp(launcher, tmp_path / "instances", config_path=config)
    before = config.read_bytes()
    unchanged = configure_codex_mcp(launcher, tmp_path / "instances", config_path=config)
    assert created["changed"] is True
    assert unchanged["status"] == "unchanged"
    assert unchanged["changed"] is False
    assert config.read_bytes() == before

    planned_config = tmp_path / "planned.toml"
    planned = configure_codex_mcp(
        launcher, tmp_path / "instances", config_path=planned_config, dry_run=True
    )
    assert planned["status"] == "planned"
    assert planned["changed"] is True
    assert planned_config.exists() is False


def test_configure_refuses_unmanaged_collision_and_malformed_markers(tmp_path):
    launcher = _launcher(tmp_path)
    config = tmp_path / "config.toml"
    config.write_text('[mcp_servers.facelink]\ncommand = "custom"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="unmanaged"):
        configure_codex_mcp(launcher, tmp_path / "instances", config_path=config)
    config.write_text(MANAGED_BEGIN + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="markers"):
        configure_codex_mcp(launcher, tmp_path / "instances", config_path=config)


def test_configure_rejects_invalid_inputs_before_writing(tmp_path):
    config = tmp_path / "config.toml"
    with pytest.raises(FileNotFoundError):
        configure_codex_mcp(tmp_path / "missing.exe", tmp_path / "instances", config_path=config)
    launcher = _launcher(tmp_path)
    with pytest.raises(ValueError, match="server name"):
        configure_codex_mcp(
            launcher,
            tmp_path / "instances",
            config_path=config,
            server_name="bad.name",
        )
    config.write_bytes(b"\xff")
    with pytest.raises(ValueError, match="UTF-8"):
        configure_codex_mcp(launcher, tmp_path / "instances", config_path=config)


def test_configure_rejects_preexisting_invalid_toml(tmp_path):
    launcher = _launcher(tmp_path)
    config = tmp_path / "config.toml"
    config.write_text("broken = [", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid TOML"):
        configure_codex_mcp(launcher, tmp_path / "instances", config_path=config)
