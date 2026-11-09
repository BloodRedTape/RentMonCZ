package config

import (
	"errors"
	"io/fs"
	"os"
	"strconv"
	"strings"

	"github.com/joho/godotenv"
)

type Settings struct {
	OpenAIAPIKey          string
	OpenAIModel           string
	OpenAIBaseURL         string
	TelegramBotToken      string
	TelegramChatID        string
	TelegramMaxPhotos     int
	PollSeconds           int
	MaxRentKc             int
	MaxRentAndUtilitiesKc *int
	DistrictID            int
	DataDir               string
	NotifyExisting        bool
}

func Default() Settings {
	return Settings{
		OpenAIModel:       "gpt-5.4-mini",
		OpenAIBaseURL:     "https://api.openai.com/v1",
		TelegramMaxPhotos: 3,
		PollSeconds:       600,
		MaxRentKc:         13500,
		DistrictID:        72,
		DataDir:           "data",
	}
}

type ValidationError struct{ Fields []string }

func (e *ValidationError) Error() string {
	return "invalid settings: " + strings.Join(e.Fields, ", ")
}

func Load() (Settings, error) {
	if err := godotenv.Load(); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return Settings{}, errors.New("cannot parse .env")
	}
	return FromLookup(os.LookupEnv)
}

func FromLookup(lookup func(string) (string, bool)) (Settings, error) {
	s := Default()
	p := parser{lookup: lookup}
	p.secret("OPENAI_API_KEY", &s.OpenAIAPIKey)
	p.secret("TELEGRAM_BOT_TOKEN", &s.TelegramBotToken)
	p.text("TELEGRAM_CHAT_ID", &s.TelegramChatID, true)
	p.text("OPENAI_MODEL", &s.OpenAIModel, false)
	p.text("OPENAI_BASE_URL", &s.OpenAIBaseURL, false)
	p.text("DATA_DIR", &s.DataDir, false)
	p.integer("TELEGRAM_MAX_PHOTOS", &s.TelegramMaxPhotos, 0, 10)
	p.integer("POLL_SECONDS", &s.PollSeconds, 1, -1)
	p.integer("MAX_RENT_KC", &s.MaxRentKc, 1, -1)
	p.integer("DISTRICT_ID", &s.DistrictID, 1, -1)
	if _, ok := lookup("MAX_RENT_AND_UTILITIES_KC"); ok {
		var limit int
		if p.integer("MAX_RENT_AND_UTILITIES_KC", &limit, 1, -1) {
			s.MaxRentAndUtilitiesKc = &limit
		}
	}
	p.boolean("NOTIFY_EXISTING", &s.NotifyExisting)
	if len(p.invalid) > 0 {
		return Settings{}, &ValidationError{Fields: p.invalid}
	}
	s.OpenAIBaseURL = strings.TrimRight(s.OpenAIBaseURL, "/")
	return s, nil
}

type parser struct {
	lookup  func(string) (string, bool)
	invalid []string
}

func (p *parser) fail(name string) bool {
	p.invalid = append(p.invalid, strings.ToLower(name))
	return false
}

func (p *parser) secret(name string, target *string) {
	value, _ := p.lookup(name)
	if strings.TrimSpace(value) == "" {
		p.fail(name)
		return
	}
	*target = value
}

func (p *parser) text(name string, target *string, required bool) {
	value, ok := p.lookup(name)
	if !ok {
		if required {
			p.fail(name)
		}
		return
	}
	if value == "" {
		p.fail(name)
		return
	}
	*target = value
}

func (p *parser) integer(name string, target *int, minimum, maximum int) bool {
	value, ok := p.lookup(name)
	if !ok {
		return false
	}
	parsed, err := strconv.Atoi(strings.TrimSpace(value))
	if err != nil || parsed < minimum || (maximum >= 0 && parsed > maximum) {
		return p.fail(name)
	}
	*target = parsed
	return true
}

func (p *parser) boolean(name string, target *bool) {
	value, ok := p.lookup(name)
	if !ok {
		return
	}
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "1", "on", "t", "true", "y", "yes":
		*target = true
	case "0", "off", "f", "false", "n", "no":
		*target = false
	default:
		p.fail(name)
	}
}
