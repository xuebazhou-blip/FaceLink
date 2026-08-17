import pytest
from pydantic import ValidationError

from facelink.compiler import compile_shot, validate_shot
from facelink.models import NavigationMesh, SceneSnapshot, ShotSpec, Vec3
from facelink.navigation import (
    NavigationError,
    allocate_path_frames,
    navigation_environment_fingerprint,
    plan_move_path,
)


def navigation_snapshot():
    return SceneSnapshot.model_validate(
        {
            "scene_name": "Navigation",
            "entities": [
                {
                    "id": "actor",
                    "name": "Actor",
                    "type": "MESH",
                    "transform": {"location": {"x": 1, "y": 1, "z": 0}},
                    "bounds": {
                        "minimum": {"x": 0.75, "y": 0.75, "z": 0},
                        "maximum": {"x": 1.25, "y": 1.25, "z": 1.8},
                    },
                },
                {
                    "id": "goal",
                    "name": "Goal",
                    "type": "EMPTY",
                    "transform": {"location": {"x": 5, "y": 5, "z": 0}},
                },
                {
                    "id": "wall",
                    "name": "Center Wall",
                    "type": "MESH",
                    "transform": {"location": {"x": 3, "y": 3, "z": 1}},
                    "bounds": {
                        "minimum": {"x": 2.5, "y": 2.5, "z": 0},
                        "maximum": {"x": 3.5, "y": 3.5, "z": 2},
                    },
                    "metadata": {"navigation_role": "obstacle"},
                },
                {
                    "id": "nav-l",
                    "name": "L Corridor",
                    "type": "MESH",
                    "transform": {"location": {}},
                    "metadata": {"navigation_role": "navmesh"},
                },
            ],
            "navigation_meshes": [
                {
                    "entity_id": "nav-l",
                    "name": "L Corridor",
                    "vertices": [
                        {"x": 0, "y": 0, "z": 0},
                        {"x": 2, "y": 0, "z": 0},
                        {"x": 2, "y": 4, "z": 0},
                        {"x": 0, "y": 4, "z": 0},
                        {"x": 6, "y": 4, "z": 0},
                        {"x": 6, "y": 6, "z": 0},
                        {"x": 0, "y": 6, "z": 0},
                        {"x": 2, "y": 6, "z": 0},
                    ],
                    "polygons": [
                        [0, 1, 2],
                        [0, 2, 3],
                        [3, 2, 7],
                        [3, 7, 6],
                        [2, 4, 5],
                        [2, 5, 7],
                    ],
                }
            ],
        }
    )


def test_direct_path_reports_swept_actor_collision():
    snapshot = navigation_snapshot()
    entities = snapshot.by_id()
    plan = plan_move_path(
        snapshot,
        entities["actor"],
        entities["actor"].transform.location,
        entities["goal"].transform.location,
        path_mode="direct",
        navigation_mesh=None,
        clearance=0.1,
        target_entity_id="goal",
    )
    assert [point.as_list() for point in plan.points] == [[1.0, 1.0, 0.0], [5.0, 5.0, 0.0]]
    assert plan.collision_ids == ["wall"]
    assert plan.considered_obstacle_ids == ["wall"]
    assert "Center Wall" in plan.warnings[0]


def test_navmesh_path_is_deterministic_connected_and_avoids_wall():
    snapshot = navigation_snapshot()
    entities = snapshot.by_id()
    arguments = (
        snapshot,
        entities["actor"],
        entities["actor"].transform.location,
        entities["goal"].transform.location,
    )
    first = plan_move_path(
        *arguments,
        path_mode="navmesh",
        navigation_mesh="nav-l",
        clearance=0.1,
        target_entity_id="goal",
    )
    second = plan_move_path(
        *arguments,
        path_mode="navmesh",
        navigation_mesh="L Corridor",
        clearance=0.1,
        target_entity_id="goal",
    )
    assert first == second
    assert first.navigation_mesh_id == "nav-l"
    assert first.collision_ids == []
    assert len(first.points) > 2
    assert first.points[0] == entities["actor"].transform.location
    assert first.points[-1] == entities["goal"].transform.location
    assert any(point.y == 4 for point in first.points[1:-1])


def test_navigation_rejects_unknown_outside_and_disconnected_meshes():
    snapshot = navigation_snapshot()
    actor = snapshot.by_id()["actor"]
    with pytest.raises(NavigationError) as unknown:
        plan_move_path(
            snapshot,
            actor,
            actor.transform.location,
            Vec3(x=5, y=5),
            path_mode="navmesh",
            navigation_mesh="missing",
            clearance=0,
            target_entity_id=None,
        )
    assert unknown.value.code == "unknown_navigation_mesh"

    with pytest.raises(NavigationError) as outside:
        plan_move_path(
            snapshot,
            actor,
            actor.transform.location,
            Vec3(x=50, y=50),
            path_mode="navmesh",
            navigation_mesh=None,
            clearance=0,
            target_entity_id=None,
        )
    assert outside.value.code == "point_outside_navigation_mesh"

    disconnected = snapshot.model_copy(deep=True)
    disconnected.navigation_meshes[0].polygons = [[0, 1, 2], [2, 4, 5]]
    with pytest.raises(NavigationError) as no_path:
        plan_move_path(
            disconnected,
            actor,
            actor.transform.location,
            Vec3(x=5, y=5),
            path_mode="navmesh",
            navigation_mesh=None,
            clearance=0,
            target_entity_id=None,
        )
    assert no_path.value.code == "navigation_path_not_found"


def test_navigation_fingerprint_changes_with_geometry_and_obstacles():
    snapshot = navigation_snapshot()
    baseline = navigation_environment_fingerprint(snapshot)
    assert baseline == "nav-63fb67061dd69bff488050e7"
    reordered = snapshot.model_copy(deep=True)
    reordered.entities.reverse()
    assert navigation_environment_fingerprint(reordered) == baseline

    moved_vertex = snapshot.model_copy(deep=True)
    moved_vertex.navigation_meshes[0].vertices[0].x = -1
    assert navigation_environment_fingerprint(moved_vertex) != baseline
    moved_wall = snapshot.model_copy(deep=True)
    moved_wall.entities[2].bounds.minimum.x = 2
    assert navigation_environment_fingerprint(moved_wall) != baseline


def test_path_frame_allocation_is_strict_monotonic_and_distance_weighted():
    points = [Vec3(x=0), Vec3(x=1), Vec3(x=4)]
    assert allocate_path_frames(points, 1, 41) == [1, 11, 41]
    repeated = [Vec3(), Vec3(), Vec3()]
    assert allocate_path_frames(repeated, 1, 3) == [1, 2, 3]
    with pytest.raises(NavigationError, match="only 1 are available") as dense:
        allocate_path_frames(points, 1, 2)
    assert dense.value.code == "navigation_path_too_dense"


def test_navigation_models_reject_invalid_topology_and_misconfigured_move():
    with pytest.raises(ValidationError, match="invalid vertex index"):
        NavigationMesh.model_validate(
            {
                "entity_id": "bad",
                "name": "Bad",
                "vertices": [{"x": 0}, {"x": 1}, {"y": 1}],
                "polygons": [[0, 1, 99]],
            }
        )
    with pytest.raises(ValidationError, match="exactly three"):
        NavigationMesh.model_validate(
            {
                "entity_id": "quad",
                "name": "Untriangulated Quad",
                "vertices": [
                    {"x": 0, "y": 0},
                    {"x": 1, "y": 0},
                    {"x": 1, "y": 1},
                    {"x": 0, "y": 1},
                ],
                "polygons": [[0, 1, 2, 3]],
            }
        )
    with pytest.raises(ValidationError, match="degenerate"):
        NavigationMesh.model_validate(
            {
                "entity_id": "flat-line",
                "name": "Flat Line",
                "vertices": [{"x": 0}, {"x": 1}, {"x": 2}],
                "polygons": [[0, 1, 2]],
            }
        )
    with pytest.raises(ValidationError, match="non-manifold"):
        NavigationMesh.model_validate(
            {
                "entity_id": "non-manifold",
                "name": "Non Manifold",
                "vertices": [
                    {"x": 0, "y": 0},
                    {"x": 1, "y": 0},
                    {"x": 0, "y": 1},
                    {"x": 1, "y": 1},
                    {"x": 0.5, "y": -1},
                ],
                "polygons": [[0, 1, 2], [1, 0, 3], [0, 1, 4]],
            }
        )
    with pytest.raises(ValidationError, match="navigation_mesh requires"):
        ShotSpec.model_validate(
            {
                "duration": 1,
                "beats": [
                    {
                        "type": "move_to",
                        "actor": "actor",
                        "target_position": {"x": 1},
                        "duration": 1,
                        "navigation_mesh": "nav-l",
                    }
                ],
            }
        )


def test_snapshot_rejects_more_than_supported_navigation_meshes():
    entities = []
    navigation_meshes = []
    for index in range(33):
        entity_id = f"nav-{index}"
        entities.append(
            {
                "id": entity_id,
                "name": f"Navigation {index}",
                "type": "MESH",
                "metadata": {"navigation_role": "navmesh"},
            }
        )
        navigation_meshes.append(
            {
                "entity_id": entity_id,
                "name": f"Navigation {index}",
                "vertices": [{"x": 0}, {"x": 1}, {"y": 1}],
                "polygons": [[0, 1, 2]],
            }
        )

    with pytest.raises(ValidationError) as too_many:
        SceneSnapshot.model_validate(
            {
                "scene_name": "Too many navigation meshes",
                "entities": entities,
                "navigation_meshes": navigation_meshes,
            }
        )
    assert "navigation_meshes" in str(too_many.value)
    assert "at most 32 items" in str(too_many.value)


def test_overhead_obstacle_does_not_report_false_collision():
    snapshot = navigation_snapshot()
    snapshot.entities[2].bounds.minimum.z = 10
    snapshot.entities[2].bounds.maximum.z = 12
    actor = snapshot.by_id()["actor"]
    plan = plan_move_path(
        snapshot,
        actor,
        actor.transform.location,
        Vec3(x=5, y=5),
        path_mode="direct",
        navigation_mesh=None,
        clearance=0.1,
        target_entity_id=None,
    )
    assert plan.collision_ids == []
    assert plan.considered_obstacle_ids == ["wall"]


def test_compiler_emits_editable_distance_timed_navmesh_keyframes_and_guards():
    snapshot = navigation_snapshot()
    snapshot.navigation_environment_fingerprint = navigation_environment_fingerprint(snapshot)
    shot = ShotSpec.model_validate(
        {
            "title": "Walk around wall",
            "duration": 4,
            "beats": [
                {
                    "type": "move_to",
                    "actor": "actor",
                    "target_entity": "goal",
                    "duration": 4,
                    "path_mode": "navmesh",
                    "navigation_mesh": "nav-l",
                }
            ],
        }
    )
    report = validate_shot(shot, snapshot)
    assert report.valid is True
    assert {issue.code for issue in report.issues} == {
        "navmesh_requires_linear_interpolation"
    }

    patch = compile_shot(shot, snapshot)
    movement = patch.operations[1]
    frames = movement.payload["frames"]
    assert patch.schema_version == "1.2"
    assert patch.navigation_environment_fingerprint == (
        snapshot.navigation_environment_fingerprint
    )
    assert patch.fingerprint_entities == ["actor", "goal", "nav-l", "wall"]
    assert movement.payload["path_mode"] == "navmesh"
    assert movement.payload["navigation_mesh"] == "nav-l"
    assert movement.payload["interpolation"] == "LINEAR"
    assert len(frames) > 2
    assert [item["frame"] for item in frames] == sorted(
        {item["frame"] for item in frames}
    )
    assert frames[0]["location"] == [1.0, 1.0, 0.0]
    assert frames[-1]["location"] == [5.0, 5.0, 0.0]


def test_direct_collision_is_a_warning_and_navigation_failure_is_an_error():
    snapshot = navigation_snapshot()
    direct = ShotSpec.model_validate(
        {
            "duration": 2,
            "beats": [
                {
                    "type": "move_to",
                    "actor": "actor",
                    "target_entity": "goal",
                    "duration": 2,
                }
            ],
        }
    )
    direct_report = validate_shot(direct, snapshot)
    assert direct_report.valid is True
    assert [issue.code for issue in direct_report.issues] == ["path_collision"]
    assert "Center Wall" in compile_shot(direct, snapshot).warnings[0]

    outside = direct.model_copy(deep=True)
    outside.beats[0].path_mode = "navmesh"
    outside.beats[0].target_entity = None
    outside.beats[0].target_position = Vec3(x=50, y=50)
    outside_report = validate_shot(outside, snapshot)
    assert outside_report.valid is False
    assert any(
        issue.code == "point_outside_navigation_mesh" for issue in outside_report.issues
    )
    with pytest.raises(ValueError, match="same navigation mesh"):
        compile_shot(outside, snapshot)


def test_declared_navigation_fingerprint_and_dense_path_fail_closed():
    snapshot = navigation_snapshot()
    snapshot.navigation_environment_fingerprint = "nav-stale"
    shot = ShotSpec.model_validate(
        {
            "fps": 1,
            "duration": 1,
            "beats": [
                {
                    "type": "move_to",
                    "actor": "actor",
                    "target_entity": "goal",
                    "duration": 1,
                    "path_mode": "navmesh",
                }
            ],
        }
    )
    report = validate_shot(shot, snapshot)
    codes = {issue.code for issue in report.issues}
    assert "navigation_fingerprint_mismatch" in codes
    assert "navigation_path_too_dense" in codes
    assert report.valid is False
