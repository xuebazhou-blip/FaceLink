from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .bridge_client import BridgeClient, discover_instances, select_instance
from .compiler import compile_shot
from .models import ScenePatch, SceneSnapshot, ShotSpec
from .provider import plan_with_openai


def _read_json(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_json(payload: Any, path: str | None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if path:
        Path(path).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


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

    plan = commands.add_parser("plan", help="Plan a shot with an OpenAI API key")
    plan.add_argument("--brief", required=True)
    plan.add_argument("--snapshot", required=True)
    plan.add_argument("--model", default="gpt-5-mini")
    plan.add_argument("--base-url")
    plan.add_argument("--out")
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
    elif args.command == "plan":
        snapshot = SceneSnapshot.model_validate(_read_json(args.snapshot))
        shot = plan_with_openai(args.brief, snapshot, model=args.model, base_url=args.base_url)
        _write_json(shot.model_dump(mode="json"), args.out)


if __name__ == "__main__":
    main()
