"""Widgets propios de la TUI: escena del autocar, campo de estación y calendario."""

from __future__ import annotations

import calendar
import time
from collections.abc import Callable
from datetime import date, timedelta
from typing import ClassVar

from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.events import Click, DescendantBlur
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Button, Input, OptionList
from textual.widgets.option_list import Option

from alsa.tui import fmt
from alsa.tui.art import BUS_WIDTH, SCENE_HEIGHT, scene_text
from alsa.tui.stations import StationOption

FPS = 30


class BusScene(Widget):
    """Skyline con el autocar: entra, sale o circula mientras se busca."""

    DEFAULT_CSS = f"""
    BusScene {{
        height: {SCENE_HEIGHT // 2};
        width: 1fr;
    }}
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._x: float | None = None
        self._frame = 0
        self._scroll = 0
        self._bounce = 0
        self._cruising = False
        self._anim: tuple[float, float, float, float, Callable[[float], float], Callable[[], None] | None] | None = None

    def on_mount(self) -> None:
        self._timer = self.set_interval(1 / FPS, self._tick, pause=True)

    @property
    def _center(self) -> int:
        return max(0, (self.size.width - BUS_WIDTH) // 2)

    def render(self) -> Text:
        x = self._center if self._x is None else round(self._x)
        return scene_text(self.size.width, x, self._frame, self._scroll, self._bounce, self.size.height * 2 or SCENE_HEIGHT)

    def arrive(self) -> None:
        """El autocar entra por la derecha y frena en el centro."""
        self._cruising = False
        start = float(self.size.width or 120)
        self._move(start, self._center, 1.1, lambda t: 1 - (1 - t) ** 3)

    def depart(self, on_done: Callable[[], None]) -> None:
        """El autocar arranca hacia la izquierda y sale de la escena."""
        self._cruising = False
        start = float(self._center if self._x is None else self._x)
        self._move(start, -BUS_WIDTH - 2, 0.55, lambda t: t**2, on_done)

    def cruise(self) -> None:
        """Autocar quieto en el centro con la ciudad pasando por detrás."""
        self._anim = None
        self._x = None
        self._cruising = True
        self._timer.resume()

    def stop(self) -> None:
        self._cruising = False
        self._anim = None
        self._x = None
        self._bounce = 0
        self._timer.pause()
        self.refresh()

    def _move(self, start: float, end: float, seconds: float, ease: Callable[[float], float], on_done: Callable[[], None] | None = None) -> None:
        self._x = start
        self._anim = (start, end, time.monotonic(), seconds, ease, on_done)
        self._timer.resume()

    def _tick(self) -> None:
        self._frame += 1
        if self._anim is not None:
            start, end, t0, seconds, ease, on_done = self._anim
            t = min(1.0, (time.monotonic() - t0) / seconds)
            self._x = start + (end - start) * ease(t)
            if t >= 1.0:
                self._anim = None
                self._x = None if on_done is None else self._x
                if not self._cruising:
                    self._timer.pause()
                if on_done is not None:
                    on_done()
        elif self._cruising:
            self._scroll += 1
            self._bounce = 1 if self._frame % 18 in (0, 1, 2) else 0
        self.refresh()


class Suggestions(OptionList, can_focus=False):
    """Desplegable de estaciones; el foco se queda en el Input."""


class _StationInput(Input):
    def check_consume_key(self, key: str, character: str | None) -> bool:
        # ? no se escribe: abre la ayuda, como en el resto de la TUI
        return character != "?" and super().check_consume_key(key, character)


class StationField(Vertical):
    """Input con sugerencias de estaciones de Alsa mientras escribes."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("down", "move(1)", "siguiente", show=False),
        Binding("up", "move(-1)", "anterior", show=False),
        Binding("escape", "close", "cerrar", show=False),
    ]

    class Picked(Message):
        def __init__(self, field: StationField, station: StationOption) -> None:
            super().__init__()
            self.field = field
            self.station = station

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.station: StationOption | None = None
        self._options: list[StationOption] = []

    def compose(self) -> ComposeResult:
        yield _StationInput(placeholder="ciudad o parada", compact=True)
        yield Suggestions()

    @property
    def input(self) -> Input:
        return self.query_one(Input)

    @property
    def suggestions(self) -> Suggestions:
        return self.query_one(Suggestions)

    @property
    def is_open(self) -> bool:
        return self.suggestions.has_class("-open")

    def set_station(self, station: StationOption | None) -> None:
        self.station = station
        self.set_class(station is not None, "-picked")
        self.remove_class("-invalid")
        self.input.value = station.label if station else ""
        self.input.cursor_position = len(self.input.value)
        self._close()

    def refresh_suggestions(self) -> None:
        query = self.input.value.strip()
        if not query or self.station is not None:
            self._close()
            return
        index = getattr(self.app, "stations", None)
        listing = self.suggestions
        listing.clear_options()
        if index is None:
            self._options = []
            message = getattr(self.app, "stations_error", None) or "Cargando las paradas de Alsa…"
            listing.add_option(Option(Text(message, style="italic"), disabled=True))
        else:
            self._options = index.search(query)
            if self._options:
                listing.add_options([Option(_station_prompt(st)) for st in self._options])
                listing.highlighted = 0
            else:
                listing.add_option(Option(Text(f"Ninguna parada de Alsa coincide con «{query}»", style="italic"), disabled=True))
        listing.add_class("-open")

    def _close(self) -> None:
        self.suggestions.remove_class("-open")

    def _pick(self, index: int) -> None:
        if not 0 <= index < len(self._options):
            return
        station = self._options[index]
        self.set_station(station)
        self.post_message(self.Picked(self, station))

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        if self.station is not None and event.value == self.station.label:
            return
        self.station = None
        self.remove_class("-picked")
        self.remove_class("-invalid")
        if self.input.has_focus:
            self.refresh_suggestions()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if self.is_open and self._options:
            self._pick(self.suggestions.highlighted or 0)
        elif self.station is not None:
            self.post_message(self.Picked(self, self.station))
        else:
            self.refresh_suggestions()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self._pick(event.option_index)

    def on_descendant_blur(self, event: DescendantBlur) -> None:
        # Al salir con Tab se queda la sugerencia resaltada, como en la web de Alsa.
        if self.station is None and self.is_open and self._options:
            highlighted = self.suggestions.highlighted or 0
            self.set_station(self._options[highlighted])
        self._close()

    def action_move(self, step: int) -> None:
        if not self.is_open:
            self.refresh_suggestions()
            return
        if self._options:
            current = self.suggestions.highlighted or 0
            self.suggestions.highlighted = (current + step) % len(self._options)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # Esc solo cierra el desplegable; si está cerrado, la tecla sigue hacia la pantalla.
        return self.is_open if action == "close" else True

    def action_close(self) -> None:
        self._close()


def _station_prompt(st: StationOption) -> Text:
    text = Text()
    if st.is_airport:
        text.append("✈ ", style="bold #40c8ec")
    if st.is_group:
        text.append(st.label.removesuffix(" (todas)"), style="bold")
        text.append("  todas las paradas", style="#40c8ec")
    else:
        text.append(st.name)
    if st.province:
        text.append(f"  {st.province}", style="dim")
    return text


class Chip(Button):
    """Botón compacto que se recorre con ← y →."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("left", "app.focus_previous", show=False),
        Binding("right", "app.focus_next", show=False),
    ]

    def __init__(self, label: str, *, id: str | None = None, classes: str = "") -> None:
        super().__init__(label, id=id, classes=f"chip {classes}".strip(), compact=True)


class MonthCalendar(Widget, can_focus=True):
    """Calendario de un mes navegable con teclado y ratón."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("left", "move(-1)", show=False),
        Binding("right", "move(1)", show=False),
        Binding("up", "move(-7)", show=False),
        Binding("down", "move(7)", show=False),
        Binding("pageup", "month(-1)", "mes anterior"),
        Binding("pagedown", "month(1)", "mes siguiente"),
        Binding("home", "today", "hoy", key_display="Inicio"),
        Binding("enter", "pick", "elegir"),
    ]

    DEFAULT_CSS = """
    MonthCalendar {
        width: 30;
        height: 9;
    }
    """

    cursor: reactive[date] = reactive(date.today)

    class Picked(Message):
        def __init__(self, day: date) -> None:
            super().__init__()
            self.day = day

    def __init__(self, selected: date, min_date: date, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.selected = selected
        self.min_date = min_date
        self.set_reactive(MonthCalendar.cursor, max(selected, min_date))

    def render(self) -> Text:
        cursor = self.cursor
        text = Text(no_wrap=True)
        can_go_back = (cursor.year, cursor.month) > (self.min_date.year, self.min_date.month)
        prev_style = Style(color="#40c8ec", bold=True) if can_go_back else Style(color="#3b4456")
        text.append(" ‹ ", prev_style + Style.from_meta({"month": -1}))
        title = f"{fmt.MONTHS[cursor.month - 1]} {cursor.year}"
        text.append(title.center(22), Style(bold=True))
        text.append(" › \n", Style(color="#40c8ec", bold=True) + Style.from_meta({"month": 1}))
        text.append(" lu  ma  mi  ju  vi  sá  do\n", Style(color="#7b8496"))
        weeks = calendar.Calendar().monthdatescalendar(cursor.year, cursor.month)
        today = date.today()
        for week in weeks:
            for day in week:
                if day.month != cursor.month:
                    text.append("    ")
                    continue
                style = Style()
                if day < self.min_date:
                    style = Style(color="#3b4456")
                elif day == cursor:
                    style = Style(color="#0b1220", bgcolor="#40c8ec", bold=True)
                elif day == self.selected:
                    style = Style(color="#40c8ec", bold=True, underline=True)
                elif day == today:
                    style = Style(bold=True)
                if day >= self.min_date:
                    style += Style.from_meta({"day": day.toordinal()})
                text.append(f" {day.day:>2} ", style)
            text.append("\n")
        return text

    def on_click(self, event: Click) -> None:
        meta = event.style.meta
        if "month" in meta:
            self.action_month(meta["month"])
        elif "day" in meta:
            self.cursor = date.fromordinal(meta["day"])
            self.action_pick()

    def action_move(self, days: int) -> None:
        self.cursor = max(self.min_date, self.cursor + timedelta(days=days))

    def action_month(self, step: int) -> None:
        year, month = divmod(self.cursor.year * 12 + self.cursor.month - 1 + step, 12)
        last = calendar.monthrange(year, month + 1)[1]
        self.cursor = max(self.min_date, date(year, month + 1, min(self.cursor.day, last)))

    def action_today(self) -> None:
        self.cursor = self.min_date

    def action_pick(self) -> None:
        self.post_message(self.Picked(self.cursor))
