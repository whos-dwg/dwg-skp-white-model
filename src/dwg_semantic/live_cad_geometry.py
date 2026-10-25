"""Bounded live-CAD contour revisions and fail-closed positional wall matching.

The source-file manifest remains an immutable base. Live Pickfirst geometry is
a separate, auditable overlay, never a fabricated entity in that old document.
"""
from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from datetime import datetime, timezone

from shapely.geometry import Polygon

from .selection_openings import _selection_errors, _path_key

LINEAR_MATCH_MM = .02


def _error(message):
    from .live_repair import LiveRepairError
    return LiveRepairError(message)


def _digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,
        separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest()


def _finite(value):
    if isinstance(value,bool):raise _error('CAD 幾何數值不能是布林值。')
    try:result=float(value)
    except (ValueError,TypeError):raise _error('CAD 幾何缺少有限數值。')
    if not math.isfinite(result):raise _error('CAD 幾何含非有限數值。')
    return result


def require_fresh_selection(selection, *, max_age_seconds=30):
    try:
        observed=datetime.fromisoformat(selection['observed_at_utc'].replace('Z','+00:00'))
        if observed.tzinfo is None:raise ValueError('timezone required')
        age=(datetime.now(timezone.utc)-observed).total_seconds()
    except (KeyError,AttributeError,TypeError,ValueError):
        raise _error('即時 CAD 輪廓需要具時區的實際讀取時間。')
    if age < -5 or age > max_age_seconds:
        raise _error('CAD 即時選取已過期；請重新讀取目前 Pickfirst 選取再規劃／套用。')


def selection_geometry_signature(selection):
    return _digest({'document':_path_key(selection.get('document',{}).get('path')),
        'objects':[{k:obj.get(k)for k in ('handle','object_type','geometry','bounding_box')}|
                   {'layer':obj.get('properties',{}).get('Layer')}
                   for obj in selection.get('objects',[])]})


def live_regions(document,selection):
    """One exact straight closed positive-Z native 2D polyline, not its bbox."""
    from .live_repair import _polygon_payload
    drawing,scale=_selection_errors(selection,document)
    if len(selection['objects'])!=1:raise _error('CAD 輪廓變更一次只接受一個完整封閉牆輪廓。')
    if selection.get('context',{}).get('active_space_code')!=1:
        raise _error('CAD 輪廓變更必須來自模型空間。')
    obj=selection['objects'][0];g=obj.get('geometry')or{}
    if obj.get('object_type')!='AcDbPolyline' or g.get('Closed')is not True:
        raise _error('即時變更目前只支援完整、封閉的原生2D多段線輪廓。')
    normal=g.get('Normal')
    if not isinstance(normal,list)or len(normal)!=3 or any(abs(_finite(v)-n)>1e-12 for v,n in zip(normal,(0,0,1))):
        raise _error('不支援此 OCS 法向；不能把傾斜或反向來源猜成 WCS 平面。')
    if _finite(g.get('ConstantWidth'))!=0 or _finite(g.get('Thickness'))!=0:
        raise _error('有寬度或厚度的 CAD 多段線需另作來源判讀。')
    coords=g.get('Coordinates')
    if not isinstance(coords,list)or len(coords)<6 or len(coords)%2:
        raise _error('CAD 多段線頂點不完整。')
    points=[[_finite(coords[i])*scale,_finite(coords[i+1])*scale]for i in range(0,len(coords),2)]
    polygon=Polygon(points)
    if not polygon.is_valid or polygon.area<=.1:raise _error('CAD 即時輪廓空白或自交。')
    if any(abs(_finite(v))>1e-12 for v in g.get('Bulges',[])):
        raise _error('含弧段的 CAD 輪廓尚不能作此即時線性替換。')
    # Coordinates do not contain bulges. Native perimeter AND area must agree
    # with the complete straight-edge ring before accepting the live geometry.
    if (abs(polygon.length-_finite(g.get('Length'))*scale)>max(1e-5,polygon.length*1e-10) or
        abs(polygon.area-_finite(g.get('Area'))*scale*scale)>max(1e-4,polygon.area*1e-10)):
        raise _error('原生周長／面積與頂點直線輪廓不符，可能含弧段或截斷資料。')
    box=obj.get('bounding_box')or{}
    if box.get('coordinate_system')!='WCS':raise _error('缺少 WCS 交叉核對資料。')
    low,high=box.get('minimum'),box.get('maximum')
    if not isinstance(low,list)or not isinstance(high,list)or len(low)!=3 or len(high)!=3:
        raise _error('CAD WCS 範圍不完整。')
    z=_finite(g.get('Elevation'))*scale
    expected=[*polygon.bounds[:2],z,*polygon.bounds[2:],z]
    actual=[_finite(v)*scale for v in [*low,*high]]
    if max(abs(a-b)for a,b in zip(expected,actual))>1e-5:
        raise _error('OCS 頂點與 WCS 範圍／高程不一致。')
    layer=obj.get('properties',{}).get('Layer')
    if not isinstance(layer,str)or not layer:raise _error('CAD 輪廓缺少原圖層。')
    signature=selection_geometry_signature(selection)
    identity='cad-live:'+document['intake']['source_sha256'][:16]+':'+str(obj['handle'])+':'+signature[:20]
    revision={'schema':'cad-pickfirst-live-revision-v1','base_source_sha256':document['intake']['source_sha256'],
        'source_path':drawing,'geometry_signature':signature,'observed_at_utc':selection.get('observed_at_utc'),
        'basis':'live_pickfirst_geometry_overlay_on_unchanged_source_file',
        'source_entity_id':identity,'source_layer':layer,'source_plane_z_mm':z,
        'footprint':_polygon_payload(polygon),'selection_snapshot':deepcopy(selection)}
    return [{'selection_handle':str(obj['handle']),'part_index':0,'polygon':polygon,
        'source_entity_ids':[identity],'source_handles':[str(obj['handle'])],
        'source_layer':layer,'source_layers':[layer],'live_cad_revision':revision}]


def _rectangle(polygon):
    if polygon.interiors:return None
    rect=polygon.minimum_rotated_rectangle
    if not isinstance(rect,Polygon)or polygon.symmetric_difference(rect).area>max(.1,polygon.area*1e-8):return None
    pts=list(rect.exterior.coords)[:-1]
    edges=[(math.dist(a,b),a,b)for a,b in zip(pts,pts[1:]+pts[:1])]
    length,a,b=max(edges,key=lambda row:row[0]);width=min(row[0]for row in edges)
    if width<=0 or length<width*1.5:return None
    u=[(b[i]-a[i])/length for i in range(2)]
    if u[0]<0 or (abs(u[0])<1e-12 and u[1]<0):u=[-v for v in u]
    return length,width,u


def match_wall_by_position(regions,entities):
    from .live_repair import _actual_footprint
    if len(regions)!=1:raise _error('定位比對一次只接受一段牆。')
    new=regions[0]['polygon'];info=_rectangle(new)
    if info is None:raise _error('自動定位目前只接受可證明的直線矩形牆；其他輪廓請明選 SKP 目標。')
    length,width,u=info;v=[-u[1],u[0]]
    def interval(p,axis):
        values=[x*axis[0]+y*axis[1]for x,y in p.exterior.coords]
        return min(values),max(values)
    na,nb=interval(new,u);nc,nd=interval(new,v)
    candidates=[]
    for entity in entities:
        if entity.get('category')not in {'architectural_wall','structural_wall'}:continue
        p=_actual_footprint(entity);old=_rectangle(p)
        if old is None:continue
        old_length,old_width,old_u=old
        if abs(u[0]*old_u[1]-u[1]*old_u[0])>1e-5:continue
        tolerance=max(LINEAR_MATCH_MM,width*1e-4)
        if abs(width-old_width)>tolerance:continue
        oa,ob=interval(p,u);oc,od=interval(p,v)
        if abs((nc+nd-oc-od)/2)>tolerance:continue
        overlap=max(0,min(nb,ob)-max(na,oa))
        if overlap/min(length,old_length)<.8:continue
        anchors={'start':abs(na-oa),'end':abs(nb-ob),'center':abs((na+nb-oa-ob)/2)}
        if min(anchors.values())>tolerance:continue
        if p.intersection(new).area<=max(.1,min(p.area,new.area)*.5):continue
        candidates.append((entity,{'entity_id':entity['entity_id'],'group_id':entity['group_id'],
            'original_length_mm':old_length,'new_length_mm':length,'thickness_mm':width,
            'centerline_offset_mm':abs((nc+nd-oc-od)/2),'axis_overlap_mm':overlap,
            'anchor_distances_mm':anchors,'linear_tolerance_mm':tolerance}))
    if len(candidates)!=1:
        raise _error('定位比對沒有唯一牆體候選；請明選 SKP 目標。候選：'+
                     json.dumps([row for _,row in candidates],ensure_ascii=False))
    entity,audit=candidates[0]
    return [entity],{'basis':'shared_world_coordinates_unique_parallel_same_thickness_anchored_wall',
                     'candidate_count':1,'match':audit}


def validate_current_selection(plan,current_selection):
    observation=plan.get('source',{}).get('sync_selection')
    if observation is not None:
        if current_selection is None:raise _error('套用同步計畫前，必須重新讀取目前 CAD 選取。')
        require_fresh_selection(current_selection)
        _selection_errors(current_selection,{'intake':plan['source']['manifest']})
        try:
            original_time=datetime.fromisoformat(observation['selection_snapshot']['observed_at_utc'].replace('Z','+00:00'))
            current_time=datetime.fromisoformat(current_selection['observed_at_utc'].replace('Z','+00:00'))
            if original_time.tzinfo is None or current_time<=original_time:
                raise ValueError('not a later observation')
        except (KeyError,TypeError,ValueError,AttributeError):
            raise _error('套用同步計畫必須使用規劃後重新讀取的 CAD 選取，不能重用原快照。')
        if selection_geometry_signature(current_selection)!=observation.get('geometry_signature'):
            raise _error('CAD 完整選取在規劃後已改變；請重新規劃。')
    revision=plan.get('source',{}).get('live_cad_revision')
    if revision is None:return
    if current_selection is None:raise _error('套用 CAD 改後輪廓前，必須重新讀取目前 CAD 選取。')
    require_fresh_selection(current_selection)
    manifest=plan['source']['manifest']
    if revision.get('kind')=='wall_system':
        from .live_wall_system import prepare_wall_system
        current=prepare_wall_system({'intake':manifest},current_selection,revision['scope'])['revision']
        if current['geometry_signature']!=revision['geometry_signature'] or current['footprint']!=revision['footprint']:
            raise _error('CAD 牆系／門洞／參考選取在規劃後已改變；請重新規劃。')
        return
    current=live_regions({'intake':manifest},current_selection)[0]['live_cad_revision']
    if (current['geometry_signature']!=revision['geometry_signature'] or
        current['source_entity_id']!=revision['source_entity_id'] or
        current['footprint']!=revision['footprint']):
        raise _error('CAD 輪廓／圖層／選取在規劃後已改變；請重新規劃。')
