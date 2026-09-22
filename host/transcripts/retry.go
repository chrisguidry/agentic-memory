package transcripts

import (
	"context"
	"path/filepath"
	"sync"
	"time"
)

// The backoff a refused file waits through. It starts short, because the
// common refusal is a service restarting, and it caps well inside the time a
// person leaves between sessions.
const (
	FirstWait   = 5 * time.Second
	LongestWait = 5 * time.Minute
)

// attempt is a file the service refused, and when to offer it again.
type attempt struct {
	request Request
	wait    time.Duration
	due     time.Time
	mu      sync.Mutex
}

// failed records that the service refused a file. The bastion is long-lived,
// so the file stays in memory and is offered again until the service takes it.
func (s *Shipper) failed(request Request, cause error) {
	stored, _ := s.pending.LoadOrStore(request.Path, &attempt{request: request})
	found := stored.(*attempt)
	found.mu.Lock()
	defer found.mu.Unlock()
	found.request = request
	if found.wait == 0 {
		found.wait = FirstWait
	} else if found.wait < LongestWait {
		found.wait = min(found.wait*2, LongestWait)
	}
	found.due = time.Now().Add(found.wait)
	s.logf("%s: behind, retrying in %s: %v", filepath.Base(request.Path), found.wait, cause)
}

func (s *Shipper) delivered(path string) {
	s.pending.Delete(path)
}

// Behind is how many files the service has not taken yet.
func (s *Shipper) Behind() int {
	found := 0
	s.pending.Range(func(any, any) bool {
		found++
		return true
	})
	return found
}

// RetryDue ships every refused file whose wait has passed. A file the service
// refuses again gets a longer wait.
func (s *Shipper) RetryDue(ctx context.Context, now time.Time) {
	var due []Request
	s.pending.Range(func(_, stored any) bool {
		found := stored.(*attempt)
		found.mu.Lock()
		if !found.due.After(now) {
			due = append(due, found.request)
		}
		found.mu.Unlock()
		return true
	})
	for _, request := range due {
		s.Ship(ctx, request)
	}
}

// Retry offers refused files to the service until it takes them, for as long
// as the bastion runs.
func (s *Shipper) Retry(ctx context.Context) {
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case now := <-ticker.C:
			s.RetryDue(ctx, now)
		}
	}
}
