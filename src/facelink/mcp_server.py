from __future__ import annotations

import logging
from typing import Any

try:
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # MCP Python SDK 1.x
    from mcp.server.fastmcp import FastMCP

from .bridge_client import BridgeClient, discover_instances, select_instance
from .compiler import compile_shot, validate_shot
from .models import ScenePatch, SceneSnapshot, ShotSpec

logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger("facelink.mcp")
mcp = FastMCP("FaceLink")


@mcp.tool()
def list_blender_instances() -> list[dict[str, Any]]:
    """List Blender windows that currently have the FaceLink bridge running."""
    return [
        {
            "instance_id": item.instance_id,
            "pid": item.pid,
            "scene_name": item.scene_name,
            "blender_version": item.blender_version,
        }
        for item in discover_instances()
    ]


@mcp.tool()
def facelink_health(instance_id: str | None = None) -> dict[str, Any]:
    """Check connectivity and capabilities for one FaceLink Blender instance."""
    return BridgeClient(select_instance(instance_id)).health()


@mcp.tool()
def scan_scene(instance_id: str | None = None) -> dict[str, Any]:
    """Read the active Blender scene as a compact, stable-ID snapshot."""
    return BridgeClient(select_instance(instance_id)).run_job("scan_scene")


@mcp.tool()
def validate_shot_spec(shot_spec: dict[str, Any], scene_snapshot: dict[str, Any]) -> dict[str, Any]:
    """Validate a typed shot without changing Blender."""
    report = validate_shot(
        ShotSpec.model_validate(shot_spec),
        SceneSnapshot.model_validate(scene_snapshot),
    )
    return report.model_dump(mode="json")


@mcp.tool()
def preview_shot(shot_spec: dict[str, Any], scene_snapshot: dict[str, Any]) -> dict[str, Any]:
    """Compile a shot into a reviewable Blender patch without applying it."""
    patch = compile_shot(
        ShotSpec.model_validate(shot_spec),
        SceneSnapshot.model_validate(scene_snapshot),
    )
    return patch.model_dump(mode="json")


@mcp.tool()
def apply_scene_patch(patch: dict[str, Any], instance_id: str | None = None) -> dict[str, Any]:
    """Apply a previously previewed, white-listed patch to Blender."""
    checked = ScenePatch.model_validate(patch)
    return BridgeClient(select_instance(instance_id)).run_job(
        "apply_patch", {"patch": checked.model_dump(mode="json")}
    )


@mcp.tool()
def undo_last_apply(instance_id: str | None = None) -> dict[str, Any]:
    """Ask Blender to undo the most recent edit."""
    return BridgeClient(select_instance(instance_id)).run_job("undo")


@mcp.tool()
def get_blender_job(job_id: str, instance_id: str | None = None) -> dict[str, Any]:
    """Get the status of a previously submitted Blender job."""
    return BridgeClient(select_instance(instance_id)).get_job(job_id)


def main() -> None:
    LOGGER.info("Starting FaceLink MCP server")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
