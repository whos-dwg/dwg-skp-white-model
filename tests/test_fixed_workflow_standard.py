"""Fixed policies survive new-job decisions and cannot be removed by a profile."""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest

from dwg_semantic.pipeline import _read, _write, inspect_source, review_job
from dwg_semantic.modeling_standard import FIXED
from test_workflow_guards import make_job


def test_new_job_cannot_silently_downgrade_to_legacy_by_removing_decision_marker(tmp_path):
    legacy, source, _ = make_job(tmp_path/'legacy', approve=False)
    doc = _read(legacy/'document.json')
    classification = _read(legacy/'classification.json')
    job = tmp_path/'fixed'
    with patch('dwg_semantic.intake.extract_document', return_value=doc), \
         patch('dwg_semantic.pipeline.classify_document', return_value=classification):
        inspect_source(source, job)
    assert _read(job/'job.json')['modeling_standard'] == FIXED
    decisions = _read(job/'decisions.json')
    del decisions['modeling_standard']
    _write(job/'decisions.json', decisions)
    result = review_job(job)
    assert any(isinstance(row,dict) and row.get('code') == 'fixed_modeling_standard_cannot_be_removed_from_job'
               for row in result['blockers'])
    assert _read(job/'decisions.json')['modeling_standard'] == FIXED
    assert 'job.json' in result['input_hashes']
    assert not (job/'approval.json').exists()


def installer_module():
    path = Path(__file__).resolve().parents[1]/'tools/install_skill.py'
    spec = importlib.util.spec_from_file_location('fixed_standard_installer_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_explicit_retirement_rejects_directory_escape_and_live_source_file(tmp_path):
    module = installer_module()
    target = tmp_path/'dwg-layered-skp'
    target.mkdir()
    (target/'references').mkdir()
    for value in ('../unrelated.txt', 'references', 'SKILL.md', str((tmp_path/'outside').resolve())):
        with pytest.raises(ValueError):
            module.install(target, dry_run=True, retire=[value])
    result = module.install(target, dry_run=True, retire=['references/obsolete-case.md'])
    assert result['retire'] == ['references/obsolete-case.md']
    assert list(target.iterdir()) == [target/'references']


def test_known_stair_marks_become_2d_evidence_without_overriding_explicit_material_choices():
    from dwg_semantic.pipeline import _fixed_stair_source_defaults
    from dwg_semantic.modeling_standard import seed_standard
    doc={'entities':[{'entity_id':key,'type':'LINE','layer':'STAIR','owner_block':'*Model_Space'}
                     for key in ('step','explicit-wall','enclosure-wall')]}
    decisions=seed_standard({'layer_mapping':{'STAIR':{'category':None}},
                             'object_overrides':{'explicit-wall':{'category':'structural_wall'}}})
    classification={'automatic_plan':{'slab_openings':{'staircase_openings':[
        {'stair_mark_source_entity_ids':['step','explicit-wall'],
         'boundary_source_entity_ids':['enclosure-wall']} ]}}}
    _fixed_stair_source_defaults(doc,classification,decisions)
    assert decisions['object_overrides']['step']['category']=='reference'
    assert decisions['object_overrides']['explicit-wall']['category']=='structural_wall'
    assert 'enclosure-wall'not in decisions['object_overrides']
