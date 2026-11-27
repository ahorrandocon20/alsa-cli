"""Tests offline del seguimiento: «Localiza tu bus» falso, estimación del retraso, CLI, MCP y TUI."""

import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from alsa import cli, tracking
from alsa.models import BusTracking, TrackedLeg
from alsa.tracking import _parse_vehicle, estimate, track_route
from alsa.tui import fmt
from alsa.tui.art import MINI_BUS_WIDTH, STRIP_HEIGHT, route_strip

TZ = ZoneInfo("Europe/Madrid")
TODAY = "26/11/2026"

# Recorte de la respuesta real de AjaxGetBusServiceTracking del 26/11/2026 para el
# Madrid 06:15 → Gijón: mismas paradas, coordenadas y horas, menos paradas intermedias.
ROUTE = {
    "codLinea": "18539",
    "nomLinea": "MADRID-CyL-ASTURIAS",
    "hsalida": "06:15",
    "hllegada": "13:15",
    "nomOrigen": "MADRID ESTACION SUR",
    "nomDestino": "GIJÓN/XIXÓN",
    "buses": [{"matricula": "0000XXX", "latitud": "42.564133911", "longitud": "-5.536707179"}],
    "itinerario": [
        {"nomPueblo": "MADRID ESTACION SUR", "parada": "ESTACION SUR DE AUTOBUSES", "pueblo": "155", "fecha": "20261126",
         "hllegada": "06:15", "hsalida": "06:15", "latitud": "40.395154622", "longitud": "-3.678027508", "visitada": "0", "estimada": "0"},
        {"nomPueblo": "VALLADOLID", "parada": "ESTACION DE AUTOBUSES", "pueblo": "305", "fecha": "20261126",
         "hllegada": "08:45", "hsalida": "08:45", "latitud": "41.641404289", "longitud": "-4.732189637", "visitada": "0", "estimada": "0"},
        {"nomPueblo": "MANSILLA DE LAS MULAS", "parada": "ESTACION DE AUTOBUSES", "pueblo": "156", "fecha": "20261126",
         "hllegada": "10:15", "hsalida": "10:15", "latitud": "42.495736501", "longitud": "-5.415693909", "visitada": "0", "estimada": "0"},
        {"nomPueblo": "LEON", "parada": "ESTACIÓN DE AUTOBUSES REINA URRACA I", "pueblo": "139", "fecha": "20261126",
         "hllegada": "10:45", "hsalida": "11:00", "latitud": "42.593176568", "longitud": "-5.578981151", "visitada": "0", "estimada": "0"},
        {"nomPueblo": "OVIEDO", "parada": "ESTACION DE AUTOBUSES", "pueblo": "186", "fecha": "20261126",
         "hllegada": "12:45", "hsalida": "12:45", "latitud": "43.369513754", "longitud": "-5.850751649", "visitada": "0", "estimada": "0"},
        {"nomPueblo": "GIJÓN/XIXÓN", "parada": "ESTACION DE AUTOBUSES", "pueblo": "108", "fecha": "20261126",
         "hllegada": "13:15", "hsalida": "13:15", "latitud": "43.53889784", "longitud": "-5.667245873", "visitada": "0", "estimada": "0"},
    ],
}


def _leg(origin_id="155", destination_id="186", dep="06:15", arr="12:45", origin="MADRID ESTACION SUR", destination="OVIEDO / UVIEU"):
    def at(hhmm: str) -> datetime:
        return datetime.strptime(f"{TODAY} {hhmm}", "%d/%m/%Y %H:%M").replace(tzinfo=TZ)

    return TrackedLeg(0, 1, 1, "18539", "107", "20261126", "MADRID-CyL-ASTURIAS", origin_id, origin, destination_id, destination, at(dep), at(arr))


def _at(hhmm: str) -> datetime:
    return datetime.strptime(f"{TODAY} {hhmm}", "%d/%m/%Y %H:%M").replace(tzinfo=TZ)


def _route(lat: str | None = "42.564133911", lon: str | None = "-5.536707179") -> dict:
    route = json.loads(json.dumps(ROUTE))
    route["buses"] = [] if lat is None else [{"latitud": lat, "longitud": lon}]
    return route


# --- estimación ---------------------------------------------------------------------


def test_bus_antes_de_leon_lleva_16_minutos_de_retraso():
    # A las 10:53 estaba a 4,7 km de León, donde el horario lo pone a las 10:37.
    t = estimate(_leg(), _parse_vehicle(_route()), _at("10:53"))

    assert t.status == "on_the_way"
    assert t.delay_minutes == 16
    assert t.vehicle.stops[t.next_stop].town == "LEON"
    assert t.distance_to_next_km == pytest.approx(4.7, abs=0.2)
    assert t.estimated_arrival == _at("13:01")
    assert 0.6 < t.progress < 0.8


def test_parado_en_la_parada_dentro_de_su_horario_va_a_su_hora():
    t = estimate(_leg(), _parse_vehicle(_route("42.59063", "-5.57722")), _at("10:55"))

    assert t.status == "on_the_way"
    assert t.at_stop and t.vehicle.stops[t.next_stop].town == "LEON"
    assert t.delay_minutes == 0


def test_bus_que_aun_no_ha_llegado_a_tu_parada_se_acerca():
    leg = _leg(origin_id="139", destination_id="186", dep="11:00", origin="LEON")
    t = estimate(leg, _parse_vehicle(_route()), _at("10:53"))

    assert t.status == "approaching"
    assert t.progress == 0.0
    assert t.estimated_departure == _at("11:16")  # 11:00 + 16 min


def test_bus_en_la_primera_parada_antes_de_salir_espera():
    t = estimate(_leg(), _parse_vehicle(_route("40.39515", "-3.67803")), _at("06:00"))

    assert t.status == "waiting"
    assert t.delay_minutes == 0


def test_bus_pasado_tu_destino_ya_ha_llegado():
    t = estimate(_leg(destination_id="139", arr="10:45", destination="LEON"), _parse_vehicle(_route("43.36951", "-5.85075")), _at("12:46"))

    assert t.status == "arrived"
    assert t.estimated_arrival is None


def test_posicion_lejos_de_la_ruta_no_estima_retraso():
    t = estimate(_leg(), _parse_vehicle(_route("41.38", "2.17")), _at("10:53"))  # Barcelona

    assert t.status == "off_route"
    assert t.delay_minutes is None


def test_sin_gps_no_hay_posicion_y_sin_recorrido_no_hay_seguimiento():
    assert estimate(_leg(), _parse_vehicle(_route(lat=None)), _at("10:53")).status == "no_position"
    assert estimate(_leg(), None, _at("10:53")).status == "no_tracking"


def test_horario_que_cruza_la_medianoche_sigue_en_orden():
    route = _route()
    last = route["itinerario"][-1]
    last["hllegada"] = last["hsalida"] = "00:15"  # Alsa deja la fecha del día anterior
    stops = _parse_vehicle(route).stops

    assert stops[-1].arrival == _at("00:15") + timedelta(days=1)
    assert all(a.departure <= b.arrival for a, b in pairwise(stops))


def test_json_del_tramo_marca_paradas_pasadas_y_de_tu_viaje():
    data = estimate(_leg(), _parse_vehicle(_route()), _at("10:53")).to_dict()

    assert data["status"] == "on_the_way" and data["delay_minutes"] == 16
    assert data["next_stop"]["town"] == "LEON"
    assert data["position"]["map_url"].startswith("https://www.openstreetmap.org/?mlat=42.56413")
    assert [s["passed"] for s in data["stops"]] == [True, True, True, False, False, False]
    assert [s["in_trip"] for s in data["stops"]] == [True, True, True, True, True, False]
    assert data["stops"][4]["estimated_arrival"].startswith("2026-11-26T13:01")


def test_tramo_sin_consultar_sale_en_el_json_sin_datos():
    data = BusTracking(leg=_leg(), checked=False).to_dict()
    assert data["status"] == "not_checked"
    assert data["position"] is None and data["stops"] == []


# --- «Localiza tu bus» falso ---------------------------------------------------------

ACTION = "https://www.alsa.es/a-bordo/localiza-tu-bus?p_p_id=TrackingBusPortlet_WAR_Alsaportlet&amp;p_p_lifecycle=1&amp;_TrackingBusPortlet_WAR_Alsaportlet_javax.portlet.action=trackingBusTrayectoAction&amp;p_auth=abc123"
SERVICE_URL = "https://www.alsa.es/a-bordo/localiza-tu-bus?p_p_id=TrackingBusPortlet_WAR_Alsaportlet&amp;p_p_lifecycle=2&amp;p_p_resource_id=AjaxGetBusServiceTracking"


def _trip(dep: str, arr: str, origin_id: str, service: str, flight: str, origin="MADRID ESTACION SUR") -> dict:
    return {
        "legs": [
            {"departureDate": TODAY, "departureTime": dep, "arrivalDate": TODAY, "arrivalTime": arr, "headerDate": TODAY,
             "serviceDescription": "MADRID-CyL-ASTURIAS", "service": service, "flight": flight, "legOrigin": origin,
             "legDestination": "OVIEDO / UVIEU", "legOriginId": origin_id, "legDestinationId": "186"}
        ]
    }


TRIPS = [
    _trip("06:15", "12:45", "155", "18539", "107"),
    _trip("08:15", "14:00", "155", "18541", "123"),
    _trip("08:30", "14:00", "5558", "18541", "123", "MADRID MONCLOA"),  # el mismo autobús, otra parada
    _trip("17:30", "23:45", "155", "2583", "101"),
]


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code


class FakeTracking:
    """Sesión falsa: la página, la búsqueda por trayecto y el seguimiento por servicio."""

    def __init__(self, routes: dict[str, dict | list], trips: list[dict]):
        self.trips = trips
        self.routes = routes
        self.impersonate = ""
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(f'<script>params["byJourney"] = "{ACTION}";</script>')

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if "trackingBusTrayectoAction" in url:
            listing = json.dumps({"navegaliaList": self.trips})
            return FakeResponse(f"""<script>params["jsonTrackingBus"] = '{listing}';\n params["getBusServiceTrackingURL"] = "{SERVICE_URL}";</script>""")
        service = kwargs["params"]["_TrackingBusPortlet_WAR_Alsaportlet_service"]
        if service not in self.routes:
            return FakeResponse("")
        entries = self.routes[service]
        return FakeResponse(json.dumps(entries if isinstance(entries, list) else [entries]))


@pytest.fixture
def fake_tracking(monkeypatch):
    sessions: list[FakeTracking] = []

    def install(routes: dict[str, dict | list], trips: list[dict] | None = None) -> list[FakeTracking]:
        def new_session():
            session = FakeTracking(routes, TRIPS if trips is None else trips)
            sessions.append(session)
            return session

        monkeypatch.setattr(tracking.curl_requests, "Session", new_session)
        return sessions

    return install


def _service_calls(session: FakeTracking) -> list[dict]:
    return [kw["params"] for url, kw in session.calls if "AjaxGetBusServiceTracking" in url]


def test_track_route_consulta_solo_los_que_van_en_ruta(fake_tracking):
    sessions = fake_tracking({"18539": _route()})

    buses, retries = track_route("90155", "90186", now=_at("10:53"))

    assert [b.status for b in buses] == ["on_the_way", "no_tracking", "no_tracking", "not_checked"]
    assert buses[0].delay_minutes == 16
    assert retries == []
    calls = _service_calls(sessions[0])
    assert len(calls) == 2  # 08:15 y 08:30 son el mismo autobús: una sola consulta
    assert calls[0] == {
        "_TrackingBusPortlet_WAR_Alsaportlet_date": "20261126",
        "_TrackingBusPortlet_WAR_Alsaportlet_service": "18539",
        "_TrackingBusPortlet_WAR_Alsaportlet_flight": "107",
        "_TrackingBusPortlet_WAR_Alsaportlet_legOriginStationId": "155",
        "_TrackingBusPortlet_WAR_Alsaportlet_legDestinationStationId": "186",
    }
    search = next(kw for url, kw in sessions[0].calls if "trackingBusTrayectoAction" in url)
    assert search["data"] == {
        "_TrackingBusPortlet_WAR_Alsaportlet_originStationId": "90155",
        "_TrackingBusPortlet_WAR_Alsaportlet_destinationStationId": "90186",
    }


def test_track_route_con_hora_consulta_solo_ese_autobus(fake_tracking):
    sessions = fake_tracking({"2583": _route()})

    buses, _ = track_route("90155", "90186", "17:30", now=_at("10:53"))

    assert [b.checked for b in buses] == [False, False, False, True]
    assert [c["_TrackingBusPortlet_WAR_Alsaportlet_service"] for c in _service_calls(sessions[0])] == ["2583"]


def test_track_route_hora_y_parada_distinguen_dos_salidas(fake_tracking):
    fake_tracking({})
    buses, _ = track_route("90155", "90186", "08:30", origin_stop_id="5558", now=_at("10:53"))
    assert [b.leg.origin_id for b in buses if b.checked] == ["5558"]


# --- CLI y MCP ------------------------------------------------------------------------


@pytest.fixture
def fake_track(monkeypatch):
    calls: list[tuple] = []
    on_the_way = estimate(_leg(), _parse_vehicle(_route()), _at("10:53"))
    others = [BusTracking(leg=_leg(dep="08:15", arr="14:00"), checked=True, status="no_tracking")]

    def track(origin_id, destination_id, departure=None, origin_stop_id=None, now=None):
        calls.append((origin_id, destination_id, departure))
        if departure not in (None, "06:15"):
            return [BusTracking(leg=b.leg, checked=False) for b in (on_the_way, *others)], []
        return [on_the_way, *others], []

    from alsa import mcp

    for module in (cli, mcp):
        monkeypatch.setattr(module, "track_route", track)
        monkeypatch.setattr(module, "get_station_id", {"Madrid": "90155", "Oviedo": "90186"}.__getitem__)
    return calls


def _run(*args: str):
    return CliRunner().invoke(cli.app, list(args))


def test_track_json_trae_estado_retraso_y_paradas(fake_track):
    result = _run("track", "Madrid", "Oviedo", "--format", "json")

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert fake_track == [("90155", "90186", None)]
    assert payload["count"] == 2
    assert [b["status"] for b in payload["buses"]] == ["on_the_way", "no_tracking"]
    assert payload["buses"][0]["delay_minutes"] == 16
    assert "estimación" in payload["note"]


def test_track_texto_muestra_el_retraso_y_los_que_no_tienen_seguimiento(fake_track):
    result = _run("track", "Madrid", "Oviedo", "-t", "06:15")

    assert result.exit_code == 0
    assert "16 min tarde" in result.stdout and "próxima parada Leon" in result.stdout
    assert "Mansilla de las Mulas" in result.stdout  # con --time salen las paradas
    assert "08:15 → 14:00" in result.stdout and "sin seguimiento" in result.stdout


@pytest.mark.parametrize(
    ("args", "message"),
    [(["-t", "6:15"], "HH:MM"), (["-t", "25:00"], "HH:MM"), (["--format", "xml"], "text o json"), (["-t", "09:00"], "salen a las 06:15, 08:15")],
)
def test_track_argumentos_invalidos_son_error_por_stderr(fake_track, args, message):
    result = _run("track", "Madrid", "Oviedo", *args)

    assert result.exit_code == 1
    assert result.stdout == ""
    assert message in json.loads(result.stderr)["error"]["message"]


async def test_mcp_track_bus_responde_lo_mismo_que_el_cli(fake_track):
    from fastmcp import Client

    from alsa import mcp

    async with Client(mcp.mcp) as client:
        result = await client.call_tool("track_bus", {"origin": "Madrid", "destination": "Oviedo"})
    from_cli = json.loads(_run("track", "Madrid", "Oviedo", "--format", "json").stdout)

    from_mcp = result.structured_content
    assert {k: v for k, v in from_mcp.items() if k != "checked_at"} == {k: v for k, v in from_cli.items() if k != "checked_at"}


async def test_mcp_track_bus_hora_sin_autobus_es_error_de_herramienta(fake_track):
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    from alsa import mcp

    async with Client(mcp.mcp) as client:
        with pytest.raises(ToolError, match="salen a las"):
            await client.call_tool("track_bus", {"origin": "Madrid", "destination": "Oviedo", "departure_time": "09:00"})


# --- formato y dibujo ----------------------------------------------------------------


def test_formato_del_seguimiento():
    assert fmt.delay(16) == "16 min tarde"
    assert fmt.delay(2) == "a su hora"
    assert fmt.delay(-7) == "a su hora"  # adelantado: el autocar espera en la parada
    assert fmt.delay(75) == "1h 15 tarde"
    assert fmt.km(4.66) == "4,7 km"
    assert fmt.place("MANSILLA DE LAS MULAS") == "Mansilla de las Mulas"
    assert fmt.place("AEROPUERTO VILLANUBLA (VA)") == "Aeropuerto Villanubla (VA)"
    assert fmt.place("Hermanos Pidal") == "Hermanos Pidal"


def test_tira_de_la_ruta_tiene_su_alto_y_el_autocar_avanza():
    width = 60

    def bus_column(progress: float) -> int:
        first_line = route_strip(width, progress).plain.split("\n")[0]
        return len(first_line) - len(first_line.lstrip())

    assert len(route_strip(width, 0.5).plain.split("\n")) == STRIP_HEIGHT // 2
    # La primera línea pinta las dos filas de arriba del autocar, que empiezan en su píxel 1.
    assert bus_column(0.0) < bus_column(0.5) < bus_column(1.0) == width - MINI_BUS_WIDTH + 1


# --- TUI ------------------------------------------------------------------------------


async def test_l_en_resultados_abre_donde_va_el_autobus(monkeypatch):
    from test_tui import STATIONS, _req

    import alsa.tui.app as app_module
    from alsa.models import Fare, Journey
    from alsa.tui import screens
    from alsa.tui import tracking as tui_tracking
    from alsa.tui.screens import ResultsScreen
    from alsa.tui.tracking import TrackingScreen

    now = datetime.now(TZ)
    journey = Journey("155", "Madrid Estación Sur", "186", "Oviedo", now - timedelta(hours=4), now + timedelta(hours=2), 360, 0, "Comfort", [Fare(30.0, "FLEX", "FLX", "")])
    on_the_way = estimate(_leg(), _parse_vehicle(_route()), _at("10:53"))
    on_the_way = replace(on_the_way, leg=replace(on_the_way.leg, departure=journey.departure, arrival=journey.arrival))
    asked: list[tuple] = []
    monkeypatch.setattr(app_module, "get_all_stations", lambda: STATIONS)
    monkeypatch.setattr(screens, "search_bus", lambda *a: ([journey], [], "https://alsa.test"))

    def fake_track(*args, **kwargs):
        asked.append(args)
        return [on_the_way], []

    monkeypatch.setattr(tui_tracking, "track_route", fake_track)
    app = app_module.AlsaApp()
    async with app.run_test(size=(110, 40)) as pilot:
        app.push_screen(ResultsScreen(_req(day=date.today())))
        for _ in range(100):
            if isinstance(app.screen, ResultsScreen) and app.screen.phase == "list":
                break
            await pilot.pause(0.05)
        assert "En ruta ahora" in str(app.screen.query_one("#detail-text").render())
        await pilot.press("l")
        for _ in range(100):
            if isinstance(app.screen, TrackingScreen) and app.screen.tracking is not None:
                break
            await pilot.pause(0.05)
        screen = app.screen
        assert asked[0] == ("90155", "90186", journey.departure.strftime("%H:%M"), "155")
        assert "16 min tarde" in str(screen.query_one("#tracking-headline").render())
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert isinstance(app.screen, ResultsScreen)


async def test_bus_que_sale_pronto_invita_a_ver_por_donde_viene(monkeypatch):
    from test_tui import STATIONS, _req

    import alsa.tui.app as app_module
    from alsa.models import Fare, Journey
    from alsa.tui import screens
    from alsa.tui.screens import ResultsScreen

    now = datetime.now(TZ)
    soon = Journey("155", "Madrid Estación Sur", "186", "Oviedo", now + timedelta(hours=1), now + timedelta(hours=6), 300, 0, "Comfort", [Fare(30.0, "FLEX", "FLX", "")])
    later = Journey("155", "Madrid Estación Sur", "186", "Oviedo", now + timedelta(hours=5), now + timedelta(hours=10), 300, 0, "Comfort", [Fare(30.0, "FLEX", "FLX", "")])
    monkeypatch.setattr(app_module, "get_all_stations", lambda: STATIONS)
    monkeypatch.setattr(screens, "search_bus", lambda *a: ([soon, later], [], "https://alsa.test"))
    app = app_module.AlsaApp()
    async with app.run_test(size=(110, 40)) as pilot:
        app.push_screen(ResultsScreen(_req(day=date.today())))
        for _ in range(100):
            if isinstance(app.screen, ResultsScreen) and app.screen.phase == "list":
                break
            await pilot.pause(0.05)
        detail = app.screen.query_one("#detail-text")
        assert "Sale pronto" in str(detail.render())
        await pilot.press("down")  # sale dentro de 5 horas: aún no
        assert "l para ver" not in str(detail.render())


async def test_l_no_se_ofrece_para_otro_dia(monkeypatch):
    from test_tui import JOURNEYS, STATIONS, _req

    import alsa.tui.app as app_module
    from alsa.tui import screens
    from alsa.tui.screens import ResultsScreen

    monkeypatch.setattr(app_module, "get_all_stations", lambda: STATIONS)
    monkeypatch.setattr(screens, "search_bus", lambda *a: (list(JOURNEYS), [], "https://alsa.test"))
    app = app_module.AlsaApp()
    async with app.run_test(size=(110, 40)) as pilot:
        app.push_screen(ResultsScreen(_req(day=date.today() + timedelta(days=1))))
        for _ in range(100):
            if isinstance(app.screen, ResultsScreen) and app.screen.phase == "list":
                break
            await pilot.pause(0.05)
        assert app.screen.check_action("track", ()) is False


# --- «Localizar autobús» desde el formulario ---------------------------------------------


@pytest.fixture
def live_app(monkeypatch):
    from test_tui import STATIONS

    import alsa.tui.app as app_module
    from alsa.tui import tracking as tui_tracking

    on_the_way = estimate(_leg(), _parse_vehicle(_route()), _at("10:53"))
    later = BusTracking(leg=_leg(dep="23:30", arr="23:59"), checked=False)
    no_tracking = BusTracking(leg=_leg(dep="08:15", arr="14:00"), checked=True, status="no_tracking")
    asked: list[tuple] = []

    def fake_track(*args, selected_leg=None, **kwargs):
        if selected_leg is not None:
            asked.append((*args, selected_leg))
            return [estimate(selected_leg, _parse_vehicle(_route()), _at("10:53"))], []
        asked.append(args)
        return [on_the_way, no_tracking, later], []

    class FrozenClock(datetime):
        """The fake coaches run on TODAY, so the screen must think it's that morning too."""

        @classmethod
        def now(cls, tz=None):
            return _at("10:53").astimezone(tz)

    monkeypatch.setattr(app_module, "get_all_stations", lambda: STATIONS)
    monkeypatch.setattr(tui_tracking, "track_route", fake_track)
    monkeypatch.setattr(tui_tracking, "datetime", FrozenClock)
    return app_module.AlsaApp, asked


async def _wait(pilot, condition):
    for _ in range(100):
        if condition():
            return
        await pilot.pause(0.05)
    raise AssertionError("la condición no se cumplió a tiempo")


async def test_localizar_lista_los_de_hoy_y_abre_el_detalle_sin_volver_a_preguntar(live_app):
    from alsa.tui.tracking import LiveRouteScreen, TrackingScreen

    app_class, asked = live_app
    app = app_class()
    async with app.run_test(size=(110, 40)) as pilot:
        await _wait(pilot, lambda: app.stations is not None)
        await pilot.press(*"mad", "enter", *"ovi", "enter", "ctrl+l")
        await _wait(pilot, lambda: isinstance(app.screen, LiveRouteScreen) and app.screen.phase == "list")
        screen = app.screen
        assert asked == [("90155", "90186")]
        table = screen.query_one("#table")
        assert [str(table.get_row_at(i)[3]) for i in range(3)] == ["en camino", "sin seguimiento", "sale más tarde"]
        assert str(table.get_row_at(0)[4]) == "16 min tarde"
        assert "1 en ruta ahora" in str(screen.query_one("#summary").render())

        await pilot.press("enter")
        await _wait(pilot, lambda: isinstance(app.screen, TrackingScreen) and app.screen.tracking is not None)
        assert len(asked) == 1  # el detalle usa lo que ya trajo la lista
        assert "16 min tarde" in str(app.screen.query_one("#tracking-headline").render())
        await pilot.press("escape")
        await _wait(pilot, lambda: app.screen is screen)


async def test_localizar_uno_sin_consultar_pregunta_por_ese_autobus(live_app):
    from alsa.tui.tracking import LiveRouteScreen, TrackingScreen

    app_class, asked = live_app
    app = app_class()
    async with app.run_test(size=(110, 40)) as pilot:
        await _wait(pilot, lambda: app.stations is not None)
        await pilot.press(*"mad", "enter", *"ovi", "enter", "ctrl+l")
        await _wait(pilot, lambda: isinstance(app.screen, LiveRouteScreen) and app.screen.phase == "list")
        await pilot.press("down", "down", "enter")
        await _wait(pilot, lambda: isinstance(app.screen, TrackingScreen) and app.screen.checked_at is not None)
        assert asked[-1] == ("90155", "90186", app.screen.tracking.leg)
        assert app.screen.tracking.leg.departure == _at("23:30")


async def test_localizar_sin_paradas_avisa_en_el_formulario(live_app):
    from alsa.tui.tracking import LiveRouteScreen

    app_class, asked = live_app
    app = app_class()
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.press("ctrl+l")
        await pilot.pause()
        assert "salida" in str(app.screen.query_one("#form-error").render())
        assert not isinstance(app.screen, LiveRouteScreen) and asked == []


# --- alsa track --watch ---------------------------------------------------------------


def test_watch_vuelve_a_preguntar_y_sale_con_ctrl_c(fake_track, monkeypatch):
    sleeps: list[int] = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 5:  # 2 s, pregunta, 2 s, pregunta, y ctrl+c
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "WATCH_SECONDS", 2)
    monkeypatch.setattr(cli.time, "sleep", sleep)
    result = _run("track", "Madrid", "Oviedo", "--watch")

    assert result.exit_code == 0
    assert len(fake_track) == 3  # la primera consulta y dos actualizaciones
    assert "16 min tarde" in result.stdout  # la última vista queda impresa


def test_watch_sigue_aunque_falle_una_actualizacion(fake_track, monkeypatch):
    from alsa.exceptions import AlsaBlockedError

    first = cli.track_route
    calls = []

    def flaky(*args, **kwargs):
        calls.append(args)
        if len(calls) == 2:
            raise AlsaBlockedError()
        return first(*args, **kwargs)

    sleeps = []
    monkeypatch.setattr(cli, "track_route", flaky)
    monkeypatch.setattr(cli, "WATCH_SECONDS", 1)
    monkeypatch.setattr(cli.time, "sleep", lambda s: sleeps.append(s) if len(sleeps) < 3 else (_ for _ in ()).throw(KeyboardInterrupt))
    result = _run("track", "Madrid", "Oviedo", "-w")

    assert result.exit_code == 0
    assert len(calls) == 4  # el bloqueo de la segunda no cortó el bucle


def test_watch_no_funciona_con_json(fake_track):
    result = _run("track", "Madrid", "Oviedo", "--watch", "--format", "json")

    assert result.exit_code == 1
    assert "--watch" in json.loads(result.stderr)["error"]["message"]
    assert fake_track == []


@pytest.mark.parametrize("case", ["sin salida", "sin destino", "orden inverso", "dos compatibles"])
def test_itinerario_no_identificado_no_atribuye_gps(fake_tracking, case):
    route = _route()
    for stop in route["itinerario"]:
        if (case == "sin salida" and stop["pueblo"] == "155") or (case == "sin destino" and stop["pueblo"] == "186"):
            stop["pueblo"] = "999"
    if case == "orden inverso":
        route["itinerario"].reverse()
    entries = [route, _route("40.5", "-3.7")] if case == "dos compatibles" else [route]
    fake_tracking({"18539": entries})
    result = _run("track", "90155", "90186", "--time", "06:15", "--format", "json")
    assert result.exit_code == 0
    bus = json.loads(result.stdout)["buses"][0]
    assert bus["status"] == "unmatched_route"
    assert bus["position"] is None and bus["route"] is None and bus["stops"] == []
    assert bus["delay_minutes"] is None and bus["estimated_arrival"] is None
    text = _run("track", "90155", "90186", "--time", "06:15").stdout
    assert "recorrido sin identificar" in text and "openstreetmap" not in text


def test_respuesta_sin_itinerario_sigue_siendo_sin_seguimiento(fake_tracking):
    fake_tracking({"18539": [{"itinerario": [], "buses": [{"latitud": "40.5", "longitud": "-3.7"}]}]})
    buses, _ = track_route("90155", "90186", "06:15", now=_at("10:53"))
    assert buses[0].status == "no_tracking" and buses[0].to_dict()["position"] is None


def test_tramo_sin_expedicion_no_pide_el_gps_de_otro_bus(fake_tracking):
    trip = _trip("06:15", "12:45", "155", "18539", "")
    sessions = fake_tracking({"18539": _route()}, [trip])
    buses, _ = track_route("90155", "90186", "06:15", now=_at("10:53"))
    assert buses[0].status == "unmatched_route" and buses[0].vehicle is None
    assert _service_calls(sessions[0]) == []


def test_un_solo_itinerario_compatible_entre_varios(fake_tracking):
    wrong = _route("40.5", "-3.7")
    wrong["itinerario"] = wrong["itinerario"][1:]
    fake_tracking({"18539": [wrong, _route()]})
    buses, _ = track_route("90155", "90186", "06:15", now=_at("10:53"))
    assert buses[0].vehicle.latitude == pytest.approx(42.564133911)
    assert (buses[0].origin_stop, buses[0].destination_stop) == (0, 4)


async def test_segundo_tramo_conserva_expedicion_al_actualizar(fake_tracking, monkeypatch):
    from test_tui import STATIONS

    import alsa.tui.app as app_module
    from alsa.tui import tracking as tui

    journey = _trip("06:15", "08:45", "155", "first", "101")
    journey["legs"][0].update(legDestinationId="305", legDestination="VALLADOLID")
    second = _trip("09:00", "12:45", "305", "second", "102", "VALLADOLID")
    journey["legs"].extend(second["legs"])
    other = _trip("09:00", "12:45", "305", "other", "103", "VALLADOLID")
    trips = [journey, other]
    sessions = fake_tracking({"first": _route(), "second": _route(), "other": _route(None)}, trips)
    # La búsqueda por hora de la CLI sigue consultando los dos tramos del primer viaje.
    buses, _ = track_route("90155", "90186", "06:15", now=_at("10:53"))
    assert [b.checked for b in buses] == [True, True, False]
    selected = buses[1]
    target = tui.TrackTarget.from_leg(selected.leg, "90155", "90186")
    monkeypatch.setattr(app_module, "get_all_stations", lambda: STATIONS)
    monkeypatch.setattr(tui, "REFRESH_SECONDS", 0.8)
    app = app_module.AlsaApp()
    async with app.run_test() as pilot:
        screen = tui.TrackingScreen(target, selected, _at("10:53"))
        app.push_screen(screen)
        await pilot.pause()
        trips.reverse()
        await pilot.press("r")
        await _wait(pilot, lambda: screen.checked_at != _at("10:53"))
        assert screen.tracking.leg.service == "second" and screen.tracking.leg.leg == 2
        assert len(_service_calls(sessions[-1])) == 1
        trips.remove(journey)
        await _wait(pilot, lambda: screen.tracking is None)
        assert "tramo seleccionado" in str(screen.query_one("#tracking-headline").render())
        assert not _service_calls(sessions[-1])
        trips.append(journey)
        await _wait(pilot, lambda: screen.tracking is not None)
        assert screen.tracking.leg.service == "second"


async def test_desde_busqueda_fija_el_tramo_y_descarta_un_recorrido_incompatible(fake_tracking, monkeypatch):
    from test_tui import JOURNEYS, STATIONS, _req

    import alsa.tui.app as app_module
    from alsa.tui.tracking import TrackingScreen, TrackTarget

    first = _trip("06:15", "12:45", "155", "A", "101")
    other = _trip("06:15", "13:30", "155", "B", "102")
    routes = {"A": _route(), "B": _route()}
    trips = [other, first]
    sessions = fake_tracking(routes, trips)
    journey = replace(JOURNEYS[0], origin_id="155", destination_id="186", departure=_at("06:15"), arrival=_at("12:45"))
    target = TrackTarget.from_journey(journey, _req())
    monkeypatch.setattr(app_module, "get_all_stations", lambda: STATIONS)
    app = app_module.AlsaApp()
    async with app.run_test() as pilot:
        screen = TrackingScreen(target)
        app.push_screen(screen)
        await _wait(pilot, lambda: screen.tracking is not None)
        assert screen.tracking.leg.service == "A"
        trips.reverse()
        other["legs"][0]["arrivalTime"] = "12:45"
        routes["A"]["itinerario"].pop(0)
        await pilot.press("r")
        await _wait(pilot, lambda: screen.tracking.status == "unmatched_route")
        assert not screen.query_one("#map").display
        assert "No se ha podido identificar el recorrido" in str(screen.query_one("#tracking-headline").render())
        assert [c[tracking.TRACKING_NS + "service"] for c in _service_calls(sessions[-1])] == ["A"]

        # Desde cero, dos viajes con la misma salida y llegada no identifican una expedición.
        await pilot.press("escape")
        fresh = TrackingScreen(target)
        app.push_screen(fresh)
        await _wait(pilot, lambda: fresh.checked_at is not None)
        assert fresh.tracking is None and not fresh.query_one("#map").display


async def test_mcp_recorrido_ambiguo_devuelve_posicion_nula(fake_tracking):
    from fastmcp import Client

    from alsa import mcp

    fake_tracking({"18539": [_route(), _route("40.5", "-3.7")]})
    async with Client(mcp.mcp) as client:
        result = await client.call_tool("track_bus", {"origin": "90155", "destination": "90186", "departure_time": "06:15"})
    bus = result.structured_content["buses"][0]
    assert bus["status"] == "unmatched_route" and bus["position"] is None


# --- tramos largos sin paradas ----------------------------------------------------------

# Madrid Moncloa 12:15 → Pola de Lena 17:05 sin paradas intermedias (Alsa, 26/11/2026). La
# carretera real (A-6 y A-66) se separa hasta 47 km de la recta entre las dos paradas.
LONG_STRETCH = {
    "codLinea": "18541",
    "nomLinea": "MADRID-CyL-ASTURIAS",
    "nomOrigen": "MADRID MONCLOA",
    "nomDestino": "OVIEDO",
    "buses": [],
    "itinerario": [
        {"nomPueblo": "MADRID MONCLOA", "parada": "INTERCAMBIADOR MONCLOA", "pueblo": "5558", "fecha": "20261126",
         "hllegada": "12:15", "hsalida": "12:15", "latitud": "40.434737411", "longitud": "-3.719566949"},
        {"nomPueblo": "POLA DE LENA", "parada": "MARQUESINA", "pueblo": "210", "fecha": "20261126",
         "hllegada": "17:05", "hsalida": "17:05", "latitud": "43.16415594", "longitud": "-5.828504271"},
        {"nomPueblo": "OVIEDO", "parada": "ESTACION DE AUTOBUSES", "pueblo": "186", "fecha": "20261126",
         "hllegada": "17:30", "hsalida": "17:30", "latitud": "43.369513754", "longitud": "-5.850751649"},
    ],
}
# Puntos de la A-6 y la A-66, de Madrid a Pola de Lena.
A6 = [(40.4347, -3.7196), (40.64, -4.01), (40.78, -4.41), (40.94, -4.60), (41.07, -4.72), (41.50, -5.00),
      (41.77, -5.27), (42.00, -5.68), (42.35, -5.60), (42.60, -5.57), (42.85, -5.70), (43.1642, -5.8285)]


def _on_the_road(fraction: float) -> tuple[float, float]:
    """Punto a `fraction` del recorrido por la carretera real."""
    from alsa.tracking import _distance_km

    lengths = [_distance_km(*a, *b) for a, b in pairwise(A6)]
    left = fraction * sum(lengths)
    for (a, b), length in zip(pairwise(A6), lengths, strict=True):
        if left <= length:
            f = left / length
            return a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f
        left -= length
    return A6[-1]


def _long_stretch_at(lat: float, lon: float):
    route = json.loads(json.dumps(LONG_STRETCH))
    route["buses"] = [{"latitud": str(lat), "longitud": str(lon)}]
    return _parse_vehicle(route)


@pytest.mark.parametrize("fraction", [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9])
def test_tramo_largo_por_la_carretera_real_sigue_localizado(fraction):
    leg = _leg(origin_id="5558", destination_id="186", dep="12:15", arr="17:30", origin="MADRID MONCLOA", destination="OVIEDO")
    now = _at("12:15") + (_at("17:05") - _at("12:15")) * fraction  # va exactamente a su hora
    t = estimate(leg, _long_stretch_at(*_on_the_road(fraction)), now)

    assert t.status == "on_the_way"
    assert abs(t.delay_minutes) <= 5
    assert t.approximate == (t.distance_to_route_km > 25)


def test_tramo_largo_lejos_de_la_recta_se_marca_aproximado():
    leg = _leg(origin_id="5558", destination_id="186", dep="12:15", arr="17:30", origin="MADRID MONCLOA", destination="OVIEDO")
    # La posición real de las 13:26: A-6 a la altura de Adanero, a 35 km de la recta.
    t = estimate(leg, _long_stretch_at(40.916067795, -4.580293942), _at("13:26"))

    assert t.status == "on_the_way" and t.approximate
    assert t.to_dict()["approximate"] is True
    from alsa.tui.tracking import headline

    assert "estimación aproximada" in headline(t).plain and "≈" in headline(t).plain


def test_tramo_corto_con_el_gps_a_30_km_sigue_siendo_dudoso():
    # Mansilla → León son 17 km: el margen se queda en 25 km.
    t = estimate(_leg(), _parse_vehicle(_route("42.80", "-5.20")), _at("10:30"))

    assert t.status == "off_route" and not t.approximate


def test_posicion_normal_no_es_aproximada():
    t = estimate(_leg(), _parse_vehicle(_route()), _at("10:53"))
    assert not t.approximate and t.to_dict()["approximate"] is False
