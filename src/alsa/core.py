"""Cliente HTTP para alsa.es: pasa Imperva con curl_cffi y ejecuta la búsqueda de dos pasos.

Flujo (descubierto con ingeniería inversa sobre el HAR real de alsa.es):

1. GET a la home → recoge cookies de Imperva y el token CSRF `p_auth` del HTML.
2. GET a `SearchJourneysAction` con origen, destino y fecha → el portlet Liferay
   guarda la búsqueda en sesión del servidor.
3. GET a `GetJourneyListResource` con `journeyDirection=I` → devuelve el JSON
   con journeys, horarios y precios de la búsqueda en sesión.

Reintentos (mismo patrón que renfe-cli): hasta 3 intentos por petición,
1 s y 2 s de pausa, solo para errores 5xx y conexiones caídas. Nunca para
bloqueos (403/429), timeouts ni la página de bloqueo de Imperva. Cada petición
que se recupera tras reintentar queda en la lista `retries` de la respuesta.
"""

from __future__ import annotations

import html
import json
import re
import time
import unicodedata
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

from curl_cffi import requests as curl_requests
from curl_cffi.requests.exceptions import RequestException, Timeout

from alsa.exceptions import (
    AlsaAuthError,
    AlsaBlockedError,
    AlsaConnectionError,
    AlsaParseError,
    AlsaStationError,
    AlsaTimeoutError,
)
from alsa.models import Fare, Journey, Package

MADRID_TZ = ZoneInfo("Europe/Madrid")

BASE_URL = "https://www.alsa.es"
PORTLET_ID = "com_babel_alsa_espania_purchase_web_PurchasePortlet"
PORTLET_NS = f"_{PORTLET_ID}"
REQUEST_TIMEOUT = 60  # segundos
RETRY_Pauses = (1.0, 2.0)  # segundos antes del segundo y tercer intento
RETRY_STATUSES = (500, 502, 503, 504)
MAX_ADULTS = 20  # MAX_PASSENGERS_AMOUNT del buscador de alsa.es
IMPERVA_BLOCK_MARK = "Incapsula incident ID"  # texto de la página de bloqueo, no de las páginas normales


def _request_with_retries(
    session: curl_requests.Session,
    method: str,
    url: str,
    label: str,
    retries_log: list[dict],
    **kwargs,
) -> curl_requests.Response:
    """Hace una petición con hasta 3 intentos, igual que renfe-cli.

    Reintenta solo errores 5xx y conexiones caídas; bloqueos (403/429),
    la página de bloqueo de Imperva y timeouts paran al primer intento.
    Si un reintento acaba bien, queda anotado en `retries_log`.
    """
    errors: list[str] = []
    max_tries = len(RETRY_Pauses) + 1

    for attempt in range(1, max_tries + 1):
        try:
            if method == "GET":
                resp = session.get(url, timeout=REQUEST_TIMEOUT, **kwargs)
            else:
                resp = session.post(url, timeout=REQUEST_TIMEOUT, **kwargs)
        except Timeout as e:
            raise AlsaTimeoutError(REQUEST_TIMEOUT) from e
        except RequestException as e:
            errors.append(f"connection dropped ({type(e).__name__})")
        else:
            if resp.status_code in (403, 429) or IMPERVA_BLOCK_MARK in resp.text:
                raise AlsaBlockedError()
            if resp.status_code not in RETRY_STATUSES:
                if errors:
                    retries_log.append({"request": label, "tries": attempt, "errors": errors})
                return resp
            errors.append(f"server error ({resp.status_code})")

        if attempt < max_tries:
            time.sleep(RETRY_Pauses[attempt - 1])

    raise AlsaConnectionError(f"{label} falló tras {max_tries} intentos ({errors[-1]})")


def search_bus(
    origin_station_id: str,
    destination_station_id: str,
    travel_date: str,
    adults: int = 1,
) -> tuple[list[Journey], list[dict], str]:
    """Busca buses entre dos estaciones de Alsa en una fecha.

    Returns:
        Tupla de (journeys ordenados por precio, lista de reintentos, shop_url).

    Raises:
        AlsaBlockedError: Imperva bloqueó la petición.
        AlsaConnectionError: no se pudo conectar tras reintentos.
        AlsaParseError: la respuesta no se pudo interpretar.
        AlsaAuthError: el token CSRF no funcionó.
    """
    retries: list[dict] = []
    session = curl_requests.Session()
    session.impersonate = "chrome"

    # --- paso 0: cookies de Imperva + p_auth de cualquier página -------------
    home = _request_with_retries(session, "GET", BASE_URL, "home", retries)

    auth_match = re.search(r'p_auth=([A-Za-z0-9_-]+)', home.text)
    if not auth_match:
        raise AlsaParseError("no se encontró p_auth en la respuesta de la home")
    p_auth = auth_match.group(1)

    # --- paso 1: SearchJourneysAction — establece la búsqueda en sesión -------
    search_url = (
        f"{BASE_URL}/checkout"
        f"?p_p_id={PORTLET_ID}"
        f"&p_p_lifecycle=1&p_p_state=normal&p_p_mode=view"
        f"&{PORTLET_NS}_javax.portlet.action=SearchJourneysAction"
        f"&p_auth={p_auth}"
        f"&alsaspam="
        f"&originStationId={origin_station_id}"
        f"&destinationStationId={destination_station_id}"
        f"&departureDate={travel_date}"
        f"&travelType=OUTWARD"
        f"&passengerType-1={adults}"
        f"&seats={adults}"
        f"&employee=false"
    )

    search_resp = _request_with_retries(session, "GET", search_url, "search", retries)

    if origin_station_id not in search_resp.text:
        raise AlsaAuthError(f"p_auth={p_auth[:8]}… posible sesión caducada")

    # --- paso 2: GetJourneyListResource — devuelve el JSON de resultados -----
    list_url = (
        f"{BASE_URL}/checkout"
        f"?p_p_id={PORTLET_ID}"
        f"&p_p_lifecycle=2&p_p_state=normal&p_p_mode=view"
        f"&p_p_resource_id=GetJourneyListResource"
        f"&p_p_cacheability=cacheLevelPage"
        f"&journeyDirection=I"
    )

    list_resp = _request_with_retries(session, "GET", list_url, "journey list", retries)

    if not list_resp.text.strip():
        return [], retries, results_url(origin_station_id, destination_station_id, travel_date, adults)

    try:
        data = json.loads(list_resp.text)
    except json.JSONDecodeError as e:
        raise AlsaParseError(f"JSON inválido en GetJourneyListResource: {e}") from e

    journeys = _parse_journeys(data)
    journeys.sort(key=lambda j: j.cheapest_price)

    shop = results_url(origin_station_id, destination_station_id, travel_date, adults)
    return journeys, retries, shop


def get_packages(
    origin_station_id: str,
    destination_station_id: str,
    travel_date: str,
    adults: int,
    departure: datetime,
    origin_stop_id: str,
) -> list[Package]:
    """Tarifas que ofrece la web para un bus concreto, identificado por hora de salida y parada.

    Repite la búsqueda en una sesión nueva porque las tarifas cuelgan de la búsqueda en sesión.
    """
    retries: list[dict] = []
    session = curl_requests.Session()
    session.impersonate = "chrome"
    _request_with_retries(session, "GET", BASE_URL, "home", retries)
    url = results_url(origin_station_id, destination_station_id, travel_date, adults)
    _request_with_retries(session, "GET", url, "search", retries)

    def resource(resource_id: str, extra: str) -> dict:
        resource_url = (
            f"{BASE_URL}/checkout?p_p_id={PORTLET_ID}&p_p_lifecycle=2&p_p_state=normal&p_p_mode=view"
            f"&p_p_resource_id={resource_id}&p_p_cacheability=cacheLevelPage{extra}"
        )
        resp = _request_with_retries(session, "GET", resource_url, resource_id, retries)
        try:
            return json.loads(resp.text)
        except json.JSONDecodeError as e:
            raise AlsaParseError(f"JSON inválido en {resource_id}: {e}") from e

    departure_ms = int(departure.timestamp() * 1000)
    journeys = resource("GetJourneyListResource", "&journeyDirection=I").get("journeys") or []
    journey = next(
        (j for j in journeys if j.get("departureDate") == departure_ms and str(j.get("originId")) == origin_stop_id),
        None,
    )
    if journey is None or not journey.get("fares"):
        return []
    base = float(journey["fares"][0].get("price") or 0)
    data = resource("GetPackageFaresResource", f"&journeyIndex={journey['index']}&journeyDirection=I")

    packages = []
    for raw in data.get("packageFareList") or []:
        if not raw.get("enabled", True):
            continue
        packages.append(
            Package(
                id=str(raw.get("farePackageId", "")),
                name=raw.get("packageName", ""),
                price=round(base + float(raw.get("totalPassengerPackagePrice") or 0), 2),
                recommended=bool(raw.get("recommended")),
                conditions=[_plain(o.get("description", "")) for o in raw.get("packageObservations") or []],
            )
        )
    return sorted(packages, key=lambda p: p.price)


def _plain(html_text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", html_text)).strip()


def results_url(origin_id: str, destination_id: str, date: str, adults: int) -> str:
    """URL que abre en alsa.es la lista de buses de esta búsqueda, sin sesión previa.

    Es la misma acción `SearchJourneysAction` que lanza el botón «Buscar» de la web; sin
    `p_auth` también funciona en un navegador recién abierto.
    """
    return (
        f"{BASE_URL}/checkout?p_p_id={PORTLET_ID}&p_p_lifecycle=1&p_p_state=normal&p_p_mode=view"
        f"&{PORTLET_NS}_javax.portlet.action=SearchJourneysAction"
        f"&alsaspam=&originStationId={origin_id}&destinationStationId={destination_id}"
        f"&departureDate={quote(date, safe='')}&returnDate=&seats={adults}&promoCode=&youngPromoCode="
        f"&travelType=OUTWARD&employee=false&passengerType-1={adults}"
    )


def _parse_journeys(data: dict) -> list[Journey]:
    """Convierte el JSON crudo de Alsa en una lista de Journey."""
    raw = data.get("journeys") or []
    journeys: list[Journey] = []

    for rj in raw:
        try:
            dep_ms = rj.get("departureDate")
            arr_ms = rj.get("arrivalDate")
            if not dep_ms or not arr_ms:
                continue

            departure = datetime.fromtimestamp(dep_ms / 1000, tz=MADRID_TZ)
            arrival = datetime.fromtimestamp(arr_ms / 1000, tz=MADRID_TZ)
            duration = int((arrival - departure).total_seconds() / 60)

            fares = []
            for rf in rj.get("fares") or []:
                price = rf.get("price")
                if price is None or price <= 0:
                    continue
                fares.append(
                    Fare(
                        price=float(price),
                        fare_name=rf.get("fareName", ""),
                        fare_type=rf.get("fareType", ""),
                        fare_conditions=rf.get("fareConditions", ""),
                    )
                )

            service_types = rj.get("serviceTypeDescList") or [""]
            journeys.append(
                Journey(
                    origin_id=str(rj.get("originId", "")),
                    origin_name=rj.get("originName", ""),
                    destination_id=str(rj.get("destinationId", "")),
                    destination_name=rj.get("destinationName", ""),
                    departure=departure,
                    arrival=arrival,
                    duration_minutes=duration,
                    transfers=int(rj.get("transfersNumber", 0)),
                    service_type=service_types[0],
                    fares=fares,
                    line_name=rj.get("lineName", ""),
                    available=rj.get("canBeSold") != "F",
                )
            )
        except (KeyError, ValueError, TypeError):
            continue  # un journey malformado no tira la lista entera

    return journeys


STATIONS_URL = (
    f"{BASE_URL}/home"
    f"?p_p_id=com_babel_alsa_espania_journeysearch_web_JourneySearchPortlet_INSTANCE_21651890"
    f"&p_p_lifecycle=2&p_p_state=normal&p_p_mode=view"
    f"&p_p_resource_id=GetOriginsResourceCommand"
    f"&p_p_cacheability=cacheLevelPage"
)


def _norm(text: str) -> str:
    """Minúsculas y sin acentos, para comparar nombres de paradas."""
    decomposed = unicodedata.normalize("NFD", text.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _fetch_station_list() -> list[dict]:
    """Descarga la lista completa de paradas de alsa.es, tal cual la devuelve la web.

    Es una sola petición, sin reintentos. Un bloqueo, un timeout o una conexión caída
    dan el mismo error que en la búsqueda, nunca una lista vacía.
    """
    session = curl_requests.Session()
    session.impersonate = "chrome"
    try:
        resp = session.post(STATIONS_URL, data={"origen": ""}, timeout=REQUEST_TIMEOUT)
    except Timeout as e:
        raise AlsaTimeoutError(REQUEST_TIMEOUT) from e
    except RequestException as e:
        raise AlsaConnectionError(str(e)) from e

    if resp.status_code in (403, 429) or IMPERVA_BLOCK_MARK in resp.text:
        raise AlsaBlockedError()

    try:
        stations = json.loads(resp.text)
    except json.JSONDecodeError:
        raise AlsaParseError("la lista de estaciones no se pudo leer") from None
    if not isinstance(stations, list):
        raise AlsaParseError("la lista de estaciones no se pudo leer")
    return stations


def get_stations(query: str = "") -> list[dict]:
    """Busca estaciones de Alsa por nombre (filtrado local, como su web)."""
    query_norm = _norm(query)
    result = []
    for st in _fetch_station_list():
        if query_norm and query_norm not in _norm(st.get("name", "")):
            continue
        result.append(
            {
                "id": st.get("id", ""),
                "name": st.get("name", ""),
                "province": st.get("provincecountryName", ""),
                "simplified_name": st.get("simplifiedName", ""),
                "icon": st.get("icon", ""),
            }
        )
        if len(result) >= 20:
            break

    return result


def get_all_stations() -> list[dict]:
    """Descarga la lista completa de estaciones de Alsa, sin filtrar ni recortar."""
    return [
        {
            "id": str(st.get("id", "")),
            "name": st.get("name", ""),
            "province": st.get("provincecountryName", ""),
            "icon": st.get("icon", ""),
        }
        for st in _fetch_station_list()
        if st.get("id") and st.get("name")
    ]


def get_station_id(name: str) -> str:
    """Encuentra el ID de una estación de Alsa por su nombre.

    Descarga la lista completa (una petición) y filtra localmente.
    Prioriza match exacto, luego agrupación de ciudad, luego startsWith.
    """
    all_stations = _fetch_station_list()

    name_norm = _norm(name)

    # 1: match exacto (ignorando acentos y mayúsculas)
    for st in all_stations:
        if _norm(st.get("name", "")) == name_norm:
            return st["id"]

    # 2: agrupación "(Todas las paradas)" que contenga el nombre
    for st in all_stations:
        st_name = _norm(st.get("name", ""))
        if "todas las paradas" in st_name and name_norm in st_name:
            return st["id"]

    # 3: el nombre EMPIEZA por la búsqueda
    for st in all_stations:
        if _norm(st.get("name", "")).startswith(name_norm):
            return st["id"]

    # 4: el nombre CONTIENE la búsqueda
    for st in all_stations:
        if name_norm in _norm(st.get("name", "")):
            return st["id"]

    raise AlsaStationError(
        f"No se encontró ninguna estación que encaje con {name!r}. "
        "Prueba con otro nombre o usa `alsa stations <texto>` para buscar."
    )
