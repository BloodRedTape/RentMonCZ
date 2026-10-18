"""Command-line entry point and polling loop."""
import argparse
import logging
import time

import httpx
from openai import OpenAI
from pydantic import ValidationError

from .config import Settings
from .service import scan, deliver
from .storage import JsonStore, StorageError

LOG = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sreality apartment rental monitor")
    parser.add_argument("--once", action="store_true", help="Run one scan and exit")
    parser.add_argument("--reextract", action="store_true", help="Reparse current listings once, ignoring saved facts")
    parser.add_argument("--debug", action="store_true", help="Enable detailed application logs")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # HTTP/SDK debug logs may contain credentials, payloads and signed URLs.
    for name in ("httpx", "httpcore", "openai"):
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        settings = Settings.from_env()
    except ValidationError as exc:
        # Do not display validation input values, which may contain credentials.
        fields = ", ".join(".".join(map(str, e["loc"])) for e in exc.errors())
        parser.error(f"Check .env configuration: {fields}")
    try:
        store = JsonStore(settings.data_dir)
    except StorageError as exc:
        parser.error(str(exc))
    except (OSError, ValidationError) as exc:
        parser.error(f"Cannot load/create {settings.data_dir / 'state.json'} ({type(exc).__name__}); file was not reset")
    LOG.info("Monitor starting (once=%s, poll=%ss, district=%s, model=%s, initialized=%s)",
             args.once, settings.poll_seconds, settings.district_id,
             settings.openai_model, store.initialized)
    try:
        with httpx.Client(timeout=30, follow_redirects=True,
                          headers={"User-Agent": "RentMont/1.0"}) as client, OpenAI(
            api_key=settings.openai_api_key.get_secret_value(), timeout=60, max_retries=2
        ) as ai:
            while True:
                start = time.monotonic()
                stats = None
                try:
                    stats = scan(client, ai, store, settings, reextract=args.reextract)
                    args.reextract = False
                except StorageError:
                    raise
                except Exception as exc:
                    LOG.error("Scan failed (%s); retry next cycle", type(exc).__name__)
                # Don't send stale queued listings if the search itself failed.
                if stats is None:
                    LOG.warning("Delivery skipped: scan failed; stale listings will not be sent")
                delivered = stats is not None and deliver(client, store, settings, stats.listings)
                LOG.info("Cycle complete (scan=%s, delivery=%s, elapsed=%.1fs)",
                         "ok" if stats is not None and stats.succeeded else "failed",
                         "ok" if delivered else "failed/skipped", time.monotonic() - start)
                if args.once:
                    if stats is None or not stats.succeeded or not delivered:
                        LOG.error("One-shot run finished with errors")
                        raise SystemExit(1)
                    LOG.info("One-shot run finished successfully")
                    break
                delay = max(0, settings.poll_seconds - (time.monotonic() - start))
                LOG.debug("Next scan in %.1fs", delay)
                time.sleep(delay)
    except StorageError as exc:
        LOG.error("Storage failure: %s; stopping", exc)
        raise SystemExit(1) from None
    except OSError as exc:
        LOG.error("IO failure (%s); stopping", type(exc).__name__)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        LOG.info("Stopped")
