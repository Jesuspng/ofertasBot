"""
utils/text_cleaner.py
=====================
Limpieza y preprocesamiento de texto de posts y mensajes de WhatsApp antes de
buscar keywords, precios o códigos.

Reglas transversales:
- Funciones puras: no guardan nada, no llaman a la red y no notifican.
- El resto de módulos las importa; este módulo no crea otros módulos.
- No adivina ni inventa datos faltantes.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional


def normalize(text: str) -> str:
    """
    Pasa a minúsculas, quita acentos (válido → valido) y colapsa espacios,
    saltos de línea y tabulaciones a un solo espacio. Recorta los extremos.
    None o cadena vacía devuelve "".

    No borra símbolos de moneda, %, / ni guiones: los usan el parser de fechas
    y el de precios. No corrige ortografía.
    """
    if not text or not isinstance(text, str):
        return ""
    t = unicodedata.normalize("NFKD", text.replace("’", "'"))
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", t).strip()


_URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
_URL_TRAILING_PUNCT = ".,;:!?)»”'\"`]>}"


def extract_urls(text: str) -> list[str]:
    """
    Devuelve cada URL http:// o https:// en el orden en que aparece, sin duplicados.
    Quita la puntuación final pegada (https://a.co/x., https://a.co/x)). No sigue
    redirecciones. Un acortador (amzn.to, bit.ly, wa.me) se deja tal cual;
    resolverlo es de quien llama, y solo si el dominio está en una lista permitida
    de config. Si no hay URLs, devuelve [].
    """
    if not text or not isinstance(text, str):
        return []

    seen: set[str] = set()
    urls: list[str] = []
    for match in _URL_RE.finditer(text):
        raw = match.group(0)
        clean = raw.rstrip(_URL_TRAILING_PUNCT)
        if clean and clean not in seen:
            seen.add(clean)
            urls.append(clean)
    return urls


def _parse_number(raw: str) -> Optional[float]:
    """
    Parsea cadenas numéricas con separadores de miles y decimales flexibles
    ('1,299.50', '1.299,50', '1299', '12,5') a float.
    None si es ambiguo o inválido.
    """
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
        val = float(s)
    except ValueError:
        return None
    return val if val >= 0 else None


_NUM = r"(?<![a-zA-Z\d])(?:\d{1,3}(?:[.,\u00a0\u202f]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)(?!\d)(?![a-zA-Z])"
_PRE = r"(?:\bmx\$|\bus\$|\busd\b(?:\s*\$)?|\bmxn\b(?:\s*\$)?|\$)"
_POST = r"(?:pesos?|mxn|usd|d[oó]lares?)"
_PRICE_RE = re.compile(
    rf"(?i)(?:(?P<pre>{_PRE})\s*(?P<n1>{_NUM})(?:\s*(?P<post1>{_POST})\b)?|(?P<n2>{_NUM})\s*(?P<post2>{_POST})\b)(?!\s*(?:%|por\s+ciento\b))"
)


def extract_money(text: str) -> list[float]:
    """
    Saca montos, no códigos ni porcentajes.

    Acepta $1,299.00, $1.299,00, 1299 pesos, 1299 mxn, MXN 1299.
    Unifica a float en unidades mayores (1299.0), no en centavos.
    Ignora un número seguido de % (65% no es dinero).
    Ignora códigos (SAVE20) y teléfonos.
    Conserva el orden del texto. Si hay dos montos, el primero suele ser el “antes”
    y el segundo el “ahora”; esta función no decide eso: solo devuelve la lista.
    La clasificación la hace coupon_analyzer.parse_prices.
    Sin montos, devuelve []. Un monto ilegible ($abc) se omite, no lanza error.
    """
    if not text or not isinstance(text, str):
        return []

    res: list[float] = []
    for match in _PRICE_RE.finditer(text):
        raw = match.group("n1") or match.group("n2")
        val = _parse_number(raw)
        if val is not None:
            res.append(val)
    return res


_PCT_RE = re.compile(r"(?i)(?<![\w.,])(\d{1,3}(?:[.,]\d{1,2})?)\s*(?:%|por\s*ciento\b)")


def extract_percent(text: str) -> list[float]:
    """
    Porcentajes explícitos: 65%, 65 %, 65 por ciento.
    Devuelve números (65.0) en orden, sin duplicar el mismo valor seguido.
    No calcula el descuento a partir de dos precios.
    """
    if not text or not isinstance(text, str):
        return []

    res: list[float] = []
    for match in _PCT_RE.finditer(text):
        val = _parse_number(match.group(1))
        if val is not None:
            if not res or res[-1] != val:
                res.append(val)
    return res
