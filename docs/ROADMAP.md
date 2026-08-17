# Roadmap

## v0.1 — completed foundation

- Stable scene IDs and compact snapshots
- Typed shot schema and deterministic compiler
- Editable transform/camera patch executor
- MCP tools, CLI, localhost bridge and BYOK OpenAI planning
- Headless Blender smoke test and installable extension archive

## v0.2 — current creator-review alpha

- Blender-side stage/review/apply/discard gate
- Structural patch summary: operations, objects, frame span and warnings
- One-command BYOK scan/plan/compile/stage workflow
- MCP workflow that uses the client's existing model without a FaceLink API key
- Automated compatibility matrix for Blender 4.2, 4.5 and 5.x

## v0.2.x — reliability follow-ups

- Persistent revision log with selective rollback
- World/local transform conversion for hierarchies
- Timeline overlap checks and stronger shot invariants
- Viewport overlay for spatial path and camera-frustum review

## v0.3 — useful intelligence

- Navmesh-aware locomotion paths and collision warnings
- Camera composition evaluator using viewport renders
- Rig/action inventory plus open retargeting adapters
- Reusable shot templates and multi-shot sequencing

The technical moat should live in deterministic scene understanding, validation, repair and
cross-version execution—not in a large prompt that competitors can copy.
