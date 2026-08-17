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


class Quaternion(StrictModel):
    w: float = 1.0
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0

    @model_validator(mode="after")
    def normalized(self) -> Quaternion:
        magnitude = (self.w**2 + self.x**2 + self.y**2 + self.z**2) ** 0.5
        if abs(magnitude - 1.0) > 1e-3:
            raise ValueError("quaternion must be normalized")
        return self


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


class RigBone(StrictModel):
    name: str = Field(min_length=1)
    parent: str | None = Field(default=None, min_length=1)
    use_deform: bool = True
    head: Vec3
    tail: Vec3
    rest_rotation: Quaternion = Field(default_factory=Quaternion)


class RigInventory(StrictModel):
    entity_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    bones: list[RigBone] = Field(default_factory=list, max_length=1_024)
    fingerprint: str | None = Field(default=None, pattern=r"^rig-[0-9a-f]{24}$")

    @model_validator(mode="after")
    def valid_bone_hierarchy(self) -> RigInventory:
        names = [bone.name for bone in self.bones]
        if len(names) != len(set(names)):
            raise ValueError("rig bone names must be unique")
        known = set(names)
        for bone in self.bones:
            if bone.parent == bone.name:
                raise ValueError(f"rig bone '{bone.name}' cannot parent itself")
            if bone.parent is not None and bone.parent not in known:
                raise ValueError(
                    f"rig bone '{bone.name}' references missing parent '{bone.parent}'"
                )
        parents = {bone.name: bone.parent for bone in self.bones}
        for bone_name in names:
            visited = set()
            current = bone_name
            while current is not None:
                if current in visited:
                    raise ValueError(f"rig bone hierarchy contains a cycle at '{current}'")
                visited.add(current)
                current = parents[current]
        return self


class ActionInventory(StrictModel):
    name: str = Field(min_length=1)
    frame_start: float
    frame_end: float
    fcurve_count: int = Field(ge=0, le=10_000)
    keyframe_count: int = Field(ge=0, le=200_000)
    pose_bones: list[str] = Field(default_factory=list, max_length=1_024)
    data_paths: list[str] = Field(default_factory=list, max_length=10_000)
    fingerprint: str = Field(pattern=r"^action-[0-9a-f]{24}$")

    @model_validator(mode="after")
    def valid_action_inventory(self) -> ActionInventory:
        if self.frame_end < self.frame_start:
            raise ValueError("action frame_end must not precede frame_start")
        if len(self.pose_bones) != len(set(self.pose_bones)):
            raise ValueError("action pose_bones must be unique")
        if len(self.data_paths) != len(set(self.data_paths)):
            raise ValueError("action data_paths must be unique")
        return self


class SceneSnapshot(StrictModel):
    schema_version: Literal["1.0", "1.1", "1.2", "1.3", "1.4"] = "1.4"
    transform_space: Literal["WORLD"] = "WORLD"
    scene_name: str
    fps: float = Field(default=24.0, gt=0)
    frame_start: int = 1
    frame_end: int = 250
    frame_current: float = 1.0
    entities: list[SceneEntity] = Field(default_factory=list)
    navigation_meshes: list[NavigationMesh] = Field(default_factory=list, max_length=32)
    navigation_environment_fingerprint: str | None = None
    rigs: list[RigInventory] = Field(default_factory=list, max_length=64)
    actions: list[ActionInventory] = Field(default_factory=list, max_length=512)

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
        missing_rigs = [rig.entity_id for rig in self.rigs if rig.entity_id not in entity_ids]
        if missing_rigs:
            raise ValueError(f"rig inventories reference missing entities: {missing_rigs}")
        entity_types = {entity.id: entity.type for entity in self.entities}
        invalid_rigs = [
            rig.entity_id for rig in self.rigs if entity_types.get(rig.entity_id) != "ARMATURE"
        ]
        if invalid_rigs:
            raise ValueError(f"rig inventories reference non-armature entities: {invalid_rigs}")
        rig_ids = [rig.entity_id for rig in self.rigs]
        if len(rig_ids) != len(set(rig_ids)):
            raise ValueError("rig inventory entity IDs must be unique")
        if self.schema_version == "1.4" and any(rig.fingerprint is None for rig in self.rigs):
            raise ValueError("Scene Snapshot 1.4 requires a fingerprint for every rig")
        action_names = [action.name for action in self.actions]
        if len(action_names) != len(set(action_names)):
            raise ValueError("action inventory names must be unique")
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


class ActionRetargetSpec(StrictModel):
    adapter: Literal["rename_only"] = "rename_only"
    bone_map: dict[str, str] = Field(min_length=1, max_length=512)
    strict: bool = True
    source_rig: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def valid_bone_map(self) -> ActionRetargetSpec:
        if any(
            not source.strip() or not target.strip()
            for source, target in self.bone_map.items()
        ):
            raise ValueError("retarget bone names cannot be empty")
        targets = list(self.bone_map.values())
        if len(targets) != len(set(targets)):
            raise ValueError("retarget target bones must be unique")
        return self


class RetargetProfile(ActionRetargetSpec):
    schema_version: Literal["1.0"] = "1.0"
    name: str = Field(min_length=1)


class RetargetCompatibilityIssue(StrictModel):
    severity: Literal["warning", "error"]
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    source_bone: str | None = None
    target_bone: str | None = None


class RetargetBoneMetric(StrictModel):
    source_bone: str
    target_bone: str
    source_length: float = Field(ge=0.0)
    target_length: float = Field(ge=0.0)
    length_ratio: float | None = Field(default=None, gt=0.0)
    length_deviation_percent: float | None = Field(default=None, ge=0.0)
    axis_angle_degrees: float = Field(ge=0.0, le=180.0)
    rest_rotation_angle_degrees: float = Field(ge=0.0, le=180.0)
    hierarchy_preserved: bool


class RetargetCompatibilityReport(StrictModel):
    source_rig_id: str
    source_rig_name: str
    target_rig_id: str
    target_rig_name: str
    status: Literal["safe", "review", "bake_required", "incompatible"]
    rename_only_safe: bool
    mapped_bone_count: int = Field(ge=0, le=1_024)
    median_length_ratio: float | None = Field(default=None, gt=0.0)
    max_axis_angle_degrees: float = Field(ge=0.0, le=180.0)
    max_rest_rotation_angle_degrees: float = Field(ge=0.0, le=180.0)
    metrics: list[RetargetBoneMetric] = Field(default_factory=list, max_length=1_024)
    issues: list[RetargetCompatibilityIssue] = Field(default_factory=list, max_length=2_048)


class BoneMapMatch(StrictModel):
    source_bone: str
    target_bone: str
    method: Literal["exact", "normalized", "alias"]
    confidence: Literal["high", "medium"]


class RetargetProfileSuggestion(StrictModel):
    source_rig_id: str
    target_rig_id: str
    profile: RetargetProfile | None = None
    matches: list[BoneMapMatch] = Field(default_factory=list, max_length=1_024)
    unmapped_sources: list[str] = Field(default_factory=list, max_length=1_024)
    unused_targets: list[str] = Field(default_factory=list, max_length=1_024)
    conflicts: dict[str, list[str]] = Field(default_factory=dict, max_length=1_024)
    review_required: Literal[True] = True


class PlayClipBeat(BeatBase):
    type: Literal["play_clip"]
    actor: str = Field(min_length=1)
    clip: str = Field(min_length=1)
    duration: float = Field(default=1.0, gt=0.0, description="Duration in seconds")
    loop: bool = False
    retarget: ActionRetargetSpec | None = None


ShotBeat = Annotated[
    MoveToBeat | TurnToBeat | LookAtBeat | WaitBeat | PlayClipBeat,
    Field(discriminator="type"),
]


class CameraCompositionSpec(StrictModel):
    enabled: bool = True
    safe_margin: float = Field(default=0.05, ge=0.0, le=0.45)
    min_subject_height: float = Field(default=0.15, ge=0.0, le=1.0)
    max_subject_height: float = Field(default=0.9, gt=0.0, le=1.0)
    max_center_offset: float = Field(default=0.2, ge=0.0, le=0.75)
    check_occlusion: bool = True

    @model_validator(mode="after")
    def valid_subject_height_range(self) -> CameraCompositionSpec:
        if self.min_subject_height >= self.max_subject_height:
            raise ValueError("min_subject_height must be less than max_subject_height")
        return self


class CameraSpec(StrictModel):
    name: str = Field(default="FaceLink Camera", min_length=1)
    mode: Literal["static", "look_at", "follow", "dolly_in"] = "static"
    target: str | None = None
    location: Vec3 | None = None
    lens_mm: float = Field(default=50.0, ge=1.0, le=300.0)
    distance: float = Field(default=6.0, gt=0.0)
    height: float = 2.0
    composition: CameraCompositionSpec = Field(default_factory=CameraCompositionSpec)

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
    schema_version: Literal["1.0", "1.1", "1.2", "1.3", "1.4"] = "1.4"
    patch_id: str
    source_title: str
    operations: list[PatchOperation]
    warnings: list[str] = Field(default_factory=list)
    scene_fingerprint: str | None = None
    fingerprint_entities: list[str] = Field(default_factory=list)
    fingerprint_frame: float | None = None
    navigation_environment_fingerprint: str | None = None
    action_fingerprints: dict[str, str] = Field(default_factory=dict, max_length=512)
    rig_fingerprints: dict[str, str] = Field(default_factory=dict, max_length=64)

    @model_validator(mode="after")
    def valid_action_fingerprints(self) -> ScenePatch:
        invalid = {
            name: fingerprint
            for name, fingerprint in self.action_fingerprints.items()
            if not name
            or not fingerprint.startswith("action-")
            or len(fingerprint) != 31
            or any(character not in "0123456789abcdef" for character in fingerprint[7:])
        }
        if invalid:
            raise ValueError("action_fingerprints contains an invalid name or fingerprint")
        invalid_rigs = {
            entity_id: fingerprint
            for entity_id, fingerprint in self.rig_fingerprints.items()
            if not entity_id
            or not fingerprint.startswith("rig-")
            or len(fingerprint) != 28
            or any(character not in "0123456789abcdef" for character in fingerprint[4:])
        }
        if invalid_rigs:
            raise ValueError("rig_fingerprints contains an invalid entity ID or fingerprint")
        return self


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
