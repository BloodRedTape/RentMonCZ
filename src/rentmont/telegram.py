"""Telegram formatting and delivery; persistence belongs to the caller."""
from html import escape
import json
import logging
import re

import httpx

from .models import Listing, NotificationRecord, TelegramDelivery
from .sreality import photo_download_url

LOG = logging.getLogger(__name__)
MAX_PHOTO_BYTES = 10 * 1024 * 1024


def format_message(listing: Listing) -> str:
    """Render a domain projection; no cache or notification state is needed."""
    source, rental = listing.source, listing.rental
    payments = rental.payments

    def money(value: float | None) -> str:
        return "не указано" if value is None else f"{value:,.0f} Kč"

    def money_pair(first: float | None, second: float | None) -> str:
        result = f"{money(first)} + {money(second)}"
        if first is not None and second is not None:
            result += f" = {money(first + second)}"
        return result

    def calendar(value) -> str:
        return "не указано" if value is None else value.strftime("%d.%m.%Y")

    area = "не указано" if rental.area_m2 is None else f"{rental.area_m2:g} м²"
    title = escape(" ".join(source.title.split())[:180])
    link = escape(str(source.link), quote=True)
    availability = rental.available_from
    move_in = "как можно скорее" if availability == "asap" else calendar(availability)
    distance = ("не указано" if source.distance_to_center_km is None
                else f"≈{source.distance_to_center_km:.1f} км по прямой")
    return "\n".join([
        f'🏠 <a href="{link}">{title}</a> — {area}',
        f"💰 Price: {money_pair(payments.rent_kc, payments.utilities_kc)}",
        f"🔐 Commission/Deposit: {money(payments.commission_kc)} + {money(payments.deposit_kc)}",
        f"🔑 Move In: {move_in}",
        f"📍 Km to Center: {distance}",
        f"🕒 Uploaded {calendar(source.published_at)} Updated {calendar(source.updated_at)}",
    ])


class TelegramRejected(RuntimeError):
    def __init__(self, code: int, description: str = "No description returned"):
        super().__init__("Telegram rejected message")
        self.code = code
        self.description = description


def safe_description(description: str, token: str) -> str:
    # Telegram error descriptions normally contain no credentials, but redact
    # the token and URLs defensively; avoid multiline/unbounded log output.
    text = description.replace(token, "[redacted]") if token else description
    text = re.sub(r"https?://\S+", "[url]", text)
    return " ".join(text.split())[:500]


def download_photo(client: httpx.Client, url: str) -> tuple[str, bytes, str]:
    """Download bounded JPEG/PNG bytes; Telegram must not fetch the CDN URL."""
    url = photo_download_url(url)
    with client.stream("GET", url, follow_redirects=True, timeout=30) as response:
        response.raise_for_status()
        length = response.headers.get("content-length")
        if length and int(length) > MAX_PHOTO_BYTES:
            raise ValueError("Photo exceeds 10 MB")
        content = bytearray()
        for chunk in response.iter_bytes():
            if len(content) + len(chunk) > MAX_PHOTO_BYTES:
                raise ValueError("Photo exceeds 10 MB")
            content.extend(chunk)
    # Check signatures rather than trusting MIME headers or a .jpeg URL.
    if content.startswith(b"\xff\xd8\xff"):
        return "photo.jpg", bytes(content), "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "photo.png", bytes(content), "image/png"
    raise ValueError("Downloaded content is not a JPEG or PNG")


def post_message(client: httpx.Client, token: str, method: str, payload: dict,
                 files: dict[str, tuple[str, bytes, str]] | None = None) -> TelegramDelivery | dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    if files:
        response = client.post(url, data=payload, files=files, timeout=90)
    else:
        response = client.post(url, json=payload)
    if response.status_code == 400:
        try:
            result = response.json()
        except ValueError:
            result = {}
        raise TelegramRejected(400, str(result.get("description", "No description returned")))
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise TelegramRejected(result.get("error_code", 0),
                               str(result.get("description", "No description returned")))
    if method in ("editMessageText", "editMessageCaption"):
        return result
    messages = result["result"] if method == "sendMediaGroup" else [result["result"]]
    if not messages:
        raise ValueError("Telegram returned no messages")
    chat_id = messages[0]["chat"]["id"]
    if any(message["chat"]["id"] != chat_id for message in messages):
        raise ValueError("Telegram returned messages from different chats")
    return TelegramDelivery(method=method, chat_id=chat_id,
                            message_ids=tuple(message["message_id"] for message in messages),
                            response=result)


def edit_ad(client: httpx.Client, token: str, record: NotificationRecord,
            listing: Listing) -> dict:
    is_text = record.telegram_method == "sendMessage"
    field = "text" if is_text else "caption"
    text = format_message(listing)
    payload = {"chat_id": record.chat_id, "message_id": record.message_ids[0],
               field: text, "parse_mode": "HTML"}
    if is_text:
        payload["link_preview_options"] = {"is_disabled": True}
    try:
        response = post_message(client, token,
                                "editMessageText" if is_text else "editMessageCaption", payload)
    except TelegramRejected as exc:
        if exc.code != 400 or "message is not modified" not in exc.description.lower():
            raise
        # Dates or price notes may change without changing the rendered message.
        # Preserve its original plain text/entities for a future Gone edit.
        return record.telegram_response
    # For albums only the first message carries the caption and is edited.
    # Keeping that message is sufficient for subsequent edits, including Gone.
    return response


def mark_ad_gone(client: httpx.Client, token: str, record: NotificationRecord) -> None:
    """Edit the original text/caption, keeping its Telegram formatting entities."""
    if record.gone_at is None or not record.telegram_response or not record.message_ids:
        raise ValueError("Missing original delivery metadata")
    messages = record.telegram_response["result"]
    original = messages[0] if isinstance(messages, list) else messages
    is_text = record.telegram_method == "sendMessage"
    field = "text" if is_text else "caption"
    entities = "entities" if is_text else "caption_entities"
    payload = {"chat_id": record.chat_id, "message_id": record.message_ids[0],
               field: original[field] + f"\n🔴 Gone {record.gone_at:%d.%m.%Y}"}
    if entities in original:
        payload[entities] = original[entities]
    if is_text:
        payload["link_preview_options"] = {"is_disabled": True}
    try:
        post_message(client, token, "editMessageText" if is_text else "editMessageCaption", payload)
    except TelegramRejected as exc:
        # A previous edit may have succeeded before a timeout or process crash.
        if exc.code != 400 or "message is not modified" not in exc.description.lower():
            raise


def send_ad(client: httpx.Client, token: str, chat: str, listing: Listing) -> TelegramDelivery:
    text = format_message(listing)
    photos = []
    for index, url in enumerate(listing.source.photos, start=1):
        try:
            photos.append(download_photo(client, str(url)))
        except httpx.HTTPStatusError as exc:
            LOG.warning("Photo #%d download failed (HTTP %d); skipping",
                        index, exc.response.status_code)
        except (httpx.HTTPError, ValueError) as exc:
            LOG.warning("Photo #%d download failed (%s); skipping", index, type(exc).__name__)
    if photos:
        # Keep HTML intact: slicing may break tags/entities. The compact format
        # and capped title keep normal listings within the caption limit.
        caption = text
        if len(photos) == 1:
            method = "sendPhoto"
            payload = {"chat_id": chat, "caption": caption, "parse_mode": "HTML"}
            files = {"photo": photos[0]}
        else:
            method = "sendMediaGroup"
            files = {f"photo{index}": photo for index, photo in enumerate(photos)}
            media = [{"type": "photo", "media": f"attach://{name}"} for name in files]
            media[0]["caption"] = caption
            media[0]["parse_mode"] = "HTML"
            payload = {"chat_id": chat, "media": json.dumps(media, ensure_ascii=False)}
        try:
            return post_message(client, token, method, payload, files=files)
        except TelegramRejected as exc:
            # Only explicit rejection is safe to fall back from. A timeout may
            # mean Telegram accepted the album: retain the queue in that case.
            if exc.code != 400:
                raise
            LOG.warning("Telegram %s rejected %d photo(s): %s; sending text instead",
                        method, len(photos), safe_description(exc.description, token))
    return post_message(client, token, "sendMessage",
                        {"chat_id": chat, "text": text, "parse_mode": "HTML",
                         "link_preview_options": {"is_disabled": True}})


