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
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
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
                delivered = stats is not None and deliver(client, store, settings, stats.listings)
                if args.once:
                    if stats is None or not stats.succeeded or not delivered:
                        raise SystemExit(1)
                    break
                time.sleep(max(0, settings.poll_seconds - (time.monotonic() - start)))
    except StorageError as exc:
        LOG.error("Storage failure: %s; stopping", exc)
        raise SystemExit(1) from None
    except OSError as exc:
        LOG.error("IO failure (%s); stopping", type(exc).__name__)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        LOG.info("Stopped")
