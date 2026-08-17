import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters, stdio_client


@pytest.mark.asyncio
async def test_stdio_server_lists_expected_tools():
    executable = Path(sys.executable).with_name("facelink-mcp.exe")
    server = StdioServerParameters(command=str(executable), cwd=str(Path.cwd()))
    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.list_tools()
    names = {tool.name for tool in result.tools}
    assert {
        "list_blender_instances",
        "scan_scene",
        "preview_shot",
        "apply_scene_patch",
        "undo_last_apply",
    } <= names
    preview = next(tool for tool in result.tools if tool.name == "preview_shot")
    schema_text = str(preview.input_schema)
    assert "ShotSpec" in schema_text
    assert "move_to" in schema_text
    assert "SceneSnapshot" in schema_text


@pytest.mark.asyncio
async def test_stdio_server_validates_and_previews_without_blender():
    executable = Path(sys.executable).with_name("facelink-mcp.exe")
    server = StdioServerParameters(command=str(executable), cwd=str(Path.cwd()))
    snapshot = {
        "schema_version": "1.0",
        "scene_name": "Scene",
        "fps": 24,
        "frame_start": 1,
        "frame_end": 100,
        "entities": [
            {
                "id": "actor",
                "name": "Actor",
                "type": "MESH",
                "transform": {
                    "location": {"x": 0, "y": 0, "z": 0},
                    "rotation_euler": {"x": 0, "y": 0, "z": 0},
                    "scale": {"x": 1, "y": 1, "z": 1},
                },
            }
        ],
    }
    shot = {
        "schema_version": "1.0",
        "title": "MCP contract",
        "fps": 24,
        "duration": 1,
        "beats": [
            {
                "type": "move_to",
                "actor": "actor",
                "target_position": {"x": 1, "y": 0, "z": 0},
                "at": 0,
                "duration": 1,
            }
        ],
    }
    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            validation = await session.call_tool(
                "validate_shot_spec",
                {"shot_spec": shot, "scene_snapshot": snapshot},
            )
            preview = await session.call_tool(
                "preview_shot",
                {"shot_spec": shot, "scene_snapshot": snapshot},
            )
            invalid = await session.call_tool(
                "preview_shot",
                {"shot_spec": shot | {"unexpected": True}, "scene_snapshot": snapshot},
            )
    assert not validation.is_error
    assert validation.structured_content["valid"] is True
    assert not preview.is_error
    assert preview.structured_content["operations"][1]["op"] == "keyframe_transform"
    assert invalid.is_error
