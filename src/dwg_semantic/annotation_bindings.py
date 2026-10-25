"""Typed, source-bound AFF review without nearest-text or floor-role guessing."""
from __future__ import annotations

import copy
import math

from shapely.geometry import Polygon


def resolve_annotation_bindings(annotations, entities, config):
    notes = {row['entity_id']: row for row in annotations}
    source_ids = {row['entity_id'] for row in entities}
    issues, resolved, records = [], [], []
    bindings = config.get('annotation_bindings', [])
    regions = config.get('functional_regions', [])
    systems = config.get('systems', [])
    if not isinstance(bindings, list) or not isinstance(regions, list) or not isinstance(systems, list):
        return {'all_pass': False, 'issues': [{'code': 'invalid_annotation_binding_collections'}], 'records': []}, []
    region_index = {row.get('id'): row for row in regions if isinstance(row, dict)}
    system_index = {row.get('id'): row for row in systems if isinstance(row, dict)}
    ids = set()

    def number(value, label):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(label + ' must be a finite model-relative millimeter value')
        return float(value)

    for binding in bindings:
        identity = binding.get('id') if isinstance(binding, dict) else None
        try:
            if not identity or identity in ids:
                raise ValueError('Binding identity must be unique')
            ids.add(identity)
            if binding.get('basis') not in {'source_explicit', 'confirmed_job_decision'} or not str(binding.get('reason', '')).strip():
                raise ValueError('Binding requires its source/confirmed decision basis and reason')
            if binding.get('token_kind') != 'aff_height' or binding.get('reference_surface') != 'finished_floor':
                raise ValueError('AFF must remain a height referenced to the finished floor')
            note = notes[binding['source_entity_id']]
            index = binding.get('token_index')
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                raise ValueError('Invalid AFF token index')
            token = note['height_candidates'][index]
            target = binding.get('target_surface')
            if target not in {'structural_bottom', 'finished_ceiling', 'railing_top', 'curtain_top', 'door_head'}:
                raise ValueError('Target surface is unresolved or unsupported')
            if token['target_surface'] not in {'unresolved', target}:
                raise ValueError('Binding contradicts the literal source surface')
            if binding['basis'] == 'source_explicit' and token['target_surface'] != target:
                raise ValueError('A source-explicit binding needs a literal typed source surface')
            override_unit = binding.get('unit')
            if override_unit is not None and (override_unit not in {'mm', 'cm', 'm'} or
                    (token['unit'] is not None and override_unit != token['unit'])):
                raise ValueError('A unit override cannot replace the literal source unit')
            unit = token['unit'] or override_unit
            if unit not in {'mm', 'cm', 'm'}:
                raise ValueError('AFF unit is independent of H elevation units and remains unresolved')
            value = number(token['numeric_value'], 'AFF distance') * {'mm': 1, 'cm': 10, 'm': 1000}[unit]
            if value <= 0:
                raise ValueError('AFF distance must be positive')
            system = system_index[binding['applies_to_system_id']]
            if system.get('role') not in {'floor', 'overhead'}:
                raise ValueError('The actual floor/overhead owner must be explicitly bound')
            region = region_index[binding['functional_region_id']]
            if not region.get('source_entity_ids') or not set(region['source_entity_ids']) <= source_ids:
                raise ValueError('Functional region lacks complete source-bound evidence')
            footprint = region['footprint']
            domain = Polygon(footprint['outer'], footprint.get('holes', []))
            extent = Polygon(system['extent']['outer'], system['extent'].get('holes', []))
            if not domain.is_valid or domain.area <= .1 or not extent.buffer(.001).covers(domain):
                raise ValueError('Functional region must be valid and contained by its bound slab extent')
            references = binding['reference_annotation_source_ids']
            if not references or not isinstance(references, list):
                raise ValueError('FFL reference requires exact source annotation IDs')
            ffl = [level for key in references for level in notes[key]['levels']
                   if level['level_kind'] == 'finished_level' and level.get('eligible_for_slab_elevation', False)]
            if len(ffl) != len(references) or any(level['value_mm'] is None for level in ffl):
                raise ValueError('Each reference must resolve to one typed, unit-known FFL token')
            reference = number(binding['reference_z_mm'], 'Finished floor model Z')
            if 'source_to_model_z_offset_mm' in binding:
                offset = number(binding['source_to_model_z_offset_mm'], 'Source datum to model datum offset')
            elif config.get('floor_datum_m') is not None:
                offset = -number(config['floor_datum_m'], 'Source floor datum in meters') * 1000
            else:
                raise ValueError('Source FFL to model Z requires an explicit datum transformation')
            if any(abs(level['value_mm']+offset-reference) > .001 for level in ffl):
                raise ValueError('FFL model Z contradicts the bound source token and datum transformation')
            row = {'id': identity, 'target_surface': target, 'reference_surface': 'finished_floor',
                   'reference_z_mm': reference, 'aff_distance_mm': value, 'value_z_mm': reference+value,
                   'applies_to_system_id': system['id'], 'functional_region_id': region['id'],
                   'footprint': copy.deepcopy(footprint), 'basis': binding['basis'], 'reason': binding['reason'],
                   'source_entity_ids': sorted({note['entity_id'], *references, *region['source_entity_ids']}),
                   'source_token': copy.deepcopy(token), 'unit_used': unit,
                   'source_to_model_z_offset_mm': offset, 'accepted_for_modeling': False,
                   'status': 'resolved_source_bound_height_candidate_not_approval'}
            if target == 'structural_bottom':
                row['bottom_z_mm'] = reference+value
            records.append({'id': identity, 'status': 'resolved', 'binding': copy.deepcopy(binding)})
            resolved.append(row)
        except (KeyError, TypeError, ValueError, IndexError, OverflowError) as error:
            issues.append({'code': 'invalid_aff_annotation_binding', 'binding_id': identity, 'detail': str(error)})
            records.append({'id': identity, 'status': 'unresolved', 'binding': copy.deepcopy(binding)})
    # No contradictory overlapping height fields may be used as a candidate.
    for index, first in enumerate(resolved):
        for second in resolved[index+1:]:
            if (first['applies_to_system_id'] == second['applies_to_system_id'] and first['target_surface'] == second['target_surface']
                    and abs(first['value_z_mm']-second['value_z_mm']) > .001):
                a,b = first['footprint'],second['footprint']
                if Polygon(a['outer'],a.get('holes',[])).intersection(Polygon(b['outer'],b.get('holes',[]))).area > .1:
                    issues.append({'code':'conflicting_aff_functional_height_regions','binding_ids':[first['id'],second['id']]})
    return {'schema': 'dwg-annotation-binding-audit-v1', 'all_pass': not issues,
            'issues': issues, 'records': records}, [] if issues else resolved


def bind_structural_height_zones(document, config):
    """Prepare only explicitly source/datum/system/region-bound RC soffits.

    Candidate preparation is not an approval. The normal confirmed_scope,
    current review digest and user approval still gate native output.
    """
    from .slabs import prepare_slab_review
    review = prepare_slab_review(document, {}, slab_decisions=config, include_automatic_recognition=False)
    audit = review['annotation_binding_audit']
    result = copy.deepcopy(config)
    if not audit['all_pass']:
        return result, audit
    systems = {row['id']: row for row in result.get('systems', [])}
    for region in review['resolved_height_regions']:
        if region['target_surface'] != 'structural_bottom':
            continue
        system = systems[region['applies_to_system_id']]
        thickness = system.get('default_thickness_mm')
        if thickness is None and system.get('thickness_policy', result.get('thickness_policy', 'default_200')) == 'default_200':
            thickness = 200.0
        if isinstance(thickness, bool) or not isinstance(thickness, (int,float)) or not math.isfinite(thickness) or thickness <= 0:
            audit['issues'].append({'code':'aff_bound_zone_requires_valid_nominal_thickness','binding_id':region['id']})
            continue
        system.setdefault('zones', []).append({'id':'aff-height:'+region['id'], 'method':'explicit_bottom',
            'bottom_z_mm':region['bottom_z_mm'], 'thickness_mm':thickness,
            'footprint':copy.deepcopy(region['footprint']), 'source_entity_ids':region['source_entity_ids'],
            'annotation_source_ids': [key for key in region['source_entity_ids']
                                      if key in {note['entity_id']for note in review['annotations']}],
            'evidence':{'method':'typed_aff_finished_floor_and_functional_region_binding','binding':region}})
    audit['all_pass'] = not audit['issues']
    audit['resolved_height_regions'] = review['resolved_height_regions']
    return result, audit
