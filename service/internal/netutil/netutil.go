// Package netutil performs bounded HTTP requests whose errors never contain
// request URLs (the Telegram URL embeds the bot token).
package netutil

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"time"
)

const (
	UserAgent    = "RentMont/1.0"
	DefaultLimit = 64 << 20
)

var ErrTooLarge = errors.New("response body exceeds limit")

type Response struct {
	StatusCode int
	Header     http.Header
	Body       []byte
}

type StatusError struct{ Code int }

func (e *StatusError) Error() string { return fmt.Sprintf("HTTP status %d", e.Code) }

type RequestError struct {
	Timeout bool
	cause   error
}

func (e *RequestError) Error() string { return "request failed: " + e.cause.Error() }
func (e *RequestError) Unwrap() error { return e.cause }

func (r *Response) CheckStatus() error {
	if r.StatusCode < 200 || r.StatusCode >= 300 {
		return &StatusError{Code: r.StatusCode}
	}
	return nil
}

func Do(ctx context.Context, client *http.Client, req *http.Request, timeout time.Duration, limit int64) (*Response, error) {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	if req.Header.Get("User-Agent") == "" {
		req.Header.Set("User-Agent", UserAgent)
	}
	resp, err := client.Do(req.WithContext(ctx))
	if err != nil {
		return nil, sanitize(err)
	}
	defer resp.Body.Close()
	if resp.ContentLength > limit {
		return nil, ErrTooLarge
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, limit+1))
	if err != nil {
		return nil, sanitize(err)
	}
	if int64(len(body)) > limit {
		return nil, ErrTooLarge
	}
	return &Response{StatusCode: resp.StatusCode, Header: resp.Header, Body: body}, nil
}

func NewRequest(method, rawURL string, body io.Reader) (*http.Request, error) {
	req, err := http.NewRequest(method, rawURL, body)
	if err != nil {
		return nil, errors.New("invalid request URL")
	}
	return req, nil
}

func Sleep(ctx context.Context, d time.Duration) {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
	case <-timer.C:
	}
}

func sanitize(err error) error {
	var urlErr *url.Error
	if errors.As(err, &urlErr) {
		return &RequestError{Timeout: urlErr.Timeout(), cause: urlErr.Err}
	}
	return &RequestError{Timeout: errors.Is(err, context.DeadlineExceeded), cause: err}
}
