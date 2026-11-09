package extraction

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"math/rand/v2"
	"net/http"
	"strconv"
	"time"

	"rentmont/internal/domain"
	"rentmont/internal/netutil"
	"rentmont/internal/sreality"
)

type Client struct {
	HTTP       *http.Client
	APIKey     string
	Model      string
	BaseURL    string
	Timeout    time.Duration
	MaxRetries int
	Sleep      func(context.Context, time.Duration)
}

func NewClient(client *http.Client, apiKey, model, baseURL string) *Client {
	return &Client{HTTP: client, APIKey: apiKey, Model: model, BaseURL: baseURL,
		Timeout: 60 * time.Second, MaxRetries: 2, Sleep: netutil.Sleep}
}

type completion struct {
	Choices []struct {
		FinishReason string `json:"finish_reason"`
		Message      struct {
			Content *string `json:"content"`
			Refusal *string `json:"refusal"`
		} `json:"message"`
	} `json:"choices"`
}

func (c *Client) Extract(ctx context.Context, detail map[string]any) (domain.RentalDetails, error) {
	body, err := json.Marshal(map[string]any{
		"model": c.Model,
		"messages": []map[string]string{
			{"role": "system", "content": Prompt},
			{"role": "user", "content": Content(detail)},
		},
		"response_format": map[string]any{
			"type": "json_schema",
			"json_schema": map[string]any{
				"name": "RentalDetails", "strict": true, "schema": json.RawMessage(schema),
			},
		},
	})
	if err != nil {
		return domain.RentalDetails{}, err
	}
	resp, err := c.post(ctx, body)
	if err != nil {
		return domain.RentalDetails{}, err
	}
	var result completion
	if err := json.Unmarshal(resp.Body, &result); err != nil {
		return domain.RentalDetails{}, errors.New("OpenAI returned an invalid response")
	}
	if len(result.Choices) == 0 {
		return domain.RentalDetails{}, errors.New("OpenAI returned no choices")
	}
	choice := result.Choices[0]
	switch {
	case choice.FinishReason == "length":
		return domain.RentalDetails{}, errors.New("OpenAI response was truncated")
	case choice.FinishReason == "content_filter":
		return domain.RentalDetails{}, errors.New("OpenAI response was filtered")
	case choice.Message.Refusal != nil && *choice.Message.Refusal != "":
		return domain.RentalDetails{}, errors.New("OpenAI refused the request")
	case choice.Message.Content == nil:
		return domain.RentalDetails{}, errors.New("OpenAI did not return parsed rental details")
	}
	var details domain.RentalDetails
	if err := json.Unmarshal([]byte(*choice.Message.Content), &details); err != nil {
		return domain.RentalDetails{}, errors.New("OpenAI returned invalid rental details: " + err.Error())
	}
	if sreality.IsRoomCategory(detail) {
		details.IsRoomRent = true
	}
	return details, nil
}

func (c *Client) post(ctx context.Context, body []byte) (*netutil.Response, error) {
	for attempt := 0; ; attempt++ {
		req, err := netutil.NewRequest(http.MethodPost, c.BaseURL+"/chat/completions", bytes.NewReader(body))
		if err != nil {
			return nil, err
		}
		req.Header.Set("Authorization", "Bearer "+c.APIKey)
		req.Header.Set("Content-Type", "application/json")
		resp, err := netutil.Do(ctx, c.HTTP, req, c.Timeout, netutil.DefaultLimit)
		retryable := err != nil && ctx.Err() == nil && !errors.Is(err, netutil.ErrTooLarge)
		if err == nil {
			if resp.StatusCode >= 200 && resp.StatusCode < 300 {
				return resp, nil
			}
			err = &netutil.StatusError{Code: resp.StatusCode}
			retryable = shouldRetry(resp)
		}
		if !retryable || attempt >= c.MaxRetries {
			return nil, err
		}
		c.Sleep(ctx, retryDelay(attempt, resp))
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
	}
}

func shouldRetry(resp *netutil.Response) bool {
	switch resp.Header.Get("x-should-retry") {
	case "true":
		return true
	case "false":
		return false
	}
	code := resp.StatusCode
	return code == 408 || code == 409 || code == 429 || code >= 500
}

func retryDelay(attempt int, resp *netutil.Response) time.Duration {
	if resp != nil {
		if ms, err := strconv.ParseFloat(resp.Header.Get("retry-after-ms"), 64); err == nil && ms > 0 && ms <= 60000 {
			return time.Duration(ms * float64(time.Millisecond))
		}
		if s, err := strconv.ParseFloat(resp.Header.Get("retry-after"), 64); err == nil && s > 0 && s <= 60 {
			return time.Duration(s * float64(time.Second))
		}
	}
	delay := min(0.5*float64(int(1)<<attempt), 8)
	return time.Duration(delay * (1 - 0.25*rand.Float64()) * float64(time.Second))
}
