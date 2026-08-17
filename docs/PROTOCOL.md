# Local bridge protocol 1.3

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

Protocol 1.2 also supports `list_revisions` and `rollback_revision`. The rollback payload is
`{"revision_id":"rev-..."}`. A rollback restores the selected revision's pre-apply state and
all newer FaceLink revisions in reverse order. It never removes a middle revision while
leaving dependent newer edits applied.

`stage_patch` performs the full scene-aware preflight and stores one deep-copied patch, but
does not mutate scene objects, animation data, cameras or frame settings. A new staged patch
replaces the previous staged patch and reports its ID. `apply_staged_patch` clears staging
only after a successful transactional apply; `discard_staged_patch` clears it without any
scene mutation. Staging is in memory and is cleared when a new `.blend` file loads.

Stage summaries may include additive `timeline_warning_count` and `preview` fields. `preview`
reports path, camera-frustum and line-segment counts plus current overlay visibility; it does
not expose GPU or Blender datablock handles. Protocol 1.2 clients that ignore these fields
remain compatible. Health capabilities advertise `timeline_diagnostics` and
`viewport_preview` when they are available.

Internally overlapping operations on the same entity/channel and collisions with existing
FaceLink NLA strips fail preflight. Existing transform keyframes in the requested frame span
are non-blocking warnings shown in the stage summary and copied into the eventual receipt and
revision audit record.

## Navigation snapshot and guard

Protocol 1.3 adds Scene Snapshot 1.2 navigation data. Only Blender mesh objects explicitly
marked `facelink_navmesh` are exported, triangulated in world space, and capped at 20,000
vertices and 20,000 triangles per object, with at most 32 navigation meshes per snapshot.
Objects marked `facelink_obstacle` contribute their world-space bounds and are capped at 2,000
per scene. The Blender panel provides exclusive Navmesh, Obstacle and Clear actions so
integrations do not need to manipulate custom properties directly.

Compiler-generated Scene Patch 1.2 payloads include a `navigation_environment_fingerprint`.
It hashes every marked navigation triangle and obstacle bound, including object additions and
removals. Blender verifies the value during both stage and apply. Health capabilities expose
`navigation_mesh_paths`, `collision_warnings` and `navigation_fingerprint`.

`move_to.path_mode` remains `direct` by default. `navmesh` selects an explicitly named mesh
or deterministically chooses a mesh containing both endpoints, runs polygon-adjacency A*, and
emits multiple ordinary world-space location keys. Marked obstacle intersections are warnings;
invalid topology, disconnected paths, endpoints outside the mesh and insufficient frames are
errors.

## Scene consistency and transform space

Protocol 1.1+ snapshots declare `transform_space: "WORLD"` and include `frame_current`.
Compiler-generated transform and camera operations also declare `space: "WORLD"`; Blender
converts those values into the object's editable local channels at each keyframe.

Compiler-generated patches contain a SHA-256-derived `scene_fingerprint`, the referenced
entity IDs and the frame at which the scene was scanned. Before staging and again before
applying, Blender hashes the current world transforms, parent links, locks and scene timing
for only those referenced entities. A mismatch fails closed with an instruction to scan and
plan again. Unrelated objects are deliberately excluded. Legacy protocol 1.0 patches remain
accepted and use local transform space when `space` is absent, but they have no stale-scene
guard.

## Revision history

Each successful apply receives a unique `revision_id`. Audit metadata—including patch ID,
title, operation types, affected objects, warnings, timestamps and applied/reverted status—is
stored as JSON on the Blender Scene and therefore survives `.blend` save/load. Up to 100
audit entries are retained.

The actual rollback snapshots contain live Blender datablock references and remain in memory,
with a maximum of 50 revisions. After reopening a `.blend`, history entries remain visible
but report `rollback_available: false`. This separation prevents a persisted log from being
mistaken for a safe executable snapshot.

Scene access is asynchronous because Blender's Python API is not thread-safe. HTTP handler
threads only enqueue data; a registered `bpy.app.timer` performs every scene read or write.
