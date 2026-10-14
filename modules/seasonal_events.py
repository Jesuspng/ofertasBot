"""seasonal_events.py — Calendario de eventos que modifica la prioridad de alertas.

No predice precios, no inventa descuentos, no scrapea, no notifica.
Fechas aproximadas (reglas por año); sobrescribibles con config.SEASONAL_EVENTS.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

try:
    import config  # type: ignore
except ImportError:
    config = None

try:
    from special_cases import ELECTRONIC_CATEGORIES, check_category, is_special_case
except ImportError:  # fallback mínimo
    ELECTRONIC_CATEGORIES = {"laptop", "pc", "celular", "tablet", "consola",
                             "audifonos", "tv", "monitor", "camara", "smartwatch"}

    def check_category(text: str) -> str | None:  # type: ignore
        return None

    def is_special_case(product: dict, threshold: float = 65) -> bool:  # type: ignore
        return False

log = logging.getLogger(__name__)

DEFAULT_COUNTRY: str = getattr(config, "COUNTRY", "MX") if config else "MX"
DEFAULT_ADVICE: str = (
    getattr(config, "SEASONAL_DEFAULT_ADVICE", "vigilar precio; la demanda suele subir")
    if config else "vigilar precio; la demanda suele subir"
)
PRIORITY_ORDER = ["low", "normal", "high"]


# ---------- helpers de fecha ----------
def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-ésimo weekday (0=lunes) del mes; n=-1 → último."""
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _dt(d: date, end: bool = False) -> datetime:
    return datetime(d.year, d.month, d.day, 23, 59, 59) if end else datetime(d.year, d.month, d.day)


def _parse_dt(value: Any, end: bool = False) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return _dt(value, end)
    d = datetime.fromisoformat(str(value))
    return _dt(d.date(), end) if len(str(value)) <= 10 else d


def _event(name: str, start: date, end: date, lead_days: int, note: str,
           categories: list[str], advice: str | None = None) -> dict:
    return {
        "name": name,
        "start": _dt(start),
        "end": _dt(end, end=True),
        "lead_days": lead_days,
        "note": note,
        "categories": categories,
        "advice": advice or DEFAULT_ADVICE,
    }


# ---------- calendario MX (reglas aproximadas; editar cada año) ----------
def _mx_events(year: int) -> list[dict]:
    third_mon_nov = _nth_weekday(year, 11, 0, 3)
    buen_fin_start = third_mon_nov - timedelta(days=3)       # viernes
    black_friday = _nth_weekday(year, 11, 3, 4) + timedelta(days=1)
    cyber_monday = black_friday + timedelta(days=3)
    hot_sale_start = _nth_weekday(year, 5, 0, -1)            # último lunes de mayo
    elec = ["electronica", "electrodomesticos"]

    return [
        _event("Reyes", date(year, 1, 1), date(year, 1, 6), 10,
               "Demanda alta de juguetes", ["juguetes"]),
        _event("Día del Niño", date(year, 4, 28), date(year, 4, 30), 14,
               "Demanda alta de juguetes y consolas", ["juguetes", "consola", "tablet"],
               "vigilar precio; juguetes suelen subir antes del 30 de abril"),
        _event("Día de las Madres", date(year, 5, 5), date(year, 5, 10), 14,
               "Demanda alta de electrodomésticos, belleza y celulares",
               ["electrodomesticos", "belleza", "celular", "smartwatch", "ropa"]),
        _event("Hot Sale", hot_sale_start, hot_sale_start + timedelta(days=8), 10,
               "Ventana de ofertas online", elec),
        _event("Prime Day", date(year, 7, 8), date(year, 7, 11), 7,
               "Ventana de ofertas en Amazon", ["electronica"]),
        _event("Buen Fin", buen_fin_start, third_mon_nov, 14,
               "Ventana de ofertas; electrónicos con alta demanda",
               elec + ["juguetes", "ropa"],
               "vigilar precio; algunos productos suben antes del Buen Fin para simular descuento"),
        _event("Black Friday", black_friday, black_friday + timedelta(days=2), 10,
               "Ventana de ofertas", elec + ["juguetes", "ropa"]),
        _event("Cyber Monday", cyber_monday, cyber_monday, 7,
               "Ventana de ofertas online", ["electronica"]),
        _event("Navidad", date(year, 12, 1), date(year, 12, 25), 14,
               "Demanda alta general", ["juguetes", "electronica", "electrodomesticos"]),
    ]


_BUILTIN = {"MX": _mx_events}


def _normalize_override(raw: list[dict]) -> list[dict]:
    out = []
    for e in raw:
        out.append({
            "name": e["name"],
            "start": _parse_dt(e["start"]),
            "end": _parse_dt(e["end"], end=True),
            "lead_days": int(e.get("lead_days", 7)),
            "note": e.get("note", ""),
            "categories": list(e.get("categories", [])),
            "advice": e.get("advice", DEFAULT_ADVICE),
        })
    return out


def _calendar(country: str, now: datetime) -> list[dict]:
    country = (country or DEFAULT_COUNTRY).upper()
    override = getattr(config, "SEASONAL_EVENTS", None) if config else None
    if isinstance(override, dict) and country in override:
        events = _normalize_override(override[country])
    elif country in _BUILTIN:
        events = [e for y in (now.year, now.year + 1) for e in _BUILTIN[country](y)]
    else:
        log.info("sin calendario para país %s", country)
        return []
    return sorted(events, key=lambda e: e["start"])


def _category_matches(category: str | None, event: dict) -> bool:
    if not category:
        return False
    cats = event.get("categories", [])
    if "*" in cats or category in cats:
        return True
    return "electronica" in cats and category in ELECTRONIC_CATEGORIES


# ---------- API ----------
def calendar_of_events(country: str = "MX") -> list[dict]:
    """Eventos del año actual y el siguiente, ordenados por inicio."""
    return _calendar(country, datetime.now())


def get_upcoming_events(days_ahead: int = 30, now: datetime | None = None) -> list[dict]:
    """Eventos con inicio en <= days_ahead, en ventana lead_days, o en curso."""
    now = now or datetime.now()
    out = []
    for e in _calendar(DEFAULT_COUNTRY, now):
        days_until = (e["start"] - now).total_seconds() / 86400
        if e["start"] <= now <= e["end"]:
            status = "ongoing"
        elif now < e["start"] and days_until <= e["lead_days"]:
            status = "lead_window"
        elif now < e["start"] and days_until <= days_ahead:
            status = "upcoming"
        else:
            continue
        out.append({**e, "status": status, "days_until_start": max(0, int(days_until))})
    return out


def is_price_hike_period(category: str, now: datetime | None = None) -> dict:
    """Activo si now está en [start - lead_days, start) de un evento típico de la categoría."""
    now = now or datetime.now()
    for e in _calendar(DEFAULT_COUNTRY, now):
        if e["start"] - timedelta(days=e["lead_days"]) <= now < e["start"] and _category_matches(category, e):
            return {"active": True, "event_name": e["name"], "advice": e["advice"]}
    return {"active": False, "event_name": None, "advice": None}


def _quality_ok(product: dict) -> Any:
    q = product.get("quality")
    if isinstance(q, dict):
        return q.get("ok", "unknown")
    if q is not None:
        return q
    return product.get("ok", "unknown")


def adjust_alert_priority(product: dict, now: datetime | None = None) -> str:
    """high si caso especial; normal si calidad ok; low si unknown/False. Sube un nivel en periodo de alza."""
    special = bool(product.get("special_case")) or is_special_case(product)
    quality = _quality_ok(product)

    if special:
        base = "high"
    elif quality is True:
        base = "normal"
    else:
        base = "low"

    if quality is False and not special:  # producto rechazado por calidad: sin bump
        return base

    category = product.get("category") or check_category(
        " ".join(str(product.get(k) or "") for k in ("title", "text"))
    )
    if is_price_hike_period(category, now)["active"]:
        base = PRIORITY_ORDER[min(PRIORITY_ORDER.index(base) + 1, len(PRIORITY_ORDER) - 1)]
    return base


def get_historical_price_context(product: dict) -> dict | None:
    """Reservado. Sin fuente de historial en el MVP."""
    return None
