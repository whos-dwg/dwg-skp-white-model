"""Prepared contours must resolve the same source-bound metal decisions."""
from pathlib import Path
import copy
import unittest

from dwg_semantic.classifier import classify_document,load_rules
from dwg_semantic.modeling import prepare_model,_json_hash
from dwg_semantic.prepared import validate_prepared
from test_nested_curtain_accessories import fixture

ROOT=Path(__file__).resolve().parents[1]
METAL='4金属构件断面'


class PreparedNestedDecisionTests(unittest.TestCase):
    def setUp(self):
        self.document=fixture(('A','B'))
        self.classification=classify_document(self.document,load_rules(ROOT/'config/default_rules.json'))
        self.decisions={'default_height_mm':3950,'layer_mapping':{METAL:{'category':'reference'},'area':{'category':'reference'}},
                        'prepared_categories_by_primary_layer':{METAL:['curtain_accessory']},
                        'object_overrides':{
                            'source:A:LEAF':{'category':'curtain_accessory','semantic_subrole':'balustrade_metal_profile','base_z_mm':0,'height_mm':1800,'footprint_confirmed':True},
                            'source:B:LEAF':{'category':'reference','reason':'Reviewed repeated instance outside this model scope.'}}}
        plan=prepare_model(self.document,self.classification,self.decisions)
        self.assertEqual([],plan['blockers'])
        self.decisions['component_height_rules']={g['id']:{'base_z_mm':g['base_z_mm'],'height_mm':g['height_mm']}for g in plan['groups']}
        self.plan=prepare_model(self.document,self.classification,self.decisions)

    def test_prepared_exact_choices_resolve_source_requests(self):
        result=validate_prepared(self.plan,self.document,self.classification,self.decisions)
        self.assertEqual([],result['blockers'])
        self.assertEqual([],result['semantic_review_audit']['unresolved_request_ids'])

    def test_prepared_cannot_claim_audit_resolution_without_exact_choices(self):
        decisions=copy.deepcopy(self.decisions)
        del decisions['object_overrides']['source:A:LEAF']
        payload=copy.deepcopy(self.plan)
        payload['decisions_digest']=_json_hash(decisions)
        before=copy.deepcopy(payload)
        result=validate_prepared(payload,self.document,self.classification,decisions)
        pending=[r for r in result['blockers']if r['code']=='nested_accessory_decision_requires_confirmation']
        self.assertEqual(1,len(pending))
        self.assertEqual(['source:A:LEAF'],pending[0]['source_entity_ids'])
        self.assertEqual(1,len(result['semantic_review_audit']['unresolved_request_ids']))
        self.assertEqual(before,payload)

    def test_prepared_disposition_must_match_actual_material(self):
        decisions=copy.deepcopy(self.decisions)
        decisions['object_overrides']['source:A:LEAF']={'category':'reference','reason':'Explicitly keep this source as a reference.'}
        payload=copy.deepcopy(self.plan)
        payload['decisions_digest']=_json_hash(decisions)
        result=validate_prepared(payload,self.document,self.classification,decisions)
        pending=[r for r in result['blockers']if r['code']=='nested_accessory_decision_requires_confirmation']
        self.assertEqual(1,len(pending))
        self.assertIn('confirmed_source_not_materialized_in_its_selected_disposition',pending[0]['source_decisions'][0]['issues'])


if __name__=='__main__':unittest.main()
