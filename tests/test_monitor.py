import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from pydantic import ValidationError

from rentmont.config import Settings
from rentmont.models import Listing, Payments, StoredAd
from rentmont.service import scan
from rentmont.sreality import matches, search
from rentmont.storage import JsonStore, StorageError
from rentmont.telegram import send_pending


def ad(ident=123, price=12000, district=72):
    return {"hash_id": ident, "price_czk": price,
            "category_main_cb": {"value": 1}, "category_type_cb": {"value": 2},
            "category_sub_cb": {"name": "1+kk"}, "advert_name": "Test apartment",
            "locality": {"district_id": district, "city_seo_name": "brno"}}


def payments():
    return Payments(rent_kc=12000, utilities_kc=2000, comission_kc=None, deposit_kc=24000)


def stored_ad(notified=False):
    return StoredAd(title="Квартира", notified=notified,
                    listing=Listing(**payments().model_dump(), link="https://www.sreality.cz/"))


def settings(directory, **overrides):
    return Settings(openai_api_key="test-key", telegram_bot_token="test-token",
                    telegram_chat_id="1", data_dir=directory, **overrides)


class MonitorTests(unittest.TestCase):
    def test_strict_filters(self):
        self.assertTrue(matches(ad(), 72, 13500))
        for listing in (ad(price=13500), ad(price=0), ad(price=None), ad(district=73)):
            self.assertFalse(matches(listing, 72, 13500))

    def test_payment_validation(self):
        unknown = Payments(rent_kc=12000, utilities_kc=None, comission_kc=None, deposit_kc=None)
        self.assertIsNone(unknown.utilities_kc)
        for value in (-1, math.inf, math.nan):
            with self.assertRaises(ValidationError):
                Payments(rent_kc=value, utilities_kc=None, comission_kc=None, deposit_kc=None)

    def test_config_validation(self):
        for overrides in ({"poll_seconds": 0}, {"notify_existing": "maybe"}, {"district_id": -1}):
            with self.assertRaises(ValidationError):
                settings(Path("data"), **overrides)
        with self.assertRaises(ValidationError):
            Settings(openai_api_key="", telegram_bot_token="token", telegram_chat_id="1")

    def test_pagination(self):
        offsets = []
        def handler(request):
            offset = int(request.url.params["offset"])
            offsets.append(offset)
            return httpx.Response(200, json={"results": [ad(offset + 1)], "pagination": {"total": 2}})
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            self.assertEqual(len(search(client, 72, 13500)), 2)
        self.assertEqual(offsets, [0, 1])

    def test_baseline_new_ads_and_deduplication(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            store = JsonStore(path / "db.json")
            config = settings(path)
            with patch("rentmont.service.search", return_value=[ad()]), \
                 patch("rentmont.service.api_json", return_value={"result": ad()}), \
                 patch("rentmont.service.extract", return_value=payments()) as extract, \
                 patch("rentmont.service.time.sleep"):
                self.assertTrue(scan(None, None, store, config))
                # Restart: ID and initialization are loaded from JSON.
                store = JsonStore(path / "db.json")
                scan(None, None, store, config)
                self.assertEqual(extract.call_count, 1)
            self.assertEqual(store.pending(), [])
            with patch("rentmont.service.search", return_value=[ad(456)]), \
                 patch("rentmont.service.api_json", return_value={"result": ad(456)}), \
                 patch("rentmont.service.extract", return_value=payments()), \
                 patch("rentmont.service.time.sleep"):
                scan(None, None, store, config)
            self.assertEqual([ident for ident, _ in store.pending()], ["456"])
            data = json.loads((path / "listings.json").read_text(encoding="utf-8"))
            self.assertEqual(len(data), 2)
            self.assertEqual(set(data[0]), {"rent_kc", "utilities_kc", "comission_kc", "deposit_kc", "link"})

    def test_notify_existing_and_extraction_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            store = JsonStore(path / "db.json")
            with patch("rentmont.service.search", return_value=[ad()]), \
                 patch("rentmont.service.api_json", return_value={"result": ad()}), \
                 patch("rentmont.service.extract", side_effect=RuntimeError("failed")), \
                 patch("rentmont.service.time.sleep"):
                self.assertFalse(scan(None, None, store, settings(path)))
            self.assertFalse(JsonStore(store.path).initialized)
            with patch("rentmont.service.search", return_value=[ad()]), \
                 patch("rentmont.service.api_json", return_value={"result": ad()}), \
                 patch("rentmont.service.extract", return_value=payments()), \
                 patch("rentmont.service.time.sleep"):
                scan(None, None, store, settings(path, notify_existing=True))
            self.assertEqual(len(store.pending()), 1)

    def test_telegram_failure_is_retried_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStore(Path(directory) / "db.json")
            store.add("1", stored_ad())
            with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429))) as client:
                self.assertFalse(send_pending(client, store, "secret", "1"))
            store = JsonStore(store.path)
            self.assertEqual(len(store.pending()), 1)
            with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}))) as client, patch("rentmont.telegram.time.sleep"):
                self.assertTrue(send_pending(client, store, "secret", "1"))
            self.assertEqual(JsonStore(store.path).pending(), [])


class StorageTests(unittest.TestCase):
    def test_pydantic_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "db.json"
            store = JsonStore(path)
            store.add("123", stored_ad())
            store.mark_initialized()
            reopened = JsonStore(path)
            self.assertTrue(reopened.initialized)
            self.assertEqual(reopened.pending(), [("123", stored_ad())])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["schema_version"], 1)

    def test_corrupt_state_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "db.json"
            for content in ('{"bad":', '{}', '{"initialized":false,"ads":{},"schema_version":2}'):
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(ValidationError):
                    JsonStore(path)
                self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_failed_replace_preserves_disk_and_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "db.json"
            store = JsonStore(path)
            before = path.read_bytes()
            with patch("rentmont.storage.Path.replace", side_effect=OSError("disk failure")):
                with self.assertRaises(StorageError):
                    store.add("1", stored_ad())
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(store.contains("1"))
            self.assertEqual(list(path.parent.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
