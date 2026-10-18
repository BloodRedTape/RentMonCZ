"""Discover listings, reuse local source data and evaluate current policy."""
import logging
import time

import httpx
from openai import OpenAI

from .config import Settings
from .extraction import extract
from .filters import rental_rejection, source_rejection
from .models import Listing, ScanStats
from .sreality import api_json, listing_gone, search, source_listing
from .storage import JsonStore, StorageError
from .telegram import TelegramRejected, mark_ad_gone, safe_description, send_ad

LOG = logging.getLogger(__name__)


def deliver(client: httpx.Client, store: JsonStore, settings: Settings,
            listings: tuple[Listing, ...]) -> bool:
    """Deliver this scan's eligible listings, remembering only successful sends."""
    token = settings.telegram_bot_token.get_secret_value()
    updated = update_gone(client, store, settings)
    for listing in listings:
        ident = listing.source.id
        if store.get_notification(ident) is not None:
            continue
        try:
            delivery = send_ad(client, token, settings.telegram_chat_id, listing)
        except Exception as exc:
            if isinstance(exc, TelegramRejected):
                LOG.warning("Telegram rejected listing %s (code %s): %s; will retry",
                            ident, exc.code, safe_description(exc.description, token))
            else:
                LOG.warning("Telegram delivery failed for listing %s (%s); will retry",
                            ident, type(exc).__name__)
            return False
        store.mark_sent(ident, delivery)
        time.sleep(1.1)
    return updated


def update_gone(client: httpx.Client, store: JsonStore, settings: Settings) -> bool:
    """Confirm missing ads and retry pending edits without changing detection dates."""
    token = settings.telegram_bot_token.get_secret_value()
    succeeded = True
    for ident, record in store.sent_notifications():
        if record.gone_notified or record.chat_id is None:
            continue
        try:
            if record.gone_at is None:
                if ident in store.current_search_ids:
                    continue
                try:
                    gone = listing_gone(client, ident)
                finally:
                    time.sleep(0.5)
                if not gone:
                    continue
                record = store.mark_gone(ident)
            mark_ad_gone(client, token, record)
            store.mark_gone(ident, notified=True)
            time.sleep(1.1)
        except StorageError:
            raise
        except Exception as exc:
            succeeded = False
            if isinstance(exc, TelegramRejected):
                LOG.warning("Gone update for %s rejected: %s; will retry", ident,
                            safe_description(exc.description, token))
            else:
                LOG.warning("Gone check/update for %s failed (%s); will retry",
                            ident, type(exc).__name__)
    return succeeded


def scan(client: httpx.Client, ai: OpenAI, store: JsonStore, settings: Settings,
         *, reextract: bool = False) -> ScanStats:
    initial = not store.initialized
    ads = search(client, settings.district_id, settings.max_rent_kc)
    store.set_current_search({str(ad["hash_id"]) for ad in ads})
    counts = {"llm_calls": 0, "cache_hits": 0, "excluded": 0, "errors": 0}
    listings = []
    for ad in ads:
        ident = str(ad["hash_id"])
        requested = False
        try:
            saved_source = store.get_source(ident)
            if saved_source is None or reextract:
                requested = True
                detail = api_json(client, f"estates/{ident}")["result"]
                if str(detail["hash_id"]) != ident:
                    raise ValueError("Sreality detail ID does not match the requested listing")
                # Keep raw data even when subsequent extraction/filtering fails.
                store.save_source(detail)
            else:
                detail = saved_source.raw
            source = source_listing(detail)
            if source_rejection(source, settings.district_id, settings.max_rent_kc) is not None:
                counts["excluded"] += 1
                continue
            cached = store.get_extraction(ident)
            if cached is not None and not reextract:
                rental = cached.details
                counts["cache_hits"] += 1
            else:
                counts["llm_calls"] += 1
                rental = extract(ai, settings.openai_model, detail)
                store.save_extraction(ident, rental)
            if rental_rejection(rental, settings.max_rent_kc,
                                settings.max_rent_and_utilities_kc) is not None:
                counts["excluded"] += 1
                continue
            listings.append(Listing(source=source, rental=rental))
            if initial and not settings.notify_existing:
                store.suppress(ident)
        except StorageError:
            raise
        except Exception as exc:
            # Keep previous facts, but don't export/deliver a failed check.
            counts["errors"] += 1
            LOG.warning("Listing %s failed (%s); will retry", ident, type(exc).__name__)
        finally:
            if requested:
                time.sleep(0.5)
    listings.sort(key=lambda listing: listing.source.id)
    stats = ScanStats(search_results=len(ads), listings=tuple(listings), **counts)
    store.export_listings(settings.data_dir / "listings.json", listings)
    if stats.succeeded:
        store.mark_initialized()
    LOG.info("Scan complete: %d results, %d LLM calls, %d cache hits, %d excluded, %d errors",
             stats.search_results, stats.llm_calls, stats.cache_hits, stats.excluded, stats.errors)
    return stats
