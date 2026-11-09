package telegram

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"maps"
	"mime/multipart"
	"net/http"
	"net/textproto"
	"regexp"
	"slices"
	"strings"
	"time"

	"rentmont/internal/domain"
	"rentmont/internal/jsonval"
	"rentmont/internal/netutil"
	"rentmont/internal/pyjson"
	"rentmont/internal/sreality"
)

const (
	DefaultBaseURL = "https://api.telegram.org"
	MaxPhotoBytes  = 10 << 20
)

type Client struct {
	HTTP    *http.Client
	Token   string
	BaseURL string
}

func New(client *http.Client, token string) *Client {
	return &Client{HTTP: client, Token: token, BaseURL: DefaultBaseURL}
}

func logger() *slog.Logger { return slog.Default().With("module", "telegram") }

type Rejected struct {
	Code        int64
	Description string
}

func (e *Rejected) Error() string { return "Telegram rejected message" }

func isNotModified(err error) bool {
	var rejected *Rejected
	return errors.As(err, &rejected) && rejected.Code == 400 &&
		strings.Contains(strings.ToLower(rejected.Description), "message is not modified")
}

var urlPattern = regexp.MustCompile(`https?://\S+`)

func SafeDescription(description, token string) string {
	if token != "" {
		description = strings.ReplaceAll(description, token, "[redacted]")
	}
	description = urlPattern.ReplaceAllString(description, "[url]")
	return truncate(strings.Join(strings.Fields(description), " "), 500)
}

type Photo struct {
	Name        string
	Data        []byte
	ContentType string
}

func (c *Client) DownloadPhoto(ctx context.Context, rawURL string) (Photo, error) {
	req, err := netutil.NewRequest(http.MethodGet, sreality.PhotoDownloadURL(rawURL), nil)
	if err != nil {
		return Photo{}, err
	}
	resp, err := netutil.Do(ctx, c.HTTP, req, 30*time.Second, MaxPhotoBytes)
	if errors.Is(err, netutil.ErrTooLarge) {
		return Photo{}, errors.New("photo exceeds 10 MB")
	}
	if err != nil {
		return Photo{}, err
	}
	if err := resp.CheckStatus(); err != nil {
		return Photo{}, err
	}
	switch {
	case bytes.HasPrefix(resp.Body, []byte("\xff\xd8\xff")):
		return Photo{"photo.jpg", resp.Body, "image/jpeg"}, nil
	case bytes.HasPrefix(resp.Body, []byte("\x89PNG\r\n\x1a\n")):
		return Photo{"photo.png", resp.Body, "image/png"}, nil
	}
	return Photo{}, errors.New("downloaded content is not a JPEG or PNG")
}

type attachment struct {
	field string
	photo Photo
}

func (c *Client) call(ctx context.Context, method string, payload map[string]any, files []attachment) (map[string]any, error) {
	logger().Debug("Telegram request", "method", method)
	var body bytes.Buffer
	contentType := "application/json"
	timeout := 30 * time.Second
	if len(files) > 0 {
		form := multipart.NewWriter(&body)
		for _, key := range slices.Sorted(maps.Keys(payload)) {
			if err := form.WriteField(key, fmt.Sprint(payload[key])); err != nil {
				return nil, err
			}
		}
		for _, file := range files {
			header := textproto.MIMEHeader{}
			header.Set("Content-Disposition", fmt.Sprintf(`form-data; name="%s"; filename="%s"`, file.field, file.photo.Name))
			header.Set("Content-Type", file.photo.ContentType)
			part, err := form.CreatePart(header)
			if err != nil {
				return nil, err
			}
			if _, err := part.Write(file.photo.Data); err != nil {
				return nil, err
			}
		}
		if err := form.Close(); err != nil {
			return nil, err
		}
		contentType = form.FormDataContentType()
		timeout = 90 * time.Second
	} else if err := json.NewEncoder(&body).Encode(payload); err != nil {
		return nil, err
	}
	req, err := netutil.NewRequest(http.MethodPost, c.BaseURL+"/bot"+c.Token+"/"+method, io.Reader(&body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", contentType)
	resp, err := netutil.Do(ctx, c.HTTP, req, timeout, netutil.DefaultLimit)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode == 400 {
		result, _ := jsonval.DecodeObject(resp.Body)
		return nil, &Rejected{Code: 400, Description: description(result)}
	}
	if err := resp.CheckStatus(); err != nil {
		return nil, err
	}
	result, err := jsonval.DecodeObject(resp.Body)
	if err != nil {
		return nil, err
	}
	if !jsonval.Truthy(result["ok"]) {
		code, _ := jsonval.Int(result["error_code"])
		return nil, &Rejected{Code: code, Description: description(result)}
	}
	logger().Debug("Telegram request succeeded", "method", method)
	return result, nil
}

func description(result map[string]any) string {
	value, ok := result["description"]
	if !ok {
		return "No description returned"
	}
	return pyjson.Str(value)
}

func deliveryFrom(method domain.TelegramMethod, result map[string]any) (domain.TelegramDelivery, error) {
	raw, ok := result["result"]
	if !ok {
		return domain.TelegramDelivery{}, errors.New("Telegram response has no result")
	}
	messages := []any{raw}
	if method == domain.SendMediaGroup {
		if messages, ok = raw.([]any); !ok {
			return domain.TelegramDelivery{}, errors.New("Telegram album response is not a list")
		}
	}
	if len(messages) == 0 {
		return domain.TelegramDelivery{}, errors.New("Telegram returned no messages")
	}
	delivery := domain.TelegramDelivery{Method: method, Response: result}
	for i, item := range messages {
		message := jsonval.Object(item)
		chatID, okChat := jsonval.Int(jsonval.Object(message["chat"])["id"])
		messageID, okMessage := jsonval.Int(message["message_id"])
		if !okChat || !okMessage {
			return domain.TelegramDelivery{}, errors.New("Telegram message has no chat or message ID")
		}
		if i == 0 {
			delivery.ChatID = chatID
		} else if chatID != delivery.ChatID {
			return domain.TelegramDelivery{}, errors.New("Telegram returned messages from different chats")
		}
		delivery.MessageIDs = append(delivery.MessageIDs, messageID)
	}
	return delivery, delivery.Validate()
}

func (c *Client) send(ctx context.Context, method domain.TelegramMethod, payload map[string]any, files []attachment) (domain.TelegramDelivery, error) {
	result, err := c.call(ctx, string(method), payload, files)
	if err != nil {
		return domain.TelegramDelivery{}, err
	}
	return deliveryFrom(method, result)
}

func (c *Client) SendAd(ctx context.Context, chat string, listing domain.Listing) (domain.TelegramDelivery, error) {
	text := FormatMessage(listing)
	var photos []Photo
	for index, rawURL := range listing.Source.Photos {
		photo, err := c.DownloadPhoto(ctx, rawURL)
		if err != nil {
			if ctx.Err() != nil {
				return domain.TelegramDelivery{}, ctx.Err()
			}
			logger().Warn("Photo download failed; skipping", "listing", listing.Source.ID, "photo", index+1, "error", err)
			continue
		}
		logger().Debug("Photo downloaded", "listing", listing.Source.ID, "photo", index+1, "bytes", len(photo.Data))
		photos = append(photos, photo)
	}
	if len(photos) > 0 {
		var method domain.TelegramMethod
		var payload map[string]any
		var files []attachment
		if len(photos) == 1 {
			method = domain.SendPhoto
			payload = map[string]any{"chat_id": chat, "caption": text, "parse_mode": "HTML"}
			files = []attachment{{"photo", photos[0]}}
		} else {
			method = domain.SendMediaGroup
			media := make([]map[string]any, len(photos))
			for i, photo := range photos {
				name := fmt.Sprintf("photo%d", i)
				files = append(files, attachment{name, photo})
				media[i] = map[string]any{"type": "photo", "media": "attach://" + name}
			}
			media[0]["caption"] = text
			media[0]["parse_mode"] = "HTML"
			encoded, err := json.Marshal(media)
			if err != nil {
				return domain.TelegramDelivery{}, err
			}
			payload = map[string]any{"chat_id": chat, "media": string(encoded)}
		}
		delivery, err := c.send(ctx, method, payload, files)
		var rejected *Rejected
		if !errors.As(err, &rejected) || rejected.Code != 400 {
			return delivery, err
		}
		logger().Warn("Telegram rejected photos; sending text instead", "listing", listing.Source.ID,
			"method", method, "photos", len(photos), "description", SafeDescription(rejected.Description, c.Token))
	}
	return c.send(ctx, domain.SendMessage, map[string]any{
		"chat_id": chat, "text": text, "parse_mode": "HTML",
		"link_preview_options": map[string]any{"is_disabled": true},
	}, nil)
}

func editTarget(record domain.NotificationRecord) (method, field, entities string, err error) {
	if record.ChatID == nil || len(record.MessageIDs) == 0 || record.TelegramMethod == nil {
		return "", "", "", errors.New("missing original delivery metadata")
	}
	if *record.TelegramMethod == domain.SendMessage {
		return "editMessageText", "text", "entities", nil
	}
	return "editMessageCaption", "caption", "caption_entities", nil
}

func (c *Client) EditAd(ctx context.Context, record domain.NotificationRecord, listing domain.Listing) (map[string]any, error) {
	method, field, _, err := editTarget(record)
	if err != nil {
		return nil, err
	}
	payload := map[string]any{"chat_id": *record.ChatID, "message_id": record.MessageIDs[0],
		field: FormatMessage(listing), "parse_mode": "HTML"}
	if field == "text" {
		payload["link_preview_options"] = map[string]any{"is_disabled": true}
	}
	result, err := c.call(ctx, method, payload, nil)
	if isNotModified(err) {
		return record.TelegramResponse, nil
	}
	return result, err
}

func (c *Client) MarkGone(ctx context.Context, record domain.NotificationRecord) error {
	method, field, entities, err := editTarget(record)
	if err != nil {
		return err
	}
	if record.GoneAt == nil || record.TelegramResponse == nil {
		return errors.New("missing original delivery metadata")
	}
	original := record.TelegramResponse["result"]
	if list, ok := original.([]any); ok && len(list) > 0 {
		original = list[0]
	}
	message := jsonval.Object(original)
	text, ok := message[field].(string)
	if !ok {
		return errors.New("original message has no " + field)
	}
	payload := map[string]any{"chat_id": *record.ChatID, "message_id": record.MessageIDs[0],
		field: text + "\n🔴 Gone " + record.GoneAt.Format("02.01.2006")}
	if value, ok := message[entities]; ok {
		payload[entities] = value
	}
	if field == "text" {
		payload["link_preview_options"] = map[string]any{"is_disabled": true}
	}
	if _, err := c.call(ctx, method, payload, nil); err != nil && !isNotModified(err) {
		return err
	}
	return nil
}
