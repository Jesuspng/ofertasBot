"""
account_manager.py
==================
Pool de cuentas de Facebook y WhatsApp para rotar sesiones y reducir el riesgo
de limitación.

Reglas transversales que respeta este módulo
--------------------------------------------
* NO envía mensajes a grupos de Facebook ni a chats de WhatsApp. `health_check`
  solo carga páginas en modo lectura.
* NO canjea cupones ni completa compras.
* NO hace login: `load_accounts` solo valida configuración y `health_check`
  solo comprueba si una sesión existente sigue viva.
* Rotación: una cuenta no se reutiliza hasta cumplir su cooldown, y un fallo de
  checkpoint la saca del pool (estado `limited`) sin tumbar el proceso.

Formato de config/accounts.yaml
-------------------------------
accounts:
  - id: fb_01
    platform: facebook            # facebook | whatsapp  (también vale "plataforma")
    session_path: sessions/fb_01  # y/o credentials: {...}
    max_uses_per_day: 20
    cooldown_minutes: 90
  - id: wa_01
    platform: whatsapp
    session_path: sessions/wa_01
    max_uses_per_day: 30
    cooldown_minutes: 60

Notas
-----
* El estado de ejecución (uses_today, cooldown_until, ...) se persiste en
  data/account_state.json (escritura atómica). Las credenciales NUNCA se copian
  al archivo de estado.
* Thread-safe dentro de un proceso (RLock). No coordina varios procesos
  escribiendo el mismo archivo de estado.
* Los cooldowns y limitaciones vencidas se tratan como `healthy` de forma
  perezosa: no hace falta ninguna acción manual ni un job que las limpie.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("account_manager")

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("ACCOUNTS_CONFIG", BASE_DIR / "config" / "accounts.yaml"))
STATE_PATH = Path(os.environ.get("ACCOUNTS_STATE", BASE_DIR / "data" / "account_state.json"))

PLATFORMS = ("facebook", "whatsapp")
HEALTHY, COOLING, LIMITED, BANNED = "healthy", "cooling", "limited", "banned"
DEFAULT_LIMITED_MINUTES = 720  # 12 h
MAX_EVENTS = 50                # historial de auditoría por cuenta

_lock = threading.RLock()
_CONFIG: dict[str, dict] = {}   # id -> config validada
_STATE: dict[str, dict] = {}    # id -> estado de ejecución
_loaded = False


# --------------------------------------------------------------------------- #
# Utilidades internas
# --------------------------------------------------------------------------- #
def _now() -> datetime:
    """Hora local con zona horaria (sobrescribible en tests)."""
    return datetime.now().astimezone()


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.astimezone()


def _new_state(now: datetime) -> dict:
    return {
        "status": HEALTHY,
        "uses_today": 0,
        "counter_date": now.date().isoformat(),
        "last_used_at": None,
        "cooldown_until": None,
        "cooldown_reason": None,
        "last_error": None,
        "last_error_at": None,
        "banned_at": None,
        "events": [],
    }


def _event(st: dict, kind: str, detail: str = "") -> None:
    st["events"].append({"at": _iso(_now()), "kind": kind, "detail": detail})
    del st["events"][:-MAX_EVENTS]


def _save() -> None:
    """Escritura atómica del estado. Un fallo de disco se registra, no se propaga."""
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=STATE_PATH.parent, prefix=".state_", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"accounts": _STATE}, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
    except OSError as exc:
        log.error("No se pudo persistir el estado de cuentas: %s", exc)


def _read_state_file() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh).get("accounts", {})
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.error("Estado ilegible (%s); se arranca con estado limpio.", exc)
        return {}


def _ensure_loaded() -> None:
    if not _loaded:
        load_accounts()


def _effective(account_id: str, now: datetime) -> tuple[str, Optional[datetime], int]:
    """
    Vista de solo lectura del estado: (status, cooldown_until, uses_today)
    aplicando expiración de cooldown/limitación y cambio de día SIN mutar nada.
    """
    st = _STATE[account_id]
    status = st["status"]
    until = _parse(st["cooldown_until"])
    if status in (COOLING, LIMITED) and until and now >= until:
        status, until = HEALTHY, None
    uses = st["uses_today"] if st["counter_date"] == now.date().isoformat() else 0
    return status, until, uses


def _normalize(account_id: str, now: datetime) -> dict:
    """Aplica en el estado real la expiración y el reset por cambio de día."""
    st = _STATE[account_id]
    status, until, uses = _effective(account_id, now)
    if status == HEALTHY and st["status"] in (COOLING, LIMITED):
        _event(st, "expired", f"{st['status']} vencido")
        st["status"], st["cooldown_until"], st["cooldown_reason"] = HEALTHY, None, None
    if st["counter_date"] != now.date().isoformat():
        st["uses_today"], st["counter_date"] = 0, now.date().isoformat()
    return st


def _known(account_id: str) -> bool:
    if account_id not in _CONFIG:
        log.error("Cuenta desconocida: %s", account_id)
        return False
    return True


def _snapshot(account_id: str, now: datetime) -> dict:
    """Config + estado efectivo (copia; seguro de modificar por el llamador)."""
    status, until, uses = _effective(account_id, now)
    st = _STATE[account_id]
    snap = dict(_CONFIG[account_id])
    snap.update(
        status=status,
        uses_today=uses,
        cooldown_until=_iso(until),
        last_used_at=st["last_used_at"],
        last_error=st["last_error"],
    )
    return snap


def _is_eligible(account_id: str, now: datetime) -> bool:
    status, _, uses = _effective(account_id, now)
    # Una cuenta que ya agotó su cupo diario no es elegible hasta el reset de
    # medianoche, aunque su cooldown ya haya vencido.
    return status == HEALTHY and uses < _CONFIG[account_id]["max_uses_per_day"]


def _pick(platform: str, now: datetime, exclude_id: Optional[str] = None) -> Optional[str]:
    """Menor uses_today; en empate, last_used_at más antiguo (None = nunca usada)."""
    epoch = datetime.min.astimezone()
    best, best_key = None, None
    for acc_id, cfg in _CONFIG.items():
        if cfg["platform"] != platform or acc_id == exclude_id or not _is_eligible(acc_id, now):
            continue
        key = (_effective(acc_id, now)[2], _parse(_STATE[acc_id]["last_used_at"]) or epoch)
        if best_key is None or key < best_key:
            best, best_key = acc_id, key
    return best


def _norm_platform(platform: str) -> str:
    return str(platform or "").strip().lower()


# --------------------------------------------------------------------------- #
# API pública
# --------------------------------------------------------------------------- #
def load_accounts() -> dict:
    """
    Lee config/accounts.yaml, valida cada cuenta y devuelve
    {"facebook": [...], "whatsapp": [...]}. Las cuentas inválidas se registran
    en el log y se omiten. No intenta login.
    """
    global _loaded
    result: dict[str, list] = {"facebook": [], "whatsapp": []}
    with _lock:
        try:
            import yaml  # PyYAML
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
        except FileNotFoundError:
            log.error("No existe %s; pool vacío.", CONFIG_PATH)
            raw = {}
        except Exception as exc:  # YAML inválido, PyYAML ausente, etc.
            log.error("No se pudo leer %s: %s", CONFIG_PATH, exc)
            raw = {}

        entries = raw.get("accounts", []) if isinstance(raw, dict) else []
        persisted = _STATE if _loaded else _read_state_file()
        now = _now()
        new_config: dict[str, dict] = {}

        for idx, entry in enumerate(entries if isinstance(entries, list) else []):
            cfg, err = _validate(entry)
            if err:
                label = entry.get("id", f"#{idx}") if isinstance(entry, dict) else f"#{idx}"
                log.error("Cuenta %s omitida: %s", label, err)
                continue
            if cfg["id"] in new_config:
                log.error("Cuenta %s omitida: id duplicado", cfg["id"])
                continue
            new_config[cfg["id"]] = cfg

        _CONFIG.clear()
        _CONFIG.update(new_config)
        _STATE.clear()
        for acc_id in _CONFIG:
            st = persisted.get(acc_id)
            _STATE[acc_id] = {**_new_state(now), **st} if isinstance(st, dict) else _new_state(now)
            result[_CONFIG[acc_id]["platform"]].append(dict(_CONFIG[acc_id]))

        _loaded = True
        _save()
    return result


def _validate(entry) -> tuple[dict, Optional[str]]:
    if not isinstance(entry, dict):
        return {}, "entrada no es un mapa"
    cfg = dict(entry)
    if "plataforma" in cfg and "platform" not in cfg:
        cfg["platform"] = cfg.pop("plataforma")

    missing = [k for k in ("id", "platform", "max_uses_per_day", "cooldown_minutes")
               if cfg.get(k) in (None, "")]
    if not (cfg.get("credentials") or cfg.get("session_path")):
        missing.append("credentials|session_path")
    if missing:
        return {}, f"faltan campos obligatorios: {', '.join(missing)}"

    cfg["id"] = str(cfg["id"]).strip()
    cfg["platform"] = _norm_platform(cfg["platform"])
    if cfg["platform"] not in PLATFORMS:
        return {}, f"plataforma no soportada: {cfg['platform']!r}"
    try:
        cfg["max_uses_per_day"] = int(cfg["max_uses_per_day"])
        cfg["cooldown_minutes"] = int(cfg["cooldown_minutes"])
    except (TypeError, ValueError):
        return {}, "max_uses_per_day y cooldown_minutes deben ser enteros"
    if cfg["max_uses_per_day"] < 1 or cfg["cooldown_minutes"] < 0:
        return {}, "max_uses_per_day debe ser >= 1 y cooldown_minutes >= 0"
    return cfg, None


def get_available_account(platform: str) -> Optional[dict]:
    """Cuenta healthy, sin cooldown y con cupo; menor uses_today, luego LRU. No modifica estado."""
    with _lock:
        _ensure_loaded()
        now = _now()
        acc_id = _pick(_norm_platform(platform), now)
        return _snapshot(acc_id, now) if acc_id else None


def mark_account_used(account_id: str) -> None:
    """uses_today += 1, last_used_at = ahora. Al llegar al máximo diario aplica cooldown."""
    with _lock:
        _ensure_loaded()
        if not _known(account_id):
            return
        now = _now()
        st = _normalize(account_id, now)
        st["uses_today"] += 1
        st["last_used_at"] = _iso(now)
        cfg = _CONFIG[account_id]
        reached = st["uses_today"] >= cfg["max_uses_per_day"]
        _save()
    if reached:
        apply_cooldown(account_id, cfg["cooldown_minutes"], _reason="daily_limit")


def apply_cooldown(account_id: str, minutes: int, _reason: str = "manual") -> None:
    """Estado cooling hasta now + minutes. Nunca rebaja una cuenta limited/banned."""
    with _lock:
        _ensure_loaded()
        if not _known(account_id):
            return
        now = _now()
        st = _normalize(account_id, now)
        if st["status"] in (BANNED, LIMITED):
            log.warning("apply_cooldown ignorado: %s está %s", account_id, st["status"])
            return
        until = now + timedelta(minutes=max(0, int(minutes)))
        st["status"], st["cooldown_until"], st["cooldown_reason"] = COOLING, _iso(until), _reason
        _event(st, "cooldown", f"{minutes} min ({_reason})")
        _save()
        log.info("Cuenta %s en cooldown hasta %s", account_id, _iso(until))


def mark_account_limited(account_id: str, reason: str, minutes: int = DEFAULT_LIMITED_MINUTES) -> None:
    """Checkpoint, login fallido, captcha, restricción... Fuera del pool durante `minutes`."""
    with _lock:
        _ensure_loaded()
        if not _known(account_id):
            return
        now = _now()
        st = _normalize(account_id, now)
        if st["status"] == BANNED:
            log.warning("mark_account_limited ignorado: %s está banned", account_id)
            return
        st["status"] = LIMITED
        st["cooldown_until"] = _iso(now + timedelta(minutes=minutes))
        st["cooldown_reason"] = reason
        st["last_error"], st["last_error_at"] = reason, _iso(now)
        _event(st, "limited", f"{reason} ({minutes} min)")
        _save()
        log.warning("Cuenta %s limitada %d min: %s", account_id, minutes, reason)


def mark_account_banned(account_id: str, reason: str) -> None:
    """Baneo permanente hasta intervención manual (mark_account_healthy)."""
    with _lock:
        _ensure_loaded()
        if not _known(account_id):
            return
        now = _now()
        st = _normalize(account_id, now)
        st["status"], st["cooldown_until"], st["cooldown_reason"] = BANNED, None, reason
        st["banned_at"] = _iso(now)
        st["last_error"], st["last_error_at"] = reason, _iso(now)
        _event(st, "banned", reason)
        _save()
        log.error("Cuenta %s BANEADA: %s", account_id, reason)


def mark_account_healthy(account_id: str) -> None:
    """
    Limpia limitación/cooldown y vuelve a healthy. uses_today solo se resetea
    si cambió el día calendario local.
    """
    with _lock:
        _ensure_loaded()
        if not _known(account_id):
            return
        st = _normalize(account_id, _now())
        if st["status"] == BANNED:
            log.warning("Cuenta %s estaba banned; reactivada manualmente.", account_id)
        st["status"], st["cooldown_until"], st["cooldown_reason"] = HEALTHY, None, None
        st["banned_at"] = None
        _event(st, "healthy", "marcada healthy")
        _save()


def rotate_account(platform: str) -> Optional[dict]:
    """
    Siguiente cuenta elegible distinta de la última usada en la plataforma.
    Si solo hay una sana, la devuelve igual y registra una advertencia.
    """
    platform = _norm_platform(platform)
    with _lock:
        _ensure_loaded()
        now = _now()
        used = [(i, _parse(_STATE[i]["last_used_at"])) for i, c in _CONFIG.items()
                if c["platform"] == platform and _STATE[i]["last_used_at"]]
        last_id = max(used, key=lambda t: t[1] or datetime.min.astimezone())[0] if used else None

        acc_id = _pick(platform, now, exclude_id=last_id)
        if acc_id:
            return _snapshot(acc_id, now)
        acc_id = _pick(platform, now)  # solo queda la última usada (o ninguna)
        if acc_id:
            log.warning("rotate_account(%s): solo hay una cuenta sana (%s); se reutiliza.",
                        platform, acc_id)
            return _snapshot(acc_id, now)
        log.error("rotate_account(%s): no hay cuentas disponibles.", platform)
        return None


# --------------------------------------------------------------------------- #
# Health check (solo lectura: nunca publica ni envía mensajes)
# --------------------------------------------------------------------------- #
class SessionUnusable(Exception):
    """La sesión no sirve por motivos de la cuenta (checkpoint, login, QR, captcha)."""


def _check_facebook(account: dict) -> None:
    session = account.get("session_path")
    if not session:
        raise SessionUnusable("sin session_path: no se hace login automático")
    from playwright.sync_api import sync_playwright  # import perezoso
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(session), headless=True)
        try:
            page = ctx.new_page()
            page.goto("https://www.facebook.com/me", wait_until="domcontentloaded", timeout=30_000)
            url = page.url.lower()
            if "checkpoint" in url:
                raise SessionUnusable("checkpoint de Facebook")
            if "captcha" in url:
                raise SessionUnusable("captcha de Facebook")
            if "/login" in url or page.query_selector("input[name='pass']"):
                raise SessionUnusable("sesión de Facebook caducada (pide login)")
        finally:
            ctx.close()


def _check_whatsapp(account: dict) -> None:
    session = account.get("session_path")
    if not session:
        raise SessionUnusable("sin session_path: no hay sesión local de WhatsApp")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(session), headless=True)
        try:
            page = ctx.new_page()
            page.goto("https://web.whatsapp.com", wait_until="domcontentloaded", timeout=30_000)
            # Lista de chats (sesión viva) o QR (sesión perdida). Selectores
            # frágiles: ajustar si WhatsApp Web cambia su DOM.
            page.wait_for_selector("#pane-side, canvas, div[data-ref]", timeout=45_000)
            if not page.query_selector("#pane-side"):
                raise SessionUnusable("WhatsApp pide QR")
        finally:
            ctx.close()


_HEALTH_CHECKERS: dict[str, Callable[[dict], None]] = {
    "facebook": _check_facebook,
    "whatsapp": _check_whatsapp,
}


def register_health_checker(platform: str, fn: Callable[[dict], None]) -> None:
    """Sustituye el comprobador de una plataforma (útil en tests). `fn` lanza SessionUnusable si falla."""
    _HEALTH_CHECKERS[_norm_platform(platform)] = fn


def health_check(account: dict) -> bool:
    """
    True solo si la sesión es usable. Si la sesión está inutilizable (checkpoint,
    login, QR, captcha) llama a mark_account_limited y devuelve False. Un error
    inesperado (p. ej. Playwright no instalado, red caída) devuelve False y se
    registra como último error, pero NO limita la cuenta: no es culpa de ella.
    Nunca propaga excepciones.
    """
    acc_id = (account or {}).get("id")
    platform = _norm_platform((account or {}).get("platform"))
    checker = _HEALTH_CHECKERS.get(platform)
    if not acc_id or checker is None:
        log.error("health_check: cuenta inválida o plataforma sin comprobador: %r", acc_id)
        return False
    try:
        checker(account)
        return True
    except SessionUnusable as exc:
        mark_account_limited(acc_id, str(exc))
        return False
    except Exception as exc:  # noqa: BLE001 — el proceso no debe caerse
        log.error("health_check(%s) inconcluso: %s", acc_id, exc)
        with _lock:
            _ensure_loaded()
            if acc_id in _STATE:
                _STATE[acc_id]["last_error"] = f"health_check inconcluso: {exc}"
                _STATE[acc_id]["last_error_at"] = _iso(_now())
                _save()
        return False


# --------------------------------------------------------------------------- #
# Consultas y mantenimiento
# --------------------------------------------------------------------------- #
def get_account_status(account_id: str) -> dict:
    """id, plataforma, estado, uses_today, max_uses_per_day, cooldown_until, last_used_at, último error."""
    with _lock:
        _ensure_loaded()
        if account_id not in _CONFIG:
            raise KeyError(f"Cuenta desconocida: {account_id}")
        now = _now()
        status, until, uses = _effective(account_id, now)
        st = _STATE[account_id]
        return {
            "id": account_id,
            "platform": _CONFIG[account_id]["platform"],
            "status": status,
            "uses_today": uses,
            "max_uses_per_day": _CONFIG[account_id]["max_uses_per_day"],
            "cooldown_until": _iso(until),
            "last_used_at": st["last_used_at"],
            "last_error": st["last_error"],
        }


def get_rotation_stats() -> dict:
    """Resumen por plataforma para logs/alertas. Avisa si queda una sola cuenta sana."""
    with _lock:
        _ensure_loaded()
        now = _now()
        stats = {p: {"total": 0, "healthy": 0, "cooling": 0, "limited": 0,
                     "banned": 0, "uses_today": 0} for p in PLATFORMS}
        for acc_id, cfg in _CONFIG.items():
            status, _, uses = _effective(acc_id, now)
            s = stats[cfg["platform"]]
            s["total"] += 1
            s[status] += 1
            s["uses_today"] += uses
        for platform, s in stats.items():
            if s["total"] and s["healthy"] <= 1:
                log.warning("Pool %s: solo %d cuenta(s) sana(s) de %d.",
                            platform, s["healthy"], s["total"])
        return stats


def reset_daily_counters() -> None:
    """Pone uses_today = 0 en todas las cuentas no baneadas. Lo dispara el scheduler a medianoche local."""
    with _lock:
        _ensure_loaded()
        today = _now().date().isoformat()
        for st in _STATE.values():
            if st["status"] != BANNED:
                st["uses_today"], st["counter_date"] = 0, today
        _save()
        log.info("Contadores diarios reiniciados.")