"""Validated payment, listing and persisted state contracts."""
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Payments(Model):
    rent_kc: float | None = Field(..., ge=0)
    utilities_kc: float | None = Field(..., ge=0)
    # Spelling preserved from the requested JSON contract.
    comission_kc: float | None = Field(..., ge=0)
    deposit_kc: float | None = Field(..., ge=0)

    @field_validator("rent_kc", "utilities_kc", "comission_kc", "deposit_kc")
    @classmethod
    def normalize_amount(cls, value: float | None) -> float | None:
        # Contract: zero amounts are represented as null in every payment field.
        return None if value == 0 else value


class RentalDetails(Payments):
    is_room_rent: bool = Field(..., strict=True)
    area_m2: float | None = Field(..., gt=0)
    available_from: date | Literal["asap"] | None = Field(...)

    @field_validator("available_from", mode="before")
    @classmethod
    def validate_availability(cls, value):
        if value is None or value == "asap" or type(value) is date:
            return value
        if isinstance(value, str):
            # Require an actual ISO calendar date, not a timestamp or partial date.
            parsed = date.fromisoformat(value)
            if parsed.isoformat() == value:
                return parsed
        raise ValueError("Expected null, 'asap' or a date in YYYY-MM-DD format")


class Listing(RentalDetails):
    # Defaults keep older db.json records readable without another LLM call.
    is_room_rent: bool = Field(default=False, strict=True)
    area_m2: float | None = Field(default=None, gt=0)
    available_from: date | Literal["asap"] | None = None
    link: HttpUrl


class StoredAd(Model):
    title: str
    listing: Listing
    notified: bool = Field(strict=True)


class Database(Model):
    schema_version: Literal[1] = 1
    initialized: bool = Field(strict=True)
    ads: dict[str, StoredAd]
