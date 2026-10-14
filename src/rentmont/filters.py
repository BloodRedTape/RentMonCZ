"""Pure filtering policy, separate from parsing and storage."""
from .models import ExclusionReason, RentalDetails, SourceListing


def source_rejection(source: SourceListing, district: int, ceiling: int) -> ExclusionReason | None:
    if (source.category_main != 1 or source.category_type != 2
            or source.district_id != district or source.api_price_kc is None
            or not 0 < source.api_price_kc < ceiling):
        return ExclusionReason.SOURCE_FILTER
    if source.is_room_category:
        return ExclusionReason.ROOM
    return None


def rental_rejection(rental: RentalDetails, ceiling: int,
                     total_ceiling: int | None = None) -> ExclusionReason | None:
    if rental.is_room_rent:
        return ExclusionReason.ROOM
    rent = rental.payments.rent_kc
    if rent is None:
        return ExclusionReason.UNKNOWN_RENT
    if not 0 < rent < ceiling:
        return ExclusionReason.RENT_LIMIT
    utilities = rental.payments.utilities_kc
    if total_ceiling is not None and utilities is not None and rent + utilities >= total_ceiling:
        return ExclusionReason.RENT_AND_UTILITIES_LIMIT
    return None
