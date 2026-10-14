from datetime import date
from email.parser import BytesParser
from email.policy import default
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
from rentmont.location import BRNO_CENTER_LAT, BRNO_CENTER_LON, distance_to_center_km
from rentmont.models import Listing, Payments, RentalDetails, StoredAd
from rentmont.service import scan
from rentmont.sreality import listing_dates, listing_photos, matches, photo_download_url, search
from rentmont.storage import JsonStore, StorageError
from rentmont.telegram import TelegramRejected, download_photo, format_message, post_message, safe_description, send_pending


PHOTO_BYTES = b"\xff\xd8\xfftest-jpeg-bytes"


def multipart_payload(request):
    headers = f"Content-Type: {request.headers['content-type']}\r\nMIME-Version: 1.0\r\n\r\n"
    message = BytesParser(policy=default).parsebytes(headers.encode() + request.content)
    payload, files = {}, {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        content = part.get_payload(decode=True)
        if part.get_filename():
            files[name] = content
        else:
            payload[name] = content.decode("utf-8")
    if "media" in payload:
        payload["media"] = json.loads(payload["media"])
    return payload, files


def ad(ident=123, price=12000, district=72):
    return {"hash_id": ident, "price_czk": price,
            "category_main_cb": {"value": 1}, "category_type_cb": {"value": 2},
            "category_sub_cb": {"name": "1+kk"}, "advert_name": "Test apartment",
            "since": "2026-10-01", "edited": "2026-10-14",
            "advert_images": [{"url": "//example.com/flat.jpg"}],
            "locality": {"district_id": district, "city_seo_name": "brno",
                         "gps_lat": 49.166982790872, "gps_lon": 16.5559570672062}}


def payments():
    return Payments(rent_kc=12000, utilities_kc=2000, comission_kc=None, deposit_kc=24000)


def rental_details(is_room=False):
    return RentalDetails(**payments().model_dump(), is_room_rent=is_room,
                         area_m2=None, available_from=None)


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
        self.assertIn('>Room</a> — 23.5 м²', message)
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
        self.assertIsNone(listing.distance_to_center_km)
        self.assertIsNone(listing.published_at)
        self.assertIsNone(listing.updated_at)
        self.assertEqual(listing.photos, [])

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
                self.assertIn(f"Move In: {text}", message)

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

    def test_distance_calculation(self):
        self.assertEqual(distance_to_center_km({"gps_lat": BRNO_CENTER_LAT,
                                               "gps_lon": BRNO_CENTER_LON}), 0.0)
        self.assertEqual(distance_to_center_km(ad()["locality"]), 4.9)
        # One latitude degree is approximately 111 km.
        self.assertEqual(distance_to_center_km({"gps_lat": BRNO_CENTER_LAT + 1,
                                               "gps_lon": BRNO_CENTER_LON}), 111.2)

    def test_invalid_coordinates(self):
        for locality in (None, {}, {"gps_lat": 49}, {"gps_lat": 0, "gps_lon": 0},
                         {"gps_lat": "49", "gps_lon": 16}, {"gps_lat": True, "gps_lon": 16},
                         {"gps_lat": math.nan, "gps_lon": 16},
                         {"gps_lat": 49, "gps_lon": math.inf},
                         {"gps_lat": 91, "gps_lon": 16}, {"gps_lat": 49, "gps_lon": 181}):
            with self.subTest(locality=locality):
                self.assertIsNone(distance_to_center_km(locality))

    def test_distance_telegram_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStore(Path(directory) / "db.json")
            listing = Listing(**payments().model_dump(), distance_to_center_km=4.9,
                              link="https://www.sreality.cz/")
            store.add("1", StoredAd(title="Flat", listing=listing, notified=False))
            loaded = JsonStore(store.path).pending()[0][1]
            self.assertEqual(loaded.listing.distance_to_center_km, 4.9)
            self.assertIn("Km to Center: ≈4.9 км по прямой", format_message(loaded))
            self.assertIn("Km to Center: не указано", format_message(stored_ad()))

    def test_listing_dates(self):
        self.assertEqual(listing_dates(ad()), {"published_at": date(2026, 10, 1),
                                              "updated_at": date(2026, 10, 14)})
        for value in (None, "", "invalid", "2026-02-30", "20261001", 0):
            with self.subTest(value=value):
                self.assertEqual(listing_dates({"since": value, "edited": value}),
                                 {"published_at": None, "updated_at": None})
        self.assertEqual(listing_dates({"ready_date": "2026-12-01"}),
                         {"published_at": None, "updated_at": None})

    def test_listing_dates_storage_and_telegram(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStore(Path(directory) / "db.json")
            listing = Listing(**payments().model_dump(), **listing_dates(ad()),
                              link="https://www.sreality.cz/")
            store.add("1", StoredAd(title="Flat", listing=listing, notified=False))
            loaded = JsonStore(store.path).pending()[0][1]
            self.assertEqual(loaded.listing.published_at, date(2026, 10, 1))
            self.assertEqual(loaded.listing.updated_at, date(2026, 10, 14))
            self.assertIn("Uploaded 01.10.2026", format_message(loaded))
            self.assertIn("Updated 14.10.2026", format_message(loaded))
            self.assertIn("Uploaded не указано", format_message(stored_ad()))

    def test_photo_urls(self):
        images = [None, {}, {"url": "invalid"}, {"url": "ftp://example.com/1.jpg"},
                  {"url": "//example.com/1.jpg"}, {"url": "https://example.com/1.jpg"},
                  {"url": "https://user:password@example.com/2.jpg"}]
        images += [{"url": f"https://example.com/{i}.jpg"} for i in range(2, 20)]
        photos = listing_photos({"advert_images": images})
        self.assertEqual(len(photos), 3)
        self.assertEqual(str(photos[0]), "https://example.com/1.jpg")
        self.assertEqual(str(photos[-1]), "https://example.com/3.jpg")
        self.assertEqual(listing_photos({}), [])

    def test_telegram_photos_and_album(self):
        for count, method in ((0, "sendMessage"), (1, "sendPhoto"), (3, "sendMediaGroup"),
                              (12, "sendMediaGroup")):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as directory:
                store = JsonStore(Path(directory) / "db.json")
                listing = Listing(**payments().model_dump(), link="https://www.sreality.cz/",
                                  photos=[f"https://example.com/{i}.jpg" for i in range(count)])
                store.add("1", StoredAd(title="Flat", listing=listing, notified=False))
                store = JsonStore(store.path)
                requests = []
                def handler(request):
                    if request.method == "GET":
                        return httpx.Response(200, content=PHOTO_BYTES)
                    requests.append(request)
                    return httpx.Response(200, json={"ok": True})
                with httpx.Client(transport=httpx.MockTransport(handler)) as client, patch("rentmont.telegram.time.sleep"):
                    self.assertTrue(send_pending(client, store, "secret", "1"))
                self.assertEqual(len(requests), 1)
                self.assertTrue(requests[0].url.path.endswith("/" + method))
                if count:
                    payload, files = multipart_payload(requests[0])
                    self.assertEqual(len(files), min(count, 3))
                    self.assertTrue(all(content == PHOTO_BYTES for content in files.values()))
                else:
                    payload = json.loads(requests[0].content)
                if count > 1:
                    self.assertEqual(len(payload["media"]), min(count, 3))
                    for item in payload["media"]:
                        self.assertTrue(item["media"].startswith("attach://"))
                        self.assertIn(item["media"].removeprefix("attach://"), files)
                    self.assertIn("Price:", payload["media"][0]["caption"])
                    self.assertEqual(payload["media"][0]["parse_mode"], "HTML")
                    self.assertNotIn("caption", payload["media"][1])
                elif count == 1:
                    self.assertIn("Price:", payload["caption"])
                    self.assertEqual(payload["parse_mode"], "HTML")
                else:
                    self.assertEqual(payload["parse_mode"], "HTML")
                self.assertEqual(JsonStore(store.path).pending(), [])

    def test_photo_rejection_falls_back_but_rate_limit_does_not(self):
        for status in (400, 429):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                store = JsonStore(Path(directory) / "db.json")
                listing = Listing(**payments().model_dump(), link="https://www.sreality.cz/",
                                  photos=["https://example.com/flat.jpg"])
                store.add("1", StoredAd(title="Flat", listing=listing, notified=False))
                methods = []
                def handler(request):
                    if request.method == "GET":
                        return httpx.Response(200, content=PHOTO_BYTES)
                    methods.append(request.url.path.split("/")[-1])
                    if methods[-1] == "sendPhoto":
                        return httpx.Response(status, json={"ok": False, "error_code": status})
                    return httpx.Response(200, json={"ok": True})
                with httpx.Client(transport=httpx.MockTransport(handler)) as client, patch("rentmont.telegram.time.sleep"):
                    self.assertEqual(send_pending(client, store, "secret", "1"), status == 400)
                self.assertEqual(methods, ["sendPhoto", "sendMessage"] if status == 400 else ["sendPhoto"])
                self.assertEqual(len(store.pending()), 0 if status == 400 else 1)

    def test_sreality_photo_rendition(self):
        raw = "https://d18-a.sdn.cz/d_18/photo.jpeg"
        transformed = httpx.URL(photo_download_url(raw))
        self.assertEqual(transformed.params["fl"], "res,1200,1200,1|shr,,20|jpg,80")
        existing = raw + "?fl=res,800,800,1&other=1"
        self.assertEqual(photo_download_url(existing), existing)
        external = "https://example.com/photo.jpeg"
        self.assertEqual(photo_download_url(external), external)
        def handler(request):
            self.assertEqual(request.url.params["fl"], "res,1200,1200,1|shr,,20|jpg,80")
            return httpx.Response(200, content=PHOTO_BYTES)
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            self.assertEqual(download_photo(client, raw)[1], PHOTO_BYTES)

    def test_download_photo_validation(self):
        for content, valid in ((PHOTO_BYTES, True), (b"<html>blocked</html>", False), (b"", False)):
            with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=content))) as client:
                if valid:
                    self.assertEqual(download_photo(client, "https://example.com/photo.jpg"),
                                     ("photo.jpg", PHOTO_BYTES, "image/jpeg"))
                else:
                    with self.assertRaises(ValueError):
                        download_photo(client, "https://example.com/photo.jpg")
        with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=PHOTO_BYTES))) as client:
            with patch("rentmont.telegram.MAX_PHOTO_BYTES", 2), self.assertRaises(ValueError):
                download_photo(client, "https://example.com/photo.jpg")

    def test_bad_download_does_not_drop_other_photos(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStore(Path(directory) / "db.json")
            listing = Listing(**payments().model_dump(), link="https://www.sreality.cz/",
                              photos=["https://example.com/bad.jpg", "https://example.com/good.jpg"])
            store.add("1", StoredAd(title="Flat", listing=listing, notified=False))
            requests = []
            def handler(request):
                if request.method == "GET":
                    return httpx.Response(404) if request.url.path == "/bad.jpg" else httpx.Response(200, content=PHOTO_BYTES)
                requests.append(request)
                return httpx.Response(200, json={"ok": True})
            with httpx.Client(transport=httpx.MockTransport(handler)) as client, patch("rentmont.telegram.time.sleep"):
                self.assertTrue(send_pending(client, store, "secret", "1"))
            self.assertEqual(len(requests), 1)
            self.assertTrue(requests[0].url.path.endswith("/sendPhoto"))
            _, files = multipart_payload(requests[0])
            self.assertEqual(files["photo"], PHOTO_BYTES)

    def test_telegram_error_description(self):
        def handler(request):
            return httpx.Response(400, json={"ok": False, "error_code": 400,
                                            "description": "Bad Request: failed to get HTTP URL content"})
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(TelegramRejected) as caught:
                post_message(client, "secret", "sendPhoto", {})
        self.assertEqual(caught.exception.code, 400)
        self.assertEqual(caught.exception.description, "Bad Request: failed to get HTTP URL content")
        sanitized = safe_description("Error secret https://api.telegram.org/botsecret/sendPhoto\nnext", "secret")
        self.assertNotIn("secret", sanitized)
        self.assertNotIn("https://", sanitized)
        self.assertNotIn("\n", sanitized)

    def test_compact_message_html(self):
        listing = Listing(**payments().model_dump(), **listing_dates(ad()),
                          area_m2=36, available_from="asap", distance_to_center_km=4.9,
                          link="https://www.sreality.cz/?a=1&b=2")
        message = format_message(StoredAd(title='Flat <nice> & "bright"', listing=listing, notified=False))
        self.assertEqual(len(message.splitlines()), 6)
        self.assertIn('href="https://www.sreality.cz/?a=1&amp;b=2"', message)
        self.assertIn('Flat &lt;nice&gt; &amp; &quot;bright&quot;</a> — 36 м²', message)
        self.assertIn("Price: 12,000 Kč + 2,000 Kč = 14,000 Kč", message)
        self.assertIn("Comission/Deposit: не указано + 24,000 Kč", message)
        self.assertIn("Move In: как можно скорее", message)
        self.assertIn("Uploaded 01.10.2026 Updated 14.10.2026", message)

    def test_message_totals_require_both_amounts(self):
        for rent, utilities, deposit, commission in (
            (12990, 1000, 13990, 1000),
            (12990, None, 13990, None),
            (None, 1000, None, 1000),
            (None, None, None, None),
        ):
            listing = Listing(rent_kc=rent, utilities_kc=utilities, deposit_kc=deposit,
                              comission_kc=commission, link="https://www.sreality.cz/")
            lines = format_message(StoredAd(title="Flat", listing=listing, notified=False)).splitlines()
            self.assertEqual(" = " in lines[1], rent is not None and utilities is not None)
            self.assertNotIn(" = ", lines[2])
            if rent is not None and utilities is not None:
                self.assertTrue(lines[1].endswith("= 13,990 Kč"))
            if deposit is not None and commission is not None:
                self.assertEqual(lines[2], "🔐 Comission/Deposit: 1,000 Kč + 13,990 Kč")

    def test_room_filter_is_persistent(self):
        for category_room in (True, False):
            with self.subTest(category_room=category_room), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                store = JsonStore(path / "db.json")
                detail = ad()
                if category_room:
                    detail["category_sub_cb"] = {"name": "Pokoj"}
                with patch("rentmont.service.search", return_value=[ad()]), \
                     patch("rentmont.service.api_json", return_value={"result": detail}), \
                     patch("rentmont.service.extract", return_value=rental_details(True)) as extract, \
                     patch("rentmont.service.time.sleep"):
                    scan(None, None, store, settings(path, notify_existing=True))
                    store = JsonStore(store.path)
                    scan(None, None, store, settings(path, notify_existing=True))
                self.assertEqual(extract.call_count, 0 if category_room else 1)
                self.assertTrue(store.contains("123"))
                self.assertEqual(store.pending(), [])
                self.assertEqual(json.loads((path / "listings.json").read_text()), [])

    def test_stored_rooms_not_sent_or_exported(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStore(Path(directory) / "db.json")
            room = Listing(**rental_details(True).model_dump(), link="https://www.sreality.cz/")
            store.add("1", StoredAd(title="Room", listing=room, notified=False))
            store = JsonStore(store.path)
            self.assertEqual(store.pending(), [])
            store.export_listings(store.path.parent / "listings.json")
            self.assertEqual(json.loads((store.path.parent / "listings.json").read_text()), [])
            legacy = Listing(**payments().model_dump(),
                             link="https://www.sreality.cz/detail/pronajem/byt/pokoj/brno/123")
            self.assertTrue(legacy.is_room_rent)

    def test_config_validation(self):
        self.assertEqual(settings(Path("data")).openai_model, "gpt-5.4-mini")
        self.assertEqual(settings(Path("data"), openai_model="custom-model").openai_model, "custom-model")
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
                 patch("rentmont.service.extract", return_value=rental_details()) as extract, \
                 patch("rentmont.service.time.sleep"):
                self.assertTrue(scan(None, None, store, config))
                # Restart: ID and initialization are loaded from JSON.
                store = JsonStore(path / "db.json")
                scan(None, None, store, config)
                self.assertEqual(extract.call_count, 1)
            self.assertEqual(store.pending(), [])
            with patch("rentmont.service.search", return_value=[ad(456)]), \
                 patch("rentmont.service.api_json", return_value={"result": ad(456)}), \
                 patch("rentmont.service.extract", return_value=rental_details()), \
                 patch("rentmont.service.time.sleep"):
                scan(None, None, store, config)
            self.assertEqual([ident for ident, _ in store.pending()], ["456"])
            data = json.loads((path / "listings.json").read_text(encoding="utf-8"))
            self.assertEqual(len(data), 2)
            self.assertEqual(set(data[0]), {"rent_kc", "utilities_kc", "comission_kc", "deposit_kc", "link", "is_room_rent", "area_m2", "available_from", "distance_to_center_km", "published_at", "updated_at", "photos"})
            self.assertEqual(data[0]["photos"], ["https://example.com/flat.jpg"])
            self.assertEqual(data[0]["published_at"], "2026-10-01")
            self.assertEqual(data[0]["updated_at"], "2026-10-14")
            self.assertEqual(data[0]["distance_to_center_km"], 4.9)

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
                 patch("rentmont.service.extract", return_value=rental_details()), \
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
