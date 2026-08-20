from __future__ import annotations

import os

from openai import OpenAI

from .models import RetargetProfile, SceneSnapshot, ShotSpec

SYSTEM_PROMPT = """You are FaceLink's previs planner. Convert the brief into one conservative,
editable Blender shot. Only reference entity IDs present in the supplied scene snapshot.
Snapshot transforms and target_position values are world-space coordinates. Prefer a few
explicit beats over invented detail. Times are seconds. Never emit Python, scripts, file
paths, shaders or arbitrary Blender operators. Use path_mode='navmesh' only when the brief
asks for obstacle avoidance and the snapshot contains one navigation mesh covering both the
actor and target; otherwise keep the backward-compatible direct path. Keep camera composition
checks enabled unless the user explicitly asks to disable preflight warnings. For play_clip,
never invent a bone mapping. Emit retarget only when an exact supplied retarget profile applies;
copy every profile field exactly. The rename_only adapter only rewrites pose-bone channel names
and does not correct rest pose, proportions, axes or root motion. The bake_pose adapter samples
direct pose transforms. The bake_evaluated_pose adapter samples the final constrained/driven pose
of a self-contained explicit source rig. Both create editable target Actions; root_motion affects
translation on a mapped root pose bone. Object-level channels are omitted unless an exact profile
supplies object_motion; preserve/scale modes bake the source object's relative transform delta
while retaining target placement. Never treat an unreviewed mapping suggestion as an approved
profile; compatibility validation belongs to FaceLink's deterministic compiler."""


def plan_with_openai(
    brief: str,
    snapshot: SceneSnapshot,
    *,
    model: str = "gpt-5-mini",
    api_key: str | None = None,
    base_url: str | None = None,
    retarget_profiles: list[RetargetProfile] | None = None,
) -> ShotSpec:
    key = api_key or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set.")
    client = OpenAI(api_key=key, base_url=base_url)
    profiles_json = "\n".join(
        profile.model_dump_json(indent=2, exclude_none=True)
        for profile in (retarget_profiles or [])
    )
    response = client.responses.parse(
        model=model,
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Scene snapshot:\n{snapshot.model_dump_json(indent=2)}\n\n"
                    f"Available retarget profiles (use only an exact supplied profile):\n"
                    f"{profiles_json or 'None supplied.'}\n\nShot brief:\n{brief}"
                ),
            },
        ],
        text_format=ShotSpec,
    )
    if response.output_parsed is None:
        raise RuntimeError("The model did not return a valid ShotSpec.")
    return ShotSpec.model_validate(response.output_parsed)
