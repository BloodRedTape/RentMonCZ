package telegram

import (
	"fmt"
	"strconv"
	"strings"
	"time"

	"rentmont/internal/domain"
)

// FormatVersion must be bumped whenever the template changes; sent messages are then re-edited.
const FormatVersion = 1

const notSpecified = "не указано"

var htmlEscaper = strings.NewReplacer("&", "&amp;", "<", "&lt;", ">", "&gt;", `"`, "&quot;", "'", "&#x27;")

func escape(s string) string { return htmlEscaper.Replace(s) }

func FormatMessage(listing domain.Listing) string {
	source, rental := listing.Source, listing.Rental
	payments := rental.Payments
	area := notSpecified
	if rental.AreaM2 != nil {
		area = strconv.FormatFloat(*rental.AreaM2, 'g', 6, 64) + " м²"
	}
	title := escape(truncate(strings.Join(strings.Fields(source.Title), " "), 180))
	moveIn := calendar(rental.AvailableFrom)
	if rental.AvailableFrom != nil && *rental.AvailableFrom == domain.ASAP {
		moveIn = "как можно скорее"
	}
	location := notSpecified
	if source.LocalityName != nil {
		location = *source.LocalityName
	}
	location = escape(location)
	if source.MapLink != nil {
		location = fmt.Sprintf(`<a href="%s">%s</a>`, escape(*source.MapLink), location)
	}
	distance := "расстояние не указано"
	if source.DistanceToCenterKm != nil {
		distance = fmt.Sprintf("≈%.1f km to center", *source.DistanceToCenterKm)
	}
	return strings.Join([]string{
		fmt.Sprintf(`🏠 <a href="%s">%s</a> — %s`, escape(source.Link), title, area),
		"💰 Price: " + moneyPair(payments.RentKc, payments.UtilitiesKc),
		fmt.Sprintf("🔐 Commission/Deposit: %s + %s", money(payments.CommissionKc), money(payments.DepositKc)),
		"🔑 Move In: " + moveIn,
		"🛋 Furnished: " + furnishingLabel(listing.Furnishing()),
		fmt.Sprintf("📍 Loc: %s %s", location, distance),
		fmt.Sprintf("🕒 Uploaded %s Updated %s", calendar(source.PublishedAt), calendar(source.UpdatedAt)),
	}, "\n")
}

func furnishingLabel(f *domain.Furnishing) string {
	if f == nil {
		return notSpecified
	}
	switch *f {
	case domain.Furnished:
		return "да"
	case domain.PartiallyFurnished:
		return "частично"
	case domain.Unfurnished:
		return "нет"
	}
	return notSpecified
}

func money(value *float64) string {
	if value == nil {
		return notSpecified
	}
	return thousands(strconv.FormatFloat(*value, 'f', 0, 64)) + " Kč"
}

func moneyPair(first, second *float64) string {
	result := money(first) + " + " + money(second)
	if first != nil && second != nil {
		total := *first + *second
		result += " = " + money(&total)
	}
	return result
}

func thousands(digits string) string {
	sign := ""
	if strings.HasPrefix(digits, "-") {
		sign, digits = "-", digits[1:]
	}
	var b strings.Builder
	for i, r := range digits {
		if i > 0 && (len(digits)-i)%3 == 0 {
			b.WriteByte(',')
		}
		b.WriteRune(r)
	}
	return sign + b.String()
}

func calendar(value *string) string {
	if value == nil {
		return notSpecified
	}
	parsed, err := time.Parse(time.DateOnly, *value)
	if err != nil {
		return notSpecified
	}
	return parsed.Format("02.01.2006")
}

func truncate(s string, limit int) string {
	runes := []rune(s)
	if len(runes) <= limit {
		return s
	}
	return string(runes[:limit])
}
