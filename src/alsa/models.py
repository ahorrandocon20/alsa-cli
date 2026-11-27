"""Modelos de datos: estaciones, journeys y fares de Alsa."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Station:
    """Una parada o estación de Alsa."""

    id: str
    name: str
    province: str
    simplified_name: str = ""
    icon: str = ""
    is_movelia: bool = False


@dataclass(frozen=True)
class Fare:
    """Una tarifa de un journey."""

    price: float
    fare_name: str
    fare_type: str  # "BASE", "FLX", etc.
    fare_conditions: str


@dataclass(frozen=True)
class Journey:
    """Un bus de Alsa entre dos puntos, con sus tarifas."""

    origin_id: str
    origin_name: str
    destination_id: str
    destination_name: str
    departure: datetime
    arrival: datetime
    duration_minutes: int
    transfers: int
    service_type: str  # "Comfort", "Supra", etc.
    fares: list[Fare]
    line_name: str = ""
    available: bool = True  # False si Alsa marca el bus como «No hay plazas disponibles»

    @property
    def cheapest_price(self) -> float:
        return min(f.price for f in self.fares) if self.fares else float("inf")

    def to_dict(self) -> dict:
        return {
            "origin_id": self.origin_id,
            "origin_name": self.origin_name,
            "destination_id": self.destination_id,
            "destination_name": self.destination_name,
            "departure": self.departure.isoformat(),
            "arrival": self.arrival.isoformat(),
            "duration_minutes": self.duration_minutes,
            "transfers": self.transfers,
            "service_type": self.service_type,
            "line_name": self.line_name,
            "available": self.available,
            "cheapest_price_eur": round(self.cheapest_price, 2) if self.fares else None,
            "fares": [
                {
                    "price": f.price,
                    "fare_name": f.fare_name,
                    "fare_type": f.fare_type,
                    "fare_conditions": f.fare_conditions,
                }
                for f in self.fares
            ],
        }


@dataclass(frozen=True)
class Package:
    """Tarifa de la web de Alsa para un bus (Básica, Flexible…), con el precio total ya sumado."""

    id: str
    name: str
    price: float  # total para todos los viajeros, sin gastos de gestión
    recommended: bool
    conditions: list[str]


# --- seguimiento en directo ---------------------------------------------------------


@dataclass(frozen=True)
class RouteStop:
    """Una parada del recorrido completo de un autobús, con su horario."""

    id: str
    town: str
    name: str
    arrival: datetime
    departure: datetime
    latitude: float | None
    longitude: float | None


@dataclass(frozen=True)
class TrackedLeg:
    """Un tramo de un autobús de hoy en la ruta buscada, tal como lo lista «Localiza tu bus»."""

    trip: int  # posición del viaje en la lista de Alsa; los tramos de un mismo viaje la comparten
    leg: int  # 1, 2… dentro del viaje
    legs: int
    service: str
    flight: str
    service_date: str  # YYYYMMDD, el día de la expedición
    line_name: str
    origin_id: str
    origin_name: str
    destination_id: str
    destination_name: str
    departure: datetime
    arrival: datetime

    @property
    def identity(self) -> tuple[str, str, str, str, str]:
        """Expedición y paradas del tramo, independientes del orden del listado de Alsa."""
        return self.service, self.flight, self.service_date, self.origin_id, self.destination_id


@dataclass(frozen=True)
class Vehicle:
    """Lo que Alsa sabe del autobús: su recorrido completo y dónde está ahora."""

    line_name: str
    route_origin: str
    route_destination: str
    latitude: float | None
    longitude: float | None
    stops: list[RouteStop]


# Qué se sabe del autobús; los valores van en inglés en el JSON, como el resto de campos.
TRACKING_STATUSES = (
    "not_checked",  # no se consultó: sale dentro de más de 3 horas o ya llegó
    "no_tracking",  # Alsa no tiene seguimiento para este autobús
    "unmatched_route",  # ningún itinerario identifica el tramo de forma inequívoca
    "no_position",  # hay recorrido, pero no posición GPS
    "off_route",  # la posición está lejos del recorrido: no se puede estimar
    "waiting",  # en la primera parada del recorrido, antes de salir
    "approaching",  # en ruta, aún no ha llegado a tu parada de salida
    "on_the_way",  # entre tu parada de salida y tu destino
    "arrived",  # ya pasó por tu destino
)


def map_url(latitude: float, longitude: float) -> str:
    return f"https://www.openstreetmap.org/?mlat={latitude:.5f}&mlon={longitude:.5f}#map=13/{latitude:.5f}/{longitude:.5f}"


@dataclass(frozen=True)
class BusTracking:
    """Seguimiento de un tramo: estado, retraso estimado y horas estimadas en cada parada."""

    leg: TrackedLeg
    checked: bool
    vehicle: Vehicle | None = None
    status: str = "not_checked"
    delay_minutes: int | None = None  # positivo = tarde; estimado con el GPS y el horario
    position: float | None = None  # en paradas del recorrido: 7.4 = 40 % entre la 7 y la 8
    at_stop: bool = False
    next_stop: int | None = None
    distance_to_next_km: float | None = None
    distance_to_route_km: float | None = None
    approximate: bool = False  # el GPS está lejos de la recta entre paradas (tramo largo): estimación gruesa
    origin_stop: int | None = None  # índice de tu parada de salida en el recorrido
    destination_stop: int | None = None
    progress: float | None = None  # 0 a 1 entre tu salida y tu llegada
    estimated_departure: datetime | None = None
    estimated_arrival: datetime | None = None
    estimated_stops: list[datetime | None] | None = None

    @property
    def located(self) -> bool:
        return self.status in ("waiting", "approaching", "on_the_way", "arrived")

    def to_dict(self) -> dict:
        leg, vehicle = self.leg, self.vehicle
        stops = vehicle.stops if vehicle else []
        estimated = self.estimated_stops or [None] * len(stops)

        def stop_ref(index: int | None) -> dict | None:
            if index is None or not stops:
                return None
            stop = stops[index]
            return {
                "town": stop.town,
                "name": stop.name,
                "arrival": stop.arrival.isoformat(),
                "estimated_arrival": _iso(estimated[index]),
            }

        has_position = vehicle is not None and vehicle.latitude is not None and vehicle.longitude is not None
        return {
            "origin_id": leg.origin_id,
            "origin_name": leg.origin_name,
            "destination_id": leg.destination_id,
            "destination_name": leg.destination_name,
            "departure": leg.departure.isoformat(),
            "arrival": leg.arrival.isoformat(),
            "line_name": leg.line_name,
            "leg": leg.leg,
            "legs": leg.legs,
            "status": self.status,
            "delay_minutes": self.delay_minutes,
            "approximate": self.approximate,
            "estimated_departure": _iso(self.estimated_departure),
            "estimated_arrival": _iso(self.estimated_arrival),
            "progress": self.progress,
            "position": (
                {
                    "latitude": vehicle.latitude,
                    "longitude": vehicle.longitude,
                    "map_url": map_url(vehicle.latitude, vehicle.longitude),  # type: ignore[arg-type]
                }
                if has_position
                else None
            ),
            "next_stop": stop_ref(self.next_stop) if self.located else None,
            "distance_to_next_stop_km": self.distance_to_next_km if self.located else None,
            "route": (
                {"line_name": vehicle.line_name, "origin": vehicle.route_origin, "destination": vehicle.route_destination}
                if vehicle
                else None
            ),
            "stops": [
                {
                    "id": stop.id,
                    "town": stop.town,
                    "name": stop.name,
                    "arrival": stop.arrival.isoformat(),
                    "departure": stop.departure.isoformat(),
                    "estimated_arrival": _iso(estimated[i]),
                    "passed": self.located and self.position is not None and i <= self.position and self.status != "waiting",
                    "in_trip": self.origin_stop is not None
                    and self.destination_stop is not None
                    and self.origin_stop <= i <= self.destination_stop,
                }
                for i, stop in enumerate(stops)
            ],
        }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
