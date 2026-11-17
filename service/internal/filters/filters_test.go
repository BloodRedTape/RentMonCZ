package filters

import (
	"reflect"
	"testing"

	"rentmont/internal/domain"
	"rentmont/internal/testutil"
)

func TestFilters(t *testing.T) {
	solo := domain.Category{Tag: "solo_cheap", MaxRentKc: 13500}
	if r := SourceRejection(testutil.Record(t).Source, 72, solo); r != "" {
		t.Errorf("source rejected: %s", r)
	}
	rental := func(rent, utilities *float64, room bool) domain.RentalDetails {
		r := testutil.Rental()
		r.Payments.RentKc, r.Payments.UtilitiesKc, r.IsRoomRent = rent, utilities, room
		return r
	}
	limit := 14000
	cases := []struct {
		rental   domain.RentalDetails
		total    *int
		expected domain.ExclusionReason
	}{
		{testutil.Rental(), nil, ""},
		{rental(testutil.F(12000), testutil.F(2000), true), nil, domain.Room},
		{rental(nil, testutil.F(2000), false), nil, domain.UnknownRent},
		{rental(nil, testutil.F(2000), false), &limit, domain.UnknownRent},
		{rental(testutil.F(13500), nil, false), nil, domain.RentLimit},
		{rental(testutil.F(12000), testutil.F(1999), false), &limit, ""},
		{rental(testutil.F(12000), testutil.F(2000), false), &limit, domain.RentAndUtilitiesLimit},
		{rental(testutil.F(12000), nil, false), &limit, ""},
		{rental(testutil.F(13600), testutil.F(300), false), &limit, ""},
		{rental(testutil.F(13600), nil, false), &limit, domain.RentLimit},
		{rental(testutil.F(13600), testutil.F(300), false), nil, domain.RentLimit},
		{rental(testutil.F(12000), testutil.F(99999), false), nil, ""},
	}
	for i, c := range cases {
		category := domain.Category{MaxRentKc: 13500, MaxRentAndUtilitiesKc: c.total}
		if got := RentalRejection(c.rental, category); got != c.expected {
			t.Errorf("case %d: got %q, want %q", i, got, c.expected)
		}
	}
}

func TestLayoutsAndTags(t *testing.T) {
	total := 25001
	solo := domain.Category{Tag: "solo_cheap", MaxRentKc: 13500}
	duo := domain.Category{Tag: "duo_cheap", MaxRentKc: 20001, MaxRentAndUtilitiesKc: &total, Layouts: []int64{5, 6}}
	source := testutil.Record(t).Source
	if r := SourceRejection(source, 72, duo); r != domain.LayoutFilter {
		t.Errorf("unknown layout: got %q", r)
	}
	layout := int64(4)
	source.Layout = &layout
	if r := SourceRejection(source, 72, duo); r != domain.LayoutFilter {
		t.Errorf("2+kk: got %q", r)
	}
	layout = 6
	categories := []domain.Category{solo, duo}
	if tags := Tags(source, testutil.Rental(), 72, categories); !reflect.DeepEqual(tags, []string{"solo_cheap", "duo_cheap"}) {
		t.Errorf("tags %v", tags)
	}
	r := testutil.Rental()
	r.Payments.RentKc, r.Payments.UtilitiesKc = testutil.F(20000), testutil.F(5000)
	price := 20000.0
	source.APIPriceKc = &price
	if tags := Tags(source, r, 72, categories); !reflect.DeepEqual(tags, []string{"duo_cheap"}) {
		t.Errorf("tags %v", tags)
	}
	r.Payments.UtilitiesKc = testutil.F(5001)
	if tags := Tags(source, r, 72, categories); len(tags) != 0 {
		t.Errorf("tags %v", tags)
	}
	r.Payments.RentKc, r.Payments.UtilitiesKc = testutil.F(21000), testutil.F(3500)
	if RentalRejection(r, duo) != "" {
		t.Error("21000 + 3500 must pass the duo total limit")
	}
	r.Payments.UtilitiesKc = nil
	if RentalRejection(r, duo) != domain.RentLimit {
		t.Error("21000 with unknown utilities must hit the duo rent limit")
	}
	price = 25000
	if r := SourceRejection(source, 72, duo); r != "" {
		t.Errorf("API price including utilities must pass: got %q", r)
	}
	price = 25001
	if r := SourceRejection(source, 72, duo); r != domain.SourceFilter {
		t.Errorf("API price over the total limit: got %q", r)
	}
	total17 := 17000
	soloTotal := domain.Category{MaxRentKc: 13500, MaxRentAndUtilitiesKc: &total17}
	price = 16999
	if r := SourceRejection(source, 72, soloTotal); r != "" {
		t.Errorf("solo with total limit: got %q", r)
	}
	if r := SourceRejection(source, 72, solo); r != domain.SourceFilter {
		t.Errorf("solo without total limit: got %q", r)
	}
}
