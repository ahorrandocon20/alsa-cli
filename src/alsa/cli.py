"""CLI de alsa-cli: busca buses de Alsa, sin cuenta."""

from __future__ import annotations

import json
import re
import sys
import time
from collections.abc import Callable
from datetime import datetime

import typer
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from alsa.core import MADRID_TZ, MAX_ADULTS, get_station_id, get_stations, search_bus
from alsa.exceptions import AlsaError
from alsa.models import BusTracking, Journey, map_url
from alsa.tracking import track_route

console = Console()

app = typer.Typer(add_completion=False)


@app.callback(invoke_without_command=True)
def root(ctx: typer.Context) -> None:
    """Busca buses de Alsa. Sin argumentos, en un terminal, abre la TUI."""
    if ctx.invoked_subcommand is not None:
        return
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(ctx.get_help())
        return
    from alsa.tui.app import run

    run()


def _fail(message: str) -> None:
    print(json.dumps({"error": {"message": message}}, ensure_ascii=False), file=sys.stderr)
    raise typer.Exit(code=1)


def _hm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def _check_date(value: str) -> str | None:
    try:
        day = datetime.strptime(value, "%d/%m/%Y").date()
    except ValueError:
        return f"fecha inválida {value!r}: se espera DD/MM/YYYY"
    # con una fecha pasada alsa.es devuelve los buses de hoy
    if day < datetime.now(MADRID_TZ).date():
        return f"la fecha {value} ya ha pasado: busca desde hoy en adelante"
    return None


def _check_adults(value: int) -> str | None:
    if not 1 <= value <= MAX_ADULTS:
        return f"número de adultos inválido ({value}): alsa.es admite de 1 a {MAX_ADULTS} por reserva"
    return None


@app.command()
def bus(
    origin: str = typer.Argument(metavar="ORIGEN", help="Nombre o ID de la estación de salida"),
    destination: str = typer.Argument(metavar="DESTINO", help="Nombre o ID de la estación de llegada"),
    date: str = typer.Option(..., "--date", "-d", help="Fecha del viaje, DD/MM/YYYY"),
    adults: int = typer.Option(1, "--adults", "-a", help=f"Número de adultos, de 1 a {MAX_ADULTS}"),
    output_format: str = typer.Option("text", "--format", help="Salida: text o json"),
) -> None:
    """Busca buses de Alsa entre dos estaciones en una fecha, con tarifas."""
    if error := _check_date(date):
        _fail(error)
    if error := _check_adults(adults):
        _fail(error)
    if output_format not in ("text", "json"):
        _fail(f"formato desconocido {output_format!r}: usa text o json")

    try:
        origin_id = origin if origin.isdigit() else get_station_id(origin)
        destination_id = destination if destination.isdigit() else get_station_id(destination)
        journeys, retries, shop_url = search_bus(origin_id, destination_id, date, adults)
    except AlsaError as e:
        _fail(str(e))

    if output_format == "json":
        payload = {
            "origin": origin,
            "destination": destination,
            "date": date,
            "adults": adults,
            "count": len(journeys),
            "buses": [j.to_dict() for j in journeys],
            "shop": {
                "url": shop_url,
                "prefilled": ["origin", "destination", "date", "adults"],
                "next": "La URL abre en alsa.es la lista de buses de esta búsqueda; elige bus y tarifa allí para comprar.",
            },
            "retries": retries,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    _print_text(origin, destination, date, journeys, shop_url, retries)


@app.command()
def stations(
    query: str = typer.Argument(help="Texto para buscar estaciones"),
) -> None:
    """Busca estaciones de Alsa por nombre (como mucho 20)."""
    try:
        results = get_stations(query)
    except AlsaError as e:
        _fail(str(e))

    payload = {
        "query": query,
        "count": len(results),
        "stations": results[:20],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _check_time(value: str | None) -> str | None:
    if value is None:
        return None
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
        return f"hora inválida {value!r}: se espera HH:MM, p. ej. 06:15"
    return None


WATCH_SECONDS = 60  # cada cuánto pregunta --watch a alsa.es


@app.command()
def track(
    origin: str = typer.Argument(metavar="ORIGEN", help="Nombre o ID de la estación de salida"),
    destination: str = typer.Argument(metavar="DESTINO", help="Nombre o ID de la estación de llegada"),
    departure: str | None = typer.Option(None, "--time", "-t", help="Hora de salida del autobús, HH:MM (por defecto, los que van en ruta)"),
    watch: bool = typer.Option(False, "--watch", "-w", help="Se queda abierto y se actualiza cada minuto; ctrl+c para salir"),
    output_format: str = typer.Option("text", "--format", help="Salida: text o json"),
) -> None:
    """Dónde van hoy los autobuses de Alsa entre dos estaciones, con su retraso estimado."""
    if error := _check_time(departure):
        _fail(error)
    if output_format not in ("text", "json"):
        _fail(f"formato desconocido {output_format!r}: usa text o json")
    if watch and output_format == "json":
        _fail("--watch solo funciona con --format text")

    now = datetime.now(MADRID_TZ)
    try:
        origin_id = origin if origin.isdigit() else get_station_id(origin)
        destination_id = destination if destination.isdigit() else get_station_id(destination)
        trackings, retries = track_route(origin_id, destination_id, departure, now=now)
    except AlsaError as e:
        _fail(str(e))

    if error := _missing_departure(departure, trackings, origin, destination):
        _fail(error)

    if output_format == "json":
        payload = tracking_payload(origin, destination, now, trackings, retries)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    detailed = departure is not None
    if not watch:
        console.print(_tracking_view(origin, destination, now, trackings, detailed))
        return

    def fetch() -> tuple[datetime, list[BusTracking]]:
        at = datetime.now(MADRID_TZ)
        return at, track_route(origin_id, destination_id, departure, now=at)[0]

    _watch(lambda at, found: _tracking_view(origin, destination, at, found, detailed), fetch, now, trackings)


def _watch(
    view: Callable[[datetime, list[BusTracking]], RenderableType],
    fetch: Callable[[], tuple[datetime, list[BusTracking]]],
    now: datetime,
    trackings: list[BusTracking],
) -> None:
    """Repinta la vista cada segundo con una cuenta atrás y vuelve a preguntar cada WATCH_SECONDS.

    Un error al actualizar no corta: se queda lo último y se avisa abajo. Al salir con
    ctrl+c, la última vista queda impresa en el terminal.
    """
    problem = ""

    def frame(remaining: int | None) -> Group:
        # Arriba: si hay muchos autobuses, la pantalla completa recorta lo de abajo.
        status = Text()
        if remaining is None:
            status.append("actualizando…", "dim italic")
        else:
            status.append(f"se actualiza en {remaining} s", "dim")
        status.append("  ·  ctrl+c para salir", "dim")
        if problem:
            status.append(f"\nno se pudo actualizar: {problem}", "#fbbf24")
        return Group(status, view(now, trackings))

    try:
        with Live(frame(WATCH_SECONDS), console=console, screen=True, auto_refresh=False) as live:
            while True:
                for remaining in range(WATCH_SECONDS, 0, -1):
                    live.update(frame(remaining), refresh=True)
                    time.sleep(1)
                live.update(frame(None), refresh=True)
                try:
                    now, trackings = fetch()
                    problem = ""
                except AlsaError as e:
                    problem = str(e)
    except KeyboardInterrupt:
        pass
    console.print(view(now, trackings))


def _missing_departure(departure: str | None, trackings: list[BusTracking], origin: str, destination: str) -> str | None:
    """Error si se pidió una hora y ningún autobús de hoy sale a esa hora."""
    if departure is None or any(t.checked for t in trackings):
        return None
    times = sorted({t.leg.departure.strftime("%H:%M") for t in trackings if t.leg.leg == 1})
    listed = f"salen a las {', '.join(times)}" if times else "Alsa no tiene ninguno con seguimiento hoy"
    return f"hoy no hay ningún autobús a las {departure} entre {origin} y {destination}: {listed}"


def tracking_payload(origin: str, destination: str, now: datetime, trackings: list[BusTracking], retries: list[dict]) -> dict:
    """JSON de `alsa track --format json`, el mismo que devuelve la herramienta MCP `track_bus`."""
    return {
        "origin": origin,
        "destination": destination,
        "date": now.strftime("%d/%m/%Y"),
        "checked_at": now.isoformat(timespec="seconds"),
        "count": len(trackings),
        "buses": [t.to_dict() for t in trackings],
        "note": "delay_minutes y las horas estimadas salen del GPS del autobús y su horario: son una estimación.",
        "retries": retries,
    }


def _tracking_view(origin: str, destination: str, now: datetime, trackings: list[BusTracking], detailed: bool) -> Group:
    """Lo que enseña `alsa track` en texto: un panel con la tira de la ruta por cada autobús localizado."""
    from alsa.tui import fmt
    from alsa.tui.tracking import headline, status_label, stops_grid, strip, whereabouts

    header = Text("alsa-cli ", "bold cyan")
    header.append(f"· {origin} → {destination} · hoy a las {_hm(now)} · en directo")
    parts: list[RenderableType] = [header]
    if not trackings:
        parts.append(Text("Alsa no tiene hoy autobuses con seguimiento en esa ruta.", "dim"))
        return Group(*parts)

    width = min(console.width, 100) - 4
    located = [t for t in trackings if t.vehicle is not None]
    for t in located:
        leg = t.leg
        title = Text(f"{_hm(leg.departure)} → {_hm(leg.arrival)}", "bold")
        if leg.legs > 1:
            title.append(f" · tramo {leg.leg} de {leg.legs}", "dim")
        title.append(f" · {leg.line_name or t.vehicle.line_name}", "dim")
        lines: list[RenderableType] = [strip(t, width), Text(""), headline(t)]
        if where := whereabouts(t):
            lines.append(where)
        if detailed:
            lines += [Text(""), stops_grid(t)]
        if t.vehicle.latitude is not None and t.vehicle.longitude is not None:
            lines.append(Text(f"mapa: {map_url(t.vehicle.latitude, t.vehicle.longitude)}", "dim"))
        parts.append(Panel(Group(*lines), title=title, title_align="left", border_style="cyan", width=width + 4))

    others = [t for t in trackings if t.vehicle is None and not (detailed and not t.checked)]
    if others:
        parts.append(Text("otros autobuses de hoy:" if located else "ningún autobús de esta ruta da su posición ahora mismo:", "dim"))
        for t in others:
            if t.status == "unmatched_route":
                reason = status_label(t, now).plain
            else:
                reason = "sin seguimiento en Alsa" if t.checked else "no consultado: usa --time " + _hm(t.leg.departure)
            parts.append(
                Text.assemble(
                    (f"  {_hm(t.leg.departure)} → {_hm(t.leg.arrival)}  ", "bold"), f"{fmt.place(t.leg.origin_name)}  ", (reason, "dim")
                )
            )
    if located:
        parts.append(Text("el retraso es una estimación con el GPS del autobús y su horario", "dim"))
    return Group(*parts)


def _print_text(
    origin: str,
    destination: str,
    date: str,
    journeys: list[Journey],
    shop_url: str,
    retries: list[dict],
) -> None:
    header = Text("alsa-cli ", "bold cyan")
    header.append(f"· {origin} → {destination} · {date}")
    console.print(header)

    if not journeys:
        console.print("[dim]Sin buses para esa fecha.[/dim]")
        return

    for i, j in enumerate(journeys, start=1):
        price_text = f"{j.cheapest_price:.2f} €"
        title = Text(f"{i} · {price_text}", "bold")
        service = j.service_type or "bus"
        title.append(f" · {service}", "dim")
        if j.transfers:
            title.append(f" · {j.transfers} transbordo" + ("s" if j.transfers > 1 else ""), "dim")
        if not j.available:
            title.append(" · agotado", "bold yellow")

        border = "green" if i == 1 else "none"
        lines = [
            Text(f"  {j.origin_name} → {j.destination_name}"),
            Text(f"  {_hm(j.departure)} → {_hm(j.arrival)} · {j.duration_minutes} min", "dim"),
        ]
        for fare in j.fares[:3]:
            fare_line = Text(f"  {fare.fare_name}: {fare.price:.2f} €", "dim")
            if fare.fare_conditions:
                fare_line.append(f"  ({fare.fare_conditions})", "dim")
            lines.append(fare_line)

        console.print(
            Panel(
                Text("\n").join(lines),
                title=title,
                title_align="left",
                border_style=border,
            )
        )

    console.print(Text(f"se muestran {len(journeys)} buses", "dim"))
    console.print(Text(f"comprar: {shop_url}", "dim"))
    if retries:
        console.print(Text(f"reintentos: {len(retries)}", "dim"))


def main() -> None:
    app()
