"""Monitoring cycle: cache successful extraction separately from filtering."""
import logging
import time

import httpx
from openai import OpenAI

from .config import Settings
from .extraction import extract, extraction_fingerprint
from .location import distance_to_center_km
from .models import Listing, RentalDetails, StoredAd
from .sreality import api_json, listing_dates, listing_link, listing_photos, matches, search
from .storage import JsonStore, StorageError

LOG = logging.getLogger(__name__)


def scan(client: httpx.Client, ai: OpenAI, store: JsonStore, settings: Settings) -> bool:
    initial = not store.initialized
    ads = search(client, settings.district_id, settings.max_rent_kc)
    failed = False
    llm_calls = cache_hits = 0
    for ad in ads:
        ident = str(ad["hash_id"])
        try:
            # Fetch details even for known IDs to detect meaningful content changes.
            detail = api_json(client, f"estates/{ident}")["result"]
            if not matches(detail, settings.district_id, settings.max_rent_kc):
                store.exclude(ident)
                continue
            category = detail.get("category_sub_cb") or {}
            if str(category.get("name", "")).strip().casefold() in {"pokoj", "pokoje"}:
                store.exclude(ident)
                continue

            fingerprint = extraction_fingerprint(detail)
            cached = store.get_extraction(ident)
            previous = store.get_ad(ident)
            if cached is not None and cached.fingerprint == fingerprint:
                result = cached.result
                cache_hits += 1
            elif cached is None and previous is not None:
                # Upgrade legacy records without paying for the same parsing again.
                # Their original source hash is unknown: today's content becomes
                # the baseline; subsequent changes will trigger re-extraction.
                result = RentalDetails.model_validate(
                    previous.listing.model_dump(include=set(RentalDetails.model_fields)))
                store.cache_extraction(ident, fingerprint, result)
                cache_hits += 1
            else:
                llm_calls += 1
                result = extract(ai, settings.openai_model, detail)
                # Persist EVERY successful result before filtering or enrichment.
                # A later failure won't cause another paid call for the same input.
                store.cache_extraction(ident, fingerprint, result)

            if result.is_room_rent or result.rent_kc is None or not 0 < result.rent_kc < settings.max_rent_kc:
                store.exclude(ident)
                continue

            listing = Listing(**result.model_dump(), **listing_dates(detail),
                              link=listing_link(detail), photos=listing_photos(detail),
                              distance_to_center_km=distance_to_center_km(detail.get("locality")))
            # Refresh metadata silently; don't resend already delivered listings.
            notified = previous.notified if previous is not None else initial and not settings.notify_existing
            store.save_ad(ident, StoredAd(title=detail["advert_name"], listing=listing, notified=notified))
        except StorageError:
            raise
        except Exception as exc:
            failed = True
            LOG.warning("Listing %s failed (%s); will retry", ident, type(exc).__name__)
        finally:
            time.sleep(0.5)
    if not failed:
        store.mark_initialized()
    store.export_listings(settings.data_dir / "listings.json")
    LOG.info("Scan complete: %d search results, %d LLM calls, %d cache hits",
             len(ads), llm_calls, cache_hits)
    return not failed
