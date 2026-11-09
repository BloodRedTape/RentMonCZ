package extraction

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"strings"
	"testing"
	"time"

	"rentmont/internal/jsonval"
	"rentmont/internal/testutil"
)

func TestHashesMatchPythonImplementation(t *testing.T) {
	ad := testutil.APIAd(t, 123)
	if got := GlobalHash(ad); got != "b06097825b3f3fb840b95410f24db0a368a620ea60dd7f47c46e5f0bb9c9a95c" {
		t.Errorf("global hash %s", got)
	}
	if got := LLMHash(ad); got != "86a28ba5b51c814324dc88cfaa726817ff02d3c45b397a90902f38ffd33fa077" {
		t.Errorf("llm hash %s", got)
	}
	data, err := os.ReadFile("../testdata/tricky.json")
	if err != nil {
		t.Fatal(err)
	}
	tricky, err := jsonval.DecodeObject(data)
	if err != nil {
		t.Fatal(err)
	}
	if got := GlobalHash(tricky); got != "c6238909477adf704c31b9f6f841366dfa26f474b238e720425f437945b77216" {
		t.Errorf("tricky global hash %s", got)
	}
	if got := LLMHash(tricky); got != "82111357dddd8887baf0b3d02d0932f2f8d37b62db09f8c06e205e71465af633" {
		t.Errorf("tricky llm hash %s", got)
	}
}

func TestHashFields(t *testing.T) {
	original := testutil.APIAd(t, 123)
	for _, key := range HashFields {
		if GlobalHash(original) == GlobalHash(testutil.With(t, original, map[string]any{key: "changed"})) {
			t.Errorf("global hash ignores %s", key)
		}
	}
	for _, key := range Fields {
		if LLMHash(original) == LLMHash(testutil.With(t, original, map[string]any{key: "changed"})) {
			t.Errorf("llm hash ignores %s", key)
		}
	}
	ignored := testutil.With(t, original, map[string]any{"user_email": "x", "advert_images": []any{"https://e.com/1.jpg"}})
	if GlobalHash(original) != GlobalHash(ignored) || LLMHash(original) != LLMHash(ignored) {
		t.Error("unrelated fields must not change hashes")
	}
	dates := testutil.With(t, original, map[string]any{"since": "2026-10-20", "edited": "2026-10-21"})
	if LLMHash(original) != LLMHash(dates) {
		t.Error("dates must not change llm hash")
	}
	furnished := testutil.With(t, original, map[string]any{"furnished": map[string]any{"value": json.Number("1")}})
	if GlobalHash(original) == GlobalHash(furnished) || LLMHash(original) != LLMHash(furnished) {
		t.Error("structured furnishing belongs to the global hash only")
	}
}

func TestExtractSendsWhitelistAndValidates(t *testing.T) {
	var requests []map[string]any
	responses := []string{"", `{"choices":[{"finish_reason":"stop","message":{"content":` +
		`"{\"payments\":{\"rent_kc\":12000,\"utilities_kc\":0,\"commission_kc\":null,\"deposit_kc\":24000},` +
		`\"is_room_rent\":false,\"area_m2\":36,\"available_from\":\"asap\",\"furnishing\":\"furnished\"}"}}]}`}
	client := NewClient(testutil.Client(func(req *http.Request) *http.Response {
		body, _ := io.ReadAll(req.Body)
		var payload map[string]any
		_ = json.Unmarshal(body, &payload)
		requests = append(requests, payload)
		if len(requests) == 1 {
			return testutil.Respond(500, "{}")
		}
		return testutil.Respond(200, responses[1])
	}), "key", "model", "https://api.openai.test/v1")
	client.Sleep = func(context.Context, time.Duration) {}
	ad := testutil.With(t, testutil.APIAd(t, 123), map[string]any{"user_email": "private"})
	details, err := client.Extract(context.Background(), ad)
	if err != nil {
		t.Fatal(err)
	}
	if len(requests) != 2 {
		t.Fatalf("expected a retry, got %d requests", len(requests))
	}
	user := requests[1]["messages"].([]any)[1].(map[string]any)["content"].(string)
	if strings.Contains(user, "private") || !strings.Contains(user, "advert_description") {
		t.Errorf("unexpected LLM input %s", user)
	}
	if details.Payments.UtilitiesKc != nil || *details.Payments.RentKc != 12000 || *details.AvailableFrom != "asap" {
		t.Errorf("unexpected details %+v", details)
	}
}

func TestExtractRejectsInvalidOutput(t *testing.T) {
	for _, content := range []string{
		`{"payments":{"rent_kc":-1,"utilities_kc":null,"commission_kc":null,"deposit_kc":null},"is_room_rent":false,"area_m2":null,"available_from":null,"furnishing":null}`,
		`{"payments":{"rent_kc":1,"utilities_kc":null,"commission_kc":null,"deposit_kc":null},"is_room_rent":false,"area_m2":null,"available_from":"2026-02-30","furnishing":null}`,
		`{"payments":{"rent_kc":1,"utilities_kc":null,"commission_kc":null,"deposit_kc":null},"is_room_rent":false,"area_m2":null,"available_from":null,"furnishing":"Ano"}`,
	} {
		encoded, _ := json.Marshal(content)
		client := NewClient(testutil.Client(func(*http.Request) *http.Response {
			return testutil.Respond(200, `{"choices":[{"finish_reason":"stop","message":{"content":`+string(encoded)+`}}]}`)
		}), "key", "model", "https://api.openai.test/v1")
		if _, err := client.Extract(context.Background(), testutil.APIAd(t, 123)); err == nil {
			t.Errorf("accepted %s", content)
		}
	}
}
