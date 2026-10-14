"""
utils/platform_detector.py
==========================
Detección de plataforma de comercio electrónico a partir de enlaces o texto.
Solo analiza la URL o el texto; no descarga fichas ni valida cupones.

Reglas transversales:
- Funciones puras: no guardan nada, no llaman a la red y no notifican.
- El resto de módulos las importa; este módulo no crea otros módulos.
- Dato incompleto o no reconocido produce 'unknown', nunca un falso positivo.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlsplit

try:
    from utils.text_cleaner import extract_urls, normalize
except ImportError:
    from text_cleaner import extract_urls, normalize  # type: ignore

UNKNOWN = "unknown"

_EXACT_HOSTS = {
    # amazon
    "amazon.com": "amazon",
    "amazon.com.mx": "amazon",
    "a.co": "amazon",
    "amzn.to": "amazon",
    # mercadolibre
    "mercadolibre.com": "mercadolibre",
    "mercadolibre.com.mx": "mercadolibre",
    "articulo.mercadolibre.com.mx": "mercadolibre",
    "meli.la": "mercadolibre",
    # temu
    "temu.com": "temu",
    # shein
    "shein.com": "shein",
    # aliexpress
    "aliexpress.com": "aliexpress",
    "s.click.aliexpress.com": "aliexpress",
    # walmart
    "walmart.com": "walmart",
    "walmart.com.mx": "walmart",
}

_STORE_DOMAINS = [
    ("amazon.com", "amazon"),
    ("amazon.com.mx", "amazon"),
    ("a.co", "amazon"),
    ("amzn.to", "amazon"),
    ("mercadolibre.com", "mercadolibre"),
    ("mercadolibre.com.mx", "mercadolibre"),
    ("meli.la", "mercadolibre"),
    ("temu.com", "temu"),
    ("shein.com", "shein"),
    ("aliexpress.com", "aliexpress"),
    ("walmart.com", "walmart"),
    ("walmart.com.mx", "walmart"),
]

# Dominios explícitamente no comerciales (redes, mensajería, acortadores genéricos)
_NON_PRODUCT_HOSTS = {
    "wa.me",
    "chat.whatsapp.com",
    "facebook.com",
    "fb.com",
    "instagram.com",
}


def domain_to_platform(url: str) -> str:
    """
    Host en minúsculas, sin www.

    Host -> Resultado:
    - amazon.com, amazon.com.mx, a.co, amzn.to -> amazon
    - mercadolibre.com, mercadolibre.com.mx, articulo.mercadolibre.com.mx, meli.la -> mercadolibre
    - temu.com -> temu
    - shein.com -> shein
    - aliexpress.com, s.click.aliexpress.com -> aliexpress
    - walmart.com, walmart.com.mx -> walmart

    Cualquier otro host, URL vacía o texto que no es URL devuelve unknown.
    No adivina la tienda por el nombre del producto.
    """
    if not url or not isinstance(url, str):
        return UNKNOWN

    s = url.strip()
    if "://" in s:
        netloc = urlsplit(s).netloc
    elif re.match(r"^[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(?:/.*)?$", s):
        netloc = urlsplit("//" + s).netloc
    else:
        return UNKNOWN

    host = netloc.lower().split("@")[-1].split(":")[0].rstrip(".")
    if host.startswith("www."):
        host = host[4:]

    if not host or host in _NON_PRODUCT_HOSTS:
        return UNKNOWN

    for non_prod in ("whatsapp.com", "facebook.com", "fb.com", "instagram.com"):
        if host == non_prod or host.endswith("." + non_prod):
            return UNKNOWN

    if host in _EXACT_HOSTS:
        return _EXACT_HOSTS[host]

    for dom, plat in _STORE_DOMAINS:
        if host.endswith("." + dom):
            return plat

    return UNKNOWN


_TEXT_PATTERNS = [
    (re.compile(r"\bmercado\s+libre\b|\bmercadolibre\b"), "mercadolibre"),
    (re.compile(r"\bamazon\b"), "amazon"),
    (re.compile(r"\btemu\b"), "temu"),
    (re.compile(r"\bshein\b"), "shein"),
    (re.compile(r"\baliexpress\b|\bali\s+express\b"), "aliexpress"),
    (re.compile(r"\bwalmart\b|\bwal-mart\b"), "walmart"),
]


def platform_from_text(text: str) -> str:
    """
    Si no hay URL útil, busca el nombre de la tienda en el texto ya normalizado:
    “mercado libre”, “amazon”, “temu”, “shein”, “aliexpress”, “walmart”.
    La primera coincidencia gana (por orden de aparición en el texto).
    Si no hay ninguna, unknown. Un post puede decir “estilo amazon” y dar falso
    positivo; por eso el analizador prefiere domain_to_platform cuando hay enlace.
    """
    norm = normalize(text)
    if not norm:
        return UNKNOWN

    earliest_idx = len(norm) + 1
    best_platform = UNKNOWN
    for rx, platform in _TEXT_PATTERNS:
        match = rx.search(norm)
        if match and match.start() < earliest_idx:
            earliest_idx = match.start()
            best_platform = platform

    return best_platform


def detect_platform(url_or_text: str) -> str:
    """
    Si extract_urls encuentra al menos una URL, usa la primera cuyo dominio no sea
    unknown. Si todas son unknown, prueba platform_from_text con el resto del
    mensaje. Si tampoco, unknown.
    """
    if not url_or_text or not isinstance(url_or_text, str):
        return UNKNOWN

    urls = extract_urls(url_or_text)
    if urls:
        for u in urls:
            plat = domain_to_platform(u)
            if plat != UNKNOWN:
                return plat
        rest = url_or_text
        for u in urls:
            rest = rest.replace(u, " ")
        return platform_from_text(rest)

    # Si no se detectaron URLs completas, probar si el texto en sí es un dominio o host
    plat = domain_to_platform(url_or_text)
    if plat != UNKNOWN:
        return plat

    return platform_from_text(url_or_text)


def is_product_url(url: str) -> bool:
    """
    False para vacíos, para wa.me, chat.whatsapp.com, facebook.com, fb.com,
    instagram.com y para acortadores que no están en la lista de tiendas.
    True si domain_to_platform no es unknown. No comprueba que la ruta sea una
    ficha (un home de Amazon cuenta como URL de esa tienda). Quien necesite
    “solo ficha de producto” lo filtra después con la ruta, no aquí.
    """
    if not url or not isinstance(url, str) or not url.strip():
        return False
    return domain_to_platform(url) != UNKNOWN


def product_url(urls: list[str]) -> Optional[str]:
    """
    Recorre la lista y devuelve la primera para la que is_product_url es True.
    Si no hay, None. Así un mensaje con enlace de WhatsApp y enlace de
    Mercado Libre se queda con la tienda.
    """
    if not urls:
        return None
    for u in urls:
        if is_product_url(u):
            return u
    return None
