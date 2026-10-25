"""Fixed policy checks on synthetic live-model snapshots; no app is contacted."""
from __future__ import annotations

import copy

import pytest
from shapely.geometry import Polygon

from dwg_semantic.live_repair import LiveRepairError, _mesh, build_live_repair_plan
from dwg_semantic.modeling_standard import FIXED
from test_live_repair import document, selection, snapshot


def roof(*, cap=3200, x0=0, x1=4000, holes=None, identity="roof:1"):
    polygon=Polygon([[x0,-1000],[x1,-1000],[x1,1000],[x0,1000]],holes or [])
    mesh=_mesh(polygon,cap,200)
    triangles=[]
    for face in mesh["faces"]:
        for i in range(1,len(face)-1):
            triangles.append([mesh["vertices"][face[0]],mesh["vertices"][face[i]],mesh["vertices"][face[i+1]]])
    return {"entity_id":identity,"group_id":identity,"parent_entity_id":"entity:30","category":"slab",
            "manifold":True,"triangles_mm":triangles,"signature":"actual-roof-signature-"+identity,
            "provenance":{"id":identity,"category":"slab","mode":"boundary_mesh","slab_role":"overhead_slab"}}


def fixed_snapshot(*, entities=None, selected=None, roofs=None):
    live=snapshot(entities=entities,selection=selected)
    if entities is None:
        for row in live['entities']:
            row['height_mm']=3200
            row['volume_mm3']=Polygon(row['footprint']['outer'],row['footprint'].get('holes',[])).area*3200
            row['provenance']['height_mm']=3200
    live["root"].update(modeling_standard=copy.deepcopy(FIXED),fixed_standard_audit={
        "standard":copy.deepcopy(FIXED),"all_pass":True,"issues":[],"fire_body_source_entity_ids":[]})
    live["protected_entities"]=[roof()] if roofs is None else roofs
    return live


def add_plan(live=None, *, height=1000, doc=None, category="architectural_wall"):
    return build_live_repair_plan(doc or document(exact_door=True),selection(),
        live or fixed_snapshot(entities=[]),action="add_missing",category=category,height_mm=height)


@pytest.mark.parametrize("height",[None,1000,5000])
def test_fixed_add_uses_actual_underside_not_caller_height(height):
    plan=add_plan(height=height)
    assert len(plan["create"])==1
    assert plan["create"][0]["height_mm"]==3200
    assert plan["modeling_standard"]==FIXED
    assert plan["create"][0]["provenance"]["fixed_live_height_audit"]["cap_z_mm"]==3200


def test_fixed_replace_preserves_base_and_correct_xy_but_fits_actual_roof():
    live=fixed_snapshot(selected=["entity:101"])
    plan=build_live_repair_plan(document(exact_door=True),selection(),live,action="replace_from_cad",height_mm=900,base_z_mm=100)
    row=plan["create"][0]
    assert row["base_z_mm"]==100 and row["height_mm"]==3100
    assert Polygon(row["footprint"]["outer"]).area==1100*200


def test_fixed_add_splits_at_actual_local_underside_steps():
    live=fixed_snapshot(entities=[],roofs=[roof(cap=3000,x1=1000),roof(cap=3200,x0=1000,identity="roof:2")])
    plan=add_plan(live)
    assert {row["height_mm"] for row in plan["create"]}=={3000,3200}
    assert sum(Polygon(row["footprint"]["outer"]).area for row in plan["create"])==1100*200
    assert len({row["group_id"] for row in plan["create"]})==2


@pytest.mark.parametrize("roofs",[[],[roof(x1=1000)],
    [roof(holes=[[[400,-100],[1700,-100],[1700,300],[400,300]]])]])
def test_fixed_live_does_not_guess_height_over_missing_or_perforated_roof(roofs):
    with pytest.raises(LiveRepairError,match="板底|板孔"):
        add_plan(fixed_snapshot(entities=[],roofs=roofs))


def test_fixed_policy_cannot_be_downgraded_by_a_missing_or_altered_audit():
    live=fixed_snapshot(entities=[])
    live["root"].pop("fixed_standard_audit")
    with pytest.raises(LiveRepairError,match="規範"):
        add_plan(live)
    live=fixed_snapshot(entities=[])
    live["root"]["modeling_standard"]["verticals_to_slab_underside"]=False
    with pytest.raises(LiveRepairError,match="規範"):
        add_plan(live)


def test_fixed_fire_body_is_rejected_regardless_of_caller_wall_category():
    doc=document(exact_door=True)
    doc["entities"][0].update(layer="DOOR_FIRE",original_layer="DOOR_FIRE",effective_layer="DOOR_FIRE")
    doc["resolved_instances"][0].update(layer="DOOR_FIRE",original_layer="DOOR_FIRE",effective_layer="DOOR_FIRE")
    with pytest.raises(LiveRepairError,match="防火門|捲簾"):
        add_plan(doc=doc)


def test_fixed_fire_source_remains_usable_as_opening_evidence_with_clear_height():
    doc=document()
    doc["entities"][0].update(layer="DOOR_FIRE",original_layer="DOOR_FIRE",effective_layer="DOOR_FIRE")
    live=fixed_snapshot()
    live["root"]["fixed_standard_audit"]["fire_body_source_entity_ids"]=["source::D1"]
    plan=build_live_repair_plan(doc,selection(),live,action="cut_opening",height_mm=2100,base_z_mm=0)
    assert plan["source"]["items"][0]["height_mm"]==2100
    assert len(plan["create"])==3
    assert min(row["base_z_mm"] for row in plan["create"])==0


def test_fixed_unique_existing_door_lintel_can_be_replaced_without_recreating_door_body():
    doc=document(exact_door=True)
    doc["entities"][0].update(layer="DOOR_FIRE",original_layer="DOOR_FIRE",effective_layer="DOOR_FIRE")
    doc["resolved_instances"][0].update(layer="DOOR_FIRE",original_layer="DOOR_FIRE",effective_layer="DOOR_FIRE")
    live=fixed_snapshot(selected=["entity:101"])
    target=live["entities"][0]
    target.update(base_z_mm=2100,height_mm=1100,volume_mm3=4000*200*1100)
    target["provenance"].update(base_z_mm=2100,height_mm=1100,semantic_role="door_lintel")
    plan=build_live_repair_plan(doc,selection(),live,action="replace_from_cad",height_mm=900)
    assert len(plan["targets"])==1
    assert plan["create"][0]["base_z_mm"]==2100 and plan["create"][0]["height_mm"]==1100
    assert plan["create"][0]["provenance"]["semantic_role"]=="door_lintel"


def test_fixed_remove_can_remove_wrong_entity_without_roof_inference():
    live=fixed_snapshot(selected=["entity:101"],roofs=[])
    plan=build_live_repair_plan(document(),None,live,action="remove_wrong")
    assert len(plan["targets"])==1 and not plan["create"]


def test_legacy_model_without_fixed_marker_keeps_its_explicit_repair_height():
    plan=add_plan(snapshot(entities=[]),height=2400)
    assert plan["modeling_standard"] is None
    assert plan["create"][0]["height_mm"]==2400


def fixed_window_snapshot():
    doc=document(exact_door=True)
    picked=selection()
    initial=fixed_snapshot()
    opened=build_live_repair_plan(doc,picked,initial,action="cut_opening",height_mm=1200,
                                  base_z_mm=900,opening_type="window")
    entities=[]
    sill_id=None
    for i,row in enumerate(opened['create']):
        identity='entity:'+str(500+i)
        entity={'entity_id':identity,'parent_entity_id':row['parent_entity_id'],'group_id':row['group_id'],
                'name':row['name'],'category':row['category'],'tag':row['tag'],'material':row['material'],
                'hidden':False,'locked':False,'manifold':True,'volume_mm3':row['mesh']['expected_volume_mm3'],
                'base_z_mm':row['base_z_mm'],'height_mm':row['height_mm'],'footprint':copy.deepcopy(row['footprint']),
                'signature':'actual-window-part-'+str(i),'provenance':copy.deepcopy(row['provenance'])}
        entities.append(entity)
        if entity['provenance'].get('opening_part_role')=='sill':sill_id=identity
    assert sill_id is not None,'Normal opening modeling must explicitly record sill provenance'
    live=fixed_snapshot(entities=entities,selected=[sill_id])
    return doc,picked,live,opened


def test_fixed_window_cut_preserves_sill_clearance_and_lintel_intervals():
    _,_,_,opened=fixed_window_snapshot()
    roles={row['provenance'].get('opening_part_role'):(row['base_z_mm'],row['base_z_mm']+row['height_mm'])
           for row in opened['create']}
    assert roles['sill']==(0,900)
    assert roles['lintel']==(2100,3200)
    assert opened['source']['items'][0]['base_z_mm']==900
    assert opened['source']['items'][0]['height_mm']==1200


@pytest.mark.parametrize('height',[None,100,5000])
def test_fixed_actual_window_sill_replace_never_refills_the_opening(height):
    doc,picked,live,_=fixed_window_snapshot()
    plan=build_live_repair_plan(doc,picked,live,action='replace_from_cad',height_mm=height)
    assert len(plan['targets'])==1 and len(plan['create'])==1
    row=plan['create'][0]
    assert (row['base_z_mm'],row['height_mm'])==(0,900)
    assert row['provenance']['opening_part_role']=='sill'
    assert row['provenance']['opening_ids']
    assert row['provenance']['fixed_live_height_audit']['basis']=='preserved_actual_source_bound_sill_top'


def test_a_role_name_without_real_opening_provenance_cannot_waive_the_fixed_cap():
    live=fixed_snapshot(selected=['entity:101'])
    live['entities'][0]['provenance']['opening_part_role']='sill'
    live['entities'][0]['provenance'].pop('opening_ids',None)
    with pytest.raises(LiveRepairError,match='sill|洞下段'):
        build_live_repair_plan(document(exact_door=True),selection(),live,action='replace_from_cad',height_mm=900)
