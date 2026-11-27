"""Tests offline de core: parser, reintentos, búsqueda, estaciones y tarifas contra un alsa.es falso."""

import copy
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from curl_cffi.requests.exceptions import ConnectionError as CurlConnectionError
from curl_cffi.requests.exceptions import Timeout

from alsa import core
from alsa.core import (
    _parse_journeys,
    _request_with_retries,
    get_all_stations,
    get_packages,
    get_station_id,
    get_stations,
    search_bus,
)
from alsa.exceptions import (
    AlsaAuthError,
    AlsaBlockedError,
    AlsaConnectionError,
    AlsaParseError,
    AlsaStationError,
    AlsaTimeoutError,
)

TZ = ZoneInfo("Europe/Madrid")

# JSON capturado del endpoint real GetJourneyListResource de alsa.es
# (simplificado, estructura fiel a la respuesta del 2026-11-24)
SAMPLE = {
    "journeys": [
        {
            "index": 0,
            "departureDate": 1795479300000,
            "arrivalDate": 1795498380000,
            "departureTime": "01:15",
            "arrivalTime": "06:33",
            "originId": "5555",
            "originName": "Aeropuerto Madrid - Barajas T4",
            "destinationId": "3100",
            "destinationName": "Pamplona",
            "transfersNumber": 1,
            "serviceTypeDescList": ["Comfort", "Comfort"],
            "lineName": "",
            "fares": [
                {
                    "price": 26.5,
                    "grossPrice": 36.22,
                    "fareType": "FLX",
                    "fareName": "Tarifa Flex",
                    "fareConditions": "Puedes anular y cambiar",
                    "travellersData": [
                        {"passengerType": "ADULT", "amount": 1, "price": 26.5}
                    ],
                }
            ],
        },
        {
            "index": 1,
            "departureDate": 1795485300000,
            "arrivalDate": 1795502100000,
            "departureTime": "09:15",
            "arrivalTime": "15:35",
            "originId": "5555",
            "originName": "Aeropuerto Madrid - Barajas T4",
            "destinationId": "3100",
            "destinationName": "Pamplona",
            "transfersNumber": 1,
            "serviceTypeDescList": ["Supra"],
            "fares": [
                {"price": 43.63, "fareType": "SFL", "fareName": "SUPER FLEX", "fareConditions": "Cambio y anulacion"}
            ],
        },
    ],
    "isRoundTrip": False,
    "countCanBeSold": 2,
}


def test_parse_extrae_journeys_con_precio():
    journeys = _parse_journeys(SAMPLE)

    assert len(journeys) == 2
    assert journeys[0].origin_name == "Aeropuerto Madrid - Barajas T4"
    assert journeys[0].destination_name == "Pamplona"
    assert journeys[0].cheapest_price == 26.50
    assert journeys[0].service_type == "Comfort"


def test_parse_lee_horarios_y_duracion():
    journeys = _parse_journeys(SAMPLE)

    j = journeys[0]
    assert j.departure.strftime("%H:%M") == "01:15"  # epoch 1795479300000 en Europe/Madrid
    assert j.arrival.strftime("%H:%M") == "06:33"
    assert j.duration_minutes == 318


def test_parse_journey_sin_fares_no_revierta():
    data = {
        "journeys": [
            {
                "departureDate": 1795479300000,
                "arrivalDate": 1795498380000,
                "originId": "1",
                "originName": "A",
                "destinationId": "2",
                "destinationName": "B",
                "transfersNumber": 0,
                "serviceTypeDescList": ["Comfort"],
                "fares": [],
            }
        ]
    }
    journeys = _parse_journeys(data)

    assert len(journeys) == 1
    assert journeys[0].fares == []
    assert journeys[0].cheapest_price == float("inf")  # al final al ordenar por precio
    assert journeys[0].to_dict()["cheapest_price_eur"] is None  # JSON válido, no Infinity


def test_parse_vacio_devuelve_lista_vacia():
    assert _parse_journeys({}) == []
    assert _parse_journeys({"journeys": []}) == []


def test_parse_marca_agotados_con_canbesold_f():
    import copy

    data = copy.deepcopy(SAMPLE)
    data["journeys"][0]["canBeSold"] = "F"
    data["journeys"][1]["canBeSold"] = "V"
    journeys = _parse_journeys(data)
    assert [j.available for j in journeys] == [False, True]
    assert journeys[0].to_dict()["available"] is False


class FakeResponse:
    def __init__(self, status_code: int, text: str = "ok"):
        self.status_code = status_code
        self.text = text


class FakeSession:
    """Devuelve (o lanza) las respuestas en orden y cuenta cuántas peticiones recibe."""

    def __init__(self, *outcomes: FakeResponse | Exception):
        self.outcomes = list(outcomes)
        self.calls = 0

    def get(self, *args, **kwargs) -> FakeResponse:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture(autouse=True)
def sin_pausas(monkeypatch):
    monkeypatch.setattr(core, "RETRY_Pauses", (0, 0))


def test_reintento_que_se_recupera_queda_en_retries():
    session = FakeSession(FakeResponse(503), CurlConnectionError("reset"), FakeResponse(200))
    log: list[dict] = []

    resp = _request_with_retries(session, "GET", "https://alsa.test", "home", log)

    assert resp.status_code == 200
    assert log == [
        {"request": "home", "tries": 3, "errors": ["server error (503)", "connection dropped (ConnectionError)"]}
    ]


def test_tres_errores_de_servidor_paran_con_error():
    session = FakeSession(FakeResponse(503), FakeResponse(502), FakeResponse(500))

    with pytest.raises(AlsaConnectionError, match="falló tras 3 intentos"):
        _request_with_retries(session, "GET", "https://alsa.test", "home", [])
    assert session.calls == 3


def test_timeout_no_se_reintenta():
    session = FakeSession(Timeout("lento"), FakeResponse(200))

    with pytest.raises(AlsaTimeoutError):
        _request_with_retries(session, "GET", "https://alsa.test", "home", [])
    assert session.calls == 1


def test_pagina_de_bloqueo_de_imperva_es_un_bloqueo():
    blocked = FakeResponse(200, "<html>Request unsuccessful. Incapsula incident ID: 123</html>")
    session = FakeSession(blocked, FakeResponse(200))

    with pytest.raises(AlsaBlockedError):
        _request_with_retries(session, "GET", "https://alsa.test", "home", [])
    assert session.calls == 1


def test_403_es_un_bloqueo_sin_reintento():
    session = FakeSession(FakeResponse(403), FakeResponse(200))

    with pytest.raises(AlsaBlockedError):
        _request_with_retries(session, "GET", "https://alsa.test", "home", [])
    assert session.calls == 1


def test_respuesta_a_la_primera_no_anota_reintentos():
    session = FakeSession(FakeResponse(200))
    log: list[dict] = []

    _request_with_retries(session, "GET", "https://alsa.test", "home", log)

    assert log == []


# --- búsqueda, estaciones y tarifas contra un alsa.es falso ---------------------


class FakeAlsa:
    """Sesión falsa de curl_cffi: responde según la acción de la URL y guarda las URLs pedidas."""

    def __init__(self, **responses: str):
        self.responses = {"home": '<a href="/checkout?p_auth=abc123&x=1">', **responses}
        self.urls: list[str] = []
        self.impersonate = ""

    def _answer(self, url: str) -> FakeResponse:
        self.urls.append(url)
        for action in ("SearchJourneysAction", "GetJourneyListResource", "GetPackageFaresResource", "GetOriginsResourceCommand"):
            if action in url:
                return FakeResponse(200, self.responses[action])
        return FakeResponse(200, self.responses["home"])

    def get(self, url, **kwargs) -> FakeResponse:
        return self._answer(url)

    def post(self, url, **kwargs) -> FakeResponse:
        return self._answer(url)


@pytest.fixture
def fake_alsa(monkeypatch):
    sessions: list[FakeAlsa] = []

    def install(**responses: str) -> list[FakeAlsa]:
        monkeypatch.setattr(core.curl_requests, "Session", lambda: sessions.append(FakeAlsa(**responses)) or sessions[-1])
        return sessions

    return install


def _list_json(journeys: list[dict]) -> str:
    return json.dumps({"journeys": journeys})


def test_search_bus_ordena_por_precio_y_devuelve_el_enlace(fake_alsa):
    reversed_sample = list(reversed(SAMPLE["journeys"]))
    fake_alsa(SearchJourneysAction="ok 90155", GetJourneyListResource=_list_json(reversed_sample))

    journeys, retries, shop_url = search_bus("90155", "90186", "21/12/2026", 2)

    assert [j.cheapest_price for j in journeys] == [26.50, 43.63]
    assert retries == []
    assert "originStationId=90155" in shop_url and "passengerType-1=2" in shop_url


def test_search_bus_manda_el_p_auth_de_la_home(fake_alsa):
    sessions = fake_alsa(SearchJourneysAction="ok 90155", GetJourneyListResource=_list_json([]))

    search_bus("90155", "90186", "21/12/2026", 1)

    search_url = next(u for u in sessions[0].urls if "SearchJourneysAction" in u)
    assert "p_auth=abc123" in search_url and "departureDate=21/12/2026" in search_url


def test_search_bus_sin_resultados_devuelve_lista_vacia(fake_alsa):
    fake_alsa(SearchJourneysAction="ok 90155", GetJourneyListResource="  ")

    journeys, _, _ = search_bus("90155", "90186", "21/12/2026", 1)

    assert journeys == []


def test_search_bus_sin_p_auth_es_error_de_lectura(fake_alsa):
    fake_alsa(home="<html>sin token</html>")

    with pytest.raises(AlsaParseError, match="p_auth"):
        search_bus("90155", "90186", "21/12/2026", 1)


def test_search_bus_sesion_rechazada_es_error_de_sesion(fake_alsa):
    fake_alsa(SearchJourneysAction="<html>otra página</html>")

    with pytest.raises(AlsaAuthError):
        search_bus("90155", "90186", "21/12/2026", 1)


def _stations(*names: str) -> str:
    return json.dumps([{"id": str(i), "name": name} for i, name in enumerate(names, start=1)])


def test_station_id_prefiere_el_nombre_exacto(fake_alsa):
    fake_alsa(GetOriginsResourceCommand=_stations("Madridejos", "Madrid (Todas las paradas)", "Madrid"))
    assert get_station_id("madrid") == "3"


def test_station_id_prefiere_el_grupo_de_la_ciudad_a_un_prefijo(fake_alsa):
    fake_alsa(GetOriginsResourceCommand=_stations("Oviedo Centro", "Oviedo / Uviéu (Todas las paradas)"))
    assert get_station_id("oviedo") == "2"


def test_station_id_prefiere_el_prefijo_a_contener(fake_alsa):
    fake_alsa(GetOriginsResourceCommand=_stations("Aeropuerto Gijón", "Gijón Puerto"))
    assert get_station_id("gij") == "2"


def test_station_id_ignora_acentos_y_mayusculas(fake_alsa):
    fake_alsa(GetOriginsResourceCommand=_stations("Plasencia", "Cáceres"))
    assert get_station_id("CACERES") == "2"


def test_station_id_desconocida_es_error(fake_alsa):
    fake_alsa(GetOriginsResourceCommand=_stations("Madrid"))
    with pytest.raises(AlsaStationError):
        get_station_id("Atlantis")


def test_stations_filtra_ignorando_acentos(fake_alsa):
    fake_alsa(GetOriginsResourceCommand=_stations("Cáceres", "Plasencia", "Cacabelos"))
    assert [s["name"] for s in get_stations("caceres")] == ["Cáceres"]


IMPERVA_PAGE = "<html>Request unsuccessful. Incapsula incident ID: 123</html>"


@pytest.mark.parametrize("lookup", [lambda: get_stations("oviedo"), get_all_stations, lambda: get_station_id("Oviedo")])
def test_bloqueo_al_buscar_paradas_es_un_bloqueo_no_una_lista_vacia(fake_alsa, lookup):
    fake_alsa(GetOriginsResourceCommand=IMPERVA_PAGE)
    with pytest.raises(AlsaBlockedError):
        lookup()


@pytest.mark.parametrize("lookup", [lambda: get_stations("oviedo"), get_all_stations, lambda: get_station_id("Oviedo")])
def test_lista_de_paradas_ilegible_es_error_de_lectura(fake_alsa, lookup):
    fake_alsa(GetOriginsResourceCommand="<html>mantenimiento</html>")
    with pytest.raises(AlsaParseError):
        lookup()


def test_timeout_al_buscar_paradas_es_un_timeout_sin_reintento(monkeypatch):
    calls = []

    class SlowSession:
        impersonate = ""

        def post(self, *args, **kwargs):
            calls.append(args)
            raise Timeout("lento")

    monkeypatch.setattr(core.curl_requests, "Session", SlowSession)
    with pytest.raises(AlsaTimeoutError):
        get_station_id("Madrid")
    assert len(calls) == 1


PACKAGE_FARES = {
    "packageFareList": [
        {"farePackageId": 1001, "packageName": "Superflexible", "totalPassengerPackagePrice": 3, "recommended": False},
        {"farePackageId": 7, "packageName": "Oculta", "totalPassengerPackagePrice": 1, "enabled": False},
        {
            "farePackageId": 45,
            "packageName": "Básica",
            "totalPassengerPackagePrice": 0,
            "recommended": True,
            "packageObservations": [{"description": "<b>Cambios</b> gratis &amp; sin coste"}],
        },
        {"farePackageId": 1000, "packageName": "Flexible", "totalPassengerPackagePrice": 2},
    ]
}


def test_get_packages_suma_el_precio_base_y_ordena(fake_alsa):
    journeys = copy.deepcopy(SAMPLE["journeys"])
    journeys[0]["departureDate"] = journeys[1]["departureDate"]  # misma hora, otra parada: no debe elegirse
    journeys[1]["originId"] = "155"
    sessions = fake_alsa(
        SearchJourneysAction="ok",
        GetJourneyListResource=_list_json(journeys),
        GetPackageFaresResource=json.dumps(PACKAGE_FARES),
    )
    departure = datetime.fromtimestamp(journeys[1]["departureDate"] / 1000, tz=TZ)

    packages = get_packages("90155", "90186", "21/12/2026", 1, departure, "155")

    assert [(p.id, p.name, p.price) for p in packages] == [
        ("45", "Básica", 43.63),
        ("1000", "Flexible", 45.63),
        ("1001", "Superflexible", 46.63),
    ]
    assert packages[0].recommended and packages[0].conditions == ["Cambios gratis & sin coste"]
    fares_url = next(u for u in sessions[-1].urls if "GetPackageFaresResource" in u)
    assert "journeyIndex=1" in fares_url


def test_get_packages_bus_que_ya_no_esta_devuelve_vacio(fake_alsa):
    fake_alsa(SearchJourneysAction="ok", GetJourneyListResource=_list_json(SAMPLE["journeys"]))
    departure = datetime(2026, 12, 21, 23, 59, tzinfo=TZ)

    assert get_packages("90155", "90186", "21/12/2026", 1, departure, "5555") == []
