from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable


Point = tuple[float, float]


@dataclass
class Segment:
    p1: Point
    p2: Point
    source_handle: str | None = None
    layer: str = ""
    source_type: str = ""
    instance_path: str = ""
    block_name: str = ""
    evidence: list[str] = field(default_factory=list)
    source_layers: list[str] = field(default_factory=list)
    source_handles: list[str] = field(default_factory=list)

    @property
    def length(self) -> float:
        return distance(self.p1, self.p2)

    @property
    def midpoint(self) -> Point:
        return ((self.p1[0] + self.p2[0]) / 2, (self.p1[1] + self.p2[1]) / 2)

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": [round(self.p1[0], 6), round(self.p1[1], 6)],
            "end": [round(self.p2[0], 6), round(self.p2[1], 6)],
            "length_mm": round(self.length, 3),
            "source_handle": self.source_handle,
            "source_layer": self.layer,
            "source_layers": list(self.source_layers or ([self.layer] if self.layer else [])),
            "source_handles": list(self.source_handles or ([self.source_handle] if self.source_handle else [])),
            "instance_path": self.instance_path or None,
            "block_name": self.block_name or None,
            "evidence": self.evidence,
        }


def distance(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def bbox(entity: dict[str, Any]) -> dict[str, Any] | None:
    return entity.get("world_extents") or entity.get("local_extents")


def bbox_dims(box: dict[str, Any] | None) -> tuple[float, float]:
    if not box:
        return 0.0, 0.0
    return abs(float(box["max"]["x"]) - float(box["min"]["x"])), abs(float(box["max"]["y"]) - float(box["min"]["y"]))


def bbox_center(box: dict[str, Any] | None) -> Point | None:
    if not box:
        return None
    return (
        (float(box["min"]["x"]) + float(box["max"]["x"])) / 2,
        (float(box["min"]["y"]) + float(box["max"]["y"])) / 2,
    )


def bbox_area(box: dict[str, Any] | None) -> float:
    width, height = bbox_dims(box)
    return width * height


def expand_bbox(box: dict[str, Any], margin: float) -> dict[str, Any]:
    return {
        "min": {"x": float(box["min"]["x"]) - margin, "y": float(box["min"]["y"]) - margin},
        "max": {"x": float(box["max"]["x"]) + margin, "y": float(box["max"]["y"]) + margin},
    }


def point_in_bbox(point: Point, box: dict[str, Any]) -> bool:
    return box["min"]["x"] <= point[0] <= box["max"]["x"] and box["min"]["y"] <= point[1] <= box["max"]["y"]


def entity_position(entity: dict[str, Any]) -> Point | None:
    position = entity.get("position_world") or entity.get("position") or entity.get("location")
    if position:
        point = float(position["x"]), float(position["y"])
        box = bbox(entity)
        if not box:
            return point
        width, height = bbox_dims(box)
        margin = max(1.0, width * 0.1, height * 0.1)
        if (
            float(box["min"]["x"]) - margin <= point[0] <= float(box["max"]["x"]) + margin
            and float(box["min"]["y"]) - margin <= point[1] <= float(box["max"]["y"]) + margin
        ):
            return point
        # Some transformed or proxy INSERTs expose an insertion point in the
        # block's source coordinate system while their extents are correctly
        # resolved in model space.  Prefer the model-space extent in that case.
        return bbox_center(box)
    return bbox_center(bbox(entity))


def _arc_points(center: Point, radius: float, start: float, sweep: float, error: float) -> list[Point]:
    """Tessellate an arc with a maximum chord sagitta in drawing millimetres."""
    if not math.isfinite(error) or error <= 0:
        raise ValueError("max_curve_error_mm must be a finite positive number")
    if not all(math.isfinite(value) for value in (*center, radius, start, sweep)) or radius <= 0:
        raise ValueError("ARC geometry must be finite and have a positive radius")
    # At most 90 degrees per chord also avoids a degenerate full-circle chord.
    step = min(math.pi / 2, 2 * math.acos(max(-1.0, 1 - min(error / radius, 2.0))))
    count = max(1, math.ceil(abs(sweep) / max(step, 1e-12)))
    if count > 100000:
        raise ValueError("Curve tessellation exceeds 100000 segments; review scale or error tolerance")
    return [(center[0] + radius * math.cos(start + sweep * index / count),
             center[1] + radius * math.sin(start + sweep * index / count)) for index in range(count + 1)]


def _bulge_points(start: Point, end: Point, bulge: float, error: float) -> list[Point]:
    if abs(bulge) <= 1e-12 or distance(start, end) <= 1e-12:
        return [start, end]
    chord = distance(start, end)
    center_offset = chord * (1 - bulge * bulge) / (4 * bulge)
    center = ((start[0] + end[0]) / 2 - (end[1] - start[1]) * center_offset / chord,
              (start[1] + end[1]) / 2 + (end[0] - start[0]) * center_offset / chord)
    radius = chord * (1 + bulge * bulge) / (4 * abs(bulge))
    points = _arc_points(center, radius, math.atan2(start[1] - center[1], start[0] - center[0]),
                         4 * math.atan(bulge), error)
    points[0], points[-1] = start, end
    return points


def entity_segments(entity: dict[str, Any], world: bool | None = None, *, max_curve_error_mm: float = 1.0) -> list[Segment]:
    if world is None:
        world = bool(entity.get("start_world") or entity.get("vertices_world") or entity.get("center_world") or entity.get("curve_points_world"))
    suffix = "_world" if world else ""
    typ = entity.get("type")
    handle = entity.get("source_handle") if world else entity.get("handle")
    path = str(entity.get("instance_path") or "").strip("/") if world else ""

    def qualify(value: str | None) -> str | None:
        value = str(value or "")
        if path and value and path != value and not path.endswith("/" + value) and not value.startswith(path + "/"):
            return path + "/" + value
        return value or None

    handle = qualify(handle)
    common = {
        "source_handle": handle,
        "layer": str(entity.get("layer") or ""),
        "source_type": str(typ or ""),
        "instance_path": str(entity.get("instance_path") or ""),
        "block_name": str(entity.get("block_name") or entity.get("source_block") or ""),
        "source_layers": list(entity.get("source_layers") or [str(entity.get("raw_layer", entity.get("layer")) or "")]),
        "source_handles": [value for item in (entity.get("source_handles") or ([handle] if handle else [])) if (value := qualify(item))],
    }
    sampled = entity.get("curve_points" + suffix)
    if sampled and typ in {"ARC", "LWPOLYLINE", "POLYLINE", "ELLIPSE", "SPLINE", "CIRCLE"}:
        points = [(float(item.get("point", item)["x"]), float(item.get("point", item)["y"])) for item in sampled]
        evidence = ["curve_tessellation", "affine_transformed_source_samples"]
        if entity.get("curve_error_mm") is not None:
            evidence.append(f"max_chord_error_mm={float(entity['curve_error_mm']):g}")
        return [Segment(a, b, evidence=list(evidence), **common) for a, b in zip(points, points[1:])]
    if typ == "LINE":
        start, end = entity.get("start" + suffix), entity.get("end" + suffix)
        if start and end:
            return [Segment((float(start["x"]), float(start["y"])), (float(end["x"]), float(end["y"])), **common)]
        return []
    if typ == "ARC":
        center = entity.get("center" + suffix)
        if not center or entity.get("radius") is None:
            return []
        if "start_angle_degrees" in entity:
            start, end = math.radians(float(entity["start_angle_degrees"])), math.radians(float(entity["end_angle_degrees"]))
        else:
            start, end = float(entity.get("start_angle", 0.0)), float(entity.get("end_angle", 0.0))
            if entity.get("angle_units") == "degrees":
                start, end = math.radians(start), math.radians(end)
        sweep = (end - start) % (2 * math.pi)
        if math.isclose(sweep, 0.0, abs_tol=1e-12) and not math.isclose(end, start, abs_tol=1e-12):
            sweep = 2 * math.pi
        points = _arc_points((float(center["x"]), float(center["y"])), float(entity["radius"]), start, sweep, max_curve_error_mm)
        return [Segment(a, b, evidence=["curve_tessellation", f"max_chord_error_mm={max_curve_error_mm:g}"], **common)
                for a, b in zip(points, points[1:])]
    if typ == "CIRCLE":
        center = entity.get("center" + suffix)
        if not center or entity.get("radius") is None:
            return []
        points = _arc_points((float(center["x"]), float(center["y"])), float(entity["radius"]),
                             0, 2*math.pi, max_curve_error_mm)
        points[-1] = points[0]
        return [Segment(a,b,evidence=["curve_tessellation",f"max_chord_error_mm={max_curve_error_mm:g}"],**common)
                for a,b in zip(points,points[1:])]
    if typ in {"LWPOLYLINE", "POLYLINE"}:
        vertices = entity.get("vertices" + suffix) or []
        points: list[Point] = []
        for item in vertices:
            point = item.get("point", item)
            points.append((float(point["x"]), float(point["y"])))
        pairs = list(zip(range(len(points) - 1), points, points[1:]))
        if entity.get("closed") and len(points) > 1:
            pairs.append((len(points) - 1, points[-1], points[0]))
        result = []
        bulges = entity.get("bulges") or []
        for index, start, end in pairs:
            bulge = float(vertices[index].get("bulge", bulges[index] if index < len(bulges) else 0.0))
            edge_points = _bulge_points(start, end, bulge, max_curve_error_mm)
            evidence = ["curve_tessellation", f"max_chord_error_mm={max_curve_error_mm:g}"] if bulge else []
            result.extend(Segment(a, b, evidence=list(evidence), **common) for a, b in zip(edge_points, edge_points[1:]))
        return result
    return []


def point_segment_distance(point: Point, segment: Segment) -> float:
    vx, vy = segment.p2[0] - segment.p1[0], segment.p2[1] - segment.p1[1]
    wx, wy = point[0] - segment.p1[0], point[1] - segment.p1[1]
    denominator = vx * vx + vy * vy
    if denominator <= 1e-12:
        return distance(point, segment.p1)
    t = max(0.0, min(1.0, (wx * vx + wy * vy) / denominator))
    return math.hypot(point[0] - (segment.p1[0] + t * vx), point[1] - (segment.p1[1] + t * vy))


def segment_angle(segment: Segment) -> float:
    angle = math.atan2(segment.p2[1] - segment.p1[1], segment.p2[0] - segment.p1[0]) % math.pi
    return angle


def angle_difference(a: float, b: float) -> float:
    value = abs(a - b) % math.pi
    return min(value, math.pi - value)


def segment_parallel_distance(a: Segment, b: Segment) -> tuple[float, float]:
    """Return perpendicular spacing and overlap ratio after assuming near parallelism."""
    length = a.length
    if length <= 1e-9 or b.length <= 1e-9:
        return float("inf"), 0.0
    ux, uy = (a.p2[0] - a.p1[0]) / length, (a.p2[1] - a.p1[1]) / length
    nx, ny = -uy, ux
    spacing = abs((b.midpoint[0] - a.midpoint[0]) * nx + (b.midpoint[1] - a.midpoint[1]) * ny)
    a0, a1 = sorted((a.p1[0] * ux + a.p1[1] * uy, a.p2[0] * ux + a.p2[1] * uy))
    b0, b1 = sorted((b.p1[0] * ux + b.p1[1] * uy, b.p2[0] * ux + b.p2[1] * uy))
    overlap = max(0.0, min(a1, b1) - max(a0, b0))
    return spacing, overlap / min(a1 - a0, b1 - b0)


def is_effectively_closed(entity: dict[str, Any], tolerance: float = 5.0) -> bool:
    if entity.get("closed"):
        return True
    vertices = entity.get("vertices") or []
    points = [item.get("point", item) for item in vertices]
    if len(points) < 4:
        return False
    return distance((float(points[0]["x"]), float(points[0]["y"])), (float(points[-1]["x"]), float(points[-1]["y"]))) <= tolerance


def orthogonality_score(entity: dict[str, Any], tolerance_degrees: float) -> float:
    segments = [segment for segment in entity_segments(entity, world=False) if segment.length > 1e-6]
    if len(segments) < 4:
        return 0.0
    tolerance = math.radians(tolerance_degrees)
    good = 0
    total = 0
    for first, second in zip(segments, segments[1:] + segments[:1]):
        delta = angle_difference(segment_angle(first), segment_angle(second))
        if abs(delta - math.pi / 2) <= tolerance:
            good += 1
        total += 1
    return good / total if total else 0.0


def merge_collinear(segments: Iterable[Segment], rules: dict[str, Any]) -> list[Segment]:
    minimum = float(rules["minimum_segment_length_mm"])
    angle_tol = math.radians(float(rules["merge_angle_degrees"]))
    offset_tol = float(rules["merge_offset_mm"])
    gap_tol = float(rules["merge_gap_mm"])
    groups: list[dict[str, Any]] = []
    curves: list[Segment] = []
    for segment in segments:
        if "curve_tessellation" in segment.evidence:
            # The chord error bound would be lost if small curved edges were
            # dropped or several chords were collapsed by a collinear merge.
            if segment.length > 1e-12:
                curves.append(segment)
            continue
        if segment.length < minimum:
            continue
        ux = (segment.p2[0] - segment.p1[0]) / segment.length
        uy = (segment.p2[1] - segment.p1[1]) / segment.length
        if ux < 0 or (abs(ux) < 1e-12 and uy < 0):
            ux, uy = -ux, -uy
        angle = math.atan2(uy, ux) % math.pi
        normal = (-uy, ux)
        offset = segment.p1[0] * normal[0] + segment.p1[1] * normal[1]
        group = next((item for item in groups if angle_difference(angle, item["angle"]) <= angle_tol and abs(offset - item["offset"]) <= offset_tol), None)
        if group is None:
            group = {"angle": angle, "u": (ux, uy), "normal": normal, "offset": offset, "intervals": []}
            groups.append(group)
        gux, guy = group["u"]
        t1 = segment.p1[0] * gux + segment.p1[1] * guy
        t2 = segment.p2[0] * gux + segment.p2[1] * guy
        group["intervals"].append({"lo": min(t1, t2), "hi": max(t1, t2), "sources": [segment]})

    result: list[Segment] = list(curves)
    for group in groups:
        intervals = sorted(group["intervals"], key=lambda item: item["lo"])
        if not intervals:
            continue
        current = intervals[0]
        for nxt in intervals[1:]:
            if nxt["lo"] <= current["hi"] + gap_tol:
                current["hi"] = max(current["hi"], nxt["hi"])
                current["sources"].extend(nxt["sources"])
            else:
                result.append(_merged_segment(group, current))
                current = nxt
        result.append(_merged_segment(group, current))
    return result


def _merged_segment(group: dict[str, Any], interval: dict[str, Any]) -> Segment:
    ux, uy = group["u"]
    nx, ny = group["normal"]
    offset = group["offset"]
    p1 = (interval["lo"] * ux + offset * nx, interval["lo"] * uy + offset * ny)
    p2 = (interval["hi"] * ux + offset * nx, interval["hi"] * uy + offset * ny)
    handles = sorted({handle for source in interval["sources"] for handle in (source.source_handles or [source.source_handle]) if handle})
    layers = sorted({layer for source in interval["sources"] for layer in (source.source_layers or [source.layer]) if layer})
    return Segment(
        p1,
        p2,
        source_handle="+".join(handles),
        layer="+".join(layers),
        source_type="MERGED",
        source_layers=layers,
        source_handles=handles,
        evidence=sorted({item for source in interval["sources"] for item in source.evidence}) + ["collinear_merge", f"source_count={len(interval['sources'])}"],
    )


def union_interval_coverage(intervals: Iterable[tuple[float, float]], full_length: float) -> float:
    if full_length <= 0:
        return 0.0
    ordered = sorted((min(a, b), max(a, b)) for a, b in intervals)
    if not ordered:
        return 0.0
    total = 0.0
    lo, hi = ordered[0]
    for next_lo, next_hi in ordered[1:]:
        if next_lo <= hi:
            hi = max(hi, next_hi)
        else:
            total += hi - lo
            lo, hi = next_lo, next_hi
    total += hi - lo
    return min(1.0, total / full_length)

