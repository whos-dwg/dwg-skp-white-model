"""Add source-bound RC slabs to a separately versioned, approved base plan.

Preparing a version does not approve it or write a native file. The ordinary
review/approval/build pipeline consumes the combined payload afterwards.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil

from .modeling import _json_hash


def resolve_building_revisions(base_groups, slab_groups, decisions, *, document=None, opening_records=()):
    """Apply explicit vertical fits, then residual RC material cuts to a base copy."""
    from .whole_slabs import expand_groups
    slab_groups = expand_groups(slab_groups)
    groups=copy.deepcopy(base_groups)
    from .modeling_standard import active, filter_fixed_fire_groups
    if document is not None and active(decisions):
        groups=filter_fixed_fire_groups(document,groups,decisions)
    result={'groups':groups,'blockers':[],'height_rules':{}}
    if decisions.get('wall_under_roof') is not None:
        from .wall_under_roof import resolve_walls_under_roof
        policy=copy.deepcopy(decisions['wall_under_roof'])
        from .modeling_standard import active, CATEGORIES
        if active(decisions):
            from .fixed_standard_validation import is_source_bound_opening_sill
            policy['target_component_ids']=[row['id'] for row in groups if row.get('category') in CATEGORIES
                and not str(row.get('semantic_subrole','')).lower().startswith(('railing','欄杆','栏杆'))
                and not is_source_bound_opening_sill(row,opening_records)]
        if active(decisions) and not policy['target_component_ids']:
            fit={'groups':groups, 'audit':{'enabled':False}, 'blockers':[], 'height_rules':{}}
        else:
            fit=resolve_walls_under_roof(groups,slab_groups,policy)
        groups=fit['groups']
        result['wall_under_roof_audit']=fit['audit']
        result['blockers'].extend(fit['blockers'])
        result['height_rules'].update(fit['height_rules'])
    if decisions.get('building_slab_cuts') is not None:
        from .slab_contacts import resolve_building_slab_cuts
        cuts=resolve_building_slab_cuts(groups,slab_groups,decisions['building_slab_cuts'])
        groups=cuts['groups']
        result['base_component_cut_audit']=cuts['audit']
        result['blockers'].extend(cuts['blockers'])
        result['height_rules'].update(cuts['height_rules'])
    actual_ids={g['id']for g in groups}
    result['height_rules']={k:v for k,v in result['height_rules'].items()if k in actual_ids}
    result['groups']=groups
    return result


def compose_slab_payload(base, document, classification, decisions):
    from .slab_modeling import prepare_slabs
    payload=copy.deepcopy(base)
    # Only a plain building base is accepted; this prevents double attachment.
    if any(group.get('category')=='slab' for group in payload.get('groups',[])):
        raise ValueError('Base plan already contains slabs; revise slab decisions in that version instead')
    if base.get('source_manifest',{}).get('source_sha256')!=document.get('intake',{}).get('source_sha256'):
        raise ValueError('Base model and RC slab source versions differ')
    config=copy.deepcopy(decisions.get('slabs',{}))
    config['source_tag_policy']=decisions.get('source_tag_policy','effective')
    slabs=prepare_slabs(document,config)
    revision=resolve_building_revisions(base['groups'],slabs['groups'],decisions,document=document,
        opening_records=[*base.get('opening_audit',[]),*base.get('selection_opening_audit',[])])
    payload['groups']=revision['groups']
    for key in ('wall_under_roof_audit','base_component_cut_audit'):
        if key in revision:payload[key]=revision[key]
        else:payload.pop(key,None)
    payload['groups'].extend(slabs['groups'])
    existing={item['tag']:item for item in payload.get('tag_mapping',[])}
    for item in slabs['tag_mapping']:
        if item['tag'] in existing and existing[item['tag']]!=item:
            raise ValueError('Derived slab tag collides with an existing DWG tag; choose a separate name')
        if item['tag'] not in existing:
            payload.setdefault('tag_mapping',[]).append(item)
            existing[item['tag']]=item
    payload['slab_audit']=slabs['slab_audit']
    if 'annotation_binding_audit'in slabs:
        payload['annotation_binding_audit']=slabs['annotation_binding_audit']
    else:payload.pop('annotation_binding_audit',None)
    if 'whole_slab_audit' in slabs:
        payload['whole_slab_audit']=slabs['whole_slab_audit']
    else:
        payload.pop('whole_slab_audit',None)
    payload['blockers']=[*base.get('blockers',[]),*slabs['blockers'],*revision['blockers']]
    payload['warnings']=[*base.get('warnings',[]),*slabs['warnings']]
    payload['document_digest']=_json_hash(document)
    payload['classification_digest']=_json_hash(classification)
    payload['decisions_digest']=_json_hash(decisions)
    payload['source_manifest']=copy.deepcopy(document.get('intake',{}))
    if 'slab_join_policy' in decisions:
        payload['slab_join_policy']=copy.deepcopy(decisions['slab_join_policy'])
    else:
        payload.pop('slab_join_policy',None)
    payload['base_model_digest']=_json_hash(base)
    if 'presentation'in decisions:
        payload['presentation']=copy.deepcopy(decisions['presentation'])
    else:payload.pop('presentation',None)
    return payload


def verify_base_snapshot(job):
    from .pipeline import _read, digest_file
    job=Path(job)
    origin=_read(job/'base-origin.json')
    parent=Path(origin['parent_job'])
    for name,digest in origin['parent_input_hashes'].items():
        path=(parent/name).resolve()
        if path.parent!=parent.resolve() or not path.is_file() or digest_file(path)!=digest:
            raise ValueError(f'Original approved base input changed: {name}')
    if digest_file(job/'base-model.plan.json')!=origin['base_plan_file_sha256']:
        raise ValueError('The copied approved base plan changed; preserve base geometry and revise only slab decisions')
    if origin.get('saved_model'):
        saved=Path(origin['saved_model'])
        if not saved.is_file() or digest_file(saved)!=origin.get('saved_model_sha256'):
            raise ValueError('The original delivered model no longer matches the base snapshot')


def attach_slab_job(parent_job, destination, slab_decisions_path, *, document_path=None):
    from .pipeline import WorkflowError,_read,_write,digest_file,_source_freshness,review_job,_now,verified_approved_parent
    parent,destination=Path(parent_job).resolve(),Path(destination).resolve()
    if destination==parent or destination in parent.parents or parent in destination.parents:
        raise WorkflowError('Create the RC revision in a separate sibling job directory')
    if destination.exists():
        raise WorkflowError('Revision already exists; choose a new job directory')
    prior_review,approval,base=verified_approved_parent(parent)
    if base.get('schema')!='dwg-skp-model-v2' or base.get('blockers') or not base.get('groups'):
        raise WorkflowError('RC slabs require a valid building base plan')
    hashes=prior_review.get('input_hashes',{})
    if not hashes or hashes.get('model.plan.json')!=digest_file(parent/'model.plan.json'):
        raise WorkflowError('Base model plan is not bound to its reviewed version')
    if approval.get('review_sha256')!=prior_review.get('review_sha256') or approval.get('input_hashes')!=hashes or not approval.get('confirmation'):
        raise WorkflowError('Base model has no matching original approval')
    for name,digest in {**hashes,**prior_review.get('presentation_hashes',{})}.items():
        path=(parent/name).resolve()
        if path.parent!=parent or not path.is_file() or digest_file(path)!=digest:
            raise WorkflowError(f'Base reviewed input changed: {name}')
    document=_read(document_path or parent/'document.json')
    if document.get('intake',{}).get('source_sha256')!=base.get('source_manifest',{}).get('source_sha256'):
        raise WorkflowError('New annotations and approved base are from different DWG versions')
    issues=_source_freshness(document)
    if issues or document.get('intake',{}).get('blockers') or document.get('intake',{}).get('units')!='mm':
        raise WorkflowError('RC slab source must be current, complete and normalized to mm: '+'; '.join(map(str,issues)))
    config=_read(slab_decisions_path)
    config=config.get('slabs',config)
    if not isinstance(config,dict) or 'systems' not in config:
        raise WorkflowError('Provide a slabs object containing systems and source-bound extents')
    decisions=_read(parent/'decisions.json')
    prior_policy=decisions.get('slabs',{})
    source_sha=document.get('intake',{}).get('source_sha256')
    for policy in (prior_policy,config):
        if policy.get('source_sha256') not in (None,source_sha):
            raise WorkflowError('Slab review exceptions belong to a different DWG version')
    # Carry only source-bound interpretation policy, never old slab geometry,
    # dimensions, confirmed_scope or approval. New per-entity decisions win.
    config=copy.deepcopy(config)
    for key in ('source_sha256','symbol_endpoint_tolerance_mm'):
        if key not in config and key in prior_policy:
            config[key]=copy.deepcopy(prior_policy[key])
    prior_overrides=prior_policy.get('region_overrides',{})
    incoming_overrides=config.get('region_overrides',{})
    if isinstance(prior_overrides,dict) and isinstance(incoming_overrides,dict) and (prior_overrides or incoming_overrides):
        config['region_overrides']={**copy.deepcopy(prior_overrides),**incoming_overrides}
    delivery=_read(parent/'delivery_qa.json') if (parent/'delivery_qa.json').is_file() else {}
    saved=Path(delivery['output']) if delivery.get('output') else None
    if saved and (not saved.is_file() or digest_file(saved)!=delivery.get('output_sha256')):
        raise WorkflowError('Existing saved SKP no longer matches its delivery record')
    destination.mkdir(parents=True)
    for name in ('classification.json','rules.json','layers.json','job.json'):
        shutil.copy2(parent/name,destination/name)
    _write(destination/'document.json',document)
    decisions['slabs']=config
    from .modeling_standard import seed_standard
    seed_standard(decisions)
    if any(system.get('role')=='overhead'for system in config.get('systems',[])):
        decisions.setdefault('presentation',{}).setdefault('overhead_visible',True)
    decisions['slab_revision_status']='awaiting_current_user_confirmation'
    # Original decisions are provenance, never approval for this RC revision.
    decisions.pop('confirmation',None)
    _write(destination/'decisions.json',decisions)
    shutil.copy2(parent/'model.plan.json',destination/'base-model.plan.json')
    _write(destination/'base-origin.json',{'schema':'dwg-slab-base-snapshot-v1','parent_job':str(parent),'parent_review_sha256':prior_review['review_sha256'],'parent_approval_sha256':digest_file(parent/'approval.json'),'parent_input_hashes':{**hashes,**prior_review.get('presentation_hashes',{})},'base_plan_file_sha256':digest_file(parent/'model.plan.json'),'saved_model':str(saved) if saved else None,'saved_model_sha256':digest_file(saved) if saved else None,'note':'Original approval covers the preserved building base only; RC additions require current review confirmation.'})
    info=_read(destination/'job.json')
    info.update(parent_job=str(parent),created_utc=_now(),revision_kind='rc_slab_addition',modeling_standard=decisions['modeling_standard'])
    _write(destination/'job.json',info)
    return review_job(destination)
