package extraction

import (
	"crypto/sha256"
	"encoding/hex"

	"rentmont/internal/pyjson"
)

// Version must be bumped when the extraction contract changes so that old facts are refreshed.
const Version = 2

var Fields = []string{
	"advert_name", "advert_description", "price_czk", "price_note",
	"cost_of_living", "commission", "refundable_deposit",
	"tenant_not_pay_commission", "price_unit_cb", "price_currency_cb",
	"category_sub_cb", "usable_area", "floor_area", "ready_date",
}

var HashFields = []string{
	"advert_name", "advert_description", "price_czk", "price_unit_cb",
	"price_currency_cb", "price_note", "cost_of_living", "commission",
	"refundable_deposit", "since", "edited", "furnished",
}

func GlobalHash(detail map[string]any) string {
	values := make([]any, len(HashFields))
	for i, key := range HashFields {
		values[i] = detail[key]
	}
	return sha(pyjson.Dumps(values))
}

func Content(detail map[string]any) string {
	values := make(map[string]any, len(Fields))
	for _, key := range Fields {
		values[key] = detail[key]
	}
	return pyjson.Dumps(values)
}

func LLMHash(detail map[string]any) string {
	return sha(pyjson.Dumps(map[string]any{"version": Version, "input": Content(detail)}))
}

func sha(content string) string {
	sum := sha256.Sum256([]byte(content))
	return hex.EncodeToString(sum[:])
}
