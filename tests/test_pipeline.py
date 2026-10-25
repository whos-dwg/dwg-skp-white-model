from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dwg_semantic.pipeline import (WorkflowError, _read, _write, approve_job, build_job,
    digest_file, inspect_source, inventory_layers, review_job, revise_job, slab_review_job)


class ApprovalWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary_root = Path(__file__).resolve().parents[1] / "work/pipeline-tests"
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temporary_root)
        self.root = Path(self.temp.name)
        self.source = self.root / "consultant.dwg"
        self.source.write_bytes(b"source fixture, never a CAD decoder input")
        self.job = self.root / "job"
        self.job.mkdir()
        self.document = {"intake": {"source_path": str(self.source), "source_sha256": digest_file(self.source), "units": "mm", "blockers": [], "dependencies": []}, "entities": [{"type": "LINE", "handle": "1", "layer": "A-WALL", "owner_block": "*Model_Space", "start": {"x": 0, "y": 0}, "end": {"x": 1000, "y": 0}}]}
        self.decisions = {"source_sha256": digest_file(self.source), "layer_mapping": {"A-WALL": {"category": "architectural_wall", "height_mm": 3000, "footprint_confirmed": True}}}
        self.payload = {"schema": "dwg-skp-model-v2", "units": "mm", "groups": [{"id": "W1", "name": "W1", "source_layer": "A-WALL", "category": "architectural_wall", "outer": [[0, 0], [1000, 0], [1000, 200], [0, 200]], "holes": [], "base_z_mm": 0, "height_mm": 3000}], "blockers": [], "warnings": []}
        for name, data in {"document.json": self.document, "classification.json": {}, "decisions.json": self.decisions, "rules.json": {}, "layers.json": inventory_layers(self.document, {}), "job.json": {}}.items():
            _write(self.job / name, data)
        self.prepare = patch("dwg_semantic.pipeline._draft_payload", return_value=self.payload)
        self.prepare.start()

    def tearDown(self):
        self.prepare.stop()
        self.assertEqual(self.root.resolve().parent, (Path(__file__).resolve().parents[1] / "work/pipeline-tests").resolve())
        self.temp.cleanup()

    def review_and_approve(self):
        review = review_job(self.job)
        self.assertEqual(review["status"], "ready_for_confirmation")
        return approve_job(self.job, confirmation="使用者確認此測試方案", expected_review=review["review_sha256"])

    def test_build_requires_actual_saved_approval(self):
        review_job(self.job)
        with self.assertRaisesRegex(WorkflowError, "核定"):
            build_job(self.job)

    def test_slab_review_preserves_existing_approved_job_and_writes_only_candidates(self):
        self.document['entities'][0]['entity_id']='wall-source'
        _write(self.job/'document.json',self.document)
        self.review_and_approve()
        before={path.name:digest_file(path) for path in self.job.iterdir() if path.is_file()}
        output=self.root/'slab-review'
        result=slab_review_job(self.job,output,elevation_unit='m')
        self.assertEqual('candidate_review_only',result['status'])
        self.assertFalse(list(output.rglob('*.skp')))
        self.assertEqual(before,{path.name:digest_file(path) for path in self.job.iterdir() if path.is_file()})

    def test_slab_annotations_from_a_different_dwg_version_are_rejected(self):
        fresh=dict(self.document,intake=dict(self.document['intake'],source_sha256='wrong-version'))
        path=self.root/'fresh-document.json'
        _write(path,fresh)
        with self.assertRaises(WorkflowError):
            slab_review_job(self.job,self.root/'slab-review',document_path=path)

    def test_slab_review_uses_source_bound_case_override_without_changing_job(self):
        frame={'entity_id':'source::frame','handle':'frame','type':'LWPOLYLINE','owner_block':'*Model_Space',
               'layer':'Upper-Slab','closed':True,'vertices':[{'x':x,'y':y}for x,y in ((0,0),(1000,0),(1000,1000),(0,1000))]}
        diagonals=[{'entity_id':f'source::diag{i}','type':'LINE','owner_block':'*Model_Space','layer':'Upper-Slab',
                    'start':{'x':a[0],'y':a[1]},'end':{'x':b[0],'y':b[1]}}
                   for i,(a,b)in enumerate((((0,0),(1000,1000)),((0,1000),(1000,0))))]
        self.document['entities']=[frame,*diagonals]
        self.decisions['slabs']={'source_sha256':self.decisions['source_sha256'],'region_overrides':{'source::frame':{'action':'level_change','reason':'Confirmed exception in this source only'}}}
        _write(self.job/'document.json',self.document);_write(self.job/'decisions.json',self.decisions)
        before={p.name:digest_file(p)for p in self.job.iterdir()if p.is_file()}
        slab_review_job(self.job,self.root/'slab-review')
        result=_read(self.root/'slab-review/slab-review.json')
        self.assertEqual('level_change',result['region_candidates'][0]['default_decision']['action'])
        self.assertEqual('explicit_source_entity_override',result['region_candidates'][0]['default_decision']['basis'])
        self.assertEqual([],result['opening_proposals'])
        self.assertEqual(before,{p.name:digest_file(p)for p in self.job.iterdir()if p.is_file()})
        self.assertFalse((self.job/'approval.json').exists())

    def test_slab_case_exceptions_cannot_be_used_for_a_different_source(self):
        self.decisions['slabs']={'source_sha256':'another-dwg','region_overrides':{}}
        _write(self.job/'decisions.json',self.decisions)
        with self.assertRaisesRegex(WorkflowError,'來源版本不同'):
            slab_review_job(self.job,self.root/'slab-review')
        self.assertFalse((self.root/'slab-review').exists())

    def test_profile_slab_exceptions_are_seeded_only_for_matching_source_and_do_not_enable_build(self):
        profile=self.root/'case-profile.json'
        policy={'source_sha256':self.decisions['source_sha256'],'region_overrides':{'source::known':{'action':'retain','reason':'Confirmed local exception'}}}
        _write(profile,{'model_defaults':{'slabs':policy}})
        with patch('dwg_semantic.intake.extract_document',return_value=self.document),patch('dwg_semantic.pipeline.classify_document',return_value={}):
            destination=self.root/'matching-profile'
            inspect_source(self.source,destination,profile=profile)
            result=_read(destination/'decisions.json')
            self.assertEqual(policy['source_sha256'],result['slabs']['source_sha256'])
            self.assertEqual(policy['region_overrides'],result['slabs']['region_overrides'])
            self.assertEqual('whole_uniform_thickness',result['slabs']['required_assembly_mode'])
            self.assertTrue(result['profile_slab_defaults_status']['applied'])
            self.assertFalse(result['slabs'].get('enabled',False))
            self.assertFalse((destination/'approval.json').exists())
            policy['source_sha256']='different-source'
            _write(profile,{'model_defaults':{'slabs':policy}})
            destination=self.root/'different-profile'
            inspect_source(self.source,destination,profile=profile)
            result=_read(destination/'decisions.json')
            self.assertNotIn('region_overrides',result['slabs'])
            self.assertEqual('whole_uniform_thickness',result['slabs']['assembly_mode'])
            self.assertFalse(result['profile_slab_defaults_status']['applied'])

    def test_blocked_review_cannot_be_approved(self):
        self.payload["blockers"] = ["未確認的高度"]
        review = review_job(self.job)
        with self.assertRaisesRegex(WorkflowError, "阻擋"):
            approve_job(self.job, confirmation="同意", expected_review=review["review_sha256"])

    def test_empty_candidate_is_blocked(self):
        self.payload["groups"] = []
        review = review_job(self.job)
        self.assertEqual(review["status"], "blocked")

    def test_invalid_intake_still_generates_a_blocked_review(self):
        source = self.root / "malformed.json"
        malformed = {"units": "mm", "entities": [{"type": "LINE", "handle": "bad", "layer": "A-WALL", "owner_block": "*Model_Space", "start": {"x": float('nan'), "y": 0}, "end": {"x": 1000, "y": 0}}]}
        source.write_text(json.dumps(malformed), encoding="utf-8")
        job = self.root / "invalid-job"
        result = inspect_source(source, job)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(_read(job / "classification.json")["validation"]["status"], "fail")
        self.assertTrue((job / "review.md").is_file())

    def test_confirmation_must_identify_the_review_and_have_text(self):
        review = review_job(self.job)
        with self.assertRaisesRegex(WorkflowError, "雜湊"):
            approve_job(self.job, confirmation="確認", expected_review="wrong")
        with self.assertRaisesRegex(WorkflowError, "原話"):
            approve_job(self.job, confirmation="  ", expected_review=review["review_sha256"])

    def test_decision_change_invalidates_approval_before_writer(self):
        self.review_and_approve()
        self.decisions["default_height_mm"] = 4000
        _write(self.job / "decisions.json", self.decisions)
        with patch("dwg_semantic.modeling.write_model") as writer:
            with self.assertRaisesRegex(WorkflowError, "decisions.json"):
                build_job(self.job)
            writer.assert_not_called()

    def test_prepared_geometry_change_invalidates_approval(self):
        self.review_and_approve()
        payload = _read(self.job / "model.plan.json")
        payload["groups"][0]["height_mm"] = 9999
        _write(self.job / "model.plan.json", payload)
        with self.assertRaisesRegex(WorkflowError, "model.plan.json"):
            build_job(self.job)

    def test_source_change_invalidates_approval(self):
        self.review_and_approve()
        self.source.write_bytes(b"revised source")
        with self.assertRaisesRegex(WorkflowError, "來源.*改變"):
            build_job(self.job)

    def test_missing_or_changed_xref_invalidates_review(self):
        xref = self.root / "structure.dwg"
        xref.write_bytes(b"xref v1")
        self.document["intake"]["dependencies"] = [{"path": str(xref), "sha256": digest_file(xref), "status": "loaded"}]
        _write(self.job / "document.json", self.document)
        self.review_and_approve()
        xref.write_bytes(b"xref v2")
        with self.assertRaisesRegex(WorkflowError, "參照已改變"):
            build_job(self.job)

    def test_preview_change_cannot_be_presented_as_approved(self):
        review = review_job(self.job)
        (self.job / "review.svg").write_text("different picture", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "review.svg"):
            approve_job(self.job, confirmation="確認", expected_review=review["review_sha256"])

    def test_runtime_change_invalidates_review(self):
        self.review_and_approve()
        with patch("dwg_semantic.pipeline._runtime_digest", return_value="different-version"):
            with self.assertRaisesRegex(WorkflowError, "程式已變更"):
                build_job(self.job)

    def test_preview_reports_actual_object_override_height_and_base(self):
        self.decisions["object_overrides"] = {"W1": {"height_mm": 4300, "base_z_mm": 1000, "thickness_mm": 200}}
        _write(self.job / "decisions.json", self.decisions)
        self.payload["groups"][0].update(height_mm=4300, base_z_mm=1000)
        self.payload["references"] = [{"id": "note", "source_layer": "NOTE", "reason": "user_confirmed_reference"}]
        review_job(self.job)
        summary = (self.job / "review.md").read_text(encoding="utf-8")
        self.assertIn("1000 | 4300 | 5300", summary)
        self.assertIn('"thickness_mm": 200', summary)
        self.assertIn("參考圖層 `NOTE`：1 件", summary)

    def test_revision_does_not_carry_approval(self):
        self.review_and_approve()
        destination = self.root / "revision2"
        result = revise_job(self.job, destination)
        # A new revision adopts the fixed standard. This legacy fixture has
        # no overhead slab, so it must stop rather than retain an arbitrary top.
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any(isinstance(row,dict)and row.get('code')=='fixed_standard_overhead_underside_required'
                            for row in result['blockers']))
        self.assertFalse((destination / "approval.json").exists())
        self.assertTrue((self.job / "approval.json").exists())

    def test_build_consumes_exact_reviewed_geometry(self):
        self.review_and_approve()
        destination = self.root / "delivery.skp"
        def write(payload, output):
            self.assertEqual(payload, _read(self.job / "model.plan.json"))
            Path(output).write_bytes(b"native writer fixture")
            return {"all_pass": True}
        with patch("dwg_semantic.modeling.write_model", side_effect=write) as writer:
            self.assertTrue(build_job(self.job, destination)["all_pass"])
            writer.assert_called_once()
        with self.assertRaisesRegex(WorkflowError, "已存在"):
            build_job(self.job, destination)


if __name__ == "__main__":
    unittest.main()
