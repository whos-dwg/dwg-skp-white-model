"""The user's fixed white-model standard, independent of drawing identity.

New jobs carry this versioned policy. Historical jobs are not rewritten; a
revision explicitly adopts it. Geometry/owner/datum evidence and actual user
approval remain mandatory. This module never changes CAD or a live model.
"""
from __future__ import annotations

import copy
import re

VERSION = 'fixed-white-model-20261021'
CATEGORIES = ['architectural_wall', 'structural_wall', 'column', 'curtain', 'curtain_accessory', 'lintel']
FIXED = {'version': VERSION, 'slab_assembly_mode': 'whole_uniform_thickness',
         'preserve_openings_and_outer_boundary': True, 'verticals_to_slab_underside': True,
         'stair_opening_scope': 'entire_enclosure_including_landings',
         'fire_door_and_shutter_bodies': 'exclude_3d_keep_opening_evidence'}


def seed_standard(decisions):
    decisions['modeling_standard'] = copy.deepcopy(FIXED)
    decisions.setdefault('slabs', {}).setdefault('assembly_mode', 'whole_uniform_thickness')
    return decisions


def active(decisions):
    return isinstance(decisions.get('modeling_standard'), dict)


def apply_standard_decisions(decisions, classification):
    if not active(decisions):
        return []  # Explicit legacy recipe: never silently rewrite old jobs.
    issues = []
    if decisions['modeling_standard'] != FIXED:
        issues.append({'code': 'fixed_modeling_standard_changed', 'expected': FIXED})
    config = decisions.setdefault('slabs', {})
    config['required_assembly_mode'] = 'whole_uniform_thickness'
    config.setdefault('assembly_mode', 'whole_uniform_thickness')
    for system in config.get('systems', []):
        if system.get('assembly_mode', config['assembly_mode']) != 'whole_uniform_thickness':
            issues.append({'code': 'fixed_standard_requires_whole_uniform_slab', 'system_id': system.get('id')})
    supplied = decisions.get('wall_under_roof', {})
    if supplied and (supplied.get('enabled') is False or supplied.get('operation') not in (None, 'fit_to_lowest_overhead_underside')):
        issues.append({'code': 'fixed_standard_requires_verticals_at_underside'})
    decisions['wall_under_roof'] = {**supplied, 'enabled': True,
        'operation': 'fit_to_lowest_overhead_underside', 'target_categories': list(CATEGORIES)}
    # Whole stair rooms are a fixed semantic choice, not a bounding-box cutter.
    automatic = classification.get('automatic_plan', {}).get('slab_openings', {})
    if config.get('enabled'):
        for pending in automatic.get('stair_boundary_review_requests', []):
            issues.append({'code': 'whole_stair_enclosure_boundary_unresolved',
                           'source_entity_ids': pending['source_entity_ids'],
                           'required_scope': FIXED['stair_opening_scope']})
        from .plan_geometry import shape_of
        candidates={row['id']:row for row in automatic.get('staircase_openings', [])}
        bindings=config.get('stair_opening_bindings', {})
        if not isinstance(bindings,dict):
            issues.append({'code':'whole_stair_opening_system_binding_invalid',
                           'detail':'stair_opening_bindings must map exact candidate IDs to target systems and a reason'})
            bindings={}
        for identity in sorted(set(bindings)-set(candidates)):
            issues.append({'code':'stair_opening_binding_candidate_unresolved','candidate_id':identity})
        systems={row.get('id'):row for row in config.get('systems', [])}
        expected_ids=set()
        for candidate_id,opening in candidates.items():
            binding=bindings.get(candidate_id)
            if binding is None:
                # Restore existing exact-ID decisions and source-explicit owner
                # evidence; the fixed scope must not make an already proved
                # slab identity into a new question.
                overrides=config.get('region_overrides',{})
                supplied=[]
                if isinstance(overrides,dict):
                    if candidate_id in overrides:
                        supplied=[overrides[candidate_id]]
                    else:
                        supplied=[overrides[key]for key in opening.get('source_entity_ids',[])if key in overrides]
                supplied=[row for row in supplied if isinstance(row,dict)
                          and('applies_to_system_id'in row or'applies_to_system_ids'in row)]
                if supplied:
                    choices=[]
                    for row in supplied:
                        targets=row.get('applies_to_system_ids')
                        if targets is None and row.get('applies_to_system_id')is not None:
                            targets=[row['applies_to_system_id']]
                        valid=(row.get('action')=='opening'and isinstance(targets,list)and bool(targets)
                               and all(isinstance(key,str)and bool(key)for key in targets)
                               and len(set(targets))==len(targets)
                               and isinstance(row.get('reason'),str)and bool(row['reason'].strip()))
                        choices.append(tuple(sorted(set(targets)))if valid else None)
                    if None in choices or len(set(choices))!=1:
                        issues.append({'code':'whole_stair_opening_system_binding_invalid','candidate_id':candidate_id,
                                       'source_entity_ids':opening['source_entity_ids'],
                                       'detail':'Exact-source slab-owner decisions are invalid or conflict'})
                        continue
                    binding={'applies_to_system_ids':list(choices[0]),
                             'reason':'; '.join(dict.fromkeys(row['reason']for row in supplied))}
                elif opening.get('slab_owner_evidence'):
                    binding={'applies_to_system_ids':opening.get('applies_to_system_ids'),
                             'applies_to_system_id':opening.get('applies_to_system_id'),
                             'reason':'source_candidate_explicit_slab_owner',
                             'slab_owner_evidence':copy.deepcopy(opening['slab_owner_evidence'])}
            if binding is None:
                issues.append({'code':'whole_stair_opening_system_binding_required','candidate_id':candidate_id,
                               'source_entity_ids':opening['source_entity_ids'],
                               'required_scope':FIXED['stair_opening_scope'],
                               'detail':'Whole-enclosure scope does not establish whether this floor or an overhead slab is cut'})
                continue
            targets=binding.get('applies_to_system_ids')if isinstance(binding,dict)else None
            if targets is None and isinstance(binding,dict)and binding.get('applies_to_system_id')is not None:
                targets=[binding['applies_to_system_id']]
            reason=binding.get('reason')if isinstance(binding,dict)else None
            if (not isinstance(targets,list)or not targets or any(not isinstance(key,str)or not key for key in targets)
                    or len(set(targets))!=len(targets)or any(key not in systems for key in targets)
                    or not isinstance(reason,str)or not reason.strip()):
                issues.append({'code':'whole_stair_opening_system_binding_invalid','candidate_id':candidate_id,
                               'source_entity_ids':opening['source_entity_ids'],
                               'detail':'Use existing, distinct slab system IDs and a nonempty source/job binding reason'})
                continue
            shape=shape_of(opening)
            valid=[]
            for target in targets:
                system=systems[target]
                if not system.get('extent')or shape_of(system['extent']).intersection(shape).area<=.001:
                    issues.append({'code':'whole_stair_opening_misses_target','candidate_id':candidate_id,
                                   'system_id':target,'source_entity_ids':opening['source_entity_ids']})
                else:
                    valid.append(system)
            # Do not apply only a subset of an invalid multi-system binding.
            if len(valid)!=len(targets):
                continue
            for system in valid:
                identity='standard-stair:'+system['id']+':'+candidate_id
                expected_ids.add(identity)
                openings=system.setdefault('openings', [])
                existing=[row for row in openings if row.get('id')==identity]
                expected={'id':identity,'footprint':copy.deepcopy(opening['footprint']),
                          'source_entity_ids':list(opening['source_entity_ids']),
                          'applies_to_system_id':system['id'],
                          'evidence':{'method':'fixed_standard_entire_source_bound_stair_enclosure',
                                      'source_candidate_id':candidate_id,'includes_landings':True,
                                      'slab_owner_binding_reason':reason}}
                if binding.get('slab_owner_evidence'):
                    expected['evidence']['slab_owner_evidence']=copy.deepcopy(binding['slab_owner_evidence'])
                if existing and existing!=[expected]:
                    issues.append({'code':'fixed_stair_opening_changed','opening_id':identity})
                elif not existing:
                    openings.append(expected)
        for system in config.get('systems', []):
            for opening in system.get('openings', []):
                if (opening.get('evidence',{}).get('method')=='fixed_standard_entire_source_bound_stair_enclosure'
                        and opening.get('id')not in expected_ids):
                    issues.append({'code':'fixed_stair_opening_stale','opening_id':opening.get('id'),
                                   'source_candidate_id':opening.get('evidence',{}).get('source_candidate_id'),
                                   'detail':'Preserved prior cut requires a current source-bound candidate and slab-owner binding'})
    return issues


def roof_height_seed(document, decisions):
    """Derive a temporary vertical envelope from actual roofs, never a default Z.

    Every final top is subsequently fit/verified at the actual local underside.
    The temporary maximum is source-derived preparation, not a confirmed height.
    """
    result = copy.deepcopy(decisions)
    if not active(decisions) or not decisions.get('slabs', {}).get('enabled'):
        return result
    from .slab_modeling import prepare_slabs
    from .whole_slabs import expand_groups
    roofs = [row for row in expand_groups(prepare_slabs(document, decisions['slabs'])['groups'])
             if row.get('slab_role') == 'overhead_slab' or row.get('host_slab_role') == 'overhead_slab']
    if not roofs:
        return result
    cap = max(row['base_z_mm'] for row in roofs)
    for mapping in (result.get('layer_mapping', {}), result.get('object_overrides', {})):
        for config in mapping.values():
            if config.get('category') in CATEGORIES and config.get('height_mm') is None:
                base = config.get('base_z_mm', 0)
                if isinstance(base, (int, float)) and cap > base:
                    config['height_mm'] = cap-base
                    config['height_basis'] = 'temporary_actual_roof_envelope_before_required_local_underside_fit'
    return result


def finish_composition(payload, decisions):
    if not active(decisions):
        return payload
    from .modeling import _json_hash
    fit = payload.get('wall_under_roof_audit') or {}
    if fit.get('enabled'):
        decisions.setdefault('component_height_rules', {}).update(copy.deepcopy(fit.get('height_rules', {})))
    payload['modeling_standard'] = copy.deepcopy(FIXED)
    payload['decisions_digest'] = _json_hash(decisions)
    return payload


def _fire_body_name(value):
    return bool(re.search(r'fire[ _-]*(?:rated[ _-]*)?(?:door|shutter)|door[ _-]*fire|'
                          r'防火(?:[捲卷][門门]|[門门])|[捲卷][簾帘]|'
                          r'rolling[ _-]*(?:door|shutter)|(?:fire[ _-]*door|shutter)[ _-]*body',str(value),re.I))


def fire_body_source_ids(document, decisions=None):
    from .modeling import entity_key, model_entities, _raw_layer
    # Names/ancestry alone determine this semantic exclusion. Do not parse
    # geometry (including unsupported reference entities) merely to read names.
    containers = {str(e.get('instance_path') or e.get('handle') or ''): e
                  for e in [*document.get('entities', []), *document.get('resolved_instances', [])]
                  if e.get('type') in {'INSERT', 'BLOCK_REFERENCE'}
                  and (e.get('owner_block') == '*Model_Space' or e.get('root_space') == '*Model_Space')}
    result = set()
    decisions=decisions or{}
    for entity in model_entities(document):
        names = [str(entity.get(key, '')) for key in ('original_layer','layer','source_block','block_name',
                                                     'semantic_role','semantic_subrole','opening_type')]
        layer=_raw_layer(entity,decisions.get('source_tag_policy','effective'))
        config=dict(decisions.get('layer_mapping',{}).get(layer)or{})
        config.update(decisions.get('object_overrides',{}).get(entity_key(entity))or{})
        names += [str(config.get(key,''))for key in ('semantic_role','semantic_subrole','opening_type')]
        path = str(entity.get('instance_path') or '').split('/')
        for count in range(1, len(path) + 1):
            parent = containers.get('/'.join(path[:count]))
            if parent is None:
                continue
            names += [str(parent.get(key, '')) for key in ('original_layer', 'layer', 'block_name', 'dynamic_block_name')]
        if any(_fire_body_name(value) for value in names):
            result.add(entity_key(entity))
    return result


def _is_fixed_fire_body_group(group, fire_ids):
    # The RC wall head over a fire-door opening is host material, not the door
    # body. Its source may legitimately be the opening's own symbol.
    if group.get('category')=='slab' or group.get('semantic_role')=='door_lintel':
        return False
    sources=set(group.get('source_entity_ids',[]))
    explicit=any(_fire_body_name(group.get(key,''))for key in ('semantic_role','semantic_subrole','opening_type'))
    return bool(sources)and(sources<=fire_ids or explicit)


def filter_fixed_fire_groups(document, groups, decisions):
    """Pure pre-fit exclusion; the immutable base and its digest stay untouched.

    The final enforcement stage retains each excluded source as complete 2D
    reference geometry. A caller may safely use this before an underside fit or
    its deterministic replay without manufacturing a body height first.
    """
    if not active(decisions):
        return copy.deepcopy(groups)
    fire_ids=fire_body_source_ids(document,decisions)
    return [copy.deepcopy(row)for row in groups if not _is_fixed_fire_body_group(row,fire_ids)]


def _retain_fire_source_references(document, payload, decisions, fire_ids, issues):
    from .modeling import entity_key, model_entities, _linework, _raw_layer, _source, _source_layers, _source_plane
    by_id={entity_key(row):row for row in model_entities(document)}
    policy=payload.get('source_tag_policy',decisions.get('source_tag_policy','effective'))
    references=payload.setdefault('references',[])
    reason='fixed_standard_fire_door_shutter_body_excluded_2d_opening_evidence_retained'
    for identity in sorted(fire_ids):
        entity=by_id.get(identity)
        if entity is None:
            issues.append({'code':'fixed_fire_body_source_unresolved','source_entity_ids':[identity]})
            continue
        layer=_raw_layer(entity,policy);source=_source(entity)
        mappings=[row for row in payload.get('tag_mapping',[])if row.get('source_layer')==layer
                  and row.get('source_id',source)==source]
        if not mappings or not isinstance(mappings[0].get('tag'),str):
            issues.append({'code':'fixed_fire_reference_tag_unresolved','source_entity_ids':[identity],
                           'source_layer':layer})
            continue
        segments=[]
        try:
            segments=[{'start':list(line.coords[0]),'end':list(line.coords[-1])}for line in _linework(entity)]
        except (KeyError,TypeError,ValueError,OverflowError):
            # Unsupported body geometry is still retained in the source-bound
            # document; it is not an excuse to fabricate a 3D body or lose its ID.
            pass
        try:
            plane=_source_plane(entity)
        except (KeyError,TypeError,ValueError,OverflowError):
            plane={'source_plane_z_mm':None,'source_z_range_mm':None,'source_nonplanar':True}
        record={'id':identity,'name':str(entity.get('name')or identity),'category':'reference','mode':'reference_edges',
                'source_entity_ids':[identity],'source_id':source,'source_layer':layer,
                'source_layers':list(_source_layers(entity,policy)),
                'source_handles':list(entity.get('source_handles')or[entity.get('source_handle')or entity.get('handle')or identity]),
                'original_layer':entity.get('original_layer',entity.get('layer')),
                'effective_layer':entity.get('effective_layer',entity.get('layer')),
                'block_layer':entity.get('block_layer'),'instance_path':entity.get('instance_path'),
                'tag':mappings[0]['tag'],'segments':segments,'base_z_mm':plane.get('source_plane_z_mm')or 0,
                'reason':reason,**plane}
        duplicates=[row for row in references if row.get('id')==identity or row.get('source_entity_ids')==[identity]]
        if duplicates:
            record={**duplicates[0],**record}
            references[:]=[row for row in references if row not in duplicates]
        references.append(record)
        decisions.setdefault('source_dispositions',{})[identity]={'status':'reference','reason':reason}


def _clear_excluded_body_blockers(payload, document, decisions, fire_ids, removed_ids):
    """Clear only obsolete body preparation failures, never door clearance data."""
    from .modeling import entity_key, model_entities, _raw_layer
    body_only=set()
    for entity in model_entities(document):
        identity=entity_key(entity)
        config=dict(decisions.get('layer_mapping',{}).get(_raw_layer(entity,decisions.get('source_tag_policy','effective')))or{})
        config.update(decisions.get('object_overrides',{}).get(identity)or{})
        if identity in fire_ids and config.get('category')!='opening':
            body_only.add(identity)
    obsolete={'missing_or_invalid_category','height_requires_confirmation','unresolved_footprint',
              'closed_contour_may_be_room','nonhorizontal_source_requires_projection_confirmation',
              'invalid_source_footprint','unpaired_boundary','invalid_geometry_or_decision',
              'nested_accessory_decision_requires_confirmation','parent_child_layer_semantics_require_confirmation',
              'automatic_material_topology_requires_candidate_decision','conflicting_composite_source_decisions',
              'composite_source_footprint_requires_assembly_override','facade_perimeter_closure_requires_confirmation'}
    kept=[];cleared=[]
    for issue in payload.get('blockers',[]):
        if not isinstance(issue,dict):
            kept.append(issue);continue
        identities=set(issue.get('source_entity_ids',[]))
        if issue.get('entity_id'):
            identities.add(issue['entity_id'])
        if issue.get('source_entity_id'):
            identities.add(issue['source_entity_id'])
        clear=(issue.get('code')in obsolete and bool(identities)and identities<=body_only)
        clear=clear or(issue.get('code')in{'overlapping_solid_components','overlapping_prepared_solids'}
                        and bool(set(issue.get('component_ids',[]))&removed_ids))
        clear=clear or(issue.get('code')=='no_solid_components'and bool(payload.get('groups')))
        (cleared if clear else kept).append(issue)
    payload['blockers']=kept
    return cleared


def _replace_arbitrary_vertical_height_questions(document, payload, decisions):
    """A fixed full-height member needs its local soffit, not an invented top."""
    from .modeling import entity_key, model_entities, _raw_layer
    by_id={entity_key(row):row for row in model_entities(document)}
    for issue in payload.get('blockers',[]):
        if not isinstance(issue,dict)or issue.get('code')!='height_requires_confirmation':
            continue
        identity=issue.get('entity_id');entity=by_id.get(identity)
        if entity is None:
            continue
        layer=_raw_layer(entity,decisions.get('source_tag_policy','effective'))
        config=dict(decisions.get('layer_mapping',{}).get(layer)or{})
        config.update(decisions.get('object_overrides',{}).get(identity)or{})
        subrole=str(config.get('semantic_subrole',entity.get('semantic_subrole',''))).lower()
        if config.get('category')not in CATEGORIES or subrole.startswith(('railing','欄杆','栏杆')):
            continue
        issue.update(code='fixed_standard_overhead_underside_required',
                     reason='需有依據的實際當地板底，不採任意牆柱或全高帷幕高度')
        for reference in payload.get('references',[]):
            if identity in reference.get('source_entity_ids',[reference.get('id')])and reference.get('reason')=='height_requires_confirmation':
                reference['reason']='fixed_standard_overhead_underside_required'
                reference['required_height_basis']='actual_local_overhead_underside_not_arbitrary_member_height'


def enforce_standard_payload(document, payload, decisions, classification):
    if not active(decisions):
        return payload
    from .plan_geometry import shape_of
    from .whole_slabs import expand_groups
    from .source_accounting import attach_final_source_accounting
    issues = []
    removed = []
    fire_ids = fire_body_source_ids(document,decisions)
    kept = []
    for group in payload.get('groups', []):
        if _is_fixed_fire_body_group(group,fire_ids):
            removed.append(group['id'])
            fire_ids.update(group.get('source_entity_ids',[]))
        else:
            kept.append(group)
    payload['groups'] = kept
    if removed:
        from .modeling import _solid_overlaps
        payload['overlap_audit']=_solid_overlaps(kept)
    # Callers normally filter before fitting. Direct preparation/enforcement is
    # also safe: remove obsolete body-only fit expectations instead of making
    # native QA look for deliberately omitted fragments.
    fit=payload.get('wall_under_roof_audit')
    if isinstance(fit,dict):
        original_body_ids={row['id']for row in fit.get('original_groups',[])
                           if _is_fixed_fire_body_group(row,fire_ids)}
        fit['original_groups']=[row for row in fit.get('original_groups',[])if row.get('id')not in original_body_ids]
        fit['changes']=[row for row in fit.get('changes',[])if row.get('original_component_id')not in original_body_ids]
        fit['target_component_ids']=[key for key in fit.get('target_component_ids',[])if key not in original_body_ids]
        fit['untouched_component_ids']=[key for key in fit.get('untouched_component_ids',[])
                                        if key not in original_body_ids and key not in removed]
        fit['height_rules']={key:value for key,value in fit.get('height_rules',{}).items()if key not in removed}
        fit['result_component_count']=sum(row.get('category')!='slab'for row in kept)
        fit['trimmed_volume_mm3']=sum(row.get('trimmed_volume_mm3',0)for row in fit['changes'])
        fit['extended_volume_mm3']=sum(row.get('extended_volume_mm3',0)for row in fit['changes'])
        if not fit['target_component_ids']:
            fit.update(enabled=False,all_pass=True)
    for key in removed:
        decisions.get('component_height_rules',{}).pop(key,None)
    _retain_fire_source_references(document,payload,decisions,fire_ids,issues)
    cleared_body_blockers=_clear_excluded_body_blockers(payload,document,decisions,fire_ids,set(removed))
    _replace_arbitrary_vertical_height_questions(document,payload,decisions)
    slabs = [row for row in kept if row.get('category') == 'slab']
    if any(row.get('mode') != 'boundary_mesh' or row.get('assembly_mode') != 'whole_uniform_thickness' for row in slabs):
        issues.append({'code': 'fixed_standard_requires_whole_uniform_slab'})
    from shapely.ops import unary_union
    locked_geometry=[]
    systems=decisions.get('slabs',{}).get('systems',[])
    for system in systems:
        material=[row for row in slabs if row.get('slab_system_id')==system.get('id')]
        if not material or not system.get('extent'):
            continue
        domain=shape_of(system['extent'])
        cutters=unary_union([shape_of(row['footprint']) for owner in systems for row in owner.get('openings', [])
                            if row.get('applies_to_system_id')==system.get('id')])
        expected=domain.difference(cutters)
        actual=unary_union([shape_of(row) for row in material])
        difference=expected.symmetric_difference(actual).area
        locked_geometry.append({'system_id':system['id'],'difference_area_mm2':difference,
                                'outer_and_openings_unchanged':difference<=1e-6})
        if difference>1e-6:
            issues.append({'code':'fixed_standard_outer_or_opening_boundary_changed','system_id':system['id'],
                           'difference_area_mm2':difference})
    verticals = [row for row in kept if row.get('category') in CATEGORIES
                 and not str(row.get('semantic_subrole', '')).lower().startswith(('railing', '欄杆', '栏杆'))]
    roofs = [row for row in expand_groups(slabs) if row.get('slab_role') == 'overhead_slab'
             or row.get('host_slab_role') == 'overhead_slab']
    if verticals and not roofs:
        issues.append({'code': 'fixed_standard_overhead_underside_required',
                       'component_ids': [row['id'] for row in verticals]})
    for change in (payload.get('wall_under_roof_audit') or {}).get('changes', []):
        if any(region.get('coverage') == 'uncovered' and shape_of(region['footprint']).area > .001
               for region in change.get('cap_regions', [])):
            issues.append({'code': 'fixed_standard_underside_coverage_required',
                           'component_id': change['original_component_id']})
    if verticals and roofs and not (payload.get('wall_under_roof_audit') or {}).get('enabled'):
        issues.append({'code': 'fixed_standard_local_underside_fit_required'})
    previous=payload.get('fixed_standard_audit',{})
    if previous.get('standard')==FIXED:
        removed=sorted(set(removed)|set(previous.get('removed_fire_body_ids',[])))
    payload['fixed_standard_audit'] = {'standard': copy.deepcopy(FIXED), 'removed_fire_body_ids': removed,
        'cleared_excluded_body_blockers':cleared_body_blockers,
        'locked_slab_geometry':locked_geometry,
        'fire_body_source_entity_ids': sorted(fire_ids), 'issues': issues, 'all_pass': not issues}
    payload['blockers'].extend(issues)
    payload['blockers'] = [row for row in payload['blockers'] if not isinstance(row, dict)
                           or row.get('code') != 'source_disposition_incomplete']
    payload['blockers'].extend(attach_final_source_accounting(document, payload, decisions, classification))
    if isinstance(payload.get('counts'),dict):
        payload['counts'].update(solids=len(payload['groups']),references=len(payload.get('references',[])),
                                 blockers=len(payload['blockers']))
    return finish_composition(payload, decisions)
