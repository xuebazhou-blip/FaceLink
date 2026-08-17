from __future__ import annotations

import hashlib
import heapq
import json
import math
from dataclasses import dataclass, field

from .models import NavigationMesh, SceneEntity, SceneSnapshot, Vec3

EPSILON = 1e-7


class NavigationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class NavigationPlan:
    points: list[Vec3]
    navigation_mesh_id: str | None = None
    collision_ids: list[str] = field(default_factory=list)
    considered_obstacle_ids: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _number(value: float) -> float:
    rounded = round(float(value), 6)
    return 0.0 if rounded == 0.0 else rounded


def _vector_payload(value: Vec3) -> list[float]:
    return [_number(value.x), _number(value.y), _number(value.z)]


def navigation_environment_fingerprint(snapshot: SceneSnapshot) -> str:
    navigation_meshes = [
        {
            "entity_id": mesh.entity_id,
            "vertices": [_vector_payload(vertex) for vertex in mesh.vertices],
            "polygons": [list(polygon) for polygon in mesh.polygons],
        }
        for mesh in sorted(snapshot.navigation_meshes, key=lambda item: item.entity_id)
    ]
    obstacles = []
    for entity in sorted(snapshot.entities, key=lambda item: item.id):
        if entity.metadata.get("navigation_role") != "obstacle":
            continue
        bounds = entity.bounds
        obstacles.append(
            {
                "entity_id": entity.id,
                "bounds": (
                    {
                        "minimum": _vector_payload(bounds.minimum),
                        "maximum": _vector_payload(bounds.maximum),
                    }
                    if bounds
                    else None
                ),
            }
        )
    encoded = json.dumps(
        {"navigation_meshes": navigation_meshes, "obstacles": obstacles},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "nav-" + hashlib.sha256(encoded).hexdigest()[:24]


def _point_on_segment(point: Vec3, first: Vec3, second: Vec3) -> bool:
    cross = (point.x - first.x) * (second.y - first.y) - (
        point.y - first.y
    ) * (second.x - first.x)
    if abs(cross) > EPSILON:
        return False
    return (
        min(first.x, second.x) - EPSILON
        <= point.x
        <= max(first.x, second.x) + EPSILON
        and min(first.y, second.y) - EPSILON
        <= point.y
        <= max(first.y, second.y) + EPSILON
    )


def _point_in_polygon(point: Vec3, mesh: NavigationMesh, polygon: list[int]) -> bool:
    vertices = [mesh.vertices[index] for index in polygon]
    for index, first in enumerate(vertices):
        if _point_on_segment(point, first, vertices[(index + 1) % len(vertices)]):
            return True
    inside = False
    previous = vertices[-1]
    for current in vertices:
        crosses = (current.y > point.y) != (previous.y > point.y)
        if crosses:
            intersection = (previous.x - current.x) * (point.y - current.y) / (
                previous.y - current.y
            ) + current.x
            if point.x < intersection:
                inside = not inside
        previous = current
    return inside


def _containing_polygons(point: Vec3, mesh: NavigationMesh) -> list[int]:
    return [
        index
        for index, polygon in enumerate(mesh.polygons)
        if _point_in_polygon(point, mesh, polygon)
    ]


def _centroid(mesh: NavigationMesh, polygon_index: int) -> Vec3:
    polygon = mesh.polygons[polygon_index]
    count = float(len(polygon))
    return Vec3(
        x=sum(mesh.vertices[index].x for index in polygon) / count,
        y=sum(mesh.vertices[index].y for index in polygon) / count,
        z=sum(mesh.vertices[index].z for index in polygon) / count,
    )


def _distance(first: Vec3, second: Vec3) -> float:
    return math.dist(first.as_list(), second.as_list())


def _adjacency(mesh: NavigationMesh):
    owners: dict[tuple[int, int], list[int]] = {}
    for polygon_index, polygon in enumerate(mesh.polygons):
        for index, first in enumerate(polygon):
            second = polygon[(index + 1) % len(polygon)]
            edge = tuple(sorted((first, second)))
            owners.setdefault(edge, []).append(polygon_index)
    neighbors = {index: set() for index in range(len(mesh.polygons))}
    portals = {}
    for edge, polygon_indices in owners.items():
        for position, first in enumerate(sorted(polygon_indices)):
            for second in sorted(polygon_indices)[position + 1 :]:
                neighbors[first].add(second)
                neighbors[second].add(first)
                portals[(first, second)] = edge
                portals[(second, first)] = edge
    return neighbors, portals


def _polygon_path(
    mesh: NavigationMesh,
    start_polygon: int,
    goal_polygon: int,
    neighbors,
) -> list[int] | None:
    if start_polygon == goal_polygon:
        return [start_polygon]
    centroids = [_centroid(mesh, index) for index in range(len(mesh.polygons))]
    goal_centroid = centroids[goal_polygon]
    queue = [
        (_distance(centroids[start_polygon], goal_centroid), 0.0, start_polygon)
    ]
    costs = {start_polygon: 0.0}
    previous = {}
    while queue:
        _, cost, current = heapq.heappop(queue)
        if cost > costs.get(current, math.inf) + EPSILON:
            continue
        if current == goal_polygon:
            path = [current]
            while current in previous:
                current = previous[current]
                path.append(current)
            return list(reversed(path))
        for neighbor in sorted(neighbors[current]):
            next_cost = cost + _distance(centroids[current], centroids[neighbor])
            if next_cost + EPSILON >= costs.get(neighbor, math.inf):
                continue
            costs[neighbor] = next_cost
            previous[neighbor] = current
            priority = next_cost + _distance(centroids[neighbor], goal_centroid)
            heapq.heappush(queue, (priority, next_cost, neighbor))
    return None


def _select_mesh(
    snapshot: SceneSnapshot,
    start: Vec3,
    goal: Vec3,
    requested: str | None,
) -> tuple[NavigationMesh, list[int], list[int], list[str]]:
    meshes = snapshot.navigation_meshes
    if requested:
        meshes = [mesh for mesh in meshes if mesh.entity_id == requested or mesh.name == requested]
        if not meshes:
            raise NavigationError(
                "unknown_navigation_mesh",
                f"Navigation mesh '{requested}' does not exist in the scene snapshot.",
            )
    candidates = []
    for mesh in sorted(meshes, key=lambda item: item.entity_id):
        start_polygons = _containing_polygons(start, mesh)
        goal_polygons = _containing_polygons(goal, mesh)
        if start_polygons and goal_polygons:
            candidates.append((mesh, start_polygons, goal_polygons))
    if not candidates:
        raise NavigationError(
            "point_outside_navigation_mesh",
            "Movement start and target must lie on the same navigation mesh.",
        )
    warnings = []
    if len(candidates) > 1 and requested is None:
        warnings.append(
            f"Multiple navigation meshes contain the path; selected '{candidates[0][0].name}'."
        )
    mesh, start_polygons, goal_polygons = candidates[0]
    return mesh, start_polygons, goal_polygons, warnings


def _navmesh_path(
    snapshot: SceneSnapshot,
    start: Vec3,
    goal: Vec3,
    requested: str | None,
) -> tuple[list[Vec3], str, list[str]]:
    mesh, start_polygons, goal_polygons, warnings = _select_mesh(
        snapshot, start, goal, requested
    )
    neighbors, portals = _adjacency(mesh)
    choices = []
    for start_polygon in start_polygons:
        for goal_polygon in goal_polygons:
            polygon_path = _polygon_path(
                mesh, start_polygon, goal_polygon, neighbors
            )
            if polygon_path is not None:
                candidate_points = [start]
                for first, second in zip(
                    polygon_path, polygon_path[1:], strict=False
                ):
                    edge = portals[(first, second)]
                    edge_start, edge_end = (mesh.vertices[index] for index in edge)
                    candidate_points.append(
                        Vec3(
                            x=(edge_start.x + edge_end.x) / 2.0,
                            y=(edge_start.y + edge_end.y) / 2.0,
                            z=(edge_start.z + edge_end.z) / 2.0,
                        )
                    )
                candidate_points.append(goal)
                cost = sum(
                    _distance(first, second)
                    for first, second in zip(
                        candidate_points, candidate_points[1:], strict=False
                    )
                )
                choices.append((cost, polygon_path, candidate_points))
    if not choices:
        raise NavigationError(
            "navigation_path_not_found",
            f"No connected navigation path exists on '{mesh.name}'.",
        )
    _, _, points = min(choices, key=lambda item: (round(item[0], 9), item[1]))
    deduplicated = [points[0]]
    for point in points[1:]:
        if _distance(point, deduplicated[-1]) > EPSILON:
            deduplicated.append(point)
    if len(deduplicated) == 1:
        deduplicated.append(goal)
    return deduplicated, mesh.entity_id, warnings


def _segment_intersects_rectangle(
    start: Vec3,
    end: Vec3,
    minimum_x: float,
    maximum_x: float,
    minimum_y: float,
    maximum_y: float,
) -> bool:
    lower = 0.0
    upper = 1.0
    for origin, delta, minimum, maximum in (
        (start.x, end.x - start.x, minimum_x, maximum_x),
        (start.y, end.y - start.y, minimum_y, maximum_y),
    ):
        if abs(delta) <= EPSILON:
            if origin < minimum or origin > maximum:
                return False
            continue
        first = (minimum - origin) / delta
        second = (maximum - origin) / delta
        if first > second:
            first, second = second, first
        lower = max(lower, first)
        upper = min(upper, second)
        if lower > upper:
            return False
    return True


def _path_collisions(
    points: list[Vec3],
    actor: SceneEntity,
    snapshot: SceneSnapshot,
    clearance: float,
    excluded_ids: set[str],
) -> tuple[list[str], list[str]]:
    actor_bounds = actor.bounds
    actor_min_x = (
        actor_bounds.minimum.x - actor.transform.location.x if actor_bounds else 0.0
    )
    actor_max_x = (
        actor_bounds.maximum.x - actor.transform.location.x if actor_bounds else 0.0
    )
    actor_min_y = (
        actor_bounds.minimum.y - actor.transform.location.y if actor_bounds else 0.0
    )
    actor_max_y = (
        actor_bounds.maximum.y - actor.transform.location.y if actor_bounds else 0.0
    )
    actor_min_z = (
        actor_bounds.minimum.z - actor.transform.location.z if actor_bounds else 0.0
    )
    actor_max_z = (
        actor_bounds.maximum.z - actor.transform.location.z if actor_bounds else 0.0
    )
    path_min_z = min(point.z for point in points) + actor_min_z
    path_max_z = max(point.z for point in points) + actor_max_z
    considered = []
    collisions = []
    for obstacle in sorted(snapshot.entities, key=lambda item: item.id):
        if (
            obstacle.id in excluded_ids
            or obstacle.metadata.get("navigation_role") != "obstacle"
        ):
            continue
        considered.append(obstacle.id)
        bounds = obstacle.bounds
        if bounds is None:
            continue
        if path_max_z < bounds.minimum.z or path_min_z > bounds.maximum.z:
            continue
        if any(
            _segment_intersects_rectangle(
                start,
                end,
                bounds.minimum.x - actor_max_x - clearance,
                bounds.maximum.x - actor_min_x + clearance,
                bounds.minimum.y - actor_max_y - clearance,
                bounds.maximum.y - actor_min_y + clearance,
            )
            for start, end in zip(points, points[1:], strict=False)
        ):
            collisions.append(obstacle.id)
    return collisions, considered


def plan_move_path(
    snapshot: SceneSnapshot,
    actor: SceneEntity,
    start: Vec3,
    goal: Vec3,
    *,
    path_mode: str,
    navigation_mesh: str | None,
    clearance: float,
    target_entity_id: str | None,
) -> NavigationPlan:
    if path_mode == "navmesh":
        points, mesh_id, warnings = _navmesh_path(
            snapshot, start, goal, navigation_mesh
        )
    else:
        points, mesh_id, warnings = [start, goal], None, []
    collisions, considered = _path_collisions(
        points,
        actor,
        snapshot,
        clearance,
        {actor.id, target_entity_id} - {None},
    )
    if collisions:
        names = [snapshot.by_id()[entity_id].name for entity_id in collisions]
        warnings.append(
            f"Movement path for '{actor.name}' intersects obstacle(s): {', '.join(names)}."
        )
    return NavigationPlan(
        points=points,
        navigation_mesh_id=mesh_id,
        collision_ids=collisions,
        considered_obstacle_ids=considered,
        warnings=warnings,
    )


def allocate_path_frames(points: list[Vec3], start_frame: int, end_frame: int) -> list[int]:
    if not points:
        raise NavigationError("empty_navigation_path", "Navigation path contains no points.")
    if len(points) == 1:
        return [start_frame]
    segment_count = len(points) - 1
    if end_frame - start_frame < segment_count:
        raise NavigationError(
            "navigation_path_too_dense",
            f"Navigation path needs {segment_count} frame intervals but only "
            f"{end_frame - start_frame} are available.",
        )
    distances = [
        _distance(first, second) for first, second in zip(points, points[1:], strict=False)
    ]
    total = sum(distances)
    cumulative = 0.0
    frames = [start_frame]
    for index, distance in enumerate(distances[:-1], start=1):
        cumulative += distance
        ratio = cumulative / total if total > EPSILON else index / segment_count
        candidate = start_frame + round((end_frame - start_frame) * ratio)
        minimum = frames[-1] + 1
        maximum = end_frame - (segment_count - index)
        frames.append(min(max(candidate, minimum), maximum))
    frames.append(end_frame)
    return frames
