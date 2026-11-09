package filters

import "rentmont/internal/domain"

func SourceRejection(source domain.SourceListing, district, ceiling int) domain.ExclusionReason {
	price := source.APIPriceKc
	if !is(source.CategoryMain, 1) || !is(source.CategoryType, 2) || !is(source.DistrictID, int64(district)) ||
		price == nil || !(*price > 0 && *price < float64(ceiling)) {
		return domain.SourceFilter
	}
	if source.IsRoomCategory {
		return domain.Room
	}
	return ""
}

func RentalRejection(rental domain.RentalDetails, ceiling int, totalCeiling *int) domain.ExclusionReason {
	if rental.IsRoomRent {
		return domain.Room
	}
	rent := rental.Payments.RentKc
	if rent == nil {
		return domain.UnknownRent
	}
	if !(*rent > 0 && *rent < float64(ceiling)) {
		return domain.RentLimit
	}
	utilities := rental.Payments.UtilitiesKc
	if totalCeiling != nil && utilities != nil && *rent+*utilities >= float64(*totalCeiling) {
		return domain.RentAndUtilitiesLimit
	}
	return ""
}

func is(value *int64, expected int64) bool { return value != nil && *value == expected }
