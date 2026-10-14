"""
utils/date_helpers.py
=====================
Manejo y normalización de fechas aware en la zona horaria local
(por defecto 'America/Mexico_City').

Reglas transversales:
- Funciones puras: no guardan nada, no llaman a la red y no notifican.
- El resto de módulos las importa; este módulo no crea otros módulos.
- Todas las fechas que salen de aquí son aware.
- Ausencia de fecha nunca se asume como "anterior" o "vencido".
"""

from __future__ import annotations

import re
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

try:
    from utils.text_cleaner import normalize
except ImportError:
    from text_cleaner import normalize  # type: ignore

DEFAULT_TZ = "America/Mexico_City"


def now_local(tz_name: str = DEFAULT_TZ) -> datetime:
    """
    Hora actual en esa zona. No usa la hora naive del servidor.
    El orquestador y el reset diario de cuentas parten de aquí.
    """
    return datetime.now(ZoneInfo(tz_name))


def ensure_aware(dt: datetime, tz_name: str = DEFAULT_TZ) -> datetime:
    """
    Si dt no tiene zona, se interpreta como hora local, no como UTC.
    Si ya tiene zona, se convierte a tz_name.
    Así un posted_at guardado sin zona no se desplaza seis horas al comparar
    con expiration_date.
    """
    tz = ZoneInfo(tz_name)
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def start_of_day(dt: datetime, tz_name: str = DEFAULT_TZ) -> datetime:
    """
    Ese instante pasado a la zona local y puesto en 00:00:00.
    account_manager.reset_daily_counters compara start_of_day(ahora) con
    start_of_day(último reset) para saber si cambió el día calendario en México,
    no en UTC.
    """
    local_dt = ensure_aware(dt, tz_name)
    return local_dt.replace(hour=0, minute=0, second=0, microsecond=0)


def end_of_day(dt: datetime, tz_name: str = DEFAULT_TZ) -> datetime:
    """
    El mismo día local a las 23:59:59. Lo usa “vence hoy”: el cupón sigue
    activo durante ese día del post, no hasta la hora en que se publicó.
    """
    local_dt = ensure_aware(dt, tz_name)
    return local_dt.replace(hour=23, minute=59, second=59, microsecond=0)


def add_hours(dt: datetime, hours: int, tz_name: str = DEFAULT_TZ) -> datetime:
    """
    Suma horas en reloj real (sirve el cruce de horario de verano).
    account_manager.apply_cooldown puede usarla, pero el cooldown en minutos
    sigue siendo de ese módulo.
    """
    tz = ZoneInfo(tz_name)
    aware = ensure_aware(dt, tz_name)
    # Suma física de horas en UTC y posterior proyección a la zona local
    return (aware.astimezone(timezone.utc) + timedelta(hours=hours)).astimezone(tz)


def is_before(left: datetime, right: datetime, tz_name: str = DEFAULT_TZ) -> bool:
    """
    Compara ambos en la misma zona. True solo si left < right.
    Sirve para “¿ya venció?” sin mezclar naive y aware.
    Si alguno es None, lanza ValueError: ausencia de fecha no es “anterior”.
    """
    if left is None or right is None:
        raise ValueError("Ambas fechas deben ser válidas; ausencia de fecha no es 'anterior'")
    return ensure_aware(left, tz_name) < ensure_aware(right, tz_name)


# --------------------------------------------------------------------------- #
# Lógica de interpretación de fechas flexibles (parse_flexible_date)
# --------------------------------------------------------------------------- #
_MONTHS = {
    "enero": 1, "ene": 1, "febrero": 2, "feb": 2, "marzo": 3, "mar": 3, "abril": 4, "abr": 4,
    "mayo": 5, "may": 5, "junio": 6, "jun": 6, "julio": 7, "jul": 7, "agosto": 8, "ago": 8,
    "septiembre": 9, "setiembre": 9, "sept": 9, "sep": 9, "octubre": 10, "oct": 10,
    "noviembre": 11, "nov": 11, "diciembre": 12, "dic": 12,
}
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_WEEKDAYS = {
    "lunes": 0, "martes": 1, "miercoles": 2, "jueves": 3,
    "viernes": 4, "sabado": 5, "domingo": 6,
}

_CUE = re.compile(
    r"hasta|venc\w*|expira\w*|caduca\w*|termina\w*|finaliza\w*|concluye\w*|cierra\w*|"
    r"valid[oa]s?|vigente|vigencia|ultimo dia|last day|until"
)
_CUE_WORDS = (
    r"(?:hasta|venc\w*|expira\w*|caduca\w*|termina\w*|finaliza\w*|valid[oa]s?|vigente|"
    r"solo|unicamente|nada mas|ultimo dia)"
)

_DATE_PATTERNS = [
    ("range", re.compile(rf"(?<!\d)(\d{{1,2}})\s+al\s+(\d{{1,2}})\s+de\s+({_MONTH_ALT})\b\.?(?:\s*(?:de|del)?\s*(\d{{4}}))?")),
    ("iso", re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")),
    ("dotted", re.compile(r"(?<![\d.])(\d{1,2})\.(\d{1,2})\.(\d{2,4})(?![\d.])")),
    ("num", re.compile(r"(?<![\d/.\-])(\d{1,2})([/\-])(\d{1,2})(?:\2(\d{2,4}))?(?![\d/])")),
    ("dmonth", re.compile(rf"(?<!\d)(\d{{1,2}})\s*(?:de\s+)?\b({_MONTH_ALT})\b\.?(?:\s*(?:de|del)?\s*(\d{{4}}))?")),
    ("monthd", re.compile(rf"\b({_MONTH_ALT})\b\.?\s+(\d{{1,2}})(?![\d:])(?:,?\s*(\d{{4}}))?")),
]
_CHAIN_BETWEEN = re.compile(r"\s*(?:al|a|-|–|hasta(?:\s+el)?)\s*")


def _build_date(y: Optional[int], m: int, d: int, base: date) -> Optional[date]:
    try:
        if y is not None:
            return date(y + 2000 if y < 100 else y, m, d)
        cand = date(base.year, m, d)
        if cand < base and (base - cand).days > 180:
            cand = date(base.year + 1, m, d)
        return cand
    except ValueError:
        return None


def _find_dates(norm: str, base: date) -> list[tuple[int, int, date]]:
    found: list[tuple[int, int, int, date]] = []
    for kind, rx in _DATE_PATTERNS:
        for m in rx.finditer(norm):
            g = m.groups()
            res: Optional[date] = None
            if kind == "range":
                res = _build_date(int(g[3]) if g[3] else None, _MONTHS[g[2]], int(g[1]), base)
            elif kind == "iso":
                res = _build_date(int(g[0]), int(g[1]), int(g[2]), base)
            elif kind == "dotted":
                res = _build_date(int(g[2]), int(g[1]), int(g[0]), base)
            elif kind == "num":
                a, b = int(g[0]), int(g[2])
                if (a, b) == (24, 7):
                    continue
                y = int(g[3]) if g[3] else None
                res = _build_date(y, b, a, base)
                if res is None and a <= 12 < b:
                    res = _build_date(y, a, b, base)
            elif kind == "dmonth":
                res = _build_date(int(g[2]) if g[2] else None, _MONTHS[g[1]], int(g[0]), base)
            elif kind == "monthd":
                res = _build_date(int(g[2]) if g[2] else None, _MONTHS[g[0]], int(g[1]), base)
            if res is not None:
                found.append((m.start(), -(m.end() - m.start()), m.end(), res))
    found.sort()
    out: list[tuple[int, int, date]] = []
    for start, _neg, end, res in found:
        if out and start < out[-1][1]:
            continue
        out.append((start, end, res))
    return out


def _explicit_date(norm: str, base: date) -> Optional[date]:
    dates = _find_dates(norm, base)
    i = 0
    while i < len(dates):
        j = i
        while j + 1 < len(dates) and _CHAIN_BETWEEN.fullmatch(norm[dates[j][1]:dates[j + 1][0]]):
            j += 1
        window = norm[max(0, dates[i][0] - 40): dates[i][0]]
        if _CUE.search(window):
            return dates[j][2]
        i = j + 1
    return None


_TODAY_RES = (
    re.compile(rf"\b{_CUE_WORDS}\s+(?:por\s+|el\s+dia\s+de\s+|este\s+dia\s+)?hoy\b"),
    re.compile(r"\bhoy\s+(?:venc\w*|termina\w*|expira\w*|finaliza\w*|es\s+el\s+ultimo\s+dia|ultimo\s+dia)\b"),
)
_TOMORROW_RE = re.compile(rf"\b{_CUE_WORDS}\s+(?:por\s+|el\s+dia\s+de\s+)?manana\b")
_WEEKDAY_RE = re.compile(
    rf"\b{_CUE_WORDS}\s+(?:el\s+)?(?:este\s+|proximo\s+)?({'|'.join(_WEEKDAYS)})\b"
)


def _relative_day(norm: str, base: date) -> Optional[date]:
    if any(rx.search(norm) for rx in _TODAY_RES):
        return base
    if _TOMORROW_RE.search(norm):
        return base + timedelta(days=1)
    m = _WEEKDAY_RE.search(norm)
    if m:
        return base + timedelta(days=(_WEEKDAYS[m.group(1)] - base.weekday()) % 7)
    return None


_TCUE = r"(?:hasta|antes\s+de|venc\w*|expira\w*|termina\w*|cierra\w*|finaliza\w*|a)"


def _fix_meridiem(norm: str) -> str:
    return re.sub(r"\b([ap])\s?\.\s?m\b\.?", r"\1m", norm)


def _hms(h: int, m: int, mer: Optional[str]) -> Optional[tuple[int, int, int]]:
    if mer in ("pm", "tarde", "noche"):
        if not 1 <= h <= 12:
            return None
        h = h + 12 if h < 12 else 12
    elif mer in ("am", "manana", "madrugada"):
        if not 1 <= h <= 12:
            return None
        h = 0 if h == 12 else h
    if not (0 <= h < 24 and 0 <= m < 60):
        return None
    return h, m, 59 if m == 59 else 0


def _find_time(tnorm: str) -> Optional[tuple[int, int, int]]:
    if re.search(rf"\b{_TCUE}\s+(?:la\s+)?medianoche\b", tnorm):
        return 23, 59, 59
    if re.search(rf"\b{_TCUE}\s+(?:el\s+)?mediodia\b", tnorm):
        return 12, 0, 0
    m = re.search(rf"\b{_TCUE}\s+las?\s+(\d{{1,2}})(?::(\d{{2}}))?\s+de\s+la\s+(manana|tarde|noche|madrugada)\b", tnorm)
    if m:
        return _hms(int(m.group(1)), int(m.group(2) or 0), m.group(3))
    m = re.search(rf"\b{_TCUE}\s+las?\s+(\d{{1,2}})(?::(\d{{2}}))?\s*(am|pm|hrs?|h)\b", tnorm)
    if m:
        mer = m.group(3) if m.group(3) in ("am", "pm") else None
        return _hms(int(m.group(1)), int(m.group(2) or 0), mer)
    m = re.search(rf"\b{_TCUE}\s+(?:las?\s+)?(\d{{1,2}}):(\d{{2}})(?::\d{{2}})?\s*(am|pm)?\b", tnorm)
    if m:
        return _hms(int(m.group(1)), int(m.group(2)), m.group(3))
    return None


_NUMWORDS = {
    "un": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7,
    "ocho": 8, "nueve": 9, "diez": 10, "doce": 12, "quince": 15, "veinte": 20,
    "veinticuatro": 24, "cuarenta y ocho": 48, "setenta y dos": 72,
}
_NUM = r"(\d{1,3}|" + "|".join(re.escape(w) for w in sorted(_NUMWORDS, key=len, reverse=True)) + r")"
_UNIT = r"(minutos?|mins?|horas?|hrs?|h|dias?|d|semanas?|sem)"
_DUR_RE = re.compile(rf"\b{_NUM}\s*{_UNIT}\b")
_DUR_BARE = re.compile(rf"\W*(?:(?:solo|por|durante|valid[oa]|vigencia de)\s+)?{_NUM}\s*{_UNIT}\W*")
_DUR_CUE = re.compile(
    r"valid[oa]s?|vigencia|vigente|venc\w*|expira\w*|termina\w*|dura\w*|durante|solo|"
    r"nada mas|unicamente|quedan|por|proximas|only|for"
)
_DUR_NOT = re.compile(
    r"envio|entrega|llega\w*|garantia|devolucion|reembolso|pedido|instalacion|plazo|"
    r"meses|cuotas|credito|recib\w*|enviamos|surte"
)


def _duration(norm: str) -> Optional[timedelta]:
    def to_td(qty: str, unit: str) -> Optional[timedelta]:
        n = int(qty) if qty.isdigit() else _NUMWORDS[qty]
        if n <= 0:
            return None
        if unit.startswith("m"):
            td = timedelta(minutes=n)
        elif unit.startswith("h"):
            td = timedelta(hours=n)
        elif unit.startswith("s"):
            td = timedelta(weeks=n)
        else:
            td = timedelta(days=n)
        return td if td <= timedelta(days=400) else None

    bare = _DUR_BARE.fullmatch(norm)
    if bare:
        return to_td(bare.group(1), bare.group(2))
    for m in _DUR_RE.finditer(norm):
        window = norm[max(0, m.start() - 30): m.start()]
        if _DUR_CUE.search(window) and not _DUR_NOT.search(window):
            td = to_td(m.group(1), m.group(2))
            if td:
                return td
    return None


def parse_flexible_date(
    text: str, reference: datetime, tz_name: str = DEFAULT_TZ
) -> Optional[datetime]:
    """
    Misma regla que parse_expiration: interpreta “válido hasta el 15/10”,
    “vence hoy”, “hasta las 23:59”, “48 horas”, “15 de octubre”, “mañana”.
    “Hoy” y la hora suelta usan la fecha de reference (el post), no el momento
    del escaneo. Sin fecha reconocible devuelve None.
    El manager de vencimiento debe llamar a esta función y no mantener una copia propia.
    """
    if not text or not isinstance(reference, datetime):
        return None

    tz = ZoneInfo(tz_name)
    base = ensure_aware(reference, tz_name)
    base_date = base.date()
    norm = normalize(text)
    time_ = _find_time(_fix_meridiem(norm))

    day = _explicit_date(norm, base_date) or _relative_day(norm, base_date)
    if day is not None:
        h, mi, s = time_ or (23, 59, 59)
        return datetime.combine(day, dtime(h, mi, s), tzinfo=tz)

    if time_ is not None:
        cand = datetime.combine(base_date, dtime(*time_), tzinfo=tz)
        if cand <= base:
            nxt = cand + timedelta(days=1)
            if nxt - base <= timedelta(hours=12):
                return nxt
        return cand

    td = _duration(norm)
    return base + td if td else None
