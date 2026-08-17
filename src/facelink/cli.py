from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .bridge_client import BridgeClient, discover_instances, select_instance
from .compiler import compile_shot
from .models import RetargetProfile, ScenePatch, SceneSnapshot, ShotSpec
from .provider import plan_with_openai
from .retargeting import analyze_rig_compatibility, suggest_retarget_profile


def _read_json(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_json(payload: Any, path: str | None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if path:
        Path(path).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


def _read_profiles(paths: list[str]) -> list[RetargetProfile]:
    return [RetargetProfile.model_validate(_read_json(path)) for path in paths]


def _rig(snapshot: SceneSnapshot, entity_id: str):
    rig = next((item for item in snapshot.rigs if item.entity_id == entity_id), None)
    if rig is None:
        raise ValueError(f"Rig inventory '{entity_id}' was not found in the snapshot")
    return rig


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="facelink")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("instances", help="List running Blender bridges")

    scan = commands.add_parser("scan", help="Scan the active Blender scene")
    scan.add_argument("--instance")
    scan.add_argument("--out")

    preview = commands.add_parser("preview", help="Compile a shot to a patch")
    preview.add_argument("--shot", required=True)
    preview.add_argument("--snapshot", required=True)
    preview.add_argument("--out")

    apply = commands.add_parser("apply", help="Apply a reviewed patch")
    apply.add_argument("--patch", required=True)
    apply.add_argument("--instance")

    history = commands.add_parser("history", help="List FaceLink revisions for a Blender scene")
    history.add_argument("--instance")

    rollback = commands.add_parser(
        "rollback", help="Undo a revision and every newer FaceLink revision"
    )
    rollback.add_argument("--revision", required=True)
    rollback.add_argument("--instance")

    plan = commands.add_parser("plan", help="Plan a shot with an OpenAI API key")
    plan.add_argument("--brief", required=True)
    plan.add_argument("--snapshot", required=True)
    plan.add_argument("--model", default="gpt-5-mini")
    plan.add_argument("--base-url")
    plan.add_argument("--retarget-profile", action="append", default=[])
    plan.add_argument("--out")

    workflow = commands.add_parser(
        "workflow", help="Plan a natural-language shot and stage it in Blender for approval"
    )
    workflow.add_argument("--brief", required=True)
    workflow.add_argument("--instance")
    workflow.add_argument("--model", default="gpt-5-mini")
    workflow.add_argument("--base-url")
    workflow.add_argument("--retarget-profile", action="append", default=[])
    workflow.add_argument("--out")

    profile = commands.add_parser(
        "validate-profile", help="Validate and normalize an open retarget profile"
    )
    profile.add_argument("--profile", required=True)
    profile.add_argument("--out")

    analyze_profile = commands.add_parser(
        "analyze-profile",
        help="Measure retarget compatibility for two rig inventories",
    )
    analyze_profile.add_argument("--profile", required=True)
    analyze_profile.add_argument("--snapshot", required=True)
    analyze_profile.add_argument("--source-rig", required=True)
    analyze_profile.add_argument("--target-rig", required=True)
    analyze_profile.add_argument("--out")

    suggest_profile = commands.add_parser(
        "suggest-profile", help="Suggest a deterministic bone map that requires human review"
    )
    suggest_profile.add_argument("--snapshot", required=True)
    suggest_profile.add_argument("--source-rig", required=True)
    suggest_profile.add_argument("--target-rig", required=True)
    suggest_profile.add_argument("--name", required=True)
    suggest_profile.add_argument("--action")
    suggest_profile.add_argument("--out")
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command == "instances":
        _write_json(
            [
                {
                    "instance_id": item.instance_id,
                    "scene_name": item.scene_name,
                    "blender_version": item.blender_version,
                }
                for item in discover_instances()
            ],
            None,
        )
    elif args.command == "scan":
        result = BridgeClient(select_instance(args.instance)).run_job("scan_scene")
        _write_json(result, args.out)
    elif args.command == "preview":
        shot = ShotSpec.model_validate(_read_json(args.shot))
        snapshot = SceneSnapshot.model_validate(_read_json(args.snapshot))
        _write_json(compile_shot(shot, snapshot).model_dump(mode="json"), args.out)
    elif args.command == "apply":
        patch = ScenePatch.model_validate(_read_json(args.patch))
        result = BridgeClient(select_instance(args.instance)).run_job(
            "apply_patch", {"patch": patch.model_dump(mode="json")}
        )
        _write_json(result, None)
    elif args.command == "history":
        result = BridgeClient(select_instance(args.instance)).run_job("list_revisions")
        _write_json(result, None)
    elif args.command == "rollback":
        result = BridgeClient(select_instance(args.instance)).run_job(
            "rollback_revision", {"revision_id": args.revision}
        )
        _write_json(result, None)
    elif args.command == "plan":
        snapshot = SceneSnapshot.model_validate(_read_json(args.snapshot))
        shot = plan_with_openai(
            args.brief,
            snapshot,
            model=args.model,
            base_url=args.base_url,
            retarget_profiles=_read_profiles(args.retarget_profile),
        )
        _write_json(shot.model_dump(mode="json"), args.out)
    elif args.command == "workflow":
        instance = select_instance(args.instance)
        client = BridgeClient(instance)
        snapshot = SceneSnapshot.model_validate(client.run_job("scan_scene"))
        shot = plan_with_openai(
            args.brief,
            snapshot,
            model=args.model,
            base_url=args.base_url,
            retarget_profiles=_read_profiles(args.retarget_profile),
        )
        patch = compile_shot(shot, snapshot)
        staged = client.run_job("stage_patch", {"patch": patch.model_dump(mode="json")})
        _write_json(
            {
                "instance_id": instance.instance_id,
                "brief": args.brief,
                "shot_spec": shot.model_dump(mode="json"),
                "patch": patch.model_dump(mode="json"),
                "review": staged,
                "next_step": "Review the staged patch in Blender, then Apply or Discard it.",
            },
            args.out,
        )
    elif args.command == "validate-profile":
        profile = RetargetProfile.model_validate(_read_json(args.profile))
        _write_json(profile.model_dump(mode="json", exclude_none=True), args.out)
    elif args.command == "analyze-profile":
        profile = RetargetProfile.model_validate(_read_json(args.profile))
        snapshot = SceneSnapshot.model_validate(_read_json(args.snapshot))
        report = analyze_rig_compatibility(
            _rig(snapshot, args.source_rig),
            _rig(snapshot, args.target_rig),
            profile,
        )
        _write_json(report.model_dump(mode="json"), args.out)
    elif args.command == "suggest-profile":
        snapshot = SceneSnapshot.model_validate(_read_json(args.snapshot))
        source_bones = None
        if args.action:
            action = next((item for item in snapshot.actions if item.name == args.action), None)
            if action is None:
                raise ValueError(f"Action inventory '{args.action}' was not found in the snapshot")
            source_bones = set(action.pose_bones)
        suggestion = suggest_retarget_profile(
            _rig(snapshot, args.source_rig),
            _rig(snapshot, args.target_rig),
            name=args.name,
            source_bones=source_bones,
        )
        _write_json(suggestion.model_dump(mode="json"), args.out)


if __name__ == "__main__":
    main()
