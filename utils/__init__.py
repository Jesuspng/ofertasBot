"""
utils
=====
Módulos de utilidades compartidas:
- text_cleaner: Limpieza y normalización de texto, extracción de URLs, dinero y porcentajes.
- date_helpers: Manejo y normalización de fechas aware en zona horaria local.
- platform_detector: Detección e identificación de plataformas comerciales a partir de enlaces o texto.

Reglas transversales:
- Son funciones puras: no guardan nada, no llaman a la red y no notifican.
- El resto de módulos las importa; ellas no crean otros módulos ni dependen de capas superiores.
"""

from __future__ import annotations

from utils.date_helpers import (
    add_hours,
    end_of_day,
    ensure_aware,
    is_before,
    now_local,
    parse_flexible_date,
    start_of_day,
)
from utils.platform_detector import (
    detect_platform,
    domain_to_platform,
    is_product_url,
    platform_from_text,
    product_url,
)
from utils.text_cleaner import (
    extract_money,
    extract_percent,
    extract_urls,
    normalize,
)

__all__ = [
    "normalize",
    "extract_urls",
    "extract_money",
    "extract_percent",
    "now_local",
    "ensure_aware",
    "start_of_day",
    "end_of_day",
    "parse_flexible_date",
    "add_hours",
    "is_before",
    "domain_to_platform",
    "platform_from_text",
    "detect_platform",
    "is_product_url",
    "product_url",
]
