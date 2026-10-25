import copy
from datetime import datetime,timedelta,timezone
from unittest.mock import patch

import pytest
from shapely.geometry import Polygon

from dwg_semantic.live_repair import LiveRepairError,build_live_repair_plan,validate_plan_digest
from dwg_semantic.live_cad_geometry import live_regions,require_fresh_selection,validate_current_selection
from dwg_semantic.sketchup_live import apply_live_repair_plan
from test_live_repair import document,snapshot,live_entity,SOURCE


def cad(points=None):
    points=points or [[0,0],[2500,0],[2500,200],[0,200]]
    p=Polygon(points)
    return {'success':True,'status':'SELECTED','observed_at_utc':datetime.now(timezone.utc).isoformat(),
        'document':{'path':SOURCE},'selection_source':'PickfirstSelectionSet','selection_count':1,
        'returned_count':1,'truncated':False,'context':{'active_space_code':1},
        'objects':[{'selection_index':0,'handle':'NEW20','object_type':'AcDbPolyline',
            'properties':{'Layer':'0'},'bounding_box':{'coordinate_system':'WCS',
                'minimum':[p.bounds[0],p.bounds[1],0],'maximum':[p.bounds[2],p.bounds[3],0]},
            'geometry':{'Closed':True,'Coordinates':[v for pt in points for v in pt],
                'Normal':[0,0,1],'Elevation':0,'ConstantWidth':0,'Thickness':0,
                'Length':p.length,'Area':p.area}}]}


def entity(identity='wall-1',entity_id='entity:101',outer=None,**kwargs):
    e=live_entity(identity,entity_id=entity_id,outer=outer,**kwargs)
    e['volume_mm3']=Polygon(e['footprint']['outer']).area*e['height_mm']
    return e


def plan(selection=None,state=None):
    return build_live_repair_plan(document(),selection or cad(),state or snapshot(),
        action='replace_from_cad',live_cad_geometry=True,auto_match_position=True)


def test_new_handle_shortens_unique_wall_and_preserves_source_base_tag_and_z():
    d,s,c=document(),snapshot(),cad();before=copy.deepcopy((d,s,c))
    p=build_live_repair_plan(d,c,s,action='replace_from_cad',live_cad_geometry=True,auto_match_position=True)
    assert [r['entity_id']for r in p['targets']]==['entity:101']
    row=p['create'][0]
    assert Polygon(row['footprint']['outer']).bounds==(0,0,2500,200)
    assert row['height_mm']==3000 and row['base_z_mm']==0 and row['tag']=='A-WALL'
    assert row['provenance']['source_layer']=='A-WALL'
    assert row['provenance']['source_layers']==['0','A-WALL']
    assert row['provenance']['source_entity_ids'][0].startswith('cad-live:')
    assert row['provenance']['replaced_source_binding']['source_handles']==['W1']
    assert p['source']['manifest']==s['root']['source_manifest']
    assert p['source']['position_match_audit']['match']['original_length_mm']==4000
    assert validate_plan_digest(p)==p['plan_sha256']
    assert before==(d,s,c)


def test_new_live_geometry_requires_opt_in_not_a_forged_normalized_source():
    with pytest.raises(LiveRepairError,match='正規化來源'):
        build_live_repair_plan(document(),cad(),snapshot(),action='replace_from_cad',auto_match_position=True)


def test_existing_handle_uses_changed_live_contour_not_stale_document_coordinates():
    d=document();d['entities'].append({'type':'LWPOLYLINE','handle':'NEW20','source_handle':'NEW20',
        'entity_id':'source::NEW20','source_file':SOURCE,'layer':'A-WALL','owner_block':'*Model_Space',
        'closed':True,'vertices_world':[[0,0],[4000,0],[4000,200],[0,200]]})
    p=build_live_repair_plan(d,cad(),snapshot(),action='replace_from_cad',live_cad_geometry=True,auto_match_position=True)
    assert Polygon(p['create'][0]['footprint']['outer']).bounds[2]==2500


def test_position_matching_ignores_incidental_other_skp_selection():
    other=entity('other','entity:202',[[5000,0],[8000,0],[8000,200],[5000,200]])
    p=plan(state=snapshot([entity(),other],selection=['entity:202']))
    assert p['targets'][0]['entity_id']=='entity:101'


@pytest.mark.parametrize('other',[
    entity('duplicate','entity:202'),
    entity('overlapping','entity:202',[[0,0],[5000,0],[5000,200],[0,200]]),
])
def test_two_geometrically_valid_targets_are_never_ranked_by_nearest(other):
    with pytest.raises(LiveRepairError,match='沒有唯一'):
        plan(state=snapshot([entity(),other]))


@pytest.mark.parametrize('points',[
    [[0,500],[2500,500],[2500,700],[0,700]],
    [[0,0],[2500,0],[2500,300],[0,300]],
    [[500,0],[3000,0],[3000,200],[500,200]],
    [[6000,0],[8000,0],[8000,200],[6000,200]],
])
def test_wrong_line_thickness_anchor_or_position_is_rejected(points):
    with pytest.raises(LiveRepairError,match='沒有唯一'):
        plan(cad(points))


def test_shared_large_world_coordinates_not_global_origin_distance():
    x,y=-182432.128,294567.54
    old=[[x,y],[x+4000,y],[x+4000,y+200],[x,y+200]]
    new=[[x,y-.004],[x+2500,y-.004],[x+2500,y+200-.004],[x,y+200-.004]]
    p=plan(cad(new),snapshot([entity(outer=old)]))
    assert p['targets'][0]['entity_id']=='entity:101'
    assert Polygon(p['create'][0]['footprint']['outer']).equals(Polygon(new))


def test_extension_collision_still_rejected_after_matching():
    other=entity('cross','entity:202',[[4500,-500],[4700,-500],[4700,500],[4500,500]])
    with pytest.raises(LiveRepairError,match='正體積重疊'):
        plan(cad([[0,0],[5000,0],[5000,200],[0,200]]),snapshot([entity(),other]))


def test_locked_and_opening_parts_are_not_silently_replaced():
    locked=entity();locked['locked']=True
    with pytest.raises(LiveRepairError,match='鎖定'):plan(state=snapshot([locked]))
    opening=entity();opening['provenance']['opening_ids']=['door']
    with pytest.raises(LiveRepairError,match='門窗分段'):plan(state=snapshot([opening]))


@pytest.mark.parametrize('mutation',[
    lambda c:c['document'].update(path='D:/Other/wrong.dwg'),
    lambda c:c.update(truncated=True),
    lambda c:c.update(returned_count=2),
    lambda c:c['context'].update(active_space_code=0),
    lambda c:c['objects'][0].update(object_type='AcDbBlockReference'),
    lambda c:c['objects'][0]['geometry'].update(Closed=False),
    lambda c:c['objects'][0]['geometry'].update(Normal=[0,1,0]),
    lambda c:c['objects'][0]['geometry'].update(Length=5401),
    lambda c:c['objects'][0]['geometry'].update(Area=499999),
    lambda c:c['objects'][0]['geometry'].update(Bulges=[0,.1,0,0]),
    lambda c:c['objects'][0]['geometry'].update(ConstantWidth=1),
    lambda c:c['objects'][0]['geometry'].update(Thickness=1),
    lambda c:c['objects'][0]['geometry'].update(Coordinates=[0,0,2500]),
    lambda c:c['objects'][0]['geometry'].update(Elevation=3),
    lambda c:c['objects'][0]['geometry'].update(Length=True),
    lambda c:c['objects'][0]['bounding_box'].update(coordinate_system='OCS'),
])
def test_unproven_or_malformed_native_geometry_fails_closed(mutation):
    c=cad();mutation(c)
    with pytest.raises((ValueError,LiveRepairError)):live_regions(document(),c)


def test_repeated_read_timestamp_is_not_a_geometry_change_but_changed_vertices_are():
    p=plan();current=cad()
    validate_current_selection(p,current)
    changed=cad([[0,0],[2600,0],[2600,200],[0,200]])
    with pytest.raises(LiveRepairError,match='已改變'):validate_current_selection(p,changed)


def test_apply_requires_fresh_re_read_before_any_remote_call():
    p=plan()
    with patch('dwg_semantic.sketchup_live._call',side_effect=AssertionError('must not reach bridge')):
        with pytest.raises(LiveRepairError,match='重新讀取'):
            apply_live_repair_plan(p,confirmation_sha256=p['plan_sha256'])
    stale=cad();stale['observed_at_utc']=(datetime.now(timezone.utc)-timedelta(minutes=2)).isoformat()
    with pytest.raises(LiveRepairError,match='過期'):validate_current_selection(p,stale)


def test_valid_fresh_read_is_passed_to_existing_narrow_apply_bridge():
    p=plan();calls=[]
    with patch('dwg_semantic.sketchup_live._require_current_bridge'),patch('dwg_semantic.sketchup_live._call',side_effect=lambda *a,**kw:calls.append((a,kw))or{'status':'applied'}):
        result=apply_live_repair_plan(p,confirmation_sha256=p['plan_sha256'],current_selection=cad())
    assert result['status']=='applied' and calls[0][0][0]=='apply_live_repair'


def test_stale_snapshot_and_moved_model_root_are_rejected():
    c=cad();c['observed_at_utc']='2000-01-01T00:00:00Z'
    with pytest.raises(LiveRepairError,match='過期'):require_fresh_selection(c)
    s=snapshot();s['root']['world_transform']['origin_mm']=[1,0,0]
    with pytest.raises(LiveRepairError,match='根群組'):plan(state=s)
