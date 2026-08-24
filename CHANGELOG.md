# Changelog

All notable FaceLink changes are recorded here. The project follows Semantic Versioning while
the public API is experimental.

## [Unreleased]

### Added

- Added a non-blocking 128-job Blender queue and a 256-record retention bound with
  terminal-record eviction and live capacity metrics.
- Added protocol 1.10 stable request and job error codes, retry metadata and a structured
  `BridgeError` client exception while preserving the legacy error string.
- Added protocol 1.11 server-side job deadlines, immediate queued-job cancellation and
  cooperative safe-checkpoint cancellation for running Blender jobs.
- Added client cancellation and deadline controls with explicit cancelled, timed-out and
  completion-wins lifecycle metadata.
- Added protocol 1.12 atomic, bounded and sanitized job journals with durable lifecycle and
  transaction checkpoints.
- Added authenticated startup-recovery status and acknowledgement endpoints. Interrupted work
  is reported with rescan or revision-history guidance and is never replayed automatically.
- Added protocol 1.13 explicit health-bootstrap negotiation for exactly 1.12/1.13, per-request
  IDs and HMAC-derived workflow correlation IDs across responses, jobs, errors and journals.
- Added an authenticated, bounded Blender diagnostic endpoint plus `facelink diagnostics` for
  an atomic, privacy-safe local JSON support report with no automatic upload.
- Added Chinese compatibility/migration and threat-model documents covering bridge/data version
  boundaries, safe downgrade behavior, local trust limits and BYOK data egress.

### Security

- Unexpected Blender exceptions now return a generic internal error instead of exposing raw
  implementation details to bridge clients.
- Running patch cancellation uses the existing revision transaction to restore scene, animation,
  camera and audit state instead of injecting an unsafe asynchronous Blender exception.
- Persisted job records exclude payloads, prompts, patch bodies, results, scene snapshots, raw
  errors, file paths and credentials; corrupt journals are quarantined instead of trusted.
- Valid future journal schemas now fail closed without quarantine, downgrade, overwrite or
  automatic replay; unsupported Scene Patch schemas fail before Blender scene mutation.
- Shareable diagnostics use strict field allowlists, bounded events/jobs/size and a second
  sensitive-pattern scan; they exclude paths, scene-facing names, request content and secrets.
- Instance discovery deterministically opens at most 32 records, caps each record at 16 KiB and
  rejects malformed schemas without reflecting their content.
- Client correlation seeds are never echoed or persisted directly; the bridge stores a
  session-derived HMAC identifier for troubleshooting instead.

## [0.3.9] - 2026-08-24

### Changed

- Sanitized the public installer screenshot and editable Blender demo so they contain no
  development-machine user directory.
- Replaced development-machine Blender and FFmpeg paths with explicit parameters, environment
  variables, PATH lookup and conventional installation discovery.
- Switched project documentation examples to portable, repository-relative paths.

### Security

- Added a repository privacy regression test for credential signatures, personal email and
  development-machine paths in tracked release content.
- Future FaceLink commits use the GitHub-generated noreply identity; historical commits remain
  unchanged to avoid a destructive public-history rewrite.

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

[Unreleased]: https://github.com/xuebazhou-blip/FaceLink/compare/v0.3.9...HEAD
[0.3.9]: https://github.com/xuebazhou-blip/FaceLink/releases/tag/v0.3.9
[0.3.8]: https://github.com/xuebazhou-blip/FaceLink/releases/tag/v0.3.8
[0.3.7]: https://github.com/xuebazhou-blip/FaceLink/releases/tag/v0.3.7
[0.3.6]: https://github.com/xuebazhou-blip/FaceLink/releases/tag/v0.3.6
[0.3.5]: https://github.com/xuebazhou-blip/FaceLink/compare/a62dc48...v0.3.6
[0.3.4]: https://github.com/xuebazhou-blip/FaceLink/commit/b14db7e
[0.3.3]: https://github.com/xuebazhou-blip/FaceLink/commit/16d82e7
[0.3.0]: https://github.com/xuebazhou-blip/FaceLink/commit/b6e9751
