import unittest

from rentmont.models import Listing
from rentmont.sreality import source_listing
from rentmont.telegram import format_message
from tests.helpers import api_ad, rental


class TelegramLocationTests(unittest.TestCase):
    def render(self, locality):
        ad = api_ad()
        ad["locality"] = locality
        return format_message(Listing(source=source_listing(ad), rental=rental()))

    def test_clickable_district_and_distance(self):
        locality = api_ad()["locality"] | {"citypart": "Černá Pole"}
        text = self.render(locality)
        self.assertIn('📍 Loc: <a href="https://www.google.com/maps/search/?api=1&amp;query=49.166982790872,16.5559570672062">Černá Pole</a> ≈4.9 km to center', text)
        self.assertNotIn("Km to Center:", text)

    def test_missing_coordinates_and_escaped_name(self):
        text = self.render({"citypart": "<Pole & okolí>"})
        self.assertIn("📍 Loc: &lt;Pole &amp; okolí&gt; расстояние не указано", text)
        self.assertNotIn("google.com", text)

    def test_invalid_coordinates_and_fallback_name(self):
        text = self.render({"quarter": "Brno-sever", "gps_lat": 100, "gps_lon": 16})
        self.assertIn("📍 Loc: Brno-sever расстояние не указано", text)
        self.assertNotIn("google.com", text)

    def test_missing_locality(self):
        self.assertIn("📍 Loc: не указано расстояние не указано", self.render({}))
