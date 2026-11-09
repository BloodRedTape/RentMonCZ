package testutil

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"testing"

	"rentmont/internal/config"
	"rentmont/internal/domain"
	"rentmont/internal/jsonval"
	"rentmont/internal/sreality"
)

var PhotoBytes = []byte("\xff\xd8\xfftest-jpeg-bytes")

func JSON(t testing.TB, text string) map[string]any {
	t.Helper()
	value, err := jsonval.DecodeObject([]byte(text))
	if err != nil {
		t.Fatalf("decode fixture: %v", err)
	}
	return value
}

func Clone(t testing.TB, value map[string]any) map[string]any {
	t.Helper()
	data, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return JSON(t, string(data))
}

func APIAd(t testing.TB, ident int) map[string]any {
	return JSON(t, fmt.Sprintf(`{
		"hash_id": %d, "advert_name": "Pronajem bytu 1+kk", "advert_description": "Rent 12000 CZK",
		"category_main_cb": {"value": 1}, "category_type_cb": {"value": 2},
		"category_sub_cb": {"name": "1+kk"}, "price_czk": 12000,
		"locality": {"district_id": 72, "city_seo_name": "brno",
		             "gps_lat": 49.166982790872, "gps_lon": 16.5559570672062},
		"since": "2026-10-01", "edited": "2026-10-14"}`, ident))
}

func With(t testing.TB, base map[string]any, overrides map[string]any) map[string]any {
	result := Clone(t, base)
	for key, value := range overrides {
		result[key] = value
	}
	return result
}

func F(v float64) *float64 { return &v }

func S(v string) *string { return &v }

func Rental() domain.RentalDetails {
	return domain.RentalDetails{
		Payments: domain.Payments{RentKc: F(12000), UtilitiesKc: F(2000), DepositKc: F(24000)},
		AreaM2:   F(36),
	}
}

func Record(t testing.TB, photos ...string) domain.Listing {
	t.Helper()
	images := make([]any, len(photos))
	for i, photo := range photos {
		images[i] = photo
	}
	detail := APIAd(t, 123)
	detail["advert_images"] = images
	source, err := sreality.ToSourceListing(detail, 3)
	if err != nil {
		t.Fatal(err)
	}
	return domain.Listing{Source: source, Rental: Rental()}
}

func Delivery(t testing.TB) domain.TelegramDelivery {
	return domain.TelegramDelivery{
		Method: domain.SendMessage, ChatID: -100123, MessageIDs: []int64{42},
		Response: JSON(t, `{"ok": true, "result": {"message_id": 42, "chat": {"id": -100123},
			"date": 1791970000, "text": "notification"}}`),
	}
}

func Settings(dir string, notifyExisting bool) config.Settings {
	s := config.Default()
	s.OpenAIAPIKey, s.TelegramBotToken, s.TelegramChatID = "test", "test", "1"
	s.DataDir, s.NotifyExisting = dir, notifyExisting
	return s
}

type RoundTripFunc func(*http.Request) *http.Response

func (f RoundTripFunc) RoundTrip(req *http.Request) (*http.Response, error) { return f(req), nil }

func Client(f RoundTripFunc) *http.Client { return &http.Client{Transport: f} }

func Respond(status int, body string) *http.Response {
	return RespondBytes(status, []byte(body))
}

func RespondBytes(status int, body []byte) *http.Response {
	return &http.Response{
		StatusCode: status, Header: http.Header{}, ContentLength: int64(len(body)),
		Body: io.NopCloser(bytes.NewReader(body)),
	}
}
