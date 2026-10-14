"""
main.py: orquestador. Solo ordena el trabajo.

No lee grupos, no calcula descuentos, no archiva por su cuenta y NUNCA envía
mensajes a grupos de Facebook ni a chats de WhatsApp. Tampoco canjea cupones
ni completa compras: cada paso llama a un módulo y reacciona a lo que devuelve.

Contrato que se asume de los módulos (ajusta los nombres si difieren):
  - Los monitores devuelven un dict:
        {"items": [...], "status": "ok" | "limited_account" | "cooldown_account"
                          | "checkpoint" | "login_wall", "watermark": <opcional>}
  - quality.is_good_product(url, ...) devuelve un objeto/dict con
        ok: True | False | None   (None = unknown, sin reseñas o sin rating)
  - special_cases.reject_implausible_discount(offer) devuelve "ok" | "needs_review"
"""
from __future__ import annotations

import logging
import os
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from queue import Empty, Queue
from typing import Any, Optional
from zoneinfo import ZoneInfo

log = logging.getLogger("main")

PRIORITY_ORDER = {"low": 0, "normal": 1, "high": 2}
LIMITING_STATUSES = {"limited_account", "cooldown_account", "checkpoint", "login_wall"}
REQUIRED_FIELDS = (
    "timezone", "cycle_minutes", "discount_threshold", "min_rating",
    "min_reviews", "calendar_country", "groups", "channels", "gui",
)


# --------------------------------------------------------------------------
# Tipos
# --------------------------------------------------------------------------
@dataclass
class Settings:
    timezone: str
    cycle_minutes: int
    discount_threshold: int
    min_rating: float
    min_reviews: int
    calendar_country: str
    groups: list
    channels: list
    gui: bool
    keywords: list = field(default_factory=list)
    scan_limit: int = 50
    save_invalid: bool = False
    shutdown_timeout: int = 120
    db_path: str = "data/coupons.db"


@dataclass
class ScanResult:
    platform: str
    items: list = field(default_factory=list)
    reason: Optional[str] = None      # no_account | limited_account | cooldown_account | ...
    account: Any = None
    error: Optional[str] = None
    watermark: Any = None


@dataclass
class CycleReport:
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    accounts_used: int = 0
    read: int = 0
    saved: int = 0
    duplicates: int = 0
    rejected: int = 0
    incomplete: int = 0
    errors: int = 0
    high_alerts: int = 0
    skipped: bool = False

    def text(self) -> str:
        if self.skipped:
            return "Ciclo saltado: ya había otro en curso."
        fin = self.finished_at.strftime("%H:%M:%S") if self.finished_at else "en curso"
        ini = self.started_at.strftime("%H:%M:%S") if self.started_at else "-"
        return (f"Ciclo {ini}-{fin} | cuentas usadas {self.accounts_used} | leídos {self.read} | "
                f"guardados {self.saved} | duplicados {self.duplicates} | "
                f"rechazados {self.rejected} | incompletos {self.incomplete} | "
                f"errores {self.errors} | alertas altas {self.high_alerts}")


@dataclass
class StatusView:
    accounts: list
    last_cycle: str
    next_cycle: str
    active_by_store: dict
    coupons: list
    expiring_48h: int
    warning: str


@dataclass
class Services:
    settings: Settings
    store: Any
    accounts: Any
    facebook: Any
    whatsapp: Any
    analyzer: Any
    expiry: Any
    special_cases: Any
    quality: Any
    calendar: Any
    notifier: Any
    date_helpers: Any
    text_cleaner: Any
    cycle_lock: threading.Lock = field(default_factory=threading.Lock)
    pending_batch: list = field(default_factory=list)
    batch_lock: threading.Lock = field(default_factory=threading.Lock)
    executor: ThreadPoolExecutor = field(default_factory=lambda: ThreadPoolExecutor(max_workers=1))
    scheduler: Any = None
    last_report: Optional[CycleReport] = None
    last_warning: str = ""
    paused: bool = False


# --------------------------------------------------------------------------
# Log (archivo + panel de la ventana)
# --------------------------------------------------------------------------
_LOG_QUEUE: "Queue[str]" = Queue()
_LOG_TZ = ZoneInfo("America/Mexico_City")


def append_log(line: str) -> None:
    stamp = datetime.now(_LOG_TZ).strftime("%H:%M:%S")
    full = f"[{stamp}] {line}"
    log.info(line)
    _LOG_QUEUE.put(full)


def _setup_logging() -> None:
    os.makedirs("logs", exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler("logs/app.log", encoding="utf-8"), logging.StreamHandler()],
    )


# --------------------------------------------------------------------------
# Arranque
# --------------------------------------------------------------------------
def load_config() -> Settings:
    """Lee config/settings.py y .env. No abre la base ni el navegador."""
    try:
        from dotenv import load_dotenv  # opcional
        load_dotenv()
    except ImportError:
        pass
    try:
        from config import settings as cfg
    except ImportError as exc:
        raise SystemExit(f"No se encontró config/settings.py: {exc}")

    values = {}
    for name in REQUIRED_FIELDS + ("keywords", "scan_limit", "save_invalid",
                                    "shutdown_timeout", "db_path"):
        value = os.environ.get(name.upper(), getattr(cfg, name, None))
        if value is not None:
            values[name] = value

    missing = [n for n in REQUIRED_FIELDS if n not in values]
    if missing:
        raise SystemExit("Falta configuración obligatoria: " + ", ".join(missing))

    def _bool(v):
        return v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "si", "sí", "yes")

    try:
        values["cycle_minutes"] = int(values["cycle_minutes"])
        values["discount_threshold"] = int(values["discount_threshold"])
        values["min_rating"] = float(values["min_rating"])
        values["min_reviews"] = int(values["min_reviews"])
        values["gui"] = _bool(values["gui"])
        if "save_invalid" in values:
            values["save_invalid"] = _bool(values["save_invalid"])
        ZoneInfo(values["timezone"])
    except (ValueError, TypeError, KeyError) as exc:
        raise SystemExit(f"Valor de configuración inválido: {exc}")

    return Settings(**values)


def build_services(settings: Settings) -> Services:
    """Crea una sola vez todos los módulos. No escanea."""
    from storage import Store, init_db
    from accounts import AccountManager
    from monitors import FacebookMonitor, WhatsAppMonitor
    from analyzer import CouponAnalyzer
    from expiry import ExpiryManager
    from special_cases import SpecialCases
    from quality import QualityChecker
    from calendar_mx import CalendarMX
    from notifier import Notifier
    from utils import date_helpers, text_cleaner

    if not os.path.exists(settings.db_path):
        os.makedirs(os.path.dirname(settings.db_path) or ".", exist_ok=True)
        init_db(settings.db_path)

    store = Store(settings.db_path)
    accounts = AccountManager(store)
    return Services(
        settings=settings,
        store=store,
        accounts=accounts,
        facebook=FacebookMonitor(settings),
        whatsapp=WhatsAppMonitor(settings),
        analyzer=CouponAnalyzer(date_helpers=date_helpers, text_cleaner=text_cleaner),
        expiry=ExpiryManager(store, date_helpers),
        special_cases=SpecialCases(threshold=settings.discount_threshold),
        quality=QualityChecker(min_rating=settings.min_rating, min_reviews=settings.min_reviews),
        calendar=CalendarMX(country=settings.calendar_country, date_helpers=date_helpers),
        notifier=Notifier(settings),
        date_helpers=date_helpers,
        text_cleaner=text_cleaner,
    )


# --------------------------------------------------------------------------
# Ciclo
# --------------------------------------------------------------------------
def maybe_reset_accounts(services: Services) -> bool:
    """Si cambió el día en México, reinicia SOLO los contadores diarios
    (no toca cooldowns ni cuentas limitadas)."""
    dh = services.date_helpers
    today = dh.start_of_day(dh.now_local())
    last = services.store.get_meta("last_account_reset")
    if last is None or today > last:
        services.accounts.reset_daily_counters()
        services.store.set_meta("last_account_reset", today)
        return True
    return False


def _scan(services: Services, platform: str, monitor: Any, call: str, **kwargs) -> ScanResult:
    result = ScanResult(platform=platform)
    try:
        account = services.accounts.get_available_account(platform)
        if account is None:
            result.reason = "no_account"
            append_log(f"sin cuenta {platform.capitalize()}")
            return result
        result.account = account
        response = getattr(monitor, call)(account, **kwargs)
        status = response.get("status", "ok")
        if status in LIMITING_STATUSES:
            result.reason = status          # no se procesan los posts
            append_log(f"{platform}: cuenta {status}")
            return result
        result.items = response.get("items", [])
        result.watermark = response.get("watermark")
    except Exception as exc:  # un monitor caído no tumba el ciclo
        result.error = str(exc)
        log.exception("fallo en monitor %s", platform)
        append_log(f"{platform}: error {exc}")
    return result


def scan_facebook(services: Services) -> ScanResult:
    s = services.settings
    return _scan(services, "facebook", services.facebook, "scan_posts",
                 keywords=s.keywords, limit=s.scan_limit)


def scan_whatsapp(services: Services) -> ScanResult:
    since = services.store.get_watermark("whatsapp")
    result = _scan(services, "whatsapp", services.whatsapp, "listen_messages", since=since)
    if result.watermark is not None and not result.error and not result.reason:
        services.store.set_watermark("whatsapp", result.watermark)
    return result


def process_offer(services: Services, raw: dict) -> str:
    """duplicate | saved | rejected_quality | incomplete | error"""
    s, st, an = services.settings, services.store, services.analyzer
    try:
        source, ref = raw["source"], raw["source_ref"]

        # 1. Duplicado
        if st.already_seen(source, ref):
            return "duplicate"

        # 2-3. Extracción
        text = services.text_cleaner.clean(raw.get("text", ""))
        platform = an.detect_platform(text)
        url = an.product_url(text)
        code = an.extract_coupon_code(text)
        prices = an.parse_prices(text)
        expires = services.expiry.parse_flexible_date(text, base=raw.get("posted_at"))

        # 4. Aplicabilidad (solo lee texto/ficha: jamás prueba el código en un carrito)
        if not (url or code or prices):
            st.mark_seen(source, ref)
            return "incomplete"
        an.validate_coupon_applicability(text, url=url, code=code, prices=prices)

        # 5. Calidad de la ficha (si falla la red, lanza y cae en 'error' sin mark_seen)
        data_status = "complete"
        quality = None
        if url:
            quality = services.quality.is_good_product(url)
            ok = quality["ok"] if isinstance(quality, dict) else quality.ok
            if ok is False:
                if s.save_invalid:
                    st.save_coupon(_build_offer(raw, platform, url, code, prices, expires,
                                                "invalid", "low", quality))
                st.mark_seen(source, ref)
                return "rejected_quality"
            if ok is None:
                data_status = "unknown"
        else:
            data_status = "unknown"
        if not prices:
            data_status = "unknown"       # dato incompleto → unknown, nunca "buena oferta"

        offer = _build_offer(raw, platform, url, code, prices, expires, data_status, "normal", quality)

        # 6. Caso especial (categoría electrónica o lista explícita + descuento >= umbral)
        special = bool(services.special_cases.is_special_case(offer))
        offer["special_case"] = special
        if services.special_cases.reject_implausible_discount(offer) == "needs_review":
            offer["status"] = "needs_review"
            special = False
            offer["special_case"] = False
        if special:
            offer["priority"] = "high"

        # 7. Prioridad con calendario
        offer["priority"] = services.calendar.adjust_alert_priority(offer["priority"], offer)
        if offer["status"] in ("unknown", "needs_review"):
            offer["priority"] = _cap_priority(offer["priority"], "normal")

        # 8-9. Guardar, marcar visto, avisar
        offer["id"] = st.save_coupon(offer)
        st.mark_seen(source, ref)
        dispatch_alert(services, offer)
        return "saved"
    except Exception:
        log.exception("process_offer falló")
        return "error"


def _cap_priority(priority: str, ceiling: str) -> str:
    return priority if PRIORITY_ORDER.get(priority, 1) <= PRIORITY_ORDER[ceiling] else ceiling


def _build_offer(raw, platform, url, code, prices, expires, status, priority, quality) -> dict:
    return {
        "source": raw["source"], "source_ref": raw["source_ref"],
        "platform": platform, "url": url, "code": code, "prices": prices,
        "expires_at": expires, "status": status, "priority": priority,
        "quality": quality, "text": raw.get("text", ""),
    }


def dispatch_alert(services: Services, offer: dict) -> None:
    """Alta: al momento. Normal/baja: se acumula y sale al cerrar el ciclo.
    Si el envío falla, el cupón ya está guardado."""
    try:
        if offer["priority"] == "high":
            services.notifier.send_priority_alert(offer)
            report = services.last_report
            if report is not None:
                report.high_alerts += 1
        else:
            with services.batch_lock:
                services.pending_batch.append(offer)
    except Exception:
        log.exception("fallo al enviar alerta (cupón %s guardado)", offer.get("id"))


def _flush_batch(services: Services) -> None:
    with services.batch_lock:
        batch, services.pending_batch = services.pending_batch, []
    if not batch:
        return
    try:
        services.notifier.batch_notify(batch)
    except Exception:
        log.exception("fallo en batch_notify (%d cupones guardados)", len(batch))


def finish_accounts(services: Services, fb: ScanResult, wa: ScanResult) -> int:
    used = 0
    for res in (fb, wa):
        if res.account is None:
            continue
        if res.reason in LIMITING_STATUSES:
            services.accounts.mark_account_limited(res.account, reason=res.reason)
        elif res.error is None and res.reason is None:
            services.accounts.mark_account_used(res.account)   # aplica el cooldown
            used += 1
        # con error ajeno a la cuenta: no se cuenta como uso limpio
    for platform in ("facebook", "whatsapp"):
        try:
            if services.accounts.healthy_count(platform) == 1:
                msg = f"queda 1 cuenta de {platform.capitalize()}"
                services.last_warning = msg
                services.notifier.send_system_warning(msg)
        except Exception:
            log.exception("aviso de sistema falló")
    return used


def run_cycle(services: Services) -> CycleReport:
    report = CycleReport()
    if not services.cycle_lock.acquire(blocking=False):
        report.skipped = True
        services.last_report = report
        append_log("ciclo saltado: ya hay otro en curso")
        return report

    report.started_at = services.date_helpers.now_local()
    services.last_report = report
    try:
        maybe_reset_accounts(services)
        fb = scan_facebook(services)
        wa = scan_whatsapp(services)

        for raw in fb.items + wa.items:
            report.read += 1
            label = process_offer(services, raw)
            if label == "saved":
                report.saved += 1
            elif label == "duplicate":
                report.duplicates += 1
            elif label == "rejected_quality":
                report.rejected += 1
            elif label == "incomplete":
                report.incomplete += 1
            else:
                report.errors += 1

        report.accounts_used = finish_accounts(services, fb, wa)
        _flush_batch(services)
    except Exception:
        log.exception("el ciclo terminó con error")
        report.errors += 1
    finally:
        report.finished_at = services.date_helpers.now_local()
        services.cycle_lock.release()
        append_log(report.text())
    return report


# --------------------------------------------------------------------------
# Jobs fuera del ciclo
# --------------------------------------------------------------------------
def run_cleanup_job(services: Services) -> int:
    try:
        expired = services.expiry.check_expired_coupons()
        return int(services.expiry.archive_expired(expired) or 0)
    except Exception:
        log.exception("cleanup falló")
        return 0


def run_expiry_notice_job(services: Services) -> int:
    try:
        rows = services.expiry.notify_expiring_soon(2)
        if not rows:
            return 0
        services.notifier.send_expiring_notice(rows)    # si falla, salta al except
        services.expiry.mark_expiry_warned(rows)        # solo tras envío exitoso
        return len(rows)
    except Exception:
        log.exception("aviso de por vencer falló; el flag no cambia")
        return 0


def run_daily_purge_job(services: Services) -> int:
    try:
        return int(services.expiry.remove_expired(30) or 0)
    except Exception:
        log.exception("purga falló")
        return 0


def start_scheduler(services: Services):
    from apscheduler.schedulers.background import BackgroundScheduler

    tz = ZoneInfo(services.settings.timezone)
    sched = BackgroundScheduler(timezone=tz)
    opts = dict(max_instances=1, coalesce=True)   # ningún job se solapa consigo mismo
    sched.add_job(run_cycle, "interval", minutes=services.settings.cycle_minutes,
                  args=[services], id="cycle", **opts)
    sched.add_job(run_cleanup_job, "interval", hours=6, args=[services], id="cleanup", **opts)
    sched.add_job(run_expiry_notice_job, "interval", hours=6, args=[services], id="expiry", **opts)
    sched.add_job(run_daily_purge_job, "interval", days=1, args=[services], id="purge", **opts)
    sched.start()
    services.scheduler = sched
    return sched


def run_forever(services: Services) -> None:
    stop = threading.Event()
    try:
        while not stop.wait(1):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        shutdown(services)


def shutdown(services: Services) -> None:
    if services.scheduler is not None:
        try:
            services.scheduler.shutdown(wait=False)
        except Exception:
            pass
    # Espera al ciclo en curso (con tope) antes de cerrar nada.
    got = services.cycle_lock.acquire(timeout=services.settings.shutdown_timeout)
    try:
        services.executor.shutdown(wait=False)
        services.store.close()
    finally:
        if got:
            services.cycle_lock.release()
    append_log("proceso cerrado")


# --------------------------------------------------------------------------
# Ventana
# --------------------------------------------------------------------------
_SERVICES: Optional[Services] = None
_UI: dict = {}
_PLATFORM_FILTER = "todas"


def refresh_status(services: Services) -> StatusView:
    """Solo lee. No scrapea."""
    st = services.store
    job = services.scheduler.get_job("cycle") if services.scheduler else None
    nxt = job.next_run_time.strftime("%H:%M:%S") if job and job.next_run_time else "pausado"
    last = services.last_report.text() if services.last_report else "sin ciclos todavía"
    if services.cycle_lock.locked():
        last = "en curso"
    platform = None if _PLATFORM_FILTER == "todas" else _PLATFORM_FILTER
    return StatusView(
        accounts=services.accounts.status_summary(),          # sin contraseñas ni teléfonos
        last_cycle=last,
        next_cycle=nxt,
        active_by_store=st.count_active_by_store(),
        coupons=st.active_coupons(platform=platform),
        expiring_48h=st.count_expiring(hours=48),
        warning=services.last_warning or "sin avisos",
    )


def paint_status(view: StatusView) -> None:
    u = _UI
    u["accounts"].delete("1.0", "end")
    for a in view.accounts:
        phone = str(a.get("phone", ""))
        tail = f" ***{phone[-3:]}" if phone else ""
        u["accounts"].insert("end", f"{a['platform']} | {a['id']}{tail} | {a['state']} | usos hoy {a['uses_today']}\n")
    u["cycle"].config(text=f"{view.last_cycle}  |  próximo: {view.next_cycle}")
    u["stores"].config(text="  ".join(f"{k}: {v}" for k, v in view.active_by_store.items())
                       + f"   (vencen en 48 h: {view.expiring_48h})")
    tree = u["tree"]
    tree.delete(*tree.get_children())
    for c in view.coupons:
        tree.insert("", "end", values=(c["store"], c["code"], f"{c['percent']}%",
                                       c["expires_at"], c["priority"]))
    u["warning"].config(text=view.warning)
    u["search_btn"].config(state="disabled" if _SERVICES.cycle_lock.locked() else "normal")


def _enqueue(fn) -> None:
    def job():
        fn(_SERVICES)
        _UI["root"].after(0, lambda: paint_status(refresh_status(_SERVICES)))
    _SERVICES.executor.submit(job)


def on_search_clicked() -> None:
    _enqueue(run_cycle)             # no salta el cooldown de las cuentas


def on_archive_clicked() -> None:
    _enqueue(run_cleanup_job)


def on_pause_toggled(paused: bool) -> None:
    _SERVICES.paused = paused
    sched = _SERVICES.scheduler
    if sched:
        sched.pause() if paused else sched.resume()   # la búsqueda manual sigue disponible


def on_platform_filter(platform: str) -> None:
    global _PLATFORM_FILTER
    _PLATFORM_FILTER = platform                        # solo filtra, no borra
    paint_status(refresh_status(_SERVICES))


def run_gui(services: Services) -> None:
    import tkinter as tk
    from tkinter import ttk

    global _SERVICES
    _SERVICES = services

    root = tk.Tk()
    root.title("Monitor de cupones")
    root.geometry("820x640")
    _UI["root"] = root

    top = ttk.Frame(root, padding=6)
    top.pack(fill="x")
    _UI["search_btn"] = ttk.Button(top, text="Buscar ahora", command=on_search_clicked)
    _UI["search_btn"].pack(side="left")
    ttk.Button(top, text="Archivar vencidos", command=on_archive_clicked).pack(side="left", padx=4)
    paused = tk.BooleanVar(value=False)
    ttk.Checkbutton(top, text="Pausar", variable=paused,
                    command=lambda: on_pause_toggled(paused.get())).pack(side="left", padx=8)
    flt = ttk.Combobox(top, values=["todas", "facebook", "whatsapp"], state="readonly", width=10)
    flt.set("todas")
    flt.bind("<<ComboboxSelected>>", lambda _e: on_platform_filter(flt.get()))
    flt.pack(side="right")

    ttk.Label(root, text="Cuentas").pack(anchor="w", padx=6)
    _UI["accounts"] = tk.Text(root, height=5, state="normal")
    _UI["accounts"].pack(fill="x", padx=6)

    ttk.Label(root, text="Último ciclo").pack(anchor="w", padx=6)
    _UI["cycle"] = ttk.Label(root, text="-")
    _UI["cycle"].pack(anchor="w", padx=6)

    ttk.Label(root, text="Cupones activos").pack(anchor="w", padx=6)
    _UI["stores"] = ttk.Label(root, text="-")
    _UI["stores"].pack(anchor="w", padx=6)
    cols = ("tienda", "código", "%", "vence", "prioridad")
    tree = ttk.Treeview(root, columns=cols, show="headings", height=8)
    for c in cols:
        tree.heading(c, text=c)
        tree.column(c, width=120)

    def copy_code(_e):
        sel = tree.selection()
        if sel:
            root.clipboard_clear()
            root.clipboard_append(tree.item(sel[0])["values"][1])

    tree.bind("<Double-1>", copy_code)
    tree.pack(fill="x", padx=6)
    _UI["tree"] = tree

    ttk.Label(root, text="Avisos").pack(anchor="w", padx=6)
    _UI["warning"] = ttk.Label(root, text="-")
    _UI["warning"].pack(anchor="w", padx=6)

    log_box = tk.Text(root, height=10)
    log_box.pack(fill="both", expand=True, padx=6, pady=6)
    lines: deque = deque(maxlen=200)

    def pump():
        try:
            while True:
                lines.append(_LOG_QUEUE.get_nowait())
        except Empty:
            pass
        log_box.delete("1.0", "end")
        log_box.insert("end", "\n".join(lines))
        log_box.see("end")
        paint_status(refresh_status(services))
        root.after(3000, pump)

    root.protocol("WM_DELETE_WINDOW", root.destroy)   # cerrar ventana → main llama a shutdown
    root.after(100, pump)
    root.mainloop()


# --------------------------------------------------------------------------
# Punto de entrada
# --------------------------------------------------------------------------
def main() -> None:
    _setup_logging()
    settings = load_config()
    services = build_services(settings)
    start_scheduler(services)
    try:
        if settings.gui:
            run_gui(services)
        else:
            run_forever(services)
            return
    finally:
        if settings.gui:
            shutdown(services)


if __name__ == "__main__":
    main()
