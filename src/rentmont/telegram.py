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
    lines = [ad.title[:250]]
    for key, label in LABELS.items():
        value = getattr(ad.listing, key)
        lines.append(f"{label}: " + ("не указано" if value is None else f"{value:,.0f} Kč"))
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
