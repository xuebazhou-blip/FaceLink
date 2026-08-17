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


class SceneSnapshot(StrictModel):
    schema_version: Literal["1.0", "1.1"] = "1.1"
    transform_space: Literal["WORLD"] = "WORLD"
    scene_name: str
    fps: float = Field(default=24.0, gt=0)
    frame_start: int = 1
    frame_end: int = 250
    frame_current: float = 1.0
    entities: list[SceneEntity] = Field(default_factory=list)

    def by_id(self) -> dict[str, SceneEntity]:
        return {entity.id: entity for entity in self.entities}


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

    @model_validator(mode="after")
    def exactly_one_target(self) -> MoveToBeat:
        if (self.target_entity is None) == (self.target_position is None):
            raise ValueError("move_to requires exactly one target_entity or target_position")
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
    schema_version: Literal["1.0", "1.1"] = "1.1"
    patch_id: str
    source_title: str
    operations: list[PatchOperation]
    warnings: list[str] = Field(default_factory=list)
    scene_fingerprint: str | None = None
    fingerprint_entities: list[str] = Field(default_factory=list)
    fingerprint_frame: float | None = None


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
