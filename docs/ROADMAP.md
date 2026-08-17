# Roadmap

## v0.1 — completed foundation

- Stable scene IDs and compact snapshots
- Typed shot schema and deterministic compiler
- Editable transform/camera patch executor
- MCP tools, CLI, localhost bridge and BYOK OpenAI planning
- Headless Blender smoke test and installable extension archive

## v0.2 — completed creator-review alpha

- Blender-side stage/review/apply/discard gate
- Structural patch summary: operations, objects, frame span and warnings
- One-command BYOK scan/plan/compile/stage workflow
- MCP workflow that uses the client's existing model without a FaceLink API key
- Automated compatibility matrix for Blender 4.2, 4.5 and 5.x

## v0.2.1 — completed scene-consistency release

- World-space snapshots and parent-aware object/camera location conversion
- Patch-scoped scene fingerprints checked at stage and apply time
- Fingerprints evaluate at the original scan frame, so timeline scrubbing is not an edit
- Backward-compatible execution of unguarded local-space protocol 1.0 patches

## v0.2.2 — completed revision-history release

- Persistent per-Scene audit history stored inside `.blend` files
- Unique revision IDs and MCP/CLI/Blender history inspection
- Safe rollback-to that reverses the target and every newer FaceLink revision
- Explicit separation between persistent metadata and session-only executable snapshots

## v0.2.3 — completed preflight-visualization release

- Positive-duration and frame-quantization invariants for motion/action beats
- Fail-closed internal timeline and FaceLink NLA overlap checks
- Existing-keyframe warnings in stage summaries, receipts and revision audit records
- Non-mutating viewport overlay for staged paths and predicted camera frustums
- Overlay visibility control and automatic cleanup on apply, discard, file load and unload

## v0.3.0 — completed navigation-intelligence release

- Explicit Blender UI roles for navmeshes and obstacles
- Scene Snapshot 1.2 world-space navigation triangles and global navigation fingerprint
- Deterministic polygon-adjacency A* with distance-timed editable keyframes
- Swept actor-bounds collision warnings for marked static obstacles
- Fail-closed topology, disconnected path, outside-mesh and stale-environment checks

## v0.3.1 — completed camera-composition preflight release

- Typed composition thresholds carried from ShotSpec into the staged camera operation
- Non-mutating perspective projection of target world bounds into normalized frame space
- Explainable size, center, safe-area, frame-edge and clip-plane diagnostics
- Read-only center-ray occlusion detection plus start/end evaluation for dolly shots
- Composition diagnostics exposed in Blender review summaries and bridge protocol 1.4

## v0.3.2 — completed rig/action compatibility release

- Scene Snapshot 1.3 bounded rig hierarchies and Action channel inventories
- Source-Action content fingerprints checked during both stage and apply
- Open, validated JSON retarget profiles with CLI and MCP support
- Strict `rename_only` Action copy/FCurve rewrite into editable NLA data
- Explainable missing-source, missing-target and target-collision failures
- Idempotent repeated apply plus transaction and revision rollback cleanup

## v0.3.3 — current rig-compatibility diagnostics release

- Scene Snapshot 1.4 local rest rotations and deterministic Rig fingerprints
- Review-only exact, normalized and controlled-alias bone-map suggestions
- Per-bone hierarchy, rest-axis, local-rest-rotation and relative-proportion metrics
- `safe`, `review`, `bake_required` and `incompatible` compatibility outcomes
- Compiler rejection when `rename_only` would require transform-aware pose baking
- Stale rest-pose guards checked at both stage and apply
- CLI and MCP profile suggestion/analysis tools

## v0.3.x — remaining useful intelligence

- Optional render/vision-based artistic composition evaluator
- Rest-pose/axis/proportion-aware retarget adapter with root-motion policy
- Reusable shot templates and multi-shot sequencing

The technical moat should live in deterministic scene understanding, validation, repair and
cross-version execution—not in a large prompt that competitors can copy.
