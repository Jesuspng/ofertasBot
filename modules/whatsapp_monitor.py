"""
whatsapp_monitor.py
===================
Lee mensajes de canales o chats de WhatsApp ya configurados (WhatsApp Web con
perfil persistente en sessions/whatsapp/<id>) y devuelve candidatos a oferta.

Reglas transversales que respeta este módulo
--------------------------------------------
* No envía mensajes ni responde. Nunca escribe en el cuadro de mensaje ni
  pulsa Enter: lo único que se teclea es el nombre en el buscador lateral
  (acotado a #side) para localizar un canal o chat, y se borra con Escape.
* No canjea cupones ni completa compras: los códigos solo se extraen como texto.
* Dato incompleto => desconocido: sin fecha legible `posted_at` es None; sin
  enlace ni código la oferta se marca `incomplete=True`; sin precio,
  `raw_price_mentions` queda vacío. No se guarda el remitente del mensaje.
* Si WhatsApp pide QR la cuenta se marca limited (requiere emparejamiento
  manual) y el proceso no se cae.

Flujo esperado del orquestador
------------------------------
    account = account_manager.get_available_account("whatsapp")
    if connect_channel(account, channel_ids):              # False => ya quedó limited
        since = get_watermark("Mi canal") or datetime.now().astimezone() - timedelta(hours=24)
        try:
            offers = listen_messages(account, keywords, since)
        except ScanAborted as exc:                         # QR a mitad de sesión (ya limited)
            offers = exc.partial_posts
        ...procesar offers...
        for ch in channel_ids:                             # solo tras procesar con éxito
            if (t := get_last_read_at(ch)):
                update_watermark(ch, t)
        account_manager.mark_account_used(account["id"])

`channel_id` es el NOMBRE tal como aparece en la lista de chats/canales de la
sesión (WhatsApp Web no expone ids estables en la interfaz).

Limitaciones honestas
---------------------
* Abrir un chat en WhatsApp Web marca sus mensajes como leídos y, en chats
  individuales/grupos, puede enviar acuse de lectura al remitente (a menos que
  lo desactives en Privacidad). En canales no hay acuses hacia los admins.
* Los selectores de WhatsApp Web cambian a menudo: ajustar _JS_* si falla.
* La hora de cada mensaje sale de `data-pre-plain-text` ("[14:32, 11/10/2026]").
  El orden de la fecha depende del idioma del navegador: config `date_order`
  ("dmy" por defecto, "mdy" si tu sesión muestra mes/día). Los canales
  (newsletters) no siempre exponen ese atributo: ahí `posted_at` queda None y
  esos mensajes se conservan (no se pueden descartar por antigüedad).
* La hora tiene resolución de minuto, por eso se lee con `>=` desde la marca
  de agua: puede repetir mensajes del último minuto; el orquestador debe
  deduplicar por message_id o por hash de texto.
* Playwright sync está atado al hilo que lo creó: usa el módulo desde un solo hilo.
* Reutiliza utilidades de texto de facebook_monitor.py para mantener el mismo
  criterio (normalización, precios, enlaces, descuentos) y su ScanAborted.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import random
import re
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import facebook_monitor as _fb
from facebook_monitor import ScanAborted  # mismo contrato para el orquestador

log = logging.getLogger("whatsapp_monitor")

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("WA_MONITOR_CONFIG", BASE_DIR / "config" / "whatsapp_monitor.yaml"))
WATERMARKS_PATH = Path(os.environ.get("WA_MONITOR_WATERMARKS", BASE_DIR / "data" / "whatsapp_watermarks.json"))
SESSIONS_DIR = BASE_DIR / "sessions" / "whatsapp"
WA_URL = "https://web.whatsapp.com"

# Regex por defecto de códigos: palabra en MAYÚSCULAS de 4–20 caracteres cerca
# de "cupón", "código" o "usa" (hasta 2 palabras en minúscula de por medio).
# Debe tener exactamente un grupo de captura: el código.
_DEFAULT_CODE_REGEX = (
    r"\b(?i:cup[oó]n(?:es)?|c[oó]digo|usa|usar|use|code|promo(?:cional)?)\b"
    r"(?:[\s:\-–]+[a-záéíóúñ]{1,12}\b){0,2}"
    r"[\s:\-–\"“'‘]*"
    r"\b([A-Z0-9]{4,20})\b"
)

DEFAULT_CONFIG: dict = {
    "channel_ids": [],                    # respaldo si no se llamó a connect_channel
    "date_order": "dmy",                  # orden de fecha en data-pre-plain-text: dmy | mdy
    "max_scrolls": 5,                     # scrolls hacia arriba por chat (sin historial infinito)
    "max_messages": 200,                  # tope de mensajes leídos por chat
    "scroll_pause": [1.0, 2.5],
    "wait_between_channels": [5, 20],
    "default_lookback_hours": 24,         # si no hay since ni marca de agua
    "max_retries": 2,
    "backoff_base_seconds": 3,
    "backoff_max_seconds": 30,
    "headless": True,
    "code_regex": None,                   # None => _DEFAULT_CODE_REGEX
    "code_stopwords": [
        "HOY", "LINK", "GRUPOS", "GRUPO", "CANAL", "OFERTA", "OFERTAS", "CUPON", "CUPONES",
        "CODIGO", "GRATIS", "DESCUENTO", "PROMO", "ENVIO", "CLICK", "AQUI", "MEXICO",
        "WHATSAPP", "TELEGRAM", "TIENDA", "PRECIO", "CODE", "FREE", "SALE", "NUEVO",
        "URGENTE", "AMAZON", "FACEBOOK", "MANANA", "AHORA", "ULTIMO", "ULTIMOS",
    ],
    "known_store_domains": [
        "amazon.com.mx", "amazon.com", "amzn.to", "a.co", "mercadolibre.com.mx", "mercadolibre.com",
        "meli.la", "liverpool.com.mx", "walmart.com.mx", "bodegaaurrera.com.mx", "coppel.com",
        "elektra.com.mx", "costco.com.mx", "aliexpress.com", "temu.com", "shein.com",
        "cyberpuerta.mx", "ddtech.mx", "sanborns.com.mx", "sears.com.mx", "homedepot.com.mx",
        "palaciodehierro.com", "bestbuy.com", "ebay.com", "claroshop.com", "linio.com.mx",
    ],
}

_config_cache: Optional[dict] = None
_overrides: dict = {}
_lock = threading.RLock()
_sleep: Callable[[float], None] = time.sleep  # sobrescribible en tests


def get_config() -> dict:
    global _config_cache
    if _config_cache is None:
        cfg = dict(DEFAULT_CONFIG)
        try:
            import yaml
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            section = data.get("whatsapp_monitor", data) if isinstance(data, dict) else {}
            cfg.update({k: v for k, v in section.items() if k in DEFAULT_CONFIG})
        except FileNotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001
            log.error("Config ilegible (%s); se usan valores por defecto.", exc)
        _config_cache = cfg
    return {**_config_cache, **_overrides}


def set_config(**kwargs) -> None:
    """Sobrescribe valores de configuración en memoria (útil en tests)."""
    _overrides.update({k: v for k, v in kwargs.items() if k in DEFAULT_CONFIG})


# --------------------------------------------------------------------------- #
# Texto: códigos, enlaces, relevancia
# --------------------------------------------------------------------------- #
_compiled: dict[str, "re.Pattern"] = {}
_SYSTEM_RES = (
    re.compile(r"^.{0,50}\b(se unio|se unieron|salio del grupo|se salio|joined|left the group|"
               r"was added|fue anadid[oa]|anadio a|uniste)\b.{0,50}$"),
    re.compile(r"cifrados de extremo a extremo|end-to-end encrypted|mensaje eliminado|"
               r"eliminaste este mensaje|este mensaje fue eliminado|this message was deleted|"
               r"cambio el (?:asunto|icono)|changed the (?:subject|group)|creo (?:el|este) grupo|"
               r"created (?:the )?group"),
)


def _code_regex() -> "re.Pattern":
    pat = get_config().get("code_regex") or _DEFAULT_CODE_REGEX
    if pat not in _compiled:
        try:
            rx = re.compile(pat)
            if rx.groups < 1:
                raise re.error("la regex debe tener un grupo de captura")
        except re.error as exc:
            log.error("code_regex inválida (%s); se usa la de por defecto.", exc)
            rx = re.compile(_DEFAULT_CODE_REGEX)
        _compiled[pat] = rx
    return _compiled[pat]


def _find_codes(text: str) -> list[str]:
    stop = {w.upper() for w in get_config()["code_stopwords"]}
    found: list[str] = []
    for m in _code_regex().finditer(text or ""):
        tok = m.group(1)
        if tok.isdigit() or tok.upper() in stop or _fb._UNIT_LIKE_RE.match(tok):
            continue
        found.append(tok)
    return list(dict.fromkeys(found))


def extract_links_and_codes(message: str) -> dict:
    """{'links': [...], 'codes': [...]}. Enlaces http/https externos; códigos según code_regex."""
    message = message or ""
    return {"links": _fb._clean_links(None, message), "codes": _find_codes(message)}


def _is_known_store(url: str) -> bool:
    host = (url.split("//", 1)[-1].split("/", 1)[0].split(":")[0]).lower()
    return any(host == d or host.endswith("." + d) for d in get_config()["known_store_domains"])


def _is_system_message(norm: str, has_offer_evidence: bool) -> bool:
    if has_offer_evidence or len(norm) > 160:
        return False
    return any(rx.search(norm) for rx in _SYSTEM_RES)


def filter_relevant_messages(message: str, keywords: list[str]) -> bool:
    """
    True si el mensaje trae keyword exacta, porcentaje/descuento/cupón, código
    o enlace de tienda conocida. Descarta mensajes vacíos (stickers, audios sin
    texto), mensajes de sistema ("X se unió") y los que solo dicen "compartan"
    sin oferta ni enlace.
    """
    if not message or not message.strip():
        return False
    norm = _fb._normalize(message)
    has_url = bool(_fb._URL_RE.search(message))
    has_price = bool(_fb._PRICE_RE.search(message))
    if _is_system_message(norm, has_url or has_price):
        return False

    parsed = extract_links_and_codes(message)
    has_code = bool(parsed["codes"])
    has_store = any(_is_known_store(u) for u in parsed["links"])
    has_discount = bool(_fb._DISCOUNT_RE.search(norm))
    kw_hit = False
    for kw in keywords or []:
        k = _fb._normalize(kw)
        if k and re.search(rf"(?<!\w){re.escape(k)}(?!\w)", norm):
            kw_hit = True
            break

    if not (kw_hit or has_discount or has_code or has_store):
        return False
    if _fb._SHARE_RE.search(norm) and not (has_discount or has_price or has_url or has_code or has_store):
        return False
    return True


def parse_offer_from_message(message: dict) -> dict:
    """Mensaje crudo -> esquema de oferta (mismo que Facebook, con source=whatsapp)."""
    text = message.get("text") or ""
    parsed = extract_links_and_codes(text)
    links = list(dict.fromkeys(_fb._clean_links(message.get("links"), text)))
    codes = parsed["codes"]
    prices = list(dict.fromkeys(m.group(0).strip().rstrip(".,") for m in _fb._PRICE_RE.finditer(text)))
    posted = message.get("posted_at")
    if isinstance(posted, datetime):
        posted = posted.isoformat()
    return {
        "source": "whatsapp",
        "channel_id": message.get("channel_id"),
        "message_id": message.get("message_id"),   # None si no se pudo leer
        "text": text,
        "posted_at": posted or None,               # None = fecha desconocida
        "links": links,
        "possible_codes": codes,
        "raw_price_mentions": prices,
        "incomplete": not links and not codes,
    }


# --------------------------------------------------------------------------- #
# Fechas de mensaje
# --------------------------------------------------------------------------- #
_PRE_RE = re.compile(r"^\[([^,\]]+),\s*([^\]]+)\]")
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(am|pm|a m|p m)?")
_DATE_RE = re.compile(r"(\d{1,4})[/.\-](\d{1,2})[/.\-](\d{1,4})")


def _parse_pre_plain_text(pre: str, date_order: str = "dmy") -> Optional[datetime]:
    """'[14:32, 11/10/2026] Juan: ' o '[2:32 p. m., 11/10/2026]' -> datetime local; None si no se entiende."""
    m = _PRE_RE.match((pre or "").strip())
    if not m:
        return None
    tm = _TIME_RE.match(_fb._normalize(m.group(1)).replace(".", ""))
    dm = _DATE_RE.search(m.group(2))
    if not tm or not dm:
        return None
    hour, minute, second, ampm = int(tm.group(1)), int(tm.group(2)), int(tm.group(3) or 0), tm.group(4)
    if ampm:
        if ampm.startswith("p") and hour < 12:
            hour += 12
        elif ampm.startswith("a") and hour == 12:
            hour = 0
    a, b, c = dm.group(1), dm.group(2), dm.group(3)
    if len(a) == 4:
        year, month, day = int(a), int(b), int(c)
    elif date_order == "mdy":
        month, day, year = int(a), int(b), int(c)
    else:
        day, month, year = int(a), int(b), int(c)
    if year < 100:
        year += 2000
    try:
        return datetime(year, month, day, hour, minute, second).astimezone()  # naive = hora local
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Marca de agua (persistente, atómica, nunca retrocede)
# --------------------------------------------------------------------------- #
_LAST_READ: dict[str, datetime] = {}  # canal -> mensaje más reciente leído en el último ciclo


def _as_aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.astimezone()


def _read_watermarks() -> dict:
    try:
        with open(WATERMARKS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.error("Marcas de agua ilegibles (%s); se ignoran.", exc)
        return {}


def get_watermark(channel_id: str) -> Optional[datetime]:
    with _lock:
        raw = _read_watermarks().get(channel_id)
    if not raw:
        return None
    try:
        return _as_aware(datetime.fromisoformat(raw))
    except ValueError:
        return None


def get_last_read_at(channel_id: str) -> Optional[datetime]:
    """Mensaje más reciente (con hora legible) leído en el último listen_messages, relevante o no."""
    return _LAST_READ.get(channel_id)


def update_watermark(channel_id: str, last_message_at: datetime) -> None:
    """Guarda la marca de agua del canal. Nunca la mueve hacia atrás."""
    if not channel_id or not isinstance(last_message_at, datetime):
        log.error("update_watermark: argumentos inválidos (%r, %r)", channel_id, last_message_at)
        return
    new = _as_aware(last_message_at)
    with _lock:
        marks = _read_watermarks()
        current = None
        try:
            current = _as_aware(datetime.fromisoformat(marks[channel_id])) if channel_id in marks else None
        except ValueError:
            pass
        if current and current >= new:
            return
        marks[channel_id] = new.isoformat()
        try:
            WATERMARKS_PATH.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=WATERMARKS_PATH.parent, prefix=".wm_", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(marks, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, WATERMARKS_PATH)
        except OSError as exc:
            log.error("No se pudo persistir la marca de agua: %s", exc)


# --------------------------------------------------------------------------- #
# Navegador (WhatsApp Web admite UNA pestaña por sesión: se reutiliza la misma)
# --------------------------------------------------------------------------- #
_SESSIONS: dict[str, tuple] = {}       # account_id -> (playwright, context, page)
_CONNECTED: dict[str, list[str]] = {}  # account_id -> canales confirmados

_QR_SELECTOR = "canvas[aria-label], div[data-ref]"
_READY_SELECTOR = "#pane-side"


def _account_id(account: dict) -> str:
    acc_id = (account or {}).get("id")
    if not acc_id:
        raise ValueError("La cuenta no tiene 'id'.")
    return str(acc_id)


def _get_page(account: dict):
    acc_id = _account_id(account)
    if acc_id in _SESSIONS:
        return _SESSIONS[acc_id][2]
    from playwright.sync_api import sync_playwright  # import perezoso
    profile = Path(account.get("session_path") or SESSIONS_DIR / acc_id)
    if not profile.is_absolute():
        profile = BASE_DIR / profile
    if not profile.exists():
        log.warning("No existe el perfil %s: la cuenta no tiene sesión guardada.", profile)
    pw = sync_playwright().start()
    try:
        ctx = pw.chromium.launch_persistent_context(
            str(profile), headless=bool(get_config()["headless"]),
            locale="es-MX", viewport={"width": 1280, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
    except Exception:
        pw.stop()
        raise
    _SESSIONS[acc_id] = (pw, ctx, page)
    return page


def close_session(account_id: str) -> None:
    entry = _SESSIONS.pop(account_id, None)
    _CONNECTED.pop(account_id, None)
    if not entry:
        return
    pw, ctx, _page = entry
    for closer in (ctx.close, pw.stop):
        try:
            closer()
        except Exception as exc:  # noqa: BLE001
            log.debug("Error cerrando sesión %s: %s", account_id, exc)


def close_all_sessions() -> None:
    for acc_id in list(_SESSIONS):
        close_session(acc_id)


atexit.register(close_all_sessions)


def _mark_limited(account_id: str, reason: str) -> None:
    """Marca la cuenta como limited vía AccountManager sin tumbar el proceso si falla."""
    try:
        import account_manager
        account_manager.mark_account_limited(account_id, reason)
    except Exception as exc:  # noqa: BLE001
        log.error("No se pudo marcar %s como limited (%s): %s", account_id, reason, exc)


def _wait_state(page) -> str:
    """Abre WhatsApp Web si hace falta y devuelve 'ready' (lista de chats) o 'qr' (pide emparejar)."""
    if "web.whatsapp.com" not in (page.url or ""):
        page.goto(WA_URL, wait_until="domcontentloaded", timeout=45_000)
    page.wait_for_selector(f"{_READY_SELECTOR}, {_QR_SELECTOR}", timeout=60_000)
    if page.query_selector(_READY_SELECTOR):
        return "ready"
    _sleep(3)  # evita falsos QR durante la transición de carga
    return "ready" if page.query_selector(_READY_SELECTOR) else "qr"


def _with_retries(fn: Callable[[], object], what: str):
    """Reintentos acotados con backoff para fallos transitorios (timeouts, DOM a medio cargar)."""
    cfg = get_config()
    max_retries = int(cfg["max_retries"])
    base, cap = float(cfg["backoff_base_seconds"]), float(cfg["backoff_max_seconds"])
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except ScanAborted:
            raise
        except Exception as exc:  # noqa: BLE001
            if attempt >= max_retries:
                log.error("%s: fallo tras %d reintentos: %s", what, max_retries, exc)
                raise
            delay = min(cap, base * (2 ** attempt)) + random.uniform(0, base)
            log.warning("%s: fallo transitorio (%s); reintento %d/%d en %.0fs",
                        what, exc, attempt + 1, max_retries, delay)
            _sleep(delay)


# --------------------------------------------------------------------------- #
# Localizar / abrir chats y canales
# --------------------------------------------------------------------------- #
_JS_TITLES = """() => Array.from(document.querySelectorAll("#pane-side span[title]"))
                   .map(e => e.getAttribute('title'))"""

_JS_CLICK_TITLE = """(name) => {
  const el = Array.from(document.querySelectorAll("#pane-side span[title]"))
                  .find(e => e.getAttribute('title') === name);
  if (!el) return false;
  const row = el.closest("[role='listitem'], [role='row'], [role='gridcell']") || el;
  row.click(); return true;
}"""

_SEARCH_SELECTOR = ("#side div[contenteditable='true'][data-tab='3'], "
                    "#side [aria-label='Search input textbox'], "
                    "#side [aria-label='Cuadro de texto de búsqueda']")


def _same(a: str, b: str) -> bool:
    return _fb._normalize(a) == _fb._normalize(b)


def _find_title(page, name: str) -> Optional[str]:
    """Título exacto (tal como lo muestra WhatsApp) de un chat/canal visible, o None."""
    for title in page.evaluate(_JS_TITLES) or []:
        if _same(title, name):
            return title
    return None


def _search_for(page, name: str) -> bool:
    """Teclea el nombre SOLO en el buscador lateral (#side). Nunca en el cuadro de mensaje, nunca Enter."""
    box = page.query_selector(_SEARCH_SELECTOR)
    if not box:
        return False
    box.click()
    box.type(name, delay=random.randint(40, 120))
    _sleep(random.uniform(1.0, 2.0))
    return True


def _clear_search(page) -> None:
    try:
        page.keyboard.press("Escape")
    except Exception:  # noqa: BLE001
        pass


def _locate_chat(page, name: str, open_it: bool) -> bool:
    """¿Está el chat/canal en la sesión? Con open_it=True además lo abre."""
    searched = False
    title = _find_title(page, name)
    if title is None and _search_for(page, name):
        searched = True
        title = _find_title(page, name)
    found = title is not None
    if found and open_it:
        found = bool(page.evaluate(_JS_CLICK_TITLE, title))
        if found:
            page.wait_for_selector("#main", timeout=15_000)
            _sleep(random.uniform(1.0, 2.0))
    if searched and not (found and open_it):
        _clear_search(page)
    return found


def connect_channel(account: dict, channel_ids: list[str]) -> bool:
    """
    Carga la sesión de sessions/whatsapp/<id> y confirma que cada canal/chat
    está en la lista de la sesión.

    * False -> WhatsApp pide QR (requiere emparejamiento manual): la cuenta ya
      queda marcada limited aquí y la sesión se cierra.
    * True  -> sesión válida. Los canales que no aparecen se registran y se
      excluyen de la lectura; no cuentan como fallo de la cuenta.
    * Errores de infraestructura (Playwright ausente, timeouts persistentes)
      se propagan: no son culpa de la cuenta.
    """
    acc_id = _account_id(account)
    page = _get_page(account)
    if _with_retries(lambda: _wait_state(page), "cargar WhatsApp Web") == "qr":
        _mark_limited(acc_id, "WhatsApp pide QR: requiere emparejamiento manual")
        close_session(acc_id)
        return False

    confirmed: list[str] = []
    for name in channel_ids or []:
        try:
            if _with_retries(lambda n=name: _locate_chat(page, n, open_it=False), f"buscar {name}"):
                confirmed.append(name)
            else:
                log.warning("Canal/chat %r no está en la lista de la sesión.", name)
        except Exception as exc:  # noqa: BLE001
            log.error("Canal/chat %r no verificable: %s", name, exc)
    _CONNECTED[acc_id] = confirmed
    log.info("Cuenta %s: %d/%d canales/chats confirmados.", acc_id, len(confirmed), len(channel_ids or []))
    return True


# --------------------------------------------------------------------------- #
# Lectura de mensajes
# --------------------------------------------------------------------------- #
_JS_READ = """() => {
  const main = document.querySelector("#main"); if (!main) return [];
  const SEL = "span.selectable-text, [data-testid='selectable-text']";
  const out = [];
  for (const row of main.querySelectorAll("[data-id]")) {
    const pre = row.querySelector("[data-pre-plain-text]");
    const parts = [];
    for (const n of row.querySelectorAll(SEL)) {
      if (n.parentElement && n.parentElement.closest(SEL)) continue;   // evitar anidados
      const t = (n.innerText || '').trim(); if (t) parts.push(t);
    }
    out.push({
      id: row.getAttribute('data-id') || '',
      pre: pre ? (pre.getAttribute('data-pre-plain-text') || '') : '',
      text: parts.join("\\n"),
      links: Array.from(row.querySelectorAll("a[href]")).map(a => a.href),
    });
  }
  return out;
}"""

_JS_SCROLL_UP = """() => {
  const row = document.querySelector("#main [data-id]"); if (!row) return false;
  let el = row.parentElement;
  while (el && el.scrollHeight <= el.clientHeight + 5) el = el.parentElement;
  if (!el || el.scrollTop <= 0) return false;
  el.scrollTop = Math.max(0, el.scrollTop - el.clientHeight * 0.9); return true;
}"""


def _to_raw_message(item: dict, channel_id: str, date_order: str) -> dict:
    return {
        "channel_id": channel_id,
        "message_id": item.get("id") or None,
        "text": item.get("text") or "",
        "posted_at": _parse_pre_plain_text(item.get("pre") or "", date_order),
        "links": [l for l in (item.get("links") or []) if l],
    }


def _read_channel(page, channel_id: str, since: datetime, keywords: list[str]) -> tuple[list[dict], Optional[datetime]]:
    """
    Lee el chat abierto: lo más reciente primero y hasta `max_scrolls` scrolls
    hacia arriba, parando en cuanto aparece algo anterior a `since`.
    Devuelve (ofertas candidatas, mensaje más reciente leído con hora conocida).
    """
    cfg = get_config()
    lo, hi = cfg["scroll_pause"]
    order = str(cfg["date_order"]).lower()
    raws: dict[str, dict] = {}

    for _ in range(int(cfg["max_scrolls"]) + 1):
        new = 0
        for item in page.evaluate(_JS_READ) or []:
            raw = _to_raw_message(item, channel_id, order)
            key = raw["message_id"] or f"{item.get('pre')}|{raw['text'][:80]}"
            if key in raws:
                continue
            raws[key] = raw
            new += 1
        known = [m["posted_at"] for m in raws.values() if m["posted_at"]]
        if known and min(known) < since:
            break  # ya se llegó a lo viejo
        if new == 0 or len(raws) >= int(cfg["max_messages"]):
            break
        if not page.evaluate(_JS_SCROLL_UP):
            break
        _sleep(random.uniform(lo, hi))

    known = [m["posted_at"] for m in raws.values() if m["posted_at"]]
    last_read = max(known) if known else None

    offers: list[dict] = []
    for raw in raws.values():
        if raw["posted_at"] is not None and raw["posted_at"] < since:
            continue  # >= since: la hora tiene resolución de minuto
        # posted_at desconocido: se conserva, no se puede descartar por antigüedad
        if filter_relevant_messages(raw["text"], keywords):
            offers.append(parse_offer_from_message(raw))
    return offers, last_read


def _dedupe(offers: list[dict]) -> list[dict]:
    seen_ids: set = set()
    seen_hashes: set = set()
    out = []
    for o in offers:
        mid, h = o.get("message_id"), _fb._content_hash(o)
        if (mid and mid in seen_ids) or (h and h in seen_hashes):
            continue
        if mid:
            seen_ids.add(mid)
        if h:
            seen_hashes.add(h)
        out.append(o)
    return out


def listen_messages(account: dict, keywords: list[str], since: datetime) -> list[dict]:
    """
    Lee mensajes nuevos desde `since` en los canales/chats confirmados por
    connect_channel (o, si no se llamó, `account["channel_ids"]` / config).
    Por canal usa max(since, marca de agua persistida), así no relee lo ya
    procesado. No hace scroll infinito. Cada mensaje pasa por
    filter_relevant_messages; solo se devuelven ofertas candidatas (esquema de
    parse_offer_from_message). No persiste nada: tras procesar, el orquestador
    llama a update_watermark(canal, get_last_read_at(canal)).

    Si WhatsApp pide QR a mitad de sesión marca la cuenta limited y lanza
    ScanAborted("limited_account") con `partial_posts`. Un fallo transitorio en
    un canal (tras los reintentos) lo omite y sigue con el siguiente.
    """
    cfg = get_config()
    acc_id = _account_id(account)
    channels = list(_CONNECTED.get(acc_id) or account.get("channel_ids") or cfg["channel_ids"] or [])
    if not channels:
        log.warning("listen_messages(%s): no hay canales confirmados/configurados.", acc_id)
        return []

    if since is None:
        since = datetime.now().astimezone() - timedelta(hours=float(cfg["default_lookback_hours"]))
    since = _as_aware(since)

    page = _get_page(account)
    collected: list[dict] = []

    def abort_qr():
        _mark_limited(acc_id, "WhatsApp pide QR: requiere emparejamiento manual")
        close_session(acc_id)
        raise ScanAborted("limited_account", "WhatsApp pide QR", _dedupe(collected))

    if _with_retries(lambda: _wait_state(page), "cargar WhatsApp Web") == "qr":
        abort_qr()

    wait_lo, wait_hi = cfg["wait_between_channels"]
    for i, name in enumerate(channels):
        if i > 0:
            _sleep(random.uniform(wait_lo, wait_hi))
        eff_since = since
        mark = get_watermark(name)
        if mark and mark > eff_since:
            eff_since = mark

        def attempt(n=name, s=eff_since):
            if not page.query_selector(_READY_SELECTOR):
                if page.query_selector(_QR_SELECTOR):
                    abort_qr()
                raise RuntimeError("WhatsApp Web no está listo")
            if not _locate_chat(page, n, open_it=True):
                log.warning("Canal/chat %r no disponible; se omite.", n)
                return [], None
            return _read_channel(page, n, s, keywords)

        try:
            offers, last_read = _with_retries(attempt, f"leer {name}")
        except ScanAborted:
            raise
        except Exception as exc:  # noqa: BLE001 — se omite este canal y se continúa
            log.error("Canal %r omitido tras reintentos: %s", name, exc)
            continue
        collected.extend(offers)
        if last_read:
            _LAST_READ[name] = last_read

    return _dedupe(collected)