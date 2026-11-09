package sreality

import (
	"errors"
	"fmt"
	"math"
	"net/url"
	"strconv"
	"strings"

	"golang.org/x/text/unicode/norm"

	"rentmont/internal/domain"
	"rentmont/internal/jsonval"
	"rentmont/internal/pyjson"
)

const (
	brnoCenterLat = 49.1951
	brnoCenterLon = 16.6080
	earthRadiusKm = 6371.0088
	sdnRendition  = "res,1200,1200,1|shr,,20|jpg,80"
	MaxPhotos     = 10
)

func ToSourceListing(detail map[string]any, maxPhotos int) (domain.SourceListing, error) {
	locality := jsonval.Object(detail["locality"])
	if locality == nil && jsonval.Truthy(detail["locality"]) {
		return domain.SourceListing{}, errors.New("locality: expected an object")
	}
	title, ok := detail["advert_name"].(string)
	if !ok {
		return domain.SourceListing{}, errors.New("advert_name: expected a string")
	}
	categoryMain, err := jsonval.OptInt(jsonval.Object(detail["category_main_cb"])["value"])
	if err != nil {
		return domain.SourceListing{}, fmt.Errorf("category_main_cb: %w", err)
	}
	categoryType, err := jsonval.OptInt(jsonval.Object(detail["category_type_cb"])["value"])
	if err != nil {
		return domain.SourceListing{}, fmt.Errorf("category_type_cb: %w", err)
	}
	district, err := jsonval.OptInt(locality["district_id"])
	if err != nil {
		return domain.SourceListing{}, fmt.Errorf("district_id: %w", err)
	}
	price, err := jsonval.OptFloat(detail["price_czk"])
	if err != nil {
		return domain.SourceListing{}, fmt.Errorf("price_czk: %w", err)
	}
	name, err := localityName(locality)
	if err != nil {
		return domain.SourceListing{}, err
	}
	photos, err := ListingPhotos(detail, maxPhotos)
	if err != nil {
		return domain.SourceListing{}, err
	}
	distance := DistanceToCenterKm(locality)
	var mapLink *string
	if distance != nil {
		link := fmt.Sprintf("https://www.google.com/maps/search/?api=1&query=%s,%s",
			pyjson.Str(locality["gps_lat"]), pyjson.Str(locality["gps_lon"]))
		mapLink = &link
	}
	published, updated := ListingDates(detail)
	source := domain.SourceListing{
		ID:                 pyjson.Str(detail["hash_id"]),
		Title:              title,
		Link:               ListingLink(detail),
		CategoryMain:       categoryMain,
		CategoryType:       categoryType,
		DistrictID:         district,
		APIPriceKc:         price,
		IsRoomCategory:     IsRoomCategory(detail),
		Furnishing:         ListingFurnishing(detail),
		Cellar:             presentFlag(detail, "cellar"),
		Balcony:            presentFlag(detail, "balcony", "loggia", "terrace"),
		LocalityName:       name,
		MapLink:            mapLink,
		DistanceToCenterKm: distance,
		PublishedAt:        published,
		UpdatedAt:          updated,
		Photos:             photos,
	}
	return source, source.Validate()
}

func localityName(locality map[string]any) (*string, error) {
	for _, key := range []string{"citypart", "quarter", "city", "district"} {
		value := locality[key]
		if !jsonval.Truthy(value) {
			continue
		}
		name, ok := value.(string)
		if !ok {
			return nil, fmt.Errorf("locality.%s: expected a string", key)
		}
		return &name, nil
	}
	return nil, nil
}

func DistanceToCenterKm(locality map[string]any) *float64 {
	lat, okLat := jsonval.Float(locality["gps_lat"])
	lon, okLon := jsonval.Float(locality["gps_lon"])
	if !okLat || !okLon {
		return nil
	}
	for _, v := range []float64{lat, lon} {
		if math.IsInf(v, 0) || math.IsNaN(v) {
			return nil
		}
	}
	if lat < -90 || lat > 90 || lon < -180 || lon > 180 || (lat == 0 && lon == 0) {
		return nil
	}
	rad := math.Pi / 180
	latitude, centerLatitude := lat*rad, brnoCenterLat*rad
	deltaLat := latitude - centerLatitude
	deltaLon := (lon - brnoCenterLon) * rad
	haversine := math.Pow(math.Sin(deltaLat/2), 2) +
		math.Cos(latitude)*math.Cos(centerLatitude)*math.Pow(math.Sin(deltaLon/2), 2)
	distance := 2 * earthRadiusKm * math.Asin(math.Sqrt(min(1, max(0, haversine))))
	rounded, _ := strconv.ParseFloat(strconv.FormatFloat(distance, 'f', 1, 64), 64)
	return &rounded
}

func PhotoDownloadURL(raw string) string {
	u, err := url.Parse(raw)
	if err != nil {
		return raw
	}
	host := strings.ToLower(u.Hostname())
	if (host == "sdn.cz" || strings.HasSuffix(host, ".sdn.cz")) && !u.Query().Has("fl") {
		param := "fl=" + url.QueryEscape(sdnRendition)
		if u.RawQuery == "" {
			u.RawQuery = param
		} else {
			u.RawQuery += "&" + param
		}
		return u.String()
	}
	return raw
}

func ListingPhotos(detail map[string]any, maxPhotos int) ([]string, error) {
	if maxPhotos < 0 || maxPhotos > MaxPhotos {
		return nil, errors.New("expected a photo limit between 0 and 10")
	}
	photos := []string{}
	images, _ := detail["advert_images"].([]any)
	if maxPhotos == 0 {
		return photos, nil
	}
	seen := map[string]bool{}
	for _, image := range images {
		normalized, ok := imageURL(image)
		if !ok || seen[normalized] {
			continue
		}
		seen[normalized] = true
		photos = append(photos, normalized)
		if len(photos) == maxPhotos {
			break
		}
	}
	return photos, nil
}

const floorPlanKind = 4

// VisionPhotos returns real photos for image inspection, skipping floor plans and AI-edited images.
func VisionPhotos(detail map[string]any) []string {
	photos := []string{}
	images, _ := detail["advert_images"].([]any)
	seen := map[string]bool{}
	for _, image := range images {
		m, ok := image.(map[string]any)
		if !ok {
			continue
		}
		if kind, ok := jsonval.Int(m["kind"]); ok && kind == floorPlanKind {
			continue
		}
		if edited, _ := m["ai_edited"].(bool); edited {
			continue
		}
		normalized, ok := imageURL(m)
		if !ok || seen[normalized] {
			continue
		}
		seen[normalized] = true
		photos = append(photos, normalized)
	}
	return photos
}

func imageURL(image any) (string, bool) {
	value := image
	if m, ok := image.(map[string]any); ok {
		value = m["url"]
	}
	raw, ok := value.(string)
	if !ok {
		return "", false
	}
	if strings.HasPrefix(raw, "//") {
		raw = "https:" + raw
	}
	normalized, ok := normalizeURL(raw)
	if !ok {
		return "", false
	}
	return PhotoDownloadURL(normalized), true
}

func normalizeURL(raw string) (string, bool) {
	u, err := url.Parse(strings.TrimSpace(raw))
	if err != nil || u.Host == "" || u.User != nil {
		return "", false
	}
	u.Scheme = strings.ToLower(u.Scheme)
	if u.Scheme != "http" && u.Scheme != "https" {
		return "", false
	}
	u.Host = strings.ToLower(u.Host)
	if u.Path == "" {
		u.Path = "/"
	}
	return u.String(), true
}

func ListingDates(detail map[string]any) (published, updated *string) {
	parse := func(key string) *string {
		value, ok := detail[key].(string)
		if !ok || !domain.IsISODate(value) {
			return nil
		}
		return &value
	}
	return parse("since"), parse("edited")
}

func IsRoomCategory(detail map[string]any) bool {
	name := ""
	if value, ok := jsonval.Object(detail["category_sub_cb"])["name"]; ok {
		name = pyjson.Str(value)
	}
	name = strings.ToLower(strings.TrimSpace(name))
	return name == "pokoj" || name == "pokoje"
}

// presentFlag trusts only true: Sreality reports unfilled checkboxes as false.
func presentFlag(detail map[string]any, keys ...string) *bool {
	for _, key := range keys {
		if value, ok := detail[key].(bool); ok && value {
			return &value
		}
	}
	return nil
}

func ListingFurnishing(detail map[string]any) *domain.Furnishing {
	furnished, ok := detail["furnished"].(map[string]any)
	if !ok {
		return nil
	}
	var code string
	switch value := furnished["value"].(type) {
	case string:
		code = value
	default:
		i, ok := jsonval.Int(value)
		if !ok {
			return nil
		}
		code = strconv.FormatInt(i, 10)
	}
	var result domain.Furnishing
	switch code {
	case "1":
		result = domain.Furnished
	case "2":
		result = domain.Unfurnished
	case "3":
		result = domain.PartiallyFurnished
	default:
		return nil
	}
	return &result
}

func ListingLink(ad map[string]any) string {
	locality := jsonval.Object(ad["locality"])
	var parts []string
	for _, key := range []string{"city_seo_name", "citypart_seo_name", "street_seo_name"} {
		if s, ok := locality[key].(string); ok && s != "" {
			parts = append(parts, s)
		}
	}
	place := strings.Join(parts, "-")
	if place == "" {
		place = "-"
	}
	layout := "-"
	if name, ok := jsonval.Object(ad["category_sub_cb"])["name"].(string); ok {
		layout = name
	}
	layout = strings.ReplaceAll(asciiFold(strings.ToLower(layout)), " ", "-")
	return fmt.Sprintf("https://www.sreality.cz/detail/pronajem/byt/%s/%s/%s", layout, place, pyjson.Str(ad["hash_id"]))
}

func asciiFold(s string) string {
	var b strings.Builder
	for _, r := range norm.NFKD.String(s) {
		if r < 128 {
			b.WriteRune(r)
		}
	}
	return b.String()
}
