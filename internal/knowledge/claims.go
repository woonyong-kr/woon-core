package knowledge

import (
	"sort"
	"strings"
	"time"
)

func evaluateClaims(claims []Claim, sources []Source) []ReviewItem {
	states := make(map[string]string, len(sources))
	for _, source := range sources {
		states[source.ID] = source.State
	}
	var items []ReviewItem
	groups := map[string][]Claim{}
	claimIDCounts := map[string]int{}
	for _, claim := range claims {
		claimIDCounts[claim.ID]++
	}
	for _, claim := range claims {
		if reason := invalidClaimReason(claim, claimIDCounts[claim.ID]); reason != "" {
			items = append(items, newReview("invalid-claim", reason, claim.SourceIDs, nil, []string{claim.ID}))
			continue
		}
		if claim.Status == "retracted" || claim.Status == "superseded" {
			continue
		}
		unavailable := []string{}
		for _, sourceID := range claim.SourceIDs {
			if states[sourceID] != "active" {
				unavailable = append(unavailable, sourceID)
			}
		}
		if len(unavailable) > 0 {
			items = append(items, newReview("claim-source-unavailable", "주장의 원본이 없거나 격리되어 기본 검색에서 제외해야 함", unavailable, nil, []string{claim.ID}))
		}
		key := normalize(claim.Subject) + "\x00" + normalize(claim.Predicate) + "\x00" + normalize(claim.Scope)
		groups[key] = append(groups[key], claim)
	}
	for _, group := range groups {
		for left := 0; left < len(group); left++ {
			for right := left + 1; right < len(group); right++ {
				a, b := group[left], group[right]
				if normalize(a.Value) == normalize(b.Value) || !intervalsOverlap(a, b) || supersedes(a, b) || supersedes(b, a) {
					continue
				}
				sourceIDs := append(append([]string{}, a.SourceIDs...), b.SourceIDs...)
				items = append(items, newReview("claim-conflict", "같은 범위와 유효 시점의 주장이 다르므로 둘 다 기본 검색에서 차단함", sourceIDs, nil, []string{a.ID, b.ID}))
			}
		}
	}
	return items
}

func invalidClaimReason(claim Claim, idCount int) string {
	if claim.ID == "" || claim.Subject == "" || claim.Predicate == "" || claim.Value == "" || claim.Scope == "" {
		return "claim에 id, subject, predicate, value와 scope가 모두 필요함"
	}
	if idCount != 1 {
		return "claim id가 중복되어 어느 주장을 가리키는지 결정할 수 없음"
	}
	if len(claim.SourceIDs) == 0 {
		return "근거 source가 없는 claim은 검색에 사용할 수 없음"
	}
	if !contains([]string{"candidate", "active", "superseded", "retracted"}, claim.Status) {
		return "지원하지 않는 claim status임"
	}
	for _, value := range []string{claim.ValidFrom, claim.ValidUntil} {
		if value == "" {
			continue
		}
		if _, err := time.Parse("2006-01-02", value); err != nil {
			return "valid_from과 valid_until은 YYYY-MM-DD 형식이어야 함"
		}
	}
	if claim.ValidFrom != "" && claim.ValidUntil != "" {
		from, _ := time.Parse("2006-01-02", claim.ValidFrom)
		until, _ := time.Parse("2006-01-02", claim.ValidUntil)
		if until.Before(from) {
			return "valid_until은 valid_from보다 빠를 수 없음"
		}
	}
	return ""
}

func normalize(value string) string {
	return strings.Join(strings.Fields(strings.ToLower(value)), " ")
}

func intervalsOverlap(a, b Claim) bool {
	aStart := parseBoundary(a.ValidFrom, time.Time{})
	bStart := parseBoundary(b.ValidFrom, time.Time{})
	aEnd := parseBoundary(a.ValidUntil, time.Date(9999, 12, 31, 0, 0, 0, 0, time.UTC))
	bEnd := parseBoundary(b.ValidUntil, time.Date(9999, 12, 31, 0, 0, 0, 0, time.UTC))
	return !aEnd.Before(bStart) && !bEnd.Before(aStart)
}

func parseBoundary(value string, fallback time.Time) time.Time {
	parsed, err := time.Parse("2006-01-02", value)
	if err != nil {
		return fallback
	}
	return parsed
}

func supersedes(a, b Claim) bool {
	values := append([]string(nil), a.Supersedes...)
	sort.Strings(values)
	index := sort.SearchStrings(values, b.ID)
	return index < len(values) && values[index] == b.ID
}
