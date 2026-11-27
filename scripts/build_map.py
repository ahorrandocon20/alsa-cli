"""Genera src/alsa/tui/map_es.json, el mapa de la TUI, a partir de Natural Earth (dominio público).

Uso:
    curl -LO https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_admin_1_states_provinces.geojson
    python scripts/build_map.py ne_10m_admin_1_states_provinces.geojson

Guarda las provincias de la península, Baleares, Ceuta y Melilla (con su comunidad) y los
países vecinos, simplificados a ~1 km y con coordenadas enteras en milésimas de grado,
cada punto como diferencia con el anterior para que el archivo pese poco.
"""

from __future__ import annotations

import json
import sys
from itertools import pairwise
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "src" / "alsa" / "tui" / "map_es.json"
NEIGHBOURS = {"Portugal": "PT", "France": "FR", "Andorra": "AD", "Morocco": "MA", "Gibraltar": "GI", "Algeria": "DZ"}
BOUNDS = (-10.5, 6.0, 34.5, 45.5)  # oeste, este, sur, norte: sin Canarias, donde Alsa no tiene líneas
TOLERANCE = 0.01  # grados, ~1 km


def simplify(points: list[tuple[int, int]], tolerance: float) -> list[tuple[int, int]]:
    """Douglas-Peucker sobre una línea abierta."""
    if len(points) < 3:
        return points
    (x0, y0), (x1, y1) = points[0], points[-1]
    dx, dy = x1 - x0, y1 - y0
    norm = (dx * dx + dy * dy) ** 0.5 or 1e-12
    index, dmax = 0, -1.0
    for i in range(1, len(points) - 1):
        d = abs(dy * points[i][0] - dx * points[i][1] + x1 * y0 - y1 * x0) / norm
        if d > dmax:
            index, dmax = i, d
    if dmax <= tolerance:
        return [points[0], points[-1]]
    return simplify(points[: index + 1], tolerance)[:-1] + simplify(points[index:], tolerance)


def main(source: str) -> None:
    west, east, south, north = BOUNDS
    regions: list[str] = []
    polygons = []
    for feature in json.loads(Path(source).read_text())["features"]:
        props = feature["properties"]
        if props["admin"] == "Spain":
            if props["region"] == "Canary Is.":
                continue
            region, province = props["region"], props["name"]
        elif props["admin"] in NEIGHBOURS:
            region, province = NEIGHBOURS[props["admin"]], ""
        else:
            continue
        if region not in regions:
            regions.append(region)
        geometry = feature["geometry"]
        rings = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
        for polygon in rings:
            ring = polygon[0]
            xs, ys = [p[0] for p in ring], [p[1] for p in ring]
            if max(xs) < west or min(xs) > east or max(ys) < south or min(ys) > north:
                continue
            points = [(round(x * 1000), round(y * 1000)) for x, y in ring]
            far = max(range(len(points)), key=lambda i: (points[i][0] - points[0][0]) ** 2 + (points[i][1] - points[0][1]) ** 2)
            tol = TOLERANCE * 1000
            points = simplify(points[: far + 1], tol)[:-1] + simplify(points[far:], tol)  # anillo cerrado: en dos mitades
            if len(points) < 4:
                continue
            flat = [points[0][0], points[0][1]]
            for (ax, ay), (bx, by) in pairwise(points):
                flat += [bx - ax, by - ay]
            polygons.append([regions.index(region), province, flat])
    data = {
        "source": "Natural Earth 1:10m admin-1 states and provinces (public domain), simplified to ~1 km",
        "scale": 1000,
        "regions": regions,
        "polygons": polygons,
    }
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    print(f"{OUT.name}: {len(polygons)} polígonos, {OUT.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main(sys.argv[1])
