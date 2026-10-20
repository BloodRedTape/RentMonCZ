"""Discover listings, reuse local source data and evaluate current policy."""
import logging
import time

import httpx
from openai import OpenAI

from .config import Settings
from .extraction import extract, global_hash, llm_hash
from .filters import rental_rejection, source_rejection
from .models import Listing, ScanStats
from .sreality import api_json, listing_gone, search, source_listing
from .storage import JsonStore, StorageError
from .telegram import (NOTIFICATION_FORMAT_VERSION, TelegramRejected, edit_ad,
                       mark_ad_gone, safe_description, send_ad)

LOG = logging.getLogger(__name__)


def deliver(client: httpx.Client, store: JsonStore, settings: Settings,
            listings: tuple[Listing, ...]) -> bool:
    """Deliver this scan's eligible listings, remembering only successful sends."""
    started = time.monotonic()
    sent = 0
    LOG.debug("Delivery started: %d eligible listings", len(listings))
    token = settings.telegram_bot_token.get_secret_value()
    updated = update_gone(client, store, settings)
    updated = update_changed(client, store, settings) and updated
    for listing in listings:
        ident = listing.source.id
        notification = store.get_notification(ident)
        if notification is not None:
            LOG.debug("Listing %s delivery skipped: %s", ident, notification.status.value)
            continue
        try:
            LOG.info("Sending new notification for listing %s", ident)
            delivery = send_ad(client, token, settings.telegram_chat_id, listing)
        except Exception as exc:
            if isinstance(exc, TelegramRejected):
                LOG.warning("Telegram rejected listing %s (code %s): %s; will retry",
                            ident, exc.code, safe_description(exc.description, token))
            else:
                LOG.warning("Telegram delivery failed for listing %s (%s); will retry",
                            ident, type(exc).__name__)
            return False
        cached = store.get_extraction(ident)
        store.mark_sent(ident, delivery, cached.global_hash if cached else None,
                        cached.llm_hash if cached else None,
                        format_version=NOTIFICATION_FORMAT_VERSION)
        sent += 1
        LOG.info("Notification sent for listing %s (method %s, chat %s, messages %s)",
                 ident, delivery.method, delivery.chat_id, delivery.message_ids)
        time.sleep(1.1)
    LOG.info("Delivery complete: %d sent, updates %s, %.1fs elapsed", sent,
             "succeeded" if updated else "pending retries", time.monotonic() - started)
    return updated


def update_changed(client: httpx.Client, store: JsonStore, settings: Settings) -> bool:
    token = settings.telegram_bot_token.get_secret_value()
    succeeded = True
    for ident, record in store.sent_notifications():
        if ident not in store.current_search_ids or record.gone_at or record.chat_id is None:
            continue
        source = store.get_source(ident)
        cached = store.get_extraction(ident)
        # Failed extraction must never pair new metadata with old rental facts.
        if (source is None or cached is None
                or cached.global_hash != global_hash(source.raw)
                or cached.llm_hash != llm_hash(source.raw)):
            LOG.debug("Listing %s edit deferred: no matching source/extraction", ident)
            continue
        format_changed = record.format_version != NOTIFICATION_FORMAT_VERSION
        if (record.global_hash == cached.global_hash and record.llm_hash == cached.llm_hash
                and not format_changed):
            continue
        try:
            if format_changed:
                LOG.info("Updating notification format for listing %s (version %s -> %s)",
                         ident, record.format_version, NOTIFICATION_FORMAT_VERSION)
            else:
                LOG.info("Updating notification for changed listing %s", ident)
            listing = Listing(source=source_listing(source.raw, settings.telegram_max_photos),
                              rental=cached.details)
            response = edit_ad(client, token, record, listing)
            store.mark_updated(ident, cached.global_hash, response, cached.llm_hash,
                               format_version=NOTIFICATION_FORMAT_VERSION)
            LOG.info("Updated changed listing %s (chat %s, message %s)",
                     ident, record.chat_id, record.message_ids[0])
            time.sleep(1.1)
        except StorageError:
            raise
        except Exception as exc:
            succeeded = False
            description = (safe_description(exc.description, token)
                           if isinstance(exc, TelegramRejected) else type(exc).__name__)
            LOG.warning("Listing %s update failed (%s); will retry", ident, description)
    return succeeded


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
                LOG.debug("Listing %s absent from search; checking availability", ident)
                try:
                    gone = listing_gone(client, ident)
                finally:
                    time.sleep(0.5)
                if not gone:
                    LOG.debug("Listing %s still available despite search absence", ident)
                    continue
                record = store.mark_gone(ident)
                LOG.info("Listing %s confirmed gone; detected at %s", ident,
                         record.gone_at.isoformat())
            LOG.info("Updating Gone notification for listing %s (detected %s)",
                     ident, record.gone_at.isoformat())
            mark_ad_gone(client, token, record)
            store.mark_gone(ident, notified=True)
            LOG.info("Gone notification updated for listing %s (chat %s, message %s)",
                     ident, record.chat_id, record.message_ids[0])
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
    started = time.monotonic()
    initial = not store.initialized
    LOG.info("Scan started (initial=%s, reextract=%s)", initial, reextract)
    ads = search(client, settings.district_id, settings.max_rent_kc)
    store.set_current_search({str(ad["hash_id"]) for ad in ads})
    LOG.info("Search complete: %d listings", len(ads))
    counts = {"llm_calls": 0, "cache_hits": 0, "excluded": 0, "errors": 0}
    listings = []
    for ad in ads:
        ident = str(ad["hash_id"])
        requested = False
        try:
            previous = store.get_source(ident)
            requested = True
            LOG.debug("Fetching detail for listing %s", ident)
            detail = api_json(client, f"estates/{ident}")["result"]
            if str(detail["hash_id"]) != ident:
                raise ValueError("Sreality detail ID does not match the requested listing")
            # Keep raw data even when subsequent extraction/filtering fails.
            store.save_source(detail)
            current_hash = global_hash(detail)
            if previous is None:
                LOG.info("New listing discovered: %s", ident)
            elif global_hash(previous.raw) != current_hash:
                LOG.info("Listing %s content changed (global hash %s)", ident, current_hash)
            source = source_listing(detail, settings.telegram_max_photos)
            reason = source_rejection(source, settings.district_id, settings.max_rent_kc)
            if reason is not None:
                LOG.debug("Listing %s excluded by source filter: %s", ident, reason.value)
                counts["excluded"] += 1
                continue
            cached = store.get_extraction(ident)
            input_hash = llm_hash(detail)
            if cached is not None and cached.llm_hash == input_hash and not reextract:
                rental = cached.details
                counts["cache_hits"] += 1
                LOG.debug("Listing %s extraction cache hit", ident)
                if cached.global_hash != current_hash:
                    store.update_extraction_global_hash(ident, current_hash)
                    LOG.info("Listing %s metadata updated; LLM input unchanged, reusing extraction",
                             ident)
            else:
                counts["llm_calls"] += 1
                reason = ("forced" if reextract else "missing" if cached is None
                          else "changed or missing LLM hash")
                LOG.info("Extracting listing %s (reason: %s)", ident, reason)
                rental = extract(ai, settings.openai_model, detail)
                store.save_extraction(ident, rental, current_hash, input_hash)
                LOG.info("Extracted listing %s (global hash %s, LLM hash %s)",
                         ident, current_hash, input_hash)
            reason = rental_rejection(rental, settings.max_rent_kc,
                                      settings.max_rent_and_utilities_kc)
            if reason is not None:
                LOG.debug("Listing %s excluded by rental filter: %s", ident, reason.value)
                counts["excluded"] += 1
                continue
            listings.append(Listing(source=source, rental=rental))
            LOG.debug("Listing %s eligible", ident)
            if initial and not settings.notify_existing:
                if store.get_notification(ident) is None:
                    store.suppress(ident)
                    LOG.info("Initial notification suppressed for listing %s", ident)
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
        if initial:
            LOG.info("Initial scan completed; monitor initialized")
    LOG.info("Scan complete: %d results, %d eligible, %d LLM calls, %d cache hits, "
             "%d excluded, %d errors, %.1fs elapsed",
             stats.search_results, len(stats.listings), stats.llm_calls, stats.cache_hits,
             stats.excluded, stats.errors, time.monotonic() - started)
    return stats
