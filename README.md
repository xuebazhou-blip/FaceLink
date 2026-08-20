# FaceLink

FaceLink turns a constrained shot description into **editable Blender scene animation**.
It is aimed at previs/white-model work: actors, props and cameras remain ordinary Blender
objects with ordinary keyframes, so artists can drag, retime and override the result.

FaceLink is not a text-to-video generator and does not give an LLM unrestricted Python
execution. The model produces a typed `ShotSpec`; FaceLink validates it, compiles it into a
small whitelist of patch operations, stages a human-readable review in Blender, and changes
the scene only after the artist presses **Apply Staged Patch**.

## Current MVP

- scans the open Blender scene and gives objects stable FaceLink IDs;
- compiles `move_to`, `turn_to`, `look_at`, `wait` and `play_clip` beats;
- creates/updates editable transforms, keyframes, cameras and tracking constraints;
- plans transforms in world space and converts them for parented Blender objects;
- exposes the workflow through an MCP server for Codex/ChatGPT-compatible MCP clients;
- supports OpenAI API-key planning with Structured Outputs;
- runs a localhost-only authenticated bridge between the MCP process and Blender;
- supports Blender-side stage/review/apply/discard, persistent audit history and safe
  rollback to a selected current-session revision.
- rejects internally overlapping transform/action timelines, warns before overwriting
  existing keyframes and rejects colliding FaceLink NLA clips;
- previews staged world-space motion paths and predicted camera frustums directly in the
  Blender viewport without creating scene datablocks;
- scans explicitly marked navigation meshes and obstacles, plans deterministic multi-segment
  locomotion paths, and warns when an actor's swept bounds intersect a marked obstacle;
- fingerprints the complete navigation environment so a newly added obstacle or edited
  navigation mesh invalidates an already staged plan;
- inventories armature bone hierarchies and editable Blender Actions, including pose-bone
  channels, rest orientations, frame ranges and deterministic content fingerprints;
- suggests review-only bone maps using deterministic name normalization, then measures mapped
  hierarchy, local rest axes and scale-normalized bone proportions before execution;
- copies compatible Actions through an open `rename_only` bone-map profile, rewrites editable
  FCurve paths, places the result in NLA, and removes created copies during rollback;
- samples reviewed `bake_pose` profiles into ordinary editable target Actions, correcting
  different local rest axes and bone scale with explicit root-motion policy and bounded work;
- evaluates existing self-contained source-rig constraints and drivers with
  `bake_evaluated_pose`, then bakes the final deform-bone pose into an ordinary editable Action;
- optionally transfers Armature object root motion as a placement-preserving relative delta,
  with source-unit or rig-scale-adjusted translation;
- predicts the staged camera frame without creating scene datablocks, measuring target size,
  center offset, safe-area fit, clipping and center-point occlusion before the artist applies;
- rejects a staged plan when a referenced transform, parent link, lock or scene timing value
  changed after the scene scan.

## Supported Blender versions

- Primary: Blender **4.5 LTS** (tested with 4.5.12)
- Minimum: Blender **4.2 LTS**
- Best effort: Blender 5.x

The Blender 4.0.2 installation found on the development machine predates the extension
baseline. FaceLink's source can still be loaded there for smoke testing, but 4.0 is not a
declared supported version.

## Install for development

```powershell
cd E:\FaceLink
$env:UV_CACHE_DIR='E:\CodexData\Work\FaceLink\uv-cache'
uv sync --extra dev
uv run pytest
```

For the reproducible multi-version acceptance matrix, including real extension installation:

```powershell
./scripts/run_acceptance.ps1
```

The harness writes JUnit, coverage, per-Blender JSON and command logs below `artifacts/`.
See [docs/TESTING.md](docs/TESTING.md) for the exact gates and known exclusions.

Build the Blender extension:

```powershell
$env:FACELINK_BLENDER_EXE='C:\path\to\Blender\blender.exe' # optional if on PATH
./scripts/build_extension.ps1
```

Then in Blender 4.5: **Edit → Preferences → Get Extensions → Install from Disk**, choose
`dist/facelink-0.3.6.zip`, enable FaceLink, and open the **FaceLink** tab in the 3D Viewport
sidebar. Press **Start Bridge**.

Run the MCP server:

```powershell
uv run facelink-mcp
```

Example MCP configuration:

```json
{
  "mcpServers": {
    "facelink": {
      "command": "E:\\FaceLink\\.venv\\Scripts\\facelink-mcp.exe",
      "env": {
        "FACELINK_INSTANCE_DIR": "E:\\CodexData\\Work\\FaceLink\\instances"
      }
    }
  }
}
```

The same `FACELINK_INSTANCE_DIR` must be set before launching Blender. If it is omitted,
FaceLink uses the current user's temporary directory.

With an MCP client, the safe default sequence is:

1. `scan_scene`
2. turn the user's natural-language request into a typed shot and call `preview_shot`
3. call `stage_scene_patch`
4. let the user inspect the summary in Blender and press **Apply Staged Patch** or **Discard**

This path uses the model already available in the MCP client; FaceLink itself needs no API
key. `apply_scene_patch` remains available as an explicit power-user bypass.

## BYOK planning

```powershell
$env:OPENAI_API_KEY='your-key'
uv run facelink plan --brief "Cube walks to Marker in 2 seconds, camera follows Cube" `
  --snapshot scene.json --out shot.json
```

Or scan the running Blender scene, plan, compile and stage the result in one command:

```powershell
$env:OPENAI_API_KEY='your-key'
uv run facelink workflow `
  --brief "Cube walks to Marker in 2 seconds, camera follows Cube"
```

The command does not apply anything. Review and approve the staged result in Blender.

To make an existing Action target a compatible armature whose bone names differ, pass a
reviewed open profile:

```powershell
uv run facelink validate-profile `
  --profile profiles/mixamo_to_facelink_compact.json

uv run facelink suggest-profile `
  --snapshot scene.json --source-rig source-armature-id `
  --target-rig target-armature-id --action "Mixamo Walk" `
  --name "Reviewed map" --out suggestion.json

uv run facelink analyze-profile `
  --profile profiles/mixamo_to_facelink_compact.json `
  --snapshot scene.json --source-rig source-armature-id `
  --target-rig target-armature-id --out compatibility.json

uv run facelink plan `
  --brief "Apply Mixamo Walk to the target rig for two seconds" `
  --snapshot scene.json `
  --retarget-profile profiles/mixamo_to_facelink_compact.json `
  --out shot.json
```

Suggestions are never applied automatically and always carry `review_required: true`. The
compatibility result is `safe`, `review`, `bake_required` or `incompatible`. The compiler
blocks `rename_only` when hierarchy, rest orientation or proportions require baking. FaceLink
fingerprints both Actions and referenced rigs, so curve or rest-pose edits after scanning fail
before mutation; it also blocks unscaled pose-bone translation channels across differently
sized rigs. Generated Actions and NLA strips remain ordinary editable Blender data. See
[profiles/README.md](profiles/README.md) and
[examples/retargeted_clip_shot.json](examples/retargeted_clip_shot.json).

When analysis says `bake_required` because local rest axes or rig scale differ, change the
reviewed profile to `adapter: "bake_pose"`, set its explicit `source_rig`, and optionally set
`sample_step` (1-16) and `root_motion` (`scale`, `preserve` or `drop`). FaceLink samples the
source Action's native frame range, writes linear location/rotation/scale keys to a normal
target Action, and puts it in the same editable NLA workflow. Object-level Action channels are
omitted unless `object_motion` is explicit; otherwise root motion must be on a mapped root pose
bone. This first adapter requires equivalent
mapped parent hierarchy and unconstrained source/target deform bones. See
[profiles/mixamo_to_facelink_compact_bake.json](profiles/mixamo_to_facelink_compact_bake.json)
and [examples/baked_retargeted_clip_shot.json](examples/baked_retargeted_clip_shot.json).

When the source Action animates controller bones or custom properties and the source deform
bones receive their final motion through constraints/drivers, use
`adapter: "bake_evaluated_pose"`. The reviewed `bone_map` maps source deform bones—not the
controller channels—to target deform bones. Version 1 permits only dependencies on the same
source armature object/data, rejects external helper objects and scene-driven variables, and
still requires equivalent mapped parent hierarchy plus unconstrained/undriven target bones.
It does not discover controllers or convert IK/FK systems automatically. See
[profiles/controller_to_deform_evaluated_bake.json](profiles/controller_to_deform_evaluated_bake.json)
and [examples/evaluated_retargeted_clip_shot.json](examples/evaluated_retargeted_clip_shot.json).

If overall character movement lives on the source Armature object, add
`object_motion: "preserve"` or `"scale"` to either bake adapter. FaceLink uses the source
object's transform relative to its first sampled frame, applies that delta after the target's
current world transform, and writes ordinary object location/rotation/scale FCurves into the
same generated Action. `scale` multiplies delta translation by the mapped-rig median length
ratio; `preserve` keeps source units. Version 1 requires unparented source/target Armatures with
no object constraints or driven target object transforms. See
[profiles/object_motion_bake.json](profiles/object_motion_bake.json) and
[examples/object_motion_clip_shot.json](examples/object_motion_clip_shot.json).

Inspect or roll back FaceLink revisions from the command line:

```powershell
uv run facelink history
uv run facelink rollback --revision rev-0123456789abcdef
```

Revision metadata is stored in the `.blend` file. Executable rollback snapshots intentionally
remain session-only because they contain live Blender datablock references. Rolling back an
older revision also rolls back every newer FaceLink revision to preserve a linear scene state.

An API key is optional when an MCP client performs the language-model planning itself.
ChatGPT subscriptions and OpenAI API billing are separate; a ChatGPT membership is not an
API key. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the trust boundary.

## Navigation workflow

Select a walkable mesh and use **FaceLink → Navigation → Navmesh**. Select walls, props or
other blocking objects and mark them as **Obstacle**. A `move_to` beat keeps the legacy
straight line by default; set `path_mode` to `navmesh` to route through connected navigation
triangles. The compiler distributes ordinary editable location keyframes by path distance
and forces linear interpolation so curved handles cannot leave the walkable corridor.

Navigation is deliberately explicit. FaceLink does not guess from object names or silently
treat every mesh as an obstacle. Current v0.3.0 planning is projected onto XY and is intended
for single-level previs floors; stacked floors, live moving obstacles and crowd routing are
not yet supported. See [examples/navmesh_walk_shot.json](examples/navmesh_walk_shot.json).

## Camera composition preflight

Camera shots with a target are checked during staging. FaceLink projects the target's
world-space bounds into the predicted camera frame and reports clipping, unsafe margins,
subject size and center offset. A read-only Blender ray cast reports when another object blocks
the target center. `dolly_in` checks both its start and end positions. Thresholds are typed in
`camera.composition`, remain visible in the ShotSpec and can be disabled explicitly. See
[examples/composition_checked_shot.json](examples/composition_checked_shot.json).

This is a deterministic preflight, not an artistic quality score. It does not render, use a
vision model, judge lighting or guarantee that every part of a complex subject is unoccluded.
Version 0.3.3 evaluates perspective cameras without lens shift and reports other projection
types as unsupported instead of returning misleading metrics.

## Repository map

```text
src/facelink/          Core schemas, compiler, bridge client, providers, CLI and MCP server
blender_extension/    Zero-dependency Blender extension and local bridge
schemas/              Portable JSON Schema for integrations
examples/             Example editable shot specifications
tests/                 Unit tests and a Blender headless smoke test
scripts/               Build and verification scripts
docs/                  Architecture, protocol and development notes
```

## Project status

Version 0.3.6 is a creator-review alpha, not yet a production animation system. It performs
bounded transform-aware pose baking for reviewed mappings and can evaluate existing constraints
and drivers when every dependency stays on the explicit source armature. It can also transfer
unparented, unconstrained Armature-object motion without moving the target's starting placement.
It does not infer controllers, translate IK/FK systems, follow external helper objects, solve
different mapped parent hierarchies, handle parented/constrained object roots, synthesize missing
motion or judge the visual result. Multi-level navigation,
multi-shot sequencing and visual diff overlays remain follow-up work.

Before promoting this alpha more broadly, add a short screen recording to this README and
enable GitHub's private vulnerability reporting.
