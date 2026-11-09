package pyjson

import (
	"encoding/json"
	"testing"
)

func TestFloatMatchesPythonRepr(t *testing.T) {
	cases := map[float64]string{
		12000: "12000.0", 1.5: "1.5", 1e16: "1e+16", 1.5e16: "1.5e+16", 1.5e-5: "1.5e-05",
		0.0001: "0.0001", 49.166982790872: "49.166982790872", 1234567890123456: "1234567890123456.0",
		-2.5: "-2.5", 0.30000000000000004: "0.30000000000000004",
	}
	for value, expected := range cases {
		if got := Float(value); got != expected {
			t.Errorf("Float(%v) = %q, want %q", value, got, expected)
		}
	}
}

func TestDumpsMatchesPython(t *testing.T) {
	value := map[string]any{
		"b": []any{json.Number("1"), json.Number("2.50"), json.Number("1E3"), nil, true},
		"a": "Č \"q\" \\ \n\t\u0001\u007f  <&>",
	}
	expected := `{"a":"Č \"q\" \\ \n\t\u0001` + "\u007f " + ` <&>","b":[1,2.5,1000.0,null,true]}`
	if got := Dumps(value); got != expected {
		t.Fatalf("got %s\nwant %s", got, expected)
	}
	if Str(json.Number("123")) != "123" || Str(json.Number("123.0")) != "123.0" {
		t.Fatal("Str must follow Python str() for numbers")
	}
}
