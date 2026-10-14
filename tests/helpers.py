from pathlib import Path

from rentmont.config import Settings
from rentmont.models import Listing, Payments, RentalDetails
from rentmont.sreality import source_listing

PHOTO_BYTES = b"\xff\xd8\xfftest-jpeg-bytes"


def api_ad(ident=123):
    return {
        "hash_id": ident, "advert_name": "Pronajem bytu 1+kk", "advert_description": "Rent 12000 CZK",
        "category_main_cb": {"value": 1}, "category_type_cb": {"value": 2},
        "category_sub_cb": {"name": "1+kk"}, "price_czk": 12000,
        "locality": {"district_id": 72, "city_seo_name": "brno",
                     "gps_lat": 49.166982790872, "gps_lon": 16.5559570672062},
        "since": "2026-10-01", "edited": "2026-10-14",
    }


def rental(rent=12000, room=False, utilities=2000, commission=None, deposit=24000):
    return RentalDetails(
        payments=Payments(rent_kc=rent, utilities_kc=utilities,
                          commission_kc=commission, deposit_kc=deposit),
        is_room_rent=room, area_m2=36, available_from=None,
    )


def record(ident=123, photos=()):
    source = source_listing(api_ad(ident) | {"advert_images": list(photos)})
    return Listing(source=source, rental=rental())


def config(path: Path, notify_existing=True):
    return Settings(openai_api_key="test", telegram_bot_token="test", telegram_chat_id="1",
                    data_dir=path, notify_existing=notify_existing)
