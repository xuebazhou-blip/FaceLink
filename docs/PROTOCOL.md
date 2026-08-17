# Local bridge protocol 1.6

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

## Camera composition preflight

Protocol 1.4 adds the `camera_composition_preflight` capability. An `ensure_camera` payload may
contain a typed `composition` object with `enabled`, `safe_margin`, `min_subject_height`,
`max_subject_height`, `max_center_offset` and `check_occlusion`. Unsupported fields, wrong
types, non-finite values and inverted height thresholds fail before staging.

Stage summaries include `composition.evaluated_count`, `warning_count`, per-camera normalized
frame metrics and stable warning codes: `composition_target_unavailable`,
`composition_camera_unsupported`, `subject_behind_camera`, `subject_clipped`,
`subject_outside_safe_area`, `subject_too_small`, `subject_too_large`, `subject_off_center` and
`subject_occluded`. The analysis is read-only;
it neither creates the proposed camera nor moves the target. `dolly_in` produces separate
`start` and `end` samples with explicit frame and predicted camera-location fields. Animated
targets are evaluated at those declared frames and the user's current frame is restored.

## Rig and Action inventory

Protocol 1.5 adds Scene Snapshot 1.3 `rigs` and `actions`. A rig record identifies its
armature entity and contains at most 1,024 bones with parent, deform flag and local rest-space
head/tail values. A snapshot contains at most 64 rigs. An Action record contains frame range,
FCurve/keyframe counts, sorted pose-bone names, sorted data paths and an
`action-<24 lowercase hex>` fingerprint. Snapshots contain at most 512 Actions, with bounded
curve and keyframe counts.

Compiler-generated Scene Patch 1.3 payloads include `action_fingerprints`. Its keys must
exactly equal the Action names used by `play_clip` operations. Blender checks each current
Action's full editable curve payload during stage and apply. Missing, renamed or modified
Actions are rejected before mutation.

A `play_clip` payload may contain:

```json
{
  "retarget": {
    "adapter": "rename_only",
    "bone_map": {"sourceBone": "targetBone"},
    "source_rig": "source-armature-entity-id",
    "strict": true
  }
}
```

The adapter accepts only explicit non-empty mappings to unique target names. Strict mode
requires every pose bone referenced by the Action to appear in the map. Every resolved bone
must exist on the target armature and fallback mappings may not collide. Stage summaries add
`action_guarded`, `retargeted_action_count` and `retargets`. Applying creates or reuses a
deterministically named copied Action, rewrites only pose-bone FCurve paths/group names and
places it in the existing editable FaceLink NLA track. Non-pose channels are preserved.

Health advertises `rig_action_inventory`, `action_fingerprint` and `rename_only_retarget`.
The adapter does not perform rest-pose, axis, scale, IK/FK or root-motion correction.

## Rest-geometry compatibility and Rig guard

Protocol 1.6 adds Scene Snapshot 1.4. Each bone includes a normalized `rest_rotation`
quaternion derived from Blender's local rest matrix, and each rig includes a
`rig-<24 lowercase hex>` fingerprint over sorted bone names, parents, deform flags, heads,
tails and canonicalized rest rotations.

Scene Patch 1.4 adds `rig_fingerprints`. For every pose Action, its target armature must be
guarded; a retarget operation also guards the explicit or deterministically inferred source
armature. The key set must exactly match those referenced rigs. Blender recalculates every
fingerprint during stage and apply, rejecting deleted bones, renames, reparenting, head/tail
edits, roll/rest-axis changes and deform-flag changes before any Action or NLA data is created.
Stage summaries expose `rig_guarded` and the Blender panel displays the guard.

The sidecar's `suggest_retarget_profile_map` MCP tool and `suggest-profile` CLI command match
only exact names, punctuation/case-normalized names and a bounded alias table. Output always
contains `review_required: true`, unresolved names and ambiguity conflicts. The
`analyze_retarget_profile`/`analyze-profile` pair reports mapped hierarchy, parent-local axis
and rest-rotation angles, median uniform rig scale and per-bone proportion deviation. Outcomes
are `safe`, `review`, `bake_required` or `incompatible`; the compiler refuses a
`rename_only` operation in the latter two states. It also refuses pose-bone location channels
when the median target/source bone scale differs, because raw FCurve translation values would
otherwise be applied in the wrong scale.

Health additionally advertises `rig_rest_geometry` and `rig_fingerprint`. These diagnostics
identify when pose baking is necessary; they do not perform the future transform-aware bake.

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
