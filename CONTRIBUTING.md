# Contributing to FaceLink

Small, testable changes are welcome. Please keep the boundary between model planning and
Blender execution explicit: new scene mutations must be represented by a versioned,
validated operation and implemented without arbitrary code execution.

Use GitHub Discussions for setup questions. Before opening an issue, search existing issues
and discussions and reduce the problem to the smallest safe reproduction. Never upload API
keys, bridge tokens, private production assets or third-party `.blend` files without a clear
redistribution license.

## Development loop

```powershell
uv sync --extra dev
$env:FACELINK_BLENDER_EXE='C:\path\to\blender.exe'
./scripts/verify.ps1
```

A pull request that changes the bridge or executor should include both ordinary Python tests
and a Blender headless smoke case. Avoid private `bpy` APIs and state the Blender versions
tested. Do not commit API keys, `.blend` files containing third-party assets, model weights
or generated dependency folders.

For a new operation, update the Pydantic models, deterministic compiler, Blender allowlist,
executor, protocol documentation and tests in the same pull request.

Pull requests must explain the user-visible behavior, editability, review boundary, stale-state
guard and rollback behavior. The repository CI must pass before merge. Participation is governed
by the [Code of Conduct](CODE_OF_CONDUCT.md), and contributions are accepted under the repository's
GPL-3.0-or-later license.
