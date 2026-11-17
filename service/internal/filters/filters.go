package filters

import (
	"slices"

	"rentmont/internal/domain"
)

func SourceRejection(source domain.SourceListing, district int, category domain.Category) domain.ExclusionReason {
	price := source.APIPriceKc
	if !is(source.CategoryMain, 1) || !is(source.CategoryType, 2) || !is(source.DistrictID, int64(district)) ||
		price == nil || !(*price > 0 && *price < float64(category.PriceCeiling())) {
		return domain.SourceFilter
	}
	if source.IsRoomCategory {
		return domain.Room
	}
	if len(category.Layouts) > 0 && (source.Layout == nil || !slices.Contains(category.Layouts, *source.Layout)) {
		return domain.LayoutFilter
	}
	return ""
}

func RentalRejection(rental domain.RentalDetails, category domain.Category) domain.ExclusionReason {
	if rental.IsRoomRent {
		return domain.Room
	}
	rent := rental.Payments.RentKc
	if rent == nil {
		return domain.UnknownRent
	}
	if *rent <= 0 {
		return domain.RentLimit
	}
	utilities := rental.Payments.UtilitiesKc
	total := category.MaxRentAndUtilitiesKc
	if total != nil && utilities != nil {
		if *rent+*utilities >= float64(*total) {
			return domain.RentAndUtilitiesLimit
		}
		return ""
	}
	if *rent >= float64(category.MaxRentKc) {
		return domain.RentLimit
	}
	return ""
}

func Tags(source domain.SourceListing, rental domain.RentalDetails, district int, categories []domain.Category) []string {
	tags := []string{}
	for _, category := range categories {
		if SourceRejection(source, district, category) == "" && RentalRejection(rental, category) == "" {
			tags = append(tags, category.Tag)
		}
	}
	return tags
}

func is(value *int64, expected int64) bool { return value != nil && *value == expected }
