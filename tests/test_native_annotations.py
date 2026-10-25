"""Optional actual AutoCAD fixture checks; normal unit suite stays portable."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import unittest


@unittest.skipUnless(os.environ.get("DWG_ANNOTATION_TEST_JSON"), "Set DWG_ANNOTATION_TEST_JSON to native annotation-fixture normalized.json")
class NativeAnnotationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = json.loads(Path(os.environ["DWG_ANNOTATION_TEST_JSON"]).read_text(encoding="utf-8"))
        cls.attributes = [entity for entity in cls.document["resolved_instances"] if entity["type"] == "ATTRIB"]

    def test_real_instance_values_not_attdef_defaults(self):
        self.assertEqual(len(self.attributes), 8)
        self.assertEqual(sorted(entity["text"] for entity in self.attributes if entity["tag"] == "SLAB_THK"),
                         ["THK=180", "THK=200", "THK=200", "THK=220"])
        self.assertTrue(all(entity["is_instance_value"] for entity in self.attributes))
        self.assertFalse(any("DEFAULT" in entity["text"] for entity in self.attributes))
        definitions = [entity for entity in self.document["entities"] if entity["type"] == "ATTDEF"]
        self.assertEqual(len(definitions), 2)
        self.assertTrue(all(entity["is_instance_value"] is False for entity in definitions))

    def test_direct_rotation_nonuniform_scale_and_mirror_positions(self):
        expected = {"THK=180": (940, 2020), "THK=220": (6990, 1040)}
        for text, (x, y) in expected.items():
            entity = next(entity for entity in self.attributes if entity["text"] == text)
            self.assertAlmostEqual(entity["position_world"]["x"], x)
            self.assertAlmostEqual(entity["position_world"]["y"], y)

    def test_nested_instances_get_ancestor_transform_once_and_unique_identity(self):
        nested = [entity for entity in self.attributes if entity["text"] == "THK=200"]
        self.assertEqual(len({entity["entity_id"] for entity in nested}), 2)
        actual = sorted((round(entity["position_world"]["x"], 6), round(entity["position_world"]["y"], 6)) for entity in nested)
        self.assertEqual(actual, [(9370, 6380), (20190, 4315)])

    def test_original_zero_and_explicit_attribute_layers_are_preserved(self):
        for entity in self.attributes:
            expected = "ANNO-SLAB" if entity["tag"] == "SLAB_THK" else "ANNO-EXPLICIT"
            self.assertEqual(entity["original_layer"], "0" if entity["tag"] == "SLAB_THK" else "ANNO-EXPLICIT")
            self.assertEqual(entity["effective_layer"], expected)
            self.assertTrue(entity["attribute_coordinates_resolved"])

    def test_mleader_links_embedded_text_and_transformed_head(self):
        placed = [entity for entity in self.document["entities"] if entity["owner_block"] == "*Model_Space"] + self.document["resolved_instances"]
        leaders = [entity for entity in placed if entity["type"] == "MLEADER"]
        self.assertEqual(len(leaders), 3)
        nested = [entity for entity in leaders if entity.get("instance_path")]
        heads = sorted((entity["leader_lines"][0]["start_world"]["x"], entity["leader_lines"][0]["start_world"]["y"]) for entity in nested)
        self.assertEqual(heads, [(9900, 6020), (20010, 4050)])
        for entity in leaders:
            self.assertIn("SLAB THK=", entity["text"])
            self.assertEqual(entity["annotation_content_read_status"], "embedded_mtext_read")
            self.assertEqual(entity["leader_lines"][0]["annotation_owner_handle"], entity["handle"])

    def test_coverage_metadata_and_source_unchanged(self):
        counts = self.document["annotation_extraction"]["counts"]
        self.assertEqual(counts["placed_attached_attribute_values_read"], 8)
        self.assertEqual(counts["placed_mleader_mtext_read"], 3)
        self.assertEqual(self.document["intake"]["blockers"], [])
        source = Path(self.document["intake"]["source_path"])
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), self.document["intake"]["source_sha256"])


if __name__ == "__main__":
    unittest.main()
