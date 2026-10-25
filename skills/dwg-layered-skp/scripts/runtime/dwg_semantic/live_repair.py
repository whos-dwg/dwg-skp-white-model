"""Plan bounded, undoable repairs for the active SketchUp white model.

The planner is deliberately separate from the SketchUp write bridge.  It
relates an explicit AutoCAD Pickfirst selection to the source-bound groups in
the active model snapshot, computes complete replacement prisms, and emits a
hash-bound plan.  The bridge may apply only that plan after rechecking model,
target, parent, and geometry signatures.
"""
from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from typing import Any

from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import orient, polygonize, triangulate, unary_union

from .modeling import apply_selection_opening_patches
from .selection_openings import (
    SelectionOpeningError,
    _entity_polygon,
    _selection_errors,
    _source_entities,
    propose_selection_openings,
)


PLAN_SCHEMA = "dwg-skp-live-repair-plan-v1"
SNAPSHOT_SCHEMA = "dwg-skp-live-snapshot-v1"
SUPPORTED_ACTIONS = {"cut_opening", "add_missing", "replace_from_cad", "remove_wrong"}
SUPPORTED_CATEGORIES = {
    "architectural_wall", "structural_wall", "column", "curtain", "curtain_accessory",
}
AREA_TOLERANCE_MM2 = 0.1
HEIGHT_TOLERANCE_MM = 0.01


class LiveRepairError(ValueError):
    """A live repair cannot be proven safe from the supplied evidence."""


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _number(value: Any, label: str, *, positive: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise LiveRepairError(f"{label} 必須是數值。") from error
    if not math.isfinite(number) or (positive and number <= 0):
        raise LiveRepairError(f"{label} 必須是{'正數' if positive else '有限數值'}。")
    return number


def _xy(value: Any) -> tuple[float, float]:
    if isinstance(value, dict):
        value = value.get("point", value)
        if isinstance(value, dict):
            return _number(value.get("x"), "X"), _number(value.get("y"), "Y")
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        raise LiveRepairError("來源幾何包含無效座標。")
    return _number(value[0], "X"), _number(value[1], "Y")


def _parts(geometry: BaseGeometry) -> list[Polygon]:
    if isinstance(geometry, Polygon):
        values = [geometry]
    elif isinstance(geometry, MultiPolygon):
        values = list(geometry.geoms)
    else:
        values = [part for part in getattr(geometry, "geoms", []) if isinstance(part, Polygon)]
    return [orient(part, sign=1.0) for part in values if part.is_valid and part.area > AREA_TOLERANCE_MM2]


def _polygon_payload(polygon: Polygon) -> dict:
    polygon = orient(polygon, sign=1.0)
    def ring_values(ring):
        points = [[float(x), float(y)] for x, y in list(ring.coords)[:-1]]
        start = min(range(len(points)), key=lambda index: points[index])
        return points[start:] + points[:start]
    return {
        "outer": ring_values(polygon.exterior),
        "holes": [
            ring_values(ring)
            for ring in polygon.interiors
        ],
    }


def _payload_polygon(value: dict) -> Polygon:
    try:
        polygon = Polygon(value["outer"], value.get("holes", []))
    except (KeyError, TypeError, ValueError) as error:
        raise LiveRepairError("SketchUp 即時快照包含無效的平面輪廓。") from error
    if not polygon.is_valid or polygon.area <= AREA_TOLERANCE_MM2:
        raise LiveRepairError("SketchUp 即時快照包含空白或自交輪廓。")
    return orient(polygon, sign=1.0)


def _points(entity: dict) -> list[tuple[float, float]]:
    raw = (entity.get("curve_points_world") or entity.get("vertices_world") or
           entity.get("curve_points") or entity.get("vertices") or [])
    if not isinstance(raw, list):
        return []
    try:
        return [_xy(item) for item in raw]
    except LiveRepairError:
        return []


def _entity_lines(entity: dict) -> list[LineString]:
    lines: list[LineString] = []
    for item in entity.get("segments") or []:
        if not isinstance(item, dict):
            continue
        try:
            start = _xy(item.get("start_world") or item.get("start"))
            end = _xy(item.get("end_world") or item.get("end"))
        except LiveRepairError:
            continue
        if start != end:
            lines.append(LineString([start, end]))
    points = _points(entity)
    for first, second in zip(points, points[1:]):
        if first != second:
            lines.append(LineString([first, second]))
    if entity.get("closed") and len(points) > 2 and points[-1] != points[0]:
        lines.append(LineString([points[-1], points[0]]))
    if not lines and (entity.get("start_world") or entity.get("start")):
        start = _xy(entity.get("start_world") or entity["start"])
        end = _xy(entity.get("end_world") or entity["end"])
        if start != end:
            lines.append(LineString([start, end]))
    return lines


def _selection_regions(document: dict, selection: dict, *, tag_policy='effective') -> list[dict]:
    """Resolve exact source contours; a bounding box is never enough to add/replace."""
    drawing, _ = _selection_errors(selection, document)
    from .plan_geometry import source_plane, layer_of, hatch_profiles, shape_of
    from .modeling import _raw_layer
    selected = {}
    for obj in selection.get("objects", []):
        handle = str(obj.get("handle") or "")
        matched = _source_entities(document, drawing, handle)
        if not matched:
            raise LiveRepairError(f"CAD handle {handle or '(missing)'} 不在白模的正規化來源中。")
        for entity in matched:
            if str(entity.get("type", "")).upper() in {"INSERT", "BLOCK_REFERENCE"}:
                continue
            plane = source_plane(entity)
            if plane["nonplanar"]:
                raise LiveRepairError("CAD 選取含非共面來源，不能無記錄地投影到 XY。")
            selected[str(entity.get("entity_id"))] = (entity, plane["plane_z_mm"], _raw_layer(entity,tag_policy))
    # All explicitly selected compatible segments form one source network.
    # Even/odd nested rings preserve material cavities; never union the inner
    # polygonized face back into its surrounding wall.
    grouped = {}
    for identity, (entity, z, layer) in selected.items():
        grouped.setdefault((round(z, 6), layer), []).append((identity, entity))
    if len({z for z, _ in grouped}) > 1:
        raise LiveRepairError("CAD 選取跨不同 Z 平面；需分開修正。")
    rows = []
    accounted = set()
    for (_, layer), entries in sorted(grouped.items()):
        lines = [line for _, entity in entries for line in _entity_lines(entity)]
        profiles = []
        for _, entity in entries:
            if entity.get("type") == "HATCH":
                found, failures = hatch_profiles(entity)
                if failures:
                    raise LiveRepairError("所選 HATCH 有未支援邊界。")
                profiles.extend(shape_of(row) for row in found)
        faces = list(polygonize(unary_union(lines))) if lines else []
        rings = [Polygon(face.exterior) for face in faces]
        for face, ring in zip(faces, rings):
            if sum(other.area > ring.area + .1 and other.covers(ring) for other in rings) % 2 == 0:
                profiles.append(face)
        parts = _parts(unary_union(profiles)) if profiles else []
        if not parts:
            raise LiveRepairError("CAD 選取沒有可證明的封閉輪廓；不能用外接框猜測。")
        for index, polygon in enumerate(parts):
            contributors = [(identity, entity) for identity, entity in entries if
                any(line.intersection(polygon.boundary.buffer(.00001)).length > .00001
                    for line in _entity_lines(entity)) or entity.get("type") == "HATCH"]
            accounted.update(identity for identity, _ in contributors)
            rows.append({"selection_handle": "+".join(sorted(str(obj["handle"]) for obj in selection["objects"])),
                         "part_index": index, "polygon": polygon,
                         "source_entity_ids": sorted(identity for identity, _ in contributors),
                         "source_handles": sorted({str(entity.get("source_handle") or entity.get("handle")) for _, entity in contributors}),
                         "source_layer": layer, "source_layers": [layer]})
    if not rows:
        raise LiveRepairError("CAD 選取沒有可證明的封閉輪廓。")
    if set(selected) - accounted:
        raise LiveRepairError("CAD 選取含未形成封閉材料界的來源：" + ", ".join(sorted(set(selected)-accounted)))
    return rows


def _validate_snapshot(snapshot: dict) -> None:
    if not isinstance(snapshot, dict) or snapshot.get("schema") != SNAPSHOT_SCHEMA:
        raise LiveRepairError("需要 DWG 即時修正橋接器產生的 SketchUp 快照。")
    if snapshot.get("status") != "ready":
        raise LiveRepairError("目前 SketchUp 模型不是可修正狀態。")
    model = snapshot.get("model") or {}
    if not model.get("guid") or not model.get("path"):
        raise LiveRepairError("目前 SketchUp 模型尚未儲存或缺少穩定 GUID。")
    if not snapshot.get("root") or not isinstance(snapshot.get("entities"), list):
        raise LiveRepairError("目前模型沒有可辨識的 DWG white-model 根節點。")
    transform = snapshot["root"].get("world_transform") or {}
    origin = transform.get("origin_mm", [0, 0, 0])
    axes = [transform.get(name) for name in ("xaxis", "yaxis", "zaxis")]
    identity = ([round(_number(value, "根節點位移"), 6) for value in origin] == [0, 0, 0] and
                axes == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
    if not identity:
        raise LiveRepairError("DWG model 根群組已被移動、旋轉或縮放；目前版本不猜測 CAD 與模型座標對應。")


def _provenance(entity: dict) -> dict:
    value = entity.get("provenance") or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = {}
    return value if isinstance(value, dict) else {}


def _actual_footprint(entity: dict) -> Polygon:
    if "bottom_triangles_mm" in entity:
        # Native face loops may contain zero-area backtracking seams. Use the
        # actual bottom-face tessellation, not a buffer, snapped contour or
        # stale construction footprint. World volume is checked below.
        triangles = entity["bottom_triangles_mm"]
        if not isinstance(triangles, list) or not triangles:
            raise LiveRepairError("目前實體沒有可驗證的底面三角網。")
        faces = []
        for points in triangles:
            if not isinstance(points, list) or len(points) != 3:
                raise LiveRepairError("目前實體的底面三角網資料不完整。")
            face = Polygon([_xy(point) for point in points])
            # SketchUp tessellation can include exactly collinear triangles
            # along a face seam. They contain no material; do not buffer them
            # or discard any positive-area thin source feature.
            if face.area == 0:
                continue
            if not face.is_valid:
                raise LiveRepairError("目前實體的底面三角網包含退化三角形。")
            faces.append(face)
        if not faces:
            raise LiveRepairError("目前實體的底面三角網沒有材料面積。")
        polygon = unary_union(faces)
        if not isinstance(polygon, Polygon) or not polygon.is_valid or polygon.area <= AREA_TOLERANCE_MM2:
            raise LiveRepairError("目前實體的實際底面不是單一閉合材料區。")
        if abs(sum(face.area for face in faces)-polygon.area) > max(AREA_TOLERANCE_MM2, polygon.area*1e-8):
            raise LiveRepairError("目前實體的實際底面三角網重疊。")
        polygon = orient(polygon, sign=1.0)
    else:
        polygon = _payload_polygon(entity.get("footprint"))
    return polygon


def _group_from_entity(entity: dict) -> dict:
    provenance = deepcopy(_provenance(entity))
    polygon = _actual_footprint(entity)
    if entity.get("manifold") is not True or entity.get("volume_mm3") is None:
        raise LiveRepairError("目前白模葉件不是可驗證的封閉擠出實體。")
    expected = polygon.area * _number(entity.get("height_mm"), "SketchUp 實體高度", positive=True)
    actual = _number(entity["volume_mm3"], "SketchUp 實體材積", positive=True)
    if abs(actual - expected) > max(1., expected * 1e-5):
        raise LiveRepairError("目前實體不再符合精確稜柱輪廓，不能沿用過期 footprint。")
    group = {
        **provenance,
        "id": str(entity.get("group_id") or provenance.get("id") or ""),
        "name": str(entity.get("name") or provenance.get("name") or "live group"),
        "category": str(entity.get("category") or provenance.get("category") or ""),
        "tag": str(entity.get("tag") or provenance.get("tag") or "Untagged"),
        "source_layer": str(provenance.get("source_layer") or entity.get("tag") or "0"),
        "source_layers": list(provenance.get("source_layers") or [provenance.get("source_layer") or entity.get("tag") or "0"]),
        "source_entity_ids": list(provenance.get("source_entity_ids") or []),
        "source_handles": list(provenance.get("source_handles") or []),
        "outer": _polygon_payload(polygon)["outer"],
        "holes": _polygon_payload(polygon)["holes"],
        "base_z_mm": _number(entity.get("base_z_mm"), "SketchUp 實體基底"),
        "height_mm": _number(entity.get("height_mm"), "SketchUp 實體高度", positive=True),
        "mode": "solid_extrusion",
        "area_mm2": polygon.area,
        "volume_mm3": polygon.area * _number(entity.get("height_mm"), "SketchUp 實體高度", positive=True),
    }
    if not group["id"] or not group["category"]:
        raise LiveRepairError("SketchUp 實體缺少白模 ID 或構件類別。")
    return group


def _live_payload(snapshot: dict) -> tuple[dict, dict[str, dict]]:
    groups, by_id = [], {}
    for entity in snapshot.get("entities", []):
        group = _group_from_entity(entity)
        if group["id"] in by_id:
            raise LiveRepairError(f"目前 SketchUp 模型含重複白模 ID：{group['id']}")
        groups.append(group)
        by_id[group["id"]] = entity
    payload = {
        "schema": "dwg-skp-model-v2", "units": "mm", "groups": groups,
        "references": [], "excluded": [], "blockers": [], "warnings": [],
        "tag_mapping": [], "opening_audit": [],
    }
    return payload, by_id


def _identifiers(entity: dict) -> set[str]:
    provenance = _provenance(entity)
    return {
        str(entity.get("group_id") or ""),
        *[str(value) for value in provenance.get("host_lineage_ids", [])],
        *[str(value) for value in provenance.get("source_entity_ids", [])],
        *[str(value) for value in provenance.get("source_handles", [])],
    } - {""}


def _target_row(entity: dict) -> dict:
    if entity.get("category") not in SUPPORTED_CATEGORIES:
        raise LiveRepairError("即時修正不支援此目標類別。")
    if entity.get("locked"):
        raise LiveRepairError(f"SketchUp 目標 {entity.get('entity_id')} 已鎖定。")
    return {
        "entity_id": entity["entity_id"],
        "group_id": entity["group_id"],
        "parent_entity_id": entity["parent_entity_id"],
        "signature": entity["signature"],
        "name": entity.get("name"),
        "category": entity.get("category"),
        "tag": entity.get("tag"),
    }


def _mesh(polygon: Polygon, bottom: float, height: float) -> dict:
    """Create a welded manifold prism mesh in model/world millimeters."""
    polygon = orient(polygon, sign=1.0)
    top = bottom + height
    triangles = [triangle for triangle in triangulate(polygon)
                 if triangle.area > AREA_TOLERANCE_MM2 and polygon.covers(triangle)]
    covered = unary_union(triangles) if triangles else Polygon()
    if abs(covered.area - polygon.area) > max(AREA_TOLERANCE_MM2, polygon.area * 1e-8):
        # Unconstrained Delaunay can straddle a nearly-collinear cavity edge.
        # Constrain the triangulation to the exact ring, without buffering or
        # dropping positive-area source triangles to make a failed cap pass.
        try:
            from shapely import constrained_delaunay_triangles
            triangles=[t for t in constrained_delaunay_triangles(polygon).geoms if t.area>0]
        except (ImportError,AttributeError) as error:
            raise LiveRepairError("輪廓需要可用的約束三角化支援。") from error
        covered=unary_union(triangles) if triangles else Polygon()
        if covered.symmetric_difference(polygon).area>max(AREA_TOLERANCE_MM2,polygon.area*1e-8):
            raise LiveRepairError("輪廓無法可靠三角化為 SketchUp 實體。")
    vertices: list[list[float]] = []
    lookup: dict[tuple[float, float, float], int] = {}

    def vertex(x: float, y: float, z: float) -> int:
        key = (round(float(x), 6), round(float(y), 6), round(float(z), 6))
        if key not in lookup:
            lookup[key] = len(vertices)
            vertices.append([*key])
        return lookup[key]

    faces: list[list[int]] = []
    for triangle in triangles:
        ring = list(orient(triangle, sign=1.0).exterior.coords)[:-1]
        faces.append([vertex(x, y, bottom) for x, y in reversed(ring)])
        faces.append([vertex(x, y, top) for x, y in ring])
    for ring in [polygon.exterior, *polygon.interiors]:
        points = list(ring.coords)[:-1]
        for first, second in zip(points, points[1:] + points[:1]):
            x0, y0 = first
            x1, y1 = second
            faces.append([
                vertex(x0, y0, bottom), vertex(x1, y1, bottom),
                vertex(x1, y1, top), vertex(x0, y0, top),
            ])
    return {
        "coordinate_space": "model_mm",
        "vertices": vertices,
        "faces": faces,
        "expected_volume_mm3": polygon.area * height,
    }


def _container(snapshot: dict, category: str) -> str:
    matches = [item for item in snapshot.get("containers", []) if item.get("name") == category]
    if len(matches) != 1:
        raise LiveRepairError(f"目前模型找不到唯一的 {category} 分類容器。")
    return str(matches[0]["entity_id"])


def _create_row(group: dict, template: dict | None, parent_entity_id: str) -> dict:
    polygon = Polygon(group["outer"], group.get("holes", []))
    if not polygon.is_valid or polygon.area <= AREA_TOLERANCE_MM2:
        raise LiveRepairError(f"修正結果 {group.get('id')} 沒有有效輪廓。")
    category = str(group.get("category") or "")
    if category not in SUPPORTED_CATEGORIES:
        raise LiveRepairError(f"即時幾何修正尚未支援類別：{category}")
    bottom = _number(group.get("base_z_mm"), "修正基底")
    height = _number(group.get("height_mm"), "修正高度", positive=True)
    footprint = _polygon_payload(polygon)
    provenance = deepcopy(group)
    # A sliced right jamb may have the opposite ring winding. The row and its
    # saved provenance must bind the same normalized exact geometry, not two
    # equivalent-but-differently-ordered rings (or the old unsliced host).
    provenance.update(outer=deepcopy(footprint["outer"]), holes=deepcopy(footprint["holes"]),
                      base_z_mm=bottom, height_mm=height)
    if "footprint" in provenance:
        provenance["footprint"] = deepcopy(footprint)
    return {
        "group_id": str(group["id"]),
        "name": str(group.get("name") or group["id"]),
        "category": category,
        "tag": str(group.get("tag") or group.get("source_layer") or "Untagged"),
        "material": (template or {}).get("material") if template else "DWG white model",
        "parent_entity_id": parent_entity_id,
        "footprint": footprint,
        "base_z_mm": bottom,
        "height_mm": height,
        "mesh": _mesh(polygon, bottom, height),
        "provenance": provenance,
    }


def _overlap_blockers(create: list[dict], snapshot: dict, ignored_targets: set[str]) -> list[dict]:
    blockers = []
    remaining = [entity for entity in snapshot.get("entities", [])
                 if entity.get("entity_id") not in ignored_targets]
    for index, row in enumerate(create):
        for other in create[index + 1:]:
            depth = min(row["base_z_mm"] + row["height_mm"], other["base_z_mm"] + other["height_mm"]) - max(row["base_z_mm"], other["base_z_mm"])
            if depth > HEIGHT_TOLERANCE_MM and _payload_polygon(row["footprint"]).intersection(_payload_polygon(other["footprint"])).area > AREA_TOLERANCE_MM2:
                blockers.append({"code": "live_repair_new_solids_overlap", "group_ids": [row["group_id"], other["group_id"]]})
    for row in create:
        polygon = _payload_polygon(row["footprint"])
        lo, hi = row["base_z_mm"], row["base_z_mm"] + row["height_mm"]
        for entity in remaining:
            other_lo = _number(entity.get("base_z_mm"), "既有構件基底")
            other_hi = other_lo + _number(entity.get("height_mm"), "既有構件高度", positive=True)
            z_overlap = min(hi, other_hi) - max(lo, other_lo)
            if z_overlap <= HEIGHT_TOLERANCE_MM:
                continue
            area = polygon.intersection(_actual_footprint(entity)).area
            if area > AREA_TOLERANCE_MM2:
                blockers.append({
                    "code": "live_repair_positive_solid_overlap",
                    "new_group_id": row["group_id"],
                    "existing_entity_id": entity.get("entity_id"),
                    "existing_group_id": entity.get("group_id"),
                    "overlap_area_mm2": area,
                    "overlap_height_mm": z_overlap,
                })
        for protected in snapshot.get("protected_entities", []):
            from .mesh_union import section_from_triangles
            triangles = protected.get("triangles_mm")
            if protected.get("manifold") is not True or not triangles:
                raise LiveRepairError("受保護構件缺少實際封閉材料證據，不能檢查修正碰撞。")
            events = sorted({lo,hi,*[float(p[2]) for triangle in triangles for p in triangle]})
            for bottom, top in zip(events, events[1:]):
                if bottom < lo-.00001 or top > hi+.00001 or top-bottom <= HEIGHT_TOLERANCE_MM:
                    continue
                material = section_from_triangles(triangles,(bottom+top)/2)
                area = polygon.intersection(material).area
                if area > AREA_TOLERANCE_MM2:
                    blockers.append({"code":"live_repair_protected_material_overlap","new_group_id":row["group_id"],
                        "protected_entity_id":protected["entity_id"],"overlap_area_mm2":area,"z_interval_mm":[bottom,top]})
    return blockers


def _common_template(targets: list[dict], *, category: str | None, tag: str | None,
                     base_z_mm: float | None, height_mm: float | None) -> dict:
    groups = [_group_from_entity(entity) for entity in targets]
    categories = {group["category"] for group in groups}
    tags = {group["tag"] for group in groups}
    bases = {group["base_z_mm"] for group in groups}
    heights = {group["height_mm"] for group in groups}
    chosen_category = category or (next(iter(categories)) if len(categories) == 1 else None)
    chosen_tag = tag or (next(iter(tags)) if len(tags) == 1 else None)
    chosen_base = base_z_mm if base_z_mm is not None else (next(iter(bases)) if len(bases) == 1 else None)
    chosen_height = height_mm if height_mm is not None else (next(iter(heights)) if len(heights) == 1 else None)
    if chosen_category not in SUPPORTED_CATEGORIES or not chosen_tag or chosen_base is None or chosen_height is None:
        raise LiveRepairError("多個 SketchUp 目標的類別、Tag、基底或高度不一致；請明確指定。")
    template = deepcopy(groups[0])
    template.update(category=chosen_category, tag=chosen_tag, source_layer=chosen_tag,
                    base_z_mm=_number(chosen_base, "修正基底"),
                    height_mm=_number(chosen_height, "修正高度", positive=True))
    return template


def _fixed_live_standard(snapshot: dict) -> dict | None:
    """A saved standard is an invariant, not a fresh caller height preference."""
    from .modeling_standard import FIXED
    root = snapshot.get("root") or {}
    marker, audit = root.get("modeling_standard"), root.get("fixed_standard_audit")
    if marker is None and audit is None:
        return None  # Historical native deliveries remain unchanged.
    if (marker != FIXED or not isinstance(audit, dict) or audit.get("standard") != FIXED
            or audit.get("all_pass") is not True or audit.get("issues")):
        raise LiveRepairError("目前模型的固定白模規範／稽核缺失或不一致；不能降級為舊版修正。")
    return deepcopy(FIXED)


def _fixed_roof_regions(snapshot: dict) -> list[tuple[float, BaseGeometry, list[str]]]:
    """Lowest horizontal cap regions from actual read-back roof surfaces only."""
    levels: dict[float, list[Polygon]] = {}
    identities: dict[float, set[str]] = {}
    for entity in snapshot.get("protected_entities", []):
        provenance = _provenance(entity)
        role = provenance.get("slab_role") or provenance.get("semantic_role")
        if entity.get("category", provenance.get("category")) != "slab" or not (
                role == "overhead_slab" or provenance.get("host_slab_role") == "overhead_slab"):
            continue
        triangles = entity.get("triangles_mm")
        if entity.get("manifold") is not True or not isinstance(triangles, list) or not triangles:
            raise LiveRepairError("上方樓板缺少實際封閉 mesh，不能以保存的板域代替當地板底。")
        for triangle in triangles:
            if not isinstance(triangle, list) or len(triangle) != 3:
                raise LiveRepairError("上方樓板實際 mesh 不是完整三角網。")
            points = [[_number(v, "板底 mesh 座標") for v in point] for point in triangle]
            if any(len(point) != 3 for point in points):
                raise LiveRepairError("上方樓板 mesh 缺少 XYZ。")
            zs = [point[2] for point in points]
            if max(zs) - min(zs) > 1e-6:
                continue
            polygon = Polygon([point[:2] for point in points])
            if not polygon.is_valid or polygon.area <= AREA_TOLERANCE_MM2:
                continue
            cap = round(sum(zs) / 3, 6)
            levels.setdefault(cap, []).append(polygon)
            identities.setdefault(cap, set()).add(str(entity["entity_id"]))
    claimed = Polygon()
    regions = []
    for cap, shapes in sorted(levels.items()):
        region = unary_union(shapes).difference(claimed)
        if region.area > AREA_TOLERANCE_MM2:
            regions.append((cap, region, sorted(identities[cap])))
        claimed = unary_union([claimed, unary_union(shapes)])
    return regions


def _fit_fixed_live_create(create: list[dict], snapshot: dict, standard: dict,
                           action: str, document: dict) -> list[dict]:
    if action not in {"add_missing", "replace_from_cad"}:
        return create  # Cutting preserves the explicitly requested opening clearance.
    from .modeling_standard import fire_body_source_ids
    fire_ids = fire_body_source_ids(document)
    fire_ids.update((snapshot["root"].get("fixed_standard_audit") or {}).get("fire_body_source_entity_ids", []))
    regions = _fixed_roof_regions(snapshot)
    result = []
    for row in create:
        group = deepcopy(row["provenance"])
        replaced = group.get("live_repair_replaced_group_ids", [])
        actual_targets = [entity for entity in snapshot.get("entities", []) if entity.get("group_id") in replaced]
        sill_targets = [entity for entity in actual_targets if _provenance(entity).get("opening_part_role") == "sill"]
        claims_sill = group.get("opening_part_role") == "sill"
        trusted_sill = (action == "replace_from_cad" and len(replaced) == 1 and len(actual_targets) == 1
                        and len(sill_targets) == 1 and claims_sill
                        and sill_targets[0].get("category") == row["category"]
                        and bool(_provenance(sill_targets[0]).get("opening_ids"))
                        and group.get("opening_ids") == _provenance(sill_targets[0]).get("opening_ids"))
        if claims_sill or sill_targets:
            if not trusted_sill:
                raise LiveRepairError("只有唯一已開洞且明示 sill 的實際洞下段可保留窗台頂；不能以自稱 sill 繞過板底規範。")
            if set(group.get("source_entity_ids", [])) & fire_ids:
                raise LiveRepairError("固定規範不建立防火門／捲簾 3D 本體；洞下段角色不授權建立其本體。")
            target = sill_targets[0]
            base = _number(target.get("base_z_mm"), "實際洞下段基底")
            height = _number(target.get("height_mm"), "實際洞下段高度", positive=True)
            if abs(row["base_z_mm"]-base) > HEIGHT_TOLERANCE_MM:
                raise LiveRepairError("保留已開洞下段時不得改變其實際基底。")
            group.update(base_z_mm=base, height_mm=height, area_mm2=_payload_polygon(row["footprint"]).area,
                         volume_mm3=_payload_polygon(row["footprint"]).area*height)
            group["fixed_live_height_audit"] = {
                "standard_version": standard["version"], "basis": "preserved_actual_source_bound_sill_top",
                "cap_z_mm": base+height, "preserved_base_z_mm": base,
                "original_group_id": target["group_id"], "opening_ids": deepcopy(group["opening_ids"]),
            }
            # The sill is existing host material below an already-established
            # opening. Extending it to the roof would refill that clear space.
            result.append(_create_row(group, row, row["parent_entity_id"]))
            continue
        original_lintels = [entity for entity in snapshot.get("entities", [])
                            if entity.get("group_id") in replaced
                            and _provenance(entity).get("semantic_role") == "door_lintel"
                            and entity.get("category") == row["category"]]
        trusted_lintel = (action == "replace_from_cad" and len(replaced) == 1 and len(original_lintels) == 1
                          and group.get("semantic_role") == "door_lintel")
        if set(group.get("source_entity_ids", [])) & fire_ids and not trusted_lintel:
            raise LiveRepairError("固定規範不建立防火門／捲簾 3D 本體；其來源仍可用 cut_opening 留洞。")
        subrole = str(group.get("semantic_subrole", "")).lower()
        if row["category"] == "curtain_accessory" and subrole.startswith(("railing", "欄杆", "栏杆")):
            result.append(row)
            continue
        shape = _payload_polygon(row["footprint"])
        remaining = shape
        pieces = []
        for cap, domain, roof_ids in regions:
            material = remaining.intersection(domain)
            if material.area <= AREA_TOLERANCE_MM2:
                continue
            if cap <= row["base_z_mm"] + HEIGHT_TOLERANCE_MM:
                raise LiveRepairError("實際上方板底不高於構件基底，不能生成零高／負高實體。")
            pieces.extend((part, cap, roof_ids) for part in _parts(material))
            remaining = remaining.difference(domain)
        if remaining.area > AREA_TOLERANCE_MM2 or not pieces:
            raise LiveRepairError("固定規範要求收到當地上方板底；此修正範圍缺實際板底覆蓋或位於板孔內，不能沿用任意高度。")
        for index, (part, cap, roof_ids) in enumerate(pieces):
            fitted = deepcopy(group)
            fitted.update(outer=_polygon_payload(part)["outer"], holes=_polygon_payload(part)["holes"],
                          base_z_mm=row["base_z_mm"], height_mm=cap-row["base_z_mm"],
                          area_mm2=part.area, volume_mm3=part.area*(cap-row["base_z_mm"]))
            if len(pieces) != 1:
                fitted["id"] = row["group_id"] + "/fixed-roof:" + _digest([cap, _polygon_payload(part)])[:16]
                fitted["name"] = row["name"] + f" roof segment {index+1}"
            fitted["fixed_live_height_audit"] = {
                "standard_version": standard["version"], "basis": "actual_protected_roof_horizontal_mesh_lowest_cap",
                "cap_z_mm": cap, "roof_entity_ids": roof_ids,
                "original_plan_height_mm": row["height_mm"], "preserved_base_z_mm": row["base_z_mm"],
                "original_group_id": row["group_id"],
            }
            result.append(_create_row(fitted, row, row["parent_entity_id"]))
    return result


def build_live_repair_plan(document: dict, selection: dict | None, snapshot: dict, *,
                           action: str, height_mm: float | None = None,
                           base_z_mm: float | None = None, category: str | None = None,
                           tag: str | None = None, opening_type: str = "opening",
                           target_entity_ids: list[str] | None = None,
                           live_cad_geometry: bool = False, auto_match_position: bool = False,
                           wall_system_scope: dict | None = None) -> dict:
    """Create a hash-bound plan for one active-model repair operation."""
    if wall_system_scope is not None:
        if action!='replace_from_cad' or not live_cad_geometry or any(v is not None for v in (height_mm,base_z_mm,category,tag)) or target_entity_ids:
            raise LiveRepairError('牆系範圍只用於依實際來源定位、保留既有分類與洞口的 CAD 輪廓替換。')
        from .live_wall_system import build_wall_system_plan
        from .local_snapshot import bind_local_read
        return seal_live_repair_plan(bind_local_read(
            build_wall_system_plan(document,selection,snapshot,wall_system_scope), snapshot))
    _validate_snapshot(snapshot)
    fixed_standard = _fixed_live_standard(snapshot)
    from .selection_openings import _path_key
    intake = document.get("intake") or {}
    manifest = snapshot["root"].get("source_manifest") or {}
    if (not intake.get("source_sha256") or manifest.get("source_sha256") != intake["source_sha256"]
            or _path_key(manifest.get("source_path")) != _path_key(intake.get("source_path"))
            or manifest.get("dependencies", []) != intake.get("dependencies", [])):
        raise LiveRepairError("目前 SketchUp 白模與本次 DWG／外部參照來源不一致。")
    if action not in SUPPORTED_ACTIONS:
        raise LiveRepairError("不支援的即時修正動作。")
    if action != "remove_wrong" and selection is None:
        raise LiveRepairError("此修正動作需要 AutoCAD Pickfirst 選取快照。")
    payload, by_group_id = _live_payload(snapshot)
    entities = list(snapshot["entities"])
    entity_by_id = {str(entity["entity_id"]): entity for entity in entities}
    explicit_ids = [str(value) for value in (target_entity_ids or [])]
    selected_ids = [str(value) for value in snapshot.get("selection_entity_ids", [])]
    targets: list[dict] = []
    create: list[dict] = []
    source_trace: list[dict] = []
    live_revision = None
    position_audit = None
    if live_cad_geometry and action != 'replace_from_cad':
        raise LiveRepairError('CAD 改後輪廓目前只用於 replace_from_cad。')
    if auto_match_position and action != 'replace_from_cad':
        raise LiveRepairError('自動定位目前只用於替換牆體。')
    def source_tag(layer):
        choices = [row["tag"] for row in snapshot["root"].get("tag_mapping", []) or []
                   if row.get("source_layer") == layer and
                   (not row.get("source_id") or _path_key(row["source_id"]) == _path_key(intake.get("source_path")))]
        if len(set(choices)) > 1:
            raise LiveRepairError("來源圖層有多個 Tag 對照，不能任選。")
        if choices:
            if tag and tag != choices[0]:
                raise LiveRepairError("指定 Tag 與既有來源圖層對照不一致。")
            return choices[0]
        if layer.casefold() in {"untagged", "layer0", "0"} and tag is None:
            raise LiveRepairError("來源保留層名需明確可逆 Tag 對照。")
        return tag or layer

    if action == "cut_opening":
        try:
            proposal = propose_selection_openings(
                document, payload, selection, height_mm=height_mm,
                base_z_mm=0 if base_z_mm is None else base_z_mm,
                opening_type=opening_type,
            )
        except SelectionOpeningError as error:
            raise LiveRepairError(str(error)) from error
        if proposal["blockers"]:
            raise LiveRepairError("CAD 選取無法唯一對應開口宿主：" +
                                  json.dumps(proposal["blockers"], ensure_ascii=False))
        decisions = {
            "source_sha256": (document.get("intake") or {}).get("source_sha256"),
            "opening_patches": proposal["patches"],
        }
        updated = apply_selection_opening_patches(payload, decisions)
        if updated.get("blockers"):
            raise LiveRepairError("開口修正會留下衝突實體：" +
                                  json.dumps(updated["blockers"], ensure_ascii=False))
        original_ids = {group["id"] for group in payload["groups"]}
        final_ids = {group["id"] for group in updated["groups"]}
        removed_ids = original_ids - final_ids
        target_entities = [by_group_id[identity] for identity in sorted(removed_ids)]
        if not target_entities:
            raise LiveRepairError("開口方案沒有命中目前 SketchUp 模型中的牆體。")
        parent_ids = {entity["parent_entity_id"] for entity in target_entities}
        if len(parent_ids) != 1:
            raise LiveRepairError("開口跨越不同 SketchUp 分類容器；不能在單一 Undo 中猜測重組。")
        parent_id = next(iter(parent_ids))
        for group in updated["groups"]:
            if group["id"] in original_ids:
                continue
            lineage = set(group.get("host_lineage_ids", []))
            templates = [entity for entity in target_entities if entity["group_id"] in lineage]
            template = templates[0] if templates else target_entities[0]
            create.append(_create_row(group, template, parent_id))
        targets = [_target_row(entity) for entity in target_entities]
        source_trace = proposal["items"]

    elif action in {"add_missing", "replace_from_cad"}:
        if live_cad_geometry:
            from .live_cad_geometry import live_regions
            regions = live_regions(document,selection)
            live_revision = deepcopy(regions[0]['live_cad_revision'])
        else:
            regions = _selection_regions(document, selection, tag_policy=snapshot['root'].get('source_tag_policy','effective'))
        source_trace = [{key: value for key, value in row.items() if key != "polygon"} for row in regions]
        source_ids = {value for row in regions for value in row["source_entity_ids"]}
        source_handles = {value for row in regions for value in row["source_handles"]}
        auto_targets = [entity for entity in entities if _identifiers(entity) & (source_ids | source_handles)]
        if explicit_ids:
            missing = [identity for identity in explicit_ids if identity not in entity_by_id]
            if missing:
                raise LiveRepairError("指定的 SketchUp 目標不存在：" + ", ".join(missing))
            target_entities = [entity_by_id[identity] for identity in explicit_ids]
        elif action == 'replace_from_cad' and auto_match_position:
            from .live_cad_geometry import match_wall_by_position
            target_entities,position_audit = match_wall_by_position(regions,entities)
        elif action == "replace_from_cad" and selected_ids:
            target_entities = [entity_by_id[identity] for identity in selected_ids if identity in entity_by_id]
        else:
            target_entities = auto_targets

        if action == "add_missing":
            if target_entities or auto_targets:
                raise LiveRepairError("CAD 選取已對應到目前模型實體；不能以『漏建』重複新增。")
            if category not in SUPPORTED_CATEGORIES:
                raise LiveRepairError("補漏建需要明確指定支援的白模構件類別。")
            if height_mm is None and fixed_standard is None:
                raise LiveRepairError("補漏建需要明確高度。")
            bottom = 0 if base_z_mm is None else _number(base_z_mm, "修正基底")
            height = _number(height_mm if height_mm is not None else 1, "修正高度", positive=True)
            parent_id = _container(snapshot, category)
            for index, row in enumerate(regions):
                identity = "cadsel-live:" + _digest([
                    (document.get("intake") or {}).get("source_sha256"),
                    row["selection_handle"], row["part_index"], row["source_layer"],
                    row["source_entity_ids"], _polygon_payload(row["polygon"]), action,
                ])[:20]
                polygon = row["polygon"]
                group = {
                    "id": identity, "name": f"CAD selection {row['selection_handle']} missing {category}",
                    "category": category, "tag": source_tag(row["source_layer"]),
                    "source_layer": row["source_layer"],
                    "source_layers": row["source_layers"],
                    "source_entity_ids": row["source_entity_ids"],
                    "source_handles": row["source_handles"],
                    "outer": _polygon_payload(polygon)["outer"],
                    "holes": _polygon_payload(polygon)["holes"],
                    "base_z_mm": bottom, "height_mm": height,
                    "mode": "solid_extrusion", "area_mm2": polygon.area,
                    "volume_mm3": polygon.area * height,
                    "evidence": ["user_selected_autocad_pickfirst_object", "active_model_missing_component_repair"],
                }
                create.append(_create_row(group, None, parent_id))
        else:
            if not target_entities:
                raise LiveRepairError("找不到要由 CAD 正確輪廓替換的 SketchUp 實體；請在 SketchUp 選取錯誤構件。")
            if live_revision is not None:
                if len(target_entities)!=1:
                    raise LiveRepairError('即時 CAD 輪廓變更一次只替換一個已定位牆體。')
                proof=_provenance(target_entities[0])
                if proof.get('opening_ids') or proof.get('opening_part_role') or proof.get('semantic_role')=='lintel':
                    raise LiveRepairError('目標屬於門窗分段；不能用單一新輪廓填回洞口，需先確認完整開口修訂範圍。')
            parent_ids = {entity["parent_entity_id"] for entity in target_entities}
            if len(parent_ids) != 1:
                raise LiveRepairError("替換目標位於不同 SketchUp 分類容器。")
            template = _common_template(target_entities, category=category, tag=tag,
                                        base_z_mm=base_z_mm, height_mm=height_mm)
            combined = unary_union([row["polygon"] for row in regions])
            parts = _parts(combined)
            source_entity_ids = sorted({value for row in regions for value in row["source_entity_ids"]})
            source_handles_all = sorted({value for row in regions for value in row["source_handles"]})
            for index, polygon in enumerate(parts):
                identity = (template["id"] if len(parts) == 1 and len(target_entities) == 1 else
                            "live-replace:" + _digest([sorted(entity["group_id"] for entity in target_entities),
                                                       source_handles_all, index])[:20])
                group = {
                    **template,
                    "id": identity,
                    "name": template.get("name") or identity,
                    "source_entity_ids": source_entity_ids,
                    "source_handles": source_handles_all,
                    "outer": _polygon_payload(polygon)["outer"],
                    "holes": _polygon_payload(polygon)["holes"],
                    "area_mm2": polygon.area,
                    "volume_mm3": polygon.area * template["height_mm"],
                    "live_repair_replaced_group_ids": sorted(entity["group_id"] for entity in target_entities),
                    "evidence": [*template.get("evidence", []), "user_selected_cad_replacement_contour"],
                }
                if live_revision is not None:
                    group['cad_live_revision']=deepcopy(live_revision)
                    group['replaced_source_binding']={key:deepcopy(template.get(key))for key in
                        ('source_entity_ids','source_handles','source_layer','source_layers','outer','holes','cad_live_revision')}
                    group['source_layers']=sorted(set(template.get('source_layers',[])+[live_revision['source_layer']]))
                    group['source_layer_policy']='inherit_matched_target_primary_tag_for_geometry_only_revision'
                    group['position_match_audit']=deepcopy(position_audit)
                create.append(_create_row(group, target_entities[0], next(iter(parent_ids))))
            targets = [_target_row(entity) for entity in target_entities]

    else:  # remove_wrong
        chosen = explicit_ids or selected_ids
        if not chosen:
            raise LiveRepairError("刪除錯建需要先在 SketchUp 選取白模構件。")
        missing = [identity for identity in chosen if identity not in entity_by_id]
        if missing:
            raise LiveRepairError("選取包含非白模葉群組或不存在的目標：" + ", ".join(missing))
        targets = [_target_row(entity_by_id[identity]) for identity in chosen]

    if fixed_standard is not None:
        create = _fit_fixed_live_create(create, snapshot, fixed_standard, action, document)
    ignored = {target["entity_id"] for target in targets}
    blockers = _overlap_blockers(create, snapshot, ignored)
    if blockers:
        raise LiveRepairError("即時修正會與其他既有實體正體積重疊：" +
                              json.dumps(blockers, ensure_ascii=False))
    if not targets and not create:
        raise LiveRepairError("修正方案沒有任何變更。")
    plan = {
        "schema": PLAN_SCHEMA,
        "status": "ready_to_apply",
        "action": action,
        "modeling_standard": fixed_standard,
        "fixed_standard_audit": deepcopy(snapshot["root"].get("fixed_standard_audit")),
        "model": {
            "guid": snapshot["model"]["guid"],
            "path": snapshot["model"]["path"],
            "root_entity_id": snapshot["root"]["entity_id"],
            "root_payload_sha256": snapshot["root"].get("payload_sha256"),
            "snapshot_context": {"root": deepcopy(snapshot["root"]),
                                 "containers": deepcopy(snapshot.get("containers", [])),
                                 "entities": deepcopy(snapshot["entities"]),
                                 "protected_entities":deepcopy(snapshot.get("protected_entities",[]))},
        },
        "source": {
            "path": (document.get("intake") or {}).get("source_path") if document else None,
            "sha256": (document.get("intake") or {}).get("source_sha256") if document else None,
            "selection_sha256": _digest(selection) if selection else None,
            "manifest": deepcopy(intake),
            "items": source_trace,
        },
        "targets": targets,
        "create": create,
        "write_policy": {
            "active_model_only": True,
            "create_new_skp": False,
            "save_model": False,
            "single_undo_operation": True,
            "verify_before_commit": True,
        },
    }
    if live_revision is not None:
        plan['source']['live_cad_revision']=live_revision
        plan['source']['position_match_audit']=position_audit
    from .local_snapshot import bind_local_read
    return seal_live_repair_plan(bind_local_read(plan, snapshot))


def seal_live_repair_plan(plan: dict) -> dict:
    """Seal shared plan fields, including optional sync observation evidence."""
    body = {key: value for key, value in plan.items() if key not in {"plan_sha256", "canonical_body_json"}}
    plan["canonical_body_json"] = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    plan["plan_sha256"] = hashlib.sha256(plan["canonical_body_json"].encode("utf-8")).hexdigest()
    return plan


def validate_plan_digest(plan: dict) -> str:
    if not isinstance(plan, dict) or plan.get("schema") != PLAN_SCHEMA:
        raise LiveRepairError("即時修正計畫格式錯誤。")
    expected = str(plan.get("plan_sha256") or "")
    body = {key: value for key, value in plan.items() if key not in {"plan_sha256", "canonical_body_json"}}
    canonical_body = plan.get("canonical_body_json")
    if not isinstance(canonical_body, str) or json.loads(canonical_body) != body:
        raise LiveRepairError("即時修正計畫雜湊不符：canonical body 與實際欄位不一致。")
    actual = hashlib.sha256(canonical_body.encode("utf-8")).hexdigest()
    if expected != actual:
        raise LiveRepairError("即時修正計畫雜湊不符；不得套用已變更的計畫。")
    return actual
