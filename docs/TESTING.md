# Testing and acceptance

FaceLink uses two test levels. `pytest` covers pure Python schemas, compiler behavior, bridge
client behavior, CLI, provider contract and a real MCP stdio handshake. Blender acceptance
scripts run inside Blender's own Python runtime and exercise the actual `bpy` mutation path.
Each supported version also runs a short hidden-window UI smoke test because GPU draw calls
are unavailable in Blender background mode; the smoke test requires a real viewport callback
before Blender exits automatically.

Run the complete Windows matrix:

```powershell
./scripts/run_acceptance.ps1
```

Or supply explicit Blender executables:

```powershell
./scripts/run_acceptance.ps1 -BlenderExe @(
  'D:\Blender-4.2\blender.exe',
  'D:\Blender-4.5\blender.exe',
  'D:\Blender-5.2\blender.exe'
)
```

Every run writes timestamped command logs, JUnit XML, coverage XML, per-Blender JSON results
and a consolidated report below `artifacts/`. `artifacts/latest-report.md` is the quickest
human-readable summary.

## Acceptance gates

- Ruff must pass.
- All Python tests must pass with at least 85% line/branch-aware aggregate coverage.
- The extension must build and validate on every supplied Blender version.
- Source-level Blender acceptance must pass: registration, stable identity, bounds, locks,
  world/local parent conversion, stale-scene rejection, transform keyframes, interpolation,
  frame rate, constraints, cameras, NLA clips, idempotency, fail-closed behavior and
  transaction rollback, multi-revision rollback and real `.blend` audit persistence. Revision
  boundary coverage also verifies unique revision IDs for repeated patch IDs, unknown-target
  rollback safety, malformed audit-log recovery, the 100-entry audit/50-snapshot limits, and
  that staging or discarding a patch never creates a false history entry.
- Preflight visualization acceptance must verify world/local path conversion, finite camera
  frustum geometry, overlay show/hide/cleanup, zero scene mutation while staged, internal
  timeline rejection, existing-keyframe warnings and existing FaceLink NLA collision safety.
- Navigation acceptance must verify stable cross-runtime fingerprints, valid triangulation,
  invalid/non-manifold topology rejection, deterministic connected paths, disconnected and
  outside-mesh failures, distance-based unique frames, swept-bounds collision/height behavior,
  Blender role operators, stale-environment rejection and editable multi-key execution.
- Camera composition acceptance must verify good framing, minimum/maximum subject size,
  safe-area and clipping metrics, center occlusion identity, dolly start/end samples, missing
  target bounds, disabled analysis, malformed-threshold rejection, receipt propagation and
  zero scene mutation while staged. Animated targets must be sampled at declared camera frames
  without changing the user's current frame, and execution must use the same start-frame pose.
- Rig/Action acceptance must create actual Blender armatures and pose-bone keyframes, verify
  bounded inventories and stable fingerprints, strict source/target coverage, unique targets,
  stale-Action rejection, source preservation, editable copied FCurve paths, non-pose channel
  preservation, NLA idempotency and removal of created Actions after full rollback. Rig
  diagnostics additionally verify deterministic suggestion conflicts, parent-local rest-axis
  angles, uniform-scale normalization, proportion drift, scaled pose-translation rejection,
  source-rig ambiguity, compiler rejection of bake-required mappings and stale Edit Mode
  rest-pose rejection. Sampled-bake coverage uses real rigs whose rest axes differ by 90
  degrees and lengths by 2x, checks all root-motion policies, exact/fractional sample endpoints,
  quaternion continuity, linear editable output, idempotent reuse, source/timeline preservation,
  rollback cleanup, old-schema rejection, constraint boundaries and workload caps.
  Evaluated-bake coverage drives deform bones from a same-rig controller constraint and a
  custom-property driver, verifies final-pose inversion, Action/NLA/pose/frame restoration,
  stale driver/property guards, deterministic expression restrictions, external constraint and
  driver rejection, bounded output names and cleanup after an injected bake failure.
  Object-motion coverage verifies placement-preserving deltas, rig-scaled translation,
  object-only direct Actions, editable object FCurves, omitted-channel restoration, rollback and
  fail-closed parent/constraint/driver/rotation-mode/singular-transform boundaries.
- Bridge acceptance must pass: localhost binding, bearer authentication, malformed requests,
  concurrent jobs, protocol 1.1 fingerprint agreement, main-thread dispatch, failed-job
  reporting, protocol 1.2 history/rollback, protocol 1.3 navigation snapshots, protocol 1.4
  composition diagnostics, protocol 1.5 rig/action capabilities, protocol 1.6 rest-geometry
  guards, protocol 1.7 sampled-pose-bake, protocol 1.8 evaluated-pose-bake, protocol 1.9
  object-motion-bake capability and
  preview diagnostics, Undo and discovery
  cleanup.
- The built ZIP must install, enable and load from each Blender version's isolated portable
  extension repository.

Blender is always invoked with `--python-exit-code 1`. Without this flag Blender can print a
Python traceback and still return process exit code zero, which creates a false-green CI
result.

## What this does not prove

The harness deliberately reports live provider calls, visual composition quality, large
production control rigs, automatic IK/FK/controller discovery, external dependency graphs and
parented/constrained Armature-object roots, plus non-Windows platforms as unverified. Passing
acceptance means the implemented direct and
self-contained evaluated deform-skeleton pose-bakes and editing surface behave as specified;
it does not mean FaceLink solves motion generation, general
character retargeting or cinematic judgment.
