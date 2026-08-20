# Changelog

All notable FaceLink changes are recorded here. The project follows Semantic Versioning while
the public API is experimental.

## [Unreleased]

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

[Unreleased]: https://github.com/xuebazhou-blip/FaceLink/compare/v0.3.6...HEAD
[0.3.6]: https://github.com/xuebazhou-blip/FaceLink/releases/tag/v0.3.6
[0.3.5]: https://github.com/xuebazhou-blip/FaceLink/compare/a62dc48...v0.3.6
[0.3.4]: https://github.com/xuebazhou-blip/FaceLink/commit/b14db7e
[0.3.3]: https://github.com/xuebazhou-blip/FaceLink/commit/16d82e7
[0.3.0]: https://github.com/xuebazhou-blip/FaceLink/commit/b6e9751
