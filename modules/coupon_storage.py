"""
coupon_storage.py
=================
Persiste cupones y ofertas en SQLite (una sola base, separada por plataforma
mediante la columna `platform`) y recuerda qué posts/mensajes ya se procesaron.

Reglas transversales que respeta este módulo
--------------------------------------------
* Solo almacena y consulta: no envía mensajes, no canjea cupones, no completa
  compras ni contacta tiendas.
* Dato incompleto => NULL, nunca un valor inventado: un rating, precio o fecha
  ausentes o inválidos se guardan como NULL (desconocido). Un porcentaje de
  descuento fuera de 0–100 (matemática inválida) se guarda como NULL.
* No se guardan credenciales ni el autor de los posts/mensajes.

Convenciones
------------
* Fechas: UTC, formato fijo "YYYY-MM-DDTHH:MM:SSZ" (comparable como texto).
  Un datetime sin zona se interpreta como hora local; una fecha sin hora
  ("2026-10-15") vence al final de ese día local.
* Identidad de un cupón: platform + code (sin distinguir mayúsculas) +
  product_url (normalizada: sin utm_*/fbclid/gclid, host en minúsculas, sin
  fragmento). Se exige al menos code o product_url; sin ninguno todas las
  ofertas colapsarían en una sola, así que save_coupon lanza ValueError.
* "Precio mejor" en save_coupon = discount_percent mayor (la tabla no tiene
  columna de precio).
* "Activo" (get_all_active_coupons y search_coupons) = status 'active' y
  expiration_date nula o futura. get_coupons_by_platform filtra solo por status
  para que el limpiador pueda encontrar los vencidos que siguen 'active'.
* Si el post/mensaje no tiene id estable (source_ref vacío), already_seen
  devuelve False y mark_seen no hace nada; usa un hash de contenido como
  source_ref (p. ej. "hash:<sha256>") para poder deduplicarlo.

Configuración: ruta de la base en la variable de entorno COUPON_DB_PATH
(por defecto data/coupons.db junto al módulo).
"""

from __future__ import annotations

import logging
import math
import os
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import date, datetime, time as dtime, timezone
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

log = logging.getLogger("coupon_storage")

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("COUPON_DB_PATH", BASE_DIR / "data" / "coupons.db"))

ALLOWED_STATUSES = ("active", "expired", "invalid", "used", "archived")
ALLOWED_DISCOUNT_TYPES = ("percent", "fixed")
SCHEMA_VERSION = 2
_MAX_DESCRIPTION = 2000

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS coupons (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    platform         TEXT NOT NULL,
    code             TEXT,
    description      TEXT,
    discount_type    TEXT CHECK (discount_type IS NULL OR discount_type IN ('percent','fixed')),
    discount_value   REAL CHECK (discount_value IS NULL OR discount_value >= 0),
    min_purchase     REAL CHECK (min_purchase IS NULL OR min_purchase >= 0),
    expiration_date  TEXT,
    source           TEXT,
    source_ref       TEXT,
    product_url      TEXT,
    category         TEXT,
    rating           REAL CHECK (rating IS NULL OR (rating >= 0 AND rating <= 5)),
    review_count     INTEGER CHECK (review_count IS NULL OR review_count >= 0),
    discount_percent REAL CHECK (discount_percent IS NULL OR (discount_percent >= 0 AND discount_percent <= 100)),
    expiry_warned    INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'active'
                     CHECK (status IN ({", ".join(repr(s) for s in ALLOWED_STATUSES)})),
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_coupons_identity
    ON coupons (platform, COALESCE(UPPER(code), ''), COALESCE(product_url, ''));
CREATE INDEX IF NOT EXISTS ix_coupons_platform_status
    ON coupons (platform, status, discount_percent DESC);
CREATE INDEX IF NOT EXISTS ix_coupons_status_expiration
    ON coupons (status, expiration_date);
CREATE TABLE IF NOT EXISTS seen_sources (
    source     TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    seen_at    TEXT NOT NULL,
    PRIMARY KEY (source, source_ref)
);
"""

_initialized: set[str] = set()


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_iso() -> str:
    return _iso(_utcnow())


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def _fold(value) -> Optional[str]:
    """Minúsculas y sin acentos (registrada en SQLite para búsquedas)."""
    return None if value is None else _strip_accents(str(value)).lower()


def _to_utc_iso(value, field: str = "expiration_date") -> Optional[str]:
    """datetime | date | str ISO -> 'YYYY-MM-DDTHH:MM:SSZ' (UTC); None si falta o no se entiende."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, date):
            dt = datetime.combine(value, dtime(23, 59, 59))
        else:
            text = str(value).strip().replace("Z", "+00:00")
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                dt = datetime.combine(date.fromisoformat(text), dtime(23, 59, 59))
            else:
                dt = datetime.fromisoformat(text)
        return _iso(dt if dt.tzinfo else dt.astimezone())  # naive = hora local
    except (ValueError, OverflowError):
        log.warning("%s no interpretable (%r): se guarda como desconocida.", field, value)
        return None


def _float(value, name: str, lo: float = 0.0, hi: Optional[float] = None) -> Optional[float]:
    if value is None or isinstance(value, bool) or (isinstance(value, str) and not value.strip()):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        log.warning("%s inválido (%r): se guarda como desconocido.", name, value)
        return None
    if math.isnan(v) or math.isinf(v) or v < lo or (hi is not None and v > hi):
        log.warning("%s fuera de rango (%r): se guarda como desconocido.", name, value)
        return None
    return v


def _text(value, limit: Optional[int] = None) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    return s[:limit] if limit else s


_TRACKING_PARAMS = {"fbclid", "gclid", "igshid", "mc_cid", "mc_eid"}


def _norm_url(url) -> Optional[str]:
    """URL canónica para identidad: http/https, host en minúsculas, sin tracking ni fragmento."""
    s = _text(url)
    if not s:
        return None
    if not re.match(r"^https?://", s, re.I):
        if re.match(r"^[\w\-]+(\.[\w\-]+)+(/|$)", s):
            s = "https://" + s
        else:
            log.warning("product_url inválida (%r): se guarda como desconocida.", url)
            return None
    parts = urlsplit(s)
    if not parts.netloc:
        return None
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in _TRACKING_PARAMS and not k.lower().startswith("utm_")]
    path = parts.path.rstrip("/") if len(parts.path) > 1 else parts.path
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(sorted(query)), ""))


def _norm_platform(platform) -> str:
    p = (str(platform).strip().lower() if platform is not None else "")
    if not re.fullmatch(r"[a-z0-9_\-]{1,32}", p):
        raise ValueError(f"platform inválida: {platform!r}")
    return p


def _check_status(status: str) -> str:
    s = (status or "").strip().lower()
    if s not in ALLOWED_STATUSES:
        raise ValueError(f"status inválido {status!r}; permitidos: {', '.join(ALLOWED_STATUSES)}")
    return s


def _clean(data: dict) -> dict:
    """Valida y normaliza coupon_data. Lo desconocido pasa a None; lo imposible lanza ValueError."""
    if not isinstance(data, dict):
        raise TypeError("coupon_data debe ser un dict")
    platform = _norm_platform(data.get("platform"))
    code = _text(data.get("code"))
    url = _norm_url(data.get("product_url"))
    if not code and not url:
        raise ValueError("coupon_data necesita al menos 'code' o 'product_url' para identificar la oferta")

    dtype = (_text(data.get("discount_type")) or "").lower() or None
    if dtype not in (None, *ALLOWED_DISCOUNT_TYPES):
        log.warning("discount_type inválido (%r): se guarda como desconocido.", dtype)
        dtype = None
    dvalue = _float(data.get("discount_value"), "discount_value", 0.0, 100.0 if dtype == "percent" else None)
    rc = _float(data.get("review_count"), "review_count", 0.0)
    return {
        "platform": platform,
        "code": code,
        "description": _text(data.get("description"), _MAX_DESCRIPTION),
        "discount_type": dtype,
        "discount_value": dvalue,
        "min_purchase": _float(data.get("min_purchase"), "min_purchase", 0.0),
        "expiration_date": _to_utc_iso(data.get("expiration_date")),
        "source": (_text(data.get("source")) or "").lower() or None,
        "source_ref": _text(data.get("source_ref")),
        "product_url": url,
        "category": _text(data.get("category")),
        "rating": _float(data.get("rating"), "rating", 0.0, 5.0),
        "review_count": int(rc) if rc is not None else None,
        "discount_percent": _float(data.get("discount_percent"), "discount_percent", 0.0, 100.0),
    }


# --------------------------------------------------------------------------- #
# Conexión
# --------------------------------------------------------------------------- #
def _migrate(conn: sqlite3.Connection) -> None:
    """v1 -> v2: añade expiry_warned (aviso de vencimiento próximo ya enviado) a bases existentes."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(coupons)")}
    if "expiry_warned" not in cols:
        try:
            conn.execute("ALTER TABLE coupons ADD COLUMN expiry_warned INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError as exc:  # otro proceso migró a la vez
            if "duplicate column" not in str(exc).lower():
                raise


def _open() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not DB_PATH.exists():
        _initialized.discard(str(DB_PATH))  # archivo borrado con el proceso vivo: recrear tablas
    conn = sqlite3.connect(str(DB_PATH), timeout=30, isolation_level=None)  # transacciones manuales
    conn.row_factory = sqlite3.Row
    conn.create_function("fold", 1, _fold, deterministic=True)
    conn.execute("PRAGMA busy_timeout = 30000")
    key = str(DB_PATH)
    if key not in _initialized:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(_SCHEMA)
        _migrate(conn)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        _initialized.add(key)
    return conn


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    conn = _open()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _tx() -> Iterator[sqlite3.Connection]:
    """Transacción de escritura (BEGIN IMMEDIATE): serializa escritores entre hilos y procesos."""
    conn = _open()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# API pública
# --------------------------------------------------------------------------- #
def init_db() -> None:
    """Crea las tablas e índices si no existen. Es idempotente; el resto de funciones lo invocan solas."""
    _initialized.discard(str(DB_PATH))
    with _conn():
        pass


def save_coupon(coupon_data: dict) -> int:
    """
    Inserta el cupón si no existe la combinación platform + code + product_url
    (status inicial 'active'; el status que traiga coupon_data se ignora) y
    devuelve su id. Si ya existe, solo se actualiza cuando el nuevo trae:
      * una expiration_date posterior (o la existente era desconocida), o
      * un discount_percent mayor (o el existente era desconocido).
    Al actualizar solo se sobrescriben campos con valor nuevo (un dato
    desconocido nunca borra uno conocido). Un cupón 'expired' con nueva fecha
    futura vuelve a 'active'; 'invalid', 'used' y 'archived' no se reactivan.
    Lanza ValueError si falta platform o no hay ni code ni product_url.
    """
    c = _clean(coupon_data)
    now = _now_iso()
    with _tx() as conn:
        row = conn.execute(
            "SELECT * FROM coupons WHERE platform = :platform "
            "AND COALESCE(UPPER(code), '') = COALESCE(UPPER(:code), '') "
            "AND COALESCE(product_url, '') = COALESCE(:product_url, '')", c,
        ).fetchone()

        if row is None:
            cur = conn.execute(
                "INSERT INTO coupons (platform, code, description, discount_type, discount_value, "
                "min_purchase, expiration_date, source, source_ref, product_url, category, rating, "
                "review_count, discount_percent, status, created_at, updated_at) VALUES "
                "(:platform, :code, :description, :discount_type, :discount_value, :min_purchase, "
                ":expiration_date, :source, :source_ref, :product_url, :category, :rating, "
                ":review_count, :discount_percent, 'active', :now, :now)", {**c, "now": now},
            )
            return int(cur.lastrowid)

        better_exp = bool(c["expiration_date"]) and (
            row["expiration_date"] is None or c["expiration_date"] > row["expiration_date"])
        better_price = c["discount_percent"] is not None and (
            row["discount_percent"] is None or c["discount_percent"] > row["discount_percent"])
        if not (better_exp or better_price):
            return int(row["id"])

        sets: dict = {}
        if better_exp:
            sets["expiration_date"] = c["expiration_date"]
            sets["expiry_warned"] = 0  # nueva fecha => merece un nuevo aviso de vencimiento próximo
        if better_price:
            for k in ("discount_type", "discount_value", "discount_percent", "min_purchase"):
                if c[k] is not None:
                    sets[k] = c[k]
        for k in ("rating", "review_count"):  # datos frescos de la tienda
            if c[k] is not None:
                sets[k] = c[k]
        for k in ("description", "category", "source", "source_ref"):  # solo rellenar huecos
            if row[k] is None and c[k] is not None:
                sets[k] = c[k]
        if better_exp and row["status"] == "expired" and c["expiration_date"] > now:
            sets["status"] = "active"
        sets["updated_at"] = now

        assignments = ", ".join(f"{k} = :{k}" for k in sets)
        conn.execute(f"UPDATE coupons SET {assignments} WHERE id = :_id", {**sets, "_id": row["id"]})
        return int(row["id"])


def already_seen(source: str, source_ref: str) -> bool:
    """True si ese post/mensaje ya se registró con mark_seen. Sin source_ref no hay forma de saberlo: False."""
    src, ref = (_text(source) or "").lower(), _text(source_ref)
    if not src or not ref:
        return False
    with _conn() as conn:
        return conn.execute(
            "SELECT 1 FROM seen_sources WHERE source = ? AND source_ref = ?", (src, ref)
        ).fetchone() is not None


def mark_seen(source: str, source_ref: str) -> None:
    """Registra la referencia tras un procesamiento exitoso o un descarte definitivo. Idempotente."""
    src, ref = (_text(source) or "").lower(), _text(source_ref)
    if not src or not ref:
        log.warning("mark_seen ignorado: source/source_ref vacíos (%r, %r).", source, source_ref)
        return
    with _tx() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO seen_sources (source, source_ref, seen_at) VALUES (?, ?, ?)",
            (src, ref, _now_iso()),
        )


_ORDER = ("discount_percent IS NULL, discount_percent DESC, "
          "expiration_date IS NULL, expiration_date ASC, id ASC")


def get_coupons_by_platform(platform: str, status: str = "active") -> list[dict]:
    """
    Cupones de esa plataforma y estado, de mayor a menor discount_percent y
    luego por expiration_date (la que vence primero va antes). Los desconocidos
    (NULL) van al final en ambos criterios. No filtra por fecha: ver docstring del módulo.
    """
    p, s = _norm_platform(platform), _check_status(status)
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM coupons WHERE platform = ? AND status = ? ORDER BY {_ORDER}", (p, s)
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_active_coupons() -> list[dict]:
    """Todos los 'active' cuya expiration_date es nula o futura, con el mismo orden."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM coupons WHERE status = 'active' "
            f"AND (expiration_date IS NULL OR expiration_date > ?) ORDER BY {_ORDER}", (_now_iso(),)
        ).fetchall()
    return [dict(r) for r in rows]


def update_coupon_status(coupon_id: int, status: str) -> None:
    """Cambia el estado (active, expired, invalid, used, archived) y actualiza updated_at."""
    s = _check_status(status)
    with _tx() as conn:
        cur = conn.execute(
            "UPDATE coupons SET status = ?, updated_at = ? WHERE id = ?", (s, _now_iso(), coupon_id)
        )
        if cur.rowcount == 0:
            log.warning("update_coupon_status: no existe el cupón %r.", coupon_id)


def _like(term: str) -> str:
    t = _fold(term) or ""
    return "%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def search_coupons(query: str, include_inactive: bool = False, limit: int = 200) -> list[dict]:
    """
    Coincidencia parcial (sin distinguir mayúsculas ni acentos) en description,
    code, platform y category. Con varias palabras deben aparecer todas, cada
    una en cualquiera de esos campos. Por defecto solo activos (status 'active'
    y no vencidos). Consulta vacía => lista vacía.
    """
    terms = (query or "").split()
    if not terms:
        return []
    clauses, params = [], []
    for term in terms:
        pat = _like(term)
        clauses.append("(" + " OR ".join(
            f"fold({col}) LIKE ? ESCAPE '\\'" for col in ("description", "code", "platform", "category")) + ")")
        params += [pat] * 4
    where = " AND ".join(clauses)
    if not include_inactive:
        where += " AND status = 'active' AND (expiration_date IS NULL OR expiration_date > ?)"
        params.append(_now_iso())
    params.append(max(1, int(limit)))
    with _conn() as conn:
        rows = conn.execute(f"SELECT * FROM coupons WHERE {where} ORDER BY {_ORDER} LIMIT ?", params).fetchall()
    return [dict(r) for r in rows]


def get_coupon(coupon_id: int) -> Optional[dict]:
    """Lectura por id (notificador y limpiador). None si no existe."""
    with _conn() as conn:
        row = conn.execute("SELECT * FROM coupons WHERE id = ?", (coupon_id,)).fetchone()
    return dict(row) if row else None