# alsa-cli

Search Alsa bus timetables and fares from the terminal: an interactive TUI that takes you to Alsa's
checkout, a CLI with JSON output for scripts and AI agents, and an MCP server. No account, no API key.

> **Unofficial.** Not affiliated with, endorsed by or supported by Alsa. The CLI talks directly to
> alsa.es's web API, anonymously, the same way the website does before anyone signs in. That API is
> undocumented and can change without notice. Alsa is a trademark of its owner.

## Features

- **Interactive TUI** (`alsa`): pick stops, day, time and travellers, browse the buses, see the real fares
  (Básica, Flexible, Superflexible) and open Alsa's checkout at the passenger details page.
- **Buses for a route and date** (`alsa bus`): times, duration, transfers, service type (Comfort, Supra…),
  availability and price, cheapest first, as text or JSON.
- **Station search** (`alsa stations`), with accents and case ignored.
- **MCP server** (`alsa-mcp`) so assistants can search buses with the same JSON.

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
alsa stations oviedo                                 # find a station and its ID
alsa-mcp                                             # start the MCP server (stdio)
```

## Commands

| Command | What it does |
|---|---|
| `alsa` | Opens the TUI. If stdin or stdout isn't a terminal (scripts, agents), prints the help instead |
| `alsa bus FROM TO -d DD/MM/YYYY` | Buses on that date, cheapest first. `--adults N` (1 to 20, default 1), `--format text\|json` (default `text`) |
| `alsa stations QUERY` | Up to 20 stations whose name contains the query, as JSON |
| `alsa-mcp` | MCP server with one tool, `search_bus`, that returns the same JSON as `alsa bus --format json` |

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

## TUI

`alsa` with no arguments, in a terminal, opens the TUI.

1. Type the origin and destination and pick the stop (↑ ↓ and Enter; "todas" groups every stop in the
   city). `ctrl+x` swaps them.
2. Pick the day (Hoy, Mañana or the calendar), the time window and the travellers (1 to 20, like alsa.es).
3. In the results: `o` sorts by time, price or duration; `a` / `s` move to the previous / next day; `t`
   removes the time filter; `p` hides sold-out buses (they're greyed out and can't be bought). `?` (F1 in the form) shows
   the help and `ctrl+q` quits.
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

It has one tool, `search_bus(origin, destination, date, adults=1)`, with the date as `DD/MM/YYYY`. It
returns the same JSON as `alsa bus --format json`. An unknown station, a badly written or past date, a
number of adults outside 1 to 20, a block or a timeout comes back as a tool error, never as an empty list.

## How it works

1. Gets Imperva's cookies (Alsa's bot protection) and the CSRF token `p_auth` from alsa.es's home page,
   using `curl_cffi` to look like Chrome.
2. Sends the search to the Liferay portlet's `SearchJourneysAction`, a GET with `originStationId`,
   `destinationStationId`, `departureDate` and passengers, which stores it in the server session.
3. Reads the results from `GetJourneyListResource`: one JSON with every bus, its times, duration,
   transfers, service type and price.

The TUI's fares come from `GetPackageFaresResource` for the chosen bus.

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

## License

[MIT](LICENSE).
