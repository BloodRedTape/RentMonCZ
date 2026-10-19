"""Independent maps and scan/delivery policy, without network."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rentmont.models import NotificationStatus
from rentmont.service import scan, deliver
from rentmont.storage import JsonStore
from tests.helpers import api_ad, config, rental, delivery


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.store = JsonStore(self.path)
        self.settings = config(self.path)
        for name in ("rentmont.service.time.sleep",):
            patcher = patch(name)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_scan(self, detail=None, facts=None, reextract=False, error=None):
        detail = api_ad() if detail is None else detail
        with patch("rentmont.service.search", return_value=[detail]), \
             patch("rentmont.service.api_json", return_value={"result": detail}), \
             patch("rentmont.service.extract", return_value=facts or rental(), side_effect=error) as extract:
            stats = scan(None, None, self.store, self.settings, reextract=reextract)
        return stats, extract.call_count

    def test_rejected_facts_survive_restart(self):
        for facts in (rental(rent=None), rental(rent=15000), rental(room=True)):
            with self.subTest(facts=facts):
                self.run_scan(facts=facts, reextract=True)
                self.store = JsonStore(self.path)
                stats, calls = self.run_scan()
                self.assertEqual(calls, 0)
                self.assertEqual(stats.excluded, 1)
                self.assertEqual(self.store.get_extraction("123").details, facts)
                self.assertEqual(stats.listings, ())

    def test_dates_reuse_llm_facts_and_preserve_extraction_time(self):
        self.run_scan()
        before = self.store.get_extraction("123")
        changed = api_ad() | {"since": "2026-10-02", "edited": "2026-10-20"}
        stats, calls = self.run_scan(detail=changed)
        after = self.store.get_extraction("123")
        self.assertEqual(calls, 0)
        self.assertEqual(stats.cache_hits, 1)
        self.assertEqual(before.llm_hash, after.llm_hash)
        self.assertNotEqual(before.global_hash, after.global_hash)
        self.assertEqual(before.extracted_at, after.extracted_at)
        self.assertEqual(stats.listings[0].source.updated_at.isoformat(), "2026-10-20")
        self.store = JsonStore(self.path)
        self.assertEqual(self.run_scan(detail=changed)[1], 0)

    def test_llm_only_fields_trigger_extraction(self):
        for field, value in (("usable_area", 42), ("floor_area", 45),
                             ("ready_date", "2026-12-01"),
                             ("tenant_not_pay_commission", True)):
            with self.subTest(field=field):
                self.run_scan()
                stats, calls = self.run_scan(detail=api_ad() | {field: value})
                self.assertTrue(stats.succeeded)
                self.assertEqual(calls, 1)

    def test_missing_llm_hash_reextracts_and_failed_migration_retries(self):
        self.run_scan()
        path = self.path / "123/extraction.json"
        old = json.loads(path.read_text())
        del old["llm_hash"]
        path.write_text(json.dumps(old))
        stats, calls = self.run_scan(error=RuntimeError("LLM failed"))
        self.assertEqual(calls, 1)
        self.assertFalse(stats.succeeded)
        self.assertIsNone(self.store.get_extraction("123").llm_hash)
        stats, calls = self.run_scan()
        self.assertEqual(calls, 1)
        self.assertTrue(stats.succeeded)
        self.assertIsNotNone(self.store.get_extraction("123").llm_hash)
        self.assertEqual(self.run_scan()[1], 0)

    def test_changed_input_automatically_reextracts(self):
        self.run_scan(facts=rental(rent=None))
        changed = api_ad() | {"advert_description": "Changed"}
        stats, calls = self.run_scan(detail=changed)
        self.assertEqual(calls, 1)
        self.assertEqual(stats.excluded, 0)
        stats, calls = self.run_scan(detail=changed, reextract=True)
        self.assertEqual(calls, 1)
        self.assertEqual(len(stats.listings), 1)
        self.assertEqual(self.store.get_source("123").raw, changed)

    def test_failed_reextract_keeps_facts_but_blocks_delivery_and_export(self):
        self.run_scan()
        stats, _ = self.run_scan(reextract=True, error=RuntimeError("failed"))
        self.assertFalse(stats.succeeded)
        self.assertEqual(stats.listings, ())
        self.assertEqual(self.store.get_extraction("123").details, rental())
        self.assertEqual(json.loads((self.path / "listings.json").read_text()), [])
        stats, calls = self.run_scan(reextract=True)
        self.assertTrue(stats.succeeded)
        self.assertEqual(calls, 1)

    def test_known_source_is_refreshed_but_not_resent(self):
        stats, _ = self.run_scan()
        with patch("rentmont.service.send_ad", return_value=delivery()) as send:
            self.assertTrue(deliver(None, self.store, self.settings, stats.listings))
            changed = api_ad()
            stats, calls = self.run_scan(detail=changed)
            self.assertTrue(deliver(None, self.store, self.settings, stats.listings))
        self.assertEqual(send.call_count, 1)
        self.assertEqual(calls, 0)
        self.assertEqual(stats.listings[0].source.updated_at.isoformat(), "2026-10-14")
        self.assertEqual(self.store.get_notification("123").status, NotificationStatus.SENT)

    def test_detail_requested_every_scan(self):
        with patch("rentmont.service.search", return_value=[api_ad()]), \
             patch("rentmont.service.api_json", return_value={"result": api_ad()}) as request, \
             patch("rentmont.service.extract", return_value=rental()):
            scan(None, None, self.store, self.settings)
            self.assertEqual(request.call_count, 1)
            self.store = JsonStore(self.path)
            scan(None, None, self.store, self.settings)
            self.assertEqual(request.call_count, 2)
            scan(None, None, self.store, self.settings, reextract=True)
            self.assertEqual(request.call_count, 3)

    def test_failed_extraction_retries_with_fresh_source(self):
        self.run_scan(error=RuntimeError("LLM failed"))
        with patch("rentmont.service.search", return_value=[api_ad()]), \
             patch("rentmont.service.api_json", return_value={"result": api_ad()}) as request, \
             patch("rentmont.service.extract", return_value=rental()) as extract:
            stats = scan(None, None, self.store, self.settings)
        request.assert_called_once()
        extract.assert_called_once()
        self.assertTrue(stats.succeeded)
        self.assertEqual(len(stats.listings), 1)

    def test_delivery_retry(self):
        stats, _ = self.run_scan()
        with patch("rentmont.service.send_ad", side_effect=RuntimeError("timeout")):
            self.assertFalse(deliver(None, self.store, self.settings, stats.listings))
        self.assertIsNone(self.store.get_notification("123"))
        with patch("rentmont.service.send_ad", return_value=delivery()):
            self.assertTrue(deliver(None, self.store, self.settings, stats.listings))
        notification = self.store.get_notification("123")
        self.assertIsNotNone(notification.sent_at)
        self.assertEqual(notification.chat_id, delivery().chat_id)
        self.assertEqual(notification.message_ids, delivery().message_ids)
        self.assertEqual(notification.telegram_response, delivery().response)

    def test_baseline_and_missing(self):
        self.settings = config(self.path, notify_existing=False)
        self.run_scan()
        self.assertEqual(self.store.get_notification("123").status, NotificationStatus.SUPPRESSED)
        with patch("rentmont.service.search", return_value=[]):
            stats = scan(None, None, self.store, self.settings)
        self.assertEqual(stats.listings, ())
        self.assertEqual(self.store.current_search_ids, frozenset())
        self.assertIsNotNone(self.store.get_extraction("123"))

    def test_source_filter_saves_raw_without_llm(self):
        stats, calls = self.run_scan(detail=api_ad() | {"category_sub_cb": {"name": "Pokoj"}})
        self.assertEqual(calls, 0)
        self.assertEqual(stats.excluded, 1)
        self.assertIsNotNone(self.store.get_source("123"))
        self.assertIsNone(self.store.get_extraction("123"))

    def test_filter_recomputed_when_budget_changes(self):
        self.run_scan(facts=rental(rent=13000))
        self.settings = self.settings.model_copy(update={"max_rent_kc": 12500})
        stats, calls = self.run_scan()
        self.assertEqual(calls, 0)
        self.assertEqual(stats.excluded, 1)

    def test_total_limit_recomputed_on_saved_facts(self):
        self.run_scan(facts=rental(rent=12000, utilities=3000))
        self.settings = self.settings.model_copy(update={"max_rent_and_utilities_kc": 15000})
        stats, calls = self.run_scan()
        self.assertEqual(calls, 0)
        self.assertEqual(stats.excluded, 1)
        self.assertEqual(stats.listings, ())
        self.settings = self.settings.model_copy(update={"max_rent_and_utilities_kc": 15001})
        stats, calls = self.run_scan()
        self.assertEqual(calls, 0)
        self.assertEqual(len(stats.listings), 1)

    def test_failed_search_preserves_membership(self):
        self.run_scan()
        with patch("rentmont.service.search", side_effect=RuntimeError("network")):
            with self.assertRaises(RuntimeError):
                scan(None, None, self.store, self.settings)
        self.assertEqual(self.store.current_search_ids, frozenset({"123"}))
