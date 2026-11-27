# alsa-cli

Search Alsa bus timetables and fares from the terminal: an interactive TUI that takes you to Alsa's
checkout, a CLI with JSON output for scripts and AI agents, and an MCP server. It also follows today's
buses live: where they are and how late they run. No account, no API key.

> **Unofficial.** Not affiliated with, endorsed by or supported by Alsa. The CLI talks directly to
> alsa.es's web API, anonymously, the same way the website does before anyone signs in. That API is
> undocumented and can change without notice. Alsa is a trademark of its owner.

## Features

- **Interactive TUI** (`alsa`): pick stops, day, time and travellers, browse the buses, see the real fares
  (Básica, Flexible, Superflexible) and open Alsa's checkout at the passenger details page.
- **Buses for a route and date** (`alsa bus`): times, duration, transfers, service type (Comfort, Supra…),
  availability and price, cheapest first, as text or JSON.
- **Live tracking** (`alsa track`, and "Localizar autobús" in the TUI): where today's buses on a route
  are, the next stop, an estimated delay and estimated times at each stop, from Alsa's "Localiza tu bus".
  Useful on the day of the trip: while you wait at the stop, to pick someone up, or on board.
- **Station search** (`alsa stations`), with accents and case ignored.
- **MCP server** (`alsa-mcp`) so assistants can search and track buses with the same JSON.

The TUI and the CLI messages are in Spanish.

## Install

You need Python 3.12 or later and [uv](https://docs.astral.sh/uv/).

```sh
uv tool install git+https://github.com/ahorrandocon20/alsa-cli

# with the MCP server
uv tool install "alsa-cli[mcp] @ git+https://github.com/ahorrandocon20/alsa-cli"
```

It isn't on PyPI, so `pip install alsa-cli` won't install it.

## Quick start

```sh
alsa                                                 # open the TUI
alsa bus Madrid Oviedo --date 21/12/2026             # buses as text
alsa bus Madrid Oviedo -d 21/12/2026 --format json   # the same as JSON
alsa bus Madrid Oviedo -d 21/12/2026 --adults 2      # price for two adults
alsa track Madrid Oviedo                             # where today's buses are, live
alsa track Madrid Oviedo --time 06:15                # one bus, with all its stops
alsa track Madrid Oviedo --time 06:15 --watch        # keep it open, refreshed every minute
alsa stations oviedo                                 # find a station and its ID
alsa-mcp                                             # start the MCP server (stdio)
```

## Commands

| Command | What it does |
|---|---|
| `alsa` | Opens the TUI. If stdin or stdout isn't a terminal (scripts, agents), prints the help instead |
| `alsa bus FROM TO -d DD/MM/YYYY` | Buses on that date, cheapest first. `--adults N` (1 to 20, default 1), `--format text\|json` (default `text`) |
| `alsa track FROM TO` | Today's buses on that route and, for the ones on the road, where they are and their estimated delay. `--time HH:MM` (`-t`) follows one bus and lists its stops, `--watch` (`-w`) stays open and refreshes every minute until ctrl+c, `--format text\|json` (default `text`) |
| `alsa stations QUERY` | Up to 20 stations whose name contains the query, as JSON |
| `alsa-mcp` | MCP server with two tools: `search_bus` (same JSON as `alsa bus --format json`) and `track_bus` (same JSON as `alsa track --format json`) |

`FROM` and `TO` take a name or a numeric ID. A name resolves, in this order, to an exact match, a city
group ("Madrid (Todas las paradas)"), a station starting with it, or one containing it. A numeric ID is
used as is: a station ID from `alsa stations` or a stop ID from a search result (`origin_id`).

## Output

`alsa bus --format json` prints one JSON document. This one ran on 24 Nov 2026; 35 of its 36 buses are
left out here:

```json
{
  "origin": "Madrid",
  "destination": "Oviedo",
  "date": "21/12/2026",
  "adults": 1,
  "count": 36,
  "buses": [
    {
      "origin_id": "155",
      "origin_name": "Madrid Estación Sur",
      "destination_id": "186",
      "destination_name": "Oviedo / Uviéu",
      "departure": "2026-12-21T00:30:00+01:00",
      "arrival": "2026-12-21T06:15:00+01:00",
      "duration_minutes": 345,
      "transfers": 0,
      "service_type": "Comfort",
      "line_name": "",
      "available": true,
      "cheapest_price_eur": 42.15,
      "fares": [
        {
          "price": 42.15,
          "fare_name": "SUPER FLEX",
          "fare_type": "BASE",
          "fare_conditions": "Puedes anular y cambiar"
        }
      ]
    }
  ],
  "shop": {
    "url": "https://www.alsa.es/checkout?p_p_id=com_babel_alsa_espania_purchase_web_PurchasePortlet&p_p_lifecycle=1&p_p_state=normal&p_p_mode=view&_com_babel_alsa_espania_purchase_web_PurchasePortlet_javax.portlet.action=SearchJourneysAction&alsaspam=&originStationId=90155&destinationStationId=90186&departureDate=21%2F12%2F2026&returnDate=&seats=1&promoCode=&youngPromoCode=&travelType=OUTWARD&employee=false&passengerType-1=1",
    "prefilled": ["origin", "destination", "date", "adults"],
    "next": "La URL abre en alsa.es la lista de buses de esta búsqueda; elige bus y tarifa allí para comprar."
  },
  "retries": []
}
```

- `fares` is what Alsa's results list shows for each bus: usually a single price, the cheapest one. The
  full set of fares for a bus (Básica, Flexible, Superflexible) is shown in the TUI.
- `available` is `false` when Alsa has no seats left on that bus.
- `cheapest_price_eur` is `null` when Alsa shows no price for a bus.
- `shop.url` opens alsa.es with the same search, to pick the bus and fare and pay there.
- `retries` lists the requests that failed and worked on a later try (see below), with `request`, `tries`
  and `errors`. It's empty when nothing was retried.

Errors go to stderr as `{"error": {"message": "..."}}` with exit code 1: a badly written or past date, a
number of adults outside 1 to 20, an unknown format or station, a block, a timeout, a connection failure
or a response the CLI can't read.

## Live tracking

`alsa track FROM TO` asks Alsa's "Localiza tu bus" page for today's buses on that route. Without `--time`
it follows the ones that leave in the next 3 hours or are still on the road; the others are listed as
not checked. With `--time HH:MM` it follows that bus, whatever the hour, and lists its stops. With
`--watch` it stays open, asks again every minute with a countdown at the top, keeps the last view if a
refresh fails, and leaves the last view printed when you quit with ctrl+c (text output only).

```text
alsa-cli · Madrid → Oviedo · hoy a las 10:53 · en directo
╭─ 06:15 → 12:45 · MADRID-CyL-ASTURIAS ─────────────────────────────────╮
│                         (pixel-art road with the bus and its stops)  │
│ 06:15 Madrid Estacion Sur               Oviedo / Uvieu 12:45 ≈ 13:01 │
│                                                                       │
│ En camino  ·  16 min tarde  ·  próxima parada Leon a 4,7 km           │
│ mapa: https://www.openstreetmap.org/?mlat=42.56413&mlon=-5.53671…     │
╰───────────────────────────────────────────────────────────────────────╯
```

`alsa track --format json` prints `origin`, `destination`, `date`, `checked_at`, `count`, `note`,
`retries` and one entry per leg in `buses`, with its times and stops and:

- `status`: `on_the_way` (between your stops), `approaching` (on the road, not yet at your stop),
  `waiting` (at the first stop of its route, before leaving), `arrived`, `no_tracking` (Alsa doesn't
  track this bus), `unmatched_route` (the returned itineraries do not identify a single route that
  visits your departure stop and then your destination, or the service identifiers are missing),
  `no_position` (no GPS right now), `off_route` (the GPS is too far from the route to
  estimate anything: more than 25 km, or 20% of a long stretch without stops) or `not_checked`.
- `approximate`: `true` when the GPS is more than 25 km from the straight line between the two stops
  around it. That happens on long stretches without stops, where the road strays from that line (up to
  47 km between Madrid and Pola de Lena on the A-6). The position is still used, up to 20% of the
  stretch's length away, but the delay and times are rougher.
- `delay_minutes`: positive when late. It can be negative when the bus is ahead of its timetable; it
  waits at the next stop, so the text output shows it as on time.
- `estimated_departure` (from your stop, while it's still coming) and `estimated_arrival` (at yours).
- `position` with `latitude`, `longitude` and an OpenStreetMap `map_url`, and `next_stop`.
- `stops`: the bus's whole route, which often starts before your stop and ends after it, each stop with
  `passed`, `in_trip` and `estimated_arrival`.

Alsa gives the bus's GPS position and its timetable, but not the delay: the "visited" and "estimated"
fields of its answer come empty. The delay is estimated here by placing the GPS position on the straight
line between stops and comparing it with the time the timetable puts the bus there. Roads aren't straight,
so take it as a few minutes either way. Only today's buses can be tracked, and not all of them: many
come back without tracking, exactly as on alsa.es.

## TUI

`alsa` with no arguments, in a terminal, opens the TUI.

1. Type the origin and destination and pick the stop (↑ ↓ and Enter; "todas" groups every stop in the
   city). `ctrl+x` swaps them. "Localizar autobús" (`ctrl+l`) skips the rest: it lists every bus of the
   day on that route, including the ones that already left (Alsa's search no longer shows them), with
   where each one is, its estimated delay and arrival, refreshed every minute. Enter opens the selected
   leg. Refreshes keep following that leg even after a transfer. If it disappears from the listing,
   the detail says so and checks for it again on the next refresh.
2. Pick the day (Hoy, Mañana or the calendar), the time window and the travellers (1 to 20, like alsa.es).
3. In the results: `o` sorts by time, price or duration; `a` / `s` move to the previous / next day; `t`
   turns the time filter off and on (if you set a time); `p` hides sold-out buses, if any (they're greyed
   out and can't be bought); `l` shows where today's bus is (the detail says when it's on the road): the
   road with the bus, its delay and your stops, refreshed every minute. `→` switches to a pixel-art map
   of Spain with each autonomous community in its colour, the bus, your trip (blue behind, yellow
   ahead), its stops and the rest of the bus's route; `z` zooms between your trip and the whole country
   and `←` goes back. The legend shows the marks visible at that zoom. Below it, `↑` and `↓` scroll through
   the remaining stops up to your destination, with names and estimated times (≈). If the position is
   uncertain, the panel shows scheduled times. If a refresh fails, the map keeps the last reading,
   shows its consultation time and pauses the bus’s blinking halo. `m` opens the position on OpenStreetMap.
   Anywhere, `?` shows the help and `ctrl+q` quits.
4. Enter on a bus shows its real fares on alsa.es (Básica, Flexible, Superflexible) with their price.
5. When you pick a fare, the TUI opens Chrome with its own profile (`~/.cache/alsa-cli/chrome`), runs the
   search, picks that bus and that fare and leaves the page at the passenger details step: you fill them
   in and pay. If there's no Chrome, Chromium, Brave or Edge, or any step fails, it opens alsa.es's
   results list in your default browser instead.

## MCP server

Install with the `mcp` extra (see [Install](#install)). Without it, `alsa-mcp` stops and prints the command
to reinstall. Then add it to your assistant:

```sh
claude mcp add alsa -- alsa-mcp        # Claude Code
```

For Claude Desktop and other clients that read a JSON config, add it to `mcpServers`. Use the full path
from `which alsa-mcp` if the client doesn't see your `PATH`:

```json
{
  "mcpServers": {
    "alsa": {"command": "alsa-mcp"}
  }
}
```

For Codex, in `~/.codex/config.toml`:

```toml
[mcp_servers.alsa]
command = "alsa-mcp"
```

It has two tools:

- `search_bus(origin, destination, date, adults=1)`, with the date as `DD/MM/YYYY`. It returns the same
  JSON as `alsa bus --format json`.
- `track_bus(origin, destination, departure_time=None)`, with the time as `HH:MM`. It returns the same
  JSON as `alsa track --format json`.

An unknown station, a badly written or past date, a badly written time or one with no bus, a number of
adults outside 1 to 20, a block or a timeout comes back as a tool error, never as an empty list.

## How it works

1. Gets Imperva's cookies (Alsa's bot protection) and the CSRF token `p_auth` from alsa.es's home page,
   using `curl_cffi` to look like Chrome.
2. Sends the search to the Liferay portlet's `SearchJourneysAction`, a GET with `originStationId`,
   `destinationStationId`, `departureDate` and passengers, which stores it in the server session.
3. Reads the results from `GetJourneyListResource`: one JSON with every bus, its times, duration,
   transfers, service type and price.

The TUI's fares come from `GetPackageFaresResource` for the chosen bus.

Live tracking uses the "Localiza tu bus" page (`/a-bordo/localiza-tu-bus`) the same way:

1. Gets the page, with Imperva's cookies and the URL of the `trackingBusTrayectoAction` search, which
   already carries `p_auth`.
2. Posts that search with the origin and destination. The page it returns has today's buses on the route
   in `jsonTrackingBus`, with the service and trip number of each leg.
3. Posts to `AjaxGetBusServiceTracking` with the service, trip, date and stops of a leg. It returns the
   bus's GPS position and every stop of its whole route with its timetable, or nothing if the bus has
   no tracking. Two legs of the same bus (from two stops in the same city) share one request.

### Retries and timeouts

Each request is tried up to 3 times, 1 s and then 2 s apart, but only after a 500, 502, 503 or 504 or a
dropped connection. A 403 or 429, Imperva's block page or a timeout (60 s per request) stops at once with
an error. Station lookups are a single request and aren't retried.

## Prices

Prices are the ones alsa.es showed at the time of the search, for all the adults, and don't include the
management fee alsa.es adds when you pay (2,90 € on an 8 € ticket on 24 Nov 2026). Check the final price,
luggage and conditions on Alsa's website before buying.

## Development

```sh
uv sync
uv run alsa --help      # your working copy
uv run pytest -q        # offline: no requests to alsa.es
uvx ruff check .
```

- Keep tests offline, with fake or trimmed responses.
- Scripts and agents read the JSON, so add fields rather than renaming or removing them.
- Never commit real passenger or booking data, HAR captures, tickets or tokens.

To contribute, open an issue first and then a pull request that closes it: see [CONTRIBUTING.md](CONTRIBUTING.md).

## Map data

The TUI's map is made from [Natural Earth](https://www.naturalearthdata.com/) 1:10m admin-1 states and
provinces, which is in the public domain, simplified to about 1 km (`src/alsa/tui/map_es.json`, 66 KB).
`scripts/build_map.py` explains how to regenerate it.

## License

[MIT](LICENSE).
