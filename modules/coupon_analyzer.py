"""
coupon_analyzer.py
==================
Decide la plataforma de una oferta, extrae el cupón, interpreta precios y
estima si el cupón aplica. Lógica pura sobre texto: no hace red, no abre
navegador y NO canjea cupones ni completa compras.

Reglas transversales que respeta este módulo
--------------------------------------------
* Dato incompleto => "unknown"/None, nunca un falso positivo de "buena oferta":
  plataforma no reconocida => applicable="unknown"; sin precio => final_price
  None; dos precios sin etiquetar => no se adivina cuál es el original.
* Matemática inválida (resultado negativo, porcentaje > 100, precios
  incoherentes) se marca y NO produce un porcentaje de descuento inventado.
* No se asume que los cupones se acumulan.

Formato del dict `coupon` (todas las claves son opcionales)
-----------------------------------------------------------
    code                  str        código principal
    text                  str        texto del anuncio (para reglas locales)
    price                 float      precio actual del producto
    min_purchase          float      compra mínima (si no, se lee del texto)
    excluded_categories   list[str]  categorías excluidas conocidas
    discount_type/value   "percent"|"fixed" / float

Qué significa applicable=True
-----------------------------
Solo que ninguna regla local lo contradice (plataforma conocida, hay código,
mínimo cumplido, categoría no excluida, sin restricciones no verificables).
Nunca se comprueba contra la tienda.

Configuración opcional: config/coupon_analyzer.yaml o set_config(...). Las
longitudes de código por plataforma son heurísticas; ajústalas con tus datos.
"""

from __future__ import annotations

import logging
import math
import os
import re
import unicodedata
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, unquote, urlsplit

log = logging.getLogger("coupon_analyzer")

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("COUPON_ANALYZER_CONFIG", BASE_DIR / "config" / "coupon_analyzer.yaml"))

DEFAULT_CONFIG: dict = {
    # Etiquetas de dominio: "amazon." -> amazon, "meli." -> mercadolibre, etc.
    "platform_domain_labels": {
        "amazon": ["amazon", "amzn"],
        "mercadolibre": ["mercadolibre", "meli"],
        "temu": ["temu"],
        "shein": ["shein"],
        "aliexpress": ["aliexpress"],
        "walmart": ["walmart"],
    },
    "short_domains": {"a.co": "amazon"},
    # Nombres de tienda para cuando el texto no trae dominio
    "platform_names": {
        "amazon": ["amazon"],
        "mercadolibre": ["mercadolibre", "mercado libre"],
        "temu": ["temu"],
        "shein": ["shein"],
        "aliexpress": ["aliexpress", "ali express"],
        "walmart": ["walmart", "wal-mart"],
    },
    # Reglas típicas de código por plataforma (heurísticas)
    "code_rules": {
        "default":      {"min_len": 4, "max_len": 20, "allow_lowercase": False, "extra_chars": ""},
        "amazon":       {"min_len": 6, "max_len": 14, "allow_lowercase": False, "extra_chars": ""},
        "mercadolibre": {"min_len": 5, "max_len": 16, "allow_lowercase": False, "extra_chars": ""},
        "temu":         {"min_len": 6, "max_len": 12, "allow_lowercase": True,  "extra_chars": ""},
        "shein":        {"min_len": 5, "max_len": 16, "allow_lowercase": False, "extra_chars": ""},
        "aliexpress":   {"min_len": 5, "max_len": 16, "allow_lowercase": False, "extra_chars": ""},
        "walmart":      {"min_len": 5, "max_len": 16, "allow_lowercase": False, "extra_chars": ""},
    },
    "code_stopwords": [
        "HOY", "LINK", "GRUPOS", "GRUPO", "CANAL", "OFERTA", "OFERTAS", "CUPON", "CUPONES", "CODIGO",
        "GRATIS", "DESCUENTO", "PROMO", "ENVIO", "CLICK", "AQUI", "MEXICO", "WHATSAPP", "TELEGRAM",
        "FACEBOOK", "TIENDA", "PRECIO", "CODE", "FREE", "SALE", "NUEVO", "URGENTE", "AMAZON",
        "MERCADOLIBRE", "TEMU", "SHEIN", "ALIEXPRESS", "WALMART", "SOLO", "PARA", "CON", "ESTE",
        "ESTA", "TODO", "TODOS", "EXTRA", "AHORA", "ULTIMO", "ULTIMOS", "APLICA", "APP", "COUPON",
    ],
    "standalone_code_fallback": True,       # aceptar SAVE20 sin frase al lado (menor confianza)
    "excluded_categories_by_platform": {},  # p. ej. {"amazon": ["gift cards"]}
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
            section = data.get("coupon_analyzer", data) if isinstance(data, dict) else {}
            cfg.update({k: v for k, v in section.items() if k in DEFAULT_CONFIG})
        except FileNotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001
            log.error("Config ilegible (%s); se usan valores por defecto.", exc)
        _config_cache = cfg
    return {**_config_cache, **_overrides}


def set_config(**kwargs) -> None:
    """Sobrescribe configuración en memoria (útil en tests)."""
    _overrides.update({k: v for k, v in kwargs.items() if k in DEFAULT_CONFIG})


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
UNKNOWN = "unknown"
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
_BARE_DOMAIN_RE = re.compile(r"(?<![\w@/.\-])((?:[a-z0-9\-]+\.)+[a-z]{2,})(?:/\S*)?", re.I)
_UNIT_LIKE_RE = re.compile(r"^\d+[A-Z]{1,3}$")  # 1080P, 256GB, 144HZ


def _normalize(text: str) -> str:
    t = unicodedata.normalize("NFKD", (text or "").replace("’", "'"))
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", t).strip()


def _clean_url(url: str) -> str:
    return url.strip().rstrip(".,;:!?)»”'\"")


def _unwrap(url: str) -> str:
    parts = urlsplit(url)
    if parts.netloc.lower() in ("l.facebook.com", "lm.facebook.com"):
        target = dict(parse_qsl(parts.query)).get("u")
        if target:
            return unquote(target)
    return url


def _num(x) -> Optional[float]:
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) or math.isinf(v) else v


# --------------------------------------------------------------------------- #
# 1. Plataforma
# --------------------------------------------------------------------------- #
def detect_platform(url_or_text: str) -> str:
    """
    amazon | mercadolibre | temu | shein | aliexpress | walmart | unknown.
    1) Dominio (etiquetas completas: amazon.com.mx, meli.la, s.click.aliexpress.com).
    2) Si no hay dominio conocido, nombre de la tienda en el texto.
    Si hay señales de varias tiendas distintas => unknown (no se adivina).
    """
    s = url_or_text or ""
    if not s.strip():
        return UNKNOWN
    cfg = get_config()
    labels = {p: set(v) for p, v in cfg["platform_domain_labels"].items()}
    short = cfg["short_domains"]

    hosts: list[str] = []
    for raw in _URL_RE.findall(s):
        hosts.append(urlsplit(_unwrap(_clean_url(raw))).netloc.lower().split("@")[-1].split(":")[0])
    no_urls = _URL_RE.sub(" ", s)
    if not hosts and not re.search(r"\s", s.strip()):
        no_urls = s  # entrada tipo "amazon.com.mx/dp/B0..." sin esquema
    hosts += [m.group(1).lower() for m in _BARE_DOMAIN_RE.finditer(no_urls)]

    found: set[str] = set()
    for host in hosts:
        if host in short:
            found.add(short[host])
        host_labels = set(host.split("."))
        for platform, plabels in labels.items():
            if host_labels & plabels:
                found.add(platform)
    if len(found) == 1:
        return next(iter(found))
    if len(found) > 1:
        return UNKNOWN

    norm = _normalize(no_urls)
    by_name: set[str] = set()
    for platform, names in cfg["platform_names"].items():
        for name in names:
            if re.search(rf"(?<![a-z0-9]){re.escape(_normalize(name))}(?![a-z0-9])", norm):
                by_name.add(platform)
    return next(iter(by_name)) if len(by_name) == 1 else UNKNOWN


# --------------------------------------------------------------------------- #
# 2. Código de cupón
# --------------------------------------------------------------------------- #
_PHRASE_RE = re.compile(
    r"(?i)\b(?:c[oó]digos?|cup[oó]n(?:es)?|usa(?:r)?|use|promo(?:c[oó]digo|cional)?|promo\s*code|code|coupon)\b"
)
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]*")
_STANDALONE_RE = re.compile(r"\b(?=[A-Z0-9]*[A-Z])(?=[A-Z0-9]*\d)[A-Z0-9]{4,20}\b")
_URL_CODE_KEYS = {"coupon", "couponcode", "coupon_code", "promo", "promocode", "promo_code", "code", "voucher"}


def _rules(platform: str) -> dict:
    rules = get_config()["code_rules"]
    return rules.get((platform or "").lower()) or rules["default"]


def _valid_code(token: str, platform: str) -> bool:
    r = _rules(platform)
    tok = token.strip("-_")
    if not (int(r["min_len"]) <= len(tok) <= int(r["max_len"])):
        return False
    extra = r.get("extra_chars", "")
    core = "".join(ch for ch in tok if ch not in extra)
    if not core or not core.isascii() or not core.isalnum() or core.isdigit():
        return False
    if tok.upper() in {w.upper() for w in get_config()["code_stopwords"]}:
        return False
    if tok == tok.upper():
        return not _UNIT_LIKE_RE.match(tok)
    # minúsculas/mixtas: solo plataformas que las admiten y con letra + dígito
    return bool(r.get("allow_lowercase")) and any(c.isdigit() for c in core) and any(c.isalpha() for c in core)


def extract_coupon_codes(text: str, platform: str = UNKNOWN) -> dict:
    """
    {'code': principal|None, 'alternates': [...], 'source': 'context'|'url'|'standalone'|None}.
    Prioridad: código junto a "código/cupón/usa/promo" > parámetro de URL
    (?coupon=...) > token suelto tipo SAVE20 (menor confianza; puede confundirse
    con modelos de producto). Aplica longitud y caracteres típicos de la plataforma.
    """
    text = text or ""
    platform = (platform or UNKNOWN).lower()
    cands: list[tuple[str, str]] = []

    no_urls = _URL_RE.sub(" ", text)
    for m in _PHRASE_RE.finditer(no_urls):
        segment = no_urls[m.end(): m.end() + 90].split("\n\n")[0]
        for tok in _TOKEN_RE.findall(segment)[:5]:
            if _valid_code(tok, platform):
                cands.append((tok.strip("-_"), "context"))
                break

    for raw in _URL_RE.findall(text):
        for key, val in parse_qsl(urlsplit(_unwrap(_clean_url(raw))).query):
            if key.lower() in _URL_CODE_KEYS and _valid_code(val, platform):
                cands.append((val.strip("-_"), "url"))

    if get_config()["standalone_code_fallback"]:
        for m in _STANDALONE_RE.finditer(no_urls):
            if _valid_code(m.group(0), platform):
                cands.append((m.group(0), "standalone"))

    seen: set[str] = set()
    ordered: list[tuple[str, str]] = []
    for code, src in cands:
        if code.upper() not in seen:
            seen.add(code.upper())
            ordered.append((code, src))
    if not ordered:
        return {"code": None, "alternates": [], "source": None}
    return {"code": ordered[0][0], "alternates": [c for c, _ in ordered[1:6]], "source": ordered[0][1]}


def extract_coupon_code(text: str, platform: str) -> Optional[str]:
    """Código principal o None. Los extras están en extract_coupon_codes()['alternates']."""
    return extract_coupon_codes(text, platform)["code"]


# --------------------------------------------------------------------------- #
# 3. Precios
# --------------------------------------------------------------------------- #
_NUM = r"(?:\d{1,3}(?:[.,\u00a0\u202f]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
_PRICE_RE = re.compile(
    rf"(?i)(?:(?P<pre>\bmx\$|\bus\$|\busd\b|\bmxn\b|\$)\s*(?P<n1>{_NUM})|(?P<n2>{_NUM})\s*(?P<post>pesos?|mxn|usd|d[oó]lares))"
)
_STRIKE = "‹tachado›"

_R_THRESHOLD = re.compile(r"minim\w*|mayor(?:es)? a|superior(?:es)? a|envio gratis|\btope\b|maxim\w*|\bhasta\b|limite|compras? de|pedidos? de")
_R_DISCOUNT = re.compile(r"descuento (?:de|por|extra de)|ahorra|ahorro de|cupon de|bono de|rebaja de|\bmenos\b|te regalan|extra de")
_R_ORIGINAL = re.compile(r"\bantes\b|\bwas\b|precio (?:normal|regular|original|anterior|de lista|sin descuento)|normalmente|regularmente|costaba|valia|\blista\b|en lugar de|en vez de|‹tachado›")
_R_CURRENT = re.compile(r"\bahora\b|por solo|\bsolo\b|a solo|\bahorita\b|precio (?:final|oferta|especial|actual|hoy|con descuento)|cuestan?\b|\bpor\s*$")
_A_DISCOUNT = re.compile(r"^\s*(?:de descuento|off\b|dto\b|de ahorro|menos\b|de rebaja|de bono)")
_A_THRESHOLD = re.compile(r"^\s*(?:de compra|en compras|o mas|o mayor|minimos?)")
_PAIR_WORDS = re.compile(r"^\s*(?:a|a solo|por|por solo|hasta)\s*$")
_PAIR_ARROW = re.compile(r"^\s*(?:→|➔|➜|➡\ufe0f?|⇒|>>+|->|=>)\s*$")


def _parse_number(raw: str) -> Optional[float]:
    """'1,299.50' '1.299,50' '1299' '12,5' -> float; None si es ambiguo."""
    s = re.sub(r"[\s\u00a0\u202f]", "", (raw or "").strip())
    if not s or not re.fullmatch(r"[\d.,]+", s):
        return None
    has_dot, has_comma = "." in s, "," in s
    if has_dot and has_comma:
        dec = "." if s.rfind(".") > s.rfind(",") else ","
        thou = "," if dec == "." else "."
        s = s.replace(thou, "").replace(dec, ".")
    elif has_dot or has_comma:
        sep = "." if has_dot else ","
        parts = s.split(sep)
        if len(parts) > 2:
            s = "".join(parts)
        else:
            left, right = parts
            if len(right) == 3 and left and len(left) <= 3:
                s = left + right
            elif len(right) in (1, 2) and left:
                s = f"{left}.{right}"
            else:
                return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v >= 0 else None


def _mark_strikes(text: str) -> str:
    """Convierte texto tachado (U+0336 o ~texto~ de WhatsApp) en una marca detectable."""
    def rep(m):
        return f" {_STRIKE} " + m.group(0).replace("\u0336", "").replace("\u0335", "") + " "
    text = re.sub(r"(?:[^\s\u0335\u0336][\u0335\u0336])+", rep, text or "")
    return re.sub(r"~([^~\n]{1,40})~", lambda m: f" {_STRIKE} {m.group(1)} ", text)


def _price_mentions(text: str) -> tuple[str, list[dict]]:
    """Menciones de precio con su rol: original|current|unknown|threshold|discount."""
    text = _mark_strikes(text)
    ms = []
    for m in _PRICE_RE.finditer(text):
        raw = m.group("n1") or m.group("n2")
        value = _parse_number(raw)
        if value is None:
            continue
        pre = (m.group("pre") or "").lower()
        post = (m.group("post") or "").lower()
        tail = _normalize(text[m.end(): m.end() + 8])
        if pre in ("mx$", "mxn") or post.startswith("peso") or post == "mxn" or re.match(r"^(pesos?|mxn)\b", tail):
            cur = "MXN"
        elif pre in ("us$", "usd") or post in ("usd",) or post.startswith("d"):
            cur = "USD"
        else:
            cur = None  # "$" a secas: moneda no declarada
        ms.append({"value": value, "start": m.start(), "end": m.end(), "currency": cur, "role": "unknown"})

    for i, m in enumerate(ms):
        prev_end = ms[i - 1]["end"] if i else 0
        next_start = ms[i + 1]["start"] if i + 1 < len(ms) else len(text)
        before = _normalize(text[max(prev_end, m["start"] - 40): m["start"]])
        after = text[m["end"]: min(next_start, m["end"] + 25)]
        if _A_DISCOUNT.match(_normalize(after)):
            m["role"] = "discount"
            continue
        if _A_THRESHOLD.match(_normalize(after)):
            m["role"] = "threshold"
            continue
        best = None  # (distancia, prioridad, rol)
        for prio, (role, rx) in enumerate((("threshold", _R_THRESHOLD), ("discount", _R_DISCOUNT),
                                           ("original", _R_ORIGINAL), ("current", _R_CURRENT))):
            found = list(rx.finditer(before))
            if found:
                cand = (len(before) - found[-1].end(), prio, role)
                if best is None or cand < best:
                    best = cand
        strike = list(re.finditer(re.escape(_STRIKE), before))
        if strike and len(before) - strike[-1].end() <= 2:
            best = (0, -1, "original")
        if best:
            m["role"] = best[2]

    # Patrones de par: "de X a Y" y "X → Y"
    for i in range(len(ms) - 1):
        a, b = ms[i], ms[i + 1]
        between = _normalize(text[a["end"]: b["start"]])
        raw_between = text[a["end"]: b["start"]]
        before_a = _normalize(text[max(0, a["start"] - 12): a["start"]])
        if a["role"] in ("unknown", "original") and b["role"] in ("unknown", "current"):
            if re.search(r"\bde\s*$", before_a) and _PAIR_WORDS.match(between):
                a["role"], b["role"] = "original", "current"
            elif _PAIR_ARROW.match(raw_between) and a["value"] >= b["value"]:
                a["role"], b["role"] = "original", "current"
    return text, ms


def parse_prices(text: str) -> dict:
    """
    {'original', 'current', 'currency', 'coherent', 'ambiguous', 'mentions'}.
    original: tachado / "antes" / "precio normal" / "de X a Y". current:
    "ahora" / "por solo" / "por $X". Un solo precio => current, original None.
    Dos o más precios sin etiquetas => ambiguous=True y no se adivina.
    Se ignoran mínimos de compra, topes y montos de descuento ("$200 de descuento").
    Números normalizados a float; 'currency' solo si se declara (MXN/USD).
    """
    _, ms = _price_mentions(text)
    product = [m for m in ms if m["role"] in ("original", "current", "unknown")]
    originals = [m for m in product if m["role"] == "original"]
    currents = [m for m in product if m["role"] == "current"]
    unknown = [m for m in product if m["role"] == "unknown"]

    original = originals[0]["value"] if originals else None
    current, ambiguous = None, False
    if currents:
        current = currents[0]["value"]
    elif len(unknown) == 1:
        current = unknown[0]["value"]
    elif len(unknown) > 1:
        ambiguous = True  # varios precios sin etiqueta: no se adivina

    used = originals[:1] + (currents[:1] or (unknown if len(unknown) == 1 else []))
    curs = {m["currency"] for m in used if m["currency"]}
    return {
        "original": original,
        "current": current,
        "currency": next(iter(curs)) if len(curs) == 1 else None,
        "coherent": (original >= current) if original is not None and current is not None else None,
        "ambiguous": ambiguous,
        "mentions": [m["value"] for m in product],
    }


# --------------------------------------------------------------------------- #
# Descuento y mínimo de compra del texto (apoyo para validar y calcular)
# --------------------------------------------------------------------------- #
_PCT_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:[.,]\d)?)\s?%")
_PCT_NOT_DISCOUNT = re.compile(r"^\s*(?:original|garantiz|algodon|poliester|seguro|autentic|natural|puro)")
_N = r"(\d[\d.,]*\d|\d)"
_MIN_PURCHASE_RES = [re.compile(p) for p in (
    rf"(?:compra|compras|pedido|pedidos) minim[oa]s?(?: de)?\s*\$?\s*{_N}",
    rf"minimo de (?:compra|pedido)s?(?: de)?\s*\$?\s*{_N}",
    rf"(?:compras?|pedidos?) (?:mayores?|superiores?) a\s*\$?\s*{_N}",
    rf"(?:a partir de|desde)\s*\$?\s*{_N}\s*(?:de compra|en compras|de pedido)",
    rf"\bmin(?:imo)?\.?\s*\$\s*{_N}",
)]


def parse_min_purchase(text: str) -> Optional[float]:
    """Compra mínima mencionada en el texto, o None."""
    norm = _normalize(text)
    for rx in _MIN_PURCHASE_RES:
        m = rx.search(norm)
        if m:
            v = _parse_number(m.group(1))
            if v is not None:
                return v
    return None


def parse_discount(text: str) -> dict:
    """
    {'discount_type': 'percent'|'fixed'|None, 'discount_value': float|None, 'ambiguous': bool}.
    Prefiere el descuento junto a "cupón/código/usa/promo". Si hay varios
    distintos y no se puede elegir, devuelve None con ambiguous=True.
    """
    text = text or ""
    _, ms = _price_mentions(text)
    stripped = _mark_strikes(text)  # mismas coordenadas para porcentajes, montos y frases
    cands: list[tuple[str, float, int]] = []
    for m in _PCT_RE.finditer(stripped):
        v = _parse_number(m.group(1))
        if v is None or not 0 < v <= 100 or _PCT_NOT_DISCOUNT.match(_normalize(stripped[m.end(): m.end() + 14])):
            continue
        cands.append(("percent", v, m.start()))
    for m in ms:
        if m["role"] == "discount":
            cands.append(("fixed", m["value"], m["start"]))
    if not cands:
        return {"discount_type": None, "discount_value": None, "ambiguous": False}

    def result(t, v):
        return {"discount_type": t, "discount_value": v, "ambiguous": False}

    phrase_pos = [m.start() for m in _PHRASE_RE.finditer(stripped)]
    near = sorted((min(abs(c[2] - p) for p in phrase_pos), c) for c in cands) if phrase_pos else []
    near = [(d, c) for d, c in near if d <= 60]
    if near:
        distinct = {(c[0], c[1]) for _, c in near}
        if len(distinct) == 1:
            return result(*next(iter(distinct)))
        # varios cerca de la frase: gana el más cercano solo si lo es por margen claro
        (d1, c1), (d2, c2) = near[0], next(x for x in near[1:] if (x[1][0], x[1][1]) != (near[0][1][0], near[0][1][1]))
        if d2 - d1 >= 15:
            return result(c1[0], c1[1])
        return {"discount_type": None, "discount_value": None, "ambiguous": True}

    distinct = {(t, v) for t, v, _ in cands}
    if len(distinct) == 1:
        return result(*next(iter(distinct)))
    return {"discount_type": None, "discount_value": None, "ambiguous": True}


# --------------------------------------------------------------------------- #
# 4. Aplicabilidad (solo reglas locales; nunca se canjea)
# --------------------------------------------------------------------------- #
_EXCLUSION_RE = re.compile(
    r"(?:no aplica(?:n)?(?: (?:en|para|a|con))?|no valid[oa](?: (?:en|para))?|excepto|excluye(?:ndo)?(?: a)?|no incluye(?:n)?)"
    r"\s+([^.;\n!]{3,80})"
)
_NOT_A_CATEGORY = re.compile(r"promo|cupon|oferta|descuento|usuario|cliente|pedido|compra|envio|tienda|\bapp\b|vendedor|otro|otra|marketplace")
_RESTRICTIONS = (
    ("new_users", re.compile(r"usuarios? nuevos?|nuevos usuarios|primera compra|clientes? nuevos?|new users?|first order")),
    ("app_only", re.compile(r"solo (?:en |desde )?(?:la )?app|exclusivo (?:de |en )?(?:la )?app|app only")),
    ("limited_uses", re.compile(r"limitado a|primeros \d+|hasta agotar|stock limitado|solo \d+ usos|\d+ usos")),
)
_EXPIRED_RE = re.compile(
    r"ya no (?:sirve|funciona|jala|aplica)|cupon(?:es)? (?:agotado|vencido|expirado|caducado)|"
    r"cupon ya (?:expiro|caduco|se agoto)|\bexpired\b"
)


def _stem(word: str) -> str:
    w = word.strip()
    return w[:-2] if len(w) > 5 and w.endswith("es") else (w[:-1] if len(w) > 4 and w.endswith("s") else w)


def _category_match(excluded: str, category: str) -> bool:
    ex = {_stem(w) for w in _normalize(excluded).split() if len(w) >= 4}
    cat = {_stem(w) for w in _normalize(category).split() if len(w) >= 4}
    return bool(ex & cat) or _normalize(excluded) == _normalize(category)


def _excluded_from_text(norm: str) -> list[str]:
    out: list[str] = []
    for m in _EXCLUSION_RE.finditer(norm):
        for item in re.split(r",|\sy\s|\se\s|\so\s|/", m.group(1)):
            item = re.sub(r"^(?:la|el|los|las|productos? de|articulos? de|categorias?(?: de)?)\s+", "", item.strip())
            if len(item) >= 3 and not _NOT_A_CATEGORY.search(item):
                out.append(item)
    return list(dict.fromkeys(out))[:8]


def validate_coupon_applicability(coupon: dict, platform: str, product_category: Optional[str]) -> dict:
    """
    {'applicable': True|False|'unknown', 'reasons': [...]}. No contacta a la tienda.
    Reglas locales: plataforma desconocida => unknown; sin código => unknown;
    mínimo de compra (si el texto lo dice) frente al precio; categoría excluida
    (si el anuncio la nombra); restricciones no verificables (usuarios nuevos,
    solo app, usos limitados) => unknown; cupón reportado vencido => False.
    """
    coupon = coupon or {}
    platform = (platform or UNKNOWN).lower()
    if platform not in get_config()["platform_domain_labels"]:
        return {"applicable": UNKNOWN, "reasons": ["platform_unknown"]}

    text = coupon.get("text") or ""
    norm = _normalize(text)
    false_reasons: list[str] = []
    unknown_reasons: list[str] = []
    ok_reasons: list[str] = []

    if not (coupon.get("code") or "").strip():
        unknown_reasons.append("code_missing")

    if _EXPIRED_RE.search(norm):
        false_reasons.append("reported_expired_or_not_working")

    # Mínimo de compra
    min_p = _num(coupon.get("min_purchase"))
    if min_p is None:
        min_p = parse_min_purchase(text)
    if min_p is not None:
        price = _num(coupon.get("price"))
        if price is None:
            price = _num(coupon.get("current_price"))
        if price is None:
            unknown_reasons.append(f"min_purchase_unverified:{min_p:g}")
        elif price < min_p:
            false_reasons.append(f"below_min_purchase:{price:g}<{min_p:g}")
        else:
            ok_reasons.append(f"min_purchase_met:{price:g}>={min_p:g}")

    # Categorías excluidas
    excluded = list(coupon.get("excluded_categories") or [])
    excluded += get_config()["excluded_categories_by_platform"].get(platform, [])
    excluded += _excluded_from_text(norm)
    excluded = list(dict.fromkeys(e for e in excluded if e))
    if excluded:
        if product_category:
            hit = next((e for e in excluded if _category_match(e, product_category)), None)
            if hit:
                false_reasons.append(f"category_excluded:{hit}")
            else:
                ok_reasons.append("category_not_excluded")
        else:
            unknown_reasons.append("category_unknown_but_exclusions_present")

    # Restricciones que no se pueden verificar localmente
    for name, rx in _RESTRICTIONS:
        if rx.search(norm):
            unknown_reasons.append(f"restriction_unverifiable:{name}")

    if false_reasons:
        return {"applicable": False, "reasons": false_reasons + unknown_reasons}
    if unknown_reasons:
        return {"applicable": UNKNOWN, "reasons": unknown_reasons + ok_reasons}
    return {"applicable": True, "reasons": ok_reasons + ["passed_local_rules"]}


# --------------------------------------------------------------------------- #
# 5. Precio final
# --------------------------------------------------------------------------- #
def calculate_final_price(original: Optional[float], current: Optional[float],
                          discount_value: Optional[float], discount_type: str) -> dict:
    """
    Aplica el descuento sobre `current` si existe, si no sobre `original`.
      percent: base * (1 - valor/100)   (valor 20 = 20 %, no 0.2)
      fixed:   base - valor
      none:    sin cupón; final = base
    Nunca devuelve negativo: capa en 0 y marca invalid_math=True (y entonces NO
    se calcula discount_percent, para no fabricar un "100 % de descuento").
    discount_percent es el ahorro total frente al precio original (si existe y
    es coherente) o frente a la base; coupon_discount_percent, solo el del cupón.
    Datos faltantes o tipo desconocido => final_price None (desconocido).
    """
    o, c, d = _num(original), _num(current), _num(discount_value)
    reasons: list[str] = []
    out = {"final_price": None, "discount_percent": None, "coupon_discount_percent": None,
           "base_price": None, "base_used": None, "invalid_math": False,
           "coherent_prices": None, "reasons": reasons}

    if any(v is not None and v < 0 for v in (o, c, d)):
        out["invalid_math"] = True
        reasons.append("negative_input")
        return out
    if o is not None and c is not None:
        out["coherent_prices"] = o >= c
        if o < c:
            reasons.append("original_below_current")
    if c is None and o is None:
        reasons.append("missing_base")
        return out

    base_used = "current" if c is not None else "original"
    base = c if c is not None else o
    out["base_price"], out["base_used"] = base, base_used

    dtype = (discount_type or "none").strip().lower()
    if dtype in ("none", "null", "no", ""):
        raw = base
    elif dtype == "percent":
        if d is None:
            reasons.append("discount_value_missing")
            return out
        raw = base * (1 - d / 100.0)
    elif dtype == "fixed":
        if d is None:
            reasons.append("discount_value_missing")
            return out
        raw = base - d
    else:
        reasons.append("unknown_discount_type")
        return out

    final = raw
    if raw < 0:
        final = 0.0
        out["invalid_math"] = True
        reasons.append("negative_result_capped")
    out["final_price"] = round(final, 2)
    if out["invalid_math"]:
        return out

    if base > 0:
        out["coupon_discount_percent"] = round((base - final) / base * 100, 2)
    ref = o if o is not None else base
    if o is not None and o < (c if c is not None else o):
        out["discount_percent"] = None  # precios incoherentes: no se calcula
    elif ref > 0:
        out["discount_percent"] = round((ref - final) / ref * 100, 2)
    return out


# --------------------------------------------------------------------------- #
# 6. Cupones acumulables
# --------------------------------------------------------------------------- #
_NOT_STACKABLE_RE = re.compile(
    r"no (?:es |son )?acumulables?|no se acumula\w*|no combina\w*|no se combina\w*|no combinables?|"
    r"no se (?:puede|pueden) (?:combinar|acumular|sumar|usar junto)\w*|no se suma\w*|"
    r"no aplica con otras? (?:promociones|cupones|ofertas)|not stackable|cannot be combined|"
    r"(?:un|1) (?:solo )?cupon por (?:compra|pedido|orden)|solo se puede usar un cupon"
)
_STACKABLE_RE = re.compile(
    r"(?<!no )(?<!no es )acumulables?\b|se (?:puede|pueden) (?:combinar|acumular|sumar)|combina con|combinables?\b|stackable"
)


def check_stackable_coupons(coupons: list[dict]) -> dict:
    """
    {'stackable': True|False|'unknown', 'reasons': [...]}.
    False si algún texto dice "no acumulable"/"no combina"/"un cupón por compra".
    Con dos o más cupones y sin restricción explícita => unknown (no se asume
    que se sumen). True solo si el texto lo afirma explícitamente y nadie lo niega.
    """
    coupons = [c for c in (coupons or []) if isinstance(c, dict)]
    with_code = [c for c in coupons if (c.get("code") or "").strip()]
    if len(with_code) < 2:
        return {"stackable": UNKNOWN, "reasons": ["fewer_than_two_coupons"]}

    negative, positive = [], []
    for c in coupons:
        norm = _normalize(c.get("text") or "")
        label = c.get("code") or "?"
        if _NOT_STACKABLE_RE.search(norm):
            negative.append(f"not_stackable_stated:{label}")
        elif _STACKABLE_RE.search(norm):
            positive.append(f"stackable_stated:{label}")
    if negative:
        return {"stackable": False, "reasons": negative}
    if positive:
        return {"stackable": True, "reasons": positive}
    return {"stackable": UNKNOWN, "reasons": ["no_explicit_statement"]}


# --------------------------------------------------------------------------- #
# Orquestación opcional: texto de anuncio -> análisis completo
# --------------------------------------------------------------------------- #
def analyze_offer(text: str, url: Optional[str] = None, product_category: Optional[str] = None) -> dict:
    """Une las funciones anteriores para un anuncio. No contacta a ninguna tienda."""
    text = text or ""
    platform = detect_platform(f"{url or ''} {text}".strip())
    codes = extract_coupon_codes(text, platform)
    prices = parse_prices(text)
    disc = parse_discount(text)
    min_p = parse_min_purchase(text)

    coupon = {
        "code": codes["code"], "text": text, "price": prices["current"], "min_purchase": min_p,
        "discount_type": disc["discount_type"], "discount_value": disc["discount_value"],
    }
    # Hay código pero no se sabe cuánto descuenta => precio final desconocido (no se asume 0).
    dtype = disc["discount_type"] or ("unknown" if codes["code"] else "none")
    return {
        "platform": platform,
        "coupon": {**coupon, "alternates": codes["alternates"], "code_source": codes["source"]},
        "prices": prices,
        "final": calculate_final_price(prices["original"], prices["current"], disc["discount_value"], dtype),
        "applicability": validate_coupon_applicability(coupon, platform, product_category),
    }