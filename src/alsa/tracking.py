"""Seguimiento en directo: por dónde va un autobús de Alsa y cuánto retraso lleva.

Flujo (el mismo que la página «Localiza tu bus» de alsa.es, sin cuenta):

1. GET a `/a-bordo/localiza-tu-bus` → cookies de Imperva y la URL de la acción
   `trackingBusTrayectoAction`, que ya lleva el token CSRF `p_auth`.
2. POST de esa acción con origen y destino → la página trae en `jsonTrackingBus` los
   buses de hoy en esa ruta (servicio y expedición de cada tramo) y la URL de
   `AjaxGetBusServiceTracking`.
3. POST a `AjaxGetBusServiceTracking` con servicio, expedición, fecha y paradas del
   tramo → posición GPS del autobús y todas las paradas de su recorrido completo, con
   horario. Vacío si ese autobús no tiene el servicio de seguimiento.

Alsa deja sin rellenar la hora estimada y las paradas visitadas (`estimada` y `visitada`
llegan a "0"), así que el retraso se estima aquí: se proyecta el GPS sobre la línea recta
entre paradas y se compara con la hora a la que el horario dice que pasaría por ese punto.
Es una aproximación: las carreteras no son rectas.
"""

from __future__ import annotations

import html
import json
import math
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from itertools import pairwise

from curl_cffi import requests as curl_requests

from alsa.core import BASE_URL, MADRID_TZ, _request_with_retries
from alsa.exceptions import AlsaParseError
from alsa.models import BusTracking, RouteStop, TrackedLeg, Vehicle

TRACKING_PAGE = f"{BASE_URL}/a-bordo/localiza-tu-bus"
TRACKING_NS = "_TrackingBusPortlet_WAR_Alsaportlet_"

# Qué buses se consultan si no se pide uno concreto: los que salen en las próximas
# horas o siguen en ruta. Un autobús puede venir de muy lejos antes de tu parada.
CHECK_BEFORE = timedelta(hours=3)
CHECK_AFTER = timedelta(minutes=30)

MAX_OFF_ROUTE_KM = 25.0  # más lejos de la ruta, la posición no sirve para estimar…
# …salvo en tramos largos sin paradas, donde la carretera se separa mucho de la recta entre
# ellas (Madrid-Pola de Lena por la A-6: hasta 47 km en 351 km). Ahí se admite hasta este
# porcentaje del tramo y el retraso se marca como aproximado.
OFF_ROUTE_SHARE = 0.2
MAX_DELAY_MINUTES = 240  # más retraso que esto es casi seguro un error de cálculo
TIME_WEIGHT_KM_PER_MIN = 0.1  # desempata tramos cercanos por cercanía al horario


# --- peticiones a alsa.es ----------------------------------------------------------


def _find_param(page: str, name: str) -> str | None:
    match = re.search(rf'params\["{name}"\] = "([^"]+)"', page)
    return html.unescape(match.group(1)) if match else None


def _parse_time(day: str, hhmm: str) -> datetime:
    """`26/11/2026` + `06:15` → datetime en hora de Madrid."""
    return datetime.strptime(f"{day} {hhmm}", "%d/%m/%Y %H:%M").replace(tzinfo=MADRID_TZ)


def _id(value: object) -> str:
    return "" if value is None else str(value)


def _parse_legs(page: str) -> list[TrackedLeg]:
    """Lee `jsonTrackingBus` de la página de resultados: un TrackedLeg por tramo."""
    match = re.search(r"params\[\"jsonTrackingBus\"\] = '(.*?)';\s*\n", page)
    if not match:
        return []
    try:
        trips = json.loads(match.group(1)).get("navegaliaList") or []
    except json.JSONDecodeError as e:
        raise AlsaParseError(f"JSON inválido en jsonTrackingBus: {e}") from e

    legs: list[TrackedLeg] = []
    for trip_index, trip in enumerate(trips):
        raw_legs = trip.get("legs") or []
        for leg_index, raw in enumerate(raw_legs):
            try:
                departure = _parse_time(raw["departureDate"], raw["departureTime"])
                arrival = _parse_time(raw["arrivalDate"], raw["arrivalTime"])
                header = datetime.strptime(raw.get("headerDate") or raw["departureDate"], "%d/%m/%Y")
            except (KeyError, ValueError, TypeError):
                continue
            legs.append(
                TrackedLeg(
                    trip=trip_index,
                    leg=leg_index + 1,
                    legs=len(raw_legs),
                    service=_id(raw.get("service")),
                    flight=_id(raw.get("flight")),
                    service_date=header.strftime("%Y%m%d"),
                    line_name=raw.get("serviceDescription", ""),
                    origin_id=_id(raw.get("legOriginId")),
                    origin_name=raw.get("legOrigin", ""),
                    destination_id=_id(raw.get("legDestinationId")),
                    destination_name=raw.get("legDestination", ""),
                    departure=departure,
                    arrival=arrival,
                )
            )
    return legs


def _stop_time(day: str, hhmm: str) -> datetime:
    return datetime.strptime(f"{day} {hhmm}", "%Y%m%d %H:%M").replace(tzinfo=MADRID_TZ)


def _parse_vehicle(entry: dict) -> Vehicle | None:
    """Una entrada de `AjaxGetBusServiceTracking`: recorrido completo y posición del bus."""
    stops: list[RouteStop] = []
    previous: datetime | None = None
    for raw in entry.get("itinerario") or []:
        try:
            arrival = _stop_time(raw["fecha"], raw["hllegada"])
            departure = _stop_time(raw["fecha"], raw.get("hsalida") or raw["hllegada"])
        except (KeyError, ValueError, TypeError):
            continue
        if departure < arrival:  # sale pasada la medianoche
            departure += timedelta(days=1)
        if previous is not None and arrival < previous:  # la fecha de la parada no cambió de día
            arrival += timedelta(days=1)
            departure += timedelta(days=1)
        previous = departure
        stops.append(
            RouteStop(
                id=_id(raw.get("pueblo")),
                town=raw.get("nomPueblo", ""),
                name=raw.get("parada", ""),
                arrival=arrival,
                departure=departure,
                latitude=_float(raw.get("latitud")),
                longitude=_float(raw.get("longitud")),
            )
        )
    if not stops:
        return None
    bus = next((b for b in entry.get("buses") or [] if _float(b.get("latitud")) is not None), None)
    return Vehicle(
        line_name=entry.get("nomLinea", ""),
        route_origin=entry.get("nomOrigen", ""),
        route_destination=entry.get("nomDestino", ""),
        latitude=_float(bus.get("latitud")) if bus else None,
        longitude=_float(bus.get("longitud")) if bus else None,
        stops=stops,
    )


def _float(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number != 0 else None


def _match_tracking(entries: list, leg: TrackedLeg, now: datetime) -> BusTracking:
    """Solo atribuye un vehículo si hay un único itinerario compatible con el tramo."""
    itineraries = [entry for entry in entries if isinstance(entry, dict) and entry.get("itinerario")]
    if not itineraries:
        return estimate(leg, None, now)
    unmatched = BusTracking(leg=leg, checked=True, status="unmatched_route")
    match: BusTracking | None = None
    for entry in itineraries:
        vehicle = _parse_vehicle(entry)
        if vehicle is None:
            continue
        candidate = estimate(leg, vehicle, now)
        if candidate.vehicle is not None:
            if match is not None:
                return unmatched
            match = candidate
    return match or unmatched


def track_route(
    origin_id: str,
    destination_id: str,
    departure: str | None = None,
    origin_stop_id: str | None = None,
    now: datetime | None = None,
    *,
    selected_leg: TrackedLeg | None = None,
) -> tuple[list[BusTracking], list[dict]]:
    """Buses de hoy entre dos paradas y, para los que están en ruta, dónde van.

    Args:
        origin_id, destination_id: IDs de parada o de ciudad («Todas las paradas»), los mismos de la búsqueda.
        departure: `HH:MM`; si se da, solo se consulta ese autobús (todos sus tramos).
        origin_stop_id: parada concreta de salida, para distinguir dos buses a la misma hora.
        selected_leg: tramo ya elegido en la TUI, identificado por expedición, fecha y paradas.
        now: hora de referencia para estimar el retraso; por defecto, ahora en Madrid.

    Returns:
        Tupla de (un BusTracking por tramo, lista de reintentos).
    """
    now = now or datetime.now(MADRID_TZ)
    retries: list[dict] = []
    session = curl_requests.Session()
    session.impersonate = "chrome"

    page = _request_with_retries(session, "GET", TRACKING_PAGE, "tracking page", retries).text
    action_url = _find_param(page, "byJourney")
    if not action_url:
        raise AlsaParseError("no se encontró la búsqueda por trayecto en «Localiza tu bus»")

    data = {f"{TRACKING_NS}originStationId": origin_id, f"{TRACKING_NS}destinationStationId": destination_id}
    results = _request_with_retries(session, "POST", action_url, "tracking search", retries, data=data).text
    legs = _parse_legs(results)
    tracking_url = _find_param(results, "getBusServiceTrackingURL")

    if selected_leg is not None:
        matches = [leg for leg in legs if leg.identity == selected_leg.identity]
        if len(matches) > 1:  # una expedición puede repetir el mismo par de paradas
            matches = [leg for leg in matches if leg.departure == selected_leg.departure]
        wanted = {id(leg) for leg in matches}
    elif departure is not None:
        trips = {
            leg.trip
            for leg in legs
            if leg.leg == 1
            and leg.departure.strftime("%H:%M") == departure
            and (origin_stop_id is None or leg.origin_id == origin_stop_id)
        }
        wanted = {id(leg) for leg in legs if leg.trip in trips}
    else:
        wanted = {id(leg) for leg in legs if leg.departure - CHECK_BEFORE <= now <= leg.arrival + CHECK_AFTER}

    answers: dict[tuple[str, str, str], list] = {}
    tracked: list[BusTracking] = []
    for leg in legs:
        if id(leg) not in wanted or not tracking_url:
            tracked.append(BusTracking(leg=leg, checked=False))
            continue
        if not all(leg.identity):
            tracked.append(BusTracking(leg=leg, checked=True, status="unmatched_route"))
            continue
        key = (leg.service, leg.flight, leg.service_date)
        if key not in answers:
            params = {
                f"{TRACKING_NS}date": leg.service_date,
                f"{TRACKING_NS}service": leg.service,
                f"{TRACKING_NS}flight": leg.flight,
                f"{TRACKING_NS}legOriginStationId": leg.origin_id,
                f"{TRACKING_NS}legDestinationStationId": leg.destination_id,
            }
            label = f"tracking {leg.departure:%H:%M}"
            resp = _request_with_retries(session, "POST", tracking_url, label, retries, params=params)
            try:
                answers[key] = json.loads(resp.text) if resp.text.strip() else []
            except json.JSONDecodeError as e:
                raise AlsaParseError(f"JSON inválido en AjaxGetBusServiceTracking: {e}") from e
        entries = answers[key] if isinstance(answers[key], list) else []
        tracked.append(_match_tracking(entries, leg, now))
    return tracked, retries


# --- estimación ----------------------------------------------------------------------


@dataclass(frozen=True)
class _Fix:
    """Dónde cae el GPS sobre la ruta.

    `position` cuenta paradas: 7.4 es el 40 % del camino entre la parada 7 y la 8.
    """

    position: float
    at_stop: bool
    distance_km: float
    scheduled: datetime  # hora a la que el horario pasa por ese punto
    segment_km: float = 0.0  # longitud en línea recta del tramo; 0 si está en una parada

    @property
    def allowed_km(self) -> float:
        """Lo lejos de la recta que puede estar el GPS para que la posición siga valiendo."""
        return max(MAX_OFF_ROUTE_KM, OFF_ROUTE_SHARE * self.segment_km)


def _xy(lat: float, lon: float, lat0: float) -> tuple[float, float]:
    """Proyección equirectangular en km, suficiente para distancias de unos cientos de km."""
    return lon * 111.32 * math.cos(math.radians(lat0)), lat * 110.57


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    return math.dist(_xy(lat1, lon1, lat1), _xy(lat2, lon2, lat1))


def _locate(vehicle: Vehicle, now: datetime) -> _Fix | None:
    """Parada o tramo del recorrido que mejor encaja con el GPS y con la hora.

    Cada candidato puntúa su distancia al GPS más 0,1 km por minuto de diferencia con el
    horario: así, en una ruta que vuelve sobre sí misma, gana el paso que toca a esta hora.
    """
    if vehicle.latitude is None or vehicle.longitude is None:
        return None
    lat0 = vehicle.latitude
    bus = _xy(vehicle.latitude, vehicle.longitude, lat0)
    located = [(i, _xy(s.latitude, s.longitude, lat0)) for i, s in enumerate(vehicle.stops) if s.latitude is not None and s.longitude is not None]
    candidates: list[_Fix] = []

    for i, here in located:
        stop = vehicle.stops[i]
        scheduled = min(max(now, stop.arrival), stop.departure)  # parado entre llegada y salida
        candidates.append(_Fix(float(i), True, math.dist(bus, here), scheduled))

    for (i, here), (j, there) in pairwise(located):
        dx, dy = there[0] - here[0], there[1] - here[1]
        length2 = dx * dx + dy * dy
        if length2 == 0:
            continue
        t = min(1.0, max(0.0, ((bus[0] - here[0]) * dx + (bus[1] - here[1]) * dy) / length2))
        start, end = vehicle.stops[i].departure, vehicle.stops[j].arrival
        scheduled = start + (end - start) * t
        point = (here[0] + t * dx, here[1] + t * dy)
        candidates.append(_Fix(i + t * (j - i), False, math.dist(bus, point), scheduled, math.sqrt(length2)))

    def score(fix: _Fix) -> float:
        return fix.distance_km + TIME_WEIGHT_KM_PER_MIN * abs((now - fix.scheduled).total_seconds()) / 60

    return min(candidates, key=score, default=None)


def _stop_index(stops: list[RouteStop], stop_id: str, start: int = 0) -> int | None:
    if not stop_id:
        return None
    return next((i for i in range(start, len(stops)) if stops[i].id == stop_id), None)


def estimate(leg: TrackedLeg, vehicle: Vehicle | None, now: datetime) -> BusTracking:
    """Estado, retraso y horas estimadas de un tramo a partir de la posición del bus."""
    if vehicle is None:
        return BusTracking(leg=leg, checked=True, status="no_tracking")

    stops = vehicle.stops
    origin = _stop_index(stops, leg.origin_id)
    destination = _stop_index(stops, leg.destination_id, origin + 1) if origin is not None else None
    if origin is None or destination is None:
        return BusTracking(leg=leg, checked=True, status="unmatched_route")
    base = BusTracking(leg=leg, checked=True, vehicle=vehicle, origin_stop=origin, destination_stop=destination)

    fix = _locate(vehicle, now)
    if fix is None:
        return replace(base, status="no_position")
    delay = round((now - fix.scheduled).total_seconds() / 60)
    if fix.distance_km > fix.allowed_km or abs(delay) > MAX_DELAY_MINUTES:
        return replace(base, status="off_route", distance_to_route_km=round(fix.distance_km, 1))

    if fix.position == 0 and now < stops[0].departure:
        status, delay = "waiting", 0
    elif fix.position < origin or (fix.position == origin and now < stops[origin].departure + timedelta(minutes=max(delay, 0))):
        status = "approaching"
    elif fix.position < destination:
        status = "on_the_way"
    else:
        status = "arrived"

    late = timedelta(minutes=max(delay, 0))
    leg_start, leg_end = stops[origin].departure, stops[destination].arrival
    if status in ("waiting", "approaching"):
        progress = 0.0
    elif status == "arrived":
        progress = 1.0
    else:
        span = (leg_end - leg_start).total_seconds()
        progress = 0.0 if span <= 0 else min(1.0, max(0.0, (fix.scheduled - leg_start).total_seconds() / span))

    next_stop = min(int(fix.position) if fix.at_stop else math.floor(fix.position) + 1, len(stops) - 1)
    upcoming = stops[next_stop]
    distance_next = (
        None
        if upcoming.latitude is None or upcoming.longitude is None
        else round(_distance_km(vehicle.latitude, vehicle.longitude, upcoming.latitude, upcoming.longitude), 1)  # type: ignore[arg-type]
    )
    return replace(
        base,
        status=status,
        delay_minutes=delay,
        position=round(fix.position, 3),
        at_stop=fix.at_stop,
        next_stop=next_stop,
        distance_to_next_km=distance_next,
        distance_to_route_km=round(fix.distance_km, 1),
        approximate=fix.distance_km > MAX_OFF_ROUTE_KM,
        progress=round(progress, 3),
        estimated_departure=None if status in ("on_the_way", "arrived") else stops[origin].departure + late,
        estimated_arrival=None if status == "arrived" else leg_end + late,
        estimated_stops=[None if i <= fix.position and status != "waiting" else s.arrival + late for i, s in enumerate(stops)],
    )
