# Testing and acceptance

FaceLink uses two test levels. `pytest` covers pure Python schemas, compiler behavior, bridge
client behavior, CLI, provider contract and a real MCP stdio handshake. Blender acceptance
scripts run inside Blender's own Python runtime and exercise the actual `bpy` mutation path.
Each supported version also runs a short hidden-window UI smoke test because GPU draw calls
are unavailable in Blender background mode; the smoke test requires a real viewport callback
before Blender exits automatically.

Product-readiness tests also cover Blender version parsing, bounded installation discovery,
missing/unsupported executables, stale bridge records, API-key/token redaction, Doctor's JSON
and human CLI contracts, installer hash validation and the Windows installer's non-mutating
`-PlanOnly` result. A release smoke test installs the built wheel into a clean off-repository
virtual environment before publication.

Bridge-instance discovery separately proves that only the deterministic first 32 candidate
records are opened, every record is capped at 16 KiB before JSON decoding, oversized or malformed
records fail closed, and Doctor reports candidates skipped by the discovery bound.

The repository privacy gate scans every tracked or release-bound file as raw and UTF-16-like
content. It rejects high-risk provider/private-key signatures, the former public author email
and known development-machine roots. Blender demo acceptance separately rejects saved Windows
user directories and requires a relative render path inside the checked-in `.blend`.

协议 1.13 的诊断导出测试使用运行时拼接的 API Key、Bearer Token、邮箱、Windows/Unix/
UNC 用户路径、场景名称、对象名称、payload、结果和原始异常作为“投毒”输入，要求最终
JSON 只保留允许列表字段，且这些敏感值逐字节不存在。测试同时覆盖 1 MiB/32 实例/
64 任务/256 事件边界、`.json` 后缀、默认禁止覆盖、原子替换失败时旧文件保持不变、
临时文件清理、确定性哈希及 `uploaded: false`。这些测试降低回归风险，但不取代用户在
分享诊断包前的人工检查。

Blender 任务与恢复中心另有三层功能验收。纯 Python 测试向状态投影输入错误类型、超大
计数、非法任务编号、未知枚举、路径、凭据和原始异常，要求输出仍然有界且只含允许列表
字段；运行时测试要求重复读取任务中心状态不会推进超时、改变运行时版本或写入日志；真实
Blender 测试覆盖注册/注销、自动重绘、连接状态、排队取消确认、运行任务安全检查点、
完成优先竞争、恢复确认失败、未来日志失败关闭、加载文件以及首次保存/另存为后的桥接
解绑。恢复门禁测试还要求 UI 禁用应用暂存、撤销和历史回滚，HTTP 对这三类及直接应用
返回带稳定错误码的 409，且拒绝时不创建任务；扫描和读取仍然可用。隐藏窗口 UI smoke
还必须实际绘制侧边栏，以发现不同 Blender 版本中的图标、属性和 Operator 注册问题。

The Windows acceptance run builds the Python distributions and Blender ZIP, embeds them into
the WinForms setup EXE, extracts them through the real setup executable, verifies both embedded
hashes and checks that `-PlanOnly` creates no install directory. MCP configuration tests cover
new files, preservation of unrelated TOML, CRLF paths, idempotent updates, timestamped backups,
dry runs, malformed marker refusal, unmanaged name collisions and invalid UTF-8/TOML.

Run the complete Windows matrix:

```powershell
./scripts/run_acceptance.ps1
```

Or supply explicit Blender executables:

```powershell
./scripts/run_acceptance.ps1 -BlenderExe @(
  'D:\Blender-4.2\blender.exe',
  'D:\Blender-4.5\blender.exe',
  'D:\Blender-5.2\blender.exe'
)
```

Every run writes timestamped command logs, JUnit XML, coverage XML, per-Blender JSON results
and a consolidated report below `artifacts/`. `artifacts/latest-report.md` is the quickest
human-readable summary.

## 开发阶段与发布候选阶段

日常功能提交运行与改动相关的单元/故障注入测试、完整快速 `pytest`、Ruff、隐私回归、
受支持 Blender 版本的功能验收和 CI。上面的 `run_acceptance.ps1` 属于可重复的功能矩阵，
不代表已经完成路线图中的 1,000 个端到端任务或连续两小时稳定性门禁。

1,000 个任务和连续两小时运行属于高成本发布候选验收，按路线图延后到 v0.5.0 候选版
统一执行，并在 v1.0.0 正式候选阶段按当时的完整标准再次执行。延后期间不得把它标记为
通过，也不能用少量边界测试推断长时间稳定性；它不阻塞 v0.5.0 各功能切片的开发和审核。

## Acceptance gates

- Ruff must pass.
- All Python tests must pass with at least 85% line/branch-aware aggregate coverage.
- The extension must build and validate on every supplied Blender version.
- Source-level Blender acceptance must pass: registration, stable identity, bounds, locks,
  world/local parent conversion, stale-scene rejection, transform keyframes, interpolation,
  frame rate, constraints, cameras, NLA clips, idempotency, fail-closed behavior and
  transaction rollback, multi-revision rollback and real `.blend` audit persistence. Revision
  boundary coverage also verifies unique revision IDs for repeated patch IDs, unknown-target
  rollback safety, malformed audit-log recovery, the 100-entry audit/50-snapshot limits, and
  that staging or discarding a patch never creates a false history entry.
- Preflight visualization acceptance must verify world/local path conversion, finite camera
  frustum geometry, overlay show/hide/cleanup, zero scene mutation while staged, internal
  timeline rejection, existing-keyframe warnings and existing FaceLink NLA collision safety.
- Navigation acceptance must verify stable cross-runtime fingerprints, valid triangulation,
  invalid/non-manifold topology rejection, deterministic connected paths, disconnected and
  outside-mesh failures, distance-based unique frames, swept-bounds collision/height behavior,
  Blender role operators, stale-environment rejection and editable multi-key execution.
- Camera composition acceptance must verify good framing, minimum/maximum subject size,
  safe-area and clipping metrics, center occlusion identity, dolly start/end samples, missing
  target bounds, disabled analysis, malformed-threshold rejection, receipt propagation and
  zero scene mutation while staged. Animated targets must be sampled at declared camera frames
  without changing the user's current frame, and execution must use the same start-frame pose.
- Rig/Action acceptance must create actual Blender armatures and pose-bone keyframes, verify
  bounded inventories and stable fingerprints, strict source/target coverage, unique targets,
  stale-Action rejection, source preservation, editable copied FCurve paths, non-pose channel
  preservation, NLA idempotency and removal of created Actions after full rollback. Rig
  diagnostics additionally verify deterministic suggestion conflicts, parent-local rest-axis
  angles, uniform-scale normalization, proportion drift, scaled pose-translation rejection,
  source-rig ambiguity, compiler rejection of bake-required mappings and stale Edit Mode
  rest-pose rejection. Sampled-bake coverage uses real rigs whose rest axes differ by 90
  degrees and lengths by 2x, checks all root-motion policies, exact/fractional sample endpoints,
  quaternion continuity, linear editable output, idempotent reuse, source/timeline preservation,
  rollback cleanup, old-schema rejection, constraint boundaries and workload caps.
  Evaluated-bake coverage drives deform bones from a same-rig controller constraint and a
  custom-property driver, verifies final-pose inversion, Action/NLA/pose/frame restoration,
  stale driver/property guards, deterministic expression restrictions, external constraint and
  driver rejection, bounded output names and cleanup after an injected bake failure.
  Object-motion coverage verifies placement-preserving deltas, rig-scaled translation,
  object-only direct Actions, editable object FCurves, omitted-channel restoration, rollback and
  fail-closed parent/constraint/driver/rotation-mode/singular-transform boundaries.
- Bridge acceptance must pass: localhost binding, bearer authentication, malformed requests,
  concurrent jobs, a real HTTP 429 at the pending-job boundary, non-blocking overload rejection,
  terminal-only record eviction, health capacity metrics, stable request/job errors, legacy
  client error compatibility, queued cancellation and deadline expiry, cancel/start race safety,
  running cancellation after a real Blender mutation, transactional cancellation rollback,
  completion-wins metadata, protocol 1.1 fingerprint agreement, main-thread dispatch, failed-job
  reporting, protocol 1.2 history/rollback, protocol 1.3 navigation snapshots, protocol 1.4
  composition diagnostics, protocol 1.5 rig/action capabilities, protocol 1.6 rest-geometry
  guards, protocol 1.7 sampled-pose-bake, protocol 1.8 evaluated-pose-bake, protocol 1.9
  object-motion-bake capability, protocol 1.10 bounded jobs and stable errors, protocol 1.11
  deadlines and safe cancellation, protocol 1.12 atomic bounded journals, corrupt-journal
  quarantine, startup recovery, explicit acknowledgement, no automatic replay, checkpoint
  rollback and persisted-record privacy, preview diagnostics, Undo and discovery cleanup.
- Protocol 1.13 compatibility acceptance must cover authenticated health bootstrap; legacy
  no-header `1.12`; explicit `1.12` and `1.13`; highest-overlap selection; malformed, older,
  future-minor and future-major rejection before request-body parsing or queue mutation; and
  stable HTTP/error-code behavior. It must also prove that every success and error carries
  matching body/header request and correlation IDs, request IDs are per-request, a valid client
  correlation seed is never echoed or persisted, and its HMAC-derived server ID links the jobs
  created in the same bridge session.
- Diagnostic bridge acceptance must require authentication, template paths without query or
  unknown-path content, a 256-event eviction boundary, at most 64 recent sanitized jobs and no
  request body, result, scene name/path, token, provider key or raw exception in memory,
  endpoint output, journal or shareable export. Concurrent event recording must remain bounded.
- Blender task-center acceptance must prove that panel redraw is side-effect free and bounded;
  queued cancellation is immediate; running cancellation remains cooperative at safe
  checkpoints; completion wins are reported honestly; recovery acknowledgement never replays
  or rolls back work; acknowledgement-write failure keeps recovery required; startup errors use
  stable content-free messages; recovery-gated apply/undo/rollback requests return HTTP 409 /
  `recovery_review_required` before creating a job while inspection requests remain available;
  future journals remain byte-for-byte unchanged; and file load, first save and Save As stop or
  rebind the bridge so later tasks cannot be recorded in the previous workspace log. Ready-state
  tests must not imply that AI or account authentication is connected.
  The task-status surface must not expose ports, paths, payloads, results, scene-facing names,
  raw exceptions, bridge tokens or provider keys.
- Migration acceptance must distinguish malformed journals from valid future schemas. Malformed
  data is quarantined; a future journal version must fail closed with its exact bytes unchanged,
  no `.corrupt-*` rename and no replacement file. Unsupported Scene Patch schemas must return
  `unsupported_schema_version` with zero scene mutation. New audit entries must carry schema
  `1.0` while legacy versionless entries remain readable.
- The built ZIP must install, enable and load from each Blender version's isolated portable
  extension repository.
- The checked-in demo `.blend` must open in the primary Blender version with its FaceLink ID,
  96-frame timeline, six ordinary transform FCurves and exactly 24 editable linear keyframe
  values intact.

Blender is always invoked with `--python-exit-code 1`. Without this flag Blender can print a
Python traceback and still return process exit code zero, which creates a false-green CI
result.

## What this does not prove

The harness deliberately reports live provider calls, visual composition quality, large
production control rigs, automatic IK/FK/controller discovery, external dependency graphs and
parented/constrained Armature-object roots, plus non-Windows platforms as unverified. Passing
acceptance means the implemented direct and
self-contained evaluated deform-skeleton pose-bakes and editing surface behave as specified;
it does not mean FaceLink solves motion generation, general
character retargeting or cinematic judgment.

Passing the security/diagnostic gates also does not prove isolation from a malicious process
running as the same operating-system account, an arbitrary Blender Add-on, an administrator or
kernel compromise, a compromised MCP/model provider, or HTTP connection-exhaustion attacks.
Those residual risks are documented in [THREAT_MODEL.md](THREAT_MODEL.md).
