package jsonval

import (
	"bytes"
	"encoding/json"
	"errors"
	"math"
	"strconv"
	"strings"
)

func Decode(data []byte) (any, error) {
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	var v any
	if err := dec.Decode(&v); err != nil {
		return nil, err
	}
	return v, nil
}

func DecodeObject(data []byte) (map[string]any, error) {
	v, err := Decode(data)
	if err != nil {
		return nil, err
	}
	m, ok := v.(map[string]any)
	if !ok {
		return nil, errors.New("expected a JSON object")
	}
	return m, nil
}

func Object(v any) map[string]any {
	m, _ := v.(map[string]any)
	return m
}

func Truthy(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case string:
		return x != ""
	case json.Number:
		f, err := x.Float64()
		return err != nil || f != 0
	case []any:
		return len(x) > 0
	case map[string]any:
		return len(x) > 0
	default:
		return true
	}
}

func Float(v any) (float64, bool) {
	switch x := v.(type) {
	case json.Number:
		f, err := x.Float64()
		return f, err == nil
	case float64:
		return x, true
	case int:
		return float64(x), true
	case int64:
		return float64(x), true
	}
	return 0, false
}

func Int(v any) (int64, bool) {
	switch x := v.(type) {
	case json.Number:
		if strings.ContainsAny(string(x), ".eE") {
			return 0, false
		}
		i, err := x.Int64()
		return i, err == nil
	case int:
		return int64(x), true
	case int64:
		return x, true
	}
	return 0, false
}

var errNumber = errors.New("expected a number")

func OptInt(v any) (*int64, error) {
	if v == nil {
		return nil, nil
	}
	if i, ok := Int(v); ok {
		return &i, nil
	}
	if f, ok := Float(v); ok && f == math.Trunc(f) && math.Abs(f) < 1<<53 {
		i := int64(f)
		return &i, nil
	}
	if s, ok := v.(string); ok {
		if i, err := strconv.ParseInt(strings.TrimSpace(s), 10, 64); err == nil {
			return &i, nil
		}
	}
	return nil, errNumber
}

func OptFloat(v any) (*float64, error) {
	if v == nil {
		return nil, nil
	}
	f, ok := Float(v)
	if s, isString := v.(string); isString {
		parsed, err := strconv.ParseFloat(strings.TrimSpace(s), 64)
		f, ok = parsed, err == nil
	}
	if !ok || math.IsInf(f, 0) || math.IsNaN(f) {
		return nil, errNumber
	}
	return &f, nil
}

func PyInt(v any) (int64, error) {
	if i, ok := Int(v); ok {
		return i, nil
	}
	if f, ok := Float(v); ok && !math.IsInf(f, 0) && !math.IsNaN(f) {
		return int64(f), nil
	}
	if s, ok := v.(string); ok {
		if i, err := strconv.ParseInt(strings.TrimSpace(s), 10, 64); err == nil {
			return i, nil
		}
	}
	return 0, errNumber
}
