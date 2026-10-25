"""Fit explicitly selected vertical source prisms to actual overhead undersides.

Pure, opt-in payload preparation. Input groups are never mutated. XY footprints
are partitioned by the lowest covering overhead horizontal slab or overhead
step-riser bottom. Uncovered XY retains its nominal top; extensions are explicit.
"""
from __future__ import annotations

import copy
import math

from shapely.geometry import GeometryCollection, Polygon
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

AREA_TOLERANCE_MM2 = 1e-6
HEIGHT_TOLERANCE_MM = 1e-6
TARGET_CATEGORIES = {"architectural_wall", "structural_wall", "lintel", "column", "curtain", "curtain_accessory"}
OPERATION = "fit_to_lowest_overhead_underside"


def _number(value, name, *, positive=False):
    if value is None or isinstance(value, bool):
        raise ValueError(f"{name} must be an explicit finite number")
    value = float(value)
    if not math.isfinite(value) or positive and value <= 0:
        raise ValueError(f"{name} must be a finite {'positive ' if positive else ''}number")
    return value


def _ids(value):
    if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
        raise ValueError("Expected a list of nonempty IDs/categories")
    return sorted(set(value))


def _geometry(group):
    if group.get("mode") != "solid_extrusion":
        raise ValueError("Only source-bound solid_extrusion material can be fitted")
    for ring in [group["outer"], *group.get("holes", [])]:
        if any(len(p) != 2 or any(isinstance(v, bool) or not math.isfinite(float(v)) for v in p) for p in ring):
            raise ValueError("Material footprint must have finite XY pairs")
    polygon = Polygon(group["outer"], group.get("holes", []))
    if not polygon.is_valid or polygon.is_empty or polygon.area <= AREA_TOLERANCE_MM2:
        raise ValueError("Material footprint must be valid and nonempty")
    base = _number(group.get("base_z_mm"), "base_z_mm")
    top = base + _number(group.get("height_mm"), "height_mm", positive=True)
    if group.get("top_z_mm") is not None and abs(_number(group["top_z_mm"], "top_z_mm")-top) > HEIGHT_TOLERANCE_MM:
        raise ValueError("top_z_mm conflicts with base_z_mm + height_mm")
    if not _ids(group.get("source_entity_ids")):
        raise ValueError("Primary source_entity_ids must be explicit")
    return polygon, base, top


def _parts(g):
    if g.is_empty:
        return []
    if g.geom_type == "Polygon":
        return [g]
    return [p for child in getattr(g, "geoms", []) for p in _parts(child)]


def _shape(p):
    p = orient(p.normalize(), sign=1)
    return {"outer": [list(v) for v in list(p.exterior.coords)[:-1]],
            "holes": [[list(v) for v in list(r.coords)[:-1]] for r in p.interiors]}


def _encoded(g):
    shapes = [_shape(p) for p in sorted(_parts(g), key=lambda p:p.normalize().wkb_hex)]
    return shapes[0] if len(shapes) == 1 else {"parts": shapes}


def _projection_shape(value):
    if not isinstance(value,dict):
        raise ValueError("Projection footprint must be an explicit XY polygon or parts")
    if "parts" in value:
        if not isinstance(value["parts"],list) or not value["parts"]:
            raise ValueError("Projection parts must be nonempty")
        shapes = [_projection_shape(p) for p in value["parts"]]
        combined = unary_union(shapes)
        if abs(sum(p.area for p in shapes)-combined.area) > AREA_TOLERANCE_MM2:
            raise ValueError("Projection footprint parts overlap")
        return combined
    polygon,_,_ = _geometry({"mode":"solid_extrusion", "base_z_mm":0,"height_mm":1,
                            "source_entity_ids":["validation-only"],**value})
    return polygon


def resolve_walls_under_roof(base_groups: list[dict], rc_groups: list[dict], config: dict) -> dict:
    """Return {groups, audit, blockers, height_rules} without mutating inputs.

    Enabled recipe::

        {"enabled": True, "operation": "fit_to_lowest_overhead_underside",
         "target_categories": ["architectural_wall", "structural_wall"],
         "target_component_ids": ["optional explicit subset of selected categories"]}

    Optional source-bound height-only projection outside actual RC material::

        {"uncovered_policy":"height_projection_regions",
         "require_projected_coverage":True,
         "height_projection_regions":[{"id":"P1", "footprint":{...},
          "cap_z_mm":3500,"roof_component_ids":["horizontal-roof-id"],
          "source_entity_ids":["actual-source-id-of-that-roof"]}]}

    Projection only constrains otherwise-uncovered XY. It never extends RC
    geometry. References must be horizontal overhead slabs at that exact cap;
    risers cannot be projected. Different caps may not claim the same uncovered
    XY. Those boundaries must be explicitly partitioned by the reviewed caller.

    Every category must be explicitly selected. Columns, façade and generic
    lintels are legal only when listed; semantic_role never broadens the scope.
    A door_lintel belonging to a curtain remains a curtain for this purpose.
    A cap at/below an original base blocks the entire recipe rather than making
    zero/negative material or silently removing a door head. Original snapshots,
    per-cap regions, trim/extension volumes and modifier sources remain in audit.
    """
    audit = {"schema":"dwg-wall-under-roof-v1", "enabled":False, "operation":None,
             "target_categories":[], "target_component_ids":[], "original_groups":[], "roofs":[], "changes":[],
             "untouched_component_ids":[g.get("id") for g in base_groups], "height_rules":{},
             "cap_policy":"lowest actual bottom among covering overhead slab and overhead-host step riser",
             "uncovered_policy":"retain_original_top", "height_projection_regions":[],"require_projected_coverage":False,
             "extension_policy":"extend_selected_prism_to_actual_underside",
             "tolerances":{"area_mm2":AREA_TOLERANCE_MM2,"height_mm":HEIGHT_TOLERANCE_MM}}
    result = {"groups":copy.deepcopy(base_groups), "audit":audit, "blockers":[], "height_rules":{}}
    def block(code, **detail):
        result["blockers"].append({"code":code, **detail})
    def fail():
        audit["all_pass"] = False
        result["groups"] = copy.deepcopy(base_groups)
        result["height_rules"] = {}
        audit["height_rules"] = {}
        return result
    if not isinstance(config,dict) or not isinstance(config.get("enabled"),bool):
        block("wall_under_roof_config_invalid",detail="Explicit boolean enabled required")
        return fail()
    if not config["enabled"]:
        audit["all_pass"] = True
        return result
    audit.update(enabled=True,operation=config.get("operation"))
    if config.get("operation") != OPERATION:
        block("wall_under_roof_operation_invalid",detail=f"Expected {OPERATION}")
    policy = config.get("uncovered_policy","retain_original_top")
    require_projection = config.get("require_projected_coverage",False)
    if not isinstance(policy,str) or policy not in {"retain_original_top","height_projection_regions"} or not isinstance(require_projection,bool):
        block("wall_under_roof_uncovered_policy_invalid")
    if policy != "height_projection_regions" and (config.get("height_projection_regions") is not None or require_projection):
        block("wall_under_roof_projection_policy_required")
    audit.update(uncovered_policy=policy,require_projected_coverage=require_projection)
    try:
        categories = _ids(config.get("target_categories"))
        if not categories or not set(categories) <= TARGET_CATEGORIES:
            raise ValueError("Explicit target_categories must contain only supported vertical solid categories")
        targets = (_ids(config["target_component_ids"]) if "target_component_ids" in config
                   else sorted(g.get("id") for g in base_groups if g.get("category") in categories and isinstance(g.get("id"),str)))
        if not targets:
            raise ValueError("Enabled fit requires at least one target")
    except (TypeError,ValueError) as error:
        block("wall_under_roof_scope_invalid",detail=str(error))
        return fail()
    audit.update(target_categories=categories,target_component_ids=targets)
    index = {}
    for g in base_groups:
        identity = g.get("id")
        if not isinstance(identity,str) or not identity or identity in index:
            block("wall_under_roof_base_identity_invalid",component_id=identity)
            continue
        index[identity] = g
    selected = {}
    for identity in targets:
        g = index.get(identity)
        if g is None:
            block("wall_under_roof_target_missing",component_id=identity)
        elif g.get("category") not in categories or g.get("category") not in TARGET_CATEGORIES:
            block("wall_under_roof_target_forbidden",component_id=identity,category=g.get("category"))
        else:
            try:
                selected[identity] = _geometry(g)
            except (KeyError,TypeError,ValueError) as error:
                block("wall_under_roof_target_invalid",component_id=identity,detail=str(error))
    roofs, all_rc_ids = {}, set()
    for g in rc_groups:
        identity = g.get("id")
        if not isinstance(identity,str) or not identity or identity in all_rc_ids or identity in index:
            block("wall_under_roof_rc_identity_invalid",component_id=identity)
            continue
        all_rc_ids.add(identity)
        role = g.get("slab_role") or g.get("semantic_role")
        if (g.get("category") != "slab" or not isinstance(role,str) or role not in {"floor_slab","overhead_slab","slab_step_riser"}
                or g.get("slab_role") and g.get("semantic_role") and g["slab_role"] != g["semantic_role"]):
            block("wall_under_roof_rc_role_invalid",component_id=identity,role=role)
            continue
        if role == "floor_slab":
            continue
        if role == "slab_step_riser":
            host = g.get("host_slab_role")
            if host == "floor_slab":
                continue
            if host != "overhead_slab":
                block("wall_under_roof_riser_host_invalid",component_id=identity)
                continue
        try:
            p,base,top = _geometry(g)
            roofs[identity] = {"group":g,"shape":p,"bottom":base,"top":top,"sources":_ids(g["source_entity_ids"]),"role":role}
        except (KeyError,TypeError,ValueError) as error:
            block("wall_under_roof_rc_invalid",component_id=identity,detail=str(error))
    if not roofs:
        block("wall_under_roof_overhead_required")
    projections = {}
    if policy == "height_projection_regions":
        recipes = config.get("height_projection_regions")
        if not isinstance(recipes,list):
            block("wall_under_roof_projection_config_invalid",detail="Explicit height_projection_regions list required")
            recipes = []
        for recipe in recipes:
            identity = recipe.get("id") if isinstance(recipe,dict) else None
            try:
                if not isinstance(identity,str) or not identity.strip() or identity in projections:
                    raise ValueError("Projection regions require distinct nonempty IDs")
                ids = _ids(recipe.get("roof_component_ids"))
                sources = _ids(recipe.get("source_entity_ids"))
                if not ids or not sources:
                    raise ValueError("Projection roof and source identity bindings must be explicit")
                if any(k not in roofs or roofs[k]["role"] != "overhead_slab" for k in ids):
                    raise ValueError("Only actual horizontal overhead slabs can be height-projected")
                cap = _number(recipe.get("cap_z_mm"),"projection cap_z_mm")
                if any(abs(roofs[k]["bottom"]-cap) > HEIGHT_TOLERANCE_MM for k in ids):
                    raise ValueError("Projection cap differs from referenced horizontal RC bottom")
                source_set = set(sources)
                if (not source_set <= {s for k in ids for s in roofs[k]["sources"]}
                        or any(not source_set.intersection(roofs[k]["sources"]) for k in ids)):
                    raise ValueError("Projection source IDs must belong to and identify each referenced actual roof")
                p = _projection_shape(recipe.get("footprint"))
                projections[identity] = {"shape":p,"cap":min(roofs[k]["bottom"] for k in ids),
                                         "roof_ids":ids,"sources":sources,"recipe":copy.deepcopy(recipe)}
            except (KeyError,TypeError,ValueError) as error:
                block("wall_under_roof_projection_invalid",projection_region_id=identity,detail=str(error))
        audit["height_projection_regions"] = [copy.deepcopy(projections[k]["recipe"]) for k in sorted(projections)]
    audit["original_groups"] = [copy.deepcopy(index[i]) for i in targets if i in index]
    if result["blockers"]:
        return fail()
    replacements, used_roofs = {}, set()
    emitted_ids = set(index)|all_rc_ids
    for identity in targets:
        original = index[identity]
        original_shape, base, nominal_top = selected[identity]
        candidates = {k:v for k,v in roofs.items() if v["shape"].intersection(original_shape).area > AREA_TOLERANCE_MM2}
        by_bottom = {}
        for key,roof in candidates.items():
            by_bottom.setdefault(roof["bottom"],[]).append(key)
        unclaimed = original_shape
        regions = []
        def add_region(cap,covered,ids,sources,coverage,projection_ids=()):
            used_roofs.update(ids)
            region_id = f"{identity}:cap-{len(regions)+1:03}"
            if cap <= base + HEIGHT_TOLERANCE_MM:
                block("wall_under_roof_cap_not_above_base",component_id=identity,cap_region_id=region_id,
                      base_z_mm=base,cap_z_mm=cap,footprint=_encoded(covered),roof_component_ids=ids)
            regions.append({"id":region_id,"coverage":coverage,"cap_z_mm":cap,"roof_component_ids":ids,
                            "modifier_source_entity_ids":sources,"height_projection_region_ids":list(projection_ids),
                            "footprint":_encoded(covered),"area_mm2":covered.area,"_shape":covered})
        for cap,ids in sorted(by_bottom.items()):
            covered = unclaimed.intersection(unary_union([candidates[k]["shape"] for k in ids]))
            if covered.area <= AREA_TOLERANCE_MM2:
                continue
            actual_ids = [k for k in sorted(ids) if candidates[k]["shape"].intersection(covered).area > AREA_TOLERANCE_MM2]
            add_region(cap,covered,actual_ids,sorted({s for k in actual_ids for s in candidates[k]["sources"]}),"covered")
            unclaimed = unclaimed.difference(covered)
        # Restrict pairwise ambiguity tests to projections actually reaching this
        # uncovered material, not every region of a whole building's recipe.
        projection_items = sorted((k,p) for k,p in projections.items()
                                  if p["shape"].intersection(unclaimed).area > AREA_TOLERANCE_MM2)
        for i,(first_id,first) in enumerate(projection_items):
            for second_id,second in projection_items[i+1:]:
                if abs(first["cap"]-second["cap"]) <= HEIGHT_TOLERANCE_MM:
                    continue
                conflict = unclaimed.intersection(first["shape"]).intersection(second["shape"])
                if conflict.area > AREA_TOLERANCE_MM2:
                    block("wall_under_roof_projection_conflict",component_id=identity,
                          projection_region_ids=[first_id,second_id],footprint=_encoded(conflict))
        by_projection_cap = {}
        for key,projection in projection_items:
            by_projection_cap.setdefault(projection["cap"],[]).append(key)
        for cap,ids in sorted(by_projection_cap.items()):
            covered = unclaimed.intersection(unary_union([projections[k]["shape"] for k in ids]))
            if covered.area <= AREA_TOLERANCE_MM2:
                continue
            active = [k for k in ids if projections[k]["shape"].intersection(covered).area > AREA_TOLERANCE_MM2]
            refs = sorted({r for k in active for r in projections[k]["roof_ids"]})
            sources = sorted({s for k in active for s in projections[k]["sources"]})
            add_region(cap,covered,refs,sources,"height_projected",active)
            unclaimed = unclaimed.difference(covered)
        if unclaimed.area > AREA_TOLERANCE_MM2:
            if require_projection:
                block("wall_under_roof_projected_coverage_incomplete",component_id=identity,footprint=_encoded(unclaimed))
            regions.append({"id":f"{identity}:uncovered","coverage":"uncovered","cap_z_mm":nominal_top,
                            "roof_component_ids":[],"modifier_source_entity_ids":[],"footprint":_encoded(unclaimed),"area_mm2":unclaimed.area,"_shape":unclaimed})
        trim = sum(r["area_mm2"]*max(0,nominal_top-r["cap_z_mm"]) for r in regions)
        extend = sum(r["area_mm2"]*max(0,r["cap_z_mm"]-nominal_top) for r in regions)
        original_volume = original_shape.area*(nominal_top-base)
        row = {"original_component_id":identity,"result_component_ids":[identity],"nominal_base_z_mm":base,"nominal_top_z_mm":nominal_top,
               "original_volume_mm3":original_volume,"trimmed_volume_mm3":trim,"extended_volume_mm3":extend,
               "expected_fitted_volume_mm3":original_volume-trim+extend,"cap_regions":[{k:v for k,v in r.items() if k!="_shape"} for r in regions],
               "status":"uncovered_original_retained" if all(r["coverage"]=="uncovered" for r in regions) else "already_at_actual_or_projected_underside"}
        audit["changes"].append(row)
        if any(r["cap_z_mm"] <= base + HEIGHT_TOLERANCE_MM for r in regions):
            row["status"] = "blocked_cap_not_above_original_base"
            continue
        coverage = unary_union([r["_shape"] for r in regions])
        if coverage.symmetric_difference(original_shape).area > AREA_TOLERANCE_MM2:
            block("wall_under_roof_footprint_coverage_invalid",component_id=identity)
        if all(abs(r["cap_z_mm"]-nominal_top) <= HEIGHT_TOLERANCE_MM for r in regions):
            row["fitted_volume_mm3"] = original_volume
            continue
        merged = {}
        for r in regions:
            cap = nominal_top if abs(r["cap_z_mm"]-nominal_top) <= HEIGHT_TOLERANCE_MM else r["cap_z_mm"]
            merged.setdefault(cap,[]).append(r["_shape"])
        pieces = [(cap,p) for cap,shapes in sorted(merged.items())
                  for p in sorted(_parts(unary_union(shapes)),key=lambda p:p.normalize().wkb_hex) if p.area > AREA_TOLERANCE_MM2]
        output = []
        for number,(cap,p) in enumerate(pieces,1):
            new_id = f"{identity}-under-roof-{number:03}"
            if new_id in emitted_ids:
                block("wall_under_roof_generated_id_collision",component_id=new_id)
                continue
            emitted_ids.add(new_id)
            contributors = [r for r in regions if r["_shape"].intersection(p).area > AREA_TOLERANCE_MM2]
            roof_ids = sorted({k for r in contributors for k in r["roof_component_ids"]})
            modifiers = sorted({s for r in contributors for s in r["modifier_source_entity_ids"]})
            trace = {"schema":"dwg-wall-under-roof-provenance-v1","operation":OPERATION,"original_component_id":identity,
                     "original_source_entity_ids":copy.deepcopy(original["source_entity_ids"]),"roof_component_ids":roof_ids,
                     "modifier_source_entity_ids":modifiers,"cap_region_ids":[r["id"] for r in contributors],
                     "height_projection_region_ids":sorted({k for r in contributors for k in r.get("height_projection_region_ids",[])}),
                     "typed_sources":[{"entity_id":s,"role":"roof_underside_height_constraint","rc_component_ids":[k for k in roof_ids if s in roofs[k]["sources"]]} for s in modifiers],
                     "nominal_base_z_mm":base,"nominal_top_z_mm":nominal_top,"effective_base_z_mm":base,"effective_top_z_mm":cap,
                     "uncovered_footprint":_encoded(p.intersection(unclaimed))}
            if original.get("geometry_modifier_provenance") is not None:
                trace["prior_geometry_modifier_provenance"] = copy.deepcopy(original["geometry_modifier_provenance"])
            group = copy.deepcopy(original)
            group.update(id=new_id,name=f"{original.get('name',identity)}-under-roof-{number:03}",
                         wall_under_roof_original_component_id=identity,nominal_base_z_mm=base,nominal_top_z_mm=nominal_top,nominal_height_mm=nominal_top-base,
                         base_z_mm=base,top_z_mm=cap,height_mm=cap-base,area_mm2=p.area,volume_mm3=p.area*(cap-base),
                         modifier_source_entity_ids=modifiers,geometry_modifier_provenance=trace,**_shape(p))
            output.append(group)
            result["height_rules"][new_id] = {"base_z_mm":base,"height_mm":cap-base}
        replacements[identity] = output
        fitted = sum(g["volume_mm3"] for g in output)
        row.update(result_component_ids=[g["id"] for g in output],fitted_volume_mm3=fitted,status="fitted_into_editable_parts")
        if abs(fitted-row["expected_fitted_volume_mm3"]) > max(AREA_TOLERANCE_MM2*(nominal_top-base),original_volume*1e-12):
            block("wall_under_roof_volume_mismatch",component_id=identity)
    audit["roofs"] = [copy.deepcopy(roofs[k]["group"]) for k in sorted(used_roofs)]
    if result["blockers"]:
        return fail()
    result["groups"] = [p for g in base_groups for p in replacements.get(g["id"],[copy.deepcopy(g)])]
    audit.update(untouched_component_ids=[g["id"] for g in base_groups if g["id"] not in replacements],
                 height_rules=copy.deepcopy(result["height_rules"]),all_pass=True,result_component_count=len(result["groups"]),
                 trimmed_volume_mm3=sum(r["trimmed_volume_mm3"] for r in audit["changes"]),
                 extended_volume_mm3=sum(r["extended_volume_mm3"] for r in audit["changes"]))
    return result
