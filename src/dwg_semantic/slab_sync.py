"""Source-bound whole slab delta planning using the existing slab engine."""
from copy import deepcopy
import math
from shapely.geometry import Polygon
from shapely.ops import unary_union
from .live_repair import LiveRepairError,_digest,_polygon_payload,_payload_polygon,_mesh
from .mesh_union import section_from_triangles,shape,topology

VERSION='1.0'
def fail(message):raise LiveRepairError(message)

def boundary_mesh(group):
    """Triangulate plane loops without filling holes; retain the source faces."""
    from shapely import constrained_delaunay_triangles
    vertices=[];faces=[];lookup={}
    for face in group['faces']:
        normal=face['normal'];drop=max(range(3),key=lambda i:abs(normal[i]));axes=[i for i in range(3)if i!=drop]
        if abs(normal[drop])<1e-10:fail('樓板面缺少可信法向。')
        project=lambda ring:[[p[i]for i in axes]for p in ring]
        p=Polygon(project(face['outer']),[project(h)for h in face.get('holes',[])])
        if not p.is_valid or p.area<=0:fail('樓板邊界面無效。')
        ts=list(constrained_delaunay_triangles(p).geoms)
        if unary_union(ts).symmetric_difference(p).area>.001:fail('樓板孔洞三角化不完整。')
        origin=face['outer'][0]
        def vertex(q):
            xyz=[0.,0.,0.];xyz[axes[0]],xyz[axes[1]]=q
            xyz[drop]=origin[drop]-sum(normal[i]*(xyz[i]-origin[i])for i in axes)/normal[drop]
            key=tuple(round(v,6)for v in xyz)
            if key not in lookup:lookup[key]=len(vertices);vertices.append(list(key))
            return lookup[key]
        for t in ts:
            ids=[vertex(q)for q in list(t.exterior.coords)[:-1]]
            a,b,c=[vertices[i]for i in ids];u=[b[i]-a[i]for i in range(3)];v=[c[i]-a[i]for i in range(3)]
            n=[u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0]]
            if sum(n[i]*normal[i]for i in range(3))<0:ids.reverse()
            faces.append(ids)
    return {'coordinate_space':'model_mm','vertices':vertices,'faces':faces,'expected_volume_mm3':group['volume_mm3']}

def triangles(row):
    if row.get('triangles_mm')is not None:return row['triangles_mm']
    mesh=row.get('mesh')
    if mesh is None:
        from .live_repair import _actual_footprint
        mesh=_mesh(_actual_footprint(row),row['base_z_mm'],row['height_mm'])
    return [[mesh['vertices'][f[i]]for i in (0,n,n+1)]for f in mesh['faces']for n in range(1,len(f)-1)]

def sections_equal(actual,desired):
    if not actual.get('manifold')or not actual.get('triangles_mm'):return False
    if actual.get('tag')!=desired['tag']:return False
    from .whole_slab_qa import verify_saved_whole
    return verify_saved_whole(desired['provenance'],actual['triangles_mm']).get('all_pass')is True

def _geometry_ids(record):return sorted(set(record.get('source_entity_ids',[]))-set(record.get('annotation_source_ids',[])))

def recover(row):
    """Recover the reviewed source recipe, never the current edited mesh."""
    g=row['provenance'];saved=g.get('model_sync_slab_state')
    if saved:return deepcopy(saved['config'])
    recipe=g.get('evidence',{}).get('construction_recipe')if isinstance(g.get('evidence'),dict)else None
    if recipe and recipe.get('mode')=='whole_uniform_thickness':
        audits=recipe['gross_source_audit']['systems'];sys=next(r for r in audits if r['id']==g['slab_system_id'])
        audit=sys['plan_audit'];base=audit.get('base_region')
        if base is None:fail('樓板原始一般區標高未保留；請提供來源綁定的樓板決策。')
        def region(r):return {k:deepcopy(r[k])for k in ('id','footprint','source_entity_ids','annotation_source_ids','top_z_mm','thickness_mm')if k in r}
        system={'id':sys['id'],'role':sys['system_role'],'extent':deepcopy(audit['extent']),
            'source_entity_ids':deepcopy(sys['source_entity_ids']),'annotation_source_ids':deepcopy(sys['annotation_source_ids']),
            'default_top_z_mm':base['top_z_mm'],'default_thickness_mm':base['thickness_mm'],
            'zones':[region(r)for r in audit['zones']],
            'openings':[dict(region(o),applies_to_system_id=sys['id'])for o in recipe['source_openings']],
            'tag_binding':{'kind':sys['tag_kind'],'tag':sys['tag'],'source_layer':sys['source_layer']},
            'evidence':deepcopy(sys['evidence'])}
    elif g.get('mode')=='solid_extrusion'and g.get('slab_role')in ('floor_slab','overhead_slab'):
        system={'id':g.get('slab_system_id')or g['id'],'role':'overhead'if g['slab_role']=='overhead_slab'else'floor',
            'extent':{k:deepcopy(g.get(k,[]))for k in ('outer','holes')},'source_entity_ids':deepcopy(g.get('source_entity_ids',[])),
            'annotation_source_ids':deepcopy(g.get('annotation_source_ids',[])),
            'default_top_z_mm':g['base_z_mm']+g['height_mm'],'default_thickness_mm':g['height_mm'],'zones':[],'openings':[],
            'tag_binding':{'kind':g.get('tag_kind','source'),'tag':g['tag'],'source_layer':g['source_layer']},'evidence':deepcopy(g.get('evidence',[]))}
    else:fail('受保護樓板缺少可重建的整片來源 recipe；不以舊網格推測降板或孔洞。')
    return {'enabled':True,'confirmed_scope':True,'assembly_mode':'whole_uniform_thickness',
        'floor_datum_m':g.get('floor_datum_m',0),'source_tag_policy':'effective','systems':[system]}

def _refresh_record(record,field,old,current,*,allow_deleted=False,bound_annotations=()):
    from .model_sync import _profile
    keys=_geometry_ids(record)
    if not keys:fail('樓板區界缺少明確來源識別碼。')
    present=[current[k]for k in keys if k in current]
    if not present:
        if allow_deleted:return None
        fail('樓板區界的部分來源已刪除，無法證明完整邊界。')
    if len(present)!=len(keys):fail('樓板區界只剩部分來源；不能用不完整線網補界。')
    changed=any(old.get(k)!=current.get(k)for k in keys)
    previous=deepcopy(record)
    if changed:
        before,z0=_profile([old[k]for k in keys if k in old]);after,z1=_profile(present)
        approved=_payload_polygon(record[field]);outer_only=False
        if before.symmetric_difference(approved).area>.1:
            if isinstance(before,Polygon)and Polygon(before.exterior).symmetric_difference(approved).area<=.1:outer_only=True
            else:fail('來源板界與已核定推導邊界不能唯一對應；需確認本次精確區界。')
        if outer_only:
            if not isinstance(after,Polygon):fail('樓板來源外框已分裂。')
            after=Polygon(after.exterior)
        if not isinstance(after,Polygon):fail('樓板區界分成多個不連通區，需分開來源系統。')
        record[field]=_polygon_payload(after)
        dz=z1-z0
        if 'default_top_z_mm'in record:record['default_top_z_mm']+=dz
        if 'top_z_mm'in record:record['top_z_mm']+=dz
    for k in previous.get('annotation_source_ids',[]):
        if old.get(k)!=current.get(k)and k not in bound_annotations:fail('樓板標高／厚度註記已變更，缺少已綁定的數值解釋；請核對標高與厚度。')
    return record

def _refresh(config,old,current):
    config=deepcopy(config);system=config['systems'][0]
    bound={k for b in config.get('annotation_bindings',[])for k in [b.get('source_entity_id'),*b.get('reference_annotation_source_ids',[])]}
    if not any(k in current for k in _geometry_ids(system)):return None
    _refresh_record(system,'extent',old,current,bound_annotations=bound)
    for field in ('zones','openings'):
        system[field]=[r for item in system.get(field,[])if(r:=_refresh_record(item,'footprint',old,current,allow_deleted=True,bound_annotations=bound))is not None]
    for region in config.get('functional_regions',[]):_refresh_record(region,'footprint',old,current,bound_annotations=bound)
    return config

def _row(group,template,parent):
    from .model_sync import _tag
    mesh=boundary_mesh(group)
    return {'group_id':group['id'],'name':group.get('name',group['id']),'category':'slab','tag':group['tag'],
        'material':template.get('material')if template else'DWG white model','parent_entity_id':parent,
        'footprint':{'outer':group['outer'],'holes':group['holes']},'base_z_mm':group['base_z_mm'],'height_mm':group['height_mm'],
        'mesh':mesh,'provenance':deepcopy(group)}

def plan_slabs(document,snapshot,old,current,observed,config=None):
    """Return slab changes plus the post-sync roofs used by wall fitting."""
    from .slab_modeling import prepare_slabs
    from .modeling import validate_slab_payload
    from .model_sync import _source_refs,_parent
    existing=[r for r in snapshot.get('protected_entities',[])if r['category']=='slab']
    result={'targets':[],'create':[],'claimed':set(),'unchanged':[],'post':[],'audit':[]}
    systems={};configs=[]
    for row in existing:
        identity=row['provenance'].get('slab_system_id')or row['group_id']
        if identity in systems:fail('同一樓板系統包含多個實體；先以既有整片流程核對單殼來源。')
        systems[identity]=row
        try:cfg=recover(row)
        except LiveRepairError:
            refs=_source_refs(row['provenance'],old,current)
            if any(old.get(k)!=current.get(k)for k in refs):raise
            result['claimed']|=refs;result['post'].append(row)
            continue
        # Reuse typed AFF bindings only if the persisted job reproduces this
        # source recipe. A changed job file cannot silently change approved Z.
        if config and config.get('annotation_bindings'):
            source_system=next((s for s in config.get('systems',[])if s['id']==identity),None)
            if source_system and not row['provenance'].get('model_sync_slab_state'):
                candidate={**deepcopy(config),'systems':[deepcopy(source_system)]}
                original_doc={**document,'entities':list(old.values()),'resolved_instances':[]}
                check=prepare_slabs(original_doc,candidate)
                if check['blockers']or len(check['groups'])!=1 or _digest(check['groups'][0]['faces'])!=_digest(row['provenance'].get('faces')):
                    fail('樓板註記綁定與目前來源 recipe 不符，不能直接更換標高決策。')
                cfg=candidate
        configs.append((cfg,row))
    if config and config.get('enabled'):
        if config.get('confirmed_scope')is not True:fail('新增樓板系統需要既有來源綁定的樓板範圍決策。')
        for system in config.get('systems',[]):
            if system['id']not in systems:configs.append(({**deepcopy(config),'systems':[deepcopy(system)]},None))
    # A newly drawn flat slab can inherit only a unique existing source layer,
    # role and source-plane-to-model-level relation. Stepped schemes require
    # their explicit system recipe rather than guessing a new level layout.
    configured_keys={k for c,_ in configs for s in c['systems']for record in [s,*s.get('zones',[]),*s.get('openings',[])]for k in record.get('source_entity_ids',[])}
    from .model_sync import _profile
    for key,e in sorted(current.items()):
        if key in configured_keys|result['claimed'] or e.get('type')not in ('LWPOLYLINE','POLYLINE','HATCH'):continue
        candidates=[]
        for c,row in configs:
            if row is None:continue
            s=c['systems'][0];boundary=_geometry_ids(s)
            if s.get('zones')or s.get('openings')or not boundary:continue
            if {current.get(k,old.get(k,{})).get('layer')for k in boundary}!={e.get('layer')}:continue
            baseline={**old,**row['provenance'].get('model_sync_slab_state',{}).get('entities',{})}
            p,z=_profile([e]);source,z0=_profile([baseline[k]for k in boundary if k in baseline])
            if z0 is None:fail('新板缺少既有板界的來源平面基線。')
            if p.intersection(_payload_polygon(s['extent'])).area>.1:fail('新增樓板內圈尚未綁定孔洞角色，不能當成新板填滿。')
            candidates.append((c,z-z0))
        if len(candidates)>1:fail('新樓板來源對應多個樓層／作用板，不能用最近位置決定。')
        if candidates:
            c,dz=candidates[0];c=deepcopy(c);s=c['systems'][0]
            s.update(id='sync-slab:'+_digest(key)[:20],extent=_polygon_payload(p),source_entity_ids=[key],annotation_source_ids=[],
                default_top_z_mm=s['default_top_z_mm']+dz)
            configs.append((c,None));configured_keys.add(key)
    # A changed slab-looking contour without a proven level/role cannot be
    # silently reported as reference-only and a successful complete sync.
    import re
    slab_layers={e.get('layer')for k,e in {**old,**current}.items()if k in configured_keys|result['claimed']}
    for key,e in current.items():
        if key in configured_keys|result['claimed']or old.get(key)==e:continue
        if e.get('type')not in ('LWPOLYLINE','POLYLINE','HATCH'):continue
        layer=e.get('layer','')
        if layer in slab_layers or re.search(r'(^|[-_ ])(slab|roof|floor)([-_ ]|$)|樓板|顶板|頂板|底板',layer,re.I):
            fail('新增樓板／孔洞／降板缺少唯一來源角色、標高與區界綁定；請先確認樓板系統決策。')
    for cfg,row in configs:
        s=cfg['systems'][0];keys=set(s.get('source_entity_ids',[]))
        keys|={k for f in ('zones','openings')for r in s.get(f,[])for k in r.get('source_entity_ids',[])}
        result['claimed']|=keys
        baseline=old
        if row and row['provenance'].get('model_sync_slab_state'):
            baseline={**old,**row['provenance']['model_sync_slab_state']['entities']}
        if row is None:baseline={**current,**baseline}
        refreshed=_refresh(cfg,baseline,current)
        desired=[]
        if refreshed is not None:
            built=prepare_slabs(document,refreshed)
            issues=built['blockers']or validate_slab_payload(built)
            if issues:fail('樓板整片材料／孔洞／等厚驗證失敗：'+str(issues))
            if len(built['groups'])!=1:fail('每個同步樓板系統必須是一個可驗證的整片單殼。')
            group=built['groups'][0]
            if row:group.update(id=row['group_id'],name=row.get('name')or row['provenance'].get('name')or row['group_id'])
            group['model_sync_slab_state']={'schema':'dwg-skp-slab-sync-state-v1','config':deepcopy(refreshed),
                'observed_at_utc':observed,'entities':{k:deepcopy(current[k])for k in keys if k in current}}
            candidate=_row(group,row,_parent('slab',snapshot));desired=[candidate]
            if row and sections_equal(row,candidate):
                result['unchanged'].append(row['entity_id']);result['post'].append(row);desired=[]
            else:
                result['create']+=desired
                result['post'] += [{'entity_id':'planned:'+group['id'],'group_id':group['id'],'category':'slab','manifold':True,
                    'tag':group['tag'],'provenance':deepcopy(group),'triangles_mm':triangles(candidate)}]
        if row and row['entity_id']not in result['unchanged']:
            if row.get('locked')or row.get('nested_geometry'):fail('樓板已鎖定或包含巢狀手工幾何；不能自動替換。')
            result['targets'].append({k:row[k]for k in ('entity_id','group_id','parent_entity_id','signature')})
        result['audit'].append({'slab_system_id':s['id'],'source_entity_ids':sorted(keys),'remove_count':int(bool(row and row['entity_id']not in result['unchanged'])),
            'create_count':len(desired),'unchanged_count':int(bool(row and row['entity_id']in result['unchanged']))})
    return result

def overlap_blockers(create,snapshot,ignored):
    """Exact horizontal material intersections for meshes and prisms alike."""
    from .live_repair import _actual_footprint
    existing=[r for r in snapshot.get('entities',[])+snapshot.get('protected_entities',[])if r['entity_id']not in ignored]
    all_rows=[*create,*existing];zs={};footprints={};ts={}
    for i,r in enumerate(all_rows):
        t=triangles(r);ts[i]=t;zs[i]=sorted({float(p[2])for tri in t for p in tri})
        footprints[i]=unary_union([Polygon([p[:2]for p in tri])for tri in t if Polygon([p[:2]for p in tri]).area>0])
    blockers=[]
    for i in range(len(create)):
        for j in range(i+1,len(all_rows)):
            if footprints[i].intersection(footprints[j]).area<=.1:continue
            events=sorted(set(zs[i]+zs[j]));lo=max(zs[i][0],zs[j][0]);hi=min(zs[i][-1],zs[j][-1])
            for a,b in zip(events,events[1:]):
                if a<lo-1e-6 or b>hi+1e-6 or b-a<=.01:continue
                area=section_from_triangles(ts[i],(a+b)/2).intersection(section_from_triangles(ts[j],(a+b)/2)).area
                if area>.1:
                    blockers.append({'code':'whole_sync_material_overlap','group_ids':[all_rows[i]['group_id'],all_rows[j]['group_id']],
                        'area_mm2':area,'z_interval_mm':[a,b]});break
    return blockers

def validate_slab_rows(plan):
    """Revalidate complete material cells, shell and thickness before dispatch."""
    from .mesh_union import boundaries
    from .whole_slab_qa import verify_saved_whole
    for row in plan.get('create',[]):
        if row['category']!='slab':continue
        g=row['provenance']
        if plan.get('action')!='synchronize_model'or plan.get('slab_sync_version')!='1.0':fail('樓板只能透過整模來源差異計畫更新。')
        intervals,faces=boundaries(g['construction_cells'])
        volume=sum(r['geometry'].area*(r['top_z_mm']-r['base_z_mm'])for r in intervals)
        if _digest(faces)!=_digest(g['faces'])or abs(volume-row['mesh']['expected_volume_mm3'])>max(1,volume*1e-8):fail('樓板材料 recipe 與邊界／材積不一致。')
        topo=topology(g['faces'])
        if not topo['all_closed']or topo['shell_count']!=1:fail('樓板不是單一閉合材料殼。')
        if not verify_saved_whole(g,triangles(row))['all_pass']:fail('樓板網格的孔洞、降板或名義等厚驗證失敗。')
