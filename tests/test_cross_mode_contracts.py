"""Independent cross-mode regressions for the second full-spec audit."""
from __future__ import annotations
import copy
import os
from pathlib import Path
import pytest
from dwg_semantic.modeling import prepare_model, write_model
from dwg_semantic.slab_modeling import prepare_slabs
from test_modeling import rectangle, decisions
from test_slab_modeling import decisions as slab_decisions, document, footprint, system, zone


def test_unique_handle_reference_override_is_consistent_with_final_source_accounting():
    first=rectangle("source:instance:AA")
    first.update(handle="AA",source_handle="AA")
    second=rectangle("source:instance:BB",x=5000)
    second.update(handle="BB",source_handle="BB")
    config=decisions()
    config["object_overrides"]={"AA":{"category":"reference","reason":"Unique source handle explicitly retained as reference"}}
    payload=prepare_model({"entities":[first,second]},{},config)
    assert payload["blockers"]==[]
    assert payload["final_source_accounting"]["all_accounted"]


def test_unique_handle_explicit_footprint_does_not_get_rechecked_against_old_contour():
    first=rectangle("source:instance:AA")
    first.update(handle="AA",source_handle="AA")
    second=rectangle("source:instance:BB",x=5000)
    second.update(handle="BB",source_handle="BB")
    config=decisions()
    config["object_overrides"]={"AA":{"footprint":footprint(0,0,1000,200)}}
    payload=prepare_model({"entities":[first,second]},{},config)
    assert payload["blockers"]==[]


def test_whole_uniform_leaf_contains_complete_union_of_source_provenance():
    config=slab_decisions(system(assembly_mode="whole_uniform_thickness",
        zones=[zone(footprint=footprint(0,0,5000,8000),top_z_mm=-600)],
        openings=[{"id":"shaft","source_entity_ids":["opening"],"footprint":footprint(6500,2500,500,1000),
                   "applies_to_system_id":"floor"}],
        step_risers={"enabled":True,"profile":"edge_aligned_full_depth","thickness_mm":200}))
    payload=prepare_slabs(document(),config)
    assert payload["blockers"]==[]
    whole=payload["groups"][0]
    assert {"B","L","Z","ZN","O"} <= set(whole["source_handles"])
    assert set(whole["source_entity_ids"]) <= {row["entity_id"] for row in whole["sources"]}
    assert {"level","zone_note"} <= set(whole["annotation_source_ids"])


@pytest.mark.skipif(os.environ.get("DWG_RUN_NATIVE_TESTS") != "1", reason="Opt-in actual SketchUp C API integration")
def test_actual_native_whole_slab_remote_low_zone_does_not_obstruct_door_in_high_zone(tmp_path):
    # Wall/door are below the high slab. The disconnected-in-Z low slab region
    # is remote in XY, so a whole-group bounding box must not obstruct the door.
    wall=rectangle("wall",x=6000,y=1000,width=2000,depth=200)
    opening=rectangle("door",layer="Doors",x=6500,y=1000,width=1000,depth=200)
    config=decisions()
    config["default_height_mm"]=3000
    config["layer_mapping"]["Walls"]["height_mm"]=3000
    config["layer_mapping"]["Doors"]={"category":"opening","height_mm":2100,"base_z_mm":0,
                                        "footprint_confirmed":True}
    base=prepare_model({"entities":[wall,opening]},{},config)
    assert base["blockers"]==[],base["blockers"]
    sc=slab_decisions(system(assembly_mode="whole_uniform_thickness",role="overhead",
        default_top_z_mm=3200,
        zones=[zone(footprint=footprint(0,0,5000,8000),top_z_mm=2000)],
        step_risers={"enabled":True,"profile":"edge_aligned_full_depth","thickness_mm":200}))
    slabs=prepare_slabs(document(),sc)
    assert slabs["blockers"]==[],slabs["blockers"]
    payload=copy.deepcopy(base)
    payload["groups"]+=slabs["groups"]
    payload["tag_mapping"]+=slabs["tag_mapping"]
    payload["slab_audit"]=slabs["slab_audit"]
    payload["whole_slab_audit"]=slabs["whole_slab_audit"]
    payload["presentation"]={"overhead_visible":True}
    qa=write_model(payload,tmp_path/"door-below-high-slab.skp")
    assert qa["all_pass"],[row for row in qa["checks"]if not row["pass"]]

@pytest.mark.skipif(os.environ.get("DWG_RUN_NATIVE_TESTS") != "1", reason="Opt-in actual SketchUp C API integration")
def test_typed_aff_binding_reaches_real_overhead_slab_build_with_indoor_preview(tmp_path):
    domain=footprint(0,0,1000,1000)
    room={"entity_id":"room","handle":"R","source_handle":"R","type":"LWPOLYLINE","layer":"A-ROOM",
          "owner_block":"*Model_Space","closed":True,
          "vertices":[{"x":x,"y":y,"z":0}for x,y in domain["outer"]]}
    def note(identity,text):
        return {"entity_id":identity,"handle":identity,"source_handle":identity,"type":"TEXT","layer":"Notes",
                "owner_block":"*Model_Space","text":text,"position":{"x":500,"y":500,"z":0}}
    doc={"entities":[room,note("aff","上層RC板底 淨高 3000mm AFF"),note("ffl","本層 FFL +0.100m")]}
    config={"enabled":True,"confirmed_scope":True,"floor_datum_m":0,
        "derived_tag":{"tag":"Synthetic RC","source_layer":"Synthetic RC","kind":"derived"},
        "systems":[{"id":"O","role":"overhead","extent":domain,"source_entity_ids":["room"],
                    "default_top_z_mm":None,"default_thickness_mm":200,"zones":[],"openings":[]}],
        "functional_regions":[{"id":"ROOM","footprint":domain,"source_entity_ids":["room"]}],
        "annotation_bindings":[{"id":"AFF-BOUND","source_entity_id":"aff","token_kind":"aff_height","token_index":0,
            "target_surface":"structural_bottom","reference_surface":"finished_floor","reference_z_mm":100,
            "reference_annotation_source_ids":["ffl"],"applies_to_system_id":"O","functional_region_id":"ROOM",
            "reason":"Synthetic typed source and exact functional-region binding, not a real project approval",
            "basis":"source_explicit"}]}
    slabs=prepare_slabs(doc,config)
    assert slabs["blockers"]==[],slabs["blockers"]
    assert slabs["annotation_binding_audit"]["all_pass"]
    assert all(group["base_z_mm"]==3100 and group["height_mm"]==200 for group in slabs["groups"])
    wall=prepare_model({"entities":[rectangle("wall",width=1000)]},{},decisions())
    # Keep the synthetic wall below the AFF-derived underside.
    wall["groups"][0]["height_mm"]=3000
    payload={"schema":"dwg-skp-model-v2","units":"mm",**slabs,
        "groups":[*wall["groups"],*slabs["groups"]],
        "references":[],"source_manifest":{"kind":"synthetic typed AFF integration"},
        "tag_mapping":[*wall["tag_mapping"],*slabs["tag_mapping"]],
        "coordinate_policy":"Isolated AFF finished-floor/reference binding fixture",
        "presentation":{"overhead_visible":True}}
    qa=write_model(payload,tmp_path/"typed-aff-indoor.skp")
    assert qa["all_pass"]
    indoor=next(row for row in qa["previews"]if row["view"]=="interior_overview"and row["format"]=="svg")
    assert indoor["includes_overhead_slabs"]is False
    svg=Path(indoor["path"]).read_text(encoding="utf-8")
    assert all(group["id"]not in svg for group in slabs["groups"])
    assert "wall"in svg
    assert all(row["pass"]for row in qa["checks"]if "overhead"in row["check"])


def test_bounded_join_envelope_uses_actual_whole_material_interval_not_remote_low_zone():
    from dwg_semantic.modeling import evaluate_slab_joins
    wall=rectangle("wall",x=6000,y=1000,width=2000,depth=200)
    config=decisions()
    config["layer_mapping"]["Walls"]["height_mm"]=3100
    base=prepare_model({"entities":[wall]},{},config)
    sc=slab_decisions(system(assembly_mode="whole_uniform_thickness",role="overhead",
        default_top_z_mm=3200,zones=[zone(footprint=footprint(0,0,5000,8000),top_z_mm=2000)],
        step_risers={"enabled":True,"profile":"edge_aligned_full_depth","thickness_mm":200}))
    slabs=prepare_slabs(document(),sc)
    whole=slabs["groups"][0]
    wall_group=base["groups"][0]
    rule={"schema":"dwg-slab-join-policy-v1","joins":[{
        "component_ids":[wall_group["id"],whole["id"]],
        "source_entity_ids":["wall","boundary"],"evidence":"Local reviewed connection in the high zone only",
        "allowed_contact":{"footprint":footprint(6000,1000,2000,200),"base_z_mm":3000,"top_z_mm":3100,
                           "max_overlap_volume_mm3":40000000}}]}
    result=evaluate_slab_joins(base["groups"]+slabs["groups"],policy=rule)
    assert result["invalid_policy"]==[],result
    assert result["unresolved_overlaps"]==[],result
    assert len(result["accepted_joins"])==1

@pytest.mark.skipif(os.environ.get("DWG_RUN_NATIVE_TESTS") != "1", reason="Opt-in actual SketchUp C API integration")
def test_whole_uniform_mode_respects_explicit_common_source_precision_before_assembling(tmp_path):
    from test_edge_aligned_risers import box, zone as source_zone, edge_aligned_fixture
    low=box(0,0,500,1000)
    low["holes"]=[[[100,400],[100.00904,600],[100.00904,400]]]
    doc,config=edge_aligned_fixture([source_zone("low",low,-600)])
    config["systems"][0]["step_risers"]["geometry_precision_mm"]=.05
    config["systems"][0]["assembly_mode"]="whole_uniform_thickness"
    slabs=prepare_slabs(doc,config)
    assert slabs["blockers"]==[],slabs["blockers"]
    assert slabs["slab_audit"]["geometry_precision"]
    payload={"schema":"dwg-skp-model-v2","units":"mm",**slabs,"references":[],
             "source_manifest":{"kind":"synthetic-explicit-common-precision"},
             "coordinate_policy":"Isolated explicitly authorized common source precision fixture"}
    qa=write_model(payload,tmp_path/"whole-explicit-common-precision.skp")
    assert qa["all_pass"],[row for row in qa["checks"]if not row["pass"]]




