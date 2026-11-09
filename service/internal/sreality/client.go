package sreality

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"net/url"
	"strconv"
	"time"

	"rentmont/internal/jsonval"
	"rentmont/internal/netutil"
	"rentmont/internal/pyjson"
)

const DefaultBaseURL = "https://www.sreality.cz/api/v1"

var errStatus = errors.New("Sreality returned a non-success status")

type Client struct {
	HTTP    *http.Client
	BaseURL string
}

func New(client *http.Client) *Client {
	return &Client{HTTP: client, BaseURL: DefaultBaseURL}
}

func logger() *slog.Logger { return slog.Default().With("module", "sreality") }

func (c *Client) get(ctx context.Context, path string, params url.Values) (*netutil.Response, error) {
	target := c.BaseURL + "/" + path
	if len(params) > 0 {
		target += "?" + params.Encode()
	}
	req, err := netutil.NewRequest(http.MethodGet, target, nil)
	if err != nil {
		return nil, err
	}
	return netutil.Do(ctx, c.HTTP, req, 30*time.Second, netutil.DefaultLimit)
}

func (c *Client) apiJSON(ctx context.Context, path string, params url.Values) (map[string]any, error) {
	resp, err := c.get(ctx, path, params)
	if err != nil {
		return nil, err
	}
	if err := resp.CheckStatus(); err != nil {
		return nil, err
	}
	data, err := jsonval.DecodeObject(resp.Body)
	if err != nil {
		return nil, err
	}
	if status := apiStatus(data); status != 200 {
		return nil, errStatus
	}
	return data, nil
}

func apiStatus(data map[string]any) float64 {
	value, ok := data["status_code"]
	if !ok {
		return 200
	}
	status, ok := jsonval.Float(value)
	if !ok {
		return -1
	}
	return status
}

func (c *Client) Detail(ctx context.Context, ident string) (map[string]any, error) {
	data, err := c.apiJSON(ctx, "estates/"+ident, nil)
	if err != nil {
		return nil, err
	}
	detail, ok := data["result"].(map[string]any)
	if !ok {
		return nil, errors.New("Sreality detail has no result")
	}
	return detail, nil
}

func (c *Client) ListingGone(ctx context.Context, ident string) (bool, error) {
	resp, err := c.get(ctx, "estates/"+ident, nil)
	if err != nil {
		return false, err
	}
	if resp.StatusCode == 404 || resp.StatusCode == 410 {
		return true, nil
	}
	if err := resp.CheckStatus(); err != nil {
		return false, err
	}
	data, err := jsonval.DecodeObject(resp.Body)
	if err != nil {
		return false, err
	}
	switch apiStatus(data) {
	case 404, 410:
		return true, nil
	case 200:
	default:
		return false, errStatus
	}
	result := jsonval.Object(data["result"])
	if result == nil || pyjson.Str(result["hash_id"]) != ident {
		return false, errors.New("Sreality detail ID does not match the requested listing")
	}
	return false, nil
}

func (c *Client) Search(ctx context.Context, district, ceiling int) ([]map[string]any, error) {
	var order []string
	ads := map[string]map[string]any{}
	offset := 0
	for {
		logger().Debug("Fetching search page", "offset", offset)
		data, err := c.apiJSON(ctx, "estates/search", url.Values{
			"category_main_cb":     {"1"},
			"category_type_cb":     {"2"},
			"locality_district_id": {strconv.Itoa(district)},
			"price_to":             {strconv.Itoa(ceiling - 1)},
			"limit":                {"100"},
			"offset":               {strconv.Itoa(offset)},
		})
		if err != nil {
			return nil, err
		}
		rows, ok := data["results"].([]any)
		if !ok {
			return nil, errors.New("Sreality search response has no results")
		}
		total, err := jsonval.PyInt(jsonval.Object(data["pagination"])["total"])
		if err != nil {
			return nil, errors.New("Sreality search response has no pagination total")
		}
		logger().Debug("Search page received", "rows", len(rows), "total", total, "offset", offset)
		if len(rows) == 0 && int64(offset) < total {
			return nil, errors.New("unexpected empty search page")
		}
		for _, row := range rows {
			ad, ok := row.(map[string]any)
			if !ok {
				return nil, errors.New("Sreality search row is not an object")
			}
			if !matches(ad, district, ceiling) {
				continue
			}
			if _, ok := ad["hash_id"]; !ok {
				return nil, errors.New("Sreality search row has no hash_id")
			}
			ident := pyjson.Str(ad["hash_id"])
			if _, seen := ads[ident]; !seen {
				order = append(order, ident)
			}
			ads[ident] = ad
		}
		offset += len(rows)
		if int64(offset) >= total {
			result := make([]map[string]any, 0, len(order))
			for _, ident := range order {
				result = append(result, ads[ident])
			}
			return result, nil
		}
	}
}

func matches(ad map[string]any, district, ceiling int) bool {
	value := func(key string) float64 {
		f, _ := jsonval.Float(jsonval.Object(ad[key])["value"])
		return f
	}
	districtID, _ := jsonval.Float(jsonval.Object(ad["locality"])["district_id"])
	price, ok := jsonval.Float(ad["price_czk"])
	return value("category_main_cb") == 1 && value("category_type_cb") == 2 &&
		districtID == float64(district) && ok && price > 0 && price < float64(ceiling)
}
