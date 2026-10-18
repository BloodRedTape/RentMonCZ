"""Operational events are visible without logging listing content or credentials."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rentmont.service import deliver, scan
from rentmont.storage import JsonStore
from tests.helpers import api_ad, config, delivery, rental


class LoggingTests(unittest.TestCase):
    @patch("rentmont.service.time.sleep")
    def test_discovery_once_and_successful_send(self, sleep):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = JsonStore(root)
            settings = config(root)
            detail = api_ad() | {"advert_description": "PRIVATE_DESCRIPTION",
                                 "user_email": "PRIVATE_EMAIL"}
            with self.assertLogs("rentmont", level="DEBUG") as logs, \
                 patch("rentmont.service.search", return_value=[detail]), \
                 patch("rentmont.service.api_json", return_value={"result": detail}), \
                 patch("rentmont.service.extract", return_value=rental()), \
                 patch("rentmont.service.send_ad", return_value=delivery()):
                first = scan(None, None, store, settings)
                deliver(None, store, settings, first.listings)
                second = scan(None, None, store, settings)
                deliver(None, store, settings, second.listings)
            text = "\n".join(logs.output)
            self.assertEqual(text.count("New listing discovered: 123"), 1)
            self.assertEqual(text.count("Notification sent for listing 123"), 1)
            self.assertIn("extraction cache hit", text)
            self.assertIn("Scan complete:", text)
            self.assertIn("Delivery complete:", text)
            self.assertIn("Exported 1 listings", text)
            self.assertNotIn("PRIVATE_DESCRIPTION", text)
            self.assertNotIn("PRIVATE_EMAIL", text)

    @patch("rentmont.service.time.sleep")
    def test_suppression_and_rejection_reason(self, sleep):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = JsonStore(root)
            settings = config(root, notify_existing=False)
            with self.assertLogs("rentmont.service", level="DEBUG") as logs, \
                 patch("rentmont.service.search", return_value=[api_ad()]), \
                 patch("rentmont.service.api_json", return_value={"result": api_ad()}), \
                 patch("rentmont.service.extract", return_value=rental()):
                scan(None, None, store, settings)
                settings = settings.model_copy(update={"max_rent_and_utilities_kc": 13000})
                scan(None, None, store, settings)
            text = "\n".join(logs.output)
            self.assertIn("Initial notification suppressed for listing 123", text)
            self.assertIn("excluded by rental filter: rent_and_utilities_limit", text)
