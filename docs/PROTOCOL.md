# Local bridge protocol 1.1

The Blender extension writes one JSON discovery record per running Blender process. The
directory is `${FACELINK_INSTANCE_DIR}` when configured, otherwise
`${TEMP}/facelink/instances`.

All requests require `Authorization: Bearer <token>` from that record.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/health` | Static protocol and capability information |
| `POST` | `/v1/jobs` | Queue one of the jobs below |
| `GET` | `/v1/jobs/{id}` | Poll a job until succeeded or failed |

Supported jobs are `scan_scene`, `stage_patch`, `get_staged_patch`,
`apply_staged_patch`, `discard_staged_patch`, `apply_patch` and `undo`.

`stage_patch` performs the full scene-aware preflight and stores one deep-copied patch, but
does not mutate scene objects, animation data, cameras or frame settings. A new staged patch
replaces the previous staged patch and reports its ID. `apply_staged_patch` clears staging
only after a successful transactional apply; `discard_staged_patch` clears it without any
scene mutation. Staging is in memory and is cleared when a new `.blend` file loads.

## Scene consistency and transform space

Protocol 1.1 snapshots declare `transform_space: "WORLD"` and include `frame_current`.
Compiler-generated transform and camera operations also declare `space: "WORLD"`; Blender
converts those values into the object's editable local channels at each keyframe.

Compiler-generated patches contain a SHA-256-derived `scene_fingerprint`, the referenced
entity IDs and the frame at which the scene was scanned. Before staging and again before
applying, Blender hashes the current world transforms, parent links, locks and scene timing
for only those referenced entities. A mismatch fails closed with an instruction to scan and
plan again. Unrelated objects are deliberately excluded. Legacy protocol 1.0 patches remain
accepted and use local transform space when `space` is absent, but they have no stale-scene
guard.

Scene access is asynchronous because Blender's Python API is not thread-safe. HTTP handler
threads only enqueue data; a registered `bpy.app.timer` performs every scene read or write.
