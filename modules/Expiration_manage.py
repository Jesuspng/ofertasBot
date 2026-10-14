"""
expiration_manager.py
=====================
Detecta vencimientos en el texto de los anuncios y, sobre la base de
coupon_storage, archiva lo vencido, avisa de lo que está por vencer y borra
(solo tras un largo periodo) lo ya archivado.

Reglas transversales que respeta este módulo
--------------------------------------------
* No envía mensajes ni avisos: notify_expiring_soon solo DEVUELVE los cupones
  por vencer; el notificador los envía y después llama a mark_expiry_warned.
* No canjea cupones ni completa compras.
* Dato incompleto => None, nunca "vencido": un texto sin fecha entendible da
  parse_expiration() == None y un cupón sin expiration_date nunca se archiva.
* No borra a ciegas: el ciclo normal solo cambia status a 'expired' (conserva
  el historial). El borrado físico (remove_expired) solo alcanza filas que ya
  están 'expired' hace más de N días y no toca seen_sources, para que un post
  viejo no se vuelva a analizar y reinserte.

Cómo se usa
-----------
    schedule_cleanup(6)            # job periódico: check_expired_coupons + archive_expired
    # job diario aparte (lo dispara el scheduler del proyecto):
    remove_expired(older_than_days=30)
    # notificador:
    for c in notify_expiring_soon(2): ...enviar aviso...
    mark_expiry_warned([c["id"] for c in enviados])

Scheduler: por defecto schedule_cleanup usa un hilo daemon propio (no bloquea el
escaneo y no impide cerrar el proceso). Si el proyecto ya tiene un scheduler
estilo APScheduler, regístralo con set_scheduler(...) y se usará su add_job.

Interpretación de textos (parse_expiration)
-------------------------------------------
* Fecha dd/mm[/aaaa], dd-mm, "15 de octubre [de 2026]", "15 oct", ISO, rangos
  ("del 1 al 15 de octubre" => el 15) y días de la semana ("hasta el viernes").
  Solo cuenta si va junto a una palabra de vigencia (hasta, vence, válido...),
  para no confundir "publicado el 3/10" con una expiración. dd/mm es el orden
  por defecto; si el segundo número es > 12 y el primero no, se lee mm/dd.
* Sin año: se usa el del post; si la fecha ya pasó hace más de ~6 meses se
  asume el año siguiente (post de diciembre que dice "hasta el 5/1").
* Fecha sin hora vence a las 23:59:59 locales. Hora con "hasta las 23:59",
  "a las 6 pm", "10 de la noche"; una hora suelta sin am/pm ni minutos
  ("hasta las 6") es ambigua y se ignora.
* "vence hoy", "solo por hoy", "hasta mañana": se usa la fecha del post en
  zona local (un posted_at sin zona se toma como hora local).
* Solo hora ("hasta las 23:59"): ese día; si ya pasó y cruzar medianoche es
  plausible (<12 h), al día siguiente.
* Duración ("válido por 48 horas", "3 días"): posted_at + duración exacta.
  Exige palabra de vigencia y descarta contextos de envío/garantía/devolución.
* Prioridad: fecha explícita > hoy/mañana/día de la semana > hora suelta > duración.
"""

from __future__ import annotations

import logging
import re
import threading
import unicodedata
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Callable, Iterable, Optional

import coupon_storage as cs

log = logging.getLogger("expiration_manager")

_CHUNK = 500  # ids por sentencia SQL (límite de variables de SQLite)


# --------------------------------------------------------------------------- #
# Interpretación de texto
# --------------------------------------------------------------------------- #
def _normalize(text: str) -> str:
    t = unicodedata.normalize("NFKD", (text or "").replace("’", "'"))
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", t).strip()


_MONTHS = {
    "enero": 1, "ene": 1, "febrero": 2, "feb": 2, "marzo": 3, "mar": 3, "abril": 4, "abr": 4,
    "mayo": 5, "may": 5, "junio": 6, "jun": 6, "julio": 7, "jul": 7, "agosto": 8, "ago": 8,
    "septiembre": 9, "setiembre": 9, "sept": 9, "sep": 9, "octubre": 10, "oct": 10,
    "noviembre": 11, "nov": 11, "diciembre": 12, "dic": 12,
}
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_WEEKDAYS = {"lunes": 0, "martes": 1, "miercoles": 2, "jueves": 3, "viernes": 4, "sabado": 5, "domingo": 6}

_CUE = re.compile(r"hasta|venc\w*|expira\w*|caduca\w*|termina\w*|finaliza\w*|concluye\w*|cierra\w*|"
                  r"valid[oa]s?|vigente|vigencia|ultimo dia|last day|until")
_CUE_WORDS = (r"(?:hasta|venc\w*|expira\w*|caduca\w*|termina\w*|finaliza\w*|valid[oa]s?|vigente|"
              r"solo|unicamente|nada mas|ultimo dia)")

# --- fechas ---
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
    """Todas las fechas del texto como (inicio, fin, date), sin solaparse."""
    found: list[tuple[int, int, int, date]] = []  # (inicio, -longitud, fin, fecha)
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
                    continue  # "24/7" no es una fecha
                y = int(g[3]) if g[3] else None
                res = _build_date(y, b, a, base)
                if res is None and a <= 12 < b:  # mm/dd
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
            continue  # solapa con una coincidencia anterior más larga/temprana
        out.append((start, end, res))
    return out


def _explicit_date(norm: str, base: date) -> Optional[date]:
    """Fecha de vencimiento: primera cadena de fechas (o rango) precedida de una palabra de vigencia."""
    dates = _find_dates(norm, base)
    i = 0
    while i < len(dates):
        j = i
        while j + 1 < len(dates) and _CHAIN_BETWEEN.fullmatch(norm[dates[j][1]:dates[j + 1][0]]):
            j += 1
        window = norm[max(0, dates[i][0] - 40): dates[i][0]]
        if _CUE.search(window):
            return dates[j][2]  # en un rango, vale la fecha final
        i = j + 1
    return None


# --- hoy / mañana / día de la semana ---
_TODAY_RES = (
    re.compile(rf"\b{_CUE_WORDS}\s+(?:por\s+|el\s+dia\s+de\s+|este\s+dia\s+)?hoy\b"),
    re.compile(r"\bhoy\s+(?:venc\w*|termina\w*|expira\w*|finaliza\w*|es\s+el\s+ultimo\s+dia|ultimo\s+dia)\b"),
)
_TOMORROW_RE = re.compile(rf"\b{_CUE_WORDS}\s+(?:por\s+|el\s+dia\s+de\s+)?manana\b")
_WEEKDAY_RE = re.compile(
    rf"\b{_CUE_WORDS}\s+(?:el\s+)?(?:este\s+|proximo\s+)?({'|'.join(_WEEKDAYS)})\b")


def _relative_day(norm: str, base: date) -> Optional[date]:
    if any(rx.search(norm) for rx in _TODAY_RES):
        return base
    if _TOMORROW_RE.search(norm):
        return base + timedelta(days=1)
    m = _WEEKDAY_RE.search(norm)
    if m:
        return base + timedelta(days=(_WEEKDAYS[m.group(1)] - base.weekday()) % 7)
    return None


# --- horas ---
_TCUE = r"(?:hasta|antes\s+de|venc\w*|expira\w*|termina\w*|cierra\w*|finaliza\w*|a)"


def _fix_meridiem(norm: str) -> str:
    return re.sub(r"\b([ap])\s?\.\s?m\b\.?", r"\1m", norm)  # "p. m." / "a.m." -> "pm" / "am"


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
    return h, m, 59 if m == 59 else 0  # "hasta 23:59" = hasta el final de ese minuto


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
    return None  # "hasta las 6" a secas es ambiguo (¿am o pm?)


# --- duraciones ---
_NUMWORDS = {"un": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7,
             "ocho": 8, "nueve": 9, "diez": 10, "doce": 12, "quince": 15, "veinte": 20,
             "veinticuatro": 24, "cuarenta y ocho": 48, "setenta y dos": 72}
_NUM = r"(\d{1,3}|" + "|".join(re.escape(w) for w in sorted(_NUMWORDS, key=len, reverse=True)) + r")"
_UNIT = r"(minutos?|mins?|horas?|hrs?|h|dias?|d|semanas?|sem)"
_DUR_RE = re.compile(rf"\b{_NUM}\s*{_UNIT}\b")
_DUR_BARE = re.compile(rf"\W*(?:(?:solo|por|durante|valid[oa]|vigencia de)\s+)?{_NUM}\s*{_UNIT}\W*")
_DUR_CUE = re.compile(r"valid[oa]s?|vigencia|vigente|venc\w*|expira\w*|termina\w*|dura\w*|durante|solo|"
                      r"nada mas|unicamente|quedan|por|proximas|only|for")
_DUR_NOT = re.compile(r"envio|entrega|llega\w*|garantia|devolucion|reembolso|pedido|instalacion|plazo|"
                      r"meses|cuotas|credito|recib\w*|enviamos|surte")


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


# --- API pública ---
def parse_expiration(text: str, posted_at: datetime) -> Optional[datetime]:
    """
    Interpreta cuándo vence una oferta según su texto. Devuelve un datetime con
    zona horaria local, o None si no hay una fecha/hora/duración entendible
    (None NO significa vencido). `posted_at` sin zona se toma como hora local;
    sin posted_at válido devuelve None (no se puede resolver "hoy"). Ver las
    reglas de interpretación en el docstring del módulo.
    """
    if not text or not isinstance(posted_at, datetime):
        return None
    base = posted_at.astimezone()          # naive => local; aware => a hora local
    base_date = base.date()
    norm = _normalize(text)
    time_ = _find_time(_fix_meridiem(norm))

    day = _explicit_date(norm, base_date) or _relative_day(norm, base_date)
    if day is not None:
        h, mi, s = time_ or (23, 59, 59)
        return datetime.combine(day, dtime(h, mi, s)).astimezone()

    if time_ is not None:
        cand = datetime.combine(base_date, dtime(*time_)).astimezone()
        if cand <= base:
            nxt = cand + timedelta(days=1)
            if nxt - base <= timedelta(hours=12):  # cruce de medianoche plausible
                return nxt
        return cand

    td = _duration(norm)
    return base + td if td else None


# --------------------------------------------------------------------------- #
# Base de datos
# --------------------------------------------------------------------------- #
def _cutoff(now: Optional[datetime]) -> str:
    iso = cs._now_iso() if now is None else cs._to_utc_iso(now)
    if iso is None:
        raise ValueError(f"now inválido: {now!r}")
    return iso


def _clean_ids(ids: Iterable) -> list[int]:
    out: list[int] = []
    for i in ids or []:
        if isinstance(i, bool) or not isinstance(i, int):
            log.warning("id ignorado (no es entero): %r", i)
            continue
        out.append(i)
    return list(dict.fromkeys(out))


def _chunks(seq: list, n: int = _CHUNK):
    for k in range(0, len(seq), n):
        yield seq[k:k + n]


def check_expired_coupons(now: Optional[datetime] = None) -> list[int]:
    """
    Ids de cupones 'active' con expiration_date < now (por defecto, ahora). Un
    `now` sin zona se toma como hora local. No modifica nada. Los cupones sin
    fecha de expiración nunca se consideran vencidos.
    """
    cutoff = _cutoff(now)
    with cs._conn() as conn:
        rows = conn.execute(
            "SELECT id FROM coupons WHERE status = 'active' AND expiration_date IS NOT NULL "
            "AND expiration_date < ? ORDER BY expiration_date, id", (cutoff,)).fetchall()
    return [int(r["id"]) for r in rows]


def archive_expired(coupon_ids: list[int], *, now: Optional[datetime] = None) -> int:
    """
    Pasa a status='expired' (conserva la fila y su historial) y devuelve cuántos
    cambió. Vuelve a comprobar en la misma transacción que el cupón siga
    'active' y vencido: si entre check_expired_coupons y esta llamada alguien le
    amplió la fecha, o ya no está activo (used/invalid/archived), no se toca.
    """
    ids = _clean_ids(coupon_ids)
    if not ids:
        return 0
    cutoff, stamp, changed = _cutoff(now), cs._now_iso(), 0
    with cs._tx() as conn:
        for chunk in _chunks(ids):
            marks = ",".join("?" * len(chunk))
            cur = conn.execute(
                f"UPDATE coupons SET status = 'expired', updated_at = ? WHERE id IN ({marks}) "
                "AND status = 'active' AND expiration_date IS NOT NULL AND expiration_date < ?",
                (stamp, *chunk, cutoff))
            changed += cur.rowcount
    log.info("Archivados como expired: %d de %d ids.", changed, len(ids))
    return changed


def remove_expired(older_than_days: int = 30, *, dry_run: bool = False) -> int:
    """
    Borra físicamente solo filas con status='expired' cuyo updated_at (momento en
    que se archivaron) es anterior a older_than_days. Pensado para un job diario,
    no para cada ciclo. older_than_days debe ser >= 1. Con dry_run=True solo
    cuenta lo que borraría. No toca seen_sources.
    """
    if isinstance(older_than_days, bool) or not isinstance(older_than_days, int) or older_than_days < 1:
        raise ValueError("older_than_days debe ser un entero >= 1")
    cutoff = cs._iso(cs._utcnow() - timedelta(days=older_than_days))
    if dry_run:
        with cs._conn() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM coupons WHERE status = 'expired' AND updated_at < ?",
                                    (cutoff,)).fetchone()[0])
    with cs._tx() as conn:
        n = conn.execute("DELETE FROM coupons WHERE status = 'expired' AND updated_at < ?", (cutoff,)).rowcount
    log.info("Borrados %d cupones expired con más de %d días.", n, older_than_days)
    return n


def notify_expiring_soon(days: float = 2) -> list[dict]:
    """
    Cupones 'active' que vencen dentro de los próximos `days` días (aún no
    vencidos) y cuyo aviso no se ha enviado (expiry_warned = 0), del que vence
    antes al que vence después. Cada dict añade `expires_in_hours`. No marca el
    flag: el notificador envía y luego llama a mark_expiry_warned(ids). Si a un
    cupón se le amplía la fecha (save_coupon), el flag se reinicia.
    """
    if isinstance(days, bool) or not isinstance(days, (int, float)) or days <= 0:
        raise ValueError("days debe ser un número > 0")
    now = cs._utcnow()
    now_iso, until_iso = cs._iso(now), cs._iso(now + timedelta(days=days))
    with cs._conn() as conn:
        rows = conn.execute(
            "SELECT * FROM coupons WHERE status = 'active' AND expiry_warned = 0 "
            "AND expiration_date IS NOT NULL AND expiration_date > ? AND expiration_date <= ? "
            "ORDER BY expiration_date, id", (now_iso, until_iso)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        exp = datetime.strptime(d["expiration_date"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        d["expires_in_hours"] = round((exp - now).total_seconds() / 3600, 1)
        out.append(d)
    return out


def mark_expiry_warned(coupon_ids: list[int]) -> int:
    """Marca expiry_warned=1 tras enviar el aviso. Devuelve cuántos cupones cambiaron."""
    ids = _clean_ids(coupon_ids)
    if not ids:
        return 0
    stamp, changed = cs._now_iso(), 0
    with cs._tx() as conn:
        for chunk in _chunks(ids):
            marks = ",".join("?" * len(chunk))
            changed += conn.execute(
                f"UPDATE coupons SET expiry_warned = 1, updated_at = ? WHERE id IN ({marks}) AND expiry_warned = 0",
                (stamp, *chunk)).rowcount
    return changed


# --------------------------------------------------------------------------- #
# Programación
# --------------------------------------------------------------------------- #
_JOB_ID = "expiration_cleanup"
_scheduler = None                      # scheduler externo tipo APScheduler (opcional)
_jobs: dict[str, "_IntervalJob"] = {}  # hilos propios por id
_jobs_lock = threading.Lock()


def run_cleanup(now: Optional[datetime] = None) -> dict:
    """Un ciclo de limpieza: check_expired_coupons + archive_expired. Nunca borra filas."""
    ids = check_expired_coupons(now)
    archived = archive_expired(ids, now=now) if ids else 0
    return {"found": len(ids), "archived": archived}


class _IntervalJob:
    """Ejecuta func cada interval_s en un hilo daemon; los errores se registran y el ciclo sigue."""

    def __init__(self, job_id: str, func: Callable[[], object], interval_s: float):
        self.job_id, self.func, self.interval_s = job_id, func, interval_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=f"job-{job_id}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                result = self.func()
                log.info("Job %s: %s", self.job_id, result)
            except Exception:  # noqa: BLE001 — un fallo no mata el job
                log.exception("Job %s falló; se reintentará en el siguiente ciclo.", self.job_id)


def set_scheduler(scheduler) -> None:
    """Usa un scheduler externo con add_job(func, 'interval', hours=..., id=..., replace_existing=True, ...)."""
    global _scheduler
    _scheduler = scheduler


def schedule_cleanup(interval_hours: float = 6) -> None:
    """
    Registra la limpieza periódica (check_expired_coupons + archive_expired).
    No bloquea: corre en un hilo daemon propio, o en el scheduler externo si se
    configuró con set_scheduler. Llamarla de nuevo reemplaza la programación
    anterior (no se duplican jobs). La primera ejecución ocurre tras un intervalo.
    """
    if isinstance(interval_hours, bool) or not isinstance(interval_hours, (int, float)) or interval_hours <= 0:
        raise ValueError("interval_hours debe ser un número > 0")
    if _scheduler is not None:
        _scheduler.add_job(run_cleanup, "interval", hours=interval_hours, id=_JOB_ID,
                           replace_existing=True, max_instances=1, coalesce=True)
        log.info("Limpieza de vencidos registrada en el scheduler externo cada %s h.", interval_hours)
        return
    with _jobs_lock:
        old = _jobs.pop(_JOB_ID, None)
        if old:
            old.stop()
        job = _IntervalJob(_JOB_ID, run_cleanup, interval_hours * 3600)
        _jobs[_JOB_ID] = job
        job.start()
    log.info("Limpieza de vencidos programada cada %s h (hilo propio).", interval_hours)


def stop_cleanup() -> None:
    """Detiene el job propio de limpieza (si existe)."""
    with _jobs_lock:
        job = _jobs.pop(_JOB_ID, None)
    if job:
        job.stop()