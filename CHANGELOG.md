# Changelog

All notable FaceLink changes are recorded here. The project follows Semantic Versioning while
the public API is experimental.

## [Unreleased]

## [0.3.8] - 2026-08-21

### Added

- A single-file WinForms graphical installer with embedded, checksum-verified FaceLink host and
  Blender extension payloads.
- Idempotent `facelink configure-mcp` support for the configuration shared by ChatGPT Desktop,
  Codex CLI and the Codex IDE extension.
- A real Blender-rendered README demo, MP4, poster and editable `.blend` source scene.
- Installer build and non-mutating embedded-payload self-test gates in the acceptance harness.

### Security

- MCP configuration is limited to a marked managed block, preserves unrelated TOML, creates a
  timestamped backup before updates and refuses unmanaged name collisions.
- The generated MCP block contains paths and a discovery directory but never an API key or
  Blender bridge bearer token.

## [0.3.7] - 2026-08-21

### Added

- `facelink doctor` human and JSON diagnostics for Python, Blender, the MCP launcher, discovery,
  live bridge capabilities and optional BYOK configuration.
- A checksum-verifying Windows installer for the Python host and Blender extension, including a
  non-mutating `-PlanOnly` mode.
- Explicit product packaging policy: Blender remains an externally detected dependency and is
  never copied into the FaceLink release.

### Security

- Doctor reports API-key presence only and never emits provider keys or bridge bearer tokens.
- The installer can verify both release artifacts against `SHA256SUMS.txt` before mutation.

## [0.3.6] - 2026-08-21

### Added

- Placement-preserving Armature object-motion baking for direct and evaluated pose adapters.
- `preserve` and rig-scaled `scale` object-translation policies.
- Editable object location, rotation and scale FCurves in generated Actions and NLA strips.
- Transactional restoration for source transforms, poses, Action/NLA state and driver-control
  custom properties.
- Blender 4.2, 4.5 and 5.2 acceptance coverage for object motion and failure boundaries.

### Security

- Object-motion baking fails closed for unsupported parenting, object constraints, target
  transform drivers, singular transforms and stale rig rotation modes.

## [0.3.5] - 2026-08-18

- Added evaluated final-pose baking for self-contained constraints, drivers and control
  properties on an explicit source rig.

## [0.3.4] - 2026-08-18

- Added deterministic sampled pose baking across different rest axes and rig scales.

## [0.3.3] - 2026-08-17

- Added rig compatibility analysis, Action inventories and reviewed rename-only retargeting.

## [0.3.0] - 2026-08-17

- Added deterministic navigation and collision preflight for single-level previs scenes.

[Unreleased]: https://github.com/xuebazhou-blip/FaceLink/compare/v0.3.8...HEAD
[0.3.8]: https://github.com/xuebazhou-blip/FaceLink/releases/tag/v0.3.8
[0.3.7]: https://github.com/xuebazhou-blip/FaceLink/releases/tag/v0.3.7
[0.3.6]: https://github.com/xuebazhou-blip/FaceLink/releases/tag/v0.3.6
[0.3.5]: https://github.com/xuebazhou-blip/FaceLink/compare/a62dc48...v0.3.6
[0.3.4]: https://github.com/xuebazhou-blip/FaceLink/commit/b14db7e
[0.3.3]: https://github.com/xuebazhou-blip/FaceLink/commit/16d82e7
[0.3.0]: https://github.com/xuebazhou-blip/FaceLink/commit/b6e9751
