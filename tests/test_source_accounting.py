import copy
import unittest

from dwg_semantic.modeling import prepare_model
from dwg_semantic.source_accounting import final_source_accounting
from test_modeling import decisions,rectangle


class FinalSourceAccountingTests(unittest.TestCase):
    def test_explicit_reference_remainder_keeps_the_measured_uncovered_geometry_visible(self):
        doc={'entities':[rectangle('w1'),rectangle('w2',x=10000)]}
        plan=prepare_model({'entities':[rectangle('w1')]},{},decisions())
        plan['groups'][0]['source_entity_ids'].append('w2')
        config=decisions(source_dispositions={'w2':{'status':'reference','scope':'unmodeled_remainder',
            'reason':'User explicitly keeps the reviewed material; this exact source remainder is 2D evidence'}})
        audit=final_source_accounting(doc,plan,config)
        self.assertTrue(audit['all_accounted'],audit)
        row=next(row for row in audit['records']if row['source_entity_id']=='w2')
        self.assertEqual('material_provenance_with_reference_remainder',row['status'])
        self.assertGreater(row['geometry_validation']['uncovered_length_mm'],1000)
        no_provenance=copy.deepcopy(plan);no_provenance['groups'][0]['source_entity_ids'].remove('w2')
        self.assertFalse(final_source_accounting(doc,no_provenance,config)['all_accounted'])

    def test_explicit_2d_network_evidence_is_not_claimed_as_extruded_boundary(self):
        from test_automatic_plan_recognition import rect,line
        doc={'entities':[rect('wall','A-WALL',0,0,3000,200),
                         line('locator','Defpoints',(10000,0),(10000,1000))]}
        candidate={'id':'network','category':'architectural_wall','source_entity_ids':['wall','locator'],
                   'source_layers':['A-WALL','Defpoints'],'footprint':{'outer':[[0,0],[3000,0],[3000,200],[0,200]],'holes':[]}}
        config={'layer_mapping':{'A-WALL':{'category':'architectural_wall','height_mm':3200,'footprint_confirmed':True}},
                'object_overrides':{'network':{'category':'architectural_wall','height_mm':3200,
                                              'footprint_confirmed':True,'primary_source_layer':'A-WALL'},
                    'locator':{'category':'reference','reason':'Reviewed auxiliary locator, not physical material'}}}
        plan=prepare_model(doc,{'automatic_plan':{'wall_materials':[candidate]}},config)
        self.assertFalse(plan['blockers'],plan['blockers'])
        self.assertEqual(['locator'],plan['groups'][0]['reference_source_entity_ids'])
        broken=copy.deepcopy(config);broken['object_overrides'].pop('locator')
        self.assertFalse(final_source_accounting(doc,plan,broken)['all_accounted'])

    def test_source_mark_inside_an_actual_slab_hole_is_a_modifier_not_missing_material(self):
        from dwg_semantic.slab_modeling import prepare_slabs
        from test_slab_modeling import system,decisions as slab_decisions,footprint
        from test_automatic_plan_recognition import rect,line
        doc={'entities':[rect('boundary','A-OUTLINE',0,0,10000,8000),
                         line('mark','SHAFT',(3000,2500),(3500,3000))]}
        config=slab_decisions(system(assembly_mode='whole_uniform_thickness',
            openings=[{'id':'hole','source_entity_ids':['mark'],'footprint':footprint(2500,2000,1500,1500),
                       'applies_to_system_id':'floor'}],annotation_source_ids=[]))
        result=prepare_slabs(doc,config)
        self.assertFalse(result['blockers'])
        audit=final_source_accounting(doc,result,{'slabs':config})
        self.assertTrue(audit['all_accounted'],audit)
        mark=next(row for row in audit['records']if row['source_entity_id']=='mark')
        self.assertEqual('slab_opening_modifier_evidence',mark['status'])
        broken=copy.deepcopy(result)
        broken['groups'][0]['construction_cells'][0]['holes']=[]
        self.assertFalse(final_source_accounting(doc,broken,{'slabs':config})['all_accounted'])
        self.assertFalse(final_source_accounting(doc,result,{'slabs':{}})['all_accounted'])
        distant=copy.deepcopy(doc)
        distant['entities'][1].update(start={'x':50000,'y':50000,'z':0},end={'x':51000,'y':51000,'z':0})
        self.assertFalse(final_source_accounting(distant,result,{'slabs':config})['all_accounted'])

    def test_declaring_a_distant_source_id_does_not_prove_geometry_coverage(self):
        doc={'entities':[rectangle('w1'),rectangle('w2',x=10000)]}
        plan=prepare_model({'entities':[rectangle('w1')]},{},decisions())
        plan['groups'][0]['source_entity_ids'].append('w2')
        audit=final_source_accounting(doc,plan,decisions())
        self.assertFalse(audit['all_accounted'])
        record=next(row for row in audit['records']if row['source_entity_id']=='w2')
        self.assertEqual('declared_source_geometry_not_covered_by_final_material',record['reason'])
        self.assertGreater(record['geometry_validation']['uncovered_length_mm'],1000)

    def test_prepared_reference_label_cannot_invent_a_missing_scope_decision(self):
        doc={'entities':[rectangle('w1'),rectangle('w2',x=10000)]}
        plan=prepare_model({'entities':[rectangle('w1')]},{},decisions())
        plan['references']=[{'id':'w2','source_entity_ids':['w2'],'reason':'user_confirmed_reference'}]
        self.assertFalse(final_source_accounting(doc,plan,decisions())['all_accounted'])
        config=decisions(source_dispositions={'w2':{'status':'reference','reason':'Reviewed out-of-scope building wing'}})
        self.assertTrue(final_source_accounting(doc,plan,config)['all_accounted'])

    def test_unmatched_or_invalid_dispositions_are_never_silently_ignored(self):
        doc={'entities':[rectangle('w1')]}
        plan=prepare_model(doc,{},decisions())
        for declaration in ({'missing':{'status':'reference','reason':'typo'}},{'w1':{'status':'exclude'}},[]):
            with self.subTest(declaration=declaration):
                result=final_source_accounting(doc,plan,decisions(source_dispositions=declaration))
                self.assertFalse(result['all_accounted'])
                self.assertTrue(result['invalid_declarations'])

    def test_explicit_source_footprint_repair_is_checked_against_the_approved_shape(self):
        doc={'entities':[rectangle('w1')]}
        config=decisions(object_overrides={'w1':{'footprint':{'outer':[[100,0],[2800,0],[2800,200],[100,200]],'holes':[]}}})
        plan=prepare_model(doc,{},config)
        self.assertFalse(plan['blockers'])
        self.assertTrue(plan['final_source_accounting']['all_accounted'])

    def test_confirmed_rc_subtraction_accounts_for_fully_and_partially_removed_sources(self):
        from dwg_semantic.slab_contacts import resolve_building_slab_cuts
        from test_slab_contacts import material, rc_material, cut_config, footprint
        doc={'entities':[rectangle('curtain',width=1000,depth=100)]}
        original=material(source_entity_ids=['curtain'])
        for width in (500,1000):
            with self.subTest(cutter_width=width):
                slab=rc_material(base=0,height=3950,shape=footprint(0,0,width,100))
                config=cut_config()
                cuts=resolve_building_slab_cuts([original],[slab],config)
                payload={'groups':cuts['groups']+[slab], 'base_component_cut_audit':cuts['audit']}
                audit=final_source_accounting(doc,payload,decisions(building_slab_cuts=config))
                self.assertTrue(audit['all_accounted'],audit)
                self.assertEqual(['CURTAIN-001'],audit['records'][0]['authorized_rc_subtraction_original_ids'])
                if width==1000:
                    self.assertEqual('material_subtracted_by_confirmed_rc',audit['records'][0]['status'])
                tampered=copy.deepcopy(payload)
                tampered['base_component_cut_audit']['cuts'][0]['removed_volume_mm3']+=1
                self.assertFalse(final_source_accounting(doc,tampered,decisions(building_slab_cuts=config))['all_accounted'])
                self.assertFalse(final_source_accounting(doc,payload,decisions())['all_accounted'])
