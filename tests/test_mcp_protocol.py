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
        "validate_retarget_profile",
        "analyze_retarget_profile",
        "suggest_retarget_profile_map",
        "preview_shot",
        "stage_scene_patch",
        "get_staged_patch",
        "apply_staged_patch",
        "discard_staged_patch",
        "apply_scene_patch",
        "undo_last_apply",
        "list_revision_history",
        "rollback_to_revision",
    } <= names
    preview = next(tool for tool in result.tools if tool.name == "preview_shot")
    schema_text = str(preview.input_schema)
    assert "ShotSpec" in schema_text
    assert "move_to" in schema_text
    assert "path_mode" in schema_text
    assert "CameraCompositionSpec" in schema_text
    assert "max_center_offset" in schema_text
    assert "SceneSnapshot" in schema_text
    assert "navigation_meshes" in schema_text
    assert "ActionRetargetSpec" in schema_text
    assert "rigs" in schema_text
    assert "actions" in schema_text
    assert "pose_bones" in schema_text


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
            profile = await session.call_tool(
                "validate_retarget_profile",
                {
                    "profile": {
                        "name": "MCP profile",
                        "bone_map": {"source": "target"},
                    }
                },
            )
    assert not validation.is_error
    assert validation.structured_content["valid"] is True
    assert not preview.is_error
    assert preview.structured_content["operations"][1]["op"] == "keyframe_transform"
    assert preview.structured_content["operations"][1]["payload"]["space"] == "WORLD"
    assert preview.structured_content["schema_version"] == "1.2"
    assert preview.structured_content["scene_fingerprint"].startswith("scene-")
    assert preview.structured_content["navigation_environment_fingerprint"].startswith("nav-")
    assert invalid.is_error
    assert not profile.is_error
    assert profile.structured_content["adapter"] == "rename_only"
    assert profile.structured_content["strict"] is True


@pytest.mark.asyncio
async def test_stdio_server_compiles_navmesh_path_and_reports_outside_target():
    executable = Path(sys.executable).with_name("facelink-mcp.exe")
    server = StdioServerParameters(command=str(executable), cwd=str(Path.cwd()))
    snapshot = {
        "schema_version": "1.2",
        "scene_name": "Navigation MCP",
        "entities": [
            {
                "id": "actor",
                "name": "Actor",
                "type": "MESH",
                "transform": {"location": {"x": 1, "y": 1, "z": 0}},
            },
            {
                "id": "goal",
                "name": "Goal",
                "type": "EMPTY",
                "transform": {"location": {"x": 5, "y": 5, "z": 0}},
            },
            {
                "id": "nav",
                "name": "L Corridor",
                "type": "MESH",
                "transform": {},
                "metadata": {"navigation_role": "navmesh"},
            },
        ],
        "navigation_meshes": [
            {
                "entity_id": "nav",
                "name": "L Corridor",
                "vertices": [
                    {"x": 0, "y": 0, "z": 0},
                    {"x": 2, "y": 0, "z": 0},
                    {"x": 2, "y": 4, "z": 0},
                    {"x": 0, "y": 4, "z": 0},
                    {"x": 6, "y": 4, "z": 0},
                    {"x": 6, "y": 6, "z": 0},
                    {"x": 0, "y": 6, "z": 0},
                    {"x": 2, "y": 6, "z": 0},
                ],
                "polygons": [
                    [0, 1, 2],
                    [0, 2, 3],
                    [3, 2, 7],
                    [3, 7, 6],
                    [2, 4, 5],
                    [2, 5, 7],
                ],
            }
        ],
    }
    shot = {
        "title": "MCP navigation",
        "duration": 4,
        "beats": [
            {
                "type": "move_to",
                "actor": "actor",
                "target_entity": "goal",
                "duration": 4,
                "path_mode": "navmesh",
                "navigation_mesh": "nav",
            }
        ],
    }
    outside = {
        **shot,
        "beats": [
            {
                **shot["beats"][0],
                "target_entity": None,
                "target_position": {"x": 50, "y": 50, "z": 0},
            }
        ],
    }
    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            preview = await session.call_tool(
                "preview_shot",
                {"shot_spec": shot, "scene_snapshot": snapshot},
            )
            validation = await session.call_tool(
                "validate_shot_spec",
                {"shot_spec": outside, "scene_snapshot": snapshot},
            )
    assert not preview.is_error
    movement = preview.structured_content["operations"][1]
    assert movement["payload"]["path_mode"] == "navmesh"
    assert movement["payload"]["interpolation"] == "LINEAR"
    assert len(movement["payload"]["frames"]) > 2
    assert preview.structured_content["navigation_environment_fingerprint"].startswith("nav-")
    assert not validation.is_error
    assert validation.structured_content["valid"] is False
    assert any(
        issue["code"] == "point_outside_navigation_mesh"
        for issue in validation.structured_content["issues"]
    )
