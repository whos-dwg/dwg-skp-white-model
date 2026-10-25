"""Compare a rerun against source-bound, previously reviewed geometry.

References live with the case, not in classification rules. Their shapes are
read only by this checker, never by the recognition engine.
"""
from __future__ import annotations

import json
from pathlib import Path

from shapely.geometry import LineString, Polygon, box
from shapely.ops import unary_union

from .plan_geometry import shape_of


def select(result,collection):
    if collection=="walls":
        return result["wall_materials"]
    if collection=="door_level_walls":
        return result["wall_materials_at_door_level"]
    if collection=="columns":
        return result["columns"]
    if collection=="doors":
        return result["door_assemblies"]
    if collection.startswith("facade:"):
        role=collection.split(":",1)[1]
        return [row for system in result["facade_systems"]for row in system["sections"]if row["semantic_subrole"]==role]
    if collection=="hatch_profiles":
        return [row for system in result["facade_systems"]for row in system["hatch_profiles"]]
    if collection=="facade_bay_air":
        return [row for system in result["facade_systems"]for row in system.get("bay_air_regions",[])]
    return result["slab_openings"][collection]


def check_recognition(result,reference,document=None):
    if reference.get("source_sha256")!=result.get("source_sha256"):
        raise ValueError("Regression reference belongs to a different source DWG SHA256")
    checks=[]
    for spec in reference["checks"]:
        kind=spec["kind"];collection=spec.get("collection");tolerance=spec.get("tolerance_mm",.1)
        rows=select(result,collection)if collection else[]
        if spec.get("where_center_in_bounds"):
            region=box(*spec["where_center_in_bounds"])
            rows=[row for row in rows if region.covers(shape_of(row).centroid)]
        record={"id":spec["id"],"kind":kind}
        if kind=="count":
            record.update(actual=len(rows),expected=spec["expected"],pass_value=len(rows)==spec["expected"])
        elif kind in{"material_contains","material_equal","material_excludes"}:
            actual=unary_union([shape_of(row)for row in rows])
            expected=shape_of(spec["footprint"])
            if kind=="material_excludes":
                error=actual.intersection(expected.buffer(-tolerance)).area
                record.update(intruding_material_area_mm2=error,pass_value=error<=spec.get("area_tolerance_mm2",.1))
            else:
                missing=expected.difference(actual.buffer(tolerance)).area
                extra=actual.difference(expected.buffer(tolerance)).area if kind=="material_equal"else 0.0
                record.update(missing_area_mm2=missing,extra_area_mm2=extra,
                              pass_value=missing+extra<=spec.get("area_tolerance_mm2",.1))
        elif kind in{"door_covers_reviewed_span","door_cuts_clear"}:
            source_id=spec.get("source_container_entity_id")
            source_ids=set(spec.get("source_entity_ids",[]))
            matched=[row for row in rows if (source_id and source_id in{row.get("source_container_entity_id"),*row.get("source_container_entity_ids",[])})
                     or(source_ids and source_ids<=set(row["source_entity_ids"]))]
            good=[row for row in matched if row.get("clearance_footprint")]
            if kind=="door_covers_reviewed_span":
                target=LineString(spec["axis_world_mm"])
                ok=bool(good)and any(Polygon(row["clearance_footprint"]["outer"]).buffer(tolerance).covers(target)
                    and row.get("leaf_count",0)>=spec.get("minimum_leaf_count",0)for row in good)
                record.update(pass_value=ok,matched_assembly_ids=[row["id"]for row in good])
            else:
                material=unary_union([shape_of(row)for row in result["wall_materials_at_door_level"]])
                overlaps=[material.intersection(Polygon(row["clearance_footprint"]["outer"]).buffer(-tolerance)).area for row in good]
                record.update(pass_value=bool(good)and max(overlaps,default=0)<=.1,remaining_material_mm2=max(overlaps,default=None))
        elif kind=="native_source_geometry":
            if document is None:
                raise ValueError("Native source geometry checks require the extracted document")
            entities=[*document.get("entities",[]),*document.get("resolved_instances",[])]
            matched=[row for row in entities if row.get("entity_id")==spec["source_entity_id"]]
            ok=len(matched)==1
            if ok:
                row=matched[0]
                ok=all(row.get(key)==value for key,value in spec.get("expected_fields",{}).items())
                ok=ok and len(row.get(spec.get("points_field","vertices_world"),[]))==spec["point_count"]
            record.update(pass_value=ok)
        else:
            raise ValueError("Unknown recognition regression check: "+kind)
        record["pass"]=record.pop("pass_value")
        checks.append(record)
    return {"schema":"dwg-recognition-regression-v1","source_sha256":result["source_sha256"],
            "all_pass":all(row["pass"]for row in checks),"passed":sum(row["pass"]for row in checks),
            "total":len(checks),"reference_sources":reference.get("reference_sources",{}),
            "correction_notes":reference.get("correction_notes",[]),"checks":checks}


def write_regression(report,destination):
    destination=Path(destination)
    destination.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    md=["# 先前修正事項回歸檢查", "",f"通過 {report['passed']} / {report['total']}；來源 SHA256：`{report['source_sha256']}`。", "",
        "基準輪廓取自先前已展示的修正檢查資料；辨識引擎不讀取這份基準。", ""]
    md += ["- "+str(note)for note in report.get("correction_notes",[])]
    md += ["", "| 檢查 | 結果 |", "|---|---|"]
    md += [f"| {row['id']} | {'通過'if row['pass']else'需處理'} |"for row in report["checks"]]
    destination.with_suffix(".md").write_text("\n".join(md)+"\n",encoding="utf-8")
