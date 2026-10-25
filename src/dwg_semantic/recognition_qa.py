"""Geometric invariants checked independently on recognition output."""
from __future__ import annotations

from collections import defaultdict
import math

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from .plan_geometry import shape_of


def audit_recognition(result,index,active):
    checks=[];review=[]
    def check(name,passed,**details):
        checks.append({"check":name,"pass":bool(passed),**details})
    materials=[*result["wall_materials"],*result["columns"],
               *[row for system in result["facade_systems"]for row in system["sections"]]]
    check("all_material_footprints_valid_and_positive",all(shape_of(row).is_valid and shape_of(row).area>0 for row in materials),count=len(materials))
    known=set(index.by_id)
    check("every_material_has_exact_placed_source_ids",all(row["source_entity_ids"]and set(row["source_entity_ids"])<=known for row in materials))
    family_materials=defaultdict(list)
    for wall in result["wall_materials"]:
        for layer in wall["source_layers"]:
            family_materials[layer].append(shape_of(wall))
    void_overlaps=[]
    for void in result["wall_voids"]:
        material=unary_union(family_materials[void["source_layer"]])
        overlap=shape_of(void).intersection(material).area
        if overlap>.1:
            void_overlaps.append({"void_id":void["id"],"overlap_mm2":overlap})
    check("wall_inner_voids_remain_empty_in_their_source_wall_family",not void_overlaps,void_count=len(result["wall_voids"]),failures=void_overlaps)
    foreign=[]
    for void in result["wall_voids"]:
        others=unary_union([shape_of(wall)for wall in result["wall_materials"]if void["source_layer"]not in wall["source_layers"]])
        area=shape_of(void).intersection(others).area
        if area>1:
            foreign.append({"void_id":void["id"],"foreign_material_area_mm2":area})
    if foreign:
        review.append({"issue":"other_source_wall_material_crosses_a_wall_void","items":foreign})
    doors=result.get("door_assemblies",[])
    missing=[d["instance_path"]for d in doors if not d.get("clearance_footprint")]
    check("all_recognized_door_assemblies_have_measured_plan_footprints",not missing,count=len(doors),unresolved_instance_paths=missing)
    leaf_errors=[];depth_errors=[]
    for door in doors:
        if not door.get("axis_world_mm"):
            continue
        axis=LineString(door["axis_world_mm"])
        for leaf in door.get("leaf_matches",[]):
            if not axis.buffer(.1).covers(LineString(leaf["axis"])):
                leaf_errors.append(door["instance_path"])
        if door.get("lintel_footprint"):
            a,b=door["axis_world_mm"];length=math.dist(a,b);normal=(-(b[1]-a[1])/length,(b[0]-a[0])/length)
            values=[(p[0]-a[0])*normal[0]+(p[1]-a[1])*normal[1]for p in shape_of(door["lintel_footprint"]).exterior.coords]
            actual=max(values)-min(values)
            expected=door["host_audit"]["wall_thickness_mm"]
            if abs(actual-expected)>.02:
                depth_errors.append({"instance_path":door["instance_path"],"actual_mm":actual,"host_wall_mm":expected})
    check("door_axes_cover_every_source_leaf_not_only_the_first",not leaf_errors,
          multi_leaf_count=sum(d.get("leaf_count",0)>1 for d in doors),failures=sorted(set(leaf_errors)))
    check("lintel_plan_depth_matches_measured_host_wall_faces",not depth_errors,failures=depth_errors)
    cut_errors=[row for row in result.get("door_wall_cut_audit",[])if row["remaining_opening_overlap_mm2"]>.1]
    check("door_level_wall_material_does_not_refill_recognized_openings",not cut_errors,
          cut_wall_count=len(result.get("door_wall_cut_audit",[])),failures=cut_errors)
    slab=result.get("slab_openings",{})
    canonical=slab.get("canonical_symbol_cut_regions",[])
    shapes=[shape_of(row)for row in canonical]
    overlap=sum(a.intersection(b).area for i,a in enumerate(shapes)for b in shapes[i+1:])
    check("deduplicated_slab_symbol_cut_regions_do_not_overlap",overlap<=.1,count=len(canonical),overlap_area_mm2=overlap)
    check("every_open_bent_mark_has_four_source_supported_sides",all(len(row["side_support_audit"])==4 and min(s["coverage_ratio"]for s in row["side_support_audit"])>=.999 for row in slab.get("open_bent_marks",[])))
    shaft_errors=[]
    for shaft in slab.get("elevator_shafts",[]):
        if not shaft["boundary_source_entity_ids"]or not set(shaft["boundary_source_entity_ids"])<=known:
            shaft_errors.append(shaft["id"])
    check("elevator_cuts_are_bound_to_full_native_shaft_perimeters",not shaft_errors,count=len(slab.get("elevator_shafts",[])),failures=shaft_errors)
    for system in result["facade_systems"]:
        if system["status"].startswith("source_bound_system_"):
            continue
        check("facade_source_hatch_boundaries_are_extracted",not system["hatch_boundary_failures"],system_instance_path=system["instance_path"],profile_count=len(system["hatch_profiles"]))
        bay_air=unary_union([shape_of(row)for row in system.get("bay_air_regions",[])])
        frame_material=unary_union([shape_of(row)for row in system["sections"]if row["semantic_subrole"]=="frame"])
        check("facade_bay_air_is_not_filled_with_frame_material",bay_air.intersection(frame_material).area<=.1,
              system_instance_path=system["instance_path"],bay_air_regions=len(system.get("bay_air_regions",[])))
        if system.get("perimeter_audit",{}).get("status")!="closed_source_perimeter_found":
            review.append({"issue":"facade_perimeter_requires_closure_confirmation","system_instance_path":system["instance_path"]})
    used=defaultdict(list)
    for row in materials:
        for key in row["source_entity_ids"]:
            used[key].append(row["id"])
    for rows in(result["wall_voids"],doors,slab.get("elevator_shafts",[]),slab.get("bounded_marks",[]),slab.get("open_bent_marks",[])):
        for row in rows:
            for key in row["source_entity_ids"]:
                used[key].append(row["id"])
    for system in result["facade_systems"]:
        for row in system.get("bay_air_regions",[]):
            for key in row["source_entity_ids"]:
                used[key].append(row["id"])
    reference_system_ids={key for system in result["facade_systems"]if system["status"].startswith("source_bound_system_")for key in system["source_entity_ids"]}
    accounted=[]
    for key,e in index.by_id.items():
        status=("outside_primary_plan_domain"if key not in active else
                "recognized_candidate_or_boundary"if used[key]else
                "source_bound_reference_system"if key in reference_system_ids else
                "unclassified_geometry_retained_for_review"if index.lines_by_id[key]else
                "annotation_or_non_linear_source_retained")
        accounted.append({"source_entity_id":key,"original_layer":e.get("original_layer",e.get("layer")),
                          "effective_layer":e.get("effective_layer",e.get("layer")),"instance_path":e.get("instance_path"),
                          "type":e.get("type"),"status":status,"candidate_ids":sorted(set(used[key])),
                          "source_z_audit":index.planes[key]})
    check("every_placed_leaf_has_a_source_disposition",len(accounted)==len(index.by_id),count=len(accounted))
    if slab.get("stair_boundary_review_requests"):
        review.append({"issue":"exact_stair_enclosure_boundary_or_slab_owner_pending","candidate_ids":[r["id"]for r in slab["stair_boundary_review_requests"]]})
    return {"all_geometric_invariants_pass":all(c["pass"]for c in checks),"checks":checks,"review_items":review,
            "source_dispositions":accounted,"qualification":"Plan-geometry checks; independent vertical and slab-owner decisions remain visible."}
