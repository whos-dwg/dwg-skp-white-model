"""Transfer reviewed automatic footprints into the normal solid planner.

Recognition proposes XY geometry. Category, primary source Tag and Z continue
to come from the job decisions and are checked by the existing review/build
contract. This prevents the writer from reverting to individual filled loops
or the first leaf after an assembly has been recognized.
"""
from __future__ import annotations

from collections import defaultdict
import json
import math

from .plan_geometry import layer_of, shape_of


def recognized_records(document, classification, decisions, tag_lookup, *, partition_audit=None):
    from .modeling import entity_key, model_entities, _source, _raw_layer, _source_plane
    automatic=classification.get("automatic_plan")
    if not automatic or decisions.get("use_automatic_plan_recognition",True)is False:
        return [],[],set(),[]
    by_id={entity_key(e):e for e in model_entities(document)}
    mapping=decisions.get("layer_mapping",{})
    overrides=decisions.get("object_overrides",{})
    policy=decisions.get("source_tag_policy","effective")
    blocked_sources={key for request in automatic.get("layer_conflict_requests",[])
                     if request["status"]=="user_confirmation_required"for key in request["source_entity_ids"]
                     if not (isinstance(overrides.get(key),dict)and overrides[key].get("category")and overrides[key].get("reason"))}
    blockers=[{"code":"parent_child_layer_semantics_require_confirmation","source_entity_ids":sorted(blocked_sources)}]if blocked_sources else[]
    if decisions.get("slabs",{}).get("enabled"):
        blockers.extend(automatic.get("slab_openings",{}).get("decision_issues",[]))
    for system in automatic.get("facade_systems",[]):
        if system.get("status","").startswith("source_bound_system_"):
            continue
        if system.get("perimeter_audit",{}).get("status") == "closed_source_perimeter_found":
            continue
        choice=automatic.get("settings",{}).get("system_decisions",{}).get(system["instance_path"],{})
        if choice.get("perimeter_closure")=="accepted_open_system" and choice.get("reason"):
            continue
        modeled=[key for key in system["source_entity_ids"] if key in by_id
                 and overrides.get(key,mapping.get(_raw_layer(by_id[key],policy),{})).get("category") not in {None,"reference","exclude"}]
        if modeled:
            blockers.append({"code":"facade_perimeter_closure_requires_confirmation","source_entity_ids":modeled,
                             "system_instance_path":system["instance_path"]})
            blocked_sources.update(modeled)
    candidates=[*automatic.get("wall_materials",[]),*automatic.get("columns",[])]
    candidates += [section for system in automatic.get("facade_systems",[])for section in system["sections"]]
    materials=[];openings=[];consumed=set()
    def record(candidate,category,footprint):
        source_ids=[key for key in candidate["source_entity_ids"]if key in by_id]
        if not source_ids or blocked_sources&set(source_ids):
            return None
        layers=sorted({_raw_layer(by_id[key],policy)for key in source_ids})
        local=overrides.get(candidate["id"],{})
        reference_sources=[key for key in source_ids if overrides.get(key,{}).get('category')=='reference'
                           and str(overrides[key].get('reason')or'').strip()]
        primary=local.get("primary_source_layer")or(candidate.get("source_layer")if candidate.get("source_layer")in layers else None)or(layers[0]if len(layers)==1 else None)
        if primary not in layers:
            return None
        config=dict(mapping.get(primary)or{})
        source_choices=[overrides[key]for key in source_ids if isinstance(overrides.get(key),dict)]
        for field in ("category","height_mm","base_z_mm","primary_source_layer","footprint_confirmed","footprint"):
            values={json.dumps(choice[field],sort_keys=True) for choice in source_choices if choice.get(field)is not None}
            if len(values)>1 and local.get(field)is None:
                blockers.append({"code":"conflicting_composite_source_decisions","candidate_id":candidate["id"],
                                 "field":field,"source_entity_ids":source_ids})
                blocked_sources.update(source_ids)
                return None
        # An assembly override is more specific than a source-layer choice.
        for choice in source_choices:
            config.update({k:v for k,v in choice.items()if v is not None})
        config.update({k:v for k,v in local.items()if v is not None})
        if config.get("footprint"):
            if len(source_ids)>1 and not local.get("footprint"):
                blockers.append({"code":"composite_source_footprint_requires_assembly_override","candidate_id":candidate["id"],
                                 "source_entity_ids":source_ids})
                blocked_sources.update(source_ids)
                return None
            from .plan_geometry import footprint as encode_footprint
            explicit=shape_of(config["footprint"])
            if not explicit.is_valid or explicit.area<=0:
                blockers.append({"code":"invalid_explicit_recognition_footprint","candidate_id":candidate["id"],"source_entity_ids":source_ids})
                blocked_sources.update(source_ids)
                return None
            footprint=encode_footprint(explicit)
        if config.get("category")!=category or not config.get("footprint_confirmed"):
            return None
        if category!='opening' and set(source_ids)<=set(reference_sources):
            blockers.append({'code':'automatic_material_has_only_reference_sources','candidate_id':candidate['id'],
                             'source_entity_ids':source_ids})
            return None
        height=config.get("height_mm",decisions.get("opening_height_mm")if category=="opening"else decisions.get("default_height_mm"))
        if height is None:
            height=decisions.get("opening_height_mm")if category=="opening"else decisions.get("default_height_mm")
        if not isinstance(height,(int,float))or isinstance(height,bool)or not math.isfinite(height)or height<=0:
            return None
        source_set={_source(by_id[key])for key in source_ids}
        if len(source_set)!=1:
            return None
        source=next(iter(source_set))
        base=config.get("base_z_mm",0)
        if not isinstance(base,(int,float))or isinstance(base,bool)or not math.isfinite(base):
            return None
        provenance=[{"entity_id":key,"original_layer":by_id[key].get("original_layer",by_id[key].get("layer")),
                     "effective_layer":by_id[key].get("effective_layer",by_id[key].get("layer")),
                     "block_layer":by_id[key].get("block_layer"),"instance_path":by_id[key].get("instance_path"),
                     **_source_plane(by_id[key])}for key in source_ids]
        low=min(row["source_z_range_mm"][0]for row in provenance)
        high=max(row["source_z_range_mm"][1]for row in provenance)
        if any(row["source_nonplanar"]for row in provenance)or high-low>.01:
            return None
        return {"id":candidate["id"],"name":str(config.get("name")or candidate["id"]),
                "category":category,"source_layer":primary,"source_layers":layers,
                "source_entity_ids":source_ids,"source_handles":sorted({str(by_id[key].get("source_handle")or by_id[key].get("handle")or"")for key in source_ids}),
                "source_id":source,"tag":tag_lookup.get((source,primary),primary),
                "base_z_mm":base,"height_mm":height,"config":config,
                "footprint":footprint,"segments":[],"automatic_recognition_id":candidate["id"],
                "source_plane_z_mm":low,"source_z_range_mm":[low,high],"source_nonplanar":False,
                "source_provenance":provenance,
                'reference_source_entity_ids':reference_sources,
                "geometry_basis":"source_bound_automatic_plan_topology"}
    prepared={}
    associated=defaultdict(set)
    for candidate in candidates:
        for key in candidate["source_entity_ids"]:
            associated[key].add(candidate["id"])
        item=record(candidate,candidate["category"],candidate["footprint"])
        if item:
            prepared[candidate["id"]]=item
    # One CAD polyline may support several pieces. Its original full polygon
    # must not be emitted beside a partly transferred material network.
    changed=True
    while changed:
        changed=False
        for identity,item in list(prepared.items()):
            if any(not associated[key]<=prepared.keys()for key in item["source_entity_ids"]):
                del prepared[identity];changed=True
    materials=list(prepared.values())
    from .material_partition import partition_materials
    materials,audit,issues=partition_materials(materials,decisions.get('material_partition'))
    blockers.extend(issues)
    if partition_audit is not None and audit is not None:partition_audit.update(audit)
    for item in materials:
        consumed.update(set(item["source_entity_ids"])-set(item.get('reference_source_entity_ids',[])))
    for candidate in candidates:
        if candidate["id"]in prepared or not candidate["footprint"].get("holes"):
            continue
        unresolved=[key for key in candidate["source_entity_ids"]if key in by_id and key not in consumed
                    and not overrides.get(key,{}).get("footprint")
                    and mapping.get(_raw_layer(by_id[key],policy),{}).get("category")not in{"reference","exclude"}]
        if unresolved:
            blockers.append({"code":"automatic_material_topology_requires_candidate_decision",
                             "candidate_id":candidate["id"],"source_entity_ids":unresolved})
    for door in automatic.get("door_assemblies",[]):
        if not door.get("clearance_footprint"):
            continue
        candidate={**door,"id":door["id"]}
        item=record(candidate,"opening",door["clearance_footprint"])
        if not item:
            continue
        item["config"]["lintel_footprint"]=door["lintel_footprint"]
        item["config"]["lintel_footprint_basis"]=door["lintel_footprint_basis"]
        item["opening_assembly_audit"]={k:door[k]for k in("axis_world_mm","leaf_span_mm","leaf_count","host_audit")if k in door}
        openings.append(item)
        consumed.update(item["source_entity_ids"])
    prepared_doors={row["id"]for row in openings}
    for door in automatic.get("door_assemblies",[]):
        if door["id"]in prepared_doors:
            continue
        layer=door.get("source_layer")
        if mapping.get(layer,{}).get("category")=="opening":
            unresolved=[key for key in door["source_entity_ids"]if key in by_id and key not in consumed
                        and not overrides.get(key,{}).get("footprint")]
            if unresolved:
                blockers.append({"code":"complete_door_assembly_requires_candidate_decision",
                                 "candidate_id":door["id"],"source_entity_ids":unresolved})
    return materials,openings,consumed,blockers
