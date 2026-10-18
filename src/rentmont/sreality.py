"""Sreality search, filters and listing links."""
from datetime import date
import unicodedata

import httpx
from pydantic import HttpUrl, ValidationError

from .location import distance_to_center_km
from .models import SourceListing

API = "https://www.sreality.cz/api/v1"


def api_json(client: httpx.Client, path: str, **params) -> dict:
    response = client.get(f"{API}/{path}", params=params)
    response.raise_for_status()
    data = response.json()
    if data.get("status_code", 200) != 200:
        raise RuntimeError("Sreality returned a non-success status")
    return data


def listing_gone(client: httpx.Client, ident: str) -> bool:
    """Only explicit unavailability is evidence; search absence alone is not."""
    response = client.get(f"{API}/estates/{ident}")
    if response.status_code in (404, 410):
        return True
    response.raise_for_status()
    data = response.json()
    status = data.get("status_code", 200)
    if status in (404, 410):
        return True
    if status != 200:
        raise RuntimeError("Sreality returned a non-success status")
    if str(data["result"]["hash_id"]) != ident:
        raise ValueError("Sreality detail ID does not match the requested listing")
    return False


def photo_download_url(url: str) -> str:
    """Use the same image rendition as Sreality's listing page.

    Raw SDN URLs returned by the estate API can respond with HTTP 401 unless
    the image transformation parameter is supplied. Preserve existing options.
    """
    parsed = httpx.URL(url)
    host = parsed.host.lower()
    if (host == "sdn.cz" or host.endswith(".sdn.cz")) and "fl" not in parsed.params:
        return str(parsed.copy_merge_params({"fl": "res,1200,1200,1|shr,,20|jpg,80"}))
    return url


def listing_photos(detail: dict) -> list[HttpUrl]:
    """Take up to three unique valid image URLs in the API's original order."""
    photos = []
    seen = set()
    images = detail.get("advert_images") or []
    if not isinstance(images, list):
        return photos
    for image in images:
        url = image.get("url") if isinstance(image, dict) else image
        if not isinstance(url, str):
            continue
        if url.startswith("//"):
            url = "https:" + url
        try:
            parsed = HttpUrl(url)
            parsed = HttpUrl(photo_download_url(str(parsed)))
        except ValidationError:
            continue
        if parsed.username or parsed.password or str(parsed) in seen:
            continue
        seen.add(str(parsed))
        photos.append(parsed)
        if len(photos) == 3:
            break
    return photos


def listing_dates(detail: dict) -> dict[str, date | None]:
    """Read API dates, never substitute today's date or the move-in date."""
    dates = {}
    for source, target in (("since", "published_at"), ("edited", "updated_at")):
        value = detail.get(source)
        parsed = None
        if isinstance(value, str):
            try:
                candidate = date.fromisoformat(value)
                if candidate.isoformat() == value:
                    parsed = candidate
            except ValueError:
                pass
        dates[target] = parsed
    return dates


def is_room_category(detail: dict) -> bool:
    category = detail.get("category_sub_cb") or {}
    return str(category.get("name", "")).strip().casefold() in {"pokoj", "pokoje"}


def source_listing(detail: dict) -> SourceListing:
    """Boundary adapter: external dictionaries become validated domain data."""
    locality = detail.get("locality") or {}
    return SourceListing(
        id=str(detail["hash_id"]), title=detail["advert_name"], link=listing_link(detail),
        category_main=(detail.get("category_main_cb") or {}).get("value"),
        category_type=(detail.get("category_type_cb") or {}).get("value"),
        district_id=locality.get("district_id"), api_price_kc=detail.get("price_czk"),
        is_room_category=is_room_category(detail),
        distance_to_center_km=distance_to_center_km(locality),
        photos=listing_photos(detail), **listing_dates(detail),
    )


def matches(ad: dict, district: int, ceiling: int) -> bool:
    price = ad.get("price_czk")
    return (
        ad.get("category_main_cb", {}).get("value") == 1
        and ad.get("category_type_cb", {}).get("value") == 2
        and ad.get("locality", {}).get("district_id") == district
        and isinstance(price, (int, float)) and 0 < price < ceiling
    )


def search(client: httpx.Client, district: int, ceiling: int) -> list[dict]:
    ads: dict[str, dict] = {}
    offset = 0
    while True:
        data = api_json(client, "estates/search", category_main_cb=1,
                        category_type_cb=2, locality_district_id=district,
                        price_to=ceiling - 1, limit=100, offset=offset)
        rows = data["results"]
        total = int(data["pagination"]["total"])
        if not rows and offset < total:
            raise RuntimeError("Unexpected empty search page")
        for ad in rows:
            if matches(ad, district, ceiling):
                ads[str(ad["hash_id"])] = ad
        offset += len(rows)
        if offset >= total:
            return list(ads.values())


def listing_link(ad: dict) -> str:
    locality = ad.get("locality", {})
    place = "-".join(filter(None, (locality.get("city_seo_name"),
                                     locality.get("citypart_seo_name"),
                                     locality.get("street_seo_name")))) or "-"
    layout = ad.get("category_sub_cb", {}).get("name", "-").lower()
    layout = unicodedata.normalize("NFKD", layout).encode("ascii", "ignore").decode()
    layout = layout.replace(" ", "-")
    return f"https://www.sreality.cz/detail/pronajem/byt/{layout}/{place}/{ad['hash_id']}"


