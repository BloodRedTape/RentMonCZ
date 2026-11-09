package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"rentmont/internal/config"
	"rentmont/internal/extraction"
	"rentmont/internal/monitor"
	"rentmont/internal/sreality"
	"rentmont/internal/storage"
	"rentmont/internal/telegram"
)

func main() {
	os.Exit(run())
}

func run() int {
	once := flag.Bool("once", false, "Run one scan and exit")
	reextract := flag.Bool("reextract", false, "Reparse current listings once, ignoring saved facts")
	debug := flag.Bool("debug", false, "Enable detailed application logs")
	flag.Parse()

	level := slog.LevelInfo
	if *debug {
		level = slog.LevelDebug
	}
	slog.SetDefault(slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: level})))

	settings, err := config.Load()
	if err != nil {
		var invalid *config.ValidationError
		if errors.As(err, &invalid) {
			fmt.Fprintf(os.Stderr, "rentmont: check .env configuration: %v\n", invalid.Fields)
		} else {
			fmt.Fprintf(os.Stderr, "rentmont: %v\n", err)
		}
		return 2
	}
	store, err := storage.Open(settings.DataDir)
	if err != nil {
		fmt.Fprintf(os.Stderr, "rentmont: %v\n", err)
		return 2
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	client := &http.Client{}
	service := monitor.New(settings, store,
		sreality.New(client),
		extraction.NewClient(client, settings.OpenAIAPIKey, settings.OpenAIModel, settings.OpenAIBaseURL),
		telegram.New(client, settings.TelegramBotToken))

	slog.Info("Monitor starting", "once", *once, "poll_seconds", settings.PollSeconds,
		"district", settings.DistrictID, "model", settings.OpenAIModel, "initialized", store.Initialized())
	return loop(ctx, service, settings, *once, *reextract)
}

func loop(ctx context.Context, service *monitor.Service, settings config.Settings, once, reextract bool) int {
	for {
		started := time.Now()
		stats, scanErr := service.Scan(ctx, reextract)
		if ctx.Err() != nil {
			slog.Info("Stopped")
			return 0
		}
		if scanErr != nil {
			if storage.IsStorageError(scanErr) {
				slog.Error("Storage failure; stopping", "error", scanErr)
				return 1
			}
			slog.Error("Scan failed; retry next cycle", "error", scanErr)
			slog.Warn("Delivery skipped: scan failed; stale listings will not be sent")
		} else {
			reextract = false
		}

		delivered := false
		if scanErr == nil {
			var err error
			delivered, err = service.Deliver(ctx, stats.Listings)
			if ctx.Err() != nil {
				slog.Info("Stopped")
				return 0
			}
			if err != nil {
				slog.Error("Delivery failure; stopping", "error", err)
				return 1
			}
		}

		scanOK := scanErr == nil && stats.Succeeded()
		slog.Info("Cycle complete", "scan_ok", scanOK, "delivery_ok", delivered,
			"elapsed", time.Since(started).Round(100*time.Millisecond))
		if once {
			if !scanOK || !delivered {
				slog.Error("One-shot run finished with errors")
				return 1
			}
			slog.Info("One-shot run finished successfully")
			return 0
		}

		delay := time.Duration(settings.PollSeconds)*time.Second - time.Since(started)
		slog.Debug("Next scan scheduled", "delay", delay.Round(time.Second))
		select {
		case <-ctx.Done():
			slog.Info("Stopped")
			return 0
		case <-time.After(max(0, delay)):
		}
	}
}
