package domain

import (
	"encoding/json"
	"strings"
	"testing"
)

const validDetails = `{"payments":{"rent_kc":12000,"utilities_kc":2000,"commission_kc":null,"deposit_kc":24000},` +
	`"is_room_rent":false,"area_m2":36,"available_from":null}`

func replace(field, value string) string {
	var fields map[string]json.RawMessage
	_ = json.Unmarshal([]byte(validDetails), &fields)
	fields[field] = json.RawMessage(value)
	data, _ := json.Marshal(fields)
	return string(data)
}

func TestPaymentsNormalizeZero(t *testing.T) {
	for _, value := range []string{"0", "0.0", "null"} {
		var d RentalDetails
		if err := json.Unmarshal([]byte(strings.Replace(validDetails, `"rent_kc":12000`, `"rent_kc":`+value, 1)), &d); err != nil {
			t.Fatal(err)
		}
		if d.Payments.RentKc != nil {
			t.Errorf("%s must become null", value)
		}
	}
	var d RentalDetails
	if err := json.Unmarshal([]byte(strings.Replace(validDetails, `"rent_kc":12000`, `"rent_kc":-1`, 1)), &d); err == nil {
		t.Error("negative amount accepted")
	}
	if err := json.Unmarshal([]byte(strings.Replace(validDetails, `"rent_kc":12000,`, ``, 1)), &d); err == nil {
		t.Error("missing payment field accepted")
	}
}

func TestRentalDetailsValidation(t *testing.T) {
	var d RentalDetails
	if err := json.Unmarshal([]byte(validDetails), &d); err != nil || d.Furnishing != nil {
		t.Fatalf("old records without furnishing must load: %v", err)
	}
	for _, value := range []string{`null`, `"asap"`, `"2026-12-01"`} {
		if err := json.Unmarshal([]byte(replace("available_from", value)), &d); err != nil {
			t.Errorf("available_from %s rejected: %v", value, err)
		}
	}
	invalid := map[string][]string{
		"available_from": {`"soon"`, `"2026-02-30"`, `"20261201"`, `0`, `true`},
		"area_m2":        {`0`, `-1`},
		"furnishing":     {`"unknown"`, `"Ano"`, `true`, `1`},
		"is_room_rent":   {`null`, `"yes"`},
		"extra":          {`1`},
	}
	for field, values := range invalid {
		for _, value := range values {
			if err := json.Unmarshal([]byte(replace(field, value)), &d); err == nil {
				t.Errorf("%s=%s accepted", field, value)
			}
		}
	}
	for _, value := range []string{`"furnished"`, `"partially_furnished"`, `"unfurnished"`} {
		if err := json.Unmarshal([]byte(replace("furnishing", value)), &d); err != nil || d.Furnishing == nil {
			t.Errorf("furnishing %s rejected", value)
		}
	}
}

func TestNotificationValidation(t *testing.T) {
	var record NotificationRecord
	if err := json.Unmarshal([]byte(`{"status":"suppressed","content_hash":"old"}`), &record); err != nil {
		t.Fatalf("obsolete content_hash must be discarded: %v", err)
	}
	for _, text := range []string{
		`{"status":"sent"}`,
		`{"status":"suppressed","sent_at":"2026-01-01T00:00:00Z"}`,
		`{"status":"sent","sent_at":"2026-01-01T00:00:00Z","chat_id":1}`,
		`{"status":"sent","sent_at":"2026-01-01T00:00:00Z","chat_id":1.0,"message_ids":[1],"telegram_method":"sendMessage","telegram_response":{}}`,
		`{"status":"unknown"}`,
	} {
		if err := json.Unmarshal([]byte(text), &record); err == nil {
			t.Errorf("accepted %s", text)
		}
	}
	var state MonitorState
	if err := json.Unmarshal([]byte(`{"schema_version":2}`), &state); err == nil {
		t.Error("old schema accepted")
	}
}
