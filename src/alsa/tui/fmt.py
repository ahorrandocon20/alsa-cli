"""Formato en español para fechas, horas, precios y duraciones."""

from __future__ import annotations

import re
from datetime import date, datetime

WEEKDAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MONTHS = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]


def long_date(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d.day} de {MONTHS[d.month - 1]}"


def short_date(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()][:3]} {d.day} {MONTHS[d.month - 1][:3]}"


def price(value: float) -> str:
    return f"{value:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def duration(minutes: int) -> str:
    hours, mins = divmod(minutes, 60)
    if not hours:
        return f"{mins} min"
    return f"{hours}h {mins:02d}"


def hm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def arrival(departure: datetime, arrival_dt: datetime) -> str:
    """Hora de llegada con «+1» si llega otro día."""
    days = (arrival_dt.date() - departure.date()).days
    return hm(arrival_dt) + (f" +{days}" if days else "")


def transfers(count: int) -> str:
    if count == 0:
        return "directo"
    return f"{count} transbordo" + ("s" if count > 1 else "")


def travellers(count: int) -> str:
    return f"{count} viajero" + ("s" if count > 1 else "")


def delay(minutes: int) -> str:
    """Retraso estimado. Adelantado cuenta como «a su hora»: el autocar espera en cada parada."""
    if minutes <= 2:
        return "a su hora"
    if minutes < 60:
        return f"{minutes} min tarde"
    return f"{duration(minutes)} tarde"


def delay_style(minutes: int) -> str:
    if minutes <= 2:
        return "bold #4ade80"
    return "bold #fbbf24" if minutes < 15 else "bold #f87171"


def km(value: float) -> str:
    return f"{value:.1f} km".replace(".", ",")


_SMALL_WORDS = {"de", "del", "la", "las", "los", "el", "y", "a"}


def place(name: str) -> str:
    """«MADRID ESTACION SUR» → «Madrid Estacion Sur»; los nombres ya escritos se dejan igual."""
    if not name.isupper():
        return name

    def word(match: re.Match[str]) -> str:
        text = match.group(0)
        if match.start() and text in _SMALL_WORDS:
            return text
        if name[match.start() - 1 : match.start()] == "(" and len(text) == 2:  # provincia: (VA)
            return text.upper()
        return text.capitalize()

    return re.sub(r"[^\W\d_]+", word, name.lower())
