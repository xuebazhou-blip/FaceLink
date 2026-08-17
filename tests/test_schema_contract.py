import json
from pathlib import Path

from facelink.models import ScenePatch, SceneSnapshot, ShotSpec


def test_committed_json_schemas_match_runtime_models():
    root = Path(__file__).resolve().parents[1] / "schemas"
    for model in (SceneSnapshot, ShotSpec, ScenePatch):
        path = root / f"{model.__name__}.schema.json"
        committed = json.loads(path.read_text(encoding="utf-8"))
        assert committed == model.model_json_schema(), f"Regenerate schema for {model.__name__}"
