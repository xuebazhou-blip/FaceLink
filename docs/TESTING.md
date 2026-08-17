# Testing and acceptance

FaceLink uses two test levels. `pytest` covers pure Python schemas, compiler behavior, bridge
client behavior, CLI, provider contract and a real MCP stdio handshake. Blender acceptance
scripts run inside Blender's own Python runtime and exercise the actual `bpy` mutation path.

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
- Bridge acceptance must pass: localhost binding, bearer authentication, malformed requests,
  concurrent jobs, protocol 1.1 fingerprint agreement, main-thread dispatch, failed-job
  reporting, protocol 1.2 history/rollback, Undo and discovery cleanup.
- The built ZIP must install, enable and load from each Blender version's isolated portable
  extension repository.

Blender is always invoked with `--python-exit-code 1`. Without this flag Blender can print a
Python traceback and still return process exit code zero, which creates a false-green CI
result.

## What this does not prove

The harness deliberately reports live provider calls, visual composition quality, large
production rigs and non-Windows platforms as unverified. Passing acceptance means the
implemented editing/control surface behaves as specified; it does not mean FaceLink already
solves motion generation, rig retargeting or cinematic judgment.
