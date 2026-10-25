"""Deterministic RC slab overlays for reviewed source-bound decisions.

This module prepares payloads only. It neither approves a proposal nor reads or
writes SKP files. Model-wide source/tag authorization remains the caller's job.
Missing elevations keep complete 2D audit regions and block the job. Ordinary
white-model slab thickness defaults to the authorized200mm with provenance;
explicit source/decision dimensions and special-case requirements take priority.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import defaultdict

from shapely.geometry import GeometryCollection, LineString, Polygon
from shapely import set_precision, union_all
from shapely.geometry.polygon import orient
from shapely.ops import linemerge, polygonize, unary_union
from shapely.strtree import STRtree

AREA_TOLERANCE_MM2 = 1e-6
HEIGHT_TOLERANCE_MM = 1e-6
ROLES = {"floor": "floor_slab", "overhead": "overhead_slab"}
RESERVED_TAGS = {"0", "Layer0", "Untagged"}
DEFAULT_SLAB_THICKNESS_MM = 200.0
DEFAULT_SLAB_THICKNESS_RULE = "slab.nominal_thickness_200.v1"


def _safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_safe(item) for item in value]
    return value


def _hash(value):
    return hashlib.sha256(json.dumps(_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _number(value, name, *, positive=False):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError(f"{name} must be a finite {'positive ' if positive else ''}number")
    return result


def _point(value):
    point = value.get("point", value) if isinstance(value, dict) else value
    result = [float(point["x"]), float(point["y"])] if isinstance(point, dict) else [float(point[0]), float(point[1])]
    if not all(math.isfinite(item) for item in result):
        raise ValueError("Slab XY coordinates must be finite")
    # Explicit slab footprints are planar XY regions. Z is defined by each
    # section decision, not by silently flattening tilted input footprints.
    if (isinstance(point, dict) and "z" in point) or (not isinstance(point, dict) and len(point) > 2):
        raise ValueError("Slab footprint must contain XY points; give elevations in top/bottom decisions")
    return result


def _polygon(footprint):
    if not isinstance(footprint, dict) or not isinstance(footprint.get("outer"), list):
        raise ValueError("Expected footprint {outer:[XY,...], holes:[[XY,...],...]}")
    polygon = Polygon([_point(point) for point in footprint["outer"]], [[_point(point) for point in ring] for ring in footprint.get("holes", [])])
    if not polygon.is_valid or polygon.is_empty or polygon.area <= AREA_TOLERANCE_MM2:
        raise ValueError("Slab footprint must be a valid positive-area polygon with valid holes")
    return polygon


def _parts(geometry):
    if geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        return [geometry]
    return [polygon for child in getattr(geometry, "geoms", []) for polygon in _parts(child)]


def _polygon_data(polygon):
    polygon = orient(polygon.normalize(), sign=1)
    return {"outer": [[float(x), float(y)] for x, y in list(polygon.exterior.coords)[:-1]], "holes": [[[float(x), float(y)] for x, y in list(ring.coords)[:-1]] for ring in polygon.interiors]}


def _footprint(geometry):
    parts = sorted((_polygon_data(polygon) for polygon in _parts(geometry)), key=lambda part: _hash(part))
    return parts[0] if len(parts) == 1 else {"parts": parts}


def _ids(value):
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError("Source IDs must be a list of nonempty strings")
    return sorted(set(value))


def _derived_tag(value):
    if not isinstance(value, dict) or value.get("kind") != "derived":
        raise ValueError("Expected an explicitly derived slab tag")
    for key in ["tag", "source_layer"]:
        name = value.get(key)
        if not isinstance(name, str) or not name.strip() or "\x00" in name:
            raise ValueError(f"Derived slab {key} must be a nonempty name")
        if name in RESERVED_TAGS:
            raise ValueError(f"Derived slab {key} cannot use reserved name {name}")
    return {"source_id": "derived:slabs", "source_layer": value["source_layer"], "tag": value["tag"], "kind": "derived", "derivation": "confirmed_rc_slab_extent_zone_opening_overlay"}


def _source_layer(entity, policy):
    original = str(entity.get("original_layer", entity.get("layer", "0")))
    effective = str(entity.get("effective_layer", entity.get("layer", original)))
    return effective if original in {"0", "Layer0"} and policy == "effective" else original


def _source_tag(value, document, system, index, policy):
    if not isinstance(value, dict) or value.get("kind") != "source":
        raise ValueError("Expected a source or derived slab tag_binding")
    name = value.get("source_layer")
    if not isinstance(name, str) or not name.strip() or "\x00" in name or name in RESERVED_TAGS:
        raise ValueError("Source slab tag requires a nonreserved original source layer name")
    if value.get("tag") != name:
        raise ValueError("Source slab tag must equal its original source_layer")
    real_layers = {_source_layer(index[identity], policy) for identity in _ids(system.get("source_entity_ids")) if identity in index}
    if name not in real_layers:
        raise ValueError("Source slab tag has no matching layer among this system's source_entity_ids")
    source_path = (document.get("intake") or {}).get("source_path")
    if not isinstance(source_path, str) or not source_path:
        raise ValueError("Source slab tag requires document.intake.source_path identity")
    # Exact base-model mapping shape lets composition reuse the existing DWG
    # tag. It intentionally carries no derived marker or invented namespace.
    return {"source_id": source_path, "source_layer": name, "tag": name, "reason": "original_name"}


def _profile(system, zone=None):
    """Return an explicit bottom/top/thickness and evidence, including nulls."""
    zone = zone or {}
    method = zone.get("method", "constant_thickness")
    if method not in {"constant_thickness", "uniform_shift", "explicit", "explicit_bottom", "explicit_thickness"}:
        raise ValueError(f"Unsupported slab zone method: {method}")
    thickness_policy = zone.get("thickness_policy", system.get("thickness_policy", "default_200"))
    if thickness_policy not in {"default_200", "explicit_required"}:
        raise ValueError("thickness_policy must be default_200 or explicit_required")
    default_top = _number(system.get("default_top_z_mm"), "default_top_z_mm")
    default_thickness = _number(system.get("default_thickness_mm"), "default_thickness_mm", positive=True)
    top = _number(zone.get("top_z_mm"), "top_z_mm")
    bottom = _number(zone.get("bottom_z_mm"), "bottom_z_mm")
    thickness = _number(zone.get("thickness_mm"), "thickness_mm", positive=True)
    default_applied = False
    if top is None:
        top = default_top
    if method == "uniform_shift":
        shift = _number(zone.get("shift_z_mm"), "shift_z_mm")
        if shift is None:
            raise ValueError("uniform_shift requires shift_z_mm")
        if default_top is None:
            top = None
        else:
            shifted = default_top + shift
            if zone.get("top_z_mm") is not None and abs(top - shifted) > HEIGHT_TOLERANCE_MM:
                raise ValueError("uniform_shift top_z_mm conflicts with default_top_z_mm + shift_z_mm")
            top = shifted
        shifted_thickness = default_thickness
        if shifted_thickness is None and thickness_policy == "default_200":
            shifted_thickness = DEFAULT_SLAB_THICKNESS_MM
        if thickness is not None and shifted_thickness is not None and abs(thickness - shifted_thickness) > HEIGHT_TOLERANCE_MM:
            raise ValueError("uniform_shift preserves default_thickness_mm; use an explicit section to change thickness")
        thickness = shifted_thickness
        default_applied = default_thickness is None and shifted_thickness is not None
    if method == "explicit_bottom" and bottom is None:
        raise ValueError("explicit_bottom requires bottom_z_mm")
    if method in {"explicit_bottom", "explicit", "explicit_thickness"} and bottom is not None and thickness is not None and zone.get("top_z_mm") is None:
        top = bottom + thickness
    if bottom is not None and top is not None:
        measured = top - bottom
        if measured <= 0:
            raise ValueError("Slab top must be above its explicit bottom")
        if thickness is not None and abs(thickness - measured) > HEIGHT_TOLERANCE_MM:
            raise ValueError("Explicit slab top/bottom/thickness disagree")
        thickness = measured
        default_applied = False
    elif thickness is None:
        thickness = default_thickness
        if thickness is None and thickness_policy == "default_200":
            thickness = DEFAULT_SLAB_THICKNESS_MM
            default_applied = True
    if top is None and bottom is not None and thickness is not None:
        top = bottom + thickness
    if top is not None and thickness is not None:
        computed = top - thickness
        if bottom is not None and abs(bottom - computed) > HEIGHT_TOLERANCE_MM:
            raise ValueError("Slab bottom conflicts with top minus thickness")
        bottom = computed
    missing = [name for name, value in [("top_z_mm", top), ("thickness_mm", thickness)] if value is None]
    profile = {"method": method, "base_z_mm": bottom, "top_z_mm": top, "thickness_mm": thickness, "missing_parameters": missing}
    if default_applied:
        profile["thickness_provenance"] = {"rule_id": DEFAULT_SLAB_THICKNESS_RULE, "value_mm": DEFAULT_SLAB_THICKNESS_MM,
            "basis": "authorized_white_model_default", "policy": thickness_policy, "source_measurement": False,
            "precedence": "explicit zone/system thickness or explicit consistent top-and-bottom section first"}
    return profile


def _line_parts(geometry):
    if geometry.is_empty:
        return []
    if geometry.geom_type in {"LineString", "LinearRing"}:
        return [geometry] if geometry.length > HEIGHT_TOLERANCE_MM else []
    return [line for child in getattr(geometry, "geoms", []) for line in _line_parts(child)]


def _line_data(geometry):
    lines = []
    for line in _line_parts(geometry):
        points = [[float(x), float(y)] for x, y in line.coords]
        lines.append(min(points, list(reversed(points))))
    return {"lines": sorted(lines)}


def _geometry_from_footprint(value):
    if "parts" in value:
        return unary_union([_geometry_from_footprint(part) for part in value["parts"]])
    return Polygon(value["outer"], value.get("holes", []))


def _riser_trace(lower_groups, upper_groups, cutters=()):
    traces = {}
    collections = [(lower_groups, "step_riser_lower_host"), (upper_groups, "step_riser_upper_host"), (cutters, "step_riser_material_cut_boundary")]
    for groups, role in collections:
        for group in groups:
            for source in group.get("sources", []):
                identity = source["entity_id"]
                trace = traces.setdefault(identity, {**source, "roles": list(source.get("roles", []))})
                trace["roles"] = sorted(set(trace["roles"]) | {role})
    groups = [group for collection, _ in collections for group in collection]
    return {"source_entity_ids": sorted({identity for group in groups for identity in group.get("source_entity_ids", [])}), "annotation_source_ids": sorted({identity for group in groups for identity in group.get("annotation_source_ids", [])}), "source_layers": sorted({layer for group in groups for layer in group.get("source_layers", [])}), "source_handles": sorted({handle for group in groups for handle in group.get("source_handles", [])}), "sources": [traces[identity] for identity in sorted(traces)], "unresolved_source_entity_ids": sorted({identity for group in groups for identity in group.get("unresolved_source_entity_ids", [])})}


def _normalize_edge_precision_system(result, system_id, grid, items):
    """Apply an explicitly reviewed precision model before any riser overlay.

    This also runs when risers are disabled, so a gross host-height projection
    can use precisely the same normalization. Raw source-derived hosts remain
    immutable snapshots, and every collapsed/split host is explicitly recorded.
    """
    audit = result["slab_audit"]
    raw_groups = [copy.deepcopy(group) for group, _ in items]
    record = {"system_id": system_id, "algorithm": "shapely.set_precision", "grid_size_mm": grid, "mode": "valid_output", "max_allowed_vertex_displacement_mm": grid / math.sqrt(2), "max_boundary_change_mm": grid, "overlay_precision_propagation": True, "raw_original_groups": raw_groups, "raw_assembly": copy.deepcopy(next(row for row in audit["assemblies"] if row["id"] == system_id)), "raw_zones": [copy.deepcopy(row) for row in audit["zones"] if row["system_id"] == system_id], "components": []}
    record["source_assembly"] = copy.deepcopy(record["raw_assembly"])
    raw_region = unary_union([polygon for _, polygon in items])
    record["source_domain_difference_area_mm2"] = raw_region.symmetric_difference(_geometry_from_footprint(record["source_assembly"]["footprint"])).area
    record["raw_assembly"].update(footprint=_footprint(raw_region), expected_area_mm2=raw_region.area, expected_volume_mm3=sum(group["volume_mm3"] for group in raw_groups))
    normalized = []
    for original, polygon in items:
        clean = set_precision(polygon, grid, mode="valid_output")
        changes = polygon.symmetric_difference(clean)
        movement = max(math.hypot(round(x / grid) * grid - x, round(y / grid) * grid - y) for ring in [polygon.exterior, *polygon.interiors] for x, y in ring.coords)
        outside = changes.difference(polygon.boundary.buffer(grid / math.sqrt(2) + HEIGHT_TOLERANCE_MM)).area
        actual_ids = []
        for part in sorted(_parts(clean), key=lambda value: _hash(_polygon_data(value))):
            if part.area <= AREA_TOLERANCE_MM2:
                continue
            shape = _polygon_data(part)
            identity = original["id"] if polygon.equals(part) else "slab-precision-host:" + _hash({"original": original["id"], "grid": grid, "shape": shape})[:24]
            group = {**copy.deepcopy(original), "id": identity, "name": identity, "original_before_precision_component_id": original["id"], "geometry_precision_mm": grid, "area_mm2": part.area, "volume_mm3": part.area * original["height_mm"], **shape}
            actual_ids.append(identity)
            # Serialized prisms use exact double-coordinate geometry. A shared
            # noded graph below, rather than per-operation snap rounding, owns
            # all later topology when the opt-in precision policy is active.
            normalized.append((group, set_precision(part, 0)))
        removed_holes = [{"ring": [[float(x), float(y)] for x, y in list(ring.coords)[:-1]], "area_mm2": Polygon(ring).area, "filled_area_mm2": Polygon(ring).intersection(clean).area} for ring in polygon.interiors if Polygon(ring).intersection(clean).area > 0]
        axis_movement = max(max(abs(round(x / grid) * grid - x), abs(round(y / grid) * grid - y)) for ring in [polygon.exterior, *polygon.interiors] for x, y in ring.coords)
        record["components"].append({"raw_component_id": original["id"], "normalized_component_ids": actual_ids, "raw_footprint": _footprint(polygon), "normalized_footprint": _footprint(clean), "raw_area_mm2": polygon.area, "normalized_area_mm2": clean.area, "symmetric_difference_area_mm2": changes.area, "raw_hole_count": len(polygon.interiors), "normalized_hole_count": sum(len(part.interiors) for part in _parts(clean)), "hausdorff_distance_mm": None if clean.is_empty else polygon.hausdorff_distance(clean), "max_vertex_displacement_mm": movement, "max_axis_vertex_displacement_mm": axis_movement, "removed_raw_holes": removed_holes, "boundary_change_outside_limit_area_mm2": outside, "status": "collapsed" if clean.is_empty else "split" if len(actual_ids) > 1 else "normalized"})
        if outside > AREA_TOLERANCE_MM2 or movement > grid / math.sqrt(2) + HEIGHT_TOLERANCE_MM:
            result["blockers"].append({"code": "slab_precision_displacement_exceeded", "system_id": system_id, "component_id": original["id"], "outside_area_mm2": outside, "vertex_displacement_mm": movement})
    # One source-owner partition is required even when risers are disabled:
    # independently rounded sloping hosts can otherwise overlap as serialized
    # exact polygons. The gross height field uses these same canonical hosts.
    record["normalized_original_groups"] = [copy.deepcopy(group) for group, _ in normalized]
    source_overlay = common_edge_precision_overlay(record["normalized_original_groups"], [], grid)
    record["source_overlay"] = source_overlay
    source_regions = {row["original_component_id"]: _geometry_from_footprint(row["footprint"]) for row in source_overlay["normalized_host_regions"]}
    noded = []
    for original, before in normalized:
        canonical = source_regions[original["id"]]
        for part in sorted(_parts(canonical), key=lambda value: _hash(_polygon_data(value))):
            shape = _polygon_data(part)
            identity = original["id"] if before.equals(part) else "slab-source-noded:" + _hash({"original": original["id"], "grid": grid, "shape": shape})[:24]
            group = {**copy.deepcopy(original), "id": identity, "name": identity, "source_noded_from_component_id": original["id"], "area_mm2": part.area, "volume_mm3": part.area * original["height_mm"], **shape}
            noded.append((group, part))
    normalized = sorted(noded, key=lambda item: item[0]["id"])
    record["source_noded_original_groups"] = [copy.deepcopy(group) for group, _ in normalized]
    raw_ids = {group["id"] for group in raw_groups}
    result["groups"] = [group for group in result["groups"] if group["id"] not in raw_ids] + [group for group, _ in normalized]
    for zone in list(audit["zones"]):
        if zone["system_id"] != system_id:
            continue
        groups = [(group, polygon) for group, polygon in normalized if group["slab_zone_id"] == zone["id"]]
        if not groups:
            audit["zones"].remove(zone)
            continue
        region = unary_union([polygon for _, polygon in groups])
        zone.update(component_ids=sorted(group["id"] for group, _ in groups), footprint=_footprint(region), expected_area_mm2=region.area, expected_volume_mm3=sum(group["volume_mm3"] for group, _ in groups), geometry_precision_mm=grid)
    assembly = next(row for row in audit["assemblies"] if row["id"] == system_id)
    region = unary_union([polygon for _, polygon in normalized])
    assembly.update(component_ids=sorted(group["id"] for group, _ in normalized), component_tags={group["id"]: group["tag"] for group, _ in normalized}, footprint=_footprint(region), expected_area_mm2=region.area, expected_volume_mm3=sum(group["volume_mm3"] for group, _ in normalized), geometry_precision_mm=grid)
    record["max_vertex_displacement_mm"] = max((row["max_vertex_displacement_mm"] for row in record["components"]), default=0)
    _record_final_precision(result, record, record["source_noded_original_groups"])
    audit.setdefault("geometry_precision", []).append(record)
    for system in audit["systems"]:
        if system["id"] == system_id:
            system.update(geometry_precision_mm=grid, normalized_projected_area_mm2=region.area)
    return sorted(normalized, key=lambda item: item[0]["id"])


def _record_final_precision(result, record, canonical_groups):
    """Measure the total raw-to-canonical change, not two separate allowances."""
    grid = record["grid_size_mm"]
    raw = {group["id"]: _geometry_from_footprint(group) for group in record["raw_original_groups"]}
    regions = defaultdict(list)
    for group in canonical_groups:
        regions[group["original_before_precision_component_id"]].append(_geometry_from_footprint(group))
    rows = []
    for identity, raw_shape in raw.items():
        clean = unary_union(regions[identity])
        changes = raw_shape.symmetric_difference(clean)
        outside = changes.difference(raw_shape.boundary.buffer(grid + HEIGHT_TOLERANCE_MM)).area
        raw_outer = unary_union([part.exterior for part in _parts(raw_shape)])
        clean_outer = unary_union([part.exterior for part in _parts(clean)])
        removed_holes = [{"ring": [[float(x), float(y)] for x, y in list(ring.coords)[:-1]], "area_mm2": Polygon(ring).area, "filled_area_mm2": Polygon(ring).intersection(clean).area} for part in _parts(raw_shape) for ring in part.interiors if Polygon(ring).intersection(clean).area > 0]
        rows.append({"raw_component_id": identity, "canonical_component_ids": sorted(group["id"] for group in canonical_groups if group["original_before_precision_component_id"] == identity), "raw_footprint": _footprint(raw_shape), "canonical_footprint": _footprint(clean), "raw_area_mm2": raw_shape.area, "canonical_area_mm2": clean.area, "symmetric_difference_area_mm2": changes.area, "raw_hole_count": sum(len(part.interiors) for part in _parts(raw_shape)), "canonical_hole_count": sum(len(part.interiors) for part in _parts(clean)), "hausdorff_distance_mm": None if clean.is_empty else raw_shape.hausdorff_distance(clean), "exterior_hausdorff_distance_mm": None if clean.is_empty else raw_outer.hausdorff_distance(clean_outer), "removed_raw_holes": removed_holes, "boundary_change_outside_limit_area_mm2": outside})
        if outside > AREA_TOLERANCE_MM2:
            result["blockers"].append({"code": "slab_precision_total_displacement_exceeded", "system_id": record["system_id"], "component_id": identity, "outside_area_mm2": outside, "bound_mm": grid})
    raw_union, clean_union = unary_union(list(raw.values())), unary_union([_geometry_from_footprint(group) for group in canonical_groups])
    record["final_components"] = rows
    record["canonical_original_groups"] = copy.deepcopy(canonical_groups)
    record["raw_projected_footprint"] = _footprint(raw_union)
    record["canonical_projected_footprint"] = _footprint(clean_union)
    record["projected_symmetric_difference_area_mm2"] = raw_union.symmetric_difference(clean_union).area
    record["removed_projected_holes"] = [{"ring": [[float(x), float(y)] for x, y in list(ring.coords)[:-1]], "area_mm2": Polygon(ring).area, "filled_area_mm2": Polygon(ring).intersection(clean_union).area} for part in _parts(raw_union) for ring in part.interiors if Polygon(ring).intersection(clean_union).area > 0]


def common_edge_precision_overlay(original_groups, requested_bands, grid):
    """Return deterministic, disjoint faces on one shared snap-rounded graph.

    Boundary segments are noded once together. Each retained face belongs to
    the source host with the largest positive material intersection, provided
    that intersection covers strictly more than half the face. Ties between
    hosts use lexical IDs; an exact half-area band tie is excluded.
    """
    hosts = {group["id"]: set_precision(_geometry_from_footprint(group), 0) for group in original_groups}
    bands = {row["connection_id"]: set_precision(_geometry_from_footprint(row["footprint"]), 0) for row in requested_bands}
    lower_ids = {row["connection_id"]: row["original_lower_component_id"] for row in requested_bands}
    graph = union_all([region.boundary for region in [*hosts.values(), *bands.values()]], grid_size=grid)
    faces = []
    host_faces, band_faces = defaultdict(list), defaultdict(list)
    for face in sorted(polygonize(graph), key=lambda part: _hash(_polygon_data(part))):
        if face.area <= AREA_TOLERANCE_MM2:
            continue
        scores = sorted(((face.intersection(region).area, identity) for identity, region in hosts.items()), key=lambda item: (-item[0], item[1]))
        if not scores or scores[0][0] <= face.area / 2:
            continue
        owner = scores[0][1]
        active = sorted(identity for identity, region in bands.items() if lower_ids[identity] == owner and face.intersection(region).area > face.area / 2)
        host_faces[owner].append(face)
        for identity in active:
            band_faces[identity].append(face)
        faces.append({"footprint": _footprint(face), "original_component_id": owner, "requested_connection_ids": active})
    return {"algorithm": "common_noded_planar_faces", "grid_size_mm": grid, "source_owner_rule": "max_intersection_area_strictly_over_half_face_area_then_lexical_original_id", "band_attribution_rule": "matching_lower_source_owner_and_intersection_area_strictly_over_half_face_area", "input_original_groups": copy.deepcopy(original_groups), "input_requested_bands": copy.deepcopy(requested_bands), "atomic_faces": faces, "normalized_host_regions": [{"original_component_id": identity, "footprint": _footprint(unary_union(host_faces[identity]))} for identity in sorted(hosts)], "normalized_requested_bands": [{"connection_id": identity, "footprint": _footprint(unary_union(band_faces[identity]))} for identity in sorted(bands)]}


def _prepare_edge_aligned_system(result, system_id, raw_policy, width, items, policy_row):
    """Replace lower-side strips with full-depth prisms, without Z slicing."""
    audit = result["slab_audit"]
    profile = "edge_aligned_full_depth"
    original_groups = [copy.deepcopy(group) for group, _ in items]
    original_assembly = copy.deepcopy(next(row for row in audit["assemblies"] if row["id"] == system_id))
    original_zones = [copy.deepcopy(row) for row in audit["zones"] if row["system_id"] == system_id]
    originals = {group["id"]: group for group, _ in items}
    polygons = {group["id"]: polygon for group, polygon in items}
    grid = raw_policy.get("geometry_precision_mm")
    links = []
    tree = STRtree([polygon for _, polygon in items])
    for number, (first, first_polygon) in enumerate(items):
        for other_index in sorted(int(index) for index in tree.query(first_polygon)):
            if other_index <= number:
                continue
            second, second_polygon = items[other_index]
            if abs(first["base_z_mm"] - second["base_z_mm"]) <= HEIGHT_TOLERANCE_MM and abs(first["top_z_mm"] - second["top_z_mm"]) <= HEIGHT_TOLERANCE_MM:
                continue
            shared = unary_union(_line_parts(first_polygon.boundary.intersection(second_polygon.boundary)))
            if shared.geom_type == "MultiLineString":
                shared = linemerge(shared)
            if shared.length <= HEIGHT_TOLERANCE_MM:
                continue
            lower, upper = sorted([first, second], key=lambda group: (group["top_z_mm"], group["base_z_mm"], group["id"]))
            band = shared.buffer(width, cap_style="flat", join_style="mitre").intersection(polygons[lower["id"]])
            identity = "slab-step-link:" + _hash({"profile": profile, "system": system_id, "hosts": [lower["id"], upper["id"]], "shared": _line_data(shared), "width": width})[:24]
            row = {"id": identity, "profile": profile, "system_id": system_id, "host_slab_role": lower["slab_role"], "component_ids": [], "original_lower_horizontal_component_ids": [lower["id"]], "original_upper_horizontal_component_ids": [upper["id"]], "lower_horizontal_component_ids": [], "upper_horizontal_component_ids": [], "lower_replacement_component_ids": [], "upper_replacement_component_ids": [], "host_tags": {"lower": lower["tag"], "upper": upper["tag"]}, "shared_boundary": _line_data(shared), "shared_boundary_length_mm": shared.length, "base_z_mm": lower["base_z_mm"], "top_z_mm": upper["top_z_mm"], "height_mm": upper["top_z_mm"] - lower["base_z_mm"], "horizontal_thickness_mm": width, "tag": lower["tag"], "source_layer": lower["source_layer"], "tag_kind": lower["tag_kind"], "requested_footprint": _footprint(band), "lower_side_band": _footprint(band), "footprint": _footprint(band), "requested_area_mm2": band.area, "buffer_policy": {"distance_mm": width, "cap_style": "flat", "join_style": "mitre", "clip": "original_lower_host"}, "status": "pending", **_riser_trace([lower], [upper])}
            audit["step_risers"].append(row)
            if band.area <= AREA_TOLERANCE_MM2:
                result["blockers"].append({"code": "slab_step_riser_empty_band", "system_id": system_id, "connection_id": identity})
                row["status"] = "blocked"
                continue
            links.append({"audit": row, "lower": lower, "upper": upper, "shared": shared, "requested": band})
    if grid is not None and links:
        requested = [{"connection_id": link["audit"]["id"], "original_lower_component_id": link["lower"]["id"], "original_upper_component_id": link["upper"]["id"], "footprint": _footprint(link["requested"])} for link in links]
        overlay = common_edge_precision_overlay(original_groups, requested, grid)
        precision_record = next(row for row in audit["geometry_precision"] if row["system_id"] == system_id)
        precision_record["edge_overlay"] = overlay
        canonical_hosts = {row["original_component_id"]: _geometry_from_footprint(row["footprint"]) for row in overlay["normalized_host_regions"]}
        canonical_bands = {row["connection_id"]: _geometry_from_footprint(row["footprint"]) for row in overlay["normalized_requested_bands"]}
        canonical_items = []
        canonical_snapshots = []
        for group, raw_polygon in items:
            polygon = canonical_hosts[group["id"]]
            changed = raw_polygon.symmetric_difference(polygon)
            outside = changed.difference(raw_polygon.boundary.buffer(grid / math.sqrt(2) + HEIGHT_TOLERANCE_MM)).area
            if outside > AREA_TOLERANCE_MM2:
                result["blockers"].append({"code": "slab_precision_overlay_displacement_exceeded", "system_id": system_id, "component_id": group["id"], "outside_area_mm2": outside})
            footprint = _footprint(polygon)
            normalized = {**copy.deepcopy(group), **footprint, "area_mm2": polygon.area, "volume_mm3": polygon.area * group["height_mm"]}
            if "parts" in footprint:
                normalized.pop("outer", None)
                normalized.pop("holes", None)
            else:
                normalized.pop("parts", None)
            canonical_items.append((normalized, polygon))
            canonical_snapshots.append(normalized)
        items = canonical_items
        originals = {group["id"]: group for group, _ in items}
        original_groups = copy.deepcopy(canonical_snapshots)
        _record_final_precision(result, precision_record, original_groups)
        polygons = canonical_hosts
        for link in links:
            row = link["audit"]
            lower_id, upper_id = link["lower"]["id"], link["upper"]["id"]
            link["lower"], link["upper"] = originals[lower_id], originals[upper_id]
            link["requested"] = canonical_bands[row["id"]]
            shared = unary_union(_line_parts(polygons[lower_id].boundary.intersection(polygons[upper_id].boundary)))
            if shared.geom_type == "MultiLineString":
                shared = linemerge(shared)
            link["shared"] = shared
            row.update(raw_shared_boundary=row["shared_boundary"], shared_boundary=_line_data(shared), shared_boundary_length_mm=shared.length, raw_requested_footprint=row["requested_footprint"], requested_footprint=_footprint(link["requested"]), lower_side_band=_footprint(link["requested"]), footprint=_footprint(link["requested"]), requested_area_mm2=link["requested"].area, geometry_precision_mm=grid)
        # These snapshots describe the exact canonical source partition that
        # subsequent full-depth replacement must preserve.
        original_region = unary_union(list(polygons.values()))
        original_assembly.update(footprint=_footprint(original_region), expected_area_mm2=original_region.area, expected_volume_mm3=sum(group["volume_mm3"] for group, _ in items))
        for zone in original_zones:
            regions = [polygon for group, polygon in items if group["slab_zone_id"] == zone["id"]]
            region = unary_union(regions)
            zone.update(footprint=_footprint(region), expected_area_mm2=region.area, expected_volume_mm3=region.area * zone["thickness_mm"])
    # Highest top owns competing corner/T strips for their entire section.
    # Distinct original lower regions have disjoint polygon interiors.
    links.sort(key=lambda link: (-link["audit"]["top_z_mm"], link["audit"]["base_z_mm"], link["lower"]["tag"], link["audit"]["id"]))
    occupied = GeometryCollection()
    buckets = defaultdict(list)
    for link in links:
        allocated = link["requested"].difference(occupied)
        occupied = unary_union([occupied, link["requested"]])
        link["allocated"] = allocated
        link["audit"]["allocated_footprint"] = _footprint(allocated)
        link["audit"]["allocated_area_mm2"] = allocated.area
        lower, upper = link["lower"], link["upper"]
        key = (lower["base_z_mm"], upper["top_z_mm"], lower["tag"], lower["source_layer"], lower["tag_kind"], lower["slab_role"])
        buckets[key].append(allocated)
    panels = []
    for key in sorted(buckets):
        bottom, top, tag, layer, kind, role = key
        for polygon in sorted(_parts(unary_union(buckets[key])), key=lambda value: _hash(_polygon_data(value))):
            if polygon.area <= AREA_TOLERANCE_MM2:
                continue
            # Zero-distance simplification removes only redundant collinear
            # vertices. Keeping them would split an otherwise planar side into
            # several coplanar extrusion faces. No smoothing or offset occurs.
            polygon = polygon.normalize().simplify(0, preserve_topology=True)
            contributions = [link for link in links if bottom <= link["audit"]["base_z_mm"] + HEIGHT_TOLERANCE_MM and top >= link["audit"]["top_z_mm"] - HEIGHT_TOLERANCE_MM and polygon.intersection(link["requested"]).area > AREA_TOLERANCE_MM2]
            lower_hosts = {link["lower"]["id"]: link["lower"] for link in contributions}
            upper_hosts = {link["upper"]["id"]: link["upper"] for link in contributions}
            shape = _polygon_data(polygon)
            identity = "slab-edge-riser:" + _hash({"system": system_id, "profile": profile, "base": bottom, "top": top, "tag": tag, "shape": shape})[:24]
            group = {"id": identity, "name": identity, "category": "slab", "semantic_role": "slab_step_riser", "slab_role": "slab_step_riser", "host_slab_role": role, "slab_system_id": system_id, "host_system_id": system_id, "slab_zone_id": f"{system_id}:step_risers", "mode": "solid_extrusion", "profile": profile, "base_z_mm": bottom, "top_z_mm": top, "height_mm": top - bottom, "horizontal_thickness_mm": width, "area_mm2": polygon.area, "volume_mm3": polygon.area * (top - bottom), "tag": tag, "source_layer": layer, "tag_kind": kind, "floor_datum_m": audit.get("floor_datum_m"), "original_lower_horizontal_component_ids": sorted(lower_hosts), "original_upper_horizontal_component_ids": sorted(upper_hosts), "lower_horizontal_component_ids": [], "upper_horizontal_component_ids": [], "riser_audit_ids": sorted(link["audit"]["id"] for link in contributions), "primary_tag_evidence": {"method": "original_lower_side_material_tag", "original_host_component_ids": sorted(host["id"] for host in lower_hosts.values() if host["tag"] == tag)}, "evidence": {"method": "approved_edge_aligned_full_depth_priority_overlay", "decision": _safe(raw_policy), "buffer_policy": {"distance_mm": width, "cap_style": "flat", "join_style": "mitre", "clip": "original_lower_host"}, "allocation_priority": "highest_top_then_lowest_base_then_original_tag_then_connection_id"}, **shape, **_riser_trace(list(lower_hosts.values()), list(upper_hosts.values()))}
            panels.append((group, polygon))
    all_bands = unary_union([polygon for _, polygon in panels])
    replacements = []
    horizontal = []
    by_original = {}
    for original, polygon in items:
        removed = polygon.intersection(all_bands)
        remaining = polygon.difference(all_bands)
        actual = []
        if removed.area <= AREA_TOLERANCE_MM2 and polygon.geom_type == "Polygon":
            actual = [original]
        else:
            for part in sorted(_parts(remaining), key=lambda value: _hash(_polygon_data(value))):
                if part.area <= AREA_TOLERANCE_MM2:
                    continue
                shape = _polygon_data(part)
                identity = "slab-edge-host:" + _hash({"original": original["id"], "profile": profile, "shape": shape})[:24]
                group = {**copy.deepcopy(original), "id": identity, "name": identity, "original_horizontal_component_id": original["id"], "horizontal_replacement_profile": profile, "area_mm2": part.area, "volume_mm3": part.area * original["height_mm"], **shape}
                group.pop("parts", None)
                actual.append(group)
        by_original[original["id"]] = actual
        horizontal.extend(actual)
        riser_ids = sorted(group["id"] for group, footprint in panels if footprint.intersection(polygon).area > AREA_TOLERANCE_MM2)
        replacements.append({"original_component_id": original["id"], "result_horizontal_component_ids": [group["id"] for group in actual], "riser_component_ids": riser_ids, "removed_footprint": _footprint(removed), "remaining_footprint": _footprint(remaining), "original_area_mm2": polygon.area, "remaining_area_mm2": remaining.area, "removed_area_mm2": removed.area, "original_volume_mm3": polygon.area * original["height_mm"], "remaining_horizontal_volume_mm3": remaining.area * original["height_mm"]})
    for link in links:
        row = link["audit"]
        for side in ("lower", "upper"):
            original_id = link[side]["id"]
            row[side + "_horizontal_component_ids"] = sorted(group["id"] for group in by_original[original_id])
            row[side + "_replacement_component_ids"] = sorted({*row[side + "_horizontal_component_ids"], *(group["id"] for group, polygon in panels if polygon.intersection(polygons[original_id]).area > AREA_TOLERANCE_MM2)})
        covering = [(group, polygon) for group, polygon in panels if row["id"] in group["riser_audit_ids"]]
        covered = unary_union([polygon for _, polygon in covering])
        uncovered = link["requested"].difference(covered)
        row.update(component_ids=sorted(group["id"] for group, _ in covering), component_tags={group["id"]: group["tag"] for group, _ in covering}, uncovered_area_mm2=uncovered.area, expected_volume_mm3=link["requested"].area * row["height_mm"], status="edge_aligned_full_depth" if uncovered.area <= AREA_TOLERANCE_MM2 else "blocked")
        if uncovered.area > AREA_TOLERANCE_MM2:
            result["blockers"].append({"code": "slab_edge_riser_band_uncovered", "system_id": system_id, "connection_id": row["id"], "area_mm2": uncovered.area})
    for group, _ in panels:
        for side in ("lower", "upper"):
            group[side + "_horizontal_component_ids"] = sorted({actual["id"] for identity in group["original_" + side + "_horizontal_component_ids"] for actual in by_original[identity]})
            group[side + "_replacement_component_ids"] = sorted({*group[side + "_horizontal_component_ids"], *(panel["id"] for identity in group["original_" + side + "_horizontal_component_ids"] for panel, polygon in panels if polygon.intersection(polygons[identity]).area > AREA_TOLERANCE_MM2)})
    result["groups"] = [group for group in result["groups"] if group["id"] not in originals] + horizontal + [group for group, _ in panels]
    for zone in list(audit["zones"]):
        if zone["system_id"] != system_id:
            continue
        groups = [group for group in horizontal if group["slab_zone_id"] == zone["id"]]
        if not groups:
            audit["zones"].remove(zone)
            continue
        region = unary_union([Polygon(group["outer"], group["holes"]) for group in groups])
        zone.update(component_ids=sorted(group["id"] for group in groups), footprint=_footprint(region), expected_area_mm2=region.area, expected_volume_mm3=sum(group["volume_mm3"] for group in groups), horizontal_replacement_profile=profile)
    assembly = next(row for row in audit["assemblies"] if row["id"] == system_id)
    net_horizontal = unary_union([Polygon(group["outer"], group["holes"]) for group in horizontal])
    projection = unary_union([net_horizontal, all_bands])
    original_projection = unary_union(list(polygons.values()))
    envelope_error = original_projection.symmetric_difference(projection).area
    assembly.update(component_ids=sorted(group["id"] for group in horizontal), component_tags={group["id"]: group["tag"] for group in horizontal}, footprint=_footprint(net_horizontal), expected_area_mm2=net_horizontal.area, expected_volume_mm3=sum(group["volume_mm3"] for group in horizontal), horizontal_replacement_profile=profile)
    replacement_audit = {"system_id": system_id, "profile": profile, "original_groups": original_groups, "original_assembly": original_assembly, "original_zones": original_zones, "replacements": replacements, "original_footprint": _footprint(original_projection), "final_projected_footprint": _footprint(projection), "expected_projected_area_mm2": original_projection.area, "envelope_difference_area_mm2": envelope_error, "riser_component_ids": sorted(group["id"] for group, _ in panels)}
    precision_record = next((row for row in audit.get("geometry_precision", []) if row["system_id"] == system_id), None)
    if precision_record is not None:
        replacement_audit.update(raw_original_groups=copy.deepcopy(precision_record["raw_original_groups"]), geometry_precision_mm=precision_record["grid_size_mm"])
    audit.setdefault("edge_aligned_replacements", []).append(replacement_audit)
    for system in audit["systems"]:
        if system["id"] == system_id:
            system.update(horizontal_replacement_profile=profile, prepared_horizontal_area_mm2=net_horizontal.area, prepared_projected_area_mm2=projection.area, expected_horizontal_volume_mm3=assembly["expected_volume_mm3"], expected_total_volume_mm3=assembly["expected_volume_mm3"] + sum(group["volume_mm3"] for group, _ in panels))
    if envelope_error > AREA_TOLERANCE_MM2:
        result["blockers"].append({"code": "slab_edge_riser_envelope_changed", "system_id": system_id, "area_mm2": envelope_error})
    policy_row.update(profile=profile, group_count=len(panels), connection_count=len(links), expected_volume_mm3=sum(group["volume_mm3"] for group, _ in panels), replacement_horizontal_count=len(horizontal))


def _prepare_step_risers(result, config, systems):
    """Fill approved internal step gaps with disjoint horizontal Z slices.

    Each original host-pair link remains independently auditable. At a three-
    way junction, slices merge active bridge masks and subtract actual plate
    material, so different-height connections share solids without overlaps.
    """
    if "step_risers" not in config and not any("step_risers" in system for system in systems.values()):
        return
    audit = result["slab_audit"]
    audit["step_risers"] = []
    audit["step_riser_policies"] = []
    audit["step_riser_slices"] = []
    horizontal = [group for group in result["groups"] if group.get("slab_role") in ROLES.values()]
    by_system = defaultdict(list)
    for group in horizontal:
        by_system[group["slab_system_id"]].append((group, Polygon(group["outer"], group["holes"])))
    assemblies = {assembly["id"]: assembly for assembly in audit["assemblies"]}
    def block(code, **detail):
        result["blockers"].append({"code": code, **detail})
    for system_id in sorted(systems):
        raw_policy = systems[system_id].get("step_risers", config.get("step_risers"))
        if raw_policy is None:
            continue
        row = {"system_id": system_id, "decision": _safe(raw_policy)}
        audit["step_riser_policies"].append(row)
        if not isinstance(raw_policy, dict) or not isinstance(raw_policy.get("enabled"), bool):
            block("slab_step_riser_policy_invalid", system_id=system_id, detail="step_risers requires an explicit boolean enabled")
            row["status"] = "invalid"
            continue
        enabled = raw_policy["enabled"]
        width = None
        if enabled:
            try:
                width = _number(raw_policy.get("thickness_mm"), "step_risers.thickness_mm", positive=True)
                if width is None:
                    raise ValueError("An enabled step riser requires an explicit positive thickness_mm")
            except (TypeError, ValueError) as exc:
                block("slab_step_riser_thickness_required", system_id=system_id, detail=str(exc))
                row["status"] = "invalid"
                continue
        row.update(status="enabled" if enabled else "disabled", horizontal_thickness_mm=width)
        items = sorted(by_system[system_id], key=lambda item: item[0]["id"])
        if system_id not in assemblies or not items:
            continue
        profile = raw_policy.get("profile", "centered_gap_only")
        if profile not in {"centered_gap_only", "edge_aligned_full_depth"}:
            block("slab_step_riser_profile_invalid", system_id=system_id, profile=profile)
            row["status"] = "invalid"
            continue
        if profile == "edge_aligned_full_depth":
            row["profile"] = profile
            if raw_policy.get("geometry_precision_mm") is not None:
                try:
                    grid = _number(raw_policy["geometry_precision_mm"], "geometry_precision_mm", positive=True)
                    if grid > .1:
                        raise ValueError("geometry_precision_mm must not exceed 0.1 mm; larger geometry edits need a separate reviewed operation")
                    items = _normalize_edge_precision_system(result, system_id, grid, items)
                    row["geometry_precision_mm"] = grid
                except (TypeError, ValueError) as exc:
                    block("slab_geometry_precision_invalid", system_id=system_id, detail=str(exc))
                    continue
            if enabled:
                _prepare_edge_aligned_system(result, system_id, raw_policy, width, items, row)
            continue
        net = _geometry_from_footprint(assemblies[system_id]["footprint"])
        links = []
        tree = STRtree([polygon for _, polygon in items])
        for first_index, (first, polygon) in enumerate(items):
            for second_index in sorted(int(index) for index in tree.query(polygon)):
                if second_index <= first_index:
                    continue
                second, other = items[second_index]
                if abs(first["base_z_mm"] - second["base_z_mm"]) <= HEIGHT_TOLERANCE_MM and abs(first["top_z_mm"] - second["top_z_mm"]) <= HEIGHT_TOLERANCE_MM:
                    continue
                shared = unary_union(_line_parts(polygon.boundary.intersection(other.boundary)))
                if shared.geom_type == "MultiLineString":
                    shared = linemerge(shared)
                if shared.length <= HEIGHT_TOLERANCE_MM:
                    continue
                lower, upper = sorted([first, second], key=lambda group: (group["top_z_mm"], group["base_z_mm"], group["id"]))
                lower_polygon = polygon if lower["id"] == first["id"] else other
                upper_polygon = other if lower["id"] == first["id"] else polygon
                bottom = lower["top_z_mm"]
                top = max(bottom, upper["base_z_mm"])
                gap = top - bottom
                identity = "slab-step-link:" + _hash({"system": system_id, "hosts": [lower["id"], upper["id"]], "shared": _line_data(shared), "width": width})[:24]
                link_audit = {"id": identity, "system_id": system_id, "host_slab_role": lower["slab_role"], "component_ids": [], "lower_horizontal_component_ids": [lower["id"]], "upper_horizontal_component_ids": [upper["id"]], "host_tags": {"lower": lower["tag"], "upper": upper["tag"]}, "shared_boundary": _line_data(shared), "shared_boundary_length_mm": shared.length, "base_z_mm": bottom, "top_z_mm": top, "gap_height_mm": gap, "horizontal_thickness_mm": width, "tag": lower["tag"], "source_layer": lower["source_layer"], "tag_kind": lower["tag_kind"], "expected_volume_mm3": 0.0, "coverage_slices": [], **_riser_trace([lower], [upper])}
                audit["step_risers"].append(link_audit)
                if gap <= HEIGHT_TOLERANCE_MM:
                    link_audit.update(status="already_continuous", vertical_contact_interval_mm=[max(lower["base_z_mm"], upper["base_z_mm"]), min(lower["top_z_mm"], upper["top_z_mm"])], requested_footprint={"parts": []}, footprint={"parts": []}, lower_contact_footprint={"parts": []}, upper_contact_footprint={"parts": []})
                    continue
                if not enabled:
                    link_audit.update(status="disabled", requested_footprint={"parts": []}, footprint={"parts": []}, lower_contact_footprint={"parts": []}, upper_contact_footprint={"parts": []})
                    continue
                requested = shared.buffer(width / 2, cap_style="flat", join_style="mitre").intersection(net)
                link_audit.update(status="pending", requested_footprint=_footprint(requested), footprint=_footprint(requested), requested_area_mm2=requested.area, buffer_policy={"distance_mm": width / 2, "cap_style": "flat", "join_style": "mitre", "clip": "system_net_extent"})
                if requested.area <= AREA_TOLERANCE_MM2:
                    block("slab_step_riser_empty_band", system_id=system_id, connection_id=identity)
                    link_audit["status"] = "blocked"
                    continue
                links.append({"audit": link_audit, "requested": requested, "shared": shared, "lower": lower, "upper": upper, "lower_polygon": lower_polygon, "upper_polygon": upper_polygon})
        if not links:
            continue
        events = sorted({value for link in links for value in [link["audit"]["base_z_mm"], link["audit"]["top_z_mm"]]} | {value for group, _ in items for value in [group["base_z_mm"], group["top_z_mm"]]})
        created = []
        slices = []
        for bottom, top in zip(events, events[1:]):
            if top - bottom <= HEIGHT_TOLERANCE_MM:
                continue
            midpoint = (bottom + top) / 2
            active = [link for link in links if link["audit"]["base_z_mm"] < midpoint < link["audit"]["top_z_mm"]]
            if not active:
                continue
            wanted = unary_union([link["requested"] for link in active])
            occupying = [(group, polygon) for group, polygon in items if group["base_z_mm"] < midpoint < group["top_z_mm"]]
            occupied = unary_union([polygon for _, polygon in occupying])
            material = wanted.difference(occupied)
            slice_groups = []
            for polygon in sorted(_parts(material), key=lambda part: _hash(_polygon_data(part))):
                if polygon.area <= AREA_TOLERANCE_MM2:
                    continue
                contributions = [link for link in active if polygon.intersection(link["requested"]).area > AREA_TOLERANCE_MM2]
                lower_groups = {link["lower"]["id"]: link["lower"] for link in contributions}
                upper_groups = {link["upper"]["id"]: link["upper"] for link in contributions}
                cutters = [group for group, boundary in occupying if boundary.intersection(wanted).area > AREA_TOLERANCE_MM2 and boundary.distance(polygon) <= HEIGHT_TOLERANCE_MM]
                shape = _polygon_data(polygon)
                identity = "slab-step-riser:" + _hash({"system": system_id, "base": bottom, "top": top, "width": width, "shape": shape})[:24]
                tag_geometries, tag_hosts = defaultdict(list), defaultdict(dict)
                for link in contributions:
                    for side in ["lower", "upper"]:
                        host = link[side]
                        key = (host["tag"], host["source_layer"], host["tag_kind"])
                        contribution = polygon.intersection(link[side + "_polygon"])
                        if contribution.area > AREA_TOLERANCE_MM2:
                            tag_geometries[key].append(contribution)
                            tag_hosts[key][host["id"]] = host
                scores = [{"tag": key[0], "source_layer": key[1], "tag_kind": key[2], "area_mm2": unary_union(parts).area, "host_component_ids": sorted(tag_hosts[key]), "source_entity_ids": sorted({source for host in tag_hosts[key].values() for source in host["source_entity_ids"]})} for key, parts in tag_geometries.items()]
                scores.sort(key=lambda score: (-score["area_mm2"], score["tag"], score["source_layer"], score["tag_kind"]))
                if not scores:
                    block("slab_step_riser_primary_tag_unresolved", system_id=system_id, component_id=identity)
                    continue
                primary = scores[0]
                primary_key = (primary["tag"], primary["source_layer"], primary["tag_kind"])
                template = tag_hosts[primary_key][sorted(tag_hosts[primary_key])[0]]
                group = {"id": identity, "name": identity, "category": "slab", "semantic_role": "slab_step_riser", "slab_role": "slab_step_riser", "host_slab_role": template["slab_role"], "slab_system_id": system_id, "host_system_id": system_id, "slab_zone_id": f"{system_id}:step_risers", "mode": "solid_extrusion", "base_z_mm": bottom, "top_z_mm": top, "height_mm": top - bottom, "horizontal_thickness_mm": width, "area_mm2": polygon.area, "volume_mm3": polygon.area * (top - bottom), "tag": template["tag"], "source_layer": template["source_layer"], "tag_kind": template["tag_kind"], "primary_tag_evidence": {"method": "largest_union_horizontal_host_footprint_in_riser_part", "tie_breaker": "lexicographic_tag_source_layer_kind", "candidates": scores}, "floor_datum_m": template.get("floor_datum_m"), "lower_horizontal_component_ids": sorted(lower_groups), "upper_horizontal_component_ids": sorted(upper_groups), "riser_audit_ids": sorted(link["audit"]["id"] for link in contributions), "evidence": {"method": "approved_step_riser_z_event_overlay", "decision": _safe(raw_policy), "buffer_policy": {"distance_mm": width / 2, "cap_style": "flat", "join_style": "mitre"}, "horizontal_material_cut_ids": sorted(group["id"] for group in cutters)}, **shape, **_riser_trace(list(lower_groups.values()), list(upper_groups.values()), cutters)}
                result["groups"].append(group)
                created.append((group, polygon))
                slice_groups.append((group, polygon))
            slice_geometry = unary_union([polygon for _, polygon in slice_groups])
            slice_record = {"system_id": system_id, "base_z_mm": bottom, "top_z_mm": top, "component_ids": [group["id"] for group, _ in slice_groups], "footprint": _footprint(material), "expected_area_mm2": material.area, "expected_volume_mm3": material.area * (top - bottom), "active_connection_ids": sorted(link["audit"]["id"] for link in active)}
            audit["step_riser_slices"].append(slice_record)
            slices.append({"base": bottom, "top": top, "groups": slice_groups, "geometry": slice_geometry, "occupied": occupied})
            for link in active:
                required = link["requested"].difference(occupied)
                relevant = [group["id"] for group, polygon in slice_groups if polygon.intersection(required).area > AREA_TOLERANCE_MM2]
                uncovered = required.difference(slice_geometry)
                link["audit"]["coverage_slices"].append({"base_z_mm": bottom, "top_z_mm": top, "footprint": _footprint(required), "existing_horizontal_footprint": _footprint(link["requested"].intersection(occupied)), "component_ids": sorted(relevant), "expected_area_mm2": required.area, "expected_volume_mm3": required.area * (top - bottom), "uncovered_area_mm2": uncovered.area})
                link["audit"]["expected_volume_mm3"] += required.area * (top - bottom)
                if uncovered.area > AREA_TOLERANCE_MM2:
                    block("slab_step_riser_gap_uncovered", system_id=system_id, connection_id=link["audit"]["id"], base_z_mm=bottom, top_z_mm=top, area_mm2=uncovered.area)
        for link in links:
            row = link["audit"]
            row["component_ids"] = sorted({identity for interval in row["coverage_slices"] for identity in interval["component_ids"]})
            row["component_tags"] = {group["id"]: group["tag"] for group, _ in created if group["id"] in row["component_ids"]}
            lower_face = unary_union([interval["geometry"] for interval in slices if abs(interval["base"] - row["base_z_mm"]) <= HEIGHT_TOLERANCE_MM])
            upper_face = unary_union([interval["geometry"] for interval in slices if abs(interval["top"] - row["top_z_mm"]) <= HEIGHT_TOLERANCE_MM])
            lower_contact = lower_face.intersection(link["lower_polygon"]).intersection(link["requested"])
            upper_contact = upper_face.intersection(link["upper_polygon"]).intersection(link["requested"])
            lower_missing = link["shared"].difference(lower_contact.buffer(HEIGHT_TOLERANCE_MM)).length
            upper_missing = link["shared"].difference(upper_contact.buffer(HEIGHT_TOLERANCE_MM)).length
            row.update(lower_contact_footprint=_footprint(lower_contact), upper_contact_footprint=_footprint(upper_contact), lower_contact_area_mm2=lower_contact.area, upper_contact_area_mm2=upper_contact.area, lower_uncovered_boundary_length_mm=lower_missing, upper_uncovered_boundary_length_mm=upper_missing)
            valid = bool(row["component_ids"]) and lower_contact.area > AREA_TOLERANCE_MM2 and upper_contact.area > AREA_TOLERANCE_MM2 and lower_missing <= HEIGHT_TOLERANCE_MM and upper_missing <= HEIGHT_TOLERANCE_MM and all(interval["uncovered_area_mm2"] <= AREA_TOLERANCE_MM2 for interval in row["coverage_slices"])
            row["status"] = "bridged_gap" if valid else "blocked"
            if not valid:
                block("slab_step_riser_host_contact_incomplete", system_id=system_id, connection_id=row["id"], lower_missing_length_mm=lower_missing, upper_missing_length_mm=upper_missing)
        row = audit["step_riser_policies"][-1]
        row.update(group_count=len(created), expected_volume_mm3=sum(group["volume_mm3"] for group, _ in created), positive_gap_connections=len(links))
    audit["step_riser_count"] = sum(group.get("slab_role") == "slab_step_riser" for group in result["groups"])


def _prepare_slabs_prisms(document: dict, slab_decisions: dict) -> dict:
    """Overlay reviewed general/drop slabs and targeted through-openings.

    `slab_decisions` is the `slabs` object (a {slabs:...} wrapper also works).
    A uniform shift moves both faces by shift_z_mm; explicit_bottom uses top
    and bottom (or bottom plus explicit thickness) and verifies consistency.
    The returned audit includes complete candidate 2D regions even when sizes
    are absent, while material groups contain only fully resolved sections.
    tag_binding may select an evidenced original DWG source tag; otherwise the
    existing per-system/global derived_tag fallback remains available.
    """
    config = slab_decisions.get("slabs", slab_decisions) if isinstance(slab_decisions, dict) else None
    invalid_config = not isinstance(config, dict)
    if invalid_config:
        config = {}
    result = {"groups": [], "tag_mapping": [], "blockers": [], "warnings": [], "slab_audit": {"schema": "dwg-slab-audit-v1", "assemblies": [], "zones": [], "openings": [], "systems": [], "unresolved_overlaps": [], "accepted_joins": [], "source_tag_policy": config.get("source_tag_policy", "effective"), "decisions_digest": _hash(config), "tolerances": {"area_mm2": AREA_TOLERANCE_MM2, "height_mm": HEIGHT_TOLERANCE_MM}}}
    audit = result["slab_audit"]
    if invalid_config:
        result["blockers"].append({"code": "slab_decisions_must_be_object"})
        audit.update(enabled=False, all_pass=False)
        return result
    if "enabled" in config and not isinstance(config["enabled"], bool):
        result["blockers"].append({"code": "slab_enabled_must_be_boolean"})
    if not config.get("enabled"):
        audit.update(enabled=False, all_pass=not result["blockers"])
        return result
    audit["enabled"] = True
    def block(code, **detail):
        result["blockers"].append({"code": code, **detail})
    def records(value, scope):
        if value is None:
            return []
        if not isinstance(value, list):
            block("slab_records_must_be_list", scope=scope)
            return []
        valid = []
        for item in value:
            if isinstance(item, dict):
                valid.append(item)
            else:
                block("slab_record_must_be_object", scope=scope)
        return valid
    if config.get("confirmed_scope") is not True:
        block("slab_scope_requires_confirmation")
    policy = config.get("source_tag_policy", "effective")
    if policy not in {"effective", "original"}:
        block("slab_source_tag_policy_invalid", policy=policy)
    global_tag = None
    if config.get("derived_tag") is not None:
        try:
            global_tag = _derived_tag(config["derived_tag"])
        except ValueError as exc:
            block("slab_derived_tag_invalid", scope="global", detail=str(exc))
    try:
        audit["floor_datum_m"] = _number(config.get("floor_datum_m"), "floor_datum_m")
    except (TypeError, ValueError) as exc:
        block("slab_floor_datum_invalid", detail=str(exc))
        audit["floor_datum_m"] = None
    index = {str(entity.get("entity_id") or entity.get("id")): entity for key in ["entities", "resolved_instances"] for entity in document.get(key, []) if entity.get("entity_id") or entity.get("id")}
    missing_sources = set()
    def provenance(records):
        roles = defaultdict(set)
        for item, geometry_role, annotation_role in records:
            for field, role in [("source_entity_ids", geometry_role), ("annotation_source_ids", annotation_role)]:
                try:
                    identities = _ids(item.get(field))
                except ValueError as exc:
                    block("slab_source_id_list_invalid", role=role, detail=str(exc))
                    identities = []
                for identity in identities:
                    roles[role].add(identity)
        requested = set().union(*roles.values()) if roles else set()
        missing_sources.update(requested - index.keys())
        valid = sorted(requested & index.keys())
        annotations = sorted({identity for role, values in roles.items() if "annotation" in role for identity in values if identity in index})
        layers, handles, sources = set(), set(), []
        for identity in valid:
            entity = index[identity]
            original = str(entity.get("original_layer", entity.get("layer", "0")))
            effective = str(entity.get("effective_layer", entity.get("layer", original)))
            layer = _source_layer(entity, policy)
            layers.add(layer)
            handle = entity.get("source_handle") or entity.get("handle")
            if handle:
                handles.add(str(handle))
            sources.append({"entity_id": identity, "roles": sorted(role for role, values in roles.items() if identity in values), "source_layer": layer, "original_layer": original, "effective_layer": effective, "instance_path": entity.get("instance_path") or "", "source_handle": handle, "source_file": entity.get("source_file")})
        return {"source_entity_ids": valid, "annotation_source_ids": annotations, "source_layers": sorted(layers), "source_handles": sorted(handles), "sources": sources, "unresolved_source_entity_ids": sorted(requested - index.keys())}
    systems = config.get("systems")
    if not isinstance(systems, list) or not systems:
        block("slab_systems_required")
        return result
    valid_systems = {}
    for raw in systems:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw.get("id"):
            block("slab_system_id_required")
            continue
        system_id = raw["id"]
        if system_id in valid_systems:
            block("slab_system_id_duplicate", system_id=system_id)
            continue
        if raw.get("role") not in ROLES:
            block("slab_system_role_invalid", system_id=system_id, role=raw.get("role"))
        valid_systems[system_id] = raw
    system_tags = {}
    system_tag_kinds = {}
    tags_to_layers, layers_to_tags, tags_to_kinds = {}, {}, {}
    def register_tag(mapping, kind):
        tag_name, layer_name = mapping["tag"], mapping["source_layer"]
        if (tag_name in tags_to_layers and tags_to_layers[tag_name] != layer_name) or (layer_name in layers_to_tags and layers_to_tags[layer_name] != tag_name) or (tag_name in tags_to_kinds and tags_to_kinds[tag_name] != kind):
            raise ValueError("Conflicting tag/source_layer mapping across slab systems or zones")
        tags_to_layers[tag_name], layers_to_tags[layer_name] = layer_name, tag_name
        tags_to_kinds[tag_name] = kind
        if mapping not in result["tag_mapping"]:
            result["tag_mapping"].append(mapping)
    for system_id in sorted(valid_systems):
        raw = valid_systems[system_id]
        try:
            if raw.get("tag_binding") is not None:
                binding = raw["tag_binding"]
            elif raw.get("derived_tag") is not None:
                binding = raw["derived_tag"]
            else:
                binding = config.get("tag_binding")
            if binding is None:
                mapping = global_tag
                kind = "derived"
            elif isinstance(binding, dict) and binding.get("kind") == "derived":
                mapping = _derived_tag(binding)
                kind = "derived"
            else:
                mapping = _source_tag(binding, document, raw, index, policy)
                kind = "source"
            if mapping is None:
                raise ValueError("System requires a tag_binding or an inherited/per-system derived_tag")
            register_tag(mapping, kind)
            system_tags[system_id] = mapping
            system_tag_kinds[system_id] = kind
        except ValueError as exc:
            code = "slab_tag_binding_invalid" if raw.get("tag_binding") is not None or config.get("tag_binding") is not None else "slab_derived_tag_invalid"
            block(code, system_id=system_id, detail=str(exc))
            system_tags[system_id] = None
            system_tag_kinds[system_id] = None
    result["tag_mapping"].sort(key=lambda mapping: (mapping["tag"], mapping["source_layer"]))
    openings = defaultdict(list)
    for owner in sorted(valid_systems):
        for raw in records(valid_systems[owner].get("openings"), f"{owner}:openings"):
            opening_id = str(raw.get("id") or "")
            target = raw.get("applies_to_system_id")
            row = {"id": opening_id, "owner_system_id": owner, "system_id": target, "applies_to_system_id": target, "footprint": _safe(raw.get("footprint")), "status": "unresolved"}
            audit["openings"].append(row)
            try:
                row.update(provenance([(raw, "opening_boundary", "opening_annotation")]))
                polygon = _polygon(raw.get("footprint"))
                row["footprint"] = _footprint(polygon)
                row["area_mm2"] = polygon.area
                if not opening_id:
                    raise ValueError("Opening id is required")
                if target not in valid_systems:
                    raise ValueError("applies_to_system_id must identify one reviewed slab system")
                if any(item["id"] == opening_id for item in openings[target]):
                    raise ValueError("Opening id must be unique within its target system")
                row["status"] = "targeted_through_opening"
                openings[target].append({"id": opening_id, "raw": raw, "polygon": polygon, "row": row})
            except (KeyError, TypeError, ValueError) as exc:
                block("slab_opening_invalid", system_id=owner, opening_id=opening_id, detail=str(exc))
                row["error"] = str(exc)
    for system_id in sorted(valid_systems):
        system = valid_systems[system_id]
        role = ROLES.get(system.get("role"))
        mapping = system_tags[system_id]
        derived_tag = mapping["tag"] if mapping else None
        derived_layer = mapping["source_layer"] if mapping else None
        tag_kind = system_tag_kinds[system_id]
        system_audit = {"id": system_id, "role": role, "system_role": system.get("role"), "tag": derived_tag, "source_layer": derived_layer, "tag_kind": tag_kind, "plan_audit": {"extent": _safe(system.get("extent")), "zones": [], "openings": [item["row"] for item in openings[system_id]]}, "evidence": _safe(system.get("evidence")), "status": "unresolved"}
        audit["systems"].append(system_audit)
        try:
            extent = _polygon(system.get("extent"))
            source = provenance([(system, "slab_boundary", "slab_annotation")])
            if not source["source_entity_ids"]:
                block("slab_system_source_required", system_id=system_id)
            system_audit.update(source)
            system_audit["plan_audit"]["extent"] = _footprint(extent)
            system_audit["plan_audit"]["gross_area_mm2"] = extent.area
        except (KeyError, TypeError, ValueError) as exc:
            block("slab_extent_invalid", system_id=system_id, detail=str(exc))
            system_audit["error"] = str(exc)
            continue
        targeted = openings[system_id]
        clipped_openings = []
        for opening in targeted:
            intersection = opening["polygon"].intersection(extent)
            opening["row"]["intersection_area_mm2"] = intersection.area
            opening["row"]["outside_extent_area_mm2"] = opening["polygon"].difference(extent).area
            if intersection.area <= AREA_TOLERANCE_MM2:
                block("slab_opening_misses_target", system_id=system_id, opening_id=opening["id"])
            clipped_openings.append(intersection)
        cut_union = unary_union(clipped_openings)
        net = extent.difference(cut_union)
        system_audit["plan_audit"].update(net_extent=_footprint(net), opening_removed_area_mm2=extent.area - net.area, net_area_mm2=net.area)
        if net.area <= AREA_TOLERANCE_MM2:
            block("slab_system_empty_after_openings", system_id=system_id)
        zones = []
        zone_ids = set()
        for raw in sorted(records(system.get("zones"), f"{system_id}:zones"), key=lambda item: str(item.get("id", ""))):
            identity = str(raw.get("id") or "")
            row = {"id": identity, "footprint": _safe(raw.get("footprint")), "status": "unresolved", "method": raw.get("method", "constant_thickness")}
            system_audit["plan_audit"]["zones"].append(row)
            try:
                if not identity or identity in zone_ids:
                    raise ValueError("Zone id must be nonempty and unique in its system")
                zone_ids.add(identity)
                polygon = _polygon(raw.get("footprint"))
                row["footprint"] = _footprint(polygon)
                row["area_mm2"] = polygon.area
                outside = polygon.difference(extent).area
                row["outside_extent_area_mm2"] = outside
                if outside > AREA_TOLERANCE_MM2:
                    block("slab_zone_outside_extent", system_id=system_id, zone_id=identity, area_mm2=outside)
                effective = polygon.intersection(net)
                row["effective_footprint"] = _footprint(effective)
                profile = _profile(system, raw)
                row.update(profile)
                if profile.get("thickness_provenance"):
                    audit.setdefault("default_assumptions", []).append({"system_id": system_id, "zone_id": identity,
                        "field": "thickness_mm", **profile["thickness_provenance"]})
                zone_mapping, zone_kind = mapping, tag_kind
                binding = raw.get("tag_binding", raw.get("derived_tag"))
                if binding is not None:
                    if isinstance(binding, dict) and binding.get("kind") == "derived":
                        zone_mapping, zone_kind = _derived_tag(binding), "derived"
                    else:
                        zone_mapping, zone_kind = _source_tag(binding, document, raw, index, policy), "source"
                    register_tag(zone_mapping, zone_kind)
                row.update(tag=zone_mapping["tag"] if zone_mapping else None, source_layer=zone_mapping["source_layer"] if zone_mapping else None, tag_kind=zone_kind)
                row.update(provenance([(raw, "zone_boundary", "zone_annotation")]))
                row["status"] = "missing_vertical_parameters" if profile["missing_parameters"] else "dimensioned"
                if profile["missing_parameters"]:
                    block("slab_zone_dimensions_required", system_id=system_id, zone_id=identity, parameters=profile["missing_parameters"])
                zones.append({"id": identity, "raw": raw, "polygon": effective, "profile": profile, "row": row, "mapping": zone_mapping, "tag_kind": zone_kind})
            except (KeyError, TypeError, ValueError) as exc:
                block("slab_zone_invalid", system_id=system_id, zone_id=identity, detail=str(exc))
                row["error"] = str(exc)
                # A valid XY zone with invalid dimensions must still reserve its
                # region. Falling back to the default slab would hide the error.
                try:
                    polygon = _polygon(raw.get("footprint")).intersection(net)
                    zones.append({"id": identity, "raw": raw, "polygon": polygon, "profile": None, "row": row})
                except (KeyError, TypeError, ValueError):
                    pass
        conflicts = []
        for first_index, first in enumerate(zones):
            for second in zones[first_index + 1:]:
                intersection = first["polygon"].intersection(second["polygon"])
                if intersection.area <= AREA_TOLERANCE_MM2:
                    continue
                one, two = first["profile"], second["profile"]
                same = one and two and not one["missing_parameters"] and not two["missing_parameters"] and abs(one["top_z_mm"] - two["top_z_mm"]) <= HEIGHT_TOLERANCE_MM and abs(one["thickness_mm"] - two["thickness_mm"]) <= HEIGHT_TOLERANCE_MM and first.get("mapping") == second.get("mapping") and first.get("tag_kind") == second.get("tag_kind")
                if same:
                    result["warnings"].append({"code": "slab_consistent_zones_merged", "system_id": system_id, "zone_ids": [first["id"], second["id"]], "overlap_area_mm2": intersection.area})
                else:
                    conflicts.append(intersection)
                    block("slab_zone_sections_conflict", system_id=system_id, zone_ids=[first["id"], second["id"]], overlap_footprint=_footprint(intersection), overlap_area_mm2=intersection.area)
        conflict_union = unary_union(conflicts)
        zone_union = unary_union([zone["polygon"] for zone in zones])
        residual = net.difference(zone_union)
        buckets = defaultdict(list)
        for zone in zones:
            profile = zone["profile"]
            if profile and not profile["missing_parameters"]:
                zone_mapping = zone["mapping"]
                if zone_mapping is not None:
                    buckets[(profile["base_z_mm"], profile["top_z_mm"], profile["thickness_mm"], zone_mapping["tag"], zone_mapping["source_layer"], zone["tag_kind"])].append(zone)
        sections = []
        if residual.area > AREA_TOLERANCE_MM2:
            base_row = {"id": f"{system_id}:base", "footprint": _footprint(residual), "status": "unresolved"}
            system_audit["plan_audit"]["base_region"] = base_row
            try:
                profile = _profile(system)
                base_row.update(profile)
                if profile.get("thickness_provenance"):
                    audit.setdefault("default_assumptions", []).append({"system_id": system_id, "zone_id": f"{system_id}:base",
                        "field": "thickness_mm", **profile["thickness_provenance"]})
                base_row["status"] = "missing_vertical_parameters" if profile["missing_parameters"] else "dimensioned"
                if profile["missing_parameters"]:
                    block("slab_default_dimensions_required", system_id=system_id, parameters=profile["missing_parameters"], unresolved_area_mm2=residual.area)
                else:
                    sections.append((f"{system_id}:base", residual, profile, [], mapping, tag_kind))
            except (TypeError, ValueError) as exc:
                block("slab_default_dimensions_invalid", system_id=system_id, detail=str(exc))
                base_row["error"] = str(exc)
        for profile_key in sorted(buckets):
            selected = buckets[profile_key]
            region = unary_union([zone["polygon"] for zone in selected]).difference(conflict_union)
            identity = f"{system_id}:zones:" + _hash(sorted(zone["id"] for zone in selected))[:16]
            sections.append((identity, region, selected[0]["profile"], selected, selected[0]["mapping"], selected[0]["tag_kind"]))
        system_groups = []
        for zone_identity, geometry, profile, selected, section_mapping, section_kind in sections:
            derived_tag = section_mapping["tag"] if section_mapping else None
            derived_layer = section_mapping["source_layer"] if section_mapping else None
            # The ordinary plate is also bounded by the zone subtraction. Keep
            # those boundary source identities even though their elevation note
            # defines the neighbouring drop rather than this plate's section.
            modifiers = [({"source_entity_ids": zone["raw"].get("source_entity_ids", [])}, "zone_cut_boundary", "zone_cut_annotation") for zone in zones if zone not in selected]
            source_records = [(system, "slab_boundary", "slab_annotation")] + [(zone["raw"], "zone_boundary", "zone_annotation") for zone in selected] + modifiers + [(opening["raw"], "opening_boundary", "opening_annotation") for opening in targeted]
            trace = provenance(source_records)
            zone_group_ids = []
            for polygon in sorted(_parts(geometry), key=lambda item: _hash(_polygon_data(item))):
                shape = _polygon_data(polygon)
                identity = f"slab:{system_id}:" + _hash({"zone": zone_identity, "shape": shape, "base": profile["base_z_mm"], "top": profile["top_z_mm"]})[:24]
                if derived_tag is None or role is None:
                    continue
                group = {"id": identity, "name": identity, "category": "slab", "semantic_role": role, "slab_role": role, "slab_system_id": system_id, "slab_zone_id": zone_identity, "decision_zone_ids": sorted(zone["id"] for zone in selected), "mode": "solid_extrusion", "source_layer": derived_layer, "tag": derived_tag, "tag_kind": section_kind, **shape, "base_z_mm": profile["base_z_mm"], "top_z_mm": profile["top_z_mm"], "height_mm": profile["thickness_mm"], "thickness_mm": profile["thickness_mm"], "area_mm2": polygon.area, "volume_mm3": polygon.area * profile["thickness_mm"], "floor_datum_m": audit["floor_datum_m"], "evidence": {"method": profile["method"], "system_evidence": _safe(system.get("evidence")), "zone_evidence": [_safe(zone["raw"].get("evidence")) for zone in selected], "opening_ids": sorted(opening["id"] for opening in targeted), "coordinate_policy": "reviewed relative slab top/bottom in mm; floor datum recorded separately"}, **trace}
                relevant_zones = [zone for zone in selected if zone["polygon"].intersection(polygon).area > AREA_TOLERANCE_MM2]
                default_zones = [zone for zone in relevant_zones if zone["profile"].get("thickness_provenance")]
                assumption = default_zones[0]["profile"]["thickness_provenance"] if default_zones else profile.get("thickness_provenance") if not selected else None
                if assumption:
                    group["evidence"]["thickness_provenance"] = copy.deepcopy(assumption)
                    if selected:
                        group["evidence"]["thickness_provenance"].update(
                            default_applied_to_decision_zone_ids=sorted(zone["id"] for zone in default_zones),
                            companion_explicit_dimension_zone_ids=sorted(zone["id"] for zone in relevant_zones if zone not in default_zones))
                result["groups"].append(group)
                system_groups.append(group)
                zone_group_ids.append(identity)
            if geometry.area > AREA_TOLERANCE_MM2:
                audit["zones"].append({"id": zone_identity, "system_id": system_id, "role": role, "tag": derived_tag, "source_layer": derived_layer, "tag_kind": section_kind, "decision_zone_ids": sorted(zone["id"] for zone in selected), "component_ids": zone_group_ids, "base_z_mm": profile["base_z_mm"], "top_z_mm": profile["top_z_mm"], "thickness_mm": profile["thickness_mm"], "footprint": _footprint(geometry), "expected_area_mm2": geometry.area, "expected_volume_mm3": geometry.area * profile["thickness_mm"], **trace})
        prepared = unary_union([Polygon(group["outer"], group["holes"]) for group in system_groups])
        unresolved = net.difference(prepared)
        extra = prepared.difference(net)
        covered_area = prepared.area
        volume = sum(group["volume_mm3"] for group in system_groups)
        system_trace = provenance([(system, "slab_boundary", "slab_annotation")] + [(zone["raw"], "zone_boundary", "zone_annotation") for zone in zones] + [(opening["raw"], "opening_boundary", "opening_annotation") for opening in targeted])
        assembly = {"id": system_id, "system_id": system_id, "role": role, "system_role": system.get("role"), "tag": mapping["tag"] if mapping else None, "source_layer": mapping["source_layer"] if mapping else None, "tag_kind": tag_kind, "component_ids": [group["id"] for group in system_groups], "component_tags": {group["id"]: group["tag"] for group in system_groups}, "footprint": _footprint(net), "expected_area_mm2": net.area, "expected_volume_mm3": volume if unresolved.area <= AREA_TOLERANCE_MM2 else None, **system_trace}
        audit["assemblies"].append(assembly)
        system_audit.update(status="complete" if unresolved.area <= AREA_TOLERANCE_MM2 else "missing_material_regions", prepared_area_mm2=covered_area, expected_area_mm2=net.area, missing_area_mm2=unresolved.area, extra_area_mm2=extra.area, expected_volume_mm3=assembly["expected_volume_mm3"], unresolved_footprint=_footprint(unresolved))
        if extra.area > AREA_TOLERANCE_MM2:
            block("slab_material_outside_net_extent", system_id=system_id, area_mm2=extra.area)
        opening_obstructions = [{"id": opening["id"], "obstruction_area_mm2": prepared.intersection(opening["polygon"]).area} for opening in targeted]
        system_audit["opening_checks"] = opening_obstructions
        for obstruction in opening_obstructions:
            if obstruction["obstruction_area_mm2"] > AREA_TOLERANCE_MM2:
                block("slab_through_opening_obstructed", system_id=system_id, **obstruction)
    _prepare_step_risers(result, config, valid_systems)
    if missing_sources:
        block("slab_source_entities_missing", source_entity_ids=sorted(missing_sources))
    groups = result["groups"]
    polygons = [Polygon(group["outer"], group["holes"]) for group in groups]
    if polygons:
        tree = STRtree(polygons)
        for first_index, first_polygon in enumerate(polygons):
            first = groups[first_index]
            for second_index in sorted(int(index) for index in tree.query(first_polygon)):
                if second_index <= first_index:
                    continue
                second = groups[second_index]
                area = first_polygon.intersection(polygons[second_index]).area
                height = min(first["top_z_mm"], second["top_z_mm"]) - max(first["base_z_mm"], second["base_z_mm"])
                if area > AREA_TOLERANCE_MM2 and height > HEIGHT_TOLERANCE_MM:
                    overlap = {"component_ids": [first["id"], second["id"]], "system_ids": [first["slab_system_id"], second["slab_system_id"]], "area_mm2": area, "height_mm": height, "volume_mm3": area * height}
                    audit["unresolved_overlaps"].append(overlap)
                    block("slab_positive_volume_overlap", **overlap)
                elif first["slab_system_id"] != second["slab_system_id"] and area > AREA_TOLERANCE_MM2 and abs(height) <= HEIGHT_TOLERANCE_MM:
                    audit["accepted_joins"].append({"component_ids": [first["id"], second["id"]], "system_ids": [first["slab_system_id"], second["slab_system_id"]], "contact": "horizontal_face", "area_mm2": area})
                elif first["slab_system_id"] != second["slab_system_id"] and area <= AREA_TOLERANCE_MM2 and height > HEIGHT_TOLERANCE_MM:
                    shared_edge = first_polygon.boundary.intersection(polygons[second_index].boundary).length
                    if shared_edge > HEIGHT_TOLERANCE_MM:
                        audit["accepted_joins"].append({"component_ids": [first["id"], second["id"]], "system_ids": [first["slab_system_id"], second["slab_system_id"]], "contact": "vertical_face", "edge_length_mm": shared_edge, "contact_height_mm": height})
    result["groups"].sort(key=lambda group: group["id"])
    result["tag_mapping"].sort(key=lambda mapping: (mapping["tag"], mapping["source_layer"]))
    audit["counts"] = {"systems": len(audit["systems"]), "assemblies": len(audit["assemblies"]), "zones": len(audit["zones"]), "openings": len(audit["openings"]), "groups": len(result["groups"]), "blockers": len(result["blockers"])}
    audit["all_pass"] = not result["blockers"]
    return result


def prepare_slabs(document: dict, slab_decisions: dict, *, emit_assembly=True) -> dict:
    config = slab_decisions.get("slabs", slab_decisions) if isinstance(slab_decisions, dict) else None
    binding_audit = None
    if isinstance(config, dict) and config.get("annotation_bindings"):
        from .annotation_bindings import bind_structural_height_zones
        config, binding_audit = bind_structural_height_zones(document, config)
        slab_decisions = config
    result = _prepare_slabs_prisms(document, slab_decisions)
    if isinstance(config, dict) and config.get('required_assembly_mode') == 'whole_uniform_thickness':
        if config.get('assembly_mode', 'whole_uniform_thickness') != 'whole_uniform_thickness' or any(
                row.get('assembly_mode', config.get('assembly_mode', 'whole_uniform_thickness')) != 'whole_uniform_thickness'
                for row in config.get('systems', [])):
            result['blockers'].append({'code': 'fixed_standard_requires_whole_uniform_slab'})
        config = {**config, 'assembly_mode': 'whole_uniform_thickness'}
    if binding_audit is not None:
        result['annotation_binding_audit'] = binding_audit
        if not binding_audit['all_pass']:
            result['blockers'].extend(binding_audit['issues'])
    if emit_assembly:
        from .whole_slabs import assemble_whole_slabs
        if isinstance(config, dict):
            result = assemble_whole_slabs(document, config, result, _prepare_slabs_prisms)
    return result
