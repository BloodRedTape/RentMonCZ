"""Structured furnishing has priority; LLM supplies a fallback when unknown."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rentmont.extraction import extraction_content, global_hash, llm_hash
from pydantic import ValidationError

from rentmont.models import Furnishing, RentalDetails
from rentmont.service import deliver, scan
from rentmont.sreality import listing_furnishing, source_listing
from rentmont.storage import JsonStore
from rentmont.telegram import format_message
from tests.helpers import api_ad, config, delivery, rental, record


class FurnishingTests(unittest.TestCase):
    def test_api_codes_and_format(self):
        for code, expected, label in (
            (1, Furnishing.FURNISHED, "меблированная"),
            (2, Furnishing.UNFURNISHED, "немеблированная"),
            (3, Furnishing.PARTIALLY_FURNISHED, "частично меблированная"),
        ):
            for value in (code, str(code)):
                with self.subTest(value=value):
                    source = source_listing(api_ad() | {"furnished": {"value": value}})
                    self.assertEqual(source.furnishing, expected)
                    listing = record().model_copy(update={"source": source})
                    self.assertIn(f"🛋 Furnishing: {label}\n", format_message(listing))

    def test_unspecified_and_invalid_are_not_unfurnished(self):
        for value in (None, 0, "Ne", [], {}, {"value": 0}, {"value": 99},
                      {"value": True}, {"value": 1.0}, {"name": "Ne"}):
            with self.subTest(value=value):
                self.assertIsNone(listing_furnishing({"furnished": value}))
        self.assertIsNone(listing_furnishing({}))
        self.assertIn("🛋 Furnishing: не указано", format_message(record()))

    def test_llm_fallback_and_api_priority(self):
        for api_code, api_expected in ((0, None), (1, Furnishing.FURNISHED),
                                       (2, Furnishing.UNFURNISHED),
                                       (3, Furnishing.PARTIALLY_FURNISHED)):
            for inferred in (None, *Furnishing):
                with self.subTest(api_code=api_code, inferred=inferred):
                    source = source_listing(api_ad() | {"furnished": {"value": api_code}})
                    facts = rental().model_copy(update={"furnishing": inferred})
                    listing = record().model_copy(update={"source": source, "rental": facts})
                    expected = api_expected or inferred
                    self.assertEqual(listing.furnishing, expected)
                    labels = {None: "не указано", Furnishing.FURNISHED: "меблированная",
                              Furnishing.PARTIALLY_FURNISHED: "частично меблированная",
                              Furnishing.UNFURNISHED: "немеблированная"}
                    self.assertIn(f"🛋 Furnishing: {labels[expected]}\n", format_message(listing))
                    self.assertEqual(listing.source.furnishing, api_expected)
                    self.assertEqual(listing.rental.furnishing, inferred)

    def test_llm_schema_and_old_records(self):
        old = rental().model_dump()
        old.pop("furnishing")
        self.assertIsNone(RentalDetails.model_validate(old).furnishing)
        for value in (None, "furnished", "partially_furnished", "unfurnished"):
            self.assertEqual(RentalDetails.model_validate(old | {"furnishing": value}).furnishing, value)
        for value in ("unknown", "Ano", True, 1):
            with self.assertRaises(ValidationError):
                RentalDetails.model_validate(old | {"furnishing": value})

    @patch("rentmont.service.time.sleep")
    def test_old_extraction_is_refreshed_once_and_message_uses_fallback(self, sleep):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = config(root)
            store = JsonStore(root)
            detail = api_ad()
            old_hash = hashlib.sha256(extraction_content(detail).encode("utf-8")).hexdigest()
            self.assertNotEqual(old_hash, llm_hash(detail))
            store.save_source(detail)
            store.save_extraction("123", rental(), global_hash(detail), old_hash)
            store.mark_sent("123", delivery(), global_hash(detail), old_hash)
            path = root / "123/extraction.json"
            old = json.loads(path.read_text())
            old["details"].pop("furnishing")
            path.write_text(json.dumps(old))
            store = JsonStore(root)
            facts = rental().model_copy(update={"furnishing": Furnishing.FURNISHED})
            with patch("rentmont.service.search", return_value=[detail]), \
                 patch("rentmont.service.api_json", return_value={"result": detail}), \
                 patch("rentmont.service.extract", return_value=facts) as extract:
                stats = scan(None, None, store, settings)
                scan(None, None, store, settings)
            extract.assert_called_once()
            self.assertEqual(stats.listings[0].furnishing, Furnishing.FURNISHED)
            self.assertEqual(store.get_extraction("123").details.furnishing, Furnishing.FURNISHED)
            self.assertIn('"furnishing": "furnished"', (root / "listings.json").read_text(encoding="utf-8"))
            with patch("rentmont.service.edit_ad", return_value=delivery().response) as edit:
                self.assertTrue(deliver(None, store, settings, stats.listings))
                self.assertTrue(deliver(None, store, settings, stats.listings))
            edit.assert_called_once()
            self.assertIn("🛋 Furnishing: меблированная", format_message(edit.call_args.args[-1]))

    def test_separate_hashes(self):
        original = api_ad()
        for value in (1, 2, 3):
            changed = original | {"furnished": {"value": value}}
            self.assertNotEqual(global_hash(original), global_hash(changed))
            self.assertEqual(llm_hash(original), llm_hash(changed))

    @patch("rentmont.service.time.sleep")
    def test_change_edits_notification_without_llm(self, sleep):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = JsonStore(root)
            settings = config(root)
            detail = api_ad() | {"furnished": {"name": "Ano", "value": 1}}
            with patch("rentmont.service.search", return_value=[detail]), \
                 patch("rentmont.service.api_json", return_value={"result": detail}), \
                 patch("rentmont.service.extract", return_value=rental()) as extract:
                stats = scan(None, None, store, settings)
                with patch("rentmont.service.send_ad", return_value=delivery()):
                    deliver(None, store, settings, stats.listings)
                before = store.get_extraction("123")
                detail["furnished"] = {"name": "Částečně", "value": 3}
                stats = scan(None, None, store, settings)
                self.assertEqual(extract.call_count, 1)
            self.assertEqual(stats.listings[0].source.furnishing, Furnishing.PARTIALLY_FURNISHED)
            self.assertEqual(store.get_extraction("123").extracted_at, before.extracted_at)
            exported = (root / "listings.json").read_text(encoding="utf-8")
            self.assertIn('"furnishing": "partially_furnished"', exported)
            with patch("rentmont.service.edit_ad", return_value=delivery().response) as edit:
                self.assertTrue(deliver(None, store, settings, stats.listings))
                self.assertTrue(deliver(None, store, settings, stats.listings))
            edit.assert_called_once()
            self.assertIn("🛋 Furnishing: частично меблированная", format_message(edit.call_args.args[-1]))
