"""Independent negative/positive contract checks for the fixed standard.

All data is synthetic. No native files, approvals, CAD or active models are used.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
import tempfile
import unittest

from shapely.geometry import Point

from dwg_semantic.modeling_standard import (apply_standard_decisions, enforce_standard_payload,
                                           filter_fixed_fire_groups, seed_standard)
from dwg_semantic.modeling import prepare_model, write_model
from dwg_semantic.pipeline import _draft_payload
from dwg_semantic.plan_geometry import SourceIndex, shape_of
from dwg_semantic.plan_recognition import DEFAULTS
from dwg_semantic.slab_recognition import recognize_slab_openings
from test_fixed_modeling_standard import fixture
from test_modeling import rectangle
from test_slab_modeling import footprint, system, decisions as slab_decisions

ROOT=next(path for path in Path(__file__).resolve().parents if(path/'pyproject.toml').is_file())


class FixedPolicyAudit(unittest.TestCase):
    def body_payload(self, category='curtain_accessory', *, semantic_only=False):
        doc,decisions=fixture()
        layer='Supplier-Metal-01'if semantic_only else'DOOR_FIRE'
        body=rectangle('fire-body',layer=layer,x=6000,y=2000,width=900,depth=50)
        doc['entities'].append(body)
        decisions['layer_mapping'][layer]={'category':category,'height_mm':3000,'base_z_mm':0,'footprint_confirmed':True}
        if semantic_only:
            decisions.setdefault('object_overrides',{})['fire-body']={'semantic_subrole':'fire_door_body'}
        payload=_draft_payload(doc,{},decisions)
        self.assertEqual([],payload['blockers'],payload['blockers'])
        return doc,decisions,payload

    def test_fire_body_exclusion_is_not_limited_to_curtain_categories(self):
        for category in ('architectural_wall','structural_wall','column','curtain_accessory'):
            with self.subTest(category=category):
                doc,decisions,payload=self.body_payload(category)
                result=enforce_standard_payload(doc,payload,decisions,{})
                self.assertFalse(any('fire-body'in row.get('source_entity_ids',[])for row in result['groups']))

    def test_explicit_body_semantics_does_not_depend_on_supplier_layer_names(self):
        doc,decisions,payload=self.body_payload(semantic_only=True)
        result=enforce_standard_payload(doc,payload,decisions,{})
        self.assertFalse(any('fire-body'in row.get('source_entity_ids',[])for row in result['groups']))

    def test_excluded_body_references_are_native_writer_safe_and_unique(self):
        doc,decisions,payload=self.body_payload()
        # One placed body may be sliced into several final pieces; it remains
        # one source reference, not one duplicate reference per output fragment.
        body=next(row for row in prepare_model(doc,{},decisions)['groups']if'fire-body'in row.get('source_entity_ids',[]))
        payload['groups'].append(body)
        duplicate=copy.deepcopy(body);duplicate['id']+='-second-slice'
        payload['groups'].append(duplicate)
        result=enforce_standard_payload(doc,payload,decisions,{})
        refs=[row for row in result['references']if row.get('source_entity_ids')==['fire-body']]
        self.assertEqual(1,len(refs))
        reference=refs[0]
        self.assertEqual('reference_edges',reference['mode'])
        self.assertTrue(reference['name'])
        self.assertIn(reference['tag'],{row['tag']for row in result['tag_mapping']})
        self.assertEqual(4,len(reference['segments']))
        self.assertEqual('DOOR_FIRE',reference['original_layer'])
        self.assertEqual(['fire-body'],reference['source_handles'])
        second=enforce_standard_payload(doc,result,decisions,{})
        self.assertEqual(1,len([row for row in second['references']if row.get('source_entity_ids')==['fire-body']]))

    def test_a_host_door_lintel_is_not_the_excluded_door_body(self):
        doc,decisions,payload=self.body_payload('architectural_wall')
        lintel=next(row for row in prepare_model(doc,{},decisions)['groups']if'fire-body'in row.get('source_entity_ids',[]))
        lintel['semantic_role']='door_lintel';lintel['id']='actual-host-door-lintel'
        before=copy.deepcopy(lintel)
        result=filter_fixed_fire_groups(doc,[lintel],decisions)
        self.assertEqual(before,lintel)
        self.assertIn('actual-host-door-lintel',{row['id']for row in result})

    @unittest.skipUnless(os.environ.get('DWG_RUN_NATIVE_TESTS')=='1','Opt-in actual SketchUp C API')
    def test_excluded_fire_body_reference_round_trips_through_actual_skp(self):
        doc,decisions,payload=self.body_payload()
        result=enforce_standard_payload(doc,payload,decisions,{})
        self.assertEqual([],result['blockers'],result['blockers'])
        with tempfile.TemporaryDirectory(dir=ROOT/'work'/'slab-tests')as folder:
            qa=write_model(result,Path(folder)/'fire-reference.skp')
            self.assertTrue(qa['all_pass'],[row for row in qa['checks']if not row['pass']])

    def test_excluded_body_dimensions_do_not_block_but_real_opening_clearance_still_does(self):
        for category,should_preserve in [('curtain_accessory',False),('opening',True)]:
            with self.subTest(category=category):
                doc,decisions=fixture()
                doc['entities'].append(rectangle('fire-body',layer='DOOR_FIRE',x=6000,y=2000,width=900,depth=50))
                decisions['slabs']['enabled']=False
                decisions['layer_mapping']['DOOR_FIRE']={'category':category,'height_mm':None,'footprint_confirmed':True}
                payload=_draft_payload(doc,{},decisions)
                self.assertTrue(any(row.get('code')=='height_requires_confirmation'and row.get('entity_id')=='fire-body'
                                    for row in payload['blockers']))
                result=enforce_standard_payload(doc,payload,decisions,{})
                remaining=[row for row in result['blockers']if row.get('code')=='height_requires_confirmation'
                           and row.get('entity_id')=='fire-body']
                self.assertEqual(should_preserve,bool(remaining))
                self.assertEqual(1,len([row for row in result['references']if row.get('source_entity_ids')==['fire-body']]))

    def test_complete_door_assembly_geometry_blocker_is_not_cleared_as_a_body_dimension(self):
        doc,decisions,payload=self.body_payload()
        issue={'code':'complete_door_assembly_requires_candidate_decision','source_entity_ids':['fire-body']}
        payload['blockers'].append(issue)
        result=enforce_standard_payload(doc,payload,decisions,{})
        self.assertIn(issue,result['blockers'])

    def test_fixed_vertical_missing_height_asks_for_actual_underside_not_an_arbitrary_height(self):
        doc,decisions=fixture();decisions['slabs']['enabled']=False
        result=enforce_standard_payload(doc,_draft_payload(doc,{},decisions),decisions,{})
        issues=[row for row in result['blockers']if row.get('entity_id')=='wall']
        self.assertTrue(any(row.get('code')=='fixed_standard_overhead_underside_required'for row in issues),issues)
        self.assertFalse(any(row.get('code')=='height_requires_confirmation'for row in issues))
        self.assertTrue(any(row.get('reason')=='fixed_standard_overhead_underside_required'for row in result['references']
                            if row.get('id')=='wall'))

    def stair_candidate(self, key='stairs'):
        return {'id':key,'footprint':footprint(200,200,4600,3600),'source_entity_ids':['stair-source','wall-source'],
                'includes_landings':True,'applies_to_system_id':None,'source_plane_z_mm':0}

    def stair_config(self):
        return seed_standard({'slabs':slab_decisions(system('F',role='floor'),system('O',role='overhead',default_top_z_mm=3400))})

    def classification(self,candidate):
        return {'automatic_plan':{'slab_openings':{'staircase_openings':[candidate],'stair_boundary_review_requests':[]}}}

    def test_whole_stair_scope_does_not_prove_which_slab_is_cut(self):
        config=self.stair_config()
        issues=apply_standard_decisions(config,self.classification(self.stair_candidate()))
        self.assertTrue(any(row['code']=='whole_stair_opening_system_binding_required'for row in issues),issues)
        self.assertTrue(all(not row['openings']for row in config['slabs']['systems']))

    def test_explicit_stair_binding_cuts_only_its_target_and_preserves_whole_platform(self):
        config=self.stair_config();candidate=self.stair_candidate()
        config['slabs']['stair_opening_bindings']={'stairs':{'applies_to_system_ids':['O'],'reason':'Source overhead stair opening scope'}}
        issues=apply_standard_decisions(config,self.classification(candidate))
        self.assertEqual([],issues)
        floor,roof=config['slabs']['systems']
        self.assertEqual([],floor['openings'])
        self.assertEqual(1,len(roof['openings']))
        self.assertEqual('O',roof['openings'][0]['applies_to_system_id'])
        self.assertEqual(candidate['footprint'],roof['openings'][0]['footprint'])
        self.assertTrue(shape_of(roof['openings'][0]['footprint']).covers(Point(4000,3000)))
        self.assertEqual([],apply_standard_decisions(config,self.classification(candidate)))
        self.assertEqual(1,len(roof['openings']))

    def test_invalid_target_or_missing_binding_reason_is_blocking_not_cross_floor_fallback(self):
        for binding in ({'applies_to_system_ids':['missing'],'reason':'Wrong system'}, {'applies_to_system_ids':['O']}):
            with self.subTest(binding=binding):
                config=self.stair_config();config['slabs']['stair_opening_bindings']={'stairs':binding}
                issues=apply_standard_decisions(config,self.classification(self.stair_candidate()))
                self.assertTrue(issues)
                self.assertTrue(all(not row['openings']for row in config['slabs']['systems']))

    def test_source_explicit_slab_owner_and_existing_exact_id_decision_are_reused(self):
        for source_bound in (True,False):
            with self.subTest(source_bound=source_bound):
                config=self.stair_config();candidate=self.stair_candidate()
                if source_bound:
                    candidate.update(applies_to_system_id='O',slab_owner_evidence={'source_note_id':'overhead-stair-note'})
                else:
                    config['slabs']['region_overrides']={'stair-source':{'action':'opening','applies_to_system_id':'O',
                        'reason':'Already established source-bound overhead opening'}}
                issues=apply_standard_decisions(config,self.classification(candidate))
                self.assertEqual([],issues)
                self.assertEqual([],config['slabs']['systems'][0]['openings'])
                self.assertEqual(1,len(config['slabs']['systems'][1]['openings']))

    def test_contradictory_source_owner_decisions_block_without_picking_first(self):
        config=self.stair_config()
        config['slabs']['region_overrides']={
            'stair-source':{'action':'opening','applies_to_system_id':'F','reason':'Floor interpretation'},
            'wall-source':{'action':'opening','applies_to_system_id':'O','reason':'Conflicting overhead interpretation'}}
        issues=apply_standard_decisions(config,self.classification(self.stair_candidate()))
        self.assertTrue(any(row['code']=='whole_stair_opening_system_binding_invalid'for row in issues),issues)
        self.assertTrue(all(not row['openings']for row in config['slabs']['systems']))

    def test_stale_derived_stair_cut_is_not_silently_kept_after_candidate_changes(self):
        config=self.stair_config()
        config['slabs']['stair_opening_bindings']={'stairs':{'applies_to_system_ids':['O'],'reason':'Source-bound target'}}
        apply_standard_decisions(config,self.classification(self.stair_candidate()))
        changed=self.stair_candidate('changed-stairs')
        changed['footprint']=footprint(200,200,4300,3600)
        issues=apply_standard_decisions(config,self.classification(changed))
        self.assertTrue(any(row['code']in{'fixed_stair_opening_stale','stair_opening_binding_candidate_unresolved'}for row in issues),issues)

    def test_outer_hole_lock_routes_openings_by_target_not_their_storage_list(self):
        doc,config=fixture()
        opening={'id':'roof-hole','source_entity_ids':['opening'],'footprint':footprint(2000,2000,1000,1000),'applies_to_system_id':'O'}
        config['slabs']=slab_decisions(system('F',role='floor',openings=[opening]),system('O',role='overhead',default_top_z_mm=3400))
        apply_standard_decisions(config,{})
        payload=_draft_payload(doc,{},config)
        self.assertEqual([],payload['blockers'],payload['blockers'])
        result=enforce_standard_payload(doc,payload,config,{})
        self.assertFalse(any(row.get('code')=='fixed_standard_outer_or_opening_boundary_changed'for row in result['blockers']),result['blockers'])

    def stair_geometry(self, *, wall_z=0, one_polyline=False, two_flights=False):
        outline={'entity_id':'room','handle':'room','type':'LWPOLYLINE','layer':'A-WALL','closed':True,
                 'owner_block':'*Model_Space','vertices':[{'x':x,'y':y,'z':wall_z}for x,y in [[0,0],[5000,0],[5000,4000],[0,4000]]]}
        room={'id':'room','source_entity_ids':['room'],'source_plane_z_mm':wall_z,
              'footprint':footprint(0,0,5000,4000,holes=[[[200,200],[4800,200],[4800,3800],[200,3800]]])}
        lines=[]
        for flight in range(2 if two_flights else 1):
            if one_polyline:
                coords=[]
                for n in range(6):
                    coords.extend([{'x':500 if n%2==0 else 2000,'y':500+n*150,'z':0},
                                   {'x':2000 if n%2==0 else 500,'y':500+n*150,'z':0}])
                lines.append({'entity_id':'single-stair-polyline','handle':'SP','type':'POLYLINE','layer':'A-STAIR',
                              'owner_block':'*Model_Space','closed':False,'vertices':coords})
            else:
                for n in range(6):
                    lines.append({'entity_id':f'flight-{flight}-tread-{n}','handle':f'F{flight}T{n}','type':'LINE','layer':'A-STAIR',
                                  'owner_block':'*Model_Space','start':{'x':500,'y':500+n*150+flight*1900,'z':0},
                                  'end':{'x':2000,'y':500+n*150+flight*1900,'z':0}})
        doc={'entities':lines+[outline]};index=SourceIndex(doc)
        return recognize_slab_openings(index,set(index.by_id),DEFAULTS,wall_context=[room])

    def test_stair_cannot_borrow_a_room_from_a_different_source_plane(self):
        result=self.stair_geometry(wall_z=3000)
        self.assertEqual([],result['staircase_openings'])
        self.assertTrue(result['stair_boundary_review_requests'])

    def test_one_native_stair_polyline_is_not_missed_by_a_leaf_object_count_gate(self):
        result=self.stair_geometry(one_polyline=True)
        self.assertEqual(1,len(result['staircase_openings']))
        self.assertIn('room',result['staircase_openings'][0]['boundary_source_entity_ids'])

    def test_separated_flights_inside_one_enclosure_produce_one_whole_cut(self):
        result=self.stair_geometry(two_flights=True)
        self.assertEqual(1,len(result['staircase_openings']))
        self.assertEqual(12,len(result['staircase_openings'][0]['stair_mark_source_entity_ids']))


if __name__=='__main__':
    unittest.main()
