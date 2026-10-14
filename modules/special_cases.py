"""special_cases.py — Detección de ofertas especiales (electrónica con descuento alto).

No notifica, no envía mensajes, no canjea cupones ni completa compras.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

try:
    from config import CATEGORY_KEYWORDS, SPECIAL_CATEGORIES  # type: ignore
except ImportError:
    CATEGORY_KEYWORDS: dict[str, list[str]] = {
        "laptop": ["laptop", "notebook", "portatil", "macbook", "ultrabook", "chromebook"],
        "pc": ["pc gamer", "computadora", "desktop", "torre gamer", "all in one", "mini pc"],
        "celular": ["celular", "smartphone", "iphone", "galaxy", "redmi", "xiaomi", "moto g", "telefono movil"],
        "tablet": ["tablet", "ipad", "galaxy tab"],
        "consola": ["consola", "playstation", "ps5", "ps4", "xbox", "nintendo switch", "steam deck"],
        "audifonos": ["audifonos", "auriculares", "headphones", "earbuds", "airpods", "diadema bluetooth"],
        "tv": ["smart tv", "televisor", "television", "pantalla led", "pantalla oled"],
        "monitor": ["monitor"],
        "camara": ["camara", "gopro", "mirrorless", "dslr"],
        "smartwatch": ["smartwatch", "reloj inteligente", "apple watch"],
    }
    SPECIAL_CATEGORIES: list[str] = []  # categorías no electrónicas habilitadas explícitamente

ELECTRONIC_CATEGORIES = set(CATEGORY_KEYWORDS.keys())


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def check_category(title_or_text: str) -> str | None:
    if not title_or_text or not isinstance(title_or_text, str):
        return None
    norm = _normalize(title_or_text)
    for category, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if re.search(rf"(?<!\w){re.escape(_normalize(kw))}(?!\w)", norm):
                return category
    return None


def calculate_discount_percentage(original: float, current: float) -> float | None:
    try:
        original, current = float(original), float(current)
    except (TypeError, ValueError):
        return None
    if original <= 0 or current < 0 or current > original:
        return None
    return round((original - current) / original * 100, 1)


def _resolve_discount(product: dict[str, Any]) -> float | None:
    pct = calculate_discount_percentage(product.get("original_price"), product.get("price"))
    if pct is not None:
        return pct
    return None


def _category_of(product: dict[str, Any]) -> str | None:
    cat = product.get("category") or check_category(
        " ".join(str(product.get(k) or "") for k in ("title", "description", "text"))
    )
    return cat


def is_special_case(product: dict[str, Any], threshold: float = 65) -> bool:
    if product.get("needs_review"):
        return False
    pct = product.get("discount_percent")
    if pct is None:
        pct = _resolve_discount(product)
    if pct is None:  # dato incompleto → unknown, no falso positivo
        return False
    cat = _category_of(product)
    if cat is None:
        return False
    if cat not in ELECTRONIC_CATEGORIES and cat not in SPECIAL_CATEGORIES:
        return False
    return pct >= threshold


def flag_high_value_deal(product: dict[str, Any]) -> dict[str, Any]:
    pct = product.get("discount_percent")
    if pct is None:
        pct = _resolve_discount(product)
    cat = _category_of(product)
    product["discount_percent"] = pct
    product["category"] = cat
    product["special_case"] = True
    product["priority"] = "high"
    product["special_reason"] = f"{cat} {pct}% off"
    return product


def reject_implausible_discount(product: dict[str, Any], max_percent: float = 95) -> bool:
    """True si el descuento es implausible y se marcó needs_review (no alertar como ganga)."""
    pct = product.get("discount_percent")
    if pct is None:
        pct = _resolve_discount(product)
    if pct is None:
        return False
    if pct > max_percent and not product.get("original_price_verified", False):
        product["needs_review"] = True
        product["special_case"] = False
        return True
    return False
