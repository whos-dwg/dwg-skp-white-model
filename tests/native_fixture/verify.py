"""Verify real DWG and DXF fixture extraction results without starting AutoCAD.

python tests/native_fixture/verify.py FIXTURE_DIRECTORY
Expected subfolders: dwg-extraction/normalized.json, dxf-extraction/normalized.json.
"""
import hashlib
import json
from pathlib import Path
import sys


def main():
    folder = Path(sys.argv[1]).resolve()
    normalized = []
    evidence = []
    for kind in ("dwg", "dxf"):
        doc = json.loads((folder/f"{kind}-extraction"/"normalized.json").read_text(encoding="utf-8"))
        intake = doc["intake"]
        assert intake["blockers"] == [], intake["blockers"]
        assert intake["unsupported"] == [], intake["unsupported"]
        assert intake["warnings"] == [], intake["warnings"]
        assert intake["backend"] == "autocad:AutoCAD 2026"
        assert intake["source_sha256"] == hashlib.sha256((folder/f"vendor-plan.{kind}").read_bytes()).hexdigest()
        assert intake["input_units"] == "mm"
        leaves = [item for item in doc["resolved_instances"] if item["type"] == "LWPOLYLINE"]
        assert len(leaves) == 2
        assert all(item["original_layer"] == "0" and item["effective_layer"] == "VENDOR_WALL" for item in leaves)
        assert leaves[0]["source_handle"] == leaves[1]["source_handle"]
        assert leaves[0]["entity_id"] != leaves[1]["entity_id"]
        actual = [sorted((round(p["x"],6),round(p["y"],6)) for p in leaf["vertices_world"]) for leaf in leaves]
        assert sorted(actual) == sorted([
            [(5850.,1000.),(5850.,3000.),(6000.,1000.),(6000.,3000.)],
            [(1500.,0.),(1500.,300.),(3500.,0.),(3500.,300.)],
        ]), actual
        profiles = [item for item in doc["entities"] if item.get("owner_block")=="*Model_Space" and item["type"]=="LWPOLYLINE"] + leaves
        assert len(profiles) == 5
        assert all(item["closed"] for item in profiles)
        signature = sorted((item["effective_layer"],tuple(sorted((round(p["x"],6),round(p["y"],6)) for p in item["vertices_world"]))) for item in profiles)
        normalized.append(signature)
        evidence.append({"format": kind, "source_sha256": intake["source_sha256"], "closed_profiles":len(profiles),
                         "nested_layer0_profiles":len(leaves), "blockers":[], "unsupported":[], "source_unchanged":True})
    assert normalized[0] == normalized[1], "DWG and DXF disagree in normalized world geometry"
    result = {"passed":True,"checks":["source_hash_unchanged","DWG_DXF_geometry_equivalence","nested_rotation","mirrored_nonuniform_scaling",
                                      "nearest_layer0_inheritance","repeated_instance_identity","closed_profile_preservation"],"sources":evidence}
    target = folder/"native-fixture-validation.json"
    target.write_text(json.dumps(result,indent=2),encoding="utf-8")
    print(target)


if __name__ == "__main__":
    main()
