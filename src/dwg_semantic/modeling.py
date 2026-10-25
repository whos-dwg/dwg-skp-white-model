"""Prepare an auditable model, then write exactly that model with the native API.

Preparation is intentionally conservative. Layer names are provenance, not solid
geometry; unresolved contours remain visible references and block final export.
No semantic inference or geometric repair takes place during write_model.
"""
from __future__ import annotations

import ctypes as C
import base64
import hashlib
import json
import math
import os
import tempfile
import struct
import zlib
from html import escape
from collections import Counter, defaultdict
from pathlib import Path

SOLID_CATEGORIES = {"architectural_wall", "structural_wall", "column", "curtain", "curtain_accessory", "slab"}
CATEGORIES = SOLID_CATEGORIES | {"opening", "reference", "exclude"}


def _geometry():
    try:
        from shapely.geometry import LineString, Polygon
        from shapely.ops import polygonize, unary_union
        from shapely.geometry.polygon import orient
    except ImportError as exc:
        raise RuntimeError("Model preparation requires Shapely >= 2. Install the project modeling dependencies.") from exc
    return LineString, Polygon, polygonize, unary_union, orient


def _json_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _point(value):
    value = value.get("point", value) if isinstance(value, dict) else value
    point = [float(value["x"]), float(value["y"])] if isinstance(value, dict) else [float(value[0]), float(value[1])]
    if not all(math.isfinite(number) for number in point):
        raise ValueError("Nonfinite source coordinate")
    return point


def _parts(geometry):
    if geometry.geom_type == "Polygon":
        return [geometry] if not geometry.is_empty else []
    return [piece for child in getattr(geometry, "geoms", []) for piece in _parts(child)]


def _solid_overlaps(groups, footprints=None):
    """Positive-volume intersections, using a spatial index for large plans.

    Contact at a face/edge is permitted. Tiny coordinate noise is ignored using
    the same 0.01 mm vertical and 0.1 mm² area tolerances as opening QA.
    """
    if any(group.get("mode") == "boundary_mesh" for group in groups):
        from .whole_slabs import cells_of
        flat, owners = [], {}
        originals = {group["id"]: group for group in groups}
        from .mesh_union import encoded
        for group_number,group in enumerate(groups):
            for number, cell in enumerate(cells_of(group)):
                key = group["id"] + ":overlap-cell:" + str(number)
                if footprints is not None and group.get('mode')!='boundary_mesh':
                    cell={**cell,**encoded(footprints[group_number])}
                flat.append({**cell, "id": key})
                owners[key] = group["id"]
        combined = {}
        for overlap in _solid_overlaps(flat):
            pair = tuple(sorted({owners[key] for key in overlap["component_ids"]}))
            if len(pair) != 2:
                continue
            if pair not in combined:
                combined[pair] = {"component_ids": list(pair), "categories": [originals[key]["category"] for key in pair],
                                  "source_layers": [originals[key]["source_layer"] for key in pair],
                                  "overlap_area_mm2": 0, "overlap_height_mm": 0, "overlap_volume_mm3": 0,
                                  "intersection_slices":[]}
            row = combined[pair]
            row["overlap_area_mm2"] = max(row["overlap_area_mm2"], overlap["overlap_area_mm2"])
            row["overlap_height_mm"] = max(row["overlap_height_mm"], overlap["overlap_height_mm"])
            row["overlap_volume_mm3"] += overlap["overlap_volume_mm3"]
            row['intersection_slices'].extend(overlap['intersection_slices'])
        return list(combined.values())
    from shapely.strtree import STRtree
    _, Polygon, *_ = _geometry()
    footprints = footprints if footprints is not None else [Polygon(group["outer"], group.get("holes", [])) for group in groups]
    if not footprints:
        return []
    tree = STRtree(footprints)
    result = []
    for first_index, footprint in enumerate(footprints):
        first = groups[first_index]
        for second_index in sorted(int(index) for index in tree.query(footprint)):
            if second_index <= first_index:
                continue
            second = groups[second_index]
            overlap_height = min(first["base_z_mm"] + first["height_mm"], second["base_z_mm"] + second["height_mm"]) - max(first["base_z_mm"], second["base_z_mm"])
            if overlap_height <= .01:
                continue
            overlap_area = footprint.intersection(footprints[second_index]).area
            if overlap_area <= .1:
                continue
            from .mesh_union import encoded
            bottom=max(first['base_z_mm'],second['base_z_mm'])
            top=min(first['base_z_mm']+first['height_mm'],second['base_z_mm']+second['height_mm'])
            result.append({"component_ids": [first["id"], second["id"]], "categories": [first["category"], second["category"]], "source_layers": [first["source_layer"], second["source_layer"]], "overlap_area_mm2": round(overlap_area, 6), "overlap_height_mm": round(overlap_height, 6), "overlap_volume_mm3": round(overlap_area * overlap_height, 6),
                           'intersection_slices':[{'base_z_mm':bottom,'top_z_mm':top,
                                                  'footprint':encoded(footprint.intersection(footprints[second_index]))}]})
    return result


def _audit_footprint(value):
    _, Polygon, _, unary_union, _ = _geometry()
    parts = value.get("parts") if isinstance(value, dict) else None
    parts = parts if parts is not None else [value]
    polygons = [Polygon(part["outer"], part.get("holes", [])) for part in parts]
    if not polygons or any(not polygon.is_valid or polygon.area <= 0 for polygon in polygons):
        raise ValueError("Audit footprint must contain valid material polygons")
    return unary_union(polygons)


def _slab_role(group):
    return group.get("slab_role") or group.get("semantic_role")


def _edge_aligned(group):
    return group.get("profile") == "edge_aligned_full_depth"


def _optional_audit_footprint(value):
    """An explicit empty parts array represents no required material in a slice."""
    if isinstance(value, dict) and value.get("parts") == []:
        from shapely.geometry import GeometryCollection
        return GeometryCollection()
    return _audit_footprint(value)


def _precision_overlay_regions(record, slab_audit, stage="edge_overlay"):
    """Independently rebuild the single noded graph and its material owners."""
    from shapely import set_precision, union_all
    from shapely.geometry import MultiLineString
    from shapely.ops import linemerge
    _, _, polygonize, unary_union, _ = _geometry()
    overlay = record.get(stage)
    source_key = "normalized_original_groups" if stage == "source_overlay" else "source_noded_original_groups"
    originals = {group["id"]: group for group in record.get(source_key, record["normalized_original_groups"])}
    if overlay is None:
        return {identity: _audit_footprint(group) for identity, group in originals.items()}, {}
    if (overlay.get("algorithm") != "common_noded_planar_faces"
            or overlay.get("source_owner_rule") != "max_intersection_area_strictly_over_half_face_area_then_lexical_original_id"
            or overlay.get("band_attribution_rule") != "matching_lower_source_owner_and_intersection_area_strictly_over_half_face_area"
            or overlay.get("grid_size_mm") != record["grid_size_mm"]):
        raise ValueError("Unsupported common precision graph or attribution rule")
    if {group["id"]: group for group in overlay["input_original_groups"]} != originals:
        raise ValueError("Precision graph does not begin with the independently normalized original hosts")
    grid = record["grid_size_mm"]
    hosts = {identity: _audit_footprint(group) for identity, group in originals.items()}

    def shared_line_parts(geometry):
        if geometry.is_empty:
            return []
        if geometry.geom_type in {"LineString", "LinearRing"}:
            return [geometry] if geometry.length > 1e-6 else []
        return [line for child in getattr(geometry, "geoms", []) for line in shared_line_parts(child)]

    links = ({row["id"]: row for row in slab_audit["step_risers"] if row["system_id"] == record["system_id"] and _edge_aligned(row)}
             if stage == "edge_overlay" else {})
    inputs = overlay["input_requested_bands"]
    if len(inputs) != len(links) or {row["connection_id"] for row in inputs} != set(links):
        raise ValueError("Common precision graph must include every approved original edge band")
    bands, lower_ids = {}, {}
    for row in inputs:
        identity = row["connection_id"]
        link = links[identity]
        low, high = row["original_lower_component_id"], row["original_upper_component_id"]
        if link["original_lower_horizontal_component_ids"] != [low] or link["original_upper_horizontal_component_ids"] != [high]:
            raise ValueError("Precision input band changed its original lower or upper host")
        lower, upper = hosts[low], hosts[high]
        # A point touching the same two regions is not a shared panel edge.
        shared = unary_union(shared_line_parts(lower.boundary.intersection(upper.boundary)))
        if shared.geom_type == "MultiLineString":
            shared = linemerge(shared)
        raw_line = MultiLineString(link["raw_shared_boundary"]["lines"])
        # Compare without imposing a precision model on the QA predicate.
        if not set_precision(shared, 0).equals(raw_line):
            raise ValueError("Precision raw shared boundary does not match the original grid-normalized hosts")
        # Requested bands precede the one common graph. They are analytic
        # double-precision buffers of the already grid-normalized source line;
        # only the subsequent shared noding rounds their new intersections.
        expected = set_precision(shared, 0).buffer(link["horizontal_thickness_mm"], cap_style=2, join_style=2).intersection(lower)
        # Polygonal footprints encode RC material. A full-width buffer may
        # also touch an isolated point/line of the lower host; those contacts
        # carry no material and cannot be serialized as prism footprints.
        # Keep strict geometric equality for every polygon, including holes.
        expected = unary_union(_parts(expected))
        if not _audit_footprint(row["footprint"]).equals(expected) or not _audit_footprint(link["raw_requested_footprint"]).equals(expected):
            raise ValueError("Precision raw requested band differs from the full-width LOWER-side source band")
        bands[identity], lower_ids[identity] = _audit_footprint(row["footprint"]), low
    graph = union_all([region.boundary for region in [*hosts.values(), *bands.values()]], grid_size=grid)
    host_faces, band_faces = defaultdict(list), defaultdict(list)
    expected_faces = []
    for face in polygonize(graph):
        if face.area <= 1e-6:
            continue
        scores = sorted(((face.intersection(region).area, identity) for identity, region in hosts.items()), key=lambda item: (-item[0], item[1]))
        if not scores or scores[0][0] <= face.area / 2:
            continue
        owner = scores[0][1]
        active = sorted(identity for identity, region in bands.items() if lower_ids[identity] == owner and face.intersection(region).area > face.area / 2)
        expected_faces.append((face, owner, active))
        host_faces[owner].append(face)
        for identity in active:
            band_faces[identity].append(face)
    recorded = overlay["atomic_faces"]
    if len(recorded) != len(expected_faces):
        raise ValueError("Common precision graph atomic face count was altered")
    # Geometry normalization creates an orientation-independent key and avoids
    # accepting an area-equivalent face assigned to the wrong original host.
    actual_faces = {(_audit_footprint(row["footprint"]).normalize().wkb_hex, row["original_component_id"],
                     tuple(row["requested_connection_ids"])) for row in recorded}
    expected_keys = {(face.normalize().wkb_hex, owner, tuple(active)) for face, owner, active in expected_faces}
    if len(actual_faces) != len(recorded) or actual_faces != expected_keys:
        raise ValueError("Common precision graph material owners or band assignments are not reproducible")
    canonical_hosts = {identity: unary_union(host_faces[identity]) for identity in hosts}
    canonical_bands = {identity: unary_union(band_faces[identity]) for identity in bands}
    for name, key, expected in (("normalized_host_regions", "original_component_id", canonical_hosts),
                                ("normalized_requested_bands", "connection_id", canonical_bands)):
        rows = overlay[name]
        if len(rows) != len(expected) or {row[key] for row in rows} != set(expected):
            raise ValueError("Common precision graph lost a canonical host or band mapping")
        if any(not _optional_audit_footprint(row["footprint"]).equals(expected[row[key]]) for row in rows):
            raise ValueError("Common precision graph canonical regions do not equal the assigned atomic faces")
    return canonical_hosts, canonical_bands


def _slab_geometry_precision_audit(payload):
    """Recompute a declared precision model without altering saved geometry.

    A precision model is a reviewed preparation operation, never a tolerance
    used to excuse a native failure. Raw snapshots, normalized snapshots and
    every topology change remain available in the saved slab audit.
    """
    from shapely import set_precision
    from shapely.errors import GEOSException
    _, Polygon, _, unary_union, _ = _geometry()
    audit = payload.get("slab_audit", {})
    report = {"checks": [], "systems": []}

    def check(name, passed, detail):
        report["checks"].append({"check": name, "pass": bool(passed), "detail": detail})

    def exact(actual, expected):
        return actual.equals(expected)

    def number_matches(value, expected):
        return abs(_number(value, "precision evidence") - expected) <= max(1e-8, abs(expected) * 1e-10)

    def hole_changes_match(rows, raw_shape, clean):
        expected = []
        for part in _parts(raw_shape):
            for ring in part.interiors:
                hole = Polygon(ring)
                filled = hole.intersection(clean).area
                if filled > 0:
                    expected.append((hole, hole.area, filled))
        if len(rows) != len(expected):
            return False
        actual = {Polygon(row["ring"]).normalize().wkb_hex: row for row in rows}
        return len(actual) == len(expected) and all(hole.normalize().wkb_hex in actual
               and number_matches(actual[hole.normalize().wkb_hex]["area_mm2"], area)
               and number_matches(actual[hole.normalize().wkb_hex]["filled_area_mm2"], filled)
               for hole, area, filled in expected)

    try:
        records = audit.get("geometry_precision", [])
        if not isinstance(records, list):
            raise ValueError("Geometry precision records must be a list")
        by_system = {row["system_id"]: row for row in records}
        if len(by_system) != len(records):
            raise ValueError("A slab system requires exactly one precision record")
        replacements = {row["system_id"]: row for row in audit.get("edge_aligned_replacements", [])}
        declared = {group["slab_system_id"] for group in payload.get("groups", [])
                    if group.get("category") == "slab" and group.get("geometry_precision_mm") is not None}
        declared.update(row["system_id"] for row in replacements.values() if row.get("geometry_precision_mm") is not None)
        check("all normalized slab systems retain their raw precision evidence", declared <= set(by_system), sorted(declared - set(by_system)))
        for system, record in by_system.items():
            grid = _number(record["grid_size_mm"], "geometry precision grid", positive=True)
            # This opt-in operation is deliberately limited to submillimetre
            # cleanup. Larger dimensional changes need a separate operation.
            if grid > .1:
                raise ValueError("Geometry precision grid cannot exceed 0.1 mm")
            if (record.get("algorithm") != "shapely.set_precision" or record.get("mode") != "valid_output"
                    or record.get("overlay_precision_propagation") is not True):
                raise ValueError("Unsupported or unpropagated slab precision algorithm")
            bound = grid / math.sqrt(2)
            check("precision displacement bound is the declared XY grid rounding bound",
                  number_matches(record["max_allowed_vertex_displacement_mm"], bound)
                  and number_matches(record["max_boundary_change_mm"], grid), system)
            raw_list, normalized_list = record["raw_original_groups"], record["normalized_original_groups"]
            raw = {group["id"]: group for group in raw_list}
            normalized = {group["id"]: group for group in normalized_list}
            if len(raw) != len(raw_list) or not raw or len(normalized) != len(normalized_list):
                raise ValueError("Precision snapshots require distinct raw and normalized component IDs")
            if any(group.get("category") != "slab" or group.get("slab_system_id") != system
                   or _slab_role(group) not in {"floor_slab", "overhead_slab"} for group in [*raw_list, *normalized_list]):
                raise ValueError("Precision snapshots must be horizontal slabs in their declared system")
            rows = record["components"]
            if len(rows) != len(raw) or {row["raw_component_id"] for row in rows} != set(raw):
                raise ValueError("Every raw slab requires one precision transformation record")
            raw_shapes = {identity: _audit_footprint(group) for identity, group in raw.items()}
            raw_envelope = unary_union(list(raw_shapes.values()))
            check("precision raw assembly binds the complete original material domain",
                  exact(raw_envelope, _audit_footprint(record["raw_assembly"]["footprint"]))
                  and set(record["raw_assembly"]["component_ids"]) == set(raw), system)
            source_domain_error = raw_envelope.symmetric_difference(_audit_footprint(record["source_assembly"]["footprint"])).area
            check("precision source assembly difference remains explicit and numerically bounded",
                  source_domain_error <= 1e-6 and number_matches(record["source_domain_difference_area_mm2"], source_domain_error),
                  {"system_id": system, "source_domain_difference_area_mm2": source_domain_error})
            zone_ids = []
            for zone in record["raw_zones"]:
                ids = zone["component_ids"]
                if any(identity not in raw for identity in ids):
                    raise ValueError("Raw precision zone references an unknown source host")
                zone_ids.extend(ids)
                check("precision raw zones preserve original footprint and vertical section",
                      exact(unary_union([raw_shapes[identity] for identity in ids]), _audit_footprint(zone["footprint"]))
                      and all(raw[identity]["slab_zone_id"] == zone["id"]
                              and abs(raw[identity]["base_z_mm"] - zone["base_z_mm"]) < 1e-6
                              and abs(raw[identity]["height_mm"] - zone["thickness_mm"]) < 1e-6 for identity in ids), zone["id"])
            check("precision raw zone coverage is complete and unique", Counter(zone_ids) == Counter(raw.keys()), system)
            seen, movements = set(), []
            for row in rows:
                identity = row["raw_component_id"]
                original, polygon = raw[identity], raw_shapes[identity]
                expected = set_precision(polygon, grid, mode="valid_output")
                ids = row["normalized_component_ids"]
                if len(set(ids)) != len(ids) or seen & set(ids) or any(key not in normalized for key in ids):
                    raise ValueError("Normalized precision components are missing or multiply assigned")
                seen.update(ids)
                actual = unary_union([_audit_footprint(normalized[key]) for key in ids])
                check("precision normalized hosts equal an independent raw grid transformation", exact(actual, expected), identity)
                check("precision transformation footprints retain both exact raw and normalized geometry",
                      exact(_audit_footprint(row["raw_footprint"]), polygon)
                      and exact(_optional_audit_footprint(row["normalized_footprint"]), expected), identity)
                allowed = {"id", "name", "outer", "holes", "area_mm2", "volume_mm3",
                           "original_before_precision_component_id", "geometry_precision_mm"}
                for key in ids:
                    clean = normalized[key]
                    check("precision preserves original Tag, source provenance and vertical section",
                          {field: value for field, value in original.items() if field not in allowed}
                          == {field: value for field, value in clean.items() if field not in allowed}
                          and clean.get("original_before_precision_component_id") == identity
                          and clean.get("geometry_precision_mm") == grid
                          and number_matches(clean["area_mm2"], _audit_footprint(clean).area)
                          and number_matches(clean["volume_mm3"], _audit_footprint(clean).area * original["height_mm"]), key)
                movement = max(math.hypot(round(x / grid) * grid - x, round(y / grid) * grid - y)
                               for ring in [polygon.exterior, *polygon.interiors] for x, y in ring.coords)
                movements.append(movement)
                axis_movement = max(max(abs(round(x / grid) * grid - x), abs(round(y / grid) * grid - y))
                                    for ring in [polygon.exterior, *polygon.interiors] for x, y in ring.coords)
                changes = polygon.symmetric_difference(expected)
                outside = changes.difference(polygon.boundary.buffer(bound + 1e-6)).area
                hausdorff = None if expected.is_empty else polygon.hausdorff_distance(expected)
                metrics = {"raw_area_mm2": polygon.area, "normalized_area_mm2": expected.area,
                           "symmetric_difference_area_mm2": changes.area, "max_vertex_displacement_mm": movement,
                           "max_axis_vertex_displacement_mm": axis_movement,
                           "boundary_change_outside_limit_area_mm2": outside}
                check("precision movement and material changes stay within the explicit boundary bound",
                      movement <= bound + 1e-6 and outside <= 1e-6,
                      {"id": identity, "grid_size_mm": grid, "hausdorff_distance_mm": hausdorff, **metrics})
                check("precision change metrics are independently reproducible",
                      all(number_matches(row[field], value) for field, value in metrics.items())
                      and row["raw_hole_count"] == len(polygon.interiors)
                      and row["normalized_hole_count"] == sum(len(part.interiors) for part in _parts(expected))
                      and (row["hausdorff_distance_mm"] is None if hausdorff is None else number_matches(row["hausdorff_distance_mm"], hausdorff))
                      and hole_changes_match(row["removed_raw_holes"], polygon, expected), identity)
            check("precision snapshots contain exactly the independently transformed hosts", seen == set(normalized), system)
            check("precision overall displacement metric matches all raw hosts",
                  number_matches(record["max_vertex_displacement_mm"], max(movements, default=0)), system)
            source_regions, _ = _precision_overlay_regions(record, audit, "source_overlay")
            source_noded_list = record["source_noded_original_groups"]
            source_noded = {group["id"]: group for group in source_noded_list}
            if len(source_noded) != len(source_noded_list):
                raise ValueError("Precision source graph component IDs must be unique")
            source_mappings = defaultdict(list)
            source_fields = {"id", "name", "outer", "holes", "parts", "area_mm2", "volume_mm3", "source_noded_from_component_id"}
            for identity, group in source_noded.items():
                original = normalized[group["source_noded_from_component_id"]]
                source_mappings[original["id"]].append(_audit_footprint(group))
                check("precision source graph preserves each original Tag, source and vertical section",
                      {key: value for key, value in group.items() if key not in source_fields}
                      == {key: value for key, value in original.items() if key not in source_fields}
                      and number_matches(group["area_mm2"], _audit_footprint(group).area)
                      and number_matches(group["volume_mm3"], _audit_footprint(group).area * group["height_mm"]), identity)
            for identity, region in source_regions.items():
                check("precision source graph snapshots equal independently owned atomic faces",
                      unary_union(source_mappings[identity]).equals(region), identity)
            canonical, canonical_hosts = source_noded, {key: _audit_footprint(group) for key, group in source_noded.items()}
            replacement = replacements.get(system)
            if replacement is not None:
                canonical_hosts, _ = _precision_overlay_regions(record, audit)
                canonical = {group["id"]: group for group in replacement["original_groups"]}
                check("edge replacement uses the exact raw and normalized precision snapshots",
                      {group["id"]: group for group in replacement.get("raw_original_groups", [])} == raw
                      and set(canonical) == set(source_noded)
                      and replacement.get("geometry_precision_mm") == grid, system)
                for identity, original in source_noded.items():
                    clean = canonical[identity]
                    fields = {"outer", "holes", "parts", "area_mm2", "volume_mm3"}
                    check("common precision graph preserves canonical host geometry, Tag, sources and height",
                          _audit_footprint(clean).equals(canonical_hosts[identity])
                          and {key: value for key, value in clean.items() if key not in fields}
                          == {key: value for key, value in original.items() if key not in fields}
                          and number_matches(clean["area_mm2"], canonical_hosts[identity].area)
                          and number_matches(clean["volume_mm3"], canonical_hosts[identity].area * original["height_mm"]), identity)
            if record.get("source_overlay") is not None:
                check("precision final canonical snapshots are bound to the edge replacements",
                      {group["id"]: group for group in record["canonical_original_groups"]} == canonical, system)
                final_rows = record["final_components"]
                if len(final_rows) != len(raw) or {row["raw_component_id"] for row in final_rows} != set(raw):
                    raise ValueError("Every raw slab requires one final canonical precision record")
                for row in final_rows:
                    identity = row["raw_component_id"]
                    polygon = raw_shapes[identity]
                    ids = sorted(key for key, group in canonical.items() if group["original_before_precision_component_id"] == identity)
                    clean = unary_union([canonical_hosts[key] for key in ids])
                    changes = polygon.symmetric_difference(clean)
                    outside = changes.difference(polygon.boundary.buffer(grid + 1e-6)).area
                    raw_outer = unary_union([part.exterior for part in _parts(polygon)])
                    clean_outer = unary_union([part.exterior for part in _parts(clean)])
                    hausdorff = None if clean.is_empty else polygon.hausdorff_distance(clean)
                    exterior_error = None if clean.is_empty else raw_outer.hausdorff_distance(clean_outer)
                    metrics = {"raw_area_mm2": polygon.area, "canonical_area_mm2": clean.area,
                               "symmetric_difference_area_mm2": changes.area, "boundary_change_outside_limit_area_mm2": outside}
                    check("precision final source to canonical material changes stay inside the total approved bound",
                          outside <= 1e-6 and row["canonical_component_ids"] == ids
                          and _audit_footprint(row["raw_footprint"]).equals(polygon)
                          and _optional_audit_footprint(row["canonical_footprint"]).equals(clean),
                          {"id": identity, "bound_mm": grid, **metrics})
                    check("precision final area, topology and removed-hole evidence are reproducible",
                          all(number_matches(row[field], value) for field, value in metrics.items())
                          and row["raw_hole_count"] == sum(len(part.interiors) for part in _parts(polygon))
                          and row["canonical_hole_count"] == sum(len(part.interiors) for part in _parts(clean))
                          and (row["hausdorff_distance_mm"] is None if hausdorff is None else number_matches(row["hausdorff_distance_mm"], hausdorff))
                          and (row["exterior_hausdorff_distance_mm"] is None if exterior_error is None else number_matches(row["exterior_hausdorff_distance_mm"], exterior_error))
                          and hole_changes_match(row["removed_raw_holes"], polygon, clean), identity)
                final_envelope = unary_union(list(canonical_hosts.values()))
                check("precision final projected domain and changed holes remain fully auditable",
                      _audit_footprint(record["raw_projected_footprint"]).equals(raw_envelope)
                      and _audit_footprint(record["canonical_projected_footprint"]).equals(final_envelope)
                      and number_matches(record["projected_symmetric_difference_area_mm2"], raw_envelope.symmetric_difference(final_envelope).area)
                      and hole_changes_match(record["removed_projected_holes"], raw_envelope, final_envelope), system)
            report["systems"].append({"system_id": system, "grid_size_mm": grid,
                                       "raw_component_count": len(raw), "normalized_component_count": len(normalized),
                                       "max_vertex_displacement_mm": max(movements, default=0)})
    except (KeyError, TypeError, ValueError, AttributeError, GEOSException) as error:
        check("slab precision audit has complete reproducible raw source evidence", False, str(error))
    report["all_pass"] = all(row["pass"] for row in report["checks"])
    return report


def evaluate_slab_joins(groups, footprints=None, policy=None):
    """Strict overlaps with narrowly bounded, source-backed slab join exceptions.

    The baseline overlap detector is never weakened. Each permitted pair must
    have exactly one slab and one wall/column, a finite XYZ overlap envelope,
    an explicit volume cap, and source evidence belonging to both components.
    """
    _, Polygon, *_ = _geometry()
    footprints = footprints if footprints is not None else [Polygon(group["outer"], group.get("holes", [])) for group in groups]
    overlaps = _solid_overlaps(groups, footprints)
    result = {"unresolved_overlaps": [], "accepted_joins": [], "invalid_policy": []}
    if policy is None:
        result["unresolved_overlaps"] = overlaps
        return result
    if not isinstance(policy, dict) or policy.get("schema") != "dwg-slab-join-policy-v1" or not isinstance(policy.get("joins"), list):
        result.update(unresolved_overlaps=overlaps, invalid_policy=[{"code": "invalid_slab_join_policy_schema"}])
        return result
    lookup = {group["id"]: (group, footprints[index]) for index, group in enumerate(groups)}
    permitted = {}
    for entry in policy["joins"]:
        try:
            ids = entry["component_ids"]
            if not isinstance(ids, list) or len(ids) != 2 or len(set(ids)) != 2 or any(key not in lookup for key in ids):
                raise ValueError("Join requires two distinct actual component IDs")
            pair = tuple(sorted(ids))
            if pair in permitted:
                raise ValueError("Duplicate join pair")
            components = [lookup[key][0] for key in ids]
            categories = {group["category"] for group in components}
            if "slab" not in categories or len(categories) != 2 or not categories <= {"slab", "architectural_wall", "structural_wall", "column"}:
                raise ValueError("Only a slab-to-wall/column pair may use an exception")
            evidence_ids = entry.get("source_entity_ids")
            component_sources = [set(group.get("source_entity_ids", [])) for group in components]
            if not isinstance(evidence_ids, list) or not evidence_ids or not isinstance(entry.get("evidence"), str) or not entry["evidence"].strip():
                raise ValueError("Join requires source IDs and an explicit evidence description")
            if not set(evidence_ids) <= set.union(*component_sources) or any(not set(evidence_ids) & sources for sources in component_sources):
                raise ValueError("Join evidence must identify verified sources from both components")
            contact = entry["allowed_contact"]
            shape = _audit_footprint(contact["footprint"])
            bottom, top = _number(contact["base_z_mm"], "join bottom"), _number(contact["top_z_mm"], "join top")
            cap = _number(contact["max_overlap_volume_mm3"], "join volume cap")
            if top <= bottom or cap < 0:
                raise ValueError("Join envelope must have positive height and a nonnegative volume cap")
            permitted[pair] = (entry, shape, bottom, top, cap)
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            result["invalid_policy"].append({"code": "invalid_slab_join", "entry": entry, "detail": str(error)})
    for overlap in overlaps:
        pair = tuple(sorted(overlap["component_ids"]))
        rule = permitted.get(pair)
        accepted = False
        if rule:
            entry, shape, bottom, top, cap = rule
            accepted = all(_optional_audit_footprint(section['footprint']).difference(shape.buffer(.01)).area <= .1
                           and section['base_z_mm'] >= bottom-.01 and section['top_z_mm'] <= top+.01
                           for section in overlap['intersection_slices'])
            accepted = accepted and overlap["overlap_volume_mm3"] <= cap + max(.1, cap * 1e-6)
            if accepted:
                result["accepted_joins"].append({**overlap, "allowed_contact": entry["allowed_contact"],
                                                   "source_entity_ids": entry["source_entity_ids"], "evidence": entry["evidence"]})
        if not accepted:
            result["unresolved_overlaps"].append(overlap)
    return result


def validate_slab_payload(payload):
    """Return blocking slab-schema errors without opening the native library."""
    if payload.get("whole_slab_audit") is not None or any(group.get("mode") == "boundary_mesh" for group in payload.get("groups", [])):
        from .whole_slabs import validate_whole_payload
        return validate_whole_payload(payload)
    all_slabs = {group["id"]: group for group in payload.get("groups", []) if group.get("category") == "slab"}
    if not all_slabs:
        return []
    slabs = {key: group for key, group in all_slabs.items() if _slab_role(group) != "slab_step_riser"}
    risers = {key: group for key, group in all_slabs.items() if _slab_role(group) == "slab_step_riser"}
    audit = payload.get("slab_audit")
    if not isinstance(audit, dict) or audit.get("schema") != "dwg-slab-audit-v1":
        return [{"code": "slab_audit_required"}]
    errors, assembly_seen, zone_seen = [], Counter(), Counter()
    edge_systems = {entry.get("system_id") for entry in audit.get("edge_aligned_replacements", [])}
    for key, group in all_slabs.items():
        role = _slab_role(group)
        if role not in {"floor_slab", "overhead_slab", "slab_step_riser"}:
            errors.append({"code": "invalid_slab_role", "component_id": key})
        if not group.get("slab_zone_id") or not group.get("slab_system_id") or not group.get("source_entity_ids"):
            errors.append({"code": "slab_zone_and_source_identity_required", "component_id": key})
        if group.get("slab_role") and group.get("semantic_role") and group["slab_role"] != group["semantic_role"]:
            errors.append({"code": "conflicting_slab_roles", "component_id": key})
        try:
            base, height = _number(group["base_z_mm"], "slab base"), _number(group["height_mm"], "slab thickness", positive=True)
            if group.get("top_z_mm") is not None and abs(_number(group["top_z_mm"], "slab top") - base - height) > .01:
                raise ValueError("top_z_mm differs from base_z_mm + height_mm")
        except (KeyError, ValueError, TypeError) as error:
            errors.append({"code": "invalid_slab_dimensions", "component_id": key, "detail": str(error)})
        if role == "slab_step_riser":
            try:
                _number(group["horizontal_thickness_mm"], "riser horizontal thickness", positive=True)
            except (KeyError, ValueError, TypeError) as error:
                errors.append({"code": "invalid_slab_step_riser_dimensions", "component_id": key, "detail": str(error)})
    for kind, seen in (("assemblies", assembly_seen), ("zones", zone_seen)):
        entries = audit.get(kind)
        if not isinstance(entries, list) or (not entries and not (edge_systems and not slabs)):
            errors.append({"code": "slab_audit_entries_required", "kind": kind})
            continue
        entry_ids = set()
        for entry in entries:
            try:
                if not entry.get("id") or entry["id"] in entry_ids:
                    raise ValueError("Audit entry IDs must be nonempty and unique")
                entry_ids.add(entry["id"])
                ids = entry["component_ids"]
                empty_edge = kind == "assemblies" and entry["id"] in edge_systems and ids == []
                if (not ids and not empty_edge) or len(set(ids)) != len(ids) or any(key not in slabs for key in ids):
                    raise ValueError("Audit entry requires distinct actual slab component IDs")
                seen.update(ids)
                footprint = _optional_audit_footprint(entry["footprint"]) if empty_edge else _audit_footprint(entry["footprint"])
                if not entry.get("source_entity_ids"):
                    raise ValueError("Audit entry requires source provenance")
                if kind == "assemblies":
                    if entry.get("role") not in {"floor_slab", "overhead_slab"}:
                        raise ValueError("Assembly role is required")
                    if any((slabs[key].get("slab_role") or slabs[key].get("semantic_role")) != entry["role"] for key in ids):
                        raise ValueError("Assembly role differs from slab component role")
                    if any(slabs[key].get("slab_system_id") != entry["id"] for key in ids):
                        raise ValueError("Assembly system identity differs from its slab components")
                    area = _number(entry["expected_area_mm2"], "slab area", positive=not empty_edge)
                    volume = _number(entry["expected_volume_mm3"], "slab volume", positive=not empty_edge)
                    if empty_edge and (area != 0 or volume != 0):
                        raise ValueError("An absorbed horizontal assembly must have zero area and volume")
                    if abs(area - footprint.area) > max(.1, area * 1e-5):
                        raise ValueError("Assembly expected area differs from approved footprint")
                else:
                    base, top = _number(entry["base_z_mm"], "zone bottom"), _number(entry["top_z_mm"], "zone top")
                    thickness = _number(entry["thickness_mm"], "zone thickness", positive=True)
                    if abs(top - base - thickness) > .01:
                        raise ValueError("Zone top/bottom/thickness are inconsistent")
                    if any(slabs[key].get("slab_zone_id") != entry["id"] or slabs[key].get("slab_system_id") != entry.get("system_id") for key in ids):
                        raise ValueError("Zone/system identity differs from slab components")
                    if any(abs(slabs[key]["base_z_mm"] - base) > .01 or abs(slabs[key]["height_mm"] - thickness) > .01 for key in ids):
                        raise ValueError("Unresolved zone dimensions differ from slab components")
            except (KeyError, TypeError, ValueError, AttributeError) as error:
                errors.append({"code": "invalid_slab_audit_entry", "kind": kind, "entry_id": entry.get("id") if isinstance(entry, dict) else None, "detail": str(error)})
    for key in slabs:
        if assembly_seen[key] != 1 or zone_seen[key] != 1:
            errors.append({"code": "slab_component_audit_coverage", "component_id": key, "assemblies": assembly_seen[key], "zones": zone_seen[key]})
    for opening in audit.get("openings", []):
        try:
            _audit_footprint(opening["footprint"])
            if not opening.get("source_entity_ids") or not opening.get("system_id"):
                raise ValueError("Slab opening requires system identity and source evidence")
        except (KeyError, TypeError, ValueError) as error:
            errors.append({"code": "invalid_slab_opening_audit", "detail": str(error)})
    riser_seen, entry_ids = defaultdict(set), set()
    entries = audit.get("step_risers", [])
    if not isinstance(entries, list):
        return [*errors, {"code": "invalid_slab_step_riser_audit"}]
    for entry in entries:
        if _edge_aligned(entry):
            continue
        try:
            if not entry.get("id") or entry["id"] in entry_ids:
                raise ValueError("Riser audit requires unique entry IDs")
            entry_ids.add(entry["id"])
            status = entry["status"]
            if status not in {"bridged_gap", "already_continuous"}:
                raise ValueError("Riser gap is not completely resolved")
            ids = entry["component_ids"]
            if not isinstance(ids, list) or len(set(ids)) != len(ids) or any(key not in risers for key in ids):
                raise ValueError("Riser audit components must identify actual step-riser groups")
            if bool(ids) != (status == "bridged_gap"):
                raise ValueError("Only a bridged positive gap may contain riser solids")
            for key in ids:
                riser_seen[key].add(entry["id"])
            lower = entry["lower_horizontal_component_ids"]
            upper = entry["upper_horizontal_component_ids"]
            if not lower or not upper or set(lower) & set(upper) or any(key not in slabs for key in [*lower, *upper]):
                raise ValueError("Riser audit requires distinct actual horizontal hosts on both sides")
            if any(slabs[key].get("slab_system_id") != entry["system_id"] for key in [*lower, *upper]):
                raise ValueError("Riser and horizontal host systems differ")
            from shapely.geometry import MultiLineString
            lines = MultiLineString([[_point(point) for point in line] for line in entry["shared_boundary"]["lines"]])
            if lines.is_empty or lines.length <= .01 or not entry.get("source_entity_ids"):
                raise ValueError("Riser requires nonempty shared boundary and source provenance")
            width = _number(entry["horizontal_thickness_mm"], "riser horizontal thickness", positive=True)
            bottom, top = _number(entry["base_z_mm"], "riser base"), _number(entry["top_z_mm"], "riser top")
            if abs(_number(entry["gap_height_mm"], "riser gap") - top + bottom) > .01:
                raise ValueError("Riser gap dimensions are inconsistent")
            if status == "bridged_gap":
                if top - bottom <= .01:
                    raise ValueError("Bridged riser gap must have positive height")
                shape = _audit_footprint(entry["footprint"])
                _audit_footprint(entry["requested_footprint"])
                _audit_footprint(entry["lower_contact_footprint"])
                _audit_footprint(entry["upper_contact_footprint"])
                expected_area = _number(entry.get("expected_area_mm2", entry.get("requested_area_mm2")), "riser area", positive=True)
                expected_volume = _number(entry["expected_volume_mm3"], "riser volume", positive=True)
                if abs(shape.area - expected_area) > max(.1, expected_area * 1e-5):
                    raise ValueError("Riser audit area disagrees with its approved footprint")
                slices = entry.get("coverage_slices")
                if not isinstance(slices, list) or not slices:
                    raise ValueError("A positive gap requires explicit vertical coverage slices")
                cursor, slice_volume, slice_ids = bottom, 0., set()
                for section in slices:
                    lo = _number(section["base_z_mm"], "riser slice base")
                    hi = _number(section["top_z_mm"], "riser slice top")
                    if abs(lo - cursor) > .01 or hi - lo <= .01 or hi > top + .01:
                        raise ValueError("Riser slices must continuously partition the complete vertical gap")
                    cursor = hi
                    section_shape = _optional_audit_footprint(section["footprint"])
                    _optional_audit_footprint(section["existing_horizontal_footprint"])
                    section_ids = section["component_ids"]
                    if any(key not in ids for key in section_ids):
                        raise ValueError("Riser slice component is outside the audited connection")
                    slice_ids.update(section_ids)
                    slice_volume += section_shape.area * (hi - lo)
                    if any(abs(risers[key]["base_z_mm"] - lo) > .01 or abs(risers[key]["height_mm"] - hi + lo) > .01 for key in section_ids):
                        raise ValueError("Riser component does not match its vertical event slice")
                if abs(cursor - top) > .01 or slice_ids != set(ids) or abs(expected_volume - slice_volume) > max(.1, expected_volume * 1e-5):
                    raise ValueError("Riser slice coverage or volume is inconsistent")
                for key in ids:
                    group = risers[key]
                    if (group.get("slab_system_id") != entry["system_id"]
                            or group.get("host_slab_role") != _slab_role(slabs[lower[0]])
                            or abs(group["horizontal_thickness_mm"] - width) > .01):
                        raise ValueError("Riser group dimensions or host identities disagree with its audit")
            elif top - bottom > .01:
                raise ValueError("A positive vertical gap cannot be marked already continuous")
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            errors.append({"code": "invalid_slab_step_riser_audit", "entry_id": entry.get("id") if isinstance(entry, dict) else None, "detail": str(error)})
    for key in risers:
        if _edge_aligned(risers[key]):
            continue
        references = risers[key].get("riser_audit_ids")
        declared = set(references) if isinstance(references, list) else set()
        if not riser_seen[key] or declared != riser_seen[key] or len(declared) != len(references or []):
            errors.append({"code": "slab_step_riser_audit_coverage", "component_id": key, "audit_entries": sorted(riser_seen[key])})
        host_rows = [entry for entry in entries if isinstance(entry, dict) and entry.get("id") in riser_seen[key]]
        lower = {identity for entry in host_rows for identity in entry.get("lower_horizontal_component_ids", [])}
        upper = {identity for entry in host_rows for identity in entry.get("upper_horizontal_component_ids", [])}
        group = risers[key]
        if (set(group.get("lower_horizontal_component_ids", [])) != lower
                or set(group.get("upper_horizontal_component_ids", [])) != upper
                or group.get("host_system_id", group.get("slab_system_id")) != group.get("slab_system_id")):
            errors.append({"code": "slab_step_riser_host_provenance_mismatch", "component_id": key})
        host_sources = {identity for host in lower | upper if host in slabs for identity in slabs[host].get("source_entity_ids", [])}
        if not host_sources.issubset(set(group.get("source_entity_ids", []))):
            errors.append({"code": "slab_step_riser_source_provenance_incomplete", "component_id": key})
    precision = _slab_geometry_precision_audit(payload)
    errors.extend({"code": "invalid_slab_geometry_precision", "detail": check}
                  for check in precision["checks"] if not check["pass"])
    if not errors and (edge_systems or any(_edge_aligned(group) for group in risers.values())):
        rows = [{"id": key, "category": group["category"], "tag": group.get("tag"), "footprint": _audit_footprint(group),
                 "base_z_mm": group["base_z_mm"], "top_z_mm": group["base_z_mm"] + group["height_mm"],
                 "volume_mm3": _audit_footprint(group).area * group["height_mm"]} for key, group in all_slabs.items()]
        checked = _saved_edge_aligned_slab_audit(payload, rows)
        errors.extend({"code": "invalid_edge_aligned_slab_audit", "detail": check} for check in checked["checks"] if not check["pass"])
    return errors


def _saved_slab_audit(payload, saved_rows):
    """Compare actual reopened slab footprints, Z ranges and volumes to audit."""
    _, _, _, unary_union, _ = _geometry()
    planned = {group["id"]: group for group in payload["groups"]}
    saved = {row["id"]: row for row in saved_rows if row["category"] == "slab" and _slab_role(planned[row["id"]]) != "slab_step_riser"}
    report = {"schema": "dwg-saved-slab-qa-v1", "checks": [], "assemblies": [], "zones": [], "openings": []}
    precision = _slab_geometry_precision_audit(payload)
    report["geometry_precision"] = precision
    report["checks"].extend(precision["checks"])

    def check(name, passed, detail):
        report["checks"].append({"check": name, "pass": bool(passed), "detail": detail})

    def compare_footprint(actual, expected, identity):
        error = actual.symmetric_difference(expected).area
        check("saved slab footprint extent and holes", error <= max(.1, expected.area * 1e-5), {"id": identity, "difference_area_mm2": error})
        expected_holes = sum(len(part.interiors) for part in _parts(expected))
        actual_holes = sum(len(part.interiors) for part in _parts(actual))
        check("saved slab hole topology", actual_holes == expected_holes,
              {"id": identity, "expected_holes": expected_holes, "actual_holes": actual_holes})
        return {"bounds_mm": list(actual.bounds), "area_mm2": actual.area, "hole_count": actual_holes}

    for kind in ("assemblies", "zones"):
        for entry in payload["slab_audit"][kind]:
            rows = [saved[key] for key in entry["component_ids"] if key in saved]
            check("saved slab audit components present", len(rows) == len(entry["component_ids"]), {"kind": kind, "id": entry["id"]})
            if not rows:
                continue
            footprint = unary_union([row["footprint"] for row in rows])
            expected = _audit_footprint(entry["footprint"])
            description = {"id": entry["id"], "component_ids": entry["component_ids"], **compare_footprint(footprint, expected, entry["id"])}
            volume = sum(row["volume_mm3"] for row in rows)
            target_volume = entry["expected_volume_mm3"] if kind == "assemblies" else expected.area * entry["thickness_mm"]
            check("saved slab material volume", abs(volume - target_volume) <= max(.1, target_volume * .001),
                  {"id": entry["id"], "actual_volume_mm3": volume, "expected_volume_mm3": target_volume})
            description.update(volume_mm3=volume, expected_volume_mm3=target_volume)
            if kind == "zones":
                check("saved slab zone bottom top and thickness",
                      all(abs(row["base_z_mm"] - entry["base_z_mm"]) < .01
                          and abs(row["top_z_mm"] - entry["top_z_mm"]) < .01
                          and abs(row["top_z_mm"] - row["base_z_mm"] - entry["thickness_mm"]) < .01 for row in rows),
                      {"id": entry["id"], "actual_z_ranges_mm": [[row["base_z_mm"], row["top_z_mm"]] for row in rows],
                       "expected_bottom_mm": entry["base_z_mm"], "expected_top_mm": entry["top_z_mm"], "expected_thickness_mm": entry["thickness_mm"]})
                description.update(actual_z_ranges_mm=[[row["base_z_mm"], row["top_z_mm"]] for row in rows])
            report[kind].append(description)
    by_system = defaultdict(list)
    for row in saved_rows:
        if row["category"] == "slab":
            by_system[planned[row["id"]].get("slab_system_id")].append(row["footprint"])
    for opening in payload["slab_audit"].get("openings", []):
        system = opening["system_id"]
        parts = by_system.get(system, [])
        footprint = _audit_footprint(opening["footprint"])
        intersection = unary_union(parts).intersection(footprint).area
        passed = bool(parts) and intersection <= .1
        check("saved slab opening remains void", passed, {"opening_id": opening["id"], "system_id": system, "obstruction_area_mm2": intersection})
        report["openings"].append({"id": opening["id"], "system_id": system, "obstruction_area_mm2": intersection, "pass": passed})
    if any(not _edge_aligned(entry) for entry in payload["slab_audit"].get("step_risers", [])):
        riser_report = _saved_slab_riser_audit(payload, saved_rows)
        report["step_risers"] = riser_report["step_risers"]
        report["checks"].extend(riser_report["checks"])
    if payload["slab_audit"].get("edge_aligned_replacements"):
        edge_report = _saved_edge_aligned_slab_audit(payload, saved_rows)
        report["edge_aligned_replacements"] = edge_report["replacements"]
        report["checks"].extend(edge_report["checks"])
    report["all_pass"] = all(item["pass"] for item in report["checks"])
    return report


def _saved_slab_riser_audit(payload, saved_rows):
    """Reconstruct gap bridges, contact faces and widths from reopened solids.

    Riser height is the positive gap between horizontal plate surfaces. Its
    horizontal thickness is independently reconstructed from the shared-line
    network; it is never treated as horizontal slab thickness or plate area.
    """
    from shapely.geometry import MultiLineString
    _, _, _, unary_union, _ = _geometry()
    planned = {group["id"]: group for group in payload["groups"]}
    saved = {row["id"]: row for row in saved_rows}
    report = {"checks": [], "step_risers": []}

    def check(name, passed, detail):
        report["checks"].append({"check": name, "pass": bool(passed), "detail": detail})

    def shape_check(name, actual, expected, identity):
        error = actual.symmetric_difference(expected).area
        check(name, actual.is_valid and error <= max(.1, expected.area * 1e-5),
              {"id": identity, "difference_area_mm2": error, "actual_area_mm2": actual.area,
               "expected_area_mm2": expected.area})

    assemblies = {entry["id"]: _audit_footprint(entry["footprint"]) for entry in payload["slab_audit"]["assemblies"]}
    horizontal = [row for key, row in saved.items() if _slab_role(planned[key]) != "slab_step_riser"]
    for entry in payload["slab_audit"].get("step_risers", []):
        if _edge_aligned(entry):
            continue
        identity = entry["id"]
        lower = [saved[key] for key in entry["lower_horizontal_component_ids"] if key in saved]
        upper = [saved[key] for key in entry["upper_horizontal_component_ids"] if key in saved]
        hosts_present = len(lower) == len(entry["lower_horizontal_component_ids"]) and len(upper) == len(entry["upper_horizontal_component_ids"])
        check("saved step riser horizontal hosts present", hosts_present, identity)
        if not hosts_present:
            continue
        lower_shape = unary_union([row["footprint"] for row in lower])
        upper_shape = unary_union([row["footprint"] for row in upper])
        lines = MultiLineString(entry["shared_boundary"]["lines"])
        shared = lower_shape.boundary.buffer(.01).intersection(upper_shape.boundary.buffer(.01))
        uncovered_host_line = lines.difference(shared).length
        check("saved step shared line belongs to both actual horizontal hosts", uncovered_host_line <= .1,
              {"id": identity, "uncovered_length_mm": uncovered_host_line})
        bottom, top = entry["base_z_mm"], entry["top_z_mm"]
        if entry["status"] == "already_continuous":
            actual_gap = min(row["base_z_mm"] for row in upper) - max(row["top_z_mm"] for row in lower)
            check("saved step already continuous has no positive vertical gap", actual_gap <= .01 and not entry["component_ids"],
                  {"id": identity, "actual_gap_mm": actual_gap})
            report["step_risers"].append({"id": identity, "status": entry["status"], "actual_gap_mm": actual_gap})
            continue
        check("saved step host contact elevations match gap endpoints",
              all(abs(row["top_z_mm"] - bottom) < .01 for row in lower)
              and all(abs(row["base_z_mm"] - top) < .01 for row in upper), identity)
        rows = [saved[key] for key in entry["component_ids"] if key in saved]
        check("saved step riser components present", len(rows) == len(entry["component_ids"]), identity)
        if not rows:
            continue
        requested = _audit_footprint(entry["requested_footprint"])
        band = lines.buffer(entry["horizontal_thickness_mm"] / 2, cap_style=2, join_style=2).intersection(assemblies[entry["system_id"]])
        shape_check("step riser horizontal thickness reconstructed from shared boundary", requested, band, identity)
        slices = entry.get("coverage_slices") or [{"base_z_mm": bottom, "top_z_mm": top,
                  "footprint": entry["footprint"], "component_ids": entry["component_ids"],
                  "existing_horizontal_footprint": {"parts": []}}]
        volume, actual_sections = 0., []
        for index, section in enumerate(slices):
            lo, hi = section["base_z_mm"], section["top_z_mm"]
            active = [saved[key] for key in section["component_ids"] if key in saved
                      and saved[key]["base_z_mm"] <= lo + .01 and saved[key]["top_z_mm"] >= hi - .01]
            check("saved step riser spans its complete event slice", len(active) == len(section["component_ids"]),
                  {"id": identity, "slice": index, "z_mm": [lo, hi]})
            actual = unary_union([row["footprint"] for row in active]).intersection(requested)
            occupied = unary_union([row["footprint"] for row in horizontal
                                    if planned[row["id"]].get("slab_system_id") == entry["system_id"]
                                    and row["base_z_mm"] <= lo + .01 and row["top_z_mm"] >= hi - .01]).intersection(requested)
            shape_check("saved step riser slice required material", actual, _optional_audit_footprint(section["footprint"]),
                        {"id": identity, "slice": index})
            shape_check("saved step riser existing horizontal occupancy", occupied,
                        _optional_audit_footprint(section["existing_horizontal_footprint"]), {"id": identity, "slice": index})
            shape_check("saved step riser completely fills the requested gap band", unary_union([actual, occupied]), requested,
                        {"id": identity, "slice": index})
            uncovered = lines.difference(unary_union([actual, occupied]).buffer(.01)).length
            check("saved step riser continuously covers the shared gap line", uncovered <= .1,
                  {"id": identity, "slice": index, "uncovered_length_mm": uncovered})
            volume += actual.area * (hi - lo)
            actual_sections.append(unary_union([actual, occupied]))
        for side, host_shape, section_shape in (("lower", lower_shape, actual_sections[0]), ("upper", upper_shape, actual_sections[-1])):
            contact = section_shape.intersection(host_shape)
            approved_contact = _audit_footprint(entry[side + "_contact_footprint"])
            shape_check("saved step riser " + side + " horizontal contact face", contact, approved_contact, identity)
            check("saved step riser " + side + " contact has positive area", contact.area > .1,
                  {"id": identity, "contact_area_mm2": contact.area})
        target_volume = entry["expected_volume_mm3"]
        check("saved step riser closed material volume", all(row["volume_mm3"] > 0 for row in rows)
              and abs(volume - target_volume) <= max(.1, target_volume * .001),
              {"id": identity, "actual_volume_mm3": volume, "expected_volume_mm3": target_volume})
        report["step_risers"].append({"id": identity, "status": entry["status"], "component_ids": entry["component_ids"],
                                      "coverage_slice_count": len(slices), "actual_volume_mm3": volume,
                                      "horizontal_thickness_mm": entry["horizontal_thickness_mm"], "gap_z_mm": [bottom, top]})
    # A shared riser part can serve several connections. Check the full actual
    # union at every Z event as well, so clipping per connection cannot conceal
    # extra width, material outside the system or an unrequested vertical tail.
    for system in assemblies:
        rows = [row for key, row in saved.items() if _slab_role(planned[key]) == "slab_step_riser"
                and planned[key].get("slab_system_id") == system]
        sections = [section for entry in payload["slab_audit"].get("step_risers", [])
                    if entry["system_id"] == system and not _edge_aligned(entry) and entry["status"] == "bridged_gap"
                    for section in entry.get("coverage_slices", [])]
        levels = sorted({round(value, 6) for item in [*rows, *sections] for value in (item["base_z_mm"], item["top_z_mm"])})
        for lo, hi in zip(levels, levels[1:]):
            if hi - lo <= .01:
                continue
            mid = (lo + hi) / 2
            actual = unary_union([row["footprint"] for row in rows if row["base_z_mm"] < mid < row["top_z_mm"]])
            expected = unary_union([_optional_audit_footprint(section["footprint"]) for section in sections
                                    if section["base_z_mm"] < mid < section["top_z_mm"]])
            shape_check("saved step riser event union has no extra or missing material", actual, expected,
                        {"system_id": system, "z_mm": [lo, hi]})
    report["all_pass"] = all(row["pass"] for row in report["checks"])
    return report


def _saved_edge_aligned_slab_audit(payload, saved_rows):
    """Verify LOWER-side full panels against the preserved pre-trim DWG edges.

    Z events below are integration samples for QA only. No sliced geometry is
    generated: each panel must span a complete original lower-base/upper-top
    section, and all original material plus the full panels must be preserved.
    """
    from shapely import set_precision
    from shapely.geometry import GeometryCollection, MultiLineString
    _, _, _, unary_union, _ = _geometry()
    report = {"checks": [], "replacements": []}
    planned = {group["id"]: group for group in payload["groups"]}
    saved = {row["id"]: row for row in saved_rows}

    def check(name, passed, detail):
        report["checks"].append({"check": name, "pass": bool(passed), "detail": detail})

    def same_shape(name, actual, expected, detail):
        difference = actual.symmetric_difference(expected).area
        check(name, actual.is_valid and difference <= max(.1, expected.area * 1e-5),
              {"detail": detail, "difference_area_mm2": difference})

    try:
        replacements = payload["slab_audit"].get("edge_aligned_replacements", [])
        edge_groups = {key for key, group in planned.items() if _slab_role(group) == "slab_step_riser" and _edge_aligned(group)}
        represented_panels = set()
        for replacement in replacements:
            system = replacement["system_id"]
            if not _edge_aligned(replacement):
                raise ValueError("Edge replacement must name its full-depth profile")
            original_list = replacement["original_groups"]
            originals = {group["id"]: group for group in original_list}
            if len(originals) != len(original_list) or not originals:
                raise ValueError("Edge profile requires unique original horizontal snapshots")
            if any(group.get("category") != "slab" or _slab_role(group) not in {"floor_slab", "overhead_slab"}
                   or group.get("slab_system_id") != system for group in original_list):
                raise ValueError("Edge original snapshots must be horizontal plates in the same system")
            grid = replacement.get("geometry_precision_mm")
            precision_record = next((record for record in payload["slab_audit"].get("geometry_precision", [])
                                     if record["system_id"] == system), None)
            canonical_bands = None
            if precision_record is not None and precision_record.get("edge_overlay") is not None:
                original_shapes, canonical_bands = _precision_overlay_regions(precision_record, payload["slab_audit"])
            else:
                original_shapes = {key: (set_precision(_audit_footprint(group), grid, mode="valid_output")
                                         if grid is not None else _audit_footprint(group)) for key, group in originals.items()}
            envelope = unary_union(list(original_shapes.values()))
            same_shape("edge original snapshots preserve the original assembly envelope", envelope,
                       _audit_footprint(replacement["original_assembly"]["footprint"]), system)
            same_shape("edge source envelope snapshot is consistent", _audit_footprint(replacement["original_footprint"]), envelope, system)
            links = [entry for entry in payload["slab_audit"]["step_risers"] if entry["system_id"] == system and _edge_aligned(entry)]
            link_ids = {entry["id"] for entry in links}
            if len(link_ids) != len(links):
                raise ValueError("Full-depth original edge link IDs must be unique")
            bands, sections, trims = {}, {}, defaultdict(list)
            for entry in links:
                identity = entry["id"]
                low_ids, high_ids = entry["original_lower_horizontal_component_ids"], entry["original_upper_horizontal_component_ids"]
                if not low_ids or not high_ids or set(low_ids) & set(high_ids) or any(key not in originals for key in [*low_ids, *high_ids]):
                    raise ValueError("Edge link must bind distinct preserved lower and upper snapshots")
                lower = unary_union([original_shapes[key] for key in low_ids])
                upper = unary_union([original_shapes[key] for key in high_ids])
                shared = lower.boundary.intersection(upper.boundary)
                line = MultiLineString(entry["shared_boundary"]["lines"])
                if grid is not None and canonical_bands is None:
                    line = set_precision(line, grid, mode="valid_output")
                # The overlay uses the declared grid. QA distance buffers must
                # retain their actual 0.00001 mm width instead of themselves
                # collapsing onto that grid.
                measured_line, measured_shared = set_precision(line, 0), set_precision(shared, 0)
                line_error = measured_line.difference(measured_shared.buffer(1e-5)).length + measured_shared.difference(measured_line.buffer(1e-5)).length
                check("edge panel follows the actual original shared DWG boundary", not line.is_empty and line.length > .01 and line_error <= .01,
                      {"id": identity, "boundary_error_mm": line_error})
                width = _number(entry["horizontal_thickness_mm"], "edge horizontal thickness", positive=True)
                band = canonical_bands[identity] if canonical_bands is not None else line.buffer(width, cap_style=2, join_style=2).intersection(lower)
                same_shape("edge panel band has full width only on the original LOWER side", _audit_footprint(entry["requested_footprint"]), band, identity)
                same_shape("edge panel declared footprint equals the LOWER side band", _audit_footprint(entry["footprint"]), band, identity)
                bottom = min(_number(originals[key]["base_z_mm"], "original lower base") for key in low_ids)
                top = max(_number(originals[key]["base_z_mm"], "original upper base") +
                          _number(originals[key]["height_mm"], "original upper thickness", positive=True) for key in high_ids)
                check("edge panel section runs from original lower bottom to original upper top",
                      abs(entry["base_z_mm"] - bottom) < .01 and abs(entry["top_z_mm"] - top) < .01, identity)
                check("edge panel policy contains no intermediate Z slice recipe", not entry.get("coverage_slices"), identity)
                bands[identity], sections[identity] = band, (bottom, top)
                for key in low_ids:
                    trims[key].append(band)
            system_panels = {key for key in edge_groups if planned[key].get("slab_system_id") == system}
            represented_panels.update(system_panels)
            check("edge panel actual closed components are present", all(key in saved for key in system_panels), system)
            horizontal_ids = set()
            replacement_rows = replacement["replacements"]
            if {row["original_component_id"] for row in replacement_rows} != set(originals) or len(replacement_rows) != len(originals):
                raise ValueError("Every original horizontal plate requires exactly one replacement mapping")
            mappings = {row["original_component_id"]: row for row in replacement_rows}
            for mapping in replacement_rows:
                identity = mapping["original_component_id"]
                original = originals[identity]
                ids = mapping["result_horizontal_component_ids"]
                if horizontal_ids & set(ids) or any(key not in saved or _slab_role(planned[key]) == "slab_step_riser" for key in ids):
                    raise ValueError("Replacement horizontal components are missing, shared or misclassified")
                horizontal_ids.update(ids)
                removed = original_shapes[identity].intersection(unary_union(trims[identity]))
                expected = original_shapes[identity].difference(removed)
                actual = unary_union([saved[key]["footprint"] for key in ids])
                same_shape("edge lower horizontal plate is trimmed by exactly its panel bands", actual, expected, identity)
                same_shape("edge declared horizontal remaining material is exact", _optional_audit_footprint(mapping["remaining_footprint"]), expected, identity)
                same_shape("edge declared removed horizontal material is exact", _optional_audit_footprint(mapping["removed_footprint"]), removed, identity)
                check("edge replacement horizontal elevation and original source Tag remain exact",
                      all(abs(saved[key]["base_z_mm"] - original["base_z_mm"]) < .01
                          and abs(saved[key]["top_z_mm"] - original["base_z_mm"] - original["height_mm"]) < .01
                          and saved[key].get("tag", planned[key].get("tag")) == original["tag"] for key in ids), identity)
            actual_horizontal_ids = {key for key, group in planned.items() if group.get("category") == "slab"
                                     and group.get("slab_system_id") == system and _slab_role(group) != "slab_step_riser"}
            check("edge replacement accounts for every actual horizontal group", horizontal_ids == actual_horizontal_ids, system)
            for key in system_panels:
                group, actual = planned[key], saved[key]
                refs = group.get("riser_audit_ids", [])
                if not refs or any(identity not in link_ids for identity in refs):
                    raise ValueError("Edge full panel lost its original shared edge audit IDs")
                low_ids = group["original_lower_horizontal_component_ids"]
                if not low_ids or any(identity not in originals for identity in low_ids):
                    raise ValueError("Edge full panel lost its original LOWER host snapshots")
                allowed = unary_union([bands[identity] for identity in refs])
                check("edge saved panel never crosses the original LOWER side or approved band",
                      actual["footprint"].difference(allowed).area <= .1, key)
                own_sections = [sections[identity] for identity in refs]
                check("edge saved panel is one complete original section without intermediate slicing",
                      any(abs(actual["base_z_mm"] - bottom) < .01 and abs(actual["top_z_mm"] - top) < .01 for bottom, top in own_sections), key)
                check("edge full panel preserves a lower host original source Tag",
                      actual.get("tag", group.get("tag")) in {originals[identity]["tag"] for identity in low_ids}, key)
            for entry in links:
                identity, band = entry["id"], bands[entry["id"]]
                bottom, top = sections[identity]
                ids = entry["component_ids"]
                if any(key not in system_panels or key not in saved for key in ids):
                    raise ValueError("Shared edge references a missing or non-panel component")
                rows = [saved[key] for key in ids]
                full = [row for row in rows if row["base_z_mm"] <= bottom + .01 and row["top_z_mm"] >= top - .01]
                covered = unary_union([row["footprint"] for row in full])
                same_shape("edge original shared edge receives continuous full-depth coverage", covered.intersection(band), band, identity)
                outer = unary_union([row["footprint"].boundary for row in full])
                line = MultiLineString(entry["shared_boundary"]["lines"])
                check("edge saved panel outer face aligns with the original shared edge", line.difference(outer.buffer(.01)).length <= .1, identity)
                for side in ("lower", "upper"):
                    host_ids = entry["original_" + side + "_horizontal_component_ids"]
                    expected_refs = {key for host in host_ids for key in
                                     [*mappings[host]["result_horizontal_component_ids"], *mappings[host]["riser_component_ids"]]}
                    check("edge " + side + " contact references preserve actual original host replacements",
                          set(entry[side + "_replacement_component_ids"]) == expected_refs, identity)
                remaining_lower = unary_union([saved[key]["footprint"] for host in entry["original_lower_horizontal_component_ids"]
                                               for key in mappings[host]["result_horizontal_component_ids"]])
                inner_contact = band.boundary.intersection(remaining_lower.boundary) if not remaining_lower.is_empty else GeometryCollection()
                check("edge panel contacts the actual trimmed lower plate along its full inner face",
                      inner_contact.difference(outer.buffer(.01)).length <= .1, {"id": identity, "contact_length_mm": inner_contact.length})
                upper_ids = entry["upper_replacement_component_ids"]
                if any(key not in saved for key in upper_ids):
                    raise ValueError("Edge panel upper contact replacement is missing")
                upper_boundary = unary_union([saved[key]["footprint"].boundary for key in upper_ids
                                               if saved[key]["top_z_mm"] > bottom + .01 and saved[key]["base_z_mm"] < top - .01])
                check("edge full panel contacts the actual upper replacement along the original edge",
                      line.difference(upper_boundary.buffer(.01)).length <= .1, identity)
            actual_rows = [saved[key] for key in horizontal_ids | system_panels]
            actual_projection = unary_union([row["footprint"] for row in actual_rows])
            same_shape("edge panels and trimmed plates preserve the entire original plan envelope", actual_projection, envelope, system)
            same_shape("edge final projected audit matches saved material", _audit_footprint(replacement["final_projected_footprint"]), actual_projection, system)
            levels = sorted({round(value, 6) for row in actual_rows for value in (row["base_z_mm"], row["top_z_mm"])} |
                            {value for section in sections.values() for value in section} |
                            {value for group in original_list for value in (group["base_z_mm"], group["base_z_mm"] + group["height_mm"])})
            expected_volume = 0.
            for lo, hi in zip(levels, levels[1:]):
                if hi - lo <= 1e-6:
                    continue
                mid = (lo + hi) / 2
                expected = unary_union([shape for key, shape in original_shapes.items()
                                        if originals[key]["base_z_mm"] < mid < originals[key]["base_z_mm"] + originals[key]["height_mm"]] +
                                       [band for key, band in bands.items() if sections[key][0] < mid < sections[key][1]])
                actual = unary_union([row["footprint"] for row in actual_rows if row["base_z_mm"] < mid < row["top_z_mm"]])
                same_shape("edge full sections obey highest-span T junction priority without missing or extra material", actual, expected,
                           {"system_id": system, "z_mm": [lo, hi]})
                expected_volume += expected.area * (hi - lo)
            volume = sum(row["volume_mm3"] for row in actual_rows)
            check("edge actual closed plate and panel volumes equal complete source sections", abs(volume - expected_volume) <= max(.1, expected_volume * .001),
                  {"system_id": system, "actual_volume_mm3": volume, "expected_volume_mm3": expected_volume})
            report["replacements"].append({"system_id": system, "original_horizontal_count": len(originals),
                                            "actual_horizontal_count": len(horizontal_ids), "actual_panel_count": len(system_panels),
                                            "original_edge_count": len(links), "actual_volume_mm3": volume})
        check("all full-depth panels have original horizontal replacement snapshots", represented_panels == edge_groups,
              {"unbound_panel_ids": sorted(edge_groups - represented_panels)})
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        check("edge full-depth audit has complete consistent source snapshots", False, str(error))
    report["all_pass"] = bool(report["checks"]) and all(item["pass"] for item in report["checks"])
    return report


def validate_building_cut_payload(payload):
    """Check an explicit RC subtraction audit; no material is repaired here."""
    if any(group.get("mode") == "boundary_mesh" for group in payload.get("groups", [])):
        from .whole_slabs import expanded_payload
        payload = expanded_payload(payload)
    groups = {group["id"]: group for group in payload.get("groups", [])}
    cut_parts = {key for key, group in groups.items() if group.get("original_component_id")}
    audit = payload.get("base_component_cut_audit")
    if audit is None:
        return [{"code": "building_cut_audit_required"}] if cut_parts else []
    if not isinstance(audit, dict) or audit.get("schema") != "dwg-building-slab-cuts-v1":
        return [{"code": "invalid_building_cut_audit_schema"}]
    if not audit.get("enabled"):
        return [{"code": "disabled_building_cut_has_modified_parts"}] if cut_parts else []
    errors, seen_parts = [], set()
    try:
        originals = {group["id"]: group for group in audit["original_groups"]}
        targets = audit["target_component_ids"]
        if len(originals) != len(audit["original_groups"]) or set(targets) != set(originals) or len(set(targets)) != len(targets):
            raise ValueError("Cut original snapshots must exactly identify the distinct target IDs")
        if any(group["category"] not in {"curtain", "curtain_accessory"} for group in originals.values()):
            raise ValueError("RC subtraction cannot modify wall or column components")
        cutters = {group["id"]: group for group in audit["cutters"]}
        if len(cutters) != len(audit["cutters"]) or any(groups.get(key) != group or group.get("category") != "slab" for key, group in cutters.items()):
            raise ValueError("Cutters must be exact actual approved RC solid snapshots")
        if {row["original_component_id"] for row in audit["cuts"]} != set(targets) or len(audit["cuts"]) != len(targets):
            raise ValueError("Every target requires exactly one material subtraction audit")
        for row in audit["cuts"]:
            original = originals[row["original_component_id"]]
            ids = row["result_component_ids"]
            if len(set(ids)) != len(ids) or any(key not in groups or key in seen_parts for key in ids):
                raise ValueError("Cut remaining parts must identify distinct actual saved components")
            seen_parts.update(ids)
            if any(key not in cutters for key in row["rc_cutter_component_ids"]):
                raise ValueError("Cut connection contains an unapproved RC cutter")
            for key in ids:
                group = groups[key]
                if group.get("original_component_id", key) != original["id"]:
                    raise ValueError("Remaining material lost its original component identity")
                for field in ("category", "tag", "source_layer", "source_layers", "source_entity_ids", "source_handles"):
                    if group.get(field) != original.get(field):
                        raise ValueError("Subtraction changed original category, Tag or primary source provenance")
            if (abs(row["nominal_base_z_mm"] - original["base_z_mm"]) > .01
                    or abs(row["nominal_top_z_mm"] - original["base_z_mm"] - original["height_mm"]) > .01):
                raise ValueError("Subtraction audit changed the original nominal height range")
            slices = row["z_slices"]
            if row.get("status") == "untouched_no_positive_intersection":
                if row["rc_cutter_component_ids"] or ids != [original["id"]] or groups.get(original["id"]) != original:
                    raise ValueError("A nonintersecting target must remain exactly unchanged")
                if not slices:
                    continue
            cursor = original["base_z_mm"]
            for section in slices:
                lo, hi = _number(section["base_z_mm"], "cut slice base"), _number(section["top_z_mm"], "cut slice top")
                if abs(cursor - lo) > 1e-6 or hi - lo <= 1e-6 or any(key not in row["rc_cutter_component_ids"] for key in section["active_cutter_ids"]):
                    raise ValueError("Subtraction event slices are incomplete or use a foreign cutter")
                _optional_audit_footprint(section["remaining_footprint"])
                cursor = hi
            if not slices or abs(cursor - original["base_z_mm"] - original["height_mm"]) > 1e-6:
                raise ValueError("Subtraction slices do not cover the complete original height")
        if cut_parts - seen_parts:
            raise ValueError("A cut component has no original snapshot audit")
        if not audit.get("all_pass"):
            raise ValueError("RC material subtraction has unresolved preparation checks")
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        errors.append({"code": "invalid_building_cut_audit", "detail": str(error)})
    return errors


def _saved_wall_roof_fit_audit(payload, saved_rows):
    """Re-evaluate actual lowest RC undersides before explicit height projections."""
    from shapely.geometry import GeometryCollection
    from shapely.errors import GEOSException
    _, _, _, unary_union, _ = _geometry()
    audit = payload["wall_under_roof_audit"]
    planned = {group["id"]: group for group in payload["groups"]}
    saved = {row["id"]: row for row in saved_rows}
    report = {"schema": "dwg-saved-wall-under-roof-qa-v1", "checks": [], "changes": []}

    def check(name, passed, detail):
        report["checks"].append({"check": name, "pass": bool(passed), "detail": detail})

    try:
        if audit.get("operation") != "fit_to_lowest_overhead_underside" or not audit.get("all_pass"):
            raise ValueError("Wall underside recipe is incomplete")
        originals = {group["id"]: group for group in audit["original_groups"]}
        roofs = {key: row for key, row in saved.items() if _slab_role(planned[key]) == "overhead_slab"
                 or (_slab_role(planned[key]) == "slab_step_riser" and planned[key].get("host_slab_role") == "overhead_slab")}
        check("wall underside source roof snapshots match actual approved RC", all(group["id"] in roofs and planned[group["id"]] == group
                                                                               for group in audit["roofs"]), None)
        projections = audit.get("height_projection_regions", [])
        for region in projections:
            refs = region["roof_component_ids"]
            check("wall height projection references actual horizontal plates at its exact cap", bool(refs)
                  and all(key in roofs and _slab_role(planned[key]) == "overhead_slab"
                          and abs(roofs[key]["base_z_mm"] - region["cap_z_mm"]) < .01 for key in refs), region["id"])
        for change in audit["changes"]:
            identity = change["original_component_id"]
            original = originals[identity]
            original_shape = _audit_footprint(original)
            bottom, nominal_top = original["base_z_mm"], original["base_z_mm"] + original["height_mm"]
            unclaimed, expected = original_shape, defaultdict(list)
            cap_roofs = defaultdict(list)
            for roof in roofs.values():
                cap_roofs[round(roof["base_z_mm"], 6)].append(roof["footprint"])
            for cap, shapes in sorted(cap_roofs.items()):
                portion = unclaimed.intersection(unary_union(shapes))
                if portion.area <= .1:
                    continue
                expected[cap].append(portion)
                unclaimed = unclaimed.difference(portion)
            actual_cover = original_shape.difference(unclaimed)
            cap_projections = defaultdict(list)
            for region in projections:
                cap_projections[round(region["cap_z_mm"], 6)].append(_audit_footprint(region["footprint"]))
            for cap, shapes in sorted(cap_projections.items()):
                portion = unclaimed.intersection(unary_union(shapes))
                if portion.area <= .1:
                    continue
                expected[cap].append(portion)
                unclaimed = unclaimed.difference(portion)
            uncovered = unclaimed
            if uncovered.area > .1:
                expected[round(nominal_top, 6)].append(uncovered)
            ids = change["result_component_ids"]
            rows = [saved[key] for key in ids if key in saved]
            check("wall underside actual fitted components are present", len(rows) == len(ids), identity)
            check("wall underside preserves original base and source Tag", all(abs(row["base_z_mm"] - bottom) < .01
                  and row.get("tag") == original["tag"] for row in rows), identity)
            expected_volume, trim, extend = 0., 0., 0.
            for cap, shapes in expected.items():
                target = unary_union(shapes)
                actual = unary_union([row["footprint"] for row in rows if abs(row["top_z_mm"] - cap) < .01])
                error = target.symmetric_difference(actual).area
                check("wall actual top follows lowest saved RC or explicit uncovered height projection", error <= max(.1, target.area * 1e-5),
                      {"original_component_id": identity, "cap_z_mm": cap, "difference_area_mm2": error})
                expected_volume += target.area * (cap - bottom)
                trim += target.area * max(0, nominal_top - cap)
                extend += target.area * max(0, cap - nominal_top)
            actual_xy = unary_union([row["footprint"] for row in rows])
            check("wall underside preserves the complete original XY material", actual_xy.symmetric_difference(original_shape).area <= max(.1, original_shape.area * 1e-5), identity)
            check("wall underside has no unaudited cap elevations", all(any(abs(row["top_z_mm"] - cap) < .01 for cap in expected) for row in rows), identity)
            for region in change["cap_regions"]:
                domain = _audit_footprint(region["footprint"])
                if region["coverage"] == "height_projected":
                    check("wall projected height never overrides actual RC material", domain.intersection(actual_cover).area <= .1, region["id"])
            actual_volume = sum(row["volume_mm3"] for row in rows)
            tolerance = max(.1, expected_volume * .001)
            check("wall actual fitted volume includes exactly the required trims and extensions", abs(actual_volume - expected_volume) <= tolerance
                  and abs(change["trimmed_volume_mm3"] - trim) <= tolerance and abs(change["extended_volume_mm3"] - extend) <= tolerance,
                  {"original_component_id": identity, "actual_volume_mm3": actual_volume, "expected_volume_mm3": expected_volume,
                   "trimmed_volume_mm3": trim, "extended_volume_mm3": extend})
            report["changes"].append({"original_component_id": identity, "result_component_ids": ids,
                                       "actual_top_z_mm": sorted({row["top_z_mm"] for row in rows}), "actual_volume_mm3": actual_volume})
    except (KeyError, TypeError, ValueError, AttributeError, GEOSException) as error:
        check("wall underside audit contains complete actual source references", False, str(error))
    report["all_pass"] = bool(report["checks"]) and all(item["pass"] for item in report["checks"])
    return report


def _saved_building_cut_audit(payload, saved_rows):
    """Independently evaluate original-minus-actual-RC at every actual Z event."""
    _, _, _, unary_union, _ = _geometry()
    from shapely.geometry import GeometryCollection
    audit = payload["base_component_cut_audit"]
    saved = {row["id"]: row for row in saved_rows}
    originals = {group["id"]: group for group in audit["original_groups"]}
    report = {"schema": "dwg-saved-building-cut-qa-v1", "checks": [], "cuts": []}

    def check(name, passed, detail):
        report["checks"].append({"check": name, "pass": bool(passed), "detail": detail})

    for row in audit["cuts"]:
        identity = row["original_component_id"]
        original = originals[identity]
        parts = [saved[key] for key in row["result_component_ids"] if key in saved]
        cutters = [saved[key] for key in row["rc_cutter_component_ids"] if key in saved]
        check("saved RC subtraction original parts and cutters present", len(parts) == len(row["result_component_ids"])
              and len(cutters) == len(row["rc_cutter_component_ids"]), identity)
        original_shape = _audit_footprint(original)
        bottom, top = original["base_z_mm"], original["base_z_mm"] + original["height_mm"]
        levels = sorted({round(value, 6) for item in [*parts, *cutters] for value in (item["base_z_mm"], item["top_z_mm"])} | {bottom, top})
        expected_volume, actual_union_volume, sections = 0., 0., []
        for lo, hi in zip(levels, levels[1:]):
            if hi - lo <= 1e-6:
                continue
            mid = (lo + hi) / 2
            original_at_z = original_shape if bottom < mid < top else GeometryCollection()
            occupied = unary_union([part["footprint"] for part in cutters if part["base_z_mm"] < mid < part["top_z_mm"]])
            expected = original_at_z.difference(occupied)
            actual = unary_union([part["footprint"] for part in parts if part["base_z_mm"] < mid < part["top_z_mm"]])
            error = actual.symmetric_difference(expected).area
            check("saved RC subtraction retains exactly original material outside actual RC", error <= max(.1, original_shape.area * 1e-5),
                  {"original_component_id": identity, "z_mm": [lo, hi], "difference_area_mm2": error,
                   "expected_area_mm2": expected.area, "actual_area_mm2": actual.area})
            expected_volume += expected.area * (hi - lo)
            actual_union_volume += actual.area * (hi - lo)
            sections.append({"base_z_mm": lo, "top_z_mm": hi, "remaining_area_mm2": actual.area})
        actual_volume = sum(part["volume_mm3"] for part in parts)
        original_volume = original_shape.area * (top - bottom)
        removed_volume = original_volume - expected_volume
        check("saved RC subtraction remaining signed volume equals independent difference", abs(actual_volume - expected_volume) <= max(.1, original_volume * .001)
              and abs(actual_union_volume - actual_volume) <= max(.1, original_volume * .001),
              {"original_component_id": identity, "actual_volume_mm3": actual_volume, "expected_volume_mm3": expected_volume})
        check("saved RC subtraction audit volume is source consistent", abs(row["remaining_volume_mm3"] - expected_volume) <= max(.1, original_volume * .001)
              and abs(row["removed_volume_mm3"] - removed_volume) <= max(.1, original_volume * .001)
              and abs(row["original_volume_mm3"] - original_volume) <= max(.1, original_volume * .001), identity)
        check("saved RC subtraction preserves original primary Tag", all(part.get("tag") == original["tag"] for part in parts), identity)
        report["cuts"].append({"original_component_id": identity, "result_component_ids": row["result_component_ids"],
                               "original_volume_mm3": original_volume, "actual_remaining_volume_mm3": actual_volume,
                               "removed_volume_mm3": removed_volume, "actual_z_sections": sections})
    report["all_pass"] = all(item["pass"] for item in report["checks"])
    return report


def entity_key(entity):
    """Stable per-instance identity; repeated INSERT handles never collapse."""
    explicit = entity.get("entity_id") or entity.get("id")
    if explicit:
        return str(explicit)
    identity = [entity.get("source_file") or entity.get("source_id") or "source", entity.get("instance_path") or "", entity.get("source_handle") or entity.get("handle") or ""]
    return "entity:" + _json_hash(identity)[:24]


def _entities(document):
    direct = [e for e in document.get("entities", []) if e.get("owner_block", e.get("space", "*Model_Space")) in ("*Model_Space", "Model", "model", "MODEL_SPACE")]
    resolved = [e for e in document.get("resolved_instances", []) if e.get("root_space", "*Model_Space") == "*Model_Space"]
    # INSERT is a container when resolved children exist, not another footprint.
    prefixes = {(_source(e), str(e.get("instance_path", "")).split("/")[0]) for e in resolved}
    container_types = {"INSERT", "BLOCK_REFERENCE"}
    direct = [e for e in direct if not (str(e.get("type", "")).upper() in container_types and (_source(e), str(e.get("handle"))) in prefixes)]
    leaf_paths = defaultdict(set)
    all_paths = defaultdict(set)
    for entity in resolved:
        source, path = _source(entity), str(entity.get("instance_path") or "")
        all_paths[source].add(path)
        if str(entity.get("type", "")).upper() not in container_types:
            leaf_paths[source].add(path)
    materialized = []
    for entity in resolved:
        if str(entity.get("type", "")).upper() in container_types:
            source, path = _source(entity), str(entity.get("instance_path") or "")
            # Native extraction includes the container at its own full path,
            # while its primitive children share that path. Deeper containers
            # add another /handle. Unresolved empty containers stay reviewable.
            has_descendants = path in leaf_paths[source] or (bool(path) and any(candidate.startswith(path + "/") for candidate in all_paths[source]))
            if has_descendants:
                continue
        materialized.append(entity)
    seen = set()
    output = []
    for entity in direct + materialized:
        key = entity_key(entity)
        if key in seen:
            continue
        seen.add(key)
        output.append(entity)
    return output


def model_entities(document: dict) -> list[dict]:
    """Source primitives and unresolved containers considered for model review.

    Inventory, preview and preparation share this function so resolved block
    containers cannot accidentally become duplicate solids or false blockers.
    """
    return _entities(document)


def _raw_layer(entity, policy):
    original = str(entity.get("original_layer", entity.get("layer", "0")))
    effective = str(entity.get("effective_layer", entity.get("layer", original)))
    return effective if original in {"0", "Layer0"} and policy == "effective" else original


def _source_layers(entity, policy):
    layers = entity.get("source_layers")
    # A single source layer must follow the original/effective policy even when
    # a normalization backend also emits an effective-layer array.
    return [str(layer) for layer in layers] if layers and len(layers) > 1 else [_raw_layer(entity, policy)]


def _source(entity):
    return str(entity.get("source_file") or entity.get("source_document_id") or entity.get("source_id") or "source")


def _linework(entity):
    LineString, *_ = _geometry()
    points = entity.get("curve_points_world") or entity.get("curve_points")
    has_bulges = any(isinstance(vertex, dict) and vertex.get("bulge") for vertex in entity.get("vertices_world", entity.get("vertices", []))) or any(entity.get("bulges") or [])
    if not points and (str(entity.get("type")).upper() == "ARC" or has_bulges):
        from .geometry import entity_segments
        segments = entity_segments(entity)
        return [LineString([segment.p1, segment.p2]) for segment in segments if segment.length > 0]
    if not points:
        points = entity.get("vertices_world") or entity.get("vertices")
    if points:
        points = [_point(point) for point in points]
        if entity.get("closed") and points and points[0] != points[-1]:
            points.append(points[0])
        return [LineString([a, b]) for a, b in zip(points, points[1:]) if a != b]
    start = entity.get("start_world") or entity.get("start")
    end = entity.get("end_world") or entity.get("end")
    if start and end and _point(start) != _point(end):
        return [LineString([_point(start), _point(end)])]
    return []


def _footprint(entity, config):
    _, Polygon, *_ = _geometry()
    footprint = config.get("footprint") or entity.get("footprint_world") or entity.get("footprint")
    if footprint:
        if isinstance(footprint, dict):
            return Polygon([_point(p) for p in footprint["outer"]], [[_point(p) for p in ring] for ring in footprint.get("holes", [])]), "explicit_footprint"
        return Polygon([_point(p) for p in footprint]), "explicit_footprint"
    points = entity.get("curve_points_world") or entity.get("curve_points")
    if not points and (any(isinstance(vertex, dict) and vertex.get("bulge") for vertex in entity.get("vertices_world", entity.get("vertices", []))) or any(entity.get("bulges") or [])):
        lines = _linework(entity)
        points = [list(line.coords[0]) for line in lines]
    if not points:
        points = entity.get("vertices_world") or entity.get("vertices")
    if points and entity.get("closed"):
        return Polygon([_point(point) for point in points]), "source_closed_contour"
    return None, None


def _number(value, label, *, positive=False):
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError(f"{label} must be a finite {'positive ' if positive else ''}number")
    return result


def _opening_axis(opening, record):
    axis=record.get("opening_assembly_audit",{}).get("axis_world_mm")
    if axis:
        return axis
    corners=list(opening.minimum_rotated_rectangle.exterior.coords)
    first,second=max(zip(corners,corners[1:]),key=lambda edge:math.dist(*edge))
    length=math.dist(first,second)
    ux,uy=(second[0]-first[0])/length,(second[1]-first[1])/length
    center=opening.centroid
    return [[center.x-ux*length/2,center.y-uy*length/2],[center.x+ux*length/2,center.y+uy*length/2]]


def _source_plane(entity):
    """Describe horizontal CAD elevation without silently flattening 3D plans."""
    default_z = entity.get("elevation", 0)
    if isinstance(default_z, dict):
        default_z = default_z.get("z", 0)
    default_z = _number(default_z, "source elevation")
    points = entity.get("curve_points_world") or entity.get("vertices_world") or entity.get("curve_points") or entity.get("vertices")
    if not points:
        points = [entity[key] for key in ["start_world", "end_world"] if entity.get(key)]
    if not points:
        points = [entity[key] for key in ["start", "end", "center_world", "center", "position_world", "position"] if entity.get(key)]
    elevations = []
    for point in points or []:
        point = point.get("point", point) if isinstance(point, dict) else point
        z = point.get("z", default_z) if isinstance(point, dict) else point[2] if len(point) > 2 else default_z
        elevations.append(_number(z, "source Z"))
    minimum, maximum = (min(elevations), max(elevations)) if elevations else (default_z, default_z)
    normal = entity.get("normal_world") or entity.get("normal") or entity.get("extrusion")
    tilted = False
    if normal:
        nx, ny = _point(normal)
        tilted = abs(nx) > 1e-8 or abs(ny) > 1e-8
    return {"source_plane_z_mm": minimum if maximum - minimum <= .01 and not tilted else None, "source_z_range_mm": [minimum, maximum], "source_nonplanar": maximum - minimum > .01 or tilted}


def _nested_accessory_decision_audit(classification, decisions, entities, payload=None):
    """Resolve only explicit nested-metal requests against exact object choices.

    Classification remains immutable source evidence. Layer defaults and global
    heights cannot acknowledge an independently discovered nested metal leaf.
    """
    requests = classification.get("audits", {}).get("nested_accessory_decision_requests", [])
    if not requests:
        return None, set()
    available = {entity_key(entity) for entity in entities}
    overrides = decisions.get("object_overrides", {})
    audit = {"schema": "dwg-nested-accessory-review-v1", "requests": [], "resolved_request_ids": [], "unresolved_request_ids": []}
    unresolved_sources = set()
    for request in requests:
        source_ids = request.get("source_entity_ids", [])
        source_rows = []
        if not isinstance(source_ids, list) or not source_ids or any(not isinstance(key, str) or not key for key in source_ids):
            source_ids = []
        for identity in source_ids:
            override = overrides.get(identity)
            issues = []
            if identity not in available:
                issues.append("source_entity_not_available")
            if not isinstance(override, dict):
                override = {}
                issues.append("exact_source_entity_id_object_override_required")
            category = override.get("category")
            if category in {"reference", "exclude"}:
                if not isinstance(override.get("reason"), str) or not override["reason"].strip():
                    issues.append("explicit_reference_or_exclusion_reason_required")
            elif category == "curtain_accessory":
                for field, positive in (("base_z_mm", False), ("height_mm", True)):
                    try:
                        if field not in override or isinstance(override[field], bool):
                            raise ValueError("explicit numeric field required")
                        _number(override[field], field, positive=positive)
                    except (TypeError, ValueError, OverflowError):
                        issues.append("explicit_finite_" + field + ("_positive_required" if positive else "_required"))
                if override.get("footprint_confirmed") is not True and not override.get("footprint"):
                    issues.append("explicit_footprint_or_footprint_confirmation_required")
            else:
                issues.append("explicit_curtain_accessory_reference_or_exclude_category_required")
            if payload is not None and not issues:
                field = "groups" if category == "curtain_accessory" else "references" if category == "reference" else "excluded"
                matches = [row for row in payload.get(field, []) if identity in row.get("source_entity_ids", [row.get("id")])]
                if not matches or (category == "curtain_accessory" and any(row.get("category") != category for row in matches)):
                    issues.append("confirmed_source_not_materialized_in_its_selected_disposition")
            status = "unresolved" if issues else "resolved" if payload is not None else "decision_complete_pending_geometry"
            if issues:
                unresolved_sources.add(identity)
            source_rows.append({"source_entity_id": identity, "category": category, "status": status,
                                "reason": override.get("reason"), "semantic_subrole": override.get("semantic_subrole"), "base_z_mm": override.get("base_z_mm"),
                                "height_mm": override.get("height_mm"), "issues": issues})
        resolved = bool(source_rows) and all(not row["issues"] for row in source_rows)
        request_id = request.get("id")
        audit["requests"].append({"request_id": request_id, "source_entity_ids": source_ids,
                                  "source_layer": request.get("source_layer"), "bbox": request.get("bbox"),
                                  "status": "resolved" if resolved and payload is not None else "decision_complete_pending_geometry" if resolved else "unresolved",
                                  "source_decisions": source_rows,
                                  "issues": [] if source_rows else ["request_has_no_valid_source_entity_ids"]})
        audit["resolved_request_ids" if resolved else "unresolved_request_ids"].append(request_id)
    return audit, unresolved_sources


def apply_selection_opening_patches(payload: dict, decisions: dict) -> dict:
    """Apply reviewed CAD-selection openings to any prepared model payload.

    This runs after ordinary, prepared-plan, or slab composition so a local
    correction does not require replacing the established model generator.
    Each patch is still part of ``decisions.json`` and therefore covered by the
    normal review hash and explicit approval gate.
    """
    _, Polygon, _, _, orient = _geometry()
    result = json.loads(json.dumps(payload))
    proposal_reviews = decisions.get("opening_selection_reviews") or []
    for review in proposal_reviews:
        for issue in review.get("blockers") or []:
            result.setdefault("blockers", []).append({
                "code": "cad_selection_opening_proposal_blocked",
                "proposal_sha256": review.get("proposal_sha256"),
                "detail": issue,
            })
    patches = decisions.get("opening_patches") or []
    if not patches:
        return result
    if not isinstance(patches, list):
        result.setdefault("blockers", []).append({"code": "invalid_opening_patches_collection"})
        return result

    audit = result.setdefault("selection_opening_audit", [])
    ordinary_audit = result.setdefault("opening_audit", [])
    applied_ids = {str(item.get("id")) for item in audit}
    source_sha = str(decisions.get("source_sha256") or "")

    def identifiers(group):
        return {
            str(group.get("id") or ""),
            *[str(value) for value in group.get("host_lineage_ids", [])],
            *[str(value) for value in group.get("source_entity_ids", [])],
            *[str(value) for value in group.get("source_handles", [])],
        } - {""}

    def patch_block(code, patch, **extra):
        result.setdefault("blockers", []).append({
            "code": code,
            "opening_patch_id": patch.get("id"),
            "selection_handle": patch.get("selection_handle"),
            **extra,
        })

    for patch in patches:
        if not isinstance(patch, dict) or patch.get("schema") != "dwg-cad-selection-opening-patch-v1":
            patch_block("invalid_selection_opening_patch_schema", patch if isinstance(patch, dict) else {})
            continue
        identity = str(patch.get("id") or "")
        if not identity or identity in applied_ids:
            patch_block("duplicate_or_missing_selection_opening_patch_id", patch)
            continue
        applied_ids.add(identity)
        if not source_sha or patch.get("source_sha256") != source_sha:
            patch_block("selection_opening_source_mismatch", patch)
            continue
        try:
            footprint = patch["footprint"]
            opening = Polygon([_point(point) for point in footprint["outer"]],
                              [[_point(point) for point in ring] for ring in footprint.get("holes", [])])
            if not opening.is_valid or opening.area <= .1:
                raise ValueError("opening footprint must be a valid positive-area polygon")
            bottom = _number(patch.get("base_z_mm", 0), "base_z_mm")
            height = _number(patch.get("height_mm"), "height_mm", positive=True)
            lintel = bottom + height
            requested_hosts = {str(value) for value in patch.get("host_ids") or [] if str(value)}
            if not requested_hosts:
                raise ValueError("selection opening requires explicit host_ids")
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            patch_block("invalid_selection_opening_geometry", patch, detail=str(error))
            continue

        possible = []
        for group in result.get("groups", []):
            if group.get("category") not in {"architectural_wall", "structural_wall", "curtain"}:
                continue
            if not (identifiers(group) & requested_hosts):
                continue
            try:
                host = Polygon(group["outer"], group.get("holes", []))
                host_bottom = _number(group.get("base_z_mm", 0), "host base_z_mm")
                host_height = _number(group.get("height_mm"), "host height_mm", positive=True)
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            host_top = host_bottom + host_height
            if min(lintel, host_top) - max(bottom, host_bottom) <= .001:
                continue
            if host.distance(opening) <= .6:
                possible.append((group, host))
        matched = {value for group, _ in possible for value in identifiers(group)}
        if not possible or not requested_hosts.issubset(matched):
            patch_block("selection_opening_requested_host_missing", patch,
                        host_ids=sorted(requested_hosts - matched))
            continue

        replacements = []
        affected = []
        failed_height = False
        for group, host in possible:
            host_bottom = float(group.get("base_z_mm", 0))
            host_top = host_bottom + float(group["height_mm"])
            cut = host.intersection(opening)
            if cut.area <= .1:
                continue
            if lintel > host_top + .001 or bottom < host_bottom - .001:
                patch_block("selection_opening_outside_host_height", patch, host_id=group.get("id"))
                failed_height = True
                continue
            affected.append(group)
            lineage = sorted({*group.get("host_lineage_ids", []), str(group.get("id"))})
            regions = [(part, host_bottom, host_top - host_bottom, "side")
                       for part in _parts(host.difference(opening))]
            if bottom > host_bottom:
                regions.extend((part, host_bottom, bottom - host_bottom, "sill")
                               for part in _parts(cut))
            if host_top > lintel:
                regions.extend((part, lintel, host_top - lintel, "lintel")
                               for part in _parts(cut))
            for index, (part, z_value, part_height, role) in enumerate(regions):
                part = orient(part, sign=1.0)
                child = dict(group)
                child.update({
                    "id": f"{group['id']}/{identity}/{role}/{index}",
                    "name": f"{group.get('name', group['id'])} {role} {index + 1}",
                    "outer": [list(point) for point in part.exterior.coords][:-1],
                    "holes": [[list(point) for point in ring.coords][:-1] for ring in part.interiors],
                    "base_z_mm": z_value,
                    "height_mm": part_height,
                    "area_mm2": part.area,
                    "host_lineage_ids": lineage,
                    "opening_ids": sorted({*group.get("opening_ids", []), identity}),
                    "opening_part_role": role,
                    "selection_opening_source_entity_ids": list(patch.get("source_entity_ids") or []),
                    "evidence": [*group.get("evidence", []), "reviewed_cad_selection_opening_subtraction"],
                })
                if "volume_mm3" in child:
                    child["volume_mm3"] = part.area * part_height
                replacements.append(child)
        if failed_height:
            continue
        if not affected:
            patch_block("selection_opening_does_not_cut_requested_host", patch)
            continue
        affected_ids = {id(group) for group in affected}
        result["groups"] = [group for group in result.get("groups", []) if id(group) not in affected_ids]
        result["groups"].extend(replacements)
        opening_record = {
            "id": identity,
            "source": "autocad_pickfirst_selection",
            "selection_handle": patch.get("selection_handle"),
            "selection_snapshot_sha256": patch.get("selection_snapshot_sha256"),
            "source_entity_ids": list(patch.get("source_entity_ids") or []),
            "host_ids": sorted(requested_hosts),
            "cut_host_ids": [str(group.get("id")) for group in affected],
            "result_component_ids": [str(group.get("id")) for group in replacements],
            "clearance_footprint": patch["footprint"],
            "base_z_mm": bottom,
            "height_mm": height,
            "footprint_basis": patch.get("footprint_basis"),
        }
        audit.append(opening_record)
        ordinary_audit.append({key: value for key, value in opening_record.items()
                               if key in {"id", "host_ids", "cut_host_ids", "clearance_footprint",
                                          "base_z_mm", "height_mm"}})

    for opening_record in audit:
        try:
            footprint = opening_record["clearance_footprint"]
            opening = Polygon(footprint["outer"], footprint.get("holes", []))
        except (KeyError, TypeError, ValueError):
            continue
        conflicts = []
        for group in result.get("groups", []):
            overlap_z = min(opening_record["base_z_mm"] + opening_record["height_mm"],
                            group["base_z_mm"] + group["height_mm"]) - max(
                                opening_record["base_z_mm"], group["base_z_mm"])
            if overlap_z > .01 and opening.intersection(Polygon(group["outer"], group.get("holes", []))).area > .1:
                conflicts.append(group["id"])
        if conflicts:
            result.setdefault("blockers", []).append({
                "code": "selection_opening_conflicting_solids",
                "opening_patch_id": opening_record["id"],
                "conflicting_solids": conflicts,
            })
    result["overlap_audit"] = _solid_overlaps(result.get("groups", []))
    result["decisions_digest"] = _json_hash(decisions)
    return result


def prepare_model(document: dict, classification: dict, decisions: dict) -> dict:
    """Create a deterministic, fully reviewable geometry payload in millimeters.

    A layer decision is required for every considered source object, unless an
    object override supplies its category. `footprint_confirmed` acknowledges a
    closed wall/curtain contour; alternatively `thickness_mm` verifies a narrow
    region against a user supplied width. Open wall sides are paired only at
    that width and ambiguous or uncovered sides remain blocking references.
    """
    LineString, Polygon, polygonize, unary_union, orient = _geometry()
    payload = {"schema": "dwg-skp-model-v2", "units": "mm", "groups": [], "references": [], "blockers": [], "warnings": [], "excluded": [], "tag_mapping": [], "coordinate_policy": "source XY in mm retained; Z per approved base_z_mm", "source_tag_policy": decisions.get("source_tag_policy", "effective"), "source_manifest": document.get("intake", document.get("metadata", {})), "document_digest": _json_hash(document), "decisions_digest": _json_hash(decisions), "classification_digest": _json_hash(classification)}
    for field in ("slab_audit", "slab_join_policy"):
        if field in decisions:
            payload[field] = json.loads(json.dumps(decisions[field]))
    floor_datum = decisions.get("floor_datum_m")
    if floor_datum is not None:
        try:
            payload["floor_datum_m"] = _number(floor_datum, "floor_datum_m")
            payload["coordinate_policy"] = f"source XY in mm retained; model Z=0 corresponds to floor H={payload['floor_datum_m']:g} m; Z per approved base_z_mm"
        except (TypeError, ValueError) as exc:
            payload["blockers"].append({"code": "invalid_floor_datum", "detail": str(exc)})
    if payload["source_tag_policy"] not in {"effective", "original"}:
        payload["blockers"].append({"code": "invalid_source_tag_policy"})
        return payload
    entities = _entities(document)
    _, unresolved_nested_sources = _nested_accessory_decision_audit(classification, decisions, entities)
    mapping = decisions.get("layer_mapping", {})
    overrides = decisions.get("object_overrides", {})
    handles = Counter(str(e.get("handle") or e.get("source_handle") or "") for e in entities)
    tag_sources = defaultdict(set)
    for entity in entities:
        for layer in _source_layers(entity, payload["source_tag_policy"]):
            tag_sources[layer].add(_source(entity))
    used_tags = {"Layer0", "Untagged"}
    tag_lookup = {}
    for layer in sorted(tag_sources):
        sources = sorted(tag_sources[layer])
        for source in sources:
            candidate = f"{source}::{layer}" if len(sources) > 1 else layer
            reason = "source_collision" if len(sources) > 1 else "original_name"
            if candidate in used_tags or candidate in {"0", ""}:
                candidate = f"DWG::{candidate or 'empty'}"
                reason = "reserved_name" if len(sources) == 1 else reason
            seed = candidate
            suffix = 1
            while candidate in used_tags:
                suffix += 1
                candidate = f"{seed} [{suffix}]"
            used_tags.add(candidate)
            tag_lookup[(source, layer)] = candidate
            payload["tag_mapping"].append({"source_id": source, "source_layer": layer, "tag": candidate, "reason": reason})
    pending = []
    paired = defaultdict(list)
    represented = set()

    def block(code, record, **extra):
        payload["blockers"].append({"code": code, "entity_id": record["id"], "source_layer": record["source_layer"], **extra})

    def reference(record, reason):
        if record["id"] in represented:
            return
        represented.add(record["id"])
        payload["references"].append({**record, "mode": "reference_edges", "reason": reason})

    def make_group(polygon, record, evidence):
        polygon = orient(polygon, sign=1.0)
        # The solid's current outer/holes are authoritative. A recognized
        # source footprint must not survive as stale geometry after slicing
        # a host or creating its door lintel.
        group = {key: value for key, value in record.items() if key not in {"segments", "config", "footprint"}}
        group.update({"mode": "solid_extrusion", "outer": [list(p) for p in polygon.exterior.coords][:-1], "holes": [[list(p) for p in ring.coords][:-1] for ring in polygon.interiors], "evidence": evidence, "area_mm2": polygon.area})
        payload["groups"].append(group)
        represented.update(record.get("source_entity_ids", [record["id"]]))
        return group

    from .recognized_model import recognized_records
    partition_audit={}
    recognized_materials, recognized_openings, recognized_sources, recognition_blockers = recognized_records(
        document, classification, decisions, tag_lookup,partition_audit=partition_audit)
    if partition_audit:payload['source_material_partition_audit']=partition_audit
    payload["blockers"].extend(recognition_blockers)
    recognition_pending_sources={key for row in recognition_blockers for key in row.get("source_entity_ids",[])}
    payload["automatic_recognition_transfer"] = {
        "material_candidate_ids": [row["id"] for row in recognized_materials],
        "opening_candidate_ids": [row["id"] for row in recognized_openings],
        "source_entity_ids": sorted(recognized_sources)}
    for row in recognized_materials:
        make_group(Polygon(row["footprint"]["outer"], row["footprint"].get("holes", [])), row,
                   ["source_bound_automatic_plan_topology", "confirmed_category_height_and_footprint"])
    for row in recognized_openings:
        pending.append((Polygon(row["footprint"]["outer"], row["footprint"].get("holes", [])), row))

    for entity in entities:
        key = entity_key(entity)
        if key in recognized_sources:
            continue
        raw = _raw_layer(entity, payload["source_tag_policy"])
        config = dict(mapping.get(raw) or {})
        override = overrides.get(key)
        handle = str(entity.get("handle") or entity.get("source_handle") or "")
        if override is None and handles[handle] == 1:
            override = overrides.get(handle)
        if override:
            config.update({key: value for key, value in override.items() if value is not None})
        source = _source(entity)
        layers = list(dict.fromkeys(_source_layers(entity, payload["source_tag_policy"])))
        primary = config.get("primary_source_layer")
        if len(layers) > 1 and primary not in layers:
            payload["blockers"].append({"code": "ambiguous_primary_source_layer", "entity_id": key, "source_layers": layers})
            continue
        raw = str(primary) if primary else raw
        category = config.get("category")
        record = {"id": key, "name": str(config.get("name") or key), "category": category, "source_layer": raw, "source_layers": layers, "source_handles": list(entity.get("source_handles") or ([handle] if handle else [])), "source_entity_ids": [key], "source_id": source, "tag": tag_lookup.get((source, raw), raw), "original_layer": entity.get("original_layer", entity.get("layer")), "effective_layer": entity.get("effective_layer", entity.get("layer")), "block_layer": entity.get("block_layer"), "instance_path": entity.get("instance_path"), "config": config}
        if isinstance(override, dict) and override.get("semantic_subrole") is not None:
            record["semantic_subrole"] = override["semantic_subrole"]
        try:
            lines = _linework(entity)
            record["segments"] = [{"start": list(line.coords[0]), "end": list(line.coords[-1])} for line in lines]
            if key in recognition_pending_sources:
                reference(record,"automatic_material_or_opening_assembly_requires_decision")
                continue
            if key in unresolved_nested_sources:
                reference(record, "nested_accessory_requires_exact_object_semantic_and_dimension_confirmation")
                continue
            if category not in CATEGORIES:
                block("missing_or_invalid_category", record)
                reference(record, "category_requires_confirmation")
                continue
            if category == "exclude":
                if not str(config.get("reason") or "").strip():
                    block("source_exclusion_requires_explicit_reason",record)
                    reference(record,"exclusion_reason_requires_confirmation")
                    continue
                payload["excluded"].append({"id": key, "source_layer": raw, "source_handles": record["source_handles"], "reason": config["reason"]})
                continue
            if category == "reference":
                record["base_z_mm"] = _number(config.get("base_z_mm", 0), "base_z_mm")
                reference(record, "user_confirmed_reference")
                continue
            record.update(_source_plane(entity))
            if record["source_nonplanar"] and not (config.get("project_to_xy") is True or config.get("footprint")):
                block("nonhorizontal_source_requires_projection_confirmation", record, source_z_range_mm=record["source_z_range_mm"])
                reference(record, "nonhorizontal_source")
                continue
            if record["source_nonplanar"]:
                record["projection_authority"] = "user_explicit_footprint" if config.get("footprint") else "user_confirmed_project_to_xy"
            base = _number(config.get("base_z_mm", 0), "base_z_mm")
            height_value = config.get("height_mm")
            if category == "slab":
                role = config.get("slab_role") or config.get("semantic_role")
                if role not in {"floor_slab", "overhead_slab"}:
                    block("slab_role_requires_confirmation", record)
                    reference(record, "slab_role_requires_confirmation")
                    continue
                record.update(slab_role=role, semantic_role=role, slab_zone_id=config.get("slab_zone_id") or key,
                              slab_system_id=config.get("slab_system_id") or role)
                if height_value is None:
                    height_value = config.get("thickness_mm")
            if height_value is None:
                height_value = decisions.get("opening_height_mm") if category == "opening" else decisions.get("default_height_mm") if category != "slab" else None
            if height_value is None:
                block("height_requires_confirmation", record)
                reference(record, "height_requires_confirmation")
                continue
            record.update(base_z_mm=base, height_mm=_number(height_value, "height_mm", positive=True))
            if category == "slab":
                record["top_z_mm"] = base + record["height_mm"]
            polygon, basis = _footprint(entity, config)
            if polygon is not None:
                if not polygon.is_valid or polygon.area <= 0:
                    block("invalid_source_footprint", record)
                    reference(record, "invalid_source_footprint")
                    continue
                if category in {"architectural_wall", "structural_wall", "curtain"} and basis != "explicit_footprint" and not config.get("footprint_confirmed"):
                    thickness = config.get("thickness_mm")
                    if thickness is None or not polygon.buffer(-_number(thickness, "thickness_mm", positive=True) * 0.55).is_empty:
                        block("closed_contour_may_be_room", record, required="footprint_confirmed or measured thickness_mm")
                        reference(record, "footprint_requires_confirmation")
                        continue
                if category == "opening":
                    pending.append((polygon, record))
                else:
                    make_group(polygon, record, [basis, "approved_category", "approved_height"])
            elif category in {"architectural_wall", "structural_wall", "curtain"} and lines and config.get("thickness_mm"):
                width = _number(config["thickness_mm"], "thickness_mm", positive=True)
                bucket = (source, raw, category, base, record["height_mm"], width)
                paired[bucket].append((record, lines))
            else:
                block("unresolved_footprint", record, source_type=entity.get("type"))
                reference(record, "unsupported_or_open_footprint")
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            block("invalid_geometry_or_decision", record, detail=str(exc))
            reference(record, "invalid_geometry_or_decision")

    # Pair only measured sides at the explicitly approved thickness. A side with
    # several possible partners is ambiguous, not a license to fill all strips.
    for bucket, items in paired.items():
        width = bucket[-1]
        lines = [(record, line) for record, paths in items for line in paths]
        candidates = defaultdict(list)
        for i, (_, first) in enumerate(lines):
            a, b = first.coords[0], first.coords[-1]
            length = first.length
            ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
            nx, ny = -uy, ux
            for j in range(i + 1, len(lines)):
                second = lines[j][1]
                c, d = second.coords[0], second.coords[-1]
                if abs(ux * (d[1] - c[1]) - uy * (d[0] - c[0])) / second.length > 1e-6:
                    continue
                offset = (c[0] - a[0]) * nx + (c[1] - a[1]) * ny
                if abs(abs(offset) - width) > max(0.5, width * .005):
                    continue
                c0, c1 = sorted(((c[0] - a[0]) * ux + (c[1] - a[1]) * uy, (d[0] - a[0]) * ux + (d[1] - a[1]) * uy))
                lo, hi = max(0.0, c0), min(length, c1)
                if hi - lo <= 0.5:
                    continue
                vertices = [(a[0] + ux * lo, a[1] + uy * lo), (a[0] + ux * hi, a[1] + uy * hi), (a[0] + ux * hi + nx * offset, a[1] + uy * hi + ny * offset), (a[0] + ux * lo + nx * offset, a[1] + uy * lo + ny * offset)]
                polygon = Polygon(vertices)
                candidates[i].append((j, polygon))
                candidates[j].append((i, polygon))
        strips = []
        contributors = []
        for i in sorted(candidates):
            if len(candidates[i]) != 1:
                continue
            j, polygon = candidates[i][0]
            if i < j and len(candidates[j]) == 1:
                strips.append(polygon)
                contributors.extend((lines[i][0], lines[j][0]))
        solid = unary_union(strips)
        coverage = solid.buffer(0.6)
        for record, paths in items:
            missing = sum(line.difference(coverage).length for line in paths)
            if missing > 0.6:
                block("unpaired_boundary", record, uncovered_length_mm=round(missing, 3))
                reference(record, "unpaired_boundary")
        for index, polygon in enumerate(_parts(solid)):
            nearby = [record for record, paths in items if any(path.intersection(polygon.buffer(.6)).length > .1 for path in paths)]
            if not nearby:
                continue
            record = dict(nearby[0])
            record.update(id=f"pair:{_json_hash([r['id'] for r in nearby])[:16]}:{index}", name=f"{bucket[1]} [{index + 1}]", source_handles=sorted({h for r in nearby for h in r["source_handles"]}), source_entity_ids=sorted({r["id"] for r in nearby}))
            make_group(polygon, record, ["paired_observed_boundaries", {"approved_thickness_mm": width}])

    # Opening footprints are a separate user decision. They must intersect a
    # named host or connect hosts at both jambs; a free-floating symbol blocks.
    opening_audit = []
    for opening, record in pending:
        host_ids = set(record["config"].get("host_ids") or [])
        possible = []
        for group in payload["groups"]:
            if group["category"] not in {"architectural_wall", "structural_wall", "curtain"}:
                continue
            if host_ids and not ({group["id"], *group["source_entity_ids"], *group["source_handles"]} & host_ids):
                continue
            footprint = Polygon(group["outer"], group["holes"])
            opening_top = record["base_z_mm"] + record["height_mm"]
            host_top = group["base_z_mm"] + group["height_mm"]
            if min(opening_top, host_top) - max(record["base_z_mm"], group["base_z_mm"]) <= .001:
                continue
            if footprint.distance(opening) <= .6:
                possible.append((group, footprint))
        from .opening_recognition import host_footprint
        recognition_settings=classification.get('automatic_plan',{}).get('settings',{})
        measured_frame=record.get('opening_assembly_audit',{}).get('host_audit',{}).get('status')=='opposite_jamb_faces_resolved'
        gap_hosts=host_footprint(_opening_axis(opening,record),[group for group,_ in possible],
            search=recognition_settings.get('door_host_search_mm',450),
            recess=recognition_settings.get('maximum_frame_recess_mm',150),
            allow_frame_bearing=measured_frame)if possible else{}
        if not possible or (not host_ids and not any(p.intersection(opening).area > .1 for _, p in possible)
                            and gap_hosts.get("status")!="opposite_jamb_faces_resolved"):
            block("opening_hosts_unresolved", record)
            reference(record, "opening_hosts_unresolved")
            continue
        if host_ids:
            matched = {value for group, _ in possible for value in [group["id"], *group["source_entity_ids"], *group["source_handles"]]}
            if not host_ids.issubset(matched):
                block("opening_requested_host_missing", record, host_ids=sorted(host_ids - matched))
                reference(record, "opening_requested_host_missing")
                continue
        replacements = []
        affected = []
        for group, footprint in possible:
            base, top = group["base_z_mm"], group["base_z_mm"] + group["height_mm"]
            bottom, lintel = record["base_z_mm"], record["base_z_mm"] + record["height_mm"]
            cut = footprint.intersection(opening)
            if lintel > top + .001 or bottom < base - .001:
                block("opening_outside_host_height", record, host_id=group["id"])
                continue
            if cut.area <= .1:
                continue
            payload["groups"].remove(group)
            affected.append(group["id"])
            regions = [(part, base, top - base, "side") for part in _parts(footprint.difference(opening))]
            if bottom > base:
                regions += [(part, base, bottom - base, "sill") for part in _parts(cut)]
            if top > lintel:
                regions += [(part, lintel, top - lintel, "lintel") for part in _parts(cut)]
            for index, (part, z, height, role) in enumerate(regions):
                child = dict(group)
                child.update(id=f"{group['id']}/{record['id']}/{role}/{index}", name=f"{group['name']} {role} {index + 1}", base_z_mm=z, height_mm=height, opening_part_role=role, opening_ids=sorted(set(group.get("opening_ids", []) + [record["id"]])))
                replacements.append((part, child))
        for part, child in replacements:
            make_group(part, child, child["evidence"] + ["approved_opening_subtraction"])
        if not affected:
            # Existing plan gaps need an explicit bridge between two touching
            # jamb hosts. Geometry and heights must agree. Different source
            # materials need an explicit, source-supported primary-layer choice.
            contacts = [(group, footprint.intersection(opening.boundary)) for group, footprint in possible]
            signatures = {(group["category"], group["tag"], group["base_z_mm"], group["height_mm"]) for group, _ in possible}
            farthest = max((first.distance(second) for _, first in contacts for _, second in contacts if not first.is_empty and not second.is_empty), default=0)
            extent = max(opening.bounds[2] - opening.bounds[0], opening.bounds[3] - opening.bounds[1])
            chosen_host=possible[0][0]
            if len(signatures)!=1:
                primary=record['config'].get('lintel_primary_source_layer')
                choices=[g for g,_ in possible if g['source_layer']==primary]
                verticals={(g['base_z_mm'],g['height_mm'])for g,_ in possible}
                choices_valid=choices and len({(g['category'],g['tag'])for g in choices})==1
                chosen_host=choices[0]if choices_valid and len(verticals)==1 else None
            if chosen_host is None or gap_hosts.get("status")!="opposite_jamb_faces_resolved":
                block("opening_jambs_or_lintel_ambiguous", record)
            else:
                host = chosen_host
                top = host["base_z_mm"] + host["height_mm"]
                lintel = record["base_z_mm"] + record["height_mm"]
                if lintel > top:
                    block("opening_outside_host_height", record)
                elif top > lintel:
                    measured = gap_hosts
                    if measured.get("status") != "opposite_jamb_faces_resolved":
                        block("lintel_host_faces_unresolved", record, host_face_audit=measured)
                    else:
                        lintel_shape = Polygon(measured["footprint"]["outer"], measured["footprint"].get("holes", []))
                        child = dict(host)
                        child.update(id=f"lintel:{record['id']}", name=f"{record['name']} lintel", base_z_mm=lintel, height_mm=top - lintel, source_handles=sorted({handle for g, _ in possible for handle in g["source_handles"]}), source_entity_ids=sorted({key for g, _ in possible for key in g["source_entity_ids"]}), opening_ids=[record["id"]], lintel_host_face_audit=measured,
                            source_layers=sorted({layer for g,_ in possible for layer in g['source_layers']}),
                            semantic_role='door_lintel',opening_part_role='lintel')
                        if len(signatures)!=1:
                            child['lintel_primary_source_layer_decision']=record['config']['lintel_primary_source_layer']
                        traces={p['entity_id']:p for g,_ in possible for p in g.get('source_provenance',[])}
                        if traces:child['source_provenance']=[traces[key]for key in sorted(traces)]
                        make_group(lintel_shape, child, ["measured_opposite_jamb_and_wall_side_faces", "host_category_and_tag_inherited"])
        reference(record, "approved_opening_reference")
        opening_audit.append({"id": record["id"], "host_ids": [g["id"] for g, _ in possible], "cut_host_ids": affected, "clearance_footprint": {"outer": [list(p) for p in opening.exterior.coords][:-1], "holes": [[list(p) for p in ring.coords][:-1] for ring in opening.interiors]}, "base_z_mm": record["base_z_mm"], "height_mm": record["height_mm"]})
    if not payload["groups"]:
        payload["blockers"].append({"code": "no_solid_components"})
    for opening in opening_audit:
        footprint = Polygon(opening["clearance_footprint"]["outer"], opening["clearance_footprint"].get("holes", []))
        conflicts = []
        for group in payload["groups"]:
            overlap_z = min(opening["base_z_mm"] + opening["height_mm"], group["base_z_mm"] + group["height_mm"]) - max(opening["base_z_mm"], group["base_z_mm"])
            if overlap_z > .01 and footprint.intersection(Polygon(group["outer"], group["holes"])).area > .1:
                conflicts.append(group["id"])
        if conflicts:
            payload["blockers"].append({"code": "opening_conflicting_solids", "entity_id": opening["id"], "conflicting_solids": conflicts})
    payload["opening_audit"] = opening_audit
    payload["overlap_audit"] = _solid_overlaps(payload["groups"])
    join_result = evaluate_slab_joins(payload["groups"], policy=payload.get("slab_join_policy"))
    if payload.get("slab_join_policy") is not None:
        payload["slab_join_audit"] = join_result
    payload["blockers"].extend(join_result["invalid_policy"])
    payload["blockers"].extend(validate_slab_payload(payload))
    for conflict in join_result["unresolved_overlaps"]:
        payload["blockers"].append({"code": "overlapping_solid_components", **conflict})
    semantic_audit, _ = _nested_accessory_decision_audit(classification, decisions, entities, payload)
    if semantic_audit is not None:
        payload["semantic_review_audit"] = semantic_audit
        for request in semantic_audit["requests"]:
            if request["status"] != "resolved":
                issue = {"code": "nested_accessory_decision_requires_confirmation", **request}
                payload["blockers"].append(issue)
                payload["warnings"].append({"code": "nested_accessory_not_modeled_pending_confirmation",
                                            "request_id": request["request_id"], "source_entity_ids": request["source_entity_ids"]})
    # Config is provenance, but storing large inline footprint copies in every
    # reference makes the review unnecessarily heavy. Decisions digest binds it.
    for reference_record in payload["references"]:
        reference_record.pop("config", None)
    from .source_accounting import attach_final_source_accounting
    payload["blockers"].extend(attach_final_source_accounting(document, payload, decisions, classification))
    payload["counts"] = {"source_entities": len(entities), "solids": len(payload["groups"]), "references": len(payload["references"]), "excluded": len(payload["excluded"]), "blockers": len(payload["blockers"])}
    return payload


def _signed_area(ring):
    return sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:] + ring[:1])) / 2


def _extrusion(group):
    outer = [list(point) for point in group["outer"]]
    holes = [[list(point) for point in ring] for ring in group.get("holes", [])]
    if _signed_area(outer) < 0:
        outer.reverse()
    for ring in holes:
        if _signed_area(ring) > 0:
            ring.reverse()
    base = group["base_z_mm"]
    top = base + group["height_mm"]
    bottom = lambda ring: [[*point, base] for point in ring]
    upper = lambda ring: [[*point, top] for point in ring]
    faces = [(list(reversed(bottom(outer))), [list(reversed(bottom(ring))) for ring in holes]), (upper(outer), [upper(ring) for ring in holes])]
    for ring in [outer, *holes]:
        for a, b in zip(ring, ring[1:] + ring[:1]):
            faces.append(([[*a, base], [*b, base], [*b, top], [*a, top]], []))
    return faces


def _volume(triangles):
    points = [point for triangle in triangles for point in triangle]
    if not points:
        return 0.0
    origin = [sum(point[i] for point in points) / len(points) for i in range(3)]
    volume = 0.0
    for triangle in triangles:
        a, b, c = [[point[i] - origin[i] for i in range(3)] for point in triangle]
        cross = [b[1] * c[2] - b[2] * c[1], b[2] * c[0] - b[0] * c[2], b[0] * c[1] - b[1] * c[0]]
        volume += sum(a[i] * cross[i] for i in range(3)) / 6
    return volume


def _depth_raster(records, width, height):
    """Orthographic triangle z-buffer; average-depth painter order is unsafe."""
    import numpy as np
    rgba=np.zeros((height,width,4),dtype=np.uint8)
    depth=np.full((height,width),-np.inf,dtype=np.float64)
    for record in records:
        p=np.asarray(record['points'],dtype=float)
        left=max(0,int(math.floor(p[:,0].min())))
        right=min(width,int(math.ceil(p[:,0].max())))
        top=max(0,int(math.floor(p[:,1].min())))
        bottom=min(height,int(math.ceil(p[:,1].max())))
        if left>=right or top>=bottom:continue
        x=np.arange(left,right,dtype=float)[None,:]+.5
        y=np.arange(top,bottom,dtype=float)[:,None]+.5
        a,b,c=p
        denominator=(b[1]-c[1])*(a[0]-c[0])+(c[0]-b[0])*(a[1]-c[1])
        if abs(denominator)<1e-12:continue
        w0=((b[1]-c[1])*(x-c[0])+(c[0]-b[0])*(y-c[1]))/denominator
        w1=((c[1]-a[1])*(x-c[0])+(a[0]-c[0])*(y-c[1]))/denominator
        w2=1-w0-w1
        z=w0*record['depths'][0]+w1*record['depths'][1]+w2*record['depths'][2]
        current=depth[top:bottom,left:right]
        visible=(w0>=-1e-9)&(w1>=-1e-9)&(w2>=-1e-9)&(z>current)
        current[visible]=z[visible]
        color=[int(record['fill'][i:i+2],16)for i in (1,3,5)]+[255]
        rgba[top:bottom,left:right][visible]=color
    return rgba


def _rgba_png(rgba):
    """Encode the preview without introducing an optional imaging dependency."""
    height,width,_=rgba.shape
    def chunk(kind,data):
        return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data)&0xffffffff)
    raw=b''.join(b'\x00'+row.tobytes()for row in rgba)
    return (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!IIBBBBB',width,height,8,6,0,0,0))+
            chunk(b'IDAT',zlib.compress(raw))+chunk(b'IEND',b''))


def _render_saved_previews(mesh, saved_solids, destination):
    """Render the reopened SKP, using no prepared geometry as a substitute.

    SVG is always available. Matplotlib PNGs are additional previews when that
    optional renderer is installed; their absence never weakens geometry QA.
    """
    width, height = 1400, 1000
    categories = sorted({group["category"] for group, _ in saved_solids})
    palette = ["#94a3b8", "#d5a96b", "#73a99b", "#8ab7d0", "#b8a1bd"]
    colors = {category: palette[index % len(palette)] for index, category in enumerate(categories)}
    sqrt2, sqrt6 = math.sqrt(2), math.sqrt(6)
    points = [point for item in mesh for triangle in item["triangles"] for point in triangle]
    center = [sum(point[index] for point in points) / len(points) for index in range(3)]

    def project(point):
        x, y, z = [point[index] - center[index] for index in range(3)]
        return ((x + y) / sqrt2, (x - y) / sqrt6 - z * math.sqrt(2 / 3))

    overview = []
    for item in mesh:
        for triangle in item["triangles"]:
            a, b, c = triangle
            u, v = [[point[index] - a[index] for index in range(3)] for point in (b, c)]
            normal = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]]
            norm = math.sqrt(sum(value * value for value in normal))
            if norm <= 1e-12 or normal[0] - normal[1] + normal[2] <= 0:
                continue
            normal = [value / norm for value in normal]
            light = max(0, normal[0] * -.35 + normal[1] * -.45 + normal[2] * .82)
            shade = max(145, min(246, round(173 + 73 * light)))
            color = f"#{shade:02x}{shade:02x}{max(0, shade - 3):02x}"
            depths=[point[0]-point[1]+point[2]for point in triangle]
            overview.append({"points": [project(point) for point in triangle], "fill": color, "depth": sum(depths)/3,
                             "depths":depths,"id": item["id"]})
    overview.sort(key=lambda item: (item["depth"], item["id"]))
    plan = []
    for group, polygon in sorted(saved_solids, key=lambda item: (item[0]["base_z_mm"], item[0]["id"])):
        for part in _parts(polygon):
            plan.append({"rings": [[[point[0] - center[0], -(point[1] - center[1])] for point in ring.coords] for ring in [part.exterior, *part.interiors]], "fill": colors[group["category"]], "id": group["id"]})
    previews = []
    for name, title, records in [("overview", "重新讀入 SKP｜白模總覽", overview), ("plan", "重新讀入 SKP｜平面", plan)]:
        plotted = [point for item in records for ring in item.get("rings", [item.get("points", [])]) for point in ring]
        if not plotted:
            raise RuntimeError(f"Reopened model produced no {name} preview geometry")
        min_x, max_x = min(point[0] for point in plotted), max(point[0] for point in plotted)
        min_y, max_y = min(point[1] for point in plotted), max(point[1] for point in plotted)
        scale = min((width - 140) / max(max_x - min_x, 1), (height - 240) / max(max_y - min_y, 1))
        offset_x = (width - (max_x - min_x) * scale) / 2
        offset_y = 130 + (height - 240 - (max_y - min_y) * scale) / 2
        screen = lambda point: ((point[0] - min_x) * scale + offset_x, (point[1] - min_y) * scale + offset_y)
        svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">', '<rect width="100%" height="100%" fill="#f6f7f8"/>', f'<text x="60" y="54" font-family="Segoe UI, Microsoft JhengHei, sans-serif" font-size="28" fill="#17212e">{escape(title)}</text>', f'<text x="60" y="84" font-family="Segoe UI, sans-serif" font-size="15" fill="#536274">{escape(destination.stem)} · {len(saved_solids)} saved solids · source coordinates in mm</text>']
        rendered = []
        for record in records:
            if "points" in record:
                projected = [screen(point) for point in record["points"]]
                rendered.append({"points": projected, "fill": record["fill"],"depths":record['depths']})
            else:
                rings = [[screen(point) for point in ring] for ring in record["rings"]]
                path = " ".join("M " + " L ".join(f"{x:.3f},{y:.3f}" for x, y in ring) + " Z" for ring in rings)
                svg.append(f'<path d="{path}" fill="{record["fill"]}" fill-opacity="0.85" fill-rule="evenodd" stroke="#344457" stroke-width="0.8"><title>{escape(record["id"])}</title></path>')
                rendered.append({"rings": rings, "fill": record["fill"]})
        depth_image=None
        if name=='overview':
            depth_image=_depth_raster(rendered,width,height)
            encoded=base64.b64encode(_rgba_png(depth_image)).decode('ascii')
            visible_ids=json.dumps(sorted({record['id']for record in records}),ensure_ascii=False)
            svg.append(f'<metadata id="reopened-mesh-component-ids">{escape(visible_ids)}</metadata>')
            svg.append(f'<image x="0" y="0" width="{width}" height="{height}" href="data:image/png;base64,{encoded}"/>')
        legend = " · ".join(categories) if name == "plan" else "Geometry rendered from triangles in the reopened SKP"
        svg.append(f'<text x="60" y="{height - 34}" font-family="Segoe UI, sans-serif" font-size="14" fill="#536274">{escape(legend)}</text></svg>')
        svg_path = destination.with_suffix(f".{name}.svg")
        svg_path.write_text("\n".join(svg), encoding="utf-8")
        previews.append({"view": name, "format": "svg", "path": str(svg_path), "source": "reopened_skp"})
        try:
            import matplotlib
            matplotlib.use("Agg")
            from matplotlib import pyplot as plt
            from matplotlib.path import Path as PlotPath
            from matplotlib.patches import PathPatch
            figure, axes = plt.subplots(figsize=(14, 10), dpi=100)
            figure.patch.set_facecolor("#f6f7f8")
            axes.set_facecolor("#f6f7f8")
            if name == "overview":
                axes.imshow(depth_image,extent=(0,width,height,0),interpolation='nearest')
            else:
                for item in rendered:
                    vertices, codes = [], []
                    for index, ring in enumerate(item["rings"]):
                        # Matplotlib uses nonzero fill; exterior and holes must
                        # have opposite winding, unlike SVG even-odd fill.
                        ring = list(ring)
                        if (_signed_area(ring[:-1]) > 0) != (index == 0):
                            ring.reverse()
                        vertices.extend(ring)
                        codes.extend([PlotPath.MOVETO] + [PlotPath.LINETO] * (len(ring) - 2) + [PlotPath.CLOSEPOLY])
                    axes.add_patch(PathPatch(PlotPath(vertices, codes), facecolor=item["fill"], edgecolor="#344457", linewidth=.6, alpha=.85))
            axes.set_xlim(0, width)
            axes.set_ylim(height, 0)
            axes.set_aspect("equal")
            axes.axis("off")
            axes.text(60, 45, f"Reopened SKP - {name}", fontsize=18, color="#17212e")
            axes.text(60, 78, f"{destination.stem} | {len(saved_solids)} saved solids", fontsize=10, color="#536274")
            axes.text(60, height - 30, legend, fontsize=9, color="#536274")
            figure.subplots_adjust(left=0, right=1, bottom=0, top=1)
            png_path = destination.with_suffix(f".{name}.png")
            figure.savefig(png_path, dpi=100, facecolor=figure.get_facecolor())
            plt.close(figure)
            previews.append({"view": name, "format": "png", "path": str(png_path), "source": "reopened_skp"})
        except ImportError:
            pass
    return previews


def _saved_previews(mesh, saved_solids, destination, *, overhead_visible=False):
    """Default overview omits overhead slabs; add a complete slab assembly view."""
    overhead = {group["id"] for group, _ in saved_solids if group.get("category") == "slab"
                and (_slab_role(group) == "overhead_slab" or group.get("host_slab_role") == "overhead_slab")}
    if not overhead:
        return _render_saved_previews(mesh, saved_solids, destination)
    visible = [(group, footprint) for group, footprint in saved_solids if group["id"] not in overhead]
    previews = (_render_saved_previews(mesh, saved_solids, destination) if overhead_visible else
                _render_saved_previews([item for item in mesh if item["id"] not in overhead], visible, destination) if visible else [])
    assembly_path = destination.with_name(destination.stem + ".slab_assembly" + destination.suffix)
    complete = _render_saved_previews(mesh, saved_solids, assembly_path)
    previews.extend({**item, "view": "slab_assembly_" + item["view"], "includes_overhead_slabs": True} for item in complete)
    if overhead_visible and visible:
        interior_path = destination.with_name(destination.stem + ".interior" + destination.suffix)
        interior = _render_saved_previews([item for item in mesh if item["id"] not in overhead], visible, interior_path)
        previews.extend({**item, "view": "interior_" + item["view"], "includes_overhead_slabs": False} for item in interior)
    return previews


def _component_container(group):
    if group.get("category") == "slab":
        return "overhead_slab" if _slab_role(group) == "slab_step_riser" and group.get("host_slab_role") == "overhead_slab" else _slab_role(group)
    return group["category"]


def write_model(payload: dict, destination: Path | str) -> dict:
    """Write an approved payload, reopen it and validate the actual saved SKP.

    This low-level operation does not approve decisions. The orchestration layer
    must compare its review/approval digest first. The payload is never changed.
    Existing deliveries are never overwritten, including on validation failure.
    """
    from .skp_native import Native, Ref, Box, P3, Color, utf
    if payload.get("schema") != "dwg-skp-model-v2" or payload.get("units") != "mm":
        raise ValueError("Expected a dwg-skp-model-v2 payload in mm")
    if payload.get("blockers") or not payload.get("groups"):
        raise ValueError("Resolve all preparation blockers before writing SKP")
    from .fixed_standard_validation import validate_fixed_payload
    standard_errors = validate_fixed_payload(payload)
    if standard_errors:
        raise ValueError('Fixed modeling standard validation failed: ' + json.dumps(standard_errors, ensure_ascii=False))
    if any(group.get("mode") != "solid_extrusion" and not(group.get("mode") == "boundary_mesh" and group.get("category") == "slab") for group in payload["groups"]):
        raise ValueError("Unsupported approved geometry mode")
    presentation = payload.get("presentation") or {}
    if not isinstance(presentation, dict):
        raise ValueError("presentation must be an object")
    overhead_visible = presentation.get("overhead_visible", False)
    if not isinstance(overhead_visible, bool):
        raise ValueError("presentation.overhead_visible must be a boolean")
    slab_errors = validate_slab_payload(payload)
    if slab_errors:
        raise ValueError("Complete slab audit and zone dimensions before writing SKP: " + json.dumps(slab_errors, ensure_ascii=False))
    cut_errors = validate_building_cut_payload(payload)
    if cut_errors:
        raise ValueError("Complete source-bound RC subtraction audit before writing SKP: " + json.dumps(cut_errors, ensure_ascii=False))
    destination = Path(destination).resolve()
    if destination.suffix.lower() != ".skp":
        raise ValueError("Destination must have .skp extension")
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite existing model: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = _json_hash(payload)
    fd, temporary_name = tempfile.mkstemp(prefix=".dwg-skp-", suffix=".skp", dir=destination.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    temporary.unlink()
    qa = {"schema": "dwg-skp-qa-v2", "payload_sha256": digest, "all_pass": False, "checks": [], "groups": [], "source_tag_mapping": payload["tag_mapping"]}
    qa_path = destination.with_suffix(".skp.qa.json")
    qa["diagnostics_path"] = str(qa_path)
    def check(name, passed, detail=None):
        qa["checks"].append({"check": name, "pass": bool(passed), "detail": detail})
    try:
        with Native() as api:
            qa["native_library"] = str(api.library_path)
            from .native_revision import NativeRevision
            revision = NativeRevision(api,payload)
            model = revision.create_model()
            try:
                entities = api.get("SUModelGetEntities", model)
                api.call("SUModelSetName", model, utf(destination.stem))
                api.call("SUModelSetDescription", model, utf(payload["coordinate_policy"]))
                root_metadata = {field: payload.get(field) for field in ("schema", "source_manifest", "source_tag_policy", "tag_mapping", "coordinate_policy", "floor_datum_m", "slab_audit", "slab_join_policy", "base_component_cut_audit", "wall_under_roof_audit", "presentation", "whole_slab_audit", "source_material_partition_audit", "final_source_accounting", "annotation_binding_audit", "modeling_standard", "fixed_standard_audit")}
                root_metadata['source_tag_policy']=payload.get('source_tag_policy','effective')
                root_metadata["payload_sha256"] = digest
                root_group, entities = revision.root_entities(model, root_metadata)
                if overhead_visible:
                    api.call('SUDrawingElementSetHidden',api.api.SUGroupToDrawingElement(root_group),False)
                manager = api.get("SUModelGetOptionsManager", model)
                units = Ref()
                api.call("SUOptionsManagerGetOptionsProviderByName", manager, b"UnitsOptions", C.byref(units))
                for key, value in [("LengthUnit", 2), ("LengthFormat", 0), ("LengthPrecision", 2)]:
                    api.set_value("SUOptionsProviderSetValue", units, key, value)
                tags = {item["tag"]: api.layer(model, item["tag"]) for item in payload["tag_mapping"]}
                category_entities = {}
                material = api.material(model, "DWG white model", (242, 242, 238))
                construction_transforms={}
                qa['native_geometry_conditioning']=[]
                for group in payload["groups"]:
                    category = _component_container(group)
                    if category not in category_entities:
                        category_group, category_entities[category] = revision.category_entities(entities, category)
                        if category == "overhead_slab":
                            api.call("SUDrawingElementSetHidden", api.api.SUGroupToDrawingElement(category_group), not overhead_visible)
                    group_ref, group_entities = revision.leaf(category_entities[category], group["name"], category, tags[group["tag"]], material, {"id": group["id"], "provenance": group, "payload_sha256": digest})
                    if category=='overhead_slab' and overhead_visible:
                        api.call('SUDrawingElementSetHidden',api.api.SUGroupToDrawingElement(group_ref),False)
                        api.call('SULayerSetVisibility',tags[group['tag']],True)
                    conditioning=api.solid_geometry(group_ref,group_entities,
                        [(face["outer"], face.get("holes", [])) for face in group["faces"]]
                        if group.get("mode") == "boundary_mesh" else _extrusion(group),
                        minimum_clearance_mm=_audit_footprint(group).minimum_clearance)
                    construction_transforms[group['id']]=conditioning['transform']
                    if conditioning['scale']!=1:
                        qa['native_geometry_conditioning'].append({'id':group['id'],**conditioning})
                for reference in payload.get("references", []):
                    if "reference" not in category_entities:
                        reference_group, category_entities["reference"] = revision.category_entities(entities, "reference")
                        api.call("SUDrawingElementSetHidden", api.api.SUGroupToDrawingElement(reference_group), True)
                    _, reference_entities = revision.leaf(category_entities["reference"], reference["name"], "reference", tags[reference["tag"]], data={"id": reference["id"], "provenance": reference, "payload_sha256": digest})
                    z = reference.get("base_z_mm", 0)
                    api.geometry(reference_entities, edges=[([*segment["start"], z], [*segment["end"], z]) for segment in reference.get("segments", [])])
                revision.finish(model)
                xs = [point[0] for group in payload["groups"] for point in group["outer"]]
                ys = [point[1] for group in payload["groups"] for point in group["outer"]]
                zmax = max(group["base_z_mm"] + group["height_mm"] for group in payload["groups"])
                zmin = min(group["base_z_mm"] for group in payload["groups"])
                center = [(min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, (zmin + zmax) / 2]
                span = max(max(xs) - min(xs), max(ys) - min(ys), zmax - zmin, 1)
                camera = api.camera([center[0] + span * 1.2, center[1] - span * 1.5, center[2] + span], center, height=span * 1.7)
                api.call("SUModelSetCamera", model, C.byref(camera))
                scene_specs = [("Model overview", [center[0] + span * 1.2, center[1] - span * 1.5, center[2] + span], center, (0, 0, 1)), ("Source plan", [center[0], center[1], zmax + span], [center[0], center[1], zmin], (0, 1, 0))]
                existing_scene_names = [api.string("SUSceneGetName",scene)for scene in api.refs("SUModelGetNumScenes","SUModelGetScenes",model)]
                for name, eye, target, up in scene_specs:
                    if name in existing_scene_names:
                        continue
                    scene = api.create("SUSceneCreate")
                    api.call("SUSceneSetName", scene, utf(name))
                    api.call("SUModelAddScenes", model, 1, C.byref(scene))
                    scene_camera = api.camera(eye, target, up, height=span * 1.7)
                    api.call("SUSceneSetCamera", scene, scene_camera)
                    api.call("SUCameraRelease", C.byref(scene_camera))
                    api.call("SUSceneSetUseCamera", scene, True)
                expected_scene_names = [api.string("SUSceneGetName",scene)for scene in api.refs("SUModelGetNumScenes","SUModelGetScenes",model)]
                api.call("SUModelSaveToFile", model, utf(temporary))
            finally:
                api.call("SUModelRelease", C.byref(model))
            model = api.create("SUModelCreateFromFile", utf(temporary))
            try:
                entities = api.get("SUModelGetEntities", model)
                layers = api.refs("SUModelGetNumLayers", "SUModelGetLayers", model)
                actual_tags = {api.string("SULayerGetName", layer) for layer in layers}
                default_tag = api.string("SULayerGetName", layers[0])
                check("source tags preserved", {item["tag"] for item in payload["tag_mapping"]}.issubset(actual_tags))
                scenes = [api.string("SUSceneGetName", scene) for scene in api.refs("SUModelGetNumScenes", "SUModelGetScenes", model)]
                check("extent derived overview and plan scenes preserved", scenes == expected_scene_names)
                preservation=revision.verify(model)
                if preservation is not None:
                    qa['native_base_preservation']=preservation
                    for field in ('source_file_unchanged','scene_camera_flags_and_layers_preserved',
                                  'scene_component_visibility_preserved_by_lineage','all_original_tags_preserved'):
                        check('native base '+field,preservation[field])
                roots = api.refs("SUEntitiesGetNumGroups", "SUEntitiesGetGroups", entities)
                check("single model root", len(roots) == 1)
                check("model approval digest preserved", api.read_metadata(api.api.SUGroupToEntity(roots[0]), "payload_sha256") == digest)
                if payload.get("base_component_cut_audit") is not None:
                    check("source-bound RC subtraction audit preserved in saved model", json.loads(api.read_metadata(
                        api.api.SUGroupToEntity(roots[0]), "base_component_cut_audit")) == payload["base_component_cut_audit"])
                for field in ("wall_under_roof_audit", "presentation", "whole_slab_audit", "source_material_partition_audit", "final_source_accounting", "annotation_binding_audit", "modeling_standard", "fixed_standard_audit"):
                    if field in payload:
                        check("saved model " + field + " metadata preserved", json.loads(api.read_metadata(
                            api.api.SUGroupToEntity(roots[0]), field)) == payload[field])
                entities = api.get("SUGroupGetEntities", roots[0])
                expected = {group["id"]: group for group in payload["groups"]}
                reference_ids = {group["id"] for group in payload.get("references", [])}
                seen = []
                seen_references = []
                saved_solids = []
                saved_mesh = []
                saved_slab_rows = []
                saved_material_rows = []
                whole_reports = []
                _, Polygon, _, unary_union, _ = _geometry()
                for category_group in api.refs("SUEntitiesGetNumGroups", "SUEntitiesGetGroups", entities):
                    children = api.get("SUGroupGetEntities", category_group)
                    category = api.string("SUGroupGetName", category_group)
                    if category == "overhead_slab":
                        hidden = C.c_bool()
                        api.call("SUDrawingElementGetHidden", api.api.SUGroupToDrawingElement(category_group), C.byref(hidden))
                        check("overhead slab parent visibility matches presentation" if overhead_visible else
                              "overhead slab parent hidden in default overview", hidden.value == (not overhead_visible))
                    for saved in api.refs("SUEntitiesGetNumGroups", "SUEntitiesGetGroups", children):
                        entity_ref = api.api.SUGroupToEntity(saved)
                        key = api.read_metadata(entity_ref, "id")
                        if category == "reference":
                            seen_references.append(key)
                            reference = next(item for item in payload["references"] if item["id"] == key)
                            check("reference provenance preserved", json.loads(api.read_metadata(entity_ref, "provenance")) == reference, key)
                            reference_tag = api.string("SULayerGetName", api.get("SUDrawingElementGetLayer", api.api.SUGroupToDrawingElement(saved)))
                            check("reference source tag preserved", reference_tag == reference["tag"], key)
                            hidden = C.c_bool()
                            api.call("SUDrawingElementGetHidden", api.api.SUGroupToDrawingElement(category_group), C.byref(hidden))
                            check("reference parent hidden", hidden.value, key)
                            continue
                        seen.append(key)
                        group = expected.get(key)
                        if group is None:
                            check("unexpected saved component", False, key)
                            continue
                        provenance = json.loads(api.read_metadata(entity_ref, "provenance"))
                        check("component provenance preserved", provenance == group, key)
                        check("component approval digest preserved", api.read_metadata(entity_ref, "payload_sha256") == digest, key)
                        drawing = api.api.SUGroupToDrawingElement(saved)
                        actual_material = Ref()
                        material_result = api.api.SUDrawingElementGetMaterial(drawing, C.byref(actual_material))
                        material_ok = material_result == 0 and bool(actual_material.ptr)
                        material_data = {"id": key, "assigned": material_ok}
                        if material_ok:
                            color = Color()
                            alpha = C.c_double()
                            api.call("SUMaterialGetColor", actual_material, C.byref(color))
                            api.call("SUMaterialGetOpacity", actual_material, C.byref(alpha))
                            name = api.string("SUMaterialGetName", actual_material)
                            material_data.update(name=name, rgba=[color.r,color.g,color.b,color.a], opacity=alpha.value)
                            material_ok = name == "DWG white model" and [color.r,color.g,color.b] == [242,242,238] and abs(alpha.value-1.) < 1e-8
                        check("actual white material assignment color and opacity", material_ok, material_data)
                        tag = api.string("SULayerGetName", api.get("SUDrawingElementGetLayer", drawing))
                        check("component tag and category", tag == group["tag"] and category == _component_container(group), key)
                        ge = api.get("SUGroupGetEntities", saved)
                        saved_transform=api.group_transform(saved)
                        check('native construction transform preserved',list(saved_transform.values)==construction_transforms[key],key)
                        check("physical material leaf has no child groups", api.count("SUEntitiesGetNumGroups", ge) == 0, key)
                        faces = api.refs("SUEntitiesGetNumFaces", "SUEntitiesGetFaces", ge)
                        face_materials_ok=True
                        for face in faces:
                            for method in ('SUFaceGetFrontMaterial','SUFaceGetBackMaterial'):
                                face_material=Ref()
                                status=getattr(api.api,method)(face,C.byref(face_material))
                                if status==9:
                                    continue
                                if status!=0 or not face_material.ptr:
                                    face_materials_ok=False;continue
                                color=Color();alpha=C.c_double()
                                api.call('SUMaterialGetColor',face_material,C.byref(color))
                                api.call('SUMaterialGetOpacity',face_material,C.byref(alpha))
                                if [color.r,color.g,color.b]!=[242,242,238] or abs(alpha.value-1.)>1e-8:
                                    face_materials_ok=False
                        check('actual face front and back materials cannot override white model appearance',face_materials_ok,key)
                        if category=='overhead_slab' and overhead_visible:
                            hidden=C.c_bool();tag_visible=C.c_bool();root_hidden=C.c_bool()
                            api.call('SUDrawingElementGetHidden',drawing,C.byref(hidden))
                            api.call('SULayerGetVisibility',api.get('SUDrawingElementGetLayer',drawing),C.byref(tag_visible))
                            api.call('SUDrawingElementGetHidden',api.api.SUGroupToDrawingElement(roots[0]),C.byref(root_hidden))
                            check('delivered overhead leaf Tag and root are actually visible',not hidden.value and tag_visible.value and not root_hidden.value,key)
                        edges = api.edges(ge)
                        incidence = Counter(api.count("SUEdgeGetNumFaces", edge) for edge in edges)
                        check("closed solid topology", bool(faces) and set(incidence) == {2}, key)
                        raw_tags = set()
                        for converter, items in [("SUFaceToDrawingElement", faces), ("SUEdgeToDrawingElement", edges)]:
                            for item in items:
                                raw_tags.add(api.string("SULayerGetName", api.get("SUDrawingElementGetLayer", getattr(api.api, converter)(item))))
                        check("raw geometry Untagged", raw_tags == {default_tag}, key)
                        box = Box()
                        api.call("SUDrawingElementGetBoundingBox", drawing, C.byref(box))
                        bounds = [[coordinate * 25.4 for coordinate in [point.x, point.y, point.z]] for point in [box.min_point, box.max_point]]
                        target_bounds = [[min(point[0] for point in group["outer"]), min(point[1] for point in group["outer"]), group["base_z_mm"]], [max(point[0] for point in group["outer"]), max(point[1] for point in group["outer"]), group["base_z_mm"] + group["height_mm"]]]
                        check("source coordinates and heights", max(abs(bounds[i][j] - target_bounds[i][j]) for i in range(2) for j in range(3)) < .01, key)
                        triangles = [triangle for face in faces for triangle in api.triangle_mesh(face,saved_transform)]
                        volume = _volume(triangles)
                        expected_volume = (group["volume_mm3"] if group.get("mode") == "boundary_mesh" else
                            (abs(_signed_area(group["outer"])) - sum(abs(_signed_area(hole)) for hole in group.get("holes", [])))) * (1 if group.get("mode") == "boundary_mesh" else group["height_mm"])
                        error = abs(volume - expected_volume) / max(expected_volume, 1)
                        check("signed solid volume", error < .001, {"id": key, "relative_error": error})
                        bottom_triangles = [Polygon([[point[0], point[1]] for point in triangle]) for triangle in triangles if all(abs(point[2] - bounds[0][2]) < .01 for point in triangle)]
                        actual_footprint = unary_union(bottom_triangles)
                        whole_report = None
                        if group.get("mode") == "boundary_mesh":
                            from .whole_slab_qa import verify_saved_whole
                            actual_faces = []
                            for face in faces:
                                loops = [api.get("SUFaceGetOuterLoop", face),
                                         *api.refs("SUFaceGetNumInnerLoops", "SUFaceGetInnerLoops", face)]
                                points = []
                                for loop in loops:
                                    ring = []
                                    for vertex in api.refs("SULoopGetNumVertices", "SULoopGetVertices", loop):
                                        point = P3()
                                        api.call("SUVertexGetPosition", vertex, C.byref(point))
                                        ring.append(api.transformed_point([point.x*25.4, point.y*25.4, point.z*25.4],saved_transform))
                                    points.append(ring)
                                actual_faces.append({"outer": points[0], "holes": points[1:]})
                            whole_report = verify_saved_whole(group, triangles, actual_faces)
                            qa["checks"].extend(whole_report["checks"])
                            actual_footprint = unary_union([_optional_audit_footprint(row["footprint"]) for row in whole_report["actual_sections"]])
                            whole_reports.append({"component_id": key, **{name:value for name,value in whole_report.items()
                                                                         if name not in {"actual_cells", "virtual_source_rows"}}})
                        approved_footprint = Polygon(group["outer"], group.get("holes", []))
                        check("saved footprint and holes match approval", actual_footprint.symmetric_difference(approved_footprint).area / max(approved_footprint.area, 1) < .00001, key)
                        actual_group = {**group, "base_z_mm": bounds[0][2], "height_mm": bounds[1][2] - bounds[0][2]}
                        if whole_report:
                            actual_group["construction_cells"] = whole_report["actual_cells"]
                        saved_solids.append((actual_group, actual_footprint))
                        if whole_report:
                            saved_material_rows.extend(whole_report["virtual_source_rows"])
                        else:
                            saved_material_rows.append({"id": key, "category": group["category"], "tag": tag, "footprint": actual_footprint,
                                                        "base_z_mm": bounds[0][2], "top_z_mm": bounds[1][2], "volume_mm3": volume})
                        saved_mesh.append({"id": key, "category": group["category"], "triangles": triangles})
                        if group["category"] == "slab" and not whole_report:
                            saved_slab_rows.append({"id": key, "category": "slab", "tag": tag, "footprint": actual_footprint,
                                                    "base_z_mm": bounds[0][2], "top_z_mm": bounds[1][2], "volume_mm3": volume})
                        qa["groups"].append({"id": key, "tag": tag, "bounds_mm": bounds, "volume_mm3": volume, "expected_volume_mm3": expected_volume, "edge_face_incidence": dict(incidence)})
                check("all components preserved", Counter(seen) == Counter(expected.keys()))
                check("all references preserved", Counter(seen_references) == Counter(reference_ids))
                for opening in payload.get("opening_audit", []):
                    footprint = Polygon(opening["clearance_footprint"]["outer"], opening["clearance_footprint"].get("holes", []))
                    conflicts = []
                    bottom, top = opening["base_z_mm"], opening["base_z_mm"] + opening["height_mm"]
                    for group, actual_footprint in saved_solids:
                        if group.get('mode')=='boundary_mesh':
                            cells=group['construction_cells']  # actual reopened sections, not planned CSG
                        else:
                            cells=[{**group,'outer':list(actual_footprint.exterior.coords)[:-1],
                                    'holes':[list(ring.coords)[:-1]for ring in actual_footprint.interiors]}]
                        for cell in cells:
                            overlap_z = min(top, cell["base_z_mm"] + cell["height_mm"]) - max(bottom, cell["base_z_mm"])
                            cell_footprint=Polygon(cell['outer'],cell.get('holes',[]))
                            if overlap_z > .01 and footprint.intersection(cell_footprint).area > .1:
                                conflicts.append(group["id"])
                                break
                    check("saved opening remains clear", not conflicts, {"opening_id": opening["id"], "conflicting_solids": conflicts})
                actual_overlaps = _solid_overlaps([group for group, _ in saved_solids], [footprint for _, footprint in saved_solids])
                qa["overlap_audit"] = actual_overlaps
                joins = evaluate_slab_joins([group for group, _ in saved_solids], [footprint for _, footprint in saved_solids], payload.get("slab_join_policy"))
                qa["slab_join_audit"] = joins
                check("saved solids have no unresolved volume overlaps", not joins["unresolved_overlaps"] and not joins["invalid_policy"], joins)
                if saved_slab_rows:
                    from .whole_slabs import prism_only_payload
                    qa["slab_audit"] = _saved_slab_audit(prism_only_payload(payload), saved_slab_rows)
                    qa["checks"].extend(qa["slab_audit"]["checks"])
                if whole_reports:
                    qa["whole_slab_audit"] = {"systems": whole_reports, "all_pass": all(row["all_pass"] for row in whole_reports)}
                if (payload.get("base_component_cut_audit") or {}).get("enabled"):
                    from .whole_slabs import expanded_payload
                    qa["base_component_cut_audit"] = _saved_building_cut_audit(expanded_payload(payload), saved_material_rows)
                    qa["checks"].extend(qa["base_component_cut_audit"]["checks"])
                if (payload.get("wall_under_roof_audit") or {}).get("schema") == "dwg-wall-under-roof-v1" and payload["wall_under_roof_audit"].get("enabled"):
                    from .whole_slabs import expanded_payload
                    qa["wall_under_roof_audit"] = _saved_wall_roof_fit_audit(expanded_payload(payload), saved_material_rows)
                    qa["checks"].extend(qa["wall_under_roof_audit"]["checks"])
                qa["previews"] = _saved_previews(saved_mesh, saved_solids, destination, overhead_visible=overhead_visible)
                check("previews rendered from reopened SKP", bool(qa["previews"]))
            finally:
                api.call("SUModelRelease", C.byref(model))
        check("payload unchanged by writer", _json_hash(payload) == digest)
        qa["all_pass"] = all(item["pass"] for item in qa["checks"])
        if not qa["all_pass"]:
            raise RuntimeError("Reopened SKP failed verification: " + json.dumps([item for item in qa["checks"] if not item["pass"]], ensure_ascii=False))
        # Windows rename is fail-if-exists; use a hard link for the same guarantee
        # on POSIX so a concurrent delivery can never be overwritten.
        if os.name == "nt":
            os.rename(temporary, destination)
        else:
            os.link(temporary, destination)
            temporary.unlink()
        qa.update(saved_file=str(destination), sha256=hashlib.sha256(destination.read_bytes()).hexdigest(), bytes=destination.stat().st_size)
        return qa
    except Exception as exc:
        qa.update(all_pass=False, error={"type": type(exc).__name__, "message": str(exc)})
        raise
    finally:
        if temporary.exists():
            temporary.unlink()
        try:
            qa_path.write_text(json.dumps(qa, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            qa["diagnostics_write_error"] = str(exc)
