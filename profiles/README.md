# Open retarget profiles

A FaceLink retarget profile is plain JSON. It maps pose-bone channel names in one existing
Blender Action to bone names on the target armature. Validate a profile with:

```powershell
uv run facelink validate-profile --profile profiles/mixamo_to_facelink_compact.json
```

Pass one or more reviewed profiles to BYOK planning with `--retarget-profile`. MCP clients can
call `validate_retarget_profile` and place the normalized `adapter`, `bone_map` and `strict`
fields in a `play_clip` beat's `retarget` object.

`rename_only` is intentionally narrow. It copies an Action, rewrites pose-bone FCurve paths,
and attaches the copy as an editable NLA strip. It does not change rest pose, bone axes,
proportions, IK/FK controls or root-motion conventions. With `strict: true`, every pose bone
animated by the source Action must be mapped and every target name must exist.

The included compact profile is a format example for a two-bone test rig, not a universal
Mixamo preset. Profiles are ordinary version-controlled files so rig maintainers can publish
and review mappings without adding Python code.
