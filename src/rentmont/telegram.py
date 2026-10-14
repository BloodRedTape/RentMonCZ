"""Telegram delivery with a persistent retry queue."""
import logging
import time

import httpx

from .models import StoredAd
from .storage import JsonStore

LOG = logging.getLogger(__name__)
LABELS = {"rent_kc": "Аренда / мес.", "utilities_kc": "Коммунальные / мес.",
          "comission_kc": "Комиссия", "deposit_kc": "Залог"}


def format_message(ad: StoredAd) -> str:
    area = ad.listing.area_m2
    lines = [ad.title[:250],
             "Тип: " + ("комната" if ad.listing.is_room_rent else "квартира целиком"),
             "Площадь: " + ("не указано" if area is None else f"{area:g} м²")]
    for key, label in LABELS.items():
        value = getattr(ad.listing, key)
        lines.append(f"{label}: " + ("не указано" if value is None else f"{value:,.0f} Kč"))
    availability = ad.listing.available_from
    if availability is None:
        move_in = "не указано"
    elif availability == "asap":
        move_in = "как можно скорее"
    else:
        move_in = availability.strftime("%d.%m.%Y")
    lines.append(f"Заселение: {move_in}")
    distance = ad.listing.distance_to_center_km
    lines.append("До центра: " + ("не указано" if distance is None
                                    else f"≈{distance:.1f} км по прямой"))
    lines.append(str(ad.listing.link))
    return "\n".join(lines)


def send_pending(client: httpx.Client, store: JsonStore, token: str, chat: str) -> bool:
    for ident, ad in store.pending():
        try:
            response = client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": format_message(ad),
                      "link_preview_options": {"is_disabled": True}},
            )
            response.raise_for_status()
            if not response.json().get("ok"):
                raise RuntimeError("Telegram rejected message")
        except Exception as exc:
            # HTTP exception strings include the secret bot token in the URL.
            LOG.warning("Telegram delivery failed (%s); queue retained", type(exc).__name__)
            return False
        store.mark_notified(ident)
        time.sleep(1.1)
    return True
