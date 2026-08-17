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

