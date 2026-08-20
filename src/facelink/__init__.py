"""FaceLink public API."""

from .compiler import compile_shot, validate_shot
from .models import (
    ActionInventory,
    ActionRetargetSpec,
    CameraCompositionSpec,
    NavigationMesh,
    RetargetCompatibilityReport,
    RetargetProfile,
    RetargetProfileSuggestion,
    RigInventory,
    ScenePatch,
    SceneSnapshot,
    ShotSpec,
)
from .navigation import navigation_environment_fingerprint, plan_move_path
from .retargeting import (
    RetargetAnalysis,
    analyze_retarget,
    analyze_rig_compatibility,
    suggest_retarget_profile,
)

__all__ = [
    "ActionInventory",
    "ActionRetargetSpec",
    "CameraCompositionSpec",
    "NavigationMesh",
    "RetargetAnalysis",
    "RetargetCompatibilityReport",
    "RetargetProfile",
    "RetargetProfileSuggestion",
    "RigInventory",
    "ScenePatch",
    "SceneSnapshot",
    "ShotSpec",
    "analyze_retarget",
    "analyze_rig_compatibility",
    "compile_shot",
    "navigation_environment_fingerprint",
    "plan_move_path",
    "suggest_retarget_profile",
    "validate_shot",
]
__version__ = "0.3.7"
