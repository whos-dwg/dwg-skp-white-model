import copy
import json
from pathlib import Path
import tempfile
import unittest

from dwg_semantic.slabs import prepare_slab_review, write_slab_review


def text(key,value,x=500,y=500,**extra):
    return {'entity_id':key,'handle':key,'type':'TEXT','owner_block':'*Model_Space','layer':'Notes','text':value,'position':{'x':x,'y':y},**extra}


def crossed_frame(layer='Supplier-Slab-Regions'):
    points=[[0,0],[1000,0],[1000,1000],[0,1000]]
    frame={'entity_id':'frame','handle':'frame','type':'LWPOLYLINE','owner_block':'*Model_Space','layer':layer,
           'closed':True,'vertices':[dict(zip(('x','y'),point))for point in points]}
    diagonals=[{'entity_id':f'diagonal-{index}','handle':f'diagonal-{index}','type':'LINE','owner_block':'*Model_Space','layer':'Symbols',
                'start':dict(zip(('x','y'),first)),'end':dict(zip(('x','y'),second))}
               for index,(first,second) in enumerate(((points[0],points[2]),(points[1],points[3])))]
    return [frame,*diagonals]


def bent_frame(layer='Supplier-Slab-Regions', endpoint_error=0, split=False):
    # A generic normalized version of the architectural two-leg opening mark:
    # diagonally opposite corners and a knee strictly inside the region.
    frame={'entity_id':'frame','handle':'frame','type':'LWPOLYLINE','owner_block':'*Model_Space','layer':layer,
           'closed':True,'vertices':[{'x':x,'y':y}for x,y in [[0,0],[2600,0],[2600,400],[0,400]]]}
    points=[[2600,400],[130,280],[endpoint_error,0]]
    if split:
        marks=[{'entity_id':f'bend-{i}','handle':f'bend-{i}','type':'LINE','owner_block':'*Model_Space','layer':'Symbols',
                'start':dict(zip(('x','y'),a)),'end':dict(zip(('x','y'),b))}for i,(a,b)in enumerate(zip(points,points[1:]))]
    else:
        marks=[{'entity_id':'bend','handle':'bend','type':'LWPOLYLINE','owner_block':'*Model_Space','layer':'Symbols',
                'closed':False,'vertices':[dict(zip(('x','y'),p))for p in points]}]
    return [frame,*marks]


class SlabReviewTests(unittest.TestCase):
    def document(self,*entities):
        return {'intake':{'source_sha256':'fixture','units':'mm'},'entities':list(entities)}

    def review(self,document,**options):
        return prepare_slab_review(document,{},**options)

    def test_unknown_elevation_units_are_not_guessed_from_drawing_mm(self):
        level=self.review(self.document(text('A','H-0.550')))['annotations'][0]['levels'][0]
        self.assertEqual(-.55,level['numeric_value'])
        self.assertIsNone(level['value_mm'])
        self.assertFalse(level['accepted_for_slab'])

    def test_annotation_units_and_structural_surface_are_separate(self):
        rows=self.review(self.document(text('A','SSL -0.100 m'),text('B','BOS +3.950 m'),text('C','FFL ±0.000 m')))['annotations']
        self.assertEqual(['structural_top','structural_bottom','finished_level'],[row['levels'][0]['level_kind']for row in rows])
        self.assertEqual([-100,3950,0],[row['levels'][0]['value_mm']for row in rows])

    def test_decimals_on_unknown_H_are_not_automatically_structural_levels(self):
        level=self.review(self.document(text('A','H-0.550')),elevation_unit='m')['annotations'][0]['levels'][0]
        self.assertEqual('unspecified_relative',level['level_kind'])
        self.assertEqual('unresolved',level['applies_to'])
        self.assertEqual(-550,level['value_mm'])

    def test_railing_curtain_and_ceiling_heights_do_not_become_slab_elevations(self):
        rows=self.review(self.document(text('A','H+2.340 玻璃栏杆高度'),text('B','H+0.540 幕墙高度'),text('C','3950 A.F.F.',layer='I-CEILING')))['annotations']
        self.assertEqual(3,len(rows))
        self.assertTrue(all(row['excluded_from_slab_elevation'] for row in rows))

    def test_aff_height_unit_is_independent_and_target_surface_is_unresolved(self):
        result=self.review(self.document(text('A','3950 A.F.F.',layer='I-CEILING')),elevation_unit='m')
        row=result['annotations'][0]
        self.assertEqual([],row['levels'])
        height=row['height_candidates'][0]
        self.assertEqual(3950,height['numeric_value'])
        self.assertIsNone(height['value_mm'])
        self.assertEqual('unresolved_separate_from_elevation_unit',height['unit_basis'])
        self.assertEqual('finished_floor',height['reference_surface'])
        self.assertEqual('unresolved',height['target_surface'])
        self.assertIn('clearance_height_candidate',row['roles'])
        self.assertEqual('unresolved_requires_user_confirmation',row['owner_status'])
        self.assertFalse(height['accepted_for_slab'])
        self.assertEqual(0,result['counts']['elevation_annotations'])

    def test_aff_before_after_units_and_attribute_values_are_retained(self):
        rows=self.review(self.document(text('A','H+3.950mAFF 天花'),text('B','AFF: 3950 mm 板底'),
                                       text('C','3950',type='ATTRIB',tag='AFF')),elevation_unit='m')['annotations']
        self.assertEqual([3950,3950,None],[row['height_candidates'][0]['value_mm']for row in rows])
        self.assertTrue(all(not row['levels']for row in rows))
        self.assertEqual(['finished_ceiling','unresolved','unresolved'],
                         [row['height_candidates'][0]['target_surface']for row in rows])
        self.assertTrue(all(not row['height_candidates'][0]['accepted_for_slab']for row in rows))

    def test_aff_railing_is_not_automatically_a_slab_or_clearance(self):
        row=self.review(self.document(text('A','玻璃栏杆 1200mm AFF')))['annotations'][0]
        self.assertEqual(1200,row['height_candidates'][0]['value_mm'])
        self.assertNotIn('clearance_height_candidate',row['roles'])
        self.assertTrue(row['excluded_from_slab_parameters'])
        self.assertEqual([],row['owner_candidates'])

    def test_bounded_cross_uses_authorized_opening_default_without_assigning_owner(self):
        result=self.review(self.document(*crossed_frame('Vendor-X12')))
        region=result['region_candidates'][0]
        self.assertEqual('default_opening_candidate_requires_scope_review',region['interpretation'])
        self.assertEqual({'opening'},{r['role']for r in region['semantic_candidates']})
        self.assertEqual('authorized_bounded_mark_default',region['default_decision']['basis'])
        self.assertEqual('unresolved_requires_user_confirmation',region['owner_status'])
        self.assertEqual(1,len(result['opening_proposals']))
        self.assertIsNone(result['opening_proposals'][0]['applies_to_system_id'])
        self.assertFalse(region['automatically_subtracted'])
        self.assertFalse(region['accepted_for_slab'])
        self.assertEqual(['diagonal-0','diagonal-1','frame'],region['symbols'][0]['source_entity_ids'])
        self.assertEqual('geometric_symbol_only',region['symbols'][0]['evidence_role'])
        self.assertIn('opening',region['symbols'][0]['does_not_establish'])

    def test_numeric_H_cannot_cancel_recognized_opening_default(self):
        result=self.review(self.document(*crossed_frame('6jd(底板)'),text('A','H-0.200')),elevation_unit='m')
        region=result['region_candidates'][0]
        self.assertEqual('default_opening_candidate_requires_scope_review',region['interpretation'])
        self.assertEqual({'opening'},{row['role']for row in region['semantic_candidates']})
        self.assertEqual('unresolved',result['annotations'][0]['levels'][0]['applies_to'])
        self.assertEqual(-200,result['annotations'][0]['levels'][0]['value_mm'])
        self.assertEqual('named_bottom_plate',region['owner_candidates'][0]['owner'])
        self.assertFalse(region['owner_candidates'][0]['accepted'])
        self.assertIn('foundation_slab',region['owner_candidates'][0]['modeled_role_candidates'])
        self.assertFalse(region['automatically_subtracted'])

    def test_explicit_top_plate_opening_note_is_a_separate_unconfirmed_candidate(self):
        result=self.review(self.document(*crossed_frame(),text('A','顶板留洞')))
        region=result['region_candidates'][0]
        self.assertEqual('opening_candidate_requires_scope_review',region['interpretation'])
        self.assertEqual({'opening'},{row['role']for row in region['semantic_candidates']})
        owner=region['semantic_candidates'][0]['owner_candidates'][0]
        self.assertEqual('named_top_plate',owner['owner'])
        self.assertEqual('annotation_text',owner['evidence']['kind'])
        self.assertFalse(owner['accepted'])
        self.assertFalse(region['automatically_subtracted'])

    def test_drop_and_opening_conflicts_and_negations_remain_visible(self):
        conflict=self.review(self.document(*crossed_frame(),text('A','降板 H-0.200；顶板留洞')))['region_candidates'][0]
        self.assertEqual('opening_and_level_change_conflict',conflict['interpretation'])
        result=self.review(self.document(*crossed_frame(),text('A','取消顶板留洞；降板 H-0.200')))
        self.assertIn('opening_note_negated',result['annotations'][0]['roles'])
        self.assertNotIn('opening_note',result['annotations'][0]['roles'])
        self.assertEqual({'level_change'},{row['role']for row in result['region_candidates'][0]['semantic_candidates']})

    def test_nearby_text_alone_does_not_assign_a_hole_but_owned_leader_is_evidence(self):
        nearby=self.review(self.document(*crossed_frame(),text('A','顶板留洞',x=1100,y=500)))['region_candidates'][0]
        self.assertEqual('nearby_only',nearby['nearby_annotation_candidates'][0]['relationship'])
        self.assertEqual('authorized_bounded_mark_default',nearby['default_decision']['basis'])
        self.assertFalse(any(r.get('source_entity_id')=='A'for r in nearby['semantic_candidates']))
        linked=text('A','顶板留洞',x=3000,y=500,type='MLEADER',leader_lines=[{'start_world':{'x':500,'y':500},'end_world':{'x':3000,'y':500}}])
        actual=self.review(self.document(*crossed_frame(),linked))['region_candidates'][0]
        self.assertEqual('leader_line_start_inside_candidate',actual['nearby_annotation_candidates'][0]['relationship'])
        self.assertEqual('opening_candidate_requires_scope_review',actual['interpretation'])
        self.assertFalse(actual['accepted_for_slab'])

    def test_partial_or_unrelated_diagonals_are_not_reported_as_cross(self):
        frame=crossed_frame()
        self.assertFalse(self.review(self.document(*frame[:2]))['region_candidates'][0]['symbols'])
        frame[-1]['start']={'x':2000,'y':1000}
        self.assertFalse(self.review(self.document(*frame))['region_candidates'][0]['symbols'])

    def test_structural_marker_nearby_does_not_overwrite_architectural_level(self):
        rows=self.review(self.document(text('A','H-0.050'),text('B','(结)',y=400)))['annotations']
        self.assertEqual(1,len(rows[0]['structural_marker_candidates']))
        self.assertEqual('unspecified_relative',rows[0]['levels'][0]['level_kind'])

    def test_datum_H_symbol_is_preserved_without_assigning_a_slab_surface(self):
        level=self.review(self.document(text('A','H')))['annotations'][0]['levels'][0]
        self.assertEqual('datum_reference',level['level_kind'])
        self.assertEqual('unresolved',level['applies_to'])

    def test_long_text_uses_extents_for_marker_candidates(self):
        value=text('A','H-0.100',x=0,y=0,world_extents={'min':{'x':0,'y':0},'max':{'x':1500,'y':200}})
        marker=text('B','(结)',x=1600,y=0,world_extents={'min':{'x':1600,'y':0},'max':{'x':1900,'y':200}})
        result=self.review(self.document(value,marker))['annotations'][0]
        self.assertEqual(100,result['structural_marker_candidates'][0]['distance_mm'])

    def test_attribute_instance_level_is_read_but_definition_is_not(self):
        instance=text('A','-0.100',type='ATTRIB',tag='SSL')
        definition=text('B','H+99.999',type='ATTDEF')
        rows=self.review(self.document(instance,definition),elevation_unit='m')['annotations']
        self.assertEqual(1,len(rows))
        self.assertEqual('structural_top',rows[0]['levels'][0]['level_kind'])

    def test_attached_attribute_is_read_when_container_geometry_is_resolved(self):
        block={'entity_id':'B','handle':'B','type':'INSERT','owner_block':'*Model_Space','layer':'Symbols','attributes':[{'handle':'A','type':'ATTRIB','tag':'FFL','text':'+0.000','position_world':{'x':20,'y':30},'instance_value':True}]}
        rows=self.review(self.document(block))['annotations']
        self.assertEqual(1,len(rows))
        self.assertEqual('FFL',rows[0]['attribute_tag'])

    def test_inline_and_placed_attribute_copies_are_not_double_counted(self):
        attribute={'handle':'A','type':'ATTRIB','tag':'SSL','text':'-0.100','instance_path':'B','parent_instance_path':'B','position_world':{'x':20,'y':30}}
        parent={'entity_id':'B','handle':'B','type':'INSERT','owner_block':'*Model_Space','layer':'Levels','instance_path':'','attributes':[attribute]}
        document=self.document(parent)
        document['resolved_instances']=[{**attribute,'entity_id':'B:A','root_space':'*Model_Space'}]
        self.assertEqual(1,len(self.review(document)['annotations']))

    def test_mep_thickness_attributes_do_not_become_rc_slab_parameters(self):
        attribute=text('A','0.004001',type='ATTRIB',tag='THICK',source_block='$LIGUAN',layer='P_FH_V')
        row=self.review(self.document(attribute),elevation_unit='m')['annotations'][0]
        self.assertIn('non_slab_component_context',row['exclusion_reasons'])
        self.assertTrue(row['excluded_from_slab_parameters'])
        self.assertIsNone(row['thickness_candidates'][0]['value_mm'])

    def test_slab_opening_notes_on_mep_layers_are_kept_as_slab_evidence(self):
        row=self.review(self.document(text('A','风 (顶板留洞)',layer='7text-设备-ch')))['annotations'][0]
        self.assertIn('opening_note',row['roles'])
        self.assertFalse(row['excluded_from_slab_parameters'])

    def test_rc_thickness_units_are_not_taken_from_elevation_units(self):
        row=self.review(self.document(text('A','板厚180')),elevation_unit='m')['annotations'][0]
        self.assertIsNone(row['thickness_candidates'][0]['value_mm'])
        self.assertFalse(row['excluded_from_slab_parameters'])

    def test_perimeter_does_not_require_a_closed_slab_entity_or_convex_hull(self):
        # Six separate exterior lines enclose an L; the upper-right notch stays.
        points=[[0,0],[10000,0],[10000,4000],[4000,4000],[4000,10000],[0,10000],[0,0]]
        entities=[{'entity_id':str(i),'handle':str(i),'type':'LINE','owner_block':'*Model_Space','layer':'Exterior','start':dict(zip(('x','y'),a)),'end':dict(zip(('x','y'),b))} for i,(a,b) in enumerate(zip(points,points[1:]))]
        boundary=self.review(self.document(*entities),boundary_layers=['Exterior'])['boundaries'][0]
        self.assertEqual(64,boundary['area_m2'])
        self.assertEqual(6,len(boundary['outer']))
        self.assertEqual('unconfirmed_building_enclosure_face',boundary['status'])

    def test_disconnected_wall_fragments_do_not_become_a_bounding_rectangle(self):
        entity={'entity_id':'1','handle':'1','type':'LINE','owner_block':'*Model_Space','layer':'Exterior','start':{'x':0,'y':0},'end':{'x':10000,'y':0}}
        result=self.review(self.document(entity),boundary_layers=['Exterior'])
        self.assertFalse(result['boundaries'])
        self.assertFalse(result['native_slab_generation'])

    def test_disconnected_exteriors_are_each_kept_with_only_their_outer_sources(self):
        def rectangle(identity,x,y,w,h):
            return {'entity_id':identity,'handle':identity,'type':'LWPOLYLINE','owner_block':'*Model_Space',
                    'layer':'Exterior','closed':True,'vertices':[{'x':a,'y':b}for a,b in [(x,y),(x+w,y),(x+w,y+h),(x,y+h)]]}
        # The small enclosed room is not a third building/slab candidate.
        main=rectangle('main',0,0,12000,8000)
        annex=rectangle('annex',18000,0,4000,4000)
        room=rectangle('nested-room',2000,2000,2000,2000)
        # A line touching the outside at one endpoint contributes no edge.
        touching={'entity_id':'touch','handle':'touch','type':'LINE','owner_block':'*Model_Space','layer':'Exterior',
                  'start':{'x':0,'y':6000},'end':{'x':2000,'y':6000}}
        result=self.review(self.document(main,annex,room,touching),boundary_layers=['Exterior'])
        self.assertEqual([96,16],[b['area_m2']for b in result['boundaries']])
        self.assertEqual([['main'],['annex']],[b['source_entity_ids']for b in result['boundaries']])
        self.assertEqual(1,len(result['boundaries'][0]['unclassified_interior_rings']))

    def test_connected_internal_partition_faces_merge_into_one_gross_slab(self):
        outer={'entity_id':'outer','handle':'outer','type':'LWPOLYLINE','owner_block':'*Model_Space','layer':'Exterior','closed':True,
               'vertices':[{'x':x,'y':y}for x,y in [[0,0],[10000,0],[10000,10000],[0,10000]]]}
        divider={'entity_id':'divider','handle':'divider','type':'LINE','owner_block':'*Model_Space','layer':'Exterior',
                 'start':{'x':5000,'y':0},'end':{'x':5000,'y':10000}}
        result=self.review(self.document(outer,divider),boundary_layers=['Exterior'])
        self.assertEqual(1,len(result['boundaries']))
        self.assertEqual(100,result['boundaries'][0]['area_m2'])
        self.assertEqual(['outer'],result['boundaries'][0]['source_entity_ids'])

    def test_region_candidates_are_not_automatically_holes_or_lowered_material(self):
        opening={'entity_id':'1','handle':'1','type':'LWPOLYLINE','owner_block':'*Model_Space','layer':'6jd(顶板)','closed':False,'vertices':[{'x':0,'y':0},{'x':1000,'y':0},{'x':1000,'y':1000},{'x':0,'y':1000},{'x':0,'y':0}]}
        result=self.review(self.document(opening,text('A','顶板留洞')))
        self.assertEqual(1,len(result['region_candidates']))
        self.assertEqual('default_opening_proposed',result['region_candidates'][0]['status'])
        self.assertFalse(result['region_candidates'][0]['automatically_subtracted'])

    def test_two_leg_bent_mark_uses_actual_region_boundary_for_polyline_and_line_legs(self):
        for split in [False,True]:
            result=self.review(self.document(*bent_frame(split=split)))
            region=result['region_candidates'][0]
            self.assertEqual('bent_diagonal',region['symbols'][0]['kind'])
            self.assertEqual('opening',region['default_decision']['action'])
            self.assertEqual(1.04,region['area_m2'])
            self.assertEqual(region['outer'],result['opening_proposals'][0]['footprint']['outer'])
            self.assertEqual({'frame','bend-0','bend-1'}if split else{'frame','bend'},set(region['source_entity_ids']))

    def test_ordinary_frame_partial_L_and_straight_diagonal_are_not_bent_marks(self):
        for knee in [[0,400],[1300,200]]:
            entities=bent_frame();entities[1]['vertices'][1]=dict(zip(('x','y'),knee))
            region=self.review(self.document(*entities))['region_candidates'][0]
            self.assertEqual([],region['symbols'])
            self.assertEqual('unresolved',region['default_decision']['action'])
        region=self.review(self.document(bent_frame()[0]))['region_candidates'][0]
        self.assertEqual('bounded_outline_without_recognized_opening_mark',region['default_decision']['basis'])

    def test_symbol_matching_tolerance_does_not_move_frame_or_geometry_precision(self):
        entities=bent_frame(endpoint_error=4.9)
        strict=self.review(self.document(*entities))
        self.assertFalse(strict['region_candidates'][0]['symbols'])
        result=self.review(self.document(*entities),slab_decisions={'symbol_endpoint_tolerance_mm':5})
        symbol=result['region_candidates'][0]['symbols'][0]
        self.assertEqual([0,4.9],sorted(symbol['endpoint_deviations_mm']))
        self.assertEqual(strict['region_candidates'][0]['outer'],result['opening_proposals'][0]['footprint']['outer'])
        self.assertEqual(.1,result['default_policy']['source_geometry_precision_mm'])
        self.assertEqual(5,result['default_policy']['symbol_endpoint_tolerance_mm'])

    def test_unflagged_closed_frame_is_supported_and_rotated_bent_geometry_is_generic(self):
        entities=bent_frame();entities[0]['closed']=False
        entities[0]['vertices'].append(copy.deepcopy(entities[0]['vertices'][0]))
        # Rotate90 and translate; no source-ID or absolute-coordinate template.
        for entity in entities:
            for p in entity['vertices']:p['x'],p['y']=8000-p['y'],5000+p['x']
        region=self.review(self.document(*entities))['region_candidates'][0]
        self.assertEqual('opening',region['default_decision']['action'])
        self.assertEqual(1.04,region['area_m2'])

    def test_noisy_repeated_closure_tail_is_bounded_cleaned_and_fully_audited(self):
        entities=crossed_frame();frame=entities[0];frame['closed']=False
        frame['vertices'] += [{'x':0,'y':0},{'x':0,'y':0},{'x':0,'y':0},
                              {'x':1e-9,'y':-1e-9},{'x':0,'y':0}]
        before=copy.deepcopy(frame)
        region=self.review(self.document(*entities))['region_candidates'][0]
        self.assertEqual('opening',region['default_decision']['action'])
        audit=region['geometry_cleanup']
        self.assertEqual(9,len(audit['raw_curve_points_mm']))
        self.assertEqual(5,audit['removed_count'])
        self.assertLess(audit['max_vertex_displacement_mm'],.1)
        self.assertLess(audit['boundary_hausdorff_mm'],.1)
        self.assertFalse(audit['surviving_points_moved'])
        self.assertEqual(4,len(region['outer']))
        self.assertEqual(before,frame)

    def test_cleanup_does_not_heal_a_large_gap_or_a_self_crossing_frame(self):
        frame=bent_frame()[0];frame['closed']=False
        frame['vertices'].append({'x':.2,'y':0})
        self.assertFalse(self.review(self.document(frame))['region_candidates'])
        frame['closed']=True;frame['vertices']=[{'x':x,'y':y}for x,y in [[0,0],[1000,1000],[0,1000],[1000,0]]]
        self.assertFalse(self.review(self.document(frame))['region_candidates'])

    def test_furniture_room_and_classified_column_crosses_do_not_default_to_rc_holes(self):
        for layer in ['FURN-Slab','A-ROOM','Vendor-Slab-Column']:
            result=self.review(self.document(*crossed_frame(layer)))
            self.assertFalse(result['opening_proposals'])
            self.assertEqual('non_slab_component_context',result['region_candidates'][0]['default_decision']['basis'])
        result=prepare_slab_review(self.document(*crossed_frame()),{'components':{'columns':[{'source_entity_ids':['frame']}]}})
        self.assertFalse(result['opening_proposals'])

    def test_gross_slab_boundary_is_not_a_hole_even_with_inner_opening_note(self):
        result=self.review(self.document(*crossed_frame(),text('A','顶板留洞')),boundary_layers=['Supplier-Slab-Regions'])
        self.assertEqual('gross_slab_extent_is_not_an_opening_mark',result['region_candidates'][0]['default_decision']['basis'])
        self.assertFalse(result['opening_proposals'])

    def test_literal_drop_and_no_hole_override_symbol_defaults_but_not_source_geometry(self):
        for note,action,basis in [('降板200','level_change','explicit_lowered_slab_note'),('無孔','retain','explicit_no_opening_note')]:
            region=self.review(self.document(*crossed_frame(),text('A',note)))['region_candidates'][0]
            self.assertEqual(action,region['default_decision']['action'])
            self.assertEqual(basis,region['default_decision']['basis'])
            self.assertEqual(1,region['area_m2'])

    def test_exact_full_id_override_wins_and_short_handle_is_not_an_alias(self):
        entities=crossed_frame();entities[0]['entity_id']='source:placed:frame'
        doc=self.document(*entities)
        reason={'action':'retain','reason':'Confirmed solid service room, source-specific exception'}
        unmatched=self.review(doc,slab_decisions={'region_overrides':{'frame':reason}})
        self.assertEqual(['frame'],unmatched['unmatched_region_override_ids'])
        self.assertEqual('opening',unmatched['region_candidates'][0]['default_decision']['action'])
        before=copy.deepcopy(doc)
        accepted=self.review(doc,slab_decisions={'region_overrides':{'source:placed:frame':reason}})
        self.assertEqual('retain',accepted['region_candidates'][0]['default_decision']['action'])
        self.assertEqual('explicit_source_entity_override',accepted['region_candidates'][0]['default_decision']['basis'])
        self.assertEqual(before,doc)
        self.assertFalse(accepted['opening_proposals'])

    def test_override_missing_reason_and_conflicting_marker_actions_are_unresolved(self):
        bad=self.review(self.document(*crossed_frame()),slab_decisions={'region_overrides':{'frame':{'action':'retain'}}})
        self.assertTrue(bad['decision_issues'])
        self.assertEqual('unresolved',bad['region_candidates'][0]['default_decision']['action'])
        config={'region_overrides':{'diagonal-0':{'action':'opening','reason':'one interpretation'},
                                    'diagonal-1':{'action':'level_change','reason':'conflicting interpretation'}}}
        conflict=self.review(self.document(*crossed_frame()),slab_decisions=config)
        self.assertEqual('slab_region_overrides_conflict',conflict['decision_issues'][0]['code'])
        config['region_overrides']['frame']={'action':'retain','reason':'Whole boundary confirmed solid'}
        final=self.review(self.document(*crossed_frame()),slab_decisions=config)
        self.assertEqual('retain',final['region_candidates'][0]['default_decision']['action'])
        self.assertFalse(final['decision_issues'])

    def test_thickness_default_is_separate_from_explicit_source_candidates_and_special_policy(self):
        normal=self.review(self.document(*crossed_frame()))
        self.assertEqual(200,normal['region_candidates'][0]['thickness_proposal']['value_mm'])
        self.assertEqual('authorized_white_model_default',normal['thickness_proposal']['basis'])
        explicit=self.review(self.document(*crossed_frame(),text('T','板厚180mm')))
        proposal=explicit['region_candidates'][0]['thickness_proposal']
        self.assertEqual(180,proposal['value_mm'])
        self.assertFalse(proposal['automatic_model_parameter_binding'])
        unknown=self.review(self.document(*crossed_frame(),text('T','板厚180')))
        self.assertIsNone(unknown['region_candidates'][0]['thickness_proposal']['value_mm'])
        conflict=self.review(self.document(*crossed_frame(),text('T','板厚180mm'),text('U','板厚220mm')))
        self.assertEqual('explicit_source_thickness_conflict',conflict['region_candidates'][0]['thickness_proposal']['status'])
        special=self.review(self.document(*crossed_frame()),slab_decisions={'thickness_policy':'explicit_required'})
        self.assertIsNone(special['thickness_proposal']['value_mm'])

    def test_default_opening_proposal_can_be_reviewed_bound_and_prepared_without_skp(self):
        from dwg_semantic.slab_modeling import prepare_slabs
        entities=crossed_frame();doc=self.document(*entities)
        review=self.review(doc)
        proposal=review['opening_proposals'][0]
        config={'enabled':True,'confirmed_scope':True,'derived_tag':{'kind':'derived','tag':'Reviewed RC','source_layer':'Reviewed RC'},
                'systems':[{'id':'F','role':'floor','extent':{'outer':[[-1000,-1000],[2000,-1000],[2000,2000],[-1000,2000]],'holes':[]},
                    'source_entity_ids':['frame'],'default_top_z_mm':0,'openings':[{'id':'reviewed-SR001','footprint':proposal['footprint'],
                        'source_entity_ids':proposal['source_entity_ids'],'applies_to_system_id':'F'}]}]}
        prepared=prepare_slabs(doc,config)
        self.assertEqual([],prepared['blockers'])
        self.assertEqual(8000000,sum(g['area_mm2']for g in prepared['groups']))
        self.assertTrue(all(g['height_mm']==200 for g in prepared['groups']))
        self.assertEqual('authorized_white_model_default',prepared['groups'][0]['evidence']['thickness_provenance']['basis'])

    def test_review_does_not_mutate_input_or_authorize_new_skp(self):
        document=self.document(text('A','板底标高 +3.950 m'))
        before=copy.deepcopy(document)
        result=self.review(document)
        self.assertEqual(before,document)
        self.assertFalse(result['native_slab_generation'])
        self.assertEqual('candidate_review_only',result['stage'])

    def test_outputs_are_review_artifacts_and_never_overwrite_existing_version(self):
        root=Path(__file__).resolve().parents[1]/'work/slab-tests'
        root.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as parent:
            self.assertEqual(root.resolve(),Path(parent).resolve().parent)
            path=Path(parent)/'review'
            result=self.review(self.document(text('A','H-0.100')))
            write_slab_review(result,path)
            self.assertEqual(result,json.loads((path/'slab-review.json').read_text(encoding='utf-8')))
            self.assertTrue((path/'slab-review.svg').is_file())
            self.assertFalse(list(path.rglob('*.skp')))
            with self.assertRaises(FileExistsError):write_slab_review(result,path)


if __name__=='__main__':unittest.main()
