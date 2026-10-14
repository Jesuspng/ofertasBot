"""product_quality.py — Evalúa calidad de producto desde la ficha pública.

No compra, no canjea cupones, no inicia sesión, no envía mensajes.
Dato incompleto → "unknown", nunca falso positivo.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections import Counter
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

try:
    import config  # type: ignore
except ImportError:
    config = None

log = logging.getLogger(__name__)


def _cfg(name: str, default: Any) -> Any:
    return getattr(config, name, default) if config else default


# --- Config (sobrescribible desde config.py) ---
MIN_RATING: float = _cfg("QUALITY_MIN_RATING", 4.0)
MIN_REVIEWS: int = _cfg("QUALITY_MIN_REVIEWS", 50)
FETCH_TIMEOUT: float = _cfg("QUALITY_FETCH_TIMEOUT", 8.0)
STRICT_UNKNOWN: bool = _cfg("QUALITY_STRICT_UNKNOWN", False)  # True → página inaccesible = ok False
REJECT_SUSPICIOUS: bool = _cfg("QUALITY_REJECT_SUSPICIOUS", False)
PROS_CONS_ENABLED: bool = _cfg("QUALITY_PROS_CONS_ENABLED", False)
PROS_CONS_LLM: bool = _cfg("QUALITY_PROS_CONS_LLM", False)
USER_AGENT: str = _cfg(
    "QUALITY_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
)
TOPIC_WORDS: list[str] = _cfg(
    "QUALITY_TOPIC_WORDS",
    ["bateria", "envio", "calidad", "pantalla", "sonido", "precio", "empaque",
     "duracion", "rendimiento", "camara", "garantia", "servicio", "material", "talla"],
)
LOGIN_MARKERS = ("iniciar sesion", "inicia sesion", "sign in", "log in", "captcha", "verify you are human")

_NUM_RE = re.compile(r"\d[\d.,]*")


# ---------- helpers ----------
def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = _NUM_RE.search(str(value))
    if not m:
        return None
    s = m.group(0)
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".") if len(s.split(",")[-1]) <= 2 else s.replace(",", "")
    elif s.count(".") > 1 or (s.count(".") == 1 and len(s.split(".")[-1]) == 3):
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def _to_int(value: Any) -> int | None:
    f = _to_float(value)
    return int(f) if f is not None else None


def _get(url: str) -> requests.Response | None:
    if urlparse(url).scheme not in ("http", "https"):
        return None
    try:
        r = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "es-MX,es;q=0.9,en;q=0.5"},
            timeout=FETCH_TIMEOUT,
            allow_redirects=True,
        )
    except requests.RequestException as e:
        log.warning("fetch failed %s: %s", url, e)
        return None
    if r.status_code != 200:
        log.info("status %s for %s", r.status_code, url)
        return None
    return r


def _is_login_wall(final_url: str, html: str) -> bool:
    path = urlparse(final_url).path.lower()
    if any(k in path for k in ("login", "signin", "ap/signin", "authentication")):
        return True
    head = _norm(html[:6000])
    soup_title = _norm(BeautifulSoup(html, "html.parser").title.string or "") if "<title" in html[:6000].lower() else ""
    return any(m in soup_title for m in LOGIN_MARKERS) or (
        any(m in head for m in LOGIN_MARKERS) and "application/ld+json" not in html
    )


def _iter_jsonld(soup: BeautifulSoup):
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                yield node
                if "@graph" in node:
                    stack.extend(node["@graph"] if isinstance(node["@graph"], list) else [node["@graph"]])


def _is_type(node: dict, name: str) -> bool:
    t = node.get("@type")
    return name in t if isinstance(t, list) else t == name


# ---------- API ----------
def fetch_product_page(url: str) -> dict | None:
    """Extrae title, price, rating, review_count, list_price. None si login-wall/404/error."""
    r = _get(url)
    if r is None or _is_login_wall(r.url, r.text):
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    title = price = rating = review_count = list_price = None

    # 1) JSON-LD (más fiable)
    for node in _iter_jsonld(soup):
        if _is_type(node, "Product"):
            title = title or node.get("name")
            offers = node.get("offers")
            if isinstance(offers, list):
                offers = offers[0] if offers else None
            if isinstance(offers, dict):
                price = price or _to_float(offers.get("price") or offers.get("lowPrice"))
                spec = offers.get("priceSpecification")
                if isinstance(spec, dict) and spec.get("priceType", "").lower().endswith("listprice"):
                    list_price = list_price or _to_float(spec.get("price"))
            agg = node.get("aggregateRating")
            if isinstance(agg, dict):
                rating = rating or _to_float(agg.get("ratingValue"))
                review_count = review_count or _to_int(agg.get("reviewCount") or agg.get("ratingCount"))

    # 2) Microdatos / meta
    def meta(*names: str) -> str | None:
        for n in names:
            t = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"itemprop": n}) \
                or soup.find("meta", attrs={"name": n})
            if t and t.get("content"):
                return t["content"]
        return None

    title = title or meta("og:title", "name") or (soup.title.string.strip() if soup.title and soup.title.string else None)
    price = price or _to_float(meta("product:price:amount", "price", "og:price:amount"))
    rating = rating or _to_float(meta("ratingValue"))
    review_count = review_count or _to_int(meta("reviewCount", "ratingCount"))

    # 3) Precio de lista tachado
    if list_price is None:
        for el in soup.select("del, s, .list-price, .price-old, .andes-money-amount--previous"):
            v = _to_float(el.get_text(" ", strip=True))
            if v:
                list_price = v
                break

    if not any((title, price, rating)):
        return None

    return {
        "url": url,
        "title": title.strip() if isinstance(title, str) else title,
        "price": price,
        "rating": rating,
        "review_count": review_count,
        "list_price": list_price,
    }


def fetch_product_reviews(url: str, max_reviews: int = 20) -> list[dict]:
    """Primera página de reseñas visibles (JSON-LD Review o marcado itemprop). Sin login."""
    r = _get(url)
    if r is None or _is_login_wall(r.url, r.text):
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    out: list[dict] = []

    for node in _iter_jsonld(soup):
        reviews = node.get("review") if _is_type(node, "Product") else ([node] if _is_type(node, "Review") else None)
        if isinstance(reviews, dict):
            reviews = [reviews]
        for rv in reviews or []:
            if not isinstance(rv, dict):
                continue
            rr = rv.get("reviewRating") or {}
            out.append({
                "text": (rv.get("reviewBody") or rv.get("description") or "").strip(),
                "stars": _to_float(rr.get("ratingValue")) if isinstance(rr, dict) else None,
                "date": rv.get("datePublished"),
            })

    if not out:
        for el in soup.select('[itemprop="review"], .review, .ui-review-capability-comments'):
            body = el.select_one('[itemprop="reviewBody"], .review-text, .ui-review-capability-comments__comment__content')
            star = el.select_one('[itemprop="ratingValue"]')
            date = el.select_one('[itemprop="datePublished"], time')
            out.append({
                "text": (body.get_text(" ", strip=True) if body else el.get_text(" ", strip=True))[:1000],
                "stars": _to_float(star.get("content") or star.get_text()) if star else None,
                "date": (date.get("datetime") or date.get_text(strip=True)) if date else None,
            })

    def _key(rv: dict):
        try:
            return datetime.fromisoformat(str(rv["date"])[:10])
        except (KeyError, ValueError, TypeError):
            return datetime.min

    out = [rv for rv in out if rv.get("text") or rv.get("stars") is not None]
    out.sort(key=_key, reverse=True)
    return out[:max_reviews]


def analyze_rating(rating: float | None, review_count: int | None) -> dict:
    if rating is None or review_count is None:
        return {"pass": "unknown", "rating": rating, "review_count": review_count,
                "reason": "missing rating or review_count"}
    ok = rating >= MIN_RATING and review_count >= MIN_REVIEWS
    reason = "meets thresholds" if ok else (
        f"rating {rating} < {MIN_RATING}" if rating < MIN_RATING else f"reviews {review_count} < {MIN_REVIEWS}"
    )
    return {"pass": ok, "rating": rating, "review_count": review_count, "reason": reason}


def detect_fake_reviews_heuristic(reviews: list[dict]) -> dict:
    signals: list[str] = []
    if len(reviews) < 5:
        return {"suspicious": False, "signals": ["insufficient_sample"]}

    # 1) Muchas reseñas el mismo día
    days = Counter(str(rv.get("date"))[:10] for rv in reviews if rv.get("date"))
    if days:
        day, n = days.most_common(1)[0]
        if n >= 5 and n / len(reviews) >= 0.5:
            signals.append(f"same_day_burst:{day}:{n}")

    # 2) Textos casi idénticos (Jaccard de tokens)
    token_sets = [set(_norm(rv.get("text", "")).split()) for rv in reviews if rv.get("text")]
    similar_pairs = 0
    for i in range(len(token_sets)):
        for j in range(i + 1, len(token_sets)):
            a, b = token_sets[i], token_sets[j]
            if a and b and len(a & b) / len(a | b) >= 0.8:
                similar_pairs += 1
    if similar_pairs >= 3:
        signals.append(f"near_duplicate_texts:{similar_pairs}")

    # 3) Solo 5 estrellas con textos muy cortos
    short_five = sum(
        1 for rv in reviews
        if rv.get("stars") == 5 and len((rv.get("text") or "").split()) < 5
    )
    if short_five / len(reviews) >= 0.6:
        signals.append(f"short_five_star:{short_five}/{len(reviews)}")

    return {"suspicious": bool(signals), "signals": signals}


def is_good_product(url: str) -> dict:
    result: dict[str, Any] = {
        "ok": "unknown", "rating": None, "review_count": None,
        "suspicious": False, "title": None, "price_from_page": None,
    }
    page = fetch_product_page(url)
    if page is None:
        if STRICT_UNKNOWN:
            result["ok"] = False
        return result

    result.update(
        title=page["title"], price_from_page=page["price"],
        rating=page["rating"], review_count=page["review_count"],
    )
    rating_res = analyze_rating(page["rating"], page["review_count"])

    reviews = fetch_product_reviews(url)
    fake = detect_fake_reviews_heuristic(reviews)
    result["suspicious"] = fake["suspicious"]
    result["signals"] = fake["signals"]

    if rating_res["pass"] is False:
        result["ok"] = False
    elif fake["suspicious"] and REJECT_SUSPICIOUS:
        result["ok"] = False
    elif rating_res["pass"] is True:
        result["ok"] = True
    else:
        result["ok"] = "unknown"
    return result


def extract_pros_cons(reviews: list[dict]) -> dict:
    empty = {"pros": [], "cons": []}
    if not PROS_CONS_ENABLED:
        return empty
    if PROS_CONS_LLM:
        log.warning("QUALITY_PROS_CONS_LLM habilitado pero no implementado; usando conteo local")

    high, low = Counter(), Counter()
    for rv in reviews:
        stars, text = rv.get("stars"), _norm(rv.get("text", ""))
        if stars is None or not text:
            continue
        bucket = high if stars >= 4 else low if stars <= 2 else None
        if bucket is None:
            continue
        for w in TOPIC_WORDS:
            if re.search(rf"\b{re.escape(w)}\b", text):
                bucket[w] += 1

    pros = [w for w, _ in high.most_common(5) if high[w] > low[w]]
    cons = [w for w, _ in low.most_common(5) if low[w] >= high[w]]
    return {"pros": pros, "cons": cons}
