"""Texto que cuenta como oferta. El monitor compara contra texto ya normalizado."""

offer_keywords = [
    "oferta",
    "descuento",
    "cupon",
    "promo",
    "promocion",
    "rebaja",
    "% off",
    "por ciento",
    "codigo",
]

code_hints = [
    "codigo",
    "cupon",
    "usa",
    "promo",
    "aplica",
]

# Si el texto es solo esto, sin precio ni enlace de tienda, se descarta.
ignore_phrases = [
    "unete al grupo",
    "comparte y etiqueta",
    "link del grupo",
]

# Mayúsculas de 4 a 20 caracteres se tratan como código solo si no están aquí.
code_blocklist = [
    "HOY",
    "LINK",
    "GRUPOS",
    "OFERTA",
    "CUPON",
    "PRECIO",
]
