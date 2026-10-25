from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .geometry import (
    Segment,
    angle_difference,
    bbox,
    bbox_area,
    bbox_center,
    bbox_dims,
    distance,
    entity_position,
    entity_segments,
    expand_bbox,
    is_effectively_closed,
    merge_collinear,
    orthogonality_score,
    point_in_bbox,
    point_segment_distance,
    segment_angle,
    segment_parallel_distance,
    union_interval_coverage,
)


def _merge_rules(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Merge objects, replace lists; a list key ending in _add appends terms."""
    result = copy.deepcopy(base)
    for key, value in overrides.items():
        if key.endswith("_add"):
            target = key[:-4]
            if not isinstance(value, list) or not isinstance(result.get(target, []), list):
                raise ValueError(f"{key} requires a list")
            result[target] = list(dict.fromkeys(result.get(target, []) + value))
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_rules(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_rules(path: str | Path, profile: str | Path | dict[str, Any] | None = None) -> dict[str, Any]:
    rules = json.loads(Path(path).read_text(encoding="utf-8"))
    if profile is not None:
        overrides = profile if isinstance(profile, dict) else json.loads(Path(profile).read_text(encoding="utf-8"))
        rules = _merge_rules(rules, overrides)
    policy = rules.get("policy", {})
    if policy.get("allow_handle_overrides") or policy.get("allow_coordinate_overrides"):
        raise ValueError("File-specific handle or coordinate overrides are forbidden by this classifier.")
    return rules


def _matches(value: str | None, terms: list[str]) -> bool:
    """ASCII semantic words must end at a letter boundary (COL != COLOR).

    Digits remain valid separators so common numbered layers 2COL and WALL1
    work. Chinese terms match substrings because those names lack word spaces.
    A glob: prefix is an explicit opt-in layer pattern for a project profile.
    """
    lowered = (value or "").casefold()
    for term in terms:
        term = term.casefold()
        if not term:
            continue
        if term.startswith("glob:"):
            import fnmatch
            if fnmatch.fnmatchcase(lowered, term[5:]):
                return True
        elif re.search(r"[^\x00-\x7f]", term):
            if term in lowered:
                return True
        elif re.search(r"(?<![a-z])" + re.escape(term) + r"(?![a-z])", lowered):
            return True
    return False


def _is_layer(layer: str | None, category: str, rules: dict[str, Any]) -> bool:
    return _matches(layer, rules["layer_semantics"].get(category, []))


def _curtain_name(name: str | None, rules: dict[str, Any]) -> bool:
    return _matches(name, rules["layer_semantics"]["curtain"])


def _is_ignored_fire_protection(layer: str | None, rules: dict[str, Any]) -> bool:
    return _is_layer(layer, "fire_protection_ignored", rules)


def _sanitize_box(points: list[tuple[float, float]]) -> dict[str, Any]:
    return {
        "min": {"x": min(point[0] for point in points), "y": min(point[1] for point in points)},
        "max": {"x": max(point[0] for point in points), "y": max(point[1] for point in points)},
    }


def _cluster_points(points: list[tuple[float, float]], radius: float) -> list[list[int]]:
    """Deterministic complete-link clustering that cannot chain distant rooms together."""
    clusters: list[list[int]] = []
    order = sorted(range(len(points)), key=lambda index: (points[index][0], points[index][1], index))
    for index in order:
        viable: list[tuple[float, int]] = []
        for cluster_index, cluster in enumerate(clusters):
            farthest = max(distance(points[index], points[member]) for member in cluster)
            if farthest <= radius:
                viable.append((farthest, cluster_index))
        if viable:
            clusters[min(viable)[1]].append(index)
        else:
            clusters.append([index])
    return clusters


def _point_in_any_bbox(point: tuple[float, float], boxes: list[dict[str, Any]]) -> bool:
    return not boxes or any(point_in_bbox(point, box) for box in boxes)


def _bbox_contains(outer: dict[str, Any], inner: dict[str, Any], tolerance: float) -> bool:
    return (
        outer["min"]["x"] - tolerance <= inner["min"]["x"]
        and outer["min"]["y"] - tolerance <= inner["min"]["y"]
        and outer["max"]["x"] + tolerance >= inner["max"]["x"]
        and outer["max"]["y"] + tolerance >= inner["max"]["y"]
    )


def _bbox_intersection_ratio(first: dict[str, Any], second: dict[str, Any]) -> float:
    width = max(0.0, min(first["max"]["x"], second["max"]["x"]) - max(first["min"]["x"], second["min"]["x"]))
    height = max(0.0, min(first["max"]["y"], second["max"]["y"]) - max(first["min"]["y"], second["min"]["y"]))
    denominator = min(bbox_area(first), bbox_area(second))
    return width * height / denominator if denominator else 0.0


def _bbox_union(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    return {
        "min": {"x": min(first["min"]["x"], second["min"]["x"]), "y": min(first["min"]["y"], second["min"]["y"])},
        "max": {"x": max(first["max"]["x"], second["max"]["x"]), "y": max(first["max"]["y"], second["max"]["y"])},
    }


def _bbox_edge_delta(first: dict[str, Any], second: dict[str, Any]) -> float:
    """Largest corresponding-edge offset between two model-space extents."""
    return max(
        abs(float(first["min"]["x"]) - float(second["min"]["x"])),
        abs(float(first["min"]["y"]) - float(second["min"]["y"])),
        abs(float(first["max"]["x"]) - float(second["max"]["x"])),
        abs(float(first["max"]["y"]) - float(second["max"]["y"])),
    )


def _box_segment_dicts(box: dict[str, Any], source_handle: str | None, layer: str) -> list[dict[str, Any]]:
    x0, y0, x1, y1 = box["min"]["x"], box["min"]["y"], box["max"]["x"], box["max"]["y"]
    return [
        Segment((x0, y0), (x1, y0), source_handle=source_handle, layer=layer).as_dict(),
        Segment((x1, y0), (x1, y1), source_handle=source_handle, layer=layer).as_dict(),
        Segment((x1, y1), (x0, y1), source_handle=source_handle, layer=layer).as_dict(),
        Segment((x0, y1), (x0, y0), source_handle=source_handle, layer=layer).as_dict(),
    ]


def _near_bbox_perimeter(point: tuple[float, float], box: dict[str, Any], band: float) -> bool:
    if not point_in_bbox(point, expand_bbox(box, band)):
        return False
    distances = (
        abs(point[0] - box["min"]["x"]),
        abs(point[0] - box["max"]["x"]),
        abs(point[1] - box["min"]["y"]),
        abs(point[1] - box["max"]["y"]),
    )
    return min(distances) <= band


def _polygon_area(vertices: list[dict[str, Any]]) -> float:
    points = [item.get("point", item) for item in vertices]
    if len(points) < 3:
        return 0.0
    return abs(
        sum(
            float(first["x"]) * float(second["y"]) - float(second["x"]) * float(first["y"])
            for first, second in zip(points, points[1:] + points[:1])
        )
    ) / 2.0


def _resolved_handle(entity: dict[str, Any]) -> str:
    """Return a stable per-instance handle for geometry expanded from a block."""
    path = str(entity.get("instance_path") or "").strip("/")
    source = str(entity.get("source_handle") or entity.get("handle") or "")
    if not path:
        return source
    if path == source or path.endswith(f"/{source}"):
        return path
    return f"{path}/{source}" if source else path


def _materialize_resolved_geometry(
    entity: dict[str, Any],
    *,
    layer: str | None = None,
    evidence: str,
) -> dict[str, Any]:
    """Copy normalized block geometry into model-space classification form."""
    output = copy.deepcopy(entity)
    output["handle"] = _resolved_handle(entity)
    output["owner_block"] = "*Model_Space"
    output["layer"] = str(layer if layer is not None else entity.get("layer") or "")
    output["local_extents"] = copy.deepcopy(entity.get("world_extents"))
    output["resolved_source_handle"] = entity.get("source_handle")
    output["raw_source_handles"] = list(entity.get("source_handles") or [entity.get("source_handle")])
    output["source_handles"] = [output["handle"]] if output["handle"] else []
    output["resolved_source_block"] = entity.get("source_block")
    output["nested_layer_evidence"] = evidence
    if entity.get("type") == "LINE":
        output["start"] = copy.deepcopy(entity.get("start_world"))
        output["end"] = copy.deepcopy(entity.get("end_world"))
    elif entity.get("type") in {"LWPOLYLINE", "POLYLINE"}:
        vertices = copy.deepcopy(entity.get("vertices_world") or [])
        output["vertices"] = vertices
        if output.get("area") is None:
            output["area"] = _polygon_area(vertices)
    if entity.get("curve_points_world"):
        output["curve_points"] = copy.deepcopy(entity["curve_points_world"])
    if entity.get("center_world"):
        output["center"] = copy.deepcopy(entity["center_world"])
    output["source_layers"] = list(entity.get("source_layers") or [str(entity.get("raw_layer", entity.get("layer")) or "")])
    return output


def _explicit_nested_semantic_geometry(
    resolved: list[dict[str, Any]], rules: dict[str, Any]
) -> list[dict[str, Any]]:
    """Materialize block descendants whose own layer has explicit semantics.

    Container inheritance stays conservative, but an object's own WALL/COL
    layer is authoritative evidence even when that object lives several block
    levels below model space. Opening descendants are handled separately so
    they remain gaps rather than becoming wall geometry.
    """
    categories = ("architectural_wall", "wall_finish", "structural")
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for entity in resolved:
        path = str(entity.get("instance_path") or "")
        if not path or entity.get("type") not in {"LINE", "LWPOLYLINE", "POLYLINE", "ARC"}:
            continue
        layer = str(entity.get("layer") or "")
        if _is_ignored_fire_protection(layer, rules):
            continue
        matched = [category for category in categories if _is_layer(layer, category, rules)]
        if not matched:
            continue
        key = (path, str(entity.get("source_handle") or ""), str(entity.get("type") or ""))
        if key in seen:
            continue
        seen.add(key)
        output.append(
            _materialize_resolved_geometry(
                entity,
                evidence=f"explicit_nested_object_layer:{','.join(matched)}",
            )
        )
    return output


def _bbox_gap(first: dict[str, Any], second: dict[str, Any]) -> float:
    x_gap = max(
        0.0,
        float(first["min"]["x"]) - float(second["max"]["x"]),
        float(second["min"]["x"]) - float(first["max"]["x"]),
    )
    y_gap = max(
        0.0,
        float(first["min"]["y"]) - float(second["max"]["y"]),
        float(second["min"]["y"]) - float(first["max"]["y"]),
    )
    return math.hypot(x_gap, y_gap)


def _segment_bbox(segment: Segment) -> dict[str, Any]:
    return {
        "min": {"x": min(segment.p1[0], segment.p2[0]), "y": min(segment.p1[1], segment.p2[1])},
        "max": {"x": max(segment.p1[0], segment.p2[0]), "y": max(segment.p1[1], segment.p2[1])},
    }


def _path_is_within(path: str, roots: set[str]) -> bool:
    return any(path == root or path.startswith(f"{root}/") for root in roots if root)


def _inherit_direct_container_geometry(
    model: list[dict[str, Any]],
    resolved: list[dict[str, Any]],
    rules: dict[str, Any],
    plan_domains: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Materialize direct world-space geometry from plan-scale semantic containers.

    Many consultant drawings keep real structural geometry on layer 0 inside one
    top-level INSERT whose layer carries the structural meaning.  Only direct
    children inherit that meaning: grandchildren commonly contain grids,
    annotations, or symbols and must stay excluded.
    """
    inheritance = rules.get("semantic_container_inheritance", {})
    minimum_width = float(inheritance.get("minimum_container_width_mm", 5000.0))
    minimum_height = float(inheritance.get("minimum_container_height_mm", 5000.0))
    minimum_ratio = float(inheritance.get("minimum_plan_extent_ratio", 0.1))
    minimum_children = int(inheritance.get("minimum_direct_linear_children", 4))
    domains = plan_domains or []
    output: list[dict[str, Any]] = []

    for container in model:
        if container.get("type") != "INSERT" or not _is_layer(container.get("layer"), "structural", rules):
            continue
        container_box = bbox(container)
        if not container_box:
            continue
        width, height = bbox_dims(container_box)
        absolute_scale = width >= minimum_width and height >= minimum_height
        relative_scale = any(
            width >= bbox_dims(domain)[0] * minimum_ratio
            and height >= bbox_dims(domain)[1] * minimum_ratio
            for domain in domains
        )
        if not (absolute_scale or relative_scale):
            continue

        root_handle = str(container.get("handle") or "")
        direct = [
            child
            for child in resolved
            if child.get("type") in {"LINE", "LWPOLYLINE", "ARC"}
            and str(child.get("instance_path") or "") == root_handle
            and not any(
                _is_layer(child.get("layer"), category, rules)
                for category in ("architectural_wall", "wall_finish", "structural", "opening")
            )
        ]
        if len(direct) < minimum_children:
            continue

        for child in direct:
            entity = _materialize_resolved_geometry(
                child,
                layer=str(container.get("layer") or ""),
                evidence="direct_child_of_plan_scale_structural_container",
            )
            entity["semantic_container_handle"] = root_handle
            entity["semantic_container_layer"] = container.get("layer")
            entity["semantic_container_block"] = container.get("block_name")
            entity["inherited_semantic_evidence"] = "direct_child_of_plan_scale_structural_container"
            output.append(entity)
    return output


def _candidate_column_entities(model: list[dict[str, Any]], rules: dict[str, Any]) -> list[dict[str, Any]]:
    column_rules = rules["column"]
    excluded = column_rules["excluded_layer_terms"]
    candidates = []
    for entity in model:
        if entity.get("type") != "LWPOLYLINE" or not bbox(entity):
            continue
        layer = str(entity.get("layer") or "")
        if _matches(layer, excluded) or _is_ignored_fire_protection(layer, rules):
            continue
        width, height = bbox_dims(bbox(entity))
        if min(width, height) < column_rules["minimum_width_mm"] or max(width, height) > column_rules["maximum_width_mm"]:
            continue
        aspect = max(width, height) / min(width, height)
        if aspect > column_rules["maximum_aspect_ratio"] or not is_effectively_closed(entity, rules["geometry"]["endpoint_snap_mm"]):
            continue
        area = abs(float(entity.get("area") or 0))
        fill_ratio = area / (width * height) if width * height else 0.0
        if fill_ratio < column_rules["minimum_bbox_fill_ratio"]:
            continue
        orthogonality = orthogonality_score(entity, column_rules["orthogonality_tolerance_degrees"])
        if orthogonality < 0.7:
            continue
        candidates.append({
            "entity": entity,
            "width": width,
            "height": height,
            "aspect": aspect,
            "fill_ratio": fill_ratio,
            "orthogonality": orthogonality,
        })

    center_tolerance = float(column_rules["deduplicate_center_tolerance_mm"])
    unique: list[dict[str, Any]] = []
    for candidate in sorted(candidates, key=lambda item: bbox_area(bbox(item["entity"])), reverse=True):
        center = bbox_center(bbox(candidate["entity"]))
        match = next(
            (
                item
                for item in unique
                if center
                and distance(center, item["center"]) <= center_tolerance
                and abs(candidate["width"] - item["width"]) <= center_tolerance
                and abs(candidate["height"] - item["height"]) <= center_tolerance
            ),
            None,
        )
        if match:
            match["entities"].append(candidate["entity"])
            continue
        candidate["entities"] = [candidate["entity"]]
        candidate["center"] = center
        unique.append(candidate)

    tolerance = float(column_rules["similar_size_tolerance_mm"])
    for candidate in unique:
        candidate["similar_size_count"] = sum(
            1
            for other in unique
            if abs(candidate["width"] - other["width"]) <= tolerance and abs(candidate["height"] - other["height"]) <= tolerance
        )
    return unique


def _classify_columns(
    model: list[dict[str, Any]], rules: dict[str, Any], plan_domains: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    scored = []
    column_rules = rules["column"]
    plan_domains = plan_domains or []
    structural_segments = [
        segment
        for entity in model
        if entity.get("type") in {"LINE", "LWPOLYLINE", "ARC"} and _is_layer(entity.get("layer"), "structural", rules)
        for segment in entity_segments(entity, world=False, max_curve_error_mm=rules["geometry"].get("max_curve_error_mm", 1.0))
    ]
    for candidate in _candidate_column_entities(model, rules):
        entity = candidate["entity"]
        score = 0.0
        evidence = []
        if _is_layer(entity.get("layer"), "structural", rules):
            score += 2.5
            evidence.append(
                "inherited_structural_container_semantics"
                if entity.get("semantic_container_handle")
                else "structural_layer_semantics"
            )
        if candidate["aspect"] <= column_rules["near_square_aspect_ratio"]:
            score += 1.5
            evidence.append("compact_near_square_footprint")
        if candidate["similar_size_count"] >= column_rules["minimum_repetition_count"]:
            score += 1.25
            evidence.append(f"repeated_size_cluster={candidate['similar_size_count']}")
        if candidate["orthogonality"] >= 0.9:
            score += 0.5
            evidence.append("orthogonal_closed_outline")
        if candidate["fill_ratio"] >= 0.9:
            score += 0.5
            evidence.append("solid_rectangular_footprint")
        perimeter_context = any(
            _near_bbox_perimeter(candidate["center"], domain, float(column_rules["perimeter_context_band_mm"]))
            for domain in plan_domains
        )
        if perimeter_context:
            score += float(column_rules["perimeter_context_score"])
            evidence.append("aligned_with_dominant_plan_perimeter")
        structural_distance = min(
            (point_segment_distance(candidate["center"], segment) for segment in structural_segments),
            default=float("inf"),
        )
        if structural_distance <= float(column_rules["structural_wall_context_mm"]):
            score += float(column_rules["structural_wall_context_score"])
            evidence.append("adjacent_to_structural_wall_network")
        if score < column_rules["classification_score"]:
            continue
        if not (_is_layer(entity.get("layer"), "structural", rules) or perimeter_context or structural_distance <= float(column_rules["structural_wall_context_mm"])):
            continue
        segments = entity_segments(entity, world=False, max_curve_error_mm=rules["geometry"].get("max_curve_error_mm", 1.0))
        source_entities = candidate["entities"]
        scored.append({
            "class": "column",
            "source_handle": entity.get("handle"),
            "source_handles": sorted({item.get("handle") for item in source_entities if item.get("handle")}),
            "source_layer": entity.get("layer"),
            "source_layers": sorted({layer for item in source_entities for layer in (item.get("source_layers") or [str(item.get("raw_layer", item.get("layer")) or "")])}),
            "bbox": bbox(entity),
            "width_mm": round(candidate["width"], 3),
            "depth_mm": round(candidate["height"], 3),
            "score": round(score, 3),
            "evidence": evidence,
            "segments": [segment.as_dict() for segment in segments],
        })

    containment_tolerance = float(column_rules["containment_tolerance_mm"])
    overlap_threshold = float(column_rules["overlap_consolidation_ratio"])
    output = []
    for item in sorted(scored, key=lambda value: bbox_area(value["bbox"]), reverse=True):
        parent = next(
            (
                existing
                for existing in output
                if _bbox_contains(existing["bbox"], item["bbox"], containment_tolerance)
                or _bbox_intersection_ratio(existing["bbox"], item["bbox"]) >= overlap_threshold
            ),
            None,
        )
        if parent:
            parent["source_handles"] = sorted(set(parent["source_handles"]) | set(item["source_handles"]))
            parent["source_layers"] = sorted(set(parent["source_layers"]) | set(item["source_layers"]))
            parent["source_layer"] = "+".join(parent["source_layers"])
            parent["bbox"] = _bbox_union(parent["bbox"], item["bbox"])
            parent["width_mm"], parent["depth_mm"] = [round(value, 3) for value in bbox_dims(parent["bbox"])]
            if "overlapping_closed_footprints_consolidated" not in parent["evidence"]:
                parent["evidence"].append("overlapping_closed_footprints_consolidated")
            parent["segments"] = _box_segment_dicts(parent["bbox"], parent["source_handle"], parent["source_layer"])
            for segment in parent["segments"]:
                segment["source_layers"] = list(parent["source_layers"])
                segment["source_handles"] = list(parent["source_handles"])
            continue
        item["id"] = f"COLUMN-{len(output) + 1:03d}"
        output.append(item)
    return output


def _find_sanitary_seeds(
    model: list[dict[str, Any]],
    resolved: list[dict[str, Any]],
    rules: dict[str, Any],
    plan_domains: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    seeds: list[dict[str, Any]] = []
    seen = set()
    maximum_extent = float(rules["sanitary_enclosure"].get("maximum_fixture_extent_mm", 3500.0))

    def fixture_scale_ok(entity: dict[str, Any]) -> bool:
        box = bbox(entity)
        return not box or max(bbox_dims(box)) <= maximum_extent

    for entity in model:
        layer = entity.get("layer")
        name = entity.get("block_name")
        if (
            entity.get("type") == "INSERT"
            and fixture_scale_ok(entity)
            and (_is_layer(layer, "sanitary", rules) or _matches(name, rules["layer_semantics"]["sanitary"]))
        ):
            point = entity_position(entity)
            if point and _point_in_any_bbox(point, plan_domains or []):
                key = (round(point[0], 3), round(point[1], 3), entity.get("handle"))
                if key not in seen:
                    seen.add(key)
                    seeds.append({"point": point, "handle": entity.get("handle"), "layer": layer, "block_name": name, "source": "top_level"})
    for entity in resolved:
        if entity.get("type") != "BLOCK_REFERENCE" or not fixture_scale_ok(entity):
            continue
        layer, name = entity.get("layer"), entity.get("block_name")
        if not (_is_layer(layer, "sanitary", rules) or _matches(name, rules["layer_semantics"]["sanitary"])):
            continue
        point = entity_position(entity)
        if point and _point_in_any_bbox(point, plan_domains or []):
            key = (round(point[0], 3), round(point[1], 3), entity.get("instance_path"))
            if key not in seen:
                seen.add(key)
                seeds.append({"point": point, "handle": entity.get("source_handle"), "layer": layer, "block_name": name, "source": "resolved"})
    return seeds


def _build_sanitary_zones(seeds: list[dict[str, Any]], rules: dict[str, Any]) -> list[dict[str, Any]]:
    if not seeds:
        return []
    enclosure = rules["sanitary_enclosure"]
    clusters = _cluster_points([seed["point"] for seed in seeds], enclosure["fixture_cluster_radius_mm"])
    zones = []
    for indices in clusters:
        points = [seeds[index]["point"] for index in indices]
        core = _sanitize_box(points)
        zones.append({
            "id": f"SANITARY-ZONE-{len(zones) + 1:03d}",
            "fixture_count": len(indices),
            "fixture_handles": [seeds[index]["handle"] for index in indices],
            "fixture_points": [[round(point[0], 3), round(point[1], 3)] for point in points],
            "fixture_bbox": core,
            "search_bbox": expand_bbox(core, enclosure["room_search_margin_mm"]),
        })
    return zones


def _parallel_pair(segment: Segment, candidates: list[Segment], rules: dict[str, Any]) -> bool:
    geometry = rules["geometry"]
    angle_tolerance = math.radians(geometry["parallel_angle_degrees"])
    for other in candidates:
        if other is segment or angle_difference(segment_angle(segment), segment_angle(other)) > angle_tolerance:
            continue
        spacing, overlap = segment_parallel_distance(segment, other)
        if geometry["parallel_wall_spacing_min_mm"] <= spacing <= geometry["parallel_wall_spacing_max_mm"] and overlap >= geometry["parallel_overlap_ratio_min"]:
            return True
    return False


def _sanitary_enclosure_audit(zone: dict[str, Any], segments: list[Segment], rules: dict[str, Any]) -> dict[str, Any]:
    search = zone["search_bbox"]
    core = zone["fixture_bbox"]
    enclosure = rules["sanitary_enclosure"]
    minimum_span = float(enclosure["minimum_audit_span_mm"])
    group_tolerance = float(enclosure["boundary_group_tolerance_mm"])
    center_x = (core["min"]["x"] + core["max"]["x"]) / 2
    center_y = (core["min"]["y"] + core["max"]["y"]) / 2
    half_width = max((core["max"]["x"] - core["min"]["x"]) / 2, minimum_span / 2)
    half_height = max((core["max"]["y"] - core["min"]["y"]) / 2, minimum_span / 2)
    spans = {
        "left": (center_y - half_height, center_y + half_height),
        "right": (center_y - half_height, center_y + half_height),
        "bottom": (center_x - half_width, center_x + half_width),
        "top": (center_x - half_width, center_x + half_width),
    }
    groups: dict[str, dict[int, list[tuple[float, float]]]] = {
        "left": {}, "right": {}, "bottom": {}, "top": {}
    }
    distances: dict[str, dict[int, float]] = {"left": {}, "right": {}, "bottom": {}, "top": {}}
    for segment in segments:
        angle = segment_angle(segment)
        vertical = abs(math.cos(angle)) < 0.15
        horizontal = abs(math.sin(angle)) < 0.15
        midpoint = segment.midpoint
        if not point_in_bbox(midpoint, search):
            continue
        tests = []
        if vertical and midpoint[0] <= center_x:
            tests.append(("left", midpoint[0], (segment.p1[1], segment.p2[1]), center_x - midpoint[0]))
        if vertical and midpoint[0] >= center_x:
            tests.append(("right", midpoint[0], (segment.p1[1], segment.p2[1]), midpoint[0] - center_x))
        if horizontal and midpoint[1] <= center_y:
            tests.append(("bottom", midpoint[1], (segment.p1[0], segment.p2[0]), center_y - midpoint[1]))
        if horizontal and midpoint[1] >= center_y:
            tests.append(("top", midpoint[1], (segment.p1[0], segment.p2[0]), midpoint[1] - center_y))
        for side, coordinate, interval, side_distance in tests:
            bucket = round(coordinate / group_tolerance)
            groups[side].setdefault(bucket, []).append(interval)
            distances[side][bucket] = min(side_distance, distances[side].get(bucket, float("inf")))

    coverage = {}
    nearest = {}
    for side, buckets in groups.items():
        start, end = spans[side]
        choices = []
        for bucket, intervals in buckets.items():
            shifted = [(first - start, second - start) for first, second in intervals]
            choices.append((union_interval_coverage(shifted, end - start), distances[side][bucket]))
        best = max(choices, default=(0.0, float("inf")), key=lambda item: (item[0], -item[1]))
        coverage[side] = best[0]
        nearest[side] = None if not math.isfinite(best[1]) else round(best[1], 3)
    threshold = enclosure["minimum_side_coverage_ratio"]
    covered_sides = sum(1 for value in coverage.values() if value >= threshold)
    return {
        "side_coverage": {key: round(value, 3) for key, value in coverage.items()},
        "nearest_boundary_distance_mm": nearest,
        "covered_sides": covered_sides,
        "required_sides": rules["sanitary_enclosure"]["minimum_enclosure_sides"],
        "status": "pass" if covered_sides >= rules["sanitary_enclosure"]["minimum_enclosure_sides"] else "review_required",
    }


def _classify_walls(
    model: list[dict[str, Any]],
    resolved: list[dict[str, Any]],
    columns: list[dict[str, Any]],
    rules: dict[str, Any],
    plan_domains: list[dict[str, Any]] | None = None,
) -> tuple[list[Segment], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    column_handles = {
        handle
        for column in columns
        for handle in column.get("source_handles", [column.get("source_handle")])
        if handle
    }
    primary: list[Segment] = []
    structural_entities = []
    structural_audit_segments: list[Segment] = []
    support_candidates: list[Segment] = []
    all_linear_support: list[Segment] = []
    excluded_terms = rules["layer_semantics"]["excluded_from_wall_recovery"]

    for entity in model:
        if entity.get("type") not in {"LINE", "LWPOLYLINE", "ARC"}:
            continue
        handle = entity.get("handle")
        layer = str(entity.get("layer") or "")
        if _is_ignored_fire_protection(layer, rules):
            continue
        segments = entity_segments(entity, world=False, max_curve_error_mm=rules["geometry"].get("max_curve_error_mm", 1.0))
        if handle in column_handles:
            continue
        if _is_layer(layer, "structural", rules):
            semantic_evidence = (
                "inherited_structural_container_semantics"
                if entity.get("semantic_container_handle")
                else "structural_layer_semantics"
            )
            structural_entities.append({
                "id": f"STRUCTURAL-WALL-{len(structural_entities) + 1:03d}",
                "class": "structural_wall",
                "source_handle": handle,
                "source_layer": layer,
                "source_layers": list(entity.get("source_layers") or [str(entity.get("raw_layer", layer) or "")]),
                "source_handles": [handle] if handle else [],
                "bbox": bbox(entity),
                "semantic_container_handle": entity.get("semantic_container_handle"),
                "evidence": [semantic_evidence, "column_rule_exclusion_passed"],
                "segments": [segment.as_dict() for segment in segments],
            })
            structural_audit_segments.extend(segments)
            continue
        if _is_layer(layer, "architectural_wall", rules) and not _is_layer(layer, "wall_finish", rules):
            for segment in segments:
                segment.evidence.append("architectural_wall_layer")
            primary.extend(segments)
            continue
        if _is_layer(layer, "wall_finish", rules):
            support_candidates.extend(segments)
        if not _matches(layer, excluded_terms):
            all_linear_support.extend(segments)

    sanitary_seeds = _find_sanitary_seeds(model, resolved, rules, plan_domains)
    sanitary_zones = _build_sanitary_zones(sanitary_seeds, rules)
    promoted: list[Segment] = []
    promotion_pool = support_candidates + [segment for segment in all_linear_support if segment.layer in {"0", "Defpoints", ""}]
    minimum_length = rules["sanitary_enclosure"]["minimum_promoted_segment_length_mm"]
    for segment in promotion_pool:
        if segment.length < minimum_length:
            continue
        zone_ids = [zone["id"] for zone in sanitary_zones if point_in_bbox(segment.midpoint, zone["search_bbox"])]
        if not zone_ids:
            continue
        has_pair = _parallel_pair(segment, promotion_pool, rules)
        finish_semantics = _is_layer(segment.layer, "wall_finish", rules)
        connects_primary = min((point_segment_distance(segment.p1, wall) for wall in primary), default=float("inf")) <= 450 or min((point_segment_distance(segment.p2, wall) for wall in primary), default=float("inf")) <= 450
        if finish_semantics or has_pair or connects_primary:
            copy = Segment(**{**segment.__dict__})
            copy.evidence = list(segment.evidence) + ["sanitary_fixture_proximity", f"sanitary_zones={','.join(zone_ids)}"]
            if finish_semantics:
                copy.evidence.append("wall_finish_promoted_only_inside_sanitary_zone")
            if has_pair:
                copy.evidence.append("parallel_wall_pair_geometry")
            if connects_primary:
                copy.evidence.append("connected_to_primary_wall_network")
            promoted.append(copy)

    merged_primary = merge_collinear(primary + promoted, rules["geometry"])
    audit = []
    for zone in sanitary_zones:
        zone_marker = f"sanitary_zones={zone['id']}"
        audit.append({
            **zone,
            "promoted_segment_count": sum(
                1 for segment in promoted if any(zone_marker in item for item in segment.evidence)
            ),
            **_sanitary_enclosure_audit(zone, merged_primary + structural_audit_segments, rules),
        })
    return merged_primary, structural_entities, promoted, audit


def _opening_component(
    entity: dict[str, Any],
    *,
    nested: bool,
    rules: dict[str, Any],
    plan_domains: list[dict[str, Any]],
) -> dict[str, Any] | None:
    box = bbox(entity)
    if not box:
        return None
    width, height = bbox_dims(box)
    opening_rules = rules["opening"]
    if max(width, height) < float(opening_rules["minimum_component_extent_mm"]):
        return None
    if max(width, height) > float(opening_rules["maximum_component_extent_mm"]):
        return None
    center = bbox_center(box)
    if center and plan_domains and not _point_in_any_bbox(center, plan_domains):
        return None

    source_handle = _resolved_handle(entity) if nested else str(entity.get("handle") or "")
    typ = str(entity.get("type") or "")
    world = nested
    segments = entity_segments(entity, world=world, max_curve_error_mm=rules["geometry"].get("max_curve_error_mm", 1.0))
    if segments:
        geometry_mode = "source_linear_geometry"
        segment_dicts = [segment.as_dict() for segment in segments]
    else:
        geometry_mode = "block_reference_extent" if typ in {"INSERT", "BLOCK_REFERENCE"} else "source_extent"
        segment_dicts = _box_segment_dicts(box, source_handle, str(entity.get("layer") or ""))
        for segment in segment_dicts:
            segment["evidence"] = ["extent_outline_for_opening_review"]

    provenance = "nested_object_layer_semantics" if nested else "top_level_object_layer_semantics"
    return {
        "class": "opening",
        "source_handle": source_handle,
        "raw_source_handle": entity.get("source_handle") if nested else entity.get("handle"),
        "source_layer": entity.get("layer"),
        "source_type": typ,
        "instance_path": entity.get("instance_path") if nested else None,
        "block_name": entity.get("block_name") or entity.get("source_block"),
        "bbox": box,
        "geometry_mode": geometry_mode,
        "evidence": ["opening_layer_semantics", provenance],
        "segments": segment_dicts,
    }


def _classify_openings(
    model: list[dict[str, Any]],
    resolved: list[dict[str, Any]],
    rules: dict[str, Any],
    plan_domains: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Classify WIN/window/door objects, including those nested in blocks."""
    domains = plan_domains or []
    supported = set(rules["opening"].get("supported_entity_types", []))
    output: list[dict[str, Any]] = []
    selected_paths: set[str] = set()
    seen: set[tuple[str, str, str]] = set()

    # Top-level opening block references define the outermost assembly. Their
    # resolved linework is evidence for that assembly and must not be counted
    # again as hundreds of separate openings.
    for entity in model:
        if (
            entity.get("type") != "INSERT"
            or not _is_layer(entity.get("layer"), "opening", rules)
            or _is_ignored_fire_protection(entity.get("layer"), rules)
        ):
            continue
        component = _opening_component(entity, nested=False, rules=rules, plan_domains=domains)
        if not component:
            continue
        output.append(component)
        path = str(entity.get("handle") or "")
        if path:
            selected_paths.add(path)
        seen.add((path, path, "INSERT"))

    # Prefer an outer opening BLOCK_REFERENCE over its own nested descendants.
    nested_references = sorted(
        (
            entity
            for entity in resolved
            if entity.get("type") == "BLOCK_REFERENCE"
            and str(entity.get("instance_path") or "")
            and _is_layer(entity.get("layer"), "opening", rules)
            and not _is_ignored_fire_protection(entity.get("layer"), rules)
        ),
        key=lambda item: (str(item.get("instance_path") or "").count("/"), str(item.get("instance_path") or "")),
    )
    for entity in nested_references:
        path = str(entity.get("instance_path") or "")
        if _path_is_within(path, selected_paths):
            continue
        component = _opening_component(entity, nested=True, rules=rules, plan_domains=domains)
        if not component:
            continue
        key = (path, str(entity.get("source_handle") or ""), "BLOCK_REFERENCE")
        if key in seen:
            continue
        seen.add(key)
        output.append(component)
        selected_paths.add(path)

    # Standalone opening-layer geometry is retained as a source object. This
    # covers drawings where windows are loose polylines rather than blocks.
    for entity in model:
        typ = str(entity.get("type") or "")
        if (
            typ == "INSERT"
            or typ not in supported
            or not _is_layer(entity.get("layer"), "opening", rules)
            or _is_ignored_fire_protection(entity.get("layer"), rules)
        ):
            continue
        key = ("", str(entity.get("handle") or ""), typ)
        if key in seen:
            continue
        component = _opening_component(entity, nested=False, rules=rules, plan_domains=domains)
        if component:
            seen.add(key)
            output.append(component)

    for entity in resolved:
        path = str(entity.get("instance_path") or "")
        typ = str(entity.get("type") or "")
        if (
            not path
            or typ not in supported
            or not _is_layer(entity.get("layer"), "opening", rules)
            or _is_ignored_fire_protection(entity.get("layer"), rules)
            or _path_is_within(path, selected_paths)
        ):
            continue
        key = (path, str(entity.get("source_handle") or ""), typ)
        if key in seen:
            continue
        component = _opening_component(entity, nested=True, rules=rules, plan_domains=domains)
        if component:
            seen.add(key)
            output.append(component)

    for index, component in enumerate(output, 1):
        component["id"] = f"OPENING-{index:04d}"
    return output


def _structural_component_segments(components: list[dict[str, Any]]) -> list[Segment]:
    output: list[Segment] = []
    for component in components:
        for item in component.get("segments", []):
            start, end = item.get("start"), item.get("end")
            if not start or not end:
                continue
            output.append(
                Segment(
                    (float(start[0]), float(start[1])),
                    (float(end[0]), float(end[1])),
                    source_handle=item.get("source_handle") or component.get("source_handle"),
                    layer=str(item.get("source_layer") or component.get("source_layer") or ""),
                    source_layers=list(item.get("source_layers") or component.get("source_layers") or []),
                    source_handles=list(item.get("source_handles") or component.get("source_handles") or []),
                    evidence=list(item.get("evidence") or []),
                )
            )
    return output


def _annotate_opening_adjacency(
    openings: list[dict[str, Any]], wall_segments: list[Segment], rules: dict[str, Any]
) -> None:
    threshold = float(rules["opening"]["wall_adjacency_mm"])
    wall_boxes = [_segment_bbox(segment) for segment in wall_segments]
    for opening in openings:
        distances = [_bbox_gap(opening["bbox"], wall_box) for wall_box in wall_boxes]
        nearest = min(distances, default=float("inf"))
        matches = sum(1 for value in distances if value <= threshold)
        opening["nearest_wall_distance_mm"] = None if not math.isfinite(nearest) else round(nearest, 3)
        opening["adjacent_wall_segment_count"] = matches
        opening["wall_adjacency_status"] = "adjacent" if nearest <= threshold else "unmatched"
        if nearest <= threshold:
            opening["evidence"].append("adjacent_to_wall_network")


def _point_near_opening(point: tuple[float, float], opening: dict[str, Any], tolerance: float) -> bool:
    if opening.get("geometry_mode") == "source_linear_geometry":
        for item in opening.get("segments", []):
            start, end = item.get("start"), item.get("end")
            if not start or not end:
                continue
            segment = Segment((float(start[0]), float(start[1])), (float(end[0]), float(end[1])))
            if point_segment_distance(point, segment) <= tolerance:
                return True
        return False
    return point_in_bbox(point, expand_bbox(opening["bbox"], tolerance))


def _near_plan_perimeter(
    point: tuple[float, float], plan_domains: list[dict[str, Any]], tolerance: float
) -> bool:
    for domain in plan_domains:
        if not point_in_bbox(point, expand_bbox(domain, tolerance)):
            continue
        distances = (
            abs(point[0] - float(domain["min"]["x"])),
            abs(point[0] - float(domain["max"]["x"])),
            abs(point[1] - float(domain["min"]["y"])),
            abs(point[1] - float(domain["max"]["y"])),
        )
        if min(distances) <= tolerance:
            return True
    return False


def _wall_continuity_audit(
    architectural: list[Segment],
    structural: list[dict[str, Any]],
    columns: list[dict[str, Any]],
    openings: list[dict[str, Any]],
    plan_domains: list[dict[str, Any]],
    rules: dict[str, Any],
) -> dict[str, Any]:
    segments = architectural + _structural_component_segments(structural)
    continuity = rules["wall_continuity"]
    connection_tolerance = float(continuity["connection_tolerance_mm"])
    opening_tolerance = float(continuity["opening_endpoint_tolerance_mm"])
    column_tolerance = float(continuity["column_endpoint_tolerance_mm"])
    perimeter_tolerance = float(continuity["plan_perimeter_tolerance_mm"])
    interface_tolerance = float(continuity["wall_interface_tolerance_mm"])
    counts = {
        "connected": 0,
        "opening_terminated": 0,
        "column_terminated": 0,
        "plan_perimeter_terminated": 0,
        "wall_interface_terminated": 0,
        "unresolved": 0,
    }
    examples = []
    for index, segment in enumerate(segments):
        for point in (segment.p1, segment.p2):
            connected = any(
                other_index != index and point_segment_distance(point, other) <= connection_tolerance
                for other_index, other in enumerate(segments)
            )
            if connected:
                counts["connected"] += 1
                continue
            if any(_point_near_opening(point, opening, opening_tolerance) for opening in openings):
                counts["opening_terminated"] += 1
                continue
            if any(point_in_bbox(point, expand_bbox(column["bbox"], column_tolerance)) for column in columns):
                counts["column_terminated"] += 1
                continue
            if _near_plan_perimeter(point, plan_domains, perimeter_tolerance):
                counts["plan_perimeter_terminated"] += 1
                continue
            if any(
                other_index != index and point_segment_distance(point, other) <= interface_tolerance
                for other_index, other in enumerate(segments)
            ):
                counts["wall_interface_terminated"] += 1
                continue
            counts["unresolved"] += 1
            if len(examples) < int(continuity.get("maximum_unresolved_examples", 40)):
                examples.append(
                    {
                        "point": [round(point[0], 3), round(point[1], 3)],
                        "source_handle": segment.source_handle,
                        "source_layer": segment.layer,
                    }
                )
    return {
        "endpoint_records_total": sum(counts.values()),
        **counts,
        "opening_objects_considered": len(openings),
        "unresolved_examples": examples,
    }


def _descendants_for_path(resolved: list[dict[str, Any]], path: str) -> list[dict[str, Any]]:
    prefix = path + "/"
    return [entity for entity in resolved if str(entity.get("instance_path") or "") == path or str(entity.get("instance_path") or "").startswith(prefix)]


def _curtain_envelopes(model: list[dict[str, Any]], resolved: list[dict[str, Any]], rules: dict[str, Any]) -> list[dict[str, Any]]:
    curtain_rules = rules["curtain_envelope"]
    candidates = []
    for entity in model:
        if entity.get("type") != "INSERT" or not bbox(entity):
            continue
        width, height = bbox_dims(bbox(entity))
        if width < curtain_rules["minimum_container_width_mm"] or height < curtain_rules["minimum_container_height_mm"]:
            continue
        path = str(entity.get("handle") or "")
        descendants = _descendants_for_path(resolved, path)
        named = [child for child in descendants if child.get("type") == "BLOCK_REFERENCE" and _curtain_name(child.get("block_name"), rules)]
        score = 0.0
        evidence = []
        if _is_layer(entity.get("layer"), "curtain", rules):
            score += 2.0
            evidence.append("container_on_curtain_or_window_layer")
        if len(named) >= curtain_rules["minimum_named_descendants"]:
            score += 2.0
            evidence.append(f"curtain_named_descendants={len(named)}")
        if len(named) >= curtain_rules["strong_named_descendants"]:
            score += 1.0
            evidence.append("strong_repeated_mullion_or_cw_evidence")

        box = bbox(entity)
        band = max(float(curtain_rules["perimeter_band_mm"]), min(width, height) * 0.008)
        side_intervals = {"left": [], "right": [], "bottom": [], "top": []}
        for child in descendants:
            if child.get("type") not in {"LINE", "LWPOLYLINE", "ARC"}:
                continue
            for segment in entity_segments(child, world=True, max_curve_error_mm=rules["geometry"].get("max_curve_error_mm", 1.0)):
                angle = segment_angle(segment)
                vertical = abs(math.cos(angle)) < 0.12
                horizontal = abs(math.sin(angle)) < 0.12
                midpoint = segment.midpoint
                if vertical and abs(midpoint[0] - box["min"]["x"]) <= band:
                    side_intervals["left"].append((segment.p1[1] - box["min"]["y"], segment.p2[1] - box["min"]["y"]))
                if vertical and abs(midpoint[0] - box["max"]["x"]) <= band:
                    side_intervals["right"].append((segment.p1[1] - box["min"]["y"], segment.p2[1] - box["min"]["y"]))
                if horizontal and abs(midpoint[1] - box["min"]["y"]) <= band:
                    side_intervals["bottom"].append((segment.p1[0] - box["min"]["x"], segment.p2[0] - box["min"]["x"]))
                if horizontal and abs(midpoint[1] - box["max"]["y"]) <= band:
                    side_intervals["top"].append((segment.p1[0] - box["min"]["x"], segment.p2[0] - box["min"]["x"]))
        coverage = {
            "left": union_interval_coverage(side_intervals["left"], height),
            "right": union_interval_coverage(side_intervals["right"], height),
            "bottom": union_interval_coverage(side_intervals["bottom"], width),
            "top": union_interval_coverage(side_intervals["top"], width),
        }
        sides_with_evidence = sum(1 for value in coverage.values() if value >= curtain_rules["minimum_side_coverage_ratio"])
        if sides_with_evidence >= 2:
            score += 1.0
            evidence.append(f"perimeter_geometry_sides={sides_with_evidence}")
        if score < curtain_rules["minimum_score"]:
            continue

        x0, y0, x1, y1 = box["min"]["x"], box["min"]["y"], box["max"]["x"], box["max"]["y"]
        envelope = [
            Segment((x0, y0), (x1, y0), source_handle=entity.get("handle"), layer=str(entity.get("layer") or ""), evidence=["full_connected_envelope_propagation"]),
            Segment((x1, y0), (x1, y1), source_handle=entity.get("handle"), layer=str(entity.get("layer") or ""), evidence=["full_connected_envelope_propagation"]),
            Segment((x1, y1), (x0, y1), source_handle=entity.get("handle"), layer=str(entity.get("layer") or ""), evidence=["full_connected_envelope_propagation"]),
            Segment((x0, y1), (x0, y0), source_handle=entity.get("handle"), layer=str(entity.get("layer") or ""), evidence=["full_connected_envelope_propagation"]),
        ]
        candidates.append({
            "id": f"CURTAIN-ENVELOPE-{len(candidates) + 1:03d}",
            "class": "curtain_wall",
            "source_handle": entity.get("handle"),
            "source_layer": entity.get("layer"),
            "source_layers": list(entity.get("source_layers") or [str(entity.get("raw_layer", entity.get("layer")) or "")]),
            "block_name": entity.get("block_name"),
            "bbox": box,
            "score": round(score, 3),
            "named_descendant_count": len(named),
            "perimeter_side_coverage": {key: round(value, 3) for key, value in coverage.items()},
            "evidence": evidence + ["entire_exterior_envelope_inherits_curtain_class"],
            "geometry_mode": "dominant_container_closed_envelope",
            "segments": [segment.as_dict() for segment in envelope],
        })
    candidates.sort(key=lambda item: bbox_area(item["bbox"]), reverse=True)
    return candidates[:1]


def _large_opening_layer_systems(
    model: list[dict[str, Any]],
    existing_curtains: list[dict[str, Any]],
    rules: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Route building-scale WIN geometry to curtains or an explicit user decision.

    A size guard is still useful for ordinary openings, but a matched semantic
    layer must never disappear silently. Three or more near-coincident large
    polylines are strong curtain-system evidence; every remaining oversized
    object becomes a review request.
    """
    opening_rules = rules["opening"]
    curtain_rules = rules["curtain_envelope"]
    maximum_opening = float(opening_rules["maximum_component_extent_mm"])
    minimum_width = float(curtain_rules["minimum_container_width_mm"])
    minimum_height = float(curtain_rules["minimum_container_height_mm"])
    bbox_tolerance = float(curtain_rules.get("parallel_outline_bbox_tolerance_mm", 250.0))
    length_ratio_min = float(curtain_rules.get("parallel_outline_length_ratio_min", 0.95))
    minimum_vertices = int(curtain_rules.get("minimum_outline_vertices", 4))
    minimum_count = int(curtain_rules.get("minimum_parallel_outline_count", 3))
    overlap_suppression = float(curtain_rules.get("block_overlap_suppression_ratio", 0.85))

    oversized: list[dict[str, Any]] = []
    for entity in model:
        box = bbox(entity)
        if (
            not box
            or not _is_layer(entity.get("layer"), "opening", rules)
            or _is_ignored_fire_protection(entity.get("layer"), rules)
        ):
            continue
        width, height = bbox_dims(box)
        if max(width, height) <= maximum_opening:
            continue
        oversized.append(entity)

    recognized_handles: set[str] = set()
    systems: list[dict[str, Any]] = []
    if curtain_rules.get("recognize_oversized_opening_layer_polylines", True):
        eligible = [
            entity
            for entity in oversized
            if entity.get("type") == "LWPOLYLINE"
            and len(entity.get("vertices") or []) >= minimum_vertices
            and bbox_dims(bbox(entity))[0] >= minimum_width
            and bbox_dims(bbox(entity))[1] >= minimum_height
            and entity_segments(entity, world=False, max_curve_error_mm=rules["geometry"].get("max_curve_error_mm", 1.0))
        ]
        groups: list[list[dict[str, Any]]] = []
        for entity in sorted(eligible, key=lambda item: str(item.get("handle") or "")):
            entity_box = bbox(entity)
            entity_length = sum(segment.length for segment in entity_segments(entity, world=False, max_curve_error_mm=rules["geometry"].get("max_curve_error_mm", 1.0)))
            matching_group = None
            for group in groups:
                compatible = True
                for member in group:
                    if str(member.get("layer") or "").casefold() != str(entity.get("layer") or "").casefold():
                        compatible = False
                        break
                    if _bbox_edge_delta(entity_box, bbox(member)) > bbox_tolerance:
                        compatible = False
                        break
                    member_length = sum(segment.length for segment in entity_segments(member, world=False, max_curve_error_mm=rules["geometry"].get("max_curve_error_mm", 1.0)))
                    if min(entity_length, member_length) / max(entity_length, member_length) < length_ratio_min:
                        compatible = False
                        break
                if compatible:
                    matching_group = group
                    break
            if matching_group is None:
                groups.append([entity])
            else:
                matching_group.append(entity)

        for group in groups:
            if len(group) < minimum_count:
                continue
            group_box = bbox(group[0])
            for entity in group[1:]:
                group_box = _bbox_union(group_box, bbox(entity))
            if any(_bbox_intersection_ratio(group_box, item["bbox"]) >= overlap_suppression for item in existing_curtains):
                recognized_handles.update(str(entity.get("handle") or "") for entity in group)
                continue
            source_handles = [str(entity.get("handle") or "") for entity in group]
            source_layers = sorted({str(entity.get("layer") or "") for entity in group})
            segments: list[dict[str, Any]] = []
            for entity in group:
                for segment in entity_segments(entity, world=False, max_curve_error_mm=rules["geometry"].get("max_curve_error_mm", 1.0)):
                    segment.evidence.extend([
                        "oversized_opening_layer_geometry",
                        "parallel_near_coincident_outline_group",
                    ])
                    segments.append(segment.as_dict())
            systems.append({
                "id": f"CURTAIN-SYSTEM-{len(systems) + 1:03d}",
                "class": "curtain_wall",
                "source_handle": "+".join(source_handles),
                "source_handles": source_handles,
                "source_layer": "+".join(source_layers),
                "source_layers": source_layers,
                "bbox": group_box,
                "score": 5.0,
                "parallel_outline_count": len(group),
                "evidence": [
                    "opening_layer_semantics",
                    "building_scale_extent",
                    f"parallel_outline_count={len(group)}",
                    f"bbox_edge_tolerance_mm={bbox_tolerance:g}",
                    "auto_classified_as_curtain_system",
                ],
                "geometry_mode": "parallel_oversized_window_layer_polylines",
                "segments": segments,
            })
            recognized_handles.update(source_handles)

    # Adjacent outline groups on the same semantic layer are parts of one
    # perimeter system (for example, two three-line runs meeting at a corner).
    merged_systems: list[dict[str, Any]] = []
    for system in systems:
        target = next(
            (
                item
                for item in merged_systems
                if item["source_layer"].casefold() == system["source_layer"].casefold()
                and _bbox_gap(item["bbox"], system["bbox"]) <= bbox_tolerance
            ),
            None,
        )
        if target is None:
            merged_systems.append(system)
            continue
        target["bbox"] = _bbox_union(target["bbox"], system["bbox"])
        target["source_handles"].extend(system["source_handles"])
        target["source_handle"] = "+".join(target["source_handles"])
        target["parallel_outline_count"] += system["parallel_outline_count"]
        target["segments"].extend(system["segments"])
        target["evidence"].append("connected_large_window_outline_groups_merged")
    for index, system in enumerate(merged_systems, 1):
        system["id"] = f"CURTAIN-SYSTEM-{index:03d}"
        system["evidence"].append(f"source_object_count={len(system['source_handles'])}")
    systems = merged_systems

    undecided = []
    for entity in oversized:
        handle = str(entity.get("handle") or "")
        entity_box = bbox(entity)
        if handle in recognized_handles:
            continue
        if any(_bbox_intersection_ratio(entity_box, item["bbox"]) >= overlap_suppression for item in existing_curtains):
            continue
        undecided.append(entity)

    review_groups: list[list[dict[str, Any]]] = []
    for entity in sorted(undecided, key=lambda item: str(item.get("handle") or "")):
        target = next(
            (
                group
                for group in review_groups
                if str(group[0].get("layer") or "").casefold() == str(entity.get("layer") or "").casefold()
                and any(_bbox_gap(bbox(member), bbox(entity)) <= bbox_tolerance for member in group)
            ),
            None,
        )
        if target is None:
            review_groups.append([entity])
        else:
            target.append(entity)

    review_requests = []
    for group in review_groups:
        group_box = bbox(group[0])
        for entity in group[1:]:
            group_box = _bbox_union(group_box, bbox(entity))
        source_handles = [str(entity.get("handle") or "") for entity in group]
        source_layers = sorted({str(entity.get("layer") or "") for entity in group})
        source_types = sorted({str(entity.get("type") or "") for entity in group})
        width, height = bbox_dims(group_box)
        review_requests.append({
            "id": f"SEMANTIC-DECISION-{len(review_requests) + 1:03d}",
            "status": "user_confirmation_required",
            "source_handle": "+".join(source_handles),
            "source_handles": source_handles,
            "source_layer": "+".join(source_layers),
            "source_layers": source_layers,
            "source_type": "+".join(source_types),
            "source_object_count": len(group),
            "bbox": group_box,
            "width_mm": round(width, 3),
            "height_mm": round(height, 3),
            "reason": "opening_layer_object_exceeds_ordinary_opening_extent",
            "question": "此建築尺度的 WIN／窗物件應歸類為帷幕、特殊大型開口、參考線，或忽略？",
            "options": ["curtain_wall_system", "large_opening", "reference_geometry", "ignore"],
        })
    return systems, review_requests


def _segment_distance(first: Segment, second: Segment) -> float:
    if _bbox_gap(_segment_bbox(first), _segment_bbox(second)) <= 1e-9:
        return 0.0
    return min(
        point_segment_distance(first.p1, second),
        point_segment_distance(first.p2, second),
        point_segment_distance(second.p1, first),
        point_segment_distance(second.p2, first),
    )


def _curtain_accessories(
    model: list[dict[str, Any]],
    resolved: list[dict[str, Any]],
    curtains: list[dict[str, Any]],
    rules: dict[str, Any],
) -> list[dict[str, Any]]:
    """Attach section/elevation/mullion objects to an established curtain path.

    Accessory layer names are not trusted globally. They inherit curtain
    semantics only when their source geometry is spatially attached to a
    curtain component already established from stronger outline evidence.
    """
    if not curtains:
        return []
    curtain_rules = rules["curtain_envelope"]
    attachment = float(curtain_rules.get("accessory_attachment_mm", 400.0))
    minimum_coverage = float(curtain_rules.get("minimum_accessory_linear_coverage_ratio", 0.55))
    supported = set(curtain_rules.get("accessory_supported_entity_types", ["LINE", "LWPOLYLINE", "INSERT"]))
    curtain_segments: list[tuple[dict[str, Any], list[Segment]]] = []
    for curtain in curtains:
        segments = []
        for item in curtain.get("segments", []):
            start, end = item.get("start"), item.get("end")
            if not start or not end:
                continue
            segments.append(Segment((float(start[0]), float(start[1])), (float(end[0]), float(end[1]))))
        curtain_segments.append((curtain, segments))

    output = []
    for entity in model:
        typ = str(entity.get("type") or "")
        box = bbox(entity)
        if (
            typ not in supported
            or not box
            or not _is_layer(entity.get("layer"), "curtain_accessory", rules)
            or _is_ignored_fire_protection(entity.get("layer"), rules)
        ):
            continue
        source_segments = entity_segments(entity, world=False, max_curve_error_mm=rules["geometry"].get("max_curve_error_mm", 1.0))
        nearest: tuple[float, dict[str, Any]] | None = None
        for curtain, segments in curtain_segments:
            if not segments:
                continue
            if source_segments:
                distance_to_curtain = min(
                    _segment_distance(source, target)
                    for source in source_segments
                    for target in segments
                )
            else:
                distance_to_curtain = min(_bbox_gap(box, _segment_bbox(target)) for target in segments)
            candidate = (distance_to_curtain, curtain)
            if nearest is None or candidate[0] < nearest[0]:
                nearest = candidate
        if nearest is None or nearest[0] > attachment:
            continue

        handle = str(entity.get("handle") or "")
        geometry_segments = list(source_segments)
        geometry_mode = "source_linear_geometry"
        descendant_count = 0
        if typ == "INSERT":
            geometry_segments = []
            for child in _descendants_for_path(resolved, handle):
                if child.get("type") not in {"LINE", "LWPOLYLINE", "ARC"}:
                    continue
                child_segments = entity_segments(child, world=True, max_curve_error_mm=rules["geometry"].get("max_curve_error_mm", 1.0))
                if child_segments:
                    descendant_count += 1
                    geometry_segments.extend(child_segments)
            geometry_mode = "resolved_insert_geometry" if geometry_segments else "insert_extent"
        target_segments = next(
            segments for curtain, segments in curtain_segments if curtain["id"] == nearest[1]["id"]
        )
        total_length = sum(segment.length for segment in geometry_segments)
        attached_length = sum(
            segment.length
            for segment in geometry_segments
            if min(_segment_distance(segment, target) for target in target_segments) <= attachment
        )
        attachment_coverage = attached_length / total_length if total_length else 1.0
        if geometry_segments and attachment_coverage < minimum_coverage:
            continue
        segment_dicts = [segment.as_dict() for segment in geometry_segments]
        if not segment_dicts:
            segment_dicts = _box_segment_dicts(box, handle, str(entity.get("layer") or ""))

        component = {
            "id": f"CURTAIN-ACCESSORY-{len(output) + 1:04d}",
            "class": "curtain_accessory",
            "curtain_id": nearest[1]["id"],
            "source_handle": handle,
            "source_layer": entity.get("layer"),
            "source_layers": list(entity.get("source_layers") or [str(entity.get("raw_layer", entity.get("layer")) or "")]),
            "source_type": typ,
            "block_name": entity.get("block_name"),
            "bbox": box,
            "nearest_curtain_distance_mm": round(nearest[0], 3),
            "curtain_attachment_coverage_ratio": round(attachment_coverage, 4),
            "resolved_linear_descendant_count": descendant_count,
            "geometry_mode": geometry_mode,
            "evidence": [
                "curtain_accessory_layer_semantics",
                "spatially_attached_to_confirmed_curtain_path",
                f"attachment_distance_mm={nearest[0]:.3f}",
                f"attachment_coverage_ratio={attachment_coverage:.4f}",
            ],
            "segments": segment_dicts,
        }
        output.append(component)

    by_curtain = {curtain["id"]: curtain for curtain in curtains}
    for curtain in curtains:
        curtain["accessory_ids"] = []
        curtain["accessory_layers"] = []
        curtain["accessory_object_count"] = 0
    for component in output:
        curtain = by_curtain[component["curtain_id"]]
        curtain["accessory_ids"].append(component["id"])
        curtain["accessory_object_count"] += 1
        layer = str(component.get("source_layer") or "")
        if layer and layer not in curtain["accessory_layers"]:
            curtain["accessory_layers"].append(layer)
    return output


def _nested_curtain_accessory_candidates(
    resolved: list[dict[str, Any]], accessories: list[dict[str, Any]], rules: dict[str, Any]
) -> list[dict[str, Any]]:
    """Retain explicit nested metal geometry without approving its use/height.

    A parent INSERT on an unrelated layer must not hide a leaf's explicit
    original metal layer. Such a leaf may be a balustrade support, rather than
    an accessory attached to an already recognized exterior curtain network.
    Existing classified parent geometry is excluded to avoid double counting.
    """
    represented = set()
    for accessory in accessories:
        for segment in accessory.get("segments", []):
            represented.update(segment.get("source_handles") or [segment.get("source_handle")])
        represented.update(accessory.get("source_handles") or [accessory.get("source_handle")])
    output, seen = [], set()
    for entity in sorted(resolved, key=lambda item: (str(item.get("source_id") or item.get("source_file") or ""), str(item.get("instance_path") or ""), str(item.get("source_handle") or item.get("handle") or ""))):
        original = str(entity.get("original_layer", entity.get("raw_layer", entity.get("layer"))) or "")
        path = str(entity.get("instance_path") or "")
        if (not path or entity.get("type") not in {"LINE", "LWPOLYLINE", "ARC"}
                or original in {"0", "Layer0", "Untagged"}
                or not _is_layer(original, "curtain_accessory", rules)
                or _is_ignored_fire_protection(original, rules)):
            continue
        handle = _resolved_handle(entity)
        identity_parts = [entity.get("source_id") or entity.get("source_file") or "", path, entity.get("source_handle") or entity.get("handle") or ""]
        identity = str(entity.get("entity_id") or "entity:" + hashlib.sha256(json.dumps(identity_parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()[:24])
        if identity in seen or handle in represented:
            continue
        segments = entity_segments(entity, world=True, max_curve_error_mm=rules["geometry"].get("max_curve_error_mm", 1.0))
        if not segments:
            continue
        seen.add(identity)
        vertices = [segments[0].p1] + [segment.p2 for segment in segments]
        xs, ys = [point[0] for point in vertices], [point[1] for point in vertices]
        box = {"min": {"x": min(xs), "y": min(ys)}, "max": {"x": max(xs), "y": max(ys)}}
        candidate = {"id": "NESTED-METAL-CANDIDATE:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20], "class": "unresolved_curtain_accessory", "status": "user_confirmation_required", "source_entity_ids": [identity], "source_handle": handle, "source_handles": [handle], "raw_source_handle": entity.get("source_handle") or entity.get("handle"), "source_layer": original, "source_layers": [original], "original_layer": original, "effective_layer": entity.get("effective_layer", entity.get("layer")), "block_layer": entity.get("block_layer"), "instance_path": path, "source_type": entity["type"], "bbox": box, "source_closed": entity.get("closed"), "geometry_mode": "resolved_world_source_geometry_pending_semantics", "segments": [segment.as_dict() for segment in segments], "category": None, "height_mm": None, "base_z_mm": None, "possible_roles": ["balustrade_metal_profile", "curtain_metal_profile", "reference_geometry"], "evidence": ["explicit_nested_original_metal_layer", "resolved_world_geometry_preserved", "not_represented_by_a_classified_parent_accessory", "parent_layer_does_not_override_explicit_leaf_layer", "use_and_vertical_extent_require_confirmation"]}
        output.append(candidate)
    return output


def _fallback_plan_domains(model: list[dict[str, Any]], rules: dict[str, Any]) -> list[dict[str, Any]]:
    """Infer a conservative plan extent when no curtain container is present."""
    boxes = [
        bbox(entity)
        for entity in model
        if entity.get("type") in {"LINE", "LWPOLYLINE", "ARC"}
        and _is_layer(entity.get("layer"), "architectural_wall", rules)
        and not _is_layer(entity.get("layer"), "wall_finish", rules)
        and bbox(entity)
    ]
    if not boxes:
        return []
    domain = boxes[0]
    for box in boxes[1:]:
        domain = _bbox_union(domain, box)
    minimum_span = float(rules.get("plan_domain", {}).get("minimum_span_mm", 5000.0))
    width, height = bbox_dims(domain)
    return [domain] if width >= minimum_span and height >= minimum_span else []


def _source_coverage(
    model: list[dict[str, Any]], resolved: list[dict[str, Any]], result: dict[str, Any], rules: dict[str, Any]
) -> dict[str, Any]:
    """Account for every input instance without treating layer names as coverage."""
    represented: set[str] = set()
    pending: set[str] = set()

    def collect(value: Any, target: set[str]) -> None:
        if isinstance(value, dict):
            target.update(str(handle) for handle in value.get("source_handles", []) if handle)
            if value.get("source_handle"):
                target.add(str(value["source_handle"]))
            for key, child in value.items():
                if key not in {"source_handles", "source_handle"}:
                    collect(child, target)
        elif isinstance(value, list):
            for child in value:
                collect(child, target)

    collect(result["components"], represented)
    collect(result["audits"]["semantic_decision_requests"], pending)
    collect(result["audits"].get("nested_accessory_decision_requests", []), pending)
    reference_terms = rules["layer_semantics"]["excluded_from_wall_recovery"]
    annotation_types = {"TEXT", "MTEXT", "DIMENSION", "HATCH", "WIPEOUT", "ATTRIB", "ATTDEF", "POINT", "MLEADER", "MULTILEADER"}
    native_geometry_types = {"LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE", "INSERT", "BLOCK_REFERENCE"}
    records = []
    for kind, entities in (("model", model), ("resolved", resolved)):
        for index, entity in enumerate(entities):
            handle = str(entity.get("handle") or "") if kind == "model" else _resolved_handle(entity)
            layer = str(entity.get("layer") or "")
            typ = str(entity.get("type") or "UNKNOWN")
            path = str(entity.get("instance_path") or "")
            sampled = bool(entity.get("curve_points_world") or entity.get("curve_points"))
            unsupported = typ not in native_geometry_types | annotation_types and not sampled
            if _is_ignored_fire_protection(layer, rules):
                disposition = "explicitly_excluded"
            elif handle and handle in pending:
                disposition = "pending_review"
            elif handle and handle in represented:
                disposition = "classified"
            elif kind == "resolved" and _path_is_within(path, represented):
                disposition = "container_reference"
            elif typ in {"INSERT", "BLOCK_REFERENCE"} and handle and any(
                item.startswith(handle + "/") for item in represented | pending
            ):
                disposition = "container_reference"
            elif typ in annotation_types or (
                _matches(layer, reference_terms)
                and not any(_is_layer(layer, category, rules) for category in ("opening", "architectural_wall", "structural"))
            ):
                disposition = "reference_only"
            else:
                disposition = "unclassified"
            records.append({
                "source_id": entity.get("source_id"),
                "entity_id": entity.get("entity_id") or f"{kind}:{handle or index}",
                "source_handle": handle or None,
                "raw_handle": entity.get("source_handle") or entity.get("handle"),
                "instance_path": path or None,
                "source_layer": str(entity.get("original_layer", entity.get("raw_layer", layer)) or ""),
                "effective_layer": layer,
                "source_type": typ,
                "disposition": disposition,
                "unsupported_geometry": unsupported,
            })
    unknown = [item for item in records if item["disposition"] == "unclassified"]
    unsupported = [item for item in records if item["unsupported_geometry"] and item["disposition"] != "explicitly_excluded"]
    return {
        "input_entities": len(records),
        "classified_entities": sum(item["disposition"] == "classified" for item in records),
        "unclassified_entities": len(unknown),
        "unsupported_geometry_entities": len(unsupported),
        "unsupported_entity_types": sorted({item["source_type"] for item in unsupported}),
        "unclassified": unknown,
        "unsupported": unsupported,
        "records": records,
    }


def _add_exact_provenance(value: Any) -> None:
    """Fill singleton arrays; merged objects must already carry their exact list."""
    if isinstance(value, dict):
        if "source_layer" in value:
            value.setdefault("source_layers", [str(value["source_layer"])] if value["source_layer"] is not None else [])
        if "source_handle" in value:
            value.setdefault("source_handles", [str(value["source_handle"])] if value["source_handle"] else [])
        for child in value.values():
            _add_exact_provenance(child)
    elif isinstance(value, list):
        for child in value:
            _add_exact_provenance(child)


def _validate(result: dict[str, Any]) -> dict[str, Any]:
    failures = []
    warnings = []
    column_handles = {
        handle
        for item in result["components"]["columns"]
        for handle in item.get("source_handles", [item.get("source_handle")])
        if handle
    }
    structural_handles = {item["source_handle"] for item in result["components"]["structural_walls"]}
    wall_handles = {
        handle
        for item in result["components"]["architectural_walls"]
        for handle in item.get("source_handles", [item.get("source_handle")])
        if handle
    }
    opening_handles = {
        item.get("source_handle")
        for item in result["components"].get("openings", [])
        if item.get("source_handle")
    }
    curtain_ids = {item.get("id") for item in result["components"].get("curtain_envelopes", [])}
    accessory_handles = {
        item.get("source_handle")
        for item in result["components"].get("curtain_accessories", [])
        if item.get("source_handle")
    }
    for accessory in result["components"].get("curtain_accessories", []):
        if accessory.get("curtain_id") not in curtain_ids:
            failures.append({
                "rule": "curtain_accessory_parent",
                "component": accessory.get("id"),
                "curtain_id": accessory.get("curtain_id"),
            })
    ignored_fire_handles = {
        item.get("source_handle")
        for item in result.get("audits", {}).get("ignored_fire_protection_objects", [])
        if item.get("source_handle")
    }
    fire_overlap = ignored_fire_handles & (opening_handles | accessory_handles)
    if fire_overlap:
        failures.append({"rule": "ignored_fire_protection_exclusivity", "handles": sorted(fire_overlap)})
    overlap = column_handles & (structural_handles | wall_handles)
    if overlap:
        failures.append({"rule": "column_exclusivity", "handles": sorted(overlap)})
    if structural_handles & wall_handles:
        failures.append({"rule": "structural_architectural_wall_exclusivity", "handles": sorted(structural_handles & wall_handles)})
    if opening_handles & (structural_handles | wall_handles):
        failures.append({
            "rule": "opening_wall_exclusivity",
            "handles": sorted(opening_handles & (structural_handles | wall_handles)),
        })
    for audit in result["audits"]["sanitary_enclosures"]:
        if audit["status"] != "pass":
            warnings.append({"rule": "sanitary_enclosure_coverage", "zone": audit["id"], "covered_sides": audit["covered_sides"]})
    for envelope in result["components"]["curtain_envelopes"]:
        segments = envelope["segments"]
        mode = envelope.get("geometry_mode")
        if mode == "dominant_container_closed_envelope" and (
            len(segments) < 4 or segments[0]["start"] != segments[-1]["end"]
        ):
            failures.append({"rule": "curtain_envelope_closed", "component": envelope["id"]})
        if mode == "parallel_oversized_window_layer_polylines" and envelope.get("parallel_outline_count", 0) < 3:
            failures.append({"rule": "curtain_parallel_outline_evidence", "component": envelope["id"]})
    if not result["components"]["columns"]:
        warnings.append({"rule": "column_presence", "message": "No independent column passed the reusable geometry score."})
    unresolved = int(result.get("audits", {}).get("wall_continuity", {}).get("unresolved", 0))
    if unresolved:
        warnings.append({
            "rule": "wall_endpoint_continuity",
            "message": "Wall endpoints remain without a wall, opening, or plan-perimeter explanation.",
            "unresolved": unresolved,
        })
    decision_requests = result.get("audits", {}).get("semantic_decision_requests", [])
    if decision_requests:
        warnings.append({
            "rule": "special_dimension_user_confirmation",
            "message": "Oversized semantic objects require a user decision and were not silently discarded.",
            "request_ids": [item["id"] for item in decision_requests],
        })
    nested_requests = result.get("audits", {}).get("nested_accessory_decision_requests", [])
    if nested_requests:
        warnings.append({"rule": "nested_metal_accessory_user_confirmation", "message": "Explicit nested metal profiles require their own semantic and vertical-extent decisions; parent layers cannot silently discard them.", "request_ids": [item["id"] for item in nested_requests]})
    coverage = result.get("audits", {}).get("source_coverage", {})
    if not coverage.get("input_entities", 0):
        failures.append({"rule": "nonempty_model_input", "message": "No model-space entities or resolved instances were read."})
    elif not any(result["components"].values()) and not decision_requests and not result["audits"].get("ignored_fire_protection_objects"):
        failures.append({"rule": "semantic_coverage", "message": "No modelable component or concrete semantic decision was produced."})
    if coverage.get("unclassified_entities"):
        warnings.append({"rule": "unclassified_source_entities", "count": coverage["unclassified_entities"], "details": "audits.source_coverage.unclassified"})
    if coverage.get("unsupported_geometry_entities"):
        warnings.append({"rule": "unsupported_source_geometry", "count": coverage["unsupported_geometry_entities"], "entity_types": coverage["unsupported_entity_types"]})
    status = "fail" if failures else "review_required" if warnings else "pass"
    return {"status": status, "failures": failures, "warnings": warnings}


def classify_document(data: dict[str, Any], rules: dict[str, Any], source_name: str = "input.json", *, recognition_decisions: dict | None = None) -> dict[str, Any]:
    model = [entity for entity in data.get("entities", []) if entity.get("owner_block") == "*Model_Space"]
    resolved = [entity for entity in data.get("resolved_instances", []) if entity.get("root_space") == "*Model_Space"]
    explicit_nested_geometry = _explicit_nested_semantic_geometry(resolved, rules)
    semantic_model = model + explicit_nested_geometry
    curtain_envelopes = _curtain_envelopes(model, resolved, rules)
    loose_curtain_systems, semantic_decision_requests = _large_opening_layer_systems(
        model, curtain_envelopes, rules
    )
    curtain_envelopes.extend(loose_curtain_systems)
    curtain_accessories = _curtain_accessories(model, resolved, curtain_envelopes, rules)
    nested_accessory_candidates = _nested_curtain_accessory_candidates(resolved, curtain_accessories, rules)
    ignored_fire_protection = [
        entity for entity in model if _is_ignored_fire_protection(entity.get("layer"), rules)
    ]
    plan_domains = [item["bbox"] for item in curtain_envelopes]
    plan_domain_source = "curtain_envelope"
    if not plan_domains:
        plan_domains = _fallback_plan_domains(semantic_model, rules)
        plan_domain_source = "architectural_wall_network_fallback" if plan_domains else "unresolved"
    inherited_geometry = _inherit_direct_container_geometry(model, resolved, rules, plan_domains)
    classification_model = semantic_model + inherited_geometry
    columns = _classify_columns(classification_model, rules, plan_domains)
    architectural_walls, structural_walls, sanitary_promoted, sanitary_audits = _classify_walls(
        classification_model, resolved, columns, rules, plan_domains
    )
    openings = _classify_openings(model, resolved, rules, plan_domains)
    wall_segments = architectural_walls + _structural_component_segments(structural_walls)
    _annotate_opening_adjacency(openings, wall_segments, rules)
    wall_continuity = _wall_continuity_audit(
        architectural_walls, structural_walls, columns, openings, plan_domains, rules
    )
    nested_architectural = sum(
        1 for entity in explicit_nested_geometry if _is_layer(entity.get("layer"), "architectural_wall", rules)
    )
    nested_structural = sum(
        1 for entity in explicit_nested_geometry if _is_layer(entity.get("layer"), "structural", rules)
    )
    nested_wall_finish = sum(
        1 for entity in explicit_nested_geometry if _is_layer(entity.get("layer"), "wall_finish", rules)
    )
    result = {
        "schema": "dwg-semantic-classification-v8",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "source_name": source_name,
        "rules_schema": rules.get("schema"),
        "profile_id": rules.get("profile_id"),
        "score_interpretation": "heuristic_evidence_score_not_probability",
        "policy": rules.get("policy"),
        "components": {
            "architectural_walls": [
                {"id": f"ARCH-WALL-{index:04d}", "class": "architectural_wall", **segment.as_dict()}
                for index, segment in enumerate(architectural_walls, 1)
            ],
            "structural_walls": structural_walls,
            "columns": columns,
            "curtain_envelopes": curtain_envelopes,
            "curtain_accessories": curtain_accessories,
            "curtain_accessory_candidates": nested_accessory_candidates,
            "openings": openings,
        },
        "inferences": {
            "sanitary_wall_segments_promoted": [segment.as_dict() for segment in sanitary_promoted],
        },
        "audits": {
            "sanitary_enclosures": sanitary_audits,
            "wall_continuity": wall_continuity,
            "semantic_decision_requests": semantic_decision_requests,
            "nested_accessory_decision_requests": [{"id": item["id"], "status": item["status"], "source_entity_ids": item["source_entity_ids"], "source_handles": item["source_handles"], "source_layer": item["source_layer"], "bbox": item["bbox"], "reason": "explicit_nested_metal_profile_requires_independent_semantic_and_height_review", "options": item["possible_roles"], "height_mm": None, "base_z_mm": None} for item in nested_accessory_candidates],
            "ignored_fire_protection_objects": [
                {
                    "source_handle": entity.get("handle"),
                    "source_layer": entity.get("layer"),
                    "source_layers": list(entity.get("source_layers") or [str(entity.get("raw_layer", entity.get("layer")) or "")]),
                    "source_type": entity.get("type"),
                    "reason": rules.get("scope", {}).get("exclusion_reason") or "explicit_rules_exclusion",
                    "basis": rules.get("scope", {}).get("exclusion_basis"),
                }
                for entity in ignored_fire_protection
            ],
            "plan_domains": [
                {"id": f"PLAN-DOMAIN-{index:03d}", "source": plan_domain_source, "bbox": domain}
                for index, domain in enumerate(plan_domains, 1)
            ],
        },
        "counts": {
            "architectural_wall_segments": len(architectural_walls),
            "structural_wall_components": len(structural_walls),
            "columns": len(columns),
            "curtain_envelopes": len(curtain_envelopes),
            "curtain_accessory_objects": len(curtain_accessories),
            "curtain_accessory_layers": len({item.get("source_layer") for item in curtain_accessories}),
            "nested_curtain_accessory_candidates": len(nested_accessory_candidates),
            "ignored_fire_protection_objects": len(ignored_fire_protection),
            "oversized_opening_objects_auto_classified_as_curtain": sum(
                int(item.get("parallel_outline_count", 0))
                for item in curtain_envelopes
                if item.get("geometry_mode") == "parallel_oversized_window_layer_polylines"
            ),
            "special_dimension_review_requests": len(semantic_decision_requests),
            "opening_source_objects": len(openings),
            "opening_block_instances": sum(
                1 for item in openings if item.get("source_type") in {"INSERT", "BLOCK_REFERENCE"}
            ),
            "opening_geometry_objects": sum(
                1 for item in openings if item.get("source_type") not in {"INSERT", "BLOCK_REFERENCE"}
            ),
            "opening_wall_adjacent_objects": sum(
                1 for item in openings if item.get("wall_adjacency_status") == "adjacent"
            ),
            "opening_unmatched_source_objects": sum(
                1 for item in openings if item.get("wall_adjacency_status") != "adjacent"
            ),
            "sanitary_zones": len(sanitary_audits),
            "sanitary_wall_segments_promoted": len(sanitary_promoted),
        },
        "generalization_guards": {
            "handle_overrides_used": 0,
            "coordinate_overrides_used": 0,
            "direct_structural_container_entities_inherited": len(inherited_geometry),
            "nested_structural_container_descendants_inherited": 0,
            "explicit_nested_layer_entities_materialized": len(explicit_nested_geometry),
            "explicit_nested_architectural_wall_entities": nested_architectural,
            "explicit_nested_structural_entities": nested_structural,
            "explicit_nested_wall_finish_entities": nested_wall_finish,
            "nested_opening_objects_classified": sum(1 for item in openings if item.get("instance_path")),
            "classification_inputs": [
                "top_level_layer_semantics",
                "explicit_nested_object_layer_semantics",
                "opening_layer_semantics",
                "oversized_semantic_object_review",
                "parallel_large_window_outline_curtain_recognition",
                "spatial_curtain_accessory_inheritance",
                "explicit_profile_or_job_fire_protection_scope",
                "world_space_block_geometry",
                "geometry",
                "topology",
                "fixture_proximity",
                "direct_container_semantics",
                "repetition",
            ],
        },
    }
    _add_exact_provenance(result["components"])
    result["audits"]["source_coverage"] = _source_coverage(model, resolved, result, rules)
    result["validation"] = _validate(result)
    attach_automatic_plan(result,data,rules,recognition_decisions)
    return result


def attach_automatic_plan(result,document,rules,decisions=None,*,slab_decisions=None):
    """Keep legacy segment APIs while exposing authoritative material topology."""
    from .plan_recognition import recognize_plan
    automatic=recognize_plan(document,rules,result,decisions=decisions,slab_decisions=slab_decisions)
    result["automatic_plan"]=automatic
    result["components"]["material_footprints"]=[*automatic["wall_materials"],*automatic["columns"],
        *[section for system in automatic["facade_systems"]for section in system["sections"]]]
    result["components"]["door_assemblies"]=automatic["door_assemblies"]
    result["counts"]["recognized_material_footprints"]=len(result["components"]["material_footprints"])
    result["counts"]["recognized_columns"]=len(automatic["columns"])
    result["counts"]["recognized_door_assemblies"]=len(automatic["door_assemblies"])

