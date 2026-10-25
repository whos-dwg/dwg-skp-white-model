"""Exact exterior boundary of reviewed horizontal prisms, plus mesh probes.

No CAD semantics, tolerances that close source gaps, or model I/O live here.
The 1e-6 mm lattice is the existing native writer's serialization weld only.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import math

from shapely.geometry import GeometryCollection, LineString, Polygon
from shapely.geometry.polygon import orient
from shapely.ops import polygonize, unary_union


WELD_MM = 1e-6


def parts(geometry):
    if geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        return [geometry]
    return [piece for child in getattr(geometry, "geoms", ()) for piece in parts(child)]


def shape(row):
    value = row.get("footprint", row)
    if "parts" in value:
        return unary_union([shape(part) for part in value["parts"]])
    return Polygon(value["outer"], value.get("holes", []))


def encoded(geometry):
    rows = []
    for polygon in sorted(parts(geometry), key=lambda p: p.normalize().wkb_hex):
        polygon = orient(polygon.normalize(), sign=1)
        rows.append({"outer": [list(p) for p in list(polygon.exterior.coords)[:-1]],
                     "holes": [[list(p) for p in list(r.coords)[:-1]] for r in polygon.interiors]})
    return rows[0] if len(rows) == 1 else {"parts": rows}


def quantized(point):
    return tuple(round(float(v), 6) for v in point)


def rings(polygon):
    polygon = orient(polygon, sign=1)
    return [[tuple(p) for p in list(r.coords)[:-1]] for r in [polygon.exterior, *polygon.interiors]]


def interval_rows(cells):
    events = sorted({float(z) for row in cells for z in (row["base_z_mm"], row["base_z_mm"] + row["height_mm"])})
    result = []
    for low, high in zip(events, events[1:]):
        active = [row for row in cells if row["base_z_mm"] < high - 1e-7
                  and row["base_z_mm"] + row["height_mm"] > low + 1e-7]
        result.append({"base_z_mm": low, "top_z_mm": high,
                       "geometry": unary_union([shape(row) for row in active]),
                       "source_cell_ids": sorted(row["id"] for row in active)})
    return result


def boundaries(cells):
    """Union cells before emitting faces; internal partition faces disappear."""
    intervals = interval_rows(cells)
    if not intervals:
        return [], []
    events = [intervals[0]["base_z_mm"], *[row["top_z_mm"] for row in intervals]]
    faces = []
    for number, z in enumerate(events):
        below = intervals[number - 1]["geometry"] if number else GeometryCollection()
        above = intervals[number]["geometry"] if number < len(intervals) else GeometryCollection()
        for material, normal in ((below.difference(above), (0, 0, 1)),
                                 (above.difference(below), (0, 0, -1))):
            for polygon in parts(material):
                loops = rings(polygon.simplify(0, preserve_topology=True))
                if normal[2] < 0:
                    loops = [list(reversed(ring)) for ring in loops]
                loops = [[quantized((x, y, z)) for x, y in ring] for ring in loops]
                faces.append({"outer": loops[0], "holes": loops[1:], "normal": list(normal),
                              "plane_kind": "horizontal", "area_mm2": polygon.area})
    planes = {}
    for interval in intervals:
        low, high = interval["base_z_mm"], interval["top_z_mm"]
        for polygon in parts(interval["geometry"]):
            for ring in rings(polygon):
                for first, second in zip(ring, ring[1:] + ring[:1]):
                    # Horizontal faces already serialize these exact endpoints.
                    # Use the SAME endpoints for both the plane key and its
                    # coordinate frame. Mixing a raw origin with a rounded
                    # direction reprojects long oblique edges onto a different
                    # lattice vertex and leaves tiny, non-manifold seams.
                    first, second = quantized(first), quantized(second)
                    a, b = [tuple(round(v / WELD_MM) for v in p) for p in (first, second)]
                    dx, dy = b[0] - a[0], b[1] - a[1]
                    divisor = math.gcd(abs(dx), abs(dy))
                    if not divisor:
                        continue
                    dx, dy = dx // divisor, dy // divisor
                    sign = 1
                    if dx < 0 or (dx == 0 and dy < 0):
                        dx, dy, sign = -dx, -dy, -1
                    key = (dx, dy, dx * a[1] - dy * a[0], sign)
                    if key not in planes:
                        length = math.hypot(dx, dy)
                        planes[key] = {"origin": first, "direction": (dx / length, dy / length), "regions": []}
                    plane = planes[key]
                    ux, uy = plane["direction"]
                    ox, oy = plane["origin"]
                    ta = (first[0] - ox) * ux + (first[1] - oy) * uy
                    tb = (second[0] - ox) * ux + (second[1] - oy) * uy
                    plane["regions"].append(Polygon([(ta, low), (tb, low), (tb, high), (ta, high)]))
    for key, plane in sorted(planes.items()):
        ux, uy = plane["direction"]
        ox, oy = plane["origin"]
        sign = key[-1]
        for polygon in parts(unary_union(plane["regions"])):
            loops = rings(polygon.simplify(0, preserve_topology=True))
            if sign < 0:
                loops = [list(reversed(ring)) for ring in loops]
            loops = [[quantized((ox + t * ux, oy + t * uy, z)) for t, z in ring] for ring in loops]
            faces.append({"outer": loops[0], "holes": loops[1:], "normal": [sign * uy, -sign * ux, 0],
                          "plane_kind": "vertical", "area_mm2": polygon.area})
    # A common vertex set prevents T-junctions at merged horizontal faces.
    vertices = sorted({quantized(p) for face in faces for ring in [face["outer"], *face["holes"]] for p in ring})
    for face in faces:
        loops = []
        for ring in [face["outer"], *face["holes"]]:
            rebuilt = []
            for a, b in zip(ring, ring[1:] + ring[:1]):
                delta = [b[i] - a[i] for i in range(3)]
                squared = sum(v * v for v in delta)
                if squared < 1e-16:
                    continue
                incident = []
                for point in vertices:
                    fraction = sum((point[i] - a[i]) * delta[i] for i in range(3)) / squared
                    if -1e-10 <= fraction < 1 - 1e-10:
                        distance = math.sqrt(sum((point[i] - a[i] - fraction * delta[i]) ** 2 for i in range(3)))
                        if distance <= WELD_MM * 2:
                            incident.append((fraction, point))
                rebuilt.extend(p for _, p in sorted(incident))
            loops.append(rebuilt)
        face.update(outer=[list(p) for p in loops[0]],
                    holes=[[list(p) for p in ring] for ring in loops[1:]])
    return intervals, faces


def topology(faces):
    uses = defaultdict(list)
    for number, face in enumerate(faces):
        for ring in [face["outer"], *face.get("holes", [])]:
            for a, b in zip(ring, ring[1:] + ring[:1]):
                uses[tuple(sorted((quantized(a), quantized(b))))].append((number, quantized(a), quantized(b)))
    bad = [edge for edge, rows in uses.items() if len(rows) != 2 or rows[0][1:] != tuple(reversed(rows[1][1:]))]
    adjacency = defaultdict(set)
    for rows in uses.values():
        for number, *_ in rows:
            adjacency[number].update(other for other, *_ in rows if number != other)
    seen, shells = set(), []
    for number in range(len(faces)):
        if number in seen:
            continue
        pending, shell = [number], []
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            shell.append(current)
            pending.extend(adjacency[current] - seen)
        shells.append(shell)
    return {"shell_count": len(shells), "all_closed": bool(faces) and not bad,
            "bad_edges": bad, "edge_incidence_counts": dict(Counter(len(rows) for rows in uses.values())),
            "shell_face_counts": [len(row) for row in shells]}


def point_in_mesh(point, triangles):
    """Independent parity ray test; no prepared cells or surface recipe used."""
    direction = (1.0, .3713906763541037, .219568029428196)
    hits = []
    cross = lambda a, b: (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])
    dot = lambda a, b: sum(x*y for x, y in zip(a, b))
    for triangle in triangles:
        a, b, c = triangle
        edge1 = tuple(b[i]-a[i] for i in range(3))
        edge2 = tuple(c[i]-a[i] for i in range(3))
        h = cross(direction, edge2)
        determinant = dot(edge1, h)
        if abs(determinant) < 1e-12:
            continue
        offset = tuple(point[i]-a[i] for i in range(3))
        u = dot(offset, h)/determinant
        q = cross(offset, edge1)
        v = dot(direction, q)/determinant
        distance = dot(edge2, q)/determinant
        if u >= -1e-10 and v >= -1e-10 and u+v <= 1+1e-10 and distance > 1e-8:
            if not any(abs(distance-prior) <= 1e-7 for prior in hits):
                hits.append(distance)
    return len(hits) % 2 == 1


def section_from_triangles(triangles, z, *, precision=1e-5):
    """Read a non-event horizontal section from actual saved triangles."""
    segments = []
    for triangle in triangles:
        crossings = []
        for a, b in zip(triangle, triangle[1:] + triangle[:1]):
            if (a[2] < z < b[2]) or (b[2] < z < a[2]):
                fraction = (z-a[2])/(b[2]-a[2])
                crossings.append((a[0]+fraction*(b[0]-a[0]), a[1]+fraction*(b[1]-a[1])))
        if len(crossings) == 2 and math.dist(*crossings) > 1e-9:
            segments.append(LineString(crossings))
    if not segments:
        return GeometryCollection()
    from shapely import set_precision
    network = unary_union(set_precision(unary_union(segments), precision))
    selected = []
    for face in polygonize(network):
        p = face.representative_point()
        if point_in_mesh((p.x, p.y, z), triangles):
            selected.append(face)
    return unary_union(selected)
