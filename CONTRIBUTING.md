# Contributing to FaceLink

Small, testable changes are welcome. Please keep the boundary between model planning and
Blender execution explicit: new scene mutations must be represented by a versioned,
validated operation and implemented without arbitrary code execution.

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

