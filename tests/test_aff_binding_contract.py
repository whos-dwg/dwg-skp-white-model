"""Proposed typed A.F.F. review contract, independent of native modeling.

These tests deliberately describe a candidate review API, not an approval API.
Bindings carry exact annotation and functional-region identities. Numeric model
Z is separated from source H/FFL datum text. No nearest-text assignment occurs.
"""
from __future__ import annotations

import copy
import unittest

from dwg_semantic.slabs import prepare_slab_review


def note(key,value,x=500,y=500):
    return {"entity_id":key,"handle":key,"source_file":"synthetic.dwg","type":"TEXT",
            "owner_block":"*Model_Space","layer":"Notes","text":value,"position":{"x":x,"y":y,"z":0}}


def frame(key,x0,y0,x1,y1):
    return {"entity_id":key,"handle":key,"source_file":"synthetic.dwg","type":"LWPOLYLINE",
            "owner_block":"*Model_Space","layer":"A-ROOM","closed":True,
            "vertices":[{"x":x,"y":y,"z":0}for x,y in [(x0,y0),(x1,y0),(x1,y1),(x0,y1)]]}


def footprint(x0=0,y0=0,x1=1000,y1=1000):
    return {"outer":[[x0,y0],[x1,y0],[x1,y1],[x0,y1]],"holes":[]}


def binding(key="aff",region="R1",reference=100,reference_id="ffl",**changes):
    return {"id":"B-"+key,"source_entity_id":key,"token_kind":"aff_height","token_index":0,
            "target_surface":"structural_bottom","reference_surface":"finished_floor","reference_z_mm":reference,
            "reference_annotation_source_ids":[reference_id],"applies_to_system_id":"O","functional_region_id":region,
            "reason":"Explicit source RC soffit note and reviewed finished-floor/functional-region binding",
            "basis":"source_explicit",**changes}


def config(bindings=None,regions=None):
    return {"floor_datum_m":0,
            "systems":[{"id":"O","role":"overhead","extent":footprint(-100,-100,2200,1100),
                        "source_entity_ids":["room1"],"default_top_z_mm":3300,"default_thickness_mm":200}],
            "functional_regions":regions if regions is not None else [
                {"id":"R1","footprint":footprint(),"source_entity_ids":["room1"]}],
            "annotation_bindings":bindings if bindings is not None else [binding()]}


class AFFBindingContract(unittest.TestCase):
    def review(self,entities,decisions=None,**kwargs):
        doc={"intake":{"source_sha256":"synthetic-aff","units":"mm"},"entities":entities}
        return prepare_slab_review(doc,{},slab_decisions=decisions,**kwargs)

    def fixture(self):
        return [frame("room1",0,0,1000,1000),note("aff","上層RC樓板底 淨高 3000mm A.F.F."),
                note("ffl","本層 FFL +0.100m")]

    def test_literal_rc_soffit_is_a_typed_source_candidate_not_generic_unresolved(self):
        review=self.review([note("mixed","上層RC樓板底 淨高 3000mm A.F.F.；本層 FFL +0.100m")])
        row=review["annotations"][0]
        aff=row["height_candidates"][0]
        self.assertEqual("structural_bottom",aff["target_surface"])
        self.assertEqual("finished_floor",aff["reference_surface"])
        self.assertEqual(3000,aff["value_mm"])
        self.assertFalse(aff["accepted_for_slab"],"Typed source meaning must not grant modeling approval")

    def test_mixed_note_keeps_ffl_token_available_while_aff_is_not_an_absolute_elevation(self):
        review=self.review([note("mixed","本層 FFL +0.100m；上層RC板底 3000mm A.F.F.")])
        row=review["annotations"][0]
        self.assertEqual([100],[token["value_mm"]for token in row["levels"]if token["level_kind"]=="finished_level"])
        self.assertFalse(row["excluded_from_slab_elevation"],"AFF must not globally exclude the independent FFL token")
        self.assertEqual(1,review["counts"]["elevation_annotations"])
        self.assertEqual(1,review["counts"]["aff_height_annotations"])

    def test_english_level_token_adjacent_to_chinese_words_is_still_typed(self):
        review=self.review([note("ffl","本層FFL +0.100m")])
        levels=review["annotations"][0]["levels"]
        self.assertEqual([100],[token["value_mm"]for token in levels if token["level_kind"]=="finished_level"])

    def test_railing_and_generic_ceiling_aff_do_not_become_rc_soffits(self):
        review=self.review([note("rail","玻璃欄杆 1200mm A.F.F."),note("ceiling","吊頂 2800mm A.F.F.")])
        self.assertTrue(all(row["height_candidates"][0]["target_surface"]!="structural_bottom"for row in review["annotations"]))
        self.assertTrue(review["annotations"][0]["excluded_from_slab_parameters"])

    def test_unitless_aff_still_does_not_inherit_h_elevation_units(self):
        review=self.review([note("aff","上層RC板底 3000 A.F.F. H+3.100m")],elevation_unit="m")
        height=review["annotations"][0]["height_candidates"][0]
        self.assertIsNone(height["value_mm"])
        self.assertEqual("unresolved_separate_from_elevation_unit",height["unit_basis"])

    def test_explicit_binding_resolves_model_relative_bos_without_mutating_source_or_approving(self):
        entities=self.fixture();decisions=config();before=copy.deepcopy((entities,decisions))
        review=self.review(entities,decisions)
        audit=review.get("annotation_binding_audit",{})
        self.assertTrue(audit.get("all_pass"),audit)
        regions=review.get("resolved_height_regions",[])
        self.assertEqual(1,len(regions))
        self.assertEqual(3100,regions[0]["bottom_z_mm"])
        self.assertEqual("O",regions[0]["applies_to_system_id"])
        self.assertEqual("R1",regions[0]["functional_region_id"])
        self.assertEqual(footprint(),regions[0]["footprint"])
        self.assertTrue({"aff","ffl","room1"}<=set(regions[0]["source_entity_ids"]))
        self.assertFalse(regions[0].get("accepted_for_modeling",False))
        self.assertEqual(before,(entities,decisions))

    def test_aff_unit_override_does_not_change_literal_source_units(self):
        decisions=config([binding(unit="m")])
        review=self.review(self.fixture(),decisions)
        audit=review.get("annotation_binding_audit",{})
        self.assertFalse(audit.get("all_pass",True),audit)
        self.assertTrue(audit.get("issues"),audit)
        self.assertFalse(review.get("resolved_height_regions",[]))

    def test_unknown_annotation_system_region_or_reference_cannot_be_silently_ignored(self):
        changes=[{"source_entity_id":"missing"},{"applies_to_system_id":"missing"},
                 {"functional_region_id":"missing"},{"reference_annotation_source_ids":["missing"]},
                 {"token_index":9},{"reference_z_mm":float("nan")},{"target_surface":"ceiling_or_rc_unsure"}]
        for change in changes:
            with self.subTest(change=change):
                decisions=config([binding(**change)])
                review=self.review(self.fixture(),decisions)
                audit=review.get("annotation_binding_audit",{})
                self.assertFalse(audit.get("all_pass",True),audit)
                self.assertTrue(audit.get("issues"),audit)
                self.assertFalse(review.get("resolved_height_regions",[]))

    def test_distinct_functional_ffl_regions_do_not_use_the_nearest_aff_or_h_zone(self):
        entities=[*self.fixture(),frame("room2",1000,0,2000,1000),note("aff2","RC板底 2800mm AFF",x=900),
                  note("ffl2","FFL +0.200m",x=900),note("h-partition","H-0.050m",x=1100)]
        regions=[{"id":"R1","footprint":footprint(),"source_entity_ids":["room1"]},
                 {"id":"R2","footprint":footprint(1000,0,2000,1000),"source_entity_ids":["room2"]}]
        decisions=config([binding(),binding("aff2","R2",200,"ffl2")],regions)
        review=self.review(entities,decisions,elevation_unit="m")
        self.assertTrue(review.get("annotation_binding_audit",{}).get("all_pass"))
        result={row["functional_region_id"]:row for row in review.get("resolved_height_regions",[])}
        self.assertEqual({"R1","R2"},set(result))
        self.assertEqual(3100,result["R1"]["bottom_z_mm"])
        self.assertEqual(3000,result["R2"]["bottom_z_mm"])
        self.assertEqual(footprint(1000,0,2000,1000),result["R2"]["footprint"])
        self.assertNotIn("h-partition",result["R2"]["source_entity_ids"])

    def test_confirmed_binding_is_reused_on_repeat_review_without_becoming_source_fact(self):
        decisions=config([binding(basis="confirmed_job_decision",reason="Existing source-bound job confirmation")])
        first=self.review(self.fixture(),decisions)
        second=self.review(self.fixture(),decisions)
        self.assertTrue(first.get("annotation_binding_audit",{}).get("all_pass"))
        self.assertEqual(first.get("annotation_binding_audit"),second.get("annotation_binding_audit"))
        self.assertEqual(first.get("resolved_height_regions"),second.get("resolved_height_regions"))
        self.assertEqual("confirmed_job_decision",first["resolved_height_regions"][0]["basis"])


if __name__=="__main__":
    unittest.main()


