"""Seguimiento en directo: textos y tira de la ruta (también para `alsa track`) y su pantalla."""

from __future__ import annotations

import time
import webbrowser
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, ClassVar

from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.widget import Widget
from textual.widgets import Button, ContentSwitcher, DataTable, Footer, Label, Static
from textual.worker import Worker, WorkerState

from alsa.core import MADRID_TZ
from alsa.exceptions import AlsaError
from alsa.models import BusTracking, Journey, TrackedLeg, map_url
from alsa.tracking import track_route
from alsa.tui import fmt
from alsa.tui.art import MINI_BUS_WIDTH, STRIP_HEIGHT, render, route_strip
from alsa.tui.spain_map import (
    BUS_COLORS,
    CASING,
    MAP_SYMBOLS,
    TODO,
    map_pixels,
    render_pixels,
)
from alsa.tui.widgets import BusScene

if TYPE_CHECKING:
    from alsa.tui.screens import SearchRequest
    from alsa.tui.stations import StationOption

REFRESH_SECONDS = 60  # Alsa actualiza la posición cada poco; no hace falta preguntar más a menudo

NO_TRACKING = "Alsa no tiene seguimiento ni geolocalización para este autobús."


# --- textos compartidos con `alsa track` ---------------------------------------------


def _hm(value: datetime) -> str:
    return value.astimezone(MADRID_TZ).strftime("%H:%M")


def stop_fractions(t: BusTracking) -> list[float]:
    """Paradas intermedias de tu trayecto como fracción del tiempo entre tu salida y tu llegada."""
    if t.vehicle is None or t.origin_stop is None or t.destination_stop is None:
        return []
    stops = t.vehicle.stops
    start, end = stops[t.origin_stop].departure, stops[t.destination_stop].arrival
    span = (end - start).total_seconds()
    if span <= 0:
        return []
    return [(stops[i].arrival - start).total_seconds() / span for i in range(t.origin_stop + 1, t.destination_stop)]


def strip(t: BusTracking, width: int, frame: int = 0) -> Text:
    """Tira de píxeles con el autocar y, debajo, tu salida y tu llegada con sus horas."""
    progress = t.progress if t.located else None
    art = route_strip(width, progress, stop_fractions(t), frame, approaching=t.status == "approaching")
    leg = t.leg
    left = Text.assemble((_hm(leg.departure), "bold"), " ", fmt.place(leg.origin_name))
    if t.status == "approaching":  # tu parada está tras el hueco del autocar que llega
        left = Text.assemble(" " * (MINI_BUS_WIDTH + 3), left)
    right = Text.assemble(fmt.place(leg.destination_name), " ", (_hm(leg.arrival), "bold"))
    late = t.delay_minutes or 0
    if t.estimated_arrival is not None and late > 2:
        right.append(f" ≈ {_hm(t.estimated_arrival)}", fmt.delay_style(late))
    gap = max(1, width - left.cell_len - right.cell_len)
    if gap == 1 and left.cell_len + right.cell_len + 1 > width:
        left.truncate(max(8, width - right.cell_len - 2), overflow="ellipsis")
        gap = max(1, width - left.cell_len - right.cell_len)
    return Text.assemble(art, "\n", left, " " * gap, right)


def headline(t: BusTracking) -> Text:
    """Una línea: dónde está el autobús y cuánto retraso lleva."""
    leg, v = t.leg, t.vehicle
    text = Text()
    if t.status == "not_checked":
        text.append("Sin consultar: ", "bold")
        text.append("sale dentro de más de 3 horas o ya llegó", "dim")
        return text
    if t.status == "unmatched_route":
        return Text("No se ha podido identificar el recorrido de este autobús.", "bold #fbbf24")
    if t.status == "no_tracking" or v is None:
        text.append("Sin seguimiento: ", "bold #fbbf24")
        text.append(NO_TRACKING, "dim")
        return text
    if t.status == "no_position":
        text.append("Sin posición: ", "bold #fbbf24")
        text.append("Alsa tiene el recorrido, pero no dice dónde está el autobús ahora mismo.", "dim")
        return text
    if t.status == "off_route":
        text.append("Posición dudosa: ", "bold #fbbf24")
        text.append(f"Alsa lo sitúa a {fmt.km(t.distance_to_route_km or 0)} del recorrido; no se puede estimar el retraso.", "dim")
        return text

    stops = v.stops
    late = t.delay_minutes or 0
    if t.status == "waiting":
        first = stops[0]
        text.append(f"En {fmt.place(first.town)}", "bold")
        text.append(f", aún no ha salido: sale a las {_hm(first.departure)}")
        if t.origin_stop:
            text.append(f" y pasa por {fmt.place(leg.origin_name)} a las {_hm(leg.departure)}", "dim")
        return text
    if t.status == "arrived":
        text.append(f"Ya ha llegado a {fmt.place(leg.destination_name)}", "bold #4ade80")
        return text

    if t.status == "approaching":
        text.append(f"Va hacia {fmt.place(leg.origin_name)}", "bold")
        if t.estimated_departure is not None:
            text.append(f": pasa ≈ {_hm(t.estimated_departure)}")
    else:
        text.append("En camino", "bold")
    text.append("  ·  ")
    text.append(("≈ " if t.approximate else "") + fmt.delay(late), fmt.delay_style(late))
    if t.next_stop is not None:
        upcoming = stops[t.next_stop]
        if t.at_stop:
            text.append(f"  ·  parado en {fmt.place(upcoming.town)}", "dim")
        elif t.next_stop == t.origin_stop:  # «Va hacia» ya nombra tu parada
            if t.distance_to_next_km is not None:
                text.append(f"  ·  a {fmt.km(t.distance_to_next_km)}", "dim")
        else:
            text.append(f"  ·  próxima parada {fmt.place(upcoming.town)}", "dim")
            if t.distance_to_next_km is not None:
                text.append(f" a {fmt.km(t.distance_to_next_km)}", "dim")
    if t.approximate:
        text.append("  ·  estimación aproximada: tramo largo sin paradas", "dim italic")
    return text


def whereabouts(t: BusTracking) -> Text | None:
    """Para un autobús que aún viene: entre qué paradas va y de dónde sale su recorrido."""
    v = t.vehicle
    if t.status != "approaching" or v is None or t.position is None:
        return None
    index = int(t.position)
    here = fmt.place(v.stops[index].town)
    text = Text("Ahora ", "dim")
    if t.at_stop or index + 1 >= len(v.stops):
        text.append(f"en {here}", "bold")
    else:
        text.append(f"entre {here} y {fmt.place(v.stops[index + 1].town)}", "bold")
    if v.route_origin:
        text.append(f"  ·  el recorrido sale de {fmt.place(v.route_origin)}", "dim")
    return text


def stops_grid(t: BusTracking, fold: bool = False) -> Table:
    """Tus paradas, de la salida a la llegada: ✓ ya pasó, › la siguiente, · por llegar.

    Con `fold`, si son muchas, se ven tu salida, la última pasada, las dos siguientes y tu
    llegada; las demás se resumen en «… N paradas» para que la lista quepa en pantalla.
    """
    grid = Table.grid(padding=(0, 2), expand=fold)
    grid.add_column(width=1, no_wrap=True)
    grid.add_column(min_width=5, no_wrap=True)
    grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    grid.add_column(justify="right", no_wrap=True)
    v = t.vehicle
    if v is None or t.origin_stop is None or t.destination_stop is None:
        return grid
    estimated = t.estimated_stops or [None] * len(v.stops)
    late = t.delay_minutes or 0
    rows = list(range(t.origin_stop, t.destination_stop + 1))
    passed = {
        i
        for i in rows
        if t.located and t.status != "waiting" and t.position is not None and i <= t.position and not (t.at_stop and i == t.position)
    }
    upcoming = t.next_stop if t.located and t.next_stop is not None else t.origin_stop
    keep = {t.origin_stop, t.destination_stop, upcoming, upcoming + 1, max(passed, default=t.origin_stop)}
    hidden = {i for i in rows if i not in keep} if fold and len(rows) > 8 else set()

    def marker(i: int) -> tuple[Text, str]:
        if i in passed:
            return Text("✓", "bold #4ade80"), "dim"
        if t.located and i == t.next_stop:
            return Text("›", "bold #40c8ec"), "bold"
        return Text("·", "dim"), ""

    run: list[int] = []

    def flush() -> None:
        if len(run) == 1:
            add(run[0])
        elif run:
            mark, _ = marker(run[0])
            what = "ya pasadas" if run[0] in passed else "más"
            grid.add_row(mark, Text(""), Text(f"… {len(run)} paradas {what}", "dim italic"), Text(""))
        run.clear()

    def add(i: int) -> None:
        stop = v.stops[i]
        mark, style = marker(i)
        scheduled = stop.departure if i == t.origin_stop else stop.arrival
        estimate = Text("")
        if estimated[i] is not None and late > 2 and i not in passed:
            when = t.estimated_departure if i == t.origin_stop and t.estimated_departure else estimated[i]
            estimate = Text(f"≈ {_hm(when)}", fmt.delay_style(late))  # type: ignore[arg-type]
        name = fmt.place(stop.town)
        name_text = Text(name, style)
        if stop.name and fmt.place(stop.name).lower() != name.lower():
            name_text.append(f"  {fmt.place(stop.name)}", "dim")
        grid.add_row(mark, Text(_hm(scheduled), style or "bold"), name_text, estimate)

    for i in rows:
        if i in hidden and (not run or (run[-1] in passed) == (i in passed)):
            run.append(i)
            continue
        flush()
        if i in hidden:
            run.append(i)
        else:
            add(i)
    flush()
    return grid


# --- pantalla de la TUI -----------------------------------------------------------------


class RouteStrip(Widget):
    """La tira de la ruta; la línea discontinua corre mientras el autobús va en camino."""

    DEFAULT_CSS = f"""
    RouteStrip {{
        height: {STRIP_HEIGHT // 2 + 1};
        width: 1fr;
    }}
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.tracking: BusTracking | None = None
        self._frame = 0

    def on_mount(self) -> None:
        self._timer = self.set_interval(0.25, self._tick, pause=True)

    def show(self, tracking: BusTracking | None, *, stale: bool = False) -> None:
        self.tracking = tracking
        moving = not stale and tracking is not None and tracking.status in ("approaching", "on_the_way") and not tracking.at_stop
        self._timer.resume() if moving else self._timer.pause()
        self.refresh()

    def _tick(self) -> None:
        self._frame += 1
        self.refresh()

    def render(self) -> Text:
        if self.tracking is None:
            return Text("")
        return strip(self.tracking, self.size.width or 60, self._frame)


@dataclass(frozen=True)
class TrackTarget:
    """El autobús que se sigue: la ruta buscada (paradas o ciudades) y su salida."""

    origin_id: str
    destination_id: str
    origin_stop_id: str
    destination_stop_id: str
    origin_name: str
    destination_name: str
    departure: datetime
    arrival: datetime
    leg: TrackedLeg | None = None

    @classmethod
    def from_journey(cls, j: Journey, req: SearchRequest) -> TrackTarget:
        return cls(req.origin.id, req.destination.id, j.origin_id, j.destination_id, j.origin_name, j.destination_name, j.departure, j.arrival)

    @classmethod
    def from_leg(cls, leg: TrackedLeg, origin_id: str, destination_id: str) -> TrackTarget:
        return cls(
            origin_id,
            destination_id,
            leg.origin_id,
            leg.destination_id,
            fmt.place(leg.origin_name),
            fmt.place(leg.destination_name),
            leg.departure,
            leg.arrival,
            leg,
        )


class MapView(Widget):
    """Mapa de píxeles de España con la ruta y el autobús; el halo del autobús parpadea."""

    DEFAULT_CSS = """
    MapView {
        width: 1fr;
        height: 1fr;
    }
    """

    class SymbolsChanged(Message):
        """El encuadre o las superposiciones han cambiado las marcas visibles."""

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.tracking: BusTracking | None = None
        self.zoom = True
        self.stale = False
        self.symbols: set[str] = set()
        self._blink = False
        self._frames: dict[bool, Text] = {}
        self._dimensions = (0, 0)

    def on_mount(self) -> None:
        self.set_interval(0.6, self._tick)

    def show(self, tracking: BusTracking | None, zoom: bool, stale: bool) -> None:
        if tracking is not self.tracking or zoom != self.zoom:
            self._frames.clear()
        self.tracking, self.zoom, self.stale = tracking, zoom, stale
        self.refresh()

    def _tick(self) -> None:
        if not self.stale and self.tracking and BUS_COLORS["B"] in self.symbols:
            self._blink = not self._blink
            self.refresh()

    def render(self) -> Text:
        width, height = self.size
        if not width or not height:
            return Text("")
        if self._dimensions != (width, height):
            self._dimensions = (width, height)
            self._frames.clear()
        hide_halo = self.stale or self._blink
        if hide_halo not in self._frames:
            pixels = map_pixels(width, height * 2, self.tracking, self.zoom, hide_halo)
            self._frames[hide_halo] = render_pixels(pixels)
            colors = {color for row in pixels for color in row}
            visible = {key for key, _, _, _ in MAP_SYMBOLS if key in colors}
            if visible != self.symbols:
                self.symbols = visible
                self.post_message(self.SymbolsChanged())
        return self._frames[hide_halo]


def map_legend(t: BusTracking, visible: set[str], stale: bool = False) -> Text:
    """Muestras de los mismos píxeles que hay en este encuadre, en dos filas."""
    text = headline(t)
    if where := whereabouts(t):
        text.append("\n")
        text.append_text(where)
    for group in (MAP_SYMBOLS[:3], MAP_SYMBOLS[3:]):
        samples = []
        for key, bitmap, palette, label in group:
            if key not in visible:
                continue
            if key == TODO and not t.located:
                label = "Tu trayecto"
            if stale and key == BUS_COLORS["B"]:
                palette = {**palette, "y": CASING}
            lines = render(bitmap, lambda x, y, ch, colors=palette: colors[ch]).split("\n")
            samples.append((lines, label))
        if not samples:
            continue
        height = max(len(lines) for lines, _ in samples)
        for row in range(height + 1):
            text.append("\n")
            for lines, label in samples:
                if row == height:
                    cell = Text(label, "dim")
                else:
                    offset = height - len(lines)
                    cell = lines[row - offset].copy() if row >= offset else Text()
                cell.align("center", max(len(label), lines[0].cell_len))
                text.append_text(cell)
                text.append(" ")
    return text


def map_stops(t: BusTracking) -> Text:
    """Paradas que el autocar aún visitará hasta tu destino, también antes de recogerte."""
    text = Text()
    v = t.vehicle
    if v is None or t.destination_stop is None:
        return text
    end = t.destination_stop
    if t.status == "arrived":
        return Text("Has llegado a tu destino · 0 paradas pendientes", "bold #4ade80")

    if t.located and t.next_stop is not None:
        first = t.next_stop
        if t.at_stop:
            stop = v.stops[first]
            first += 1
            text.append(f"Parado en {fmt.place(stop.town or stop.name)}", "bold #40c8ec")
            text.append(f" · salida {fmt.hm(stop.departure)} (horario)\n", "dim")
        count = end - first + 1
        title = "1 parada hasta tu destino" if count == 1 else f"{count} paradas hasta tu destino"
    else:
        first = t.origin_stop or 0
        title = "Paradas de tu viaje · horario"
    text.append(title, "bold")
    for i in range(first, end + 1):
        stop = v.stops[i]
        scheduled = stop.departure if i == t.origin_stop else stop.arrival
        eta = t.estimated_departure if i == t.origin_stop else None
        if eta is None and t.estimated_stops:
            eta = t.estimated_stops[i]
        text.append("\n")
        when = fmt.arrival(v.stops[0].departure, eta or scheduled)
        text.append(f"≈ {when}" if eta else f"  {when}", "bold #40c8ec" if i == first and t.located else "bold")
        text.append(f"  {fmt.place(stop.town or stop.name)}")
        if i == t.origin_stop:
            text.append(" · tu salida", "dim")
        if not eta and t.located:
            text.append(" (horario)", "dim")
        if stop.town and stop.name and stop.town.casefold() != stop.name.casefold():
            text.append(f" · {fmt.place(stop.name)}", "dim")
    if t.located and t.estimated_stops:
        text.append("\n≈ hora estimada", "dim")
    return text


def find_leg(trackings: list[BusTracking], target: TrackTarget, now: datetime) -> BusTracking | None:
    """Identifica el viaje de los resultados y elige su tramo actual o el siguiente."""
    # track_route ya ha filtrado por hora y parada de salida. Comprobamos la fecha
    # y la parada de llegada del viaje completo antes de elegir uno de sus tramos.
    checked = [t for t in trackings if t.checked]
    starts = {t.leg.trip for t in checked if t.leg.leg == 1 and t.leg.departure.date() == target.departure.date()}
    ends = [
        t.leg for t in checked
        if t.leg.trip in starts and t.leg.leg == t.leg.legs and t.leg.destination_id == target.destination_stop_id
    ]
    if len(ends) > 1:
        ends = [leg for leg in ends if leg.arrival == target.arrival]
    if len(ends) != 1:
        return None
    legs = [t for t in checked if t.leg.trip == ends[0].trip]
    for t in legs:
        if t.leg.departure <= now <= t.leg.arrival:
            return t
    upcoming = [t for t in legs if t.leg.departure > now]
    return upcoming[0] if upcoming else legs[-1]


class TrackingScreen(ModalScreen[None]):
    """Dónde va un autobús, con retraso y paradas; se actualiza solo."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "volver"),
        Binding("r", "refresh", "actualizar"),
        Binding("m", "open_map", "mapa en el navegador"),
        Binding("right", "view('mapa')", "mapa", show=False),
        Binding("left", "view('ruta')", "ruta", show=False),
        Binding("z", "toggle_zoom", "zoom", show=False),
        Binding("down", "scroll_stops(1)", "paradas siguientes", show=False),
        Binding("up", "scroll_stops(-1)", "paradas anteriores", show=False),
    ]
    VERTICAL_BREAKPOINTS: ClassVar[list[tuple[int, str]]] = [(0, "-short"), (32, "-tall")]
    HORIZONTAL_BREAKPOINTS: ClassVar[list[tuple[int, str]]] = [(0, "-narrow"), (76, "-wide")]

    def __init__(self, target: TrackTarget, tracking: BusTracking | None = None, checked_at: datetime | None = None) -> None:
        super().__init__()
        self.target = target
        self.tracking = tracking  # si llega ya consultado, se pinta sin esperar
        self.checked_at = checked_at
        self._busy = False
        self._refresh_error = ""
        self._started = 0.0
        self.view = "ruta"
        self.zoom = True

    def compose(self) -> ComposeResult:
        j = self.target
        summary = Text()
        summary.append(f"{fmt.hm(j.departure)}  ", "bold")
        summary.append(j.origin_name)
        summary.append("  →  ", "bold #40c8ec")
        summary.append(f"{fmt.arrival(j.departure, j.arrival)}  ", "bold")
        summary.append(j.destination_name)
        with Vertical(id="tracking-box", classes="dialog"):
            yield Label("Dónde va tu autobús", classes="dialog-title")
            yield Static(summary, id="tracking-summary")
            yield Static(id="tracking-tabs")
            with Vertical(id="route-view"):
                yield RouteStrip(id="tracking-strip")
                yield Static(id="tracking-headline", classes="dialog-text")
                yield Static(id="tracking-stops", classes="dialog-text")
            with Horizontal(id="map-view"):
                yield MapView(id="tracking-map")
                with VerticalScroll(id="map-sidebar"):
                    yield Static(id="map-legend")
                    yield Static(id="map-stops")
            yield Static(id="tracking-note", classes="dialog-text")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Volver", id="back", variant="primary", compact=True)
                yield Button("Actualizar", id="refresh", compact=True, classes="link")
                yield Button("Mapa en el navegador", id="map", compact=True, classes="link")

    def on_mount(self) -> None:
        self.query_one("#back", Button).focus()
        self.set_interval(REFRESH_SECONDS, self.action_refresh)
        self.set_interval(0.5, self._tick_loading)
        if self.tracking is not None and self.checked_at is not None:
            self._render_tracking()
        else:
            self.action_refresh()

    # --- consulta -------------------------------------------------------------

    def action_refresh(self) -> None:
        if self._busy:
            return
        self._busy = True
        self._started = time.monotonic()
        self._tick_loading()
        self._fetch()

    @work(thread=True, exclusive=True, group="tracking", exit_on_error=False)
    def _fetch(self) -> tuple[BusTracking | None, datetime]:
        now = datetime.now(MADRID_TZ)
        j = self.target
        if j.leg is not None:
            rows, _ = track_route(j.origin_id, j.destination_id, selected_leg=j.leg, now=now)
            return next((t for t in rows if t.checked), None), now

        rows, _ = track_route(j.origin_id, j.destination_id, fmt.hm(j.departure), j.origin_stop_id, now=now)
        return find_leg(rows, j, now), now

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.group != "tracking" or event.state not in (WorkerState.SUCCESS, WorkerState.ERROR):
            return
        self._busy = False
        if event.state == WorkerState.ERROR:
            error = event.worker.error
            self._refresh_error = str(error) if isinstance(error, AlsaError) else f"Error inesperado: {error}"
            if self.tracking is None:
                self.query_one("#tracking-headline", Static).update("")
            self._render_view()
            self.query_one(RouteStrip).show(self.tracking if self._has_route() else None, stale=True)
            self._render_note()
            return
        self._refresh_error = ""
        self.tracking, self.checked_at = event.worker.result
        if self.target.leg is None and self.tracking is not None:
            self.target = replace(self.target, leg=self.tracking.leg)
        self._render_tracking()

    def _tick_loading(self) -> None:
        if not self._busy:
            return
        dots = "." * (1 + int(time.monotonic() - self._started) % 3)
        if self.tracking is None:
            self.query_one("#tracking-headline", Static).update(
                Text.assemble(("Preguntando a alsa.es dónde va", "bold"), (dots.ljust(3), "bold #40c8ec"))
            )
        self._render_note()

    # --- pintado --------------------------------------------------------------

    def _render_tracking(self) -> None:
        t = self.tracking
        strip_widget = self.query_one(RouteStrip)
        if t is None:
            strip_widget.show(None)
            message = (
                "No se encuentra el tramo seleccionado en «Localiza tu bus»."
                if self.target.leg else "No se ha podido identificar este viaje en «Localiza tu bus» de hoy."
            )
            self.query_one("#tracking-headline", Static).update(Text(message, "bold #fbbf24"))
        else:
            strip_widget.show(t if t.vehicle is not None else None)
            where = whereabouts(t)
            self.query_one("#tracking-headline", Static).update(Text.assemble(headline(t), "\n", where) if where else headline(t))
        self.query_one("#tracking-stops", Static).update(stops_grid(t, fold=True) if t and t.vehicle else "")
        self.query_one("#map", Button).display = bool(self.check_action("open_map", ()))
        if self.view == "mapa" and not self._has_route():
            self.action_view("ruta")
        self._render_view()
        self.refresh_bindings()
        self._render_note()

    def _has_route(self) -> bool:
        t = self.tracking
        return bool(t and t.vehicle and t.vehicle.stops)

    def _render_view(self) -> None:
        """Pestañas, y el mapa con su leyenda si es la vista activa."""
        tabs = Text()
        if self._has_route():
            for name in ("ruta", "mapa"):
                active = name == self.view
                tabs.append(f" {name} ", "bold #0b1220 on #40c8ec" if active else "dim")
                if name == "ruta":
                    tabs.append(" · ", "dim")
            tabs.append("   ← → vista" if self.view == "mapa" else "   ← → cambia la vista", "dim italic")
            if self.view == "mapa":
                tabs.append("  ·  z " + ("España entera" if self.zoom else "zoom a tu viaje"), "dim")
                tabs.append("  ·  ↑↓ paradas", "dim")
                if self.check_action("open_map", ()):
                    tabs.append("  ·  m navegador", "dim")
        self.query_one("#tracking-tabs", Static).update(tabs)
        self.query_one("#tracking-tabs", Static).display = self._has_route()
        on_map = self.view == "mapa"
        self.set_class(on_map, "-map")
        widget = self.query_one(MapView)
        widget.show(self.tracking if on_map else None, self.zoom, bool(self._refresh_error))
        if on_map and self.tracking:
            self._update_legend()
            self.query_one("#map-stops", Static).update(map_stops(self.tracking))

    @on(MapView.SymbolsChanged)
    def _update_legend(self) -> None:
        if self.view == "mapa" and self.tracking:
            self.query_one("#map-legend", Static).update(map_legend(self.tracking, self.query_one(MapView).symbols, bool(self._refresh_error)))

    def _render_note(self) -> None:
        if self._refresh_error:
            note = Text("No se pudo actualizar. ", "bold #fbbf24")
            if self.checked_at:
                note.append(f"Última consulta a las {self.checked_at:%H:%M}. ", "bold")
            note.append(self._refresh_error)
            self.query_one("#tracking-note", Static).update(note)
            self.query_one("#tracking-note").add_class("-error")
            return
        self.query_one("#tracking-note").remove_class("-error")
        note = Text(style="dim")
        if self._busy and self.tracking is not None:
            note.append("Actualizando…  ")
        elif self.checked_at is not None:
            note.append(f"Consultado a las {self.checked_at:%H:%M}; se actualiza cada minuto.  ")
        t = self.tracking
        if t is not None and t.located and t.delay_minutes is not None:
            note.append("El retraso es una estimación con el GPS del autobús y su horario.")
        self.query_one("#tracking-note", Static).update(note)

    # --- acciones ---------------------------------------------------------------

    def on_resize(self) -> None:
        if self.view == "mapa":
            self.call_after_refresh(self._render_view)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "open_map":
            t = self.tracking
            return bool(t and t.vehicle and t.vehicle.latitude is not None and t.vehicle.longitude is not None)
        if action == "view":
            return self._has_route() and parameters[0] != self.view
        if action in ("toggle_zoom", "scroll_stops"):
            return self.view == "mapa"
        return True

    def action_view(self, view: str) -> None:
        if view == "mapa" and not self._has_route():
            return
        self.view = view
        self._render_view()
        self.refresh_bindings()

    def action_scroll_stops(self, direction: int) -> None:
        sidebar = self.query_one("#map-sidebar", VerticalScroll)
        sidebar.scroll_relative(y=direction, animate=False)

    def action_toggle_zoom(self) -> None:
        self.zoom = not self.zoom
        self._render_view()

    @on(Button.Pressed, "#map")
    def action_open_map(self) -> None:
        t = self.tracking
        if not t or not t.vehicle or t.vehicle.latitude is None or t.vehicle.longitude is None:
            return
        url = map_url(t.vehicle.latitude, t.vehicle.longitude)
        try:
            opened = webbrowser.open(url, new=2)
        except webbrowser.Error:
            opened = False
        if not opened:
            self.app.copy_to_clipboard(url)
            self.notify("No se pudo abrir el navegador; enlace del mapa copiado.", timeout=3)

    @on(Button.Pressed, "#refresh")
    def _refresh_pressed(self) -> None:
        self.action_refresh()

    @on(Button.Pressed, "#back")
    def action_close(self) -> None:
        self.workers.cancel_group(self, "tracking")
        self.dismiss()


# --- autobuses de hoy en una ruta ---------------------------------------------------------

STATUS_LABELS = {
    "on_the_way": ("en camino", "bold #40c8ec"),
    "approaching": ("viene hacia tu parada", "#40c8ec"),
    "waiting": ("aún no ha salido", ""),
    "arrived": ("ya ha llegado", "dim"),
    "no_tracking": ("sin seguimiento", "dim"),
    "unmatched_route": ("recorrido sin identificar", "#fbbf24"),
    "no_position": ("sin posición ahora", "dim"),
    "off_route": ("posición dudosa", "#fbbf24"),
}


def status_label(t: BusTracking, now: datetime) -> Text:
    if t.status == "not_checked":
        return Text("sale más tarde", "dim") if t.leg.departure > now else Text("ya ha llegado", "dim")
    label, style = STATUS_LABELS.get(t.status, (t.status, ""))
    return Text(label, style)


class LiveRouteScreen(Screen):
    """Todos los autobuses de hoy en una ruta, también los que ya salieron, con su estado y retraso."""

    HORIZONTAL_BREAKPOINTS: ClassVar[list[tuple[int, str]]] = [(0, "-narrow"), (100, "-wide")]

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "back", "volver"),
        Binding("r", "reload", "actualizar"),
        Binding("question_mark", "app.help", "ayuda", key_display="?"),
        Binding("f1", "app.help", show=False),
    ]

    def __init__(self, origin: StationOption, destination: StationOption) -> None:
        super().__init__()
        self.origin = origin
        self.destination = destination
        self.trackings: list[BusTracking] = []
        self.checked_at: datetime | None = None
        self.phase = "loading"
        self._started = 0.0
        self._compact: bool | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="route-bar"):
            yield Static(id="route")
            yield Static(id="route-day")
        yield Static(id="summary")
        with ContentSwitcher(initial="loading", id="results-body"):
            with Vertical(id="loading"):
                yield BusScene(id="loader")
                yield Static(id="loading-text")
            with Vertical(id="message"):
                yield Static(id="message-text")
            with Vertical(id="list"):
                yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
                with Horizontal(id="detail"):
                    yield Static(id="detail-text")
                    with Vertical(id="detail-actions"):
                        yield Button("Ver la ruta", id="open", variant="primary", compact=True)
                        yield Button("Nueva búsqueda", id="new", compact=True, classes="link")
        yield Footer(compact=True)

    def on_mount(self) -> None:
        route = Text.assemble((self.origin.label, "bold"), ("  →  ", "#40c8ec bold"), (self.destination.label, "bold"))
        self.query_one("#route", Static).update(route)
        self.query_one("#route-day", Static).update(Text.assemble(("hoy", "bold"), ("  ·  en directo", "dim")))
        self.set_interval(0.5, self._tick_loading)
        self.set_interval(REFRESH_SECONDS, self._auto_refresh)
        self.search()

    # --- consulta -------------------------------------------------------------

    def search(self) -> None:
        self.phase = "loading"
        self._started = time.monotonic()
        self.query_one(ContentSwitcher).current = "loading"
        self.query_one("#summary", Static).update("")
        self.query_one("#loader", BusScene).cruise()
        self._tick_loading()
        self._fetch()

    def _auto_refresh(self) -> None:
        if self.phase == "list" and self.app.screen is self:  # con el detalle encima, se actualiza el detalle
            self._fetch()

    @work(thread=True, exclusive=True, group="live", exit_on_error=False)
    def _fetch(self) -> tuple[list[BusTracking], datetime]:
        now = datetime.now(MADRID_TZ)
        trackings, _retries = track_route(self.origin.id, self.destination.id, now=now)
        return trackings, now

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.group != "live":
            return
        if event.state == WorkerState.SUCCESS:
            keep = self._current()
            self.trackings, self.checked_at = event.worker.result
            self._show(keep)
        elif event.state == WorkerState.ERROR and self.phase != "list":
            error = event.worker.error
            message = str(error) if isinstance(error, AlsaError) else f"Error inesperado: {error}"
            self._show_message("No se pudo consultar «Localiza tu bus»", message, "r para reintentar  ·  esc para cambiar la ruta", error=True)

    def _tick_loading(self) -> None:
        if self.phase != "loading":
            return
        elapsed = int(time.monotonic() - self._started)
        dots = "." * (1 + elapsed % 3)
        text = Text.assemble(
            ("Buscando autobuses en ruta", "bold"),
            (dots.ljust(3), "bold #40c8ec"),
            ("\n\nPreguntando a «Localiza tu bus» de alsa.es por los de hoy", "dim"),
            (f"  ·  {elapsed} s" if elapsed else "", "dim"),
            ("\nesc para cancelar", "dim italic"),
        )
        self.query_one("#loading-text", Static).update(text)

    def _show_message(self, title: str, body: str, hint: str, error: bool = False) -> None:
        self.phase = "message"
        self.query_one("#loader", BusScene).stop()
        text = Text.assemble((title, "bold #f87171" if error else "bold"), "\n\n", body, "\n\n", (hint, "dim"))
        self.query_one("#message-text", Static).update(text)
        self.query_one(ContentSwitcher).current = "message"
        self.refresh_bindings()

    # --- lista ------------------------------------------------------------------

    def _show(self, keep: BusTracking | None = None) -> None:
        self.query_one("#loader", BusScene).stop()
        if not self.trackings:
            self._show_message(
                "Ningún autobús de hoy en esta ruta",
                f"«Localiza tu bus» no tiene autobuses de Alsa hoy de {self.origin.label} a {self.destination.label}.",
                "Solo sigue los autobuses del día  ·  esc para cambiar la ruta",
            )
            return
        self.phase = "list"
        self._fill_table(keep)
        self._render_summary()
        self.query_one(ContentSwitcher).current = "list"
        self.query_one(DataTable).focus()
        self.refresh_bindings()

    def _render_summary(self) -> None:
        moving = sum(t.status in ("on_the_way", "approaching", "waiting") for t in self.trackings)
        count = len(self.trackings)
        summary = Text(f"{count} autobús hoy" if count == 1 else f"{count} autobuses hoy", style="bold")
        if moving:
            summary.append(f"  ·  {moving} en ruta ahora", "bold #40c8ec")
        else:
            summary.append("  ·  ninguno da su posición ahora mismo", "#fbbf24")
        if self.checked_at is not None:
            summary.append(f"  ·  consultado a las {self.checked_at:%H:%M}, se actualiza cada minuto", "dim")
        self.query_one("#summary", Static).update(summary)

    def _fill_table(self, keep: BusTracking | None = None) -> None:
        table = self.query_one(DataTable)
        compact = self.has_class("-narrow")
        self._compact = compact
        now = self.checked_at or datetime.now(MADRID_TZ)
        table.clear(columns=True)
        table.add_column("Sale", key="dep")
        table.add_column("Llega", key="arr")
        if not compact:
            table.add_column("Sale de", key="from")
        table.add_column("Ahora", key="status")
        table.add_column("Retraso", key="delay")
        table.add_column("Llega ≈", key="eta")
        for i, t in enumerate(self.trackings):
            leg = t.leg
            dim = "dim" if not t.located else ""
            late = t.delay_minutes if t.located and t.status != "arrived" else None
            cells = [Text(fmt.hm(leg.departure), style="bold" if t.located else "dim"), Text(fmt.arrival(leg.departure, leg.arrival), style=dim)]
            if not compact:
                cells.append(Text(fmt.place(leg.origin_name), style=dim, overflow="ellipsis", no_wrap=True))
            cells.append(status_label(t, now))
            cells.append(Text(fmt.delay(late), style=fmt.delay_style(late)) if late is not None else Text("—", "dim"))
            eta = t.estimated_arrival if late is not None and late > 2 else None
            cells.append(Text(_hm(eta), style=fmt.delay_style(late)) if eta and late is not None else Text(""))
            table.add_row(*cells, key=str(i))
        if keep is not None:
            row = next((i for i, t in enumerate(self.trackings) if t.leg.identity == keep.leg.identity and t.leg.departure == keep.leg.departure), 0)
        else:  # el primero que va en ruta o, si no, el próximo en salir
            row = next((i for i, t in enumerate(self.trackings) if t.status in ("on_the_way", "approaching", "waiting")), None)
            if row is None:
                row = next((i for i, t in enumerate(self.trackings) if t.leg.departure > now), 0)
        table.move_cursor(row=row)
        self._render_detail(row)

    def on_resize(self) -> None:
        if self.phase == "list" and self._compact is not None and self._compact != self.has_class("-narrow"):
            self.call_after_refresh(self._fill_table, self._current())

    def _current(self) -> BusTracking | None:
        if self.phase != "list":
            return None
        row = self.query_one(DataTable).cursor_row
        return self.trackings[row] if 0 <= row < len(self.trackings) else None

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._render_detail(event.cursor_row)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_open()

    def _render_detail(self, row: int) -> None:
        if not 0 <= row < len(self.trackings):
            return
        t = self.trackings[row]
        leg = t.leg
        text = Text()
        text.append(f"{fmt.hm(leg.departure)}  ", "bold")
        text.append(fmt.place(leg.origin_name))
        text.append("  →  ", "#40c8ec bold")
        text.append(f"{fmt.arrival(leg.departure, leg.arrival)}  ", "bold")
        text.append(fmt.place(leg.destination_name))
        if leg.line_name:
            text.append(f"\n{leg.line_name}", "dim")
        text.append("\n\n")
        if t.status == "not_checked":
            text.append("Aún no se ha consultado. ", "dim")
            text.append("Enter para preguntar a Alsa por dónde va", "dim")
        else:
            text.append_text(headline(t))
            if where := whereabouts(t):
                text.append("\n")
                text.append_text(where)
            if t.vehicle is not None:
                text.append("\nEnter para ver la ruta y las paradas", "dim")
        self.query_one("#detail-text", Static).update(text)

    # --- acciones -------------------------------------------------------------

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "reload":
            return self.phase != "loading"
        return True

    @on(Button.Pressed, "#open")
    def action_open(self) -> None:
        t = self._current()
        if t is None:
            return
        target = TrackTarget.from_leg(t.leg, self.origin.id, self.destination.id)
        if t.checked:
            self.app.push_screen(TrackingScreen(target, t, self.checked_at))
        else:
            self.app.push_screen(TrackingScreen(target))

    def action_reload(self) -> None:
        if self.phase == "list":
            self.query_one("#summary", Static).update(Text("Actualizando…", "dim italic"))
            self._fetch()
        else:
            self.search()

    @on(Button.Pressed, "#new")
    def action_back(self) -> None:
        self.workers.cancel_group(self, "live")
        self.dismiss()
