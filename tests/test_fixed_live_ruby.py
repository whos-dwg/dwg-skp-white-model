"""Production Ruby policy preflight on a pure synthetic SketchUp adapter.

No active app or bridge listener is contacted. Source geometry is represented
by actual triangle fixture arrays, never stored construction-cell recipes.
"""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import sys

import pytest
from shapely.geometry import Polygon

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests'))
sys.path.insert(0,str(ROOT/'tests/native_safety'))

from test_fixed_live_standard import fixed_snapshot,add_plan,roof,fixed_window_snapshot
from test_native_safety_contract import run_ruby
from test_native_safety_followup import rehash
from dwg_semantic.live_repair import _mesh
from dwg_semantic.live_repair import build_live_repair_plan
from test_live_repair import document,selection


def case(tmp_path,roofs=None):
    source=tmp_path/'synthetic-source.dwg'
    source.write_bytes(b'Synthetic source hash fixture; never read by CAD.')
    doc=document(exact_door=True)
    doc['intake'].update(source_path=str(source),source_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
    for row in doc['entities']+doc['resolved_instances']:row['source_file']=str(source)
    picked=selection();picked['document']['path']=str(source)
    live=fixed_snapshot(entities=[],roofs=roofs)
    live['root']['source_manifest']=copy.deepcopy(doc['intake'])
    plan=build_live_repair_plan(doc,picked,live,action='add_missing',category='architectural_wall',height_mm=1000)
    return live,plan


@pytest.mark.parametrize('split',[False,True])
def test_ruby_fixed_live_valid_actual_local_caps_commit_once(split,tmp_path):
    live,plan=case(tmp_path,roofs=([roof(cap=3000,x1=1000),roof(cap=3200,x0=1000,identity='roof:2')] if split else None))
    result=run_ruby({'snapshot':live,'plan':plan,'production_create':True})
    assert result['accepted'] is True,result
    assert result['operations']==['start','commit'],result


@pytest.mark.parametrize('height',[900,5000])
def test_ruby_recomputed_hash_cannot_override_fixed_actual_underside(height,tmp_path):
    live,plan=case(tmp_path)
    row=plan['create'][0]
    row['height_mm']=height
    row['provenance']['height_mm']=height
    row['mesh']=_mesh(Polygon(row['footprint']['outer'],row['footprint'].get('holes',[])),row['base_z_mm'],height)
    result=run_ruby({'snapshot':live,'plan':rehash(plan)})
    assert result['accepted'] is False,result
    assert result['operations']==[],result
    assert result['code'] in {'FIXED_CAP_UNCOVERED','FIXED_CAP_MISMATCH'},result


def test_ruby_fixed_marker_cannot_be_removed_from_plan_while_root_remains_fixed(tmp_path):
    live,plan=case(tmp_path)
    plan['modeling_standard']=None;plan['fixed_standard_audit']=None
    result=run_ruby({'snapshot':live,'plan':rehash(plan)})
    assert result['accepted'] is False and result['operations']==[],result
    assert result['code']=='FIXED_STANDARD_MISMATCH',result


def test_ruby_actual_mesh_hole_blocks_even_when_stored_fixed_audit_is_pass(tmp_path):
    live,plan=case(tmp_path)
    live['protected_entities']=[roof(holes=[[[400,-100],[1700,-100],[1700,300],[400,300]]])]
    plan['model']['snapshot_context']['protected_entities']=copy.deepcopy(live['protected_entities'])
    result=run_ruby({'snapshot':live,'plan':rehash(plan)})
    assert result['accepted'] is False and result['operations']==[],result
    assert result['code']=='FIXED_CAP_UNCOVERED',result


@pytest.mark.parametrize('claim_lintel',[False,True])
def test_ruby_fire_body_cannot_be_created_with_a_forged_source_consistent_plan(tmp_path,claim_lintel):
    live,plan=case(tmp_path)
    fire_id=plan['create'][0]['provenance']['source_entity_ids'][0]
    live['root']['fixed_standard_audit']['fire_body_source_entity_ids']=[fire_id]
    plan['fixed_standard_audit']=copy.deepcopy(live['root']['fixed_standard_audit'])
    plan['model']['snapshot_context']['root']=copy.deepcopy(live['root'])
    if claim_lintel:
        plan['create'][0]['provenance'].update(semantic_role='door_lintel',live_repair_replaced_group_ids=['not-an-actual-target'])
    result=run_ruby({'snapshot':live,'plan':rehash(plan)})
    assert result['accepted'] is False and result['operations']==[],result
    assert result['code']=='FIXED_FIRE_BODY_FORBIDDEN',result


def test_ruby_unique_actual_fire_door_lintel_can_be_replaced_at_its_original_base(tmp_path):
    initial,_=case(tmp_path)
    doc=document(exact_door=True)
    doc['intake']=copy.deepcopy(initial['root']['source_manifest'])
    for row in doc['entities']+doc['resolved_instances']:
        row.update(source_file=doc['intake']['source_path'],layer='DOOR_FIRE',original_layer='DOOR_FIRE',effective_layer='DOOR_FIRE')
    picked=selection();picked['document']['path']=doc['intake']['source_path']
    live=fixed_snapshot(selected=['entity:101'])
    live['root']['source_manifest']=copy.deepcopy(doc['intake'])
    live['root']['fixed_standard_audit']['fire_body_source_entity_ids']=['source:D1:LEAF']
    target=live['entities'][0]
    target.update(base_z_mm=2100,height_mm=1100,volume_mm3=4000*200*1100)
    target['provenance'].update(base_z_mm=2100,height_mm=1100,semantic_role='door_lintel')
    plan=build_live_repair_plan(doc,picked,live,action='replace_from_cad',height_mm=900)
    result=run_ruby({'snapshot':live,'plan':plan,'production_create':True})
    assert result['accepted'] is True and result['operations']==['start','commit'],result


def window_case(tmp_path):
    initial,_=case(tmp_path)
    doc,picked,live,_=fixed_window_snapshot()
    doc['intake']=copy.deepcopy(initial['root']['source_manifest'])
    for row in doc['entities']+doc['resolved_instances']:row['source_file']=doc['intake']['source_path']
    picked['document']['path']=doc['intake']['source_path']
    live['root']['source_manifest']=copy.deepcopy(doc['intake'])
    return doc,picked,live


def test_ruby_true_window_sill_replacement_preserves_900_2100_3200_geometry(tmp_path):
    doc,picked,live=window_case(tmp_path)
    plan=build_live_repair_plan(doc,picked,live,action='replace_from_cad',height_mm=5000)
    result=run_ruby({'snapshot':live,'plan':plan,'production_create':True})
    assert result['accepted'] is True and result['operations']==['start','commit'],result
    assert plan['create'][0]['height_mm']==900
    assert any(row['base_z_mm']==2100 and row['height_mm']==1100 for row in live['entities'])


def test_ruby_fixed_window_cut_does_not_require_its_sill_to_reach_the_roof(tmp_path):
    initial,_=case(tmp_path)
    doc=document(exact_door=True);doc['intake']=copy.deepcopy(initial['root']['source_manifest'])
    for row in doc['entities']+doc['resolved_instances']:row['source_file']=doc['intake']['source_path']
    picked=selection();picked['document']['path']=doc['intake']['source_path']
    live=fixed_snapshot();live['root']['source_manifest']=copy.deepcopy(doc['intake'])
    plan=build_live_repair_plan(doc,picked,live,action='cut_opening',opening_type='window',height_mm=1200,base_z_mm=900)
    result=run_ruby({'snapshot':live,'plan':plan,'production_create':True})
    assert result['accepted'] is True and result['operations']==['start','commit'],result
    intervals={row['provenance'].get('opening_part_role'):(row['base_z_mm'],row['base_z_mm']+row['height_mm'])
               for row in plan['create']}
    assert intervals['sill']==(0,900) and intervals['lintel']==(2100,3200)


@pytest.mark.parametrize('mutation',['raise_sill','claim_sill_on_add'])
def test_ruby_sill_exception_cannot_be_self_claimed_or_raise_the_actual_window_bottom(tmp_path,mutation):
    if mutation=='raise_sill':
        doc,picked,live=window_case(tmp_path)
        plan=build_live_repair_plan(doc,picked,live,action='replace_from_cad')
        row=plan['create'][0];row['height_mm']=3200;row['provenance']['height_mm']=3200
        row['mesh']=_mesh(Polygon(row['footprint']['outer'],row['footprint'].get('holes',[])),0,3200)
        expected='FIXED_SILL_CAP_CHANGED'
    else:
        live,plan=case(tmp_path)
        plan['create'][0]['provenance'].update(opening_part_role='sill',opening_ids=['invented-opening'])
        expected='FIXED_SILL_SCOPE'
    result=run_ruby({'snapshot':live,'plan':rehash(plan)})
    assert result['accepted'] is False and result['operations']==[],result
    assert result['code']==expected,result
