# RentMont

Sreality.cz monitor: rentals of **entire apartments** in **Brno-město**, base rent **strictly < 13,500 Kč/month**, checked every **10 minutes**. Utilities are not included in the threshold. Facts are extracted with **gpt-5.4-mini**; notifications are sent to Telegram with up to **3 photos** by default (`TELEGRAM_MAX_PHOTOS`, 0–10).

The service is written in Go and lives in `service/`.

## Running

Requires Go 1.24+. Copy `.env.example` to `.env` and fill in `OPENAI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. A ChatGPT subscription does not replace OpenAI API billing. Send `/start` to the bot before using a private chat.

Settings are read from `.env` in the current directory and from environment variables (environment variables take precedence), so run from the repository root:

```bash
go -C service build -o ../rentmont ./cmd/rentmont
./rentmont --once
./rentmont
# Force re-extraction of current listings:
./rentmont --once --reextract
```

`OPENAI_BASE_URL` is also supported (default `https://api.openai.com/v1`). `Ctrl+C`/`SIGTERM` stop the process.

The process must keep running. For 24/7 operation use a VPS/systemd, Windows Task Scheduler or Docker. **Only one process per DATA_DIR**: there is no inter-process locking.

By default the first successful check stores matching listings as `suppressed`, without Telegram spam. To get notifications right away, set `NOTIFY_EXISTING=true` before the first run. The initial processing still consumes the OpenAI API.

## Docker

Requires Docker Engine / Docker Desktop (Linux containers) and Docker Compose v2.
Settings are taken from `.env`; keys and local data are not copied into the image.
`./data` is mounted into the container as `/app/data`, so state persists when the container is recreated. `DATA_DIR` is fixed to `/app/data` inside Compose.

```bash
docker compose up -d --build
docker compose logs -f --tail=100
docker compose down
```

The image is multi-stage: built in `golang:1.27-alpine`, the static binary runs in `gcr.io/distroless/static-debian12` (as root inside the container). In rootless Docker this is not the host root; the mounted `./data` directory must be accessible to the user who runs Docker. In regular rootful Docker, created files may be owned by the host root.
Do not run the local monitor at the same time as the container: there must be only one process per data directory.

One-off check (stop the long-running container first):

```bash
docker compose stop rentmont
docker compose run --rm --build rentmont --once
# Force refresh of source and extraction:
docker compose run --rm rentmont --once --reextract
docker compose up -d
```

After changing code, rebuild the image: `docker compose up -d --build`.
After changing `.env`, recreate the container: `docker compose up -d --force-recreate`.
The `unless-stopped` policy restarts the container after the process exits, including on errors; check the logs for the cause. Log rotation is limited to three files of 10 MB each.

## Separation of concerns

```text
service/
  cmd/rentmont/         # CLI, polling loop, signals
  internal/config/      # settings from .env/environment
  internal/domain/      # contracts for facts, files and shared state; strict JSON validation
  internal/filters/     # pure selection policy, no persistence of decisions
  internal/sreality/    # external API, raw → SourceListing adapter, distance to center
  internal/extraction/  # hashes, input field whitelist, OpenAI → RentalDetails
  internal/telegram/    # formatting, photo download, sending without DB access
  internal/storage/     # separate atomic files per listing
  internal/monitor/     # refresh, selection, delivery orchestration
  internal/pyjson/      # Python-style json.dumps/str() for compatible hashes
  internal/jsonval/     # access to untyped JSON values
  internal/netutil/     # HTTP with limits; errors without URLs (Telegram URLs contain the token)
```

`SourceListing` is a projection of the raw listing for filtering and display. `RentalDetails` holds only extracted facts: `payments`, `is_room_rent`, `area_m2`, `available_from`, `furnishing`, `amenities`. `Listing` is the public `{source, rental}` projection for Telegram and export. The service's external dependencies (`Source`, `Extractor`, `Notifier`) are defined as interfaces in `internal/monitor`.

## Storage: one folder per listing

```text
data/
  state.json
  listings.json
  <id>/
    source.json
    extraction.json
    notification.json
```

The format is compatible with the Python version: a `data/` directory created by it is read without migration.

`state.json` contains only the monitor's shared state, format **v4**:

```json
{
  "schema_version": 4,
  "initialized": false,
  "current_search_ids": []
}
```

Files in each listing's folder are independent:

- `source.json`: `{raw, first_seen_at, last_seen_at}`. The full detail from the API, regardless of the filtering result. `last_seen_at` is the time of the last detail download. Data is refreshed every cycle; `first_seen_at` is preserved.
- `extraction.json`: `{global_hash, llm_hash, details, extracted_at}`. A successful LLM result, including rooms, unknown rent and over-budget listings. The file is absent until the first successful extraction.
- `notification.json`: `{status, sent_at, chat_id, message_ids, telegram_method, telegram_response, format_version, gone_at, gone_notified}`. For `sent`, the send time, actual chat ID, all message IDs, API method and full Telegram JSON response are stored. For an album, `message_ids[0]` is the message with the caption. For `suppressed`, send metadata is empty. A missing file means "not notified yet".

`current_search_ids` is updated only after the search completes successfully. Disappeared listings remain in the archive but do not take part in the current export and delivery. A search error keeps the previous set of IDs, but no delivery happens in that cycle.

Each file is read on demand and saved atomically: a temporary file is written and replaces the main one. Refreshing the source does not overwrite the extraction or notification. Records are strictly validated (unknown fields, required keys, types, ranges); the ID must be digits only and match the ID inside source. On a read/save error the monitor stops. Corrupted files are not reset automatically.

There is no transaction across files: after a crash a source may be left without an extraction — the next cycle will finish processing. Delivery state is saved only after a successful send.

The raw API response may contain contact details: the database is local and `data/` is excluded from Git. Only a whitelist of description and payment fields is sent to OpenAI — no contacts or photos.

## Budget limits

`MAX_RENT_KC` is the strict base rent limit (default 13500).
You can additionally set `MAX_RENT_AND_UTILITIES_KC=17000` in `.env`: if **both** amounts are known, a listing passes only when `rent_kc + utilities_kc < MAX_RENT_AND_UTILITIES_KC`. When utilities are unknown, the extra check is skipped; unknown rent is still rejected by the main filter. Commission and deposit are not included in the sum.

If `MAX_RENT_AND_UTILITIES_KC` is not set, the extra limit is disabled. The value must be a positive integer. The check is performed on extracted facts, does not restrict the API search, and does not require calling the LLM again when the limit changes.

## Updates and filtering

1. Fetch the full current search and save its IDs.
2. Fetch fresh detail for each current listing and save the raw response.
3. Build a `SourceListing` and check category, district and API price.
4. Compute `global_hash`: SHA-256 of the title, description, price (`price_czk`, unit and currency), price note, monthly costs (`cost_of_living`), commission (`commission`), deposit, furnishing (`furnished`), the `cellar`/`balcony`/`loggia`/`terrace` flags and the `since`/`edited` dates. Separately compute `llm_hash` from the extraction contract version (`extraction.Version`) and all `extraction.Fields` actually passed to the LLM: including area, move-in date and the no-commission flag, but without publish/edit dates. Use the extraction with a matching `llm_hash`; otherwise call the LLM.
5. Immediately save a successful extraction before applying the business filter.
6. Apply the current rules to the facts; collect the matching listings for this cycle.
7. Update the export, edit sent listings that changed, and send new ones without `sent`/`suppressed`.

Hashes are serialized the same way as `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"))` in Python (`internal/pyjson`). Since the amenity flags were added to `global_hash` and `extraction.Version` was bumped to 3, hashes saved by the Python version no longer match: each listing is re-extracted and its message re-edited once.

Both hashes are stored in `extraction.json`, and their sent versions in `notification.json`. A mismatch of either hash triggers editing the original message or the first photo's caption, without resending or replacing photos. If only dates changed, LLM facts are reused and `global_hash` is updated without changing `extracted_at`. Photos and their limit are not part of the hashes and do not trigger edits. The legacy `content_hash` field is dropped on read. A Telegram error leaves the update for a retry. Old extractions without `llm_hash` or with a previous contract version are recomputed once. Changing the model or prompt without bumping the version requires `--reextract`: the flag forces re-parsing of current listings that passed the initial filter. On an LLM error the old facts are kept but not used to update a changed listing; the next cycle will retry extraction.

OpenAI requests use Structured Outputs (strict `RentalDetails` JSON schema) and are retried up to twice on network errors, 408/409/429 and 5xx. The response is additionally validated: negative amounts, invalid dates and unknown values are rejected, zero amounts become `null`. Filters are applied every cycle regardless of stored facts. A failed delivery does not create a `sent` record, so it is retried after the next successful check.

The search is limited by the API price before the LLM: listings whose total API price is above the threshold but whose base rent is below it may be missed.

## Export and facts

`data/listings.json` is an array of matching listings from the last completed check, each in `{source, rental}` format. Internal states are not exported.

- `source`: ID, title, link, categories, district, API price, up to `TELEGRAM_MAX_PHOTOS` photos, distance, publish/edit dates.
- `source.furnishing`: `furnished`, `partially_furnished`, `unfurnished` or `null`. Taken from the structured API field `furnished.value` (1, 3, 2 respectively); 0, missing or an unknown code — `null`. Takes priority when displayed in Telegram and is included in `global_hash`, but is not passed to the LLM.
- `rental.furnishing`: the same values, extracted by the LLM from explicit mentions in the title/description. Used in Telegram only when `source.furnishing=null`.
- `source.cellar`/`source.balcony`: `true` or `null`. Taken from the API flags `cellar` and `balcony`/`loggia`/`terrace`; only `true` is trusted, because Sreality reports unfilled checkboxes as `false`. Not passed to the LLM; when `true`, they take priority over `rental.amenities`.
- `rental.amenities`: `washing_machine`, `microwave`, `oven`, `storage_room` (komora/spíž/kladovka), `cellar`, `balcony` (including loggia/terrace) are `true`/`false`/`null`, where `false` means explicitly absent and `null` means not mentioned, optional, only installable or shared. `stove`: `gas`, `electric`, `portable_electric` (vařič/plotýnka) or `null`. `bathing`: `shower`, `bathtub`, `shower_and_bathtub` or `null`. Extracted only from the title/description, never from photos. Old extractions without `amenities` load with all values `null`.
- `rental.payments`: `rent_kc`, `utilities_kc`, `commission_kc`, `deposit_kc`. All amounts in Kč. Rent and utilities are monthly; commission and deposit are one-time. Unknown, ambiguous and zero amounts are `null`.
- `is_room_rent=true` for explicit rental of a room/bed; the `Pokoj` category is rejected before the LLM.
- `area_m2` — area from the listing, unknown — `null`.
- `available_from`: `null`, `"asap"` or a `YYYY-MM-DD` date, without guessing incomplete dates.
- `published_at`/`updated_at` are taken from `since`/`edited`; missing/invalid — `null`.
- `distance_to_center_km` — straight-line distance to náměstí Svobody (49.1951, 16.6080), rounded to 0.1 km; without coordinates — `null`.

## Telegram

The template version is set by the `FormatVersion` constant in `service/internal/telegram/format.go`.
Bump it whenever formatting changes. The successfully sent/updated version is stored
in `notification.json` as `format_version`. A version mismatch (including a missing field)
triggers editing the text/caption of the existing message, even if both hashes match.
The LLM is not called for this, and no new messages or photos are sent. A Telegram
"message is not modified" response is also treated as success.

The title is a clickable link. The message contains rent and utilities with a total, commission and deposit without summing, move-in, furnishing, location and dates. Location: `📍 Loc: Černá Pole ≈1.2 km to center`, where the district name links to the point on Google Maps (when coordinates are available). HTML is escaped; the title is limited to 180 characters.

Photos: `TELEGRAM_MAX_PHOTOS` sets the maximum number of JPEG/PNG images (default 3, range 0–10; 0 — text only), up to 10 MB each. They are downloaded locally and sent as multipart via `sendPhoto`/`sendMediaGroup`. For `*.sdn.cz`, the `fl=res,1200,1200,1|shr,,20|jpg,80` processing is applied. A failed image is skipped. With no photos or on an explicit upload rejection (400), text is sent. On a timeout/rate limit the notification stays unsent.

To edit a text message, `chat_id` and `message_ids[0]` are used with `editMessageText`; for a photo/album, with `editMessageCaption`. When a listing disappears from search and the detail API confirms 404/410, `🔴 Gone DD.MM.YYYY` is appended while preserving the original formatting; the first-detection date and edit status are stored in `gone_at`/`gone_notified`. A crash between the message being accepted and the status being saved may cause duplicates: the API does not provide exactly-once. The bot token is not stored; the Telegram response may contain chat data, so do not publish `data/`.

## Logs

Logs are written to stderr via `log/slog` in `key=value` format (time, level, message, `module`, fields). The default is `INFO`: startup, scan and cycle start/summary, listing discovery, hash changes, extractions, notification suppression and sending, updates and `Gone`. Errors and retries are `WARN`, cycle/storage failures are `ERROR`. Details (pagination, detail/photo downloads, cache, rejection reasons, file saves) are available with `--debug`.

Listing texts, contacts and tokens are not logged; HTTP errors do not contain URLs. Listing, chat and message IDs remain in the logs.

## Checks

```bash
cd service
go vet ./...
go test ./...
```

Tests do not use keys or the external network. `service/internal/testdata/tricky.json` and the reference hashes in `extraction_test.go` were produced by the Python implementation. The Sreality API is not guaranteed to be a stable contract. Respect the site's rules: bypassing CAPTCHAs/restrictions is not implemented. LLM results may contain errors. `.env` and `data/` are excluded from Git.
