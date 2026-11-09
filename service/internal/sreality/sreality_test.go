package sreality_test

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"testing"

	"rentmont/internal/domain"
	. "rentmont/internal/sreality"
	"rentmont/internal/testutil"
)

func TestSearchPagination(t *testing.T) {
	var offsets []string
	client := &Client{BaseURL: DefaultBaseURL, HTTP: testutil.Client(func(req *http.Request) *http.Response {
		offset := req.URL.Query().Get("offset")
		offsets = append(offsets, offset)
		ad, _ := json.Marshal(testutil.APIAd(t, len(offsets)))
		return testutil.Respond(200, fmt.Sprintf(`{"results":[%s],"pagination":{"total":2}}`, ad))
	})}
	ads, err := client.Search(context.Background(), 72, 13500)
	if err != nil || len(ads) != 2 {
		t.Fatalf("ads=%d err=%v", len(ads), err)
	}
	if fmt.Sprint(offsets) != "[0 1]" {
		t.Errorf("offsets %v", offsets)
	}
}

func TestListingGoneOnlyWhenConfirmed(t *testing.T) {
	for status, expected := range map[int]bool{200: false, 404: true, 410: true} {
		client := &Client{BaseURL: DefaultBaseURL, HTTP: testutil.Client(func(*http.Request) *http.Response {
			return testutil.Respond(status, `{"result":{"hash_id":123}}`)
		})}
		gone, err := client.ListingGone(context.Background(), "123")
		if err != nil || gone != expected {
			t.Errorf("status %d: gone=%v err=%v", status, gone, err)
		}
	}
	for _, status := range []int{403, 429, 500} {
		client := &Client{BaseURL: DefaultBaseURL, HTTP: testutil.Client(func(*http.Request) *http.Response {
			return testutil.Respond(status, `{}`)
		})}
		if _, err := client.ListingGone(context.Background(), "123"); err == nil {
			t.Errorf("status %d must be an error", status)
		}
	}
}

func TestDatesAndPhotos(t *testing.T) {
	published, _ := ListingDates(testutil.APIAd(t, 123))
	if published == nil || *published != "2026-10-01" {
		t.Errorf("published %v", published)
	}
	for _, value := range []any{nil, "invalid", "2026-02-30", "20261001", json.Number("0")} {
		if p, _ := ListingDates(map[string]any{"since": value}); p != nil {
			t.Errorf("date %v accepted", value)
		}
	}
	photos, _ := ListingPhotos(map[string]any{"advert_images": []any{nil, "bad", "//example.com/1.jpg",
		"https://example.com/1.jpg", map[string]any{"url": "https://user:pw@example.com/2.jpg"}}}, 3)
	if len(photos) != 1 {
		t.Errorf("photos %v", photos)
	}
	u, _ := url.Parse(PhotoDownloadURL("https://d18-a.sdn.cz/d_18/photo.jpeg"))
	if u.Query().Get("fl") != "res,1200,1200,1|shr,,20|jpg,80" {
		t.Errorf("sdn url %s", u)
	}
	if PhotoDownloadURL("https://example.com/p.jpg") != "https://example.com/p.jpg" {
		t.Error("non-SDN URL changed")
	}
	if _, err := ListingPhotos(map[string]any{}, 11); err == nil {
		t.Error("photo limit 11 accepted")
	}
}

func TestFurnishingCodes(t *testing.T) {
	codes := map[any]domain.Furnishing{
		json.Number("1"): domain.Furnished, "1": domain.Furnished,
		json.Number("2"): domain.Unfurnished, "3": domain.PartiallyFurnished,
	}
	for value, expected := range codes {
		got := ListingFurnishing(map[string]any{"furnished": map[string]any{"value": value}})
		if got == nil || *got != expected {
			t.Errorf("code %v: %v", value, got)
		}
	}
	for _, value := range []any{nil, json.Number("0"), "Ne", []any{},
		map[string]any{"value": json.Number("0")}, map[string]any{"value": json.Number("99")},
		map[string]any{"value": true}, map[string]any{"value": json.Number("1.0")}, map[string]any{"name": "Ne"}} {
		if got := ListingFurnishing(map[string]any{"furnished": value}); got != nil {
			t.Errorf("%v must be unspecified", value)
		}
	}
}

func TestSourceListingLocationAndLink(t *testing.T) {
	ad := testutil.APIAd(t, 123)
	ad["category_sub_cb"] = map[string]any{"name": "Atypický"}
	source, err := ToSourceListing(ad, 3)
	if err != nil {
		t.Fatal(err)
	}
	if source.Link != "https://www.sreality.cz/detail/pronajem/byt/atypicky/brno/123" {
		t.Errorf("link %s", source.Link)
	}
	if *source.MapLink != "https://www.google.com/maps/search/?api=1&query=49.166982790872,16.5559570672062" {
		t.Errorf("map link %s", *source.MapLink)
	}
	if *source.DistanceToCenterKm != 4.9 {
		t.Errorf("distance %v", *source.DistanceToCenterKm)
	}
	ad["locality"] = map[string]any{"quarter": "Brno-sever", "gps_lat": json.Number("100"), "gps_lon": json.Number("16")}
	source, _ = ToSourceListing(ad, 3)
	if source.MapLink != nil || source.DistanceToCenterKm != nil || *source.LocalityName != "Brno-sever" {
		t.Errorf("invalid coordinates must be ignored: %+v", source)
	}
}
