"""Domain, persistence, source boundary and Telegram regression tests."""
from datetime import date
import json
import math
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import httpx
from pydantic import ValidationError

from rentmont.extraction import extract
from rentmont.filters import rental_rejection, source_rejection
from rentmont.models import ExclusionReason, Payments, RentalDetails
from rentmont.sreality import listing_dates, listing_photos, photo_download_url, search
from rentmont.storage import JsonStore, StorageError
from rentmont.telegram import (TelegramRejected, download_photo, format_message,
                              post_message, safe_description, send_ad)
from tests.helpers import PHOTO_BYTES, api_ad, rental, record, delivery


class DomainTests(unittest.TestCase):
    def test_payments(self):
        for field in Payments.model_fields:
            for value in (0, 0.0, "0.0", None, 15000):
                parsed = Payments(**(rental().payments.model_dump() | {field: value}))
                self.assertEqual(getattr(parsed, field), 15000 if value == 15000 else None)
            for value in (-1, math.inf, math.nan):
                with self.assertRaises(ValidationError):
                    Payments(**(rental().payments.model_dump() | {field: value}))

    def test_availability_area_and_immutability(self):
        base = rental().model_dump()
        for value in (None, "asap", date(2026, 12, 1), "2026-12-01"):
            RentalDetails(**(base | {"available_from": value}))
        for value in ("soon", "2026-02-30", "20261201", 0, True):
            with self.assertRaises(ValidationError):
                RentalDetails(**(base | {"available_from": value}))
        for value in (0, -1, math.inf):
            with self.assertRaises(ValidationError):
                RentalDetails(**(base | {"area_m2": value}))
        with self.assertRaises(ValidationError):
            rental().is_room_rent = True

    def test_filters(self):
        self.assertIsNone(source_rejection(record().source, 72, 13500))
        self.assertIsNone(rental_rejection(rental(), 13500))
        self.assertEqual(rental_rejection(rental(room=True), 13500), ExclusionReason.ROOM)
        self.assertEqual(rental_rejection(rental(rent=None), 13500), ExclusionReason.UNKNOWN_RENT)
        self.assertEqual(rental_rejection(rental(rent=13500), 13500), ExclusionReason.RENT_LIMIT)


class TotalLimitTests(unittest.TestCase):
    def test_total_limit_boundaries(self):
        for utilities, expected in ((1999, None), (2000, ExclusionReason.RENT_AND_UTILITIES_LIMIT),
                                    (2001, ExclusionReason.RENT_AND_UTILITIES_LIMIT), (None, None)):
            with self.subTest(utilities=utilities):
                self.assertEqual(rental_rejection(rental(utilities=utilities), 13500, 14000), expected)
        self.assertIsNone(rental_rejection(rental(utilities=99999), 13500))
        self.assertEqual(rental_rejection(rental(rent=None), 13500, 14000), ExclusionReason.UNKNOWN_RENT)

    def test_setting_validation(self):
        from tests.helpers import config
        settings = config(Path("data"))
        self.assertIsNone(settings.max_rent_and_utilities_kc)
        for value in (0, -1):
            with self.assertRaises(ValidationError):
                type(settings).model_validate(settings.model_dump() | {"max_rent_and_utilities_kc": value})
        parsed = type(settings).model_validate(settings.model_dump() | {"max_rent_and_utilities_kc": "17000"})
        self.assertEqual(parsed.max_rent_and_utilities_kc, 17000)


class StorageTests(unittest.TestCase):
    def test_independent_files_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = JsonStore(root)
            store.save_source(api_ad())
            first_seen = store.get_source("123").first_seen_at
            source_bytes = (root / "123/source.json").read_bytes()
            store.save_extraction("123", rental())
            store.mark_sent("123", delivery())
            self.assertEqual((root / "123/source.json").read_bytes(), source_bytes)
            store.save_source(api_ad() | {"advert_description": "Changed"})
            store.set_current_search({"123"})
            store.mark_initialized()
            store = JsonStore(root)
            self.assertTrue(store.initialized)
            self.assertEqual(store.get_source("123").first_seen_at, first_seen)
            self.assertEqual(store.get_extraction("123").details, rental())
            self.assertIsNotNone(store.get_notification("123").sent_at)
            self.assertEqual(store.get_notification("123").telegram_response, delivery().response)
            self.assertEqual(store.get_notification("123").message_ids, (42,))
            store.get_source("123").raw["hash_id"] = 999
            self.assertEqual(store.get_source("123").raw["hash_id"], 123)
            self.assertEqual(set(json.loads(store.path.read_text())),
                             {"schema_version", "initialized", "current_search_ids"})
            self.assertEqual({p.name for p in (root / "123").iterdir()},
                             {"source.json", "extraction.json", "notification.json"})

    def test_invalid_files_not_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = JsonStore(root)
            store.save_source(api_ad())
            for name, read in (("source.json", store.get_source),
                               ("extraction.json", store.get_extraction),
                               ("notification.json", store.get_notification)):
                path = root / "123" / name
                path.write_text('{"bad":')
                with self.assertRaises(StorageError):
                    read("123")
                self.assertEqual(path.read_text(), '{"bad":')
            store.path.write_text('{"schema_version":2}')
            with self.assertRaises(StorageError):
                JsonStore(root)

    def test_failed_write_preserves_memory_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStore(Path(directory))
            store.save_source(api_ad())
            source_path = Path(directory) / "123/source.json"
            before = source_path.read_bytes()
            with patch("rentmont.storage.Path.replace", side_effect=OSError("disk")):
                with self.assertRaises(StorageError):
                    store.save_source(api_ad() | {"advert_description": "Changed"})
                with self.assertRaises(StorageError):
                    store.mark_initialized()
            self.assertFalse(store.initialized)
            self.assertEqual(source_path.read_bytes(), before)
            self.assertEqual(list(Path(directory).rglob("*.tmp")), [])

    def test_safe_paths_and_id_match(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStore(Path(directory))
            for ident in ("../escape", "", "/tmp/test", "123/456", ".."):
                with self.assertRaises(StorageError):
                    store.get_source(ident)
            store.save_source(api_ad())
            path = Path(directory) / "123/source.json"
            content = json.loads(path.read_text())
            content["raw"]["hash_id"] = 456
            path.write_text(json.dumps(content))
            with self.assertRaises(StorageError):
                store.get_source("123")


class SourceTests(unittest.TestCase):
    def test_pagination(self):
        offsets = []
        def handler(request):
            offset = int(request.url.params["offset"])
            offsets.append(offset)
            return httpx.Response(200, json={"results": [api_ad(offset + 1)], "pagination": {"total": 2}})
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            self.assertEqual(len(search(client, 72, 13500)), 2)
        self.assertEqual(offsets, [0, 1])

    def test_dates_photos(self):
        self.assertEqual(listing_dates(api_ad())["published_at"], date(2026, 10, 1))
        for value in (None, "invalid", "2026-02-30", "20261001", 0):
            self.assertIsNone(listing_dates({"since": value})["published_at"])
        photos = listing_photos({"advert_images": [None, "bad", "//example.com/1.jpg",
                                                   "https://example.com/1.jpg"]})
        self.assertEqual(len(photos), 1)
        raw = "https://d18-a.sdn.cz/d_18/photo.jpeg"
        self.assertEqual(httpx.URL(photo_download_url(raw)).params["fl"], "res,1200,1200,1|shr,,20|jpg,80")

    def test_extraction_whitelist(self):
        ai = MagicMock()
        ai.chat.completions.parse.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(parsed=rental()))])
        extract(ai, "model", api_ad() | {"user_email": "private"})
        args = ai.chat.completions.parse.call_args.kwargs
        self.assertIs(args["response_format"], RentalDetails)
        self.assertNotIn("user_email", json.loads(args["messages"][1]["content"]))


class TelegramTests(unittest.TestCase):
    def test_format(self):
        listing = record()
        listing = listing.model_copy(update={"source": listing.source.model_copy(update={"title": "Flat <nice> & bright"})})
        text = format_message(listing)
        self.assertIn("Flat &lt;nice&gt; &amp; bright", text)
        self.assertIn("12,000 Kč + 2,000 Kč = 14,000 Kč", text)
        self.assertEqual(len(text.splitlines()), 6)
        for value, expected in ((None, "не указано"), ("asap", "как можно скорее"), (date(2026, 12, 1), "01.12.2026")):
            self.assertIn(expected, format_message(listing.model_copy(update={"rental": rental().model_copy(update={"available_from": value})})))

    def test_multipart_and_fallback(self):
        for count in (0, 1, 3):
            for status in (200, 400, 429):
                requests = []
                def handler(request):
                    if request.method == "GET":
                        return httpx.Response(200, content=PHOTO_BYTES)
                    requests.append(request)
                    code = status if not request.url.path.endswith("sendMessage") else 200
                    messages = [{"message_id": 42 + i, "chat": {"id": -100123},
                                 "date": 1791970000, "caption": "notification"}
                                for i in range(count if request.url.path.endswith("sendMediaGroup") else 1)]
                    result = messages if request.url.path.endswith("sendMediaGroup") else messages[0]
                    return httpx.Response(code, json={"ok": code == 200, "description": "Rejected", "result": result})
                with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                    if count and status == 429:
                        with self.assertRaises(httpx.HTTPStatusError):
                            send_ad(client, "secret", "1", record(photos=[f"https://example.com/{i}.jpg" for i in range(count)]))
                    else:
                        receipt = send_ad(client, "secret", "1", record(photos=[f"https://example.com/{i}.jpg" for i in range(count)]))
                        expected_count = count if count > 1 and status == 200 else 1
                        self.assertEqual(receipt.chat_id, -100123)
                        self.assertEqual(receipt.message_ids, tuple(range(42, 42 + expected_count)))
                        returned = receipt.response["result"]
                        first_message = returned[0] if isinstance(returned, list) else returned
                        self.assertEqual(first_message["date"], 1791970000)
                        self.assertEqual(first_message["caption"], "notification")
                        self.assertEqual(receipt.method, "sendMessage" if not count or status == 400 else "sendPhoto" if count == 1 else "sendMediaGroup")
                if count:
                    self.assertIn("multipart/form-data", requests[0].headers["content-type"])
                    self.assertIn(PHOTO_BYTES, requests[0].content)
                self.assertEqual(len(requests), 2 if count and status == 400 else 1)

    def test_missing_message_metadata_is_not_accepted(self):
        with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}))) as client:
            with self.assertRaises(KeyError):
                post_message(client, "secret", "sendMessage", {"chat_id": "1", "text": "test"})

    def test_photo_validation_and_secret_redaction(self):
        with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"<html>"))) as client:
            with self.assertRaises(ValueError):
                download_photo(client, "https://example.com/p.jpg")
        with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(400, json={"description": "Rejected"}))) as client:
            with self.assertRaises(TelegramRejected):
                post_message(client, "secret", "sendPhoto", {})
        safe = safe_description("secret https://api.telegram.org/botsecret/sendPhoto\nnext", "secret")
        self.assertNotIn("secret", safe)
        self.assertNotIn("https://", safe)
        self.assertNotIn("\n", safe)
