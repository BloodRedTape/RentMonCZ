"""Cache regression tests (no network or API keys)."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rentmont.config import Settings
from rentmont.extraction import extraction_fingerprint
from rentmont.models import Listing, RentalDetails, StoredAd
from rentmont.service import scan
from rentmont.storage import JsonStore


def detail():
    return {"hash_id": 123, "advert_name": "Apartment", "advert_description": "Rent 12000 CZK",
            "category_main_cb": {"value": 1}, "category_type_cb": {"value": 2},
            "category_sub_cb": {"name": "1+kk"}, "price_czk": 12000,
            "locality": {"district_id": 72, "city_seo_name": "brno"}}


def result(rent=12000, room=False):
    return RentalDetails(rent_kc=rent, utilities_kc=None, comission_kc=None,
                         deposit_kc=None, is_room_rent=room, area_m2=None, available_from=None)


def config(path):
    return Settings(openai_api_key="test", telegram_bot_token="test", telegram_chat_id="1",
                    data_dir=path, notify_existing=True)


class CacheTests(unittest.TestCase):
    def test_rejected_successes_are_cached_across_restart(self):
        for parsed in (result(rent=None), result(rent=15000), result(room=True)):
            with self.subTest(parsed=parsed), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                store = JsonStore(path / "db.json")
                with patch("rentmont.service.search", return_value=[detail()]), \
                     patch("rentmont.service.api_json", return_value={"result": detail()}), \
                     patch("rentmont.service.extract", return_value=parsed) as extract, \
                     patch("rentmont.service.time.sleep"):
                    self.assertTrue(scan(None, None, store, config(path)))
                    store = JsonStore(store.path)
                    self.assertTrue(scan(None, None, store, config(path)))
                self.assertEqual(extract.call_count, 1)
                self.assertEqual(store.get_extraction("123").result, parsed)
                self.assertEqual(store.pending(), [])
                self.assertEqual(json.loads((path / "listings.json").read_text()), [])

    def test_changed_input_can_pass_previously_failed_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            store = JsonStore(path / "db.json")
            revised = detail() | {"advert_description": "Monthly base rent is 12000 CZK"}
            with patch("rentmont.service.search", return_value=[detail()]), \
                 patch("rentmont.service.api_json", side_effect=[{"result": detail()}, {"result": revised}]), \
                 patch("rentmont.service.extract", side_effect=[result(rent=None), result()]) as extract, \
                 patch("rentmont.service.time.sleep"):
                scan(None, None, store, config(path))
                scan(None, None, store, config(path))
            self.assertEqual(extract.call_count, 2)
            self.assertEqual(len(store.pending()), 1)
            self.assertEqual(store.get_ad("123").listing.rent_kc, 12000)

    def test_metadata_changes_do_not_call_llm_or_resend(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            store = JsonStore(path / "db.json")
            revised = detail() | {"edited": "2026-10-15", "advert_images": [{"url": "https://example.com/photo.jpg"}]}
            with patch("rentmont.service.search", return_value=[detail()]), \
                 patch("rentmont.service.api_json", side_effect=[{"result": detail()}, {"result": revised}]), \
                 patch("rentmont.service.extract", return_value=result()) as extract, \
                 patch("rentmont.service.time.sleep"):
                scan(None, None, store, config(path))
                store.mark_notified("123")
                scan(None, None, store, config(path))
            self.assertEqual(extract.call_count, 1)
            saved = store.get_ad("123")
            self.assertEqual(saved.listing.updated_at.isoformat(), "2026-10-15")
            self.assertEqual(len(saved.listing.photos), 1)
            self.assertTrue(saved.notified)
            self.assertEqual(store.pending(), [])

    def test_failures_are_not_cached_and_can_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            store = JsonStore(path / "db.json")
            with patch("rentmont.service.search", return_value=[detail()]), \
                 patch("rentmont.service.api_json", return_value={"result": detail()}), \
                 patch("rentmont.service.extract", side_effect=[RuntimeError("failed"), result()]) as extract, \
                 patch("rentmont.service.time.sleep"):
                self.assertFalse(scan(None, None, store, config(path)))
                self.assertIsNone(store.get_extraction("123"))
                self.assertTrue(scan(None, None, store, config(path)))
            self.assertEqual(extract.call_count, 2)

    def test_legacy_ads_seed_cache_without_llm(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            store = JsonStore(path / "db.json")
            listing = Listing(**result().model_dump(), link="https://www.sreality.cz/")
            store.add("123", StoredAd(title="Apartment", listing=listing, notified=True))
            with patch("rentmont.service.search", return_value=[detail()]), \
                 patch("rentmont.service.api_json", return_value={"result": detail()}), \
                 patch("rentmont.service.extract") as extract, patch("rentmont.service.time.sleep"):
                scan(None, None, store, config(path))
            extract.assert_not_called()
            self.assertEqual(store.get_extraction("123").result, result())
            self.assertEqual(store.pending(), [])

    def test_fingerprint_covers_only_llm_input(self):
        original = extraction_fingerprint(detail())
        self.assertEqual(original, extraction_fingerprint(detail() | {"edited": "2026-10-15"}))
        self.assertEqual(original, extraction_fingerprint(detail() | {"advert_images": ["photo"]}))
        self.assertNotEqual(original, extraction_fingerprint(detail() | {"advert_description": "Changed"}))
        self.assertNotEqual(original, extraction_fingerprint(detail() | {"price_note": "Plus utilities"}))


if __name__ == "__main__":
    unittest.main()
