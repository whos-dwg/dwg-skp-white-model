"""Second-pass independent counterexamples, with production Ruby create_group.

These tests do not access a live app. Layer R+ calls production create_group on
synthetic SketchUp collections with real affine matrix/inverse calculations.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from shapely.geometry import Polygon

from test_native_safety_contract import (source,document,selection,snapshot,poly,entity_id,
                                         plan_add,plan_opening,run_ruby,LiveRepairError)


def test_n16_distinct_source_layers_cannot_produce_duplicate_group_ids(source):
    doc=document(source,polygons=[poly(source,"A",[[0,0],[1000,0],[1000,200],[0,200]],layer="A-WALL"),
                                 poly(source,"B",[[2000,0],[3000,0],[3000,200],[2000,200]],layer="A-PARTITION")],door=False)
    plan=plan_add(source,doc=doc,picked=selection(source,["A","B"],layer="A-ASSEMBLY"))
    assert len(plan["create"])==2
    identities=[row["group_id"] for row in plan["create"]]
    assert len(set(identities))==len(identities),f"duplicate white-model identities: {identities}"


def test_n17_open_selected_remainder_is_not_silently_discarded(source):
    contour=poly(source,"P1",[[0,0],[1000,0],[1000,200],[0,200]])
    dangling={"entity_id":entity_id(source,"L1"),"handle":"L1","source_handle":"L1","type":"LINE",
              "source_file":source["source_path"],"layer":"A-WALL","original_layer":"A-WALL",
              "effective_layer":"A-WALL","instance_path":"","owner_block":"*Model_Space",
              "start_world":[5000,0,0],"end_world":[5500,0,0]}
    with pytest.raises((LiveRepairError,ValueError)):
        plan_add(source,doc=document(source,polygons=[contour,dangling],door=False),
                  picked=selection(source,["P1","L1"],layer="A-WALL"))


def test_n18_live_add_reuses_existing_reversible_source_tag_mapping(source):
    live=snapshot(source,entities=[])
    live["root"]["tag_mapping"]=[{"source_id":source["source_path"],"source_layer":"A-WALL",
                                  "tag":"MAIN::A-WALL","reason":"source_collision"}]
    plan=plan_add(source,live=live)
    assert plan["create"][0]["tag"]=="MAIN::A-WALL","raw source layer is not always the existing SketchUp Tag"


@pytest.mark.parametrize("transform",[
    {"origin_mm":[300.0,-450.0,0.0],"xaxis":[1.,0.,0.],"yaxis":[0.,1.,0.],"zaxis":[0.,0.,1.]},
    {"origin_mm":[300.0,-450.0,0.0],"xaxis":[0.,1.,0.],"yaxis":[-1.,0.,0.],"zaxis":[0.,0.,1.]}
])
def test_rplus_production_create_group_uses_inverse_parent_and_untagged_faces(source,transform):
    live=snapshot(source,entities=[])
    live["containers"][0]["world_transform"]=transform
    result=run_ruby({"snapshot":live,"plan":plan_add(source,live=live),"production_create":True})
    assert result["accepted"] is True,result
    assert result["operations"]==["start","commit"],result
    assert result["selection_entity_ids"] == [row["entity_id"] for row in result["receipt"]["created"]]


def test_rplus_production_face_creation_failure_aborts_without_partial_commit(source):
    live=snapshot(source,entities=[])
    result=run_ruby({"snapshot":live,"plan":plan_add(source,live=live),"production_create":True,"inject_create_failure":True})
    assert result["accepted"] is False,result
    assert result["operations"]==["start","abort"],result


def test_n03_receipt_race_never_reports_an_applied_operation_as_unexecuted(source,tmp_path):
    from dwg_semantic.pipeline import live_repair_apply_job
    plan=plan_opening(source)
    path=tmp_path/"synthetic-plan.json";path.write_text(json.dumps(plan),encoding="utf-8")
    receipt=tmp_path/"racing-receipt.json"
    committed={"status":"applied","verified":True,"plan_sha256":plan["plan_sha256"],"model_saved":False}
    def applied(*args,**kwargs):
        receipt.write_text("Evidence appeared during the remote call",encoding="utf-8")
        return copy.deepcopy(committed)
    with patch("dwg_semantic.sketchup_live.apply_live_repair_plan",side_effect=applied):
        try:
            result=live_repair_apply_job(path,confirmation_sha256=plan["plan_sha256"],receipt_path=receipt)
        except Exception as error:
            assert getattr(error,"applied_receipt",None)==committed, "post-commit receipt failure lost the applied result"
        else:
            assert result["status"]=="applied" and result.get("receipt_persistence_error"),result


@pytest.mark.parametrize("mutation",["missing_truncation_flag","non_boolean_truncation_flag","boolean_returned_count"])
def test_n19_pickfirst_completeness_requires_well_typed_positive_evidence(source,mutation):
    picked=selection(source)
    if mutation=="missing_truncation_flag": picked.pop("truncated")
    elif mutation=="non_boolean_truncation_flag": picked["truncated"]=None
    else: picked["returned_count"]=True
    with pytest.raises((LiveRepairError,ValueError)):
        plan_opening(source,picked=picked)


def rehash(plan):
    body={k:v for k,v in plan.items() if k not in {"plan_sha256","canonical_body_json"}}
    plan["canonical_body_json"]=json.dumps(body,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False)
    plan["plan_sha256"]=hashlib.sha256(plan["canonical_body_json"].encode("utf-8")).hexdigest()
    return plan


@pytest.mark.parametrize("mutation",["unknown_action","remove_action_with_create","wrong_source","invalid_write_policy"])
def test_n20_ruby_plan_schema_is_a_semantic_contract_not_only_self_hash(source,mutation):
    live=snapshot(source,entities=[])
    plan=plan_add(source,live=live)
    if mutation=="unknown_action": plan["action"]="execute_unapproved_change"
    elif mutation=="remove_action_with_create": plan["action"]="remove_wrong"
    elif mutation=="wrong_source":
        plan["source"]["sha256"]="a-different-source"
        plan["source"]["manifest"]["source_sha256"]="a-different-source"
    else: plan["write_policy"]["save_model"]=True
    result=run_ruby({"snapshot":live,"plan":rehash(plan)})
    assert result["accepted"] is False,f"bridge accepted incoherent {mutation} plan: {result}"
    assert result["operations"]==[],f"incoherent plan must be rejected before Undo: {result}"


def test_rplus_real_inspector_accepts_actual_clean_prism_mesh(source):
    live=snapshot(source,entities=[])
    result=run_ruby({"mode":"inspect_geometry","snapshot":live,"plan":plan_add(source,live=live)})
    assert result["accepted"] is True,result
    assert len(result["inspection"]["entities"])==1,result


@pytest.mark.parametrize("mutation",["sloped_top","sheared_top","nested_group"])
def test_rplus_real_inspector_rejects_non_prism_or_unsupported_nested_leaf(source,mutation):
    live=snapshot(source,entities=[])
    result=run_ruby({"mode":"inspect_geometry","snapshot":live,"plan":plan_add(source,live=live),"mutation":mutation})
    assert result["accepted"] is False,f"unsupported {mutation} leaf was certified: {result}"
    assert result["operations"]==[],result


def test_n21_whole_stepped_slab_companion_does_not_disable_bounded_wall_repairs(source):
    live=snapshot(source,entities=[])
    result=run_ruby({"mode":"inspect_geometry","snapshot":live,"plan":plan_add(source,live=live),
                     "mutation":"stepped_slab_companion"})
    assert result["accepted"] is True,"unsupported-to-edit companions must be protected, not poison wall inspection: "+str(result)
    assert "stepped-slab:302" in json.dumps(result["inspection"]),"companion geometry/provenance cannot disappear silently"


