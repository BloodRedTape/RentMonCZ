"""Validated payment, listing and persisted state contracts."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Payments(Model):
    rent_kc: float | None = Field(..., ge=0)
    utilities_kc: float | None = Field(..., ge=0)
    # Spelling preserved from the requested JSON contract.
    comission_kc: float | None = Field(..., ge=0)
    deposit_kc: float | None = Field(..., ge=0)


class Listing(Payments):
    link: HttpUrl


class StoredAd(Model):
    title: str
    listing: Listing
    notified: bool = Field(strict=True)


class Database(Model):
    schema_version: Literal[1] = 1
    initialized: bool = Field(strict=True)
    ads: dict[str, StoredAd]
