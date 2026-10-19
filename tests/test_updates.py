"""Hash-driven extraction and editing regressions."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from rentmont.extraction import EXTRACTION_FIELDS, HASH_FIELDS, global_hash, llm_hash
from rentmont.service import deliver, scan
from rentmont.storage import JsonStore
from rentmont.telegram import mark_ad_gone
from tests.helpers import api_ad, config, delivery, rental


class UpdateTests(unittest.TestCase):
    def test_hash_fields_and_stability(self):
        original = api_ad()
        for key in HASH_FIELDS:
            with self.subTest(key=key):
                self.assertNotEqual(global_hash(original), global_hash(original | {key: "changed"}))
        self.assertEqual(global_hash(original), global_hash(dict(reversed(list(original.items())))))
        self.assertEqual(global_hash(original), global_hash(original | {"user_email": "ignored"}))

    def test_llm_hash_uses_exact_input_fields(self):
        original = api_ad()
        for key in EXTRACTION_FIELDS:
            with self.subTest(key=key):
                self.assertNotEqual(llm_hash(original), llm_hash(original | {key: "changed"}))
        self.assertEqual(llm_hash(original), llm_hash(original | {
            "since": "2026-10-20", "edited": "2026-10-21", "user_email": "ignored"}))
        self.assertEqual(llm_hash(original), llm_hash(dict(reversed(list(original.items())))))

    @patch("rentmont.service.time.sleep")
    def test_separate_hashes_both_trigger_message_updates(self, sleep):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = JsonStore(root)
            settings = config(root)
            detail = api_ad()
            with patch("rentmont.service.search", return_value=[detail]), \
                 patch("rentmont.service.api_json", return_value={"result": detail}), \
                 patch("rentmont.service.extract", return_value=rental()):
                stats = scan(None, None, store, settings)
            with patch("rentmont.service.send_ad", return_value=delivery()):
                deliver(None, store, settings, stats.listings)
            for changes, expected_calls in (({"edited": "2026-10-20"}, 0),
                                            ({"usable_area": 42}, 1)):
                detail = detail | changes
                with patch("rentmont.service.search", return_value=[detail]), \
                     patch("rentmont.service.api_json", return_value={"result": detail}), \
                     patch("rentmont.service.extract", return_value=rental()) as extract:
                    stats = scan(None, None, store, settings)
                self.assertEqual(extract.call_count, expected_calls)
                with patch("rentmont.service.edit_ad", return_value=delivery().response) as edit:
                    self.assertTrue(deliver(None, store, settings, stats.listings))
                    self.assertTrue(deliver(None, store, settings, stats.listings))
                edit.assert_called_once()
                saved = store.get_notification("123")
                self.assertEqual(saved.global_hash, global_hash(detail))
                self.assertEqual(saved.llm_hash, llm_hash(detail))

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
                self.assertEqual(store.get_extraction("123").global_hash, global_hash(detail))
                edits = []
                def handler(request):
                    edits.append(json.loads(request.content))
                    if len(edits) == 1:
                        return httpx.Response(500)
                    message = {"message_id": 42, "chat": {"id": -100123}, field: "Updated plain body"}
                    return httpx.Response(200, json={"ok": True, "result": message})
                with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                    self.assertFalse(deliver(client, store, settings, stats.listings))
                    self.assertNotEqual(store.get_notification("123").global_hash, global_hash(detail))
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
            store.save_extraction("123", rental(), global_hash(api_ad()), llm_hash(api_ad()))
            store.mark_sent("123", delivery(), global_hash(api_ad()), llm_hash(api_ad()))
            store.set_current_search({"123"})
            for changes in ({"advert_description": "new"}, {"usable_area": 42}):
                with self.subTest(changes=changes):
                    store.save_source(api_ad() | changes)
                    with patch("rentmont.service.edit_ad") as edit:
                        self.assertTrue(deliver(None, store, config(root), ()))
                    edit.assert_not_called()
