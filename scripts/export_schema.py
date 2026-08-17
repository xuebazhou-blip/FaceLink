import json
from pathlib import Path

from facelink.models import ScenePatch, SceneSnapshot, ShotSpec

root = Path(__file__).resolve().parents[1] / "schemas"
root.mkdir(exist_ok=True)
for model in (SceneSnapshot, ShotSpec, ScenePatch):
    target = root / f"{model.__name__}.schema.json"
    target.write_text(json.dumps(model.model_json_schema(), indent=2) + "\n", encoding="utf-8")
    print(target)

