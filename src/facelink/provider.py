from __future__ import annotations

import os

from openai import OpenAI

from .models import SceneSnapshot, ShotSpec

SYSTEM_PROMPT = """You are FaceLink's previs planner. Convert the brief into one conservative,
editable Blender shot. Only reference entity IDs present in the supplied scene snapshot.
Prefer a few explicit beats over invented detail. Times are seconds. Never emit Python,
scripts, file paths, shaders or arbitrary Blender operators."""


def plan_with_openai(
    brief: str,
    snapshot: SceneSnapshot,
    *,
    model: str = "gpt-5-mini",
    api_key: str | None = None,
    base_url: str | None = None,
) -> ShotSpec:
    key = api_key or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set.")
    client = OpenAI(api_key=key, base_url=base_url)
    response = client.responses.parse(
        model=model,
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Scene snapshot:\n{snapshot.model_dump_json(indent=2)}\n\n"
                    f"Shot brief:\n{brief}"
                ),
            },
        ],
        text_format=ShotSpec,
    )
    if response.output_parsed is None:
        raise RuntimeError("The model did not return a valid ShotSpec.")
    return ShotSpec.model_validate(response.output_parsed)
