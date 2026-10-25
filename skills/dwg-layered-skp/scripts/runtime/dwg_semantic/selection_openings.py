"""Turn a read-only AutoCAD Pickfirst snapshot into reviewed opening patches.

The live selection is only an explicit source choice.  Geometry remains bound
to the normalized job and the reviewed model plan; a selection from another
drawing, a truncated snapshot, or an ambiguous host is never treated as an
opening instruction.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union


SCHEMA = "dwg-cad-selection-opening-proposal-v1"
PATCH_SCHEMA = "dwg-cad-selection-opening-patch-v1"
WALL_CATEGORIES = {"architectural_wall", "structural_wall", "curtain"}


class SelectionOpeningError(ValueError):
    """The selection snapshot cannot safely be related to this job."""


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                         allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _path_key(value: str | Path | None) -> str:
    if not value:
        return ""
    return str(Path(value).expanduser().resolve()).replace("/", "\\").casefold()


def _finite(value: Any, label: str, *, positive: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise SelectionOpeningError(f"{label} 必須是數值。") from error
    if not math.isfinite(number) or (positive and number <= 0):
        requirement = "正數" if positive else "有限數值"
        raise SelectionOpeningError(f"{label} 必須是{requirement}。")
    return number


def _xy(point: Any) -> tuple[float, float]:
    if isinstance(point, dict):
        return _finite(point.get("x"), "X"), _finite(point.get("y"), "Y")
    if not isinstance(point, (list, tuple)) or len(point) < 2:
        raise SelectionOpeningError("來源輪廓包含無效座標。")
    return _finite(point[0], "X"), _finite(point[1], "Y")


def _entity_polygon(entity: dict) -> Polygon | None:
    if not entity.get("closed"):
        return None
    points = (entity.get("curve_points_world") or entity.get("vertices_world") or
              entity.get("curve_points") or entity.get("vertices"))
    if not isinstance(points, list) or len(points) < 3:
        return None
    polygon = Polygon([_xy(item.get("point", item) if isinstance(item, dict) else item)
                       for item in points])
    return polygon if polygon.is_valid and polygon.area > .1 else None


def _group_polygon(group: dict) -> Polygon | None:
    try:
        polygon = Polygon([_xy(point) for point in group["outer"]],
                          [[_xy(point) for point in ring] for ring in group.get("holes", [])])
    except (KeyError, TypeError, SelectionOpeningError, ValueError):
        return None
    return polygon if polygon.is_valid and polygon.area > .1 else None


def _root_instance(path: Any) -> str:
    return str(path or "").split("/", 1)[0]


def _source_entities(document: dict, drawing_path: str, handle: str) -> list[dict]:
    """Return the selected top-level entity and its normalized block leaves."""
    drawing = _path_key(drawing_path)
    output = []
    for entity in [*document.get("entities", []), *document.get("resolved_instances", [])]:
        source_handle = str(entity.get("source_handle") or entity.get("handle") or "")
        instance_path = str(entity.get("instance_path") or "")
        direct = (_path_key(entity.get("source_file")) == drawing and not instance_path and
                  source_handle == handle)
        descendant = (bool(instance_path) and _path_key(entity.get("source_file")) == drawing and
                      _root_instance(instance_path) == handle)
        if direct or descendant:
            output.append(entity)
    unique = {}
    for entity in output:
        identity = str(entity.get("entity_id") or entity.get("id") or
                       f"{entity.get('source_file')}:{entity.get('instance_path')}:{entity.get('source_handle')}")
        unique[identity] = entity
    return list(unique.values())


def _selection_polygon(obj: dict, entities: list[dict], scale_to_mm: float) -> tuple[Polygon | None, str]:
    """Prefer an exact selected closed source; otherwise use its WCS extent."""
    handle = str(obj.get("handle") or "")
    exact = [entity for entity in entities
             if str(entity.get("source_handle") or entity.get("handle") or "") == handle
             and not entity.get("instance_path")]
    polygons = [polygon for entity in exact if (polygon := _entity_polygon(entity)) is not None]
    if len(polygons) == 1:
        return polygons[0], "selected_closed_source_contour"
    bounds = obj.get("bounding_box") or {}
    if str(bounds.get("coordinate_system") or "").upper() != "WCS":
        return None, "selection_bbox_not_wcs"
    minimum, maximum = bounds.get("minimum"), bounds.get("maximum")
    if not isinstance(minimum, list) or not isinstance(maximum, list) or len(minimum) < 2 or len(maximum) < 2:
        return None, "selection_bbox_missing"
    x0, y0 = _finite(minimum[0], "選取框 Xmin") * scale_to_mm, _finite(minimum[1], "選取框 Ymin") * scale_to_mm
    x1, y1 = _finite(maximum[0], "選取框 Xmax") * scale_to_mm, _finite(maximum[1], "選取框 Ymax") * scale_to_mm
    if x1 - x0 <= .01 or y1 - y0 <= .01:
        return None, "selection_bbox_has_no_plan_area"
    return Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)]), "selection_wcs_bbox"


def _polygon_payload(polygon: Polygon) -> dict:
    return {
        "outer": [[float(x), float(y)] for x, y in list(polygon.exterior.coords)[:-1]],
        "holes": [[[float(x), float(y)] for x, y in list(ring.coords)[:-1]]
                  for ring in polygon.interiors],
    }


def _selection_errors(selection: dict, document: dict) -> tuple[str, float]:
    if not isinstance(selection, dict) or selection.get("success") is not True:
        raise SelectionOpeningError("AutoCAD 選取快照未成功。")
    if selection.get("status") != "SELECTED" or not selection.get("objects"):
        raise SelectionOpeningError("AutoCAD 目前沒有可用的 Pickfirst 選取物件。")
    if selection.get("selection_source") != "PickfirstSelectionSet":
        raise SelectionOpeningError("只接受 AutoCAD 目前的 Pickfirst 選取集。")
    if selection.get("truncated") is not False:
        raise SelectionOpeningError("AutoCAD 選取結果已截斷；縮小選取範圍後重試。")
    objects = selection.get("objects")
    count = selection.get("selection_count")
    if (not isinstance(objects, list) or not isinstance(count, int) or isinstance(count, bool)
            or not isinstance(selection.get("returned_count"), int) or isinstance(selection.get("returned_count"), bool)
            or count != selection.get("returned_count") or count != len(objects)):
        raise SelectionOpeningError("AutoCAD 選取數量與回傳物件數不一致。")
    handles = [str(row.get("handle") or "").casefold() for row in objects]
    indices = [row.get("selection_index") for row in objects]
    if not all(handles) or len(set(handles)) != count or indices != list(range(count)):
        raise SelectionOpeningError("AutoCAD 選取 handle／順序不完整或重複。")
    drawing = str((selection.get("document") or {}).get("path") or "")
    source = str((document.get("intake") or {}).get("source_path") or "")
    if not drawing or _path_key(drawing) != _path_key(source):
        raise SelectionOpeningError("目前 AutoCAD 圖檔不是這個白模工作的主來源 DWG。")
    intake = document.get("intake") or {}
    if intake.get("units") != "mm":
        raise SelectionOpeningError("白模來源尚未正規化為 mm。")
    factor = _finite(intake.get("scale_to_mm"), "來源單位換算倍率", positive=True)
    return drawing, factor


def propose_selection_openings(document: dict, model_plan: dict, selection: dict, *,
                               height_mm: float, base_z_mm: float = 0,
                               opening_type: str = "opening", host_tolerance_mm: float = 1.0,
                               existing_patch_ids: set[str] | None = None) -> dict:
    """Produce source-bound opening patches without changing a job or SKP."""
    drawing, scale = _selection_errors(selection, document)
    if opening_type not in {"opening", "door", "window"}:
        raise SelectionOpeningError("開口類型必須是 opening、door 或 window。")
    height = _finite(height_mm, "開口高度", positive=True)
    base = _finite(base_z_mm, "開口基底")
    tolerance = _finite(host_tolerance_mm, "宿主搜尋容差", positive=True)
    source_sha = str((document.get("intake") or {}).get("source_sha256") or "")
    if not source_sha:
        raise SelectionOpeningError("白模工作缺少來源雜湊。")
    snapshot_sha = _digest(selection)
    occupied = set(existing_patch_ids or set())
    hosts = []
    for group in model_plan.get("groups", []):
        if group.get("category") not in WALL_CATEGORIES:
            continue
        polygon = _group_polygon(group)
        if polygon is not None:
            hosts.append((group, polygon))
    if not hosts:
        raise SelectionOpeningError("目前模型方案沒有可作為開口宿主的牆或帷幕。")

    items, patches, blockers = [], [], []
    for obj in selection.get("objects", []):
        handle = str(obj.get("handle") or "")
        row = {"selection_index": obj.get("selection_index"), "handle": handle,
               "object_type": obj.get("object_type"), "status": "blocked", "issues": []}
        if not handle:
            row["issues"].append("selection_object_missing_handle")
            blockers.append({"code": "selection_object_missing_handle", "selection_index": obj.get("selection_index")})
            items.append(row)
            continue
        matched = _source_entities(document, drawing, handle)
        from .plan_geometry import source_plane
        planes = [source_plane(entity) for entity in matched if str(entity.get("type", "")).upper() not in {"INSERT", "BLOCK_REFERENCE", "TEXT", "MTEXT", "ATTRIB"}]
        if any(plane["nonplanar"] for plane in planes) or len({round(plane["plane_z_mm"], 6) for plane in planes}) > 1:
            raise SelectionOpeningError("所選開口來源非共面或跨 Z 平面，不允許無記錄投影。")
        source_ids = sorted({str(entity.get("entity_id") or "") for entity in matched if entity.get("entity_id")})
        row["matched_source_entity_ids"] = source_ids
        if not matched:
            row["issues"].append("selection_handle_not_in_normalized_source")
            blockers.append({"code": "selection_handle_not_in_normalized_source", "handle": handle})
            items.append(row)
            continue
        source_polygon, basis = _selection_polygon(obj, matched, scale)
        row["footprint_basis"] = basis
        if source_polygon is None or not source_polygon.is_valid or source_polygon.area <= .1:
            row["issues"].append(basis)
            blockers.append({"code": basis, "handle": handle})
            items.append(row)
            continue
        candidates = [(group, polygon) for group, polygon in hosts if polygon.distance(source_polygon) <= tolerance]
        if not candidates:
            row["issues"].append("selection_opening_host_not_found")
            blockers.append({"code": "selection_opening_host_not_found", "handle": handle})
            items.append(row)
            continue
        host_union: BaseGeometry = unary_union([polygon for _, polygon in candidates])
        cut = source_polygon.intersection(host_union)
        parts = [part for part in getattr(cut, "geoms", [cut])
                 if isinstance(part, Polygon) and part.area > .1]
        if len(parts) != 1:
            code = "selection_opening_no_material_overlap" if not parts else "selection_opening_spans_disconnected_hosts"
            row["issues"].append(code)
            blockers.append({"code": code, "handle": handle,
                             "candidate_host_ids": [group.get("id") for group, _ in candidates]})
            items.append(row)
            continue
        footprint = parts[0]
        affected = [group for group, polygon in candidates if polygon.intersection(footprint).area > .1]
        signatures = {(group.get("category"), group.get("tag"), group.get("base_z_mm"), group.get("height_mm"))
                      for group in affected}
        if not affected or len(signatures) != 1:
            row["issues"].append("selection_opening_host_material_ambiguous")
            blockers.append({"code": "selection_opening_host_material_ambiguous", "handle": handle,
                             "candidate_host_ids": [group.get("id") for group in affected]})
            items.append(row)
            continue
        identity_seed = [source_sha, drawing.casefold(), handle]
        patch_id = "cadsel-opening:" + _digest(identity_seed)[:20]
        if patch_id in occupied:
            row["issues"].append("duplicate_selection_opening_patch")
            blockers.append({"code": "duplicate_selection_opening_patch", "handle": handle,
                             "patch_id": patch_id})
            items.append(row)
            continue
        occupied.add(patch_id)
        source_layer = str((obj.get("properties") or {}).get("Layer") or
                           matched[0].get("effective_layer") or matched[0].get("layer") or "0")
        patch = {
            "schema": PATCH_SCHEMA,
            "id": patch_id,
            "name": f"CAD selection {handle} {opening_type}",
            "opening_type": opening_type,
            "selection_handle": handle,
            "selection_object_type": obj.get("object_type"),
            "selection_snapshot_sha256": snapshot_sha,
            "selection_observed_at_utc": selection.get("observed_at_utc"),
            "source_path": drawing,
            "source_sha256": source_sha,
            "source_layer": source_layer,
            "source_entity_ids": source_ids,
            "source_handles": sorted({handle, *[str(entity.get("source_handle") or "") for entity in matched]} - {""}),
            "host_ids": [str(group.get("id")) for group in affected],
            "footprint": _polygon_payload(footprint),
            "footprint_basis": basis + "_intersect_reviewed_host_material",
            "base_z_mm": base,
            "height_mm": height,
            "evidence": ["user_selected_autocad_pickfirst_object", "normalized_source_identity_match",
                         "selection_extent_intersected_reviewed_host_material"],
        }
        patches.append(patch)
        row.update(status="proposed", patch_id=patch_id, issues=[], host_ids=patch["host_ids"],
                   footprint=patch["footprint"], base_z_mm=base, height_mm=height)
        items.append(row)

    proposal = {
        "schema": SCHEMA,
        "status": "blocked" if blockers else "ready_for_review",
        "source_path": drawing,
        "source_sha256": source_sha,
        "selection_snapshot_sha256": snapshot_sha,
        "selection_observed_at_utc": selection.get("observed_at_utc"),
        "selection_count": selection.get("selection_count"),
        "model_plan_sha256": _digest(model_plan),
        "opening_type": opening_type,
        "base_z_mm": base,
        "height_mm": height,
        "host_tolerance_mm": tolerance,
        "items": items,
        "patches": patches,
        "blockers": blockers,
    }
    proposal["proposal_sha256"] = _digest(proposal)
    return proposal
