# Local bridge protocol 1.9

The Blender extension writes one JSON discovery record per running Blender process. The
directory is `${FACELINK_INSTANCE_DIR}` when configured, otherwise
`${TEMP}/facelink/instances`.

All requests require `Authorization: Bearer <token>` from that record. Health includes the
additive `facelink_version` field so sidecars can report extension/host version skew without
reading or exposing the discovery token.

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
Protocol 1.7 extends this opaque Rig fingerprint with each pose bone's rotation mode because
the generated bake Action must choose Euler, quaternion or axis-angle FCurve channels. Changing
that mode after staging therefore invalidates the patch before mutation.
Protocol 1.8 also fingerprints pose-constraint stacks, object/Armature-data drivers and custom
properties on the armature and its pose bones. A controller, driver expression or control
property edit after staging therefore invalidates an evaluated bake before mutation.
Protocol 1.9 includes the Armature object's rotation representation because object-motion
output must select Euler, quaternion or axis-angle FCurve channels deterministically.

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

Health additionally advertises `rig_rest_geometry` and `rig_fingerprint`.

## Sampled pose bake

Protocol 1.7 adds the `sampled_pose_bake` capability. A reviewed Scene Patch 1.4
`play_clip.retarget` may use:

```json
{
  "adapter": "bake_pose",
  "bone_map": {"sourceBone": "targetBone"},
  "source_rig": "source-armature-entity-id",
  "strict": true,
  "sample_step": 1,
  "root_motion": "scale"
}
```

`source_rig` is mandatory. `sample_step` is an integer from 1 through 16 and defaults to 1.
The source Action's exact first and last frames are always included, including fractional end
frames. `root_motion` defaults to `scale`: translation on a mapped root bone is multiplied by
the median target/source mapped-bone length ratio. `preserve` keeps source units and `drop`
zeros root-bone translation. Translation on non-root pose bones uses that bone's individual
target/source length ratio. Non-pose object channels are omitted unless `object_motion` is
explicitly enabled as described below.

Blender evaluates the Action on a temporary source-armature copy and samples each mapped
`PoseBone.matrix_basis`. That transform is relative to the source bone's parent and rest bone;
writing it through the target bone's own local basis makes different target rest axes evaluate
naturally. The output contains ordinary location, scale and the target bone's current rotation
representation, with linear keys, and is placed in an editable NLA strip. Quaternion signs and
Euler compatibility are stabilized between samples.

The first bake adapter requires `strict: true`, an equivalent fully mapped parent hierarchy,
non-zero mapped bone lengths, ordinary pose transform channels, and no pose constraints or
transform drivers on mapped source or target bones. Armatures must be outside Edit Mode and a
Blender 4.4+ source Action may have at most one slot. Control-rig IK/FK, constraint and driver
evaluation are not inferred. A source Action must contain pose-bone channels unless explicit
object motion is the only requested output.
Sampling is capped at 20,000 frames, 10,000 FCurves and 200,000 keyframe values. Stage summary
records the adapter, sample count, root-motion policy and deterministic output name. That name
includes the source Action fingerprint, bake settings and both Rig fingerprints; repeat apply
reuses the same generated Action, while rollback removes it when unused.

## Evaluated pose bake

Protocol 1.8 adds the `evaluated_pose_bake` capability and the
`bake_evaluated_pose` adapter. Its JSON fields, sampling limits, root-motion policy, editable
output, deterministic naming and Scene Patch 1.4 guards match `bake_pose`. The semantic
difference is important: `bone_map` names source and target **deform/output bones**, while the
source Action may animate different controller bones or pose custom properties.

At each sample Blender evaluates the Action on the original explicit source armature, including
its existing self-contained constraints and drivers. FaceLink reads each mapped source
`PoseBone.matrix` (the final pose), converts it back to local basis with
`Bone.convert_local_to_pose(..., invert=True)`, and keys the corresponding target deform bone.
The source Action/slot, NLA mute flags, pose and scene frame are restored transactionally.

Version 1 fails closed unless the source and target are distinct armatures, mapped parent
hierarchies match, the target mapped bones have no constraints or transform drivers, and every
source constraint/driver dependency points only to the same source armature object or its
Armature datablock. External helper objects, Actions referenced by constraints, Scene/context
driver variables, external rigs, automatic controller discovery and IK/FK-system conversion
are not supported. Scripted drivers are limited to declared variables, arithmetic and a fixed
set of deterministic math functions; implicit `self`, `frame`, custom driver namespaces and
arbitrary expression syntax are rejected. Object-level Action channels are not copied unless
`object_motion` is explicit, although custom-property channels may serve as driver inputs.

## Armature object motion bake

Protocol 1.9 adds the `object_motion_bake` capability. Either pose-bake adapter may set:

```json
{"object_motion": "preserve"}
```

The mode is optional; omission retains the previous bone-only behavior. `preserve` keeps the
source object's translation units. `scale` multiplies translation by the median mapped-bone
target/source length ratio. Both modes transfer the full location/rotation/scale delta relative
to the source Action's first exact sample, compose it after the target's current world transform,
and key ordinary object transform FCurves in the same generated Action. Thus the first generated
key preserves target placement instead of teleporting it to the source rig's coordinates.

Version 1 requires at least one source Action object-transform channel, unparented source and
target Armatures, non-singular transforms and no object constraints. Target object transform
drivers are rejected. `bake_pose` accepts only Action-driven source object transforms;
`bake_evaluated_pose` may evaluate self-contained source drivers under its existing dependency
rules. Source and target object transforms, source custom-property values, Action/slot, NLA,
pose and scene frame are restored transactionally; even when object output is omitted, source
object or driver-control channels cannot leak their last sampled value into the scene.

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
