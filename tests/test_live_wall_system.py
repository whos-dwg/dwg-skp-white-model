"""Synthetic compound-wall contracts; never contacts either live application."""
import copy
import ctypes as C
import json
import os
from unittest.mock import patch

import pytest
from shapely.geometry import Polygon,box
from shapely.ops import unary_union

from dwg_semantic.live_repair import build_live_repair_plan,LiveRepairError,validate_plan_digest,_mesh
from dwg_semantic.live_cad_geometry import validate_current_selection
from dwg_semantic.live_wall_system import prepare_wall_system
from test_live_repair import document,live_entity,snapshot
from test_live_cad_geometry import cad
from test_fixed_live_standard import fixed_snapshot,roof


def poly(handle,p,closed=True):
    o=cad(list(p.exterior.coords)[:-1])['objects'][0]
    o['handle']=handle;o['properties']['Layer']='A-PARAPET'
    if not closed:
        o['geometry']['Closed']=False;o['geometry']['Coordinates']+=o['geometry']['Coordinates'][:2]
    return o


def line(handle,a,b,layer='A-PARAPET',z=0):
    return {'handle':handle,'object_type':'AcDbLine','properties':{'Layer':layer},
        'geometry':{'StartPoint':[*a,z],'EndPoint':[*b,z],'Length':((a[0]-b[0])**2+(a[1]-b[1])**2)**.5,'Normal':[0,0,1],'Thickness':0},
        'bounding_box':{'coordinate_system':'WCS','minimum':[min(a[0],b[0]),min(a[1],b[1]),z],
            'maximum':[max(a[0],b[0]),max(a[1],b[1]),z]}}


def fixture():
    outer=box(0,0,5000,1400);inners=[box(200,200,3000,1200),box(3200,200,4800,1200)]
    material=outer.difference(unary_union(inners));opening=box(500,0,1500,200)
    old=material.difference(box(0,600,200,800))
    body=old.difference(opening)
    rows=[]
    for i,p in enumerate(body.geoms):
        e=live_entity(f'wall-family/side/{i}',entity_id=f'entity:{101+i}',outer=list(p.exterior.coords)[:-1],height=3200)
        e['footprint']={'outer':list(p.exterior.coords)[:-1],'holes':[list(r.coords)[:-1]for r in p.interiors]}
        e['volume_mm3']=p.area*3200
        e['tag']='A-PARAPET';e['provenance'].update(tag='A-PARAPET',source_layer='A-PARAPET',source_layers=['A-PARAPET'],
            opening_part_role='side',opening_ids=['opening:synthetic'],material_partition_evidence={
                'source_candidate_id':'wall-family','original_footprint':{'outer':list(material.exterior.coords)[:-1],
                    'holes':[list(r.coords)[:-1]for r in material.interiors]}})
        rows.append(e)
    lintel=copy.deepcopy(rows[0]);lintel.update(entity_id='entity:201',group_id='wall-family/lintel',name='lintel',
        footprint={'outer':list(opening.exterior.coords)[:-1],'holes':[]},base_z_mm=2000,height_mm=1200,volume_mm3=opening.area*1200)
    lintel['provenance'].update(id='wall-family/lintel',base_z_mm=2000,height_mm=1200,opening_part_role='lintel')
    rows.append(lintel)
    picked=cad();picked['objects']=[poly('O',outer,False),poly('I1',inners[0]),poly('I2',inners[1]),
        line('J1',(500,0),(500,200)),line('J2',(1500,200),(1500,0)),
        line('E1',(500,0),(1500,0),'A-DOOR',200),line('E2',(500,200),(1500,200),'A-DOOR',200),
        {'handle':'S','object_type':'AcDbBlockReference','properties':{'Layer':'A-DOOR'},'geometry':{'InsertionPoint':[1000,100,0]}},
        {'handle':'C','object_type':'AcDbCircle','properties':{'Layer':'PIPE'},'geometry':{'Center':[900,900,0],'Radius':50}}]
    for i,o in enumerate(picked['objects']):o['selection_index']=i
    picked['selection_count']=picked['returned_count']=len(picked['objects'])
    scope={'schema':'cad-wall-system-scope-v1','wall_layer':'A-PARAPET','preserve_openings':True,
        'user_scope_quote':'Synthetic test scope only: wall system, not equipment.',
        'roles':{'O':'outer_boundary','I1':'inner_boundary','I2':'inner_boundary','J1':'door_jamb','J2':'door_jamb',
            'E1':'opening_edge','E2':'opening_edge','S':'opening_symbol','C':'unchanged_reference'},
        'openings':[{'jamb_handles':['J1','J2'],'edge_handles':['E1','E2'],'symbol_handle':'S'}]}
    return document(),picked,snapshot(rows),scope,material,opening


def plan(case=None):
    d,c,s,scope,*_=case or fixture()
    return build_live_repair_plan(d,c,s,action='replace_from_cad',live_cad_geometry=True,wall_system_scope=scope)


def test_complete_system_preserves_cavities_and_2000_clearance_rejoins_exposed_material():
    f=fixture();original=copy.deepcopy(f);p=plan(f)
    assert len(p['targets'])==3 and len(p['create'])==2
    assert validate_plan_digest(p)==p['plan_sha256']
    low=[Polygon(r['footprint']['outer'],r['footprint']['holes'])for r in p['create']if r['base_z_mm']==0]
    high=[Polygon(r['footprint']['outer'],r['footprint']['holes'])for r in p['create']]
    assert unary_union(low).symmetric_difference(f[4].difference(f[5])).area<.001
    assert unary_union(high).symmetric_difference(f[4]).area<.001
    assert not unary_union(low).intersection(f[5]).area
    assert {r['base_z_mm']+r['height_mm']for r in p['create']}=={3200}
    assert p['source']['preserved_opening_audit'][0]['clear_top_z_mm']==2000
    assert p['source']['preserved_opening_audit'][0]['source_2d_edge_z_mm']==200
    assert 'C'not in p['create'][0]['provenance']['source_handles']
    assert p['source']['live_cad_revision']['selection_snapshot']==f[1]
    assert all('material_partition_evidence'not in r['provenance']and r['provenance']['replaced_source_binding']for r in p['create'])
    assert f==original


@pytest.mark.parametrize('mutation',['missing_role','unknown_role','wall_as_reference','missing_scope_quote','no_preserve',
    'incomplete_selection','missing_jamb','missing_edge','missing_symbol','wrong_normal','native_length',
    'native_area','wcs_disagrees','actual_open_outer','sloping_wall','outside_inner','wrong_door_evidence'])
def test_invalid_or_partial_evidence_stops_before_mutation(mutation):
    f=list(fixture());c,s,scope=f[1:4]
    if mutation=='missing_role':scope['roles'].pop('C')
    elif mutation=='unknown_role':scope['roles']['C']='guess'
    elif mutation=='wall_as_reference':scope['roles']['I1']='unchanged_reference'
    elif mutation=='missing_scope_quote':scope['user_scope_quote']=''
    elif mutation=='no_preserve':scope['preserve_openings']=False
    elif mutation=='incomplete_selection':c['returned_count']-=1
    elif mutation=='missing_jamb':scope['openings'][0]['jamb_handles']=['J1']
    elif mutation=='missing_edge':scope['openings'][0]['edge_handles']=['E1']
    elif mutation=='missing_symbol':scope['openings'][0]['symbol_handle']='C'
    elif mutation=='wrong_normal':c['objects'][0]['geometry']['Normal']=[0,0,-1]
    elif mutation=='native_length':c['objects'][0]['geometry']['Length']+=100
    elif mutation=='native_area':c['objects'][0]['geometry']['Area']+=100
    elif mutation=='wcs_disagrees':c['objects'][0]['bounding_box']['minimum'][0]-=10
    elif mutation=='actual_open_outer':c['objects'][0]['geometry']['Coordinates']=c['objects'][0]['geometry']['Coordinates'][:-2]
    elif mutation=='sloping_wall':c['objects'][3]['geometry']['EndPoint'][2]=100
    elif mutation=='outside_inner':
        c['objects'][1]=poly('I1',box(-100,200,3000,1200));c['objects'][1]['selection_index']=1
    else:
        c['objects'][5]=line('E1',(500,10),(1500,10),'A-DOOR',200);c['objects'][5]['selection_index']=5
    before=copy.deepcopy(s)
    with pytest.raises(LiveRepairError):plan(f)
    assert s==before


def test_duplicate_retraced_inner_reference_is_only_accepted_when_covered():
    f=list(fixture());p=box(3200,200,4800,1200)
    duplicate=poly('D',p,False);duplicate['geometry']['Coordinates']+=duplicate['geometry']['Coordinates'][2:4]
    # Derive native length from the exact open trace instead of a role guess.
    from shapely.geometry import LineString
    vals=duplicate['geometry']['Coordinates'];duplicate['geometry']['Length']=LineString(list(zip(vals[::2],vals[1::2]))).length
    duplicate['selection_index']=len(f[1]['objects']);f[1]['objects'].append(duplicate)
    f[1]['selection_count']+=1;f[1]['returned_count']+=1;f[3]['roles']['D']='duplicate_boundary_reference'
    assert plan(f)['create']
    duplicate['geometry']['Coordinates'][-1]+=50
    with pytest.raises(LiveRepairError):plan(f)


def test_ambiguous_source_families_never_use_nearest_candidate():
    f=list(fixture())
    for e in copy.deepcopy(f[2]['entities']):
        e['entity_id']+='9';e['group_id']+='other';e['provenance']['material_partition_evidence']['source_candidate_id']='other-family'
        f[2]['entities'].append(e)
    with pytest.raises(LiveRepairError,match='唯一'):plan(f)


def test_apply_rechecks_entire_selection_including_unchanged_reference():
    f=fixture();p=plan(f);validate_current_selection(p,f[1])
    changed=copy.deepcopy(f[1]);changed['objects'][-1]['geometry']['Radius']=60
    with pytest.raises(LiveRepairError,match='改變'):validate_current_selection(p,changed)


def test_fixed_system_uses_actual_roof_not_2d_edge_elevation():
    f=list(fixture());r=roof(x0=-500,x1=5500)
    for t in r['triangles_mm']:
        for pt in t:pt[1]*=2
    f[2]=fixed_snapshot(entities=f[2]['entities'],roofs=[r])
    assert {r['base_z_mm']+r['height_mm']for r in plan(f)['create']}=={3200}
    f[2]['protected_entities']=[]
    with pytest.raises(LiveRepairError):plan(f)


def test_constrained_cap_fallback_preserves_complete_boundary_without_buffer():
    p=fixture()[4].difference(fixture()[5])
    with patch('dwg_semantic.live_repair.triangulate',return_value=[]):
        mesh=_mesh(p,0,3200)
    caps=[Polygon([mesh['vertices'][i][:2]for i in f])for f in mesh['faces']
          if all(mesh['vertices'][i][2]==0 for i in f)]
    assert unary_union(caps).symmetric_difference(p).area<1e-8


@pytest.mark.skipif(os.environ.get('DWG_RUN_NATIVE_TESTS')!='1',reason='Native runtime opt-in')
def test_actual_native_compound_mesh_save_reopen_is_closed(tmp_path):
    from dwg_semantic.skp_native import Native,utf
    p=plan();path=tmp_path/'synthetic-compound-live-mesh.skp'
    with Native()as api:
        model=api.create('SUModelCreate')
        try:
            entities=api.get('SUModelGetEntities',model)
            for row in p['create']:
                group,child=api.group(entities,row['group_id'])
                vertices=row['mesh']['vertices']
                api.geometry(child,faces=[([vertices[i]for i in face],[])for face in row['mesh']['faces']])
            api.call('SUModelSaveToFile',model,utf(path))
        finally:api.call('SUModelRelease',C.byref(model))
        reopened=api.create('SUModelCreateFromFile',utf(path))
        try:
            groups=api.refs('SUEntitiesGetNumGroups','SUEntitiesGetGroups',api.get('SUModelGetEntities',reopened))
            assert len(groups)==2
            for group in groups:
                child=api.get('SUGroupGetEntities',group)
                assert all(api.count('SUEdgeGetNumFaces',edge)==2 for edge in api.edges(child))
        finally:api.call('SUModelRelease',C.byref(reopened))
