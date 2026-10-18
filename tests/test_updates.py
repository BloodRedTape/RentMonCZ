"""Hash-driven extraction and editing regressions."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from rentmont.extraction import HASH_FIELDS, listing_hash
from rentmont.service import deliver, scan
from rentmont.storage import JsonStore
from rentmont.telegram import mark_ad_gone
from tests.helpers import api_ad, config, delivery, rental


class UpdateTests(unittest.TestCase):
    def test_hash_fields_and_stability(self):
        original = api_ad()
        for key in HASH_FIELDS:
            with self.subTest(key=key):
                self.assertNotEqual(listing_hash(original), listing_hash(original | {key: "changed"}))
        self.assertEqual(listing_hash(original), listing_hash(dict(reversed(list(original.items())))))
        self.assertEqual(listing_hash(original), listing_hash(original | {"user_email": "ignored"}))

    @patch("rentmont.service.time.sleep")
    def test_edit_retry_and_later_gone_uses_updated_body(self, sleep):
        for method in ("sendMessage", "sendPhoto", "sendMediaGroup"):
            with self.subTest(method=method), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                store = JsonStore(root)
                settings = config(root)
                field = "text" if method == "sendMessage" else "caption"
                receipt = delivery().model_copy(update={"method": method})
                detail = api_ad()
                def run_scan():
                    with patch("rentmont.service.search", return_value=[detail]), \
                         patch("rentmont.service.api_json", return_value={"result": detail}), \
                         patch("rentmont.service.extract", return_value=rental(rent=12500)) as extract:
                        stats = scan(None, None, store, settings)
                    return stats, extract.call_count
                stats, calls = run_scan()
                self.assertEqual(calls, 1)
                with patch("rentmont.service.send_ad", return_value=receipt):
                    self.assertTrue(deliver(None, store, settings, stats.listings))
                detail = detail | {"advert_description": "Updated", "edited": "2026-10-20"}
                stats, calls = run_scan()
                self.assertEqual(calls, 1)
                self.assertEqual(store.get_extraction("123").content_hash, listing_hash(detail))
                edits = []
                def handler(request):
                    edits.append(json.loads(request.content))
                    if len(edits) == 1:
                        return httpx.Response(500)
                    message = {"message_id": 42, "chat": {"id": -100123}, field: "Updated plain body"}
                    return httpx.Response(200, json={"ok": True, "result": message})
                with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                    self.assertFalse(deliver(client, store, settings, stats.listings))
                    self.assertNotEqual(store.get_notification("123").content_hash, listing_hash(detail))
                    store = JsonStore(root)
                    self.assertTrue(deliver(client, store, settings, stats.listings))
                    self.assertTrue(deliver(client, store, settings, stats.listings))
                self.assertEqual(len(edits), 2)
                self.assertEqual(edits[0], edits[1])
                self.assertIn("20.10.2026", edits[1][field])
                self.assertEqual(store.get_notification("123").telegram_response["result"][field],
                                 "Updated plain body")
                stats, calls = run_scan()
                self.assertEqual(calls, 0)
                with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                    mark_ad_gone(client, "test", store.mark_gone("123"))
                self.assertTrue(edits[-1][field].startswith("Updated plain body\n🔴 Gone "))

    @patch("rentmont.service.time.sleep")
    def test_failed_reextraction_does_not_edit(self, sleep):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = JsonStore(root)
            store.save_source(api_ad() | {"advert_description": "new"})
            store.save_extraction("123", rental(), listing_hash(api_ad()))
            store.mark_sent("123", delivery(), listing_hash(api_ad()))
            store.set_current_search({"123"})
            with patch("rentmont.service.edit_ad") as edit:
                self.assertTrue(deliver(None, store, config(root), ()))
            edit.assert_not_called()
