package config

import (
	"errors"
	"reflect"
	"testing"
)

func lookup(values map[string]string) func(string) (string, bool) {
	return func(name string) (string, bool) {
		value, ok := values[name]
		return value, ok
	}
}

func base() map[string]string {
	return map[string]string{"OPENAI_API_KEY": "k", "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}
}

func TestDefaultsAndOverrides(t *testing.T) {
	s, err := FromLookup(lookup(base()))
	if err != nil || s.SoloCheapMaxRentKc != 13500 || s.TelegramMaxPhotos != 3 || s.SoloCheapMaxRentAndUtilitiesKc != nil || s.NotifyExisting {
		t.Fatalf("settings %+v err %v", s, err)
	}
	values := base()
	values["SOLO_CHEAP_MAX_RENT_AND_UTILITIES_KC"], values["NOTIFY_EXISTING"], values["TELEGRAM_MAX_PHOTOS"] = "17000", "true", "10"
	s, err = FromLookup(lookup(values))
	if err != nil || *s.SoloCheapMaxRentAndUtilitiesKc != 17000 || !s.NotifyExisting || s.TelegramMaxPhotos != 10 {
		t.Fatalf("settings %+v err %v", s, err)
	}
}

func TestInvalidValuesAreReportedByName(t *testing.T) {
	values := base()
	values["OPENAI_API_KEY"], values["SOLO_CHEAP_MAX_RENT_AND_UTILITIES_KC"], values["TELEGRAM_MAX_PHOTOS"], values["NOTIFY_EXISTING"] = " ", "0", "11", "maybe"
	delete(values, "TELEGRAM_CHAT_ID")
	_, err := FromLookup(lookup(values))
	var invalid *ValidationError
	if !errors.As(err, &invalid) {
		t.Fatalf("expected validation error, got %v", err)
	}
	expected := []string{"openai_api_key", "telegram_chat_id", "telegram_max_photos", "solo_cheap_max_rent_and_utilities_kc", "notify_existing"}
	if !reflect.DeepEqual(invalid.Fields, expected) {
		t.Errorf("fields %v", invalid.Fields)
	}
}

func TestRenamedVariablesAreRejected(t *testing.T) {
	values := base()
	values["MAX_RENT_KC"] = "13500"
	_, err := FromLookup(lookup(values))
	var invalid *ValidationError
	if !errors.As(err, &invalid) || !reflect.DeepEqual(invalid.Fields, []string{"max_rent_kc (renamed to solo_cheap_max_rent_kc)"}) {
		t.Errorf("expected rename error, got %v", err)
	}
}
