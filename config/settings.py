"""Números, umbrales e intervalos. Los secretos están en .env, no aquí."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_list(name: str) -> list[str]:
    raw = _env(name)
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass
class Settings:
    timezone: str = "America/Mexico_City"
    country: str = "MX"

    cycle_minutes: int = 20
    cleanup_hours: int = 6
    expiry_notice_hours: int = 6
    expiry_notice_days: int = 2
    purge_older_days: int = 30
    gui: bool = True

    post_limit: int = 50
    max_post_age_hours: int = 24
    group_delay_min_seconds: int = 20
    group_delay_max_seconds: int = 90

    # También puedes definirlos en .env como listas separadas por coma.
    facebook_group_ids: list[str] = field(default_factory=list)
    whatsapp_channel_ids: list[str] = field(default_factory=list)

    special_discount_percent: float = 65.0
    max_plausible_discount: float = 95.0
    special_categories: list[str] = field(
        default_factory=lambda: ["laptop", "pc", "celular", "tablet", "consola", "audifonos"]
    )

    min_rating: float = 4.0
    min_reviews: int = 50
    reject_suspicious_reviews: bool = False
    save_rejected: bool = False

    database_path: str = str(ROOT / "data" / "coupons.db")
    log_path: str = str(ROOT / "logs" / "bot.log")
    accounts_path: str = str(ROOT / "config" / "accounts.yaml")
    events_path: str = str(ROOT / "config" / "events.yaml")

    notify_channel: str = "telegram"  # telegram | discord | webhook
    admin_channel: str = "telegram"


def load_settings() -> Settings:
    settings = Settings()
    groups = _env_list("FACEBOOK_GROUP_IDS")
    channels = _env_list("WHATSAPP_CHANNEL_IDS")
    if groups:
        settings.facebook_group_ids = groups
    if channels:
        settings.whatsapp_channel_ids = channels
    gui = _env("GUI")
    if gui:
        settings.gui = gui.lower() in {"1", "true", "yes", "si"}
    channel = _env("NOTIFY_CHANNEL")
    if channel:
        settings.notify_channel = channel
    admin = _env("ADMIN_CHANNEL")
    if admin:
        settings.admin_channel = admin
    return settings
