package filters

import (
	"testing"

	"rentmont/internal/domain"
	"rentmont/internal/testutil"
)

func TestFilters(t *testing.T) {
	if r := SourceRejection(testutil.Record(t).Source, 72, 13500); r != "" {
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
		{rental(testutil.F(12000), testutil.F(99999), false), nil, ""},
	}
	for i, c := range cases {
		if got := RentalRejection(c.rental, 13500, c.total); got != c.expected {
			t.Errorf("case %d: got %q, want %q", i, got, c.expected)
		}
	}
}
