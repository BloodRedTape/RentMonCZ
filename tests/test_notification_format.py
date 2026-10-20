"""Template versions refresh existing messages without new sends or LLM calls."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from rentmont.extraction import global_hash, llm_hash
from rentmont.service import deliver, scan
from rentmont.storage import JsonStore
from rentmont.telegram import NOTIFICATION_FORMAT_VERSION
from tests.helpers import api_ad, config, delivery, rental


class NotificationFormatTests(unittest.TestCase):
    def prepare(self, root, method="sendMessage", version=None):
        store = JsonStore(root)
        detail = api_ad()
        store.save_source(detail)
        store.save_extraction("123", rental(), global_hash(detail), llm_hash(detail))
        receipt = delivery().model_copy(update={"method": method})
        store.mark_sent("123", receipt, global_hash(detail), llm_hash(detail),
                        format_version=version)
        store.set_current_search({"123"})
        return store

    @patch("rentmont.service.time.sleep")
    def test_missing_version_updates_once_for_text_photo_and_album(self, sleep):
        for method in ("sendMessage", "sendPhoto", "sendMediaGroup"):
            with self.subTest(method=method), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                store = self.prepare(root, method)
                before = store.get_notification("123")
                with patch("rentmont.service.edit_ad", return_value=delivery().response) as edit, \
                     patch("rentmont.service.send_ad") as send, \
                     patch("rentmont.service.extract") as extract:
                    self.assertTrue(deliver(None, store, config(root), ()))
                    store = JsonStore(root)
                    self.assertTrue(deliver(None, store, config(root), ()))
                edit.assert_called_once()
                send.assert_not_called()
                extract.assert_not_called()
                after = store.get_notification("123")
                self.assertEqual(after.format_version, NOTIFICATION_FORMAT_VERSION)
                self.assertEqual(after.message_ids, before.message_ids)
                self.assertEqual(after.sent_at, before.sent_at)
                self.assertEqual(after.telegram_method, before.telegram_method)

    @patch("rentmont.service.time.sleep")
    def test_version_bump_with_unchanged_input_uses_cache(self, sleep):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.prepare(root, version=NOTIFICATION_FORMAT_VERSION)
            with patch("rentmont.service.NOTIFICATION_FORMAT_VERSION", NOTIFICATION_FORMAT_VERSION + 1), \
                 patch("rentmont.service.search", return_value=[api_ad()]), \
                 patch("rentmont.service.api_json", return_value={"result": api_ad()}), \
                 patch("rentmont.service.extract") as extract, \
                 patch("rentmont.service.edit_ad", return_value=delivery().response) as edit, \
                 patch("rentmont.service.send_ad") as send:
                stats = scan(None, None, store, config(root))
                self.assertEqual(stats.cache_hits, 1)
                self.assertTrue(deliver(None, store, config(root), stats.listings))
                self.assertTrue(deliver(None, store, config(root), stats.listings))
            edit.assert_called_once()
            send.assert_not_called()
            extract.assert_not_called()
            self.assertEqual(store.get_notification("123").format_version,
                             NOTIFICATION_FORMAT_VERSION + 1)

    @patch("rentmont.service.time.sleep")
    def test_failed_edit_keeps_old_version_and_retries_after_restart(self, sleep):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.prepare(root)
            with patch("rentmont.service.edit_ad", side_effect=RuntimeError("timeout")):
                self.assertFalse(deliver(None, store, config(root), ()))
            store = JsonStore(root)
            self.assertIsNone(store.get_notification("123").format_version)
            with patch("rentmont.service.edit_ad", return_value=delivery().response) as edit:
                self.assertTrue(deliver(None, store, config(root), ()))
                self.assertTrue(deliver(None, store, config(root), ()))
            edit.assert_called_once()

    @patch("rentmont.service.time.sleep")
    def test_not_modified_acknowledges_format_version(self, sleep):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.prepare(root)
            with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
                400, json={"ok": False, "description": "Bad Request: message is not modified"}
            ))) as client:
                self.assertTrue(deliver(client, store, config(root), ()))
            self.assertEqual(store.get_notification("123").format_version, NOTIFICATION_FORMAT_VERSION)
            self.assertEqual(store.get_notification("123").telegram_response, delivery().response)

    @patch("rentmont.service.time.sleep")
    def test_new_send_records_format_version(self, sleep):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = JsonStore(root)
            with patch("rentmont.service.search", return_value=[api_ad()]), \
                 patch("rentmont.service.api_json", return_value={"result": api_ad()}), \
                 patch("rentmont.service.extract", return_value=rental()):
                stats = scan(None, None, store, config(root))
            with patch("rentmont.service.send_ad", return_value=delivery()):
                self.assertTrue(deliver(None, store, config(root), stats.listings))
            self.assertEqual(store.get_notification("123").format_version, NOTIFICATION_FORMAT_VERSION)
