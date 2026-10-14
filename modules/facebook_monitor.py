"""
facebook_monitor.py
===================
Lee publicaciones recientes de grupos de Facebook ya configurados y devuelve
candidatos a oferta. SOLO LECTURA.

Reglas transversales que respeta este módulo
--------------------------------------------
* No publica, no comenta, no reacciona, no se une a grupos nuevos y no envía
  mensajes. Las únicas interacciones con la página son navegar, hacer scroll y
  expandir "Ver más" en el feed.
* No canjea cupones ni completa compras: los códigos solo se extraen como texto.
* Dato incompleto => desconocido, nunca un positivo inventado: si no se puede
  leer la fecha, `posted_at` es None; si no hay enlace ni código, la oferta se
  marca `incomplete=True`; si no hay `post_id`, queda en None (no se inventa).
* El orquestador decide qué hacer con la cuenta según `handle_rate_limits` /
  `ScanAborted.action`; este módulo nunca llama a AccountManager.

Flujo esperado del orquestador
------------------------------
    account = account_manager.get_available_account("facebook")
    try:
        if not connect_to_groups(account, group_ids):
            account_manager.mark_account_limited(account["id"], "checkpoint/login wall")
        else:
            posts = scan_posts(account, keywords)
            account_manager.mark_account_used(account["id"])
    except ScanAborted as exc:
        posts = exc.partial_posts                      # lo leído hasta el corte
        if exc.action == "cooldown_account":
            account_manager.apply_cooldown(account["id"], 60)
        elif exc.action == "limited_account":
            account_manager.mark_account_limited(account["id"], exc.reason)

Configuración opcional: config/facebook_monitor.yaml (ver DEFAULT_CONFIG) o
set_config(...). Sin archivo se usan los valores por defecto.

Limitaciones honestas
---------------------
* Facebook ofusca su DOM y lo cambia a menudo. Los selectores y la lectura de
  fechas relativas ("2 h", "1 d") son de mejor esfuerzo y habrá que ajustarlos.
* Playwright sync está atado al hilo que lo creó: usa este módulo desde un solo
  hilo. Cierra navegadores con close_session / close_all_sessions (también se
  cierran al salir del proceso).
"""

from __future__ import annotations

import atexit
import hashlib
import logging
import os
import random
import re
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

log = logging.getLogger("facebook_monitor")

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("FB_MONITOR_CONFIG", BASE_DIR / "config" / "facebook_monitor.yaml"))
SESSIONS_DIR = BASE_DIR / "sessions" / "facebook"

DEFAULT_CONFIG: dict = {
    "max_age_hours": 24,                 # posts más viejos se descartan
    "wait_between_groups": [20, 90],     # segundos, espera aleatoria entre grupos
    "scroll_pause": [1.5, 3.5],          # segundos entre scrolls
    "max_scrolls": 40,                   # tope duro por grupo
    "consecutive_old_stop": 3,           # posts viejos seguidos para dejar el grupo (ignora fijados)
    "max_retries": 3,                    # reintentos máximos ante fallos transitorios
    "backoff_base_seconds": 5,
    "backoff_max_seconds": 60,
    "headless": True,
    "group_ids": [],                     # respaldo si no se llamó a connect_to_groups
}

_config_cache: Optional[dict] = None
_overrides: dict = {}


def get_config() -> dict:
    global _config_cache
    if _config_cache is None:
        cfg = dict(DEFAULT_CONFIG)
        try:
            import yaml
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            section = data.get("facebook_monitor", data) if isinstance(data, dict) else {}
            cfg.update({k: v for k, v in section.items() if k in DEFAULT_CONFIG})
        except FileNotFoundError:
            pass
        except Exception as exc:  # YAML inválido, PyYAML ausente...
            log.error("Config ilegible (%s); se usan valores por defecto.", exc)
        _config_cache = cfg
    return {**_config_cache, **_overrides}


def set_config(**kwargs) -> None:
    """Sobrescribe valores de configuración en memoria (útil en tests)."""
    _overrides.update({k: v for k, v in kwargs.items() if k in DEFAULT_CONFIG})


_sleep: Callable[[float], None] = time.sleep  # sobrescribible en tests


# --------------------------------------------------------------------------- #
# Excepciones y clasificación de fallos
# --------------------------------------------------------------------------- #
class FacebookBlocked(Exception):
    """Checkpoint, login wall o limitación detectada en la página."""

    def __init__(self, reason: str, status: Optional[int] = None):
        super().__init__(reason)
        self.status = status


class ScanAborted(Exception):
    """
    El escaneo se detuvo y el orquestador debe actuar sobre la cuenta.
    action: "cooldown_account" | "limited_account". `partial_posts` conserva
    lo candidatos leídos antes del corte.
    """

    def __init__(self, action: str, reason: str = "", partial_posts: Optional[list] = None):
        super().__init__(f"{action}: {reason}")
        self.action = action
        self.reason = reason
        self.partial_posts = partial_posts or []


# Frases ya normalizadas (minúsculas, sin acentos).
LIMITED_PHRASES = (
    "checkpoint", "login wall", "confirm your identity", "confirma tu identidad",
    "confirm it's you", "confirma que eres tu", "account has been locked",
    "tu cuenta ha sido bloqueada", "session expired", "sesion expirada",
)
COOLDOWN_PHRASES = (
    "try again later", "intentalo de nuevo mas tarde", "intenta de nuevo mas tarde",
    "too many requests", "rate limit", "temporarily blocked", "bloqueado temporalmente",
    "limit how often", "limitamos la frecuencia",
)


def _normalize(text: str) -> str:
    t = unicodedata.normalize("NFKD", (text or "").replace("’", "'"))
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", t).strip()


def handle_rate_limits(error: Exception) -> str:
    """
    Clasifica un fallo y devuelve la acción para el orquestador:
      "limited_account"  -> checkpoint, "confirm your identity", login wall
      "cooldown_account" -> HTTP 429, "try again later", bloqueo temporal
      "retry"            -> timeouts y fallos transitorios/desconocidos
    No reintenta nada por sí misma: los reintentos con tope los hace
    `_call_with_retries` (máximo `max_retries`, con backoff exponencial).
    """
    text = _normalize(f"{type(error).__name__} {error}")
    if any(p in text for p in LIMITED_PHRASES):
        return "limited_account"
    status = getattr(error, "status", None) or getattr(error, "status_code", None)
    if status == 429 or re.search(r"\b429\b", text) or any(p in text for p in COOLDOWN_PHRASES):
        return "cooldown_account"
    return "retry"


def _call_with_retries(fn: Callable[[], object], what: str):
    """Ejecuta fn con reintentos acotados. Lanza ScanAborted si hay que actuar sobre la cuenta."""
    cfg = get_config()
    max_retries = int(cfg["max_retries"])
    base, cap = float(cfg["backoff_base_seconds"]), float(cfg["backoff_max_seconds"])
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            label = handle_rate_limits(exc)
            if label != "retry":
                raise ScanAborted(label, str(exc)) from exc
            if attempt >= max_retries:
                log.error("%s: fallo tras %d reintentos: %s", what, max_retries, exc)
                raise
            delay = min(cap, base * (2 ** attempt)) + random.uniform(0, base)
            log.warning("%s: fallo transitorio (%s); reintento %d/%d en %.0fs",
                        what, exc, attempt + 1, max_retries, delay)
            _sleep(delay)


# --------------------------------------------------------------------------- #
# Texto: keywords, códigos, precios, enlaces
# --------------------------------------------------------------------------- #
_DISCOUNT_RE = re.compile(r"descuent|\bcupon|\d\s?%|%\s?off|\d\s?off\b|\bdto\b")
_SHARE_RE = re.compile(r"\bcompart\w*|\bshare\b")
_PRICE_RE = re.compile(r"\$\s?\d[\d,.]*|\b\d[\d,.]*\s*pesos\b", re.I)
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
_CTX_CODE_RE = re.compile(
    r"(?i)\b(?:c[oó]digo|code|cup[oó]n|promo(?:cional)?)\b\s*[:\-–]?\s*[\"“'‘]?([A-Za-z0-9]{4,20})\b"
)
_STANDALONE_CODE_RE = re.compile(r"\b(?=[A-Z0-9]*[A-Z])(?=[A-Z0-9]*\d)[A-Z0-9]{4,20}\b")
_UNIT_LIKE_RE = re.compile(r"^\d+[A-Z]{1,3}$")  # 1080P, 256GB, 144HZ no son cupones


def _find_codes(text: str) -> list[str]:
    """Tokens tipo SAVE20 (mayúsculas con letras y dígitos) o tras 'código:'/'cupón'."""
    found: list[str] = []
    for m in _CTX_CODE_RE.finditer(text or ""):
        tok = m.group(1)
        if tok.isdigit():
            continue
        if any(c.isdigit() for c in tok) or tok.isupper():
            found.append(tok)
    for m in _STANDALONE_CODE_RE.finditer(text or ""):
        if not _UNIT_LIKE_RE.match(m.group(0)):
            found.append(m.group(0))
    return list(dict.fromkeys(found))


def _strip_tracking(url: str) -> str:
    parts = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
         if k.lower() != "fbclid" and not k.lower().startswith("utm_")]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), ""))


def _clean_links(raw_links, text: str) -> list[str]:
    """URLs http/https externas: desenvuelve l.facebook.com y descarta enlaces internos de Facebook."""
    out: list[str] = []
    for url in list(raw_links or []) + _URL_RE.findall(text or ""):
        if not isinstance(url, str):
            continue
        url = url.strip().rstrip(".,;:!?)»”'\"")
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            continue
        host = parts.netloc.lower().split(":")[0]
        if host in ("l.facebook.com", "lm.facebook.com"):
            target = dict(parse_qsl(parts.query)).get("u")
            if not target:
                continue
            url = unquote(target)
            parts = urlsplit(url)
            host = parts.netloc.lower().split(":")[0]
            if parts.scheme not in ("http", "https") or not host:
                continue
        if host == "fb.com" or host == "facebook.com" or host.endswith(".facebook.com"):
            continue
        out.append(_strip_tracking(url))
    return list(dict.fromkeys(out))


def filter_by_keywords(post_text: str, keywords: list[str]) -> bool:
    """
    True si el texto (normalizado: minúsculas, sin acentos) contiene alguna
    keyword exacta (palabra/frase completa) o un patrón de oferta: cupón,
    descuento, "% off", código alfanumérico. Ignora posts que solo dicen
    "compartan" sin oferta (precio, descuento, código) ni enlace.
    """
    if not post_text or not post_text.strip():
        return False
    norm = _normalize(post_text)
    has_discount = bool(_DISCOUNT_RE.search(norm))
    has_price = bool(_PRICE_RE.search(post_text))
    has_url = bool(_URL_RE.search(post_text))
    has_code = bool(_find_codes(post_text))
    kw_hit = False
    for kw in keywords or []:
        k = _normalize(kw)
        if k and re.search(rf"(?<!\w){re.escape(k)}(?!\w)", norm):
            kw_hit = True
            break
    if not (kw_hit or has_discount or has_code):
        return False
    if _SHARE_RE.search(norm) and not (has_discount or has_price or has_url or has_code):
        return False
    return True


def extract_offer_data(post: dict) -> dict:
    """
    Normaliza una publicación cruda al formato de oferta. `author` solo sirve
    para deduplicar; no debe notificarse. Sin enlace ni código => incomplete=True.
    """
    text = post.get("text") or ""
    links = _clean_links(post.get("links"), text)
    codes = _find_codes(text)
    prices = list(dict.fromkeys(m.group(0).strip().rstrip(".,") for m in _PRICE_RE.finditer(text)))
    posted = post.get("posted_at")
    if isinstance(posted, datetime):
        posted = posted.isoformat()
    return {
        "source": "facebook",
        "group_id": post.get("group_id"),
        "post_id": post.get("post_id"),
        "author": post.get("author"),       # solo para deduplicar
        "text": text,
        "posted_at": posted or None,        # None = fecha desconocida
        "links": links,
        "raw_price_mentions": prices,
        "possible_codes": codes,
        "incomplete": not links and not codes,
    }


def _content_hash(post: dict) -> Optional[str]:
    norm = _normalize(post.get("text") or "")
    links = post.get("links") or []
    first = _strip_tracking(links[0]) if links and isinstance(links[0], str) else ""
    if not norm and not first:
        return None  # nada que comparar: no se deduplica por hash
    return hashlib.sha256(f"{norm}|{first}".encode("utf-8")).hexdigest()


def dedupe_posts(posts: list[dict]) -> list[dict]:
    """Elimina duplicados por post_id y por hash(texto normalizado + primer enlace). Conserva el primero."""
    seen_ids: set = set()
    seen_hashes: set = set()
    out: list[dict] = []
    for p in posts:
        pid = p.get("post_id")
        h = _content_hash(p)
        if (pid and pid in seen_ids) or (h and h in seen_hashes):
            continue
        if pid:
            seen_ids.add(pid)
        if h:
            seen_hashes.add(h)
        out.append(p)
    return out


# --------------------------------------------------------------------------- #
# Fechas relativas
# --------------------------------------------------------------------------- #
_REL_RE = re.compile(
    r"^(?:hace\s+)?(\d+)\s*(s|seg\w*|sec\w*|m|min\w*|h|hr\w*|hora\w*|hour\w*|"
    r"d|dia\w*|day\w*|w|sem\w*|week\w*|mes\w*|month\w*)\b"
)


def _parse_relative_time(label: str, now: datetime) -> Optional[datetime]:
    """'5 m', '2 h', '1 d', 'Ahora', 'Ayer'... -> datetime aproximado; None si no se entiende."""
    t = _normalize(label)
    if not t:
        return None
    if t in ("ahora", "now", "just now", "justo ahora", "hace un momento"):
        return now
    if t in ("ayer", "yesterday"):
        return now - timedelta(days=1)
    m = _REL_RE.match(t)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    if unit.startswith(("sem", "w")):
        return now - timedelta(weeks=n)
    if unit.startswith("s"):
        return now - timedelta(seconds=n)
    if unit.startswith(("mes", "mo")):
        return now - timedelta(days=30 * n)
    if unit.startswith("m"):
        return now - timedelta(minutes=n)
    if unit.startswith("h"):
        return now - timedelta(hours=n)
    if unit.startswith("d"):
        return now - timedelta(days=n)
    return None


# --------------------------------------------------------------------------- #
# Navegador (Playwright, perfil persistente por cuenta)
# --------------------------------------------------------------------------- #
_SESSIONS: dict[str, tuple] = {}      # account_id -> (playwright, context)
_CONNECTED: dict[str, list[str]] = {}  # account_id -> grupos accesibles


def _account_id(account: dict) -> str:
    acc_id = (account or {}).get("id")
    if not acc_id:
        raise ValueError("La cuenta no tiene 'id'.")
    return str(acc_id)


def _valid_group_id(group_id) -> bool:
    return bool(re.fullmatch(r"[\w.\-]+", str(group_id or "")))


def _get_context(account: dict):
    acc_id = _account_id(account)
    if acc_id in _SESSIONS:
        return _SESSIONS[acc_id][1]
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
    except Exception:
        pw.stop()
        raise
    _SESSIONS[acc_id] = (pw, ctx)
    return ctx


def close_session(account_id: str) -> None:
    pw_ctx = _SESSIONS.pop(account_id, None)
    _CONNECTED.pop(account_id, None)
    if not pw_ctx:
        return
    pw, ctx = pw_ctx
    for closer in (ctx.close, pw.stop):
        try:
            closer()
        except Exception as exc:  # noqa: BLE001
            log.debug("Error cerrando sesión %s: %s", account_id, exc)


def close_all_sessions() -> None:
    for acc_id in list(_SESSIONS):
        close_session(acc_id)


atexit.register(close_all_sessions)


def _check_block(page) -> None:
    """Lanza FacebookBlocked si la página muestra checkpoint, login wall o limitación."""
    url = page.url.lower()
    if "checkpoint" in url:
        raise FacebookBlocked("checkpoint (url)")
    if "/login" in url or "login.php" in url or page.query_selector("input[name='pass']"):
        raise FacebookBlocked("login wall: sesión no autenticada")
    if page.query_selector("div[role='feed']"):
        return  # feed visible: no se busca texto de bloqueo dentro de posts de usuarios
    try:
        text = _normalize(page.inner_text("body"))[:6000]
    except Exception:  # noqa: BLE001
        return
    for phrase in LIMITED_PHRASES:
        if phrase in text:
            raise FacebookBlocked(f"checkpoint: {phrase}")
    for phrase in COOLDOWN_PHRASES:
        if phrase in text:
            raise FacebookBlocked(f"rate limit: {phrase}", status=429)


def _open_group(page, group_id: str) -> None:
    url = f"https://www.facebook.com/groups/{group_id}/?sorting_setting=CHRONOLOGICAL"
    resp = page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    if resp is not None and resp.status == 429:
        raise FacebookBlocked("HTTP 429 too many requests", status=429)
    try:
        page.wait_for_selector("div[role='feed']", timeout=15_000)
    except Exception:  # noqa: BLE001 — sin feed puede ser falta de acceso o bloqueo; se comprueba abajo
        pass
    _check_block(page)


def connect_to_groups(account: dict, group_ids: list[str]) -> bool:
    """
    Abre la sesión (perfil de navegador en sessions/facebook/<id>) y comprueba
    acceso de lectura a cada grupo. No se une a grupos nuevos.

    * False  -> checkpoint o login wall: el orquestador marca la cuenta limited.
    * True   -> sesión válida. Los grupos sin acceso (no es miembro, no existe)
                se registran y se excluyen del escaneo; no cuentan como fallo
                de la cuenta.
    * Lanza ScanAborted(action="cooldown_account") ante HTTP 429 / "try again later".
    * Errores de infraestructura (p. ej. Playwright no instalado) se propagan:
      no son culpa de la cuenta.
    """
    acc_id = _account_id(account)
    ctx = _get_context(account)
    accessible: list[str] = []
    for gid in group_ids or []:
        if not _valid_group_id(gid):
            log.error("group_id inválido, se omite: %r", gid)
            continue
        page = ctx.new_page()
        try:
            _call_with_retries(lambda g=gid: _open_group(page, g), f"abrir grupo {gid}")
            if page.query_selector("div[role='feed']"):
                accessible.append(gid)
            else:
                log.warning("Grupo %s: sin acceso o feed no disponible (no se une a grupos).", gid)
        except ScanAborted as exc:
            if exc.action == "limited_account":
                log.error("Cuenta %s: %s", acc_id, exc.reason)
                close_session(acc_id)
                return False
            raise
        except Exception as exc:  # noqa: BLE001 — reintentos agotados en este grupo
            log.error("Grupo %s no verificable: %s", gid, exc)
        finally:
            page.close()
        _sleep(random.uniform(2, 6))
    _CONNECTED[acc_id] = accessible
    log.info("Cuenta %s: %d/%d grupos accesibles.", acc_id, len(accessible), len(group_ids or []))
    return True


# --------------------------------------------------------------------------- #
# Lectura del feed
# --------------------------------------------------------------------------- #
_JS_EXPAND = """() => {
  const feed = document.querySelector("div[role='feed']"); if (!feed) return 0; let n = 0;
  for (const el of feed.querySelectorAll("div[role='button']")) {
    const t = (el.innerText || '').trim().toLowerCase();
    if (t === 'ver más' || t === 'ver mas' || t === 'see more') { el.click(); n++; }
  }
  return n;
}"""

_JS_READ = """() => {
  const feed = document.querySelector("div[role='feed']"); if (!feed) return [];
  const out = [];
  for (const a of feed.querySelectorAll("div[role='article']")) {
    if (a.parentElement && a.parentElement.closest("div[role='article']")) continue; // comentarios
    const links = Array.from(a.querySelectorAll("a[href]")).map(x => ({
      href: x.href, text: (x.innerText || '').trim(), label: x.getAttribute('aria-label') || ''}));
    const msg = a.querySelector("[data-ad-comet-preview='message'], [data-ad-preview='message']");
    const text = ((msg ? msg.innerText : a.innerText) || '').trim();
    const au = a.querySelector("h2 a, h3 a, h4 a, strong a");
    out.push({text, author: au ? (au.innerText || '').trim() : '', links});
  }
  return out;
}"""

_POST_ID_RE = re.compile(r"(?:/posts/|/permalink/|story_fbid=|multi_permalinks=)(\d+)")


def _to_raw_post(item: dict, group_id: str, now: datetime) -> dict:
    links = item.get("links") or []
    post_id, posted_at = None, None
    for link in links:
        href = link.get("href") or ""
        m = _POST_ID_RE.search(href)
        if not m:
            continue
        post_id = post_id or m.group(1)
        if posted_at is None:
            posted_at = (_parse_relative_time(link.get("text") or "", now)
                         or _parse_relative_time(link.get("label") or "", now))
    return {
        "group_id": group_id,
        "post_id": post_id,                      # None si no se pudo leer
        "author": item.get("author") or None,
        "text": item.get("text") or "",
        "posted_at": posted_at,                  # None si no se pudo leer
        "links": [l["href"] for l in links if l.get("href")],
    }


def _scan_group(page, group_id: str, keywords: list[str], limit: int, max_age: timedelta) -> list[dict]:
    cfg = get_config()
    lo, hi = cfg["scroll_pause"]
    seen: set = set()
    candidates: list[dict] = []
    read = consecutive_old = stagnant = 0

    for _ in range(int(cfg["max_scrolls"])):
        try:
            if page.evaluate(_JS_EXPAND):
                _sleep(random.uniform(0.5, 1.2))
        except Exception:  # noqa: BLE001 — expandir es opcional
            pass
        now = datetime.now().astimezone()
        new = 0
        for item in page.evaluate(_JS_READ):
            raw = _to_raw_post(item, group_id, now)
            key = raw["post_id"] or _content_hash(raw) or f"{raw['author']}|{raw['text'][:80]}"
            if key in seen:
                continue
            seen.add(key)
            new += 1
            read += 1

            if raw["posted_at"] is not None and now - raw["posted_at"] > max_age:
                consecutive_old += 1  # ignorar fijados: solo se corta tras varios seguidos
                if consecutive_old >= int(cfg["consecutive_old_stop"]):
                    return candidates
                continue
            if raw["posted_at"] is not None:
                consecutive_old = 0
            # posted_at desconocido: se conserva (no se descarta por dato incompleto)

            if filter_by_keywords(raw["text"], keywords):
                candidates.append(extract_offer_data(raw))
            if read >= limit:
                return candidates

        stagnant = stagnant + 1 if new == 0 else 0
        if stagnant >= 3:
            break  # el feed dejó de producir posts nuevos
        _check_block(page)
        page.evaluate("(d) => window.scrollBy(0, d)", random.randint(900, 1600))
        _sleep(random.uniform(lo, hi))
    return candidates


def scan_posts(account: dict, keywords: list[str], limit: int = 50) -> list[dict]:
    """
    Recorre el feed reciente de cada grupo (los verificados por connect_to_groups
    o, si no se llamó, los de config/`account["group_ids"]`) hasta `limit`
    publicaciones POR GRUPO o hasta posts más viejos que max_age_hours. Entre
    grupos espera un tiempo aleatorio (wait_between_groups). Devuelve solo
    candidatos deduplicados; no los guarda.

    Un fallo transitorio en un grupo (tras los reintentos) lo omite y sigue.
    Si hay que actuar sobre la cuenta lanza ScanAborted (action
    "cooldown_account" | "limited_account") con `partial_posts`.
    """
    cfg = get_config()
    acc_id = _account_id(account)
    groups = list(_CONNECTED.get(acc_id) or account.get("group_ids") or cfg["group_ids"] or [])
    groups = [g for g in groups if _valid_group_id(g)]
    if not groups:
        log.warning("scan_posts(%s): no hay grupos accesibles/configurados.", acc_id)
        return []

    ctx = _get_context(account)
    max_age = timedelta(hours=float(cfg["max_age_hours"]))
    wait_lo, wait_hi = cfg["wait_between_groups"]
    collected: list[dict] = []

    for i, gid in enumerate(groups):
        if i > 0:
            _sleep(random.uniform(wait_lo, wait_hi))
        page = ctx.new_page()

        def attempt(g=gid, p=page):
            _open_group(p, g)
            if not p.query_selector("div[role='feed']"):
                log.warning("Grupo %s: feed no disponible, se omite.", g)
                return []
            return _scan_group(p, g, keywords, limit, max_age)

        try:
            collected.extend(_call_with_retries(attempt, f"escanear grupo {gid}"))
        except ScanAborted as exc:
            exc.partial_posts = dedupe_posts(collected)
            if exc.action == "limited_account":
                page.close()
                close_session(acc_id)
            raise
        except Exception as exc:  # noqa: BLE001 — se omite este grupo y se continúa
            log.error("Grupo %s omitido tras reintentos: %s", gid, exc)
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass

    return dedupe_posts(collected)