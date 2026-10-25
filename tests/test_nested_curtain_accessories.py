from __future__ import annotations

import copy
import unittest
from pathlib import Path

from shapely.geometry import Polygon

from dwg_semantic.classifier import _curtain_accessories, _nested_curtain_accessory_candidates, classify_document, load_rules
from dwg_semantic.modeling import prepare_model


ROOT = Path(__file__).resolve().parents[1]
RING = [[84.52,119],[84.52,51],[100,51],[100,39],[56,39],[56,0],[44,0],[44,39],[0,39],[0,51],[15.48,51],[15.48,119],[27.48,119],[27.48,51],[72.52,51],[72.52,119]]


def fixture(paths=("A",), *, parent_layer="area", rotate=False):
    model, resolved = [], []
    for number, path in enumerate(paths):
        offset = number * 1000
        world = [[-y + offset, x + 1000] if rotate else [x + offset, y + 1000] for x,y in RING]
        polygon = Polygon(world)
        xmin,ymin,xmax,ymax = polygon.bounds
        extent = {"min":{"x":xmin,"y":ymin},"max":{"x":xmax,"y":ymax}}
        model.append({"type":"INSERT","handle":path,"entity_id":"source::"+path,"source_id":"source","source_file":"source.dwg","owner_block":"*Model_Space","layer":parent_layer,"block_name":"generic-profile-definition","local_extents":extent,"world_extents":extent})
        resolved.append({"type":"LWPOLYLINE","handle":"LEAF","source_handle":"LEAF","entity_id":f"source:{path}:LEAF","source_id":"source","source_file":"source.dwg","root_space":"*Model_Space","owner_block":"generic-profile-definition","instance_path":path,"layer":"4金属构件断面","original_layer":"4金属构件断面","effective_layer":"4金属构件断面","block_layer":parent_layer,"closed":True,"vertices":[{"x":x,"y":y} for x,y in RING],"vertices_world":[{"x":x,"y":y} for x,y in world],"world_extents":extent})
    return {"units":"mm","intake":{"units":"mm","source_path":"source.dwg"},"entities":model,"resolved_instances":resolved}


class NestedCurtainAccessoryTests(unittest.TestCase):
    def setUp(self):
        self.rules = load_rules(ROOT / "config/default_rules.json")

    def candidates(self, document, existing=None):
        return _nested_curtain_accessory_candidates(document["resolved_instances"], existing or [], self.rules)

    def test_nonmetal_parent_cannot_hide_explicit_original_metal_profile(self):
        for layer in ["area","4win","A-REFERENCE"]:
            with self.subTest(layer=layer):
                document = fixture(parent_layer=layer)
                result = classify_document(document,self.rules)
                candidate = result["components"]["curtain_accessory_candidates"][0]
                self.assertEqual(["source:A:LEAF"],candidate["source_entity_ids"])
                self.assertEqual("A/LEAF",candidate["source_handle"])
                self.assertEqual("4金属构件断面",candidate["source_layer"])
                self.assertEqual("user_confirmation_required",candidate["status"])
                self.assertIsNone(candidate["category"])
                self.assertIsNone(candidate["height_mm"])
                self.assertIsNone(candidate["base_z_mm"])
                self.assertEqual("review_required",result["validation"]["status"])
                self.assertIn("nested_metal_accessory_user_confirmation",{warning["rule"] for warning in result["validation"]["warnings"]})
                coverage = next(row for row in result["audits"]["source_coverage"]["records"] if row["entity_id"]=="source:A:LEAF")
                self.assertEqual("pending_review",coverage["disposition"])

    def test_repeated_definition_handles_keep_all_instance_paths(self):
        document = fixture(("A","B","ROOT/C"))
        candidates = self.candidates(document)
        self.assertEqual(3,len(candidates))
        self.assertEqual(3,len({row["id"] for row in candidates}))
        self.assertEqual({"source:A:LEAF","source:B:LEAF","source:ROOT/C:LEAF"},{row["source_entity_ids"][0] for row in candidates})
        self.assertEqual({"A/LEAF","B/LEAF","ROOT/C/LEAF"},{row["source_handle"] for row in candidates})

    def test_rotated_resolved_world_profile_is_not_retransformed_or_local(self):
        document = fixture(rotate=True)
        candidate = self.candidates(document)[0]
        points = [candidate["segments"][0]["start"]]+[segment["end"] for segment in candidate["segments"]]
        polygon = Polygon(points)
        self.assertEqual((-119,1000,0,1100),polygon.bounds)
        self.assertAlmostEqual(3300,polygon.area)
        self.assertAlmostEqual(574,polygon.length)

    def test_already_classified_parent_accessory_does_not_duplicate_its_leaf(self):
        document = fixture(parent_layer="4金属构件断面")
        curtains = [{"id":"CURTAIN-001","segments":[{"start":[0,1000],"end":[100,1000]}]}]
        existing = _curtain_accessories(document["entities"],document["resolved_instances"],curtains,self.rules)
        self.assertEqual(1,len(existing))
        self.assertEqual([],self.candidates(document,existing))

    def test_duplicate_resolved_rows_are_one_candidate_but_distinct_sources_are_not(self):
        document=fixture()
        document["resolved_instances"].append(copy.deepcopy(document["resolved_instances"][0]))
        self.assertEqual(1,len(self.candidates(document)))
        other=copy.deepcopy(document["resolved_instances"][0]);other.update(source_id="other",entity_id="other:A:LEAF",source_file="other.dwg")
        document["resolved_instances"].append(other)
        self.assertEqual(2,len(self.candidates(document)))

    def test_layer_zero_or_unrelated_nested_leaf_is_not_promoted_to_metal(self):
        document=fixture(parent_layer="4金属构件断面")
        for layer in ["0","Layer0","furniture"]:
            document["resolved_instances"][0]["original_layer"]=layer
            self.assertEqual([],self.candidates(document))

    def test_review_candidate_resolves_through_existing_object_override_to_plan(self):
        document=fixture(("A","B"))
        classification=classify_document(document,self.rules)
        config={"layer_mapping":{"area":{"category":"reference"},"4金属构件断面":{"category":"reference"}},"object_overrides":{}}
        pending=prepare_model(document,classification,config)
        self.assertIn("nested_accessory_decision_requires_confirmation",{row["code"] for row in pending["blockers"]})
        self.assertEqual([],pending["groups"])
        identity=classification["components"]["curtain_accessory_candidates"][0]["source_entity_ids"][0]
        # Explicit reviewed dimensions; no RC/ceiling inheritance and no use of
        # a raw H-note as an automatic extrusion height.
        config["object_overrides"][identity]={"category":"curtain_accessory","height_mm":1800,"base_z_mm":240,"footprint_confirmed":True}
        other=next(row["source_entity_ids"][0] for row in classification["components"]["curtain_accessory_candidates"] if row["source_entity_ids"]!=[identity])
        config["object_overrides"][other]={"category":"reference","reason":"Reviewed separately; omit this second instance from the fixture model."}
        resolved=prepare_model(document,classification,config)
        self.assertEqual([],resolved["blockers"])
        self.assertEqual(1,len(resolved["groups"]))
        group=resolved["groups"][0]
        self.assertEqual([identity],group["source_entity_ids"])
        self.assertEqual("4金属构件断面",group["tag"])
        self.assertEqual((240,1800),(group["base_z_mm"],group["height_mm"]))
        self.assertAlmostEqual(3300,Polygon(group["outer"]).area)
        self.assertEqual(1,len(resolved["references"]))


if __name__=="__main__":unittest.main()
