from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


class Vec3(StrictModel):
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0

    def as_list(self) -> list[float]:
        return [self.x, self.y, self.z]


class Transform(StrictModel):
    location: Vec3 = Field(default_factory=Vec3)
    rotation_euler: Vec3 = Field(default_factory=Vec3)
    scale: Vec3 = Field(default_factory=lambda: Vec3(x=1.0, y=1.0, z=1.0))


class Bounds(StrictModel):
    minimum: Vec3
    maximum: Vec3


class SceneEntity(StrictModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    type: str
    transform: Transform
    bounds: Bounds | None = None
    locked: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class NavigationMesh(StrictModel):
    entity_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    vertices: list[Vec3] = Field(min_length=3, max_length=20_000)
    polygons: list[list[int]] = Field(min_length=1, max_length=20_000)

    @model_validator(mode="after")
    def valid_polygon_indices(self) -> NavigationMesh:
        vertex_count = len(self.vertices)
        seen_polygons = set()
        edge_owners: dict[tuple[int, int], int] = {}
        for index, polygon in enumerate(self.polygons):
            if len(polygon) != 3 or len(set(polygon)) != 3:
                raise ValueError(
                    f"navigation polygon {index} must contain exactly three unique vertices"
                )
            if any(vertex < 0 or vertex >= vertex_count for vertex in polygon):
                raise ValueError(f"navigation polygon {index} has an invalid vertex index")
            canonical = tuple(sorted(polygon))
            if canonical in seen_polygons:
                raise ValueError(f"navigation polygon {index} duplicates another polygon")
            seen_polygons.add(canonical)
            area = 0.0
            for position, vertex_index in enumerate(polygon):
                first = self.vertices[vertex_index]
                second = self.vertices[polygon[(position + 1) % len(polygon)]]
                area += first.x * second.y - second.x * first.y
                edge = tuple(sorted((vertex_index, polygon[(position + 1) % len(polygon)])))
                edge_owners[edge] = edge_owners.get(edge, 0) + 1
            if abs(area) <= 1e-9:
                raise ValueError(f"navigation polygon {index} is degenerate in the XY plane")
        if any(count > 2 for count in edge_owners.values()):
            raise ValueError("navigation mesh has a non-manifold edge shared by over two polygons")
        return self


class SceneSnapshot(StrictModel):
    schema_version: Literal["1.0", "1.1", "1.2"] = "1.2"
    transform_space: Literal["WORLD"] = "WORLD"
    scene_name: str
    fps: float = Field(default=24.0, gt=0)
    frame_start: int = 1
    frame_end: int = 250
    frame_current: float = 1.0
    entities: list[SceneEntity] = Field(default_factory=list)
    navigation_meshes: list[NavigationMesh] = Field(default_factory=list, max_length=32)
    navigation_environment_fingerprint: str | None = None

    def by_id(self) -> dict[str, SceneEntity]:
        return {entity.id: entity for entity in self.entities}

    @model_validator(mode="after")
    def navigation_meshes_reference_scene_entities(self) -> SceneSnapshot:
        entity_ids = [entity.id for entity in self.entities]
        if len(set(entity_ids)) != len(entity_ids):
            raise ValueError("scene entity IDs must be unique")
        missing = [
            mesh.entity_id for mesh in self.navigation_meshes if mesh.entity_id not in entity_ids
        ]
        if missing:
            raise ValueError(f"navigation meshes reference missing entities: {missing}")
        return self


class BeatBase(StrictModel):
    at: float = Field(default=0.0, ge=0.0, description="Start time in seconds")
    duration: float = Field(default=0.0, ge=0.0, description="Duration in seconds")


class MoveToBeat(BeatBase):
    type: Literal["move_to"]
    actor: str = Field(min_length=1)
    duration: float = Field(default=1.0, gt=0.0, description="Duration in seconds")
    target_entity: str | None = Field(default=None, min_length=1)
    target_position: Vec3 | None = None
    easing: Literal["LINEAR", "BEZIER", "CONSTANT"] = "BEZIER"
    path_mode: Literal["direct", "navmesh"] = "direct"
    navigation_mesh: str | None = Field(default=None, min_length=1)
    clearance: float = Field(default=0.1, ge=0.0, le=100.0)

    @model_validator(mode="after")
    def exactly_one_target(self) -> MoveToBeat:
        if (self.target_entity is None) == (self.target_position is None):
            raise ValueError("move_to requires exactly one target_entity or target_position")
        if self.path_mode == "direct" and self.navigation_mesh is not None:
            raise ValueError("navigation_mesh requires path_mode='navmesh'")
        return self


class TurnToBeat(BeatBase):
    type: Literal["turn_to"]
    actor: str = Field(min_length=1)
    duration: float = Field(default=1.0, gt=0.0, description="Duration in seconds")
    target_entity: str | None = Field(default=None, min_length=1)
    target_position: Vec3 | None = None
    easing: Literal["LINEAR", "BEZIER", "CONSTANT"] = "BEZIER"

    @model_validator(mode="after")
    def exactly_one_target(self) -> TurnToBeat:
        if (self.target_entity is None) == (self.target_position is None):
            raise ValueError("turn_to requires exactly one target_entity or target_position")
        return self


class LookAtBeat(BeatBase):
    type: Literal["look_at"]
    actor: str = Field(min_length=1)
    target: str = Field(min_length=1)


class WaitBeat(BeatBase):
    type: Literal["wait"]
    actor: str | None = Field(default=None, min_length=1)
    duration: float = Field(default=1.0, gt=0.0, description="Duration in seconds")


class PlayClipBeat(BeatBase):
    type: Literal["play_clip"]
    actor: str = Field(min_length=1)
    clip: str = Field(min_length=1)
    duration: float = Field(default=1.0, gt=0.0, description="Duration in seconds")
    loop: bool = False


ShotBeat = Annotated[
    MoveToBeat | TurnToBeat | LookAtBeat | WaitBeat | PlayClipBeat,
    Field(discriminator="type"),
]


class CameraSpec(StrictModel):
    name: str = Field(default="FaceLink Camera", min_length=1)
    mode: Literal["static", "look_at", "follow", "dolly_in"] = "static"
    target: str | None = None
    location: Vec3 | None = None
    lens_mm: float = Field(default=50.0, ge=1.0, le=300.0)
    distance: float = Field(default=6.0, gt=0.0)
    height: float = 2.0

    @model_validator(mode="after")
    def moving_modes_require_target(self) -> CameraSpec:
        if self.mode in {"look_at", "follow", "dolly_in"} and not self.target:
            raise ValueError(f"camera mode '{self.mode}' requires a target")
        return self


class ShotSpec(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    title: str = "Untitled shot"
    fps: float = Field(default=24.0, gt=0)
    duration: float = Field(default=5.0, gt=0)
    beats: list[ShotBeat] = Field(default_factory=list)
    camera: CameraSpec | None = None


class PatchOperation(StrictModel):
    op: Literal[
        "keyframe_transform",
        "look_at",
        "play_clip",
        "ensure_camera",
        "set_frame_range",
    ]
    entity_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ScenePatch(StrictModel):
    schema_version: Literal["1.0", "1.1", "1.2"] = "1.2"
    patch_id: str
    source_title: str
    operations: list[PatchOperation]
    warnings: list[str] = Field(default_factory=list)
    scene_fingerprint: str | None = None
    fingerprint_entities: list[str] = Field(default_factory=list)
    fingerprint_frame: float | None = None
    navigation_environment_fingerprint: str | None = None


class ValidationIssue(StrictModel):
    severity: Literal["error", "warning"]
    code: str
    message: str
    beat_index: int | None = None


class ValidationReport(StrictModel):
    valid: bool
    issues: list[ValidationIssue] = Field(default_factory=list)


class ExecutionReceipt(StrictModel):
    patch_id: str
    revision_id: str | None = None
    applied_operations: int
    changed_entities: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
