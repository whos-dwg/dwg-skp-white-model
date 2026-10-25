"""Subtract confirmed RC material from explicitly selected façade components.

Pure payload preparation: no files, model approval, source mutation, nominal
height changes, wall/column clipping, or overlap exemptions occur here.
"""
from __future__ import annotations

import copy
import math

from shapely.geometry import Polygon
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

AREA_TOLERANCE_MM2 = 1e-6
HEIGHT_TOLERANCE_MM = 1e-6
TARGET_CATEGORIES = {"curtain", "curtain_accessory"}
RC_ROLES = {"floor_slab", "overhead_slab", "slab_step_riser"}


def _number(value, name, positive=False):
    if value is None or isinstance(value, bool):
        raise ValueError(f"{name} must be an explicit finite number")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError(f"{name} must be a finite {'positive ' if positive else ''}number")
    return result


def _ids(value):
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError("Expected a list of nonempty source/component IDs")
    return sorted(set(value))


def _geometry(group):
    if group.get("mode") != "solid_extrusion":
        raise ValueError("Only explicit solid_extrusion material can be cut")
    rings = [group["outer"], *group.get("holes", [])]
    if any(len(point) != 2 or any(not math.isfinite(float(value)) for value in point) for ring in rings for point in ring):
        raise ValueError("Footprint coordinates must be finite XY pairs")
    polygon = Polygon(group["outer"], group.get("holes", []))
    if not polygon.is_valid or polygon.is_empty or polygon.area <= AREA_TOLERANCE_MM2:
        raise ValueError("Invalid or empty original material footprint")
    if any(not math.isfinite(value) for point in polygon.exterior.coords for value in point):
        raise ValueError("Footprint coordinates must be finite")
    base = _number(group.get("base_z_mm"), "base_z_mm")
    height = _number(group.get("height_mm"), "height_mm", True)
    top = base + height
    if group.get("top_z_mm") is not None and abs(_number(group["top_z_mm"], "top_z_mm") - top) > HEIGHT_TOLERANCE_MM:
        raise ValueError("top_z_mm conflicts with base_z_mm + height_mm")
    return polygon, base, top


def _parts(geometry):
    if geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        return [geometry]
    return [part for child in getattr(geometry, "geoms", []) for part in _parts(child)]


def _shape(polygon):
    polygon = orient(polygon.normalize(), sign=1)
    return {"outer": [list(point) for point in list(polygon.exterior.coords)[:-1]], "holes": [[list(point) for point in list(ring.coords)[:-1]] for ring in polygon.interiors]}


def _encoded(geometry):
    shapes = [_shape(part) for part in sorted(_parts(geometry), key=lambda polygon: polygon.normalize().wkb_hex)]
    return shapes[0] if len(shapes) == 1 else {"parts": shapes}


def resolve_building_slab_cuts(base_groups: list[dict], slab_groups: list[dict], config: dict) -> dict:
    """Return exact base material minus authorized horizontal RC/riser prisms.

    Minimal enabled config: {enabled:true, operation:'subtract_rc_material_only',
    target_component_ids:[...], target_categories:['curtain','curtain_accessory']}.
    Only a positive-volume intersection changes an original group. Adjacent Z
    slices with identical remaining footprints are merged before editable parts
    are emitted. Any invalid recipe returns the unmodified base plus blockers.
    """
    result = {"groups": copy.deepcopy(base_groups), "audit": {"schema": "dwg-building-slab-cuts-v1", "enabled": False, "operation": None, "target_component_ids": [], "target_categories": [], "original_groups": [], "cutters": [], "cuts": [], "untouched_component_ids": [group.get("id") for group in base_groups], "height_rules": {}, "tolerances": {"area_mm2": AREA_TOLERANCE_MM2, "height_mm": HEIGHT_TOLERANCE_MM}}, "blockers": [], "height_rules": {}}
    audit = result["audit"]
    def block(code, **detail):
        result["blockers"].append({"code": code, **detail})
    if not isinstance(config, dict) or not isinstance(config.get("enabled"), bool):
        block("building_slab_cut_config_invalid", detail="An explicit boolean enabled is required")
        audit["all_pass"] = False
        return result
    if config["enabled"] is False:
        audit["all_pass"] = True
        return result
    audit["enabled"] = True
    audit["operation"] = config.get("operation")
    if config.get("operation") != "subtract_rc_material_only":
        block("building_slab_cut_operation_invalid")
    try:
        targets = _ids(config.get("target_component_ids"))
        categories = _ids(config.get("target_categories"))
        if not targets:
            raise ValueError("Enabled cuts require explicit target_component_ids")
        if not categories or not set(categories).issubset(TARGET_CATEGORIES):
            raise ValueError("Only curtain and curtain_accessory categories may be targeted")
    except ValueError as exc:
        block("building_slab_cut_scope_invalid", detail=str(exc))
        audit["all_pass"] = False
        return result
    audit.update(target_component_ids=targets, target_categories=categories)
    base_index = {}
    for group in base_groups:
        identity = group.get("id")
        if not isinstance(identity, str) or not identity or identity in base_index:
            block("building_slab_cut_base_identity_invalid", component_id=identity)
        base_index[identity] = group
    selected = {}
    for identity in targets:
        group = base_index.get(identity)
        if group is None:
            block("building_slab_cut_target_missing", component_id=identity)
            continue
        if group.get("category") not in categories or group.get("category") not in TARGET_CATEGORIES:
            block("building_slab_cut_target_forbidden", component_id=identity, category=group.get("category"))
            continue
        try:
            selected[identity] = _geometry(group)
            if not _ids(group.get("source_entity_ids")):
                raise ValueError("Target primary source_entity_ids must remain traceable")
        except (KeyError, TypeError, ValueError) as exc:
            block("building_slab_cut_target_invalid", component_id=identity, detail=str(exc))
    rc = {}
    for group in slab_groups:
        identity = group.get("id")
        if not isinstance(identity, str) or not identity or identity in rc or identity in base_index:
            block("building_slab_cut_rc_identity_invalid", component_id=identity)
            continue
        role = group.get("slab_role") or group.get("semantic_role")
        if group.get("category") != "slab" or role not in RC_ROLES:
            block("building_slab_cut_cutter_forbidden", component_id=identity, category=group.get("category"), role=role)
            continue
        try:
            shape, base, top = _geometry(group)
            sources = _ids(group.get("source_entity_ids"))
            if not sources:
                raise ValueError("RC cutter source_entity_ids must be explicit")
            rc[identity] = {"group": group, "shape": shape, "base": base, "top": top, "sources": sources}
        except (KeyError, TypeError, ValueError) as exc:
            block("building_slab_cut_cutter_invalid", component_id=identity, detail=str(exc))
    if not rc:
        block("building_slab_cut_rc_material_required")
    audit["original_groups"] = [copy.deepcopy(base_index[identity]) for identity in targets if identity in base_index]
    if result["blockers"]:
        audit["all_pass"] = False
        return result
    replacement = {}
    used_cutters = set()
    new_ids = set(base_index)
    for identity in targets:
        original = base_index[identity]
        shape, nominal_base, nominal_top = selected[identity]
        interacting = {key: rc[key] for key in sorted(rc) if min(nominal_top, rc[key]["top"]) - max(nominal_base, rc[key]["base"]) > HEIGHT_TOLERANCE_MM and shape.intersection(rc[key]["shape"]).area > AREA_TOLERANCE_MM2}
        volume = shape.area * (nominal_top - nominal_base)
        record = {"original_component_id": identity, "result_component_ids": [identity], "rc_cutter_component_ids": sorted(interacting), "z_slices": [], "original_volume_mm3": volume, "remaining_volume_mm3": volume, "removed_volume_mm3": 0.0, "nominal_base_z_mm": nominal_base, "nominal_top_z_mm": nominal_top, "status": "untouched_no_positive_intersection"}
        audit["cuts"].append(record)
        if not interacting:
            record["z_slices"] = [{"base_z_mm": nominal_base, "top_z_mm": nominal_top, "active_cutter_ids": [], "original_area_mm2": shape.area, "remaining_footprint": _encoded(shape), "remaining_area_mm2": shape.area, "removed_area_mm2": 0.0, "remaining_volume_mm3": volume, "removed_volume_mm3": 0.0}]
            record["expected_remaining_volume_mm3"] = volume
            continue
        used_cutters.update(interacting)
        events = sorted({nominal_base, nominal_top} | {max(nominal_base, cutter["base"]) for cutter in interacting.values()} | {min(nominal_top, cutter["top"]) for cutter in interacting.values()})
        slices = []
        for bottom, top in zip(events, events[1:]):
            if top - bottom <= HEIGHT_TOLERANCE_MM:
                continue
            midpoint = (bottom + top) / 2
            active = {key: cutter for key, cutter in interacting.items() if cutter["base"] < midpoint < cutter["top"]}
            cutters_union = unary_union([cutter["shape"] for cutter in active.values()])
            remaining = shape.difference(cutters_union)
            removed_area = shape.area - remaining.area
            record["z_slices"].append({"base_z_mm": bottom, "top_z_mm": top, "active_cutter_ids": sorted(active), "original_area_mm2": shape.area, "remaining_footprint": _encoded(remaining), "remaining_area_mm2": remaining.area, "removed_area_mm2": removed_area, "remaining_volume_mm3": remaining.area * (top - bottom), "removed_volume_mm3": removed_area * (top - bottom)})
            slices.append({"base": bottom, "top": top, "shape": remaining, "cutters": set(active)})
        # Whole-slice equality is exact material equality, independent of polygon
        # ring order. Do not merge across an empty slice or change either face.
        merged = []
        for interval in slices:
            if merged and abs(merged[-1]["top"] - interval["base"]) <= HEIGHT_TOLERANCE_MM and merged[-1]["shape"].equals(interval["shape"]):
                merged[-1]["top"] = interval["top"]
                merged[-1]["cutters"].update(interval["cutters"])
            else:
                merged.append({**interval, "cutters": set(interval["cutters"])})
        pieces = []
        for interval in merged:
            for polygon in sorted(_parts(interval["shape"]), key=lambda part: part.normalize().wkb_hex):
                if polygon.area <= AREA_TOLERANCE_MM2:
                    continue
                pieces.append({**interval, "shape": polygon})
        parts = []
        for number, interval in enumerate(pieces, 1):
            new_id = f"{identity}-rc-cut-{number:03}"
            if new_id in new_ids:
                block("building_slab_cut_generated_id_collision", component_id=new_id)
                continue
            new_ids.add(new_id)
            group = copy.deepcopy(original)
            modifier_ids = sorted(interacting)
            modifier_sources = sorted({source for key in modifier_ids for source in interacting[key]["sources"]})
            modifier_trace = [{"entity_id": source, "role": "rc_material_subtraction", "rc_cutter_component_ids": sorted(key for key in modifier_ids if source in interacting[key]["sources"])} for source in modifier_sources]
            provenance = {"schema": "dwg-rc-material-subtraction-v1", "operation": "subtract_rc_material_only", "original_component_id": identity, "original_source_entity_ids": copy.deepcopy(original["source_entity_ids"]), "rc_cutter_component_ids": modifier_ids, "slice_active_cutter_ids": sorted(interval["cutters"]), "modifier_source_entity_ids": modifier_sources, "typed_sources": modifier_trace, "nominal_base_z_mm": nominal_base, "nominal_top_z_mm": nominal_top, "effective_base_z_mm": interval["base"], "effective_top_z_mm": interval["top"]}
            if original.get("geometry_modifier_provenance") is not None:
                provenance["prior_geometry_modifier_provenance"] = copy.deepcopy(original["geometry_modifier_provenance"])
            group.update(id=new_id, name=f"{original.get('name', identity)}-rc-cut-{number:03}", original_component_id=identity, nominal_base_z_mm=nominal_base, nominal_top_z_mm=nominal_top, nominal_height_mm=nominal_top - nominal_base, base_z_mm=interval["base"], top_z_mm=interval["top"], height_mm=interval["top"] - interval["base"], area_mm2=interval["shape"].area, volume_mm3=interval["shape"].area * (interval["top"] - interval["base"]), modifier_source_entity_ids=modifier_sources, geometry_modifier_provenance=provenance, **_shape(interval["shape"]))
            parts.append(group)
            result["height_rules"][new_id] = {"base_z_mm": group["base_z_mm"], "height_mm": group["height_mm"]}
        replacement[identity] = parts
        record.update(result_component_ids=[group["id"] for group in parts], remaining_volume_mm3=sum(group["volume_mm3"] for group in parts), removed_volume_mm3=volume - sum(group["volume_mm3"] for group in parts), merged_intervals=[{"base_z_mm": interval["base"], "top_z_mm": interval["top"], "remaining_footprint": _encoded(interval["shape"])} for interval in merged], modifier_source_entity_ids=sorted({source for cutter in interacting.values() for source in cutter["sources"]}), status="cut_into_editable_parts" if parts else "fully_removed_by_confirmed_rc_material")
        expected_remaining = sum(interval["shape"].area * (interval["top"] - interval["base"]) for interval in slices)
        tolerance = max(AREA_TOLERANCE_MM2 * (nominal_top - nominal_base), volume * 1e-12)
        if abs(record["remaining_volume_mm3"] - expected_remaining) > tolerance:
            block("building_slab_cut_volume_mismatch", component_id=identity, expected_remaining_volume_mm3=expected_remaining, emitted_remaining_volume_mm3=record["remaining_volume_mm3"])
        record["expected_remaining_volume_mm3"] = expected_remaining
    audit["cutters"] = [copy.deepcopy(rc[identity]["group"]) for identity in sorted(used_cutters)]
    if result["blockers"]:
        # A malformed scope or generated-ID collision must never partly modify
        # the building while reporting a failed recipe.
        result["groups"] = copy.deepcopy(base_groups)
        result["height_rules"] = {}
    else:
        result["groups"] = [part for group in base_groups for part in replacement.get(group["id"], [copy.deepcopy(group)])]
    audit["untouched_component_ids"] = [group["id"] for group in base_groups if group["id"] not in replacement]
    audit["height_rules"] = copy.deepcopy(result["height_rules"])
    audit["all_pass"] = not result["blockers"]
    audit["removed_volume_mm3"] = sum(record["removed_volume_mm3"] for record in audit["cuts"])
    audit["result_component_count"] = len(result["groups"])
    return result
