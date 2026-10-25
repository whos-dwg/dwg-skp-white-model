"""Expected safety contract, NOT a snapshot of the known-broken implementation.

Layer P = pure Python synthetic fixtures/mocked boundary.
Layer R = production Ruby in an isolated embedded Ruby process with a synthetic
          SketchUp adapter; no SketchUp application, listener or live model.
Layer C = actual SketchUp C API write/reopen of a synthetic temporary model.

All assertions express the intended repaired behavior. Baseline failures must
remain failures; no xfail, altered expected volumes, or invented human approval.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest
from shapely.geometry import Polygon
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


from dwg_semantic.live_repair import LiveRepairError, build_live_repair_plan
from dwg_semantic.pipeline import WorkflowError, live_repair_apply_job, live_repair_plan_job


IDENTITY = {"origin_mm":[0.0,0.0,0.0], "xaxis":[1.0,0.0,0.0],
            "yaxis":[0.0,1.0,0.0], "zaxis":[0.0,0.0,1.0]}
WALL = [[0,0],[4000,0],[4000,200],[0,200]]


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "synthetic-source.dwg"
    path.write_bytes(b"Synthetic source fingerprint only; never passed to CAD.")
    return {"source_path":str(path), "source_sha256":hashlib.sha256(path.read_bytes()).hexdigest(),
            "units":"mm", "input_units":"mm", "scale_to_mm":1.0,
            "dependencies":[], "blockers":[]}


def entity_id(source, handle, path=""):
    return f"{source['source_sha256'][:16]}:{path}:{handle}"


def poly(source, handle, ring, *, path="", layer="A-WALL"):
    return {"entity_id":entity_id(source,handle,path), "source_handle":handle, "handle":handle,
            "type":"LWPOLYLINE", "source_file":source["source_path"], "layer":layer,
            "original_layer":layer, "effective_layer":layer, "instance_path":path,
            "owner_block":"*Model_Space" if not path else "BLOCK", "root_space":"*Model_Space",
            "closed":True, "vertices_world":ring}


def document(source, *, polygons=None, door=True, block_layer="A-DOOR"):
    entities = [poly(source,"W1",WALL)] if polygons is None else list(polygons)
    if door:
        entities.append({"entity_id":entity_id(source,"D1"),"handle":"D1","source_handle":"D1",
                         "type":"INSERT", "source_file":source["source_path"], "layer":block_layer,
                         "original_layer":block_layer,"effective_layer":block_layer,
                         "owner_block":"*Model_Space","instance_path":""})
    return {"intake":copy.deepcopy(source), "entities":entities, "resolved_instances":[]}


def selection(source, handles=None, *, layer="A-DOOR"):
    handles = handles or ["D1"]
    return {"success":True,"status":"SELECTED","selection_source":"PickfirstSelectionSet",
            "observed_at_utc":"2026-10-21T00:00:00Z", "selection_count":len(handles),
            "returned_count":len(handles),"truncated":False,
            "document":{"path":source["source_path"]},
            "objects":[{"handle":h,"selection_index":i,"properties":{"Layer":layer},
                        "bounding_box":{"coordinate_system":"WCS","minimum":[500,-100,0],
                                        "maximum":[1600,300,0]}}
                       for i,h in enumerate(handles)]}


def live_entity(source, *, category="architectural_wall", identity="wall-1", persistent_id="entity:101"):
    footprint={"outer":copy.deepcopy(WALL),"holes":[]}
    provenance={"id":identity,"name":identity,"category":category,"mode":"solid_extrusion",
                "source_layer":"A-WALL","source_layers":["A-WALL"],"tag":"A-WALL",
                "source_entity_ids":[entity_id(source,"W1")],"source_handles":["W1"],
                "outer":copy.deepcopy(WALL),"holes":[],"base_z_mm":0.0,"height_mm":3000.0}
    return {"entity_id":persistent_id,"parent_entity_id":"entity:20","group_id":identity,
            "name":identity,"category":category,"tag":"A-WALL","material":"DWG white model",
            "hidden":False,"locked":False,"manifold":True,"volume_mm3":4000*200*3000,
            "base_z_mm":0.0,"height_mm":3000.0,"footprint":footprint,
            "signature":"synthetic-real-geometry-signature-"+identity,"provenance":provenance,
            "raw_geometry_tags":["Untagged"],"world_transform":copy.deepcopy(IDENTITY)}


def snapshot(source, *, entities=None, selected=None):
    return {"schema":"dwg-skp-live-snapshot-v1","status":"ready",
            "model":{"guid":"synthetic-model-guid","path":"D:/audit-only/synthetic.skp"},
            "root":{"entity_id":"entity:10","schema":"dwg-skp-model-v2",
                    "payload_sha256":"synthetic-payload-sha","live_geometry_state":None,
                    "source_manifest":copy.deepcopy(source), "world_transform":copy.deepcopy(IDENTITY)},
            "containers":[{"entity_id":"entity:20","parent_entity_id":"entity:10",
                           "name":"architectural_wall","world_transform":copy.deepcopy(IDENTITY)}],
            "entities":[live_entity(source)] if entities is None else entities,
            "selection_entity_ids":selected or [],"references":[]}


def plan_opening(source, *, live=None, picked=None):
    return build_live_repair_plan(document(source),picked or selection(source),live or snapshot(source),
                                  action="cut_opening",height_mm=2100,base_z_mm=0,opening_type="door")


def plan_add(source, *, doc=None, picked=None, live=None):
    doc = doc or document(source,polygons=[poly(source,"P1",[[5000,0],[6000,0],[6000,200],[5000,200]])],door=False)
    picked = picked or selection(source,["P1"],layer="A-WALL")
    live = live or snapshot(source,entities=[])
    return build_live_repair_plan(doc,picked,live,action="add_missing",
                                  category="architectural_wall",height_mm=3000,base_z_mm=0)


def run_ruby(request):
    """Execute production Ruby in a fresh standalone process, never the live app."""
    install=Path(os.environ.get("DWG_AUDIT_RUBY_HOME","C:/Program Files/SketchUp/SketchUp 2021"))
    if not list(install.glob("*ruby*.dll")):
        pytest.skip("Environment layer R unavailable: isolated installed Ruby DLL not found")
    env=dict(os.environ)
    env.update(DWG_AUDIT_REQUEST=json.dumps(request,ensure_ascii=False,separators=(",",":")),
               DWG_AUDIT_BRIDGE=os.environ.get("DWG_TEST_BRIDGE",str(ROOT/"skills/dwg-layered-skp/scripts/sketchup/dwg_live_repair_bridge.rb")),
               PYTHONIOENCODING="utf-8")
    completed=subprocess.run([sys.executable,str(HERE/"embedded_ruby.py"),str(HERE/"ruby_bridge_adapter.rb")],
                             cwd=HERE,env=env,capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=20)
    assert completed.returncode==0, f"Ruby adapter failed before assertion: {completed.stdout}\n{completed.stderr}"
    return json.loads(completed.stdout.strip().splitlines()[-1])


# Positive controls stop unrelated fixture rejection from hiding negative cases.
def test_positive_matching_model_and_exact_plan_are_accepted(source):
    opening=plan_opening(source)
    addition=plan_add(source)
    assert opening["status"]==addition["status"]=="ready_to_apply"
    assert len(opening["targets"])==1 and len(opening["create"])==3
    assert len(addition["create"])==1


def test_positive_ruby_clean_plan_commits_one_undo_without_saving(source):
    live=snapshot(source,entities=[])
    result=run_ruby({"snapshot":live,"plan":plan_add(source,live=live)})
    assert result["accepted"] is True, result
    assert result["operations"]==["start","commit"],result
    assert result["receipt"]["model_saved"] is False
    assert result["receipt"]["new_skp_created"] is False
    assert result["selection_entity_ids"] == [row["entity_id"] for row in result["receipt"]["created"]]


def test_positive_ruby_clean_created_readback_is_accepted(source):
    live=snapshot(source,entities=[])
    result=run_ruby({"mode":"readback","snapshot":live,"plan":plan_add(source,live=live)})
    assert result["accepted"] is True,result


# N01: the active model must be from the same source, not just the same UI path.
def test_n01_wrong_source_model_rejected_before_plan(source):
    live=snapshot(source)
    live["root"]["source_manifest"]["source_sha256"]="unrelated-source-sha"
    with pytest.raises((LiveRepairError,ValueError)):
        plan_opening(source,live=live)


def test_n01_missing_source_manifest_rejected_before_plan(source):
    live=snapshot(source)
    live["root"].pop("source_manifest")
    with pytest.raises((LiveRepairError,ValueError)):
        plan_opening(source,live=live)


def test_n01_changed_dwg_source_blocks_plan_job(source,tmp_path):
    job=tmp_path/"isolated-job";job.mkdir()
    (job/"document.json").write_text(json.dumps(document(source)),encoding="utf-8")
    selected=tmp_path/"selected.json"
    selected.write_text(json.dumps(selection(source)),encoding="utf-8")
    Path(source["source_path"]).write_bytes(b"Changed synthetic CAD source after normalization")
    output=tmp_path/"must-not-exist.json"
    with patch("dwg_semantic.sketchup_live.inspect_active_white_model",return_value=snapshot(source)):
        with pytest.raises((LiveRepairError,WorkflowError,ValueError)):
            live_repair_plan_job(job,output,action="cut_opening",selection_path=selected,height_mm=2100)
    assert not output.exists(),"stale source cannot produce an applicable repair plan"


def test_n01_changed_source_blocks_apply_before_bridge_call(source,tmp_path):
    plan=plan_opening(source)
    path=tmp_path/"source-bound-plan.json"
    path.write_text(json.dumps(plan),encoding="utf-8")
    Path(source["source_path"]).write_bytes(b"Changed synthetic source after plan creation")
    with patch("dwg_semantic.sketchup_live._call",return_value={"status":"applied"}) as remote:
        with pytest.raises((LiveRepairError,WorkflowError,ValueError)):
            live_repair_apply_job(path,confirmation_sha256=plan["plan_sha256"])
        remote.assert_not_called()


# N02: targetless plans must be invalidated by root/parent/context drift too.
@pytest.mark.parametrize("drift",["root_transform","parent_transform","live_state","new_context_entity","provenance"])
def test_n02_bridge_rejects_context_drift_before_undo(source,drift):
    before=snapshot(source,entities=[])
    plan=plan_add(source,live=before)
    after=copy.deepcopy(before)
    if drift=="root_transform":
        after["root"]["world_transform"]["origin_mm"][0]=100.0
    elif drift=="parent_transform":
        after["containers"][0]["world_transform"]["origin_mm"][0]=100.0
    elif drift=="live_state":
        after["root"]["live_geometry_state"]="another-live-repair-hash"
    elif drift=="new_context_entity":
        other=live_entity(source,identity="inserted-after-plan",persistent_id="entity:777")
        other["footprint"]={"outer":plan["create"][0]["footprint"]["outer"],"holes":[]}
        after["entities"].append(other)
    else:
        after["root"]["source_manifest"]["source_path"]="D:/foreign/source.dwg"
    result=run_ruby({"snapshot":after,"plan":plan})
    assert result["accepted"] is False, f"stale {drift} plan was applied: {result}"
    assert result["operations"]==[], f"drift must be rejected before start_operation: {result}"


# N03: preflight the receipt path before making an irreversible remote call.
def test_n03_existing_receipt_does_not_call_apply(source,tmp_path):
    path=tmp_path/"synthetic-plan.json"
    path.write_text(json.dumps(plan_opening(source)),encoding="utf-8")
    receipt=tmp_path/"already-exists.json"
    receipt.write_text("Original evidence must remain unchanged",encoding="utf-8")
    with patch("dwg_semantic.sketchup_live.apply_live_repair_plan",return_value={"status":"applied"}) as apply:
        with pytest.raises((WorkflowError,ValueError,OSError)):
            live_repair_apply_job(path,confirmation_sha256="unused-preflight",receipt_path=receipt)
        apply.assert_not_called()
    assert receipt.read_text(encoding="utf-8")=="Original evidence must remain unchanged"


# N04: nested boundaries denote wall material and voids, never solid room fill.
def test_n04_selected_nested_boundaries_preserve_hole(source):
    doc=document(source,polygons=[],block_layer="A-WALL")
    doc["resolved_instances"]=[poly(source,"OUT",[[0,0],[2000,0],[2000,2000],[0,2000]],path="D1"),
                               poly(source,"IN",[[200,200],[1800,200],[1800,1800],[200,1800]],path="D1")]
    plan=plan_add(source,doc=doc,picked=selection(source,layer="A-WALL"))
    expected=Polygon([[0,0],[2000,0],[2000,2000],[0,2000]],holes=[[[200,200],[1800,200],[1800,1800],[200,1800]]])
    actual=unary_union([Polygon(row["footprint"]["outer"],row["footprint"].get("holes",[])) for row in plan["create"]])
    assert actual.symmetric_difference(expected).area < 0.01
    assert sum(row["mesh"]["expected_volume_mm3"] for row in plan["create"])==pytest.approx(expected.area*3000)


# N05: XY projection cannot be manufactured by dropping source Z.
@pytest.mark.parametrize("shape",["nonplanar","mixed_plane_network"])
def test_n05_nonplanar_or_mixed_z_selection_blocks(source,shape):
    if shape=="nonplanar":
        doc=document(source,polygons=[poly(source,"P1",[[0,0,0],[1000,0,100],[1000,200,0],[0,200,0]])],door=False)
        picked=selection(source,["P1"],layer="A-WALL")
    else:
        ring=[[0,0,0],[1000,0,0],[1000,200,100],[0,200,100],[0,0,0]]
        lines=[{"entity_id":entity_id(source,str(i)),"handle":str(i),"source_handle":str(i),
                "type":"LINE","source_file":source["source_path"],"layer":"A-WALL","original_layer":"A-WALL",
                "effective_layer":"A-WALL","instance_path":"","owner_block":"*Model_Space",
                "start_world":a,"end_world":b} for i,(a,b) in enumerate(zip(ring,ring[1:]))]
        doc=document(source,polygons=lines,door=False)
        picked=selection(source,["0","1","2","3"],layer="A-WALL")
    with pytest.raises((LiveRepairError,ValueError)):
        plan_add(source,doc=doc,picked=picked)


# N06: matching reported counts are insufficient without the actual objects.
@pytest.mark.parametrize("mutation",["wrong_length","missing_counts","duplicate_handles"])
def test_n06_incomplete_pickfirst_snapshot_rejected(source,mutation):
    picked=selection(source)
    if mutation=="wrong_length":
        picked.update(selection_count=999,returned_count=999)
    elif mutation=="missing_counts":
        picked.pop("selection_count");picked.pop("returned_count")
    else:
        picked["objects"].append(copy.deepcopy(picked["objects"][0]))
        picked.update(selection_count=2,returned_count=2)
    with pytest.raises((LiveRepairError,ValueError)):
        plan_opening(source,picked=picked)


# N07: a fully selected compatible line network is valid exact geometry.
def test_n07_shared_selected_line_network_closes_with_all_sources(source):
    ring=[[0,0,0],[1000,0,0],[1000,200,0],[0,200,0],[0,0,0]]
    lines=[{"entity_id":entity_id(source,str(i)),"handle":str(i),"source_handle":str(i),"type":"LINE",
            "source_file":source["source_path"],"layer":"A-WALL","original_layer":"A-WALL",
            "effective_layer":"A-WALL","instance_path":"","owner_block":"*Model_Space",
            "start_world":a,"end_world":b} for i,(a,b) in enumerate(zip(ring,ring[1:]))]
    plan=plan_add(source,doc=document(source,polygons=lines,door=False),
                  picked=selection(source,["0","1","2","3"],layer="A-WALL"))
    assert len(plan["create"])==1
    assert plan["create"][0]["mesh"]["expected_volume_mm3"]==pytest.approx(1000*200*3000)
    assert set(plan["create"][0]["provenance"]["source_entity_ids"])=={entity_id(source,str(i)) for i in range(4)}


# N08: reject overlapping outputs or safely consolidate before plan approval.
def test_n08_new_solids_cannot_have_positive_overlap(source):
    polygons=[poly(source,"P1",[[0,0],[1000,0],[1000,200],[0,200]]),
              poly(source,"P2",[[500,0],[1500,0],[1500,200],[500,200]])]
    try:
        plan=plan_add(source,doc=document(source,polygons=polygons,door=False),
                      picked=selection(source,["P1","P2"],layer="A-WALL"))
    except (LiveRepairError,ValueError):
        return
    footprints=[Polygon(row["footprint"]["outer"],row["footprint"].get("holes",[])) for row in plan["create"]]
    for i,first in enumerate(footprints):
        for second in footprints[i+1:]:
            assert first.intersection(second).area<0.1, "new plan solids overlap each other"
    assert unary_union(footprints).area==pytest.approx(1500*200)


# N09: remove_wrong cannot silently expand the allowed edit surface.
def test_n09_unsupported_slab_remove_rejected(source):
    live=snapshot(source,entities=[live_entity(source,category="slab")],selected=["entity:101"])
    with pytest.raises((LiveRepairError,ValueError)):
        build_live_repair_plan(document(source),None,live,action="remove_wrong")


# N10: a neutral insert layer never overrides an explicit child source layer.
def test_n10_explicit_child_layer_survives_live_add(source):
    doc=document(source,polygons=[],block_layer="A-ASSEMBLY")
    child=poly(source,"LEAF",[[0,0],[1000,0],[1000,200],[0,200]],path="D1")
    doc["resolved_instances"]=[child]
    plan=plan_add(source,doc=doc,picked=selection(source,layer="A-ASSEMBLY"))
    row=plan["create"][0]
    assert row["tag"]=="A-WALL"
    assert "A-WALL" in row["provenance"]["source_layers"]
    assert entity_id(source,"LEAF","D1") in row["provenance"]["source_entity_ids"]


# N11: do not reconstruct an arbitrary edited shell from stale stored metadata.
@pytest.mark.parametrize("mutation",["nonmanifold","missing_volume","inconsistent_volume"])
def test_n11_invalid_active_prism_rejected(source,mutation):
    live=snapshot(source)
    if mutation=="nonmanifold": live["entities"][0]["manifold"]=False
    elif mutation=="missing_volume": live["entities"][0]["volume_mm3"]=None
    else: live["entities"][0]["volume_mm3"]*=0.8
    with pytest.raises((LiveRepairError,ValueError)):
        plan_opening(source,live=live)


# N12: source reference edges remain inspectable but never become solid targets.
def test_n12_ruby_inspector_accepts_reference_edges_without_promoting_them(source):
    live=snapshot(source,entities=[])
    result=run_ruby({"mode":"inspect","snapshot":live,"plan":plan_add(source,live=live),"reference_fixture":True})
    assert result["accepted"] is True, result
    inspection=result["inspection"]
    assert "reference:301" not in {row.get("group_id") for row in inspection["entities"]}
    refs=inspection.get("references",[])
    assert refs, "source reference provenance must remain visible in the snapshot"


# N13: actual C API readback must detect a missing assigned white material.
def test_n13_native_missing_material_fails_actual_readback(source,tmp_path):
    from dwg_semantic.modeling import write_model
    from dwg_semantic.skp_native import Native, discover_library
    try:
        discover_library()
    except FileNotFoundError:
        pytest.skip("Environment layer C unavailable: SketchUp C API not installed")
    group=copy.deepcopy(live_entity(source)["provenance"])
    payload={"schema":"dwg-skp-model-v2","units":"mm","coordinate_policy":"Synthetic audit fixture mm",
             "source_manifest":copy.deepcopy(source),"groups":[group],"references":[],"warnings":[],"blockers":[],
             "tag_mapping":[{"source_layer":"A-WALL","tag":"A-WALL"}]}
    original=Native.group
    def omit_material(self,parent,name,layer=None,material=None,data=None):
        return original(self,parent,name,layer=layer,material=None,data=data)
    with patch.object(Native,"group",omit_material):
        with pytest.raises((RuntimeError,ValueError),match="(?i)material|材質"):
            write_model(payload,tmp_path/"synthetic-missing-material.skp")


# N14: volume/id alone cannot certify metadata, tags, or a wrong-position mesh.
@pytest.mark.parametrize("mutation",["tag","provenance","raw_tag","offset"])
def test_n14_ruby_actual_readback_rejects_mismatched_created_geometry(source,mutation):
    live=snapshot(source,entities=[])
    result=run_ruby({"mode":"readback","snapshot":live,"plan":plan_add(source,live=live),"mutation":mutation})
    assert result["accepted"] is False, f"created {mutation} mismatch survived readback: {result}"


def test_n14_ruby_creation_failure_aborts_exactly_one_undo(source):
    live=snapshot(source,entities=[])
    result=run_ruby({"snapshot":live,"plan":plan_add(source,live=live),"inject_create_failure":True})
    assert result["accepted"] is False, result
    assert result["operations"]==["start","abort"], result


# Numeric canonicalization must survive Python -> JSON -> Ruby for valid input.
def test_n15_python_ruby_plan_digest_accepts_small_decimal_coordinates(source):
    doc=document(source,polygons=[poly(source,"P1",[[0.000001,0],[1000.000001,0],
                                                            [1000.000001,200],[0.000001,200]])],door=False)
    live=snapshot(source,entities=[])
    plan=plan_add(source,doc=doc,live=live)
    result=run_ruby({"mode":"digest","snapshot":live,"plan":plan})
    assert result["accepted"] is True, f"valid cross-language digest rejected: {result}"
    assert result["digest"]==plan["plan_sha256"]


