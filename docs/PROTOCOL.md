# Local bridge protocol 1.13

The Blender extension writes one JSON discovery record per running Blender process. The
directory is `${FACELINK_INSTANCE_DIR}` when configured, otherwise
`${TEMP}/facelink/instances`. Protocol 1.13 deliberately keeps the discovery record on the
1.12 field set so an unchanged 1.12 client can still parse it; protocol negotiation happens
through authenticated health instead of a new required discovery field.

All requests require `Authorization: Bearer <token>` from that record. Health includes the
additive `facelink_version` field so sidecars can report extension/host version skew without
reading or exposing the discovery token. Protocol 1.13 explicitly negotiates only versions
`1.12` and `1.13`; the complete policy is documented in
[COMPATIBILITY.md](COMPATIBILITY.md).

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/health` | Protocol, capability and bounded job-runtime information |
| `POST` | `/v1/jobs` | Queue one of the jobs below |
| `GET` | `/v1/jobs/{id}` | Poll a job until succeeded or failed |
| `POST` | `/v1/jobs/{id}/cancel` | Cancel queued work or request a safe running cancellation |
| `GET` | `/v1/recovery` | Inspect interrupted work from the previous bridge session |
| `POST` | `/v1/recovery/acknowledge` | Acknowledge the current recovery report |
| `GET` | `/v1/diagnostics` | Read bounded, allowlisted bridge support metadata |

Supported jobs are `scan_scene`, `stage_patch`, `get_staged_patch`,
`apply_staged_patch`, `discard_staged_patch`, `apply_patch`, `undo`, `list_revisions` and
`rollback_revision`.

## 协议协商、关联编号与安全诊断

`GET /v1/health` 是经过 Bearer Token 认证、但不受请求协议版本限制的启动入口。客户端
读取响应体中的 `protocol_version` 和 `protocol_compatibility.supported_versions`，从双方
明确支持的 `1.12`、`1.13` 中选择最高交集。其他请求必须通过
`FaceLink-Protocol-Version` 请求头发送选定版本；省略该头只为旧客户端提供 `1.12`
语义。格式非法时返回 HTTP 400 / `invalid_protocol_version`，无明确交集时返回 HTTP 426 /
`unsupported_protocol_version`，并在读取任务正文或进入队列前拒绝。

协议 1.13 给每个受支持的 `GET`/`POST` 响应生成独立的 `request_id`，并给一个客户端
工作流提供 `correlation_id`。Python 客户端发送形如 `corr-<32 lowercase hex>` 的随机
关联种子。
桥接使用本次启动的 Bearer Token 对该种子做 HMAC，记录和返回派生后的关联编号，不
回显调用方原种子；相同种子在同一次桥接会话内得到相同关联编号，桥接重启后会变化。
服务端生成的编号同时出现在 JSON 响应及 `X-FaceLink-Request-ID`、
`X-FaceLink-Correlation-ID` 响应头中，已接收任务的脱敏生命周期记录也保留这两个编号。
关联编号只用于排错，不是身份验证或授权凭据。非法关联编号返回
`invalid_correlation_id`。

经过认证的 `GET /v1/diagnostics` 返回诊断结构 `1.0`，包括版本、能力、有界队列/恢复
计数、最多 64 个脱敏任务以及最多 256 个脱敏请求事件。事件只记录时间、请求/关联/
任务编号、`GET`/`POST`、模板化路由、HTTP 状态和稳定错误/任务状态；查询参数、具体
未知路径、请求正文、任务 payload/result、场景内容、绝对路径、Token、Key 和原始异常
均不进入事件缓冲区。该端点不会上传数据。

用于对外分享时不要直接分享 `facelink doctor` 原始输出，而应运行：

```powershell
facelink diagnostics --out facelink-diagnostics.json
```

该命令在本地把 Doctor 结果和桥接诊断再次按严格允许列表投影，执行敏感模式扫描，
以原子方式写出最多 1 MiB 的单个 JSON，且不会自动上传。完整隐私边界见
[THREAT_MODEL.md](THREAT_MODEL.md)。

## Bounded jobs and stable errors

Protocol 1.10 adds the `bounded_job_queue` and `stable_error_codes` capabilities. Each Blender
process accepts at most 128 pending jobs and retains at most 256 total job records, including
queued, running and completed records. Admission uses a non-blocking queue operation. When the
pending queue is full, the bridge returns HTTP 429 with `queue_full` instead of blocking an HTTP
worker or growing memory without a limit. If all retained records are still active, it returns
HTTP 429 with `job_registry_full`.

Before a new job is admitted, the oldest succeeded or failed records are removed as needed.
Queued and running records are never removed. Clients should therefore poll accepted jobs
promptly and treat `job_not_found` as either an unknown ID or an expired completed record. The
health response contains a point-in-time `job_runtime` object:

```json
{
  "pending": 2,
  "pending_capacity": 128,
  "retained": 9,
  "retention_capacity": 256,
  "status_counts": {
    "queued": 2,
    "running": 0,
    "succeeded": 4,
    "failed": 1,
    "cancelled": 1,
    "timed_out": 1,
    "interrupted": 0
  },
  "cancel_requested": 0,
  "evicted_total": 3
}
```

Every protocol error keeps the legacy human-readable `error` string and adds stable fields:

```json
{
  "error": "The Blender job queue is full; retry after pending work completes.",
  "error_code": "queue_full",
  "retryable": true,
  "retry_after_ms": 100,
  "details": {
    "pending": 128,
    "pending_capacity": 128,
    "retained": 256,
    "retention_capacity": 256
  }
}
```

HTTP 429 responses also include `Retry-After: 1`. Request-level stable codes are
`authentication_required`, `route_not_found`, `job_not_found`, `invalid_content_length`,
`invalid_request_size`, `request_too_large`, `invalid_json`, `invalid_request`,
`unsupported_job_type`, `invalid_job_payload`, `queue_full`, `job_registry_full`,
`journal_unavailable`, `bridge_stopping`, `invalid_correlation_id`,
`invalid_protocol_version`, `unsupported_protocol_version` and `recovery_review_required`.
Request bodies are limited to 4 MiB.

A failed job record uses `invalid_job_payload` for missing or type-invalid payload data,
`job_rejected` for a valid request rejected by scene-aware validation, and `internal_error` for
an unexpected implementation failure. Unexpected exception text is not returned to the client.
An unknown Scene Patch `schema_version` uses `unsupported_schema_version` and is rejected before
scene mutation.
The Python bridge client exposes these fields through `BridgeError`, which remains a
`RuntimeError` subclass for compatibility with existing callers.

## Deadlines and safe cancellation

Protocol 1.11 adds `job_deadlines` and `safe_job_cancellation`. A submit request may contain an
optional top-level `timeout_ms` integer from 1 through 3,600,000. The deadline begins when the
bridge admits the job, so it includes queue wait. The accepted job record echoes `timeout_ms`
and an informational Unix `deadline_at`; expiry decisions use a monotonic clock and are not
affected by wall-clock adjustments.

```json
{
  "type": "apply_patch",
  "payload": {"patch": {}},
  "timeout_ms": 30000
}
```

`POST /v1/jobs/{id}/cancel` controls an accepted job and requires the same bearer token as all
other bridge calls. A queued job is removed immediately, releases its queue slot and becomes
`cancelled` with `error_code: job_cancelled`. Repeating cancellation for a `cancelled` or
`timed_out` job is idempotent. An unknown or expired ID returns `job_not_found`; a succeeded or
failed job returns HTTP 409 with `job_not_cancellable`.

A running job returns HTTP 202 with `cancel_requested: true`. Blender scene access is not
forcefully interrupted because asynchronous exceptions can corrupt a `.blend` transaction.
Instead, patch application checks cancellation and deadlines before and after each ordinary
operation. Interruption raises inside the existing transaction, restores the captured scene,
Action, camera and audit state, then records `cancelled` or `timed_out`. Staging restores the
previous preview if interrupted, and read-only jobs check again after their scan.

Some short commit operations such as Undo, revision rollback and staged-patch discard have no
safe intermediate point. If cancellation or expiry races with their final commit, the completed
mutation wins and the job remains `succeeded`; its record adds `completed_after_cancel_request`
or `completed_after_deadline`. FaceLink never reports such a committed mutation as cancelled.
This also applies to the tiny race after the final transactional checkpoint.

The health `job_control` object publishes the maximum timeout, cancellation endpoint and
`cooperative_safe_checkpoints` policy. Python clients use `submit_job(..., timeout_ms=...)`,
`cancel_job(job_id)` or `run_job(..., job_timeout_ms=...)`. A client's polling timeout remains
separate and does not silently create a server deadline.

Protocol 1.11 adds request/control errors `invalid_job_timeout` and `job_not_cancellable`, plus
terminal job errors `job_cancelled` and `job_timed_out`. Deadline terminal errors are retryable;
explicit cancellation is not.

## Persistent journal and startup recovery

Protocol 1.12 adds `persistent_job_journal` and `startup_recovery`. The job journal is an
independently versioned data structure whose current `schema_version` is `1.0`; that schema
number is not the bridge protocol version. Before an accepted job is returned to a client, the
bridge atomically persists a bounded lifecycle record. It records at
most 256 jobs and only a fixed allowlist of identifiers, job types, timestamps, status,
deadlines, cancellation state and transaction-checkpoint metadata. It never persists a request
payload, prompt, patch body, result, scene snapshot, bearer token, provider key, raw exception
text or `.blend` path.

The journal uses `${FACELINK_STATE_DIR}` when configured, otherwise Blender's user configuration
directory under `facelink/state`. A saved `.blend` receives a stable SHA-256-derived workspace
identifier without exposing its path. An unsaved file uses a process-scoped identifier and the
recovery response explicitly advises saving before cross-process recovery can be expected.
Writes use a same-directory temporary file, flush and atomic replacement. A malformed journal
is preserved with a `.corrupt-<timestamp>` suffix and startup reports
`recovery_journal_corrupt` instead of silently trusting or deleting it.

A syntactically valid journal with an unsupported `schema_version`—future or older without an
available migrator—is not treated as corruption. The extension fails closed during bridge
startup and leaves the original bytes in place: it does not quarantine, overwrite, downgrade
or replay that journal. Use the FaceLink version that created it after backing up the state
directory; never edit only the version string to bypass this guard.

Lifecycle checkpoints are `accepted`, `main_thread_started`, `transaction_committed` and
`terminal`. Patch execution persists `transaction_committed` only after the Blender revision
audit entry has been written. If that durable checkpoint fails, the existing patch transaction
restores the scene and audit state. Bridge shutdown changes queued or running work to terminal
`interrupted` records with `bridge_stopped` and persists them before clearing memory.

On startup, prior `queued`, `running` or `interrupted` records appear in `GET /v1/recovery`.
FaceLink never replays them automatically. Records with a committed-transaction signal return
`guidance_code: inspect_revision_history`; other records return
`guidance_code: rescan_and_replan`. A mutating job that reached the Blender main thread but not
the committed checkpoint reports `may_have_partial_changes: true` and
`guidance_code: rescan_and_inspect_scene`, because a hard process crash cannot run the in-memory
rollback handler. An unclean shutdown without an identifiable active job still requires a fresh
scene scan. `POST /v1/recovery/acknowledge` durably clears the report but does not modify the
Blender scene. Health exposes only the recovery reason and interrupted-job count; the full
sanitized report remains behind the authenticated recovery endpoint.

While recovery reports `required: true`, the bridge rejects the four high-impact scene mutation
and rollback job types: `apply_staged_patch`, `apply_patch`, `undo` and
`rollback_revision`. Rejection happens before a job ID is generated or queue admission, returns
HTTP 409 with `error_code: recovery_review_required`, and therefore creates no job record.
Inspection and preparation calls such as `scan_scene`, `get_staged_patch`, `list_revisions`,
`stage_patch` and `discard_staged_patch` remain available. A scan may add stable FaceLink entity
ID metadata, but it does not apply animation or geometry changes. Successful acknowledgement removes
the gate; a failed acknowledgement write leaves recovery required. The acknowledgement itself
never replays, applies, undoes or rolls back a task.

If admission or a safe transaction checkpoint cannot be journaled, the bridge fails closed with
HTTP/job error `journal_unavailable`. Cancellation changes in-memory state before persistence so
the current session stops as early as safely possible. If that write fails, HTTP returns 503 /
`journal_unavailable` with `details.cancellation_status`: `cancelled` means a queued job was
removed from this session, while `running` means only a cooperative cancellation was requested
and the final outcome remains unknown. Neither response claims that a running job already stopped.
Python clients use `recovery_status()` and `acknowledge_recovery()`. They also treat `interrupted`
as a terminal status, with fallback error code `job_interrupted` for compatible servers.

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
audit entries are retained. New entries declare `schema_version: "1.0"`; older dictionary
entries without that field remain displayable. Because the audit contains scene-facing names,
it is not part of the privacy-safe diagnostic format.

The actual rollback snapshots contain live Blender datablock references and remain in memory,
with a maximum of 50 revisions. After reopening a `.blend`, history entries remain visible
but report `rollback_available: false`. This separation prevents a persisted log from being
mistaken for a safe executable snapshot.

Scene access is asynchronous because Blender's Python API is not thread-safe. HTTP handler
threads only enqueue data; a registered `bpy.app.timer` performs every scene read or write.
