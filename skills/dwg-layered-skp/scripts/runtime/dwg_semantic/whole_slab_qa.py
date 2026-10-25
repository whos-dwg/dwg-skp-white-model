"""Saved-surface verification for whole RC slabs (not a planner rerun)."""
from __future__ import annotations

import math

from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

from .mesh_union import encoded, parts, section_from_triangles, shape, topology


def nominal_step_probes(recipe, sections):
    """Cover every source subdivision, return, opening edge and Z interval.

    Probe stations partition the full source edge at projected material and
    hole vertices. Within each linear interval, endpoint limits and midpoint
    are checked. Rays measure across a riser, never along a slab's length.
    """
    if recipe.get("mode") != "whole_uniform_thickness":
        return []
    sources = recipe["gross_source_prisms"]
    width = recipe["nominal_thickness_mm"]
    rows = []
    for number, first in enumerate(sources):
        for second in sources[number+1:]:
            lower, upper = sorted((first, second), key=lambda row: row["base_z_mm"])
            lo, hi = lower["base_z_mm"]+width, upper["base_z_mm"]
            if hi-lo <= .01:
                continue
            shared = shape(first).boundary.intersection(shape(second).boundary)
            lines = [shared] if shared.geom_type == "LineString" else [p for p in getattr(shared, "geoms", ()) if p.geom_type == "LineString"]
            for line in lines:
                points = list(line.coords)
                for a, b in zip(points, points[1:]):
                    length = math.dist(a, b)
                    if length < .01:
                        continue
                    tangent = ((b[0]-a[0])/length, (b[1]-a[1])/length)
                    normal = (-tangent[1], tangent[0])
                    stations = {0.0, length}
                    relevant = [row for row in sections if row["base_z_mm"] < hi-.01 and row["top_z_mm"] > lo+.01]
                    for section in relevant:
                        for polygon in parts(shape(section["footprint"])):
                            for ring in [polygon.exterior, *polygon.interiors]:
                                for p in ring.coords:
                                    along = (p[0]-a[0])*tangent[0]+(p[1]-a[1])*tangent[1]
                                    offset = abs((p[0]-a[0])*normal[0]+(p[1]-a[1])*normal[1])
                                    if 0 < along < length and offset <= width*2.01:
                                        stations.add(along)
                    stations = sorted(stations)
                    for start, end in zip(stations, stations[1:]):
                        if end-start <= .01:
                            continue
                        epsilon = min(.005, (end-start)/1000)
                        for station in (start+epsilon, (start+end)/2, end-epsilon):
                            origin = (a[0]+station*tangent[0], a[1]+station*tangent[1])
                            ray = LineString([(origin[0]-normal[0]*width*2, origin[1]-normal[1]*width*2),
                                              (origin[0]+normal[0]*width*2, origin[1]+normal[1]*width*2)])
                            for section in relevant:
                                z0, z1 = max(lo, section["base_z_mm"]), min(hi, section["top_z_mm"])
                                if z1-z0 <= .01:
                                    continue
                                hit = shape(section["footprint"]).intersection(ray)
                                segments = [hit] if hit.geom_type == "LineString" else [p for p in getattr(hit, "geoms", ()) if p.geom_type == "LineString"]
                                lengths = []
                                for segment in segments:
                                    values = [(p[0]-origin[0])*normal[0]+(p[1]-origin[1])*normal[1] for p in segment.coords]
                                    if min(values) <= .01 and max(values) >= -.01:
                                        lengths.append(max(values)-min(values))
                                # Zero material can be a confirmed through-hole.
                                if not lengths:
                                    continue
                                actual = max(lengths)
                                rows.append({"source_component_ids": [lower["id"], upper["id"]],
                                    "source_edge_mm": [list(a), list(b)], "source_station_interval_mm": [start, end],
                                    "station_mm": station, "z_interval_mm": [z0, z1],
                                    "normal_origin_xy_mm": list(origin), "normal_xy": list(normal),
                                    "actual_transverse_material_mm": actual, "nominal_thickness_mm": width,
                                    "pass": actual >= width-.02})
    return rows


def horizontal_normal_checks(recipe, sections):
    """Check all XY cells of the horizontal skin, excluding vertical risers.

    Every source contour and actual Z-section boundary participates in the
    planar difference. This is a continuous area proof, not a few ray samples.
    The ray direction is the horizontal surface normal (global Z).
    """
    if recipe.get('mode')!='whole_uniform_thickness':
        return []
    sources=recipe['gross_source_prisms'];width=recipe['nominal_thickness_mm']
    cutter=unary_union([shape(row)for row in recipe['source_openings']])
    bars=[]
    for i,first in enumerate(sources):
        for second in sources[i+1:]:
            if abs(first['base_z_mm']-second['base_z_mm'])<.001:
                continue
            shared=shape(first).boundary.intersection(shape(second).boundary)
            if shared.length>.001:
                bars.append(shared.buffer(width,cap_style='flat',join_style='mitre'))
    riser_domain=unary_union(bars)
    rows=[]
    for source in sources:
        domain=shape(source).difference(cutter).difference(riser_domain)
        for section in sections:
            z=(section['base_z_mm']+section['top_z_mm'])/2
            active=source['base_z_mm']<z<source['base_z_mm']+width
            actual=shape(section['footprint']).intersection(domain)
            expected=domain if active else Polygon()
            missing=expected.difference(actual.buffer(.001)).area
            extra=actual.difference(expected.buffer(.001)).area
            rows.append({'source_component_id':source['id'],'normal_xyz':[0,0,1],
                'source_bottom_z_mm':source['base_z_mm'],'source_top_z_mm':source['base_z_mm']+width,
                'normal_thickness_mm':width,'continuously_checked_area_mm2':domain.area,
                'actual_z_interval_mm':[section['base_z_mm'],section['top_z_mm']],
                'missing_area_mm2':missing,'extra_area_mm2':extra,'pass':missing+extra<=.1})
    return rows


def verify_saved_whole(group, triangles, actual_faces=None):
    """Compare all actual Z intervals against reviewed material and source holes."""
    checks = []
    def check(name, passed, detail=None):
        checks.append({"check": name, "pass": bool(passed), "detail": detail})
    cells = group["construction_cells"]
    events = sorted({float(p[2]) for triangle in triangles for p in triangle}
                    | {float(z) for cell in cells for z in (cell["base_z_mm"], cell["base_z_mm"]+cell["height_mm"])})
    # C API coordinates may differ from source by floating point roundoff.
    clean = []
    for value in events:
        if not clean or value-clean[-1] > .001:
            clean.append(value)
    actual_cells, sections, virtual = [], [], []
    for low, high in zip(clean, clean[1:]):
        if high-low <= .001:
            continue
        z = (low+high)/2
        actual = section_from_triangles(triangles, z)
        active = [cell for cell in cells if cell["base_z_mm"] < z < cell["base_z_mm"]+cell["height_mm"]]
        expected = unary_union([shape(cell) for cell in active])
        missing = expected.difference(actual.buffer(.001)).area
        extra = actual.difference(expected.buffer(.001)).area
        check("saved whole slab material and holes match source at every Z event interval",
              missing+extra <= .1, {"id": group["id"], "z_interval_mm": [low, high],
                                   "missing_area_mm2": missing, "extra_area_mm2": extra})
        sections.append({"base_z_mm": low, "top_z_mm": high, "footprint": encoded(actual)})
        for polygon in parts(actual):
            actual_cells.append({**group, "mode": "solid_extrusion", **encoded(polygon),
                                 "id": group["id"]+":actual-cell:"+str(len(actual_cells)),
                                 "base_z_mm": low, "height_mm": high-low, "top_z_mm": high})
    for cell in cells:
        expected = shape(cell)
        portions, volume = [], 0.0
        for row in sections:
            lo = max(cell["base_z_mm"], row["base_z_mm"])
            hi = min(cell["base_z_mm"]+cell["height_mm"], row["top_z_mm"])
            if hi-lo > .001:
                actual = shape(row["footprint"]).intersection(expected)
                portions.append(actual)
                volume += actual.area*(hi-lo)
        virtual.append({"id": cell["id"], "category": "slab", "tag": cell["tag"],
                        "footprint": unary_union(portions), "base_z_mm": cell["base_z_mm"],
                        "top_z_mm": cell["base_z_mm"]+cell["height_mm"], "volume_mm3": volume,
                        "zone_attribution_only": True, "physical_whole_component_id": group["id"]})
    if actual_faces is not None:
        closed = topology(actual_faces)
        check("saved whole slab is one connected closed shell", closed["all_closed"] and closed["shell_count"] == 1, closed)
        axis_valid=True
        for face in actual_faces:
            ring=face['outer']
            if max(p[2]for p in ring)-min(p[2]for p in ring)<=.001:
                continue
            points=list({(round(p[0],6),round(p[1],6))for p in ring})
            if len(points)<2:
                axis_valid=False;break
            a,b=max(((a,b)for a in points for b in points),key=lambda ab:math.dist(*ab))
            length=math.dist(a,b)
            if any(abs((b[0]-a[0])*(p[1]-a[1])-(b[1]-a[1])*(p[0]-a[0]))/length>.001 for p in points):
                axis_valid=False;break
        check('saved whole slab has only source-supported horizontal and vertical material faces',axis_valid)
    recipe = group["evidence"]["construction_recipe"]
    probes = nominal_step_probes(recipe, sections)
    check("saved whole slab has no thin riser body at source subdivisions returns and hole edges",
          all(row["pass"] for row in probes), {"id": group["id"], "probe_count": len(probes),
                                               "failures": [row for row in probes if not row["pass"]]})
    horizontal=horizontal_normal_checks(recipe,sections)
    check('saved whole slab horizontal normal thickness is continuous over all non-riser material cells',
          all(row['pass']for row in horizontal),{'id':group['id'],'interval_cell_checks':len(horizontal),
          'failures':[row for row in horizontal if not row['pass']]})
    return {"checks": checks, "actual_sections": sections, "actual_cells": actual_cells,
            "virtual_source_rows": virtual, "normal_thickness_probes": probes,
            "horizontal_normal_thickness_checks":horizontal,
            "all_pass": all(row["pass"] for row in checks)}
