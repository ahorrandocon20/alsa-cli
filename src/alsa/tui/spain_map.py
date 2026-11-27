"""Mapa de píxeles de España con el autobús, su ruta y sus paradas, para la vista «mapa» del seguimiento.

El mapa sale de `map_es.json` (Natural Earth, dominio público; se regenera con
`scripts/build_map.py`) y se dibuja al tamaño que haya: cuanto más grande la ventana, más
píxeles. Cada comunidad lleva su color, apagado para que la ruta y el autobús sean lo más
vivo; las fronteras entre comunidades van en negro y las de provincia, más suaves.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import cache, lru_cache
from itertools import pairwise
from pathlib import Path

from rich.text import Text

from alsa.models import BusTracking
from alsa.tui.art import render

MAP_FILE = Path(__file__).with_name("map_es.json")
SPAIN = (35.8, 43.9, -9.5, 4.4)  # sur, norte, oeste, este: península y Baleares
MIN_ZOOM_SPAN = 0.6  # grados: ni la ruta más corta se amplía más que esto

REGION_COLORS = {  # colores vivos; las comunidades vecinas nunca repiten tono
    "Galicia": "#2fae72",
    "Asturias": "#9bd13f",
    "Cantabria": "#1f9c9c",
    "País Vasco": "#d9485f",
    "Foral de Navarra": "#f08a3c",
    "La Rioja": "#9a5fd0",
    "Aragón": "#3f6fd8",
    "Cataluña": "#e8c13c",
    "Castilla y León": "#e8a93c",
    "Madrid": "#e04a4a",
    "Castilla-La Mancha": "#7cc05a",
    "Extremadura": "#b46ad8",
    "Valenciana": "#f06e9a",
    "Murcia": "#f0a35a",
    "Andalucía": "#34b8a8",
    "Islas Baleares": "#f06e9a",
    "Ceuta": "#e04a4a",
    "Melilla": "#e04a4a",
}
SEA = "#0c2a52"
SHALLOW = "#1d5a96"  # agua junto a la costa
WAVE = "#2f6fb0"
ABROAD = "#4a4a55"  # Portugal, Francia, Marruecos…
BORDER = "#10131c"  # frontera de comunidad o de país
CASING = "#05070c"  # borde de la ruta, de las paradas y del autobús
DONE = "#40c8ec"  # ruta ya recorrida
TODO = "#ffd166"  # ruta que te queda
OTHER = "#9aa3b5"  # recorrido del autobús fuera de tu viaje
PIN = "#ff8a1f"  # tu salida y tu llegada
HALO = "#b6eafa"
BACKGROUND = (11, 18, 32)  # el fondo del tema, para apagar los colores

# Autocar de frente con contorno negro y halo; "y" es el halo, que parpadea.
BUS_SPRITE = [
    ".yyyyy.",
    "ykWWWky",
    "ykGWGky",
    "ykBBBky",
    ".kKkKk.",
]
BUS_COLORS = {"k": CASING, "W": "#f4f7fb", "G": "#16305c", "B": "#1d6fe0", "K": "#2e333d"}
PIN_COLORS = {"k": CASING, "o": PIN, "W": "#ffffff"}
STOP = "#e6ecf5"
STOP_SPRITE = ["kkk", "ksk", "kkk"]
STOP_COLORS = {"k": CASING, "s": STOP}
PIN_SPRITE = ["..k..", ".kok.", "koWok", ".kok.", "..k.."]


# El color identifica una marca que sigue visible después de superponer el autocar y las paradas.
MAP_SYMBOLS = (
    (BUS_COLORS["B"], BUS_SPRITE, {**BUS_COLORS, "y": HALO}, "Autobús"),
    (PIN, PIN_SPRITE, PIN_COLORS, "Salida/llegada"),
    (STOP, STOP_SPRITE, STOP_COLORS, "Parada"),
    (DONE, ["kkkkkk", "ssssss"], {"k": CASING, "s": DONE}, "Hecho"),
    (TODO, ["kkkkkk", "ssssss"], {"k": CASING, "s": TODO}, "Queda"),
    (OTHER, ["ss.ss."], {"s": OTHER}, "Fuera de tu viaje"),
)


@dataclass(frozen=True)
class Area:
    """Trozo del mapa que se dibuja: esquina noroeste y tamaño en grados."""

    north: float
    west: float
    lat_span: float
    lon_span: float

    def to_px(self, lat: float, lon: float, width: int, height: int) -> tuple[float, float]:
        return (lon - self.west) / self.lon_span * width, (self.north - lat) / self.lat_span * height


@cache
def _polygons() -> list[tuple[str, str, list[tuple[float, float]]]]:
    """(comunidad o país, provincia, [(lon, lat)…]) de cada polígono del mapa."""
    data = json.loads(MAP_FILE.read_text(encoding="utf-8"))
    scale = data["scale"]
    polygons = []
    for region, province, flat in data["polygons"]:
        x, y = flat[0], flat[1]
        points = [(x / scale, y / scale)]
        for dx, dy in zip(flat[2::2], flat[3::2], strict=True):
            x, y = x + dx, y + dy
            points.append((x / scale, y / scale))
        polygons.append((data["regions"][region], province, points))
    return polygons


def area_for(points: list[tuple[float, float]], width: int, height: int, pad: float = 0.12) -> Area:
    """Encuadre que contiene `points` (lat, lon) con margen y píxeles cuadrados."""
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    lat0, lat1, lon0, lon1 = min(lats), max(lats), min(lons), max(lons)
    k = math.cos(math.radians((lat0 + lat1) / 2))
    lat_span = max(lat1 - lat0, MIN_ZOOM_SPAN) * (1 + 2 * pad)
    lon_span = max(lon1 - lon0, MIN_ZOOM_SPAN / k) * (1 + 2 * pad)
    if lon_span * k / width > lat_span / height:
        lat_span = lon_span * k / width * height
    else:
        lon_span = lat_span / height * width / k
    return Area((lat0 + lat1) / 2 + lat_span / 2, (lon0 + lon1) / 2 - lon_span / 2, lat_span, lon_span)


def area_of(tracking: BusTracking | None, zoom: bool, width: int, height: int) -> Area:
    """España entera o, con `zoom`, tu viaje y el autobús; siempre con el autobús dentro."""
    south, north, west, east = SPAIN
    points = [(south, west), (north, east)]
    vehicle = tracking.vehicle if tracking else None
    if vehicle is not None:
        bus = [(vehicle.latitude, vehicle.longitude)] if vehicle.latitude is not None and vehicle.longitude is not None else []
        first = tracking.origin_stop or 0
        last = tracking.destination_stop if tracking.destination_stop is not None else len(vehicle.stops) - 1
        trip = [(s.latitude, s.longitude) for s in vehicle.stops[first : last + 1] if s.latitude is not None and s.longitude is not None]
        if zoom and trip:
            points = trip + bus
        else:
            points += bus  # un autobús en Francia o Portugal ensancha el mapa
    return area_for(points, width, height, pad=0.12 if zoom else 0.02)  # type: ignore[arg-type]


def _fill(width: int, height: int, area: Area) -> list[list[int]]:
    """Índice del polígono de cada píxel (-1 = mar), rellenando por filas."""
    grid = [[-1] * width for _ in range(height)]
    for index, (_, _, ring) in enumerate(_polygons()):
        pts = [area.to_px(lat, lon, width, height) for lon, lat in ring]
        ys = [p[1] for p in pts]
        if max(ys) < 0 or min(ys) > height:
            xs = [p[0] for p in pts]
            if max(xs) < 0 or min(xs) > width:
                continue
        edges = list(zip(pts, pts[1:] + pts[:1], strict=True))
        for py in range(max(0, int(min(ys))), min(height, int(max(ys)) + 1)):
            yc = py + 0.5
            crossings = sorted(xa + (yc - ya) * (xb - xa) / (yb - ya) for (xa, ya), (xb, yb) in edges if (ya > yc) != (yb > yc))
            for a, b in zip(crossings[::2], crossings[1::2], strict=False):
                for px in range(max(0, math.ceil(a - 0.5)), min(width, math.floor(b - 0.5) + 1)):
                    grid[py][px] = index
    return grid


def _mix(color: str, factor: float) -> str:
    """Acerca `color` al fondo: 1 lo deja igual, 0 lo funde con el fondo."""
    rgb = (int(color[i : i + 2], 16) for i in (1, 3, 5))
    return "#" + "".join(f"{int(c * factor + b * (1 - factor)):02x}" for c, b in zip(rgb, BACKGROUND, strict=True))


@lru_cache(maxsize=8)
def _base(width: int, height: int, area: Area) -> tuple[tuple[str, ...], ...]:
    """Colores del mapa sin la ruta: mar, costa, comunidades, provincias y fronteras.

    Se guarda en caché: el parpadeo del autobús repinta sin volver a rellenar el mapa.
    """
    grid = _fill(width, height, area)
    polygons = _polygons()
    colors = [[SEA] * width for _ in range(height)]

    def at(x: int, y: int) -> int:
        return grid[y][x] if 0 <= x < width and 0 <= y < height else -1

    for y in range(height):
        for x in range(width):
            index = grid[y][x]
            around = (at(x + 1, y), at(x, y + 1), at(x - 1, y), at(x, y - 1))
            if index < 0:
                if any(n >= 0 for n in around):
                    colors[y][x] = SHALLOW
                elif (x * 7 + y * 13) % 29 == 0 and y % 2 == 0:
                    colors[y][x] = WAVE
                continue
            region, province, _ = polygons[index]
            base = REGION_COLORS.get(region)
            if base is None:  # otro país
                touches_spain = any(n >= 0 and polygons[n][0] in REGION_COLORS for n in around)
                colors[y][x] = BORDER if touches_spain else ABROAD
                continue
            right_below = [n for n in around[:2] if n >= 0]
            if any(polygons[n][0] != region for n in right_below):
                colors[y][x] = BORDER
            elif any(polygons[n][1] != province for n in right_below):
                colors[y][x] = _mix(base, 0.45)
            else:
                colors[y][x] = _mix(base, 0.58)
    return tuple(tuple(row) for row in colors)


def _line(a: tuple[float, float], b: tuple[float, float]) -> list[tuple[int, int]]:
    """Píxeles de a a b sin saltos en diagonal, para que la línea se vea continua."""
    x0, y0, x1, y1 = round(a[0]), round(a[1]), round(b[0]), round(b[1])
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
    err, out = dx + dy, []
    while True:
        out.append((x0, y0))
        if (x0, y0) == (x1, y1):
            return out
        e2 = 2 * err
        if dy <= e2 <= dx:
            out.append((x0 + sx, y0))
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


def render_map(width: int, height: int, tracking: BusTracking | None = None, zoom: bool = True, blink: bool = False) -> Text:
    """Mapa de `width` × `height` píxeles (`height` par: dos píxeles por línea) con la ruta y el autobús."""
    return render_pixels(map_pixels(width, height, tracking, zoom, blink))


def render_pixels(colors: list[list[str]]) -> Text:
    return render(["#" * len(colors[0]) for _ in colors], lambda x, y, _: colors[y][x])


def map_pixels(width: int, height: int, tracking: BusTracking | None = None, zoom: bool = True, blink: bool = False) -> list[list[str]]:
    """Color de cada píxel del mapa, con la ruta, las paradas y el autobús encima."""
    width, height = max(width, 12), max(height - height % 2, 8)
    area = area_of(tracking, zoom, width, height)
    colors = [list(row) for row in _base(width, height, area)]

    def plot(x: float, y: float, color: str) -> None:
        x, y = round(x), round(y)
        if 0 <= x < width and 0 <= y < height:
            colors[y][x] = color

    def sprite(rows: list[str], cx: float, cy: float, palette: dict[str, str]) -> None:
        left, top = round(cx) - len(rows[0]) // 2, round(cy) - len(rows) // 2
        for dy, row in enumerate(rows):
            for dx, ch in enumerate(row):
                if ch in palette:
                    plot(left + dx, top + dy, palette[ch])

    vehicle = tracking.vehicle if tracking else None
    if vehicle is not None:
        stops = vehicle.stops
        origin = tracking.origin_stop or 0
        destination = tracking.destination_stop if tracking.destination_stop is not None else len(stops) - 1
        position = tracking.position if tracking.located and tracking.position is not None else -1
        located = [(i, area.to_px(s.latitude, s.longitude, width, height)) for i, s in enumerate(stops) if s.latitude is not None and s.longitude is not None]
        segments = []
        for (i, a), (j, b) in pairwise(located):
            mine = origin <= i and j <= destination
            pixels = _line(a, b)
            if mine and i < position < j:
                split = round((position - i) / (j - i) * (len(pixels) - 1))
                segments.append((pixels[:split], "done"))
                segments.append((pixels[split:], "todo"))
            else:
                segments.append((pixels, "other" if not mine else ("done" if j <= position else "todo")))
        for pixels, style in segments:  # el resto del recorrido, en gris discontinuo
            if style == "other":
                for k, (x, y) in enumerate(pixels):
                    if k % 3 != 2:
                        plot(x, y, OTHER)
        for pixels, style in segments:  # el borde negro de tu viaje…
            if style != "other":
                for x, y in pixels:
                    for dx in (-1, 0, 1):
                        for dy in (-1, 0, 1):
                            plot(x + dx, y + dy, CASING)
        for pixels, style in segments:  # …y la línea encima
            if style != "other":
                for x, y in pixels:
                    plot(x, y, DONE if style == "done" else TODO)
        pins = {origin: None, destination: None}
        drawn: list[tuple[float, float]] = [p for i, p in located if i in pins]
        for i, (x, y) in located:  # paradas de tu viaje, como en un plano de metro; las muy juntas, una sola
            if origin < i < destination and all(abs(x - u) >= 3 or abs(y - v) >= 3 for u, v in drawn):
                sprite(STOP_SPRITE, x, y, STOP_COLORS)
                drawn.append((x, y))
        for i, point in located:
            if i in pins:
                sprite(PIN_SPRITE, *point, PIN_COLORS)
        if vehicle.latitude is not None and vehicle.longitude is not None:
            bx, by = area.to_px(vehicle.latitude, vehicle.longitude, width, height)
            sprite(BUS_SPRITE, bx, by, {**BUS_COLORS, "y": CASING if blink else HALO})
    return colors
