# Open retarget profiles

A FaceLink retarget profile is plain JSON. It maps pose-bone channel names in one existing
Blender Action to bone names on the target armature. Validate a profile with:

```powershell
uv run facelink validate-profile --profile profiles/mixamo_to_facelink_compact.json
```

Pass one or more reviewed profiles to BYOK planning with `--retarget-profile`. MCP clients can
call `validate_retarget_profile` and place the normalized `adapter`, `bone_map` and `strict`
fields in a `play_clip` beat's `retarget` object.

After `facelink scan --out scene.json`, `suggest-profile` can propose exact, normalized and a
small set of explicit alias matches. Its output always says `review_required: true`; FaceLink
never silently applies a suggestion. Run `analyze-profile` on the reviewed map to receive
per-bone hierarchy, rest-axis, local-rest-rotation and relative-length metrics. The matching
MCP tools are `suggest_retarget_profile_map` and `analyze_retarget_profile`.

The controlled alias groups are intentionally short and auditable: hips/hip/pelvis,
arm/upperarm, lowerarm/forearm, upleg/upperleg/thigh and lowerleg/leg/calf. Left/right tokens
at either end of a name are preserved, and namespaces such as `mixamorig:` are ignored for
matching. Ambiguous candidates remain conflicts rather than being guessed.

Current deterministic thresholds are conservative: up to 1 degree of parent-local rest-axis
or rest-rotation difference and 2% relative bone-length deviation is `safe`; small differences
up to 5 degrees/10% are `review`; larger differences or hierarchy changes are
`bake_required`. Missing/duplicate mappings are `incompatible`. A uniform whole-rig scale is
normalized for rotation-only Actions, but pose-bone location channels across different scale
are blocked because they need translation-aware baking.

`rename_only` is intentionally narrow. It copies an Action, rewrites pose-bone FCurve paths,
and attaches the copy as an editable NLA strip. It does not change rest pose, bone axes,
proportions, IK/FK controls or root-motion conventions. With `strict: true`, every pose bone
animated by the source Action must be mapped and every target name must exist.

`bake_pose` uses the same reviewed map but requires an explicit `source_rig`. It samples local
pose transforms into an ordinary target Action and can correct different rest axes and scale.
`sample_step` is 1-16; `root_motion` is `scale`, `preserve` or `drop`. Root motion means
translation on a mapped root pose bone—object-level Action channels are omitted. Version 1
requires the mapped parent hierarchy to match and mapped source/target bones to be free of pose
constraints and transform drivers; it also requires `strict: true` and ordinary pose transform
channels. Use deform skeletons or bake a control rig to a source Action first.

The included compact rename and bake profiles are format examples for a two-bone test rig, not
universal Mixamo presets. Profiles are ordinary version-controlled files so rig maintainers can
publish and review mappings without adding Python code.
