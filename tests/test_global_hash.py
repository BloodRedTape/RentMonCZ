"""Photos and photo limits do not invalidate existing Telegram messages."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from rentmont.extraction import global_hash, llm_hash
from rentmont.service import deliver, scan
from rentmont.sreality import listing_photos
from rentmont.storage import JsonStore
from tests.helpers import api_ad, config, delivery, rental


class GlobalHashTests(unittest.TestCase):
    def test_photo_changes_are_ignored(self):
        photos = [f"https://example.com/{i}.jpg" for i in range(5)]
        detail = api_ad() | {"advert_images": photos}
        for index in range(3):
            changed = photos.copy()
            changed[index] = "https://example.com/changed.jpg"
            self.assertEqual(global_hash(detail), global_hash(detail | {"advert_images": changed}))
        self.assertEqual(global_hash(detail), global_hash(detail | {"advert_images": photos[:2]}))
        self.assertEqual(global_hash(detail), global_hash(detail | {"advert_images": photos[::-1]}))
        self.assertEqual(global_hash(detail), global_hash(detail | {"advert_images": photos[:3]}))
        changed = detail | {"advert_images": ["https://example.com/other.jpg"]}
        self.assertEqual(llm_hash(detail), llm_hash(changed))
        self.assertEqual(global_hash(detail), global_hash(changed))
        self.assertEqual(global_hash(detail), global_hash(api_ad()))

    def test_same_normalized_selection_has_same_hash(self):
        url = "https://example.com/photo.jpg"
        first = api_ad() | {"advert_images": [url]}
        second = api_ad() | {"advert_images": [None, "bad", {"url": url}, url]}
        self.assertEqual(global_hash(first), global_hash(second))
        self.assertEqual(listing_photos(second), listing_photos(first))

    def test_photo_limit_validation(self):
        settings = config(Path("data"))
        for value in (0, 1, 3, 10):
            type(settings).model_validate(settings.model_dump() | {"telegram_max_photos": value})
        for value in (-1, 11):
            with self.assertRaises(ValidationError):
                type(settings).model_validate(settings.model_dump() | {"telegram_max_photos": value})

    @patch("rentmont.service.time.sleep")
    def test_photo_and_limit_change_do_not_trigger_edits_or_llm(self, sleep):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = JsonStore(root)
            settings = config(root)
            detail = api_ad() | {"advert_images": [f"https://example.com/{i}.jpg" for i in range(6)]}
            with patch("rentmont.service.search", return_value=[detail]), \
                 patch("rentmont.service.api_json", return_value={"result": detail}), \
                 patch("rentmont.service.extract", return_value=rental()) as extract:
                stats = scan(None, None, store, settings)
                with patch("rentmont.service.send_ad", return_value=delivery()):
                    deliver(None, store, settings, stats.listings)
                before = store.get_extraction("123")
                settings = settings.model_copy(update={"telegram_max_photos": 5})
                detail["advert_images"] = [f"https://example.com/new-{i}.jpg" for i in range(6)]
                stats = scan(None, None, store, settings)
                after = store.get_extraction("123")
                self.assertEqual(extract.call_count, 1)
                self.assertEqual(len(stats.listings[0].source.photos), 5)
                self.assertEqual(before.global_hash, after.global_hash)
                self.assertEqual(before.llm_hash, after.llm_hash)
                self.assertEqual(before.extracted_at, after.extracted_at)
                with patch("rentmont.service.edit_ad", return_value=delivery().response) as edit:
                    self.assertTrue(deliver(None, store, settings, stats.listings))
                    self.assertTrue(deliver(None, store, settings, stats.listings))
                edit.assert_not_called()
                self.assertEqual(store.get_notification("123").global_hash, global_hash(detail))

    def test_obsolete_content_hash_is_discarded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = JsonStore(root)
            store.save_extraction("123", rental(), "new", llm_hash(api_ad()))
            store.mark_sent("123", delivery(), "new", llm_hash(api_ad()))
            for filename, reader in (("extraction.json", store.get_extraction),
                                     ("notification.json", store.get_notification)):
                path = root / "123" / filename
                old = json.loads(path.read_text())
                old["content_hash"] = old.pop("global_hash")
                path.write_text(json.dumps(old))
                record = reader("123")
                self.assertIsNone(record.global_hash)
                self.assertEqual(record.llm_hash, llm_hash(api_ad()))
                self.assertNotIn("content_hash", record.model_dump())
