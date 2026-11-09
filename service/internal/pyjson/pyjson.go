// Package pyjson reproduces Python's json.dumps(ensure_ascii=False,
// sort_keys=True, separators=(",", ":")) and str() for decoded JSON values,
// so content hashes stay identical to those written by the Python version.
package pyjson

import (
	"encoding/json"
	"fmt"
	"maps"
	"math"
	"math/big"
	"slices"
	"strconv"
	"strings"
)

func Dumps(v any) string {
	var b strings.Builder
	encode(&b, v)
	return b.String()
}

func Str(v any) string {
	switch x := v.(type) {
	case nil:
		return "None"
	case string:
		return x
	case bool:
		if x {
			return "True"
		}
		return "False"
	default:
		return Dumps(v)
	}
}

func Number(n json.Number) string {
	s := string(n)
	if strings.ContainsAny(s, ".eE") {
		f, err := strconv.ParseFloat(s, 64)
		if err != nil && !math.IsInf(f, 0) {
			return s
		}
		return Float(f)
	}
	var i big.Int
	if _, ok := i.SetString(s, 10); ok {
		return i.String()
	}
	return s
}

func Float(f float64) string {
	switch {
	case math.IsNaN(f):
		return "NaN"
	case math.IsInf(f, 1):
		return "Infinity"
	case math.IsInf(f, -1):
		return "-Infinity"
	case f == 0:
		if math.Signbit(f) {
			return "-0.0"
		}
		return "0.0"
	}
	s := strconv.FormatFloat(f, 'e', -1, 64)
	sign := ""
	if s[0] == '-' {
		sign, s = "-", s[1:]
	}
	mantissa, expText, _ := strings.Cut(s, "e")
	exp, _ := strconv.Atoi(expText)
	digits := strings.Replace(mantissa, ".", "", 1)
	if exp < -4 || exp >= 16 {
		m := digits[:1]
		if len(digits) > 1 {
			m += "." + digits[1:]
		}
		return fmt.Sprintf("%s%se%+03d", sign, m, exp)
	}
	if exp < 0 {
		return sign + "0." + strings.Repeat("0", -exp-1) + digits
	}
	if len(digits) <= exp+1 {
		return sign + digits + strings.Repeat("0", exp+1-len(digits)) + ".0"
	}
	return sign + digits[:exp+1] + "." + digits[exp+1:]
}

func encode(b *strings.Builder, v any) {
	switch x := v.(type) {
	case nil:
		b.WriteString("null")
	case bool:
		b.WriteString(strconv.FormatBool(x))
	case string:
		quote(b, x)
	case json.Number:
		b.WriteString(Number(x))
	case int:
		b.WriteString(strconv.Itoa(x))
	case int64:
		b.WriteString(strconv.FormatInt(x, 10))
	case float64:
		b.WriteString(Float(x))
	case []any:
		b.WriteByte('[')
		for i, item := range x {
			if i > 0 {
				b.WriteByte(',')
			}
			encode(b, item)
		}
		b.WriteByte(']')
	case map[string]any:
		b.WriteByte('{')
		for i, key := range slices.Sorted(maps.Keys(x)) {
			if i > 0 {
				b.WriteByte(',')
			}
			quote(b, key)
			b.WriteByte(':')
			encode(b, x[key])
		}
		b.WriteByte('}')
	default:
		quote(b, fmt.Sprint(x))
	}
}

func quote(b *strings.Builder, s string) {
	b.WriteByte('"')
	for i := 0; i < len(s); i++ {
		switch c := s[i]; c {
		case '"':
			b.WriteString(`\"`)
		case '\\':
			b.WriteString(`\\`)
		case '\n':
			b.WriteString(`\n`)
		case '\r':
			b.WriteString(`\r`)
		case '\t':
			b.WriteString(`\t`)
		case '\b':
			b.WriteString(`\b`)
		case '\f':
			b.WriteString(`\f`)
		default:
			if c < 0x20 {
				fmt.Fprintf(b, `\u%04x`, c)
			} else {
				b.WriteByte(c)
			}
		}
	}
	b.WriteByte('"')
}
