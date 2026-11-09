package domain

import (
	"errors"
	"fmt"
	"math"
	"net/url"
	"time"

	"rentmont/internal/jsonval"
	"rentmont/internal/pyjson"
)

type Furnishing string

const (
	Furnished          Furnishing = "furnished"
	PartiallyFurnished Furnishing = "partially_furnished"
	Unfurnished        Furnishing = "unfurnished"
)

func (f Furnishing) Valid() bool {
	return f == Furnished || f == PartiallyFurnished || f == Unfurnished
}

type Stove string

const (
	GasStove              Stove = "gas"
	ElectricStove         Stove = "electric"
	PortableElectricStove Stove = "portable_electric"
)

func (s Stove) Valid() bool {
	return s == GasStove || s == ElectricStove || s == PortableElectricStove
}

type Bathing string

const (
	Shower           Bathing = "shower"
	Bathtub          Bathing = "bathtub"
	ShowerAndBathtub Bathing = "shower_and_bathtub"
)

func (b Bathing) Valid() bool {
	return b == Shower || b == Bathtub || b == ShowerAndBathtub
}

type Washer string

const (
	WasherInFlat        Washer = "in_flat"
	WasherSharedLaundry Washer = "shared_laundry"
	WasherNone          Washer = "none"
)

func (w Washer) Valid() bool {
	return w == WasherInFlat || w == WasherSharedLaundry || w == WasherNone
}

type Storage string

const (
	StorageRoom          Storage = "storage_room"
	StorageCellar        Storage = "cellar"
	StorageRoomAndCellar Storage = "both"
	StorageNone          Storage = "none"
)

func (s Storage) Valid() bool {
	return s == StorageRoom || s == StorageCellar || s == StorageRoomAndCellar || s == StorageNone
}

func (s *Storage) WithCellar() *Storage {
	result := StorageCellar
	if s != nil && (*s == StorageRoom || *s == StorageRoomAndCellar) {
		result = StorageRoomAndCellar
	}
	return &result
}

type Amenities struct {
	Washer    *Washer  `json:"washer"`
	Microwave *bool    `json:"microwave"`
	Oven      *bool    `json:"oven"`
	Stove     *Stove   `json:"stove"`
	Bathing   *Bathing `json:"bathing"`
	Storage   *Storage `json:"storage"`
	Balcony   *bool    `json:"balcony"`
}

var amenityKeys = []string{"washer", "microwave", "oven", "stove", "bathing", "storage", "balcony"}

func (a *Amenities) UnmarshalJSON(data []byte) error {
	type plain Amenities
	var v plain
	if err := strictDecode(data, &v, amenityKeys...); err != nil {
		return err
	}
	*a = Amenities(v)
	return a.Validate()
}

func (a *Amenities) Validate() error {
	if s := a.Stove; s != nil && !s.Valid() {
		return errors.New("amenities.stove: unknown value")
	}
	if b := a.Bathing; b != nil && !b.Valid() {
		return errors.New("amenities.bathing: unknown value")
	}
	if w := a.Washer; w != nil && !w.Valid() {
		return errors.New("amenities.washer: unknown value")
	}
	if s := a.Storage; s != nil && !s.Valid() {
		return errors.New("amenities.storage: unknown value")
	}
	return nil
}

const ASAP = "asap"

type Payments struct {
	RentKc       *float64 `json:"rent_kc"`
	UtilitiesKc  *float64 `json:"utilities_kc"`
	CommissionKc *float64 `json:"commission_kc"`
	DepositKc    *float64 `json:"deposit_kc"`
}

func (p *Payments) UnmarshalJSON(data []byte) error {
	type plain Payments
	var v plain
	if err := strictDecode(data, &v, "rent_kc", "utilities_kc", "commission_kc", "deposit_kc"); err != nil {
		return err
	}
	*p = Payments(v)
	return p.Normalize()
}

func (p *Payments) Normalize() error {
	for _, field := range []struct {
		name  string
		value **float64
	}{{"rent_kc", &p.RentKc}, {"utilities_kc", &p.UtilitiesKc}, {"commission_kc", &p.CommissionKc}, {"deposit_kc", &p.DepositKc}} {
		if *field.value == nil {
			continue
		}
		amount := **field.value
		if amount < 0 || math.IsInf(amount, 0) || math.IsNaN(amount) {
			return fmt.Errorf("payments.%s: expected a finite non-negative amount", field.name)
		}
		if amount == 0 {
			*field.value = nil
		}
	}
	return nil
}

type RentalDetails struct {
	Payments      Payments    `json:"payments"`
	IsRoomRent    bool        `json:"is_room_rent"`
	AreaM2        *float64    `json:"area_m2"`
	AvailableFrom *string     `json:"available_from"`
	Furnishing    *Furnishing `json:"furnishing"`
	Amenities     Amenities   `json:"amenities"`
}

func (r *RentalDetails) UnmarshalJSON(data []byte) error {
	type plain RentalDetails
	var v plain
	if err := strictDecode(data, &v, "payments!", "is_room_rent!", "area_m2", "available_from"); err != nil {
		return err
	}
	*r = RentalDetails(v)
	return r.Normalize()
}

func (r *RentalDetails) Normalize() error {
	if err := r.Payments.Normalize(); err != nil {
		return err
	}
	if a := r.AreaM2; a != nil && (*a <= 0 || math.IsInf(*a, 0) || math.IsNaN(*a)) {
		return errors.New("area_m2: expected a positive area")
	}
	if a := r.AvailableFrom; a != nil && *a != ASAP && !IsISODate(*a) {
		return errors.New("available_from: expected null, 'asap' or YYYY-MM-DD")
	}
	if f := r.Furnishing; f != nil && !f.Valid() {
		return errors.New("furnishing: unknown value")
	}
	return r.Amenities.Validate()
}

func IsISODate(value string) bool {
	parsed, err := time.Parse(time.DateOnly, value)
	return err == nil && parsed.Format(time.DateOnly) == value
}

type SourceListing struct {
	ID                 string      `json:"id"`
	Title              string      `json:"title"`
	Link               string      `json:"link"`
	CategoryMain       *int64      `json:"category_main"`
	CategoryType       *int64      `json:"category_type"`
	DistrictID         *int64      `json:"district_id"`
	APIPriceKc         *float64    `json:"api_price_kc"`
	IsRoomCategory     bool        `json:"is_room_category"`
	Furnishing         *Furnishing `json:"furnishing"`
	Cellar             *bool       `json:"cellar"`
	Balcony            *bool       `json:"balcony"`
	LocalityName       *string     `json:"locality_name"`
	MapLink            *string     `json:"map_link"`
	DistanceToCenterKm *float64    `json:"distance_to_center_km"`
	PublishedAt        *string     `json:"published_at"`
	UpdatedAt          *string     `json:"updated_at"`
	Photos             []string    `json:"photos"`
}

func (s *SourceListing) Validate() error {
	if s.ID == "" {
		return errors.New("id: must not be empty")
	}
	if !IsHTTPURL(s.Link) {
		return errors.New("link: expected an HTTP URL")
	}
	if s.MapLink != nil && !IsHTTPURL(*s.MapLink) {
		return errors.New("map_link: expected an HTTP URL")
	}
	if s.DistanceToCenterKm != nil && *s.DistanceToCenterKm < 0 {
		return errors.New("distance_to_center_km: must not be negative")
	}
	if len(s.Photos) > 10 {
		return errors.New("photos: at most 10 allowed")
	}
	if s.Photos == nil {
		s.Photos = []string{}
	}
	return nil
}

func IsHTTPURL(value string) bool {
	u, err := url.Parse(value)
	return err == nil && (u.Scheme == "http" || u.Scheme == "https") && u.Host != ""
}

type Listing struct {
	Source SourceListing `json:"source"`
	Rental RentalDetails `json:"rental"`
}

func (l Listing) Furnishing() *Furnishing {
	if l.Source.Furnishing != nil {
		return l.Source.Furnishing
	}
	return l.Rental.Furnishing
}

func (l Listing) Amenities() Amenities {
	result := l.Rental.Amenities
	if l.Source.Cellar != nil {
		result.Storage = result.Storage.WithCellar()
	}
	if l.Source.Balcony != nil {
		result.Balcony = l.Source.Balcony
	}
	return result
}

type ExclusionReason string

const (
	SourceFilter          ExclusionReason = "source_filter"
	Room                  ExclusionReason = "room"
	UnknownRent           ExclusionReason = "unknown_rent"
	RentLimit             ExclusionReason = "rent_limit"
	RentAndUtilitiesLimit ExclusionReason = "rent_and_utilities_limit"
)

type SourceRecord struct {
	Raw         map[string]any `json:"raw"`
	FirstSeenAt time.Time      `json:"first_seen_at"`
	LastSeenAt  time.Time      `json:"last_seen_at"`
}

func (r *SourceRecord) UnmarshalJSON(data []byte) error {
	type plain SourceRecord
	var v plain
	if err := strictDecode(data, &v, "raw!", "first_seen_at!", "last_seen_at!"); err != nil {
		return err
	}
	*r = SourceRecord(v)
	return r.Validate()
}

func (r *SourceRecord) Validate() error {
	if !jsonval.Truthy(r.Raw["hash_id"]) {
		return errors.New("raw: listing must have a hash_id")
	}
	return nil
}

func (r *SourceRecord) ID() string { return pyjson.Str(r.Raw["hash_id"]) }

type ExtractionRecord struct {
	GlobalHash  *string       `json:"global_hash"`
	LLMHash     *string       `json:"llm_hash"`
	Details     RentalDetails `json:"details"`
	ExtractedAt time.Time     `json:"extracted_at"`
}

func (r *ExtractionRecord) UnmarshalJSON(data []byte) error {
	type plain ExtractionRecord
	var v plain
	data, err := withoutKey(data, "content_hash")
	if err != nil {
		return err
	}
	if err := strictDecode(data, &v, "details!", "extracted_at!"); err != nil {
		return err
	}
	*r = ExtractionRecord(v)
	return nil
}

func (r *ExtractionRecord) Validate() error { return r.Details.Normalize() }

type TelegramMethod string

const (
	SendMessage    TelegramMethod = "sendMessage"
	SendPhoto      TelegramMethod = "sendPhoto"
	SendMediaGroup TelegramMethod = "sendMediaGroup"
)

func (m TelegramMethod) Valid() bool {
	return m == SendMessage || m == SendPhoto || m == SendMediaGroup
}

type TelegramDelivery struct {
	Method     TelegramMethod
	ChatID     int64
	MessageIDs []int64
	Response   map[string]any
}

func (d *TelegramDelivery) Validate() error {
	if !d.Method.Valid() {
		return errors.New("method: unknown Telegram method")
	}
	if len(d.MessageIDs) == 0 {
		return errors.New("message_ids: at least one required")
	}
	return validMessageIDs(d.MessageIDs)
}

type NotificationStatus string

const (
	Sent       NotificationStatus = "sent"
	Suppressed NotificationStatus = "suppressed"
)

type NotificationRecord struct {
	GlobalHash       *string            `json:"global_hash"`
	LLMHash          *string            `json:"llm_hash"`
	Status           NotificationStatus `json:"status"`
	SentAt           *time.Time         `json:"sent_at"`
	ChatID           *int64             `json:"chat_id"`
	MessageIDs       []int64            `json:"message_ids"`
	TelegramMethod   *TelegramMethod    `json:"telegram_method"`
	TelegramResponse map[string]any     `json:"telegram_response"`
	FormatVersion    *int64             `json:"format_version"`
	GoneAt           *time.Time         `json:"gone_at"`
	GoneNotified     bool               `json:"gone_notified"`
}

func (r *NotificationRecord) UnmarshalJSON(data []byte) error {
	type plain NotificationRecord
	var v plain
	data, err := withoutKey(data, "content_hash")
	if err != nil {
		return err
	}
	if err := strictDecode(data, &v, "status!"); err != nil {
		return err
	}
	*r = NotificationRecord(v)
	return r.Validate()
}

func (r *NotificationRecord) Validate() error {
	if r.Status != Sent && r.Status != Suppressed {
		return errors.New("status: unknown value")
	}
	if (r.Status == Sent) != (r.SentAt != nil) {
		return errors.New("sent_at: only sent notifications must have sent_at")
	}
	metadata := []bool{r.ChatID != nil, len(r.MessageIDs) > 0, r.TelegramMethod != nil, r.TelegramResponse != nil}
	some, all := false, true
	for _, present := range metadata {
		some = some || present
		all = all && present
	}
	if some && (!all || r.Status != Sent) {
		return errors.New("delivery metadata must be complete and only belongs to sent notifications")
	}
	if r.TelegramMethod != nil && !r.TelegramMethod.Valid() {
		return errors.New("telegram_method: unknown value")
	}
	if r.FormatVersion != nil && *r.FormatVersion < 1 {
		return errors.New("format_version: must be at least 1")
	}
	if r.MessageIDs == nil {
		r.MessageIDs = []int64{}
	}
	return validMessageIDs(r.MessageIDs)
}

func validMessageIDs(ids []int64) error {
	for _, id := range ids {
		if id <= 0 {
			return errors.New("message_ids: must be positive")
		}
	}
	return nil
}

const SchemaVersion = 4

type MonitorState struct {
	SchemaVersion    int      `json:"schema_version"`
	Initialized      bool     `json:"initialized"`
	CurrentSearchIDs []string `json:"current_search_ids"`
}

func NewMonitorState() MonitorState {
	return MonitorState{SchemaVersion: SchemaVersion, CurrentSearchIDs: []string{}}
}

func (s *MonitorState) UnmarshalJSON(data []byte) error {
	type plain MonitorState
	v := plain(NewMonitorState())
	if err := strictDecode(data, &v); err != nil {
		return err
	}
	*s = MonitorState(v)
	return s.Validate()
}

func (s *MonitorState) Validate() error {
	if s.SchemaVersion != SchemaVersion {
		return fmt.Errorf("schema_version: expected %d", SchemaVersion)
	}
	if s.CurrentSearchIDs == nil {
		s.CurrentSearchIDs = []string{}
	}
	return nil
}

type ScanStats struct {
	SearchResults int
	LLMCalls      int
	CacheHits     int
	Excluded      int
	Errors        int
	Listings      []Listing
}

func (s ScanStats) Succeeded() bool { return s.Errors == 0 }
