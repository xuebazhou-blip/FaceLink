# FaceLink

FaceLink turns a constrained shot description into **editable Blender scene animation**.
It is aimed at previs/white-model work: actors, props and cameras remain ordinary Blender
objects with ordinary keyframes, so artists can drag, retime and override the result.

FaceLink is not a text-to-video generator and does not give an LLM unrestricted Python
execution. The model produces a typed `ShotSpec`; FaceLink validates it, compiles it into a
small whitelist of patch operations, previews the changes, then lets Blender apply them on
its main thread.

## Current MVP

- scans the open Blender scene and gives objects stable FaceLink IDs;
- compiles `move_to`, `turn_to`, `look_at`, `wait` and `play_clip` beats;
- creates/updates editable transforms, keyframes, cameras and tracking constraints;
- exposes the workflow through an MCP server for Codex/ChatGPT-compatible MCP clients;
- supports OpenAI API-key planning with Structured Outputs;
- runs a localhost-only authenticated bridge between the MCP process and Blender;
- supports preview-before-apply and an in-memory FaceLink revision undo stack.

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
`dist/facelink-0.1.0.zip`, enable FaceLink, and open the **FaceLink** tab in the 3D Viewport
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

## BYOK planning

```powershell
$env:OPENAI_API_KEY='your-key'
uv run facelink plan --brief "Cube walks to Marker in 2 seconds, camera follows Cube" `
  --snapshot scene.json --out shot.json
```

An API key is optional when an MCP client performs the language-model planning itself.
ChatGPT subscriptions and OpenAI API billing are separate; a ChatGPT membership is not an
API key. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the trust boundary.

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

This is a functional alpha/MVP, not yet a production animation system. Character rig
retargeting, collision-aware path planning, multi-shot sequencing and visual diff overlays
are intentionally listed as follow-up work rather than hidden behind unreliable prompts.

Before publishing your fork, replace the placeholder GitHub URLs in `pyproject.toml`, add a
short screen recording to this README, and enable GitHub's private vulnerability reporting.
