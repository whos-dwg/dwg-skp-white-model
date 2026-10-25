"""Full native pipeline on generated fixtures, never a real project's approval."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def validate(source: Path, job: Path, runtime: Path | None = None) -> dict:
    source, job = source.resolve(), job.resolve()
    fixture_root = (ROOT / "work/upgraded-validation/fixtures").resolve()
    if fixture_root not in source.parents:
        raise ValueError("Automated fixture consent is restricted to generated test fixtures")
    dependency_path = runtime.parent / ".deps" if runtime else ROOT / "work/skp-deps"
    sys.path[:0] = [str(runtime or ROOT / "src"), str(dependency_path)]
    from dwg_semantic.pipeline import (WorkflowError, _read, _write, approve_job,
        build_job, digest_file, inspect_source, review_job)
    before = digest_file(source)
    initial = inspect_source(source, job, unit="mm")
    try:
        build_job(job)
    except WorkflowError:
        no_approval_blocked = True
    else:
        raise AssertionError("Build unexpectedly accepted a draft")
    decisions = _read(job / "decisions.json")
    decisions["default_height_mm"] = 3250
    for layer, config in decisions["layer_mapping"].items():
        config.update(category="column" if layer == "CONS_COL" else "architectural_wall" if layer == "VENDOR_WALL" else "reference", footprint_confirmed=True)
    _write(job / "decisions.json", decisions)
    review = review_job(job)
    if review["status"] != "ready_for_confirmation":
        raise AssertionError(json.dumps(review["blockers"], ensure_ascii=False))
    approve_job(job, confirmation="AUTOMATED GENERATED-FIXTURE VALIDATION ONLY; no human approval for any project is implied.", expected_review=review["review_sha256"], approved_by="automated-fixture-validation")
    qa = build_job(job)
    if not qa.get("all_pass"):
        raise AssertionError("Saved-file QA failed")
    original_decisions = (job / "decisions.json").read_bytes()
    decisions["default_height_mm"] = 9999
    _write(job / "decisions.json", decisions)
    try:
        build_job(job, job / "must-not-exist.skp")
    except WorkflowError:
        mutation_blocked = True
    else:
        raise AssertionError("Changed decisions unexpectedly accepted")
    finally:
        (job / "decisions.json").write_bytes(original_decisions)
    document = _read(job / "document.json")
    result = {"schema": "dwg-skp-upgrade-fixture-validation-v2", "runtime_package": str(runtime or ROOT / "src"), "source": str(source), "source_sha256": before, "source_unchanged": digest_file(source) == before, "intake_blockers": document["intake"]["blockers"], "source_stats": document["intake"]["stats"], "draft_build_blocked": no_approval_blocked, "decision_change_blocked": mutation_blocked, "review_sha256": review["review_sha256"], "model_groups": len(qa.get("groups", [])), "saved_file_checks": len(qa.get("checks", [])), "all_pass": qa["all_pass"], "qa": qa}
    _write(job / "upgrade-validation.json", result)
    return {key: value for key, value in result.items() if key != "qa"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--runtime", type=Path)
    args = parser.parse_args()
    print(json.dumps(validate(args.source, args.job, args.runtime), ensure_ascii=False, indent=2))
