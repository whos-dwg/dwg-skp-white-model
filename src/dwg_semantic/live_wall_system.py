"""Explicit-scope live wall-system revisions, preserving actual door clearances.

The raw complete Pickfirst snapshot is immutable evidence. Per-handle roles and
the user's scope quote are job data, not rules tied to any drawing or layer.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math

from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

from .live_cad_geometry import _finite, _digest, selection_geometry_signature
from .selection_openings import _selection_errors, SelectionOpeningError


def _fail(message):
    from .live_repair import LiveRepairError
    raise LiveRepairError(message)


def _native_xy(obj, scale):
    """Validate one native linear object before using any of its XY data."""
    g=obj.get('geometry') or {}
    normal=g.get('Normal')
    if not isinstance(normal,list) or len(normal)!=3 or any(abs(_finite(v)-n)>1e-12 for v,n in zip(normal,(0,0,1))):
        _fail('牆系來源缺少正 Z 原生法向。')
    if _finite(g.get('Thickness'))!=0:_fail('牆系來源含未支援的實際厚度。')
    if obj.get('object_type')=='AcDbPolyline':
        if _finite(g.get('ConstantWidth'))!=0:_fail('牆系輪廓多段線不得帶寬度。')
        values=g.get('Coordinates')
        if not isinstance(values,list) or len(values)<4 or len(values)%2:_fail('牆系頂點不完整。')
        points=[[_finite(values[i])*scale,_finite(values[i+1])*scale]for i in range(0,len(values),2)]
        z=_finite(g.get('Elevation'))*scale
        measured=LineString(points).length
        if g.get('Closed') is True:measured+=math.dist(points[-1],points[0])
        if any(abs(_finite(v))>1e-12 for v in g.get('Bulges',[])):_fail('牆系含未支援弧段。')
    elif obj.get('object_type')=='AcDbLine':
        native=[g.get('StartPoint'),g.get('EndPoint')]
        if any(not isinstance(p,list) or len(p)!=3 for p in native):_fail('原生牆線端點不完整。')
        xyz=[[_finite(v)*scale for v in p]for p in native]
        if abs(xyz[0][2]-xyz[1][2])>1e-6:_fail('牆線非共面，不能投影成牆界。')
        points=[p[:2]for p in xyz];z=xyz[0][2];measured=math.dist(*xyz)
    else:_fail('牆界只接受可核對的原生直線或2D多段線。')
    if abs(measured-_finite(g.get('Length'))*scale)>max(1e-5,measured*1e-10):
        _fail('牆線原生周長與頂點不符，可能含曲線或截斷。')
    bounds=LineString(points).bounds
    box=obj.get('bounding_box') or {}
    lo,hi=box.get('minimum'),box.get('maximum')
    if box.get('coordinate_system')!='WCS' or not isinstance(lo,list) or not isinstance(hi,list) or len(lo)!=3 or len(hi)!=3:
        _fail('牆系缺少完整 WCS 範圍。')
    expected=[*bounds[:2],z,*bounds[2:],z]
    if max(abs(_finite(a)*scale-b)for a,b in zip([*lo,*hi],expected))>1e-5:_fail('牆系 OCS／WCS 範圍不一致。')
    return points,z


def _native_polygon(obj,scale):
    points,z=_native_xy(obj,scale)
    g=obj['geometry']
    if obj['object_type']!='AcDbPolyline' or (g.get('Closed')is not True and points[0]!=points[-1]):
        _fail('材料界必須原生閉合，或原生頂點首尾完全相同；不能補缺線。')
    p=Polygon(points)
    if not p.is_valid or p.area<=.1:_fail('牆系材料輪廓空白或自交。')
    if abs(p.area-_finite(g.get('Area'))*scale**2)>max(1e-4,p.area*1e-10):_fail('原生面積與直線輪廓不符。')
    return p,z


def _serialized(p):
    # This is exactly the existing writer's 1e-6 mm serialization lattice,
    # never a case tolerance or buffer. Retain the full native rings separately.
    def ring(r):
        points=[]
        for x,y in list(r.coords)[:-1]:
            pt=(round(x,6),round(y,6))
            if not points or points[-1]!=pt:points.append(pt)
        if len(points)>1 and points[0]==points[-1]:points.pop()
        return points
    result=Polygon(ring(p.exterior),[ring(r)for r in p.interiors])
    if not result.is_valid or result.area<=.1 or p.symmetric_difference(result).area>max(.01,p.area*1e-8):
        _fail('牆系在既有數值序列化格上無法維持原輪廓。')
    return result


def prepare_wall_system(document,selection,scope):
    from .live_repair import _polygon_payload
    try:drawing,scale=_selection_errors(selection,document)
    except SelectionOpeningError as error:_fail(str(error))
    if selection.get('context',{}).get('active_space_code')!=1:_fail('牆系必須取自模型空間。')
    if (not isinstance(scope,dict) or scope.get('schema')!='cad-wall-system-scope-v1' or
        not str(scope.get('user_scope_quote','')).strip() or scope.get('preserve_openings')is not True):
        _fail('整組牆系需要明確使用者範圍與保留門洞的決策。')
    objects={str(o['handle']):o for o in selection['objects']}
    roles=scope.get('roles') or {}
    if len(objects)!=len(selection['objects']) or set(objects)!=set(roles):_fail('每個實際選取都須有唯一角色，不得略過或偽造部分選取。')
    allowed={'outer_boundary','inner_boundary','door_jamb','duplicate_boundary_reference','opening_edge','opening_symbol','unchanged_reference'}
    if set(roles.values())-allowed:_fail('牆系角色不受支援。')
    layer=scope.get('wall_layer')
    if not isinstance(layer,str) or not layer:_fail('缺少本次明確牆系圖層。')
    boundaries={};z_values=[];jambs={};duplicates={};opening_edges={};symbols={}
    for handle,obj in objects.items():
        role=roles[handle];source_layer=obj.get('properties',{}).get('Layer')
        if role in {'outer_boundary','inner_boundary','door_jamb','duplicate_boundary_reference'}:
            if source_layer!=layer:_fail('牆界角色與使用者指定牆系圖層不一致。')
            if role in {'outer_boundary','inner_boundary'}:
                p,z=_native_polygon(obj,scale);boundaries[handle]=p
            else:
                pts,z=_native_xy(obj,scale)
                if role=='door_jamb' and (obj['object_type']!='AcDbLine' or len(pts)!=2):_fail('門側界必須是完整原生直線。')
                (jambs if role=='door_jamb'else duplicates)[handle]=LineString(pts)
            z_values.append(z)
        elif role=='opening_edge':
            pts,z=_native_xy(obj,scale)
            if obj['object_type']!='AcDbLine':_fail('洞口邊界證據必須為完整原生直線。')
            opening_edges[handle]=(LineString(pts),z)
        elif role=='opening_symbol':
            if obj['object_type']!='AcDbBlockReference':_fail('洞口符號角色需要實際原生圖塊。')
            symbols[handle]=obj
        elif source_layer==layer:_fail('不能把選取內的牆界默默排除為設備參考。')
    if not z_values or max(z_values)-min(z_values)>1e-6:_fail('牆材料界與門側界不在同一 Z 平面。')
    outer_handles=[h for h,r in roles.items()if r=='outer_boundary']
    if len(outer_handles)!=1:_fail('整組牆系需一個確切外框。')
    outer=boundaries[outer_handles[0]]
    inners=[p for h,p in boundaries.items()if roles[h]=='inner_boundary']
    if any(not outer.contains(p)for p in inners):_fail('內圈必須完全位於實際外框內。')
    if any(p.intersection(q).area>0 for i,p in enumerate(inners)for q in inners[i+1:]):_fail('牆系內圈相互重疊。')
    material=outer.difference(unary_union(inners))
    if not isinstance(material,Polygon) or not material.is_valid:_fail('外框扣內圈未形成單一材料域。')
    for line in duplicates.values():
        if not any(line.difference(p.boundary.buffer(1e-5)).length<=1e-5 for p in inners):
            _fail('重複牆界參考尚未被實際內圈完整涵蓋；不得忽略新牆線。')
    openings=[];used_jambs=set();used_edges=set();used_symbols=set()
    for item in scope.get('openings',[]):
        hs=item.get('jamb_handles',[]);es=item.get('edge_handles',[]);symbol=item.get('symbol_handle')
        if len(hs)!=2 or len(set(hs))!=2 or not set(hs)<=set(jambs) or len(es)!=2 or len(set(es))!=2 or not set(es)<=set(opening_edges) or symbol not in symbols:
            _fail('門洞缺少兩側牆界、兩條洞口邊與原生符號的完整配對。')
        a,b=[list(jambs[h].coords)for h in hs]
        candidates=[Polygon([*a,*b]),Polygon([*a,*reversed(b)])]
        p=next((p for p in candidates if p.is_valid and p.area>.1 and abs(p.area-p.minimum_rotated_rectangle.area)<.01),None)
        if p is None:_fail('門側界不能證明矩形洞口。')
        if abs(jambs[hs[0]].length-jambs[hs[1]].length)>1e-5:_fail('門洞兩側牆厚不一致。')
        if any(line.difference(p.boundary.buffer(1e-5)).length>1e-5 for line,z in [opening_edges[h]for h in es]):_fail('洞口邊未對應實際門側界。')
        if abs(opening_edges[es[0]][1]-opening_edges[es[1]][1])>1e-6:_fail('洞口2D邊界的來源高程互相衝突。')
        insertion=symbols[symbol].get('geometry',{}).get('InsertionPoint')
        if not isinstance(insertion,list) or len(insertion)!=3:_fail('洞口符號缺少插入點。')
        from shapely.geometry import Point
        if p.distance(Point(*[_finite(v)*scale for v in insertion[:2]]))>1e-5:_fail('門扇符號未位於洞口。')
        if any(p.boundary.distance(jambs[h])>1e-5 for h in hs):_fail('門側界未落在洞口界線。')
        opening_material=p.intersection(material)
        if opening_material.area<p.area-.01:_fail('門洞範圍未完整穿越本牆材料；不能延長或猜洞。')
        if any(p.intersection(row['polygon']).area>.1 for row in openings):_fail('門洞相互重疊。')
        openings.append({'polygon':_serialized(p),'native_footprint':_polygon_payload(p),
                         'handles':[*hs,*es,symbol],'source_2d_edge_z_mm':opening_edges[es[0]][1]})
        used_jambs.update(hs);used_edges.update(es);used_symbols.add(symbol)
    if used_jambs!=set(jambs) or used_edges!=set(opening_edges) or used_symbols!=set(symbols):_fail('牆系含未配對的門洞證據。')
    revision={'schema':'cad-pickfirst-live-revision-v1','kind':'wall_system',
        'base_source_sha256':document['intake']['source_sha256'],'source_path':drawing,
        'geometry_signature':selection_geometry_signature(selection),'observed_at_utc':selection.get('observed_at_utc'),
        'basis':'explicit_wall_scope_native_outer_minus_inner_and_preserved_actual_openings',
        'source_layer':layer,'scope':deepcopy(scope),'selection_snapshot':deepcopy(selection),
        'native_footprint':_polygon_payload(material),'footprint':_polygon_payload(_serialized(material)),
        'serialization_grid_mm':1e-6,'source_plane_z_mm':z_values[0]}
    return {'material':_serialized(material),'revision':revision,'openings':openings,'layer':layer}


def build_wall_system_plan(document,selection,snapshot,scope):
    from .live_repair import (build_live_repair_plan,_actual_footprint,_provenance,_group_from_entity,
        _create_row,_fit_fixed_live_create,_fixed_live_standard,_overlap_blockers,_polygon_payload)
    data=prepare_wall_system(document,selection,scope)
    families={}
    for e in snapshot['entities']:
        if e.get('tag')!=data['layer'] or e.get('category')not in {'architectural_wall','structural_wall'}:continue
        p=_provenance(e);partition=p.get('material_partition_evidence')or{}
        identity=p.get('wall_system_family_id')or partition.get('source_candidate_id')or p.get('automatic_recognition_id')
        if identity:families.setdefault(identity,[]).append(e)
    matches=[]
    for identity,rows in families.items():
        original=[_provenance(e).get('wall_system_match_footprint')or
                  _provenance(e).get('material_partition_evidence',{}).get('original_footprint')for e in rows]
        if any(not isinstance(v,dict)for v in original):continue
        profiles=[Polygon(v['outer'],v.get('holes',[]))for v in original]
        if any(not p.is_valid or not p.equals(profiles[0])for p in profiles):continue
        if profiles[0].boundary.hausdorff_distance(data['material'].boundary)>.02:continue
        if any(_actual_footprint(e).difference(data['material'].buffer(.02)).area>.1 for e in rows):continue
        matches.append((identity,rows))
    if len(matches)!=1:_fail('整組牆系沒有唯一、同原圖層及原位輪廓的來源家族，不能用最近者猜測。')
    family,targets=matches[0]
    plan=build_live_repair_plan(document,None,snapshot,action='remove_wrong',target_entity_ids=[e['entity_id']for e in targets])
    parents={e['parent_entity_id']for e in targets};categories={e['category']for e in targets}
    if len(parents)!=1 or len(categories)!=1 or len({e.get('material')for e in targets})!=1:_fail('整組牆跨不同父群組、分類或材質。')
    sides=[e for e in targets if _provenance(e).get('opening_part_role')=='side']
    lintels=[e for e in targets if _provenance(e).get('opening_part_role')=='lintel']
    if len(sides)+len(lintels)!=len(targets) or len(lintels)!=len(data['openings']):_fail('現有牆系門洞分段不完整；不能填回未辨識洞口。')
    bases={round(e['base_z_mm'],6)for e in sides}
    tops={round(e['base_z_mm']+e['height_mm'],6)for e in targets}
    if len(bases)!=1 or len(tops)!=1:_fail('現有牆系有未支援的高程分段。')
    bottom=min(bases);top=max(tops);paired=[];used=set()
    for opening in data['openings']:
        found=[e for e in lintels if _actual_footprint(e).boundary.hausdorff_distance(opening['polygon'].boundary)<=.02]
        if len(found)!=1 or found[0]['entity_id']in used:_fail('CAD 門洞未唯一對應到現有實際過梁。')
        e=found[0];used.add(e['entity_id'])
        if not bottom<e['base_z_mm']<top:_fail('現有門洞淨高無法證明。')
        paired.append((opening,e))
    voids=unary_union([o['polygon']for o,e in paired])
    body=data['material'].difference(voids)
    parts=[body]if isinstance(body,Polygon)else list(getattr(body,'geoms',[]))
    if not parts or any(not isinstance(p,Polygon)or not p.is_valid for p in parts):_fail('新牆體門側材料無法證明。')
    assignments=[];assigned=set()
    for p in parts:
        found=[e for e in sides if _actual_footprint(e).intersection(p).area>_actual_footprint(e).area*.9]
        if not found or any(e['entity_id']in assigned for e in found):_fail('新舊門側實體無法唯一配對。')
        assigned.update(e['entity_id']for e in found);assignments.append((p,found,bottom,top-bottom))
    if assigned!={e['entity_id']for e in sides}:_fail('新牆範圍未完整涵蓋既有門側材料。')
    assignments.extend((data['material'].intersection(o['polygon']),[e],e['base_z_mm'],top-e['base_z_mm'])for o,e in paired)
    signature=data['revision']['geometry_signature']
    ids=['cad-live:'+document['intake']['source_sha256'][:16]+':'+h+':'+signature[:20]
         for h,r in scope['roles'].items()if r!='unchanged_reference']
    create=[]
    for p,templates,base,height in assignments:
        e=templates[0]
        p=_serialized(p);group=_group_from_entity(e)
        original=[_group_from_entity(row)for row in templates]
        if len(templates)>1:
            identity='live-wall-system:'+_digest([family,sorted(e['group_id']for e in templates),_polygon_payload(p)])[:20]
            group.update(id=identity,name=identity)
        group.update(**_polygon_payload(p),base_z_mm=base,height_mm=height,area_mm2=p.area,volume_mm3=p.area*height,
            source_entity_ids=ids,source_handles=sorted(h for h,r in scope['roles'].items()if r!='unchanged_reference'),
            live_repair_replaced_group_ids=[t['group_id']for t in targets],cad_live_revision=deepcopy(data['revision']),
            replaced_source_binding=original,wall_system_family_id=family)
        group['wall_system_match_footprint']=_polygon_payload(data['material'])
        # Historical partition/recognition evidence belongs to the replaced
        # binding; do not label restored material as still subtracted.
        group.pop('material_partition_evidence',None)
        group.pop('source_provenance',None)
        group['geometry_basis']='live_native_outer_minus_inner_and_preserved_actual_door_clearance'
        group['evidence']=[*group.get('evidence',[]),'user_confirmed_live_wall_system_scope']
        create.append(_create_row(group,e,next(iter(parents))))
    standard=_fixed_live_standard(snapshot)
    if standard is not None:create=_fit_fixed_live_create(create,snapshot,standard,'replace_from_cad',document)
    blockers=_overlap_blockers(create,snapshot,{e['entity_id']for e in targets})
    if blockers:_fail('牆系同步與未選取實體／樓板相交：'+json.dumps(blockers,ensure_ascii=False))
    # All constraints are validated above; seal the new compound plan only now.
    plan.pop('canonical_body_json');plan.pop('plan_sha256')
    plan.update(action='replace_from_cad',create=create)
    plan['source'].update(selection_sha256=_digest(selection),live_cad_revision=data['revision'],
        items=[{'handle':h,'role':r}for h,r in scope['roles'].items()],
        position_match_audit={'basis':'unique_same_tag_source_family_and_complete_world_contour',
            'family_id':family,'target_entity_ids':[e['entity_id']for e in targets]},
        preserved_opening_audit=[{'opening_ids':_provenance(e).get('opening_ids'),
            'base_z_mm':bottom,'clear_top_z_mm':e['base_z_mm'],'native_handles':o['handles'],
            'footprint':_polygon_payload(o['polygon']),'source_2d_edge_z_mm':o['source_2d_edge_z_mm']}for o,e in paired])
    # The bridge contract is JSON, including callers that supplied tuple rings.
    plan=json.loads(json.dumps(plan,ensure_ascii=False,allow_nan=False))
    from .local_snapshot import bind_local_read
    from .live_repair import seal_live_repair_plan
    return seal_live_repair_plan(bind_local_read(plan,snapshot))
