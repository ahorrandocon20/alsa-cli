"""CLI de alsa-cli: busca buses de Alsa, sin cuenta."""

from __future__ import annotations

import json
import sys
from datetime import datetime

import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from alsa.core import MADRID_TZ, MAX_ADULTS, get_station_id, get_stations, search_bus
from alsa.exceptions import AlsaError
from alsa.models import Journey

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
        price_text = f"{j.cheapest_price:.2f} €" if j.fares else "sin precio"
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
