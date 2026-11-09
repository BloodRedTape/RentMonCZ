package storage

import (
	"bytes"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"rentmont/internal/testutil"
)

func TestIndependentFilesRoundtrip(t *testing.T) {
	root := t.TempDir()
	store, err := Open(root)
	if err != nil {
		t.Fatal(err)
	}
	must(t, store.SaveSource(testutil.APIAd(t, 123)))
	first, _ := store.GetSource("123")
	sourceBytes, _ := os.ReadFile(filepath.Join(root, "123", "source.json"))
	must(t, store.SaveExtraction("123", testutil.Rental(), nil, nil))
	must(t, store.MarkSent("123", testutil.Delivery(t), nil, nil, 1))
	after, _ := os.ReadFile(filepath.Join(root, "123", "source.json"))
	if !bytes.Equal(sourceBytes, after) {
		t.Error("other files must not rewrite source.json")
	}
	must(t, store.SaveSource(testutil.With(t, testutil.APIAd(t, 123), map[string]any{"advert_description": "Changed"})))
	must(t, store.SetCurrentSearch([]string{"123"}))
	must(t, store.MarkInitialized())

	store, err = Open(root)
	if err != nil {
		t.Fatal(err)
	}
	source, _ := store.GetSource("123")
	extraction, _ := store.GetExtraction("123")
	notification, _ := store.GetNotification("123")
	if !store.Initialized() || !source.FirstSeenAt.Equal(first.FirstSeenAt) || source.Raw["advert_description"] != "Changed" {
		t.Error("state or source not persisted")
	}
	if !reflect.DeepEqual(extraction.Details, testutil.Rental()) {
		t.Errorf("details %+v", extraction.Details)
	}
	if notification.SentAt == nil || !reflect.DeepEqual(notification.TelegramResponse, testutil.Delivery(t).Response) ||
		!reflect.DeepEqual(notification.MessageIDs, []int64{42}) {
		t.Errorf("notification %+v", notification)
	}
	var state map[string]any
	data, _ := os.ReadFile(filepath.Join(root, "state.json"))
	_ = json.Unmarshal(data, &state)
	if len(state) != 3 || state["schema_version"] != float64(4) {
		t.Errorf("state %v", state)
	}
}

func TestReadsPythonWrittenFiles(t *testing.T) {
	root := t.TempDir()
	write(t, filepath.Join(root, "state.json"), `{"schema_version":4,"initialized":true,"current_search_ids":["123"]}`)
	write(t, filepath.Join(root, "123", "source.json"), `{
  "raw": {"hash_id": 123, "price_czk": 12000.0},
  "first_seen_at": "2026-10-01T10:00:00.123456Z",
  "last_seen_at": "2026-10-02T10:00:00Z"
}`)
	write(t, filepath.Join(root, "123", "extraction.json"), `{"global_hash":"g","llm_hash":"l",
  "details":{"payments":{"rent_kc":12000.0,"utilities_kc":null,"commission_kc":null,"deposit_kc":null},
  "is_room_rent":false,"area_m2":36.0,"available_from":"2026-12-01","furnishing":null},
  "extracted_at":"2026-10-01T10:00:00.123456Z"}`)
	write(t, filepath.Join(root, "123", "notification.json"), `{"global_hash":"g","llm_hash":"l","status":"sent",
  "sent_at":"2026-10-01T10:00:00Z","chat_id":-100123,"message_ids":[42],"telegram_method":"sendMessage",
  "telegram_response":{"ok":true,"result":{"message_id":42}},"format_version":1,"gone_at":null,"gone_notified":false}`)
	store, err := Open(root)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.GetSource("123"); err != nil {
		t.Error(err)
	}
	if record, err := store.GetExtraction("123"); err != nil || *record.Details.AvailableFrom != "2026-12-01" {
		t.Error(err)
	}
	if sent, err := store.SentNotifications(); err != nil || len(sent) != 1 {
		t.Errorf("sent=%v err=%v", sent, err)
	}
}

func TestInvalidFilesAreNotReset(t *testing.T) {
	root := t.TempDir()
	store, _ := Open(root)
	must(t, store.SaveSource(testutil.APIAd(t, 123)))
	readers := map[string]func() error{
		"source.json":       func() error { _, err := store.GetSource("123"); return err },
		"extraction.json":   func() error { _, err := store.GetExtraction("123"); return err },
		"notification.json": func() error { _, err := store.GetNotification("123"); return err },
	}
	for name, read := range readers {
		path := filepath.Join(root, "123", name)
		write(t, path, `{"bad":`)
		if err := read(); !IsStorageError(err) {
			t.Errorf("%s: expected storage error, got %v", name, err)
		}
		if data, _ := os.ReadFile(path); string(data) != `{"bad":` {
			t.Errorf("%s was reset", name)
		}
	}
	write(t, filepath.Join(root, "state.json"), `{"schema_version":2}`)
	if _, err := Open(root); !IsStorageError(err) {
		t.Errorf("old state accepted: %v", err)
	}
}

func TestFailedWritePreservesDiskAndMemory(t *testing.T) {
	root := t.TempDir()
	store, _ := Open(root)
	must(t, store.SaveSource(testutil.APIAd(t, 123)))
	path := filepath.Join(root, "123", "source.json")
	before, _ := os.ReadFile(path)
	rename = func(string, string) error { return errors.New("disk") }
	defer func() { rename = os.Rename }()
	if err := store.SaveSource(testutil.With(t, testutil.APIAd(t, 123), map[string]any{"advert_description": "x"})); !IsStorageError(err) {
		t.Error("expected storage error")
	}
	if err := store.MarkInitialized(); !IsStorageError(err) || store.Initialized() {
		t.Error("failed commit must not change memory state")
	}
	if after, _ := os.ReadFile(path); !bytes.Equal(before, after) {
		t.Error("source changed")
	}
	temporary, _ := filepath.Glob(filepath.Join(root, "*", "*.tmp"))
	if len(temporary) != 0 {
		t.Errorf("leftover temp files %v", temporary)
	}
}

func TestSafePathsAndIDMatch(t *testing.T) {
	root := t.TempDir()
	store, _ := Open(root)
	for _, ident := range []string{"../escape", "", "/tmp/test", "123/456", ".."} {
		if _, err := store.GetSource(ident); !IsStorageError(err) {
			t.Errorf("%q accepted", ident)
		}
	}
	must(t, store.SaveSource(testutil.APIAd(t, 123)))
	path := filepath.Join(root, "123", "source.json")
	data, _ := os.ReadFile(path)
	write(t, path, string(bytes.Replace(data, []byte(`"hash_id": 123`), []byte(`"hash_id": 456`), 1)))
	if _, err := store.GetSource("123"); !IsStorageError(err) {
		t.Error("mismatched ID accepted")
	}
}

func TestObsoleteContentHashIsDiscarded(t *testing.T) {
	root := t.TempDir()
	store, _ := Open(root)
	must(t, store.SaveExtraction("123", testutil.Rental(), testutil.S("new"), testutil.S("llm")))
	must(t, store.MarkSent("123", testutil.Delivery(t), testutil.S("new"), testutil.S("llm"), 1))
	for _, name := range []string{"extraction.json", "notification.json"} {
		path := filepath.Join(root, "123", name)
		data, _ := os.ReadFile(path)
		write(t, path, string(bytes.Replace(data, []byte(`"global_hash"`), []byte(`"content_hash"`), 1)))
	}
	extraction, err := store.GetExtraction("123")
	if err != nil || extraction.GlobalHash != nil || *extraction.LLMHash != "llm" {
		t.Errorf("extraction %+v err=%v", extraction, err)
	}
	notification, err := store.GetNotification("123")
	if err != nil || notification.GlobalHash != nil {
		t.Errorf("notification %+v err=%v", notification, err)
	}
}

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}

func write(t *testing.T, path, content string) {
	t.Helper()
	must(t, os.MkdirAll(filepath.Dir(path), 0o755))
	must(t, os.WriteFile(path, []byte(content), 0o644))
}
