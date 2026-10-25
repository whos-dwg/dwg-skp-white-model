from __future__ import annotations

import copy
from pathlib import Path
import tempfile
import unittest

from dwg_semantic.classifier import classify_document, load_rules
from dwg_semantic.modeling import prepare_model
from dwg_semantic.pipeline import _read, _write, digest_file, inventory_layers, review_job
from test_nested_curtain_accessories import fixture


ROOT = Path(__file__).resolve().parents[1]
METAL = "4金属构件断面"


class NestedAccessoryDecisionTests(unittest.TestCase):
    def setUp(self):
        self.document = fixture(("A", "B"))
        self.document["entities"].append({"entity_id": "source:wall", "handle": "W", "type": "LWPOLYLINE",
                                         "owner_block": "*Model_Space", "layer": "Walls", "source_file": "source.dwg", "closed": True,
                                         "vertices": [{"x": 5000, "y": 0}, {"x": 6000, "y": 0}, {"x": 6000, "y": 200}, {"x": 5000, "y": 200}]})
        self.rules = load_rules(ROOT / "config/default_rules.json")
        self.classification = classify_document(self.document, self.rules)
        self.decisions = {"default_height_mm": 3950,
                          "layer_mapping": {METAL: {"category": "reference"}, "area": {"category": "reference"},
                                            "Walls": {"category": "architectural_wall", "height_mm": 3200, "footprint_confirmed": True}},
                          "object_overrides": {}}

    def confirm(self):
        self.decisions["object_overrides"] = {
            "source:A:LEAF": {"category": "curtain_accessory", "semantic_subrole": "balustrade_metal_profile", "base_z_mm": 240, "height_mm": 1800, "footprint_confirmed": True},
            "source:B:LEAF": {"category": "reference", "reason": "This repeated instance was reviewed and remains reference geometry."}}

    def test_layer_reference_cannot_silently_resolve_nested_requests(self):
        before = copy.deepcopy(self.classification)
        plan = prepare_model(self.document, self.classification, self.decisions)
        self.assertEqual(1, len(plan["groups"]))
        self.assertEqual(2, len(plan["references"]))
        pending = [row for row in plan["blockers"] if row["code"] == "nested_accessory_decision_requires_confirmation"]
        self.assertEqual(2, len(pending))
        self.assertEqual({"source:A:LEAF", "source:B:LEAF"}, {row["source_entity_ids"][0] for row in pending})
        self.assertEqual(2, len(plan["warnings"]))
        self.assertEqual(before, self.classification)

    def test_exact_instance_decisions_resolve_without_changing_classification_status(self):
        self.confirm()
        plan = prepare_model(self.document, self.classification, self.decisions)
        self.assertEqual([], plan["blockers"])
        self.assertEqual([], plan["semantic_review_audit"]["unresolved_request_ids"])
        self.assertEqual(2, len(plan["semantic_review_audit"]["resolved_request_ids"]))
        metal = next(row for row in plan["groups"] if row["category"] == "curtain_accessory")
        self.assertEqual(["source:A:LEAF"], metal["source_entity_ids"])
        self.assertEqual((240, 1800, METAL), (metal["base_z_mm"], metal["height_mm"], metal["tag"]))
        self.assertEqual("balustrade_metal_profile", metal["semantic_subrole"])
        choice = next(row for request in plan["semantic_review_audit"]["requests"] for row in request["source_decisions"] if row["source_entity_id"] == "source:A:LEAF")
        self.assertEqual("balustrade_metal_profile", choice["semantic_subrole"])
        self.assertEqual("review_required", self.classification["validation"]["status"])

    def test_global_or_layer_height_cannot_substitute_for_explicit_vertical_decision(self):
        self.confirm()
        self.decisions["layer_mapping"][METAL].update(category="curtain_accessory", height_mm=3950)
        for missing in ("base_z_mm", "height_mm", "footprint_confirmed"):
            with self.subTest(missing=missing):
                config = copy.deepcopy(self.decisions)
                config["object_overrides"]["source:A:LEAF"][missing] = None
                plan = prepare_model(self.document, self.classification, config)
                self.assertTrue(any(row["code"] == "nested_accessory_decision_requires_confirmation" for row in plan["blockers"]))
                self.assertFalse(any(row["category"] == "curtain_accessory" for row in plan["groups"]))
                self.assertIn("source:A:LEAF", {row["id"] for row in plan["references"]})

    def test_definition_handle_or_candidate_id_cannot_acknowledge_repeated_instances(self):
        for key in ("LEAF", self.classification["components"]["curtain_accessory_candidates"][0]["id"]):
            with self.subTest(key=key):
                self.decisions["object_overrides"] = {key: {"category": "curtain_accessory", "base_z_mm": 0, "height_mm": 1800, "footprint_confirmed": True}}
                plan = prepare_model(self.document, self.classification, self.decisions)
                self.assertEqual(2, len(plan["semantic_review_audit"]["unresolved_request_ids"]))
                self.assertFalse(any(row["category"] == "curtain_accessory" for row in plan["groups"]))

    def test_reference_and_exclude_need_per_instance_reasons(self):
        for category in ("reference", "exclude"):
            with self.subTest(category=category):
                self.confirm()
                choice = self.decisions["object_overrides"]["source:B:LEAF"]
                choice.update(category=category, reason=" ")
                plan = prepare_model(self.document, self.classification, self.decisions)
                self.assertEqual(1, len(plan["semantic_review_audit"]["unresolved_request_ids"]))
                choice["reason"] = "Reviewed this full instance and explicitly selected its disposition."
                plan = prepare_model(self.document, self.classification, self.decisions)
                self.assertEqual([], plan["blockers"])
                field = "references" if category == "reference" else "excluded"
                self.assertIn("source:B:LEAF", {row["id"] for row in plan[field]})

    def test_open_profile_is_pending_geometry_until_an_explicit_footprint_exists(self):
        self.confirm()
        leaf = self.document["resolved_instances"][0]
        footprint = {"outer": [[point["x"], point["y"]] for point in leaf["vertices_world"]], "holes": []}
        leaf.update(type="LINE", closed=False, start_world=leaf["vertices_world"][0], end_world=leaf["vertices_world"][1])
        del leaf["vertices_world"]
        del leaf["vertices"]
        self.classification = classify_document(self.document, self.rules)
        plan = prepare_model(self.document, self.classification, self.decisions)
        self.assertIn("unresolved_footprint", {row["code"] for row in plan["blockers"]})
        self.assertEqual(1, len(plan["semantic_review_audit"]["unresolved_request_ids"]))
        self.decisions["object_overrides"]["source:A:LEAF"]["footprint"] = footprint
        plan = prepare_model(self.document, self.classification, self.decisions)
        self.assertEqual([], plan["blockers"])
        self.assertEqual([], plan["semantic_review_audit"]["unresolved_request_ids"])

    def test_actual_review_closes_after_full_id_choices_without_approval_or_skp(self):
        temporary_root = ROOT / "work/pipeline-tests"
        temporary_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="nested-decision-closure-", dir=temporary_root) as directory:
            root = Path(directory)
            source = root / "source.dwg"
            source.write_bytes(b"Read-only freshness fixture; not a DWG decoder input.")
            self.document["intake"].update(source_path=str(source), source_sha256=digest_file(source), blockers=[], dependencies=[])
            self.decisions["source_sha256"] = digest_file(source)
            job = root / "job"
            for name, value in {"document.json": self.document, "classification.json": self.classification,
                                "decisions.json": self.decisions, "rules.json": self.rules,
                                "layers.json": inventory_layers(self.document, self.classification)}.items():
                _write(job / name, value)
            review = review_job(job)
            self.assertEqual("blocked", review["status"])
            self.assertIn("nested_accessory_decision_requires_confirmation", {row["code"] for row in review["blockers"]})
            self.confirm()
            _write(job / "decisions.json", self.decisions)
            review = review_job(job)
            self.assertEqual("ready_for_confirmation", review["status"])
            self.assertEqual([], review["blockers"])
            plan = _read(job / "model.plan.json")
            self.assertEqual([], plan["semantic_review_audit"]["unresolved_request_ids"])
            self.assertFalse((job / "approval.json").exists())
            self.assertFalse(list(job.rglob("*.skp")))


if __name__ == "__main__":
    unittest.main()
