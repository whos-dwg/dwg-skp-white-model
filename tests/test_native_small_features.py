"""World coordinates survive native small-feature conditioning and real save/read."""
import copy
import ctypes as C
import json
import os
from collections import Counter

import pytest
from shapely.geometry import Polygon
from shapely.ops import unary_union

from dwg_semantic.skp_native import Native,utf
from dwg_semantic.modeling import _extrusion,prepare_model,write_model
from test_modeling import rectangle,decisions


@pytest.mark.parametrize('feature',[.02,.000008])
def test_conditioning_preserves_world_lattice_without_mutating_input(feature):
    faces=[([[143721.001,-284613.6,0],[143821.001,-284613.6,0],
             [143821.001,-284613.6+feature,0]],[])]
    before=copy.deepcopy(faces);api=object.__new__(Native);captured={}
    api.geometry=lambda entities,faces,**kw:captured.update(kw)
    api.call=lambda name,group,matrix:None
    result=api.solid_geometry(None,None,faces)
    assert result['scale']>1
    assert captured['construction_scale']==result['scale']
    from dwg_semantic.skp_native import Transform
    transform=Transform((C.c_double*16)(*result['transform']))
    for point in faces[0][0]:
        native=[(round(v,6)-result['origin_mm'][i])*result['scale']for i,v in enumerate(point)]
        assert Native.transformed_point(native,transform)==pytest.approx([round(v,6)for v in point],abs=1e-9)
    assert faces==before


@pytest.mark.skipif(os.environ.get('DWG_RUN_NATIVE_TESTS')!='1',reason='Native runtime opt-in')
@pytest.mark.parametrize('feature',[.02,.000008])
def test_actual_saved_small_step_remains_closed_and_exact_in_world_space(tmp_path,feature):
    x,y=143721.001,-284613.6
    outline=[[x,y],[x+100,y],[x+100,y+800],[x+100-feature,y+800],
             [x+100-feature,y+1800],[x,y+1800]]
    p=prepare_model({'intake':{'source_sha256':'synthetic-small-step'},'entities':[rectangle('S')]},{},decisions())
    p['groups'][0].update(outer=outline,holes=[])
    original=copy.deepcopy(p)
    destination=tmp_path/'small-step.skp'
    qa=write_model(p,destination)
    assert qa['all_pass'] and p==original
    assert len(qa['native_geometry_conditioning'])==1
    # Independent face-loop readback: not planner geometry or metadata.
    with Native()as api:
        model=api.create('SUModelCreateFromFile',utf(destination))
        try:
            root=api.refs('SUEntitiesGetNumGroups','SUEntitiesGetGroups',api.get('SUModelGetEntities',model))[0]
            category=api.refs('SUEntitiesGetNumGroups','SUEntitiesGetGroups',api.get('SUGroupGetEntities',root))[0]
            leaf=api.refs('SUEntitiesGetNumGroups','SUEntitiesGetGroups',api.get('SUGroupGetEntities',category))[0]
            entities=api.get('SUGroupGetEntities',leaf);transform=api.group_transform(leaf)
            assert set(Counter(api.count('SUEdgeGetNumFaces',e)for e in api.edges(entities)))=={2}
            bottom=[]
            for face in api.refs('SUEntitiesGetNumFaces','SUEntitiesGetFaces',entities):
                pts=[]
                loop=api.get('SUFaceGetOuterLoop',face)
                for vertex in api.refs('SULoopGetNumVertices','SULoopGetVertices',loop):
                    from dwg_semantic.skp_native import P3
                    point=P3();api.call('SUVertexGetPosition',vertex,C.byref(point))
                    pts.append(api.transformed_point([point.x*25.4,point.y*25.4,point.z*25.4],transform))
                if all(abs(pt[2])<1e-7 for pt in pts):bottom.append(Polygon([pt[:2]for pt in pts]))
            assert unary_union(bottom).symmetric_difference(Polygon(outline)).area<1e-6
        finally:api.call('SUModelRelease',C.byref(model))


def test_narrow_hole_clearance_also_requests_conditioning():
    api=object.__new__(Native);api.geometry=lambda *a,**kw:None;api.call=lambda *a:None
    faces=[([[0,0,0],[1000,0,0],[1000,1000,0],[0,1000,0]],[])]
    assert api.solid_geometry(None,None,faces,minimum_clearance_mm=.01)['scale']==100


def test_normal_size_solids_keep_identity_transform():
    api=object.__new__(Native);api.geometry=lambda *a,**kw:None;api.call=lambda *a:None
    faces=[([[1000,2000,0],[1100,2000,0],[1100,2100,0],[1000,2100,0]],[])]
    result=api.solid_geometry(None,None,faces)
    assert result['scale']==1 and result['origin_mm']==[0,0,0]
