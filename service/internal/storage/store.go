package storage

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"log/slog"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"time"

	"rentmont/internal/domain"
	"rentmont/internal/pyjson"
)

type Error struct {
	Message string
	Err     error
}

func (e *Error) Error() string {
	if e.Err != nil {
		return e.Message + ": " + e.Err.Error()
	}
	return e.Message
}

func (e *Error) Unwrap() error { return e.Err }

func IsStorageError(err error) bool {
	var storageErr *Error
	return errors.As(err, &storageErr)
}

func fail(err error, format string, args ...any) error {
	return &Error{Message: fmt.Sprintf(format, args...), Err: err}
}

var numericID = regexp.MustCompile(`^[0-9]+$`)

var Now = func() time.Time { return time.Now().UTC().Truncate(time.Microsecond) }

func logger() *slog.Logger { return slog.Default().With("module", "storage") }

type validator interface{ Validate() error }

type Store struct {
	Dir     string
	path    string
	state   domain.MonitorState
	current map[string]bool
}

type SentNotification struct {
	ID     string
	Record domain.NotificationRecord
}

func Open(dir string) (*Store, error) {
	s := &Store{Dir: dir, path: filepath.Join(dir, "state.json")}
	var state domain.MonitorState
	found, err := s.read(s.path, &state)
	if err != nil {
		return nil, err
	}
	if !found {
		state = domain.NewMonitorState()
		if err := s.write(s.path, &state); err != nil {
			return nil, err
		}
		logger().Info("Created monitor state", "dir", dir)
	} else {
		logger().Debug("Loaded monitor state", "path", s.path, "initialized", state.Initialized)
	}
	s.setState(state)
	return s, nil
}

func (s *Store) setState(state domain.MonitorState) {
	s.state = state
	s.current = make(map[string]bool, len(state.CurrentSearchIDs))
	for _, id := range state.CurrentSearchIDs {
		s.current[id] = true
	}
}

func (s *Store) Initialized() bool { return s.state.Initialized }

func (s *Store) InCurrentSearch(ident string) bool { return s.current[ident] }

func (s *Store) CurrentSearchIDs() []string { return slices.Clone(s.state.CurrentSearchIDs) }

func (s *Store) SetCurrentSearch(ids []string) error {
	for _, ident := range ids {
		if _, err := s.listingPath(ident, "source.json"); err != nil {
			return err
		}
	}
	unique := slices.Compact(slices.Sorted(slices.Values(ids)))
	if unique == nil {
		unique = []string{}
	}
	state := s.state
	state.CurrentSearchIDs = unique
	return s.commit(state)
}

func (s *Store) MarkInitialized() error {
	if s.state.Initialized {
		return nil
	}
	state := s.state
	state.Initialized = true
	return s.commit(state)
}

func (s *Store) commit(state domain.MonitorState) error {
	if err := s.write(s.path, &state); err != nil {
		return err
	}
	s.setState(state)
	return nil
}

func (s *Store) GetSource(ident string) (*domain.SourceRecord, error) {
	var record domain.SourceRecord
	path, found, err := s.readListing(ident, "source.json", &record)
	if err != nil || !found {
		return nil, err
	}
	if record.ID() != ident {
		return nil, fail(nil, "Listing ID does not match directory: %s", path)
	}
	return &record, nil
}

func (s *Store) SaveSource(raw map[string]any) error {
	ident := pyjson.Str(raw["hash_id"])
	old, err := s.GetSource(ident)
	if err != nil {
		return err
	}
	now := Now()
	record := domain.SourceRecord{Raw: raw, FirstSeenAt: now, LastSeenAt: now}
	if old != nil {
		record.FirstSeenAt = old.FirstSeenAt
	}
	return s.writeListing(ident, "source.json", &record)
}

func (s *Store) GetExtraction(ident string) (*domain.ExtractionRecord, error) {
	var record domain.ExtractionRecord
	_, found, err := s.readListing(ident, "extraction.json", &record)
	if err != nil || !found {
		return nil, err
	}
	return &record, nil
}

func (s *Store) SaveExtraction(ident string, details domain.RentalDetails, globalHash, llmHash *string) error {
	return s.writeListing(ident, "extraction.json", &domain.ExtractionRecord{
		GlobalHash: globalHash, LLMHash: llmHash, Details: details, ExtractedAt: Now(),
	})
}

func (s *Store) UpdateExtractionGlobalHash(ident, globalHash string) error {
	record, err := s.GetExtraction(ident)
	if err != nil {
		return err
	}
	if record == nil {
		return fail(nil, "Cannot refresh a missing extraction")
	}
	record.GlobalHash = &globalHash
	return s.writeListing(ident, "extraction.json", record)
}

func (s *Store) GetNotification(ident string) (*domain.NotificationRecord, error) {
	var record domain.NotificationRecord
	_, found, err := s.readListing(ident, "notification.json", &record)
	if err != nil || !found {
		return nil, err
	}
	return &record, nil
}

func (s *Store) SentNotifications() ([]SentNotification, error) {
	entries, err := os.ReadDir(s.Dir)
	if err != nil {
		return nil, fail(err, "Cannot list %s", s.Dir)
	}
	var records []SentNotification
	for _, entry := range entries {
		if !entry.IsDir() || !numericID.MatchString(entry.Name()) {
			continue
		}
		record, err := s.GetNotification(entry.Name())
		if err != nil {
			return nil, err
		}
		if record != nil && record.Status == domain.Sent {
			records = append(records, SentNotification{ID: entry.Name(), Record: *record})
		}
	}
	return records, nil
}

func (s *Store) sentNotification(ident, action string) (*domain.NotificationRecord, error) {
	record, err := s.GetNotification(ident)
	if err != nil {
		return nil, err
	}
	if record == nil || record.Status != domain.Sent {
		return nil, fail(nil, "Cannot %s an unsent notification", action)
	}
	return record, nil
}

func (s *Store) MarkGone(ident string, notified bool) (domain.NotificationRecord, error) {
	record, err := s.sentNotification(ident, "mark gone")
	if err != nil {
		return domain.NotificationRecord{}, err
	}
	if record.GoneAt == nil {
		now := Now()
		record.GoneAt = &now
	}
	record.GoneNotified = notified
	return *record, s.writeListing(ident, "notification.json", record)
}

func (s *Store) MarkUpdated(ident string, globalHash *string, response map[string]any, llmHash *string, formatVersion int64) error {
	record, err := s.sentNotification(ident, "update")
	if err != nil {
		return err
	}
	record.GlobalHash, record.LLMHash = globalHash, llmHash
	record.FormatVersion = &formatVersion
	record.TelegramResponse = response
	return s.writeListing(ident, "notification.json", record)
}

func (s *Store) MarkSent(ident string, delivery domain.TelegramDelivery, globalHash, llmHash *string, formatVersion int64) error {
	now := Now()
	method := delivery.Method
	chatID := delivery.ChatID
	return s.writeListing(ident, "notification.json", &domain.NotificationRecord{
		Status: domain.Sent, SentAt: &now, GlobalHash: globalHash, LLMHash: llmHash,
		FormatVersion: &formatVersion, ChatID: &chatID, MessageIDs: delivery.MessageIDs,
		TelegramMethod: &method, TelegramResponse: delivery.Response,
	})
}

func (s *Store) Suppress(ident string) error {
	record, err := s.GetNotification(ident)
	if err != nil || record != nil {
		return err
	}
	return s.writeListing(ident, "notification.json", &domain.NotificationRecord{Status: domain.Suppressed})
}

func (s *Store) ExportListings(path string, listings []domain.Listing) error {
	if listings == nil {
		listings = []domain.Listing{}
	}
	content, err := marshal(listings)
	if err != nil {
		return fail(err, "Cannot export listings to %s", path)
	}
	if err := atomicWrite(path, content); err != nil {
		return fail(err, "Cannot export listings to %s", path)
	}
	logger().Debug("Exported listings", "count", len(listings), "path", path)
	return nil
}

func (s *Store) listingPath(ident, filename string) (string, error) {
	if !numericID.MatchString(ident) {
		return "", fail(nil, "Expected a numeric Sreality ID")
	}
	dir := filepath.Join(s.Dir, ident)
	if info, err := os.Lstat(dir); err == nil && info.Mode()&fs.ModeSymlink != 0 {
		return "", fail(nil, "Listing directory must not be a symlink: %s", dir)
	}
	return filepath.Join(dir, filename), nil
}

func (s *Store) readListing(ident, filename string, v any) (string, bool, error) {
	path, err := s.listingPath(ident, filename)
	if err != nil {
		return "", false, err
	}
	found, err := s.read(path, v)
	return path, found, err
}

func (s *Store) writeListing(ident, filename string, v validator) error {
	path, err := s.listingPath(ident, filename)
	if err != nil {
		return err
	}
	return s.write(path, v)
}

func (s *Store) read(path string, v any) (bool, error) {
	content, err := os.ReadFile(path)
	if errors.Is(err, fs.ErrNotExist) {
		return false, nil
	}
	if err != nil {
		return false, fail(err, "Cannot read %s", path)
	}
	if err := json.Unmarshal(content, v); err != nil {
		return false, fail(nil, "Invalid data in %s: %s; file was not reset", path, err)
	}
	return true, nil
}

func (s *Store) write(path string, v validator) error {
	if err := v.Validate(); err != nil {
		return fail(err, "Refusing to save invalid %s", path)
	}
	content, err := marshal(v)
	if err != nil {
		return fail(err, "Cannot encode %s", path)
	}
	if err := atomicWrite(path, content); err != nil {
		return fail(err, "Cannot save %s", path)
	}
	logger().Debug("Saved", "path", path)
	return nil
}

func marshal(v any) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", "  ")
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	return bytes.TrimRight(buf.Bytes(), "\n"), nil
}

var rename = os.Rename

func atomicWrite(path string, content []byte) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	file, err := os.CreateTemp(dir, "."+filepath.Base(path)+".*.tmp")
	if err != nil {
		return err
	}
	temporary := file.Name()
	defer os.Remove(temporary)
	if _, err := file.Write(content); err != nil {
		file.Close()
		return err
	}
	if err := file.Sync(); err != nil {
		file.Close()
		return err
	}
	if err := file.Close(); err != nil {
		return err
	}
	return rename(temporary, path)
}
