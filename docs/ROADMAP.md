# FaceLink Roadmap to 1.0

- **Current release:** v0.3.9 Alpha
- **Roadmap status:** proposed for maintainer review
- **Last reviewed:** 2026-08-24

This roadmap turns FaceLink from a technically capable alpha into a stable, attractive and
supportable Blender product. It is intentionally gate-driven: a version is complete only when
its acceptance criteria pass. A date or a feature count is not a substitute for evidence.

## Product promise

FaceLink turns a natural-language shot request into an editable Blender previs scene. Objects,
cameras, Actions, NLA strips and keyframes remain ordinary Blender data. Every write is typed,
bounded, previewable and explicitly approved by the artist.

The 1.0 product must let a first-time user install FaceLink, connect a supported AI client,
describe a shot, review the proposed change, apply it, edit the result in Blender and recover
from a mistake without using a terminal.

## Decisions that remain true through 1.0

- **Hybrid product, not MCP alone.** The Blender extension owns scene access, the viewport UI
  and transactional execution. The sidecar owns model/MCP integration and orchestration. MCP is
  an integration surface, not the complete product.
- **Blender is not bundled.** FaceLink detects a supported official Blender installation. This
  keeps downloads small, avoids repackaging another project's binaries and lets studios choose
  their LTS version.
- **Local-first and private by default.** Local MCP and deterministic execution require no
  FaceLink account. Telemetry is off unless the user explicitly opts in. Diagnostic bundles are
  redacted before export.
- **Model-independent core.** The compiler and executor accept typed FaceLink schemas, not a
  provider-specific prompt. An MCP client can use its existing model entitlement; BYOK is a
  separate adapter path.
- **No unrestricted generated Python.** Models may propose only versioned, whitelisted schema
  operations. Unsupported scene structures fail with a useful explanation.
- **Human approval stays mandatory by default.** Scan, preview, stage and apply remain separate
  steps. A power-user bypass must be explicit and visibly labelled.
- **Editability beats visual fakery.** FaceLink 1.0 is a previs and animation control product,
  not a text-to-video generator or a promise of final cinematic motion.

## Baseline delivered in v0.3.9

The alpha already provides a serious technical foundation:

- stable scene identities, typed ShotSpec compilation and guarded Scene Patches;
- world-space object/camera transforms, editable keys, cameras, constraints and NLA clips;
- stage/review/apply/discard, persistent audit metadata and same-session rollback;
- navmesh paths, collision warnings and geometric camera-composition preflight;
- rig/Action inventory, rename-only retargeting and sampled/evaluated pose baking;
- Armature object-motion baking with deterministic output and rollback cleanup;
- authenticated localhost Blender bridge, CLI, BYOK OpenAI planning and local MCP stdio;
- Blender 4.2/4.5/5.2 acceptance coverage, Windows installer and privacy regression gates.

These capabilities prove the execution model. They do not yet prove product reliability,
usability or production-scale performance.

## Largest gaps before 1.0

| Area | Alpha state | 1.0 requirement |
|---|---|---|
| Recovery | executable rollback is session-only | durable apply journal, restart recovery and tested interrupted-write handling |
| Runtime safety | concurrent bridge behavior is tested functionally | bounded queues, cancellation, deadlines, backpressure, isolation and soak tests |
| Performance | no published workload or latency budget | repeatable benchmark scenes, p50/p95 results and regression budgets |
| Product UI | one dense Blender sidebar and a basic installer | guided workflow, job progress, visual review, useful errors, settings and polished states |
| Project workflow | one staged patch per Blender instance | persistent projects, reusable shot templates, multi-shot sequencing and version comparison |
| Scene adaptability | explicit roles and bounded retarget profiles | adapter SDK, assisted mapping, full-matrix object transforms and a certified fixture corpus |
| Distribution | unsigned Windows alpha | signed artifacts, repair/update/uninstall, macOS/Linux packages, SBOM and provenance |
| Integrations | local stdio MCP and one BYOK provider | stable client setup, provider boundary and optional self-hosted Streamable HTTP gateway |
| Operations | local logs and acceptance artifacts | privacy-safe diagnostics, health metrics, compatibility reports and release rollback |
| Adoption | demo and technical docs | first-run sample, user guide, contributor path, issue triage and transparent support policy |

## What “three highs” means for FaceLink

For this roadmap, “三高” means **high reliability, high performance and high
concurrency/scalability**. Because Blender's Python API must execute scene writes on Blender's
main thread, FaceLink will not claim unsafe parallel writes inside one `.blend` file. It will
serialize writes per Blender instance and scale safely across clients and Blender instances.

### Reference workload

The v0.7 benchmark harness will check in redistributable small, medium and large fixture
generators. The 1.0 performance gate uses the documented medium workload:

- 2,000 objects, 10 cameras and 8 armatures with up to 256 bones each;
- 100 Actions and 100,000 existing editable keyframe values;
- 20,000 navigation triangles and 500 marked obstacles;
- a patch with up to 100 ordinary operations or a bake with up to 200,000 output values.

Results must name the CPU, memory, OS, Blender version and FaceLink commit. Provider/model
latency is reported separately and is never hidden inside local-engine results.

### 1.0 reliability SLO

- 100% pass rate on the supported OS × Blender acceptance matrix.
- Zero partial scene mutations in the transaction and injected-failure suite.
- 100% successful rollback/recovery in supported recovery scenarios.
- At least 99.9% successful terminal outcomes across 10,000 valid jobs in a 24-hour soak; an
  intentionally cancelled job counts as successful only when it leaves no unintended change.
- No deadlocks, cross-instance data leaks or stale-token acceptance in the soak suite.
- No open P0 or P1 defects at release; every accepted lower-severity limitation is documented.

### 1.0 performance SLO

On the published reference machine and medium workload, using Blender 5.2 LTS:

- instance discovery and health p95 at or below 200 ms;
- scene scan p95 at or below 2.5 seconds;
- patch preflight/stage p95 at or below 1.5 seconds;
- apply of 100 ordinary transform/camera operations p95 at or below 2.0 seconds;
- cancellation acknowledgement at or below 500 ms when the current Blender operation exposes
  a safe cancellation point;
- Blender UI event handling never performs network/model waits on the main thread.

Pose baking and navigation are size-dependent. Their throughput is published per fixture and
must not regress by more than 10% from the approved v0.8 baseline without an explicit review.

### 1.0 concurrency/scalability SLO

- 32 concurrent MCP client sessions across 8 Blender instances for a two-hour stress run.
- At least 100 submitted jobs per instance are either accepted within documented bounds or
  rejected immediately with retry guidance; queues may not grow without limit.
- Strict per-instance write serialization, parallel work across independent instances and zero
  job misrouting.
- Fair scheduling, job deadlines, cancellation, retention limits and cleanup after client or
  Blender disconnects.
- Optional remote deployments enforce per-user quotas, OAuth scopes and tenant isolation; the
  local-only product remains fully usable without that deployment.

These are release gates, not marketing estimates. v0.7 establishes the reproducible baseline;
v0.8 must meet the load targets; v0.9 proves them in a release candidate.

## Compatibility policy for 1.0

### Blender

- Primary development target: the latest maintained Blender LTS at release time, currently
  Blender 5.2 LTS.
- Supported compatibility target: the immediately previous still-maintained LTS, currently
  Blender 4.5 LTS.
- Blender 4.2 remains in the regression matrix while it stays green, but it is a legacy target
  rather than a 1.0 support promise because it is no longer in Blender's maintained-LTS list.
- New non-LTS Blender versions receive best-effort CI before being promoted to supported.
- Schema and protocol capabilities are negotiated; version skew produces an actionable message,
  never a best-guess scene mutation.

### Operating systems

- Windows is the primary product and installer target.
- v0.9 adds signed/notarized packages and acceptance runners for supported Windows, macOS on
  Apple Silicon and a documented x86-64 Linux distribution.
- A platform is not called supported until install, upgrade, uninstall, bridge, UI and real
  Blender mutation acceptance all pass on that platform.

### AI clients and providers

- **Subscription path:** local MCP clients use their own available model; FaceLink stores no
  model API key.
- **BYOK path:** the OpenAI adapter remains the supported reference provider and credentials stay
  in the host environment or OS credential store, never in `.blend` files.
- **Provider extension path:** a documented planner interface allows additional providers
  without changing the compiler/executor trust boundary.
- **Remote path:** a self-hosted Streamable HTTP gateway may be offered for compatible clients.
  Public ChatGPT plugin publication also requires a stable HTTPS service, OAuth and a secure
  cloud-to-local Blender pairing design, so it is an optional deployment track rather than a
  dependency of the local open-source 1.0 release.

## Version sequence

| Version | Theme | Exit result |
|---|---|---|
| v0.4.0 | Durable reliability core | interrupted work is bounded, inspectable and recoverable |
| v0.5.0 | Product UI foundation | a new user completes the safe workflow without a terminal |
| v0.6.0 | Projects and multi-shot workflow | real previs sequences can be created, revised and reused |
| v0.7.0 | Scene adaptability and benchmark contract | supported complexity is broader and measured honestly |
| v0.8.0 | High-performance multi-instance runtime | published performance and concurrency targets pass |
| v0.9.0 | Cross-platform product beta | distributable, secure release candidate survives field use |
| v1.0.0 | Stable general availability | all GA gates below pass with no release-critical defects |

No version begins feature development until the previous version's exit gate is accepted.
Patch releases (`0.x.1`, `0.x.2`) contain compatible fixes and documentation only; unfinished
features stay behind disabled experimental flags and are not advertised.

## v0.4.0 — durable reliability core

**Goal:** make the execution engine safe under restarts, overload and version skew before the
workflow grows.

Scope:

- define protocol/schema compatibility, migration and deprecation policy;
- add bounded job queues, terminal-state retention, deadlines, cancellation and cleanup;
- introduce a durable apply journal with operation checkpoints and startup recovery guidance;
- persist staged-patch metadata safely while requiring a fresh stale-scene check after reload;
- make apply/rollback idempotency explicit across sidecar reconnects;
- add structured error codes, correlation IDs and redacted diagnostic export;
- add fault injection for exceptions, timeouts, disconnects and malformed persisted state;
- publish a threat model covering local discovery files, bearer tokens, MCP tools and updates.

Exit gate:

- 1,000-job/two-hour reliability run passes on Blender 4.5 and 5.2;
- queue/resource limits are documented and tested at every boundary;
- every injected failure ends in applied, rolled back, cancelled or safely recoverable state;
- protocol N-1 compatibility and unsupported-version behavior are covered by tests;
- no regression in the existing Python and real-Blender acceptance suite.

Not in this version: UI redesign, new scene-generation operations or remote hosting.

## v0.5.0 — product UI foundation

**Goal:** replace the alpha control panel with a coherent Blender-native product flow.

Scope:

- redesign the Blender sidebar around Connect, Describe, Preview, Apply and History states;
- add connection/instance status, job progress, safe cancellation and recovery banners;
- show a structured change list with affected objects, frames, warnings and blocking errors;
- make viewport paths, camera framing and before/after selection easy to inspect;
- add editable shot parameters so small corrections do not require rewriting the prompt;
- build useful empty, loading, success, error, offline and version-skew states;
- add settings for privacy, provider/MCP setup, Blender target and diagnostics;
- upgrade the installer with guided detection, repair, upgrade and uninstall flows;
- establish reusable visual tokens, icon rules, keyboard navigation and localization plumbing;
- add deterministic UI-state tests plus maintained product screenshots.

Exit gate:

- a clean-machine install-to-demo acceptance test requires no terminal;
- at least five representative first-time users are observed; at least four complete the demo
  workflow without maintainer intervention, and all blocking usability findings are resolved;
- every mutating action has an unambiguous review/confirmation state;
- all documented UI states have automated or screenshot-assisted regression coverage.

Not in this version: a separate Electron/web desktop application. Blender remains the primary
creative UI unless evidence shows a Blender-native workflow cannot meet the usability gate.

## v0.6.0 — projects and multi-shot workflow

**Goal:** move from a single patch demo to a repeatable previs production workflow.

Scope:

- add a versioned FaceLink project/sequence/shot model with stable IDs;
- create, duplicate, reorder, rename, archive and restore shots;
- save reusable typed shot templates with exposed parameters and validation;
- compare shot revisions and restore an earlier approved version;
- support multi-shot compile/stage queues while preserving per-shot human approval;
- add marker, camera and frame-range coordination without destructive timeline overlap;
- import/export portable project metadata without embedding credentials or machine paths;
- make missing assets, linked libraries and renamed scene objects repairable through mapping UI;
- provide two complete redistributable sample projects and a first real workflow tutorial.

Exit gate:

- a 20-shot project survives save/reload, reorder, duplication and schema migration;
- deterministic project round trips and conflict recovery pass on supported Blender versions;
- batch planning cannot bypass per-shot review or cross-contaminate revisions;
- sample projects can be completed from a clean install using only the product UI.

## v0.7.0 — scene adaptability and benchmark contract

**Goal:** support more real Blender scenes without pretending arbitrary rigs and dependency
graphs can be solved safely.

Scope:

- implement full-matrix object/camera conversion for supported parent chains;
- add a versioned adapter SDK with capability discovery and explainable compatibility reports;
- provide assisted controller/deform-bone mapping and explicit IK/FK mode review;
- ship certified adapters/fixtures for at least two redistributable rig families;
- visualize unsupported constraints, drivers, parents and dependency edges before execution;
- extend navigation toward multi-level static geometry and declared moving-obstacle windows;
- extend camera preflight for animated bounds and explicitly supported projection/lens cases;
- add asset normalization checks for units, axes, scale, naming and Action ownership;
- create public synthetic fixture generators and the small/medium/large benchmark suite;
- publish compatibility and performance reports from CI artifacts.

Exit gate:

- at least 25 redistributable scene fixtures cover parents, cameras, navigation and rig variants;
- every fixture either succeeds with editable output or fails with a stable actionable code—no
  silent approximation is accepted;
- certified rig adapters pass pose, root-motion, rollback and visual sanity fixtures;
- benchmark results are reproducible within documented variance on the reference machine;
- the medium workload establishes the approved latency and memory baseline for v0.8.

General automatic control-rig solving, arbitrary external driver graphs and final-quality motion
generation remain explicitly out of scope for 1.0.

## v0.8.0 — high-performance multi-instance runtime

**Goal:** meet the three-high runtime targets without violating Blender's main-thread rules.

Scope:

- add incremental snapshots and fingerprints so unchanged scene data is not repeatedly encoded;
- separate parallel read-only planning from per-instance serialized Blender writes;
- add a fair bounded scheduler with priority, deadlines, cancellation and backpressure;
- expose progress and cancellation through stable MCP tools; negotiate experimental MCP task
  support only when both client and server advertise it;
- harden multi-instance discovery, explicit targeting, reconnect and stale-record cleanup;
- add local metrics for latency, queue depth, failures and resource use with privacy-safe export;
- profile and optimize snapshot, fingerprint, navigation, preflight and bake hot paths;
- add Streamable HTTP self-hosting as an optional, separately secured transport;
- document OAuth 2.1, rate-limit and tenant-isolation requirements for any remote deployment.

Exit gate:

- every 1.0 performance and concurrency SLO passes on the reference workload;
- two-hour 32-session/8-instance stress testing has zero deadlocks, misroutes or leaked jobs;
- overload is bounded and returns deterministic retry/capacity errors;
- performance regression checks run in a controlled scheduled workflow and publish history;
- stdio remains fully supported and does not require a remote service or FaceLink account.

## v0.9.0 — cross-platform product beta

**Goal:** freeze features and prove the complete product outside the development machine.

Scope:

- deliver Windows, macOS Apple Silicon and Linux install/upgrade/repair/uninstall paths;
- code-sign Windows releases and sign/notarize macOS artifacts;
- generate checksums, SBOM, dependency/license report and build provenance;
- add safe update checks, release-channel selection and rollback to the previous FaceLink build;
- publish a Blender extension repository/install path in addition to local ZIP installation;
- finalize privacy policy, security response, support matrix and data-flow documentation;
- add opt-in crash/usage reporting or a fully local redacted support-bundle alternative;
- run provider contract tests, MCP Inspector tests and clean-account client onboarding tests;
- complete user, administrator, troubleshooting and contributor documentation;
- run a public beta with a published feedback/defect triage process.

Exit gate:

- clean install, upgrade from v0.8, repair and uninstall pass on every supported platform;
- artifacts verify, signatures validate and the release can be reproduced from a clean tag;
- a minimum 30-day beta has no unresolved P0/P1 defect and all accepted P2 defects are listed;
- at least ten external beta users complete the core workflow on real projects, with consented
  issue reports and no known data-loss event;
- the full 24-hour reliability soak and release-candidate security review pass.

Only fixes, documentation and performance work may enter after v0.9 feature freeze.

## v1.0.0 — stable general availability

FaceLink 1.0 ships only when all of the following are true.

### Functional

- install, connect, describe, preview, stage, apply, edit, save, reopen and recover work through
  the product UI on every supported platform;
- direct transforms, cameras, navigation, Action use and certified retarget/bake paths produce
  ordinary editable Blender data;
- multi-shot projects and templates migrate safely from every supported pre-1.0 format;
- unsupported scene structures fail before mutation with a documented recovery path.

### Three-high

- every reliability, performance and concurrency/scalability SLO in this document passes on the
  final release commit;
- benchmark and soak evidence is published with the GitHub release;
- there is no unpublished exception to the SLOs.

### UI and product quality

- the Blender UI has polished responsive states, consistent terminology and accessible keyboard
  operation for the core workflow;
- first-run demo, contextual help, diagnostics and recovery require no terminal;
- installer/updater/repair/uninstaller are productized and signed where the OS supports it;
- release notes state supported versions, breaking changes, known limitations and rollback steps.

### Security, privacy and supply chain

- the threat model and an independent release security review have no unresolved critical/high
  finding;
- secrets, machine paths and private scene contents are absent from repository/release artifacts;
- network behavior is explicit, local-only mode is complete and telemetry remains opt-in;
- dependencies, licenses, SBOM, checksums, signatures and provenance are published;
- vulnerability reporting and supported-version security patch policy are active.

### Field readiness

- at least ten external beta users and two non-trivial redistributable reference projects have
  exercised the release candidate;
- no open P0/P1 issue and no known data-loss bug;
- documentation, contribution guide, issue templates and support boundaries match the product;
- an older supported release can be restored if the 1.0 update fails.

## Explicitly after 1.0

The following may become 1.x/2.x projects, but they must not delay a safe local 1.0:

- an operated public ChatGPT plugin and cloud-to-local Blender pairing service;
- real-time multi-user scene collaboration and hosted project synchronization;
- an asset/adapter marketplace and proprietary DCC integrations;
- learned final-quality motion generation or autonomous cinematic judgment;
- general automatic solving of arbitrary rigs, constraints and driver graphs;
- bundling or redistributing Blender itself.

## Commit and review discipline

Implementation follows small vertical slices, not one large version dump:

1. One commit contains one coherent behavior, its tests and its documentation.
2. The commit must pass the proportionate local gate before it is offered for review.
3. After every implementation commit, work stops for maintainer review unless the maintainer
   explicitly asks for a batch.
4. Review feedback is resolved before the next roadmap slice starts.
5. A version is tagged only after every exit criterion is evidenced in its release report.
6. Refactors, generated artifacts and product behavior are not mixed without a clear reason.

After this roadmap is accepted, each version will be converted into a GitHub milestone and
small ordered issues. The first implementation slice should be v0.4.0's bounded job queue and
stable error model because later UI, cancellation and load testing depend on them.

## Official inputs

- [Blender maintained LTS releases](https://www.blender.org/download/lts/)
- [Blender extension add-on and dependency guidance](https://docs.blender.org/manual/en/4.2/advanced/extensions/addons.html)
- [OpenAI MCP setup for ChatGPT and Codex](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
- [OpenAI production MCP/plugin endpoint guidance](https://developers.openai.com/plugins/build/mcp-server)
- [OpenAI plugin authentication guidance](https://developers.openai.com/plugins/build/auth)
- [MCP 2025-11-25 authorization specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)
- [MCP experimental task lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks)
