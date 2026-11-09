package monitor

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"testing"
	"time"

	"rentmont/internal/domain"
	"rentmont/internal/extraction"
	"rentmont/internal/netutil"
	"rentmont/internal/pyjson"
	"rentmont/internal/sreality"
	"rentmont/internal/storage"
	"rentmont/internal/telegram"
	"rentmont/internal/testutil"
)

type fakeSource struct {
	t           *testing.T
	ads         []map[string]any
	err         error
	detailCalls int
}

func (f *fakeSource) Search(context.Context, int, int) ([]map[string]any, error) { return f.ads, f.err }

func (f *fakeSource) Detail(_ context.Context, ident string) (map[string]any, error) {
	f.detailCalls++
	for _, ad := range f.ads {
		if pyjson.Str(ad["hash_id"]) == ident {
			return testutil.Clone(f.t, ad), nil
		}
	}
	return nil, errors.New("not found")
}

func (f *fakeSource) ListingGone(context.Context, string) (bool, error) { return false, nil }

type fakeExtractor struct {
	facts        domain.RentalDetails
	err          error
	calls        int
	washer       bool
	visionErr    error
	visionCalls  int
	visionPhotos []string
}

func (f *fakeExtractor) Extract(context.Context, map[string]any) (domain.RentalDetails, error) {
	f.calls++
	return f.facts, f.err
}

func (f *fakeExtractor) WasherVisible(_ context.Context, photos []string) (bool, error) {
	f.visionCalls++
	f.visionPhotos = photos
	return f.washer, f.visionErr
}

type fakeNotifier struct {
	delivery domain.TelegramDelivery
	response map[string]any
	sendErr  error
	editErr  error
	sends    int
	edits    int
	lastEdit domain.Listing
}

func (f *fakeNotifier) SendAd(context.Context, string, domain.Listing) (domain.TelegramDelivery, error) {
	f.sends++
	return f.delivery, f.sendErr
}

func (f *fakeNotifier) EditAd(_ context.Context, _ domain.NotificationRecord, listing domain.Listing) (map[string]any, error) {
	f.edits++
	f.lastEdit = listing
	return f.response, f.editErr
}

func (f *fakeNotifier) MarkGone(context.Context, domain.NotificationRecord) error { return nil }

type harness struct {
	t         *testing.T
	root      string
	service   *Service
	source    *fakeSource
	extractor *fakeExtractor
	notifier  *fakeNotifier
}

func newHarness(t *testing.T, notifyExisting bool) *harness {
	root := t.TempDir()
	h := &harness{t: t, root: root, source: &fakeSource{t: t}, extractor: &fakeExtractor{facts: testutil.Rental()},
		notifier: &fakeNotifier{delivery: testutil.Delivery(t), response: testutil.Delivery(t).Response}}
	h.service = New(testutil.Settings(root, notifyExisting), h.open(), h.source, h.extractor, h.notifier)
	h.service.Sleep = func(context.Context, time.Duration) {}
	return h
}

func (h *harness) open() *storage.Store {
	store, err := storage.Open(h.root)
	if err != nil {
		h.t.Fatal(err)
	}
	return store
}

func (h *harness) restart() { h.service.Store = h.open() }

func (h *harness) scan(detail map[string]any, reextract bool) domain.ScanStats {
	h.t.Helper()
	if detail == nil {
		detail = testutil.APIAd(h.t, 123)
	}
	h.source.ads = []map[string]any{detail}
	stats, err := h.service.Scan(context.Background(), reextract)
	if err != nil {
		h.t.Fatal(err)
	}
	return stats
}

func (h *harness) deliver(listings []domain.Listing) bool {
	h.t.Helper()
	ok, err := h.service.Deliver(context.Background(), listings)
	if err != nil {
		h.t.Fatal(err)
	}
	return ok
}

func (h *harness) calls(f func()) int {
	before := h.extractor.calls
	f()
	return h.extractor.calls - before
}

func rental(rent *float64, utilities *float64, room bool) domain.RentalDetails {
	r := testutil.Rental()
	r.Payments.RentKc, r.Payments.UtilitiesKc, r.IsRoomRent = rent, utilities, room
	return r
}

func TestRejectedFactsSurviveRestart(t *testing.T) {
	for _, facts := range []domain.RentalDetails{rental(nil, testutil.F(2000), false),
		rental(testutil.F(15000), testutil.F(2000), false), rental(testutil.F(12000), testutil.F(2000), true)} {
		h := newHarness(t, true)
		h.extractor.facts = facts
		h.scan(nil, true)
		h.restart()
		var stats domain.ScanStats
		if n := h.calls(func() { stats = h.scan(nil, false) }); n != 0 || stats.Excluded != 1 || len(stats.Listings) != 0 {
			t.Errorf("calls=%d stats=%+v", n, stats)
		}
		saved, _ := h.service.Store.GetExtraction("123")
		if !reflect.DeepEqual(saved.Details, facts) {
			t.Errorf("facts not preserved: %+v", saved.Details)
		}
	}
}

func withPhotos(t *testing.T, images ...map[string]any) map[string]any {
	list := make([]any, len(images))
	for i, image := range images {
		list[i] = image
	}
	return testutil.With(t, testutil.APIAd(t, 123), map[string]any{"advert_images": list})
}

func photo(url string, kind int, aiEdited bool) map[string]any {
	return map[string]any{"url": url, "kind": json.Number(strconv.Itoa(kind)), "ai_edited": aiEdited}
}

func TestPhotoCheckForUnknownWasher(t *testing.T) {
	h := newHarness(t, true)
	h.extractor.washer = true
	detail := withPhotos(t, photo("//e.cz/a.jpg", 2, false), photo("//e.cz/plan.jpg", 4, false), photo("//e.cz/ai.jpg", 2, true))
	stats := h.scan(detail, false)
	if h.extractor.visionCalls != 1 || stats.VisionCalls != 1 || len(h.extractor.visionPhotos) != 1 ||
		!strings.HasPrefix(h.extractor.visionPhotos[0], "https://e.cz/a.jpg") {
		t.Fatalf("calls=%d photos=%v", h.extractor.visionCalls, h.extractor.visionPhotos)
	}
	if !stats.Listings[0].WasherFromPhotos() || !strings.Contains(telegram.FormatMessage(stats.Listings[0]), "🧺 Washer: есть (фото)") {
		t.Error("washer from photos not shown")
	}
	h.restart()
	h.scan(detail, false)
	if h.extractor.visionCalls != 1 {
		t.Error("photo check must be cached")
	}
	textChanged := testutil.With(t, detail, map[string]any{"advert_description": "changed"})
	h.scan(textChanged, false)
	if h.extractor.visionCalls != 1 {
		t.Error("photo facts must survive text re-extraction when photos are unchanged")
	}
	h.scan(withPhotos(t, photo("//e.cz/b.jpg", 2, false)), false)
	if h.extractor.visionCalls != 2 {
		t.Error("changed photos must be checked again")
	}
}

func TestPhotoCheckSkipped(t *testing.T) {
	known := testutil.Rental()
	washer := domain.WasherNone
	known.Amenities.Washer = &washer
	for name, setup := range map[string]func(h *harness) map[string]any{
		"washer known": func(h *harness) map[string]any {
			h.extractor.facts = known
			return withPhotos(t, photo("//e.cz/a.jpg", 2, false))
		},
		"disabled": func(h *harness) map[string]any {
			h.service.Settings.VisionEnabled = false
			return withPhotos(t, photo("//e.cz/a.jpg", 2, false))
		},
		"only plans": func(h *harness) map[string]any { return withPhotos(t, photo("//e.cz/p.jpg", 4, false)) },
		"rejected": func(h *harness) map[string]any {
			h.extractor.facts = rental(testutil.F(20000), nil, false)
			return withPhotos(t, photo("//e.cz/a.jpg", 2, false))
		},
	} {
		h := newHarness(t, true)
		h.scan(setup(h), false)
		if h.extractor.visionCalls != 0 {
			t.Errorf("%s: photos must not be checked", name)
		}
	}
}

func TestPhotoCheckErrors(t *testing.T) {
	h := newHarness(t, true)
	h.extractor.visionErr = errors.New("timeout")
	detail := withPhotos(t, photo("//e.cz/a.jpg", 2, false))
	if stats := h.scan(detail, false); stats.Errors != 1 || len(stats.Listings) != 0 {
		t.Errorf("transient error must retry the listing: %+v", stats)
	}
	h.extractor.visionErr = &netutil.StatusError{Code: 400}
	stats := h.scan(detail, false)
	if stats.Errors != 0 || len(stats.Listings) != 1 || stats.Listings[0].WasherFromPhotos() {
		t.Errorf("rejected photos must leave the washer unknown: %+v", stats)
	}
	h.scan(detail, false)
	if h.extractor.visionCalls != 2 {
		t.Error("rejected photos must not be retried")
	}
}

func TestDatesReuseFactsAndKeepExtractionTime(t *testing.T) {
	h := newHarness(t, true)
	h.scan(nil, false)
	before, _ := h.service.Store.GetExtraction("123")
	changed := testutil.With(t, testutil.APIAd(t, 123), map[string]any{"since": "2026-10-02", "edited": "2026-10-20"})
	var stats domain.ScanStats
	if n := h.calls(func() { stats = h.scan(changed, false) }); n != 0 || stats.CacheHits != 1 {
		t.Fatalf("calls=%d hits=%d", n, stats.CacheHits)
	}
	after, _ := h.service.Store.GetExtraction("123")
	if *before.LLMHash != *after.LLMHash || *before.GlobalHash == *after.GlobalHash || !before.ExtractedAt.Equal(after.ExtractedAt) {
		t.Error("only the global hash must change")
	}
	if *stats.Listings[0].Source.UpdatedAt != "2026-10-20" {
		t.Error("fresh source metadata not used")
	}
	h.restart()
	if n := h.calls(func() { h.scan(changed, false) }); n != 0 {
		t.Error("cache lost after restart")
	}
}

func TestLLMFieldsTriggerExtraction(t *testing.T) {
	for field, value := range map[string]any{"usable_area": json.Number("42"), "floor_area": json.Number("45"),
		"ready_date": "2026-12-01", "tenant_not_pay_commission": true} {
		h := newHarness(t, true)
		h.scan(nil, false)
		if n := h.calls(func() { h.scan(testutil.With(t, testutil.APIAd(t, 123), map[string]any{field: value}), false) }); n != 1 {
			t.Errorf("%s: %d calls", field, n)
		}
	}
}

func TestMissingLLMHashReextractsAndFailedMigrationRetries(t *testing.T) {
	h := newHarness(t, true)
	h.scan(nil, false)
	path := filepath.Join(h.root, "123", "extraction.json")
	var old map[string]any
	data, _ := os.ReadFile(path)
	_ = json.Unmarshal(data, &old)
	delete(old, "llm_hash")
	data, _ = json.Marshal(old)
	_ = os.WriteFile(path, data, 0o644)

	h.extractor.err = errors.New("LLM failed")
	if stats := h.scan(nil, false); stats.Succeeded() {
		t.Error("failed extraction must fail the scan")
	}
	if saved, _ := h.service.Store.GetExtraction("123"); saved.LLMHash != nil {
		t.Error("old facts must be kept unchanged")
	}
	h.extractor.err = nil
	if n := h.calls(func() { h.scan(nil, false) }); n != 1 {
		t.Error("expected retry")
	}
	if n := h.calls(func() { h.scan(nil, false) }); n != 0 {
		t.Error("expected cache hit")
	}
}

func TestFailedReextractBlocksDeliveryAndExport(t *testing.T) {
	h := newHarness(t, true)
	h.scan(nil, false)
	h.extractor.err = errors.New("failed")
	stats := h.scan(nil, true)
	if stats.Succeeded() || len(stats.Listings) != 0 {
		t.Errorf("stats %+v", stats)
	}
	exported, _ := os.ReadFile(filepath.Join(h.root, "listings.json"))
	if string(exported) != "[]" {
		t.Errorf("export %s", exported)
	}
	h.extractor.err = nil
	if stats := h.scan(nil, true); !stats.Succeeded() {
		t.Error("retry failed")
	}
}

func TestDeliveryRetryAndNoResend(t *testing.T) {
	h := newHarness(t, true)
	stats := h.scan(nil, false)
	h.notifier.sendErr = errors.New("timeout")
	if h.deliver(stats.Listings) {
		t.Error("failed delivery reported as success")
	}
	if n, _ := h.service.Store.GetNotification("123"); n != nil {
		t.Error("failed delivery must not be recorded")
	}
	h.notifier.sendErr = nil
	if !h.deliver(stats.Listings) {
		t.Error("delivery failed")
	}
	stats = h.scan(nil, false)
	if !h.deliver(stats.Listings) || h.notifier.sends != 2 {
		t.Errorf("sends=%d", h.notifier.sends)
	}
	n, _ := h.service.Store.GetNotification("123")
	if n.Status != domain.Sent || *n.ChatID != -100123 || *n.FormatVersion != telegram.FormatVersion {
		t.Errorf("notification %+v", n)
	}
}

func TestBaselineSuppressionAndMissing(t *testing.T) {
	h := newHarness(t, false)
	h.scan(nil, false)
	if n, _ := h.service.Store.GetNotification("123"); n == nil || n.Status != domain.Suppressed {
		t.Fatal("initial listing must be suppressed")
	}
	h.source.ads = nil
	stats, err := h.service.Scan(context.Background(), false)
	if err != nil || len(stats.Listings) != 0 || len(h.service.Store.CurrentSearchIDs()) != 0 {
		t.Errorf("stats %+v err %v", stats, err)
	}
	if e, _ := h.service.Store.GetExtraction("123"); e == nil {
		t.Error("archive must be kept")
	}
}

func TestSourceFilterSavesRawWithoutLLM(t *testing.T) {
	h := newHarness(t, true)
	room := testutil.With(t, testutil.APIAd(t, 123), map[string]any{"category_sub_cb": map[string]any{"name": "Pokoj"}})
	var stats domain.ScanStats
	if n := h.calls(func() { stats = h.scan(room, false) }); n != 0 || stats.Excluded != 1 {
		t.Errorf("calls=%d excluded=%d", n, stats.Excluded)
	}
	if s, _ := h.service.Store.GetSource("123"); s == nil {
		t.Error("raw source not saved")
	}
}

func TestBudgetChangesUseSavedFacts(t *testing.T) {
	h := newHarness(t, true)
	h.extractor.facts = rental(testutil.F(12000), testutil.F(3000), false)
	h.scan(nil, false)
	h.service.Settings.MaxRentKc = 12500
	if stats := h.scan(nil, false); stats.Excluded != 0 {
		t.Error("12000 < 12500 must pass")
	}
	h.service.Settings.MaxRentKc = 12000
	if stats := h.scan(nil, false); stats.Excluded != 1 {
		t.Error("rent limit not recomputed")
	}
	h.service.Settings.MaxRentKc = 13500
	limit := 15000
	h.service.Settings.MaxRentAndUtilitiesKc = &limit
	if stats := h.scan(nil, false); stats.Excluded != 1 {
		t.Error("total limit not applied")
	}
	limit = 15001
	if n := h.calls(func() {
		if stats := h.scan(nil, false); len(stats.Listings) != 1 {
			t.Error("total limit too strict")
		}
	}); n != 0 {
		t.Error("budget change must not call the LLM")
	}
}

func TestFailedSearchPreservesMembership(t *testing.T) {
	h := newHarness(t, true)
	h.scan(nil, false)
	h.source.err = errors.New("network")
	if _, err := h.service.Scan(context.Background(), false); err == nil {
		t.Fatal("expected error")
	}
	if !h.service.Store.InCurrentSearch("123") {
		t.Error("membership lost")
	}
}

func TestHashChangesEditOnce(t *testing.T) {
	h := newHarness(t, true)
	stats := h.scan(nil, false)
	h.deliver(stats.Listings)
	detail := testutil.APIAd(t, 123)
	for _, change := range []struct {
		fields map[string]any
		calls  int
	}{
		{map[string]any{"edited": "2026-10-20"}, 0},
		{map[string]any{"usable_area": json.Number("42")}, 1},
		{map[string]any{"furnished": map[string]any{"name": "Částečně", "value": json.Number("3")}}, 0},
	} {
		detail = testutil.With(t, detail, change.fields)
		if n := h.calls(func() { stats = h.scan(detail, false) }); n != change.calls {
			t.Errorf("%v: %d calls", change.fields, n)
		}
		edits := h.notifier.edits
		h.deliver(stats.Listings)
		h.deliver(stats.Listings)
		if h.notifier.edits != edits+1 {
			t.Errorf("%v: expected exactly one edit", change.fields)
		}
		saved, _ := h.service.Store.GetNotification("123")
		if *saved.GlobalHash != extraction.GlobalHash(detail) || *saved.LLMHash != extraction.LLMHash(detail) {
			t.Error("hashes not acknowledged")
		}
	}
	if !strings.Contains(telegram.FormatMessage(h.notifier.lastEdit), "🛋 Furnished: частично") {
		t.Error("edit must use refreshed source furnishing")
	}
	if h.notifier.sends != 1 {
		t.Error("edits must not resend")
	}
}

func TestPhotoChangesDoNotEdit(t *testing.T) {
	h := newHarness(t, true)
	detail := testutil.With(t, testutil.APIAd(t, 123), map[string]any{"advert_images": []any{"https://e.com/1.jpg", "https://e.com/2.jpg"}})
	stats := h.scan(detail, false)
	h.deliver(stats.Listings)
	h.service.Settings.TelegramMaxPhotos = 5
	detail["advert_images"] = []any{"https://e.com/new.jpg"}
	stats = h.scan(detail, false)
	h.deliver(stats.Listings)
	if h.notifier.edits != 0 || h.extractor.calls != 1 {
		t.Errorf("edits=%d calls=%d", h.notifier.edits, h.extractor.calls)
	}
}

func TestRateLimitedEditsWaitOrStop(t *testing.T) {
	for _, c := range []struct {
		retryAfter time.Duration
		edits      int
		waits      []time.Duration
	}{
		{5 * time.Second, 2, []time.Duration{5 * time.Second, 5 * time.Second}},
		{0, 2, []time.Duration{messageInterval, messageInterval}},
		{2 * time.Hour, 1, nil},
	} {
		h := newHarness(t, true)
		h.source.ads = []map[string]any{testutil.APIAd(t, 123), testutil.APIAd(t, 456)}
		stats, err := h.service.Scan(context.Background(), false)
		if err != nil {
			t.Fatal(err)
		}
		h.deliver(stats.Listings)
		h.service.FormatVersion++
		h.notifier.editErr = &telegram.RateLimited{RetryAfter: c.retryAfter}
		var waits []time.Duration
		h.service.Sleep = func(_ context.Context, d time.Duration) {
			if d != messageInterval || h.notifier.editErr != nil {
				waits = append(waits, d)
			}
		}
		edits := h.notifier.edits
		if h.deliver(nil) {
			t.Error("rate-limited edits reported as success")
		}
		if h.notifier.edits-edits != c.edits || !reflect.DeepEqual(waits, c.waits) {
			t.Errorf("retry_after=%s: edits=%d waits=%v", c.retryAfter, h.notifier.edits-edits, waits)
		}
	}
}

func TestFormatVersionEditsOnceAndRetries(t *testing.T) {
	h := newHarness(t, true)
	stats := h.scan(nil, false)
	h.deliver(stats.Listings)
	h.service.FormatVersion++
	h.notifier.editErr = errors.New("timeout")
	if h.deliver(nil) {
		t.Error("failed edit reported as success")
	}
	h.restart()
	if n, _ := h.service.Store.GetNotification("123"); *n.FormatVersion != telegram.FormatVersion {
		t.Error("version must not be acknowledged on failure")
	}
	h.notifier.editErr = nil
	edits := h.notifier.edits
	h.deliver(nil)
	h.deliver(nil)
	if h.notifier.edits != edits+1 || h.extractor.calls != 1 || h.notifier.sends != 1 {
		t.Errorf("edits=%d calls=%d sends=%d", h.notifier.edits-edits, h.extractor.calls, h.notifier.sends)
	}
}

func TestFailedReextractionDoesNotEdit(t *testing.T) {
	h := newHarness(t, true)
	stats := h.scan(nil, false)
	h.deliver(stats.Listings)
	for _, change := range []map[string]any{{"advert_description": "new"}, {"usable_area": json.Number("42")}} {
		if err := h.service.Store.SaveSource(testutil.With(t, testutil.APIAd(t, 123), change)); err != nil {
			t.Fatal(err)
		}
		if !h.deliver(nil) || h.notifier.edits != 0 {
			t.Errorf("%v: stale facts must not be edited in", change)
		}
	}
}

func TestGoneWithRealClients(t *testing.T) {
	for _, method := range []domain.TelegramMethod{domain.SendMessage, domain.SendPhoto, domain.SendMediaGroup} {
		root := t.TempDir()
		store, _ := storage.Open(root)
		field := "caption"
		if method == domain.SendMessage {
			field = "text"
		}
		message := `{"message_id":42,"chat":{"id":-100123},"` + field + `":"Original"}`
		result := message
		if method == domain.SendMediaGroup {
			result = "[" + message + "]"
		}
		delivery := testutil.Delivery(t)
		delivery.Method, delivery.Response = method, testutil.JSON(t, `{"ok":true,"result":`+result+`}`)
		if err := store.MarkSent("123", delivery, nil, nil, 1); err != nil {
			t.Fatal(err)
		}
		if err := store.Suppress("456"); err != nil {
			t.Fatal(err)
		}
		var edits [][]byte
		client := testutil.Client(func(req *http.Request) *http.Response {
			if req.Method == http.MethodGet {
				if !strings.HasSuffix(req.URL.Path, "/123") {
					t.Errorf("unexpected check %s", req.URL.Path)
				}
				return testutil.Respond(404, "{}")
			}
			body, _ := io.ReadAll(req.Body)
			edits = append(edits, body)
			if len(edits) == 1 {
				return testutil.Respond(500, "{}")
			}
			return testutil.Respond(200, `{"ok":true,"result":`+message+`}`)
		})
		service := New(testutil.Settings(root, true), store, &sreality.Client{HTTP: client, BaseURL: sreality.DefaultBaseURL},
			&fakeExtractor{}, &telegram.Client{HTTP: client, Token: "test", BaseURL: telegram.DefaultBaseURL})
		service.Sleep = func(context.Context, time.Duration) {}
		if ok, _ := service.Deliver(context.Background(), nil); ok {
			t.Error("failed edit reported as success")
		}
		service.Store, _ = storage.Open(root)
		for range 2 {
			if ok, err := service.Deliver(context.Background(), nil); !ok || err != nil {
				t.Errorf("retry failed: %v", err)
			}
		}
		if len(edits) != 2 || !bytes.Equal(edits[0], edits[1]) || !bytes.Contains(edits[1], []byte(`Original\n🔴 Gone `)) {
			t.Errorf("%s: edits %s", method, edits)
		}
		if n, _ := service.Store.GetNotification("123"); n.GoneAt == nil || !n.GoneNotified {
			t.Errorf("%s: gone state not saved", method)
		}
	}
}

func TestLogsHideListingContent(t *testing.T) {
	var buf bytes.Buffer
	previous := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&buf, &slog.HandlerOptions{Level: slog.LevelDebug})))
	defer slog.SetDefault(previous)
	h := newHarness(t, true)
	detail := testutil.With(t, testutil.APIAd(t, 123), map[string]any{"advert_description": "PRIVATE_DESCRIPTION", "user_email": "PRIVATE_EMAIL"})
	for range 2 {
		h.deliver(h.scan(detail, false).Listings)
	}
	text := buf.String()
	if strings.Count(text, `msg="New listing discovered"`) != 1 || strings.Count(text, `msg="Notification sent"`) != 1 {
		t.Error("expected one discovery and one send")
	}
	for _, expected := range []string{"Extraction cache hit", "Scan complete", "Delivery complete", "Exported listings"} {
		if !strings.Contains(text, expected) {
			t.Errorf("missing log %q", expected)
		}
	}
	if strings.Contains(text, "PRIVATE") {
		t.Error("listing content leaked into logs")
	}
}
