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

## v0.3.1 — current camera-composition preflight release

- Typed composition thresholds carried from ShotSpec into the staged camera operation
- Non-mutating perspective projection of target world bounds into normalized frame space
- Explainable size, center, safe-area, frame-edge and clip-plane diagnostics
- Read-only center-ray occlusion detection plus start/end evaluation for dolly shots
- Composition diagnostics exposed in Blender review summaries and bridge protocol 1.4

## v0.3.x — remaining useful intelligence

- Optional render/vision-based artistic composition evaluator
- Rig/action inventory plus open retargeting adapters
- Reusable shot templates and multi-shot sequencing

The technical moat should live in deterministic scene understanding, validation, repair and
cross-version execution—not in a large prompt that competitors can copy.
