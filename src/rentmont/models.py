"""Domain contracts: source metadata, rental facts and monitoring state."""
from datetime import date, datetime
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)


class Payments(Model):
    rent_kc: float | None = Field(..., ge=0)
    utilities_kc: float | None = Field(..., ge=0)
    commission_kc: float | None = Field(..., ge=0)
    deposit_kc: float | None = Field(..., ge=0)

    @field_validator("rent_kc", "utilities_kc", "commission_kc", "deposit_kc")
    @classmethod
    def normalize_amount(cls, value: float | None) -> float | None:
        return None if value == 0 else value


class RentalDetails(Model):
    """Only facts extracted by the LLM; no API metadata or delivery flags."""
    payments: Payments
    is_room_rent: bool = Field(..., strict=True)
    area_m2: float | None = Field(..., gt=0)
    available_from: date | Literal["asap"] | None = Field(...)

    @field_validator("available_from", mode="before")
    @classmethod
    def validate_availability(cls, value):
        if value is None or value == "asap" or type(value) is date:
            return value
        if isinstance(value, str):
            parsed = date.fromisoformat(value)
            if parsed.isoformat() == value:
                return parsed
        raise ValueError("Expected null, 'asap' or YYYY-MM-DD")


class SourceListing(Model):
    """Metadata read from Sreality, refreshed independently of LLM parsing."""
    id: str = Field(min_length=1)
    title: str
    link: HttpUrl
    category_main: int | None
    category_type: int | None
    district_id: int | None
    api_price_kc: float | None
    is_room_category: bool
    locality_name: str | None = None
    map_link: HttpUrl | None = None
    distance_to_center_km: float | None = Field(..., ge=0)
    published_at: date | None
    updated_at: date | None
    photos: tuple[HttpUrl, ...] = Field(max_length=10)


class ExclusionReason(str, Enum):
    SOURCE_FILTER = "source_filter"
    ROOM = "room"
    UNKNOWN_RENT = "unknown_rent"
    RENT_LIMIT = "rent_limit"
    RENT_AND_UTILITIES_LIMIT = "rent_and_utilities_limit"


class NotificationStatus(str, Enum):
    SENT = "sent"
    SUPPRESSED = "suppressed"


class Listing(Model):
    """Public projection for export and Telegram, without internal state."""
    source: SourceListing
    rental: RentalDetails


class SourceRecord(Model):
    raw: dict
    first_seen_at: datetime
    last_seen_at: datetime

    @model_validator(mode="after")
    def validate_id(self):
        if not self.raw.get("hash_id"):
            raise ValueError("Raw listing must have a hash_id")
        return self


class HashedRecord(Model):
    global_hash: str | None = None
    llm_hash: str | None = None

    @model_validator(mode="before")
    @classmethod
    def discard_obsolete_hash(cls, value):
        if isinstance(value, dict) and "content_hash" in value:
            return {key: item for key, item in value.items() if key != "content_hash"}
        return value


class ExtractionRecord(HashedRecord):
    details: RentalDetails
    extracted_at: datetime


class TelegramDelivery(Model):
    method: Literal["sendMessage", "sendPhoto", "sendMediaGroup"]
    chat_id: int = Field(strict=True)
    message_ids: tuple[Annotated[int, Field(strict=True, gt=0)], ...] = Field(min_length=1)
    response: dict


class NotificationRecord(HashedRecord):
    status: NotificationStatus
    sent_at: datetime | None = None
    chat_id: int | None = Field(default=None, strict=True)
    message_ids: tuple[Annotated[int, Field(strict=True, gt=0)], ...] = ()
    telegram_method: Literal["sendMessage", "sendPhoto", "sendMediaGroup"] | None = None
    telegram_response: dict | None = None
    gone_at: datetime | None = None
    gone_notified: bool = False

    @model_validator(mode="after")
    def validate_sent_at(self):
        if (self.status == NotificationStatus.SENT) != (self.sent_at is not None):
            raise ValueError("Only sent notifications must have sent_at")
        metadata = (self.chat_id is not None, bool(self.message_ids),
                    self.telegram_method is not None, self.telegram_response is not None)
        if any(metadata) and (not all(metadata) or self.status != NotificationStatus.SENT):
            raise ValueError("Delivery metadata must be complete and only belongs to sent notifications")
        return self


class MonitorState(Model):
    schema_version: Literal[4] = 4
    initialized: bool = Field(default=False, strict=True)
    current_search_ids: frozenset[str] = frozenset()


class ScanStats(Model):
    search_results: int
    llm_calls: int = 0
    cache_hits: int = 0
    excluded: int = 0
    errors: int = 0
    listings: tuple[Listing, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.errors == 0
