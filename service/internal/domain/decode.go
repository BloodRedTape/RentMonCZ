package domain

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strings"
)

// strictDecode rejects unknown fields and checks required keys; a "!" suffix
// additionally forbids null, which encoding/json would silently accept.
func strictDecode(data []byte, v any, required ...string) error {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		return err
	}
	if fields == nil {
		return fmt.Errorf("expected an object")
	}
	for _, key := range required {
		key, nonNull := strings.CutSuffix(key, "!")
		raw, ok := fields[key]
		if !ok {
			return fmt.Errorf("%s: field required", key)
		}
		if nonNull && string(raw) == "null" {
			return fmt.Errorf("%s: must not be null", key)
		}
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	dec.UseNumber()
	return dec.Decode(v)
}

func withoutKey(data []byte, key string) ([]byte, error) {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		return nil, err
	}
	if _, ok := fields[key]; !ok {
		return data, nil
	}
	delete(fields, key)
	return json.Marshal(fields)
}
