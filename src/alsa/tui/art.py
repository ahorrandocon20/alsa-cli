"""Arte de píxeles de la TUI: el rótulo ALSA y el autocar, pintados con medios bloques."""

from __future__ import annotations

from collections.abc import Callable

from rich.style import Style
from rich.text import Text

# Cada carácter es un píxel; "." es transparente. Dos filas de píxeles = una línea de texto.
Bitmap = list[str]
ColorAt = Callable[[int, int, str], str | None]

PALETTE = {
    "W": "#f4f7fb",  # carrocería
    "w": "#c9d3e3",  # sombra de la carrocería
    "G": "#16305c",  # cristales
    "g": "#2a4d86",  # reflejo del cristal
    "B": "#1d6fe0",  # franja azul Alsa
    "C": "#40c8ec",  # franja celeste
    "K": "#2e333d",  # neumático
    "H": "#c9d3e3",  # tapacubos
    "h": "#7b8496",  # radio de la rueda
    "Y": "#ffd166",  # faro
    "R": "#ef4444",  # piloto trasero
    "S": "#3b4456",  # faldón
    "D": "#22417a",  # puerta
}

_LETTERS = {
    "A": [
        "..###..",
        ".#####.",
        "##...##",
        "##...##",
        "##...##",
        "#######",
        "#######",
        "##...##",
        "##...##",
    ],
    "L": [
        "##.....",
        "##.....",
        "##.....",
        "##.....",
        "##.....",
        "##.....",
        "##.....",
        "#######",
        "#######",
    ],
    "S": [
        ".######",
        "#######",
        "##.....",
        "######.",
        ".######",
        ".....##",
        ".....##",
        "#######",
        "######.",
    ],
}

TITLE_GRADIENT = ["#9bdcff", "#7fcbff", "#62b8ff", "#4aa3fb", "#3a8ff2", "#2f7de9", "#2670e0", "#1f63d1", "#1a57bf"]


def word_bitmap(word: str, gap: int = 2) -> Bitmap:
    letters = [_LETTERS[ch] for ch in word]
    height = len(letters[0])
    return [(("." * gap).join(letter[y] for letter in letters)) for y in range(height)]


def render(bitmap: Bitmap, color_at: ColorAt) -> Text:
    """Convierte un bitmap en Text usando ▀/▄ para tener píxeles cuadrados."""
    rows = list(bitmap)
    if len(rows) % 2:
        rows.append("." * len(rows[0]))
    text = Text(no_wrap=True, overflow="crop")
    for y in range(0, len(rows), 2):
        top_row, bottom_row = rows[y], rows[y + 1]
        for x in range(len(top_row)):
            top = color_at(x, y, top_row[x]) if top_row[x] != "." else None
            bottom = color_at(x, y + 1, bottom_row[x]) if bottom_row[x] != "." else None
            if top is None and bottom is None:
                text.append(" ")
            elif bottom is None:
                text.append("▀", Style(color=top))
            elif top is None:
                text.append("▄", Style(color=bottom))
            elif top == bottom:
                text.append("█", Style(color=top))
            else:
                text.append("▀", Style(color=top, bgcolor=bottom))
        if y + 2 < len(rows):
            text.append("\n")
    return text


def title_text() -> Text:
    return render(word_bitmap("ALSA"), lambda x, y, ch: TITLE_GRADIENT[y])


def palette_color(x: int, y: int, ch: str) -> str | None:
    return PALETTE.get(ch)


BUS_WIDTH = 50
BUS_HEIGHT = 18
_WHEELS = (10, 37, 43)
_SPOKES = ((0, -1), (1, 0), (0, 1), (-1, 0))


def bus_bitmap(frame: int = 0) -> Bitmap:
    """Autocar de perfil mirando a la izquierda; `frame` gira las ruedas."""
    w, h = BUS_WIDTH, BUS_HEIGHT
    grid = [["." for _ in range(w)] for _ in range(h)]

    def put(x: int, y: int, ch: str) -> None:
        if 0 <= x < w and 0 <= y < h:
            grid[y][x] = ch

    # carrocería: techo con morro redondeado y trasera recta
    for y in range(15):
        start = {0: 3, 1: 1}.get(y, 0)
        for x in range(start, w - 1):
            put(x, y, "W")
    for x in range(3, w - 2):
        put(x, 0, "w")

    # parabrisas grande, puerta y ventanillas en banda
    for y in range(2, 9):
        for x in range(max(0, 3 - y), 5):
            put(x, y, "G")
    put(1, 4, "g")
    put(2, 3, "g")
    for y in range(2, 14):
        for x in range(6, 9):
            put(x, y, "G" if y < 9 else "D")
    put(8, 11, "H")
    x = 10
    while x + 5 <= w - 3:
        for y in range(2, 8):
            for dx in range(5):
                put(x + dx, y, "G")
        put(x + 1, 3, "g")
        x += 6

    # librea: franja azul y ola celeste que sube hacia atrás
    for x in range(w - 1):
        if not 6 <= x <= 8:
            put(x, 10, "B")
            put(x, 11, "B")
    wave_start = 24
    for x in range(wave_start, w - 1):
        rise = (x - wave_start) // 4
        for y in range(max(8, 9 - rise), 10):
            put(x, y, "C")
        if rise >= 2:
            put(x, 8, "C")

    put(0, 11, "Y")
    put(0, 12, "Y")
    put(w - 2, 3, "R")
    put(w - 2, 10, "R")
    put(w - 2, 11, "R")

    # maleteros y faldón
    for x in range(14, 33):
        if x % 6 != 1:
            put(x, 13, "w")
    for x in range(w - 1):
        put(x, 14, "S")

    # ruedas de 5x5 con un radio que gira
    for cx in _WHEELS:
        for dy, span in ((13, 1), (14, 2), (15, 2), (16, 2), (17, 1)):
            for dx in range(-span, span + 1):
                put(cx + dx, dy, "K")
        put(cx, 15, "H")
        sx, sy = _SPOKES[frame % 4]
        put(cx + sx, 15 + sy, "h")
    return ["".join(row) for row in grid]


SCENE_HEIGHT = 22
SCENE_PALETTE = {
    **PALETTE,
    "k": "#0f1829",  # edificios al fondo
    "j": "#132038",  # edificios más cerca
    "o": "#1b2a47",  # ventana apagada
    "l": "#b88d36",  # ventana encendida
    "a": "#232b3a",  # asfalto
    "m": "#6f7a8e",  # línea discontinua
}


def _skyline_column(x: int, ground: int) -> list[tuple[int, str]]:
    """Píxeles de fondo de la columna `x`, deterministas para poder desplazar el skyline."""
    block, inner = divmod(x, 7)
    seed = (block * 2654435761) & 0xFFFFFFFF
    height = 6 + seed % 11
    body = "j" if (seed >> 5) % 3 == 0 else "k"
    if inner == 6:
        return []
    top = max(0, ground - height)
    pixels = []
    for y in range(top, ground):
        ch = body
        if inner in (1, 3) and (y - top) % 2 == 1 and y < ground - 1:
            ch = "l" if ((seed >> (y % 13)) ^ block ^ y) % 6 == 0 else "o"
        pixels.append((y, ch))
    return pixels


def scene_text(
    width: int, bus_x: int | None, frame: int = 0, scroll: int = 0, bounce: int = 0, height: int = SCENE_HEIGHT
) -> Text:
    """Skyline, carretera y autocar en `bus_x` (None = sin autocar); `height` en píxeles."""
    ground = height - 2
    grid = [["." for _ in range(width)] for _ in range(height)]
    for x in range(width):
        for y, ch in _skyline_column(x + scroll // 3, ground):
            grid[y][x] = ch
        grid[ground][x] = "a"
        grid[ground + 1][x] = "m" if (x + scroll) % 8 < 4 else "a"
    if bus_x is not None:
        top = ground - BUS_HEIGHT - bounce
        for by, row in enumerate(bus_bitmap(frame)):
            for bx, ch in enumerate(row):
                x = bus_x + bx
                if ch != "." and 0 <= x < width and 0 <= top + by < height:
                    grid[top + by][x] = ch
    return render(["".join(row) for row in grid], lambda x, y, ch: SCENE_PALETTE.get(ch))


# --- seguimiento: tira de la ruta con un autocar pequeño ---------------------------

# Autocar de perfil mirando a la derecha, hacia el destino: 8 píxeles = 4 líneas.
MINI_BUS = [
    "..wwwwwwwwwwwwwwww..",
    ".WWWWWWWWWWWWWWWWWW.",
    ".RGGgWGGgWGGgWDWGGGW",
    ".WGGGWGGGWGGGWDWGGGW",
    ".WWWWWWWWWWWWWDWWWWW",
    ".BCCCCBBBBBBBBDBBBBY",
    ".SSKKKSSSSSSSSKKKSSS",
    "...KHK........KHK...",
]
MINI_BUS_WIDTH = len(MINI_BUS[0])
STRIP_HEIGHT = 10  # píxeles: autocar, postes de parada y carretera
STRIP_PALETTE = {
    **PALETTE,
    "a": "#232b3a",  # asfalto por recorrer
    "m": "#6f7a8e",  # línea discontinua
    "r": "#1d6fe0",  # asfalto ya recorrido, azul Alsa
    "p": "#5b6478",  # poste de parada
    "o": "#ffb547",  # tu parada de salida y de llegada, ámbar como el letrero
    "i": "#8b95a7",  # parada intermedia
}


def route_strip(
    width: int, progress: float | None, stops: list[float] | None = None, frame: int = 0, approaching: bool = False
) -> Text:
    """Carretera de tu salida (izquierda) a tu llegada (derecha) con el autocar en `progress`.

    `stops` son las paradas intermedias como fracción del trayecto (0 a 1) y `progress=None`
    dibuja la carretera sin autocar. Con `approaching`, el autocar aún no ha llegado a tu
    parada: espera a su izquierda, en un tramo de carretera previo. `frame` anima la línea
    discontinua.
    """
    width = max(width, 2 * MINI_BUS_WIDTH + 4)
    ground = STRIP_HEIGHT - 2
    grid = [["." for _ in range(width)] for _ in range(STRIP_HEIGHT)]
    start = MINI_BUS_WIDTH + 3 if approaching else 0  # x de tu parada de salida
    span = width - 1 - start
    if approaching:
        bus_x: int | None = 0
    else:
        bus_x = None if progress is None else round(progress * (width - MINI_BUS_WIDTH))
    # El tramo recorrido, en azul, llega hasta el morro del autocar.
    travelled = bus_x + MINI_BUS_WIDTH - 1 if bus_x is not None and progress and not approaching else -1
    for x in range(width):
        grid[ground][x] = "r" if x <= travelled else "a"
        grid[ground + 1][x] = "m" if (x - frame) % 6 < 3 else "a"

    def post(x: int, sign: str) -> None:
        x = min(width - 1, max(0, x))
        for y in range(ground - 3, ground):
            grid[y][x] = "p"
        grid[ground - 4][x] = sign

    for fraction in stops or []:
        post(start + round(fraction * span), "i")
    post(start, "o")
    post(width - 1, "o")

    if bus_x is not None:
        top = ground - len(MINI_BUS)  # las ruedas apoyan sobre la carretera
        for by, row in enumerate(MINI_BUS):
            for bx, ch in enumerate(row):
                if ch != "." and 0 <= bus_x + bx < width:
                    grid[top + by][bus_x + bx] = ch
    return render(["".join(row) for row in grid], lambda x, y, ch: STRIP_PALETTE.get(ch))
