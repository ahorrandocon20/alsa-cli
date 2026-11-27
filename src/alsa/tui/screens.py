"""Pantallas de la TUI: búsqueda, resultados y modales de calendario, compra y ayuda."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import ClassVar

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Center, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    ContentSwitcher,
    DataTable,
    Footer,
    Label,
    Select,
    Static,
)
from textual.worker import Worker, WorkerState

from alsa.core import MADRID_TZ, MAX_ADULTS, search_bus
from alsa.exceptions import AlsaError
from alsa.models import Journey
from alsa.tracking import CHECK_AFTER, CHECK_BEFORE
from alsa.tui import fmt
from alsa.tui.art import title_text
from alsa.tui.purchase import (
    BuyOutcome,
    BuyScreen,
    CheckoutScreen,
    GoToChrome,
    PurchaseScreen,
)
from alsa.tui.stations import StationOption
from alsa.tui.tracking import LiveRouteScreen, TrackingScreen, TrackTarget
from alsa.tui.widgets import (
    BusScene,
    Chip,
    MonthCalendar,
    StationField,
    Stepper,
    TimeSelect,
)

ANY_TIME = "any"
TIME_OPTIONS = [("a cualquier hora", ANY_TIME)] + [
    (f"{h:02d}:{m:02d}", f"{h:02d}:{m:02d}") for h in range(24) for m in (0, 30)
]
SORTS = [("hora de salida", "departure"), ("precio", "price"), ("duración", "duration")]


@dataclass(frozen=True)
class SearchRequest:
    origin: StationOption
    destination: StationOption
    day: date
    adults: int
    depart_after: str | None
    arrive_before: str | None

    @property
    def alsa_date(self) -> str:
        return self.day.strftime("%d/%m/%Y")


def _minutes(hhmm: str) -> int:
    hours, mins = hhmm.split(":")
    return int(hours) * 60 + int(mins)


def matches_time(journey: Journey, req: SearchRequest) -> bool:
    """Filtro horario: sale desde `depart_after` y llega antes de `arrive_before` (mismo día)."""
    dep = journey.departure.hour * 60 + journey.departure.minute
    if req.depart_after and dep < _minutes(req.depart_after):
        return False
    if req.arrive_before:
        if journey.arrival.date() > journey.departure.date():
            return False
        arr = journey.arrival.hour * 60 + journey.arrival.minute
        if arr > _minutes(req.arrive_before):
            return False
    return True


def led_sign(destination: str) -> Text:
    """Letrero de destino ámbar, como el de la frente de un autocar."""
    letters = " ".join(destination.upper()[:22])
    return Text(f"  {letters}  ", style="bold #ffb547 on #1c1407")


def describe_filter(req: SearchRequest) -> str:
    parts = []
    if req.depart_after:
        parts.append(f"salen desde las {req.depart_after}")
    if req.arrive_before:
        parts.append(f"llegan antes de las {req.arrive_before}")
    return " y ".join(parts)


class SearchScreen(Screen):
    """Formulario: desde, hasta, día, hora y viajeros."""

    VERTICAL_BREAKPOINTS: ClassVar[list[tuple[int, str]]] = [(0, "-tiny"), (22, "-compact"), (24, "-roomy"), (30, "-short"), (35, "-tall")]
    HORIZONTAL_BREAKPOINTS: ClassVar[list[tuple[int, str]]] = [(0, "-narrow"), (78, "-wide")]

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+x", "swap", "intercambiar"),
        Binding("ctrl+l", "locate", "localizar autobús", priority=True),
        Binding("question_mark", "app.help", "ayuda", key_display="?", priority=True),
        Binding("f1", "app.help", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.day = date.today()
        self.adults = 1
        # El autocar solo entra al abrir la app o al volver de los resultados, no al cerrar un modal.
        self._bus_away = True

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="search-scroll"):
            with Center():
                yield Static(title_text(), id="title")
            with Center():
                yield Static("Horarios y precios de los autobuses de Alsa, en tu terminal", id="tagline")
            yield BusScene(id="scene")
            with Center(), Vertical(id="form"):
                with Horizontal(classes="row"):
                    yield Label("Desde", classes="field-label")
                    yield StationField(id="origin")
                    yield Button("⇄", id="swap", compact=True, tooltip="Intercambiar origen y destino (ctrl+x)")
                with Horizontal(classes="row"):
                    yield Label("Hasta", classes="field-label")
                    yield StationField(id="destination")
                    yield Static("", classes="swap-spacer")
                with Horizontal(classes="row", id="day-row"):
                    yield Label("Día", classes="field-label")
                    yield Chip("Hoy", id="day-today", classes="-selected")
                    yield Chip("Mañana", id="day-tomorrow")
                    yield Chip("Otro día…", id="day-other")
                    yield Static(fmt.long_date(self.day), id="day-label")
                with Horizontal(classes="row", id="time-row"):
                    yield Label("Hora", classes="field-label")
                    yield Label("Salir desde", classes="inline-label")
                    yield TimeSelect(TIME_OPTIONS, value=ANY_TIME, allow_blank=False, compact=True, id="depart-after")
                    yield Label("Llegar antes", classes="inline-label")
                    yield TimeSelect(TIME_OPTIONS, value=ANY_TIME, allow_blank=False, compact=True, id="arrive-before")
                with Horizontal(classes="row", id="pax-row"):
                    yield Label("Viajeros", classes="field-label")
                    yield Stepper("−", id="pax-minus")
                    yield Static(fmt.travellers(self.adults), id="pax-count")
                    yield Stepper("+", id="pax-plus")
                with Horizontal(classes="row", id="submit-row"):
                    yield Label("", classes="field-label")
                    yield Button("Buscar autobuses", id="search", variant="primary", compact=True)
                    yield Button("Localizar autobús", id="locate", compact=True, classes="link", tooltip="Dónde van hoy los autobuses de esta ruta (ctrl+l)")
                    yield Static("", id="form-error")
            with Center(id="links"), Horizontal(id="links-row"):
                yield Button("Ayuda", id="help", compact=True, classes="link")
                yield Button("Créditos", id="credits", compact=True, classes="link")
            yield Static("", id="stations-status")
        yield Footer(compact=True)

    def on_mount(self) -> None:
        self.query_one("#origin", StationField).input.focus()
        self.query_one("#swap", Button).can_focus = False
        self._set_adults(self.adults)
        self.refresh_stations_status()

    def on_screen_resume(self) -> None:
        if self._bus_away:
            self._bus_away = False
            self.query_one(BusScene).arrive()

    def refresh_stations_status(self) -> None:
        status = self.query_one("#stations-status", Static)
        error = getattr(self.app, "stations_error", None)
        if error:
            status.update(Text.assemble(("No se pudieron cargar las paradas: ", "bold"), error, ("  ·  ctrl+r reintentar", "dim")))
            status.add_class("-error")
        elif getattr(self.app, "stations", None) is None:
            status.update(Text("Cargando las paradas de Alsa…", style="dim italic"))
            status.remove_class("-error")
        else:
            status.update("")
            status.remove_class("-error")
        status.set_class(not error and getattr(self.app, "stations", None) is not None, "-empty")
        for field in self.query(StationField):
            if field.input.has_focus:
                field.refresh_suggestions()

    # --- estaciones -----------------------------------------------------------

    @on(StationField.Picked)
    def _station_picked(self, event: StationField.Picked) -> None:
        self._clear_error()
        if event.field.id == "origin":
            destination = self.query_one("#destination", StationField)
            if destination.station is None:
                destination.input.focus()
                return
        self.query_one("#day-row .-selected", Chip).focus()

    def action_swap(self) -> None:
        origin = self.query_one("#origin", StationField)
        destination = self.query_one("#destination", StationField)
        a, b = origin.station, destination.station
        a_text, b_text = origin.input.value, destination.input.value
        origin.set_station(b)
        destination.set_station(a)
        if b is None:
            origin.input.value = b_text
        if a is None:
            destination.input.value = a_text

    # --- día, viajeros y botones ---------------------------------------------

    def _set_day(self, day: date) -> None:
        self.day = day
        today = date.today()
        active = {today: "day-today", today + timedelta(days=1): "day-tomorrow"}.get(day, "day-other")
        for chip in self.query(Chip):
            chip.set_class(chip.id == active, "-selected")
        self.query_one("#day-label", Static).update(fmt.long_date(day))

    def _set_adults(self, adults: int) -> None:
        self.adults = max(1, min(MAX_ADULTS, adults))
        self.query_one("#pax-count", Static).update(fmt.travellers(self.adults))
        self.query_one("#pax-minus", Button).disabled = self.adults == 1
        self.query_one("#pax-plus", Button).disabled = self.adults == MAX_ADULTS

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        today = date.today()
        if button_id == "day-today":
            self._set_day(today)
            self.query_one("#depart-after").focus()
        elif button_id == "day-tomorrow":
            self._set_day(today + timedelta(days=1))
            self.query_one("#depart-after").focus()
        elif button_id == "day-other":
            self.app.push_screen(CalendarScreen(self.day), self._calendar_closed)
        elif button_id == "pax-minus":
            self._set_adults(self.adults - 1)
        elif button_id == "pax-plus":
            self._set_adults(self.adults + 1)
        elif button_id == "swap":
            self.action_swap()
        elif button_id == "search":
            self.submit()
        elif button_id == "locate":
            self.action_locate()
        elif button_id == "help":
            self.run_action("app.help")
        elif button_id == "credits":
            self.app.push_screen(CreditsScreen())

    def _calendar_closed(self, day: date | None) -> None:
        if day is not None:
            self._set_day(day)
            self.query_one("#depart-after").focus()
        else:
            self.query_one("#day-other", Chip).focus()

    # --- envío ----------------------------------------------------------------

    def _show_error(self, message: str, field: StationField | None = None) -> None:
        self.query_one("#form-error", Static).update(message)
        if field is not None:
            field.add_class("-invalid")
            field.input.focus()

    def _clear_error(self) -> None:
        self.query_one("#form-error", Static).update("")

    def _picked_route(self) -> tuple[StationOption, StationOption] | None:
        """Salida y llegada elegidas, o None tras avisar en el formulario de lo que falta."""
        origin = self.query_one("#origin", StationField)
        destination = self.query_one("#destination", StationField)
        if origin.station is None:
            self._show_error("Elige la parada de salida", origin)
            return None
        if destination.station is None:
            self._show_error("Elige la parada de llegada", destination)
            return None
        if origin.station.id == destination.station.id:
            self._show_error("La salida y la llegada son la misma parada", destination)
            return None
        return origin.station, destination.station

    def _leave(self, screen: Screen) -> None:
        """El autocar sale de la escena (si hay sitio para verlo) y se abre `screen`."""
        self._clear_error()
        if self._bus_away:
            return
        self._bus_away = True
        if self.has_class("-tall"):
            self.query_one(BusScene).depart(lambda: self.app.push_screen(screen))
        else:
            self.app.push_screen(screen)

    def action_locate(self) -> None:
        """Autobuses de hoy en la ruta elegida, con dónde van; el día del formulario no cuenta."""
        route = self._picked_route()
        if route is not None:
            self._leave(LiveRouteScreen(*route))

    def submit(self) -> None:
        route = self._picked_route()
        if route is None:
            return
        origin_station, destination_station = route
        if self.day < date.today():
            self._set_day(date.today())
        depart = self.query_one("#depart-after", Select).value
        arrive = self.query_one("#arrive-before", Select).value
        req = SearchRequest(
            origin=origin_station,
            destination=destination_station,
            day=self.day,
            adults=self.adults,
            depart_after=None if depart == ANY_TIME else str(depart),
            arrive_before=None if arrive == ANY_TIME else str(arrive),
        )
        self._leave(ResultsScreen(req))


class CalendarScreen(ModalScreen[date | None]):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "dismiss_none", "cancelar")]

    def __init__(self, selected: date) -> None:
        super().__init__()
        self.selected = selected

    def compose(self) -> ComposeResult:
        with Vertical(id="calendar-box", classes="dialog"):
            yield Label("Elige el día", classes="dialog-title")
            yield MonthCalendar(self.selected, date.today())
            yield Static(
                "Flechas para moverte, RePág y AvPág para cambiar de mes, Enter para elegir.",
                classes="dialog-hint",
            )
            with Horizontal(classes="dialog-buttons"):
                yield Button("Elegir", id="pick", variant="primary", compact=True)
                yield Button("Cancelar", id="cancel", compact=True, classes="link")

    def on_mount(self) -> None:
        self.query_one(MonthCalendar).focus()

    @on(MonthCalendar.Picked)
    def _picked(self, event: MonthCalendar.Picked) -> None:
        self.dismiss(event.day)

    @on(Button.Pressed, "#pick")
    def _pick(self) -> None:
        self.dismiss(self.query_one(MonthCalendar).cursor)

    @on(Button.Pressed, "#cancel")
    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class BusTable(DataTable):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("enter", "select_cursor", "comprar")]

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "select_cursor":
            return getattr(self.screen, "phase", "list") == "list"
        return True


class ResultsScreen(Screen):
    """Lista de autobuses con su detalle; Enter abre la compra."""

    HORIZONTAL_BREAKPOINTS: ClassVar[list[tuple[int, str]]] = [(0, "-narrow"), (100, "-wide")]

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "back", "volver"),
        Binding("o", "sort", "ordenar"),
        Binding("a", "shift_day(-1)", "día anterior"),
        Binding("s", "shift_day(1)", "día siguiente"),
        Binding("t", "toggle_filter", "filtro de hora"),
        Binding("p", "toggle_sold_out", "agotados"),
        Binding("l", "track", "dónde va"),
        Binding("r", "reload", "actualizar", show=False),
        Binding("question_mark", "app.help", "ayuda", key_display="?"),
        Binding("f1", "app.help", show=False),
    ]

    def __init__(self, req: SearchRequest) -> None:
        super().__init__()
        self.req = req
        self.journeys: list[Journey] = []
        self.shown: list[Journey] = []
        self.sort_index = 0
        self.filter_on = bool(req.depart_after or req.arrive_before)
        self.hide_sold_out = False
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
                yield BusTable(id="table", cursor_type="row", zebra_stripes=True)
                with Horizontal(id="detail"):
                    yield Static(id="detail-text")
                    with Vertical(id="detail-actions"):
                        yield Button("Comprar en alsa.es", id="buy", variant="primary", compact=True)
                        yield Button("Nueva búsqueda", id="new", compact=True, classes="link")
        yield Footer(compact=True)

    def on_mount(self) -> None:
        self.call_after_refresh(self._render_route)
        self.set_interval(0.5, self._tick_loading)
        self.search()

    def _render_route(self) -> None:
        req = self.req
        route = Text.assemble((req.origin.label, "bold"), ("  →  ", "#40c8ec bold"), (req.destination.label, "bold"))
        self.query_one("#route", Static).update(route)
        narrow = self.has_class("-narrow")
        day = fmt.short_date(req.day) if narrow else fmt.long_date(req.day)
        separator = " · " if narrow else "  ·  "
        travellers = f"{separator}{fmt.travellers(req.adults)}"
        self.query_one("#route-day", Static).update(Text.assemble((day, "bold"), (travellers, "dim")))

    # --- búsqueda -------------------------------------------------------------

    def search(self) -> None:
        self.phase = "loading"
        self._started = time.monotonic()
        self.query_one(ContentSwitcher).current = "loading"
        self.query_one("#summary", Static).update("")
        self.query_one("#loader", BusScene).cruise()
        self._tick_loading()
        self.refresh_bindings()
        self._fetch(self.req)

    @work(thread=True, exclusive=True, group="search", exit_on_error=False)
    def _fetch(self, req: SearchRequest) -> tuple[SearchRequest, list[Journey]]:
        journeys, _retries, _url = search_bus(req.origin.id, req.destination.id, req.alsa_date, req.adults)
        return req, journeys

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.group != "search":
            return
        if event.state == WorkerState.SUCCESS:
            req, journeys = event.worker.result
            if req != self.req:
                return
            self.journeys = journeys
            self._show_results()
        elif event.state == WorkerState.ERROR:
            error = event.worker.error
            message = str(error) if isinstance(error, AlsaError) else f"Error inesperado: {error}"
            self._show_message("No se pudo completar la búsqueda", message, "r para reintentar  ·  esc para cambiar la búsqueda", error=True)

    def _tick_loading(self) -> None:
        if self.phase != "loading":
            return
        elapsed = int(time.monotonic() - self._started)
        dots = "." * (1 + elapsed % 3)
        text = Text.assemble(
            led_sign(self.req.destination.label.removesuffix(" (todas)")),
            "\n\n",
            ("Buscando autobuses", "bold"),
            (dots.ljust(3), "bold #40c8ec"),
            (f"\n\nPreguntando a alsa.es por {fmt.long_date(self.req.day)}", "dim"),
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

    def _show_results(self, keep: Journey | None = None) -> None:
        self.query_one("#loader", BusScene).stop()
        if not self.journeys:
            self.query_one("#summary", Static).update("")
            self._show_message(
                "No hay autobuses de Alsa para ese día",
                f"Alsa no tiene plazas a la venta de {self.req.origin.label} a {self.req.destination.label} "
                f"el {fmt.long_date(self.req.day)}.",
                "a / s para probar el día anterior o el siguiente  ·  esc para cambiar la búsqueda",
            )
            return
        in_time = [j for j in self.journeys if not self.filter_on or matches_time(j, self.req)]
        self.shown = [j for j in in_time if j.available or not self.hide_sold_out]
        if in_time and not self.shown:
            self._show_message(
                "Todos los autobuses que encajan están agotados",
                f"Hay {len(in_time)} autobuses, pero Alsa no tiene plazas en ninguno.",
                "p para verlos igualmente  ·  a / s para probar otro día  ·  esc para cambiar la búsqueda",
            )
            return
        if not self.shown:
            self._show_message(
                "Ningún autobús encaja con la hora",
                f"Hay {len(self.journeys)} autobuses ese día, pero ninguno {describe_filter(self.req)}.",
                "t para ver todas las horas  ·  esc para cambiar la búsqueda",
            )
            return
        self.phase = "list"
        key = SORTS[self.sort_index][1]
        sorters = {
            "departure": lambda j: j.departure,
            "price": lambda j: (not j.available, j.cheapest_price, j.departure),
            "duration": lambda j: (not j.available, j.duration_minutes, j.departure),
        }
        self.shown.sort(key=sorters[key])
        self._fill_table(keep)
        self._render_summary()
        self.query_one(ContentSwitcher).current = "list"
        self.query_one(DataTable).focus()
        self.refresh_bindings()

    def _render_summary(self) -> None:
        available = [j for j in self.shown if j.available]
        shown = len(self.shown)
        summary = Text(f"{shown} autobús" if shown == 1 else f"{shown} autobuses", style="bold")
        if self.filter_on:
            summary.append(f" que {describe_filter(self.req)}", "bold")
        sold_out = shown - len(available)
        if sold_out:
            summary.append(f"  ·  {sold_out} agotado" + ("s" if sold_out > 1 else ""), "#8b95a7")
        if available:
            summary.append("  ·  desde ")
            summary.append(fmt.price(min(j.cheapest_price for j in available)), "bold #4ade80")
        else:
            summary.append("  ·  sin plazas: prueba otro día", "bold #fbbf24")
        summary.append(f"  ·  por {SORTS[self.sort_index][0]}", "dim")
        self.query_one("#summary", Static).update(summary)

    def _fill_table(self, keep: Journey | None = None) -> None:
        table = self.query_one(DataTable)
        # En terminales estrechos: sin columna de trayecto y con la parada recortada.
        compact = self.has_class("-narrow")
        self._compact = compact
        table.clear(columns=True)
        price_label = "Precio" if self.req.adults == 1 else f"Precio ({self.req.adults})"
        table.add_column("Sale", key="dep")
        table.add_column("Llega", key="arr")
        table.add_column("Duración", key="dur")
        table.add_column("Sale de", key="from", width=16 if compact else None)
        table.add_column("Servicio", key="svc")
        if not compact:
            table.add_column("Trayecto", key="hops")
        table.add_column(price_label, key="price")
        available = [j for j in self.shown if j.available] or self.shown
        cheapest = min(j.cheapest_price for j in available)
        fastest = min(j.duration_minutes for j in available)
        for i, j in enumerate(self.shown):
            if not j.available:
                table.add_row(*self._sold_out_cells(j, compact), key=str(i))
                continue
            supra = "supra" in j.service_type.lower()
            price_text = Text(fmt.price(j.cheapest_price), style="bold #4ade80" if j.cheapest_price == cheapest else "")
            if j.cheapest_price == cheapest:
                price_text.append(" ★", style="#4ade80")
            duration = Text(fmt.duration(j.duration_minutes), style="#40c8ec" if j.duration_minutes == fastest else "")
            if compact and j.transfers:
                duration.append(" ⇄", style="#fbbf24")
            cells = [
                Text(fmt.hm(j.departure), style="bold"),
                Text(fmt.arrival(j.departure, j.arrival)),
                duration,
                Text(j.origin_name, overflow="ellipsis", no_wrap=True),
                Text(j.service_type or "—", style="bold #40c8ec" if supra else ""),
            ]
            if not compact:
                cells.append(Text(fmt.transfers(j.transfers), style="#fbbf24" if j.transfers else "dim"))
            cells.append(price_text)
            table.add_row(*cells, key=str(i))
        row = self.shown.index(keep) if keep in self.shown else 0
        table.move_cursor(row=row)
        self._render_detail(row)

    def _sold_out_cells(self, j: Journey, compact: bool) -> list[Text]:
        dim = "#5b6478"
        cells = [
            Text(fmt.hm(j.departure), style=dim),
            Text(fmt.arrival(j.departure, j.arrival), style=dim),
            Text(fmt.duration(j.duration_minutes), style=dim),
            Text(j.origin_name, style=dim, overflow="ellipsis", no_wrap=True),
            Text(j.service_type or "—", style=dim),
        ]
        if not compact:
            cells.append(Text(fmt.transfers(j.transfers), style=dim))
        cells.append(Text("agotado", style="italic #5b6478"))
        return cells

    def on_resize(self) -> None:
        self.call_after_refresh(self._relayout)

    def _relayout(self) -> None:
        self._render_route()
        if self.phase == "list" and self._compact is not None and self._compact != self.has_class("-narrow"):
            self._fill_table(keep=self._current())

    def _current(self) -> Journey | None:
        row = self.query_one(DataTable).cursor_row
        return self.shown[row] if self.phase == "list" and 0 <= row < len(self.shown) else None

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._render_detail(event.cursor_row)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_buy()

    def _render_detail(self, row: int) -> None:
        if not 0 <= row < len(self.shown):
            return
        j = self.shown[row]
        text = Text()
        text.append(f"{fmt.hm(j.departure)}  ", "bold")
        text.append(j.origin_name)
        text.append("  →  ", "#40c8ec bold")
        text.append(f"{fmt.arrival(j.departure, j.arrival)}  ", "bold")
        text.append(j.destination_name)
        text.append("\n")
        text.append(f"{fmt.duration(j.duration_minutes)}  ·  {fmt.transfers(j.transfers)}  ·  {j.service_type or 'autobús'}", "dim")
        if j.line_name and j.line_name != j.service_type:
            text.append(f"  ·  línea {j.line_name}", "dim")
        text.append("\n\n")
        if not j.available:
            text.append("Agotado: ", "bold #fbbf24")
            text.append("Alsa no tiene plazas en este autobús. Elige otro o prueba otro día con a / s.", "dim")
        else:
            text.append("Desde ", "dim")
            text.append(fmt.price(j.cheapest_price), "bold #4ade80")
            if self.req.adults > 1:
                text.append(f"  ({fmt.price(j.cheapest_price / self.req.adults)} por viajero)", "dim")
            text.append("   Enter para elegir tarifa y pagar", "dim")
        if self._trackable(j):
            text.append("\n")
            if j.departure > datetime.now(MADRID_TZ):
                text.append("Sale pronto: ", "bold #40c8ec")
                text.append("l para ver por dónde viene y si llega tarde", "dim")
            else:
                text.append("En ruta ahora: ", "bold #40c8ec")
                text.append("l para ver dónde va y si lleva retraso", "dim")
        self.query_one("#detail-text", Static).update(text)
        self.query_one("#buy", Button).disabled = not j.available

    def _trackable(self, j: Journey) -> bool:
        """Sale en las próximas 3 horas o va en ruta: su autocar puede estar ya de camino."""
        if self.req.day != date.today():
            return False
        now = datetime.now(MADRID_TZ)
        return j.departure - CHECK_BEFORE <= now <= j.arrival + CHECK_AFTER

    # --- acciones -------------------------------------------------------------

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in ("buy", "sort"):
            return self.phase == "list"
        if action == "track":
            return self.phase == "list" and self.req.day == date.today()
        if action == "toggle_sold_out":
            return self.phase != "loading" and any(not j.available for j in self.journeys)
        if action == "toggle_filter":
            return bool(self.req.depart_after or self.req.arrive_before) and self.phase != "loading"
        if action == "reload":
            return self.phase != "loading"
        if action == "shift_day" and parameters and parameters[0] == -1:
            return self.req.day > date.today()
        return True

    def action_back(self) -> None:
        self.workers.cancel_group(self, "search")
        self.dismiss()

    def action_buy(self) -> None:
        table = self.query_one(DataTable)
        if self.phase != "list" or not 0 <= table.cursor_row < len(self.shown):
            return
        journey = self.shown[table.cursor_row]
        if not journey.available:
            self.notify("Ese autobús está agotado; elige otro.", severity="warning", timeout=2.5)
            return

        def finished(outcome: BuyOutcome | None) -> None:
            if outcome is not None:
                self.app.push_screen(PurchaseScreen(journey, self.req, outcome), self._purchase_closed)

        def chosen(choice: GoToChrome | BuyOutcome | None) -> None:
            if isinstance(choice, GoToChrome):
                self.app.push_screen(CheckoutScreen(journey, self.req, choice.package), finished)
            else:
                finished(choice)

        self.app.push_screen(BuyScreen(journey, self.req), chosen)

    def action_track(self) -> None:
        journey = self._current()
        if journey is not None and self.req.day == date.today():
            self.app.push_screen(TrackingScreen(TrackTarget.from_journey(journey, self.req)))

    def _purchase_closed(self, result: str | None) -> None:
        if result == "new":
            self.dismiss()
        elif result == "quit":
            self.app.exit()

    def action_sort(self) -> None:
        self.sort_index = (self.sort_index + 1) % len(SORTS)
        self._show_results(keep=self._current())
        self.notify(f"Ordenados por {SORTS[self.sort_index][0]}", timeout=1.5)

    def action_shift_day(self, days: int) -> None:
        new_day = self.req.day + timedelta(days=days)
        if new_day < date.today():
            return
        self.req = replace(self.req, day=new_day)
        self._render_route()
        self.search()

    def action_toggle_filter(self) -> None:
        self.filter_on = not self.filter_on
        if self.journeys:
            self._show_results(keep=self._current())
        self.notify("Mostrando todas las horas" if not self.filter_on else f"Solo los que {describe_filter(self.req)}", timeout=2)

    def action_toggle_sold_out(self) -> None:
        self.hide_sold_out = not self.hide_sold_out
        if self.journeys:
            self._show_results(keep=self._current())
        self.notify("Ocultando los agotados" if self.hide_sold_out else "Mostrando también los agotados", timeout=2)

    def action_reload(self) -> None:
        self.search()

    @on(Button.Pressed, "#buy")
    def _buy_pressed(self) -> None:
        self.action_buy()

    @on(Button.Pressed, "#new")
    def _new_pressed(self) -> None:
        self.action_back()


# ctrl+q y la tecla de ayuda ya van siempre en la barra de abajo.
HELP = {
    "Formulario": [
        ("Escribe y ↑ ↓", "elige parada de la lista"),
        ("Enter / Tab", "acepta la parada y sigue"),
        ("ctrl+x", "intercambia origen y destino"),
        ("ctrl+l", "localiza los autobuses de hoy"),
        ("esc", "cierra la lista de paradas"),
        ("← →", "Hoy, Mañana u Otro día"),
        ("Tab / Enter", "hora y viajeros (− +)"),
    ],
    "Resultados": [
        ("↑ ↓", "recorre los autobuses"),
        ("Enter", "elige tarifa y abre la compra"),
        ("o", "ordena: hora, precio, duración"),
        ("a / s", "día anterior / siguiente"),
        ("t", "filtro de hora (si lo pusiste)"),
        ("p", "oculta agotados (si los hay)"),
        ("l", "dónde va el autobús (hoy)"),
        ("r", "vuelve a buscar"),
        ("esc", "vuelve a la búsqueda"),
    ],
}


def _help_section(title: str) -> Text:
    text = Text()
    text.append(f"{title}\n", "bold #40c8ec")
    for key, desc in HELP[title]:
        text.append(f"  {key:<14}", "bold")
        text.append(f"{desc}\n")
    text.rstrip()
    return text


class HelpScreen(ModalScreen[None]):
    HORIZONTAL_BREAKPOINTS: ClassVar[list[tuple[int, str]]] = [(0, "-narrow"), (110, "-wide")]
    VERTICAL_BREAKPOINTS: ClassVar[list[tuple[int, str]]] = [(0, "-short"), (33, "-tall")]
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "cerrar"),
        Binding("f1", "close", show=False),
        Binding("question_mark", "close", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box", classes="dialog"):
            yield Label("Ayuda", classes="dialog-title")
            with Horizontal(id="help-cols"):
                yield Static(_help_section("Formulario"), classes="help-col")
                yield Static(_help_section("Resultados"), classes="help-col")
            yield Static(
                Text("Al comprar, Chrome se abre en alsa.es con el autobús y la tarifa elegidos; los datos y el pago se hacen allí.", "dim"),
                id="help-note",
                classes="dialog-text",
            )
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cerrar", id="close", variant="primary", compact=True)

    @on(Button.Pressed, "#close")
    def action_close(self) -> None:
        self.dismiss()


class CreditsScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "close", "cerrar")]

    def compose(self) -> ComposeResult:
        text = Text.assemble(
            ("alsa-cli", "bold"),
            " · MIT\n\n",
            "Proyecto no oficial: no está afiliado, respaldado ni soportado por Alsa. ",
            "Habla con la web pública de alsa.es igual que lo hace tu navegador, sin cuenta ni API key.\n\n",
            ("Inspirado en renfe-cli.\n", "dim"),
            ("Alsa es una marca de sus respectivos propietarios.", "dim"),
        )
        with Vertical(id="credits-box", classes="dialog"):
            yield Label("Créditos", classes="dialog-title")
            yield Static(text, classes="dialog-text")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cerrar", id="close", variant="primary", compact=True)

    @on(Button.Pressed, "#close")
    def action_close(self) -> None:
        self.dismiss()

