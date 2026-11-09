package monitor

import (
	"context"
	"errors"
	"log/slog"
	"path/filepath"
	"slices"
	"strings"
	"time"

	"rentmont/internal/config"
	"rentmont/internal/domain"
	"rentmont/internal/extraction"
	"rentmont/internal/filters"
	"rentmont/internal/netutil"
	"rentmont/internal/pyjson"
	"rentmont/internal/sreality"
	"rentmont/internal/storage"
	"rentmont/internal/telegram"
)

type Source interface {
	Search(ctx context.Context, district, ceiling int) ([]map[string]any, error)
	Detail(ctx context.Context, ident string) (map[string]any, error)
	ListingGone(ctx context.Context, ident string) (bool, error)
}

type Extractor interface {
	Extract(ctx context.Context, detail map[string]any) (domain.RentalDetails, error)
}

type Notifier interface {
	SendAd(ctx context.Context, chat string, listing domain.Listing) (domain.TelegramDelivery, error)
	EditAd(ctx context.Context, record domain.NotificationRecord, listing domain.Listing) (map[string]any, error)
	MarkGone(ctx context.Context, record domain.NotificationRecord) error
}

type Service struct {
	Settings      config.Settings
	Store         *storage.Store
	Source        Source
	Extractor     Extractor
	Notifier      Notifier
	FormatVersion int64
	Sleep         func(context.Context, time.Duration)
}

func New(settings config.Settings, store *storage.Store, source Source, extractor Extractor, notifier Notifier) *Service {
	return &Service{Settings: settings, Store: store, Source: source, Extractor: extractor, Notifier: notifier,
		FormatVersion: telegram.FormatVersion, Sleep: netutil.Sleep}
}

func logger() *slog.Logger { return slog.Default().With("module", "monitor") }

func (s *Service) describe(err error) string {
	var rejected *telegram.Rejected
	if errors.As(err, &rejected) {
		return "rejected: " + telegram.SafeDescription(rejected.Description, s.Settings.TelegramBotToken)
	}
	return telegram.SafeDescription(err.Error(), s.Settings.TelegramBotToken)
}

func (s *Service) Scan(ctx context.Context, reextract bool) (domain.ScanStats, error) {
	started := time.Now()
	initial := !s.Store.Initialized()
	logger().Info("Scan started", "initial", initial, "reextract", reextract)
	ads, err := s.Source.Search(ctx, s.Settings.DistrictID, s.Settings.MaxRentKc)
	if err != nil {
		return domain.ScanStats{}, err
	}
	ids := make([]string, len(ads))
	for i, ad := range ads {
		ids[i] = pyjson.Str(ad["hash_id"])
	}
	if err := s.Store.SetCurrentSearch(ids); err != nil {
		return domain.ScanStats{}, err
	}
	logger().Info("Search complete", "listings", len(ads))
	stats := domain.ScanStats{SearchResults: len(ads), Listings: []domain.Listing{}}
	for _, ident := range ids {
		if err := ctx.Err(); err != nil {
			return stats, err
		}
		listing, err := s.scanListing(ctx, ident, initial, reextract, &stats)
		if err != nil {
			if storage.IsStorageError(err) {
				return stats, err
			}
			stats.Errors++
			logger().Warn("Listing failed; will retry", "listing", ident, "error", s.describe(err))
			continue
		}
		if listing != nil {
			stats.Listings = append(stats.Listings, *listing)
		}
	}
	slices.SortFunc(stats.Listings, func(a, b domain.Listing) int { return strings.Compare(a.Source.ID, b.Source.ID) })
	if err := s.Store.ExportListings(filepath.Join(s.Settings.DataDir, "listings.json"), stats.Listings); err != nil {
		return stats, err
	}
	if stats.Succeeded() {
		if err := s.Store.MarkInitialized(); err != nil {
			return stats, err
		}
		if initial {
			logger().Info("Initial scan completed; monitor initialized")
		}
	}
	logger().Info("Scan complete", "results", stats.SearchResults, "eligible", len(stats.Listings),
		"llm_calls", stats.LLMCalls, "cache_hits", stats.CacheHits, "excluded", stats.Excluded,
		"errors", stats.Errors, "elapsed", time.Since(started).Round(100*time.Millisecond))
	return stats, nil
}

func (s *Service) scanListing(ctx context.Context, ident string, initial, reextract bool, stats *domain.ScanStats) (*domain.Listing, error) {
	previous, err := s.Store.GetSource(ident)
	if err != nil {
		return nil, err
	}
	defer s.Sleep(ctx, 500*time.Millisecond)
	logger().Debug("Fetching detail", "listing", ident)
	detail, err := s.Source.Detail(ctx, ident)
	if err != nil {
		return nil, err
	}
	if pyjson.Str(detail["hash_id"]) != ident {
		return nil, errors.New("Sreality detail ID does not match the requested listing")
	}
	if err := s.Store.SaveSource(detail); err != nil {
		return nil, err
	}
	currentHash := extraction.GlobalHash(detail)
	if previous == nil {
		logger().Info("New listing discovered", "listing", ident)
	} else if extraction.GlobalHash(previous.Raw) != currentHash {
		logger().Info("Listing content changed", "listing", ident, "global_hash", currentHash)
	}
	source, err := sreality.ToSourceListing(detail, s.Settings.TelegramMaxPhotos)
	if err != nil {
		return nil, err
	}
	if reason := filters.SourceRejection(source, s.Settings.DistrictID, s.Settings.MaxRentKc); reason != "" {
		logger().Debug("Listing excluded by source filter", "listing", ident, "reason", reason)
		stats.Excluded++
		return nil, nil
	}
	cached, err := s.Store.GetExtraction(ident)
	if err != nil {
		return nil, err
	}
	inputHash := extraction.LLMHash(detail)
	var rental domain.RentalDetails
	if cached != nil && equal(cached.LLMHash, inputHash) && !reextract {
		rental = cached.Details
		stats.CacheHits++
		logger().Debug("Extraction cache hit", "listing", ident)
		if !equal(cached.GlobalHash, currentHash) {
			if err := s.Store.UpdateExtractionGlobalHash(ident, currentHash); err != nil {
				return nil, err
			}
			logger().Info("Listing metadata updated; LLM input unchanged, reusing extraction", "listing", ident)
		}
	} else {
		stats.LLMCalls++
		reason := "changed or missing LLM hash"
		if reextract {
			reason = "forced"
		} else if cached == nil {
			reason = "missing"
		}
		logger().Info("Extracting listing", "listing", ident, "reason", reason)
		if rental, err = s.Extractor.Extract(ctx, detail); err != nil {
			return nil, err
		}
		if err := s.Store.SaveExtraction(ident, rental, &currentHash, &inputHash); err != nil {
			return nil, err
		}
		logger().Info("Extracted listing", "listing", ident, "global_hash", currentHash, "llm_hash", inputHash)
	}
	if reason := filters.RentalRejection(rental, s.Settings.MaxRentKc, s.Settings.MaxRentAndUtilitiesKc); reason != "" {
		logger().Debug("Listing excluded by rental filter", "listing", ident, "reason", reason)
		stats.Excluded++
		return nil, nil
	}
	logger().Debug("Listing eligible", "listing", ident)
	if initial && !s.Settings.NotifyExisting {
		notification, err := s.Store.GetNotification(ident)
		if err != nil {
			return nil, err
		}
		if notification == nil {
			if err := s.Store.Suppress(ident); err != nil {
				return nil, err
			}
			logger().Info("Initial notification suppressed", "listing", ident)
		}
	}
	return &domain.Listing{Source: source, Rental: rental}, nil
}

func (s *Service) Deliver(ctx context.Context, listings []domain.Listing) (bool, error) {
	started := time.Now()
	logger().Debug("Delivery started", "eligible", len(listings))
	goneOK, err := s.updateGone(ctx)
	if err != nil {
		return false, err
	}
	changedOK, err := s.updateChanged(ctx)
	if err != nil {
		return false, err
	}
	sent := 0
	for _, listing := range listings {
		if err := ctx.Err(); err != nil {
			return false, err
		}
		ident := listing.Source.ID
		notification, err := s.Store.GetNotification(ident)
		if err != nil {
			return false, err
		}
		if notification != nil {
			logger().Debug("Delivery skipped", "listing", ident, "status", notification.Status)
			continue
		}
		logger().Info("Sending new notification", "listing", ident)
		delivery, err := s.Notifier.SendAd(ctx, s.Settings.TelegramChatID, listing)
		if err != nil {
			logger().Warn("Telegram delivery failed; will retry", "listing", ident, "error", s.describe(err))
			return false, nil
		}
		cached, err := s.Store.GetExtraction(ident)
		if err != nil {
			return false, err
		}
		var globalHash, llmHash *string
		if cached != nil {
			globalHash, llmHash = cached.GlobalHash, cached.LLMHash
		}
		if err := s.Store.MarkSent(ident, delivery, globalHash, llmHash, s.FormatVersion); err != nil {
			return false, err
		}
		sent++
		logger().Info("Notification sent", "listing", ident, "method", delivery.Method,
			"chat", delivery.ChatID, "messages", delivery.MessageIDs)
		s.Sleep(ctx, 1100*time.Millisecond)
	}
	updated := goneOK && changedOK
	logger().Info("Delivery complete", "sent", sent, "updates_ok", updated,
		"elapsed", time.Since(started).Round(100*time.Millisecond))
	return updated, nil
}

func (s *Service) updateChanged(ctx context.Context) (bool, error) {
	records, err := s.Store.SentNotifications()
	if err != nil {
		return false, err
	}
	succeeded := true
	for _, item := range records {
		ident, record := item.ID, item.Record
		if !s.Store.InCurrentSearch(ident) || record.GoneAt != nil || record.ChatID == nil {
			continue
		}
		source, err := s.Store.GetSource(ident)
		if err != nil {
			return false, err
		}
		cached, err := s.Store.GetExtraction(ident)
		if err != nil {
			return false, err
		}
		if source == nil || cached == nil || !equal(cached.GlobalHash, extraction.GlobalHash(source.Raw)) ||
			!equal(cached.LLMHash, extraction.LLMHash(source.Raw)) {
			logger().Debug("Edit deferred: no matching source/extraction", "listing", ident)
			continue
		}
		formatChanged := record.FormatVersion == nil || *record.FormatVersion != s.FormatVersion
		if same(record.GlobalHash, cached.GlobalHash) && same(record.LLMHash, cached.LLMHash) && !formatChanged {
			continue
		}
		if formatChanged {
			logger().Info("Updating notification format", "listing", ident, "from", record.FormatVersion, "to", s.FormatVersion)
		} else {
			logger().Info("Updating notification for changed listing", "listing", ident)
		}
		if err := s.edit(ctx, ident, record, source, cached); err != nil {
			if storage.IsStorageError(err) {
				return false, err
			}
			succeeded = false
			logger().Warn("Listing update failed; will retry", "listing", ident, "error", s.describe(err))
		}
	}
	return succeeded, nil
}

func (s *Service) edit(ctx context.Context, ident string, record domain.NotificationRecord,
	source *domain.SourceRecord, cached *domain.ExtractionRecord) error {
	sourceListing, err := sreality.ToSourceListing(source.Raw, s.Settings.TelegramMaxPhotos)
	if err != nil {
		return err
	}
	response, err := s.Notifier.EditAd(ctx, record, domain.Listing{Source: sourceListing, Rental: cached.Details})
	if err != nil {
		return err
	}
	if err := s.Store.MarkUpdated(ident, cached.GlobalHash, response, cached.LLMHash, s.FormatVersion); err != nil {
		return err
	}
	logger().Info("Updated changed listing", "listing", ident, "chat", *record.ChatID, "message", record.MessageIDs[0])
	s.Sleep(ctx, 1100*time.Millisecond)
	return nil
}

func (s *Service) updateGone(ctx context.Context) (bool, error) {
	records, err := s.Store.SentNotifications()
	if err != nil {
		return false, err
	}
	succeeded := true
	for _, item := range records {
		if item.Record.GoneNotified || item.Record.ChatID == nil {
			continue
		}
		if err := s.gone(ctx, item.ID, item.Record); err != nil {
			if storage.IsStorageError(err) {
				return false, err
			}
			succeeded = false
			logger().Warn("Gone check/update failed; will retry", "listing", item.ID, "error", s.describe(err))
		}
	}
	return succeeded, nil
}

func (s *Service) gone(ctx context.Context, ident string, record domain.NotificationRecord) error {
	if record.GoneAt == nil {
		if s.Store.InCurrentSearch(ident) {
			return nil
		}
		logger().Debug("Listing absent from search; checking availability", "listing", ident)
		gone, err := s.Source.ListingGone(ctx, ident)
		s.Sleep(ctx, 500*time.Millisecond)
		if err != nil {
			return err
		}
		if !gone {
			logger().Debug("Listing still available despite search absence", "listing", ident)
			return nil
		}
		if record, err = s.Store.MarkGone(ident, false); err != nil {
			return err
		}
		logger().Info("Listing confirmed gone", "listing", ident, "detected", record.GoneAt.Format(time.RFC3339))
	}
	logger().Info("Updating Gone notification", "listing", ident, "detected", record.GoneAt.Format(time.RFC3339))
	if err := s.Notifier.MarkGone(ctx, record); err != nil {
		return err
	}
	if _, err := s.Store.MarkGone(ident, true); err != nil {
		return err
	}
	logger().Info("Gone notification updated", "listing", ident, "chat", *record.ChatID, "message", record.MessageIDs[0])
	s.Sleep(ctx, 1100*time.Millisecond)
	return nil
}

func equal(value *string, expected string) bool { return value != nil && *value == expected }

func same(a, b *string) bool { return (a == nil && b == nil) || (a != nil && b != nil && *a == *b) }
