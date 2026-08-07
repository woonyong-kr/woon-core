package app

import (
	"testing"
	"time"
)

func TestKnowledgeWatchIntervalRejectsInvalidValuesBeforeWatching(t *testing.T) {
	tests := [][]string{
		{"watch", "--interval", "0s"},
		{"watch", "--interval", "-1s"},
		{"watch", "--interval", "invalid"},
		{"watch", "unexpected"},
	}
	for _, args := range tests {
		if _, err := knowledgeWatchInterval(args, 5*time.Second); err == nil {
			t.Fatalf("invalid watch arguments were accepted: %v", args)
		}
	}
}

func TestKnowledgeWatchIntervalAcceptsDefaultAndOverride(t *testing.T) {
	if interval, err := knowledgeWatchInterval([]string{"watch"}, 5*time.Second); err != nil || interval != 5*time.Second {
		t.Fatalf("default interval rejected: interval=%s err=%v", interval, err)
	}
	if interval, err := knowledgeWatchInterval([]string{"watch", "--interval", "250ms"}, 5*time.Second); err != nil || interval != 250*time.Millisecond {
		t.Fatalf("override interval rejected: interval=%s err=%v", interval, err)
	}
}
