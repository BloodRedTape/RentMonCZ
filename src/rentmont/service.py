"""One monitoring cycle, independent of CLI scheduling."""
import logging
import time

import httpx
from openai import OpenAI

from .config import Settings
from .extraction import extract
from .models import Listing, StoredAd
from .sreality import api_json, listing_link, matches, search
from .storage import JsonStore, StorageError

LOG = logging.getLogger(__name__)


def scan(client: httpx.Client, ai: OpenAI, store: JsonStore, settings: Settings) -> bool:
    initial = not store.initialized
    ads = search(client, settings.district_id, settings.max_rent_kc)
    failed = False
    for ad in ads:
        ident = str(ad["hash_id"])
        if store.contains(ident):
            continue
        try:
            detail = api_json(client, f"estates/{ident}")["result"]
            if not matches(detail, settings.district_id, settings.max_rent_kc):
                continue
            payments = extract(ai, settings.openai_model, detail)
            if payments.rent_kc is None or not 0 < payments.rent_kc < settings.max_rent_kc:
                continue
            listing = Listing(**payments.model_dump(), link=listing_link(detail))
            store.add(ident, StoredAd(title=detail["advert_name"], listing=listing,
                                     notified=initial and not settings.notify_existing))
            LOG.info("Saved listing %s", ident)
        except StorageError:
            # A persistence failure is fatal, not a listing extraction error.
            raise
        except Exception as exc:
            failed = True
            LOG.warning("Listing %s failed (%s); will retry", ident, type(exc).__name__)
        time.sleep(0.5)
    if not failed:
        store.mark_initialized()
    store.export_listings(settings.data_dir / "listings.json")
    LOG.info("Scan complete: %d matching search results", len(ads))
    return not failed
