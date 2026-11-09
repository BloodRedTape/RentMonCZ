package telegram

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"rentmont/internal/domain"
	"rentmont/internal/netutil"
	"rentmont/internal/sreality"
	"rentmont/internal/testutil"
)

func TestFormat(t *testing.T) {
	listing := testutil.Record(t)
	listing.Source.Title = "Flat <nice> & bright"
	text := FormatMessage(listing)
	for _, expected := range []string{
		"Flat &lt;nice&gt; &amp; bright", "12,000 Kč + 2,000 Kč = 14,000 Kč", "— 36 м²",
		"🛋 Furnished: не указано\n", "🕒 Uploaded 01.10.2026 Updated 14.10.2026",
		`📍 Loc: <a href="https://www.google.com/maps/search/?api=1&amp;query=49.166982790872,16.5559570672062">не указано</a> ≈4.9 km to center`,
	} {
		if !strings.Contains(text, expected) {
			t.Errorf("missing %q in\n%s", expected, text)
		}
	}
	if len(strings.Split(text, "\n")) != 8 {
		t.Error("expected 8 lines")
	}
	for value, expected := range map[string]string{"": "не указано", "asap": "как можно скорее", "2026-12-01": "01.12.2026"} {
		listing.Rental.AvailableFrom = nil
		if value != "" {
			listing.Rental.AvailableFrom = testutil.S(value)
		}
		if !strings.Contains(FormatMessage(listing), "🔑 Move In: "+expected) {
			t.Errorf("availability %q", value)
		}
	}
}

func TestFurnishingPriority(t *testing.T) {
	labels := map[domain.Furnishing]string{domain.Furnished: "да", domain.PartiallyFurnished: "частично", domain.Unfurnished: "нет"}
	listing := testutil.Record(t)
	api, llm := domain.Unfurnished, domain.Furnished
	listing.Rental.Furnishing = &llm
	if !strings.Contains(FormatMessage(listing), "🛋 Furnished: "+labels[llm]+"\n") {
		t.Error("LLM fallback not used")
	}
	listing.Source.Furnishing = &api
	if !strings.Contains(FormatMessage(listing), "🛋 Furnished: "+labels[api]+"\n") {
		t.Error("API furnishing must take priority")
	}
}

func TestAmenities(t *testing.T) {
	listing := testutil.Record(t)
	if text := FormatMessage(listing); !strings.Contains(text, "🛋 Furnished: не указано\n🧺 Washer: не указано · 📦 Storage: не указано\n") {
		t.Error(text)
	}
	yes := true
	washer, storage := domain.WasherSharedLaundry, domain.StorageRoom
	listing.Rental.Amenities = domain.Amenities{Washer: &washer, Storage: &storage, Balcony: &yes}
	if text := FormatMessage(listing); !strings.Contains(text, "🧺 Washer: прачечная · 📦 Storage: кладовка\n") {
		t.Error(text)
	}
	listing.Source.Cellar = &yes
	for llm, expected := range map[domain.Storage]string{domain.StorageRoom: "кладовка и подвал", domain.StorageNone: "подвал",
		domain.StorageCellar: "подвал", domain.StorageRoomAndCellar: "кладовка и подвал"} {
		listing.Rental.Amenities.Storage = &llm
		if !strings.Contains(FormatMessage(listing), "📦 Storage: "+expected+"\n") {
			t.Errorf("storage %s with API cellar", llm)
		}
	}
	listing.Rental.Amenities.Storage = nil
	if !strings.Contains(FormatMessage(listing), "📦 Storage: подвал\n") {
		t.Error("API cellar alone must show cellar")
	}
}

func TestLocationEscapingAndMissing(t *testing.T) {
	render := func(locality map[string]any) string {
		ad := testutil.APIAd(t, 123)
		ad["locality"] = locality
		source, err := sreality.ToSourceListing(ad, 3)
		if err != nil {
			t.Fatal(err)
		}
		return FormatMessage(domain.Listing{Source: source, Rental: testutil.Rental()})
	}
	if text := render(map[string]any{"citypart": "<Pole & okolí>"}); !strings.Contains(text, "📍 Loc: &lt;Pole &amp; okolí&gt; расстояние не указано") {
		t.Error(text)
	}
	if text := render(map[string]any{}); !strings.Contains(text, "📍 Loc: не указано расстояние не указано") {
		t.Error(text)
	}
}

func TestSendMultipartAndFallback(t *testing.T) {
	for _, count := range []int{0, 1, 3} {
		for _, status := range []int{200, 400, 429} {
			var posts []*http.Request
			var bodies [][]byte
			client := New(testutil.Client(func(req *http.Request) *http.Response {
				if req.Method == http.MethodGet {
					return testutil.RespondBytes(200, testutil.PhotoBytes)
				}
				body, _ := io.ReadAll(req.Body)
				posts, bodies = append(posts, req), append(bodies, body)
				album := strings.HasSuffix(req.URL.Path, "sendMediaGroup")
				code := status
				if strings.HasSuffix(req.URL.Path, "sendMessage") {
					code = 200
				}
				n := 1
				if album {
					n = count
				}
				var messages []string
				for i := range n {
					messages = append(messages, fmt.Sprintf(`{"message_id":%d,"chat":{"id":-100123},"date":1791970000,"caption":"notification"}`, 42+i))
				}
				result := messages[0]
				if album {
					result = "[" + strings.Join(messages, ",") + "]"
				}
				return testutil.Respond(code, fmt.Sprintf(`{"ok":%v,"description":"Rejected","result":%s}`, code == 200, result))
			}), "secret")
			var photos []string
			for i := range count {
				photos = append(photos, fmt.Sprintf("https://example.com/%d.jpg", i))
			}
			delivery, err := client.SendAd(context.Background(), "1", testutil.Record(t, photos...))
			name := fmt.Sprintf("count=%d status=%d", count, status)
			if count > 0 && status == 429 {
				var statusErr *netutil.StatusError
				if !errors.As(err, &statusErr) {
					t.Errorf("%s: expected HTTP error, got %v", name, err)
				}
			} else {
				if err != nil {
					t.Fatalf("%s: %v", name, err)
				}
				expectedCount, method := 1, domain.SendMessage
				if count > 0 && status == 200 {
					method = domain.SendPhoto
					if count > 1 {
						expectedCount, method = count, domain.SendMediaGroup
					}
				}
				if delivery.Method != method || len(delivery.MessageIDs) != expectedCount || delivery.ChatID != -100123 {
					t.Errorf("%s: unexpected delivery %+v", name, delivery)
				}
			}
			if count > 0 {
				if !strings.Contains(posts[0].Header.Get("Content-Type"), "multipart/form-data") || !bytes.Contains(bodies[0], testutil.PhotoBytes) {
					t.Errorf("%s: photos must be sent as multipart", name)
				}
			}
			expectedPosts := 1
			if count > 0 && status == 400 {
				expectedPosts = 2
			}
			if len(posts) != expectedPosts {
				t.Errorf("%s: %d posts", name, len(posts))
			}
		}
	}
}

func TestValidationAndRedaction(t *testing.T) {
	ok := New(testutil.Client(func(*http.Request) *http.Response { return testutil.Respond(200, `{"ok":true}`) }), "secret")
	if _, err := ok.send(context.Background(), domain.SendMessage, map[string]any{"chat_id": "1"}, nil); err == nil {
		t.Error("missing message metadata accepted")
	}
	html := New(testutil.Client(func(*http.Request) *http.Response { return testutil.Respond(200, "<html>") }), "secret")
	if _, err := html.DownloadPhoto(context.Background(), "https://example.com/p.jpg"); err == nil {
		t.Error("HTML accepted as photo")
	}
	rejected := New(testutil.Client(func(*http.Request) *http.Response { return testutil.Respond(400, `{"description":"Rejected"}`) }), "secret")
	var rejection *Rejected
	if _, err := rejected.call(context.Background(), "sendPhoto", map[string]any{}, nil); !errors.As(err, &rejection) {
		t.Errorf("expected rejection, got %v", err)
	}
	failing := New(&http.Client{Transport: failingTransport{}}, "secret")
	if _, err := failing.call(context.Background(), "sendMessage", map[string]any{}, nil); err == nil || strings.Contains(err.Error(), "secret") {
		t.Errorf("transport errors must not leak the token: %v", err)
	}
	safe := SafeDescription("secret https://api.telegram.org/botsecret/sendPhoto\nnext", "secret")
	if strings.Contains(safe, "secret") || strings.Contains(safe, "https://") || strings.Contains(safe, "\n") {
		t.Errorf("unsafe description %q", safe)
	}
}

type failingTransport struct{}

func (failingTransport) RoundTrip(*http.Request) (*http.Response, error) {
	return nil, errors.New("connection refused")
}

func TestMarkGoneKeepsEntitiesAndBody(t *testing.T) {
	for _, method := range []domain.TelegramMethod{domain.SendMessage, domain.SendPhoto, domain.SendMediaGroup} {
		field, entities := "caption", "caption_entities"
		if method == domain.SendMessage {
			field, entities = "text", "entities"
		}
		message := fmt.Sprintf(`{"message_id":42,"chat":{"id":-100123},"%s":"Original","%s":[{"type":"bold","offset":0,"length":8}]}`, field, entities)
		result := message
		if method == domain.SendMediaGroup {
			result = "[" + message + "]"
		}
		gone := time.Date(2026, 11, 9, 10, 0, 0, 0, time.UTC)
		chat := int64(-100123)
		record := domain.NotificationRecord{Status: domain.Sent, ChatID: &chat, MessageIDs: []int64{42},
			TelegramMethod: &method, TelegramResponse: testutil.JSON(t, `{"ok":true,"result":`+result+`}`), GoneAt: &gone}
		var payload map[string]any
		client := New(testutil.Client(func(req *http.Request) *http.Response {
			body, _ := io.ReadAll(req.Body)
			_ = json.Unmarshal(body, &payload)
			return testutil.Respond(400, `{"ok":false,"description":"Bad Request: message is not modified"}`)
		}), "secret")
		if err := client.MarkGone(context.Background(), record); err != nil {
			t.Fatal(err)
		}
		if payload[field] != "Original\n🔴 Gone 09.11.2026" || payload[entities] == nil || payload["message_id"] != float64(42) {
			t.Errorf("%s: payload %v", method, payload)
		}
	}
}
