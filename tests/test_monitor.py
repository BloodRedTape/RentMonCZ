from datetime import date
import json
import math
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
from pydantic import ValidationError

from rentmont.config import Settings
from rentmont.extraction import extract
from rentmont.models import Listing, Payments, RentalDetails, StoredAd
from rentmont.service import scan
from rentmont.sreality import matches, search
from rentmont.storage import JsonStore, StorageError
from rentmont.telegram import format_message, send_pending


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

    def test_zero_payments_are_null(self):
        for field in Payments.model_fields:
            for value in (0, 0.0, "0.0", None, 15000):
                with self.subTest(field=field, value=value):
                    expected = 15000 if value == 15000 else None
                    parsed = Payments(**(payments().model_dump() | {field: value}))
                    self.assertEqual(getattr(parsed, field), expected)
                    self.assertEqual(json.loads(parsed.model_dump_json())[field], expected)
            with self.assertRaises(ValidationError):
                Payments(**(payments().model_dump() | {field: -1}))

    def test_existing_zero_payments_are_normalized(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "db.json"
            record = stored_ad().model_dump(mode="json")
            for field in Payments.model_fields:
                record["listing"][field] = 0.0
            path.write_text(json.dumps({"initialized": True, "ads": {"1": record}}), encoding="utf-8")
            store = JsonStore(path)
            for field in Payments.model_fields:
                self.assertIsNone(getattr(store.pending()[0][1].listing, field))
            store.mark_initialized()
            store.export_listings(path.parent / "listings.json")
            saved = json.loads(path.read_text(encoding="utf-8"))
            exported = json.loads((path.parent / "listings.json").read_text(encoding="utf-8"))
            for field in Payments.model_fields:
                self.assertIsNone(saved["ads"]["1"]["listing"][field])
                self.assertIsNone(exported[0][field])

    def test_rental_details_and_telegram(self):
        details = RentalDetails(**payments().model_dump(), is_room_rent=True, area_m2=23.5, available_from=None)
        listing = Listing(**details.model_dump(), link="https://www.sreality.cz/")
        message = format_message(StoredAd(title="Room", listing=listing, notified=False))
        self.assertIn("Тип: комната", message)
        self.assertIn("23.5 м²", message)
        for area in (0, -1, math.inf):
            with self.assertRaises(ValidationError):
                RentalDetails(**payments().model_dump(), is_room_rent=False, area_m2=area, available_from=None)

    def test_room_category_and_extraction_payload(self):
        ai = MagicMock()
        parsed = RentalDetails(**payments().model_dump(), is_room_rent=False, area_m2=80, available_from=None)
        ai.chat.completions.parse.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))])
        detail = ad() | {"category_sub_cb": {"name": "Pokoj"}, "usable_area": 80,
                         "advert_description": "Pronajem pokoje", "user_email": "private",
                         "ready_date": "2026-12-01"}
        result = extract(ai, "model", detail)
        self.assertTrue(result.is_room_rent)
        self.assertEqual(result.area_m2, 80)
        args = ai.chat.completions.parse.call_args.kwargs
        self.assertIs(args["response_format"], RentalDetails)
        payload = json.loads(args["messages"][1]["content"])
        self.assertEqual(payload["usable_area"], 80)
        self.assertEqual(payload["ready_date"], "2026-12-01")
        self.assertNotIn("user_email", payload)

    def test_legacy_listing_defaults(self):
        listing = Listing.model_validate(payments().model_dump() | {"link": "https://www.sreality.cz/"})
        self.assertFalse(listing.is_room_rent)
        self.assertIsNone(listing.area_m2)
        self.assertIsNone(listing.available_from)

    def test_availability_variants_and_telegram(self):
        for value, expected, text in (
            (None, None, "не указано"),
            ("asap", "asap", "как можно скорее"),
            ("2026-12-01", "2026-12-01", "01.12.2026"),
            (date(2026, 12, 1), "2026-12-01", "01.12.2026"),
        ):
            with self.subTest(value=value):
                details = RentalDetails(**payments().model_dump(), is_room_rent=False,
                                        area_m2=None, available_from=value)
                self.assertEqual(json.loads(details.model_dump_json())["available_from"], expected)
                listing = Listing(**details.model_dump(), link="https://www.sreality.cz/")
                message = format_message(StoredAd(title="Flat", listing=listing, notified=False))
                self.assertIn(f"Заселение: {text}", message)

    def test_invalid_availability(self):
        for value in ("soon", "2026-02-30", "01.12.2026", "2026-12", "20261201", 0, True):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                RentalDetails(**payments().model_dump(), is_room_rent=False,
                              area_m2=None, available_from=value)

    def test_availability_storage_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStore(Path(directory) / "db.json")
            for ident, value in enumerate((None, "asap", "2026-12-01")):
                listing = Listing(**payments().model_dump(), available_from=value,
                                  link="https://www.sreality.cz/")
                store.add(str(ident), StoredAd(title="Flat", listing=listing, notified=False))
            reopened = JsonStore(store.path)
            reopened.export_listings(Path(directory) / "listings.json")
            data = json.loads((Path(directory) / "listings.json").read_text(encoding="utf-8"))
            self.assertEqual([item["available_from"] for item in data], [None, "asap", "2026-12-01"])

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
            self.assertEqual(set(data[0]), {"rent_kc", "utilities_kc", "comission_kc", "deposit_kc", "link", "is_room_rent", "area_m2", "available_from"})

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
