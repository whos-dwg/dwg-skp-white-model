import copy
import unittest
from dwg_semantic.modeling import _json_hash
from dwg_semantic.prepared import validate_prepared


class PreparedContourTests(unittest.TestCase):
    def setUp(self):
        self.document = {"intake": {"source_sha256": "test-source"}, "entities": [{"entity_id": "w1", "owner_block": "*Model_Space", "type": "LINE", "layer": "Walls", "original_layer": "Walls", "handle": "1"}]}
        self.classification = {"components": {}}
        self.decisions = {"default_height_mm": 3200, "opening_height_mm": 2100, "prepared_categories_by_primary_layer": {"Walls": ["architectural_wall"]}}
        self.payload = {"schema": "dwg-skp-model-v2", "units": "mm", "source_manifest": self.document["intake"], "document_digest": _json_hash(self.document), "classification_digest": _json_hash(self.classification), "decisions_digest": _json_hash(self.decisions), "groups": [{"id": "assembled-wall", "category": "architectural_wall", "mode": "solid_extrusion", "source_layer": "Walls", "source_layers": ["Walls"], "tag": "Walls", "source_entity_ids": ["w1"], "outer": [[0,0],[1000,0],[1000,200],[0,200]], "holes": [], "base_z_mm": 0, "height_mm": 3200, "evidence": ["observed paired source boundaries"]}], "references": [], "tag_mapping": [{"source_layer": "Walls", "tag": "Walls"}], "blockers": []}

    def validate(self, payload=None):
        return validate_prepared(payload or self.payload, self.document, self.classification, self.decisions)

    def test_measured_composite_preserves_inputs(self):
        before = copy.deepcopy(self.payload)
        self.assertEqual([], self.validate()["blockers"])
        self.assertEqual(before, self.payload)

    def test_unchanged_trusted_ordinary_base_does_not_require_solver_scope_or_global_height(self):
        self.decisions={'default_height_mm':None,'layer_mapping':{'Walls':{'category':'architectural_wall','height_mm':3200,'footprint_confirmed':True}}}
        self.payload['decisions_digest']=_json_hash(self.decisions)
        base=copy.deepcopy(self.payload)
        self.payload['base_model_digest']=_json_hash(base)
        result=validate_prepared(self.payload,self.document,self.classification,self.decisions,base_payload=base)
        self.assertEqual([],result['blockers'])
        # Exact base provenance cannot authorize changed geometry or categories.
        for key,value in [('height_mm',9999),('category','curtain')]:
            forged=copy.deepcopy(self.payload);forged['groups'][0][key]=value
            result=validate_prepared(forged,self.document,self.classification,self.decisions,base_payload=base)
            self.assertIn('prepared_building_geometry_differs_from_immutable_base',{b['code']for b in result['blockers']})

    def test_a_forged_base_digest_cannot_grant_ordinary_base_scope(self):
        self.decisions={'default_height_mm':None,'layer_mapping':{'Walls':{'category':'architectural_wall','height_mm':3200}}}
        self.payload['decisions_digest']=_json_hash(self.decisions)
        base=copy.deepcopy(self.payload)
        self.payload['base_model_digest']='not-the-trusted-base'
        result=validate_prepared(self.payload,self.document,self.classification,self.decisions,base_payload=base)
        codes={b['code']for b in result['blockers']}
        self.assertIn('prepared_immutable_base_digest_mismatch',codes)
        self.assertIn('prepared_category_outside_decision_scope',codes)

    def test_stale_decisions_block_geometry(self):
        self.decisions["default_height_mm"] = 3500
        self.assertTrue(any(b["code"] == "prepared_input_digest_mismatch" for b in self.validate()["blockers"]))

    def test_unknown_source_id_blocks(self):
        self.payload["groups"][0]["source_entity_ids"] = ["invented"]
        self.assertTrue(any(b["code"] == "prepared_sources_unresolved" for b in self.validate()["blockers"]))

    def test_prepared_geometry_cannot_expand_approved_material_scope(self):
        self.payload["groups"][0]["category"] = "curtain"
        self.assertTrue(any(b["code"] == "prepared_category_outside_decision_scope" for b in self.validate()["blockers"]))

    def test_wrong_primary_tag_and_missing_secondary_source_block(self):
        self.payload["groups"][0]["source_layer"] = "NotInDWG"
        self.assertTrue(any(b["code"] == "prepared_primary_layer_lacks_source_evidence" for b in self.validate()["blockers"]))

    def test_height_cannot_change_in_solver(self):
        self.payload["groups"][0]["height_mm"] = 9999
        self.assertTrue(any(b["code"] == "prepared_height_differs_from_approved_decisions" for b in self.validate()["blockers"]))

    def test_overlap_is_not_accepted_as_consolidation(self):
        second = copy.deepcopy(self.payload["groups"][0])
        second["id"] = "duplicate-wall"
        self.payload["groups"].append(second)
        self.assertTrue(any(b["code"] == "overlapping_prepared_solids" for b in self.validate()["blockers"]))

    def test_lintel_uses_confirmed_clear_height(self):
        self.payload["groups"][0].update(semantic_role="door_lintel",base_z_mm=2100,height_mm=1100)
        self.assertEqual([],self.validate()["blockers"])

    def riser_fixture(self):
        from test_modeling import synthetic_riser_plan
        document, config, payload = synthetic_riser_plan(diagonal=True)
        decisions = {"slabs": config, "source_tag_policy": "effective"}
        classification = {}
        payload.update(document_digest=_json_hash(document), classification_digest=_json_hash(classification),
                       decisions_digest=_json_hash(decisions))
        return document, classification, decisions, payload

    def test_recomputed_step_risers_preserve_source_bound_sections(self):
        document, classification, decisions, payload = self.riser_fixture()
        before = copy.deepcopy(payload)
        result = validate_prepared(payload, document, classification, decisions)
        self.assertEqual([], result["blockers"])
        self.assertEqual(len(payload["groups"]), result["prepared_validation"]["valid_solid_count"])
        self.assertEqual(before, payload)

    def test_a_solver_cannot_remove_a_riser_or_change_gap_occupancy(self):
        document, classification, decisions, payload = self.riser_fixture()
        payload["groups"] = [group for group in payload["groups"] if group["slab_role"] != "slab_step_riser"]
        result = validate_prepared(payload, document, classification, decisions)
        self.assertIn("prepared_slab_geometry_differs_from_decisions", {row["code"] for row in result["blockers"]})
        document, classification, decisions, payload = self.riser_fixture()
        next(entry for entry in payload["slab_audit"]["step_risers"] if entry["status"] == "bridged_gap")["coverage_slices"] = []
        result = validate_prepared(payload, document, classification, decisions)
        self.assertIn("prepared_slab_audit_differs_from_decisions", {row["code"] for row in result["blockers"]})

    def test_prepared_risers_cannot_overlap_horizontal_material(self):
        document, classification, decisions, payload = self.riser_fixture()
        riser = next(group for group in payload["groups"] if group["slab_role"] == "slab_step_riser")
        riser["base_z_mm"] = -200
        riser["height_mm"] = 200
        riser["top_z_mm"] = 0
        result = validate_prepared(payload, document, classification, decisions)
        self.assertIn("overlapping_prepared_solids", {row["code"] for row in result["blockers"]})

    def test_building_cuts_are_recomputed_from_the_trusted_immutable_base(self):
        from test_modeling import synthetic_building_cut_plan
        document, decisions, base, payload = synthetic_building_cut_plan()
        result = validate_prepared(payload, document, {}, decisions, base_payload=base)
        self.assertEqual([], result["blockers"])
        result = validate_prepared(payload, document, {}, decisions)
        self.assertIn("prepared_building_cuts_require_trusted_base_and_slab_decisions", {row["code"] for row in result["blockers"]})
        broken = copy.deepcopy(payload)
        broken["base_component_cut_audit"]["original_groups"][0]["height_mm"] = 9999
        result = validate_prepared(broken, document, {}, decisions, base_payload=base)
        self.assertIn("prepared_building_cut_audit_differs_from_recomputed_recipe", {row["code"] for row in result["blockers"]})

    def test_building_cuts_cannot_change_other_base_geometry_or_forge_height_rules(self):
        from test_modeling import synthetic_building_cut_plan
        document, decisions, base, payload = synthetic_building_cut_plan()
        broken = copy.deepcopy(payload)
        wall = next(group for group in broken["groups"] if group["id"] == "untouched-wall")
        wall["outer"] = [[point[0] + 1, point[1]] for point in wall["outer"]]
        result = validate_prepared(broken, document, {}, decisions, base_payload=base)
        self.assertIn("prepared_building_cut_geometry_differs_from_immutable_base", {row["code"] for row in result["blockers"]})
        broken_decisions = copy.deepcopy(decisions)
        next(iter(broken_decisions["component_height_rules"].values()))["height_mm"] += 1
        broken = copy.deepcopy(payload)
        broken["decisions_digest"] = _json_hash(broken_decisions)
        result = validate_prepared(broken, document, {}, broken_decisions, base_payload=base)
        self.assertIn("prepared_building_cut_height_rule_differs_from_recipe", {row["code"] for row in result["blockers"]})

    def test_building_cut_solver_cannot_overexcavate_or_use_an_invented_cutter(self):
        from test_modeling import synthetic_building_cut_plan
        document, decisions, base, payload = synthetic_building_cut_plan()
        broken = copy.deepcopy(payload)
        part = next(group for group in broken["groups"] if group.get("original_component_id"))
        part["outer"] = [[point[0] + 10, point[1]] for point in part["outer"]]
        result = validate_prepared(broken, document, {}, decisions, base_payload=base)
        self.assertIn("prepared_building_cut_geometry_differs_from_immutable_base", {row["code"] for row in result["blockers"]})
        broken = copy.deepcopy(payload)
        broken["base_component_cut_audit"]["cutters"][0]["id"] = "invented-cutter"
        result = validate_prepared(broken, document, {}, decisions, base_payload=base)
        self.assertIn("prepared_building_cut_audit_differs_from_recomputed_recipe", {row["code"] for row in result["blockers"]})


if __name__ == "__main__":
    unittest.main()
