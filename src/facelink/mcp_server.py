from __future__ import annotations

import logging
from typing import Any

try:
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # MCP Python SDK 1.x
    from mcp.server.fastmcp import FastMCP

from .bridge_client import BridgeClient, discover_instances, select_instance
from .compiler import compile_shot, validate_shot
from .models import RetargetProfile, ScenePatch, SceneSnapshot, ShotSpec

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
    """Read stable IDs, bounds, nav data, armature bones and Action channel inventories."""
    return BridgeClient(select_instance(instance_id)).run_job("scan_scene")


@mcp.tool()
def validate_retarget_profile(profile: RetargetProfile) -> dict[str, Any]:
    """Validate and normalize an open rename-only bone-map profile without changing Blender."""
    return profile.model_dump(mode="json")


@mcp.tool()
def validate_shot_spec(shot_spec: ShotSpec, scene_snapshot: SceneSnapshot) -> dict[str, Any]:
    """Validate a typed shot without changing Blender."""
    report = validate_shot(shot_spec, scene_snapshot)
    return report.model_dump(mode="json")


@mcp.tool()
def preview_shot(shot_spec: ShotSpec, scene_snapshot: SceneSnapshot) -> dict[str, Any]:
    """Compile a shot, including deterministic navmesh paths, without applying it."""
    patch = compile_shot(shot_spec, scene_snapshot)
    return patch.model_dump(mode="json")


@mcp.tool()
def stage_scene_patch(patch: ScenePatch, instance_id: str | None = None) -> dict[str, Any]:
    """Stage a patch in Blender for visible human review without changing the scene."""
    return BridgeClient(select_instance(instance_id)).run_job(
        "stage_patch", {"patch": patch.model_dump(mode="json")}
    )


@mcp.tool()
def get_staged_patch(instance_id: str | None = None) -> dict[str, Any]:
    """Read the patch and artist-facing summary currently waiting for approval."""
    return BridgeClient(select_instance(instance_id)).run_job("get_staged_patch")


@mcp.tool()
def apply_staged_patch(instance_id: str | None = None) -> dict[str, Any]:
    """Apply the patch that a human has reviewed in Blender."""
    return BridgeClient(select_instance(instance_id)).run_job("apply_staged_patch")


@mcp.tool()
def discard_staged_patch(instance_id: str | None = None) -> dict[str, Any]:
    """Discard the staged patch without changing Blender."""
    return BridgeClient(select_instance(instance_id)).run_job("discard_staged_patch")


@mcp.tool()
def apply_scene_patch(patch: ScenePatch, instance_id: str | None = None) -> dict[str, Any]:
    """Power-user escape hatch: apply a white-listed patch without Blender review staging."""
    return BridgeClient(select_instance(instance_id)).run_job(
        "apply_patch", {"patch": patch.model_dump(mode="json")}
    )


@mcp.tool()
def undo_last_apply(instance_id: str | None = None) -> dict[str, Any]:
    """Ask Blender to undo the most recent edit."""
    return BridgeClient(select_instance(instance_id)).run_job("undo")


@mcp.tool()
def list_revision_history(instance_id: str | None = None) -> dict[str, Any]:
    """List persistent FaceLink audit entries and current-session rollback availability."""
    return BridgeClient(select_instance(instance_id)).run_job("list_revisions")


@mcp.tool()
def rollback_to_revision(revision_id: str, instance_id: str | None = None) -> dict[str, Any]:
    """Undo the selected revision and every newer FaceLink revision in this session."""
    return BridgeClient(select_instance(instance_id)).run_job(
        "rollback_revision", {"revision_id": revision_id}
    )


@mcp.tool()
def get_blender_job(job_id: str, instance_id: str | None = None) -> dict[str, Any]:
    """Get the status of a previously submitted Blender job."""
    return BridgeClient(select_instance(instance_id)).get_job(job_id)


def main() -> None:
    LOGGER.info("Starting FaceLink MCP server")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
