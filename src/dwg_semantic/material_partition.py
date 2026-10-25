"""Explicitly reviewed ownership of overlapping source-bound material.

Only ownership changes. The source material union, cavities and coordinates
are invariant; slabs and opening cutters never enter this operation.
"""
from __future__ import annotations

import copy
from collections import Counter

from shapely.geometry import GeometryCollection
from shapely.ops import unary_union

from .plan_geometry import footprint, parts, shape_of


CATEGORIES={'column','structural_wall','architectural_wall','curtain','curtain_accessory'}


def partition_materials(rows, config):
    if config is None or (isinstance(config,dict) and config.get('enabled') is False):
        return rows, None, []
    original=copy.deepcopy(rows)
    try:
        order=config['priority_categories']
        if (config.get('enabled') is not True or not isinstance(order,list) or not order
                or len(set(order))!=len(order) or not set(order)<=CATEGORIES
                or not {row['category']for row in rows}<=set(order)
                or not isinstance(config.get('reason'),str) or not config['reason'].strip()):
            raise ValueError('Explicit category priority and a nonempty source/job reason are required')
        if len({row['source_id']for row in rows})>1:
            raise ValueError('Material partition cannot silently combine independent drawing sources')
        if len({(row['base_z_mm'],row['height_mm'])for row in rows})>1:
            raise ValueError('Different vertical material intervals need separate reviewed partitions')
        occupied=GeometryCollection();result=[];ledger=[]
        for row in sorted(original,key=lambda r:(order.index(r['category']),r['id'])):
            source_shape=shape_of(row)
            remaining=source_shape.difference(occupied)
            owners=[]
            for owner in result:
                overlap=shape_of(owner).intersection(source_shape).area
                if overlap<=0:
                    continue
                owners.append({'component_id':owner['id'],'overlap_area_mm2':overlap})
                physical_prior=set(owner['source_entity_ids'])-set(owner.get('reference_source_entity_ids',[]))
                # The other source supported the same physical material. Keep
                # that coverage relationship without changing the owner's Tag.
                for field in ('source_entity_ids','source_handles','source_layers'):
                    owner[field]=sorted(set(owner.get(field,[]))|set(row.get(field,[])))
                # Reference evidence stays evidence after ownership changes.
                refs=set(owner.get('reference_source_entity_ids',[]))|set(row.get('reference_source_entity_ids',[]))
                physical=physical_prior|(set(row['source_entity_ids'])-set(row.get('reference_source_entity_ids',[])))
                owner['reference_source_entity_ids']=sorted(refs-physical)
                provenance={p['entity_id']:p for p in owner.get('source_provenance',[])}
                for trace in row.get('source_provenance',[]):provenance.setdefault(trace['entity_id'],trace)
                owner['source_provenance']=[provenance[key]for key in sorted(provenance)]
                owner.setdefault('partition_covered_candidate_ids',[]).append(row['id'])
            pieces=sorted(parts(remaining),key=lambda p:p.normalize().wkb_hex)
            removed=source_shape.area-remaining.area
            changed=bool(owners) or len(pieces)!=1
            for number,piece in enumerate(pieces):
                item=copy.deepcopy(row)
                if changed:
                    item['id']=row['id']+'/partition/'+str(number)
                    item['footprint']=footprint(piece)
                    item['material_partition_evidence']={'source_candidate_id':row['id'],
                        'original_footprint':row['footprint'],'removed_overlap_area_mm2':removed,
                        'owners':copy.deepcopy(owners),'operation':'subtract_prior_source_material_only'}
                result.append(item)
            if owners:
                ledger.append({'candidate_id':row['id'],'original_footprint':row['footprint'],
                    'remaining_footprints':[footprint(p)for p in pieces],
                    'removed_overlap_area_mm2':removed,'retained_component_ids':[row['id']+'/partition/'+str(n)
                        if changed else row['id']for n in range(len(pieces))],
                    'owners':owners})
            occupied=unary_union([occupied,source_shape])
        actual=unary_union([shape_of(row)for row in result])
        difference=actual.symmetric_difference(occupied).area
        if difference>1e-4:
            raise ValueError('Material ownership changed the actual source union')
        audit={'enabled':True,'operation':'source_material_priority_partition',
            'priority_categories':list(order),'reason':config['reason'],
            'original_union_area_mm2':occupied.area,'final_union_area_mm2':actual.area,
            'union_symmetric_difference_mm2':difference,'changes':ledger,
            'input_count':len(rows),'output_count':len(result),
            'category_counts':dict(Counter(row['category']for row in result))}
        return result,audit,[]
    except (KeyError,TypeError,ValueError,AttributeError) as error:
        return rows,None,[{'code':'source_material_partition_invalid','detail':str(error)}]
