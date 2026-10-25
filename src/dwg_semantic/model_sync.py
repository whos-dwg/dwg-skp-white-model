"""Source-bound whole-model difference planning; one shared native transaction.

The immutable intake is the base, the complete live CAD database is an overlay.
Existing approved material cuts and door/window Z clearances remain recipes.
Unchanged native groups are never replaced merely to refresh bookkeeping.
"""
from __future__ import annotations
from copy import deepcopy
import json
from pathlib import Path
from functools import lru_cache
from datetime import datetime,timezone
from shapely.geometry import Polygon,GeometryCollection
from shapely.ops import unary_union,polygonize

from .live_repair import (LiveRepairError,_digest,_validate_snapshot,_fixed_live_standard,
    _actual_footprint,_provenance,_target_row,_create_row,_fit_fixed_live_create,
    _overlap_blockers,_polygon_payload,_payload_polygon,_parts,_entity_lines,seal_live_repair_plan)
from .modeling import model_entities
from .plan_geometry import source_plane,hatch_profiles,shape_of,layer_of
from .cad_model import normalized_model,validate_model_snapshot
from .selection_openings import _path_key

AREA=.1

def _fail(message):raise LiveRepairError(message)

def _state_time(state):
    try:return datetime.fromisoformat(state.get('observed_at_utc','1970-01-01T00:00:00+00:00').replace('Z','+00:00')).astimezone(timezone.utc)
    except (ValueError,TypeError):_fail('構件來源修訂基線時間無效。')

def _historical_sources(snapshot,old,current):
    """Keep deletions traceable after an earlier unsaved add/local revision."""
    archived={}
    present_handles={str(e.get('source_handle',e.get('handle','')))for e in (*old.values(),*current.values())}
    for row in snapshot['entities']:
        proof=_provenance(row);state=proof.get('model_sync_source_state')or{}
        for key,e in {**state.get('entities',{}),**state.get('opening_entities',{})}.items():
            if key in old or key in current:continue
            if key not in archived or _state_time(state)>archived[key][0]:archived[key]=(_state_time(state),deepcopy(e))
        revision=proof.get('cad_live_revision')or{}
        for obj in revision.get('selection_snapshot',{}).get('objects',[]):
            h=str(obj.get('handle',''))
            if not h or h in present_handles:continue
            key='cad-live:'+str(revision.get('base_source_sha256',''))[:16]+':'+h+':'+str(revision.get('geometry_signature',''))[:20]
            archived.setdefault(key,(_state_time({}),{'entity_id':key,'source_handle':h,'layer':obj.get('properties',{}).get('Layer'),
                'type':obj.get('object_type'),'historical_live_selection':True}))
    return {**old,**{k:e for k,(_,e)in archived.items()}}

def _index(document):
    rows=model_entities(document);result={}
    for e in rows:
        key=e.get('entity_id')
        if not key or key in result:_fail('CAD 整模有缺少或重複的來源實例 ID。')
        result[key]=e
    # Resolved INSERT containers are not material primitives, but their own
    # source identity remains necessary to track a bound opening symbol.
    containers=[e for e in document.get('entities',[])if e.get('owner_block')in ('*Model_Space','Model','model','MODEL_SPACE')]
    containers += [e for e in document.get('resolved_instances',[])if e.get('root_space','*Model_Space')=='*Model_Space']
    for e in containers:
        if e.get('type')in ('INSERT','BLOCK_REFERENCE')and e.get('entity_id')not in result:
            result[e['entity_id']]={**deepcopy(e),'_sync_container_evidence_only':True}
    return result

def _source_refs(proof,old,current):
    available={**old,**current};found={x for x in proof.get('source_entity_ids',[])if x in available}
    handles=set(map(str,proof.get('source_handles',[])))
    for h in handles:
        if any(str(available[k].get('source_handle',available[k].get('handle','')))==h for k in found):continue
        matches={k for k,e in available.items()if str(e.get('source_handle',e.get('handle','')))==h}
        if len(matches)!=1:_fail('來源 handle 沒有唯一實例對應，不能用最近位置猜測：'+h)
        found.update(matches)
    if not found:_fail('白模構件缺少可追溯的來源實例。')
    return found

def _profile(entries):
    """Existing normalized curve topology; never a bounding-box footprint."""
    if not entries:return GeometryCollection(),None
    planes=[source_plane(e) for e in entries]
    if any(p['nonplanar'] or p['plane_z_mm'] is None for p in planes):_fail('整模來源有非共面構件，不能投影猜測。')
    zs={round(p['plane_z_mm'],6) for p in planes}
    if len(zs)!=1:_fail('同一來源構件跨不同 Z 平面；需要明確的構件分層。')
    lines=[];hatches=[]
    for e in entries:
        if e.get('unsupported'):_fail('構件包含不支援的 CAD 幾何。')
        lines.extend(_entity_lines(e))
        if e.get('type')=='HATCH':
            profiles,issues=hatch_profiles(e)
            if issues:_fail('HATCH 來源邊界不完整。')
            hatches.extend(shape_of(p) for p in profiles)
    faces=list(polygonize(unary_union(lines))) if lines else []
    rings=[Polygon(p.exterior) for p in faces]
    material=[face for face,ring in zip(faces,rings)if sum(other.area>ring.area+AREA and other.covers(ring)for other in rings)%2==0]
    result=unary_union(material+hatches)
    if result.is_empty or not result.is_valid or result.area<=AREA:_fail('來源構件未形成可靠的封閉材料輪廓；不能以外接框補建。')
    return result,next(iter(zs))

def _families(snapshot,old,current):
    result=[]
    for row in snapshot['entities']:
        refs=_source_refs(_provenance(row),old,current)
        overlaps=[i for i,(_,keys)in enumerate(result)if keys&refs]
        members=[row]
        for i in reversed(overlaps):
            others,keys=result.pop(i);members+=others;refs|=keys
        result.append((members,refs))
    return sorted(result,key=lambda r:sorted(r[1]))

def _tag(layer,snapshot):
    choices={m['tag']for m in snapshot['root'].get('tag_mapping',[])or[]if m.get('source_layer')==layer}
    if len(choices)>1:_fail('來源圖層有多個 Tag 對照：'+layer)
    if choices:return next(iter(choices))
    if layer.casefold() in ('0','layer0','untagged'):_fail('保留圖層名稱缺少可逆 Tag 對照。')
    return layer

@lru_cache(maxsize=1)
def _rules():
    return json.loads((Path(__file__).with_name('config')/'default_rules.json').read_text(encoding='utf-8'))

def _category(layer,snapshot):
    from .classifier import _is_layer,_matches
    rules=_rules()
    choices={r['category']for r in snapshot['entities']if _provenance(r).get('source_layer')==layer}
    if len(choices)==1:return next(iter(choices))
    if _is_layer(layer,'curtain_accessory',rules):return 'curtain_accessory'
    if _is_layer(layer,'structural',rules):return 'column' if _matches(layer,['column','col','柱'])else 'structural_wall'
    if _is_layer(layer,'curtain',rules):return 'curtain'
    if _is_layer(layer,'architectural_wall',rules):return 'architectural_wall'
    return None

def _parent(category,snapshot):
    found=[r['entity_id']for r in snapshot.get('containers',[])if r['name']==category]
    if len(found)>1:_fail('分類容器不唯一：'+category)
    return found[0] if found else 'new-category:'+category

def _recipe(row):
    proof=deepcopy(_provenance(row))
    if not proof.get('outer') and proof.get('footprint'):proof.update(proof['footprint'])
    if not proof.get('outer'):_fail('構件缺少已綁定的原始輪廓 recipe：'+row['group_id'])
    p=_payload_polygon(proof)
    lo=float(proof.get('base_z_mm',row['base_z_mm']));height=float(proof.get('height_mm',row['height_mm']))
    if height<=0:_fail('來源構件 recipe 高度無效。')
    return p,lo,lo+height

def _prisms(slices):
    """Partition XY once; merge identical occupancy intervals vertically.

    This keeps sides full height, actual lintels above clear openings, and
    source-bound sills below them rather than filling holes or adding overlaps.
    """
    shapes=[p for _,_,p in slices if not p.is_empty]
    if not shapes:return []
    cells=list(polygonize(unary_union([p.boundary for p in shapes])))
    if len(cells)>50000:_fail('整模差異拓樸過大，無法完整驗證。')
    intervals={}
    for cell in cells:
        spans=[]
        for lo,hi,shape in slices:
            if shape.intersection(cell).area>=cell.area-max(1e-6,cell.area*1e-9):
                if spans and abs(spans[-1][1]-lo)<1e-6:spans[-1]=(spans[-1][0],hi)
                else:spans.append((lo,hi))
        for span in spans:intervals.setdefault(span,[]).append(cell)
    return [(lo,hi,p)for (lo,hi),cells in sorted(intervals.items())for p in _parts(unary_union(cells))]

def _equal(row,candidate):
    return (row['category']==candidate['category'] and row['tag']==candidate['tag'] and
        abs(row['base_z_mm']-candidate['base_z_mm'])<.000001 and
        abs(row['height_mm']-candidate['height_mm'])<.000001 and
        _actual_footprint(row).symmetric_difference(_payload_polygon(candidate['footprint'])).area<=AREA and
        row.get('manifold') is True and row.get('volume_mm3') is not None and
        abs(row['volume_mm3']-candidate['mesh']['expected_volume_mm3'])<=max(1,candidate['mesh']['expected_volume_mm3']*1e-7))

def _desired(members,keys,old,current,snapshot,document,standard,observed_at_utc):
    """Recompute native CAD material while keeping the approved vertical cuts."""
    existing=[current[k]for k in sorted(keys)if k in current]
    if not existing:return []
    proofs=[_provenance(m)for m in members]
    categories={m['category']for m in members};materials={m.get('material')for m in members}
    if len(categories)!=1 or len(materials)!=1:_fail('來源家族跨不同分類或材質；不能自動合併其語意。')
    primary={p.get('source_layer') for p in proofs}-{None,''}
    # A recorded wall-system role map separates material boundaries from 2D
    # opening evidence. Preserve the map instead of treating symbols as walls.
    saved=[p.get('model_sync_source_state')for p in proofs if p.get('model_sync_source_state')]
    if saved:
        latest=max(saved,key=_state_time)
        if any(s!=latest and _state_time(s)==_state_time(latest)for s in saved):_fail('同一家族的最新來源修訂基線不一致。')
        saved=[latest]
    scope=next((p.get('cad_live_revision',{}).get('scope')or p.get('model_sync_source_state',{}).get('wall_system_scope')
        for p in proofs if p.get('cad_live_revision',{}).get('scope')or p.get('model_sync_source_state',{}).get('wall_system_scope')),None)
    role_maps=[(scope or {}).get('roles',{})]
    roles={h:r for mapping in role_maps for h,r in mapping.items()}
    opening_keys={x for p in proofs for x in p.get('selection_opening_source_entity_ids',[])}
    prior_entities=saved[0]['entities']if saved else old
    if any(p.get('opening_ids')for p in proofs):
        for key in keys-opening_keys:
            previous=prior_entities.get(key);latest=current.get(key)
            if previous==latest:continue
            source=previous or latest or {}
            role=roles.get(str(source.get('source_handle')))
            # Two-dimensional jamb/symbol edits cannot supply new vertical
            # clearances. Preserve approved cuts and require explicit pairing.
            if (role in ('door_jamb','opening_edge','opening_symbol') or
                (not roles and (layer_of(source)not in primary or source.get('type')in ('LINE','INSERT')))):
                _fail('既有門窗牆系的側界、洞口邊或符號已變更；需以明確開口範圍重新核對淨空，不能沿用舊洞。')
    if roles:
        existing=[e for e in existing if roles.get(str(e.get('source_handle'))) in ('outer_boundary','inner_boundary','door_jamb','duplicate_boundary_reference')]
    elif len(primary)==1:
        main=next(iter(primary));filtered=[e for e in existing if layer_of(e)==main]
        if filtered:existing=filtered
    new_shape,new_z=_profile(existing)
    revision=next((p.get('cad_live_revision')for p in proofs if p.get('cad_live_revision')),None)
    baseline=[old[k]for k in sorted(keys)if k in old]
    if roles:baseline=[e for e in baseline if roles.get(str(e.get('source_handle')))in ('outer_boundary','inner_boundary','door_jamb','duplicate_boundary_reference')]
    elif len(primary)==1:
        filtered=[e for e in baseline if layer_of(e)==next(iter(primary))]
        if filtered:baseline=filtered
    if saved:
        old_shape=unary_union([_payload_polygon(p)for p in saved[0]['material']]);old_z=saved[0]['plane_z_mm']
    elif revision:
        old_shape=_payload_polygon(revision.get('native_footprint')or revision['footprint']);old_z=revision['source_plane_z_mm']
    else:old_shape,old_z=_profile(baseline)
    recipes=[_recipe(m)for m in members];bottom=min(lo for _,lo,_ in recipes);top=max(hi for _,_,hi in recipes)
    dz=new_z-old_z
    layers={layer_of(e)for e in existing}
    if len(layers)!=1:_fail('構件材料來源圖層不唯一，不能任選 Tag。')
    layer=next(iter(layers));category=next(iter(categories))
    if primary and layer not in primary:
        category=_category(layer,snapshot)
        if category is None:_fail('變更後圖層沒有可證明的白模分類：'+layer)
    template=deepcopy(proofs[0]);template.update(id=members[0]['group_id'],category=category,tag=_tag(layer,snapshot),
        source_layer=layer,source_layers=[layer],source_entity_ids=sorted(keys),source_handles=sorted({str(e['source_handle'])for e in existing}),
        base_z_mm=bottom+dz,height_mm=top-bottom,**_polygon_payload(new_shape) if isinstance(new_shape,Polygon) else {})
    # Fit each full source domain to actual protected soffits before subtracting
    # approved openings. Per-slice roof fitting would incorrectly fill windows.
    full=[]
    for n,p in enumerate(_parts(new_shape)):
        group=deepcopy(template);group.update(id=members[0]['group_id']+'/domain/'+str(n),**_polygon_payload(p))
        for k in ('opening_part_role','opening_ids','semantic_role'):group.pop(k,None)
        row=_create_row(group,members[0],_parent(category,snapshot))
        full.extend(_fit_fixed_live_create([row],snapshot,standard,'replace_from_cad',document)if standard else [row])
    source_state={'material':[_polygon_payload(p)for p in _parts(new_shape)],'plane_z_mm':new_z,'observed_at_utc':observed_at_utc,
        'entities':{k:deepcopy(current[k])for k in sorted(keys)if k in current}}
    if scope:source_state['wall_system_scope']=deepcopy(scope)
    prior_openings=(saved[0].get('opening_entities',{})if saved else {})
    source_state['opening_entities']={k:deepcopy(current[k])for k in sorted(opening_keys)if k in current}
    cuts=sorted({bottom,top,*[z for _,lo,hi in recipes for z in (lo,hi)]})
    desired=[]
    for domain in full:
        p=_payload_polygon(domain['footprint']);lo=domain['base_z_mm'];hi=lo+domain['height_mm']
        zs=sorted({lo,hi,*[z+dz for z in cuts if lo<z+dz<hi]});slices=[]
        for a,b in zip(zs,zs[1:]):
            sample=(a+b)/2-dz
            occupied=unary_union([shape for shape,l,h in recipes if l-1e-6<=sample<=h+1e-6])
            # Above a former short wall the standard extends material to soffit.
            if sample>=top:occupied=old_shape
            void=old_shape.difference(occupied)
            for key in opening_keys:
                original=prior_openings.get(key)or old.get(key)
                if original is None:_fail('既有開口缺少來源輪廓基線。')
                old_cut,_=_profile([original]);covered=void.intersection(old_cut)
                if covered.area>AREA:
                    if old_shape.intersection(old_cut).difference(void).area>AREA:_fail('開口淨空的垂直角色不完整。')
                    void=void.difference(old_cut)
                    if key in current:
                        new_cut,_=_profile([current[key]]);void=void.union(new_cut)
            slices.append((a,b,p.difference(void)))
        desired.extend(_prisms(slices))
    result=[];used=set()
    for lo,hi,p in desired:
        candidates=[m for m,(_,a,b)in zip(members,recipes)if abs(a+dz-lo)<1e-6 and abs(b+dz-hi)<1e-6]
        exact=[m for m in candidates if _actual_footprint(m).symmetric_difference(p).area<=AREA]
        preferred=(exact or candidates or members)[0]
        group=deepcopy(_provenance(preferred));identity=preferred['group_id']
        if identity in used:identity='model-sync:'+_digest([sorted(keys),lo,hi,_polygon_payload(p)])[:24]
        used.add(identity)
        group.update(id=identity,name=preferred.get('name')or identity,category=category,tag=template['tag'],
            source_layer=layer,source_layers=[layer],source_entity_ids=sorted(keys),source_handles=source_state['entities'] and sorted({str(e['source_handle'])for e in source_state['entities'].values()}),
            base_z_mm=lo,height_mm=hi-lo,area_mm2=p.area,volume_mm3=p.area*(hi-lo),**_polygon_payload(p),model_sync_source_state=deepcopy(source_state),
            live_repair_replaced_group_ids=[preferred['group_id']],geometry_basis='complete_live_cad_source_bound_difference')
        group.pop('cad_live_revision',None)
        result.append(_create_row(group,preferred,_parent(category,snapshot)))
    return result

def _new_rows(entries,snapshot,document,standard,observed_at_utc):
    grouped={};ignored=[]
    for e in entries:
        if e.get('_sync_container_evidence_only'):ignored.append(e['entity_id']);continue
        category=_category(layer_of(e),snapshot)
        if category is None:ignored.append(e['entity_id']);continue
        if str(e.get('type')) in ('TEXT','MTEXT','ATTRIB','ATTDEF','DIMENSION','POINT'):ignored.append(e['entity_id']);continue
        plane=source_plane(e)
        if plane['nonplanar']:_fail('新構件來源不是共面輪廓。')
        grouped.setdefault((layer_of(e),category,plane['plane_z_mm']),[]).append(e)
    output=[]
    for (layer,category,z),items in sorted(grouped.items()):
        material,_=_profile(items)
        templates=[r for r in snapshot['entities']if r['category']==category]
        specific=[r for r in templates if _provenance(r).get('source_layer')==layer]
        templates=specific or templates
        heights={r['height_mm']for r in templates if not _provenance(r).get('opening_ids')}
        if not standard and len(heights)!=1:_fail('補建新構件缺少唯一的既有分類高度或實際板底：'+layer)
        for p in _parts(material):
            keys=sorted(e['entity_id']for e in items);identity='model-sync:'+_digest([keys,_polygon_payload(p)])[:24]
            group={'id':identity,'name':identity,'category':category,'tag':_tag(layer,snapshot),
                'source_layer':layer,'source_layers':[layer],'source_entity_ids':keys,
                'source_handles':sorted({e['source_handle']for e in items}),**_polygon_payload(p),
                'base_z_mm':z,'height_mm':next(iter(heights)) if heights else 1,'mode':'solid_extrusion',
                'area_mm2':p.area,'volume_mm3':p.area*(next(iter(heights))if heights else 1),
                'model_sync_source_state':{'material':[_polygon_payload(q)for q in _parts(material)],'plane_z_mm':z,'observed_at_utc':observed_at_utc,
                    'entities':{e['entity_id']:deepcopy(e)for e in items},'opening_entities':{}},
                'geometry_basis':'new_complete_live_cad_source_contour','evidence':['complete_live_cad_model_read','existing_category_height_or_actual_protected_soffit']}
            row=_create_row(group,templates[0]if templates else None,_parent(category,snapshot))
            output.extend(_fit_fixed_live_create([row],snapshot,standard,'add_missing',document)if standard else [row])
    return output,ignored

def build_model_sync_plan(document,cad_model,snapshot,*,slab_decisions=None):
    _validate_snapshot(snapshot);standard=_fixed_live_standard(snapshot)
    manifest=document['intake'];bound=snapshot['root'].get('source_manifest',{})
    if (bound.get('source_sha256')!=manifest.get('source_sha256') or _path_key(bound.get('source_path'))!=_path_key(manifest.get('source_path'))or bound.get('dependencies',[])!=manifest.get('dependencies',[])):
        _fail('整模同步的 CAD／SKP 來源與參照綁定不一致。')
    proof=snapshot.get('global_read')or{}
    if proof.get('schema')!='dwg-skp-global-read-v1' or len(proof.get('inventory_sha256',''))!=64:_fail('缺少完整 SKP 幾何與參考清單的漂移證據。')
    signature=validate_model_snapshot(cad_model,document);live=normalized_model(cad_model,document)
    old,current=_index(document),_index(live)
    old=_historical_sources(snapshot,old,current)
    from .slab_sync import plan_slabs,overlap_blockers
    slabs=plan_slabs(live,snapshot,old,current,cad_model['observed_at_utc'],slab_decisions)
    working=deepcopy(snapshot)
    working['protected_entities']=[r for r in snapshot.get('protected_entities',[])if r['category']!='slab']+slabs['post']
    families=_families(snapshot,old,current);claimed=set(slabs['claimed']);targets=list(slabs['targets']);create=list(slabs['create']);unchanged=list(slabs['unchanged']);audit=list(slabs['audit'])
    owned={k for _,keys in families for k in keys}
    bound_openings={k for members,_ in families for m in members for k in _provenance(m).get('selection_opening_source_entity_ids',[])}
    owned|=bound_openings
    # A new native closed inner ring on the same source layer and instance is
    # a topological material boundary, not an independent solid inside a wall.
    # Require a unique containing source family; never rank by distance.
    from .selection_openings import _entity_polygon
    grafts={}
    for key,e in current.items():
        if key in owned or key in slabs['claimed']:continue
        inner=_entity_polygon(e)
        if inner is None:continue
        matches=[]
        for i,(members,keys)in enumerate(families):
            if members[0]['category']not in ('architectural_wall','structural_wall'):continue
            natives=[current[k]for k in keys if k in current]
            for outer in natives:
                p=_entity_polygon(outer)
                if (p is not None and p.area>inner.area+AREA and p.contains(inner) and p.boundary.disjoint(inner.boundary) and
                    layer_of(outer)==layer_of(e) and outer.get('source_file')==e.get('source_file') and
                    outer.get('instance_path','')==e.get('instance_path','') and
                    source_plane(outer)['plane_z_mm']==source_plane(e)['plane_z_mm']):matches.append(i);break
        if len(matches)>1:_fail('新增內圈同時落在多個來源家族，不能推測材料歸屬。')
        if matches:grafts.setdefault(matches[0],set()).add(key)
    families=[(members,keys|grafts.get(i,set()))for i,(members,keys)in enumerate(families)]
    # Unsupported/protected objects remain immutable; changed source evidence
    # must block rather than silently claim the complete model is synchronized.
    protected_keys=set()
    for row in snapshot.get('protected_entities',[]):
        if row['category']=='slab':continue
        keys=_source_refs(_provenance(row),old,current);protected_keys|=keys
        if any(old.get(k)!=current.get(k)for k in keys):_fail('受保護樓板或未支援構件的 CAD 來源已變更；需專屬修訂流程。')
    for members,keys in families:
        claimed|=keys
        desired=_desired(members,keys,old,current,working,live,standard,cad_model['observed_at_utc'])
        remaining=list(members);pending=[]
        for row in desired:
            matches=[m for m in remaining if _equal(m,row)]
            if len(matches)>1:_fail('多個原生構件同時符合相同差異候選，來源實例不唯一。')
            if matches:remaining.remove(matches[0]);unchanged.append(matches[0]['entity_id'])
            else:pending.append(row)
        retained_ids={m['group_id']for m in members if m not in remaining};assigned=set(retained_ids)
        for row in pending:
            if row['group_id']in assigned:
                identity='model-sync:'+_digest([sorted(keys),row['base_z_mm'],row['height_mm'],row['footprint']])[:24]
                row['group_id']=identity;row['provenance']['id']=identity
            assigned.add(row['group_id'])
        targets.extend(_target_row(m)for m in remaining);create+=pending
        audit.append({'source_entity_ids':sorted(keys),'original_group_ids':[m['group_id']for m in members],
            'unchanged_count':len(members)-len(remaining),'remove_count':len(remaining),'create_count':len(pending)})
    deleted_openings=[m for members,keys in families if not any(k in current for k in keys)for m in members
        if _provenance(m).get('opening_ids')]
    for e in current.values():
        if e['entity_id']in claimed|protected_keys:continue
        p=_entity_polygon(e)
        if p is not None and any(p.intersection(_actual_footprint(m)).area>AREA for m in deleted_openings):
            _fail('重新繪製的新來源與已刪除的門窗牆家族交會；缺少來源接續證據，不能填回開口。')
    unclaimed=[e for k,e in current.items()if k not in claimed|protected_keys|bound_openings]
    from .classifier import _is_layer
    if any(_is_layer(layer_of(e),'opening',_rules())and old.get(e['entity_id'])!=e for e in unclaimed):
        _fail('新增或變更的未綁定門窗來源缺少宿主與淨空證據；請先用局部開口同步確認。')
    new,ignored=_new_rows(unclaimed,working,live,standard,cad_model['observed_at_utc'])
    create+=new
    blockers=overlap_blockers(create,snapshot,{t['entity_id']for t in targets})if slabs['targets']or slabs['create']else _overlap_blockers(create,snapshot,{t['entity_id']for t in targets})
    if blockers:_fail('整模差異更新有構件或受保護樓板碰撞：'+json.dumps(blockers,ensure_ascii=False))
    ids=[r['group_id']for r in create]
    if len(set(ids))!=len(ids):_fail('整模更新結果有重複構件 ID。')
    new_categories=sorted({r['category']for r in create if r['parent_entity_id'].startswith('new-category:')})
    return seal_live_repair_plan({'schema':'dwg-skp-live-repair-plan-v1','status':'ready_to_apply'if targets or create else'up_to_date',
        'action':'synchronize_model','slab_sync_version':'1.0','modeling_standard':standard,'fixed_standard_audit':deepcopy(snapshot['root'].get('fixed_standard_audit')),
        'model':{'guid':snapshot['model']['guid'],'path':snapshot['model']['path'],'root_entity_id':snapshot['root']['entity_id'],
            'root_payload_sha256':snapshot['root'].get('payload_sha256'),'global_read':deepcopy(proof),
            'snapshot_context':{'root':deepcopy(snapshot['root']),'containers':deepcopy(snapshot.get('containers',[])),
                'entities':deepcopy(snapshot['entities']),'protected_entities':deepcopy(snapshot.get('protected_entities',[]))}},
        'source':{'path':manifest['source_path'],'sha256':manifest['source_sha256'],'manifest':deepcopy(manifest),
            'sync_model':{'schema':'dwg-cad-model-observation-v1','geometry_signature':signature,'document':deepcopy(cad_model['document']),
                'observed_at_utc':cad_model['observed_at_utc'],'selection_count':0},'items':audit},
        'targets':targets,'create':create,'create_containers':[{'entity_id':'new-category:'+c,'name':c,'parent_entity_id':snapshot['root']['entity_id']}for c in new_categories],
        'difference_summary':{'unchanged_count':len(unchanged),'unchanged_entity_ids':unchanged,'remove_count':len(targets),
            'create_count':len(create),'reference_only_source_count':len(ignored),'reference_only_source_ids':sorted(ignored),'family_audit':audit},
        'write_policy':{'active_model_only':True,'create_new_skp':False,'save_model':False,'single_undo_operation':True,'verify_before_commit':True}})
