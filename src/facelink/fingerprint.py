from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from .models import SceneEntity, SceneSnapshot


def _number(value: float) -> float:
    rounded = round(float(value), 6)
    return 0.0 if rounded == 0.0 else rounded


def _vector(value: Any) -> list[float]:
    return [_number(value.x), _number(value.y), _number(value.z)]


def _entity_state(entity: SceneEntity) -> dict[str, Any]:
    return {
        "id": entity.id,
        "type": entity.type,
        "location": _vector(entity.transform.location),
        "rotation_euler": _vector(entity.transform.rotation_euler),
        "scale": _vector(entity.transform.scale),
        "locked": entity.locked,
        "parent": entity.metadata.get("parent"),
    }


def fingerprint_snapshot(snapshot: SceneSnapshot, entity_ids: Iterable[str]) -> str:
    """Hash the scene settings and world transforms relevant to one planned patch."""
    entities = snapshot.by_id()
    selected = [_entity_state(entities[entity_id]) for entity_id in sorted(set(entity_ids))]
    canonical = {
        "scene_name": snapshot.scene_name,
        "fps": _number(snapshot.fps),
        "frame_start": snapshot.frame_start,
        "frame_end": snapshot.frame_end,
        "frame_current": _number(snapshot.frame_current),
        "entities": selected,
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "scene-" + hashlib.sha256(encoded).hexdigest()[:24]
