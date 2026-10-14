"""Sreality search, filters and listing links."""
import unicodedata

import httpx

API = "https://www.sreality.cz/api/v1"


def api_json(client: httpx.Client, path: str, **params) -> dict:
    response = client.get(f"{API}/{path}", params=params)
    response.raise_for_status()
    data = response.json()
    if data.get("status_code", 200) != 200:
        raise RuntimeError("Sreality returned a non-success status")
    return data


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


