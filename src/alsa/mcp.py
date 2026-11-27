"""Servidor MCP: expone alsa-cli a asistentes sin terminal.

Dos herramientas, con el mismo núcleo que el CLI: `search_bus` (alsa.core.search_bus)
y `track_bus` (alsa.tracking.track_route).
"""

from __future__ import annotations

import sys

try:
    from fastmcp import FastMCP
except ImportError:  # instalado sin el extra mcp
    sys.exit(
        "alsa-mcp necesita el extra mcp. Reinstala con:\n"
        '  uv tool install --force "alsa-cli[mcp] @ git+https://github.com/ahorrandocon20/alsa-cli"'
    )

from datetime import datetime

from alsa.cli import (
    _check_adults,
    _check_date,
    _check_time,
    _missing_departure,
    tracking_payload,
)
from alsa.core import MADRID_TZ, get_station_id
from alsa.core import search_bus as _search_bus_core
from alsa.tracking import track_route

mcp = FastMCP("alsa-cli")


@mcp.tool
def search_bus(
    origin: str,
    destination: str,
    date: str,
    adults: int = 1,
) -> dict:
    """Busca buses de Alsa entre dos estaciones en una fecha, con horarios y tarifas.

    Args:
        origin: nombre o ID de la estación de salida (p. ej. "Madrid" o "5555").
        destination: nombre o ID de la estación de llegada (p. ej. "Toledo" o "1690").
        date: fecha del viaje en formato DD/MM/YYYY, de hoy en adelante.
        adults: número de pasajeros adultos, de 1 a 20 (default 1).

    Returns:
        Payload JSON con la lista de buses ordenados por precio,
        cada uno con horarios, duración, transbordos, tipo de servicio
        (Comfort, Supra…) y tarifas desglosadas. El campo shop.url abre
        en alsa.es la lista de buses de esta búsqueda para comprar. Un bloqueo de
        Imperva, una estación desconocida, una fecha mal escrita o pasada
        o un número de adultos fuera de rango devuelven error de herramienta,
        no un resultado vacío.
    """
    if error := _check_date(date) or _check_adults(adults):
        raise ValueError(error)
    origin_id = origin if origin.isdigit() else get_station_id(origin)
    destination_id = destination if destination.isdigit() else get_station_id(destination)
    journeys, retries, shop_url = _search_bus_core(origin_id, destination_id, date, adults)

    return {
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


@mcp.tool
def track_bus(
    origin: str,
    destination: str,
    departure_time: str | None = None,
) -> dict:
    """Dónde van hoy los autobuses de Alsa entre dos estaciones y cuánto retraso llevan.

    Usa la página «Localiza tu bus» de alsa.es: solo hay datos de hoy y de los autobuses
    que tienen seguimiento. Sin `departure_time` consulta los que van en ruta o salen en
    las próximas 3 horas; el resto vuelve con status "not_checked".

    Args:
        origin: nombre o ID de la estación de salida (p. ej. "Madrid" o "90155").
        destination: nombre o ID de la estación de llegada.
        departure_time: hora de salida del autobús, HH:MM, para consultar solo ese.

    Returns:
        El mismo JSON que `alsa track --format json`: por cada tramo, status (waiting,
        approaching, on_the_way, arrived, no_tracking…), delay_minutes, horas estimadas,
        posición GPS con enlace al mapa y paradas del recorrido. El retraso es una
        estimación con el GPS y el horario; si es negativo, el autocar espera en la parada.
    """
    if error := _check_time(departure_time):
        raise ValueError(error)
    now = datetime.now(MADRID_TZ)
    origin_id = origin if origin.isdigit() else get_station_id(origin)
    destination_id = destination if destination.isdigit() else get_station_id(destination)
    trackings, retries = track_route(origin_id, destination_id, departure_time, now=now)
    if error := _missing_departure(departure_time, trackings, origin, destination):
        raise ValueError(error)
    return tracking_payload(origin, destination, now, trackings, retries)


def main() -> None:
    mcp.run()
