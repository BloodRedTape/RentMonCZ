"""Unavailable listings edit their original notification exactly once."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from rentmont.models import TelegramDelivery
from rentmont.service import update_gone
from rentmont.storage import JsonStore
from tests.helpers import config, delivery


class GoneTests(unittest.TestCase):
    @patch("rentmont.service.time.sleep")
    def test_text_photo_album_and_retry(self, sleep):
        for method in ("sendMessage", "sendPhoto", "sendMediaGroup"):
            with self.subTest(method=method), tempfile.TemporaryDirectory() as directory:
                store = JsonStore(Path(directory))
                field = "text" if method == "sendMessage" else "caption"
                entities = "entities" if field == "text" else "caption_entities"
                message = {"message_id": 42, "chat": {"id": -100123}, field: "Original",
                           entities: [{"type": "bold", "offset": 0, "length": 8}]}
                store.mark_sent("123", TelegramDelivery(
                    method=method, chat_id=-100123, message_ids=(42,),
                    response={"ok": True, "result": [message] if method == "sendMediaGroup" else message}))
                edits = []
                def handler(request):
                    if request.method == "GET":
                        return httpx.Response(404)
                    edits.append(json.loads(request.content))
                    if len(edits) == 1:
                        return httpx.Response(500)
                    return httpx.Response(200, json={"ok": True, "result": message})
                with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                    self.assertFalse(update_gone(client, store, config(Path(directory))))
                    detected = store.get_notification("123").gone_at
                    self.assertIsNotNone(detected)
                    store = JsonStore(Path(directory))
                    self.assertTrue(update_gone(client, store, config(Path(directory))))
                    self.assertTrue(update_gone(client, store, config(Path(directory))))
                self.assertEqual(len(edits), 2)
                self.assertEqual(edits[0], edits[1])
                self.assertEqual(edits[1][field], f"Original\n🔴 Gone {detected:%d.%m.%Y}")
                self.assertEqual(edits[1][entities], message[entities])
                self.assertEqual(edits[1]["message_id"], 42)
                self.assertTrue(store.get_notification("123").gone_notified)

    @patch("rentmont.service.time.sleep")
    def test_only_confirmed_missing(self, sleep):
        for status in (200, 403, 429, 500, 404, 410):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                store = JsonStore(Path(directory))
                store.mark_sent("123", delivery())
                edits = []
                def handler(request):
                    if request.method == "GET":
                        return httpx.Response(status, json={"result": {"hash_id": 123}})
                    edits.append(request)
                    return httpx.Response(200, json=delivery().response)
                with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                    update_gone(client, store, config(Path(directory)))
                self.assertEqual(bool(edits), status in (404, 410))
                self.assertEqual(store.get_notification("123").gone_at is not None,
                                 status in (404, 410))

    def test_current_and_suppressed_are_not_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStore(Path(directory))
            store.mark_sent("123", delivery())
            store.suppress("456")
            store.set_current_search({"123"})
            def handler(request):
                self.fail("Unexpected request")
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                self.assertTrue(update_gone(client, store, config(Path(directory))))
